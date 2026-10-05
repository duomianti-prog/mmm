"""Task 6: TikTok 创作者作品监控单测。

覆盖:
* handle 解析(主页链接 / @handle / 裸号;拒绝作品链接与异站);
* fetch_tiktok_works 增量水位线(翻到已知作品即停、首屏全已知零滚动、
  返回前过滤已知条目),本人全量同步模式行为不变;
* MonitorEngine._scan_tiktok_target_locked:连续扫描去重不重复、
  首扫不通知、账号强制绑定(网络闸门)、自动下载媒体筛选;
* POST /api/monitors 的 tiktok 目标解析与账号校验;
* WORK_MONITOR 能力注册面。
"""
from __future__ import annotations

import asyncio
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
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms import registry as pf
from app.platforms.tiktok import hub as tthub
from test_project_optimizations import local_project, store  # noqa: F401
from test_tiktok_hub import _HubMgr, _HubPage, run, work_item


# ── handle 解析 ──────────────────────────────────────────────────────

@pytest.mark.parametrize(("text", "expected"), [
    ("https://www.tiktok.com/@tiktok", "tiktok"),
    ("https://www.tiktok.com/@foo.bar_1/", "foo.bar_1"),
    ("http://tiktok.com/@ab_cd", "ab_cd"),
    ("@creator_name", "creator_name"),
    ("creator.name", "creator.name"),
])
def test_parse_handle_accepts_profile_forms(text, expected):
    assert tthub.parse_tiktok_handle(text) == expected


@pytest.mark.parametrize("text", [
    "https://www.tiktok.com/@u/video/7300000000000000001",   # 作品链接
    "https://www.tiktok.com/@u/photo/7300000000000000001",
    "https://www.douyin.com/@u",                              # 异站
    "https://vm.tiktok.com/ABCDE/",                           # 短链(无 handle)
    "bad name!",                                              # 非法字符
    "x",                                                      # 太短
    "",
])
def test_parse_handle_rejects_non_profile(text):
    assert tthub.parse_tiktok_handle(text) == ""


# ── 水位线:翻到已知作品即停 ─────────────────────────────────────────

def page1(items, has_more=True):
    return {"statusCode": 0, "itemList": items, "hasMore": has_more,
            "cursor": "20"}


def test_stop_after_known_halts_paging_and_filters_known():
    page = _HubPage(
        goto_payloads=[page1([work_item("1"), work_item("2")])],
        scroll_payloads=[
            page1([work_item("3"), work_item("4")]),
            page1([work_item("5")]),
        ],
        current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator", {"3"},
        max_scrolls=6, settle_ms=1, stop_after_known=True))
    assert err == ""
    # 第 1 次滚动命中已知 3(同页的 4 仍保留),第 2 次滚动不得发生
    assert sorted(i["id"] for i in items) == ["1", "2", "4"]
    assert len(page._scroll_payloads) == 1


def test_first_page_all_known_means_zero_scrolls_and_empty_success():
    page = _HubPage(
        goto_payloads=[page1([work_item("1"), work_item("2")])],
        scroll_payloads=[page1([work_item("3")])],
        current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator", {"1", "2"},
        max_scrolls=6, settle_ms=1, stop_after_known=True))
    assert items == [] and err == ""
    assert len(page._scroll_payloads) == 1   # 水位线命中,一次都没滚


def test_full_sync_mode_keeps_known_and_paginates():
    # 本人作品同步(stop_after_known=False):已知条目仍回传(upsert)
    page = _HubPage(
        goto_payloads=[page1([work_item("1"), work_item("2")],
                             has_more=False)],
        current_url="https://www.tiktok.com/@creator")
    items, err = run(tthub.fetch_tiktok_works(
        _HubMgr(page), SimpleNamespace(), "creator", {"1"},
        max_scrolls=3, settle_ms=1))
    assert err == ""
    assert sorted(i["id"] for i in items) == ["1", "2"]


# ── 引擎层:连续扫描去重 / 通知 / 下载 ───────────────────────────────

