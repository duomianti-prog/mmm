"""TikTok 国际版网页登录态与本人资料解析。

TikTok 网页(www.tiktok.com)把首屏数据放在
``window.__UNIVERSAL_DATA_FOR_REHYDRATION__`` 里,本人信息位于
``__DEFAULT_SCOPE__["web.app.app"].userInfo.user``;未登录时该对象为空。
登录强信号 Cookie 为 sessionid/sessionid_ss/sid_guard(ttwid、msToken
在游客态也存在,不能作为登录判据)。

选择器/Cookie 名随 tiktok.com 改版可能变化,集中在本模块便于维护。
"""
from __future__ import annotations

from typing import Any

# 登录后才会写入的强信号 Cookie。ttwid/msToken/odin_tt 游客态也有,不能用。
TT_LOGIN_COOKIES = frozenset({"sessionid", "sessionid_ss", "sid_guard"})

TT_HOME_URL = "https://www.tiktok.com/"
TT_LOGIN_URL = "https://www.tiktok.com/login"

# 在页面内读取本人资料;只读全局状态,不触发任何额外签名接口。
_TT_SELF_USER_JS = """
() => {
    try {
        const raw = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
        if (!raw) return null;
        const data = (typeof raw === 'string') ? JSON.parse(raw) : raw;
        const scope = (data && data['__DEFAULT_SCOPE__']) || {};
        const app = scope['web.app.app'] || {};
        const user = ((app.userInfo || {}).user) || null;
        if (user && user.uniqueId) return user;
        return null;
    } catch (e) {
        return null;
    }
}
"""


def tt_login_ready(cookie_names, current_url: str = "") -> bool:
    """强 Cookie 已写入且页面已离开登录/护照域,才判定登录完成。"""
    names = set(cookie_names or ())
    if not (names & TT_LOGIN_COOKIES):
        return False
    url = str(current_url or "").lower()
    if "/login" in url or "passport" in url:
        return False
    return True


def extract_self_user(payload: Any) -> dict:
    """从 __UNIVERSAL_DATA_FOR_REHYDRATION__ 结构中取本人 user 原始对象。"""
    if not isinstance(payload, dict):
        return {}
    scope = payload.get("__DEFAULT_SCOPE__")
    if not isinstance(scope, dict):
        return {}
    app = scope.get("web.app.app")
    if not isinstance(app, dict):
        return {}
    info = app.get("userInfo")
    if not isinstance(info, dict):
        return {}
    user = info.get("user")
    return user if isinstance(user, dict) and user.get("uniqueId") else {}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_tiktok_self_user(raw: dict) -> dict:
    """归一为引擎通用账号资料字段(与 parse_ks_self_user 等对齐)。

    sec_uid 存 TikTok secUid(后续主页抓取用),douyin_id 复用为展示用
    TikTok 账号(@uniqueId),tiktok 数字 id 暂并入 sec_uid 之外不另设列。
    """
    if not isinstance(raw, dict):
        return {}
    nickname = str(raw.get("nickname") or "").strip()
    unique_id = str(raw.get("uniqueId") or "").strip()
    sec_uid = str(raw.get("secUid") or raw.get("sec_uid") or "").strip()
    if not (nickname or unique_id or sec_uid):
        return {}
    avatar = str(raw.get("avatarLarger") or raw.get("avatarMedium")
                  or raw.get("avatar") or "").strip()
    return {
        "nickname": nickname or unique_id,
        "sec_uid": sec_uid,
        "douyin_id": unique_id,
        "avatar": avatar,
        "follower_count": _int(raw.get("followerCount")),
        "following_count": _int(raw.get("followingCount")),
        "aweme_count": _int(raw.get("videoCount")),
    }


async def read_logged_in_user(page) -> dict:
    """在已落地页面上读取本人 user;读不到返回 {}(调用方据此判失效)。"""
    try:
        user = await page.evaluate(_TT_SELF_USER_JS)
        return user if isinstance(user, dict) else {}
    except Exception:
        return {}
