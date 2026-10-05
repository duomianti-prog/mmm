"""TikTok 账号登录态体检:浏览器内打开 tiktok.com 读取本人资料。"""
from __future__ import annotations

from typing import Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .webstate import (
    TT_HOME_URL,
    TT_LOGIN_COOKIES,
    read_logged_in_user,
)


async def fetch_tiktok_self_profile(
        mgr: BrowserManager, identity: Identity,
        timeout_ms: int = 20000, block_media: bool = False
) -> Tuple[dict, str]:
    """拿登录账号资料并判活。返回 (tiktok user 原始对象, error)。

    error == "logged_out" 表示未登录或会话已失效(无强 Cookie 或
    首页全局状态中没有本人 userInfo);其它 error 为网络/环境异常,
    上层不应据此判账号失效。
    """
    try:
        page = await mgr.new_page(identity, block_media)
    except Exception as exc:
        return {}, f"open_page:{type(exc).__name__}"

    error = ""
    final_url = ""
    try:
        try:
            cookies = await page.context.cookies()
            names = {c.get("name") for c in cookies}
        except Exception:
            names = set()
        if not (names & TT_LOGIN_COOKIES):
            return {}, "logged_out"

        try:
            await page.goto(TT_HOME_URL,
                            wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(2500)
            final_url = str(page.url or "")
        except Exception as exc:
            return {}, f"goto:{type(exc).__name__}:{exc}"

        user = await read_logged_in_user(page)
        if user:
            return user, ""
        # 强 Cookie 还在但首页拿不到本人:会话实际已失效(或被登出重定向)。
        if "/login" in final_url.lower() or "passport" in final_url.lower():
            error = "logged_out"
        else:
            print(f"[tiktok_self_profile] no userInfo; final_url={final_url}")
            error = "logged_out"
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return {}, error
