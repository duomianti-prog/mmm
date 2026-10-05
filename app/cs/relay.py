# -*- coding: utf-8 -*-
"""坐席回复回投到来源平台的适配层。

来源 source 命名：``{platform}_{kind}``，kind ∈ dm | comment，
例如 douyin_dm / xhs_dm / douyin_comment / xhs_comment。

- 私信：复用写操作队列 AccountActionTask（send_dm），由持有平台登录态的
  工作台引擎执行（imapi/页面自动化），与手动发私信完全同一条链路。
- 评论：复用 CommentTask（reply 目标评论），同样走引擎的评论执行链路。

部署形态：
- 工作台本机（引擎在线）：直接执行回投，同步得到结果。
- 纯云服务器客服节点（引擎为 None）：回投写入 CsRelayJob 队列，由持有
  平台账号登录态的 Windows 工作台通过节点接口认领执行并回报结果。
"""
from __future__ import annotations

import asyncio

from sqlmodel import select

from ..db import get_session
from ..engine.monitor import AccountBusyError
from ..models import (
    AccountActionTask, CommentRecord, CommentTask, CommentWatch, DmConversation,
    DouyinAccount,
)
from .models import (
    CsConversation, CsRelayJob, RELAY_CLAIMED, RELAY_PENDING,
)


def relay_idem_key(message_id: int) -> str:
    """同一条坐席回复对应唯一回投幂等键（入队/重试/多轮执行全程不变）。"""
    return f"relay:m{int(message_id or 0)}" if message_id else ""

# 回投最长等待（浏览器写操作可能较慢）。超时不判失败为"已拒绝"，按未送达处理。
RELAY_TIMEOUT_SECONDS = 150

_engine_getter = lambda: None  # noqa: E731 — 由 main 启动时注入


def set_engine_getter(getter) -> None:
    global _engine_getter
    _engine_getter = getter


def _mark_relay_timeout(model, task_id: int) -> None:
    """回投被 150s 超时 cancel 后,任务可能还停在 doing(前端显示"发送中")。
    必须在 cancel 路径收尾为 failed,否则任务永久卡在进行中。浏览器子任务
    可能实际上已发出,所以文案提示人工核查,不自动重试。"""
    try:
        with get_session() as s:
            t = s.get(model, task_id)
            if t and getattr(t, "status", "") == "doing":
                t.status = "failed"
                t.error = (f"回投等待超过 {RELAY_TIMEOUT_SECONDS}s 被中断，"
                           "请在平台侧核查是否已发出")
                s.add(t)
                s.commit()
    except Exception:
        pass


def _split_source(source: str) -> tuple[str, str]:
    platform, _, kind = str(source or "").partition("_")
    return platform, kind


async def relay_message(conv: dict, text: str) -> dict:
    """把一条坐席回复投递到来源平台。

    返回 ``{"state": "sent"|"queued", "ok": bool, "error": str}``：
    - sent：本机引擎已执行，ok/error 为平台结果；
    - queued：本机无引擎，任务已入跨节点队列，等待工作台认领，relayed 暂不置位。
    """
    source = str(conv.get("source") or "")
    platform, kind = _split_source(source)
    account_id = int(conv.get("account_id") or 0)
    account_key = str(conv.get("account_key") or "")
    thread_key = str(conv.get("thread_key") or "")
    text = (text or "").strip()
    if not source or source == "guest":
        return {"state": "sent", "ok": True, "error": ""}     # 访客链接会话无需回投
    if not text:
        return {"state": "sent", "ok": False, "error": "内容为空"}
    engine = _engine_getter()
    if engine is None:
        # 纯客服节点：排队给持有平台登录态的工作台执行
        job_id = enqueue_relay_job(
            source=source, platform=platform, kind=kind,
            account_id=account_id, account_key=account_key,
            thread_key=thread_key, text=text,
            conv_id=int(conv.get("conv_id") or 0),
            message_id=int(conv.get("message_id") or 0))
        return {"state": "queued", "ok": job_id > 0,
                "error": "" if job_id > 0 else "回投任务入队失败"}
    try:
        if kind == "dm":
            ok, error = await asyncio.wait_for(
                _relay_dm(engine, platform, account_id, thread_key, text),
                timeout=RELAY_TIMEOUT_SECONDS)
            return {"state": "sent", "ok": ok, "error": error}
        if kind == "comment":
            ok, error = await asyncio.wait_for(
                _relay_comment(engine, platform, account_id, thread_key, text,
                               account_key=account_key),
                timeout=RELAY_TIMEOUT_SECONDS)
            return {"state": "sent", "ok": ok, "error": error}
    except asyncio.TimeoutError:
        return {"state": "sent", "ok": False,
                "error": f"平台回投超时（{RELAY_TIMEOUT_SECONDS}s），请在平台侧核查是否已发出"}
    except Exception as e:  # 任何异常都不能冲垮客服会话
        return {"state": "sent", "ok": False, "error": f"平台回投异常：{e!r}"}
    return {"state": "sent", "ok": False,
            "error": f"暂不支持的会话来源：{source}"}


