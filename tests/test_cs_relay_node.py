# -*- coding: utf-8 -*-
"""跨节点客服回投：节点令牌、任务入队/按账号认领/回报、超时回收与幂等。"""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

import app.main as main
from app.cs import service
from app.cs.models import (
    CsRelayJob, RELAY_CLAIMED, RELAY_DONE, RELAY_FAILED, RELAY_PENDING,
    RELAY_TIMEOUT, SENDER_AGENT, SENDER_SYSTEM,
)
from app.db import get_session
from app.models import CommentTask, CommentWatch, DouyinAccount
from sqlmodel import select
from test_project_optimizations import local_project, store  # noqa: F401

from app.cs.relay import _relay_comment

NODE_TOKEN = "test-node-token-123456"


def client(peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


def _node_headers(token=NODE_TOKEN):
    return {"X-CS-Node-Token": token}


@pytest.fixture()
def admin_token(local_project):
    service.create_agent("root", "secret123", "管理员", role="admin")

    async def login():
        async with client() as http:
            r = await http.post("/api/cs/agent/login",
                                json={"username": "root", "password": "secret123"})
            return r.json()["token"]

    return asyncio.run(login())


@pytest.fixture()
def cs_only(local_project, monkeypatch):
    """模拟纯云客服节点：不启动平台引擎，回投一律走跨节点队列。"""
    monkeypatch.setattr(main, "engine", None)
    yield


async def _takeover_and_reply(http, token, *, source, account_id, account_key,
                              thread_key, text="收到，马上处理"):
    """转人工 → 接单 → 坐席回复 → 等入队。返回 (conv_id, message_id, job_id)。"""
    r = await http.post("/api/cs/agent/takeover",
                        headers={"Authorization": "Bearer " + token},
                        json={"source": source, "account_id": account_id,
                              "account_key": account_key,
                              "thread_key": thread_key,
                              "customer_name": "网友"})
    assert r.status_code == 200, r.text
    conv_id = r.json()["id"]
    await http.post(f"/api/cs/agent/conversations/{conv_id}/accept",
                    headers={"Authorization": "Bearer " + token})
    r = await http.post(f"/api/cs/agent/conversations/{conv_id}/messages",
                        headers={"Authorization": "Bearer " + token},
                        json={"text": text})
    assert r.status_code == 200
    await asyncio.sleep(0.2)
    with get_session() as s:
        job = s.exec(select(CsRelayJob).where(
            CsRelayJob.conv_id == conv_id)).first()
        assert job and job.status == RELAY_PENDING
        return conv_id, job.message_id, job.id


def test_node_endpoints_disabled_without_token(cs_only, admin_token):
    service.set_node_token("")

    async def run():
        async with client() as http:
            r = await http.post("/api/cs/node/claim",
                                json={"node_id": "n1", "accounts": []})
            assert r.status_code == 503
            r = await http.get("/api/cs/node/status")
            assert r.status_code == 503

    asyncio.run(run())


def test_node_claim_requires_valid_token(cs_only, admin_token):
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client() as http:
            r = await http.post("/api/cs/node/claim",
                                headers=_node_headers("wrong-token"),
                                json={"node_id": "n1", "accounts": []})
            assert r.status_code == 401
            r = await http.get("/api/cs/node/status", headers=_node_headers())
            assert r.status_code == 200 and r.json()["ok"] is True

    asyncio.run(run())


def test_node_endpoints_reachable_from_lan_peer(cs_only, admin_token):
    """局域网工作台从别的机器来（非回环）：节点接口必须被本机访问
    中间件放行（令牌鉴权由端点自身负责），非客服 API 继续 403。"""
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client(peer="192.168.0.136",
                          base="http://192.168.0.10:8080") as http:
            # 无令牌 → 401（端点鉴权），不能被中间件提前挡成 403
            r = await http.get("/api/cs/node/status")
            assert r.status_code == 401
            r = await http.get("/api/cs/node/status",
                               headers=_node_headers())
            assert r.status_code == 200 and r.json()["ok"] is True
            # 非客服路径的远程访问仍被拒绝
            r = await http.get("/api/accounts")
            assert r.status_code == 403

    asyncio.run(run())


def test_node_worker_direct_host_classification():
    from app.cs.node_worker import _is_direct_host
    # 回环/局域网/链路本地/内网机器名必须直连，绕过系统代理(否则 502)
    assert _is_direct_host("http://127.0.0.1:8080/")
    assert _is_direct_host("http://[::1]:8080/")
    assert _is_direct_host("http://localhost:8080")
    assert _is_direct_host("http://192.168.0.10:8080")
    assert _is_direct_host("http://10.0.0.5:8080")
    assert _is_direct_host("http://172.16.4.4:8080")
    assert _is_direct_host("http://server-pc:8080")
    assert _is_direct_host("http://mmm.local:8080")
    # 公网云服务器仍走系统代理，兼容代理出网的企业网络
    assert not _is_direct_host("http://8.8.8.8:8080")
    assert not _is_direct_host("https://cs.example.com")


def test_cs_public_paths_include_node():
    from app.local_access import cs_public_path
    assert cs_public_path("/api/cs/node/status")
    assert cs_public_path("/api/cs/node/claim")
    assert cs_public_path("/api/cs/agent/login")
    assert cs_public_path("/chat/abc123")
    assert not cs_public_path("/api/accounts")
    assert not cs_public_path("/api/cs/nodes")  # 前缀边界，不得误放行


def test_full_node_claim_success_result(cs_only, admin_token):
    service.set_node_token(NODE_TOKEN)
    local_acct = store(DouyinAccount(platform="douyin", nickname="本机号",
                                     sec_uid="sec-xyz", status="active"))

    async def run():
        async with client() as http:
            conv_id, msg_id, job_id = await _takeover_and_reply(
                http, admin_token, source="douyin_comment",
                account_id=99, account_key="sec-xyz",
                thread_key="aweme1:cid1")

            # 别的节点（没有该账号）认领不到
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "other-node",
                                      "accounts": [{"platform": "xhs",
                                                    "account_key": "aaa",
                                                    "account_id": 1}]})
            assert r.status_code == 200 and r.json()["jobs"] == []

            # 持有该账号的本机节点认领；account_id 替换为本机 id
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "workbench-1",
                                      "accounts": [{"platform": "douyin",
                                                    "account_key": "sec-xyz",
                                                    "account_id": local_acct}]})
            jobs = r.json()["jobs"]
            assert len(jobs) == 1
            job = jobs[0]
            assert job["id"] == job_id
            assert job["account_id"] == local_acct   # 不再是云端的 99
            assert job["thread_key"] == "aweme1:cid1"

            # 重复认领同一任务不会拿到第二条
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "workbench-1",
                                      "accounts": [{"platform": "douyin",
                                                    "account_key": "sec-xyz",
                                                    "account_id": local_acct}]})
            assert r.json()["jobs"] == []

            # 回报成功
            r = await http.post(f"/api/cs/node/jobs/{job_id}/result",
                                headers=_node_headers(),
                                json={"node_id": "workbench-1", "ok": True})
            assert r.status_code == 200
            msgs = (await http.get(
                f"/api/cs/agent/conversations/{conv_id}/messages",
                headers={"Authorization": "Bearer " + admin_token})).json()
            agent_msg = [m for m in msgs if m["id"] == msg_id][0]
            assert agent_msg["relayed"] is True
            assert not any(m["sender_kind"] == SENDER_SYSTEM
                           and "失败" in m["text"] for m in msgs)

            with get_session() as s:
                row = s.get(CsRelayJob, job_id)
                assert row.status == RELAY_DONE and row.claimed_by == "workbench-1"

    asyncio.run(run())


