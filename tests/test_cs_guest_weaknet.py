# -*- coding: utf-8 -*-
"""Task 17 访客 H5 弱网加固：幂等键去重 + 旧访客链接兼容 + 消息顺序。"""
import asyncio

import httpx
import pytest

import app.main as main
from app.cs import service
from test_project_optimizations import local_project, store  # noqa: F401


def client(peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


def test_guest_message_idempotency_same_key_no_duplicate(local_project, monkeypatch):
    """同一 idem_key 重复发送只入库一条，返回同一消息。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            link = service.create_link("访客幂等")
            token = link.token
            # 第一次发送
            r1 = await http.post(f"/api/cs/guest/{token}/messages",
                                 json={"text": "你好", "idem_key": "idem-abc-123"})
            assert r1.status_code == 200
            m1 = r1.json()["message"]
            mid1 = m1["id"]

            # 用同一 idem_key 重发（模拟网络重试）
            r2 = await http.post(f"/api/cs/guest/{token}/messages",
                                 json={"text": "你好", "idem_key": "idem-abc-123"})
            assert r2.status_code == 200
            m2 = r2.json()["message"]
            # 幂等：返回同一条消息
            assert m2["id"] == mid1

            # 拉取消息列表，确认只有一条 "你好"
            r3 = await http.get(f"/api/cs/guest/{token}")
            msgs = r3.json()["messages"]
            hello_msgs = [m for m in msgs if m["text"] == "你好"]
            assert len(hello_msgs) == 1

    asyncio.run(scenario())


def test_guest_message_different_keys_create_separate(local_project, monkeypatch):
    """不同 idem_key 产生不同消息，顺序正确。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            link = service.create_link("访客顺序")
            token = link.token
            r1 = await http.post(f"/api/cs/guest/{token}/messages",
                                 json={"text": "第一条", "idem_key": "k1"})
            r2 = await http.post(f"/api/cs/guest/{token}/messages",
                                 json={"text": "第二条", "idem_key": "k2"})
            assert r1.status_code == 200 and r2.status_code == 200
            assert r1.json()["message"]["id"] != r2.json()["message"]["id"]

            r = await http.get(f"/api/cs/guest/{token}")
            texts = [m["text"] for m in r.json()["messages"]]
            assert texts == ["第一条", "第二条"]

    asyncio.run(scenario())


def test_guest_message_no_idem_key_backward_compatible(local_project, monkeypatch):
    """不传 idem_key 的旧客户端仍可正常发送（向后兼容）。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            link = service.create_link("旧客户端")
            token = link.token
            # 旧请求体不含 idem_key
            r = await http.post(f"/api/cs/guest/{token}/messages",
                                json={"text": "旧版消息", "nickname": "老张"})
            assert r.status_code == 200
            assert r.json()["message"]["text"] == "旧版消息"

            # 消息列表能查到
            r2 = await http.get(f"/api/cs/guest/{token}")
            texts = [m["text"] for m in r2.json()["messages"]]
            assert "旧版消息" in texts

    asyncio.run(scenario())


def test_guest_poll_after_id_returns_only_new_messages(local_project, monkeypatch):
    """after_id 轮询只返回增量消息，断线重连后不重复。"""
    monkeypatch.setenv("CREATORHUB_SKIP_LICENSE", "1")

    async def scenario():
        async with client() as http:
            link = service.create_link("访客轮询")
            token = link.token
            # 发两条
            await http.post(f"/api/cs/guest/{token}/messages",
                            json={"text": "m1", "idem_key": "p1"})
            r = await http.post(f"/api/cs/guest/{token}/messages",
                                json={"text": "m2", "idem_key": "p2"})
            last_id = r.json()["message"]["id"]

            # 用 after_id 拉取，应无新消息
            r2 = await http.get(f"/api/cs/guest/{token}?after_id={last_id}")
            assert r2.json()["messages"] == []

            # 再发一条，after_id 应只返回新的
            await http.post(f"/api/cs/guest/{token}/messages",
                            json={"text": "m3", "idem_key": "p3"})
            r3 = await http.get(f"/api/cs/guest/{token}?after_id={last_id}")
            msgs = r3.json()["messages"]
            assert len(msgs) == 1 and msgs[0]["text"] == "m3"

    asyncio.run(scenario())