def tt_video_item(item_id="7300000000000000001"):
    return {
        "id": item_id,
        "desc": "tt post " + item_id,
        "createTime": 1700000000,
        "author": {"uniqueId": "creator", "nickname": "Creator",
                   "avatarLarger": {"urlList": ["https://cdn/ava.jpg"]}},
        "statsV2": {"diggCount": "10", "commentCount": "2"},
        "video": {"duration": 12, "cover": ["https://cdn/cover.jpg"],
                  "playAddr": "https://media.invalid/v.mp4"},
    }


def tt_image_item(item_id="7300000000000000009"):
    return {
        "id": item_id,
        "desc": "tt images " + item_id,
        "createTime": 1700000001,
        "author": {"uniqueId": "creator", "nickname": "Creator"},
        "imagePost": {
            "cover": {"imageURL": {"urlList": ["https://cdn/ic.jpg"]}},
            "images": [{"imageURL": {"urlList": ["https://cdn/i1.jpg"]}}],
        },
    }


def _engine(local_project, monkeypatch):
    local_project.cfg.risk_control.enabled = False
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(engine, "_identity_proxy", lambda acc: (acc.id, ""))
    engine._notify_new = AsyncMock()
    engine._download = AsyncMock()
    return engine


def test_consecutive_scans_dedup_and_only_notify_new(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    account = store(DouyinAccount(platform="tiktok", status="active"))
    before = datetime.utcnow() - timedelta(hours=1)
    target = store(MonitorTarget(
        platform="tiktok", sec_uid="creator", account_id=account,
        created_at=before, download_enabled=False))
    calls = []

    async def fake_fetch(_browser, identity, handle, known, **kwargs):
        calls.append((handle, set(known), kwargs.get("stop_after_known")))
        assert kwargs.get("stop_after_known") is True
        if len(calls) == 1:
            return [tt_video_item("1")], ""
        if len(calls) == 2:
            return [], ""                    # 第二次扫描:无新作品
        return [tt_video_item("2")], ""      # 第三次:发布了新作品

    monkeypatch.setattr(monitor, "fetch_tiktok_works", fake_fetch)

    r1 = asyncio.run(engine.scan_target(target))
    r2 = asyncio.run(engine.scan_target(target))
    r3 = asyncio.run(engine.scan_target(target))
    assert (r1["ok"], r1["new"]) == (True, 1)
    assert (r2["ok"], r2["new"]) == (True, 0)
    assert (r3["ok"], r3["new"]) == (True, 1)
    assert [c[0] for c in calls] == ["creator"] * 3
    assert [c[1] for c in calls] == [set(), {"1"}, {"1"}]
    with db.get_session() as session:
        rows = session.exec(select(ContentRecord)
                            .where(ContentRecord.target_id == target)).all()
        assert {r.aweme_id for r in rows} == {"1", "2"}
        t = session.get(MonitorTarget, target)
        assert t.nickname == "Creator" and t.avatar == "https://cdn/ava.jpg"
        assert not t.last_error
    # 首扫不通知;仅第三次(非首扫且真有新作品)通知一次
    assert engine._notify_new.await_count == 1
    engine._download.assert_not_awaited()


def test_scan_requires_bound_active_account(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    monkeypatch.setattr(monitor, "fetch_tiktok_works", AsyncMock(
        return_value=([], "")))
    # 未绑定账号:网络闸门禁止裸连
    anon = store(MonitorTarget(platform="tiktok", sec_uid="creator"))
    res = asyncio.run(engine.scan_target(anon))
    assert not res["ok"] and "网络体检" in res["error"]
    monitor.fetch_tiktok_works.assert_not_called()
    # 失效账号 / 错平台账号同样跳过
    expired_acc = store(DouyinAccount(platform="tiktok", status="expired"))
    wrong_acc = store(DouyinAccount(platform="douyin", status="active"))
    expired = store(MonitorTarget(
        platform="tiktok", sec_uid="creator", account_id=expired_acc))
    wrong = store(MonitorTarget(
        platform="tiktok", sec_uid="creator", account_id=wrong_acc))
    for tid in (expired, wrong):
        r = asyncio.run(engine.scan_target(tid))
        assert not r["ok"] and r.get("skipped")
    monitor.fetch_tiktok_works.assert_not_called()


def test_scan_auto_download_respects_media_filter(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    account = store(DouyinAccount(platform="tiktok", status="active"))

    async def fake_fetch(_browser, identity, handle, known, **kwargs):
        return [tt_video_item("1"), tt_image_item("9")], ""

    monkeypatch.setattr(monitor, "fetch_tiktok_works", fake_fetch)

    video_only = store(MonitorTarget(
        platform="tiktok", sec_uid="creator", account_id=account,
        download_enabled=True, media_filter="video"))
    res = asyncio.run(engine.scan_target(video_only))
    assert res["ok"] and res["new"] == 2
    # 仅视频触发下载;图集记录为 skipped
    assert engine._download.await_count == 1
    with db.get_session() as session:
        statuses = {r.aweme_id: r.download_status for r in session.exec(
            select(ContentRecord).where(ContentRecord.target_id == video_only))}
    assert statuses == {"1": "pending", "9": "skipped"}


def test_scan_empty_homepage_is_success(local_project, monkeypatch):
    engine = _engine(local_project, monkeypatch)
    account = store(DouyinAccount(platform="tiktok", status="active"))
    target = store(MonitorTarget(
        platform="tiktok", sec_uid="newbie", account_id=account,
        download_enabled=False))
    monkeypatch.setattr(monitor, "fetch_tiktok_works",
                        AsyncMock(return_value=([], "empty")))
    res = asyncio.run(engine.scan_target(target))
    assert res["ok"] and res["new"] == 0 and not res["error"]


# ── API:创建 tiktok 监控目标 ─────────────────────────────────────────

def _request(method, path="/api/monitors", **kwargs):
    async def run_req():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run_req())


def _body(account_id, url, **extra):
    return {"platform": "tiktok", "url_or_secuid": url,
            "account_id": account_id, "download_enabled": False, **extra}


def test_create_tiktok_monitor_resolves_handle(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    res = _request("POST", json=_body(
        account, "看看这个创作者 https://www.tiktok.com/@creator123/ 很赞"))
    assert res.status_code == 200
    data = res.json()
    assert data["sec_uid"] == "creator123" and data["platform"] == "tiktok"


def test_create_tiktok_monitor_rejects_item_url_and_short(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    item = _request("POST", json=_body(
        account, "https://www.tiktok.com/@u/video/7300000000000000001"))
    assert item.status_code == 400
    short = _request("POST", json=_body(account, "https://vm.tiktok.com/ABC/"))
    assert short.status_code == 400
    with db.get_session() as session:
        assert not session.exec(select(MonitorTarget)).all()


def test_create_tiktok_monitor_requires_active_tiktok_account(local_project):
    assert _request("POST", json=_body(
        None, "https://www.tiktok.com/@creator")).status_code == 400
    wrong = store(DouyinAccount(platform="douyin", status="active"))
    assert _request("POST", json=_body(
        wrong, "https://www.tiktok.com/@creator")).status_code == 400
    expired = store(DouyinAccount(platform="tiktok", status="expired"))
    assert _request("POST", json=_body(
        expired, "https://www.tiktok.com/@creator")).status_code == 400


def test_duplicate_tiktok_monitor_rejected(local_project):
    account = store(DouyinAccount(platform="tiktok", status="active"))
    first = _request("POST", json=_body(account, "@creator"))
    assert first.status_code == 200
    dup = _request("POST", json=_body(
        account, "https://www.tiktok.com/@creator/"))
    assert dup.status_code == 409


# ── 注册面 ───────────────────────────────────────────────────────────

def test_tiktok_has_work_monitor_capability():
    assert "tiktok" in pf.keys_with(pf.WORK_MONITOR)
    assert pf.has_cap("tiktok", pf.WORK_MONITOR)
