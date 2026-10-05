"""TikTok 关键词搜索采集(浏览器拦截 /api/search/*/full)。

打开搜索页 ``https://www.tiktok.com/search?q=<keyword>``,滚动结果容器
触发翻页,拦截 ``/api/search/general/full/``、``/api/search/item/full/``
等响应,收集其中的 itemStruct(与作品详情/主页作品同构,可直接用
share.parse_tiktok_item 归一化)。

搜索是风控高发场景:跳转到验证码/人机校验中心(captcha/whale/security)
时不崩溃、不重试接口,而是把有头窗口置前并被动等待人工完成校验,
拦截到正常结果后自动续跑;等待超时则返回可执行中文错误,由采集
流水线停止该任务后续关键词。

筛选(排序/发布时间/内容类型/点赞评论门槛)在本地对拦截到的完整
itemStruct 执行——TikTok Web 搜索 URL 的筛选参数各地区版本不稳定,
本地过滤行为确定且可单测。
"""
from __future__ import annotations

import time
from typing import List, Optional, Set, Tuple
from urllib.parse import quote

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

TT_SEARCH_URL = "https://www.tiktok.com/search?q={keyword}"
TT_SEARCH_API_HINTS = (
    "/api/search/general/full",
    "/api/search/item/full",
    "/api/search/top/full",
)

# 验证码/人机校验中心路径(TikTok 全球风控常见落点)。
TT_VERIFY_PATH_MARKERS = (
    "/captcha", "/whale", "/verifycenter", "/security/verify",
)

# 滚动搜索结果容器:优先找可滚动容器,否则滚整页。
_TT_SCROLL_SEARCH_JS = """
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

# 首屏注水兜底:在 search 相关 scope 内深度收集 itemStruct。
_TT_SEARCH_HYDRATION_JS = r"""
() => {
  const looks = x => x && typeof x === 'object'
    && String(x.id || '').match(/^\d+$/) && (x.video || x.imagePost);
  const found = [];
  const seen = new Set();
  const push = item => {
    const id = String(item.id);
    if (!seen.has(id)) { seen.add(id); found.push(item); }
  };
  try {
    const scopes = (window.__UNIVERSAL_DATA_FOR_REHYDRATION__
      && window.__UNIVERSAL_DATA_FOR_REHYDRATION__.__DEFAULT_SCOPE__) || {};
    const walk = (v, depth) => {
      if (depth > 7 || v == null) return;
      if (Array.isArray(v)) {
        if (looks(v)) return;
        for (const one of v) walk(one, depth + 1);
        return;
      }
      if (typeof v === 'object') {
        if (looks(v)) { push(v); return; }
        for (const k of Object.keys(v)) walk(v[k], depth + 1);
      }
    };
    for (const key of Object.keys(scopes)) {
      if (key.toLowerCase().includes("search")) walk(scopes[key], 0);
    }
  } catch (e) {}
  return found;
}
"""


def _count(stats: dict, *keys: str) -> int:
    """TikTok statsV2 数值多为字符串,可能带 K/M/B 缩写。"""
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


def _is_verify_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(marker in lowered for marker in TT_VERIFY_PATH_MARKERS)


def _payload_needs_verification(payload) -> bool:
    """识别搜索响应里的显式风控/验证码信号(宁漏勿误判)。"""
    if not isinstance(payload, dict):
        return False
    for key in ("need_captcha", "needCaptcha", "show_captcha",
                "showCaptcha", "need_verify", "needVerify",
                "need_sms_captcha", "needSmsCaptcha"):
        value = payload.get(key)
        if value not in (None, False, "", 0, "0"):
            return True
    verify_type = payload.get("verify_type") or payload.get("verifyType")
    return verify_type not in (None, "", 0, "0")


def extract_tiktok_search_items(payload) -> List[dict]:
    """搜索响应 → itemStruct 列表(兼容 data 包裹与裸数组)。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if data is None:
        data = payload.get("item_list") or payload.get("itemList")
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item")
        if not isinstance(item, dict):
            # 少数版本直接在 data 元素里给 itemStruct
            item = entry if (entry.get("video") or entry.get("imagePost")) else None
        if isinstance(item, dict) and str(item.get("id") or "").strip().isdigit():
            items.append(item)
    return items


def tiktok_search_item_matches(item: dict, *, content_type: str = "all",
                               publish_time: str = "all",
                               min_likes: int = 0, min_comments: int = 0,
                               now: Optional[int] = None) -> bool:
    """对完整 itemStruct 做本地筛选。"""
    if content_type == "video" and (item.get("imagePost")
                                    or item.get("image_post")):
        return False
    if content_type == "images" and not (item.get("imagePost")
                                         or item.get("image_post")):
        return False
    if publish_time and publish_time != "all":
        windows = {"day": 86400, "week": 604800, "half_year": 15552000}
        window = windows.get(publish_time)
        created = int(item.get("createTime") or item.get("create_time") or 0)
        if window and created:
            current = int(now if now is not None else time.time())
            if current - created > window:
                return False
    stats = item.get("statsV2") or item.get("stats") or {}
    if min_likes and _count(stats, "diggCount", "digg_count") < min_likes:
        return False
    if min_comments and _count(stats, "commentCount", "comment_count") < min_comments:
        return False
    return True


def sort_tiktok_search_items(items: List[dict], search_sort: str) -> List[dict]:
    if search_sort == "latest":
        return sorted(items, key=lambda it: int(
            it.get("createTime") or it.get("create_time") or 0), reverse=True)
    if search_sort == "most_liked":
        return sorted(items, key=lambda it: _count(
            (it.get("statsV2") or it.get("stats") or {}),
            "diggCount", "digg_count"), reverse=True)
    return items


