# -*- coding: utf-8 -*-
"""Task 15: 回投节点健壮性测试。

覆盖 TR-15.1（离线-恢复演练：离线排队、恢复续投、不重复）与
TR-15.2 配套的节点侧幂等执行/回报不丢/渐进退避/状态可观测。
多节点同账号 claim 互斥的服务端语义已在 test_cs_relay_reliability.py
::test_same_account_two_nodes_only_one_claims 覆盖，此处不重复。
"""
import asyncio
import json
import time
from datetime import datetime, timedelta

import httpx
import pytest

import app.main as main
from app.cs import node_worker as nw
from app.cs import service
from app.cs.models import (
    CsRelayJob, RELAY_CLAIMED, RELAY_DONE,
)
from app.cs.relay import enqueue_relay_job
from app.db import get_session
from test_project_optimizations import local_project  # noqa: F401


# ── 桩：HTTP 客户端 / relay_message ──────────────────────────────────────

class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _StubClient:
    """按脚本顺序应答 POST；脚本耗尽默认 200 {}。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    async def post(self, url, json=None, **kw):
        self.calls.append({"url": url, "json": json})
        item = self.script.pop(0) if self.script else _Resp()
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Relay:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    async def __call__(self, snapshot, text):
        self.calls.append((snapshot, text))
        return self.results.pop(0) if self.results else {"ok": True}


def make_job(job_id=1, mid=77, idem="relay:m77"):
    return {"id": job_id, "conv_id": 5, "message_id": mid, "idem_key": idem,
            "source": "douyin_dm", "platform": "douyin", "kind": "dm",
            "account_id": 11, "account_key": "sec-1", "thread_key": "t1",
            "text": "hi"}


def patch_worker(monkeypatch, client, relay):
    monkeypatch.setattr(nw, "node_config", lambda: {
        "enabled": True, "url": "http://cs", "has_token": True,
        "node_id": "wb-test", "status": {}})
    monkeypatch.setattr(
        nw, "_local_accounts",
        lambda: [{"platform": "douyin", "account_key": "sec-1",
                  "account_id": 11}])
    monkeypatch.setattr(nw, "_http_client",
                        lambda *, url, headers=None: client)
    monkeypatch.setattr(nw, "relay_message", relay)


def result_posts(client):
    return [c for c in client.calls if "/result" in c["url"]]


# ── claim 幂等：回收重发不重复执行（TR-15.1 核心）────────────────────────

def test_reclaimed_job_not_reexecuted_and_report_flushed(local_project,
                                                         monkeypatch):
    # 第 1 轮：执行成功，但回报被服务端 500 拒收 → 进待报队列
    c1 = _StubClient([_Resp(200, {"jobs": [make_job()]}), _Resp(500, text="x")])
    relay = _Relay([{"ok": True}])
    patch_worker(monkeypatch, c1, relay)
    assert asyncio.run(nw.RelayNodeWorker()._tick()) is False
    assert len(relay.calls) == 1
    assert nw._recall_result("relay:m77")["ok"] is True
    assert nw._pending_count() == 1

    # 第 2 轮：先补报成功；服务端此前已把该任务超时回收重发 → 再次认领到
    # 同一任务（同 idem_key），节点必须不再执行平台写操作，只补报缓存结果
    c2 = _StubClient([_Resp(200),                       # 补报成功
                      _Resp(200, {"jobs": [make_job()]}),
                      _Resp(200)])                      # 缓存结果补报成功
    patch_worker(monkeypatch, c2, relay)
    assert asyncio.run(nw.RelayNodeWorker()._tick()) is False
    assert len(relay.calls) == 1                        # 只执行过一次
    posts = result_posts(c2)
    assert len(posts) == 2
    assert posts[0]["json"]["ok"] is True               # 上轮欠账补报
    assert posts[1]["json"]["ok"] is True               # 缓存结果补报
    assert nw._pending_count() == 0
    assert nw._recall_result("relay:m77")["ok"] is True


def test_legacy_job_without_idem_key_uses_message_id(local_project,
                                                     monkeypatch):
    legacy = make_job(job_id=7, mid=90, idem="")
    c = _StubClient([_Resp(200, {"jobs": [legacy]}), _Resp(200)])
    relay = _Relay([{"ok": False, "error": "账号登录态已失效"}])
    patch_worker(monkeypatch, c, relay)
    asyncio.run(nw.RelayNodeWorker()._tick())
    cached = nw._recall_result("relay:m90")
    assert cached is not None and cached["ok"] is False
    # 回报 payload 不带 retryable（交服务端按文案分类）
    assert "retryable" not in result_posts(c)[0]["json"]


# ── 结果回报语义：retryable 提示 ─────────────────────────────────────────

def test_engine_queued_reports_retryable_false(local_project, monkeypatch):
    c = _StubClient([_Resp(200, {"jobs": [make_job(2, 88, "relay:m88")]}),
                     _Resp(200)])
    patch_worker(monkeypatch, c, _Relay([{"state": "queued"}]))
    asyncio.run(nw.RelayNodeWorker()._tick())
    body = result_posts(c)[0]["json"]
    assert body["ok"] is False and body["retryable"] is False
    assert "引擎未运行" in body["error"]
    assert nw._recall_result("relay:m88")["retryable"] is False


def test_transient_platform_error_left_to_server_classification(
        local_project, monkeypatch):
    c = _StubClient([_Resp(200, {"jobs": [make_job(3, 99, "relay:m99")]}),
                     _Resp(200)])
    patch_worker(monkeypatch, c,
                 _Relay([{"ok": False, "error": "网络连接超时"}]))
    asyncio.run(nw.RelayNodeWorker()._tick())
    body = result_posts(c)[0]["json"]
    assert body["ok"] is False and "retryable" not in body
    assert nw._recall_result("relay:m99")["retryable"] is None


# ── 回报不丢：待报队列补报 / 过期放弃 ────────────────────────────────────

def test_stale_pending_dropped_fresh_flushed_before_claim(local_project,
                                                          monkeypatch):
    nw._save_json_setting(nw.SET_PENDING_REPORTS, [
        {"job_id": 9, "ok": True, "error": "", "retryable": None,
         "at": time.time() - nw.REPORT_MAX_AGE_SECONDS - 100},
        {"job_id": 8, "ok": False, "error": "网络", "retryable": True,
         "at": time.time() - 10},
    ])
    c = _StubClient([_Resp(200),                    # job 8 补报成功
                     _Resp(200, {"jobs": []})])     # claim 空
    patch_worker(monkeypatch, c, _Relay())
    assert asyncio.run(nw.RelayNodeWorker()._tick()) is False
    assert nw._pending_count() == 0                 # 过期项放弃 + 成功项移除
    assert "/jobs/8/result" in c.calls[0]["url"]    # 补报先于 claim
    assert c.calls[0]["json"]["retryable"] is True


# ── 轮询退避（渐进指数，封顶）────────────────────────────────────────────

def test_error_backoff_progression():
    assert nw.node_error_backoff(1) == 30
    assert nw.node_error_backoff(2) == 60
    assert nw.node_error_backoff(3) == 120
    assert nw.node_error_backoff(4) == 240
    assert nw.node_error_backoff(5) == nw.MAX_BACKOFF_SECONDS == 300
    assert nw.node_error_backoff(9) == 300


# ── 状态可观测 ───────────────────────────────────────────────────────────

def test_status_reports_pending_and_executed(local_project, monkeypatch):
    relay = _Relay([{"ok": True}, {"ok": True}])
    c1 = _StubClient([_Resp(200, {"jobs": [make_job()]}), _Resp(200)])
    patch_worker(monkeypatch, c1, relay)
    asyncio.run(nw.RelayNodeWorker()._tick())
    st = nw._load_status()
    assert st["connected"] is True and st["executed_count"] == 1
    assert st["pending"] == 0

    c2 = _StubClient([_Resp(200, {"jobs": [make_job(3, 99, "relay:m99")]}),
                      _Resp(503, text="down")])
    patch_worker(monkeypatch, c2, relay)
    asyncio.run(nw.RelayNodeWorker()._tick())
    st = nw._load_status()
    assert st["pending"] == 1 and st["connected"] is True


# ── TR-15.1 集成：离线排队 → 恢复续投 → 不重复（真实 App + 回收路径）────

def _make_server_job(account_key="sec-1", thread_key="t15"):
    conv = service.open_or_create_for_takeover(
        "douyin_dm", account_id=1, thread_key=thread_key,
        customer_name="网友", account_key=account_key)
    agent = service.create_agent("ag15", "pw123456", "坐席", role="admin")
    msg = service.agent_send(conv.id, agent, "你好")
    jid = enqueue_relay_job(
        source="douyin_dm", platform="douyin", kind="dm", account_id=1,
        account_key=account_key, thread_key=thread_key, text="你好",
        conv_id=conv.id, message_id=msg.id)
    return conv.id, msg.id, jid


class _DropResult:
    """转发真实 ASGI 请求；/result 按共享策略脚本放行或掐断（模拟断网）。"""

    def __init__(self, inner, policy):
        self._inner = inner
        self._policy = policy          # 共享 list，跨 tick 消费

    async def post(self, url, json=None, **kw):
        if "/result" in url:
            act = self._policy.pop(0) if self._policy else "ok"
            if act == "fail":
                raise httpx.ConnectError("网络中断")
        return await self._inner.post(url, json=json, **kw)

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *a):
        return await self._inner.__aexit__(*a)


def test_offline_recover_no_repeat_integration(local_project, monkeypatch):
    service.set_node_token("tok15")
    conv_id, msg_id, jid = _make_server_job()   # 节点离线期：任务已在服务端排队

    relay = _Relay([{"ok": True}])
    monkeypatch.setattr(nw, "node_config", lambda: {
        "enabled": True, "url": "http://cs", "has_token": True,
        "node_id": "wb-int", "status": {}})
    monkeypatch.setattr(
        nw, "_local_accounts",
        lambda: [{"platform": "douyin", "account_key": "sec-1",
                  "account_id": 11}])
    monkeypatch.setattr(nw, "relay_message", relay)

    def asgi_factory(policy):
        def _f(*, url, headers=None):
            inner = httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=main.app, client=("127.0.0.1", 1234)),
                base_url="http://cs",
                headers={"X-CS-Node-Token": "tok15"})
            return _DropResult(inner, policy)
        return _f

    worker = nw.RelayNodeWorker()

    # 阶段 A：节点恢复后第一轮——执行成功，但结果回报被网络掐断
    monkeypatch.setattr(nw, "_http_client", asgi_factory(["fail"]))
    asyncio.run(worker._tick())
    assert len(relay.calls) == 1
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        assert row.status == RELAY_CLAIMED       # 服务端仍在等回报
    assert nw._pending_count() == 1

    # 服务端认领超时，回收该任务（claimed_at 过期，attempts 1 < 3）
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        row.claimed_at = datetime.utcnow() - timedelta(
            seconds=service.CLAIM_TIMEOUT_SECONDS + 5)
        s.add(row)
        s.commit()

    # 阶段 B：补报仍被掐断一次；claim 触发回收重发 → 节点凭幂等键拒绝
    # 重复执行，只补报缓存结果 → 服务端落 done
    monkeypatch.setattr(nw, "_http_client", asgi_factory(["fail"]))
    asyncio.run(worker._tick())
    assert len(relay.calls) == 1                 # 平台写操作始终只有一次
    with get_session() as s:
        row = s.get(CsRelayJob, jid)
        assert row.status == RELAY_DONE
        assert row.attempts == 3                 # 原认领(1) → 回收(2) → 重领(3)

    # 阶段 C：网络恢复——补报积压欠账（服务端幂等，dup 无副作用）后清空
    monkeypatch.setattr(nw, "_http_client", asgi_factory([]))
    asyncio.run(worker._tick())
    assert nw._pending_count() == 0
    from app.cs.models import CsMessage
    with get_session() as s:
        m = s.get(CsMessage, msg_id)
        assert m.relayed is True
