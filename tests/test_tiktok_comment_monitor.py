"""Task 7: TikTok 评论监控单测。

覆盖:
* parse_tiktok_comment 字段映射(cid/text/user/digg/create_time/reply_id);
* fetch_tiktok_comments 拦截 /api/comment/list/、去重 known、滚动翻页、
  空响应错误;
* MonitorEngine._cw_tiktok_video 连续扫描去重不重复、首扫不通知;
* POST /api/comment-watches 的 tiktok 视频/账号解析与账号校验;
* COMMENT_MONITOR 能力注册面。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.engine.monitor as monitor
import app.main as main
from app.engine.monitor import MonitorEngine
from app.models import CommentRecord, CommentWatch, DouyinAccount
from app.platforms import registry as pf
from app.platforms.tiktok import comments as ttcomments
from test_project_optimizations import local_project, store  # noqa: F401
from test_tiktok_hub import _HubMgr, _HubPage, run


# ── parse_tiktok_comment ──────────────────────────────────────────────

def tt_comment(cid="7300000000000000001", **over):
    base = {
        "cid": cid,
        "text": "hello tiktok",
        "user": {"uniqueId": "fan1", "nickname": "Fan One",
                 "secUid": "MS4wLjABAAAAsec1"},
        "digg_count": 5,
        "create_time": 1700000000,
        "reply_id": "0",
    }
    base.update(over)
    return base


def test_parse_comment_maps_fields():
    c = ttcomments.parse_tiktok_comment(tt_comment())
    assert c["comment_id"] == "7300000000000000001"
    assert c["text"] == "hello tiktok"
    assert c["user_nickname"] == "Fan One"
    assert c["user_sec_uid"] == "MS4wLjABAAAAsec1"
    assert c["like_count"] == 5
    assert c["create_time"] == 1700000000
    assert c["reply_to"] == ""   # reply_id=0 视为顶级评论


def test_parse_comment_reply_to_nonzero():
    c = ttcomments.parse_tiktok_comment(tt_comment(reply_id="7200"))
    assert c["reply_to"] == "7200"


def test_parse_comment_rejects_missing_cid():
    assert ttcomments.parse_tiktok_comment({"text": "no cid"}) is None
    assert ttcomments.parse_tiktok_comment(None) is None
    assert ttcomments.parse_tiktok_comment("not a dict") is None


def test_parse_comment_tolerates_snake_case_keys():
    c = ttcomments.parse_tiktok_comment({
        "cid": "9", "text": "x",
        "user": {"sec_uid": "SEC", "nickname": "N"},
        "diggCount": "3", "createTime": 1700000001,
    })
    assert c["like_count"] == 3
    assert c["create_time"] == 1700000001
    assert c["user_sec_uid"] == "SEC"


# ── fetch_tiktok_comments:浏览器拦截 / 去重 / 滚动 ────────────────────

COMMENT_API = "https://www.tiktok.com/api/comment/list/?aid=1988"


def comment_page(items, has_more=True):
    return (COMMENT_API, {"comments": items, "has_more": has_more,
                          "cursor": "20"})


def test_fetch_comments_intercepts_api_and_filters_known():
    page = _HubPage(
        goto_payloads=[comment_page([tt_comment("1"), tt_comment("2")])],
        scroll_payloads=[comment_page([tt_comment("3")], has_more=False)],
        current_url="https://www.tiktok.com/@creator/video/7300")
    items, err = run(ttcomments.fetch_tiktok_comments(
        _HubMgr(page), SimpleNamespace(), "7300", {"2"},
        max_scrolls=6, settle_ms=1))
    assert err == ""
    # known 中的 "2" 被过滤;首屏 "1" + 翻页 "3" 返回
    assert sorted(i["cid"] for i in items) == ["1", "3"]
    assert page.closed


def test_fetch_comments_uses_handle_in_url():
    page = _HubPage(
        goto_payloads=[comment_page([tt_comment("1")], has_more=False)],
        current_url="https://www.tiktok.com/@creator/video/7300")
    run(ttcomments.fetch_tiktok_comments(
        _HubMgr(page), SimpleNamespace(), "7300", set(),
        handle="creator", max_scrolls=2, settle_ms=1))
    assert page.visited_urls[0] == "https://www.tiktok.com/@creator/video/7300"


def test_fetch_comments_empty_response_is_error():
    page = _HubPage(
        goto_payloads=[],
        scroll_payloads=[],
        current_url="https://www.tiktok.com/@creator/video/7300")
    items, err = run(ttcomments.fetch_tiktok_comments(
        _HubMgr(page), SimpleNamespace(), "7300", set(),
        max_scrolls=2, settle_ms=1))
    assert items == []
    assert "未拦截到评论" in err


def test_fetch_comments_missing_aweme_id():
    items, err = run(ttcomments.fetch_tiktok_comments(
        _HubMgr(_HubPage()), SimpleNamespace(), "", set()))
    assert items == [] and err == "missing_aweme_id"


def test_fetch_comments_ignores_non_comment_responses():
    # 非 /api/comment/list/ 的响应不应被收集
    page = _HubPage(
        goto_payloads=[
            ("https://www.tiktok.com/api/post/item_list/",
             {"itemList": []}),
            comment_page([tt_comment("1")], has_more=False),
        ],
        current_url="https://www.tiktok.com/@creator/video/7300")
    items, err = run(ttcomments.fetch_tiktok_comments(
        _HubMgr(page), SimpleNamespace(), "7300", set(),
        max_scrolls=2, settle_ms=1))
    assert err == ""
    assert [i["cid"] for i in items] == ["1"]


# ── 引擎层:连续扫描去重 / 首扫不通知 ──────────────────────────────────

def _engine(local_project, monkeypatch):
    local_project.cfg.risk_control.enabled = False
    local_project.browser.anon_identity = lambda: SimpleNamespace()
    local_project.browser.identity_for = lambda acc: SimpleNamespace()
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(engine, "_identity_proxy", lambda acc: (acc.id, ""))
    engine._notify_comments = AsyncMock()
    return engine


def test_cw_video_consecutive_scans_dedup(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    account = store(DouyinAccount(platform="tiktok", status="active"))
    watch = store(CommentWatch(
        platform="tiktok", kind="video", aweme_id="7300",
        account_id=account, mode="public"))
    calls = []

    async def fake_fetch(_browser, identity, aweme_id, known, **kwargs):
        calls.append((aweme_id, set(known)))
        base_ts = int(time.time())
        if len(calls) == 1:
            items = [tt_comment("1", create_time=base_ts),
                     tt_comment("2", create_time=base_ts + 1)]
        elif len(calls) == 2:
            items = [tt_comment("1", create_time=base_ts),
                     tt_comment("2", create_time=base_ts + 1)]
        else:
            items = [tt_comment("3", create_time=base_ts + 100)]
        return [i for i in items if i["cid"] not in known], ""

    monkeypatch.setattr(monitor, "fetch_tiktok_comments", fake_fetch)

    r1 = asyncio.run(engine.scan_comment_watch(watch, manual=True))
    r2 = asyncio.run(engine.scan_comment_watch(watch, manual=True))
    r3 = asyncio.run(engine.scan_comment_watch(watch, manual=True))
    assert r1["new_comments"] == 2
    assert r2["new_comments"] == 0
    assert r3["new_comments"] == 1
    # known 随扫描累积
    assert calls[1][1] == {"1", "2"}
    assert calls[2][1] == {"1", "2"}
    with db.get_session() as s:
        rows = s.exec(select(CommentRecord.comment_id)
                      .where(CommentRecord.watch_id == watch)).all()
        assert set(rows) == {"1", "2", "3"}
    # 首扫不通知;仅第三次(非首扫且有新评论)通知一次
    assert engine._notify_comments.await_count == 1


def test_cw_video_skips_proxy_bad_account(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    monkeypatch.setattr(monitor, "fetch_tiktok_comments", AsyncMock(
        return_value=([], "")))
    bad_acc = store(DouyinAccount(
        platform="tiktok", status="active", proxy="http://1.2.3.4:8080",
        proxy_status="bad"))
    watch = store(CommentWatch(
        platform="tiktok", kind="video", aweme_id="7300",
        account_id=bad_acc, mode="public"))
    r = asyncio.run(engine.scan_comment_watch(watch, manual=True))
    assert r["ok"] is False and r.get("skipped")
    assert "代理" in (r.get("error") or "")
    monitor.fetch_tiktok_comments.assert_not_called()


# ── API:创建 tiktok 评论监控 ──────────────────────────────────────────

def _request(method, path="/api/comment-watches", **kwargs):
    async def run_req():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run_req())


def _cw_body(account_id, url, kind="auto", **extra):
    return {"platform": "tiktok", "url_or_id": url, "kind": kind,
            "account_id": account_id, "interval_seconds": 600, **extra}


def test_create_tiktok_comment_watch_video_by_url(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_cw_body(
        account, "https://www.tiktok.com/@creator/video/7300000000000000001"))
    assert res.status_code == 200
    data = res.json()
    assert data["platform"] == "tiktok"
    assert data["kind"] == "video"
    assert data["aweme_id"] == "7300000000000000001"
    assert data["mode"] == "public"


def test_create_tiktok_comment_watch_video_by_numeric_id(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_cw_body(
        account, "7300000000000000001", kind="video"))
    assert res.status_code == 200
    assert res.json()["aweme_id"] == "7300000000000000001"


def test_create_tiktok_comment_watch_user_by_handle(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_cw_body(
        account, "https://www.tiktok.com/@creator123/", kind="user"))
    assert res.status_code == 200
    data = res.json()
    assert data["kind"] == "user"
    assert data["sec_uid"] == "creator123"


def test_create_tiktok_comment_watch_auto_detects_video(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_cw_body(
        account, "看看这个 https://www.tiktok.com/@u/video/7300000000000000009"))
    assert res.status_code == 200
    assert res.json()["kind"] == "video"


def test_create_tiktok_comment_watch_auto_detects_user(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_cw_body(account, "@creator_name"))
    assert res.status_code == 200
    assert res.json()["kind"] == "user"


def test_create_tiktok_comment_watch_requires_active_account(local_project):
    assert _request("POST", json=_cw_body(
        None, "https://www.tiktok.com/@creator")).status_code == 400
    wrong = store(DouyinAccount(platform="douyin", status="active"))
    assert _request("POST", json=_cw_body(
        wrong, "https://www.tiktok.com/@creator")).status_code == 400


def test_create_tiktok_comment_watch_rejects_bad_target(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    bad_video = _request("POST", json=_cw_body(
        account, "https://www.douyin.com/video/123", kind="video"))
    assert bad_video.status_code == 400
    bad_user = _request("POST", json=_cw_body(account, "bad name!", kind="user"))
    assert bad_user.status_code == 400


# ── 注册面 ───────────────────────────────────────────────────────────

def test_tiktok_has_comment_monitor_capability():
    assert "tiktok" in pf.keys_with(pf.COMMENT_MONITOR)
    assert pf.has_cap("tiktok", pf.COMMENT_MONITOR)
