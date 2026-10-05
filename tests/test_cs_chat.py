# -*- coding: utf-8 -*-
"""人工客服 IM 端到端回归：访客链接、队列/接单/转接/邀请/关闭、公网豁免、回投失败留痕。"""
import asyncio
import time

import httpx
import pytest

import app.main as main
from app.cs import service
from app.cs.models import SENDER_AGENT, SENDER_CUSTOMER, SENDER_SYSTEM, STATUS_ACTIVE, STATUS_CLOSED, STATUS_QUEUED
from app.models import DmConversation, DouyinAccount
from test_project_optimizations import local_project, store  # noqa: F401


def client(peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


@pytest.fixture()
def agents():
    admin = service.create_agent("admin1", "secret123", "管理员", role="admin")
    alice = service.create_agent("alice", "secret123", "小爱")
    bob = service.create_agent("bob", "secret123", "小波")
    return SimpleNamespaceish(admin=admin, alice=alice, bob=bob)


class SimpleNamespaceish:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _auth(token):
    return {"Authorization": "Bearer " + token}


def test_cs_full_flow(local_project, monkeypatch, agents):
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            # ── 登录 / me / 错误密码限流前的友好报错 ──
            r = await http.post("/api/cs/agent/login",
                                json={"username": "alice", "password": "wrong"})
            assert r.status_code == 400
            r = await http.post("/api/cs/agent/login",
                                json={"username": "alice", "password": "secret123"})
            assert r.status_code == 200
            alice_token = r.json()["token"]
            assert r.json()["agent"]["display_name"] == "小爱"

            r = await http.post("/api/cs/agent/login",
                                json={"username": "bob", "password": "secret123"})
            bob_token = r.json()["token"]
            r = await http.post("/api/cs/agent/login",
                                json={"username": "admin1", "password": "secret123"})
            admin_token = r.json()["token"]

            assert (await http.get("/api/cs/agent/me",
                                   headers=_auth(alice_token))).json()["username"] == "alice"
            # 无 token 401
            assert (await http.get("/api/cs/agent/conversations")).status_code == 401

            # ── 坐席管理：管理员建号 / 非管理员 403 ──
            r = await http.get("/api/cs/agent/admin/agents", headers=_auth(alice_token))
            assert r.status_code == 403
            r = await http.post("/api/cs/agent/admin/agents", headers=_auth(admin_token),
                                json={"username": "carol", "password": "secret123",
                                      "display_name": "小客", "role": "agent"})
            assert r.status_code == 200
            carol_id = r.json()["id"]
            # 重名报错
            r = await http.post("/api/cs/agent/admin/agents", headers=_auth(admin_token),
                                json={"username": "carol", "password": "secret123"})
            assert r.status_code == 400
            # 停用后 carol 无法登录
            r = await http.put(f"/api/cs/agent/admin/agents/{carol_id}/enabled",
                               headers=_auth(admin_token), json={"enabled": False})
            assert r.status_code == 200
            r = await http.post("/api/cs/agent/login",
                                json={"username": "carol", "password": "secret123"})
            assert r.status_code == 400
            await http.put(f"/api/cs/agent/admin/agents/{carol_id}/enabled",
                           headers=_auth(admin_token), json={"enabled": True})

            # ── 新建访客链接（未发言前不计入排队） ──
            r = await http.post("/api/cs/agent/links", headers=_auth(alice_token),
                                json={"customer_name": "王先生"})
            assert r.status_code == 200
            link = r.json()
            assert link["url"].endswith("/chat/" + link["token"])
            counts = (await http.get("/api/cs/agent/counts",
                                     headers=_auth(alice_token))).json()
            assert counts["queued"] == 0

            # ── 访客公网访问（非回环 + 外部域名，中间件应放行客服路径） ──
            async with client(peer="203.0.113.9", base="http://mm.example.com") as pub:
                r = await pub.get("/api/cs/guest/" + link["token"])
                assert r.status_code == 200
                assert r.json()["conversation"]["status"] == STATUS_QUEUED
                # 其它 API 公网仍然 403
                assert (await pub.get("/api/accounts")).status_code == 403
                # 访客页 HTML
                r = await pub.get("/chat/" + link["token"])
                assert r.status_code == 200 and "mmm" in r.text
                # 错误 token 404
                assert (await pub.get("/api/cs/guest/nope")).status_code == 404
                # 访客发言
                r = await pub.post("/api/cs/guest/" + link["token"] + "/messages",
                                   json={"text": "你好，在吗？", "nickname": "王先生"})
                assert r.status_code == 200
                await pub.post("/api/cs/guest/" + link["token"] + "/messages",
                               json={"text": "我想问下价格"})

            # ── 队列出现；两个坐席都能看到 ──
            r = await http.get("/api/cs/agent/conversations?status=queued",
                               headers=_auth(alice_token))
            queued = r.json()
            assert len(queued) == 1 and queued[0]["last_text"] == "我想问下价格"
            conv_id = queued[0]["id"]
            assert (await http.get("/api/cs/agent/conversations?status=queued",
                                   headers=_auth(bob_token))).json()

            # ── 接单 ──
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/accept",
                                headers=_auth(alice_token))
            assert r.status_code == 200
            assert r.json()["status"] == STATUS_ACTIVE
            assert r.json()["owner"]["username"] == "alice"
            # bob 此时在进行中列表看不到（非参与人），但 alice 能
            assert not [c for c in (await http.get(
                "/api/cs/agent/conversations?status=active",
                headers=_auth(bob_token))).json() if c["id"] == conv_id]

            # ── 坐席回复：访客轮询可见，含系统接入消息 ──
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/messages",
                                headers=_auth(alice_token), json={"text": "在的，请问咨询哪款？"})
            assert r.status_code == 200
            r = await http.get("/api/cs/guest/" + link["token"])
            kinds = [m["sender_kind"] for m in r.json()["messages"]]
            assert SENDER_CUSTOMER in kinds and SENDER_AGENT in kinds
            assert any(m["text"] == "在的，请问咨询哪款？" for m in r.json()["messages"])

            # ── 邀请 bob 协作：bob 可见可发言 ──
            r = await http.get("/api/cs/agent/agents", headers=_auth(alice_token))
            picker = {a["username"]: a["id"] for a in r.json()}
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/invite",
                                headers=_auth(alice_token),
                                json={"agent_id": picker["bob"]})
            assert r.status_code == 200
            assert {p["username"] for p in r.json()["participants"]} >= {"alice", "bob"}
            # 重复邀请报错
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/invite",
                                headers=_auth(alice_token),
                                json={"agent_id": picker["bob"]})
            assert r.status_code == 400
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/messages",
                                headers=_auth(bob_token), json={"text": "我也来帮忙看看"})
            assert r.status_code == 200

            # ── 转接给 bob 后 alice 仍可见（历史参与人） ──
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/transfer",
                                headers=_auth(alice_token),
                                json={"agent_id": picker["bob"]})
            assert r.status_code == 200
            assert r.json()["owner"]["username"] == "bob"
            assert (await http.get(f"/api/cs/agent/conversations/{conv_id}",
                                   headers=_auth(alice_token))).status_code == 200

            # ── 已读清零 + 关闭 + 访客再发言自动重新排队 ──
            await http.post(f"/api/cs/agent/conversations/{conv_id}/read",
                            headers=_auth(bob_token))
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/close",
                                headers=_auth(bob_token))
            assert r.json()["status"] == STATUS_CLOSED
            # 已关闭会话坐席不能再发言
            r = await http.post(f"/api/cs/agent/conversations/{conv_id}/messages",
                                headers=_auth(bob_token), json={"text": "还在吗"})
            assert r.status_code == 400
            async with client(peer="203.0.113.9", base="http://mm.example.com") as pub:
                r = await pub.post("/api/cs/guest/" + link["token"] + "/messages",
                                   json={"text": "有人吗？"})
                assert r.json()["status"] == STATUS_QUEUED

            # ── 平台私信转人工：无引擎时回复进入跨节点回投队列（不再立即判失败） ──
            monkeypatch.setattr(main, "engine", None)   # 模拟纯云客服节点
            account_id = store(DouyinAccount(nickname="抖音号A", sec_uid="sec-A"))
            store(DmConversation(
                platform="douyin", account_id=account_id, conv_id="cid-1",
                peer_uid="u1", peer_sec_uid="s1", peer_nickname="抖音网友"))
            r = await http.post("/api/cs/agent/takeover", headers=_auth(alice_token),
                                json={"source": "douyin_dm", "account_id": account_id,
                                      "account_key": "sec-A",
                                      "thread_key": "cid-1", "customer_name": "抖音网友",
                                      "first_message": "评论区来的"})
            assert r.status_code == 200
            assert r.json()["account_key"] == "sec-A"
            tk_id = r.json()["id"]
            await http.post(f"/api/cs/agent/conversations/{tk_id}/accept",
                            headers=_auth(alice_token))
            r = await http.post(f"/api/cs/agent/conversations/{tk_id}/messages",
                                headers=_auth(alice_token), json={"text": "您好，已加您私信"})
            assert r.status_code == 200 and r.json()["relay"] is True
            # 等后台回投入队列落定
            await asyncio.sleep(0.3)
            msgs = (await http.get(f"/api/cs/agent/conversations/{tk_id}/messages",
                                   headers=_auth(alice_token))).json()
            agent_msgs = [m for m in msgs if m["sender_kind"] == SENDER_AGENT]
            assert agent_msgs and agent_msgs[-1]["relayed"] is False
            # 入队期间不应有"失败"系统消息（等待工作台认领）
            assert not any(m["sender_kind"] == SENDER_SYSTEM and "失败" in m["text"]
                           for m in msgs)
            from app.cs.models import CsRelayJob, RELAY_PENDING
            from app.db import get_session as _gs
            from sqlmodel import select as _sel
            with _gs() as _s:
                jobs = _s.exec(_sel(CsRelayJob).where(
                    CsRelayJob.conv_id == tk_id)).all()
            assert len(jobs) == 1 and jobs[0].status == RELAY_PENDING
            assert jobs[0].account_key == "sec-A"

            # ── 入站平台私信联动（去重） ──
            service.mirror_platform_message(
                "douyin_dm", account_id, "cid-1", "抖音网友", "平台又来一条",
                platform_msg_id="m-100")
            service.mirror_platform_message(
                "douyin_dm", account_id, "cid-1", "抖音网友", "平台又来一条",
                platform_msg_id="m-100")  # 同平台消息 id 去重
            msgs = (await http.get(f"/api/cs/agent/conversations/{tk_id}/messages",
                                   headers=_auth(alice_token))).json()
            assert sum(1 for m in msgs if m["text"] == "平台又来一条") == 1

            # ── 同 source+thread 再转人工复用同一条会话 ──
            r = await http.post("/api/cs/agent/takeover", headers=_auth(alice_token),
                                json={"source": "douyin_dm", "account_id": account_id,
                                      "thread_key": "cid-1"})
            assert r.json()["id"] == tk_id

            # ── 登出后 token 失效 ──
            await http.post("/api/cs/agent/logout", headers=_auth(bob_token))
            assert (await http.get("/api/cs/agent/me",
                                   headers=_auth(bob_token))).status_code == 401

    asyncio.run(scenario())