def test_node_failure_result_marks_relayed_and_system_msg(cs_only, admin_token):
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client() as http:
            conv_id, msg_id, job_id = await _takeover_and_reply(
                http, admin_token, source="douyin_dm",
                account_id=0, account_key="sec-zz",
                thread_key="cid-zz")
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "wb",
                                      "accounts": [{"platform": "douyin",
                                                    "account_key": "sec-zz",
                                                    "account_id": 7}]})
            assert len(r.json()["jobs"]) == 1
            r = await http.post(f"/api/cs/node/jobs/{job_id}/result",
                                headers=_node_headers(),
                                json={"node_id": "wb", "ok": False,
                                      "error": "账号登录态已失效"})
            assert r.status_code == 200
            msgs = (await http.get(
                f"/api/cs/agent/conversations/{conv_id}/messages",
                headers={"Authorization": "Bearer " + admin_token})).json()
            assert [m for m in msgs if m["id"] == msg_id][0]["relayed"] is False
            assert any(m["sender_kind"] == SENDER_SYSTEM
                       and "账号登录态已失效" in m["text"] for m in msgs)
            with get_session() as s:
                assert s.get(CsRelayJob, job_id).status == RELAY_FAILED

    asyncio.run(run())


def test_keyless_job_claimed_by_platform_owner(cs_only, admin_token):
    """历史会话没有 account_key 时，同平台节点可认领（节点侧按作品监控兜底）。"""
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client() as http:
            conv_id, _msg_id, job_id = await _takeover_and_reply(
                http, admin_token, source="douyin_comment",
                account_id=0, account_key="",
                thread_key="aweme-old:cid-old")
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "wb",
                                      "accounts": [{"platform": "douyin",
                                                    "account_key": "sec-1",
                                                    "account_id": 3}]})
            jobs = r.json()["jobs"]
            assert len(jobs) == 1 and jobs[0]["id"] == job_id
            assert jobs[0]["account_id"] == 0

    asyncio.run(run())


