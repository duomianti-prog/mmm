# -*- coding: utf-8 -*-
"""多平台统一入站总线（Task 13）。

四个来源汇入同一个 sink：
1. 抖音私信 WS（im_receiver，断线重连后由拉取侧补漏）；
2. 小红书私信（dm_automation push 唤醒 + 历史补拉）；
3. TikTok 私信收件箱（main.sync_dm 快照同步）；
4. 本账号作品评论（monitor 各平台轮询发现的新评论）。

统一语义：
- 账号级接待开关关闭 → 整账号丢弃（不建档、不镜像）；
- 首次入站自动建档（queued），同 source+账号+平台对象复用一条进行中会话；
- 平台消息全局幂等键（CsMessage 部分唯一索引），重复投递只入库一次；
- 入站游标按 platform+账号+kind 持久化，重连/重启后由来源侧据此补拉，
  补拉批次按服务端时间升序落库，保证会话内消息顺序；
- 坐席不在线也照常入库/广播（消息落库即存在，SSE 只负责在线分发）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..db import get_session
from . import service
from .events import bus
from .models import (
    CS_PLATFORMS, CsConversation, CsMessage,
    KIND_COMMENT, KIND_DM, STATUS_CLOSED, STATUS_QUEUED,
    make_source, parse_source,
)
from .security import new_token


@dataclass
class InboundMessage:
    """一条平台入站消息（私信或评论）。"""
    platform: str
    kind: str                        # dm | comment
    account_id: int
    thread_key: str                  # 私信=平台会话 id；评论={aweme_id}:{cid}
    text: str
    platform_msg_id: str = ""
    account_key: str = ""            # 抖音 sec_uid / 小红书 user_id / TikTok sec_uid
    peer_uid: str = ""
    peer_nickname: str = ""
    peer_avatar: str = ""
    msg_ts: int = 0                  # 平台服务端 unix 秒；补拉排序依据
    cursor: str = ""                 # 本条携带的不透明游标（批次取最新一条）


@dataclass
class IngestResult:
    ingested: int = 0
    duplicates: int = 0
    ignored: int = 0
    conv_ids: List[int] = field(default_factory=list)
    created_conv_ids: List[int] = field(default_factory=list)
    cursor: str = ""

    def merge(self, other: "IngestResult") -> None:
        self.ingested += other.ingested
        self.duplicates += other.duplicates
        self.ignored += other.ignored
        for cid in other.conv_ids:
            if cid not in self.conv_ids:
                self.conv_ids.append(cid)
        for cid in other.created_conv_ids:
            if cid not in self.created_conv_ids:
                self.created_conv_ids.append(cid)
        if other.cursor:
            self.cursor = other.cursor


def _validate(m: InboundMessage) -> Optional[str]:
    """返回归一化后的 source；非法消息返回 None。"""
    platform = (m.platform or "").strip().lower()
    kind = (m.kind or "").strip().lower()
    if platform not in CS_PLATFORMS or kind not in (KIND_DM, KIND_COMMENT):
        return None
    if not (m.thread_key or "").strip() or not (m.text or "").strip():
        return None
    if kind == KIND_COMMENT and not (m.platform_msg_id or "").strip():
        return None
    m.platform, m.kind = platform, kind
    m.thread_key = m.thread_key.strip()
    m.text = m.text.strip()
    m.platform_msg_id = (m.platform_msg_id or "").strip()
    m.account_key = (m.account_key or "").strip()
    return make_source(platform, kind)


def _find_or_create_conv(s, source: str, m: InboundMessage,
                         scope: str, auto_create: bool
                         ) -> tuple[Optional[CsConversation], bool]:
    """定位进行中的客服会话；找不到且允许自动建档时创建（queued）。"""
    rows = s.exec(select(CsConversation).where(
        CsConversation.source == source,
        CsConversation.thread_key == m.thread_key)).all()
    conv = None
    # 优先进行中的会话；没有则复用已关闭会话（由消息入库逻辑自动重开排队）
    for row in rows:
        if row.status != STATUS_CLOSED and m.account_key and row.account_key == m.account_key:
            conv = row
            break
    if conv is None:
        for row in rows:
            if row.status != STATUS_CLOSED and m.account_id and row.account_id == m.account_id:
                conv = row
                break
    if conv is None and rows and not m.account_key and not m.account_id:
        conv = next((r for r in rows if r.status != STATUS_CLOSED), None)
    if conv is None:
        conv = next((r for r in rows if m.account_key and r.account_key == m.account_key), None)
    if conv is None:
        conv = next((r for r in rows if m.account_id and r.account_id == m.account_id), None)
    if conv is not None:
        # 补齐历史缺失的账号标识，顺带更新昵称/头像
        changed = False
        if m.account_id and not conv.account_id:
            conv.account_id = m.account_id
            changed = True
        if m.account_key and not conv.account_key:
            conv.account_key = m.account_key
            changed = True
        if m.peer_nickname and not conv.customer_name:
            conv.customer_name = m.peer_nickname[:50]
            changed = True
        if m.peer_avatar and not conv.customer_avatar:
            conv.customer_avatar = m.peer_avatar[:500]
            changed = True
        if changed:
            s.add(conv)
        return conv, False
    if not auto_create:
        return None, False
    conv = CsConversation(
        token=new_token(), source=source, account_id=m.account_id or 0,
        account_key=m.account_key, thread_key=m.thread_key,
        customer_name=(m.peer_nickname or "")[:50],
        customer_avatar=(m.peer_avatar or "")[:500],
        status=STATUS_QUEUED, last_time=int(m.msg_ts or 0))
    s.add(conv)
    s.flush()
    return conv, True


def ingest(m: InboundMessage, *, auto_create: bool = True) -> IngestResult:
    """单条入站。供实时通道（WS/push）逐条调用。"""
    return ingest_batch([m], auto_create=auto_create)


def ingest_batch(messages: List[InboundMessage], *,
                 auto_create: bool = True,
                 cursor: str = "") -> IngestResult:
    """批量入站（补拉/轮询快照）。

    - 按 (msg_ts, platform_msg_id) 升序落库，保证会话内顺序正确；
    - 批次成功后持久化游标（cursor 参数优先，否则取批次内最新非空游标）。
    每条消息独立事务，单条失败不影响其余消息。
    """
    result = IngestResult(cursor=cursor or "")
    valid: List[tuple[str, InboundMessage]] = []
    for m in messages:
        source = _validate(m)
        if source is None:
            result.ignored += 1
            continue
        valid.append((source, m))
    # 稳定排序：有服务端时间按时间；无时间（0）排最前，其次按平台消息 id
    valid.sort(key=lambda pair: (pair[1].msg_ts or 0,
                                 pair[1].platform_msg_id or ""))

    for source, m in valid:
        scope = service.account_scope_key(m.account_key, m.account_id)
        # 账号级接待开关（无 key 的历史账号用 id: 作用域判断）
        if not service.reception_enabled(m.platform, scope):
            result.ignored += 1
            if m.cursor and not result.cursor:
                result.cursor = m.cursor
            continue
        idem = service.inbound_idem_key(
            source, m.account_key, m.account_id, m.platform_msg_id)
        broadcast: list[dict] = []
        try:
            with get_session() as s:
                if idem and s.exec(select(CsMessage).where(
                        CsMessage.idem_key == idem)).first():
                    result.duplicates += 1
                    if m.cursor and not result.cursor:
                        result.cursor = m.cursor
                    continue
                conv, created = _find_or_create_conv(
                    s, source, m, scope, auto_create)
                if conv is None:
                    # 未自动建档（手动转人工前不入库客服侧）
                    result.ignored += 1
                    continue
                msg = service._add_customer_message(
                    s, conv, m.text, m.platform_msg_id, idem, m.msg_ts)
                s.commit()
                s.refresh(msg)
                s.refresh(conv)
                payload = service.message_dict(msg)
                status = conv.status
                conv_id = conv.id
        except IntegrityError:
            # 并发投递下部分唯一索引兜底
            result.duplicates += 1
            continue
        except Exception as e:
            print(f"[cs-inbound] {source} 入站失败（不影响来源通道）: {e!r}")
            result.ignored += 1
            continue
        result.ingested += 1
        if conv_id not in result.conv_ids:
            result.conv_ids.append(conv_id)
        if created and conv_id not in result.created_conv_ids:
            result.created_conv_ids.append(conv_id)
        if m.cursor:
            result.cursor = m.cursor
        # 入库后再广播（坐席不在线也已落库；总线无在线订阅者时为空操作）
        bus.publish(bus.conv_room(conv_id),
                    {"type": "message", "message": payload})
        bus.publish(bus.AGENTS_ROOM,
                    {"type": "message", "message": payload})
        bus.publish(bus.AGENTS_ROOM,
                    {"type": "conversation", "conv_id": conv_id,
                     "status": status,
                     "inbound_created": created})

    final_cursor = cursor or result.cursor
    if final_cursor and valid:
        last = valid[-1][1]
        scope = service.account_scope_key(last.account_key, last.account_id)
        try:
            service.set_inbound_cursor(
                last.platform, scope, last.kind, final_cursor)
        except Exception as e:
            print(f"[cs-inbound] 游标持久化失败: {e!r}")
    return result


# ── 来源侧便捷封装 ────────────────────────────────────────────────────────

def ingest_dm(platform: str, account_id: int, account_key: str,
              thread_key: str, text: str, platform_msg_id: str,
              *, peer_uid: str = "", peer_nickname: str = "",
              peer_avatar: str = "", msg_ts: int = 0,
              cursor: str = "", auto_create: bool = True) -> IngestResult:
    """单条私信入站（抖音 WS / TikTok 快照逐条调用）。"""
    return ingest(InboundMessage(
        platform=platform, kind=KIND_DM, account_id=account_id,
        account_key=account_key, thread_key=thread_key, text=text,
        platform_msg_id=platform_msg_id, peer_uid=peer_uid,
        peer_nickname=peer_nickname, peer_avatar=peer_avatar,
        msg_ts=msg_ts, cursor=cursor), auto_create=auto_create)


def ingest_dm_batch(platform: str, account_id: int, account_key: str,
                    rows: list[dict], *, cursor: str = "",
                    auto_create: bool = True) -> IngestResult:
    """私信批量入站（小红书历史补拉 / 收件箱快照）。

    rows 每项：thread_key / text / platform_msg_id / peer_uid /
    peer_nickname / peer_avatar / msg_ts / cursor（可选）。
    """
    msgs = [InboundMessage(
        platform=platform, kind=KIND_DM, account_id=account_id,
        account_key=account_key,
        thread_key=str(r.get("thread_key") or ""),
        text=str(r.get("text") or ""),
        platform_msg_id=str(r.get("platform_msg_id") or ""),
        peer_uid=str(r.get("peer_uid") or ""),
        peer_nickname=str(r.get("peer_nickname") or ""),
        peer_avatar=str(r.get("peer_avatar") or ""),
        msg_ts=int(r.get("msg_ts") or 0),
        cursor=str(r.get("cursor") or "")) for r in rows]
    return ingest_batch(msgs, auto_create=auto_create, cursor=cursor)


def ingest_comment(platform: str, account_id: int, account_key: str,
                   aweme_id: str, comment_id: str, text: str,
                   *, peer_uid: str = "", peer_nickname: str = "",
                   peer_avatar: str = "", msg_ts: int = 0) -> IngestResult:
    """本账号作品单条新评论入站。

    thread_key 与评论回投约定一致：``{aweme_id}:{comment_id}``。
    """
    return ingest(InboundMessage(
        platform=platform, kind=KIND_COMMENT, account_id=account_id,
        account_key=account_key,
        thread_key=f"{aweme_id}:{comment_id}", text=text,
        platform_msg_id=str(comment_id), peer_uid=peer_uid,
        peer_nickname=peer_nickname, peer_avatar=peer_avatar,
        msg_ts=msg_ts))
