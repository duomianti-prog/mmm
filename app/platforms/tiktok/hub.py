"""TikTok 本账号作品分页同步(浏览器优先)。

在登录态浏览器中打开本人主页 ``https://www.tiktok.com/@<uniqueId>``,
拦截分页接口 ``/api/post/item_list``(游标/hasMore 由 TikTok 自己给出),
滚动加载直到无更多、连续无新增、达到滚动上限或被协作式取消。

首屏作品也可能直接注水在 ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` 的
``webapp.user-detail`` scope,导航后额外读取一次,避免漏掉首屏。
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable, Optional, Set, Tuple
from urllib.parse import urlsplit

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

TT_PROFILE_URL = "https://www.tiktok.com/@{handle}"
TT_ITEM_LIST_HINT = "/api/post/item_list"

# TikTok 号:字母/数字/点/下划线,2~24 字符(官方规则)。
_TT_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._]{2,24}$")
_TT_PROFILE_PATH_RE = re.compile(
    r"^/@(?P<handle>[A-Za-z0-9._]{2,24})(?P<rest>/.*)?$")


def parse_tiktok_handle(text: str) -> str:
    """从主页链接/分享文案/@handle/裸 handle 提取 uniqueId。

    作品链接(video/photo)不算主页目标,返回空串;无法识别也返回空串。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 直接是 @handle / handle
    if not raw.startswith("http"):
        candidate = raw.lstrip("@").strip()
        return candidate if _TT_HANDLE_RE.match("@" + candidate) else ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return ""
    match = _TT_PROFILE_PATH_RE.match(parts.path or "")
    if not match:
        return ""
    rest = match.group("rest") or ""
    # /@user/video/123、/@user/photo/123 是作品链接,不是主页监控目标
    if rest.strip("/"):
        return ""
    return match.group("handle")

# 首屏注水作品(不同版本结构略有差异,在 user-detail scope 里找"作品数组")。
_TT_PROFILE_HYDRATION_JS = r"""
() => {
  const looks = x => x && typeof x === 'object'
    && String(x.id || '').match(/^\d+$/) && (x.video || x.imagePost);
  try {
    const ud = window.__UNIVERSAL_DATA_FOR_REHYDRATION__
      && window.__UNIVERSAL_DATA_FOR_REHYDRATION__.__DEFAULT_SCOPE__;
    const detail = ud && ud['webapp.user-detail'];
    const buckets = [];
    const walk = (v, depth) => {
      if (depth > 6 || v == null) return;
      if (Array.isArray(v)) {
        if (v.length && v.every(looks)) buckets.push(v);
        return;
      }
      if (typeof v === 'object') {
        for (const k of Object.keys(v)) walk(v[k], depth + 1);
      }
    };
    walk(detail, 0);
    if (buckets.length) return buckets[0];
  } catch (e) {}
  try {
    const sigi = window.SIGI_STATE;
    const list = sigi && (sigi.ItemList
      || (sigi.ItemModule && Object.keys(sigi.ItemModule).length
          ? Object.values(sigi.ItemModule) : null));
    if (Array.isArray(list) && list.length && list.every(looks)) return list;
  } catch (e) {}
  return [];
}
"""

