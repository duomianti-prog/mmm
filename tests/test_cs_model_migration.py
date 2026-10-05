# -*- coding: utf-8 -*-
"""Task 12: 客服数据模型演进——旧库升级、平台消息幂等键、入站游标、
账号级接待开关、节点心跳/路由持久化。

旧库用裸 sqlite3 按 v1.6 形态构造（csmessage 无 idem_key、三张新表不存在），
随后走真实 ``db.init_db`` 升级路径，验证既有坐席/访客/消息数据完整、
新字段就位、历史平台消息幂等键回填且唯一索引生效。
"""
import sqlite3
import time

import pytest
from sqlalchemy import inspect, text
from sqlmodel import select

import app.db as db
from app.cs import service
from app.cs.models import (
    CsAccountReception, CsConversation, CsInboundCursor, CsMessage,
    CsNodeHeartbeat, KIND_COMMENT, KIND_DM, SENDER_CUSTOMER,
    STATUS_QUEUED, make_source, parse_source,
)

# ── 旧库（Task 12 之前）结构 ─────────────────────────────────────────────

_LEGACY_DDL = [
    """CREATE TABLE csagent (
        id INTEGER NOT NULL PRIMARY KEY,
        username VARCHAR NOT NULL, password_hash VARCHAR NOT NULL DEFAULT '',
        salt VARCHAR NOT NULL DEFAULT '', display_name VARCHAR NOT NULL DEFAULT '',
        role VARCHAR NOT NULL DEFAULT 'agent', enabled BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL)""",
    """CREATE TABLE cssession (
        id INTEGER NOT NULL PRIMARY KEY,
        token_hash VARCHAR NOT NULL, agent_id INTEGER NOT NULL,
        user_agent VARCHAR NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL, last_seen DATETIME NOT NULL,
        expires_at DATETIME NOT NULL, revoked BOOLEAN NOT NULL DEFAULT 0)""",
    """CREATE TABLE csconversation (
        id INTEGER NOT NULL PRIMARY KEY,
        token VARCHAR NOT NULL, source VARCHAR NOT NULL DEFAULT 'guest',
        account_id INTEGER NOT NULL DEFAULT 0, account_key VARCHAR NOT NULL DEFAULT '',
        thread_key VARCHAR NOT NULL DEFAULT '', customer_name VARCHAR NOT NULL DEFAULT '',
        customer_avatar VARCHAR NOT NULL DEFAULT '',
        status VARCHAR NOT NULL DEFAULT 'queued', owner_agent_id INTEGER,
        last_text VARCHAR NOT NULL DEFAULT '', last_time INTEGER NOT NULL DEFAULT 0,
        unread_agent INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL, closed_at DATETIME)""",
    """CREATE TABLE csmessage (
        id INTEGER NOT NULL PRIMARY KEY,
        conv_id INTEGER NOT NULL, sender_kind VARCHAR NOT NULL,
        agent_id INTEGER, sender_name VARCHAR NOT NULL DEFAULT '',
        msg_type VARCHAR NOT NULL DEFAULT 'text', text VARCHAR NOT NULL DEFAULT '',
        relayed BOOLEAN NOT NULL DEFAULT 0, platform_msg_id VARCHAR NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL)""",
    """CREATE TABLE csparticipant (
        id INTEGER NOT NULL PRIMARY KEY,
        conv_id INTEGER NOT NULL, agent_id INTEGER NOT NULL,
        joined_at DATETIME NOT NULL)""",
    """CREATE TABLE csrelayjob (
        id INTEGER NOT NULL PRIMARY KEY,
        conv_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        source VARCHAR NOT NULL DEFAULT '', platform VARCHAR NOT NULL DEFAULT '',
        kind VARCHAR NOT NULL DEFAULT '', account_id INTEGER NOT NULL DEFAULT 0,
        account_key VARCHAR NOT NULL DEFAULT '', thread_key VARCHAR NOT NULL DEFAULT '',
        text VARCHAR NOT NULL DEFAULT '', status VARCHAR NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, claimed_by VARCHAR NOT NULL DEFAULT '',
        claimed_at DATETIME, result_error VARCHAR NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)""",
]


