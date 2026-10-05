"""TikTok 网页私信收件箱(浏览器优先)。

会话列表:打开 ``https://www.tiktok.com/messages``,拦截含 conversation/
chat 字段的 JSON 响应,启发式抽取会话。

发送:打开目标主页 → 点「Message」→ 输入 → 发送。不拦截发送回包,
仅做 DOM 确认(输入框清空 / 消息出现),无法确认时报 ``write_uncertain``。

能力边界:网页收件箱不支持向陌生人发送首条私信(平台限制),该路径
在 UI 降级标注。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

TT_MESSAGES_URL = "https://www.tiktok.com/messages"
_TT_PROFILE_URL = "https://www.tiktok.com/@{uid}"
_TT_PROFILE_SEC_URL = "https://www.tiktok.com/user/{sec}"

# 私信入口按钮(主页上)
_DM_ENTRY_SELECTORS = [
    'button[data-e2e="message-button"]',
    'a[href*="/messages"]',
    'button:has-text("Message")',
    'div[role="button"]:has-text("Message")',
]

# 私信输入框
_DM_INPUT_SELECTORS = [
    'div[contenteditable="true"]',
    'textarea[placeholder*="Message"]',
    'textarea[placeholder*="message"]',
    'input[placeholder*="Message"]',
    'input[placeholder*="message"]',
]

# 发送按钮
_DM_SEND_SELECTORS = [
    'button[data-e2e="send-message-button"]',
    'button:has-text("Send")',
    'button:has-text("Post")',
]


def _norm_conversation(raw: dict) -> Optional[dict]:
    """启发式从 JSON 中抽取会话字段。返回 None 表示不像会话。"""
    if not isinstance(raw, dict):
        return None
    # 常见字段名
    conv_id = raw.get("conversation_id") or raw.get("conversationId") \
        or raw.get("conv_id") or raw.get("id")
    if not conv_id:
        return None
    peer = raw.get("peer_user") or raw.get("peerUser") or raw.get("user") \
        or raw.get("from_user") or raw.get("fromUser") or {}
    if not isinstance(peer, dict):
        peer = {}
    last_msg = raw.get("last_message") or raw.get("lastMessage") \
        or raw.get("last_msg") or {}
    if not isinstance(last_msg, dict):
        last_msg = {}
    return {
        "conv_id": str(conv_id),
        "conv_short_id": str(raw.get("conversation_short_id")
                           or raw.get("conversationShortId") or ""),
        "ticket": str(raw.get("ticket") or ""),
        "peer_uid": str(peer.get("uid") or peer.get("id") or ""),
        "peer_sec_uid": str(peer.get("sec_uid") or peer.get("secUid") or ""),
        "peer_nickname": str(peer.get("nickname") or peer.get("uniqueId") or ""),
        "peer_avatar": str(peer.get("avatar_thumb") or peer.get("avatarThumb")
                          or peer.get("avatar") or ""),
        "last_text": str(last_msg.get("text") or last_msg.get("content") or ""),
        "last_time": int(last_msg.get("create_time") or last_msg.get("createTime")
                         or last_msg.get("timestamp") or 0),
        "unread_count": int(raw.get("unread_count") or raw.get("unreadCount") or 0),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


def _walk_conversations(data, out: Dict[str, dict]) -> None:
    """递归遍历 JSON,收集看起来像会话的 dict。"""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                conv = _norm_conversation(item)
                if conv:
                    out[conv["conv_id"]] = conv
                else:
                    _walk_conversations(item, out)
            elif isinstance(item, list):
                _walk_conversations(item, out)
    elif isinstance(data, dict):
        for v in data.values():
            _walk_conversations(v, out)


async def fetch_tiktok_dm_conversations(
        mgr: BrowserManager, identity: Identity,
        settle_ms: int = 2600, max_scrolls: int = 8) -> Tuple[List[dict], str]:
    """打开私信页,拦截 JSON 响应,启发式抽取会话列表。

    返回 ``(会话列表, error)``。``logged_out:`` 前缀表示登录态失效。
    """
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=False)

    async def on_response(resp):
        try:
            url = resp.url.lower()
            if "tiktok.com" not in url:
                return
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            # 会话列表接口关键词
            if not any(k in url for k in (
                    "conversation", "chat", "message", "im", "inbox")):
                return
            try:
                data = await resp.json()
            except Exception:
                return
            _walk_conversations(data, collected)
        except Exception:
            pass

    page.on("response", lambda resp: asyncio.create_task(on_response(resp)))

    try:
        await page.goto(TT_MESSAGES_URL, wait_until="domcontentloaded",
                        timeout=30000)
        lowered = str(page.url or "").lower()
        if "/login" in lowered or "passport" in lowered:
            return [], "logged_out:登录态失效,请重新登录"
        await page.wait_for_timeout(settle_ms)
        # 滚动加载更多会话
        for _ in range(max_scrolls):
            await page.evaluate(
                "() => window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(800)
        return list(collected.values()), ""
    except Exception as e:
        return list(collected.values()), f"fetch_error:{type(e).__name__}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def send_tiktok_dm(
        mgr: BrowserManager, identity: Identity,
        target_uid: str = "", target_sec_uid: str = "", text: str = "") \
        -> Tuple[bool, str]:
    """给目标发私信:打开主页 → 点「Message」→ 输入 → 发送。

    返回三态:``(True, "")`` 成功;``(False, "write_uncertain:...")``
    已提交但无法 DOM 确认;``(False, <原因>)`` 业务硬失败。
    """
    text = (text or "").strip()
    if not text:
        return False, "空内容"
    uid = (target_uid or "").strip()
    sec = (target_sec_uid or "").strip()
    if uid:
        url = _TT_PROFILE_URL.format(uid=uid)
    elif sec:
        url = _TT_PROFILE_SEC_URL.format(sec=sec)
    else:
        return False, "missing_target:缺目标用户标识"

    ctx = None
    page = None
    try:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        lowered = str(page.url or "").lower()
        if "/login" in lowered or "passport" in lowered:
            return False, "logged_out:账号未登录"

        # 点「Message」入口
        opened = False
        for sel in _DM_ENTRY_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=2000):
                    await loc.click(timeout=4000)
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            return False, "未找到私信入口(对方可能未开放私信)"

        await page.wait_for_timeout(1500)

        # 找输入框
        editor = None
        for sel in _DM_INPUT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=2000):
                    editor = loc
                    break
            except Exception:
                continue
        if editor is None:
            return False, "未找到私信输入框"

        # 输入
        await editor.click(timeout=2000)
        await editor.fill(text)
        await page.wait_for_timeout(500)

        # 点发送
        sent = False
        for sel in _DM_SEND_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=2000):
                    await loc.click(timeout=4000)
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            # 兜底:回车发送
            await page.keyboard.press("Enter")
            sent = True

        await page.wait_for_timeout(1200)

        # DOM 确认:输入框应被清空
        try:
            value = await editor.input_value()
            if value.strip():
                return False, "write_uncertain:发送后输入框未清空,结果未确认"
        except (AttributeError, TypeError):
            # contenteditable 没有 input_value,改用 inner_text
            try:
                inner = await editor.inner_text()
                if inner.strip():
                    return False, "write_uncertain:发送后输入框未清空,结果未确认"
            except Exception:
                pass

        return True, ""
    except Exception as e:
        return False, f"send_dm异常: {e!r}"
    finally:
        try:
            if page is not None:
                await page.close()
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
