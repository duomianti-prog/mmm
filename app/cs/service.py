# -*- coding: utf-8 -*-
"""客服业务状态机：会话、消息、接单/转接/邀请/关闭。

全部为同步 DB 短事务（SQLite），事件通过 EventBus 广播；平台回投在 api 层
异步触发，service 只负责落库与状态。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..db import get_session
from ..settings import get_setting, set_setting
from .events import bus
from .models import (
    CS_PLATFORMS, CsAccountReception, CsAgent, CsConversation,
    CsInboundCursor, CsMessage, CsNodeHeartbeat, CsParticipant, CsRelayJob,
    CsSession, FAIL_PERMANENT, FAIL_TIMEOUT, FAIL_TRANSIENT,
    KIND_COMMENT, KIND_DM,
    RELAY_CLAIMED, RELAY_DONE, RELAY_FAILED, RELAY_PENDING, RELAY_TIMEOUT,
    SENDER_AGENT, SENDER_CUSTOMER, SENDER_SYSTEM, SOURCE_GUEST,
    STATUS_ACTIVE, STATUS_CLOSED, STATUS_QUEUED, make_source, parse_source,
)
from .security import hash_password, hash_token, new_token, verify_password

# 节点令牌/心跳相关
NODE_TOKEN_SETTING = "cs_node_token"
NODE_TOKEN_ENV = "MMM_CS_NODE_TOKEN"
# 认领后多久没回报视为掉线，可被重新认领；超过此次数判超时并通知坐席
CLAIM_TIMEOUT_SECONDS = 600
CLAIM_MAX_ATTEMPTS = 3
# 瞬时故障自动重试：最多执行次数与指数退避（秒，封顶）
RELAY_TRANSIENT_MAX_ATTEMPTS = 3
RELAY_BACKOFF_BASE_SECONDS = 20
RELAY_BACKOFF_CAP_SECONDS = 300
# 心跳记录在库中最长保留时间（秒），超过的节点行在新心跳落库时顺带清理
HEARTBEAT_RETENTION_SECONDS = 7 * 24 * 3600

# 工作台回报错误文案 → 是否值得自动重试的关键词
_TRANSIENT_HINTS = (
    "超时", "timeout", "timed out", "网络", "network", "连接", "connection",
    "断开", "reset", "502", "503", "504", "稍后", "繁忙", "busy",
    "temporar", "try again", "重试", "unreachable", "拒绝连接",
)
_PERMANENT_HINTS = (
    "登录态", "登录失效", "未登录", "logged_out", "logged out", "未授权",
    "风控", "risk", "限制发言", "被禁言", "验证码", "参数错误", "内容为空",
    "缺少", "未找到", "找不到", "不存在", "invalid", "不支持", "已拒绝",
    "rejected", "400", "403", "永久",
)


def classify_relay_failure(error: str, retryable: Optional[bool] = None) -> str:
    """把工作台回报的失败分为瞬时（可退避重试）/永久（立即判死）。

    retryable 为工作台显式给出的判断时优先；否则按错误文案关键词分类，
    无法判定时保守视为瞬时（一次重试通常无害，上限兜底）。
    """
    if retryable is True:
        return FAIL_TRANSIENT
    if retryable is False:
        return FAIL_PERMANENT
    text = (error or "").lower()
    if any(k in text for k in _PERMANENT_HINTS):
        return FAIL_PERMANENT
    if any(k in text for k in _TRANSIENT_HINTS):
        return FAIL_TRANSIENT
    return FAIL_TRANSIENT


def _backoff_delay(attempts: int) -> int:
    """第 attempts 次执行失败后的退避秒数：base * 2^(n-1)，封顶 cap。"""
    delay = RELAY_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))
    return min(RELAY_BACKOFF_CAP_SECONDS, delay)


def account_scope_key(account_key: str, account_id: int) -> str:
    """跨节点账号作用域：优先平台稳定标识，历史无 key 退回本地库 id。"""
    return (account_key or "").strip() or (
        f"id:{int(account_id or 0)}" if account_id else "")


def inbound_idem_key(source: str, account_key: str, account_id: int,
                     platform_msg_id: str) -> str:
    """平台入站消息的全局幂等键。

    优先用跨节点稳定的 account_key 作用域；历史无 key 的会话退回本地 account_id。
    """
    platform_msg_id = (platform_msg_id or "").strip()
    if not platform_msg_id:
        return ""
    scope = account_scope_key(account_key, account_id)
    return f"{source}|{scope}|{platform_msg_id}"


def _utc_ts(dt: datetime | None) -> int:
    """模型里 created_at 为 naive UTC（datetime.utcnow），按 UTC 解释取秒级时间戳。"""
    if not dt:
        return 0
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class CsError(Exception):
    """业务规则错误（映射为 HTTP 400/403）。"""


# ───────────────────────── 坐席与会话令牌 ─────────────────────────

def create_agent(username: str, password: str, display_name: str = "",
                 role: str = "agent") -> CsAgent:
    username = (username or "").strip().lower()
    if not username or len(username) < 2:
        raise CsError("用户名至少 2 个字符")
    if len(password or "") < 6:
        raise CsError("密码至少 6 个字符")
    if role not in {"admin", "agent"}:
        role = "agent"
    with get_session() as s:
        if s.exec(select(CsAgent).where(CsAgent.username == username)).first():
            raise CsError("用户名已存在")
        digest, salt = hash_password(password)
        agent = CsAgent(username=username, password_hash=digest, salt=salt,
                        display_name=display_name.strip() or username, role=role)
        s.add(agent)
        s.commit()
        s.refresh(agent)
        return agent


def list_agents(include_disabled: bool = True) -> list[CsAgent]:
    with get_session() as s:
        rows = s.exec(select(CsAgent).order_by(CsAgent.id)).all()
        agents = [a for a in rows if include_disabled or a.enabled]
        for a in agents:
            a.password_hash = ""
            a.salt = ""
        return agents


def set_agent_password(agent_id: int, password: str) -> None:
    if len(password or "") < 6:
        raise CsError("密码至少 6 个字符")
    digest, salt = hash_password(password)
    with get_session() as s:
        agent = s.get(CsAgent, agent_id)
        if not agent:
            raise CsError("坐席不存在")
        agent.password_hash, agent.salt = digest, salt
        s.add(agent)
        s.commit()


def set_agent_enabled(agent_id: int, enabled: bool) -> None:
    with get_session() as s:
        agent = s.get(CsAgent, agent_id)
        if not agent:
            raise CsError("坐席不存在")
        agent.enabled = enabled
        s.add(agent)
        s.commit()


def login(username: str, password: str, user_agent: str = "") -> tuple[str, CsAgent]:
    username = (username or "").strip().lower()
    with get_session() as s:
        agent = s.exec(select(CsAgent).where(CsAgent.username == username)).first()
        if not agent or not agent.enabled \
                or not verify_password(password, agent.password_hash, agent.salt):
            raise CsError("用户名或密码错误")
        token = new_token()
        session = CsSession(
            token_hash=hash_token(token), agent_id=agent.id,
            user_agent=(user_agent or "")[:200],
            expires_at=datetime.utcnow() + timedelta(days=7))
        s.add(session)
        s.commit()
        s.refresh(agent)
        agent.password_hash = ""
        agent.salt = ""
        return token, agent


def logout(token: str) -> None:
    with get_session() as s:
        row = s.exec(select(CsSession).where(
            CsSession.token_hash == hash_token(token))).first()
        if row:
            row.revoked = True
            s.add(row)
            s.commit()


def agent_from_token(token: str) -> Optional[CsAgent]:
    """校验会话令牌，返回坐席；无效/过期/停用返回 None。"""
    if not token:
        return None
    with get_session() as s:
        session = s.exec(select(CsSession).where(
            CsSession.token_hash == hash_token(token))).first()
        if not session or session.revoked or session.expires_at < datetime.utcnow():
            return None
        agent = s.get(CsAgent, session.agent_id)
        if not agent or not agent.enabled:
            return None
        session.last_seen = datetime.utcnow()
        s.add(session)
        s.commit()
        s.refresh(agent)
        agent.password_hash = ""
        agent.salt = ""
        return agent


# ───────────────────────── 会话与消息 ─────────────────────────

def now_ts() -> int:
    return int(time.time())


def _system_msg(s, conv_id: int, text: str) -> None:
    s.add(CsMessage(conv_id=conv_id, sender_kind=SENDER_SYSTEM, text=text,
                    created_at=datetime.utcnow()))


def agent_dict(agent: CsAgent | None) -> dict:
    if not agent:
        return None
    return {"id": agent.id, "username": agent.username,
            "display_name": agent.display_name, "role": agent.role,
            "enabled": agent.enabled}


def agent_created_ts(agent: CsAgent) -> int:
    return _utc_ts(agent.created_at)


def message_dict(m: CsMessage) -> dict:
    return {
        "id": m.id, "conv_id": m.conv_id, "sender_kind": m.sender_kind,
        "agent_id": m.agent_id, "sender_name": m.sender_name,
        "msg_type": m.msg_type, "text": m.text, "relayed": m.relayed,
        "platform_msg_id": m.platform_msg_id,
        "created_at": _utc_ts(m.created_at),
    }


def conversation_dict(s, conv: CsConversation) -> dict:
    owner = s.get(CsAgent, conv.owner_agent_id) if conv.owner_agent_id else None
    participants = []
    for p in s.exec(select(CsParticipant).where(CsParticipant.conv_id == conv.id)).all():
        agent = s.get(CsAgent, p.agent_id)
        if agent:
            participants.append(agent_dict(agent))
    # 平台账号显示名（用于工作台列表上下文，非客服服务器无此表时降级为空）
    account_name = ""
    if conv.account_id:
        try:
            from ..models import DouyinAccount
            acc = s.get(DouyinAccount, conv.account_id)
            if acc:
                account_name = acc.nickname or acc.username or ""
        except Exception:
            account_name = ""
    return {
        "id": conv.id, "token": conv.token, "source": conv.source,
        "platform": parse_source(conv.source)[0] or conv.source,
        "kind": parse_source(conv.source)[1],
        "account_id": conv.account_id, "account_key": conv.account_key or "",
        "account_name": account_name,
        "thread_key": conv.thread_key,
        "customer_name": conv.customer_name, "customer_avatar": conv.customer_avatar,
        "status": conv.status, "owner": agent_dict(owner),
        "participants": participants,
        "last_text": conv.last_text, "last_time": conv.last_time,
        "unread_agent": conv.unread_agent,
        "created_at": _utc_ts(conv.created_at),
    }


def create_link(customer_name: str = "") -> CsConversation:
    """生成一条访客链接会话（客户未发言前处于 queued）。"""
    with get_session() as s:
        conv = CsConversation(token=new_token(), source=SOURCE_GUEST,
                              customer_name=(customer_name or "").strip()[:50],
                              status=STATUS_QUEUED, last_time=now_ts())
        s.add(conv)
        s.commit()
        s.refresh(conv)
        return conv


def open_or_create_for_takeover(source: str, account_id: int, thread_key: str,
                                customer_name: str = "",
                                customer_avatar: str = "",
                                first_message: str = "",
                                account_key: str = "") -> CsConversation:
    """平台私信/评论「转人工」：同来源+同平台对象只开一条进行中的会话。"""
    source = (source or "").strip().lower()
    if source == SOURCE_GUEST or not thread_key:
        raise CsError("转人工会话参数不完整")
    platform, _kind = parse_source(source)
    if not platform:
        raise CsError("来源格式不正确，应为 平台_dm/平台_comment")
    if platform not in CS_PLATFORMS:
        raise CsError(f"暂不支持的平台来源：{platform}")
    account_key = (account_key or "").strip()
    with get_session() as s:
        # 同一条平台评论/私信只对应一条进行中的客服会话:早期匿名转人工可能
        # 以 account_id=0 建过会话,后续补齐回投账号时应复用并补全,而不是分叉。
        existing = s.exec(select(CsConversation).where(
            CsConversation.source == source,
            CsConversation.thread_key == thread_key,
            CsConversation.status != STATUS_CLOSED)).first()
        if existing:
            # 旧版工作台转人工时没带账号稳定标识，补上以便跨节点回投匹配
            changed = False
            if account_id and not existing.account_id:
                existing.account_id = account_id
                changed = True
            if account_key and not existing.account_key:
                existing.account_key = account_key
                changed = True
            if changed:
                s.add(existing)
                s.commit()
                s.refresh(existing)
            return existing
        conv = CsConversation(token=new_token(), source=source, account_id=account_id,
                              account_key=account_key,
                              thread_key=thread_key,
                              customer_name=(customer_name or "平台用户")[:50],
                              customer_avatar=customer_avatar or "",
                              status=STATUS_QUEUED, last_time=now_ts())
        s.add(conv)
        s.commit()
        s.refresh(conv)
        if first_message:
            _add_customer_message(s, conv, first_message, platform_msg_id="")
            s.commit()
            s.refresh(conv)
        return conv


def get_conversation(conv_id: int) -> CsConversation | None:
    with get_session() as s:
        conv = s.get(CsConversation, conv_id)
        if conv:
            s.expunge(conv)
        return conv


def get_conversation_by_token(token: str) -> CsConversation | None:
    with get_session() as s:
        conv = s.exec(select(CsConversation).where(CsConversation.token == token)).first()
        if conv:
            s.expunge(conv)
        return conv


def conversation_dict_safe(conv_id: int) -> dict | None:
    """按 id 取会话快照（独立短事务，供 API 层在对象已分离时使用）。"""
    with get_session() as s:
        conv = s.get(CsConversation, conv_id)
        return conversation_dict(s, conv) if conv else None


def list_conversations(agent: CsAgent, status: str = "",
                       source: str = "", platform: str = "",
                       unread_only: bool = False) -> list[dict]:
    """坐席可见的会话：自己归属的、被邀请的，以及队列中的（待接单，全员可见）。

    筛选：status（排队/进行中/已关闭）、source（精确来源，如 douyin_dm）、
    platform（平台，如 douyin）、unread_only（仅未读）。筛选在可见性之后应用，
    不改变可见性规则。
    """
    with get_session() as s:
        rows = s.exec(select(CsConversation).order_by(
            CsConversation.status.asc(), CsConversation.last_time.desc())).all()
        invited = {p.conv_id for p in s.exec(select(CsParticipant).where(
            CsParticipant.agent_id == agent.id)).all()}
        result = []
        for conv in rows:
            if status and conv.status != status:
                continue
            if source and conv.source != source:
                continue
            if platform:
                pf, _ = parse_source(conv.source)
                if pf != platform:
                    continue
            waiting = conv.status == STATUS_QUEUED and bool(conv.last_text)
            if not (waiting or conv.owner_agent_id == agent.id
                    or conv.id in invited or agent.role == "admin"):
                continue
            if unread_only and not (conv.unread_agent or 0):
                continue
            result.append(conversation_dict(s, conv))
        return result


def list_messages(conv_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
    with get_session() as s:
        stmt = select(CsMessage).where(CsMessage.conv_id == conv_id)
        if after_id:
            stmt = stmt.where(CsMessage.id > after_id)
        rows = s.exec(stmt.order_by(CsMessage.id.asc()).limit(limit)).all()
        payload = [message_dict(m) for m in rows]
        # 附带跨节点回投任务状态，让历史消息正确显示排队/失败标签
        msg_ids = [m.id for m in rows if m.sender_kind == SENDER_AGENT
                   and m.relayed is False]
        if msg_ids:
            jobs = s.exec(select(CsRelayJob).where(
                CsRelayJob.message_id.in_(msg_ids))).all()
            job_by_msg = {j.message_id: j for j in jobs}
            for item in payload:
                job = job_by_msg.get(item["id"])
                if not job:
                    continue
                item["relay_state"] = job.status
                item["relay_error"] = job.result_error or ""
                item["relay_fail_kind"] = job.fail_kind or ""
                item["relay_attempts"] = job.attempts or 0
                item["relay_resendable"] = job.status in (
                    RELAY_FAILED, RELAY_TIMEOUT)
        return payload


def _add_customer_message(s, conv: CsConversation, text: str,
                          platform_msg_id: str = "",
                          idem_key: str = "",
                          msg_ts: int = 0) -> CsMessage:
    text = (text or "").strip()
    msg = CsMessage(conv_id=conv.id, sender_kind=SENDER_CUSTOMER,
                    sender_name=conv.customer_name or "访客", text=text[:4000],
                    platform_msg_id=platform_msg_id, idem_key=idem_key)
    s.add(msg)
    conv.last_text = text[:200]
    # 平台消息带服务端时间（补拉时用于顺序）；实时消息缺省取入库时刻
    conv.last_time = int(msg_ts) or now_ts()
    conv.unread_agent = (conv.unread_agent or 0) + 1
    # 关闭后的访客新消息自动重新排队
    if conv.status == STATUS_CLOSED:
        conv.status = STATUS_QUEUED
        conv.owner_agent_id = None
        conv.closed_at = None
        _system_msg(s, conv.id, "访客再次发来消息，会话已重新进入接待队列。")
    s.add(conv)
    s.flush()
    return msg


def customer_send(token: str, text: str, nickname: str = "",
                  idem_key: str = "") -> CsMessage:
    with get_session() as s:
        conv = s.exec(select(CsConversation).where(CsConversation.token == token)).first()
        if not conv:
            raise CsError("会话不存在")
        if nickname.strip() and not conv.customer_name:
            conv.customer_name = nickname.strip()[:50]
            s.add(conv)
        try:
            # _add_customer_message 内部 s.flush() 会触发唯一索引校验
            msg = _add_customer_message(s, conv, text, idem_key=idem_key)
            s.commit()
        except IntegrityError:
            # 客户端幂等键重复：回滚并返回已存在的消息（不重复广播）
            s.rollback()
            existing = s.exec(select(CsMessage).where(
                CsMessage.conv_id == conv.id,
                CsMessage.idem_key == idem_key)).first()
            if existing:
                return existing
            raise CsError("消息提交失败，请重试")
        s.refresh(msg)
        payload = message_dict(msg)
        conv_id = conv.id
        status = conv.status
    bus.publish(bus.conv_room(conv_id), {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "conversation", "conv_id": conv_id,
                                  "status": status})
    return msg


def mirror_platform_message(source: str, account_id: int, thread_key: str,
                            peer_nickname: str, text: str,
                            platform_msg_id: str = "") -> int | None:
    """平台入站私信联动：已转人工的会话追加客户消息。返回会话 id（无则 None）。

    同一条平台消息（idem_key 全局唯一）重复投递只入库一次，且不重复广播。
    """
    text = (text or "").strip()
    if not text:
        return None
    source = (source or "").strip().lower()
    with get_session() as s:
        conv = s.exec(select(CsConversation).where(
            CsConversation.source == source,
            CsConversation.account_id == account_id,
            CsConversation.thread_key == thread_key,
            CsConversation.status != STATUS_CLOSED)).first()
        if not conv:
            return None
        idem_key = inbound_idem_key(
            source, conv.account_key, conv.account_id or account_id,
            platform_msg_id)
        # 幂等：先按全局键预检；再兜底会话内 platform_msg_id（历史数据）。
        if idem_key and s.exec(select(CsMessage).where(
                CsMessage.idem_key == idem_key)).first():
            return conv.id
        if platform_msg_id and s.exec(select(CsMessage).where(
                CsMessage.conv_id == conv.id,
                CsMessage.platform_msg_id == platform_msg_id)).first():
            return conv.id
        if peer_nickname and not conv.customer_name:
            conv.customer_name = peer_nickname[:50]
        msg = _add_customer_message(s, conv, text, platform_msg_id, idem_key)
        try:
            s.commit()
        except IntegrityError:
            # 并发投递下唯一索引兜底：视为重复消息，静默忽略。
            s.rollback()
            return conv.id
        s.refresh(msg)
        payload, conv_id, status = message_dict(msg), conv.id, conv.status
    bus.publish(bus.conv_room(conv_id), {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "conversation", "conv_id": conv_id,
                                  "status": status})
    return conv_id


# ─────────────── 入站游标 / 账号级接待开关（Task 13 总线使用）───────────────

def _normalize_scope(platform: str, account_key: str,
                     kind: str = KIND_DM) -> tuple[str, str, str]:
    platform = (platform or "").strip().lower()
    account_key = (account_key or "").strip()
    kind = (kind or "").strip().lower() or KIND_DM
    if not platform:
        raise CsError("缺少平台标识")
    if kind not in (KIND_DM, KIND_COMMENT):
        raise CsError("入站类型仅支持 dm / comment")
    return platform, account_key, kind


def get_inbound_cursor(platform: str, account_key: str, kind: str) -> str:
    platform, account_key, kind = _normalize_scope(platform, account_key, kind)
    with get_session() as s:
        row = s.exec(select(CsInboundCursor).where(
            CsInboundCursor.platform == platform,
            CsInboundCursor.account_key == account_key,
            CsInboundCursor.kind == kind)).first()
        return row.cursor if row else ""


def set_inbound_cursor(platform: str, account_key: str, kind: str,
                       cursor: str) -> None:
    platform, account_key, kind = _normalize_scope(platform, account_key, kind)
    cursor = (cursor or "")[:500]
    with get_session() as s:
        row = s.exec(select(CsInboundCursor).where(
            CsInboundCursor.platform == platform,
            CsInboundCursor.account_key == account_key,
            CsInboundCursor.kind == kind)).first()
        if row is None:
            row = CsInboundCursor(platform=platform, account_key=account_key,
                                  kind=kind)
        row.cursor = cursor
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()


def reception_enabled(platform: str, account_key: str) -> bool:
    """账号级接待开关；无记录默认开启（历史部署平滑兼容）。"""
    platform = (platform or "").strip().lower()
    account_key = (account_key or "").strip()
    if not platform or not account_key:
        return True
    with get_session() as s:
        row = s.exec(select(CsAccountReception).where(
            CsAccountReception.platform == platform,
            CsAccountReception.account_key == account_key)).first()
        return True if row is None else bool(row.enabled)


def set_reception_enabled(platform: str, account_key: str, enabled: bool) -> None:
    platform = (platform or "").strip().lower()
    account_key = (account_key or "").strip()
    if not platform or not account_key:
        raise CsError("缺少平台账号标识")
    with get_session() as s:
        row = s.exec(select(CsAccountReception).where(
            CsAccountReception.platform == platform,
            CsAccountReception.account_key == account_key)).first()
        if row is None:
            row = CsAccountReception(platform=platform, account_key=account_key)
        row.enabled = bool(enabled)
        row.updated_at = datetime.utcnow()
        s.add(row)
        s.commit()


# ───────────────────────── 坐席操作 ─────────────────────────

def _visible_conv(s, conv_id: int, agent: CsAgent) -> CsConversation:
    conv = s.get(CsConversation, conv_id)
    if not conv:
        raise CsError("会话不存在")
    invited = s.exec(select(CsParticipant).where(
        CsParticipant.conv_id == conv_id,
        CsParticipant.agent_id == agent.id)).first()
    if conv.owner_agent_id != agent.id and not invited and agent.role != "admin":
        raise CsError("无权操作该会话")
    return conv


def accept_conversation(conv_id: int, agent: CsAgent) -> dict:
    with get_session() as s:
        conv = s.get(CsConversation, conv_id)
        if not conv:
            raise CsError("会话不存在")
        if conv.status == STATUS_CLOSED:
            raise CsError("会话已关闭")
        previous_owner = conv.owner_agent_id
        conv.owner_agent_id = agent.id
        conv.status = STATUS_ACTIVE
        conv.closed_at = None
        if not s.exec(select(CsParticipant).where(
                CsParticipant.conv_id == conv_id,
                CsParticipant.agent_id == agent.id)).first():
            s.add(CsParticipant(conv_id=conv_id, agent_id=agent.id))
        s.add(conv)
        if previous_owner and previous_owner != agent.id:
            prev = s.get(CsAgent, previous_owner)
            _system_msg(s, conv_id,
                        f"{agent.display_name} 从 {prev.display_name if prev else '原客服'} 处接走了会话。")
        else:
            _system_msg(s, conv_id, f"{agent.display_name} 已接入会话。")
        s.commit()
        data = conversation_dict(s, conv)
        conv_id_e = conv.id
    _broadcast_conv(conv_id_e)
    return data


def _ensure_participant(s, conv_id: int, agent_id: int) -> bool:
    exists = s.exec(select(CsParticipant).where(
        CsParticipant.conv_id == conv_id,
        CsParticipant.agent_id == agent_id)).first()
    if not exists:
        s.add(CsParticipant(conv_id=conv_id, agent_id=agent_id))
        return True
    return False


def transfer_conversation(conv_id: int, target_agent_id: int, agent: CsAgent) -> dict:
    with get_session() as s:
        conv = _visible_conv(s, conv_id, agent)
        target = s.get(CsAgent, target_agent_id)
        if not target or not target.enabled:
            raise CsError("目标坐席不存在或已停用")
        previous = s.get(CsAgent, conv.owner_agent_id) if conv.owner_agent_id else None
        conv.owner_agent_id = target.id
        conv.status = STATUS_ACTIVE
        conv.closed_at = None
        _ensure_participant(s, conv_id, target.id)
        s.add(conv)
        _system_msg(s, conv_id,
                    f"{agent.display_name} 把会话转接给了 {target.display_name}。")
        s.commit()
        data = conversation_dict(s, conv)
        conv_id_e = conv.id
    _broadcast_conv(conv_id_e)
    return data


def invite_agent(conv_id: int, target_agent_id: int, agent: CsAgent) -> dict:
    with get_session() as s:
        conv = _visible_conv(s, conv_id, agent)
        target = s.get(CsAgent, target_agent_id)
        if not target or not target.enabled:
            raise CsError("目标坐席不存在或已停用")
        added = _ensure_participant(s, conv_id, target.id)
        if not added:
            raise CsError("该坐席已在会话中")
        _system_msg(s, conv_id,
                    f"{agent.display_name} 邀请 {target.display_name} 加入了会话。")
        s.commit()
        data = conversation_dict(s, conv)
    _broadcast_conv(conv_id)
    return data


def close_conversation(conv_id: int, agent: CsAgent) -> dict:
    with get_session() as s:
        conv = s.get(CsConversation, conv_id)
        if not conv:
            raise CsError("会话不存在")
        # 排队中的会话尚未分配坐席,任何在线坐席都可关闭(取消受理);
        # 进行中的会话仅负责人/参与者/管理员可结束。
        if conv.status != STATUS_QUEUED:
            _visible_conv(s, conv_id, agent)
        conv.status = STATUS_CLOSED
        conv.closed_at = datetime.utcnow()
        s.add(conv)
        _system_msg(s, conv_id, f"{agent.display_name} 结束了会话。")
        s.commit()
        data = conversation_dict(s, conv)
        conv_id_e = conv.id
    _broadcast_conv(conv_id_e)
    return data


def release_conversation(conv_id: int, agent: CsAgent) -> dict:
    """接待中的会话退回受理大厅(重新排队),无法继续受理时使用。"""
    with get_session() as s:
        conv = _visible_conv(s, conv_id, agent)
        if conv.status != STATUS_ACTIVE:
            raise CsError("仅接待中的会话可以退回受理大厅")
        conv.status = STATUS_QUEUED
        conv.owner_agent_id = None
        conv.closed_at = None
        s.add(conv)
        _system_msg(s, conv_id, f"{agent.display_name} 将会话退回了受理大厅。")
        s.commit()
        data = conversation_dict(s, conv)
        conv_id_e = conv.id
    _broadcast_conv(conv_id_e)
    return data


def mark_read(conv_id: int, agent: CsAgent) -> None:
    with get_session() as s:
        conv = _visible_conv(s, conv_id, agent)
        conv.unread_agent = 0
        s.add(conv)
        s.commit()


def add_system_message(conv_id: int, text: str) -> None:
    with get_session() as s:
        _system_msg(s, conv_id, text)
        s.commit()
    bus.publish(bus.conv_room(conv_id),
                {"type": "message", "message": {"conv_id": conv_id,
                 "sender_kind": SENDER_SYSTEM, "text": text}})
    bus.publish(bus.AGENTS_ROOM,
                {"type": "message", "message": {"conv_id": conv_id,
                 "sender_kind": SENDER_SYSTEM, "text": text}})


def agent_send(conv_id: int, agent: CsAgent, text: str) -> CsMessage:
    """坐席发言落库。平台回投结果由 api 层写回 relayed 标记。"""
    text = (text or "").strip()
    if not text:
        raise CsError("内容不能为空")
    with get_session() as s:
        conv = _visible_conv(s, conv_id, agent)
        if conv.status == STATUS_CLOSED:
            raise CsError("会话已关闭，请先重新接入")
        msg = CsMessage(conv_id=conv_id, sender_kind=SENDER_AGENT, agent_id=agent.id,
                        sender_name=agent.display_name, text=text[:4000])
        s.add(msg)
        conv.last_text = text[:200]
        conv.last_time = now_ts()
        s.add(conv)
        s.commit()
        s.refresh(msg)
        payload = message_dict(msg)
        conv_id_e = conv.id
    bus.publish(bus.conv_room(conv_id), {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "message", "message": payload})
    bus.publish(bus.AGENTS_ROOM, {"type": "conversation", "conv_id": conv_id_e})
    return msg


def mark_relayed(message_id: int, ok: bool) -> None:
    with get_session() as s:
        msg = s.get(CsMessage, message_id)
        if msg:
            msg.relayed = ok
            s.add(msg)
            s.commit()


def _broadcast_conv(conv_id: int) -> None:
    with get_session() as s:
        conv = s.get(CsConversation, conv_id)
        data = conversation_dict(s, conv) if conv else None
    bus.publish(bus.AGENTS_ROOM, {"type": "conversation", "conv_id": conv_id,
                                  "conversation": data})
    bus.publish(bus.conv_room(conv_id), {"type": "conversation", "conversation": data})


def counts_overview() -> dict:
    with get_session() as s:
        queued = len(s.exec(select(CsConversation).where(
            CsConversation.status == STATUS_QUEUED,
            CsConversation.last_text != "")).all())
        active = len(s.exec(select(CsConversation).where(
            CsConversation.status == STATUS_ACTIVE)).all())
        unread = sum(c.unread_agent or 0 for c in s.exec(
            select(CsConversation).where(CsConversation.status != STATUS_CLOSED)).all())
    return {"queued": queued, "active": active, "unread": unread}


# ───────────────── 跨节点回投：节点令牌 / 认领 / 回报 ─────────────────

def get_node_token() -> str:
    """节点令牌：环境变量优先（容器部署），其次读 AppSetting（管理界面配置）。"""
    return (os.environ.get(NODE_TOKEN_ENV) or "").strip() \
        or get_setting(NODE_TOKEN_SETTING, "")


def set_node_token(token: str) -> None:
    set_setting(NODE_TOKEN_SETTING, (token or "").strip())


def node_enabled() -> bool:
    return bool(get_node_token())


def _record_node_heartbeat(node_id: str, accounts: list[dict]) -> None:
    """落库节点心跳与路由信息（可回投账号清单），顺带清理过期节点。"""
    platforms = sorted({str(a.get("platform") or "") for a in (accounts or [])
                        if a.get("platform")})
    now = int(time.time())
    with get_session() as s:
        row = s.get(CsNodeHeartbeat, node_id)
        if row is None:
            row = CsNodeHeartbeat(node_id=node_id)
        row.platforms = ",".join(platforms)
        row.account_count = len(accounts or [])
        row.accounts_json = json.dumps(accounts or [], ensure_ascii=False)[:20000]
        row.last_seen = now
        row.updated_at = datetime.utcnow()
        s.add(row)
        stale = s.exec(select(CsNodeHeartbeat).where(
            CsNodeHeartbeat.last_seen < now - HEARTBEAT_RETENTION_SECONDS)).all()
        for old in stale:
            if old.node_id != node_id:
                s.delete(old)
        s.commit()


def list_node_heartbeats() -> list[dict]:
    """最近 5 分钟有心跳的节点；routes 为该节点可回投的平台账号路由清单。"""
    cutoff = int(time.time()) - 300
    with get_session() as s:
        rows = s.exec(select(CsNodeHeartbeat).where(
            CsNodeHeartbeat.last_seen >= cutoff)).all()
        result = []
        for row in rows:
            try:
                routes = json.loads(row.accounts_json or "[]")
            except (ValueError, TypeError):
                routes = []
            result.append({
                "node_id": row.node_id,
                "accounts": row.account_count,
                "platforms": [p for p in (row.platforms or "").split(",") if p],
                "last_seen": row.last_seen,
                "routes": routes,
            })
        return result


def relay_queue_overview() -> dict:
    with get_session() as s:
        rows = s.exec(select(CsRelayJob.status)).all()
    counts = {RELAY_PENDING: 0, RELAY_CLAIMED: 0,
              RELAY_DONE: 0, RELAY_FAILED: 0, RELAY_TIMEOUT: 0}
    for status in rows:
        counts[status] = counts.get(status, 0) + 1
    return {"jobs": counts, "nodes": list_node_heartbeats(),
            "enabled": node_enabled()}


def _fail_job_notify(s, job: CsRelayJob, error: str,
                     status: str = RELAY_FAILED,
                     fail_kind: str = FAIL_PERMANENT) -> None:
    """任务终态失败/超时：落库 + 会话系统消息 + 置 relayed。广播在提交后做。"""
    job.status = status
    job.fail_kind = (FAIL_TIMEOUT if status == RELAY_TIMEOUT else fail_kind)
    job.result_error = error[:300]
    job.next_run_at = None
    job.updated_at = datetime.utcnow()
    s.add(job)
    msg = s.get(CsMessage, job.message_id) if job.message_id else None
    if msg:
        msg.relayed = False
        s.add(msg)
    prefix = "⌛ 回投等待超时" if status == RELAY_TIMEOUT else "⚠️ 回投平台失败"
    _system_msg(s, job.conv_id, prefix + "：" + error)


def _broadcast_system_and_relay(conv_id: int, message_id: int,
                                ok: bool, error: str,
                                include_system: bool = True) -> None:
    """提交后广播：系统消息全文（仅失败时）+ relayed 结果 + 会话变化。"""
    if include_system and not ok:
        messages = list_messages(conv_id)
        sys_msg = next((m for m in reversed(messages)
                        if m.get("sender_kind") == SENDER_SYSTEM), None)
        if sys_msg:
            bus.publish(bus.conv_room(conv_id),
                        {"type": "message", "message": sys_msg})
    bus.publish(bus.conv_room(conv_id),
                {"type": "relayed", "conv_id": conv_id,
                 "message_id": message_id, "ok": ok, "error": error})
    bus.publish(bus.AGENTS_ROOM,
                {"type": "relayed", "conv_id": conv_id,
                 "message_id": message_id, "ok": ok, "error": error})
    bus.publish(bus.AGENTS_ROOM, {"type": "conversation", "conv_id": conv_id})


def claim_relay_jobs(node_id: str, accounts: list[dict],
                     max_jobs: int = 5) -> list[dict]:
    """工作台认领待回投任务。

    accounts: [{platform, account_key, account_id}]；优先按平台账号稳定标识
    精确匹配；account_key 为空的历史任务可被拥有同平台账号的节点认领，
    在节点侧走「按作品查监控绑定账号」兜底。
    """
    node_id = (node_id or "").strip()[:64]
    if not node_id:
        raise CsError("缺少节点标识")
    max_jobs = max(1, min(10, int(max_jobs or 5)))
    # 平台账号稳定标识 -> 该节点本地账号 id
    key_to_local: dict[tuple[str, str], int] = {}
    platforms: set[str] = set()
    for a in accounts or []:
        plat = str(a.get("platform") or "").strip().lower()
        key = str(a.get("account_key") or "").strip()
        if not plat:
            continue
        platforms.add(plat)
        if key:
            key_to_local[(plat, key)] = int(a.get("account_id") or 0)

    now = datetime.utcnow()
    stale_before = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    dead: list[tuple[int, int, str]] = []
    claimed: list[CsRelayJob] = []
    with get_session() as s:
        # 1) 回收超时认领；超过最大认领次数判 timeout（结果未知，需人工核查/重发）
        stale = s.exec(select(CsRelayJob).where(
            CsRelayJob.status == RELAY_CLAIMED,
            CsRelayJob.claimed_at < stale_before)).all()
        for job in stale:
            job.attempts = (job.attempts or 0) + 1
            if job.attempts >= CLAIM_MAX_ATTEMPTS:
                reason = (f"工作台「{job.claimed_by or '未知'}」认领后多次未回报，"
                          "请在平台侧核查是否已发出后手动重发")
                _fail_job_notify(s, job, reason, status=RELAY_TIMEOUT)
                dead.append((job.conv_id, job.message_id, reason))
            else:
                job.status = RELAY_PENDING
                job.claimed_by = ""
                job.claimed_at = None
                job.fail_kind = ""
                job.result_error = ""
                job.updated_at = now
                s.add(job)

        # 2) 挑本节点能执行的待处理任务（按入队时间，先到先得）；
        #    瞬时故障退避中的任务（next_run_at 未到）不可认领
        pending = s.exec(select(CsRelayJob).where(
            CsRelayJob.status == RELAY_PENDING
        ).order_by(CsRelayJob.id)).all()
        for job in pending:
            if len(claimed) >= max_jobs:
                break
            if job.next_run_at is not None and job.next_run_at > now:
                continue
            local_account_id = key_to_local.get((job.platform, job.account_key))
            if local_account_id is None:
                if job.account_key or job.platform not in platforms:
                    continue
                local_account_id = 0   # 历史无标识任务：节点侧按作品监控兜底
            job.status = RELAY_CLAIMED
            job.claimed_by = node_id
            job.claimed_at = now
            job.next_run_at = None
            job.attempts = (job.attempts or 0) + 1
            job.updated_at = now
            s.add(job)
            claimed.append((job, local_account_id))
        s.commit()
        payload = [{
            "id": job.id,
            "conv_id": job.conv_id,
            "message_id": job.message_id,
            "idem_key": job.idem_key or "",
            "source": job.source,
            "platform": job.platform,
            "kind": job.kind,
            "account_id": local_id,        # 已替换为认领节点的本地账号 id
            "account_key": job.account_key,
            "thread_key": job.thread_key,
            "text": job.text,
        } for job, local_id in claimed]
        conv_ids = [job.conv_id for job, _ in claimed]
    _record_node_heartbeat(node_id, accounts)
    # 判超时通知：系统消息全文 + relayed 失败事件（坐席侧可手动重发）
    for conv_id, message_id, reason in dead:
        _broadcast_system_and_relay(conv_id, message_id, False, reason)
    if conv_ids:
        bus.publish(bus.AGENTS_ROOM, {"type": "relay_claimed",
                                      "conv_ids": conv_ids})
    return payload


def complete_relay_job(job_id: int, node_id: str, ok: bool,
                       error: str = "",
                       retryable: Optional[bool] = None) -> dict:
    """工作台回报回投结果：写回消息 relayed 标记并广播给坐席。

    失败分类：永久故障（登录态/风控/参数）立即判 failed；瞬时故障（网络/
    繁忙）在执行次数上限内按指数退避重新排队，上限耗尽才判 failed。
    """
    node_id = (node_id or "").strip()[:64]
    error = (error or "").strip()
    with get_session() as s:
        job = s.get(CsRelayJob, job_id)
        if not job:
            raise CsError("回投任务不存在")
        if job.status not in (RELAY_PENDING, RELAY_CLAIMED):
            return {"ok": True, "dup": True}    # 幂等：重复回报直接忽略
        if job.claimed_by and node_id and job.claimed_by != node_id:
            raise CsError("任务已被其他节点认领")
        conv_id, message_id = job.conv_id, job.message_id

        if ok:
            job.status = RELAY_DONE
            job.fail_kind = ""
            job.result_error = ""
            job.next_run_at = None
            job.updated_at = datetime.utcnow()
            s.add(job)
            msg = s.get(CsMessage, message_id) if message_id else None
            if msg:
                msg.relayed = True
                s.add(msg)
            s.commit()
            broadcast = (conv_id, message_id, True, "", False)
        else:
            kind = classify_relay_failure(error, retryable)
            will_retry = (kind == FAIL_TRANSIENT
                          and (job.attempts or 0) < RELAY_TRANSIENT_MAX_ATTEMPTS)
            if will_retry:
                # 瞬时故障：释放认领并按指数退避重新排队，坐席可见「自动重试中」
                delay = _backoff_delay(job.attempts or 1)
                job.status = RELAY_PENDING
                job.claimed_by = ""
                job.claimed_at = None
                job.fail_kind = FAIL_TRANSIENT
                job.result_error = error[:300]
                job.next_run_at = datetime.utcnow() + timedelta(seconds=delay)
                job.updated_at = datetime.utcnow()
                s.add(job)
                notice = (f"平台回投遇到瞬时问题（{error or '网络繁忙'}），"
                          f"{delay}s 后自动重试"
                          f"（第 {job.attempts}/{RELAY_TRANSIENT_MAX_ATTEMPTS} 次）")
                _system_msg(s, conv_id, "🔁 " + notice)
                s.commit()
                broadcast = (conv_id, message_id, False, notice, True)
            else:
                reason = error or "重试次数已耗尽"
                if kind == FAIL_TRANSIENT:
                    reason = f"多次自动重试仍失败：{error or '网络异常'}"
                _fail_job_notify(s, job, reason, status=RELAY_FAILED,
                                 fail_kind=kind)
                s.commit()
                broadcast = (conv_id, message_id, False, reason, True)
    _broadcast_system_and_relay(*broadcast)
    return {"ok": True}


def retry_relay_job(message_id: int) -> dict:
    """坐席手动重发失败/超时的回投（同一行任务、同一幂等键，平台侧不重复）。

    复位执行计数与退避，重新排队等待认领；每次操作写系统消息留痕。
    """
    with get_session() as s:
        job = s.exec(select(CsRelayJob).where(
            CsRelayJob.message_id == message_id)).first()
        if not job:
            raise CsError("该消息没有回投任务")
        if job.status == RELAY_DONE:
            raise CsError("该消息已成功回投，无需重发")
        if job.status in (RELAY_PENDING, RELAY_CLAIMED):
            raise CsError("该消息正在投递中，请等待结果")
        old_state = job.status
        job.status = RELAY_PENDING
        job.claimed_by = ""
        job.claimed_at = None
        job.next_run_at = None
        job.attempts = 0
        job.fail_kind = ""
        job.result_error = ""
        job.updated_at = datetime.utcnow()
        s.add(job)
        _system_msg(s, job.conv_id,
                    "🔁 坐席已手动重发"
                    + ("（此前超时，结果未知）" if old_state == RELAY_TIMEOUT
                       else "（此前失败）"))
        conv_id = job.conv_id
        s.commit()
    _broadcast_system_and_relay(conv_id, message_id, False,
                                "坐席手动重发，已重新排队",
                                include_system=True)
    bus.publish(bus.AGENTS_ROOM, {"type": "relay_queued", "conv_id": conv_id,
                                  "message_id": message_id})
    return {"ok": True, "relay_state": RELAY_PENDING}


# ───────────────────────── 快捷回复（坐席个人模板） ─────────────────────────

_QUICK_REPLIES_KEY = "cs_quick_replies_{agent_id}"


def _quick_replies_key(agent_id: int) -> str:
    return _QUICK_REPLIES_KEY.format(agent_id=agent_id)


def list_quick_replies(agent_id: int) -> list[dict]:
    """坐席个人快捷回复模板列表（按创建顺序）。"""
    raw = get_setting(_quick_replies_key(agent_id), "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("text")]


def create_quick_reply(agent_id: int, title: str, text: str) -> dict:
    title = (title or "").strip()[:50]
    text = (text or "").strip()
    if not text:
        raise CsError("快捷回复内容不能为空")
    items = list_quick_replies(agent_id)
    qr = {"id": new_token()[:16], "title": title or text[:20], "text": text}
    items.append(qr)
    set_setting(_quick_replies_key(agent_id), json.dumps(items, ensure_ascii=False))
    return qr


def update_quick_reply(agent_id: int, qr_id: str, title: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise CsError("快捷回复内容不能为空")
    items = list_quick_replies(agent_id)
    for d in items:
        if d.get("id") == qr_id:
            d["title"] = (title or "").strip()[:50] or text[:20]
            d["text"] = text
            set_setting(_quick_replies_key(agent_id),
                        json.dumps(items, ensure_ascii=False))
            return d
    raise CsError("快捷回复不存在")


def delete_quick_reply(agent_id: int, qr_id: str) -> None:
    items = list_quick_replies(agent_id)
    kept = [d for d in items if d.get("id") != qr_id]
    if len(kept) == len(items):
        raise CsError("快捷回复不存在")
    set_setting(_quick_replies_key(agent_id),
                json.dumps(kept, ensure_ascii=False))