def enqueue_relay_job(*, source: str, platform: str, kind: str,
                      account_id: int, account_key: str,
                      thread_key: str, text: str,
                      conv_id: int, message_id: int) -> int:
    """纯客服节点把回投写入跨节点队列。

    幂等：同一条坐席消息（idem_key=relay:m<id>）全生命周期只有一行任务——
    排队/认领中重复入队直接复用；已成功/失败/超时也不新建（手动重发走
    retry_relay_job 显式复位同一行），保证平台侧写操作不会因重复入队而多发。
    """
    idem_key = relay_idem_key(message_id)
    with get_session() as s:
        existing = None
        if idem_key:
            existing = s.exec(select(CsRelayJob).where(
                CsRelayJob.idem_key == idem_key)).first()
        if existing is None and message_id:
            # 兼容升级前无 idem_key 的历史队列
            existing = s.exec(select(CsRelayJob).where(
                CsRelayJob.message_id == message_id)).first()
        if existing is not None:
            return int(existing.id)
        # 顺带把平台账号稳定标识补到会话上（旧会话可能没有）
        if conv_id and account_key:
            conv_row = s.get(CsConversation, conv_id)
            if conv_row and not conv_row.account_key:
                conv_row.account_key = account_key
                s.add(conv_row)
        job = CsRelayJob(
            conv_id=conv_id, message_id=message_id, source=source,
            platform=platform, kind=kind, account_id=account_id or 0,
            account_key=account_key or "", thread_key=thread_key,
            text=text[:4000], status=RELAY_PENDING, idem_key=idem_key)
        s.add(job)
        try:
            s.commit()
        except Exception:
            # 并发入队下幂等唯一索引兜底：复用已存在的那行
            s.rollback()
            row = s.exec(select(CsRelayJob).where(
                CsRelayJob.idem_key == idem_key)).first()
            if row is not None:
                return int(row.id)
            raise
        s.refresh(job)
        return int(job.id)


async def _relay_dm(engine, platform: str, account_id: int,
                    conv_id: str, text: str) -> tuple[bool, str]:
    """私信回投：补全会话对方信息 → 建 send_dm 写任务 → 立即执行。"""
    if not conv_id:
        return False, "缺少平台会话标识（conv_id）"
    target_uid = target_sec_uid = target_nick = ""
    with get_session() as s:
        row = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if row:
            target_uid = row.peer_uid or ""
            target_sec_uid = row.peer_sec_uid or ""
            target_nick = row.peer_nickname or ""
        task = AccountActionTask(
            platform=platform, account_id=account_id, action="send_dm",
            target_uid=target_uid, target_sec_uid=target_sec_uid,
            target_nick=target_nick, conv_id=conv_id, content=text,
            status="pending")
        s.add(task)
        s.commit()
        s.refresh(task)
        task_id = task.id
    try:
        res = await engine.execute_action_task(task_id, manual=True)
    except AccountBusyError as exc:
        return False, str(exc)
    except asyncio.CancelledError:
        _mark_relay_timeout(AccountActionTask, task_id)
        raise
    if res.get("ok"):
        return True, ""
    return False, str(res.get("error") or "发送失败")

async def _relay_comment(engine, platform: str, account_id: int,
                         thread_key: str, text: str,
                         account_key: str = "") -> tuple[bool, str]:
    """评论回投：thread_key = {aweme_id}:{comment_id}，复用 CommentTask。"""
    aweme_id, _, target_cid = thread_key.partition(":")
    if not aweme_id:
        return False, "缺少作品标识（aweme_id）"
    target_nick = target_text = ""
    with get_session() as s:
        if not account_id:
            # 兜底1:转人工时未带账号,按作品监控绑定的抓取账号回投
            watch = s.exec(select(CommentWatch).where(
                CommentWatch.platform == platform,
                CommentWatch.aweme_id == aweme_id)).first()
            if watch and watch.account_id:
                account_id = int(watch.account_id)
        if not account_id and account_key:
            # 兜底2:跨节点认领只带来了账号稳定标识(sec_uid/user_id),
            # 在本机库解析出实际账号 id。
            row = s.exec(select(DouyinAccount).where(
                DouyinAccount.platform == platform,
                DouyinAccount.sec_uid == account_key,
                DouyinAccount.status == "active")).first()
            if row:
                account_id = int(row.id)
        if not account_id:
            # 兜底3:回复他人作品评论不要求是作品发布者,同平台只有一个
            # 有效登录账号时直接由它承担回投;多个有效账号无法替用户抉择,
            # 给出可操作的明确指引。
            actives = [a for a in s.exec(select(DouyinAccount).where(
                DouyinAccount.platform == platform,
                DouyinAccount.status == "active")).all()
                if (a.sec_uid or "").strip()]
            if len(actives) == 1:
                account_id = int(actives[0].id)
        record = None
        if target_cid:
            record = s.exec(select(CommentRecord).where(
                CommentRecord.platform == platform,
                CommentRecord.aweme_id == aweme_id,
                CommentRecord.comment_id == target_cid
            ).order_by(CommentRecord.id.desc())).first()
        if record:
            target_nick = record.user_nickname or ""
            target_text = record.text or ""
        if not account_id:
            return False, ("未找到可用于回投的同平台登录账号：请在该评论所属监控上"
                           "绑定已登录账号,或转人工时选择回投账号后再回复")
        task = CommentTask(
            platform=platform, account_id=account_id,
            aweme_id=aweme_id, target_comment_id=target_cid,
            target_nick=target_nick, target_text=target_text,
            content=text, status="pending")
        s.add(task)
        s.commit()
        s.refresh(task)
        task_id = task.id
    # 坐席在接待台点发送等同界面「立即发送」:manual=True 绕过活跃时段/操作
    # 节奏等软节流(登录态失效、代理、风控冷却等硬门槛仍由执行链路拦截)。
    try:
        res = await engine.execute_comment_task(task_id, manual=True)
    except AccountBusyError as exc:
        return False, str(exc)
    except asyncio.CancelledError:
        _mark_relay_timeout(CommentTask, task_id)
        raise
    if res.get("ok"):
        return True, ""
    return False, str(res.get("error") or "评论发送失败")
