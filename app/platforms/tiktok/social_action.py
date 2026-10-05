"""TikTok 关注/取关浏览器自动化。

打开目标主页 ``https://www.tiktok.com/@<uniqueId>``,读取「Follow/Following」
按钮文案 → 点击 → 轮询文案翻转确认生效(与抖音 do_follow 同构,选择器/文案
因平台而异)。

三态约定:
- ``(True, "")`` 成功(含已是目标态的幂等成功);
- ``(False, "logged_out:...")`` 登录态失效;
- ``(False, <原因>)`` 业务硬失败,不重试。
"""
from __future__ import annotations

from typing import Any, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

# ── 选择器 ──────────────────────────────────────────────────────────────
_TT_FOLLOW_ANCHORS = [
    'button[data-e2e="follow-button"]', 'button[data-e2e="follow-btn"]',
    'div[data-e2e="follow-button"]', 'div[data-e2e="follow-btn"]',
]
_TT_FOLLOW_FALLBACK = [
    'button:has-text("Follow")', 'button:has-text("Following")',
    'div[role="button"]:has-text("Follow")',
    'div[role="button"]:has-text("Following")',
]

# 文案 → 是否已关注(英/日/西等多语言首版只认英文,后续可扩展)
_FOLLOWING_TEXTS = ("following", "message")   # Following 态下按钮常变成 Message
_FOLLOW_TEXTS = ("follow",)

_TT_PROFILE_URL = "https://www.tiktok.com/@{uid}"
_TT_PROFILE_SEC_URL = "https://www.tiktok.com/user/{sec}"


# ── 辅助 ────────────────────────────────────────────────────────────────

def _is_following(text: str) -> bool:
    t = text.strip().lower()
    return any(t == ft.lower() for ft in _FOLLOWING_TEXTS) or t == "following"


async def _first_visible(page, sel: str, limit: int = 8):
    loc = page.locator(sel)
    for i in range(min(await loc.count(), limit)):
        cand = loc.nth(i)
        try:
            if await cand.is_visible():
                return cand
        except Exception:
            continue
    return None


async def _follow_button(page) -> Tuple[Any, str]:
    """返回(可见的关注/消息按钮 locator, 按钮文案);找不到则 (None, "")。"""
    for sel in _TT_FOLLOW_ANCHORS:
        btn = await _first_visible(page, sel)
        if btn is not None:
            try:
                return btn, (await btn.inner_text()).strip()
            except Exception:
                return btn, ""
    for sel in _TT_FOLLOW_FALLBACK:
        btn = await _first_visible(page, sel)
        if btn is not None:
            try:
                return btn, (await btn.inner_text()).strip()
            except Exception:
                return btn, ""
    return None, ""


async def _await_follow_button(page, timeout_ms: int = 12000) -> Tuple[Any, str]:
    waited = 0
    while True:
        btn, text = await _follow_button(page)
        if btn is not None and text:
            return btn, text
        if waited >= timeout_ms:
            return btn, text
        await page.wait_for_timeout(500)
        waited += 500


async def _wait_flip(page, want_following: bool, timeout_ms: int = 6000) -> str:
    waited, text = 0, ""
    while waited < timeout_ms:
        await page.wait_for_timeout(400)
        waited += 400
        _, text = await _follow_button(page)
        if text and _is_following(text) == want_following:
            return text
    return text


# ── 主入口 ──────────────────────────────────────────────────────────────

async def follow_tiktok_browser(
        mgr: BrowserManager, identity: Identity,
        target_uid: str = "", target_sec_uid: str = "",
        unfollow: bool = False) -> Tuple[bool, str]:
    """TikTok 关注/取关:UI 自动化,有头窗口更稳。

    ``target_uid`` 优先当作 uniqueId 用于 ``/@uid`` 主页 URL;
    若为空则退而使用 ``target_sec_uid`` 的 ``/user/{sec}`` 格式。
    """
    want = "unfollow" if unfollow else "follow"
    want_following = not unfollow
    uid = (target_uid or "").strip()
    sec = (target_sec_uid or "").strip()
    if uid:
        url = _TT_PROFILE_URL.format(uid=uid)
    elif sec:
        url = _TT_PROFILE_SEC_URL.format(sec=sec)
    else:
        return False, "missing_target:缺目标用户标识(uniqueId 或 sec_uid)"

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

        btn, text = await _await_follow_button(page)
        if btn is None or not text:
            return False, f"未找到关注按钮(主页未渲染/改版?url={page.url})"
        if _is_following(text) == want_following:
            return True, ""

        after = text
        for attempt in range(3):
            try:
                await btn.click(timeout=4000)
            except Exception as e:
                if attempt == 2:
                    return False, f"{want}点击失败: {e!r}"
                await page.wait_for_timeout(800)
                continue
            after = await _wait_flip(page, want_following)
            if after and _is_following(after) == want_following:
                return True, ""
            btn, cur = await _follow_button(page)
            if btn is None:
                return False, f"{want}后按钮消失(点前「{text}」)"
            after = cur
        return False, f"{want}未生效:点了 3 次,按钮仍是「{after}」(风控?)"
    except Exception as e:
        return False, f"{want}异常: {e!r}"
    finally:
        try:
            if page is not None:
                await page.close()
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
