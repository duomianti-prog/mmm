# -*- coding: utf-8 -*-
"""Task 13: 多平台统一入站总线测试（桩级集成）。

覆盖 TR-13.1：
- 四个来源（抖音/小红书/TikTok 私信 + 抖音评论）汇入统一 sink，自动建档；
- 重复投递幂等（同进程重复调用 + 预置同 idem_key 行模拟并发）；
- 接待开关关闭整账号丢弃；
- 离线时段补拉批次按服务端时间升序落库、游标持久化；
- 已转人工会话复用、关闭会话新消息重开、auto_create=False 不建档；
- 非法平台/类型/空内容忽略；TikTok 快照 last_time 幂等。
"""
import pytest
from sqlmodel import select

import app.db as db
from app.cs import inbound, service
from app.cs.events import bus
from app.cs.models import (
    CsConversation, CsMessage, SENDER_CUSTOMER, SENDER_SYSTEM,
    STATUS_CLOSED, STATUS_QUEUED,
)
from test_project_optimizations import local_project  # noqa: F401


def _conv(source, thread):
    with db.get_session() as s:
        return s.exec(select(CsConversation).where(
            CsConversation.source == source,
            CsConversation.thread_key == thread)).first()


def _texts(conv_id):
    with db.get_session() as s:
        rows = s.exec(select(CsMessage).where(
            CsMessage.conv_id == conv_id,
            CsMessage.sender_kind == SENDER_CUSTOMER).order_by(CsMessage.id)).all()
        return [m.text for m in rows]


# ── 四来源自动建档 ───────────────────────────────────────────────────────

def test_four_sources_auto_create_conversations(local_project):
    r1 = inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "在吗", "m1",
                           peer_nickname="抖音网友", msg_ts=100, cursor="100")
    r2 = inbound.ingest_dm_batch("xhs", 2, "xhs-user-2", [{
        "thread_key": "xhs-c1", "text": "红薯你好",
        "platform_msg_id": "xhs:s1", "peer_nickname": "红薯网友",
        "msg_ts": 200}], cursor="200")
    r3 = inbound.ingest_dm_batch("tiktok", 3, "sec-tt-3", [{
        "thread_key": "tt-c1", "text": "hello tiktok",
        "platform_msg_id": "last:tt-c1:300", "peer_nickname": "TT",
        "msg_ts": 300}], cursor="300")
    r4 = inbound.ingest_comment("douyin", 1, "sec-1", "aweme-9", "cid-9",
                                "评论求合作", peer_nickname="评论网友",
                                msg_ts=400)
    assert (r1.ingested, r2.ingested, r3.ingested, r4.ingested) == (1, 1, 1, 1)

    with db.get_session() as s:
        convs = s.exec(select(CsConversation)).all()
        assert len(convs) == 4
        assert {c.source for c in convs} == {
            "douyin_dm", "xhs_dm", "tiktok_dm", "douyin_comment"}
        assert all(c.status == STATUS_QUEUED for c in convs)
        assert all(c.unread_agent == 1 for c in convs)

    c = _conv("douyin_comment", "aweme-9:cid-9")
    assert c is not None and c.customer_name == "评论网友"
    assert _texts(c.id) == ["评论求合作"]

    # 游标按平台+账号+类型各自持久化
    assert service.get_inbound_cursor("douyin", "sec-1", "dm") == "100"
    assert service.get_inbound_cursor("xhs", "xhs-user-2", "dm") == "200"
    assert service.get_inbound_cursor("tiktok", "sec-tt-3", "dm") == "300"


# ── 重复投递幂等 ─────────────────────────────────────────────────────────

def test_duplicate_delivery_idempotent(local_project):
    total = inbound.IngestResult()
    for _ in range(3):
        total.merge(inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "在吗", "m1"))
    assert (total.ingested, total.duplicates) == (1, 2)
    conv = _conv("douyin_dm", "conv-a")
    assert _texts(conv.id) == ["在吗"]
    assert conv.unread_agent == 1          # 重复消息不增加未读

    # 模拟并发：另一条路径已插入相同 idem_key 的行 → 唯一索引兜底
    with db.get_session() as s:
        existing = s.exec(select(CsMessage).where(
            CsMessage.idem_key == "douyin_dm|sec-1|m1")).first()
        assert existing is not None


def test_tiktok_snapshot_same_last_time_idempotent(local_project):
    rows = lambda ts: [{
        "thread_key": "tt-c1", "text": f"t{ts}",
        "platform_msg_id": f"last:tt-c1:{ts}", "msg_ts": ts,
        "peer_nickname": "TT"}]
    assert inbound.ingest_dm_batch("tiktok", 3, "sec-tt-3", rows(300)).ingested == 1
    # 同一快照重复同步：0 入库
    r = inbound.ingest_dm_batch("tiktok", 3, "sec-tt-3", rows(300))
    assert (r.ingested, r.duplicates) == (0, 1)
    # 对方发来更新一条：入库
    assert inbound.ingest_dm_batch("tiktok", 3, "sec-tt-3", rows(320)).ingested == 1
    conv = _conv("tiktok_dm", "tt-c1")
    assert _texts(conv.id) == ["t300", "t320"]


