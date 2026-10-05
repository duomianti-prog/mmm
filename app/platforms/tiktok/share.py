"""TikTok 分享链接解析与作品读取(浏览器优先)。

支持的链接形态:
* 长链 ``https://www.tiktok.com/@user/video/<id>``、``/photo/<id>``;
* 移动页 ``https://m.tiktok.com/v/<id>.html``;
* 短链 ``https://vm.tiktok.com/xxx``、``https://vt.tiktok.com/xxx``
  (短链跳转交给浏览器导航完成,不做私有 API 逆向)。

作品详情从页面全局状态 ``__UNIVERSAL_DATA_FOR_REHYDRATION__``(及旧版
``SIGI_STATE``)读取,再归一化为共享的 :class:`Aweme` 结构,媒体落盘复用
通用 Downloader。
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ..douyin.extract import Aweme, MediaItem

TT_WEB_URL = "https://www.tiktok.com/"

# 短链域名也以 tiktok.com 结尾,share_downloader 的后缀匹配已能识别;
# 这里保留显式集合,便于调用方判断"必须经过一次跳转"。
TT_SHORT_HOSTS = frozenset({"vm.tiktok.com", "vt.tiktok.com"})

_ITEM_PATH_RE = re.compile(
    r"^/@(?P<user>[^/?#]+)/(?P<kind>video|photo)/(?P<id>\d+)(?:[/?#]|$)"
)
_MOBILE_PATH_RE = re.compile(
    r"^/(?:v|embed)/(?P<id>\d+)(?:\.html)?(?:[/?#]|$)"
)

# 页面端读取作品详情:优先新版注水数据,回退旧版 SIGI_STATE。
_TT_SHARE_ITEM_JS = r"""
() => {
  try {
    const root = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
    const scope = root && root.__DEFAULT_SCOPE__;
    if (scope) {
      for (const key of Object.keys(scope)) {
        const value = scope[key];
        if (!value) continue;
        const item = (value.itemInfo && value.itemInfo.itemStruct) || value.itemStruct;
        if (item && (item.video || item.imagePost)) return item;
      }
    }
  } catch (e) {}
  try {
    const sigi = window.SIGI_STATE;
    const mods = sigi && sigi.ItemModule;
    if (mods) {
      const firstKey = Object.keys(mods)[0];
      if (firstKey && mods[firstKey]) return mods[firstKey];
    }
  } catch (e) {}
  return null;
}
"""


def is_tiktok_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def is_tiktok_short_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in TT_SHORT_HOSTS


def parse_tiktok_item_url(url: str) -> Optional[Tuple[str, str]]:
    """从长链/移动页提取 ``(kind, item_id)``;短链等需跳转形态返回 None。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return None
    path = parts.path or "/"
    match = _ITEM_PATH_RE.match(path)
    if match:
        return match.group("kind"), match.group("id")
    if host in {"m.tiktok.com", "www.tiktok.com", "tiktok.com"}:
        match = _MOBILE_PATH_RE.match(path)
        if match:
            return "video", match.group("id")
    return None


def _first_url(value) -> str:
    """TikTok Web 字段里同一地址可能是字符串或 urlList 数组。"""
    if isinstance(value, str):
        return value if value.startswith("http") else ""
    if isinstance(value, (list, tuple)):
        return next((str(u) for u in value if isinstance(u, str)
                     and u.startswith("http")), "")
    return ""


def _best_url(value) -> str:
    """媒体 urlList 通常按清晰度升序排列,取最后一个 http 地址。"""
    if isinstance(value, str):
        return value if value.startswith("http") else ""
    if isinstance(value, (list, tuple)):
        urls = [str(u) for u in value
                if isinstance(u, str) and u.startswith("http")]
        return urls[-1] if urls else ""
    return ""


def _image_ext(url: str) -> str:
    suffix = (urlsplit(url).path or "").lower().rsplit(".", 1)[-1]
    return suffix if suffix in {"jpg", "jpeg", "webp", "png", "avif"} else "jpeg"


def _count(stats: dict, *keys: str) -> int:
    for key in keys:
        value = stats.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _video_candidates(item: dict) -> list[tuple[str, int, int]]:
    """返回 ``(url, 高度, 码率)`` 候选,字段命名兼容 Web camelCase。"""
    video = item.get("video") or {}
    candidates: list[tuple[str, int, int]] = []
    for entry in video.get("bitRateInfo") or video.get("bit_rate") or []:
        play = entry.get("PlayAddr") or entry.get("play_addr") or {}
        url = _best_url(play.get("UrlList") or play.get("url_list"))
        if not url:
            continue
        size = entry.get("Size") or {}
        height = int(
            play.get("Height") or size.get("height")
            or play.get("height") or 0
        )
        bitrate = int(entry.get("Bitrate") or entry.get("bit_rate") or 0)
        candidates.append((url, height, bitrate))
    return candidates


def _fallback_video_url(item: dict) -> str:
    video = item.get("video") or {}
    for key in ("playAddr", "play_addr", "downloadAddr", "download_addr"):
        url = _first_url(video.get(key))
        if url:
            return url
    play = video.get("playAddr") or video.get("play_addr")
    if isinstance(play, dict):
        return _first_url(play.get("UrlList") or play.get("url_list"))
    return ""


