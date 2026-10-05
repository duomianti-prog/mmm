"""Task 21:视频号不可行能力在接口层同步拒绝(TR-21.1)。

前端已按平台能力隐藏入口(app.js switchTab/switchHubTab 能力门 +
.notsh-only),本测试保证绕过 UI 直调 API 时,后端对 shipinhao 缺失的
对外能力一律返回 400 与明确原因,而不是静默降级到默认平台后空转:
  - 作品监控 / 评论监控 / 自动评论 / 关键词采集(按 body.platform)
  - 私信同步 / 关注粉丝同步(按账号 platform)
另含 registry 能力原因表的纯函数单测。
"""
import asyncio

import httpx
import pytest

import app.main as main
from app.models import DouyinAccount
from app.platforms import registry as platforms
from test_project_optimizations import local_project, store


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


# ───────────────────────── registry 纯函数 ─────────────────────────

def test_shipinhao_blocked_reasons_exist_for_all_external_caps():
    for cap in (platforms.WORK_MONITOR, platforms.COMMENT_MONITOR,
                platforms.KEYWORD_COLLECTION, platforms.AUTO_COMMENT,
                platforms.DM, platforms.FOLLOW_SYNC):
        reason = platforms.blocked_capability_reason("shipinhao", cap)
        assert reason and "视频号" in reason, cap


def test_shipinhao_own_capabilities_not_blocked():
    assert platforms.blocked_capability_reason("shipinhao", platforms.OWN_WORKS) is None
    assert platforms.blocked_capability_reason("shipinhao", platforms.PUBLISH) is None


def test_unknown_and_other_platforms_keep_legacy_behavior():
    # 未知平台不阻断(各端点保持默认平台兼容)
    assert platforms.blocked_capability_reason("mars", platforms.WORK_MONITOR) is None
    assert platforms.blocked_capability_reason("", platforms.DM) is None
    # 抖音具备全部能力
    for cap in (platforms.WORK_MONITOR, platforms.DM, platforms.FOLLOW_SYNC):
        assert platforms.blocked_capability_reason("douyin", cap) is None


# ───────────────────────── body.platform 类创建端点 ─────────────────────────

def test_create_work_monitor_blocked_for_shipinhao(local_project):
    r = request("POST", "/api/monitors", json={
        "platform": "shipinhao", "url_or_secuid": "finder/abc",
        "download_enabled": False})
    assert r.status_code == 400
    assert "他人作品监控" in r.json()["detail"]


def test_create_comment_watch_blocked_for_shipinhao(local_project):
    r = request("POST", "/api/comment-watches", json={
        "platform": "shipinhao", "url_or_id": "xxx", "account_id": 1})
    assert r.status_code == 400
    assert "本账号作品" in r.json()["detail"]


def test_create_comment_rule_blocked_for_shipinhao(local_project):
    r = request("POST", "/api/comment-rules", json={
        "platform": "shipinhao", "account_id": 1,
        "templates": ["好的"], "target_kind": "self"})
    assert r.status_code == 400
    assert "自动评论" in r.json()["detail"] or "本账号作品" in r.json()["detail"]


def test_keyword_collection_blocked_for_shipinhao(local_project):
    r = request("POST", "/api/collections", json={
        "platform": "shipinhao", "account_id": 1, "keywords": ["美食"]})
    assert r.status_code == 400
    assert "关键词" in r.json()["detail"]


def test_unknown_platform_still_falls_back_not_capability_blocked(local_project):
    # 历史兼容:未知平台走默认抖音逻辑,不应命中能力闸门
    # (抖音账号校验先发生,返回的是账号相关 400 而非视频号能力原因)
    r = request("POST", "/api/monitors", json={
        "platform": "mars", "url_or_secuid": "abc", "download_enabled": False})
    assert "视频号" not in r.text


# ───────────────────────── 账号级能力端点(私信/关注) ─────────────────────────

@pytest.fixture
def channels_account(local_project):
    return store(DouyinAccount(platform="shipinhao", status="active",
                               storage_state="{}", creator_storage_state="{}"))


def test_dm_sync_blocked_for_shipinhao_account(local_project, channels_account):
    r = request("POST", f"/api/accounts/{channels_account}/dm/sync")
    assert r.status_code == 400
    assert "视频号" in r.json()["detail"] and "私信" in r.json()["detail"]


@pytest.mark.parametrize("direction", ["following", "fan"])
def test_follow_sync_blocked_for_shipinhao_account(local_project,
                                                   channels_account, direction):
    r = request("POST",
                f"/api/accounts/{channels_account}/follows/sync",
                params={"direction": direction})
    assert r.status_code == 400
    assert "关注/粉丝" in r.json()["detail"]


@pytest.mark.parametrize("direction", ["following", "fan"])
def test_follow_sync_job_blocked_for_shipinhao_account(local_project,
                                                       channels_account, direction):
    r = request("POST",
                f"/api/accounts/{channels_account}/follows/sync-jobs",
                params={"direction": direction})
    assert r.status_code == 400
    assert "关注/粉丝" in r.json()["detail"]


def test_missing_account_still_404_before_capability_gate(local_project):
    r = request("POST", "/api/accounts/999999/dm/sync")
    assert r.status_code == 404
