"""Task 8: TikTok 关键词/标签采集单测。

覆盖:
* 搜索响应提取/验证码信号/本地筛选(类型/时间/门槛)/排序;
* fetch_tiktok_search 拦截 /api/search/*/full、known 去重、上限、
  登录墙、人机验证被动等待与超时、非搜索接口忽略;
* KeywordCollector 的 tiktok 流水线:有头窗口、解析入库、评论、
  两轮续跑去重、缺媒体降级、人机验证停止后续关键词;
* POST/PUT/GET /api/collections 的 tiktok 支持与账号校验。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

import app.db as db
import app.engine.collection as collection
import app.main as main
from app.config import Config
from app.engine.collection import KeywordCollector
from app.main import app
from app.models import (
    DouyinAccount,
    KeywordCollectionComment,
    KeywordCollectionContent,
    KeywordCollectionJob,
)
from app.platforms.tiktok import search as ttsearch
from app.risk import RiskCategory, classify_platform_error
from sqlmodel import select
from test_project_optimizations import local_project, store  # noqa: F401
from test_tiktok_hub import _HubPage, run


# ── 夹具构造 ──────────────────────────────────────────────────────────

SEARCH_API = "https://www.tiktok.com/api/search/general/full/?aid=1988"
ITEM_LIST_API = "https://www.tiktok.com/api/post/item_list/?aid=1988"


def tt_item(iid="7300000000000000001", *, kind="video", likes="10",
            comments="2", created=1_700_000_000, sec_uid="", desc=""):
    base = {
        "id": iid,
        "desc": desc or ("clip " + iid),
        "createTime": created,
        "author": {"uniqueId": "creator", "nickname": "Creator",
                   "secUid": sec_uid or "SEC" + iid},
        "statsV2": {"diggCount": likes, "commentCount": comments},
    }
    if kind == "video":
        base["video"] = {
            "duration": 12,
            "cover": ["https://cdn.test/cover.jpg"],
            "bitRateInfo": [{
                "PlayAddr": {"UrlList": ["https://media.test/v.mp4"],
                             "Height": 720},
                "Bitrate": 1_000_000,
            }],
        }
    else:
        base["imagePost"] = {
            "cover": {"imageURL": {"urlList": ["https://cdn.test/pc.webp"]}},
            "images": [{"imageURL": {"urlList": ["https://cdn.test/i1.webp"]}}],
        }
    return base


def search_payload(items, *, verify=False):
    if verify:
        return {"data": [], "statusCode": 10204,
                "need_captcha": True, "verify_type": "slide"}
    return {"data": [{"type": 1, "item": it} for it in items],
            "has_more": bool(items), "cursor": "20"}


def empty_search_payload():
    return {"data": [], "has_more": False, "cursor": "0"}


# ── 纯函数:提取 / 验证信号 / 筛选 / 排序 ─────────────────────────────

def test_extract_search_items_unwraps_data_items():
    payload = search_payload([tt_item("1"), tt_item("2")])
    items = ttsearch.extract_tiktok_search_items(payload)
    assert [it["id"] for it in items] == ["1", "2"]


def test_extract_search_items_accepts_bare_and_rejects_garbage():
    bare = {"data": [tt_item("9")]}
    assert ttsearch.extract_tiktok_search_items(bare)[0]["id"] == "9"
    assert ttsearch.extract_tiktok_search_items({"data": []}) == []
    assert ttsearch.extract_tiktok_search_items({"data": [
        {"item": {"desc": "no id"}}, "x", {"item": "y"}, None,
    ]}) == []
    assert ttsearch.extract_tiktok_search_items("nope") == []


def test_payload_verification_signals():
    assert ttsearch._payload_needs_verification(
        {"need_captcha": 1})
    assert ttsearch._payload_needs_verification(
        {"verifyType": "slide"})
    assert not ttsearch._payload_needs_verification(
        search_payload([tt_item("1")]))
    assert not ttsearch._payload_needs_verification({})


def test_item_matches_content_type_and_thresholds():
    video = tt_item("1", kind="video", likes="1.2K", comments="34")
    image = tt_item("2", kind="images", likes="5", comments="0")
    now = 1_700_000_100
    assert ttsearch.tiktok_search_item_matches(video, now=now)
    assert not ttsearch.tiktok_search_item_matches(
        video, content_type="images", now=now)
    assert not ttsearch.tiktok_search_item_matches(
        image, content_type="video", now=now)
    assert ttsearch.tiktok_search_item_matches(
        video, min_likes=1000, min_comments=10, now=now)
    assert not ttsearch.tiktok_search_item_matches(
        video, min_likes=2000, now=now)
    assert not ttsearch.tiktok_search_item_matches(
        video, min_comments=100, now=now)
    # 发布时间窗:作品在 100 秒前,一天内通过、超过一周淘汰
    assert ttsearch.tiktok_search_item_matches(
        video, publish_time="day", now=now)
    assert not ttsearch.tiktok_search_item_matches(
        video, publish_time="week", now=now + 10_000_000)


def test_sort_items_latest_and_most_liked():
    old = tt_item("1", created=100, likes="10")
    new = tt_item("2", created=300, likes="5")
    hot = tt_item("3", created=200, likes="500")
    items = [old, new, hot]
    assert [it["id"] for it in
            ttsearch.sort_tiktok_search_items(items, "general")] == [
        "1", "2", "3"]
    assert [it["id"] for it in
            ttsearch.sort_tiktok_search_items(items, "latest")] == [
        "2", "3", "1"]
    assert [it["id"] for it in
            ttsearch.sort_tiktok_search_items(items, "most_liked")] == [
        "3", "1", "2"]


# ── fetch_tiktok_search:浏览器拦截 ───────────────────────────────────

def test_search_intercepts_paginates_dedupes_and_caps():
    page = _HubPage(
        goto_payloads=[(SEARCH_API, search_payload([tt_item("1"),
                                                    tt_item("2")]))],
        scroll_payloads=[
            (SEARCH_API, search_payload([tt_item("2"), tt_item("3")])),
            (SEARCH_API, empty_search_payload())],
        current_url="https://www.tiktok.com/search?q=camping")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "camping", set(),
        max_results=10, max_scrolls=4, stagnant_limit=2, settle_ms=1))
    assert err == ""
    assert sorted(it["id"] for it in items) == ["1", "2", "3"]
    assert page.visited_urls[0] == "https://www.tiktok.com/search?q=camping"
    assert page.closed


def test_search_respects_max_results_and_known_filter():
    page = _HubPage(
        goto_payloads=[(SEARCH_API, search_payload(
            [tt_item("1"), tt_item("2"), tt_item("3")]))],
        current_url="https://www.tiktok.com/search?q=x")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "x", {"2"},
        max_results=2, max_scrolls=1, settle_ms=1))
    assert err == ""
    # known 的 "2" 过滤;上限 2 条
    assert sorted(it["id"] for it in items) == ["1", "3"]


def test_search_uses_supplied_headed_context():
    page = _HubPage(
        goto_payloads=[(SEARCH_API, search_payload([tt_item("1")]))],
        current_url="https://www.tiktok.com/search?q=x")
    context = SimpleNamespace(
        pages=[], new_page=AsyncMock(return_value=page))
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(), SimpleNamespace(), "x", set(),
        max_results=5, max_scrolls=1, settle_ms=1, context=context))
    assert err == "" and [it["id"] for it in items] == ["1"]
    context.new_page.assert_awaited_once()


class _ScriptEmitPage(_HubPage):
    """首次非滚动 evaluate(首屏注水)时吐出一个 script 资源响应。"""

    def __init__(self, script_payload, **kw):
        super().__init__(**kw)
        self._script_payload = script_payload

    async def evaluate(self, js, *args):
        if self._script_payload is not None and "scrollTo" not in js:
            from test_tiktok_hub import _Resp
            payload = self._script_payload
            self._script_payload = None
            await self.handlers[0](
                _Resp(SEARCH_API, payload, rtype="script"))
        return await super().evaluate(js, *args)


def test_search_ignores_non_search_api_and_non_xhr():
    page = _ScriptEmitPage(
        search_payload([tt_item("8")]),
        goto_payloads=[(ITEM_LIST_API, search_payload([tt_item("9")]))],
        current_url="https://www.tiktok.com/search?q=x")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "x", set(),
        max_results=5, max_scrolls=1, settle_ms=1))
    assert items == []
    assert "未拦截到" in err


def test_search_valid_empty_data_is_not_error():
    page = _HubPage(
        goto_payloads=[(SEARCH_API, empty_search_payload())],
        current_url="https://www.tiktok.com/search?q=zzz")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "zzz", set(),
        max_results=5, max_scrolls=1, settle_ms=1))
    assert items == [] and err == ""


def test_search_missing_keyword():
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(), SimpleNamespace(), "  ", set()))
    assert items == [] and err == "missing_keyword"


def test_search_login_wall_is_auth_error():
    page = _HubPage(
        goto_payloads=[],
        current_url="https://www.tiktok.com/login/phone-or-email")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "kw", set(),
        max_results=5, max_scrolls=1, settle_ms=1))
    assert items == [] and "登录态已失效" in err
    assert classify_platform_error(err)[0] == RiskCategory.AUTH


class _VerifyPage(_HubPage):
    """验证码页桩:第 N 次等待时模拟人工完成验证。"""

    def __init__(self, *, resume_on_wait=0, **kw):
        super().__init__(**kw)
        self._resume_on_wait = resume_on_wait
        self.waits = 0
        self.fronted = False

    async def bring_to_front(self):
        self.fronted = True

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)
        if ms < 1000:          # settle 短等待不计入验证轮询
            return
        self.waits += 1
        if self._resume_on_wait and self.waits == self._resume_on_wait:
            self._current_url = "https://www.tiktok.com/search?q=kw"
            await self._emit((SEARCH_API, search_payload([tt_item("1")])))


def test_search_verification_passive_wait_resumes_after_human():
    page = _VerifyPage(
        resume_on_wait=1,
        goto_payloads=[],
        current_url="https://www.tiktok.com/captcha/verify?aid=1988")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "kw", set(),
        max_results=5, max_scrolls=1, settle_ms=1,
        captcha_wait_seconds=5))
    assert err == ""
    assert [it["id"] for it in items] == ["1"]
    assert page.fronted  # 窗口被置前等待人工


def test_search_verification_timeout_is_risk_error():
    page = _VerifyPage(
        resume_on_wait=0,
        goto_payloads=[],
        current_url="https://www.tiktok.com/whale/verify")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "kw", set(),
        max_results=5, max_scrolls=1, settle_ms=1,
        captcha_wait_seconds=2))
    assert items == []
    assert "人机验证" in err
    assert classify_platform_error(err)[0] == RiskCategory.RISK


def test_search_verify_payload_marks_verification():
    page = _HubPage(
        goto_payloads=[(SEARCH_API, search_payload([], verify=True))],
        scroll_payloads=[],
        current_url="https://www.tiktok.com/search?q=kw")
    items, err = run(ttsearch.fetch_tiktok_search(
        SimpleNamespace(new_page=AsyncMock(return_value=page)),
        SimpleNamespace(), "kw", set(),
        max_results=5, max_scrolls=1, stagnant_limit=1, settle_ms=1,
        captcha_wait_seconds=1))
    assert items == []
    assert "人机验证" in err or "验证" in err


# ── KeywordCollector:tiktok 流水线 ───────────────────────────────────

def _acc(account_id):
    """run() 只需要 id/proxy 与 identity_for 入参,无需 ORM 实体。"""
    return SimpleNamespace(id=account_id, platform="tiktok",
                           proxy="", storage_state="{}")


def _tt_cfg():
    cfg = Config()
    cfg.engine.tiktok_keyword_gap_seconds = 0
    cfg.engine.tiktok_item_gap_seconds = 0
    cfg.engine.tiktok_captcha_wait_seconds = 1
    return cfg


def _headed_browser():
    opened = []

    @asynccontextmanager
    async def temporary_headed_context(identity):
        opened.append(identity)
        yield SimpleNamespace()

    return opened, SimpleNamespace(
        identity_for=lambda acc: SimpleNamespace(ua="tt-agent"),
        temporary_headed_context=temporary_headed_context)


def test_pipeline_persists_and_dedupes_on_resume(local_project, monkeypatch):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    job = store(KeywordCollectionJob(
        platform="tiktok", account_id=account,
        keywords=json.dumps(["camping"]),
        max_contents_per_keyword=5, max_comments_per_content=10))
    opened, browser = _headed_browser()
    collector = KeywordCollector(_tt_cfg(), browser, SimpleNamespace())
    collector._discover_tiktok = AsyncMock(return_value=(
        [tt_item("1"), tt_item("2")], ""))
    collector._tiktok_comments = AsyncMock(return_value=([
        {"comment_id": "c1", "text": "hi", "user_nickname": "fan",
         "like_count": 1, "create_time": 1_700_000_010, "reply_to": ""},
        {"comment_id": "c2", "text": "yo", "user_nickname": "fan2",
         "like_count": 0, "create_time": 1_700_000_011, "reply_to": ""},
    ], ""))

    r1 = asyncio.run(collector.run(job, _acc(account)))
    r2 = asyncio.run(collector.run(job, _acc(account)))

    assert r1["contents"] == 2 and r1["comments"] == 4
    assert r2["contents"] == 2 and r2["comments"] == 4
    assert len(opened) == 2  # 每轮复用一次有头窗口
    with db.get_session() as s:
        contents = s.exec(select(KeywordCollectionContent)
                          .where(KeywordCollectionContent.job_id == job)).all()
        comments = s.exec(select(KeywordCollectionComment)
                          .where(KeywordCollectionComment.job_id == job)).all()
        fresh = s.get(KeywordCollectionJob, job)
    assert {c.aweme_id for c in contents} == {"1", "2"}
    assert len(comments) == 4
    # author secUid 入库;媒体地址解析成功
    by_id = {c.aweme_id: c for c in contents}
    assert by_id["1"].author_id == "SEC1"
    assert "v.mp4" in by_id["1"].media_json
    assert by_id["1"].download_status == "skipped"
    assert fresh.content_count == 2 and fresh.comment_count == 4


def test_pipeline_keeps_metadata_when_media_missing(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    job = store(KeywordCollectionJob(
        platform="tiktok", account_id=account,
        keywords=json.dumps(["kw"]),
        max_contents_per_keyword=5, max_comments_per_content=0))
    _, browser = _headed_browser()
    collector = KeywordCollector(_tt_cfg(), browser, SimpleNamespace())
    # 只有元数据、没有 video/imagePost:parse_tiktok_item 拿不到媒体
    collector._discover_tiktok = AsyncMock(return_value=([{
        "id": "777", "desc": "region locked", "createTime": 1_700_000_000,
        "author": {"uniqueId": "creator", "nickname": "C",
                   "secUid": "SEC777"},
        "statsV2": {"diggCount": "3", "commentCount": "1"},
    }], ""))

    result = asyncio.run(collector.run(job, _acc(account)))

    assert result["contents"] == 1
    with db.get_session() as s:
        row = s.exec(select(KeywordCollectionContent)
                     .where(KeywordCollectionContent.job_id == job)).one()
        fresh = s.get(KeywordCollectionJob, job)
    assert row.aweme_id == "777"
    assert row.author_id == "SEC777"
    assert row.media_json == "[]"
    assert "未取得媒体地址" in (row.error or "")
    assert fresh.error_count == 1


def test_pipeline_verification_stops_remaining_keywords(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    job = store(KeywordCollectionJob(
        platform="tiktok", account_id=account,
        keywords=json.dumps(["a", "b"]),
        max_contents_per_keyword=5, max_comments_per_content=0))
    _, browser = _headed_browser()
    collector = KeywordCollector(_tt_cfg(), browser, SimpleNamespace())
    collector._discover_tiktok = AsyncMock(return_value=(
        [], "TikTok 要求完成人机验证；本次任务已停止后续请求，"
            "请在弹出的浏览器窗口中完成验证并等待冷却后再续跑"))

    result = asyncio.run(collector.run(job, _acc(account)))

    # RISK 分类后 break:第二个关键词不再发起搜索
    assert collector._discover_tiktok.await_count == 1
    assert result["errors"] == 1
    with db.get_session() as s:
        fresh = s.get(KeywordCollectionJob, job)
    assert "人机验证" in fresh.error


def test_tiktok_comments_parses_filters_reply_and_caps(local_project,
                                                       monkeypatch):
    cfg = _tt_cfg()
    collector = KeywordCollector(
        cfg, SimpleNamespace(identity_for=lambda a: SimpleNamespace(ua="x")),
        SimpleNamespace())
    aweme = SimpleNamespace(aweme_id="1")
    raw = [
        {"cid": "1", "text": "top", "user": {"nickname": "a"},
         "digg_count": 2, "create_time": 1_700_000_010, "reply_id": "0"},
        {"cid": "2", "text": "reply", "user": {"nickname": "b"},
         "digg_count": 1, "create_time": 1_700_000_011,
         "reply_id": "1"},
    ]
    fake = AsyncMock(return_value=(raw, ""))
    monkeypatch.setattr(collection, "fetch_tiktok_comments", fake)
    account = SimpleNamespace(id=1)

    comments, err = asyncio.run(collector._tiktok_comments(
        account, aweme, 10, include_replies=False, handle="creator"))
    assert err == ""
    assert [c["comment_id"] for c in comments] == ["1"]
    assert comments[0]["reply_to"] == ""
    assert fake.await_args.kwargs["handle"] == "creator"

    # include_replies=True:归一化后的回复条目保留(回复抓取尚为同接口适配)
    fake.reset_mock()
    comments_all, _ = asyncio.run(collector._tiktok_comments(
        account, aweme, 10, include_replies=True))
    assert [c["comment_id"] for c in comments_all] == ["1", "2"]
    assert comments_all[1]["reply_to"] == "1"

    fake.reset_mock()
    zero, _ = asyncio.run(collector._tiktok_comments(
        account, aweme, 0, include_replies=False))
    assert zero == []
    fake.assert_not_awaited()


# ── API:创建 / 编辑 / 列表 ───────────────────────────────────────────

def _request(method, path, **kwargs):
    async def run_req():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run_req())


def _collection_body(account_id, platform="tiktok", **extra):
    base = {
        "platform": platform, "account_id": account_id,
        "keywords": ["camping", "gear"],
        "max_contents_per_keyword": 5,
        "max_pages_per_keyword": 3, "stagnant_pages": 2,
        "search_sort": "latest", "publish_time": "week",
        "content_type": "video", "min_likes": 100, "min_comments": 5,
        "max_comments_per_content": 10, "include_replies": False,
        "download_media": False, "video_quality": "720",
        "download_dir": "",
    }
    base.update(extra)
    return base


def test_create_tiktok_collection(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active",
                                  storage_state="{}"))
    main.engine.enqueue_collection_job = MagicMock()
    res = _request("POST", "/api/collections",
                   json=_collection_body(account))
    assert res.status_code == 200
    data = res.json()
    assert data["platform"] == "tiktok"
    assert data["max_contents_per_keyword"] == 5
    assert data["search_sort"] == "latest"
    assert data["publish_time"] == "week"
    assert data["content_type"] == "video"
    assert data["video_quality"] == "720"
    assert data["keywords"] == ["camping", "gear"]
    with db.get_session() as s:
        assert s.exec(select(KeywordCollectionJob)).one().platform == "tiktok"


def test_create_collection_rejects_bad_account(local_project):
    other = store(DouyinAccount(platform="douyin", status="active",
                                storage_state="{}"))
    invalid = store(DouyinAccount(platform="tiktok", status="invalid"))
    # 跨平台账号
    res = _request("POST", "/api/collections",
                   json=_collection_body(other))
    assert res.status_code == 400
    # invalid 账号
    res2 = _request("POST", "/api/collections",
                    json=_collection_body(invalid))
    assert res2.status_code == 400


def test_create_collection_rejects_unsupported_platform(local_project):
    account = store(DouyinAccount(platform="kuaishou", status="active"))
    res = _request("POST", "/api/collections",
                   json=_collection_body(account, platform="kuaishou"))
    assert res.status_code == 400
    assert "TikTok" in res.json()["detail"]


def test_list_collections_filters_tiktok(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active",
                                  storage_state="{}"))
    main.engine.enqueue_collection_job = MagicMock()
    _request("POST", "/api/collections",
             json=_collection_body(account, keywords=["only-tt"]))
    rows = _request("GET", "/api/collections?platform=tiktok").json()
    assert len(rows) == 1 and rows[0]["platform"] == "tiktok"
    assert _request("GET", "/api/collections?platform=douyin").json() == []


def test_edit_tiktok_collection_keeps_results(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active",
                                  storage_state="{}"))
    job = store(KeywordCollectionJob(
        platform="tiktok", account_id=account,
        keywords='["old"]', status="done", current_step="已完成"))
    content = store(KeywordCollectionContent(
        job_id=job, platform="tiktok", keyword="old", aweme_id="keep-1"))
    res = _request("PUT", f"/api/collections/{job}",
                   json=_collection_body(account, keywords=["new"]))
    assert res.status_code == 200
    assert res.json()["keywords"] == ["new"]
    with db.get_session() as s:
        kept = s.exec(select(KeywordCollectionContent)
                      .where(KeywordCollectionContent.job_id == job)).all()
        fresh = s.get(KeywordCollectionJob, job)
    assert [c.aweme_id for c in kept] == ["keep-1"]
    assert fresh.current_step == "配置已更新，可点击续跑"


def test_running_tiktok_collection_cannot_be_edited(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active",
                                  storage_state="{}"))
    job = store(KeywordCollectionJob(
        platform="tiktok", account_id=account,
        keywords='["old"]', status="running"))
    res = _request("PUT", f"/api/collections/{job}",
                   json=_collection_body(account))
    assert res.status_code == 409