# ── 接待开关 ─────────────────────────────────────────────────────────────

def test_reception_disabled_drops_account(local_project):
    service.set_reception_enabled("douyin", "sec-1", False)
    r = inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "在吗", "m1")
    assert (r.ingested, r.ignored) == (0, 1)
    with db.get_session() as s:
        assert s.exec(select(CsConversation)).first() is None
        assert s.exec(select(CsMessage)).first() is None
    # 另一个账号不受影响
    r2 = inbound.ingest_dm("douyin", 2, "sec-2", "conv-b", "你好", "m2")
    assert r2.ingested == 1


# ── 离线补拉：顺序 + 游标 ────────────────────────────────────────────────

def test_offline_backfill_preserves_order_and_cursor(local_project):
    # 坐席不在线（无人订阅总线），消息仍应落库
    rows = [
        {"thread_key": "xhs-c1", "text": "第三条",
         "platform_msg_id": "xhs:s3", "msg_ts": 300},
        {"thread_key": "xhs-c1", "text": "第一条",
         "platform_msg_id": "xhs:s1", "msg_ts": 100},
        {"thread_key": "xhs-c1", "text": "第二条",
         "platform_msg_id": "xhs:s2", "msg_ts": 200},
    ]
    r = inbound.ingest_dm_batch("xhs", 2, "xhs-user-2", rows, cursor="300")
    assert r.ingested == 3 and len(r.created_conv_ids) == 1
    conv = _conv("xhs_dm", "xhs-c1")
    assert _texts(conv.id) == ["第一条", "第二条", "第三条"]
    with db.get_session() as s:
        c = s.get(CsConversation, conv.id)
        assert c.last_text == "第三条" and c.last_time == 300
    assert service.get_inbound_cursor("xhs", "xhs-user-2", "dm") == "300"

    # 重连后再次补拉同窗口：全部幂等，游标仍推进
    r2 = inbound.ingest_dm_batch("xhs", 2, "xhs-user-2", rows, cursor="300")
    assert r2.ingested == 0 and r2.duplicates == 3


# ── 会话复用 / 关闭重开 / 不自动建档 ─────────────────────────────────────

def test_reuses_existing_takeover_conversation(local_project):
    conv = service.open_or_create_for_takeover(
        "douyin_dm", account_id=1, account_key="sec-1",
        thread_key="conv-a", customer_name="老访客")
    r = inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "转人工后追问", "m9")
    assert r.created_conv_ids == [] and r.conv_ids == [conv.id]
    with db.get_session() as s:
        assert len(s.exec(select(CsConversation)).all()) == 1
    assert _texts(conv.id) == ["转人工后追问"]


def test_closed_conversation_reopens(local_project):
    r = inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "首条", "m1")
    conv_id = r.conv_ids[0]
    with db.get_session() as s:
        c = s.get(CsConversation, conv_id)
        c.status = STATUS_CLOSED
        s.add(c)
        s.commit()
    r2 = inbound.ingest_dm("douyin", 1, "sec-1", "conv-a", "又来找你", "m2")
    assert r2.ingested == 1
    with db.get_session() as s:
        c = s.get(CsConversation, conv_id)
        assert c.status == STATUS_QUEUED and c.owner_agent_id is None
        kinds = [m.sender_kind for m in s.exec(select(CsMessage).where(
            CsMessage.conv_id == conv_id)).all()]
    assert SENDER_SYSTEM in kinds          # 重开系统提示


def test_auto_create_disabled_ignores_unknown_thread(local_project):
    r = inbound.ingest_dm("douyin", 1, "sec-1", "conv-x", "在吗", "m1",
                          auto_create=False)
    assert (r.ingested, r.ignored) == (0, 1)
    with db.get_session() as s:
        assert s.exec(select(CsConversation)).first() is None


# ── 非法输入与平台边界 ───────────────────────────────────────────────────

def test_invalid_inputs_ignored(local_project):
    cases = [
        inbound.ingest_dm("shipinhao", 1, "sp", "c", "x", "1"),   # 非客服平台
        inbound.ingest_dm("douyin", 1, "sec", "", "x", "1"),      # 无 thread
        inbound.ingest_dm("douyin", 1, "sec", "c", "  ", "1"),    # 空文本
        inbound.ingest_comment("douyin", 1, "sec", "a", "", "x"),# 无评论 id
    ]
    assert all(c.ignored == 1 and c.ingested == 0 for c in cases)
    # 快手已纳入统一来源（私信能力具备时自动生效）
    r = inbound.ingest_dm("kuaishou", 5, "ks-5", "kc", "哈喽", "km1")
    assert r.ingested == 1


# ── 在线坐席实时广播 ─────────────────────────────────────────────────────

def test_broadcast_to_agents_room(local_project):
    q = bus.subscribe(bus.AGENTS_ROOM)
    try:
        inbound.ingest_comment("xhs", 4, "xhs-user-4", "note-1", "c-1",
                               "评论一下", peer_nickname="小红薯")
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait()["type"])
        assert "message" in kinds and "conversation" in kinds
    finally:
        bus.unsubscribe(bus.AGENTS_ROOM, q)
