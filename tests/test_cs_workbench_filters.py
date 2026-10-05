# -*- coding: utf-8 -*-
"""Task 16 坐席工作台增强：会话列表筛选（来源/平台/未读）+ 快捷回复 CRUD。"""
import asyncio

import httpx
import pytest

import app.main as main
from app.cs import service
from test_project_optimizations import local_project, store  # noqa: F401


def client(peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture()
def agents():
    admin = service.create_agent("wf_admin", "secret123", "管理员", role="admin")
    alice = service.create_agent("wf_alice", "secret123", "小爱")
    return NS(admin=admin, alice=alice)


def _auth(token):
    return {"Authorization": "Bearer " + token}


async def _login(http, username):
    r = await http.post("/api/cs/agent/login",
                        json={"username": username, "password": "secret123"})
    return r.json()["token"]


def test_conversation_filters_source_platform_unread(local_project, monkeypatch, agents):
    """来源/平台/未读筛选生效，不改变可见性规则。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            token = await _login(http, "wf_alice")
            h = _auth(token)
            # 建三条不同来源的会话：访客、抖音私信、小红书评论
            guest = service.create_link("访客A")
            service.customer_send(guest.token, "访客消息", "访客A")  # 产生未读
            dy = service.open_or_create_for_takeover(
                source="douyin_dm", account_id=0, thread_key="dy_thread_1",
                customer_name="抖音用户", first_message="抖音私信")
            xhs = service.open_or_create_for_takeover(
                source="xhs_comment", account_id=0, thread_key="xhs_thread_1",
                customer_name="小红书用户", first_message="小红书评论")

            # 全部（排队）可见
            r = await http.get("/api/cs/agent/conversations?status=queued", headers=h)
            assert r.status_code == 200
            all_ids = {c["id"] for c in r.json()}
            assert {guest.id, dy.id, xhs.id} <= all_ids

            # 来源精确筛选 douyin_dm
            r = await http.get("/api/cs/agent/conversations?source=douyin_dm", headers=h)
            ids = {c["id"] for c in r.json()}
            assert dy.id in ids and guest.id not in ids and xhs.id not in ids

            # 平台筛选 xhs
            r = await http.get("/api/cs/agent/conversations?platform=xhs", headers=h)
            ids = {c["id"] for c in r.json()}
            assert xhs.id in ids and dy.id not in ids

            # 未读筛选：只有访客发言过的那条有未读
            r = await http.get("/api/cs/agent/conversations?unread_only=1", headers=h)
            ids = {c["id"] for c in r.json()}
            assert guest.id in ids
            # 抖音/小红书虽有 first_message，但 first_message 在建会话时写入，
            # 也应产生未读——核对它们是否也在未读集合
            assert dy.id in ids and xhs.id in ids

            # 接单后该会话不再是未读（mark_read 清零）
            await http.post(f"/api/cs/agent/conversations/{guest.id}/accept", headers=h)
            await http.post(f"/api/cs/agent/conversations/{guest.id}/read", headers=h)
            r = await http.get("/api/cs/agent/conversations?unread_only=1", headers=h)
            ids = {c["id"] for c in r.json()}
            assert guest.id not in ids

    asyncio.run(scenario())


def test_quick_reply_crud(local_project, monkeypatch, agents):
    """快捷回复 CRUD：增删改查，按坐席隔离。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            alice = await _login(http, "wf_alice")
            admin = await _login(http, "wf_admin")
            ha = _auth(alice)
            hm = _auth(admin)

            # 初始为空
            r = await http.get("/api/cs/agent/quick-replies", headers=ha)
            assert r.status_code == 200 and r.json() == []

            # 新建
            r = await http.post("/api/cs/agent/quick-replies", headers=ha,
                                json={"title": "问候", "text": "您好，请问有什么可以帮您？"})
            assert r.status_code == 200
            qr = r.json()
            assert qr["title"] == "问候" and qr["text"].startswith("您好")
            qr_id = qr["id"]

            # 列表能查到
            r = await http.get("/api/cs/agent/quick-replies", headers=ha)
            assert len(r.json()) == 1 and r.json()[0]["id"] == qr_id

            # 空内容：纯空白被 service 层拒绝（400）；空串被 pydantic 拒绝（422）
            r = await http.post("/api/cs/agent/quick-replies", headers=ha,
                                json={"title": "x", "text": "   "})
            assert r.status_code == 400

            # 更新
            r = await http.put(f"/api/cs/agent/quick-replies/{qr_id}", headers=ha,
                               json={"title": "问候语", "text": "您好~欢迎咨询"})
            assert r.status_code == 200 and r.json()["title"] == "问候语"

            # 坐席隔离：管理员看不到 alice 的
            r = await http.get("/api/cs/agent/quick-replies", headers=hm)
            assert r.json() == []

            # 删除
            r = await http.delete(f"/api/cs/agent/quick-replies/{qr_id}", headers=ha)
            assert r.status_code == 200
            r = await http.get("/api/cs/agent/quick-replies", headers=ha)
            assert r.json() == []

            # 删不存在的报错
            r = await http.delete(f"/api/cs/agent/quick-replies/nope", headers=ha)
            assert r.status_code == 400

    asyncio.run(scenario())


def test_conversation_list_includes_account_context(local_project, monkeypatch, agents):
    """会话列表返回 platform/kind/account_name 字段，供前端展示上下文。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            token = await _login(http, "wf_admin")
            h = _auth(token)
            guest = service.create_link("访客")
            dy = service.open_or_create_for_takeover(
                source="douyin_dm", account_id=0, thread_key="t1",
                customer_name="u", first_message="hi")

            r = await http.get("/api/cs/agent/conversations", headers=h)
            by_id = {c["id"]: c for c in r.json()}
            assert by_id[guest.id]["platform"] == "guest"
            assert by_id[dy.id]["platform"] == "douyin"
            assert by_id[dy.id]["kind"] == "dm"
            # account_name 字段存在（无账号时为空串）
            assert "account_name" in by_id[dy.id]

    asyncio.run(scenario())