def _build_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        for ddl in _LEGACY_DDL:
            conn.execute(ddl)
        now = "2026-09-01 10:00:00"
        conn.execute(
            "INSERT INTO csagent (id, username, password_hash, salt, display_name,"
            " role, enabled, created_at) VALUES (1,'alice','h','s','小爱','agent',1,?)",
            (now,))
        # 两条已转人工的平台会话（不同账号），各有一条同 id 的历史平台消息
        conn.execute(
            "INSERT INTO csconversation (id, token, source, account_id, account_key,"
            " thread_key, customer_name, customer_avatar, status, owner_agent_id,"
            " last_text, last_time, unread_agent, created_at, closed_at) VALUES "
            "(1,'tok-1','douyin_dm',7,'sec-A','cid-1','网友A','','queued',NULL,"
            "'在吗',1756700000,1,?,NULL)", (now,))
        conn.execute(
            "INSERT INTO csconversation (id, token, source, account_id, account_key,"
            " thread_key, customer_name, customer_avatar, status, owner_agent_id,"
            " last_text, last_time, unread_agent, created_at, closed_at) VALUES "
            "(2,'tok-2','douyin_dm',8,'sec-B','cid-2','网友B','','active',1,"
            "'你好',1756700100,0,?,NULL)", (now,))
        conn.execute(
            "INSERT INTO csmessage (id, conv_id, sender_kind, agent_id, sender_name,"
            " msg_type, text, relayed, platform_msg_id, created_at) VALUES "
            "(1,1,'customer',NULL,'网友A','text','在吗',0,'dup-mid',?)", (now,))
        conn.execute(
            "INSERT INTO csmessage (id, conv_id, sender_kind, agent_id, sender_name,"
            " msg_type, text, relayed, platform_msg_id, created_at) VALUES "
            "(2,2,'customer',NULL,'网友B','text','你好',0,'dup-mid',?)", (now,))
        conn.execute(
            "INSERT INTO csmessage (id, conv_id, sender_kind, agent_id, sender_name,"
            " msg_type, text, relayed, platform_msg_id, created_at) VALUES "
            "(3,2,'agent',1,'小爱','text','您好，请问',0,'',?)", (now,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def upgraded_db(tmp_path):
    previous = db._engine
    path = tmp_path / "legacy-cs.db"
    _build_legacy_db(path)
    db.init_db(str(path))
    yield path
    db._engine.dispose()
    db._engine = previous


# ── 来源纯函数 ───────────────────────────────────────────────────────────

def test_make_and_parse_source():
    assert make_source("TikTok", "DM") == "tiktok_dm"
    assert parse_source("tiktok_comment") == ("tiktok", "comment")
    assert parse_source("douyin_dm") == ("douyin", "dm")
    assert parse_source("guest") == ("", "")
    assert parse_source("douyin_like") == ("", "")     # kind 不认可
    assert parse_source("") == ("", "")


# ── 旧库升级 ─────────────────────────────────────────────────────────────

def test_legacy_db_upgrade_preserves_data_and_adds_fields(upgraded_db):
    insp = inspect(db._engine)
    # 既有坐席/会话/消息数据完整
    with db.get_session() as s:
        agent = s.exec(select(CsConversation)).all()
        assert {c.id for c in agent} == {1, 2}
        conv1 = s.get(CsConversation, 1)
        assert conv1.account_key == "sec-A" and conv1.customer_name == "网友A"
        msgs = s.exec(select(CsMessage).order_by(CsMessage.id)).all()
        assert [(m.id, m.text) for m in msgs] == [
            (1, "在吗"), (2, "你好"), (3, "您好，请问")]

    # csmessage 新列就位
    cols = {c["name"] for c in insp.get_columns("csmessage")}
    assert "idem_key" in cols

    # 历史平台消息按 legacy:<conv_id>:<mid> 回填；跨会话同 mid 不冲突，
    # 坐席/无平台 id 消息保持空键
    with db.get_session() as s:
        rows = {m.id: m.idem_key for m in s.exec(select(CsMessage)).all()}
    assert rows[1] == "legacy:1:dup-mid"
    assert rows[2] == "legacy:2:dup-mid"
    assert rows[3] == ""

    # 幂等部分唯一索引存在且为 unique
    indexes = {i["name"]: i for i in insp.get_indexes("csmessage")}
    assert indexes["ux_csmessage_idemkey"]["unique"] == 1

    # 三张新表就位
    for table in ("csinboundcursor", "csaccountreception", "csnodeheartbeat"):
        assert insp.has_table(table), table


def test_idem_index_blocks_duplicates_allows_empty(upgraded_db):
    with db.get_session() as s:
        conn = s.connection()
        conn.execute(text(
            "INSERT INTO csmessage (conv_id, sender_kind, sender_name, msg_type,"
            " text, relayed, platform_msg_id, idem_key, created_at) VALUES"
            " (1,'customer','n','text','x',0,'m','k|s|m',"
            " '2026-09-02 00:00:00')"))
        # 空幂等键允许多条（访客/坐席消息）
        conn.execute(text(
            "INSERT INTO csmessage (conv_id, sender_kind, sender_name, msg_type,"
            " text, relayed, platform_msg_id, idem_key, created_at) VALUES"
            " (1,'agent','n','text','y',0,'','', '2026-09-02 00:00:01')"))
        conn.execute(text(
            "INSERT INTO csmessage (conv_id, sender_kind, sender_name, msg_type,"
            " text, relayed, platform_msg_id, idem_key, created_at) VALUES"
            " (2,'agent','n','text','z',0,'','', '2026-09-02 00:00:02')"))
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO csmessage (conv_id, sender_kind, sender_name, msg_type,"
                " text, relayed, platform_msg_id, idem_key, created_at) VALUES"
                " (2,'customer','n','text','w',0,'m2','k|s|m',"
                " '2026-09-02 00:00:03')"))
        s.rollback()


def test_relay_job_upgrade_adds_task14_fields_and_idem_index(upgraded_db):
    """Task 14：旧 csrelayjob 升级后新增 next_run_at/fail_kind/idem_key，
    且 idem_key 部分唯一索引就位（同键只能一行，空键不约束）。"""
    conn = sqlite3.connect(str(upgraded_db))
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(csrelayjob)")}
        assert {"next_run_at", "fail_kind", "idem_key"} <= cols
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(csrelayjob)")}
        assert "ux_csrelayjob_idemkey" in indexes
        conn.execute(
            "INSERT INTO csrelayjob (conv_id, message_id, source, platform, kind,"
            " account_id, account_key, thread_key, text, status, attempts,"
            " claimed_by, claimed_at, result_error, idem_key, created_at,"
            " updated_at) VALUES (1,3,'douyin_dm','douyin','dm',7,'sec-A',"
            "'cid-1','您好','pending',0,'',NULL,'','relay:m3',"
            "'2026-09-02 00:00:00','2026-09-02 00:00:00')")
        # 空键历史任务不受唯一约束
        conn.execute(
            "INSERT INTO csrelayjob (conv_id, message_id, source, platform, kind,"
            " account_id, account_key, thread_key, text, status, attempts,"
            " claimed_by, claimed_at, result_error, idem_key, created_at,"
            " updated_at) VALUES (2,99,'douyin_dm','douyin','dm',8,'sec-B',"
            "'cid-2','在吗','pending',0,'',NULL,'','',"
            "'2026-09-02 00:00:01','2026-09-02 00:00:01')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO csrelayjob (conv_id, message_id, source, platform,"
                " kind, account_id, account_key, thread_key, text, status,"
                " attempts, claimed_by, claimed_at, result_error, idem_key,"
                " created_at, updated_at) VALUES (1,3,'douyin_dm','douyin','dm',"
                "7,'sec-A','cid-1','您好','pending',0,'',NULL,'','relay:m3',"
                "'2026-09-02 00:00:02','2026-09-02 00:00:02')")
        conn.rollback()
    finally:
        conn.close()


# ── 同键消息幂等（service 层）─────────────────────────────────────────────
def test_mirror_platform_message_idempotent(upgraded_db):
    # conv 1：douyin_dm / account_id=7 / account_key=sec-A / cid-1
    for _ in range(2):
        conv_id = service.mirror_platform_message(
            "douyin_dm", 7, "cid-1", "网友A", "新消息",
            platform_msg_id="m-100")
        assert conv_id == 1
    with db.get_session() as s:
        rows = s.exec(select(CsMessage).where(
            CsMessage.platform_msg_id == "m-100")).all()
        assert len(rows) == 1
        assert rows[0].idem_key == "douyin_dm|sec-A|m-100"
        conv = s.get(CsConversation, 1)
        assert conv.unread_agent == 2          # 历史 1 + 新消息 1
        assert conv.last_text == "新消息"


def test_mirror_uses_local_id_scope_without_account_key(upgraded_db):
    # 历史无 account_key 的会话：幂等作用域退回本地 account_id
    with db.get_session() as s:
        conv = CsConversation(
            token="tok-3", source="xhs_dm", account_id=9, account_key="",
            thread_key="x-1", customer_name="红薯",
            status=STATUS_QUEUED, last_time=int(time.time()))
        s.add(conv)
        s.commit()
        conv_id = conv.id
    service.mirror_platform_message(
        "xhs_dm", 9, "x-1", "红薯", "第一条", platform_msg_id="x-9")
    service.mirror_platform_message(
        "xhs_dm", 9, "x-1", "红薯", "第一条", platform_msg_id="x-9")
    with db.get_session() as s:
        rows = s.exec(select(CsMessage).where(CsMessage.conv_id == conv_id)).all()
        assert len(rows) == 1
        assert rows[0].idem_key == "xhs_dm|id:9|x-9"


def test_takeover_accepts_tiktok_source_and_rejects_bad_source(upgraded_db):
    conv = service.open_or_create_for_takeover(
        "tiktok_dm", account_id=3, account_key="sec-tt",
        thread_key="tt-conv-1", customer_name="TT 网友")
    assert conv.source == "tiktok_dm"
    payload = service.conversation_dict_safe(conv.id)
    assert payload["platform"] == "tiktok" and payload["kind"] == "dm"
    with pytest.raises(service.CsError):
        service.open_or_create_for_takeover(
            "weibo_dm", account_id=1, thread_key="w-1")
    with pytest.raises(service.CsError):
        service.open_or_create_for_takeover(
            "guest", account_id=0, thread_key="g-1")


# ── 入站游标 ─────────────────────────────────────────────────────────────

def test_inbound_cursor_upsert_and_default(upgraded_db):
    assert service.get_inbound_cursor("tiktok", "sec-tt", KIND_DM) == ""
    service.set_inbound_cursor("tiktok", "sec-tt", KIND_DM, "cursor-1")
    service.set_inbound_cursor("TikTok", "sec-tt", "dm", "cursor-2")  # 归一化后同行
    assert service.get_inbound_cursor("tiktok", "sec-tt", KIND_DM) == "cursor-2"
    service.set_inbound_cursor("douyin", "sec-A", KIND_COMMENT, "1756700200")
    assert service.get_inbound_cursor("douyin", "sec-A", "comment") == "1756700200"
    with db.get_session() as s:
        assert len(s.exec(select(CsInboundCursor)).all()) == 2
    with pytest.raises(service.CsError):
        service.set_inbound_cursor("tiktok", "sec-tt", "like", "x")
    with pytest.raises(service.CsError):
        service.set_inbound_cursor("", "sec-tt", KIND_DM, "x")


# ── 账号级接待开关 ───────────────────────────────────────────────────────

def test_account_reception_switch_default_on(upgraded_db):
    assert service.reception_enabled("douyin", "sec-A") is True
    service.set_reception_enabled("douyin", "sec-A", False)
    assert service.reception_enabled("douyin", "sec-A") is False
    assert service.reception_enabled("tiktok", "sec-tt") is True   # 其他账号不受影响
    service.set_reception_enabled("douyin", "sec-A", True)
    assert service.reception_enabled("douyin", "sec-A") is True
    with db.get_session() as s:
        rows = s.exec(select(CsAccountReception)).all()
        assert len(rows) == 1 and rows[0].enabled is True
    with pytest.raises(service.CsError):
        service.set_reception_enabled("douyin", "", False)


# ── 节点心跳 / 路由 ──────────────────────────────────────────────────────

def test_node_heartbeat_persisted_with_routes_and_pruned(upgraded_db):
    accounts = [
        {"platform": "douyin", "account_key": "sec-A", "account_id": 7},
        {"platform": "tiktok", "account_key": "sec-tt", "account_id": 3},
    ]
    service._record_node_heartbeat("workbench-1", accounts)
    # 再调一次：upsert，不产生多行
    service._record_node_heartbeat("workbench-1", accounts)
    nodes = service.list_node_heartbeats()
    assert len(nodes) == 1
    node = nodes[0]
    assert node["node_id"] == "workbench-1"
    assert node["accounts"] == 2
    assert node["platforms"] == ["douyin", "tiktok"]
    assert node["routes"] == accounts

    # 10 分钟前心跳的节点：不展示但保留
    with db.get_session() as s:
        s.add(CsNodeHeartbeat(
            node_id="stale-10m", platforms="xhs", account_count=1,
            accounts_json="[]", last_seen=int(time.time()) - 600))
        # 8 天前心跳的节点：超过保留期
        s.add(CsNodeHeartbeat(
            node_id="dead-8d", platforms="douyin", account_count=1,
            accounts_json="[]", last_seen=int(time.time()) - 8 * 86400))
        s.commit()
    visible = {n["node_id"] for n in service.list_node_heartbeats()}
    assert visible == {"workbench-1"}
    # 新心跳落库顺带清理超期节点
    service._record_node_heartbeat("workbench-2", [])
    with db.get_session() as s:
        ids = {n.node_id for n in s.exec(select(CsNodeHeartbeat)).all()}
    assert "dead-8d" not in ids and "stale-10m" in ids


def test_relay_queue_overview_includes_persisted_nodes(upgraded_db):
    service._record_node_heartbeat("wb-ov", [
        {"platform": "douyin", "account_key": "sec-A", "account_id": 7}])
    overview = service.relay_queue_overview()
    assert [n["node_id"] for n in overview["nodes"]] == ["wb-ov"]
