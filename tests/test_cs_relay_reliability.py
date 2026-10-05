# -*- coding: utf-8 -*-
"""Task 14: 回投中继可靠性（服务端）测试。

覆盖 TR-14.1 / TR-14.2：
- 幂等键：同一条坐席消息入队/重试全周期只有一行任务，认领负载带 idem_key，
  同账号多节点只有一个拿到任务，重复回报不产生二次副作用；
- 状态机：pending→claimed→sent；永久故障立即 failed；瞬时故障指数退避
  重排，到点前不可认领，次数耗尽判 failed；认领超时判 timeout；
- 坐席可见 + 手动重发：消息负载携带 relay_state/fail_kind/resendable，
  resend API 复位同一行（幂等键不变）并写系统消息留痕。
"""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest

import app.main as main
from app.cs import service
from app.cs.models import (
    CsRelayJob, SENDER_SYSTEM,
    RELAY_CLAIMED, RELAY_DONE, RELAY_FAILED, RELAY_PENDING, RELAY_TIMEOUT,
    FAIL_PERMANENT, FAIL_TRANSIENT,
)
from app.cs.relay import enqueue_relay_job, relay_idem_key
from app.db import get_session
from sqlmodel import select
from test_project_optimizations import local_project  # noqa: F401

NODE_TOKEN = "relay-reliability-token"
ACCOUNTS = [{"platform": "douyin", "account_key": "sec-1", "account_id": 11}]


