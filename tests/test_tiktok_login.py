"""Task 3: TikTok 网页登录、资料解析、登录态体检与接入面。

浏览器全部以桩代替,不发起真实网络请求。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.db as db
import app.engine.monitor as monitor
import app.main as main
from app.browser.identity import Identity
from app.browser.manager import cookie_string_to_state
from app.config import Config
from app.models import DouyinAccount
from app.platforms import registry as pf
from app.platforms.tiktok import (
    extract_self_user,
    fetch_tiktok_self_profile,
    interactive_tiktok_login,
    parse_tiktok_self_user,
    tt_login_ready,
)
from test_project_optimizations import local_project, store  # noqa: F401

TT_USER = {
    "id": "6800000000000000001",
    "uniqueId": "creator",
    "nickname": "Creator Name",
    "secUid": "MS4wLjABAAAA-secuid-fixture",
    "avatarLarger": "https://example.com/l.jpg",
    "followerCount": 12,
    "followingCount": 3,
    "videoCount": 0,
}
UNIVERSAL_PAYLOAD = {
    "__DEFAULT_SCOPE__": {"web.app.app": {"userInfo": {"user": TT_USER}}}}
LOGGED_IN_STATE = {"cookies": [
    {"name": "sessionid_ss", "value": "fixture", "domain": ".tiktok.com"}],
    "origins": []}


def run(coro):
    return asyncio.run(coro)


# ── 纯函数:登录判据与资料解析 ──

def test_login_ready_requires_strong_cookie_and_non_login_url():
    # ttwid/msToken 是游客态 Cookie,不能算登录
    assert not tt_login_ready({"ttwid", "msToken", "odin_tt"},
                             "https://www.tiktok.com/")
    assert tt_login_ready({"sessionid_ss", "ttwid"},
                          "https://www.tiktok.com/foryou")
    assert tt_login_ready({"sid_guard"}, "https://www.tiktok.com/@creator")
    # 强 Cookie 刚写入但仍停在登录/护照域:不算完成
    assert not tt_login_ready({"sessionid"}, "https://www.tiktok.com/login")
    assert not tt_login_ready({"sessionid_ss"},
                              "https://www.tiktok.com/passport/web/index/")
    assert not tt_login_ready(set(), "")


def test_extract_and_parse_self_user():
    assert extract_self_user(UNIVERSAL_PAYLOAD)["uniqueId"] == "creator"
    assert extract_self_user({"__DEFAULT_SCOPE__": {}}) == {}
    assert extract_self_user(None) == {}
    guest = {"__DEFAULT_SCOPE__": {
        "web.app.app": {"userInfo": {"user": {}}}}}
    assert extract_self_user(guest) == {}

    p = parse_tiktok_self_user(TT_USER)
    assert p["nickname"] == "Creator Name"
    assert p["douyin_id"] == "creator"
    assert p["sec_uid"] == "MS4wLjABAAAA-secuid-fixture"
    assert p["avatar"] == "https://example.com/l.jpg"
    assert (p["follower_count"], p["following_count"], p["aweme_count"]) == (
        12, 3, 0)  # videoCount=0 是合法值,不得被丢弃
    assert parse_tiktok_self_user({}) == {}
    assert parse_tiktok_self_user(None) == {}
    # 只有昵称也成立(nickname 兜底为 uniqueId)
    p2 = parse_tiktok_self_user({"uniqueId": "u2"})
    assert p2["nickname"] == "u2" and p2["follower_count"] == 0


# ── 交互式登录(浏览器桩) ──

class _Page:
    def __init__(self, cookie_sets, urls, eval_results):
        self._cookie_sets = cookie_sets
        self._urls = urls
        self._eval_results = eval_results
        self._poll = 0
        self.closed = False
        self.gotos = []

    def is_closed(self):
        return self.closed

    @property
    def url(self):
        idx = min(self._poll, len(self._urls) - 1)
        return self._urls[idx]

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, js):
        val = self._eval_results[min(self._poll,
                                     len(self._eval_results) - 1)]
        return val

    async def bring_to_front(self):
        return None

    def advance(self):
        self._poll += 1


class _Ctx:
    def __init__(self, page):
        self.page = page
        self.cleared = False
        self.closed = False

    async def clear_cookies(self):
        self.cleared = True

    async def new_page(self):
        return self.page

    async def cookies(self):
        return [{"name": n, "value": "v"}
                for n in self.page._cookie_sets[min(
                    self.page._poll, len(self.page._cookie_sets) - 1)]]

    async def storage_state(self):
        return LOGGED_IN_STATE

    async def close(self):
        self.closed = True


class _Mgr:
    def __init__(self, page):
        self.ctx = _Ctx(page)

    async def open_headed(self, identity):
        return self.ctx


async def _drive_login(page):
    """复刻登录轮询:每 0.5s 推进一步;用 sleep 桩避免真实等待。"""
    orig_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        page.advance()
        return None

    mgr = _Mgr(page)
    identity = SimpleNamespace(observed_login_profile=None)
    with patch("app.platforms.tiktok.login.asyncio.sleep", fast_sleep):
        return await interactive_tiktok_login(
            mgr, identity, timeout_seconds=300), identity, mgr.ctx


def test_interactive_login_success_persists_state_and_profile():
    page = _Page(
        cookie_sets=[{"ttwid"}, {"ttwid"},
                      {"ttwid", "sessionid_ss", "sid_tt"}],
        urls=["https://www.tiktok.com/login",
              "https://www.tiktok.com/login",
              "https://www.tiktok.com/foryou"],
        eval_results=[None, None, TT_USER])
    (ok, state_json, nickname), identity, ctx = run(_drive_login(page))
    assert ok and nickname == "Creator Name"
    assert json.loads(state_json)["cookies"][0]["name"] == "sessionid_ss"
    assert identity.observed_login_profile["sec_uid"] == \
        "MS4wLjABAAAA-secuid-fixture"
    assert identity.observed_login_profile["douyin_id"] == "creator"
    assert ctx.closed


def test_interactive_login_force_reauth_clears_cookies():
    page = _Page(
        cookie_sets=[{"sessionid_ss"}],
        urls=["https://www.tiktok.com/foryou"],
        eval_results=[TT_USER])
    mgr = _Mgr(page)
    identity = SimpleNamespace(observed_login_profile=None)

    async def fast_sleep(_seconds):
        page.advance()

    with patch("app.platforms.tiktok.login.asyncio.sleep", fast_sleep):
        ok, _, _ = run(interactive_tiktok_login(
            mgr, identity, force_reauth=True))
    assert ok
    assert mgr.ctx.cleared  # 重登必须先清旧会话


def test_interactive_login_window_closed_means_not_logged_in():
    page = _Page(
        cookie_sets=[{"ttwid"}],
        urls=["https://www.tiktok.com/login"],
        eval_results=[None])
    page.closed = True
    (ok, state_json, nickname), _, ctx = run(_drive_login(page))
    assert not ok and state_json == "" and nickname == ""
    assert ctx.closed


def test_strong_cookie_but_stuck_on_login_page_does_not_complete():
    # timeout=0:轮询体一次都不执行,直接判定未成功(模拟超时/未离开登录页)
    page = _Page(
        cookie_sets=[{"sessionid_ss"}],
        urls=["https://www.tiktok.com/login"],
        eval_results=[None])
    mgr = _Mgr(page)
    identity = SimpleNamespace(observed_login_profile=None)
    ok, state_json, _ = run(interactive_tiktok_login(
        mgr, identity, timeout_seconds=0))
    assert not ok and state_json == ""


# ── 体检 ──

class _ProbePage:
    def __init__(self, cookie_names, user, goto_raises=None):
        self.context = SimpleNamespace(cookies=self._cookies)
        self._cookie_names = cookie_names
        self._user = user
        self._goto_raises = goto_raises
        self.url = "https://www.tiktok.com/foryou"
        self.closed = False

    async def _cookies(self):
        return [{"name": n, "value": "v"} for n in self._cookie_names]

    async def goto(self, url, **kwargs):
        if self._goto_raises:
            raise self._goto_raises

    async def wait_for_timeout(self, ms):
        return None

    async def evaluate(self, js):
        return self._user

    async def close(self):
        self.closed = True


class _ProbeMgr:
    def __init__(self, page):
        self.page = page

    async def new_page(self, identity, block_media=False):
        return self.page


def test_profile_fetch_paths():
    # 无强 Cookie:直接判失效,不导航
    page = _ProbePage({"ttwid", "msToken"}, None)
    user, err = run(fetch_tiktok_self_profile(
        _ProbeMgr(page), SimpleNamespace()))
    assert user == {} and err == "logged_out"
    assert page.gotos == [] if hasattr(page, "gotos") else True
    assert page.closed

    # 强 Cookie + 首页有本人资料:活
    page2 = _ProbePage({"sessionid_ss", "ttwid"}, TT_USER)
    user, err = run(fetch_tiktok_self_profile(
        _ProbeMgr(page2), SimpleNamespace()))
    assert err == "" and user["uniqueId"] == "creator"

    # 强 Cookie 但全局状态无本人:失效
    page3 = _ProbePage({"sessionid_ss"}, None)
    user, err = run(fetch_tiktok_self_profile(
        _ProbeMgr(page3), SimpleNamespace()))
    assert user == {} and err == "logged_out"

    # 导航异常:不可误判为失效
    page4 = _ProbePage({"sessionid_ss"}, None,
                       goto_raises=TimeoutError("slow proxy"))
    user, err = run(fetch_tiktok_self_profile(
        _ProbeMgr(page4), SimpleNamespace()))
    assert user == {} and err.startswith("goto:") and err != "logged_out"


# ── 注册/配置面 ──

def test_tiktok_enabled_with_expected_capability_surface():
    spec = pf.get("tiktok")
    assert spec.enabled is True
    assert pf.has_cap("tiktok", pf.COOKIE_LOGIN)
    # Task 9 起开放创作者中心网页发布
    assert pf.has_cap("tiktok", pf.PUBLISH)
    # Task 6 起开放作品监控,Task 7 评论监控,Task 8 关键词采集,Task 9 发布
    assert pf.has_cap("tiktok", pf.WORK_MONITOR)
    assert pf.has_cap("tiktok", pf.COMMENT_MONITOR)
    assert pf.has_cap("tiktok", pf.KEYWORD_COLLECTION)
    assert "tiktok" in pf.cookie_login_platforms()
    assert "tiktok" in [s.key for s in pf.all_specs()]


def test_cookie_string_to_state_tiktok_domain():
    state = json.loads(cookie_string_to_state(
        "sessionid=abc; ttwid=xyz", platform="tiktok"))
    assert {c["domain"] for c in state["cookies"]} == {".tiktok.com"}


def test_overview_api_accepts_tiktok(local_project):
    async def scenario():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            ok = await client.get("/api/overview/summary",
                                  params={"platform": "tiktok"})
            bad = await client.get("/api/overview/summary",
                                   params={"platform": "future-x"})
            return ok, bad
    ok, bad = run(scenario())
    assert ok.status_code == 200 and ok.json()["platform"] == "tiktok"
    assert bad.status_code == 422


# ── 资料补全(登录成功后)与周期体检接入 ──

def test_enrich_persists_tiktok_profile_and_invalid_state(local_project):
    acc_id = store(DouyinAccount(
        platform="tiktok", status="active",
        storage_state=json.dumps(LOGGED_IN_STATE)))
    browser_stub = SimpleNamespace(identity_for=lambda a: object())
    with (patch.object(main, "browser", browser_stub),
          patch.object(main, "fetch_tiktok_self_profile",
                       AsyncMock(return_value=(TT_USER, "")))):
        status = run(main._enrich_account_profile(
            acc_id, json.dumps(LOGGED_IN_STATE)))
    assert status == "ok"
    with db.get_session() as s:
        acc = s.get(DouyinAccount, acc_id)
        assert acc.nickname == "Creator Name"
        assert acc.douyin_id == "creator"
        assert acc.sec_uid == "MS4wLjABAAAA-secuid-fixture"
        assert acc.status == "active"
        assert acc.follower_count == 12 and acc.aweme_count == 0

    logged_out = AsyncMock(return_value=({}, "logged_out"))
    with (patch.object(main, "browser", browser_stub),
          patch.object(main, "fetch_tiktok_self_profile", logged_out)):
        status = run(main._enrich_account_profile(
            acc_id, json.dumps(LOGGED_IN_STATE)))
    assert status == "invalid"
    with db.get_session() as s:
        assert s.get(DouyinAccount, acc_id).status == "invalid"


def test_health_probe_marks_tiktok_account_active_or_invalid(
        local_project, monkeypatch):
    cfg = Config()
    cfg.engine.verify_proxy_region = False
    cfg.engine.work_health_stat_snapshots = False
    acc_id = store(DouyinAccount(
        platform="tiktok", status="active", nickname="旧名",
        storage_state=json.dumps(LOGGED_IN_STATE)))
    engine = monitor.MonitorEngine(cfg, local_project.browser)
    monkeypatch.setattr(engine, "_verify_proxy_region", AsyncMock())
    # 本用例聚焦资料探测分支;网络闸门已在 Task 2 单测覆盖,这里放行
    monkeypatch.setattr(engine, "_platform_network_gate",
                        AsyncMock(return_value=None))
    identity = SimpleNamespace(timezone_id="America/New_York")
    probe = (acc_id, "tiktok", json.dumps(LOGGED_IN_STATE), "",
             "http://proxy:8080", identity)

    monkeypatch.setattr(monitor, "fetch_tiktok_self_profile",
                        AsyncMock(return_value=(TT_USER, "")))
    result = run(engine._probe_account_health(probe))
    assert result["ok"] and not result.get("error")
    with db.get_session() as s:
        acc = s.get(DouyinAccount, acc_id)
        assert acc.status == "active" and acc.nickname == "Creator Name"
        assert acc.sec_uid == "MS4wLjABAAAA-secuid-fixture"

    # 失效判定用独立账号:首个账号在记录 logged_out 失败后会被风控挂起,
    # 真实周期中后续探测走恢复路径,不在本用例范围。
    out_id = store(DouyinAccount(
        platform="tiktok", status="active",
        storage_state=json.dumps(LOGGED_IN_STATE)))
    out_probe = (out_id, "tiktok", json.dumps(LOGGED_IN_STATE), "",
                 "http://proxy:8080", identity)
    monkeypatch.setattr(monitor, "fetch_tiktok_self_profile",
                        AsyncMock(return_value=({}, "logged_out")))
    result = run(engine._probe_account_health(out_probe))
    assert not result["ok"]
    with db.get_session() as s:
        assert s.get(DouyinAccount, out_id).status == "invalid"
