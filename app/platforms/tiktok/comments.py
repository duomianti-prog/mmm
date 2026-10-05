"""TikTok 评论抓取与解析(浏览器拦截 /api/comment/list)。

打开作品页 ``https://www.tiktok.com/@{handle}/video/<aweme_id>``,滚动评论
容器触发翻页,拦截 ``/api/comment/list/`` 响应收集评论原始 JSON。

只抓顶级评论(与抖音默认行为一致);子评论(reply)接口 /api/comment/list/reply/
暂不抓取,如需可在后续扩展。
"""
from __future__ import annotations

from typing import List, Optional, Set, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

TT_COMMENT_API = "/api/comment/list/"

# 滚动评论区:优先找可滚动容器,否则滚页面。
_TT_SCROLL_COMMENTS_JS = """
() => {
  let best = null, bh = 0;
  document.querySelectorAll('div,section,main').forEach(el => {
    const s = getComputedStyle(el);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
        && el.scrollHeight > el.clientHeight + 40 && el.scrollHeight > bh) {
      best = el; bh = el.scrollHeight;
    }
  });
  if (best) { best.scrollTop = best.scrollHeight; }
  window.scrollTo(0, document.body.scrollHeight);
}
"""


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_tiktok_comment(raw: dict) -> Optional[dict]:
    """TikTok 评论 JSON → CommentRecord 规范化 dict(与抖音 parse_comment 同形)。"""
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("cid") or "").strip()
    if not cid:
        return None
    user = raw.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    reply_id = str(_first(raw, "reply_id", "reply_to_comment_id", default="") or "")
    if reply_id in ("0", "null", "None"):
        reply_id = ""
    return {
        "comment_id": cid,
        "text": str(raw.get("text") or "").strip(),
        "user_nickname": str(user.get("nickname") or ""),
        "user_sec_uid": str(_first(user, "secUid", "sec_uid", default="") or ""),
        "like_count": int(_first(raw, "digg_count", "diggCount", "like_count",
                                  default=0) or 0),
        "create_time": int(_first(raw, "create_time", "createTime", default=0) or 0),
        "reply_to": reply_id,
    }


def _looks_like_comment_list(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    items = payload.get("comments")
    return isinstance(items, list)


async def fetch_tiktok_comments(
        mgr: BrowserManager,
        identity: Identity,
        aweme_id: str,
        known_cids: Set[str],
        *,
        handle: str = "",
        max_scrolls: int = 6,
        settle_ms: int = 1600,
        block_media: bool = True,
) -> Tuple[List[dict], str]:
    """打开 TikTok 作品页,滚动评论容器,拦截评论列表接口。

    返回 ``(新评论原始 JSON 列表, error)``。error 为空表示成功(含零评论)。
    """
    aweme_id = str(aweme_id or "").strip()
    if not aweme_id:
        return [], "missing_aweme_id"
    handle = (handle or "").strip().lstrip("@")
    collected: dict[str, dict] = {}
    error = ""

    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        if TT_COMMENT_API not in resp.url:
            return
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not _looks_like_comment_list(payload):
            return
        for c in (payload.get("comments") or []):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("cid") or "")
            if cid:
                collected[cid] = c

    page.on("response", on_response)
    try:
        if handle:
            url = f"https://www.tiktok.com/@{handle}/video/{aweme_id}"
        else:
            url = f"https://www.tiktok.com/video/{aweme_id}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.evaluate(_TT_SCROLL_COMMENTS_JS)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
    except Exception as e:
        error = f"打开作品页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error:
        error = "未拦截到评论(可能未登录/评论区未加载/作品无评论)"
    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error