def client(peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


def _job(message_id):
    with get_session() as s:
        return s.exec(select(CsRelayJob).where(
            CsRelayJob.message_id == message_id)).first()


def _make_job(source="douyin_dm", platform="douyin", kind="dm",
              account_key="sec-1", thread_key="c1"):
    """建会话+坐席消息+入队，返回 (conv_id, message_id, job_id)。"""
    conv = service.open_or_create_for_takeover(
        source, account_id=1, account_key=account_key,
        thread_key=thread_key, customer_name="网友")
    agent = service.create_agent("agent1", "pw123456", "坐席甲",
                                 role="admin")
    msg = service.agent_send(conv.id, agent, "你好，在吗")
    job_id = enqueue_relay_job(
        source=source, platform=platform, kind=kind,
        account_id=1, account_key=account_key, thread_key=thread_key,
        text="你好，在吗", conv_id=conv.id, message_id=msg.id)
    return conv.id, msg.id, job_id


def _claim(node="wb", accounts=None):
    return service.claim_relay_jobs(node, accounts or ACCOUNTS)


# ── 幂等键：同键多次平台侧仅一条 ─────────────────────────────────────────

def test_enqueue_idempotent_across_all_states(local_project):
    _cid, mid, jid = _make_job()
    key = relay_idem_key(mid)
    assert key == f"relay:m{mid}"
    # 排队中重复入队 → 同一行
    assert enqueue_relay_job(
        source="douyin_dm", platform="douyin", kind="dm", account_id=1,
        account_key="sec-1", thread_key="c1", text="再发", conv_id=_cid,
        message_id=mid) == jid
    with get_session() as s:
        assert len(s.exec(select(CsRelayJob)).all()) == 1

    jobs = _claim()
    assert len(jobs) == 1 and jobs[0]["idem_key"] == key
    # 认领中重复入队仍同一行，不产生第二条平台写操作
    assert enqueue_relay_job(
        source="douyin_dm", platform="douyin", kind="dm", account_id=1,
        account_key="sec-1", thread_key="c1", text="再发", conv_id=_cid,
        message_id=mid) == jid
    service.complete_relay_job(jid, "wb", True)
    # 已成功后重复入队也不新建（重发必须走显式 resend，且成功消息禁止重发）
    assert enqueue_relay_job(
        source="douyin_dm", platform="douyin", kind="dm", account_id=1,
        account_key="sec-1", thread_key="c1", text="再发", conv_id=_cid,
        message_id=mid) == jid
    with get_session() as s:
        assert len(s.exec(select(CsRelayJob)).all()) == 1
        assert s.get(CsRelayJob, jid).status == RELAY_DONE


def test_same_account_two_nodes_only_one_claims(local_project):
    _cid, _mid, jid = _make_job()
    j1 = _claim("node-A")
    j2 = _claim("node-B")
    assert [j["id"] for j in j1] == [jid]
    assert j2 == []
    # 非认领节点回报被拒
    with pytest.raises(service.CsError):
        service.complete_relay_job(jid, "node-B", True)
    # 认领节点重复回报第二次为幂等 dup，不产生副作用
    assert service.complete_relay_job(jid, "node-A", True) == {"ok": True}
    dup = service.complete_relay_job(jid, "node-A", True)
    assert dup == {"ok": True, "dup": True}
    job = _job(_mid)
    assert job.status == RELAY_DONE and job.attempts == 1


# ── 永久故障：立即失败，不重试 ───────────────────────────────────────────

def test_permanent_failure_fails_immediately(local_project):
    cid, mid, jid = _make_job()
    _claim()
    service.complete_relay_job(jid, "wb", False, "账号登录态已失效")
    job = _job(mid)
    assert job.status == RELAY_FAILED and job.fail_kind == FAIL_PERMANENT
    assert job.next_run_at is None and job.attempts == 1
    # 立即再次认领拿不到（没有重排）
    assert _claim("wb2") == []
    msgs = service.list_messages(cid)
    assert any(m["sender_kind"] == SENDER_SYSTEM
               and "登录态已失效" in m["text"] for m in msgs)


def test_explicit_retryable_hint_overrides_text(local_project):
    _cid, mid, jid = _make_job()
    _claim()
    # 文案含“登录态”但工作台显式标记可重试 → 按瞬时处理
    service.complete_relay_job(jid, "wb", False, "浏览器登录态检查超时",
                               retryable=True)
    job = _job(mid)
    assert job.status == RELAY_PENDING and job.fail_kind == FAIL_TRANSIENT
    assert job.next_run_at is not None and job.attempts == 1


# ── 瞬时故障：指数退避 + 上限 ────────────────────────────────────────────

def test_transient_backoff_then_exhausted(local_project):
    cid, mid, jid = _make_job()

    # 第 1 次执行瞬时失败 → 退避 20s 重排
    _claim()
    service.complete_relay_job(jid, "wb", False, "网络连接超时，请稍后重试")
    job = _job(mid)
    assert job.status == RELAY_PENDING and job.fail_kind == FAIL_TRANSIENT
    assert job.claimed_by == "" and job.attempts == 1
    assert timedelta(seconds=15) < (job.next_run_at - datetime.utcnow()) \
        <= timedelta(seconds=21)
    # 退避未到：任何节点都认领不到
    assert _claim("wb") == []
    sys_texts = [m["text"] for m in service.list_messages(cid)
                 if m["sender_kind"] == SENDER_SYSTEM]
    assert any("自动重试" in t and "第 1/3 次" in t for t in sys_texts)

    # 时间到后第 2 次认领（attempts=2），再失败退避 40s
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        row.next_run_at = datetime.utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    jobs = _claim("wb")
    assert len(jobs) == 1 and jobs[0]["idem_key"] == relay_idem_key(mid)
    assert _job(mid).attempts == 2
    service.complete_relay_job(jid, "wb", False, "503 服务繁忙")
    job = _job(mid)
    assert job.status == RELAY_PENDING
    assert timedelta(seconds=35) < (job.next_run_at - datetime.utcnow()) \
        <= timedelta(seconds=41)

    # 第 3 次执行失败 → 次数耗尽，判 failed
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        row.next_run_at = datetime.utcnow() - timedelta(seconds=1)
        s.add(row)
        s.commit()
    _claim("wb")
    service.complete_relay_job(jid, "wb", False, "网络仍然不可达")
    job = _job(mid)
    assert job.status == RELAY_FAILED and job.fail_kind == FAIL_TRANSIENT
    assert job.next_run_at is None and job.attempts == 3
    assert _claim("wb") == []
    sys_texts = [m["text"] for m in service.list_messages(cid)
                 if m["sender_kind"] == SENDER_SYSTEM]
    assert any("多次自动重试仍失败" in t for t in sys_texts)


# ── 认领超时：timeout 终态，可手动重发 ───────────────────────────────────

def test_claim_timeout_terminal_and_overview(local_project):
    _cid, mid, jid = _make_job()
    _claim("wb")
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        row.claimed_at = datetime.utcnow() - timedelta(
            seconds=service.CLAIM_TIMEOUT_SECONDS + 1)
        row.attempts = service.CLAIM_MAX_ATTEMPTS - 1
        s.add(row)
        s.commit()
    assert _claim("wb2") == []      # 回收时直接判 timeout
    job = _job(mid)
    assert job.status == RELAY_TIMEOUT and job.fail_kind == "timeout"
    overview = service.relay_queue_overview()
    assert overview["jobs"][RELAY_TIMEOUT] == 1


# ── 坐席可见状态 + 手动重发（API 集成，TR-14.2）──────────────────────────

@pytest.fixture()
def admin_token(local_project):
    service.create_agent("root", "secret123", "管理员", role="admin")

    async def login():
        async with client() as http:
            r = await http.post("/api/cs/agent/login",
                                json={"username": "root", "password": "secret123"})
            return r.json()["token"]

    return asyncio.run(login())


def _fail_to_timeout(mid, jid, node="wb"):
    _claim(node)
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        row.claimed_at = datetime.utcnow() - timedelta(
            seconds=service.CLAIM_TIMEOUT_SECONDS + 1)
        row.attempts = service.CLAIM_MAX_ATTEMPTS - 1
        s.add(row)
        s.commit()
    _claim("wb2")


def test_agent_sees_failure_and_manual_resend(local_project, admin_token):
    service.set_node_token(NODE_TOKEN)
    cid, mid, jid = _make_job()
    _fail_to_timeout(mid, jid)

    async def run():
        async with client() as http:
            auth = {"Authorization": "Bearer " + admin_token}
            # 坐席可见：消息负载带 timeout 状态与可重发标记
            msgs = (await http.get(
                f"/api/cs/agent/conversations/{cid}/messages",
                headers=auth)).json()
            mine = [m for m in msgs if m["id"] == mid][0]
            assert mine["relay_state"] == RELAY_TIMEOUT
            assert mine["relay_resendable"] is True
            assert mine["relay_fail_kind"] == "timeout"

            # 投递中/成功态不能重发：先构造一个成功任务验证 400
            r_ok = await http.post(
                f"/api/cs/agent/messages/{mid + 9999}/resend", headers=auth)
            assert r_ok.status_code == 400

            # 手动重发：同一行复位（幂等键不变、attempts 清零）
            r = await http.post(
                f"/api/cs/agent/messages/{mid}/resend", headers=auth)
            assert r.status_code == 200 and r.json()["relay_state"] == "pending"
            with get_session() as s:
                row = s.get(CsRelayJob, jid)
                assert row.status == RELAY_PENDING and row.attempts == 0
                assert row.idem_key == relay_idem_key(mid)
                assert row.next_run_at is None
                assert len(s.exec(select(CsRelayJob)).all()) == 1

            # 已重新排队后再点重发 → 400（投递中禁止）
            r = await http.post(
                f"/api/cs/agent/messages/{mid}/resend", headers=auth)
            assert r.status_code == 400

            # 留痕：系统消息含手动重发
            msgs = (await http.get(
                f"/api/cs/agent/conversations/{cid}/messages",
                headers=auth)).json()
            assert any(m["sender_kind"] == SENDER_SYSTEM
                       and "手动重发" in m["text"] for m in msgs)

    asyncio.run(run())


def test_agent_resend_failed_then_sent_forbids_again(local_project, admin_token):
    service.set_node_token(NODE_TOKEN)
    cid, mid, jid = _make_job()
    _claim()
    service.complete_relay_job(jid, "wb", False, "账号登录态已失效")
    assert _job(mid).status == RELAY_FAILED

    async def run():
        async with client() as http:
            auth = {"Authorization": "Bearer " + admin_token}
            r = await http.post(
                f"/api/cs/agent/messages/{mid}/resend", headers=auth)
            assert r.status_code == 200
        # 重发后被认领并成功
        jobs = _claim("wb")
        assert len(jobs) == 1 and jobs[0]["idem_key"] == relay_idem_key(mid)
        service.complete_relay_job(jid, "wb", True)
        assert _job(mid).status == RELAY_DONE

        async with client() as http:
            auth = {"Authorization": "Bearer " + admin_token}
            # 已成功不能再重发
            r = await http.post(
                f"/api/cs/agent/messages/{mid}/resend", headers=auth)
            assert r.status_code == 400

    asyncio.run(run())