async def fetch_tiktok_search(
        mgr: BrowserManager,
        identity: Identity,
        keyword: str,
        known_cids: Optional[Set[str]],
        *,
        max_results: int = 20,
        max_scrolls: int = 12,
        stagnant_limit: int = 3,
        captcha_wait_seconds: int = 300,
        settle_ms: int = 1800,
        block_media: bool = True,
        content_type: str = "all",
        publish_time: str = "all",
        min_likes: int = 0,
        min_comments: int = 0,
        search_sort: str = "general",
        context=None,
) -> Tuple[List[dict], str]:
    """打开 TikTok 搜索页并返回符合筛选条件的新 itemStruct 列表。

    返回 ``(items, error)``。error 为空表示成功(含"筛选后无结果")。
    遇人机校验会被动等待人工处理;超时/登录失效返回中文可执行错误。
    """
    keyword = str(keyword or "").strip()
    if not keyword:
        return [], "missing_keyword"
    known_cids = known_cids or set()
    stagnant_limit = max(1, min(int(stagnant_limit or 3), 8))
    collected: dict[str, dict] = {}
    state = {"verify": False, "verify_seen": False, "saw_api": False}

    def _accept(payload) -> int:
        before = len(collected)
        for item in extract_tiktok_search_items(payload):
            iid = str(item.get("id") or "")
            if iid in collected or iid in known_cids:
                continue
            if tiktok_search_item_matches(
                    item, content_type=content_type,
                    publish_time=publish_time,
                    min_likes=min_likes, min_comments=min_comments):
                collected[iid] = item
        return len(collected) - before

    if context is not None:
        page = next((candidate for candidate in context.pages
                     if candidate.url == "about:blank"), None)
        page = page or await context.new_page()
    else:
        page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        url = resp.url
        if not any(hint in url for hint in TT_SEARCH_API_HINTS):
            return
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        state["saw_api"] = True
        if _payload_needs_verification(payload):
            state["verify"] = True
            state["verify_seen"] = True
            return
        if _accept(payload):
            state["verify"] = False

    page.on("response", on_response)
    error = ""
    try:
        target = TT_SEARCH_URL.format(keyword=quote(keyword))
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        async def verification_present() -> bool:
            try:
                if _is_verify_url(page.url):
                    return True
            except Exception:
                pass
            return state["verify"]

        async def wait_for_verification() -> bool:
            """被动等待人工完成校验;期间不主动发搜索请求。"""
            if not await verification_present():
                return True
            state["verify_seen"] = True
            try:
                await page.bring_to_front()
            except Exception:
                pass
            rounds = max(1, min(600, int(captcha_wait_seconds)))
            for _ in range(rounds):
                await page.wait_for_timeout(1000)
                if collected or not await verification_present():
                    state["verify"] = False
                    return True
            return False

        await page.wait_for_timeout(settle_ms)
        lowered = str(page.url or "").lower()
        if "/login" in lowered or "passport" in lowered:
            return [], "TikTok 登录态已失效；请重新登录后再续跑"
        if await verification_present() and not await wait_for_verification():
            return [], ("TikTok 要求完成人机验证；本次任务已停止后续请求，"
                        "请在弹出的浏览器窗口中完成验证并等待冷却后再续跑")

        # 首屏注水兜底(拦截器没拿到首屏搜索响应时)
        try:
            for raw in await page.evaluate(_TT_SEARCH_HYDRATION_JS) or []:
                iid = str(raw.get("id") or "")
                if iid not in collected and iid not in known_cids and \
                        tiktok_search_item_matches(
                            raw, content_type=content_type,
                            publish_time=publish_time,
                            min_likes=min_likes, min_comments=min_comments):
                    collected[iid] = raw
        except Exception:
            pass

        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls or 1), 40))):
            if len(collected) >= max_results:
                break
            before = len(collected)
            try:
                await page.evaluate(_TT_SCROLL_SEARCH_JS)
                await page.mouse.wheel(0, 3000)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            lowered = str(page.url or "").lower()
            if "/login" in lowered or "passport" in lowered:
                error = "TikTok 登录态已失效；请重新登录后再续跑"
                break
            if await verification_present():
                if not await wait_for_verification():
                    error = ("TikTok 要求完成人机验证；本次任务已停止后续请求，"
                             "请在弹出的浏览器窗口中完成验证并等待冷却后再续跑")
                    break
            if len(collected) == before:
                stagnant += 1
                if collected and stagnant >= stagnant_limit:
                    break
            else:
                stagnant = 0

        if not error and not collected and state["verify_seen"]:
            error = ("TikTok 触发人机验证；本次任务已停止后续请求，"
                     "请完成验证并等待冷却后再续跑")
        elif not error and not collected and not state["saw_api"]:
            # 见过搜索接口但 data 为空 = 有效空结果;完全没见过接口则给可观测诊断
            # (归 BUSINESS:不暂停账号、不阻断后续关键词)。
            error = ("未拦截到 TikTok 搜索结果(可能未登录/触发人机验证/关键词无结果)")
    except Exception as exc:
        error = f"打开 TikTok 搜索页失败: {type(exc).__name__}: {exc}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if error:
        return list(collected.values()), error
    items = sort_tiktok_search_items(list(collected.values()), search_sort)
    return items[:max(1, int(max_results or 20))], ""
