"""TikTok 国际版交互式网页登录(浏览器优先,不做私有 API 逆向)。

打开 www.tiktok.com/login 真实窗口,用户可用 TikTok App 扫码或邮箱/第三方
账号登录;轮询到强登录 Cookie 且离开登录页后,从页面全局状态读取本人资料,
登录态同时落盘持久 profile 与库内 storage_state。
"""
from __future__ import annotations

import asyncio
import json
from typing import Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ...browser.login import _focus, _open_first_environment_check
from .webstate import (
    TT_HOME_URL,
    TT_LOGIN_URL,
    parse_tiktok_self_user,
    read_logged_in_user,
    tt_login_ready,
)


async def _goto_home_and_read(page) -> dict:
    """登录落地后回首页读取本人资料(登录页本身不带 userInfo)。"""
    try:
        user = await read_logged_in_user(page)
        if user:
            return user
        await page.goto(TT_HOME_URL, wait_until="domcontentloaded",
                        timeout=30000)
        await page.wait_for_timeout(1500)
        return await read_logged_in_user(page)
    except Exception:
        return {}


async def interactive_tiktok_login(
        mgr: BrowserManager, identity: Identity,
        timeout_seconds: int = 300,
        start_url: str = TT_LOGIN_URL,
        force_reauth: bool = False) -> Tuple[bool, str, str]:
    """返回 (是否成功, storage_state_json, nickname)。

    判定登录:出现 sessionid/sessionid_ss/sid_guard 且页面离开 /login。
    用户中途关窗视为未登录,不抛错。
    """
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        # 重登必须清掉可能已被服务端撤销的旧会话,否则轮询会误判成功。
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""

    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        waited = 0.0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            url = ""
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            if tt_login_ready({c["name"] for c in cookies}, url):
                await page.wait_for_timeout(1200)
                user = await _goto_home_and_read(page)
                if user and user.get("uniqueId"):
                    logged = True
                    parsed = parse_tiktok_self_user(user)
                    nickname = parsed.get("nickname") or ""
                    # 与小红书登录一致:观察到的资料挂在 identity 上,由主流程落库
                    identity.observed_login_profile = parsed
                    break
            await asyncio.sleep(0.5)
            waited += 0.5

        if logged:
            await page.wait_for_timeout(800)  # 给 localStorage 一次落盘机会
            state = await ctx.storage_state()
            state_json = json.dumps(state)
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    return logged, state_json, nickname
