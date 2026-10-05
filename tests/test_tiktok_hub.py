"""Task 5: TikTok 本账号作品 / 关注 / 粉丝分页同步单测。

覆盖:作品归一化、item_list 分页(hasMore 游标)、协作式取消、空列表语义、
登录失效/导航异常;以及 account_hub.fetch_account_works / fetch_follows
对 platform="tiktok" 的分派与独立列表路由。
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser import account_hub
from app.platforms import registry as pf
from app.platforms.tiktok import hub as tthub


def run(coro):
    return asyncio.run(coro)


# ── 作品归一化 ────────────────────────────────────────────────────────

def work_item(item_id="7300000000000000001", kind="video"):
    base = {
        "id": item_id,
        "desc": "post " + item_id,
        "createTime": 1700000000,
        "statsV2": {"diggCount": "1.2K", "commentCount": "34",
                    "collectCount": "5", "shareCount": "2",
                    "playCount": "1.5M"},
    }
    if kind == "video":
        base["video"] = {"duration": 10,
                         "cover": ["https://cdn/cover.jpg"]}
    else:
        base["imagePost"] = {
            "cover": {"imageURL": {"urlList": ["https://cdn/pcover.webp"]}},
            "images": [{"imageURL": {"urlList": ["https://cdn/i1.webp"]}}],
        }
    return base


def test_norm_tiktok_work_video_counts_and_cover():
    w = tthub.norm_tiktok_work(work_item())
    assert w["item_id"] == "7300000000000000001"
    assert w["media_type"] == "video"
    assert w["cover_url"] == "https://cdn/cover.jpg"
    assert w["like_count"] == 1200
    assert w["comment_count"] == 34
    assert w["collect_count"] == 5
    assert w["play_count"] == 1_500_000
    assert w["create_time"] == 1700000000


def test_norm_tiktok_work_images_and_rejects_bad_id():
    w = tthub.norm_tiktok_work(work_item(kind="images"))
    assert w["media_type"] == "images"
    assert w["cover_url"] == "https://cdn/pcover.webp"
    assert tthub.norm_tiktok_work({"id": "abc", "video": {}}) is None
    assert tthub.norm_tiktok_work({"desc": "no id"}) is None
    assert tthub.norm_tiktok_work(None) is None


# ── 浏览器桩 ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, url, payload, rtype="xhr", status=200):
        self.url = url
        self.status = status
        self.request = SimpleNamespace(resource_type=rtype)
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HubPage:
    """作品/关注页桩:导航与滚动时按脚本吐出 XHR 响应。"""

    def __init__(self, *, goto_urls=None, goto_payloads=None,
                 scroll_payloads=None, current_url=None,
                 goto_error=None, hydration=None, login_on_scroll=False):
        self._goto_payloads = list(goto_payloads or [])
        self._scroll_payloads = list(scroll_payloads or [])
        self._current_url = current_url
        self._goto_error = goto_error
        self._hydration = hydration
        self._login_on_scroll = login_on_scroll
        self.handlers = []
        self.closed = False
        self.visited_urls = []
        self.mouse = SimpleNamespace(wheel=AsyncMock())

    def on(self, event, handler):
        if event == "response":
            self.handlers.append(handler)

    async def _emit(self, payload, url=None):
        if payload is None:
            return
        if isinstance(payload, tuple):       # (url, payload) 显式指定来源
            url, payload = payload
        resp = _Resp(url or self._default_api_url(), payload)
        for handler in list(self.handlers):
            outcome = handler(resp)
            if inspect.isawaitable(outcome):
                await outcome

    def _default_api_url(self):  # 子类/场景可覆盖
        return "https://www.tiktok.com/api/post/item_list/?aid=1988"

    async def goto(self, url, **kwargs):
        self.visited_urls.append(url)
        if self._current_url is None:
            self._current_url = url
        if self._goto_error:
            raise self._goto_error
        for payload in self._goto_payloads:
            await self._emit(payload)

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)

    async def evaluate(self, js, *args):
        if "scrollTo" in js:
            if self._login_on_scroll:
                self._current_url = "https://www.tiktok.com/login"
            payload = self._scroll_payloads.pop(0) \
                if self._scroll_payloads else None
            await self._emit(payload)
            return None
        if self._hydration is not None:
            hyd = self._hydration
            self._hydration = None
            return hyd
        return None

    @property
    def url(self):
        return self._current_url or ""

    async def close(self):
        self.closed = True


class _HubMgr:
    def __init__(self, page):
        self._page = page

    async def new_page(self, identity, block_media=True):
        return self._page


# ── fetch_tiktok_works:分页 / 游标 / 取消 / 空 / 异常 ────────────────

def page1(items, has_more=True):
    return {"statusCode": 0, "itemList": items, "hasMore": has_more,
            "cursor": "20"}


def test_works_paginates_until_has_more_false():
    page = _HubPage(
        goto_payloads=[page1([work_item("1"), work_item("2")])],
        scroll_payloads=[page1([work_item("3")], has_more=False)],
        current_url="https://www.tiktok.com/@creator")
    progress = []
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator",
        max_scrolls=10, settle_ms=1,
        progress=lambda c: progress.append(c)))
    assert err == ""
    assert sorted(i["id"] for i in items) == ["1", "2", "3"]
    assert page.closed
    assert any(c.get("pages") == 2 for c in progress)
    assert any(c.get("fetched") == 3 for c in progress)


def test_works_valid_empty_list():
    page = _HubPage(
        goto_payloads=[{"statusCode": 0, "itemList": [],
                        "hasMore": False, "cursor": "0"}],
        current_url="https://www.tiktok.com/@newbie")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "newbie",
        max_scrolls=3, settle_ms=1))
    assert items == [] and err == "empty"


def test_works_cancel_before_next_scroll_keeps_partial():
    pending = page1([work_item("3")])
    page = _HubPage(
        goto_payloads=[page1([work_item("1"), work_item("2")])],
        scroll_payloads=[pending],
        current_url="https://www.tiktok.com/@creator")

    def canceled():
        return True        # 第一轮滚动前即取消:第二页不应被请求

    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator",
        max_scrolls=10, settle_ms=1, is_canceled=canceled))
    assert err == "canceled"
    assert {i["id"] for i in items} == {"1", "2"}
    assert len(page._scroll_payloads) == 1     # 未再触发分页


def test_works_stagnant_then_timeout_without_confirmation():
    page = _HubPage(
        goto_payloads=[page1([work_item("1")], has_more=True)],
        scroll_payloads=[],
        current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "@creator",
        max_scrolls=10, settle_ms=1))
    assert len(items) == 1 and err == ""


def test_works_no_data_at_all_is_timeout():
    page = _HubPage(goto_payloads=[],
                    current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator",
        max_scrolls=6, settle_ms=1))
    assert items == [] and err == "timeout"


def test_works_hydration_feeds_first_page():
    page = _HubPage(
        goto_payloads=[], hydration=[work_item("9")],
        current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator",
        max_scrolls=6, settle_ms=1))
    assert err == "" and [i["id"] for i in items] == ["9"]


def test_works_login_redirect_and_goto_error_and_missing_uid():
    login_page = _HubPage(goto_payloads=[],
                          current_url="https://www.tiktok.com/login?lang=en")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(login_page), SimpleNamespace(), "creator",
        max_scrolls=2, settle_ms=1))
    assert items == [] and err.startswith("logged_out")

    class _Boom(Exception):
        pass

    boom_page = _HubPage(goto_error=_Boom("net"),
                         current_url="https://www.tiktok.com/")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(boom_page), SimpleNamespace(), "creator",
        max_scrolls=2, settle_ms=1))
    assert err.startswith("goto:_Boom")

    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(_HubPage()), SimpleNamespace(), "  ",
        max_scrolls=2, settle_ms=1))
    assert err.startswith("missing_uid")


# ── account_hub.fetch_account_works 分派 ─────────────────────────────

def test_account_works_dispatch_tiktok(monkeypatch):
    captured = {}

    async def fake_works(mgr, identity, uid, known, *, max_scrolls=14):
        captured["uid"] = uid
        captured["max_scrolls"] = max_scrolls
        return [work_item("7"), work_item("bad")], ""

    monkeypatch.setattr(account_hub, "fetch_tiktok_works", fake_works)
    items, err = run(account_hub.fetch_account_works(
        SimpleNamespace(), SimpleNamespace(), "tiktok", "creator"))
    assert err == "" and captured["uid"] == "creator"
    assert [w["item_id"] for w in items] == ["7"]   # 非法 id 被归一化过滤


def test_account_works_tiktok_self_resolve_unique_id(monkeypatch):
    captured = {}

    async def fake_profile(mgr, identity, **kw):
        return {"uniqueId": "resolved_handle"}, ""

    async def fake_works(mgr, identity, uid, known, *, max_scrolls=14):
        captured["uid"] = uid
        return [work_item("7")], ""

    monkeypatch.setattr(account_hub, "fetch_tiktok_self_profile",
                        fake_profile)
    monkeypatch.setattr(account_hub, "fetch_tiktok_works", fake_works)
    items, err = run(account_hub.fetch_account_works(
        SimpleNamespace(), SimpleNamespace(), "tiktok", ""))
    assert err == "" and captured["uid"] == "resolved_handle"


def test_account_works_tiktok_logged_out(monkeypatch):
    async def fake_profile(mgr, identity, **kw):
        return {}, "logged_out"

    monkeypatch.setattr(account_hub, "fetch_tiktok_self_profile",
                        fake_profile)
    items, err = run(account_hub.fetch_account_works(
        SimpleNamespace(), SimpleNamespace(), "tiktok", ""))
    assert items == [] and err.startswith("logged_out")


# ── fetch_follows:TikTok 独立列表路由 ────────────────────────────────

class _FollowPage(_HubPage):
    def __init__(self, direction, **kw):
        super().__init__(**kw)
        self._direction = direction

    def _default_api_url(self):
        seg = "following" if self._direction == "following" else "follower"
        return (f"https://www.tiktok.com/api/user/list/{seg}/list/"
                "?aid=1988&cursor=0")


def tt_user(uid, name):
    return {"id": uid, "uniqueId": name, "nickname": name.upper(),
            "secUid": f"SEC{uid}", "signature": "bio",
            "avatarLarger": {"urlList": [f"https://cdn/{uid}.jpg"]}}


def follow_payload(users, *, has_more=False):
    return {"statusCode": 0, "userList": users, "hasMore": has_more,
            "cursor": str(len(users))}


def test_fetch_follows_tiktok_following_route_and_users():
    page = _FollowPage(
        "following",
        goto_payloads=[follow_payload([tt_user("1001", "alice"),
                                       tt_user("1002", "bob")])],
        current_url="https://www.tiktok.com/@creator/following")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(page), SimpleNamespace(), "tiktok", "creator", "following",
        set(), settle_ms=1, max_scrolls=2))
    assert err == "" and len(users) == 2
    assert page.visited_urls[0] == \
        "https://www.tiktok.com/@creator/following"
    alice = next(u for u in users if u["uid"] == "1001")
    assert alice["nickname"] == "ALICE"
    assert alice["sec_uid"] == "SEC1001"
    assert alice["avatar"] == "https://cdn/1001.jpg"
    # 关注列表方向强制 is_following
    assert all(u["is_following"] for u in users)


def test_fetch_follows_tiktok_fans_route():
    page = _FollowPage(
        "fan",
        goto_payloads=[follow_payload([tt_user("2001", "carol")])],
        current_url="https://www.tiktok.com/@creator/followers")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(page), SimpleNamespace(), "tiktok", "creator", "fan",
        set(), settle_ms=1, max_scrolls=2))
    assert users[0]["uid"] == "2001"
    assert page.visited_urls[0] == \
        "https://www.tiktok.com/@creator/followers"


def test_fetch_follows_tiktok_empty_is_confirmed():
    page = _FollowPage("fan", goto_payloads=[follow_payload([])],
                       current_url="https://www.tiktok.com/@creator/followers")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(page), SimpleNamespace(), "tiktok", "creator", "fan",
        set(), settle_ms=1, max_scrolls=2))
    assert users == [] and err == "empty"


def test_fetch_follows_tiktok_ignores_unrelated_apis():
    page = _FollowPage(
        "following",
        goto_payloads=[(
            "https://www.tiktok.com/api/recommend/feed/?aid=1988",
            {"statusCode": 0,
             "otherUsers": [tt_user("9", "stranger")]})],
        current_url="https://www.tiktok.com/@creator/following")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(page), SimpleNamespace(), "tiktok", "creator", "following",
        set(), settle_ms=1, max_scrolls=5))
    assert users == []
    # 不是有效空列表:必须保留旧快照(错误不允许变成 empty)
    assert err and err != "empty"


def test_fetch_follows_tiktok_self_resolve_and_login_redirect(monkeypatch):
    async def fake_profile(mgr, identity, **kw):
        return {"uniqueId": "resolved_handle"}, ""

    monkeypatch.setattr(account_hub, "fetch_tiktok_self_profile",
                        fake_profile)
    page = _FollowPage(
        "following",
        goto_payloads=[follow_payload([tt_user("1", "a")])],
        current_url="https://www.tiktok.com/@resolved_handle/following")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(page), SimpleNamespace(), "tiktok", "", "following",
        set(), settle_ms=1, max_scrolls=2))
    assert err == "" and len(users) == 1

    login_page = _FollowPage("fan", goto_payloads=[],
                             current_url="https://www.tiktok.com/login")
    users, err = run(account_hub.fetch_follows(
        _HubMgr(login_page), SimpleNamespace(), "tiktok", "creator", "fan",
        set(), settle_ms=1, max_scrolls=2))
    assert err.startswith("logged_out")


# ── 注册面 ────────────────────────────────────────────────────────────

def test_tiktok_capabilities_after_task5():
    assert pf.has_cap("tiktok", pf.OWN_WORKS)
    assert pf.has_cap("tiktok", pf.FOLLOW_SYNC)
    # KEYWORD_COLLECTION 已在 Task 8 开放
    assert pf.has_cap("tiktok", pf.KEYWORD_COLLECTION)
    # PUBLISH 已在 Task 9 开放(创作者中心网页发布)
    assert pf.has_cap("tiktok", pf.PUBLISH)
    # AUTO_COMMENT 已在 Task 10 开放(浏览器证据化评论/回复)
    assert pf.has_cap("tiktok", pf.AUTO_COMMENT)
    # DM / SOCIAL_ACTION 已在 Task 11 开放
    assert pf.has_cap("tiktok", pf.DM)
    assert pf.has_cap("tiktok", pf.SOCIAL_ACTION)