_TT_SCROLL_JS = """
() => {
  let best = null, bh = 0;
  document.querySelectorAll('div,ul,section,main').forEach(el => {
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


def _count(stats: dict, *keys: str) -> int:
    """TikTok statsV2 数值多为字符串,可能带 K/M/B 英文缩写。"""
    for key in keys:
        raw = stats.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, (int, float)):
            return int(raw)
        text = str(raw).strip().replace(",", "").replace("+", "")
        try:
            scale = 1
            if text and text[-1].lower() in {"k", "m", "b"}:
                scale = {"k": 1_000, "m": 1_000_000,
                         "b": 1_000_000_000}[text[-1].lower()]
                text = text[:-1]
            return int(float(text) * scale)
        except (ValueError, TypeError):
            continue
    return 0


def _cover_url(item: dict) -> str:
    image_post = item.get("imagePost") or item.get("image_post") or {}
    if image_post:
        cover = image_post.get("cover") or {}
        urls = ((cover.get("imageURL") or cover.get("image_url") or {})
                .get("urlList") or
                (cover.get("imageURL") or cover.get("image_url") or {})
                .get("url_list") or [])
        urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        if urls:
            return urls[-1]
    video = item.get("video") or {}
    for key in ("originCover", "cover", "dynamicCover"):
        value = video.get(key)
        if isinstance(value, list):
            hit = next((u for u in value
                        if isinstance(u, str) and u.startswith("http")), "")
            if hit:
                return hit
        if isinstance(value, str) and value.startswith("http"):
            return value
    return ""


def norm_tiktok_work(item: dict) -> Optional[dict]:
    """TikTok itemStruct → AccountWork 扁平 dict(与 _norm_douyin_work 同形)。"""
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id") or "").strip()
    if not item_id or not item_id.isdigit():
        return None
    stats = item.get("statsV2") or item.get("stats") or {}
    has_images = bool(item.get("imagePost") or item.get("image_post"))
    return {
        "item_id": item_id,
        "desc": str(item.get("desc") or "").strip(),
        "media_type": "images" if has_images else "video",
        "cover_url": _cover_url(item),
        "create_time": int(item.get("createTime")
                           or item.get("create_time") or 0),
        "like_count": _count(stats, "diggCount", "digg_count"),
        "comment_count": _count(stats, "commentCount", "comment_count"),
        "collect_count": _count(stats, "collectCount", "collect_count"),
        "share_count": _count(stats, "shareCount", "share_count"),
        "play_count": _count(stats, "playCount", "play_count"),
        "status": str(item.get("itemStatus")
                      or item.get("item_status") or ""),
    }


async def fetch_tiktok_works(
        mgr: BrowserManager,
        identity: Identity,
        unique_id: str,
        known: Optional[Set[str]] = None,
        *,
        max_scrolls: int = 14,
        settle_ms: int = 1500,
        block_media: bool = True,
        stop_after_known: bool = False,
        progress: Optional[Callable[[dict], None]] = None,
        is_canceled: Optional[Callable[[], bool]] = None,
) -> Tuple[list[dict], str]:
    """滚动同步作品(本人全量同步 / 他人主页增量监控共用)。

    返回 ``(原始 itemStruct 列表, error)``:
    成功(含有效空列表)error 为 ``""``/``"empty"``;``"canceled"`` 表示
    协作式取消,已抓到的条目仍随列表返回(作品为 upsert,部分结果可入库);
    ``logged_out:``/``goto:``/``timeout`` 为失败,上层保留旧数据。

    ``stop_after_known=True``(监控增量扫描)时采用水位线:一旦拦截到
    ``known`` 中作品即停止向前翻页(本页比它新的作品仍保留),并在返回前
    过滤掉 known 条目;本人同步保持 ``False`` 全量返回(upsert)。
    """
    handle = (unique_id or "").strip().lstrip("@")
    if not handle:
        return [], "missing_uid:账号缺 TikTok 号(uniqueId),请先「刷新资料」"
    known = known or set()

    def report(**changes):
        if progress is not None:
            try:
                progress(changes)
            except Exception:
                pass

    collected: dict[str, dict] = {}
    state = {"pages": 0, "has_more": True, "empty_confirmed": False,
             "saw_item_api": False, "hit_known": False}

    page = await mgr.new_page(identity, block_media=block_media)

    def _absorb(payload) -> bool:
        if not isinstance(payload, dict):
            return False
        # 注意:itemList 可能是空列表(账号 0 作品的有效信号),不能用 `or` 兜底
        items = payload.get("itemList")
        if items is None:
            items = payload.get("item_list")
        if not isinstance(items, list):
            return False
        state["saw_item_api"] = True
        state["pages"] += 1
        for raw in items:
            if not isinstance(raw, dict):
                continue
            iid = str(raw.get("id") or "")
            if iid and iid.isdigit() and iid not in collected:
                collected[iid] = raw
                if stop_after_known and iid in known:
                    # 列表按时间倒序:翻到已收录作品,更早的页不必再看
                    state["hit_known"] = True
        has_more = payload.get("hasMore")
        if has_more is None:
            has_more = payload.get("has_more")
        if isinstance(has_more, bool):
            state["has_more"] = has_more
        elif str(has_more) in {"0", "False", "false"}:
            state["has_more"] = False
        if not items:
            state["empty_confirmed"] = True
        report(phase="fetching", pages=state["pages"],
               fetched=len(collected), has_more=state["has_more"])
        return bool(items)

    async def on_response(resp):
        url = resp.url
        if TT_ITEM_LIST_HINT not in url or resp.request.resource_type \
                not in ("xhr", "fetch"):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        _absorb(payload)

    page.on("response", lambda resp: asyncio.create_task(on_response(resp)))

    error = ""
    try:
        target = TT_PROFILE_URL.format(handle=handle)
        try:
            await page.goto(target, wait_until="domcontentloaded",
                            timeout=30000)
        except Exception as exc:
            return list(collected.values()), f"goto:{type(exc).__name__}"
        lowered = str(page.url or "").lower()
        if "/login" in lowered or "passport" in lowered:
            return [], "logged_out:登录态失效,请重新登录"
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(800)
        # 首屏注水兜底(拦截器没拿到首屏 item_list 时)
        try:
            hydrated = await page.evaluate(_TT_PROFILE_HYDRATION_JS) or []
            for raw in hydrated:
                iid = str(raw.get("id") or "")
                if iid and iid.isdigit() and iid not in collected:
                    collected[iid] = raw
                    if stop_after_known and iid in known:
                        state["hit_known"] = True
        except Exception:
            pass
        report(phase="fetching", pages=state["pages"],
               fetched=len(collected), has_more=state["has_more"])

        stagnant = 0
        for _ in range(max(1, max_scrolls)):
            if is_canceled is not None:
                try:
                    canceled = bool(is_canceled())
                except Exception:
                    canceled = False
                if canceled:
                    error = "canceled"
                    break
            if (state["empty_confirmed"] or not state["has_more"]
                    or state["hit_known"]):
                break
            before = len(collected)
            try:
                await page.evaluate(_TT_SCROLL_JS)
                await page.mouse.wheel(0, 3000)
            except Exception:
                pass
            try:
                await page.wait_for_timeout(settle_ms)
            except Exception:
                await asyncio.sleep(settle_ms / 1000.0)
            lowered = str(page.url or "").lower()
            if "/login" in lowered or "passport" in lowered:
                error = "logged_out:登录态失效,请重新登录"
                break
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 4:
                    break
            else:
                stagnant = 0
    finally:
        try:
            await page.close()
        except Exception:
            pass

    items = list(collected.values())
    if error:
        # 取消时监控模式同样过滤已知条目(部分新结果仍可入库)
        if stop_after_known:
            items = [it for it in items
                     if str(it.get("id") or "") not in known]
        return items, error
    if not items:
        return [], ("empty" if state["empty_confirmed"] else "timeout")
    if stop_after_known:
        # 增量监控:只回传比水位线新的作品;命中过水位线说明"无更多新内容"
        # 是有效结论(空列表 + ""),不应被当成 empty/timeout
        return [it for it in items
                if str(it.get("id") or "") not in known], ""
    return items, ""