def test_cs_takeover_requires_valid_source(local_project, monkeypatch, agents):
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            r = await http.post("/api/cs/agent/login",
                                json={"username": "alice", "password": "secret123"})
            token = r.json()["token"]
            r = await http.post("/api/cs/agent/takeover", headers=_auth(token),
                                json={"source": "guest", "account_id": 0,
                                      "thread_key": "x"})
            assert r.status_code == 400

    asyncio.run(scenario())


def test_cs_guest_unavailable_without_license(local_project):
    async def scenario():
        async with client(peer="203.0.113.9", base="http://mm.example.com") as pub:
            conv = service.create_link("无授权访客")
            r = await pub.get("/api/cs/guest/" + conv.token)
            assert r.status_code == 503
            r = await pub.get("/chat/" + conv.token)
            assert r.status_code == 503 and "暂不可用" in r.text

    asyncio.run(scenario())


def test_cs_cors_scoped_to_cs_paths(local_project, monkeypatch):
    """跨域放开仅限客服路径：/api/cs/* 有 ACAO 头并应答预检；其他 API 不带 CORS 头。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            origin = {"Origin": "http://evil.example"}
            # 预检：仅 /api/cs/ 应答 204 + CORS 头
            r = await http.options("/api/cs/agent/login", headers={
                **origin, "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers":
                    "authorization,content-type,x-creatorhub-actor"})
            assert r.status_code == 204
            assert r.headers.get("access-control-allow-origin") == "*"
            # 工作台全局 fetch 恒带 X-CreatorHub-Actor，预检必须放行该头，
            # 否则浏览器在预检通过后仍拦截实际 POST（failed to fetch）。
            allowed = r.headers.get("access-control-allow-headers", "").lower()
            assert "x-creatorhub-actor" in allowed
            assert "content-type" in allowed
            # 未带 Access-Control-Request-Headers 时的默认值也覆盖兼容头
            r = await http.options("/api/cs/agent/login", headers={
                **origin, "Access-Control-Request-Method": "POST"})
            assert "x-creatorhub-actor" in \
                r.headers.get("access-control-allow-headers", "").lower()
            # 普通请求：/api/cs/ 带 ACAO
            r = await http.post("/api/cs/agent/login", headers=origin,
                                json={"username": "x", "password": "y"})
            assert r.headers.get("access-control-allow-origin") == "*"
            # 非客服路径不带 CORS 头（LocalAccess 同源保护不被削弱）
            r = await http.get("/api/accounts", headers=origin)
            assert "access-control-allow-origin" not in r.headers
            # 非客服路径预检不应答 204 CORS
            r = await http.options("/api/accounts", headers={
                **origin, "Access-Control-Request-Method": "GET"})
            assert r.headers.get("access-control-allow-origin") != "*"

    asyncio.run(scenario())


def test_cs_sse_realtime_delivery(local_project, monkeypatch):
    """坐席 SSE 大厅收到 hello；访客发言后实时收到 message 事件。

    httpx/Starlette 的进程内测试传输都会等应用结束才返回，无法验证无限 SSE，
    因此在线程内启动真实 uvicorn（随机端口），用真实 HTTP 做流式验证。
    """
    import queue as queue_mod
    import threading

    import uvicorn

    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    # lifespan="off"：避免启动流程把数据库引擎重定向到开发库，
    # 请求所需的引擎/配置已由 local_project fixture 指向临时库。
    config = uvicorn.Config(main.app, host="127.0.0.1", port=0,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # 非主线程不能注册信号
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 测试服务器未能启动"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = "http://127.0.0.1:%d" % port

    try:
        service.create_agent("alice", "secret123", "小爱")
        with httpx.Client(base_url=base, timeout=10) as http:
            lr = http.post("/api/cs/agent/login",
                           json={"username": "alice", "password": "secret123"})
            assert lr.status_code == 200, lr.text
            token = lr.json()["token"]
            link_token = http.post("/api/cs/agent/links", headers=_auth(token),
                                   json={"customer_name": "SSE 客户"}).json()["token"]

            outcome = queue_mod.Queue()

            def reader():
                try:
                    with httpx.Client(base_url=base, timeout=15) as rc:
                        with rc.stream("GET", "/api/cs/agent/stream?token=" + token) as resp:
                            assert resp.status_code == 200
                            buf = ""
                            for chunk in resp.iter_text():
                                buf += chunk
                                if "SSE_REALTIME_TOKEN" in buf:
                                    outcome.put("ok")
                                    return
                                if len(buf) > 20000:
                                    break
                    outcome.put("miss")
                except Exception as e:  # noqa: BLE001
                    outcome.put("error:" + repr(e))

            def poster():
                time.sleep(1.0)
                with httpx.Client(base_url=base, timeout=10) as pc:
                    r = pc.post("/api/cs/guest/" + link_token + "/messages",
                                json={"text": "SSE_REALTIME_TOKEN"})
                outcome.put(("post_status", r.status_code))

            t_read = threading.Thread(target=reader, daemon=True)
            t_post = threading.Thread(target=poster, daemon=True)
            t_read.start()
            t_post.start()
            t_read.join(15)
            t_post.join(5)
            results = []
            while not outcome.empty():
                results.append(outcome.get_nowait())
            assert ("post_status", 200) in results, results
            assert "ok" in results, results
    finally:
        server.should_exit = True
        server_thread.join(10)


