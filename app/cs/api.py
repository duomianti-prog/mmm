# -*- coding: utf-8 -*-
"""客服子系统 HTTP/SSE 接口。

- 访客（凭会话 token 免登录）：``/chat/{token}`` 页面 + ``/api/cs/guest/*``
- 坐席（登录后 Bearer token；SSE 允许用 ?token=）：``/api/cs/agent/*``
- 管理员（role=admin）：``/api/cs/agent/admin/*``
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import service
from .events import bus
from .models import CS_PLATFORMS, SOURCE_GUEST, parse_source
from .relay import relay_message

router = APIRouter()

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 登录失败限流：每个来源 IP 每 10 分钟最多 5 次失败
_LOGIN_FAILS: dict[str, deque] = {}
_LOGIN_WINDOW = 600
_LOGIN_MAX_FAILS = 5

# 后台回投任务强引用，避免被 GC 中途回收
_BG_TASKS: set[asyncio.Task] = set()


# ───────────────────────── 鉴权与小工具 ─────────────────────────

def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # EventSource 无法自定义请求头，SSE 允许 query token
    return request.query_params.get("token", "").strip()


def _current_agent(request: Request):
    token = _bearer_token(request)
    agent = service.agent_from_token(token)
    if not agent:
        raise HTTPException(401, "未登录或登录已已过期")
    request.state.cs_token = token
    return agent


def _require_admin(request: Request):
    agent = _current_agent(request)
    if agent.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return agent


def _client_key(request: Request) -> str:
    if request.client:
        return request.client.host or "?"
    return "?"


def _login_rate_limited(request: Request) -> bool:
    now = time.time()
    key = _client_key(request)
    bucket = _LOGIN_FAILS.setdefault(key, deque())
    while bucket and now - bucket[0] > _LOGIN_WINDOW:
        bucket.popleft()
    return len(bucket) >= _LOGIN_MAX_FAILS


def _record_login_fail(request: Request) -> None:
    _LOGIN_FAILS.setdefault(_client_key(request), deque()).append(time.time())


def _guest_conv_or_404(token: str):
    conv = service.get_conversation_by_token(token)
    if not conv:
        raise HTTPException(404, "会话不存在或链接已失效")
    return conv


def _guest_conv_dict(conv) -> dict:
    return {
        "token": conv.token, "source": conv.source,
        "customer_name": conv.customer_name, "status": conv.status,
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ───────────────────────── 访客页面 ─────────────────────────

@router.get("/chat/{token}")
async def guest_chat_page(token: str):
    """访客客服页（独立移动 H5，无工作台框架）。token 错误也返回页面，由页面自行提示。"""
    page = _WEB_DIR / "chat.html"
    if not page.exists():
        raise HTTPException(404, "客服页面缺失")
    return FileResponse(str(page), media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store"})


# ───────────────────────── 访客接口 ─────────────────────────

@router.get("/api/cs/guest/{token}")
async def guest_state(token: str, after_id: int = 0):
    conv = _guest_conv_or_404(token)
    return {"conversation": _guest_conv_dict(conv),
            "messages": service.list_messages(conv.id, after_id=after_id)}


class GuestMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    nickname: str = Field(default="", max_length=50)
    idem_key: str = Field(default="", max_length=64)


@router.post("/api/cs/guest/{token}/messages")
async def guest_send(token: str, body: GuestMessageIn):
    conv = _guest_conv_or_404(token)
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "内容不能为空")
    msg = service.customer_send(token, text, body.nickname.strip(),
                                idem_key=body.idem_key.strip())
    refreshed = service.get_conversation(conv.id)
    return {"message": service.message_dict(msg),
            "status": refreshed.status if refreshed else conv.status}


@router.get("/api/cs/guest/{token}/stream")
async def guest_stream(token: str, request: Request):
    conv = _guest_conv_or_404(token)
    room = bus.conv_room(conv.id)

    async def gen():
        q = bus.subscribe(room)
        try:
            yield "retry: 3000\n" + _sse({"type": "hello", "conv_id": conv.id})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse(evt)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(room, q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


# ───────────────────────── 坐席登录 ─────────────────────────

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/api/cs/agent/login")
async def agent_login(body: LoginIn, request: Request):
    if _login_rate_limited(request):
        raise HTTPException(429, "登录尝试过于频繁，请 10 分钟后再试")
    try:
        token, agent = service.login(
            body.username, body.password,
            user_agent=request.headers.get("user-agent", ""))
    except service.CsError as e:
        _record_login_fail(request)
        raise HTTPException(400, str(e))
    return {"token": token, "agent": service.agent_dict(agent)}


@router.post("/api/cs/agent/logout")
async def agent_logout(request: Request):
    service.logout(_bearer_token(request))
    return {"ok": True}


@router.get("/api/cs/agent/me")
async def agent_me(request: Request):
    return service.agent_dict(_current_agent(request))


# ───────────────────────── 坐席会话操作 ─────────────────────────

@router.get("/api/cs/agent/counts")
async def agent_counts(request: Request):
    _current_agent(request)
    return service.counts_overview()


@router.get("/api/cs/agent/agents")
async def agent_picker(request: Request):
    """转接/邀请用的在线坐席列表（仅启用的，不含敏感字段）。"""
    _current_agent(request)
    return [service.agent_dict(a)
            for a in service.list_agents(include_disabled=False)]


@router.get("/api/cs/agent/conversations")
async def agent_conversations(request: Request, status: str = "",
                              source: str = "", platform: str = "",
                              unread_only: bool = False):
    agent = _current_agent(request)
    if status and status not in {"queued", "active", "closed"}:
        status = ""
    return service.list_conversations(agent, status=status, source=source,
                                      platform=platform,
                                      unread_only=unread_only)


def _conv_visible_or_404(conv_id: int, agent) -> None:
    # service 内部在写操作里做可见性校验；读接口先做一次友好的 404
    conv = service.get_conversation(conv_id)
    if not conv:
        raise HTTPException(404, "会话不存在")
    if agent.role != "admin" and conv.status != "queued" \
            and conv.owner_agent_id != agent.id:
        # 被邀请的参与人也允许读
        from sqlmodel import select
        from .models import CsParticipant
        with service.get_session() as s:
            invited = s.exec(select(CsParticipant).where(
                CsParticipant.conv_id == conv_id,
                CsParticipant.agent_id == agent.id)).first()
        if not invited:
            raise HTTPException(403, "无权查看该会话")


@router.get("/api/cs/agent/conversations/{conv_id}")
async def agent_conversation(conv_id: int, request: Request):
    agent = _current_agent(request)
    _conv_visible_or_404(conv_id, agent)
    data = service.conversation_dict_safe(conv_id)
    if data is None:
        raise HTTPException(404, "会话不存在")
    return data


@router.get("/api/cs/agent/conversations/{conv_id}/messages")
async def agent_messages(conv_id: int, request: Request, after_id: int = 0):
    agent = _current_agent(request)
    _conv_visible_or_404(conv_id, agent)
    return service.list_messages(conv_id, after_id=after_id)


@router.post("/api/cs/agent/conversations/{conv_id}/accept")
async def agent_accept(conv_id: int, request: Request):
    agent = _current_agent(request)
    try:
        return service.accept_conversation(conv_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))


class TargetAgentIn(BaseModel):
    agent_id: int


@router.post("/api/cs/agent/conversations/{conv_id}/transfer")
async def agent_transfer(conv_id: int, body: TargetAgentIn, request: Request):
    agent = _current_agent(request)
    try:
        return service.transfer_conversation(conv_id, body.agent_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.post("/api/cs/agent/conversations/{conv_id}/invite")
async def agent_invite(conv_id: int, body: TargetAgentIn, request: Request):
    agent = _current_agent(request)
    try:
        return service.invite_agent(conv_id, body.agent_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.post("/api/cs/agent/conversations/{conv_id}/close")
async def agent_close(conv_id: int, request: Request):
    agent = _current_agent(request)
    try:
        return service.close_conversation(conv_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.post("/api/cs/agent/conversations/{conv_id}/release")
async def agent_release(conv_id: int, request: Request):
    agent = _current_agent(request)
    try:
        return service.release_conversation(conv_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.post("/api/cs/agent/conversations/{conv_id}/read")
async def agent_read(conv_id: int, request: Request):
    agent = _current_agent(request)
    try:
        service.mark_read(conv_id, agent)
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class AgentMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/api/cs/agent/conversations/{conv_id}/messages")
async def agent_send_message(conv_id: int, body: AgentMessageIn, request: Request):
    agent = _current_agent(request)
    try:
        msg = service.agent_send(conv_id, agent, body.text.strip())
    except service.CsError as e:
        raise HTTPException(400, str(e))
    payload = service.message_dict(msg)
    conv = service.get_conversation(conv_id)
    conv_snapshot = {
        "source": conv.source, "account_id": conv.account_id,
        "account_key": conv.account_key or "",
        "thread_key": conv.thread_key,
        "conv_id": conv_id, "message_id": msg.id,
    } if conv else {"source": SOURCE_GUEST}
    if conv_snapshot.get("source") != SOURCE_GUEST:
        task = asyncio.create_task(
            _do_relay(conv_id, msg.id, conv_snapshot, body.text.strip()))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    return {"message": payload, "relay": conv_snapshot["source"] != SOURCE_GUEST}


async def _do_relay(conv_id: int, message_id: int,
                    conv_snapshot: dict, text: str) -> None:
    """坐席回复落库后回投平台：本机有引擎则直接执行；纯客服节点则入跨节点队列，
    由持有平台登录态的工作台认领后把结果回报回来（结果 SSE 由 service 广播）。"""
    result = await relay_message(conv_snapshot, text)
    if result.get("state") == "queued":
        # 已排队：消息保持 relayed=False，工作台完成后会收到 relayed 事件
        bus.publish(bus.conv_room(conv_id),
                    {"type": "relay_queued", "conv_id": conv_id,
                     "message_id": message_id})
        bus.publish(bus.AGENTS_ROOM,
                    {"type": "relay_queued", "conv_id": conv_id,
                     "message_id": message_id})
        return
    ok, error = bool(result.get("ok")), str(result.get("error") or "")
    try:
        service.mark_relayed(message_id, ok)
        if not ok:
            service.add_system_message(conv_id, "⚠️ 回投平台失败：" + error)
    except Exception as e:
        print(f"[cs-relay] 状态写回失败: {e!r}")
    evt = {"type": "relayed", "conv_id": conv_id, "message_id": message_id,
           "ok": ok, "error": error}
    bus.publish(bus.conv_room(conv_id), evt)
    bus.publish(bus.AGENTS_ROOM, evt)


@router.post("/api/cs/agent/messages/{message_id}/resend")
async def agent_resend_message(message_id: int, request: Request):
    """坐席手动重发失败/超时的平台回投（同一幂等键复位重排，平台侧不重复）。"""
    _current_agent(request)
    try:
        return service.retry_relay_job(message_id)
    except service.CsError as e:
        raise HTTPException(400, str(e))


# ───────────────────────── 快捷回复（坐席个人模板） ─────────────────────────

class QuickReplyIn(BaseModel):
    title: str = Field(default="", max_length=50)
    text: str = Field(min_length=1, max_length=2000)


@router.get("/api/cs/agent/quick-replies")
async def agent_quick_replies(request: Request):
    agent = _current_agent(request)
    return service.list_quick_replies(agent.id)


@router.post("/api/cs/agent/quick-replies")
async def agent_create_quick_reply(body: QuickReplyIn, request: Request):
    agent = _current_agent(request)
    try:
        return service.create_quick_reply(agent.id, body.title, body.text)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.put("/api/cs/agent/quick-replies/{qr_id}")
async def agent_update_quick_reply(qr_id: str, body: QuickReplyIn, request: Request):
    agent = _current_agent(request)
    try:
        return service.update_quick_reply(agent.id, qr_id, body.title, body.text)
    except service.CsError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/cs/agent/quick-replies/{qr_id}")
async def agent_delete_quick_reply(qr_id: str, request: Request):
    agent = _current_agent(request)
    try:
        service.delete_quick_reply(agent.id, qr_id)
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.get("/api/cs/agent/stream")
async def agent_stream(request: Request):
    """坐席全局事件流：会话变化、新消息、回投结果。"""
    agent = _current_agent(request)

    async def gen():
        q = bus.subscribe(bus.AGENTS_ROOM)
        try:
            yield "retry: 3000\n" + _sse(
                {"type": "hello", "agent_id": agent.id,
                 "counts": service.counts_overview()})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=20)
                    yield _sse(evt)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(bus.AGENTS_ROOM, q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"})


class LinkIn(BaseModel):
    customer_name: str = Field(default="", max_length=50)


@router.post("/api/cs/agent/links")
async def agent_create_link(body: LinkIn, request: Request):
    _current_agent(request)
    conv = service.create_link(body.customer_name.strip())
    base = str(request.base_url).rstrip("/")
    return {"token": conv.token, "id": conv.id,
            "path": f"/chat/{conv.token}",
            "url": f"{base}/chat/{conv.token}"}


class TakeoverIn(BaseModel):
    source: str = Field(min_length=2, max_length=40)
    account_id: int
    account_key: str = Field(default="", max_length=200)
    thread_key: str = Field(min_length=1, max_length=200)
    customer_name: str = Field(default="", max_length=50)
    customer_avatar: str = Field(default="", max_length=500)
    first_message: str = Field(default="", max_length=4000)


@router.post("/api/cs/agent/takeover")
async def agent_takeover(body: TakeoverIn, request: Request):
    """平台私信/评论「转人工」：建立或取回一条客服会话。"""
    _current_agent(request)
    source = body.source.strip().lower()
    platform, _kind = parse_source(source)
    if source == SOURCE_GUEST or not platform:
        raise HTTPException(400, "来源格式不正确，应为 平台_dm/平台_comment")
    if platform not in CS_PLATFORMS:
        raise HTTPException(
            400, f"暂不支持的平台来源：{platform}（支持 douyin/xhs/tiktok）")
    try:
        conv = service.open_or_create_for_takeover(
            source=source, account_id=body.account_id,
            thread_key=body.thread_key.strip(),
            customer_name=body.customer_name.strip(),
            customer_avatar=body.customer_avatar.strip(),
            first_message=body.first_message.strip(),
            account_key=body.account_key.strip())
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return service.conversation_dict_safe(conv.id) or {"id": conv.id}


# ───────────────────────── 管理员：坐席账号管理 ─────────────────────────

class AgentCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=6, max_length=100)
    display_name: str = Field(default="", max_length=40)
    role: str = "agent"


@router.get("/api/cs/agent/admin/agents")
async def admin_list_agents(request: Request):
    _require_admin(request)
    return [service.agent_dict(a) | {"enabled": a.enabled,
                                    "created_at": service.agent_created_ts(a)}
            for a in service.list_agents(include_disabled=True)]


@router.post("/api/cs/agent/admin/agents")
async def admin_create_agent(body: AgentCreateIn, request: Request):
    _require_admin(request)
    try:
        agent = service.create_agent(
            body.username, body.password,
            body.display_name.strip(),
            "admin" if body.role == "admin" else "agent")
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return service.agent_dict(agent)


class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=100)


@router.put("/api/cs/agent/admin/agents/{agent_id}/password")
async def admin_reset_password(agent_id: int, body: PasswordIn, request: Request):
    _require_admin(request)
    try:
        service.set_agent_password(agent_id, body.password)
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class EnabledIn(BaseModel):
    enabled: bool


@router.put("/api/cs/agent/admin/agents/{agent_id}/enabled")
async def admin_set_enabled(agent_id: int, body: EnabledIn, request: Request):
    _require_admin(request)
    try:
        service.set_agent_enabled(agent_id, body.enabled)
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ───────────── 管理员：回投节点令牌 / 队列状态 ─────────────

class NodeTokenIn(BaseModel):
    token: str = Field(default="", max_length=200)


@router.get("/api/cs/agent/admin/node-token")
async def admin_get_node_token(request: Request):
    """查看当前节点令牌（环境变量配置时标明来源，不回显环境变量明文）。"""
    _require_admin(request)
    import os
    env_token = (os.environ.get(service.NODE_TOKEN_ENV) or "").strip()
    return {"token": service.get_node_token(),
            "source": "env" if env_token else ("setting" if service.get_node_token() else ""),
            "queue": service.relay_queue_overview()}


@router.put("/api/cs/agent/admin/node-token")
async def admin_set_node_token(body: NodeTokenIn, request: Request):
    """设置/清空节点令牌（环境变量已配置时，环境变量优先，界面设置不生效）。"""
    _require_admin(request)
    import os
    if (os.environ.get(service.NODE_TOKEN_ENV) or "").strip():
        raise HTTPException(400, "令牌由环境变量 MMM_CS_NODE_TOKEN 提供，界面不可修改")
    token = body.token.strip()
    if token and len(token) < 8:
        raise HTTPException(400, "节点令牌至少 8 个字符")
    service.set_node_token(token)
    return {"ok": True}


# ───────────── 回投节点（持有平台登录态的工作台） ─────────────

def _require_node(request: Request) -> str:
    """节点接口鉴权：X-CS-Node-Token 或 Bearer 令牌。"""
    token = request.headers.get("x-cs-node-token", "").strip()
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    expected = service.get_node_token()
    if not expected:
        raise HTTPException(503, "节点回投未启用：请先在坐席管理中配置节点令牌")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "节点令牌无效")
    return token


class NodeAccount(BaseModel):
    platform: str = Field(min_length=1, max_length=20)
    account_key: str = Field(default="", max_length=200)
    account_id: int = 0


class ClaimIn(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    accounts: list[NodeAccount] = []
    max_jobs: int = 5


@router.post("/api/cs/node/claim")
async def node_claim(body: ClaimIn, request: Request):
    """工作台拉取自己可执行的待回投任务（按平台账号稳定标识匹配）。"""
    _require_node(request)
    try:
        jobs = service.claim_relay_jobs(
            body.node_id, [a.model_dump() for a in body.accounts],
            max_jobs=body.max_jobs)
    except service.CsError as e:
        raise HTTPException(400, str(e))
    return {"jobs": jobs}


@router.get("/api/cs/node/status")
async def node_status(request: Request):
    """节点配置自检：令牌有效即返回队列概况（供工作台显示连接状态）。"""
    _require_node(request)
    return {"ok": True, "queue": service.relay_queue_overview(),
            "server_time": int(time.time())}


class JobResultIn(BaseModel):
    node_id: str = Field(min_length=1, max_length=64)
    ok: bool
    error: str = Field(default="", max_length=500)
    # 工作台对故障是否可重试的显式判断；缺省时服务端按错误文案分类
    retryable: Optional[bool] = None


@router.post("/api/cs/node/jobs/{job_id}/result")
async def node_job_result(job_id: int, body: JobResultIn, request: Request):
    """工作台回报单条回投结果。"""
    _require_node(request)
    try:
        return service.complete_relay_job(
            job_id, body.node_id, body.ok, body.error.strip(),
            retryable=body.retryable)
    except service.CsError as e:
        raise HTTPException(400, str(e))