def select_tiktok_video_url(item: dict, quality: str = "highest"
                            ) -> Tuple[str, str]:
    """按画质偏好挑选播放地址,返回 ``(url, 画质标签)``。"""
    candidates = _video_candidates(item)
    if candidates:
        candidates.sort(key=lambda c: (c[1], c[2]))
        if quality in {"lowest", "worst", "省流"}:
            url, height, _bitrate = candidates[0]
            return url, f"{height}p" if height else "lowest"
        if quality not in {"highest", "best", "原画", "", None}:
            try:
                target = int(str(quality).rstrip("pP"))
            except ValueError:
                target = 0
            within = [c for c in candidates if c[1] and c[1] <= target]
            chosen = within[0] if within else candidates[-1]
            return chosen[0], f"{chosen[1]}p" if chosen[1] else "highest"
        url, height, _bitrate = candidates[-1]
        return url, f"{height}p" if height else "highest"
    return _fallback_video_url(item), ""


def parse_tiktok_item(item: dict, quality: str = "highest"
                      ) -> Optional[Aweme]:
    """把 Web itemStruct 归一化为共享 Aweme;缺 id 或缺媒体时返回 None。"""
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id") or "").strip()
    if not item_id or not item_id.isdigit():
        return None
    author = item.get("author") or {}
    aweme = Aweme(
        aweme_id=item_id,
        desc=str(item.get("desc") or "").strip(),
        create_time=int(item.get("createTime") or item.get("create_time") or 0),
        author_name=str(
            author.get("nickname") or author.get("uniqueId") or ""),
        media_type="video",
        platform="tiktok",
    )
    avatar = _first_url(
        author.get("avatarLarger") or author.get("avatarMedium")
        or author.get("avatarThumb"))
    if avatar:
        aweme.avatar = avatar

    stats = item.get("statsV2") or item.get("stats") or {}
    aweme.like_count = _count(stats, "diggCount", "digg_count")
    aweme.comment_count = _count(stats, "commentCount", "comment_count")

    video = item.get("video") or {}
    duration = int(video.get("duration") or 0)
    # Web 详情里 duration 单位为秒;异常过大值按毫秒兜底换算。
    aweme.duration = int(duration / 1000) if duration > 100000 else duration

    image_post = item.get("imagePost") or item.get("image_post") or {}
    images = image_post.get("images") or []
    if images:
        aweme.media_type = "images"
        for index, image in enumerate(images):
            image_url = image.get("imageURL") or image.get("image_url") or {}
            url = _best_url(image_url.get("urlList")
                            or image_url.get("url_list"))
            if url:
                aweme.medias.append(MediaItem(
                    url=url, kind="image", ext=_image_ext(url), index=index))
        cover = (image_post.get("cover") or {})
        cover_url = (_best_url(
            (cover.get("imageURL") or {}).get("urlList"))
            if cover else "")
        if cover_url:
            aweme.cover = cover_url
    else:
        url, label = select_tiktok_video_url(item, quality)
        if url:
            aweme.medias.append(MediaItem(
                url=url, kind="video", ext="mp4", index=0))
            aweme.quality_label = label
        cover = (video.get("cover") or video.get("originCover")
                 or video.get("dynamicCover") or "")
        aweme.cover = _first_url(cover) if not isinstance(cover, str) else (
            cover if cover.startswith("http") else "")

    if not aweme.medias:
        return None
    return aweme


async def fetch_tiktok_share_item(
        mgr: BrowserManager,
        identity: Identity,
        source_url: str,
        *,
        timeout_ms: int = 30000,
        block_media: bool = True,
        poll_interval_ms: int = 500,
) -> Tuple[dict, str, str]:
    """在账号浏览器中打开分享链接并读取 itemStruct。

    返回 ``(item, final_url, error)``。error 取值:
    ``login_required``(跳到登录页)、``timeout``(页面始终没有作品数据)、
    ``goto:<ExcName>``(导航失败,上层不做进一步推断)。
    """
    try:
        page = await mgr.new_page(identity, block_media)
    except Exception as exc:
        return {}, source_url, f"open_page:{type(exc).__name__}"

    error = ""
    item: dict = {}
    final_url = source_url
    try:
        try:
            await page.goto(source_url, wait_until="domcontentloaded",
                            timeout=timeout_ms)
        except Exception as exc:
            return {}, source_url, f"goto:{type(exc).__name__}:{exc}"

        deadline_loops = max(1, int(timeout_ms / max(50, poll_interval_ms)))
        for _ in range(deadline_loops):
            try:
                current = str(page.url or "")
            except Exception:
                current = ""
            lowered = current.lower()
            if "/login" in lowered or "passport" in lowered:
                return {}, current, "login_required"
            try:
                raw = await page.evaluate(_TT_SHARE_ITEM_JS)
            except Exception:
                raw = None
            if isinstance(raw, dict) and raw:
                return raw, current or source_url, ""
            try:
                await page.wait_for_timeout(poll_interval_ms)
            except Exception:
                await asyncio.sleep(max(50, poll_interval_ms) / 1000.0)
        try:
            final_url = str(page.url or source_url)
        except Exception:
            pass
        error = "timeout"
    finally:
        try:
            await page.close()
        except Exception:
            pass
    return item, final_url, error