def test_stale_claim_reclaimed_then_dead(cs_only, admin_token):
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client() as http:
            conv_id, msg_id, job_id = await _takeover_and_reply(
                http, admin_token, source="douyin_dm",
                account_id=0, account_key="sec-q",
                thread_key="cid-q")
            accounts = [{"platform": "douyin", "account_key": "sec-q",
                         "account_id": 5}]
            # 首次认领
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "wb", "accounts": accounts})
            assert len(r.json()["jobs"]) == 1
            # 模拟节点掉线：认领超时且已到最大认领次数
            with get_session() as s:
                row = s.get(CsRelayJob, job_id)
                row.claimed_at = datetime.utcnow() - timedelta(seconds=601)
                row.attempts = service.CLAIM_MAX_ATTEMPTS - 1
                s.add(row)
                s.commit()
            # 再次轮询：任务判超时（结果未知），不再被认领
            r = await http.post("/api/cs/node/claim", headers=_node_headers(),
                                json={"node_id": "wb2", "accounts": accounts})
            assert r.json()["jobs"] == []
            with get_session() as s:
                row = s.get(CsRelayJob, job_id)
                assert row.status == RELAY_TIMEOUT
                assert row.fail_kind == "timeout"
            msgs = (await http.get(
                f"/api/cs/agent/conversations/{conv_id}/messages",
                headers={"Authorization": "Bearer " + admin_token})).json()
            assert any(m["sender_kind"] == SENDER_SYSTEM
                       and "未回报" in m["text"] for m in msgs)

    asyncio.run(run())


def test_result_endpoint_is_idempotent(cs_only, admin_token):
    service.set_node_token(NODE_TOKEN)

    async def run():
        async with client() as http:
            _conv_id, _msg_id, job_id = await _takeover_and_reply(
                http, admin_token, source="douyin_dm",
                account_id=0, account_key="sec-i",
                thread_key="cid-i")
            await http.post("/api/cs/node/claim", headers=_node_headers(),
                            json={"node_id": "wb",
                                  "accounts": [{"platform": "douyin",
                                                "account_key": "sec-i",
                                                "account_id": 1}]})
            payload = {"node_id": "wb", "ok": True}
            r1 = await http.post(f"/api/cs/node/jobs/{job_id}/result",
                                 headers=_node_headers(), json=payload)
            r2 = await http.post(f"/api/cs/node/jobs/{job_id}/result",
                                 headers=_node_headers(), json=payload)
            assert r1.status_code == r2.status_code == 200

    asyncio.run(run())


class _CommentRelayEngine:
    """记录回投时实际选用的账号,不触发真实平台写操作。"""

    def __init__(self):
        self.used_account_id = None
        self.manual_calls = []

    async def execute_comment_task(self, task_id, *, manual=False):
        self.manual_calls.append(bool(manual))
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            self.used_account_id = t.account_id if t else None
        return {"ok": True}


def test_relay_comment_resolves_account_key_to_local_account(local_project):
    """跨节点认领带来 account_key 但无本地 id 时,按稳定标识解析账号。"""
    acct = store(DouyinAccount(platform="douyin", nickname="本机号",
                               sec_uid="sec-match", status="active"))
    engine = _CommentRelayEngine()

    ok, error = asyncio.run(_relay_comment(
        engine, "douyin", 0, "aw1:cid1", "你好", account_key="sec-match"))

    assert ok, error
    assert engine.used_account_id == acct
    # 坐席点发送等同「立即发送」:必须绕过操作节奏等软节流
    assert engine.manual_calls == [True]


def test_relay_comment_falls_back_to_unique_active_account(local_project):
    """无监控绑定/无 account_key 时,同平台唯一有效账号承担回投。"""
    acct = store(DouyinAccount(platform="douyin", nickname="唯一号",
                               sec_uid="sec-only", status="active"))
    engine = _CommentRelayEngine()

    ok, error = asyncio.run(_relay_comment(
        engine, "douyin", 0, "aw2:cid2", "收到"))

    assert ok, error
    assert engine.used_account_id == acct


def test_relay_comment_watch_binding_takes_precedence(local_project):
    """作品监控绑定的抓取账号优先于 account_key 解析。"""
    watched = store(DouyinAccount(platform="douyin", nickname="监控号",
                                  sec_uid="sec-watched", status="active"))
    store(DouyinAccount(platform="douyin", nickname="其他号",
                        sec_uid="sec-other", status="active"))
    store(CommentWatch(platform="douyin", kind="user", sec_uid="creator-x",
                       aweme_id="aw3", mode="public", account_id=watched))
    engine = _CommentRelayEngine()

    ok, error = asyncio.run(_relay_comment(
        engine, "douyin", 0, "aw3:cid3", "谢谢", account_key="sec-other"))

    assert ok, error
    assert engine.used_account_id == watched


def test_relay_comment_multiple_accounts_without_binding_returns_guidance(local_project):
    """多个有效账号且无任何绑定线索时,不替用户抉择,返回可操作的错误。"""
    store(DouyinAccount(platform="douyin", nickname="号A",
                        sec_uid="sec-a", status="active"))
    store(DouyinAccount(platform="douyin", nickname="号B",
                        sec_uid="sec-b", status="active"))
    engine = _CommentRelayEngine()

    ok, error = asyncio.run(_relay_comment(
        engine, "douyin", 0, "aw4:cid4", "在吗"))

    assert not ok
    assert "登录账号" in error
    assert engine.used_account_id is None
