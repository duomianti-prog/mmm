"""用真实浏览器打开用户主页,拦截抖音自己发的 post 接口响应,
直接拿到 aweme_list —— 绕过自算 a_bogus。
对应原项目 engine.ContentChecker + NativeClient 的抓取角色。

优化:屏蔽图片/视频/字体资源(只取数据,省带宽提速)、无新增即提前停止下滑。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse, urlsplit

from .identity import Identity
from .manager import BrowserManager
from ..platforms.douyin.extract import danmaku_key

POST_API = "aweme/v1/web/aweme/post"
PROFILE_API = "aweme/v1/web/user/profile/other"
SELF_PROFILE_API = "aweme/v1/web/user/profile/self"
COMMENT_API = "aweme/v1/web/comment/list"
DANMAKU_API = "aweme/v1/web/danmaku"
SEARCH_API_MARKERS = (
    "/aweme/v1/web/general/search/single/",
    "/aweme/v1/web/search/item/",
    "/aweme/v1/web/search/feed/",
)
# 与 login.py 的登录成功判据保持一致。资料接口改版时不能再只靠页面“登录”按钮
# 判断登录态：按钮可能未渲染，或者被 AB 页面隐藏。
_LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard"}
# 重发时必须去掉的一次性签名/风控参数,让抖音的 fetch 拦截器重新签
_SIGN_PARAMS = ("a_bogus", "X-Bogus", "x-bogus", "msToken", "_signature", "verifyFp")

# 抖音主页不一定由 window 承担滚动。选出页面里滚动范围最大的容器并拉到底，
# 再配合 mouse.wheel 触发 React 的滚动/分页监听。
_SCROLL_PROFILE_JS = """() => {
  const roots = [document.scrollingElement, document.documentElement, document.body];
  const nodes = [...document.querySelectorAll('main,section,div')];
  let best = null;
  let bestRange = 0;
  for (const el of [...roots, ...nodes]) {
    if (!el) continue;
    const range = (el.scrollHeight || 0) - (el.clientHeight || 0);
    if (range > bestRange) { best = el; bestRange = range; }
  }
  window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
  if (best) best.scrollTop = best.scrollHeight;
  return { range: bestRange, top: best ? best.scrollTop : window.scrollY };
}"""

_DOUYIN_SEARCH_INPUTS = (
    'input[data-e2e="searchbar-input"]',
    'input[data-e2e="search-input"]',
    'input[placeholder*="搜索"]',
    'input[type="search"]',
)


async def _submit_douyin_search(page, keyword: str) -> bool:
    """只操作站内搜索框；不拼装、重放或主动调用抖音搜索接口。"""
    for selector in _DOUYIN_SEARCH_INPUTS:
        try:
            field = page.locator(selector).first
            if not await field.count() or not await field.is_visible(timeout=600):
                continue
            await field.click(timeout=2000)
            await field.fill(keyword, timeout=3000)
            await field.press("Enter", timeout=3000)
            return True
        except Exception:
            continue
    return False


def _page_reaches_boundary(items: List[dict], known_ids: Set[str],
                           stop_before: int = 0) -> bool:
    """一整页都已见过/早于监控起点时，才认为翻到了历史边界。

    置顶作品会把旧 ID 混在第一页，不能因为单个旧 ID 就停止。
    """
    rows = [it for it in items if str(it.get("aweme_id") or "")]
    if not rows:
        return False
    if known_ids and all(str(it.get("aweme_id") or "") in known_ids for it in rows):
        return True
    if stop_before:
        times = [int(it.get("create_time") or 0) for it in rows]
        return bool(times) and all(ts and ts < stop_before for ts in times)
    return False


def extract_search_awemes(payload) -> List[dict]:
    """从抖音不同版本的搜索响应中提取作品对象并保持首次出现顺序。

    搜索接口先后出现过 ``data[].aweme_info``、``aweme_list`` 与混合卡片等
    包装；这里只遍历已知容器键，避免把作者推荐卡误判成作品。
    """
    found: Dict[str, dict] = {}
    container_keys = (
        "data", "items", "list", "aweme_list", "aweme_info",
        "aweme_mix_info", "mix_items", "search_result", "card",
    )

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        aid = str(value.get("aweme_id") or "")
        if aid and (value.get("video") or value.get("images")):
            found.setdefault(aid, value)
            return
        for key in container_keys:
            child = value.get(key)
            if child is not None and child is not value:
                visit(child)

    visit(payload)
    return list(found.values())


def douyin_search_empty_error(page_title: str, page_text: str,
                              final_url: str, api_seen: List[str]) -> str:
    """把空搜索结果区分为验证、掉登录、接口变化和真正的空响应。"""
    diagnostic = f"{page_title}\n{page_text}\n{final_url}".casefold()
    # 登录弹窗本身包含“验证码登录”字样，必须先识别“扫码登录”等登录墙信号，
    # 否则会被下面的安全验证码分支误报成风控验证。
    if any(token in diagnostic for token in (
            "扫码登录", "登录后即可", "登录后查看", "立即登录", "/login")):
        return "抖音登录态已失效；请重新扫码登录后再续跑"
    if any(token in diagnostic for token in (
            "验证码", "安全验证", "访问频繁", "环境异常",
            "captcha", "verify")):
        return ("抖音触发验证码/安全验证；请在账号页点击“打开浏览器”，"
                "完成验证并关窗保存登录态后再续跑")
    if api_seen:
        return "抖音搜索接口有响应，但没有可解析的作品（关键词可能无结果或接口结构已变化）"
    return "抖音搜索页没有发出作品搜索接口（页面可能未完整加载或平台页面已变化）"


def douyin_search_exception_error(exc: Exception) -> str:
    """把用户主动关窗与真正的页面异常分开，避免向界面泄漏超长堆栈文本。"""
    detail = repr(exc)
    if "TargetClosedError" in detail or "has been closed" in detail:
        return "抖音采集窗口被关闭；请续跑任务，并在任务结束前保持窗口打开"
    return f"打开抖音搜索页失败: {detail}"


def _douyin_search_needs_verification(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    nil_info = payload.get("search_nil_info") or {}
    marker = " ".join(str(nil_info.get(key) or "") for key in (
        "search_nil_type", "search_nil_item", "text_type"))
    return "verify" in marker.casefold()


_SEARCH_SORT_CODES = {"general": "0", "most_liked": "1", "latest": "2"}
_SEARCH_TIME_CODES = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
_SEARCH_TIME_SECONDS = {"day": 86400, "week": 7 * 86400, "half_year": 180 * 86400}


def _douyin_search_item_matches(item: dict, *, content_type: str = "all",
                                publish_time: str = "all", min_likes: int = 0,
                                min_comments: int = 0, now: int | None = None) -> bool:
    """对平台返回结果做确定性二次筛选，避免页面筛选未生效时混入错误数据。"""
    if not isinstance(item, dict):
        return False
    if content_type == "video" and not item.get("video"):
        return False
    if content_type == "images" and not item.get("images"):
        return False
    statistics = item.get("statistics") or {}
    if int(statistics.get("digg_count") or 0) < max(0, int(min_likes or 0)):
        return False
    if int(statistics.get("comment_count") or 0) < max(0, int(min_comments or 0)):
        return False
    window = _SEARCH_TIME_SECONDS.get(publish_time)
    created = int(item.get("create_time") or 0)
    # 少数搜索卡片不带 create_time；平台筛选仍可能已生效，未知值不在本地误删。
    if window and created and created < int(now or time.time()) - window:
        return False
    return True


def _sort_douyin_search_items(items: list[dict], search_sort: str) -> list[dict]:
    if search_sort == "latest":
        return sorted(items, key=lambda item: int(item.get("create_time") or 0), reverse=True)
    if search_sort == "most_liked":
        return sorted(
            items,
            key=lambda item: int((item.get("statistics") or {}).get("digg_count") or 0),
            reverse=True,
        )
    return items


# 作品页没拿到数据时,看页面究竟是什么状态:登录墙?空态?还是 tab 没激活?
_WORKS_DOM_PROBE_JS = """() => {
  const txt = (document.body.innerText || '').replace(/\\s+/g, ' ');
  const tabs = [...document.querySelectorAll('[data-e2e*="tab"],[class*="tab"]')]
    .map(e => (e.textContent || '').trim().slice(0, 8))
    .filter(t => t && t.length <= 8).slice(0, 8);
  return {
    tabs: [...new Set(tabs)],
    items: document.querySelectorAll('[data-e2e="user-post-list"] li, li[data-e2e]').length,
    // 这几种文案能把「空态 / 登录墙 / 风控」区分开
    empty: /暂无作品|还没有发布|没有更多了/.test(txt),
    login_wall: /登录后查看|立即登录|扫码登录/.test(txt),
    risk: /访问频繁|环境异常|验证/.test(txt),
    body_len: txt.length,
  };
}"""


def _works_zero_capture_error(final_url: str, dom: object, post_hits: list,
                              api_seen: list) -> str:
    """零抓取时用页面证据拼一条可读错误,随试跑结果直接展示给用户。

    措辞刻意避开 classify_platform_error 的 auth/risk/network 关键词,
    保持 BUSINESS 分类——这些只是诊断线索,不能当作判定登录态失效、
    进而去惩罚账号的证据。
    """
    if not isinstance(dom, dict):
        dom = {}
    parts: list = []
    low_url = str(final_url or "").lower()
    if "passport" in low_url or "/login" in low_url:
        parts.append("页面被重定向到登录页,请到「账号」页重新扫码登录后重试")
    elif dom.get("login_wall"):
        parts.append("页面出现登录提示墙,请到「账号」页重新扫码登录后重试")
    if dom.get("empty"):
        parts.append("主页显示暂无作品")
    if dom.get("risk"):
        parts.append("页面出现平台异常提示,请稍后重试")
    if not parts:
        if post_hits:
            parts.append(f"作品接口有 {len(post_hits)} 次响应但没有返回作品数据")
        elif api_seen:
            parts.append(f"页面共发出 {len(api_seen)} 个接口请求但没有作品列表接口")
        else:
            parts.append("页面没有发出任何可观测的接口请求")
    probe_failed = str(dom.get("probe_failed") or "")
    if probe_failed:
        parts.append(f"页面诊断失败:{probe_failed[:120]}")
    return f"未拦截到作品数据({';'.join(parts)})"


async def fetch_videos(mgr: BrowserManager, identity: Identity, sec_uid: str,
                       known_ids: Set[str], max_scrolls: int = 12,
                       settle_ms: int = 1800, block_media: bool = True,
                       stop_before: int = 0, min_scrolls: int = 2,
                       ) -> Tuple[List[dict], Optional[dict], str]:
    """打开主页并下滑,收集作品。返回 (新作品列表, 作者信息dict, error)。"""
    collected: Dict[str, dict] = {}
    author: Optional[dict] = None
    error = ""
    post_hits = []        # 命中的 aweme/post 响应(判断是「没发」还是「发了解不出」)
    post_pages: List[List[dict]] = []  # 保留每页边界，不能拿混合后的 collected 判断停止
    api_seen = []         # 该页发出的抖音 API(post_hits 为空时,靠它看页面到底在请求什么)
    pagination_stalled = False

    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal author
        url = resp.url
        if ("douyin.com" in url and ("/aweme/v1/web/" in url or "/web/api/" in url)
                and len(api_seen) < 40):
            api_seen.append(f"{resp.status} {url.split('?')[0].split('douyin.com')[-1]}")
        if POST_API in url:
            try:
                data = await resp.json()
            except Exception as e:
                # 页面跳转会丢弃 body。别静默 pass,否则「200 却没数据」永远查不出原因
                post_hits.append(f"{resp.status} body_read_failed={e!r}")
                return
            lst = data.get("aweme_list")
            post_hits.append(f"{resp.status} status_code={data.get('status_code')} "
                             f"aweme_list={len(lst) if isinstance(lst, list) else lst!r} "
                             f"has_more={data.get('has_more')} max_cursor={data.get('max_cursor')} "
                             f"keys={sorted(data)[:8]}")
            if isinstance(lst, list):
                post_pages.append(lst)
            for it in (lst or []):
                aid = str(it.get("aweme_id") or "")
                if aid:
                    collected[aid] = it
                    if author is None and it.get("author"):
                        author = it["author"]
        elif PROFILE_API in url and author is None:
            try:
                data = await resp.json()
            except Exception:
                return
            if data.get("user"):
                author = data["user"]

    page.on("response", on_response)

    try:
        await page.goto(f"https://www.douyin.com/user/{sec_uid}",
                        wait_until="domcontentloaded", timeout=30000)
        # 同私信/粉丝入口:没 hydrate 完,作品列表的分页请求根本不会发
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        min_scrolls = max(1, min(min_scrolls, max_scrolls))
        for scroll_index in range(max_scrolls):
            before = len(collected)
            pages_before = len(post_pages)
            try:
                await page.evaluate(_SCROLL_PROFILE_JS)
            except Exception:
                pass
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(settle_ms)
            fresh_pages = post_pages[pages_before:]
            boundary = any(_page_reaches_boundary(p, known_ids, stop_before)
                           for p in fresh_pages)
            # 至少真实滚动几次，避免首屏里一个置顶旧作品直接截断扫描。
            if scroll_index + 1 >= min_scrolls and boundary:
                break
            if len(collected) == before:               # 本次下滑无新增
                # 一条都没抓到时别提前退:那是「还没开始」,不是「已经到底」。
                # 首屏 XHR 可能比 networkidle 更晚,滚满 max_scrolls 再放弃。
                stagnant += 1
                if (collected and scroll_index + 1 >= min_scrolls
                        and stagnant >= 3):             # 连续三次无响应才判定到底
                    pagination_stalled = bool(post_pages and len(post_pages) == 1)
                    break
            else:
                stagnant = 0
    except Exception as e:
        error = f"打开主页失败: {e!r}"
    finally:
        final_url = page.url
        dom = {}
        if not collected:
            try:                        # 页面到底渲染成什么样了(tab?空态?登录墙?)
                dom = await page.evaluate(_WORKS_DOM_PROBE_JS)
            except Exception as e:
                dom = {"probe_failed": repr(e)}
        try:
            await page.close()
        except Exception:
            pass

    if not collected:
        # 「aweme/post 没发出来」和「发了但 aweme_list 空/读不到」是两回事,原来一律报同一句话
        print(f"[works] 未拿到作品; sec_uid={sec_uid[:24]}… final_url={final_url}; "
              f"post_hits({len(post_hits)})={post_hits[:5]}; dom={dom}")
        print(f"[works] api_seen({len(api_seen)})={api_seen[:30]}")
        error = (error or _works_zero_capture_error(
            final_url, dom, post_hits, api_seen))
    elif pagination_stalled:
        print(f"[works] 主页分页未触发; sec_uid={sec_uid[:24]}… "
              f"collected={len(collected)} post_hits={post_hits[:3]}")
    new_items = [it for aid, it in collected.items() if aid not in known_ids]
    return new_items, author, error


async def fetch_douyin_search(mgr: BrowserManager, identity: Identity, keyword: str,
                               max_results: int = 20, max_scrolls: int = 12,
                               stagnant_limit: int = 3,
                               search_sort: str = "general",
                               publish_time: str = "all",
                               content_type: str = "all",
                               min_likes: int = 0,
                               min_comments: int = 0,
                               settle_ms: int = 1800,
                               captcha_wait_seconds: int = 300,
                               block_media: bool = False,
                               context=None) -> Tuple[List[dict], str]:
    """打开抖音视频搜索页，拦截站内搜索响应并返回作品原始对象。"""
    collected: Dict[str, dict] = {}
    api_seen: List[str] = []
    error = ""
    verification_active = False
    verification_seen = False
    search_candidates_seen = 0
    search_sort = search_sort if search_sort in _SEARCH_SORT_CODES else "general"
    publish_time = publish_time if publish_time in _SEARCH_TIME_CODES else "all"
    content_type = content_type if content_type in {"all", "video", "images"} else "all"
    stagnant_limit = max(1, min(int(stagnant_limit or 3), 8))
    # 关键词搜索在抖音无头上下文中容易直接落到“验证码中间页”。批量任务可传入
    # 同账号的临时有头 context；普通调用仍沿用后台常驻 context。
    if context is not None:
        page = next((candidate for candidate in context.pages
                     if candidate.url == "about:blank"), None)
        page = page or await context.new_page()
    else:
        page = await mgr.new_page(identity, block_media)

    def collect_payload(payload) -> int:
        nonlocal search_candidates_seen
        before = len(collected)
        for raw in extract_search_awemes(payload):
            search_candidates_seen += 1
            aid = str(raw.get("aweme_id") or "")
            if aid and _douyin_search_item_matches(
                    raw, content_type=content_type, publish_time=publish_time,
                    min_likes=min_likes, min_comments=min_comments):
                collected.setdefault(aid, raw)
        return len(collected) - before

    async def on_response(resp):
        nonlocal verification_active, verification_seen
        url = resp.url
        # 保留已知接口，同时接受路径中含 search 的新版本网页接口，避免平台只改
        # 路径就被误报成“没有发出搜索接口”。
        is_search_api = any(marker in url for marker in SEARCH_API_MARKERS)
        is_search_api = is_search_api or (
            "/aweme/v1/web/" in url and "search" in urlsplit(url).path.casefold())
        if not is_search_api:
            return
        if len(api_seen) < 40:
            api_seen.append(f"{resp.status} {url.split('?')[0]}")
        try:
            payload = await resp.json()
        except Exception:
            return
        if _douyin_search_needs_verification(payload):
            verification_active = True
            verification_seen = True
            return
        if collect_payload(payload):
            verification_active = False

    async def wait_for_verification() -> bool:
        """被动等待用户完成验证；等待期间不产生搜索请求。"""
        if not verification_active:
            return True
        try:
            await page.bring_to_front()
        except Exception:
            pass
        rounds = max(1, min(600, int(captcha_wait_seconds)))
        for _ in range(rounds):
            await page.wait_for_timeout(1000)
            if collected or not verification_active:
                return True
        return False

    page.on("response", on_response)
    final_url = ""
    page_title = ""
    page_text = ""
    try:
        # 先像普通用户一样进入站内搜索结果页，只消费页面自己发出的响应。
        query = urlencode({
            "type": "video" if content_type == "video" else "general",
            "publish_time": _SEARCH_TIME_CODES[publish_time],
            "sort_type": _SEARCH_SORT_CODES[search_sort],
        })
        target = f"https://www.douyin.com/search/{quote(keyword, safe='')}?{query}"
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_ms)

        challenge_error = ""
        if verification_active and not await wait_for_verification():
            challenge_error = (
                "抖音要求完成滑块验证；本次任务已停止后续请求，"
                "请完成验证并等待冷却后再续跑")

        # 某些 AB 页面进入 URL 后不会自动提交首屏搜索。此时只操作一次页面搜索框，
        # 由抖音页面生成参数和签名；不再使用 context.request/page.fetch 兜底。
        if not collected and not challenge_error:
            submitted = await _submit_douyin_search(page, keyword)
            if submitted:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(max(settle_ms, 2200))
                if verification_active and not await wait_for_verification():
                    challenge_error = (
                        "抖音要求完成滑块验证；本次任务已停止后续请求，"
                        "请完成验证并等待冷却后再续跑")

        stagnant = 0
        for _ in range(0 if challenge_error else max(1, min(max_scrolls, 40))):
            if len(collected) >= max_results:
                break
            before = len(collected)
            try:
                await page.evaluate(_SCROLL_PROFILE_JS)
            except Exception:
                pass
            await page.mouse.wheel(0, 4200)
            await page.wait_for_timeout(settle_ms)
            if verification_active and not await wait_for_verification():
                challenge_error = (
                    "抖音要求完成滑块验证；本次任务已停止后续请求，"
                    "请完成验证并等待冷却后再续跑")
                break
            if len(collected) == before:
                stagnant += 1
                if collected and stagnant >= stagnant_limit:
                    break
            else:
                stagnant = 0
        final_url = page.url
        if not collected:
            try:
                page_title = (await page.title()).strip()
            except Exception:
                pass
            try:
                page_text = (await page.locator("body").inner_text())[:1200]
            except Exception:
                pass
            filter_error = (
                "抖音搜索有结果，但当前内容类型、发布时间或数据门槛下没有符合条件的作品"
                if search_candidates_seen else "")
            error = challenge_error or filter_error or (
                "抖音触发验证码/安全验证；本次任务已停止后续请求，"
                "请完成验证并等待冷却后再续跑"
                if verification_seen else "") or douyin_search_empty_error(
                page_title, page_text, final_url, api_seen)
    except Exception as exc:
        error = douyin_search_exception_error(exc)
    finally:
        try:
            final_url = final_url or page.url
            await page.close()
        except Exception:
            pass

    if not collected:
        print(f"[dy_search] keyword={keyword!r} final_url={final_url!r} "
              f"title={page_title!r} api_seen({len(api_seen)})={api_seen[:10]}")
    return _sort_douyin_search_items(list(collected.values()), search_sort)[:max_results], error


# 滚动评论区的可滚动容器(而不是整页),驱动抖音自己的分页请求
_SCROLL_COMMENTS = """
() => {
  // 必须从"可见"的评论项出发找滚动容器。抖音页面同时存在两套
  // comment-item:隐藏的预渲染节点(0x0,含首屏评论全文,供 SEO/SSR)
  // 和可见的真实评论区。document.querySelector 返回文档序第一个——
  // 恰是隐藏节点,它的祖先链没有滚动容器,于是回退 window.scrollBy
  // 滚主页面,评论区永远加载不出后续评论(30 轮翻页全无效的根因)。
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  let item = null;
  const all = document.querySelectorAll('[data-e2e="comment-item"]');
  for (const it of all) { if (vis(it)) { item = it; break; } }
  if (!item) {
    const list = document.querySelector('[data-e2e="comment-list"]');
    if (list && vis(list)) item = list;
  }
  if (!item) { window.scrollBy(0, 3000); return false; }
  let el = item;
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
      // 不要一步 scrollTop=scrollHeight:部分虚拟列表只认增量滚动,
      // 分段滚到底更稳(右侧抽屉和下方布局都适用)。v1.6.5:步长从一屏
      // 提到 1.2 屏——536 条评论列表高 8 万 px,一屏步长×24 轮只能扫
      // 1/5(B 机实锤 top=15196/80464 即轮次跑满)。1.2 屏仍是连续增量,
      // 虚拟列表哨兵正常触发分页。
      const step = Math.max(600, Math.round(el.clientHeight * 1.2));
      el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
      return true;
    }
    el = el.parentElement;
  }
  // 下方布局(评论区在视频之下)的滚动容器就是主页面
  window.scrollBy(0, 3000);
  return false;
}
"""

# 点击评论入口展开评论区(新版抖音作品页评论区可能是收起的,需点击才渲染)。
# 尝试多种选择器:评论图标按钮/评论数文本/评论侧栏标题。
_CLICK_COMMENT_ENTRY = r"""
() => {
  // 1) 找可点击的评论入口(通常带气泡图标或"评论"文案)
  const candidates = [
    '[data-e2e="comment-icon"]',           // 新版评论图标
    '[data-e2e="comment-count"]',
    'button[aria-label*="评论"]',
    '[class*="comment" i][class*="icon" i]',
    '[class*="CommentIcon" i]',
    'svg[class*="comment" i]',
  ];
  for (const sel of candidates) {
    try {
      const el = document.querySelector(sel);
      if (el && el.offsetWidth > 0) {
        el.click();
        return true;
      }
    } catch (e) {}
  }
  // 2) 按文本匹配「评论」「条评论」等(点击评论数也展开)
  const all = document.querySelectorAll('button, div[role="button"], span[class*="count" i]');
  for (const el of all) {
    const t = (el.innerText || el.textContent || '').trim();
    if (/评论|\d+\s*条评论|展开评论/.test(t) && el.offsetWidth > 0) {
      el.click();
      return true;
    }
  }
  return false;
}
"""

# 在评论区中定位"要回复的那条评论"。纯表情/手势评论(如 👍、[赞])在抖音 DOM
# 里常渲染成 <img alt="[赞]"> 而非文本节点,Playwright 的 has_text 匹配不到;
# 这里同时收集评论项的 textContent/innerText 与表情节点的 alt/title/aria-label
# 等,并对 emoji 做规范化(去变体选择符 U+FE0x、零宽连接符/空格、所有空白)后
# 做包含匹配;形如 [赞] 的短代码额外允许与裸 alt "赞" 相等匹配。命中的评论项
# 打 data-mmm-target 标记(而非返回索引),避免懒加载/虚拟列表导致索引漂移。
_FIND_COMMENT_ITEM = r"""
(arg) => {
  // arg 形如 {text, nick, cid};向后兼容直接传字符串。
  const text = (arg && typeof arg === 'object' && arg.text) ? arg.text : (arg || '');
  const nick = (arg && typeof arg === 'object' && arg.nick) || '';
  const cid = (arg && typeof arg === 'object' && arg.cid) || '';
  const norm = s => (s || '').replace(/[\uFE00-\uFE0F\u200B-\u200D\uFEFF]/g, '').replace(/\s+/g, '');
  const t = norm(text);
  const n = norm(nick);
  // 关键教训(v1.5.4): _vis 用 getBoundingClientRect 阈值过滤会在评论区
  // 虚拟列表/懒加载场景把已渲染但暂时在视口外的评论项误判为隐藏,导致
  // "明明样本里有目标评论却 30 轮匹配不到"。改为:匹配所有 comment-item,
  // 仅对可见项额外加 500 分(优先点可见的);隐藏项也能命中,点「回复」时
  // 浏览器会自动把它滚进视口(Playwright 的 click 自带 scroll-into-view)。
  const _vis = el => {
    // 空文本/空表情的节点不算可见(防止测试桩或预渲染空节点被加分)
    if (!(el.innerText || el.textContent || '').trim() && !el.querySelectorAll('img,[data-emoji]').length) return false;
    if (el && typeof el.getBoundingClientRect === 'function') {
      const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    }
    return true; // 无布局 API 的离线测试桩放行
  };
  const items = [...document.querySelectorAll('[data-e2e="comment-item"]')];
  document.querySelectorAll('[data-mmm-target="1"]')
    .forEach(el => el.removeAttribute('data-mmm-target'));
  if (!t && !n) return [false, items.length];
  // 短前缀:DOM 里长评论会被截断并带「展开」,全文匹配失败时用前缀兜底
  const prefix = t.slice(0, Math.min(14, Math.max(4, t.length - 2)));
  const m = /^\[(.+)\]$/.exec(t);
  const bare = m ? m[1] : '';
  // v1.5.7 关键修复:表情短代码前缀(如 [666][666]柳州欢迎您！)的评论,
  // DOM 里表情是 <img alt="[666]">、文字是后续文本节点。旧实现把文本和
  // 各 img alt 当作独立片段用 \n 拼接,永远拼不出连续的
  // "[666][666]柳州欢迎您！",导致 203 条评论扫完仍不命中。这里按 DOM
  // 文档序遍历,遇到表情节点用其 alt 占位,得到逻辑连续文本。
  const logicalOf = (root) => {
    let out = '';
    const takeAlt = (node) => {
      out += node.getAttribute('alt') || node.getAttribute('title')
          || node.getAttribute('aria-label') || node.getAttribute('data-name')
          || node.getAttribute('data-emoji-name') || '';
    };
    const walk = (node) => {
      if (node.nodeType === 3) { out += node.nodeValue || ''; return; }
      if (node.nodeType !== 1) return;
      const tag = node.tagName;
      const cls = (typeof node.className === 'string' ? node.className : '');
      if (tag === 'IMG' || tag === 'XG-ICON') { takeAlt(node); return; }
      if (node.hasAttribute && (node.hasAttribute('data-emoji')
          || node.hasAttribute('data-emoji-name')
          || node.hasAttribute('data-name')
          || /emoji|sticker/i.test(cls))) { takeAlt(node); return; }
      const kids = node.childNodes || [];
      for (let i = 0; i < kids.length; i++) walk(kids[i]);
    };
    walk(root);
    return out;
  };
  // 表情短代码拆分:tokens=["[666]","[666]"],plain="柳州欢迎您！"
  const tokens = (t.match(/\[[^\]]*\]/g) || []);
  const plain = t.replace(/\[[^\]]*\]/g, '');
  // 在元素自身及其子树属性里找 cid(新版 DOM 的 cid 可能挂在任意 data-*
  // 属性或 id 上,位置随版本漂移)
  const cidOnAttrs = (node, depth) => {
    if (!node || !cid) return false;
    if (node.attributes) {
      const attrs = node.attributes;
      for (let i = 0; i < attrs.length; i++) {
        const v = attrs[i].value || '';
        if (v === cid || (cid.length > 8 && v.indexOf(cid) !== -1)) return true;
      }
    }
    // getAttribute 兜底(离线桩无 NamedNodeMap;真实 DOM 也多一道保险)
    if (node.getAttribute) {
      for (const a of ['data-cid', 'data-comment-id', 'cid', 'data-id', 'id']) {
        const v = node.getAttribute(a);
        if (v === cid || (v && cid.length > 8 && v.indexOf(cid) !== -1))
          return true;
      }
    }
    if (depth > 0 && node.querySelectorAll) {
      const kids = node.querySelectorAll('*');
      for (let i = 0; i < kids.length && i < 120; i++) {
        if (cidOnAttrs(kids[i], 0)) return true;
      }
    }
    return false;
  };
  const cand = [];
  for (const el of items) {
    const logical = norm(logicalOf(el))
      || norm(el.innerText || el.textContent || '');
    const parts = [logical, norm(el.innerText || ''), norm(el.textContent || '')];
    el.querySelectorAll(
      'img,[data-emoji],[data-emoji-name],[class*="emoji" i],[class*="sticker" i]'
    ).forEach(node => {
      for (const a of ['alt', 'title', 'aria-label', 'data-name', 'data-emoji-name']) {
        const v = node.getAttribute && node.getAttribute(a);
        if (v) parts.push(norm(v));
      }
    });
    const blob = parts.join('\n');
    // it_prefix:本条评论 DOM 逻辑文本的前缀(截断兜底)
    const it_prefix = logical.slice(0, 14);
    // v1.6.5 安全闸门(B 机实锤:第三次"假找到"并误回复了别人):
    // 旧逻辑 nick 命中 +30、可见 +500,一条"回复 @目标昵称"的子评论正文
    // 一字不差地不含目标正文,仅靠昵称出现+可见就以 530 分压过一切被当成
    // 目标。重构:①正文证据与可见性分离,可见性只作同分时的微幅 tiebreak,
    // 不得让弱证据翻盘;②nick 只作佐证不计正文分;③必须达到接受门槛才
    // 标记,宁可报失败也不能回复错评论。
    let cs = 0;            // 正文证据分(content score)
    let why = '';
    let alignPrefix = false; // 前缀与目标开头对齐(高独特性)
    if (t && logical.indexOf(t) !== -1) { cs = 100; why = 'full-logical'; }
    else if (t && blob.indexOf(t) !== -1) { cs = 90; why = 'full-blob'; }
    else if (plain.length >= 4) {
      // 表情+文字混合:去掉短代码后的纯文字部分必须连续出现
      const blobPlain = blob.replace(/\[[^\]]*\]/g, '');
      const logicalPlain = logical.replace(/\[[^\]]*\]/g, '');
      if (logicalPlain.indexOf(plain) !== -1
          || blobPlain.indexOf(plain) !== -1) {
        cs = 85; why = 'plain';
        if (tokens.length && tokens.every(tk => logical.indexOf(tk) !== -1))
          { cs += 10; why = 'plain+tokens'; }
      }
    }
    // 中等证据:前缀/DOM 截断前缀/裸表情名。单独不足以定罪,需满足门槛。
    let midWhy = '';
    if (!cs) {
      if (t && prefix && logical.indexOf(prefix) !== -1) { cs = 45; midWhy = 'prefix'; }
      else if (t && prefix && blob.indexOf(prefix) !== -1) { cs = 40; midWhy = 'blob-prefix'; }
      else if (bare && blob.indexOf(bare) !== -1) { cs = 55; midWhy = 'bare'; }
      else if (t && it_prefix.length >= 6) {
        // DOM 项是被截断的目标:其前 14 字必须出现在目标前 40 字内(排除
        // 短评论"哈哈哈"恰好是目标某位子串的巧合);长度 6+ 且有 nick 佐证
        // 即可,nick 会把巧合概率压到极低
        const pos = t.indexOf(it_prefix);
        if (pos !== -1 && pos < 40) { cs = 50; midWhy = 'item-prefix'; }
      }
      why = midWhy;
      // 前缀是否与目标开头对齐(用于无 nick 时的独特性判断)
      alignPrefix = !!(t && prefix && logical.indexOf(prefix) === 0)
        || (it_prefix.length >= 10 && t.indexOf(it_prefix) === 0);
    }
    const nickHit = !!(n && (logical.indexOf(n) !== -1
                             || blob.indexOf(n) !== -1));
    const cidHit = !!(cid && cidOnAttrs(el, 1));
    if (cidHit) { cs = 1000; why = 'cid'; }
    if (cs <= 0) continue;
    // 接受门槛:
    //  - 强证据(>=85,或 cid):直接接受
    //  - 中等证据(前缀/截断前缀/裸表情名):必须有 nick 佐证;无 nick 时
    //    仅当前缀与目标开头对齐且长度>=10(独特性足够)才接受
    //  - 纯表情目标(整体是一个表情短代码,无纯文字)沿用旧行为:裸名即接受
    const strong = cs >= 85;
    const pureEmoji = !!bare && plain.length === 0;
    const alignedLong = alignPrefix && (prefix.length >= 10
                                        || it_prefix.length >= 10);
    const accepted = strong || pureEmoji
      || (midWhy && (nickHit || alignedLong));
    if (!accepted) continue;
    // 同分时优先可见项(+3)与不含嵌套评论的叶子项(+2):父评论项 innerText
    // 会包含折叠子评论的预览文本,叶子项才能保证回复到精确楼层
    const leaf = !el.querySelector
      || !el.querySelector('[data-e2e="comment-item"]');
    let score = cs + (leaf ? 2 : 0);
    if (_vis(el)) score += 3;
    if (nickHit) score += 1;
    cand.push({ el, score, why });
  }
  // 最后兜底:cid 作为长数字串直接出现在评论项序列化 HTML 中(React 可能把
  // id 放在任意属性里),只在前述全部失败时做,避免常态开销
  if (!cand.length && cid) {
    for (const el of items) {
      try {
        if (el.outerHTML && el.outerHTML.indexOf(cid) !== -1) {
          const leaf = !el.querySelector
            || !el.querySelector('[data-e2e="comment-item"]');
          let score = 1000 + (leaf ? 2 : 0);
          if (_vis(el)) score += 3;
          cand.push({ el, score, why: 'cid-html' });
          break;
        }
      } catch (e) {}
    }
  }
  if (cand.length) {
    cand.sort((a, b) => b.score - a.score);
    cand[0].el.setAttribute('data-mmm-target', '1');
    return [true, items.length, cand[0].why || ''];
  }
  return [false, items.length, ''];
}
"""

_CLEAR_TARGET_MARK = """
() => {
  document.querySelectorAll('[data-mmm-target="1"]')
    .forEach(n => n.removeAttribute('data-mmm-target'));
}
"""

# 定位评论区"最热/最新"排序控件并打标(v1.5.7)。评论默认按热度排序,监控
# 发现的目标(按时间排序取)可能沉在热度榜 200 条之后;热度榜滚到底仍未
# 命中时,切到"最新"再扫一遍。控件可能是两个平铺 Tab,也可能是点"最热"
# 弹出下拉——后者选项常挂在 body 下,所以支持 scope=any 全局再扫一次。
# arg: 'panel'(默认,只认评论区容器内) | 'any'(下拉打开后全局可见项)
_FIND_COMMENT_SORT = r"""
(scope) => {
  document.querySelectorAll('[data-mmm-sort]')
    .forEach(el => el.removeAttribute('data-mmm-sort'));
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden'
        || (+st.opacity || 1) < 0.1) return false;
    return true;
  };
  const inPanel = el => {
    if (scope === 'any') return true;
    return !!(el.closest && el.closest(
      '[data-e2e*="comment"], [class*="comment" i]'));
  };
  let hot = null, fresh = null;
  const nodes = document.querySelectorAll(
    'span, div, a, button, [role="button"], li,'
    + ' [class*="sort" i], [class*="order" i], [class*="tab" i],'
    + ' [data-e2e*="sort"], [data-e2e*="order"], [data-e2e*="tab"]');
  for (const el of nodes) {
    if (!vis(el) || !inPanel(el)) continue;
    // 叶子级短文本("最热"/"最新"),避开"最新评论"之类的容器;
    // 归一化所有空白(新版 DOM 可能在字间插入换行/空格导致严格相等失败)。
    // v1.6.5: B 机现场 sort:[] —— 新版控件文案可能带箭头/图标后缀
    // ("最热▾""最新 "),精确相等识别不到,改为短文本包含匹配(<=6 字且
    // 不含"评论"等容器词)。
    const t = (el.innerText || el.textContent || '')
      .replace(/\s+/g, '');
    if (!t || t.length > 6 || /评论/.test(t)) continue;
    const leafText = !(el.children && el.children.length);
    if (!leafText && t !== '最热' && t !== '最熱' && t !== '最新') continue;
    if (!hot && (t === '最热' || t === '最熱'
        || (t.indexOf('最热') !== -1 && t.indexOf('最新') === -1)
        || t.indexOf('最熱') !== -1)) hot = el;
    if (!fresh && (t === '最新'
        || (t.indexOf('最新') !== -1 && t.indexOf('最热') === -1
            && t.indexOf('最熱') === -1))) fresh = el;
  }
  if (hot) hot.setAttribute('data-mmm-sort', 'hot');
  if (fresh) fresh.setAttribute('data-mmm-sort', 'new');
  return { hot: !!hot, new: !!fresh, scope: scope || 'panel' };
}
"""

# 末轮兜底:目标可能是折叠在「展开 N 条回复」里的子评论。每次只标记并
# 返回一个可点的叶子文本元素,由调用方 Playwright 点击、循环展开。
_FIND_EXPAND_REPLIES = r"""
() => {
  document.querySelectorAll('[data-mmm-expand]')
    .forEach(el => el.removeAttribute('data-mmm-expand'));
  const vis = el => {
    try {
      const r = el.getBoundingClientRect();
      if (!r || r.width < 4 || r.height < 4) return false;
      const st = getComputedStyle(el);
      return st.display !== 'none' && st.visibility !== 'hidden'
        && (+st.opacity || 1) >= 0.1;
    } catch (e) { return false; }
  };
  let hit = null;
  const nodes = document.querySelectorAll(
    'span, div, a, button, [role="button"], p, li, font');
  for (const el of nodes) {
    if (!vis(el)) continue;
    if (el.children && el.children.length) continue;   // 只认叶子文本
    const t = (el.innerText || el.textContent || '')
      .replace(/\s+/g, '');
    if (!/^展开(\d+条)?回复/.test(t)) continue;
    if (el.closest && !el.closest(
        '[data-e2e="comment-item"], [data-e2e*="comment"],'
        + ' [class*="comment" i]')) continue;
    hit = el;
    break;
  }
  if (hit) hit.setAttribute('data-mmm-expand', '1');
  return { found: !!hit,
           text: hit ? (hit.innerText || '').trim().slice(0, 20) : '' };
}
"""

# 找不到目标评论时导出评论区现场(v1.5.7):①排序控件有哪些(最热/最新)
# ②滚动容器位置与是否到底 ③首条评论的结构签名(item 标签/属性/带 data-e2e
# 或长数字 id 的后代)——新版 DOM 的 nick/cid 挂载点漂移时靠它定位
# ④前 6 条评论文本样本。一次失败即可看清"缺什么、卡在哪"。
_COMMENT_PANEL_DIAG = r"""
() => {
  const vis = el => {
    try { const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2; } catch (e) { return false; }
  };
  const items = [...document.querySelectorAll('[data-e2e="comment-item"]')];
  const visItems = items.filter(vis);
  // 1) 排序控件
  const sortLabels = [];
  document.querySelectorAll('span,div,a,button,li,[role="button"]').forEach(el => {
    if (!vis(el)) return;
    const t = (el.innerText || el.textContent || '').trim();
    if ((t === '最热' || t === '最新')
        && el.closest && el.closest('[data-e2e*="comment"],[class*="comment" i]'))
      sortLabels.push(t);
  });
  // 2) 滚动容器余量
  let sm = null;
  const it0 = visItems[0];
  if (it0) {
    let p = it0;
    while (p && p !== document.body) {
      const oy = getComputedStyle(p).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight + 20) {
        sm = 'top=' + Math.round(p.scrollTop) + '/' + p.scrollHeight
           + ' client=' + p.clientHeight
           + ' bottomGap=' + Math.round(p.scrollHeight - p.scrollTop - p.clientHeight);
        break;
      }
      p = p.parentElement;
    }
  }
  // 3) 首条评论结构签名
  let sig = '';
  const it = visItems[0] || items[0];
  if (it) {
    const attrs = [...it.attributes]
      .map(a => a.name + '=' + String(a.value).slice(0, 22)).join(' ');
    const desc = [];
    const nodes = it.querySelectorAll('*');
    for (let i = 0; i < nodes.length && desc.length < 16; i++) {
      const n = nodes[i];
      const bits = [];
      const de = n.getAttribute && n.getAttribute('data-e2e');
      if (de) bits.push('@' + de);
      if (n.id) bits.push('#' + String(n.id).slice(0, 24));
      if (n.attributes) {
        for (const a of n.attributes) {
          if (/^\d{12,}$/.test(a.value)) {
            bits.push('$' + a.name + '=' + a.value.slice(0, 19));
            break;
          }
        }
      }
      const cn = typeof n.className === 'string' ? n.className : '';
      if (/name|nick|user|author/i.test(cn)) bits.push('.' + cn.slice(0, 26));
      if (bits.length) desc.push(n.tagName + '[' + bits.join(',') + ']');
    }
    sig = it.tagName + ' ' + attrs.slice(0, 220)
        + ' :: ' + desc.join(' ').slice(0, 420);
  }
  // 4) 前 6 条文本样本(nick/cid 用宽选择器+长数字属性兜底)
  const samples = visItems.slice(0, 6).map(el => {
    let nick = '';
    const ne = el.querySelector(
      '[data-e2e="comment-username"],[data-e2e*="user-name"],'
      + 'a[href*="/user/"],[class*="username" i],[class*="nickname" i],'
      + '[class*="author" i],[class*="comment-name" i]');
    if (ne) nick = (ne.innerText || ne.textContent || '').trim().slice(0, 12);
    let oc = '';
    if (el.attributes) {
      for (const a of el.attributes) {
        if (/^\d{12,}$/.test(a.value)) { oc = a.name + ':' + a.value.slice(0, 19); break; }
      }
    }
    if (!oc && el.querySelectorAll) {
      const kids = el.querySelectorAll('*');
      for (let i = 0; i < kids.length && i < 100; i++) {
        if (!kids[i].attributes) continue;
        for (const a of kids[i].attributes) {
          if (/^\d{12,}$/.test(a.value)) { oc = a.name + ':' + a.value.slice(0, 19); break; }
        }
        if (oc) break;
      }
    }
    const r = el.getBoundingClientRect();
    const txt = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 50);
    return '[(' + (r.width | 0) + 'x' + (r.height | 0) + ') nick=' + nick
         + ' ' + oc + '] ' + txt;
  }).join(' | ');
  return JSON.stringify({ n: items.length,
                          sort: [...new Set(sortLabels)],
                          scroll: sm || 'none',
                          sig: sig.slice(0, 600),
                          samples: samples.slice(0, 600) });
}
"""



async def fetch_comments(mgr: BrowserManager, identity: Identity, aweme_id: str,
                         known_cids: Set[str], max_scrolls: int = 6,
                         settle_ms: int = 1600, block_media: bool = True,
                         context=None, xsec_token: str = "",
                         ) -> Tuple[List[dict], str]:
    """打开作品详情页,滚动评论容器翻页,拦截评论列表接口收集评论原始 JSON。
    返回 (新评论原始列表, error)。注意:抖音评论默认按热度排序,非严格时间序,
    只能尽量翻页扫到前若干页的新评论。
    """
    collected: Dict[str, dict] = {}
    error = ""
    page = (await context.new_page() if context is not None
            else await mgr.new_page(identity, block_media))

    async def on_response(resp):
        if COMMENT_API in resp.url:
            try:
                data = await resp.json()
            except Exception:
                return
            for c in (data.get("comments") or []):
                cid = str(c.get("cid") or "")
                if cid:
                    collected[cid] = c

    page.on("response", on_response)
    try:
        url = f"https://www.douyin.com/video/{aweme_id}"
        if xsec_token:
            url += "?xsec_source=pc_user&xsec_token=" + quote(xsec_token)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.evaluate(_SCROLL_COMMENTS)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:        # 本次没翻出新评论
                stagnant += 1
                if stagnant >= 2:               # 连续两次到底,停
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


def _dig_danmaku_list(data, depth: int = 0) -> list:
    """从播放页/创作中心响应中递归提取弹幕数组。"""
    if depth > 5:
        return []
    if isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
        if rows and any(any(k in x for k in (
                 "danmaku_id", "barrage_id", "bullet_id", "content",
                 "text", "danmaku_text", "time_point", "video_time", "offset_time"))
                for x in rows):
            return rows
        for value in data:
            found = _dig_danmaku_list(value, depth + 1)
            if found:
                return found
        return []
    if not isinstance(data, dict):
        return []
    for key in ("danmaku_list", "barrage_list", "bullet_list", "danmakus",
                "barrages", "items", "list", "data"):
        value = data.get(key)
        found = _dig_danmaku_list(value, depth + 1)
        if found:
            return found
    for value in data.values():
        if isinstance(value, (dict, list)):
            found = _dig_danmaku_list(value, depth + 1)
            if found:
                return found
    return []


_PROBE_DANMAKU_JS = """async (options) => {
  const video = document.querySelector('video');
  if (!video) {
    window.scrollBy(0, 800);
    return { ok: false, duration: 0 };
  }
  const cfg = (options && typeof options === 'object') ? options : {};
  try { await video.play(); } catch (_) {}
  await new Promise(resolve => setTimeout(resolve, 180));
  const durationHint = Number(cfg.duration || 0);
  const duration = Number.isFinite(video.duration) && video.duration > 0
    ? video.duration : durationHint;
  const start = Math.max(0, Number(cfg.start_ms || 0) / 1000);
  const requestedEnd = Number(cfg.end_ms || 0) / 1000;
  const end = Math.max(start, Math.min(duration || requestedEnd || start, requestedEnd > 0 ? requestedEnd : (duration || start)));
  const step = Math.max(0.25, Number(cfg.step_seconds || 1));
  const maxPoints = Math.max(1, Number(cfg.max_points || 120));
  const span = Math.max(0, end - start);
  const actualStep = span > 0 ? Math.max(step, span / Math.max(1, maxPoints - 1)) : step;
  const points = [];
  if (span <= 0) {
    points.push(start);
  } else {
    for (let point = start; point <= end + 0.01 && points.length < maxPoints; point += actualStep) {
      points.push(Math.min(end, point));
    }
    if (points[points.length - 1] < end - 0.01 && points.length < maxPoints) points.push(end);
  }
  try { await video.play(); } catch (_) {}
  for (const point of points) {
    try {
      if (duration > 0) video.currentTime = Math.max(0, Math.min(duration - .05, point));
      video.dispatchEvent(new Event('timeupdate'));
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  try { video.pause(); } catch (_) {}
  return { ok: true, duration, points: points.length, start, end };
}"""


def _is_danmaku_url(url: str, creator: bool = False) -> bool:
    low = (url or "").lower()
    if creator and "creator.douyin.com" not in low:
        return False
    return (DANMAKU_API in low or "/danmaku/" in low
            or "danmaku/get" in low or "barrage" in low)


def _danmaku_position_ms(row: dict) -> int:
    for key in ("video_time_ms", "position_ms", "time_ms", "offset_time",
                "offsetTime", "video_offset"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return max(0, int(float(value)))
            except (TypeError, ValueError):
                pass
    for key in ("time_point", "video_time", "timepoint", "position"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return max(0, int(float(value) * 1000))
            except (TypeError, ValueError):
                pass
    return 0


async def fetch_danmaku(mgr: BrowserManager, identity: Identity, aweme_id: str,
                        known_ids: Set[str], duration: int = 0,
                        max_rounds: int = 4, settle_ms: int = 1800,
                        block_media: bool = False, start_ms: int = 0,
                        end_ms: int = 0, step_seconds: float = 1.0,
                        max_points: int = 120, max_items: int = 0
                        ) -> Tuple[List[dict], str]:
    """打开公开视频页，拦截播放器弹幕接口并按视频时间点收集弹幕。"""
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if not _is_danmaku_url(resp.url):
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for row in _dig_danmaku_list(data):
            key = danmaku_key(row)
            if key:
                collected[key] = row

    page.on("response", on_response)
    try:
        await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        attempts = max(1, min(max_rounds, 2 if step_seconds > 0 else 8))
        for _ in range(attempts):
            before = len(collected)
            try:
                await page.evaluate(_PROBE_DANMAKU_JS, {
                    "duration": duration, "start_ms": max(0, start_ms),
                    "end_ms": max(0, end_ms),
                    "step_seconds": max(0.25, float(step_seconds or 1)),
                    "max_points": max(1, int(max_points or 120)),
                })
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            if step_seconds > 0 and collected:
                break
    except Exception as e:
        error = f"打开作品页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error:
        error = "未拦截到视频弹幕(可能未开启弹幕/页面未加载/接口已改版)"
    new = [row for key, row in collected.items() if key not in known_ids]
    new.sort(key=lambda row: (_danmaku_position_ms(row), danmaku_key(row)))
    if max_items > 0:
        new = new[:max_items]
    return new, error


async def fetch_creator_danmaku(mgr: BrowserManager, identity: Identity,
                                known_ids: Set[str], page_url: str,
                                aweme_id: str = "", max_scrolls: int = 8,
                                settle_ms: int = 1600,
                                block_media: bool = True, max_items: int = 0
                                ) -> Tuple[List[dict], str]:
    """打开创作中心弹幕管理页，拦截弹幕列表接口。

    创作中心页面/接口属于实验性网页能力，页面地址和字段变化集中在此处适配。
    aweme_id 非空时只保留目标作品；为空时返回账号范围内的弹幕。
    """
    collected: Dict[str, dict] = {}
    error = ""
    capture_path = os.environ.get("CREATORHUB_DANMAKU_CAPTURE", "").strip()
    captured_requests: list[dict] = []
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if not _is_danmaku_url(resp.url, creator=True):
            return
        if capture_path:
            request = resp.request
            captured_requests.append({
                "url": request.url,
                "method": request.method,
                "post_data": request.post_data or "",
                "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
            })
        try:
            data = await resp.json()
        except Exception:
            return
        for row in _dig_danmaku_list(data):
            if max_items > 0 and len(collected) >= max_items:
                break
            row_aweme = str(row.get("aweme_id") or row.get("item_id")
                            or row.get("group_id") or row.get("object_id") or "")
            if aweme_id and row_aweme and row_aweme != str(aweme_id):
                continue
            key = danmaku_key(row)
            if key:
                collected[key] = row

    page.on("response", on_response)
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "/login" in page.url or "passport" in page.url:
            error = "创作者登录态已失效,请重新创作者登录"
        else:
            stagnant = 0
            for _ in range(max(1, min(max_scrolls, 20))):
                before = len(collected)
                try:
                    await page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                if len(collected) == before:
                    stagnant += 1
                    if stagnant >= 2:
                        break
                else:
                    stagnant = 0
            if not collected:
                error = error or "未拦截到创作中心弹幕(页面/接口可能已改版)"
    except Exception as e:
        error = f"打开创作中心弹幕页失败: {e!r}"
    finally:
        if capture_path and captured_requests:
            try:
                with open(capture_path, "w", encoding="utf-8") as f:
                    json.dump(captured_requests, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                print(f"[creator-danmaku] capture dump failed: {exc!r}")
        try:
            await page.close()
        except Exception:
            pass

    new = [row for key, row in collected.items() if key not in known_ids]
    return new, error


# ── 抖音发评论(浏览器自动化)──
# 评论输入框 / 发送按钮选择器(抖音改版时改这里。data-e2e 较稳,排前)
_COMMENT_INPUT = [
    '[data-e2e="comment-input"] [contenteditable]:not([contenteditable="false"])',
    '[data-e2e="comment-input"]',
    'div.comment-input-inner [contenteditable]:not([contenteditable="false"])',
    'div[data-e2e="comment-publish"] [contenteditable]:not([contenteditable="false"])',
    '.comment-input [contenteditable]:not([contenteditable="false"])',
    'div[contenteditable][data-line-wrapper]',
    'div[contenteditable]:not([contenteditable="false"])',
]
_COMMENT_SUBMIT = [
    '[data-e2e="comment-publish"]',
    'div.comment-input-area button:has-text("发送")',
    'button:has-text("发送")',
    'span:has-text("发送")',
    # 评论区常见结构:右侧抽屉底部 toolbar 里的发送按钮
    '[class*="comment"] [class*="send" i]',
    '[class*="comment"] [class*="publish" i]',
    'div[contenteditable="true"] ~ button',
    'div[contenteditable="true"] + button',
]

# 在页面端严格筛选"真正可点的发送按钮"并打 data-mmm-send 标记。
# 教训(v1.5.4): Python 侧 span:has-text("发送") 会命中所有包含该文本
# 的祖先元素+隐藏预渲染节点,每个 click 空等 3s,累计可达 2 分钟假死。
# 页面端一次性用 可见性+未禁用+中心点不被遮挡 三重条件筛准,Python 只点
# 被标记的那一个。返回 {found, enabled, tag, text} 便于失败时诊断。
_FIND_SEND_BUTTON = r"""
() => {
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden'
        || (+st.opacity || 1) < 0.1) return false;
    // 中心点遮挡检测:按钮上有遮罩层时点击会落空
    try {
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const top = document.elementFromPoint(cx, cy);
      if (top && top !== el && !el.contains(top)
          && !(top.contains && top.contains(el))) return false;
    } catch (e) {}
    return true;
  };
  // v1.5.6:新版发送按钮是"红色向上箭头"svg,无文字。用颜色判激活态:
  // 抖音红约 rgb(254,44,84);灰色为未激活。
  const isRed = el => {
    try {
      const probe = [el, el.querySelector('svg'), el.querySelector('xg-icon'),
                     el.querySelector('i, span')].find(Boolean);
      const st = getComputedStyle(probe || el);
      const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(st.color || '');
      if (m && +m[1] > 150 && +m[1] - +m[2] > 55 && +m[1] - +m[3] > 55)
        return true;
      const mf = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(st.fill || '');
      if (mf && +mf[1] > 150 && +mf[1] - +mf[2] > 55 && +mf[1] - +mf[3] > 55)
        return true;
    } catch (e) {}
    return false;
  };
  const disabled = el => {
    if (el.disabled) return true;
    if (el.getAttribute && el.getAttribute('aria-disabled') === 'true')
      return true;
    const cls = (typeof el.className === 'string'
                 ? el.className : '').toLowerCase();
    if (/disabled|forbid|forbidden|ban(ned)?|gray|grey|disable/.test(cls))
      return true;
    return false;
  };
  document.querySelectorAll('[data-mmm-send]')
    .forEach(el => el.removeAttribute('data-mmm-send'));
  // 以编辑器为锚点向上找输入区容器
  const editor = document.querySelector('[data-mmm-editor="1"]');
  let box = null;
  let edRect = null;
  if (editor) {
    try { edRect = editor.getBoundingClientRect(); } catch (e) { edRect = null; }
    // v1.5.8 关键:下方面板的内联回复框插在评论项内部,真发送按钮(文字
    // "发送"/红箭头)往往在 editor.parentElement 之上 2-4 层,而评论项操作
    // 栏里的点赞红心是 parentElement 的兄弟。box 取小了,红心会以 box 域
    // 候选身份高分赢过真发送(点击→内联框失焦收起、零网络请求)。
    // 改为:找"同时包含编辑器与权威发送控件"的最近祖先作为表单容器,
    // 操作栏图标落在 box 外→走全局规则→被 inOtherComment 直接排除。
    const hasAuthSend = (p) => {
      if (!p || typeof p.querySelectorAll !== 'function') return false;
      try {
        if (p.querySelector('[data-e2e="comment-publish"]')) return true;
        if (p.querySelector(
            '[class*="sendbtn" i],[class*="publishbtn" i],'
            + '[class*="send-btn" i],[class*="send-button" i],'
            + '[class*="publish-button" i],[class*="send-arrow" i],'
            + '[class*="publish-arrow" i],[class*="submit" i]')) return true;
        let hit = false;
        p.querySelectorAll('button,[role="button"],a,[tabindex],span,div')
          .forEach(e => {
            if (hit || e === editor) return;
            if ((e.innerText || '').trim() !== '发送') return;
            let rr;
            try { rr = e.getBoundingClientRect(); }
            catch (err) { return; }
            if (rr.width >= 4 && rr.height >= 4) hit = true;
          });
        return hit;
      } catch (e) { return false; }
    };
    let p = editor;
    for (let i = 0; i < 10 && p; i++) {
      if (hasAuthSend(p)) { box = p; break; }
      p = p.parentElement;
    }
    if (!box) {
      p = editor;
      for (let i = 0; i < 6 && p; i++) {
        const de = p.getAttribute && p.getAttribute('data-e2e') || '';
        const cls = typeof p.className === 'string' ? p.className : '';
        if (de === 'comment-publish' || de === 'comment-input'
            || /comment-input|input-area|commentinput|publishbar|sendbar/i.test(cls)) {
          box = p; break;
        }
        p = p.parentElement;
      }
    }
    if (!box) box = editor.parentElement;
  }
  const cands = [];
  const push = (el, scope) => {
    if (el && !cands.some(c => c.el === el))
      cands.push({ el, scope: scope });
  };
  const collect = (root, scope) => {
    if (!root) return;
    root.querySelectorAll('[data-e2e="comment-publish"]')
        .forEach(el => push(el, scope));
    // v1.5.9:data-e2e 不能用 *=publish——"发布时间"元素的 data-e2e 形如
    // publish-time/video-publish-time,B 机实测它被当成发送按钮点中
    // (零请求+内联框收起)。只收精确 comment-publish 或 -publish/-send
    // 结尾的语义节点;class 侧补 send-button/publish-button。
    root.querySelectorAll(
      '[class*="sendbtn" i], [class*="publishbtn" i], [class*="send-btn" i],'
      + ' [class*="send-button" i], [class*="publish-button" i],'
      + ' [class*="submit" i], [data-e2e$="-publish" i],'
      + ' [data-e2e$="-send" i]'
    ).forEach(el => push(el, scope));
    // 表单容器内的纯 class 箭头也收(B 机新版发送箭头是无 svg/无文字的
    // span,仅靠 class 名识别);仅限 box 域,全局箭头仍受邻近/红色规则约束
    if (scope === 'box') {
      root.querySelectorAll('[class*="arrow" i]')
        .forEach(el => push(el, scope));
    }
    root.querySelectorAll('button, [role="button"], a, [tabindex]')
      .forEach(el => {
        const t = (el.innerText || '').trim();
        if (t === '发送' || t === '发布') { push(el, scope); return; }
        // 图标按钮:无文字但内含 svg/xg-icon/img/css 图标/箭头(红色向上
        // 箭头在新版 DOM 里可能是带 class 的 span 而非 svg)
        const onlyIcon = t.length === 0
          && el.querySelector(
               'svg, xg-icon, img, i[class*="icon" i], [class*="icon" i],'
               + ' [class*="arrow" i]');
        if (onlyIcon) {
          const r = el.getBoundingClientRect();
          if (r.width >= 10 && r.width <= 120
              && r.height >= 10 && r.height <= 120) push(el, scope);
        }
      });
    // 纯文本"发送"也可能挂在 span/p 等非按钮叶子上:只收无元素子节点、
    // 尺寸像按钮的叶子,避免把整行容器当按钮。
    root.querySelectorAll('span, div, p, b, em, font, label').forEach(el => {
      try {
        if (el.children && el.children.length) return;
        const t = (el.innerText || el.textContent || '').trim();
        if (t !== '发送') return;
        const r = el.getBoundingClientRect();
        if (r.width >= 8 && r.width <= 80
            && r.height >= 8 && r.height <= 60) push(el, scope);
      } catch (e) {}
    });
  };
  if (box) collect(box, 'box');
  collect(document, 'global');   // 容器扫描不全时全局兜底
  let best = null, bestScore = -1;
  // 全局红图标与输入框的几何邻近判定(v1.5.7):页面上红色图标很多
  // (已赞红心/关注按钮等),全局兜底只接受"就在输入框那一行"的图标。
  const nearEditor = (el) => {
    if (!edRect || edRect.width < 4) return true;  // 无锚点时不限制(含离线桩)
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (r.width < 4) return false;
    const vgap = Math.max(0, Math.max(edRect.top - r.bottom,
                                      r.top - edRect.bottom));
    // 垂直间距 <=120px(同一输入行),或水平区间与输入框重叠
    return vgap <= 120
      || (r.bottom > edRect.top && r.top < edRect.bottom);
  };
  for (const c of cands) {
    const el = c.el;
    if (!vis(el)) continue;
    const red = isRed(el);
    const t = (el.innerText || '').trim();
    const cls = typeof el.className === 'string' ? el.className : '';
    const de = (el.getAttribute && el.getAttribute('data-e2e')) || '';
    const hasIcon = !!el.querySelector('svg, xg-icon, img');
    // v1.5.9 硬排除元信息元素:B 机实测 data-e2e="publish-time"的
    // "发布时间:202x-xx-xx"SPAN 被旧 *=publish 选择器收入并被语义正则
    // 判成发送按钮,点击后零请求、内联框失焦收起。
    if (t && /时间|日期|发布于|收藏|分享|举报|删除/.test(t)) continue;
    if (t && t !== '发送' && t !== '发布'
        && de !== 'comment-publish') continue;
    const sig = (cls + ' ' + de).toLowerCase();
    if (de !== 'comment-publish'
        && /(^|[^a-z])(publish|send|submit)/.test(sig)
        && /(time|date|meta|count|nick|name)/.test(sig)) continue;
    // 语义化按钮:有权威标记/"发送"文字/发送相关 class(词边界,避开
    // publisher/publish-time 这类误伤)。纯图标箭头没有这些信号,只能靠
    // 颜色——非红(灰)即未激活,红色才可点。
    const semantic = de === 'comment-publish' || t === '发送' || t === '发布'
      || (/(^|[^a-z])(send|publish|submit)(btn|button)?([^a-z]|$)/i.test(sig)
          && !/(time|date|nick|name|count|meta|text|label)/i.test(sig));
    // v1.5.7 防误点:全局范围里、藏在评论项内的无语义红图标=点赞红心等,
    // 绝不允许当发送按钮(box 域内不受限——内联回复框本就插在评论项里)
    const inOtherComment = c.scope !== 'box' && semantic === false
      && el.closest && el.closest('[data-e2e="comment-item"]')
      && !(box && box.contains && box.contains(el));
    if (inOtherComment) continue;
    // 全局无语义纯图标还必须几何邻近输入框
    if (c.scope !== 'box' && !semantic && hasIcon && !nearEditor(el)) continue;
    const dis = red ? false
      : (disabled(el) || (!semantic && t === '' && hasIcon));
    let score = c.scope === 'box' ? 10 : 0;
    if (de === 'comment-publish') score += 30;
    if (/发送/.test(t)) score += 20;
    // 红色加分:box 域/语义按钮 +40;来源不明的全局纯图标压到 +25,
    // 避免远处的红色图标赢过输入区里真正的发送控件
    score += red ? ((c.scope === 'box' || semantic) ? 40 : 25) : 0;
    if (!dis) score += 25;
    if (score > bestScore) {
      bestScore = score;
      best = { el, dis, red, score, sem: semantic };
    }
  }
  if (best) {
    best.el.setAttribute('data-mmm-send', '1');
    return { found: true, enabled: !best.dis, red: best.red,
             sem: !!best.sem,
             tag: best.el.tagName || '',
             text: (best.el.innerText || '').trim().slice(0, 10),
             cls: (typeof best.el.className === 'string'
                   ? best.el.className : '').slice(0, 60),
             nCand: cands.length };
  }
  return { found: false };
}
"""
# 抖音发表评论接口(权威成功判据:拦截它的响应看 status_code)
_PUBLISH_API = "aweme/v1/web/comment/publish"
# 发表后可能弹出的人工验证(短信验证码/扫码/滑块),需用户在弹出的有头窗口
# 手动完成——不能也不该绕过;出现时延长等待,验证通过后回包会正常到达。
_COMMENT_VERIFY_KW = ("接收短信验证码", "短信验证码", "为确保是本人操作", "输入验证码",
                      "安全验证", "完成验证", "拖动滑块", "使用原设备扫码", "身份验证")


async def _visible_any_text(page, keywords) -> str:
    """返回第一个当前可见的关键词文案(无则空串);用于轮询,不阻塞。"""
    for kw in keywords:
        try:
            if await page.get_by_text(kw, exact=False).first.is_visible():
                return kw
        except Exception:
            continue
    return ""

# 判断评论面板是否"真正可见地展开",并给可见可写的评论输入框打标记。
# 关键教训:不能用 DOM 存在判断评论区已展开——抖音作品页会预渲染隐藏的
# 评论区 DOM(comment-item / contenteditable 都在 DOM 里但 CSS 隐藏),
# 只数 querySelectorAll().length 会在页面刚加载时就误判"已展开",从而永远
# 不点评论按钮、面板一直收起。这里严格检查可见性,并排除顶部搜索框。
_FIND_VISIBLE_EDITOR = """
() => {
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width < 12 || r.height < 12) return false;
    if (typeof getComputedStyle === 'function') {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden'
          || (+st.opacity || 1) < 0.1) return false;
    }
    return true;
  };
  document.querySelectorAll('[data-mmm-editor]')
    .forEach(el => el.removeAttribute('data-mmm-editor'));
  const cands = [...document.querySelectorAll(
    // 优先真正的可编辑元素(contenteditable="" 空值也算 true),
    // 容器[data-e2e="comment-input"]放最后兜底——对容器 focus/execCommand
    // 不生效,会导致"焦点看似进去了但打字全丢"
    '[data-e2e="comment-input"] [contenteditable]:not([contenteditable="false"]), '
    + '[data-e2e="comment-publish"] [contenteditable]:not([contenteditable="false"]), '
    + '.comment-input [contenteditable]:not([contenteditable="false"]), '
    + '.comment-input-inner [contenteditable]:not([contenteditable="false"]), '
    + 'div[contenteditable]:not([contenteditable="false"]), '
    + '[data-e2e="comment-input"]')];
  let panelPick = null, anyPick = null;
  for (const el of cands) {
    if (!vis(el)) continue;
    const ph = ((el.getAttribute('data-placeholder') || '')
      + (el.getAttribute('placeholder') || '')
      + (el.getAttribute('aria-label') || '')
      + (typeof el.className === 'string' ? el.className : '')).toLowerCase();
    if (ph.indexOf('搜索') >= 0 || ph.indexOf('search') >= 0) continue;
    const inPanel = el.closest(
      '[data-e2e="comment-list"], [data-e2e="video-comment"], '
      + '[data-e2e="comment-input"], [data-e2e="comment-publish"], '
      + '[class*="commentdetail" i], [class*="commentlist" i], '
      + '[class*="comment-main" i], [class*="commentinput" i]');
    const looksComment = ph.indexOf('友善') >= 0 || ph.indexOf('评论') >= 0
      || ph.indexOf('回复') >= 0 || ph.indexOf('说点什么') >= 0
      || el.getAttribute('data-e2e') === 'comment-input';
    if ((inPanel || looksComment) && !panelPick) panelPick = el;
    if (!anyPick) anyPick = el;
  }
  const target = panelPick || anyPick;
  if (target) target.setAttribute('data-mmm-editor', '1');
  const items = [...document.querySelectorAll('[data-e2e="comment-item"]')]
    .filter(vis).length;
  return { editor: !!target, items: items };
}
"""

# 点「回复」后定位"真正的回复编辑器"(v1.5.6)。两种页面布局机制不同:
#  - 右侧抽屉:底部评论框整体切到回复模式(框内出现"回复@xx ×"标签)
#  - 下方面板:目标评论下方新插入一个内联 contenteditable,底部主框不变
# 旧代码只找一次、且把底部主框当回复框→键盘事件进了新内联框(用户能看到
# 字),程序却读主框 innerText 为空→误报"无法写入内容"。本 JS 按优先级找:
# ①目标评论项内可见可编辑元素 ②目标项邻近(向上4层容器、项之后插入区)
# ③当前焦点元素 ④底部框——仅当它确实进入回复模式(容器文本含"回复@")。
# 命中打 data-mmm-editor=1 并返回 {found, how, top, bottom}。
_FIND_REPLY_EDITOR = """
() => {
  const mark = document.querySelector('[data-mmm-target="1"]');
  if (!mark) return { found: false, reason: 'no-target-mark' };
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width < 20 || r.height < 8) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden'
        || (+st.opacity || 1) < 0.1) return false;
    return true;
  };
  const editables = root => [
    ...root.querySelectorAll(
      '[contenteditable]:not([contenteditable="false"])')];
  const ce = el => el && (el.isContentEditable
    || el.getAttribute('contenteditable') !== null
       && el.getAttribute('contenteditable') !== 'false');
  document.querySelectorAll('[data-mmm-editor]')
    .forEach(el => el.removeAttribute('data-mmm-editor'));
  const pick = (el, how) => {
    el.setAttribute('data-mmm-editor', '1');
    const r = el.getBoundingClientRect();
    return { found: true, how,
             top: Math.round(r.top), bottom: Math.round(r.bottom) };
  };
  let el = null;
  // ① 目标评论项内部
  el = editables(mark).find(vis);
  if (el) return pick(el, 'inside-item');
  // ② 邻近:目标项向上最多 4 层的容器,取位置在目标项下半部附近的编辑器
  let box = mark;
  for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement;
  const mr = mark.getBoundingClientRect();
  let near = null, nearGap = 1e9;
  for (const cand of editables(box)) {
    if (!vis(cand)) continue;
    const cr = cand.getBoundingClientRect();
    // 在目标项下方 400px 内(新插入的回复行),或垂直区间与目标项重叠
    const gap = cr.top - mr.bottom;
    if ((gap >= -20 && gap < 400)
        || (cr.top < mr.bottom && cr.bottom > mr.top)) {
      if (gap < nearGap) { nearGap = gap; near = cand; }
    }
  }
  if (near) return pick(near, 'nearby-item');
  // ③ 当前焦点元素是可编辑且可见(点回复后抖音常自动聚焦新框)
  const ae = document.activeElement;
  if (ce(ae) && vis(ae)) return pick(ae, 'active-focus');
  // ④ 底部主框:仅在确认进入回复模式时认——其输入区容器内含"回复@"标签
  const mains = editables(document).filter(vis);
  for (const cand of mains) {
    let p = cand;
    for (let i = 0; i < 6 && p; i++) {
      const t = p.innerText || '';
      if (/回复\\s*@/.test(t) && t.length < 200) return pick(cand, 'main-reply-mode');
      p = p.parentElement;
    }
  }
  return { found: false, reason: 'no-reply-editor',
           activeTag: ae ? (ae.tagName || '') : '',
           activeCE: !!(ae && ce(ae)),
           nEditable: mains.length };
}
"""

# 在目标评论项内找"可见可点的回复按钮"并打 data-mmm-replybtn 标记。
# 教训:Playwright 直接对 [data-e2e=comment-reply].first click,若首个
# 匹配是隐藏/视口外元素会自动等待到 timeout(下方面板每条约4-8s空耗)。
# 页面端按 可见+中心点不遮挡 筛准,Python 只点打标元素。
_FIND_REPLY_BUTTON = """
() => {
  const mark = document.querySelector('[data-mmm-target="1"]');
  if (!mark) return { found: false, reason: 'no-target-mark' };
  document.querySelectorAll('[data-mmm-replybtn]')
    .forEach(el => el.removeAttribute('data-mmm-replybtn'));
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!r || r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden'
        || (+st.opacity || 1) < 0.1) return false;
    return true;
  };
  const cands = [];
  mark.querySelectorAll('[data-e2e="comment-reply"]').forEach(el => cands.push(el));
  // 文本恰为"回复"的叶子元素(排除"展开N条回复"——含"条"字;排除"回复列表")
  mark.querySelectorAll('button, [role="button"], span, div, a').forEach(el => {
    const t = (el.innerText || '').trim();
    if (t === '回复' || t === '回 复') cands.push(el);
  });
  for (const el of cands) {
    if (!vis(el)) continue;
    // 中心点遮挡检测
    try {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(
        r.left + r.width / 2, r.top + r.height / 2);
      if (top && top !== el && !el.contains(top)
          && !(top.contains && top.contains(el))) continue;
    } catch (e) {}
    el.setAttribute('data-mmm-replybtn', '1');
    return { found: true,
             tag: el.tagName || '',
             text: (el.innerText || '').trim().slice(0, 10) };
  }
  return { found: false, reason: 'no-visible-reply-btn', n: cands.length };
}
"""

# 输入后多路读回:下方面板点回复后焦点进的是新内联框,而 editor 句柄可能
# 指向底部主框,只读 editor.innerText 会误报"无法写入"(用户肉眼已看到字)。
# 依次检查:打标编辑器 / 当前焦点元素 / 面板内所有可见可编辑元素,把真正
# 含文本的元素重新打 data-mmm-editor 标记(供 Python 切换句柄+邻近找发送
# 按钮),返回 {found, where}。
_READBACK_EDITOR = """
(text) => {
  const want = (text || '').slice(0, 8);
  const vis = el => {
    if (!el) return false;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return false; }
    return r.width >= 20 && r.height >= 8;
  };
  const has = el => {
    const v = (el.innerText || el.textContent || '').trim();
    return want && v.indexOf(want) !== -1;
  };
  const retag = (el, where) => {
    document.querySelectorAll('[data-mmm-editor]')
      .forEach(n => n.removeAttribute('data-mmm-editor'));
    el.setAttribute('data-mmm-editor', '1');
    return { found: true, where,
             value: (el.innerText || el.textContent || '').trim().slice(0, 30) };
  };
  const marked = document.querySelector('[data-mmm-editor="1"]');
  if (marked && has(marked)) return retag(marked, 'marked');
  const ae = document.activeElement;
  if (ae && vis(ae) && has(ae)) return retag(ae, 'active');
  const all = document.querySelectorAll(
    '[contenteditable]:not([contenteditable="false"])');
  for (const el of all) {
    if (vis(el) && has(el)) return retag(el, 'panel-scan');
  }
  return { found: false,
           activeVal: ae ? ((ae.innerText || ae.textContent || '').trim().slice(0, 30)) : '',
           nEditable: all.length };
}
"""

# 发表后验证(v1.5.7):不依赖网络拦截的第二成功判据。v1.5.6 在新版页面
# 点发送后未捕获 comment/publish 回包,一律标 uncertain——但实际可能已
# 发出(回复 DOM 已插入),也可能点偏按钮(编辑器仍有字)。本 JS:
#  posted=true  回复文本已出现在评论项/回复列表中,且编辑器已清空或消失
#               (排除正在编辑的行,避免把"刚输入"误判成"已发出")
# 同时返回编辑器存活/内容状态,供 Python 决定是否补点发送。
_VERIFY_COMMENT_POST = r"""
(text) => {
  const norm = s => (s || '').replace(/[\uFE00-\uFE0F\u200B-\u200D\uFEFF]/g, '').replace(/\s+/g, '');
  const want = norm((text || '').slice(0, 12));
  const logicalOf = (root) => {
    let out = '';
    const takeAlt = (node) => {
      out += node.getAttribute('alt') || node.getAttribute('title')
          || node.getAttribute('aria-label') || node.getAttribute('data-name')
          || node.getAttribute('data-emoji-name') || '';
    };
    const walk = (node) => {
      if (node.nodeType === 3) { out += node.nodeValue || ''; return; }
      if (node.nodeType !== 1) return;
      const tag = node.tagName;
      const cls = (typeof node.className === 'string' ? node.className : '');
      if (tag === 'IMG' || tag === 'XG-ICON') { takeAlt(node); return; }
      if (node.hasAttribute && (node.hasAttribute('data-emoji')
          || node.hasAttribute('data-emoji-name')
          || node.hasAttribute('data-name')
          || /emoji|sticker/i.test(cls))) { takeAlt(node); return; }
      const kids = node.childNodes || [];
      for (let i = 0; i < kids.length; i++) walk(kids[i]);
    };
    walk(root);
    return out;
  };
  const ed = document.querySelector('[data-mmm-editor="1"]');
  const alive = !!(ed && document.contains(ed));
  const edVal = ed ? norm(ed.innerText || ed.textContent || '') : '';
  let posted = false;
  // 评论项 + 回复列表/回复项容器(回复挂在目标评论项内的折叠区)
  let roots = [];
  try {
    roots = document.querySelectorAll(
      '[data-e2e="comment-item"], [data-e2e*="reply-list"],'
      + ' [data-e2e*="reply-item"], [class*="reply-item" i],'
      + ' [class*="replylist" i]');
  } catch (e) { roots = []; }
  if (want) {
    for (const root of roots) {
      // 跳过仍含活跃编辑器的行(那是我们刚输入、还没发出的内容)
      if (root.hasAttribute('data-mmm-editor')) continue;
      if (root.querySelector && root.querySelector('[data-mmm-editor="1"]'))
        continue;
      const s = norm(logicalOf(root))
        || norm(root.innerText || root.textContent || '');
      if (s && s.indexOf(want) !== -1) { posted = true; break; }
    }
  }
  // 必须配合"编辑器已清空或消失":防止回复文本碰巧与他人评论重合时误判
  const settled = posted && (!alive || !edVal);
  let nItems = 0, nCE = 0, url = '';
  try {
    nItems = document.querySelectorAll('[data-e2e="comment-item"]').length;
  } catch (e) {}
  try {
    nCE = document.querySelectorAll(
      '[contenteditable]:not([contenteditable="false"])').length;
  } catch (e) {}
  try { url = (location.href || '').slice(-80); } catch (e) {}
  return { posted, settled, alive, empty: !edVal,
           val: edVal.slice(0, 20), nRoots: roots.length,
           items: nItems, ce: nCE, url: url };
}
"""


# 找不到输入框时,导出页面真实结构,便于对症补选择器
_DIAG_INPUTS = """
() => {
  const ce = [];
  document.querySelectorAll('[contenteditable]').forEach(el => {
    ce.push(((el.tagName || '') + '.' + (typeof el.className === 'string' ? el.className : ''))
      .slice(0, 70) + ' | ph=' +
      (el.getAttribute('data-placeholder') || el.getAttribute('placeholder')
       || el.getAttribute('aria-label') || '').slice(0, 30)
       + ' | vis=' + (el.getBoundingClientRect().width | 0));
  });
  const e2e = [];
  document.querySelectorAll('[data-e2e]').forEach(el => {
    const v = el.getAttribute('data-e2e');
    if (v && /comment|input|publish|reply|editor|feed|icon/i.test(v))
      e2e.push(v + '(' + (el.offsetWidth||0) + 'x' + (el.offsetHeight||0) + ')');
  });
  // 额外导出右侧操作栏候选(评论入口可能在里面)
  const btns = [];
  document.querySelectorAll('button, [role="button"], xg-icon, [class*="icon" i]')
    .forEach(el => {
      const t = (el.innerText || '').trim().slice(0, 20);
      const e = el.getAttribute('data-e2e') || '';
      if (t || e) btns.push((e || 'no-e2e') + ':' + (t || 'no-text')
        + '(' + (el.offsetWidth||0) + ')');
    });
  return JSON.stringify({ ce: ce.slice(0, 12),
                          e2e: [...new Set(e2e)].slice(0, 25),
                          btns: [...new Set(btns)].slice(0, 30),
                          url: location.href });
}
"""


async def post_comment_browser(mgr: BrowserManager, identity: Identity, aweme_id: str,
                               content: str, reply_to_text: str = "", headed: bool = True,
                               settle_ms: int = 1800, timeout_ms: int = 12000,
                               require_reply: bool = False,
                               verify_wait_seconds: int = 300,
                               xsec_token: str = "",
                               target_nick: str = "",
                               target_cid: str = "",
                               ) -> Tuple[bool, str]:
    """用账号持久 profile(已含登录态)打开作品页,在评论框输入并发送。
    headed=True:弹真实浏览器窗口(抖音对无头写操作常降级/拦截,有头更稳,且能手动过验证码)。
    成功判据 = 拦截抖音 comment/publish 接口响应的 status_code(0=成功),
    而非"输入框是否清空"(后者会被验证码/频控误判为成功)。
    reply_to_text/target_nick/target_cid 非空:尝试定位该评论、点「回复」内联输入;
    匹配策略:全文→短前缀(防截断)→作者昵称→评论id,失败回退顶层评论。
    返回 (ok, error)。⚠️ 选择器随抖音改版可能失效,集中在 _COMMENT_INPUT/_COMMENT_SUBMIT。"""
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    if require_reply and not (reply_to_text or "").strip():
        return False, "缺少目标评论原文，已跳过回复"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)   # 同 profile 有头窗口(关闭即落盘 Cookie)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)
    # 拦截发表接口响应(权威判据)。seen 在 URL 命中时即置位:即使回包解析
    # 失败,也说明请求已发出、结果未知(uncertain),绝不能当作"未提交"重试。
    pub = {"seen": False, "known": False, "ok": False, "code": None, "msg": "",
           "http": None, "url": "", "raw": ""}

    async def on_response(resp):
        if _PUBLISH_API in resp.url and not pub["seen"]:
            pub["seen"] = True
            try:
                pub["http"] = resp.status
                pub["url"] = resp.url[-60:]
            except Exception:
                pass
            data = None
            raw = ""
            try:
                raw = (await resp.text()).strip().lstrip("\ufeff")
            except Exception:
                raw = ""
            pub["raw"] = raw[:120]
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    try:
                        # 有些响应是 {"status_code":..} 外面包了层或带前缀
                        j = raw.find("{")
                        if j >= 0:
                            data = json.loads(raw[j:])
                    except Exception:
                        data = None
            if isinstance(data, dict):
                pub["known"] = True
                # 抖音发评论成功可能是 status_code==0 或外层 data.status_code
                inner = data.get("data") if isinstance(data.get("data"), dict) else {}
                code = data.get("status_code", inner.get("status_code"))
                pub["code"] = code
                pub["ok"] = code == 0
                pub["msg"] = str(data.get("status_msg")
                                 or inner.get("status_msg") or "")

    page.on("response", on_response)

    # v1.5.7:记录发送窗口内所有 comment 相关 POST 的 URL。若平台改版换了
    # 发表端点(_PUBLISH_API 拦不到),失败信息会带出真实端点,无需再盲猜。
    _req_log: List[str] = []

    async def on_request(req):
        try:
            if req.method == "POST" and "comment" in req.url:
                _req_log.append(str(req.url)[-110:])
                if len(_req_log) > 8:
                    _req_log.pop(0)
        except Exception:
            pass

    page.on("request", on_request)

    # 阶段耗时埋点:远程黑盒排障时,失败信息附带各阶段已用时间,一次就能
    # 看出时间耗在 goto / 翻页 / 点回复 / 输入 / 等发送的哪一段(v1.5.6)。
    _t0 = time.monotonic()
    _stages: List[str] = []

    def _stage(name: str) -> None:
        _stages.append(f"{name}:{time.monotonic() - _t0:.0f}s")

    try:
        _stage("start")
        # 作品页 URL 对 xsec_token 敏感:别人的作品通常需要带 token 才能加载评论区,
        # 自己的作品有时带 token 反而被重定向。但绝不能逐个 goto 全部候选——每次
        # goto 都是一次整页刷新,带 token 被重定向时用户会看到"页面反复刷新",且
        # 每次 goto+停留要十几秒,累计超过 1 分钟。所以:带 token 只试一次,判定
        # 失败立刻回退裸链,最多 2 次 goto。
        base = f"https://www.douyin.com/video/{aweme_id}"
        url_forms = []
        if xsec_token:
            url_forms.append(f"{base}?xsec_source=pc_user&xsec_token={quote(xsec_token)}")
        url_forms.append(base)                      # 裸链兜底(自己的作品常用)

        loaded = False
        last_url = ""
        for u in url_forms:
            await page.goto(u, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(min(settle_ms, 1200))
            last_url = page.url
            if "passport" in last_url or "/login" in last_url:
                return False, "logged_out:账号未登录,无法发评论"
            has_video = await page.evaluate(
                "() => !!document.querySelector('video')")
            if aweme_id in last_url or has_video:
                loaded = True
                break
        if not loaded:
            return False, (
                f"作品页无法加载(尝试了 {len(url_forms)} 种 URL),"
                f"最后停留页面:{last_url[:140]}")
        _stage("loaded")

        # 展开评论区。三条铁律:
        # 1) 不能用 page.evaluate(JS .click())——抖音 React 事件绑在
        #    pointerdown/mousedown,JS .click() 不触发,必须用 Playwright .click()。
        # 2) 不能用 DOM 存在判断"已展开"——抖音预渲染隐藏的评论区 DOM,
        #    comment-item/输入框在 DOM 里但不可见;必须用 _FIND_VISIBLE_EDITOR
        #    的可见性判断。否则页面一加载就误判已展开、永远不点按钮。
        # 3) 面板已展开就绝不再点评论按钮(会切换关闭);也不要 scrollBy
        #    主页面——评论区是右侧抽屉,滚主页面只会切视频/打乱布局。
        _comment_entry_selectors = [
            '[data-e2e="feed-comment-icon"]',     # feed 流评论图标
            '[data-e2e="comment-icon"]',           # 评论图标
            'xg-icon[data-e2e="comment"]',         # 播放器内评论按钮
            '[data-e2e="comment-count"]',          # 评论数
            'div[class*="comment"][class*="icon"]',
            'div[class*="CommentIcon"]',
        ]
        panel_open = False
        for attempt in range(6):
            # 先看可见的评论输入框/评论项
            try:
                st = await page.evaluate(_FIND_VISIBLE_EDITOR) or {}
            except Exception:
                st = {}
            if st.get("editor"):
                panel_open = True
                break                       # 已有可见可写输入框,直接进入输入
            if int(st.get("items") or 0) > 0:
                # 面板已展开(能看到评论)只是输入框还没渲染,等待,绝不点按钮
                panel_open = True
                await page.wait_for_timeout(500)
                continue
            # 面板未展开:第一轮先 hover 视频中央唤起右侧操作栏,再点评论入口
            if attempt == 0:
                try:
                    v = page.locator("video").first
                    if await v.count():
                        await v.hover(timeout=1500)
                        await page.wait_for_timeout(300)
                except Exception:
                    pass
            clicked = False
            for sel in _comment_entry_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=2000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                # 文本回退:只用足够特异的文案,避免裸"评论"误中页面其它元素
                for txt in ["条评论", "写评论", "友善评论"]:
                    try:
                        loc = page.get_by_text(txt, exact=False).first
                        if await loc.count() and await loc.is_visible():
                            await loc.click(timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue
            # 等右侧抽屉滑出动画 + 评论区渲染
            await page.wait_for_timeout(900)
        _stage(f"panel({'open' if panel_open else 'closed'})")

        editor = None
        # 回复模式:先在评论区找到目标评论,点它的「回复」打开内联框。
        # 目标可能是纯表情/手势评论(DOM 里是表情图片而非文字),用页面端
        # 表情感知匹配;找不到就滚动评论区翻页后重试。
        # 注意:只能定位顶层评论——子评论(回复)默认折叠在「展开 N 条回复」
        # 里,发现阶段已不生成子评论目标;页面真实评论数随错误返回,不再用
        # 写死的"约190条"掩盖"评论区根本没加载"的事实。
        if reply_to_text or target_nick or target_cid:
            found = False
            shown_count = 0
            # 翻页找目标评论。两阶段扫描——先默认(最热)排序,滚到底未命中
            # 再切"最新";都未命中则展开折叠的子评论再扫一轮。v1.6.5:
            # B 机 536 条评论(列表高 80464px)现场 top=15196 就结束——
            # 24 轮×一屏步长只能扫 1/5,是轮次跑满而非真到底。改为每阶段
            # 90 轮、每轮 1.2 屏、等待 450ms(最坏 ~40s/阶段,通常目标在
            # 前面会提前命中);"到底"必须同时满足数量不增长+位移<8px+
            # 底部余量 gap<400px,慢网络短暂卡顿不再误判到底。
            per_phase = 90
            _scroll_state_js = (
                "() => {"
                " const vis=el=>{try{const r=el.getBoundingClientRect();"
                "return r.width>=2&&r.height>=2;}catch(e){return true;}};"
                " let item=null;"
                " for (const it of document.querySelectorAll"
                "('[data-e2e=\"comment-item\"]'))"
                " { if (vis(it)) { item=it; break; } }"
                " let el=item;"
                " while (el && el!==document.body) {"
                " const oy=getComputedStyle(el).overflowY;"
                " if ((oy==='auto'||oy==='scroll')"
                " && el.scrollHeight>el.clientHeight+20)"
                " return {top:Math.round(el.scrollTop||0),"
                "gap:Math.max(0,Math.round(el.scrollHeight"
                "-el.scrollTop-el.clientHeight)),win:0};"
                " el=el.parentElement;}"
                " return {top:Math.round(window.scrollY||0),"
                "gap:Math.max(0,Math.round(document.documentElement.scrollHeight"
                "-window.innerHeight-(window.scrollY||0))),win:1};"
                "}")

            async def _scroll_state() -> dict:
                try:
                    return await page.evaluate(_scroll_state_js) or {}
                except Exception:
                    return {}

            async def _scan_one_phase(rounds: int = 0) -> tuple[bool, int, str]:
                last_count = -1
                stale = 0
                hit_why = ""
                last = await _scroll_state()
                for j in range(rounds or per_phase):
                    try:
                        hit_n = await page.evaluate(_FIND_COMMENT_ITEM, {
                            "text": (reply_to_text or "")[:60],
                            "nick": target_nick or "",
                            "cid": target_cid or "",
                        })
                    except Exception:
                        hit_n = [False, 0]
                    hit = bool(hit_n and hit_n[0])
                    cnt = int((hit_n or [False, 0])[1] or 0)
                    if hit:
                        hit_why = str((hit_n or [])[2] or "")
                        return True, cnt, hit_why
                    try:
                        await page.evaluate(_SCROLL_COMMENTS)
                    except Exception:
                        pass
                    await page.wait_for_timeout(450)
                    cur = await _scroll_state()
                    moved = False
                    try:
                        moved = abs(int(cur.get("top", 0))
                                    - int(last.get("top", 0))) >= 8
                    except (TypeError, ValueError):
                        moved = False
                    # 仅当确实接近底部(gap<400px)时,数量与位移双停滞才算
                    # 滚到底;gap 还很大时的停顿只是分页加载慢,继续滚。
                    near_bottom = False
                    try:
                        near_bottom = int(cur.get("gap", 1 << 30)) < 400
                    except (TypeError, ValueError):
                        near_bottom = False
                    last = cur
                    if cnt == last_count and not moved and near_bottom:
                        stale += 1
                    else:
                        stale = 0
                    last_count = cnt
                    if stale >= 4 and j >= 6:
                        break
                return False, max(last_count, 0), hit_why

            sort_note = "hot"
            hit_why = ""
            found, phase_n, hit_why = await _scan_one_phase()
            shown_count = max(shown_count, phase_n)
            if not found:
                # 热度榜到底未命中:尝试切"最新"。可能是平铺 Tab 直接点,
                # 也可能是点"最热"弹出下拉(选项挂 body)→ 全局再点一次。
                switched = False
                try:
                    sr = await page.evaluate(_FIND_COMMENT_SORT, "panel") or {}
                    # 下方布局排序 Tab 可能挂在评论容器之外:面板域找不到
                    # 时全局再扫一次,避免误判 no-sort-ctl 直接放弃最新序。
                    if not sr.get("new") and not sr.get("hot"):
                        sr = await page.evaluate(
                            _FIND_COMMENT_SORT, "any") or {}
                    if sr.get("new"):
                        try:
                            await page.locator(
                                '[data-mmm-sort="new"]').first.click(
                                    timeout=2000, force=True)
                            switched = True
                        except Exception:
                            switched = False
                    if not switched and sr.get("hot"):
                        try:
                            await page.locator(
                                '[data-mmm-sort="hot"]').first.click(
                                    timeout=2000, force=True)
                            await page.wait_for_timeout(500)
                            await page.evaluate(_FIND_COMMENT_SORT, "any")
                            await page.locator(
                                '[data-mmm-sort="new"]').first.click(
                                    timeout=2000, force=True)
                            switched = True
                        except Exception:
                            switched = False
                except Exception:
                    switched = False
                if switched:
                    sort_note = "switched-new"
                    _stage("sort-new")
                    await page.wait_for_timeout(1200)
                    found, phase_n, hit_why = await _scan_one_phase()
                    shown_count = max(shown_count, phase_n)
                else:
                    sort_note = "no-sort-ctl"
                if not found:
                    # 末轮兜底:热度+最新都没命中,目标可能是折叠在「展开
                    # N 条回复」里的子评论。逐个展开折叠线程(v1.6.5:12→24
                    # 个,热帖楼层远多于 12),每展开一个后短扫。
                    expanded = 0
                    for _eb in range(24):
                        try:
                            er = await page.evaluate(_FIND_EXPAND_REPLIES) or {}
                        except Exception:
                            er = {}
                        if not er.get("found"):
                            break
                        try:
                            await page.locator(
                                '[data-mmm-expand="1"]').first.click(
                                    timeout=2000, force=True)
                            expanded += 1
                            await page.wait_for_timeout(500)
                        except Exception:
                            break
                    if expanded:
                        sort_note = "expanded-replies"
                        _stage("expand-replies")
                        found, phase_n, hit_why = await _scan_one_phase(24)
                        shown_count = max(shown_count, phase_n)
            _stage(f"find({sort_note},n={shown_count}"
                   + (f",by={hit_why}" if found and hit_why else "")
                   + ")")
            if found:
                # 命中的可能是隐藏预渲染节点(可见项+500 优先,但目标只存在
                # 于隐藏 DOM 时仍会命中它)。点回复前先 JS 滚到可见,避免
                # Playwright 对隐藏元素空等数秒后超时。
                try:
                    await page.evaluate(
                        "() => { const el = document.querySelector"
                        "('[data-mmm-target=\"1\"]');"
                        " if (el) el.scrollIntoView({block:'center'}); }")
                except Exception:
                    pass
                await page.wait_for_timeout(400)
                try:
                    item = page.locator(
                        '[data-e2e="comment-item"][data-mmm-target="1"]').first
                    # 优先页面端打标的"可见回复按钮":JS 在目标项内找
                    # data-e2e=comment-reply 或文本恰为"回复"的可见可点元素,
                    # 避免 Playwright 在隐藏/视口外按钮上空等超时。
                    reply_marked = False
                    try:
                        rb = await page.evaluate(_FIND_REPLY_BUTTON) or {}
                        reply_marked = bool(rb.get("found"))
                    except Exception:
                        rb = {}
                    if reply_marked:
                        rbtn = page.locator(
                            '[data-mmm-replybtn="1"]').first
                        await rbtn.click(timeout=2500)
                    else:
                        rbtn = item.locator(
                            '[data-e2e="comment-reply"]').first
                        if not await rbtn.count():
                            rbtn = item.get_by_text(
                                "回复", exact=False).first
                        await rbtn.click(timeout=2500)
                    _stage("reply-click")
                    # 下方面板点回复后新内联框是异步插入的,短轮询等待
                    # (6×500ms=3s),用 _FIND_REPLY_EDITOR 严格区分内联框与
                    # 底部主框;右侧抽屉则等主框切到"回复@"模式。
                    reply_info = {}
                    for _ in range(6):
                        reply_info = await page.evaluate(
                            _FIND_REPLY_EDITOR) or {}
                        if reply_info.get("found"):
                            break
                        await page.wait_for_timeout(500)
                    if reply_info.get("found"):
                        marked = page.locator(
                            '[data-mmm-editor="1"]').first
                        if await marked.count():
                            editor = marked
                    _stage(f"reply-editor({reply_info.get('how') or reply_info.get('reason')})")
                except Exception as _re:
                    editor = None  # 点回复异常,无内联框
                    _stage(f"reply-err:{type(_re).__name__}")
                finally:
                    try:
                        await page.evaluate(_CLEAR_TARGET_MARK)
                    except Exception:
                        pass
            if editor is None and require_reply:
                if shown_count <= 0:
                    # 评论区没打开:导出页面真实结构帮助定位评论入口
                    diag = ""
                    try:
                        diag = await page.evaluate(_DIAG_INPUTS)
                    except Exception:
                        pass
                    return False, (
                        f"未找到目标评论回复区：作品页评论区未加载出任何评论"
                        f"(页面URL:{page.url[:120]})，可能评论区未展开或作品不可见。"
                        f"页面诊断: {diag[:300]}，未发送顶层评论")
                # 有评论但没匹配到:导出评论区现场(排序控件/滚动余量/首条
                # 结构签名/cid 挂载属性/前 6 条样本)帮助一次定位。
                diag2 = ""
                try:
                    diag2 = await page.evaluate(_COMMENT_PANEL_DIAG)
                except Exception:
                    pass
                return False, (
                    f"未找到目标评论回复区：该作品页面实际加载 {shown_count} 条"
                    f"评论，已按最热/最新两种排序滚到底并展开折叠子评论、"
                    f"原文/前缀/表情/作者昵称/cid多轮匹配仍未命中。"
                    f"目标: 原文[:30]={(reply_to_text or '')[:30]!r}"
                    f" nick={target_nick or '无'!r} cid={target_cid or '无'!r}。"
                    f"现场: {str(diag2)[:700]}；"
                    f"可能评论已被删除/仅作者可见，未发送回复。"
                    f"[耗时 {' '.join(_stages)}]")

        if editor is None:
            # 优先用 _FIND_VISIBLE_EDITOR 打好标记的可见输入框(面板内、非搜索框)
            try:
                await page.evaluate(_FIND_VISIBLE_EDITOR)
                marked = page.locator('[data-mmm-editor="1"]').first
                if await marked.count() and await marked.is_visible():
                    editor = marked
            except Exception:
                editor = None
        if editor is None:
            # 兜底:遍历选择器,但必须可见(不可见的预渲染 DOM / 顶部搜索框都排除)
            for sel in _COMMENT_INPUT:
                try:
                    locs = page.locator(sel)
                    n = await locs.count()
                    for i in range(min(n, 6)):
                        cand = locs.nth(i)
                        if await cand.is_visible():
                            editor = cand
                            break
                    if editor is not None:
                        break
                except Exception:
                    continue
        if editor is None:
            diag = ""
            n_vis_items = 0
            try:
                diag = await page.evaluate(_DIAG_INPUTS)
                n_vis_items = int((await page.evaluate(_FIND_VISIBLE_EDITOR)
                                   or {}).get("items") or 0)
            except Exception:
                pass
            try:
                print(f"[comment_post] 未找到可见输入框 aweme={aweme_id} "
                      f"panel_open={panel_open} vis_items={n_vis_items} "
                      f"diag={diag[:200]}")
            except Exception:
                pass
            return False, ("未找到评论输入框(评论区可能未展开/被关闭/页面改版)。"
                           f"可见评论数={n_vis_items} "
                           f"页面诊断: {diag[:300]}")

        # 输入评论。v1.5.6 关键修复:键盘事件进的可能是新内联框而 editor
        # 句柄指向底部主框(下方面板布局),只读本句柄 innerText 会误报
        # "无法写入"——用户肉眼已看到字。所以读回走多路(打标框/焦点元素/
        # 面板扫描),找到真正含文本的框就把句柄切过去(后续发送按钮按它的
        # 邻近容器定位)。
        # v1.5.8:输入与发送抽成闭包——下方面板首击若点到评论项操作栏红心,
        # 内联框会失焦收起(零请求),验证循环里可重开回复框整体重跑一次。
        typed_ok = False
        typed_where = ""
        sent = False
        send_diag: dict = {}
        clicked_sem = None    # 实际点到的按钮是否权威("发送"文字/comment-publish)

        async def _readback() -> Tuple[bool, str]:
            """多路读回;命中时 data-mmm-editor 已重标到真框。"""
            try:
                r = await page.evaluate(_READBACK_EDITOR, content[:20]) or {}
            except Exception:
                r = {}
            return bool(r.get("found")), str(r.get("where") or "")

        async def _do_type() -> bool:
            """聚焦→逐字输入→多路读回→execCommand/textContent 兜底。"""
            nonlocal editor, typed_where
            try:
                await editor.click(timeout=2500)
            except Exception:
                pass
            try:
                await editor.evaluate("el => el.focus()")
            except Exception:
                pass
            await page.wait_for_timeout(random.randint(220, 420))
            await page.keyboard.type(content, delay=40)   # 逐字输入,更像真人
            await page.wait_for_timeout(random.randint(280, 520))
            ok, where = await _readback()
            if not ok:
                # execCommand 兜底(对当前 editor 句柄)
                try:
                    await editor.evaluate("el => { el.focus(); }")
                    await page.evaluate(
                        "(args) => { const [el, text] = args;"
                        " el.focus();"
                        " return document.execCommand('insertText', false, text); }",
                        [await editor.element_handle(), content])
                except Exception:
                    pass
                await page.wait_for_timeout(300)
                ok, where = await _readback()
            if not ok:
                # 最后手段:直接写 textContent 并补发 input
                try:
                    await editor.evaluate(
                        "(el, text) => { el.focus(); el.textContent = text;"
                        " el.dispatchEvent(new InputEvent('input',"
                        " {bubbles: true, inputType: 'insertText', data: text})); }",
                        content)
                except Exception:
                    pass
                await page.wait_for_timeout(300)
                ok, where = await _readback()
            if ok:
                typed_where = where
                # 句柄切换到真正含文本的框(供发送按钮邻近定位)
                try:
                    editor = page.locator('[data-mmm-editor="1"]').first
                except Exception:
                    pass
            else:
                # 读不到也不判死:可能是 Shadow DOM/React 渲染时序导致 DOM
                # 读不到但用户肉眼可见文字已输入。继续走发送,最终以 publish
                # 接口回包裁决;空内容时抖音发送按钮本就禁用,不会误发。
                try:
                    print("[comment_post] readback missing after typing,"
                          " continue sending (judge by publish response)")
                except Exception:
                    pass
            return bool(ok)

        async def _do_send() -> bool:
            """轮询可用发送按钮点击(force+dispatch 双保险);无按钮时回车。"""
            nonlocal send_diag, clicked_sem
            for _ in range(10):
                try:
                    send_diag = await page.evaluate(_FIND_SEND_BUTTON) or {}
                except Exception:
                    send_diag = {}
                if send_diag.get("found") and send_diag.get("enabled", True):
                    btn = page.locator('[data-mmm-send="1"]').first
                    try:
                        # force=True:可见性/遮挡已在 JS 侧确认,跳过
                        # Playwright actionability 的额外等待
                        await btn.click(timeout=2000, force=True)
                        clicked_sem = send_diag.get("sem")
                        return True
                    except Exception:
                        try:
                            await btn.dispatch_event("click")
                            clicked_sem = send_diag.get("sem")
                            return True
                        except Exception:
                            pass
                await page.wait_for_timeout(500)
            # 回车兜底:先把焦点拉回编辑器再按 Enter
            try:
                await editor.click(timeout=2000)
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                clicked_sem = None
                return True
            except Exception:
                return False

        typed_ok = await _do_type()
        _stage(f"typed({'ok:'+typed_where if typed_ok else 'unverified'})")
        # 拟人停顿:输入完成后随机停 0.8-1.8 秒再提交(叠加逐字输入时长,
        # 整体落在约 1-3 秒);发送按钮是否可用仍由 _do_send 事件驱动轮询裁决。
        await page.wait_for_timeout(random.randint(800, 1800))

        # 点发送按钮。found 但 enabled=false(灰色)说明 React 还没识别到
        # 输入,继续轮询等它激活;只有可用才点。
        sent = await _do_send()
        _stage(f"send({'clicked' if sent else 'no-btn'})")
        if not sent:
            return False, ("未找到可点击的发送按钮"
                           f"(页面端筛选:{send_diag})且回车提交失败。"
                           f"[耗时 {' '.join(_stages)}]")
        send_t0 = time.monotonic()

        # ---- 发表验证(v1.5.7):三路裁决,不再只赌网络回包 ----
        # ① comment/publish 接口回包(权威) ②回复已出现在评论区 DOM 且编辑器
        # 清空/卸载(平台换端点、回包漏拦时的第二判据) ③人工验证弹窗。
        # 另:首击发送若没生效(编辑器内容原样还在、按钮仍红),自动补点。
        verify_hit = ""
        dom_ok = False
        post_diag: dict = {}

        async def _dom_verify() -> dict:
            try:
                return await page.evaluate(
                    _VERIFY_COMMENT_POST, content[:20]) or {}
            except Exception:
                return {}

        async def _click_send_once() -> bool:
            try:
                sd = await page.evaluate(_FIND_SEND_BUTTON) or {}
            except Exception:
                sd = {}
            if sd.get("found") and sd.get("enabled", True):
                b = page.locator('[data-mmm-send="1"]').first
                try:
                    await b.click(timeout=1500, force=True)
                    return True
                except Exception:
                    try:
                        await b.dispatch_event("click")
                        return True
                    except Exception:
                        return False
            return False

        def _pub_result():
            """接口已回包时的统一结论(成功/拒绝/无法解析)。"""
            if not pub["seen"]:
                return None
            if pub["ok"]:
                return (True, "")
            if pub["known"]:
                return (False, (f"抖音拒绝评论(status_code={pub['code']}"
                                f"{' ' + pub['msg'] if pub['msg'] else ''})—— "
                                f"多为验证码/频控/风控,请降低频率或换号稍后再试"))
            return (False, ("write_uncertain:发表接口已响应但回包无法解析，"
                            "结果未知(任务不会自动重试，请人工核对该作品评论区)"
                            f"[http={pub.get('http')} raw={pub.get('raw')!r}]"))

        extra_clicks = 0
        recovered = False
        last_click_at = -10.0
        clock = time.monotonic()

        def _btn_diag() -> str:
            sd = send_diag or {}
            return ("[按钮:"
                    f"{sd.get('tag', '?')}/{(sd.get('text') or '')[:8]}/"
                    f"red={int(bool(sd.get('red')))}/sem={sd.get('sem')}/"
                    f"nc={sd.get('nCand')}/重发={int(recovered)}] ")

        async def _recover_inline() -> bool:
            """下方面板首击点偏(红心/空白)→内联框失焦收起、零请求。
            重新定位目标评论→点回复→重输→重发,全程仅执行一次。调用方已
            保证回复不在 DOM 且无 publish 请求,不会造成重复发表。"""
            nonlocal editor, send_diag, clicked_sem
            _stage("recover-reopen")
            try:
                ri = await page.evaluate(_FIND_REPLY_EDITOR) or {}
                if not ri.get("found") and require_reply:
                    arg = {"text": reply_to_text or ""}
                    if target_nick:
                        arg["nick"] = target_nick
                    if target_cid:
                        arg["cid"] = target_cid
                    try:
                        await page.evaluate(_FIND_COMMENT_ITEM, arg)
                    except Exception:
                        pass
                    await page.wait_for_timeout(300)
                    try:
                        await page.evaluate(
                            "() => { const el = document.querySelector"
                            "('[data-mmm-target=\"1\"]');"
                            " if (el) el.scrollIntoView({block:'center'}); }")
                    except Exception:
                        pass
                    rb2 = await page.evaluate(_FIND_REPLY_BUTTON) or {}
                    if rb2.get("found"):
                        await page.locator(
                            '[data-mmm-replybtn="1"]').first.click(timeout=2500)
                    else:
                        await page.locator(
                            '[data-mmm-target="1"] [data-e2e="comment-reply"]'
                        ).first.click(timeout=2500)
                    for _ in range(6):
                        ri = await page.evaluate(_FIND_REPLY_EDITOR) or {}
                        if ri.get("found"):
                            break
                        await page.wait_for_timeout(500)
                if ri.get("found"):
                    editor = page.locator('[data-mmm-editor="1"]').first
                else:
                    try:
                        await page.evaluate(_FIND_VISIBLE_EDITOR)
                    except Exception:
                        pass
                    editor = page.locator('[data-mmm-editor="1"]').first
                    if not await editor.count():
                        return False
            except Exception:
                return False
            finally:
                try:
                    await page.evaluate(_CLEAR_TARGET_MARK)
                except Exception:
                    pass
            if not await _do_type():
                return False
            send_diag = {}
            clicked_sem = None
            if await _do_send():
                _stage("recovered-send")
                return True
            return False

        for _ in range(24):   # 12s 主观察窗
            if pub["seen"]:
                break
            if not verify_hit:
                verify_hit = await _visible_any_text(page, _COMMENT_VERIFY_KW)
                if verify_hit:
                    try:
                        print("[comment_post] [VERIFY] douyin asks for manual"
                              " verification; complete it in the browser window,"
                              f" waiting up to {verify_wait_seconds}s")
                    except Exception:
                        pass
            v = await _dom_verify()
            post_diag = v
            if v.get("settled"):
                dom_ok = True
                break
            # 编辑器仍存活且有字=提交动作没生效;按钮可用则补点(最多 2 次)
            if (v.get("alive") and not v.get("empty")
                    and extra_clicks < 2):
                now = time.monotonic() - clock
                if now - last_click_at >= 1.5:
                    if await _click_send_once():
                        extra_clicks += 1
                        last_click_at = now
            # v1.5.9:首击后内联框被收起(编辑器消失+空+回复未进 DOM+零
            # publish+评论区仍在)即判定点偏,自动重开重发一次——不再限制
            # "非权威按钮":B 机实测"发布时间"这类元信息元素会被误判为
            # sem=True,sem 闸门反而挡住了恢复。零 POST+回复不在 DOM 是
            # 更强的证据,不会造成重复发表。1.2s 下限避开正常提交时编辑器
            # 瞬时卸载的竞态(抖音乐观渲染通常 <500ms)。
            if (not recovered and not pub["seen"]
                    and v.get("alive") is False and v.get("empty")
                    and not v.get("posted")
                    and int(v.get("items") or 0) > 0
                    and 1.2 <= time.monotonic() - send_t0 <= 9.0):
                try:
                    print("[comment_post] editor dismissed after click with"
                          " zero POST and reply missing; reopening inline"
                          " reply once")
                except Exception:
                    pass
                if await _recover_inline():
                    recovered = True
                    extra_clicks = 0
                    last_click_at = -10.0
                    send_t0 = time.monotonic()
            await page.wait_for_timeout(500)
        _stage(f"verify(pub={int(pub['seen'])},dom={int(dom_ok)},"
               f"reclick={extra_clicks},recover={int(recovered)})")

        # ① 接口回包裁决
        pr = _pub_result()
        if pr is not None:
            return pr
        # ② DOM 裁决:评论区已出现回复且编辑器已收尾=实际发表成功
        if dom_ok:
            try:
                print("[comment_post] publish response not captured, but the"
                      " reply is present in comment DOM -> treat as success")
            except Exception:
                pass
            _stage("verified-dom")
            return True, ""

        # ②b v1.5.8:编辑器已收尾但两条判据都缺失,留 6s 迟滞窗——平台
        # 乐观渲染慢半拍、或验证码弹窗在主观察窗之后才出现
        if not pub["seen"]:
            for _ in range(12):
                if pub["seen"]:
                    break
                v = await _dom_verify()
                post_diag = v
                if v.get("settled"):
                    dom_ok = True
                    break
                if not verify_hit:
                    verify_hit = await _visible_any_text(
                        page, _COMMENT_VERIFY_KW)
                    if verify_hit:
                        break
                await page.wait_for_timeout(500)
            _stage(f"tail(pub={int(pub['seen'])},dom={int(dom_ok)})")
            pr = _pub_result()
            if pr is not None:
                return pr
            if dom_ok:
                try:
                    print("[comment_post] late DOM settlement -> success")
                except Exception:
                    pass
                _stage("verified-dom-late")
                return True, ""

        # ③ 人工验证:主窗口末尾才弹出也再查一次,然后长时间等用户完成
        if not verify_hit:
            verify_hit = await _visible_any_text(page, _COMMENT_VERIFY_KW)
        if verify_hit:
            try:
                print("[comment_post] [VERIFY] late-detected manual"
                      f" verification, waiting up to {verify_wait_seconds}s")
            except Exception:
                pass
            extra = 0.0
            while extra < verify_wait_seconds:
                if pub["seen"]:
                    break
                vv = await _dom_verify()
                if vv.get("settled"):
                    dom_ok = True
                    break
                await page.wait_for_timeout(500)
                extra += 0.5
            pr = _pub_result()
            if pr is not None:
                return pr
            if dom_ok:
                return True, ""
            return False, ("write_uncertain:验证码等待超时,评论可能已/未发出,"
                           "请人工核对该作品评论区(任务不会自动重试)")

        # ④ 编辑器内容还在=未提交:留 15s 人工补救窗口(点发送/过验证码),
        #    期间同时盯接口回包与 DOM
        still_there = bool(post_diag.get("alive") and not post_diag.get("empty"))
        if still_there:
            try:
                print("[comment_post] text still in editor after click,"
                      " waiting 15s for manual recovery ...")
            except Exception:
                pass
            for _ in range(30):
                if pub["seen"]:
                    break
                vv = await _dom_verify()
                if vv.get("settled"):
                    dom_ok = True
                    break
                await page.wait_for_timeout(500)
            pr = _pub_result()
            if pr is not None:
                return pr
            if dom_ok:
                return True, ""
            return False, ("write_uncertain:已输入但未触发发表"
                           "(可能弹了验证码/发送按钮未激活),"
                           "请人工核对该作品评论区(任务不会自动重试)。"
                           f"{_btn_diag()}"
                           f"[近期POST:{_req_log[-4:]}] "
                           f"[耗时 {' '.join(_stages)}]")
        # ⑤ 编辑器已清空但两种成功判据都没拿到:结果未知,严禁重试。
        #    带上被点按钮/comment POST/编辑器现场,便于判断误点还是端点改版
        return False, ("write_uncertain:评论已提交但未捕获到平台回包，"
                       "结果未知(任务不会自动重试，请人工核对该作品评论区)。"
                       f"{_btn_diag()}"
                       f"[近期POST:{_req_log[-4:]} 现场:{post_diag}] "
                       f"[耗时 {' '.join(_stages)}]")
    except Exception as e:
        return False, f"发评论异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()   # 有头:关 context 即落盘 Cookie
            else:
                await page.close()
        except Exception:
            pass


def _extract_user(data) -> Optional[dict]:
    """从 profile 响应里挖出 user 对象(多结构兜底)。"""
    if not isinstance(data, dict):
        return None
    nested = data.get("data")
    for u in (data.get("user"), data.get("user_info"),
              nested.get("user") if isinstance(nested, dict) else None,
              nested.get("user_info") if isinstance(nested, dict) else None,
              data):
        if isinstance(u, dict) and u.get("sec_uid"):
            return u
    return None


def _extract_post_author(data, expected_sec_uid: str = "") -> Optional[dict]:
    """从本人作品接口里取作者。

    /user/profile/self 偶尔会因页面缓存而不发，但同一页面通常仍会请求
    /aweme/post。只有作者 sec_uid 与本地登录用户一致时才接纳，避免把
    精选页或其它主页的作者误绑到当前账号。
    """
    if not isinstance(data, dict):
        return None
    for item in data.get("aweme_list") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("author")
        if not isinstance(user, dict) or not user.get("sec_uid"):
            continue
        if expected_sec_uid and str(user.get("sec_uid")) != expected_sec_uid:
            continue
        return user
    return None


def _user_from_web_storage(value) -> Optional[dict]:
    """把网页 localStorage 的 user_info 归一成抖音 user 形状。

    2026 版网页会稳定写入：
      {uid: <sec_uid>, nickname: ..., avatarUrl: ...}
    即使 profile/self 被缓存而未发，这份登录用户身份仍可用于打开显式主页。
    数字 uid 是内部账号号，不当作 sec_uid，防止构造错误主页。
    """
    if not isinstance(value, dict):
        return None
    sec_uid = str(value.get("sec_uid") or value.get("secUid") or "")
    if not sec_uid:
        uid = str(value.get("uid") or "")
        if len(uid) >= 24 and not uid.isdigit():
            sec_uid = uid
    if not sec_uid:
        return None
    user = {
        "sec_uid": sec_uid,
        "nickname": value.get("nickname") or value.get("name") or "",
    }
    avatar = value.get("avatarUrl") or value.get("avatar_url") or value.get("avatar")
    if isinstance(avatar, str) and avatar:
        user["avatar_thumb"] = {"url_list": [avatar]}
    return user


_READ_SELF_STORAGE_JS = """() => {
  const out = [];
  const keys = ['user_info', 'userInfo', 'user_info_passport'];
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (const key of keys) {
      try {
        const raw = store.getItem(key);
        if (!raw) continue;
        let value = JSON.parse(raw);
        if (typeof value === 'string') value = JSON.parse(value);
        if (value && typeof value === 'object') out.push(value);
      } catch (_) {}
    }
  }
  return out;
}"""


async def _read_self_from_web_storage(page) -> Optional[dict]:
    try:
        values = await page.evaluate(_READ_SELF_STORAGE_JS)
    except Exception:
        return None
    merged: dict = {}
    for value in values or []:
        user = _user_from_web_storage(value)
        if not user:
            continue
        if merged.get("sec_uid") and user["sec_uid"] != merged["sec_uid"]:
            continue
        for key, item in user.items():
            if item not in (None, "", [], {}):
                merged[key] = item
    return merged or None


def _fill_missing_user_fields(target: dict, source: Optional[dict]) -> None:
    """用弱来源补空字段，不覆盖 profile/self 已返回的权威字段。"""
    for key, value in (source or {}).items():
        if key not in target or target[key] in (None, "", [], {}):
            target[key] = value


async def _refetch_in_page(page, full_url: str) -> Optional[dict]:
    """在 douyin 页面内重发 profile/self。剥掉一次性签名参数后走相对路径,
    抖音自己的 fetch 拦截器会重新补 a_bogus(同 account_hub._fetch_im_user_info)。"""
    try:
        u = urlsplit(full_url)
        qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k not in _SIGN_PARAMS]
        path = u.path + (("?" + urlencode(qs)) if qs else "")
        return await page.evaluate(
            """async (p) => {
              try {
                const r = await fetch(p, {credentials:'include',
                                          headers:{'accept':'application/json'}});
                return await r.json();
              } catch (e) { return null; }
            }""", path)
    except Exception as e:
        print(f"[self_profile] refetch failed: {e!r}")
        return None


def _self_profile_session_is_invalid(has_login_btn, has_login_cookie,
                                     has_result: bool,
                                     profile_user_seen: bool) -> bool:
    """根据页面和 Cookie 证据判断本人登录态是否已经失效。

    登录 Cookie 即使仍在有效期内，也可能已被服务端撤销；页面明确显示可见的
    “登录”入口时，应优先判定为退出状态，避免把 localStorage 或缓存接口中的
    旧资料误当成一次成功的登录校验。
    """
    if has_login_btn is True:
        return True
    return bool(has_result and not profile_user_seen and has_login_cookie is False)


async def fetch_self_profile(mgr: BrowserManager, identity: Identity,
                             timeout_ms: int = 15000, block_media: bool = False
                             ) -> Tuple[dict, str]:
    """打开自己的主页,拦截 user/profile/self 拿登录账号真实资料。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。

    新版网页可能把 /user/self 重定向到 /jingxuan，且因 user_self_cache AB
    不再发 profile/self。此时从 localStorage.user_info 取得本人 sec_uid，
    再打开 /user/<sec_uid> 触发资料请求；仍未触发时，用同页 aweme/post 中
    sec_uid 完全匹配的 author 兜底。无作品账号至少可返回昵称、头像和 sec_uid。

    拦截时若页面正在跳转，Patchright 读 body 会失败，故再补一发页内 refetch
    （抖音自己的 fetch 拦截器会补 a_bogus 签名）。
    注:query/user 不是资料接口,它返回的是设备会话记录(user_uid/browser_name),无 sec_uid。"""
    result: dict = {}
    api_seen = []                   # 看到的抖音 API 请求(诊断用)
    hit_apis = []                   # 命中的 profile/self(判断是"没发"还是"读不到")
    shapes = []                     # 命中但挖不出 user 时的响应结构/读取异常(标定用)
    self_urls: List[str] = []       # profile/self 的完整 URL(带 query),供页内 refetch 复用
    post_users: List[dict] = []     # profile/self 不发时，用本人作品 author 兜底
    storage_uid = ""
    profile_user_seen = False
    error = ""
    page = await mgr.new_page(identity, block_media)

    def is_profile(resp):
        # 只认自己的 profile/self:profile/other 是看别人主页时发的,拦了会绑错号
        return SELF_PROFILE_API in resp.url

    async def on_response(resp):
        nonlocal profile_user_seen
        url = resp.url
        if ("douyin.com" in url and ("/aweme/v1/web/" in url or "/web/api/" in url)
                and len(api_seen) < 40):
            api_seen.append(f"{resp.status} {url.split('?')[0]}")
        if is_profile(resp) and resp.status == 200:
            path = url.split("?")[0]
            hit_apis.append(path)
            if url not in self_urls:
                self_urls.append(url)
            try:
                data = await resp.json()
            except Exception as e:
                # 页面跳转会丢弃 body。别静默 return,否则日志显示"命中了"却查不出原因
                if len(shapes) < 4:
                    shapes.append(f"{path} body_read_failed={e!r}")
                return
            u = _extract_user(data)
            if u:
                result.update(u)
                profile_user_seen = True
            elif isinstance(data, dict) and len(shapes) < 4:
                shapes.append(f"{path} keys={sorted(data)[:12]}")
        elif POST_API in url and resp.status == 200:
            try:
                data = await resp.json()
            except Exception:
                return
            # 此时可能还没读到 localStorage，先暂存；稍后按本人 sec_uid 严格筛选。
            u = _extract_post_author(data)
            if u:
                post_users.append(u)

    page.on("response", on_response)
    logged_out = False
    final_url = ""
    has_login_btn = None
    has_login_cookie = None
    try:
        # 先走本人路由；它即使重定向，登录页脚本通常也已经写好 user_info。
        for url in ("https://www.douyin.com/user/self", "https://www.douyin.com/"):
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 等 hydrate/XHR；wait_for_response 若在 goto 后调用会漏掉已经返回的响应，
            # 因此统一由上面的 response handler 收集。
            await page.wait_for_timeout(min(max(timeout_ms // 4, 1800), 4000))
            final_url = page.url
            if "passport" in final_url or "/login" in final_url:
                logged_out = True
                break

            storage_user = await _read_self_from_web_storage(page)
            if storage_user:
                storage_uid = str(storage_user.get("sec_uid") or "")
                _fill_missing_user_fields(result, storage_user)
                # aweme/post 可能比 localStorage 更早返回；现在才能安全确认它是本人。
                post_user = next(
                    (u for u in post_users
                     if str(u.get("sec_uid") or "") == storage_uid),
                    None,
                )
                if post_user:
                    result.update(post_user)
            if result:
                break

        # profile/self 被缓存/路由改写时，显式本人 sec_uid 路由会重新触发它。
        # 已经命中过权威资料接口则无需多跳一次。
        if storage_uid and not profile_user_seen:
            await page.goto(f"https://www.douyin.com/user/{storage_uid}",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(min(max(timeout_ms // 3, 2200), 5000))
            final_url = page.url
            post_user = next(
                (u for u in reversed(post_users)
                 if str(u.get("sec_uid") or "") == storage_uid),
                None,
            )
            if post_user:
                result.update(post_user)

        if not profile_user_seen and self_urls:
            # 拦到了但 body 读不到:页内重发一次(此时页面已静止,不会再丢 body)
            data = await _refetch_in_page(page, self_urls[-1])
            u = _extract_user(data)
            if u:
                result.update(u)
                profile_user_seen = True
            elif isinstance(data, dict) and len(shapes) < 6:
                shapes.append(f"refetch keys={sorted(data)[:12]}")
        # 是否能看到“登录”按钮(看到=其实没登录进去)
        try:
            has_login_btn = await page.get_by_text("登录", exact=True).first.is_visible(
                timeout=1500)
        except Exception:
            has_login_btn = None
        try:
            cookies = await page.context.cookies("https://www.douyin.com/")
            has_login_cookie = any(c.get("name") in _LOGIN_COOKIES for c in cookies)
        except Exception:
            has_login_cookie = None

        # 页面上的可见登录入口是最直接的退出证据。Cookie 可能尚未过期但已被
        # 服务端撤销；localStorage/profile 缓存也可能继续返回旧账号资料。
        if _self_profile_session_is_invalid(
                has_login_btn, has_login_cookie, bool(result), profile_user_seen):
            result.clear()
            logged_out = True
    except Exception as e:
        error = f"{e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not result:
        if logged_out:
            error = "logged_out"
        elif has_login_cookie is False:
            error = "logged_out"
        elif not error:
            # 区分「接口没发出来」和「发了但取不到 user」——之前一律报后者,误导排查
            error = ("profile/self 命中但取不到 user" if hit_apis else "no_profile_xhr")
        print(f"[self_profile] 未拿到资料; err={error}; final_url={final_url}; "
              f"login_btn_visible={has_login_btn}; login_cookie={has_login_cookie}; "
              f"storage_uid={bool(storage_uid)}; hit={hit_apis}; shapes={shapes}; "
              f"api_seen({len(api_seen)})={api_seen[:25]}")
    return result, error


def _dig_comment_list(data) -> list:
    """从创作中心各种可能的响应结构里挖出评论数组(防御式)。"""
    if not isinstance(data, dict):
        return []
    for key in ("comments", "comment_list", "comment_infos", "list", "data"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):     # 再下钻一层
            for k2 in ("comments", "comment_list", "list"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


def _is_creator_comment_list_url(url: str) -> bool:
    """创作中心评论列表接口的宽松匹配(2026-09 新旧版端点均命中)。"""
    return "creator.douyin.com" in url and "comment" in url and "list" in url


def _is_comment_api_url(url: str) -> bool:
    """评论数据接口判定:宽松覆盖创作中心改版前后的端点。
    只用于 XHR/fetch 响应(文档/静态资源由调用方按 resource_type 排除)。"""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return False
    host, path = parts.netloc, parts.path
    if "creator.douyin.com" in host and "comment" in path:
        return True
    if "/aweme/v1/creator/comment" in path:
        return True
    if "/aweme/v1/web/comment/list" in path:
        return True
    return False


_COMMENT_ID_KEYS = ("cid", "comment_id")
_COMMENT_TEXT_KEYS = ("text", "content", "comment_text")
# 作品id 候选键:snake_case 为主,2026-09 后部分新端点出现 camelCase
_COMMENT_AWEME_KEYS = ("aweme_id", "item_id", "group_id", "object_id",
                       "awemeId", "itemId")
_COMMENT_REPLY_KEYS = ("reply_comment", "reply_comment_infos", "replies",
                       "reply_list", "sub_comments")
# 信封里可能包裹作品信息的容器键,其 id 即作品id
_COMMENT_CONTAINER_KEYS = ("item", "aweme", "video", "work",
                           "item_info", "work_info", "aweme_info")


def _looks_like_comment(d) -> bool:
    """形状判定:有评论id,且有评论正文或用户对象。防止把用户/作品对象误当评论。"""
    if not isinstance(d, dict):
        return False
    cid = d.get("cid") or d.get("comment_id")
    if cid in (None, "") or isinstance(cid, (list, dict)):
        return False
    if any(str(d.get(k) or "").strip() for k in _COMMENT_TEXT_KEYS):
        return True
    return isinstance(d.get("user"), dict) or "reply_comment" in d


def _walk_comment_items(data, out: list, depth: int = 0) -> None:
    """递归挖掘任意信封里的评论数组(抖音改版常只改外层包裹)。
    列表中至少 1/3 元素带评论id 才认定为评论列表,避免误采用户/作品列表;
    命中后继续下钻每条评论的回复字段。"""
    if depth > 12:
        return
    if isinstance(data, list):
        hits = [x for x in data if isinstance(x, dict)
                and any(x.get(k) not in (None, "") for k in _COMMENT_ID_KEYS)]
        if hits and len(hits) >= max(1, (len(data) + 2) // 3):
            for x in hits:
                if _looks_like_comment(x):
                    out.append(x)
                    for rk in _COMMENT_REPLY_KEYS:
                        rv = x.get(rk)
                        if isinstance(rv, (dict, list)):
                            _walk_comment_items(rv, out, depth + 1)
            return
        for x in data[:40]:
            if isinstance(x, (dict, list)):
                _walk_comment_items(x, out, depth + 1)
    elif isinstance(data, dict):
        # dict 自身就是一条评论(如主评论的 reply_comment 单对象):收集并下钻回复
        if _looks_like_comment(data) and any(
                data.get(k) not in (None, "") for k in _COMMENT_ID_KEYS):
            out.append(data)
            for rk in _COMMENT_REPLY_KEYS:
                rv = data.get(rk)
                if isinstance(rv, (dict, list)):
                    _walk_comment_items(rv, out, depth + 1)
            return
        for v in data.values():
            if isinstance(v, (dict, list)):
                _walk_comment_items(v, out, depth + 1)


# query/请求体里的作品id:兼容 snake/camel 参数名与 JSON/表单两种编码
_AWEME_ID_RE = re.compile(
    r"(?:item_id|itemId|aweme_id|awemeId)[\"'=:\s]{1,6}\"?(\d{10,})")


def _aweme_fallback_from_url(url: str) -> str:
    """创作中心按作品查询时 item_id 即作品id(纯数字);非数字令牌不可用作归因。
    parse_qs 之外再用正则扫全串,兼容参数被截断/位置靠后/camelCase 的情况。"""
    try:
        q = urlsplit(str(url)).query
    except Exception:
        return ""
    try:
        qs = parse_qs(q)
        for name in ("item_id", "itemId", "aweme_id", "awemeId"):
            iid = (qs.get(name) or [""])[0]
            if iid.isdigit() and len(iid) >= 10:
                return iid
    except Exception:
        pass
    m = _AWEME_ID_RE.search(q)
    return m.group(1) if m else ""


def _aweme_fallback_from_body(body: str) -> str:
    """POST 接口常把 item_id 放在请求体(JSON 或表单)而非 query。"""
    if not body:
        return ""
    m = _AWEME_ID_RE.search(str(body))
    return m.group(1) if m else ""


def _find_aweme_id_anywhere(data, depth: int = 0) -> str:
    """从按作品查询的评论响应信封任意位置找作品id(仅认作品id 键名,
    避免把同样是长数字的 cid 误当作品id)。评论对象外层常带 item/aweme 信息。"""
    if depth > 10:
        return ""
    if isinstance(data, dict):
        for k in _COMMENT_AWEME_KEYS:
            v = data.get(k)
            if v not in (None, "") and isinstance(v, (str, int)) \
                    and str(v).isdigit() and len(str(v)) >= 10:
                return str(v)
        # 容器嵌套:item/aweme/video/work 等子对象的 id 即作品id
        for ck in _COMMENT_CONTAINER_KEYS:
            sub = data.get(ck)
            if isinstance(sub, dict):
                for ik in ("id", "item_id", "aweme_id", "itemId", "awemeId"):
                    v = sub.get(ik)
                    if v not in (None, "") and isinstance(v, (str, int)) \
                            and str(v).isdigit() and len(str(v)) >= 10:
                        return str(v)
        for v in data.values():
            if isinstance(v, (dict, list)):
                found = _find_aweme_id_anywhere(v, depth + 1)
                if found:
                    return found
    elif isinstance(data, list):
        for x in data[:10]:
            found = _find_aweme_id_anywhere(x, depth + 1)
            if found:
                return found
    return ""


def _attach_fallback_aweme(comments: list, request_url: str,
                           envelope_aweme_id: str = "",
                           request_body: str = "",
                           context_aweme_id: str = "") -> None:
    """评论体缺作品id 时兜底(缺作品id 会被下游按作品归因整批丢弃)。
    优先级:评论自带(含 camelCase 归一为 aweme_id) > 同响应信封内作品id 键
    > query 的数字 item_id > POST 请求体 item_id > 当前选中作品上下文
    (逐作品点击后紧随的响应,时间相关性强,可安全归因)。"""
    fallback = (str(envelope_aweme_id or "") or _aweme_fallback_from_url(request_url)
                or _aweme_fallback_from_body(request_body)
                or str(context_aweme_id or ""))
    for c in comments:
        if not isinstance(c, dict):
            continue
        if not str(c.get("aweme_id") or ""):
            for k in ("item_id", "group_id", "object_id", "awemeId", "itemId"):
                v = c.get(k)
                if v not in (None, "") and not isinstance(v, (list, dict)):
                    c["aweme_id"] = str(v)
                    break
        if not str(c.get("aweme_id") or "") and fallback:
            c["aweme_id"] = fallback


# ── 2026-09 新版创作中心评论页:评论管理面板 +「选择作品」下拉(Semi Design),
# 必须逐作品选择才按作品发 comment/list 请求。以下 JS 均为防御式探测。
# 注意:innerText 会聚合子节点文本,点击时必须限定叶子附近(childElementCount<=4)。
# 抓数主通道:文档创建前注入 fetch/XHR hook,把所有含 comment 的请求路径与
# 响应体存入 window.__mmm_cap —— 不依赖端点名、不怕 Playwright json() 失败、
# 不漏首屏请求。改版后只需读诊断里观测到的真实路径即可快速适配。
_CREATOR_CAP_HOOK_JS = """
(function () {
  if (window.__mmm_cap) return;
  var cap = { urls: [], bodies: [], all: [], seq: 0 };
  window.__mmm_cap = cap;
  var MAX_BODY = 2000000;
  function short(u) {
    try {
      var m = String(u).match(/^https?:\\/\\/[^/]+(\\/[^?]*)/);
      return m ? m[1] : String(u).slice(0, 160);
    } catch (e) { return String(u).slice(0, 160); }
  }
  function recAll(u) {
    try {
      var p = short(u);
      if (cap.all.indexOf(p) < 0 && cap.all.length < 80) cap.all.push(p);
    } catch (e) {}
  }
  function rec(u, text, method, reqBody) {
    try {
      if (typeof u !== 'string') return;
      recAll(u);
      if (u.indexOf('comment') < 0) return;
      var p = short(u);
      if (cap.urls.indexOf(p) < 0 && cap.urls.length < 40) cap.urls.push(p);
      if (text && cap.bodies.length < 40) {
        var q = '';
        var i = String(u).indexOf('?');
        if (i >= 0) q = String(u).slice(i + 1, i + 601);
        cap.seq += 1;
        cap.bodies.push({ i: cap.seq, p: p, q: q, m: String(method || ''),
                          rb: (typeof reqBody === 'string') ? reqBody.slice(0, 600) : '',
                          t: String(text).slice(0, MAX_BODY) });
      }
    } catch (e) {}
  }
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function () {
      var args = arguments;
      var u = (args[0] && args[0].url) ? args[0].url : args[0];
      var mth = (args[1] && args[1].method) ? args[1].method : 'GET';
      var rb = (args[1] && typeof args[1].body === 'string') ? args[1].body : '';
      return origFetch.apply(this, args).then(function (resp) {
        try {
          if (typeof u === 'string' && u.indexOf('comment') >= 0) {
            resp.clone().text().then(function (t) { rec(u, t, mth, rb); }).catch(function () {});
          } else if (typeof u === 'string') {
            recAll(u);
          }
        } catch (e) {}
        return resp;
      });
    };
  }
  var OrigXHR = window.XMLHttpRequest;
  if (OrigXHR) {
    function HookXHR() {
      var xhr = new OrigXHR();
      var u = '';
      var mth = 'GET';
      var rb = '';
      var open = xhr.open;
      var send = xhr.send;
      xhr.open = function (m, url) { mth = m; u = url; return open.apply(xhr, arguments); };
      xhr.send = function (b) { rb = (typeof b === 'string') ? b : ''; return send.apply(xhr, arguments); };
      xhr.addEventListener('load', function () {
        try { rec(u, xhr.responseText, mth, rb); } catch (e) {}
      });
      return xhr;
    }
    HookXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = HookXHR;
  }
})();
"""

_CREATOR_READ_CAP_JS = """() => {
  const c = window.__mmm_cap || {urls: [], bodies: [], all: []};
  return {urls: c.urls.slice(0, 40), all: (c.all || []).slice(0, 80),
          bodies: c.bodies.slice(0, 40).map(b => ({i: b.i, p: b.p, q: b.q,
              m: b.m || '', rb: b.rb || '', t: b.t}))};
}"""

_CREATOR_DOM_ROWS_JS = """() => {
  let rows = 0;
  const nodes = document.querySelectorAll('button, a, span, div');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    if (el.childElementCount > 2) continue;
    const t = (el.innerText || '').trim();
    if (t === '回复' || t === '回复评论') rows += 1;
    if (rows >= 300) break;
  }
  return rows;
}"""

_CREATOR_HAS_SELECTOR_JS = """() => {
  const nodes = document.querySelectorAll('button, [role="button"], .semi-button, div, span');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim();
    if (t && t.length <= 20 && (t.indexOf('选择作品') >= 0 || t.indexOf('筛选作品') >= 0))
      return true;
  }
  return false;
}"""

_CREATOR_WORK_OPTIONS_JS = """() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('div, li, span, p');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    const text = (el.innerText || '').trim();
    if (!text || text.length > 120) continue;
    if (text.indexOf('发布于') < 0) continue;
    if (el.childElementCount > 4) continue;
    if (seen.has(text)) continue;
    seen.add(text);
    out.push(text);
    if (out.length >= 60) break;
  }
  return out;
}"""

_CREATOR_CLICK_OPTION_JS = """(text) => {
  const nodes = document.querySelectorAll('div, li, span, p');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim();
    if (t !== text || el.childElementCount > 4) continue;
    const r = el.getBoundingClientRect();
    const opt = {bubbles: true, cancelable: true, view: window,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                 button: 0};
    const PE = window.PointerEvent || MouseEvent;
    el.dispatchEvent(new PE('pointerdown', opt));
    el.dispatchEvent(new MouseEvent('mousedown', opt));
    el.dispatchEvent(new PE('pointerup', opt));
    el.dispatchEvent(new MouseEvent('mouseup', opt));
    el.dispatchEvent(new MouseEvent('click', opt));
    return true;
  }
  return false;
}"""

_CREATOR_CLICK_SELECTOR_BTN_JS = """() => {
  const nodes = document.querySelectorAll('button, [role="button"], .semi-button, div, span');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 20) continue;
    if (t.indexOf('选择作品') < 0 && t.indexOf('筛选作品') < 0) continue;
    const r = el.getBoundingClientRect();
    const opt = {bubbles: true, cancelable: true, view: window,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                 button: 0};
    const PE = window.PointerEvent || MouseEvent;
    el.dispatchEvent(new PE('pointerdown', opt));
    el.dispatchEvent(new MouseEvent('mousedown', opt));
    el.dispatchEvent(new PE('pointerup', opt));
    el.dispatchEvent(new MouseEvent('mouseup', opt));
    el.dispatchEvent(new MouseEvent('click', opt));
    return true;
  }
  return false;
}"""

# probe 实测:切换「按评论时间排序」会让页面重新请求评论列表端点,
# 是触发数据加载的有效手段(默认排序可能走另一条首屏通道)。
_CREATOR_CLICK_SORT_JS = """() => {
  const want = '按评论时间排序';
  const nodes = document.querySelectorAll('div, li, span, p, button');
  for (const el of nodes) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim();
    if (t !== want || el.childElementCount > 2) continue;
    const r = el.getBoundingClientRect();
    const opt = {bubbles: true, cancelable: true, view: window,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
                 button: 0};
    const PE = window.PointerEvent || MouseEvent;
    el.dispatchEvent(new PE('pointerdown', opt));
    el.dispatchEvent(new MouseEvent('mousedown', opt));
    el.dispatchEvent(new PE('pointerup', opt));
    el.dispatchEvent(new MouseEvent('mouseup', opt));
    el.dispatchEvent(new MouseEvent('click', opt));
    return true;
  }
  return false;
}"""


async def _creator_work_options(page) -> List[str]:
    """枚举作品下拉当前可见的选项文本(含「发布于」的行,防御式)。"""
    try:
        raw = await page.evaluate(_CREATOR_WORK_OPTIONS_JS) or []
    except Exception:
        return []
    return [t for t in raw if isinstance(t, str) and t]


async def _open_creator_work_selector(page) -> None:
    """点开「选择作品」下拉;Playwright 定位失败时退回页面内事件派发。"""
    try:
        await page.locator("button", has_text="选择作品").first.click(timeout=3000)
        return
    except Exception:
        pass
    try:
        await page.evaluate(_CREATOR_CLICK_SELECTOR_BTN_JS)
    except Exception:
        pass


async def _creator_drain_cap(page, collected: Dict[str, dict],
                             observed: Set[str], body_hits: List[int],
                             processed: Optional[Set[int]] = None,
                             context_aweme: str = "",
                             all_paths: Optional[Set[str]] = None) -> int:
    """读取注入 hook 缓存的全部评论响应,递归挖评论并入 collected。
    body_hits:[收到的评论响应体数, 解析出评论的响应体数](原地更新);
    processed 记录已处理响应序号,避免轮询重复计数。返回新增条数。"""
    if processed is None:
        processed = set()
    try:
        cap = await page.evaluate(_CREATOR_READ_CAP_JS)
    except Exception:
        return 0
    before = len(collected)
    for b in cap.get("bodies") or []:
        bid = b.get("i")
        if bid in processed:
            continue
        processed.add(bid)
        body_hits[0] += 1
        path = b.get("p") or ""
        query = ("?" + b["q"]) if b.get("q") else ""
        if not _is_comment_api_url("https://creator.douyin.com" + path + query):
            continue
        text = b.get("t") or ""
        data = None
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}|\[.*\]", text, re.S)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
        if data is None:
            continue
        items: list = []
        _walk_comment_items(data, items)
        if not items:
            continue
        body_hits[1] += 1
        full_url = "https://creator.douyin.com" + path + query
        _attach_fallback_aweme(items, full_url, _find_aweme_id_anywhere(data),
                               b.get("rb") or "", context_aweme)
        for c in items:
            cid = str(c.get("cid") or c.get("comment_id") or "")
            if cid:
                collected[cid] = c
    for u in cap.get("urls") or []:
        observed.add(u)
    if all_paths is not None:
        for u in cap.get("all") or []:
            all_paths.add(u)
    return len(collected) - before


def _creator_zero_capture_error(final_url: str, observed: List[str],
                                body_total: int, body_with_comments: int,
                                dom_rows: int, has_selector: bool,
                                options_tried: int, api_hits: int) -> str:
    """零抓取时的证据化提示。措辞避开账号惩罚类关键词,保持 BUSINESS 归类。
    observed 是 B 机器真实页面实际发出的评论相关请求路径——改版适配的第一手证据。"""
    paths = "、".join(observed[:4]) if observed else "(无)"
    head = "未拦截到创作中心评论"
    if not observed:
        return (f"{head}(评论页加载后未发现评论相关数据请求,"
                f"页面疑似评论行 {dom_rows},最终页面={final_url};"
                "页面交互可能已再次改版)")
    if body_total == 0:
        return (f"{head}(观测到评论请求 {paths},但未取得响应内容)")
    if body_with_comments == 0:
        return (f"{head}(观测到评论请求 {paths},共 {body_total} 个响应均无评论结构,"
                f"页面疑似评论行 {dom_rows})")
    return (f"{head}(评论请求 {_body_with_comments_hint(body_with_comments, api_hits)}"
            f"已处理但无有效评论id;观测路径 {paths},页面疑似评论行 {dom_rows},"
            f"选择作品尝试 {options_tried} 个,新版特征={has_selector})")


def _body_with_comments_hint(n: int, api_hits: int) -> str:
    return f"{n} 个、接口命中 {api_hits} 次" if api_hits else f"{n} 个"


def _creator_missing_aweme_error(collected: Dict[str, dict],
                                 observed: List[str],
                                 all_paths: List[str]) -> str:
    """抓到评论但全部缺作品id 时的证据化提示。
    附上评论字段样例与页面真实请求路径——据此可一次定位新结构,
    避免远程盲猜。措辞避开账号惩罚类关键词,保持 BUSINESS 归类。"""
    paths = "、".join(observed[:4]) if observed else "(无)"
    sample = next(iter(collected.values()), {})
    keys = ",".join(sorted(str(k) for k in sample.keys())[:14]) or "(空)"
    others = [p for p in all_paths if "comment" not in p][:6]
    other_hint = (";页面其它数据请求 " + "、".join(others)) if others else ""
    return (f"创作中心评论缺作品id(拦截到 {len(collected)} 条评论,"
            f"均无 aweme_id/item_id 字段,无法按作品归因;"
            f"评论字段样例 [{keys}];评论请求路径 {paths}{other_hint})")


async def fetch_creator_comments(mgr: BrowserManager, identity: Identity,
                                 known_cids: Set[str], page_url: str,
                                 max_scrolls: int = 8, settle_ms: int = 1600,
                                 block_media: bool = True
                                 ) -> Tuple[List[dict], str]:
    """⚠️ 实验性:打开创作中心评论管理页,抓取评论列表数据。
    2026-09 抖音改版后页面为「评论管理」面板 +「选择作品」下拉,首屏/切作品/
    切排序各自触发不同端点。采用多通道鲁棒抓取:
      1) 文档创建前注入 fetch/XHR hook(主通道,不漏首屏、不挑端点名);
      2) Playwright response 事件(副通道,resource_type=xhr/fetch);
      3) 动作触发:等首屏 → 面板内滚动 → 逐作品选择 → 切「按评论时间排序」;
      4) 响应体递归挖掘「评论形状」对象,作品id 缺失时用 item_id 兜底归因。
    未检出新版特征时仍保留旧版整页滚动回退。返回 (新评论原始列表, error)。
    """
    collected: Dict[str, dict] = {}
    observed: Set[str] = set()
    all_paths: Set[str] = set()      # 页面全部 xhr/fetch 路径(诊断证据)
    processed_bodies: Set[int] = set()
    body_hits = [0, 0]          # [收到评论响应体数, 其中解析出评论的数]
    api_hits = 0
    error = ""
    has_selector = False
    options_tried = 0
    context_aweme = ""          # 最近一次逐作品选择对应的作品id(时间相关性归因)
    page = await mgr.new_page(identity, block_media)

    try:
        await page.add_init_script(_CREATOR_CAP_HOOK_JS)
    except Exception:
        pass

    async def on_response(resp):
        nonlocal api_hits, context_aweme
        if not _is_comment_api_url(resp.url):
            return
        try:
            if resp.request.resource_type not in ("xhr", "fetch", "other"):
                return
        except Exception:
            pass
        api_hits += 1
        observed.add(urlsplit(resp.url).path or resp.url[:120])
        req_body = ""
        try:
            req_body = resp.request.post_data or ""
        except Exception:
            req_body = ""
        rid = (_aweme_fallback_from_url(resp.url)
               or _aweme_fallback_from_body(req_body))
        if rid:
            context_aweme = rid
        try:
            data = await resp.json()
        except Exception:
            return  # hook 通道会兜住 json 解析失败的响应
        items: list = []
        _walk_comment_items(data, items)
        _attach_fallback_aweme(items, resp.url, _find_aweme_id_anywhere(data),
                               req_body, rid)
        for c in items:
            cid = str(c.get("cid") or c.get("comment_id") or "")
            if cid:
                collected[cid] = c

    page.on("response", on_response)
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        # 等首屏:重页面渲染慢,轮询 hook 缓存最多 ~12s,已有评论提前结束
        deadline = time.time() + 12.0
        while time.time() < deadline:
            await page.wait_for_timeout(900)
            await _creator_drain_cap(page, collected, observed, body_hits, processed_bodies)
            if collected:
                break
            cur = page.url
            if "/login" in cur or "passport" in cur:
                error = "创作者登录态已失效,请重新创作者登录"
                break
        if not error:
            has_selector = bool(await page.evaluate(_CREATOR_HAS_SELECTOR_JS))
            # ── 动作1:面板内滚动(创作中心是表格面板,滚动最大 overflow 容器)──
            stagnant = 0
            for _ in range(max(2, max_scrolls)):
                before = len(collected)
                try:
                    await page.evaluate(_SCROLL_PROFILE_JS)
                    await page.evaluate(
                        "() => window.scrollBy(0, document.body.scrollHeight)")
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                await _creator_drain_cap(page, collected, observed, body_hits,
                                         processed_bodies, context_aweme, all_paths)
                if len(collected) == before:
                    stagnant += 1
                    if stagnant >= 3:
                        break
                else:
                    stagnant = 0
            # ── 动作2:逐作品选择(无论是否检测到按钮都容错尝试)──
            tried: Set[str] = set()
            works_limit = max(6, int(max_scrolls) * 3)
            for _ in range(works_limit):
                texts = await _creator_work_options(page)
                fresh = [t for t in texts if t not in tried]
                if not fresh:
                    await _open_creator_work_selector(page)
                    await page.wait_for_timeout(1100)
                    texts = await _creator_work_options(page)
                    fresh = [t for t in texts if t not in tried]
                    if not fresh:
                        break
                target = fresh[0]
                tried.add(target)
                clicked = False
                try:
                    async with page.expect_response(
                            lambda r: _is_comment_api_url(r.url), timeout=8000):
                        clicked = await page.evaluate(_CREATOR_CLICK_OPTION_JS, target)
                except Exception:
                    pass
                if not clicked:
                    break
                await page.wait_for_timeout(700 + settle_ms // 2)
                await _creator_drain_cap(page, collected, observed, body_hits,
                                         processed_bodies, context_aweme, all_paths)
            options_tried = len(tried)
            # ── 动作3:切「按评论时间排序」触发列表端点重发,再滚几轮 ──
            try:
                await page.evaluate(_CREATOR_CLICK_SORT_JS)
            except Exception:
                pass
            await page.wait_for_timeout(1400)
            for _ in range(3):
                before = len(collected)
                try:
                    await page.evaluate(_SCROLL_PROFILE_JS)
                except Exception:
                    pass
                await page.wait_for_timeout(settle_ms)
                await _creator_drain_cap(page, collected, observed, body_hits,
                                         processed_bodies, context_aweme, all_paths)
                if len(collected) == before:
                    break
            await _creator_drain_cap(page, collected, observed, body_hits,
                                     processed_bodies, context_aweme, all_paths)
            # ── 汇总与证据化报错 ──
            if not collected:
                dom_rows = 0
                try:
                    dom_rows = await page.evaluate(_CREATOR_DOM_ROWS_JS)
                except Exception:
                    pass
                error = _creator_zero_capture_error(
                    page.url, sorted(observed), body_hits[0], body_hits[1],
                    dom_rows, has_selector, options_tried, api_hits)
            else:
                missing = sum(
                    1 for c in collected.values()
                    if not any(str(c.get(k) or "") for k in _COMMENT_AWEME_KEYS))
                if missing == len(collected):
                    error = _creator_missing_aweme_error(
                        collected, sorted(observed), sorted(all_paths))
    except Exception as e:
        error = f"打开创作中心失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error
