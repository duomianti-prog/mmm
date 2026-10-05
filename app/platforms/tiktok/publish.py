"""TikTok 网页创作者中心发布(浏览器自动化 www.tiktok.com/creator-center)。

与抖音/快手一致走「登录态浏览器 + 不逆向签名」路线:用账号专属持久 profile
打开创作者中心上传页,set_input_files 上传、填 caption、设置可见范围,
点一次 Post,随后只做证据确认,绝不二次点击/兜底重发。

安全三态(配合引擎 _finish_publish):
* 拿到提交成功证据(commit 接口 status_code=0 / 跳转作品管理页 / 成功文案)
  → (True, url, "") 任务 done;
* 点击 Post 的**那一刻之前**的失败(未登录/上传失败/找不到编辑器或按钮)
  → 普通错误:按风控/网络/瞬时失败分类,可安全自动重试,平台侧不会有作品;
* 点击 Post **之后**任何拿不到成功证据的情况(超时/断连/业务拒绝/人工验证
  未完成)一律 write_uncertain —— 提交可能已经到达平台,任务落「待确认」,
  系统不自动重发,平台侧最多一条。

预约发布由后台引擎按 scheduled_at 排队执行(与抖音/快手一致),不使用网页内
定时开关;瞬时重试保留预约时刻(keep_schedule)。

⚠️ 实验性:创作者中心为英文界面(账号上下文 pin en-US),选择器随改版可能
   失效,集中在下面的 _* 常量;失败时截图 + 文本快照落 data/debug/tt_publish_*。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, List, Tuple
from urllib.parse import urlparse

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

UPLOAD_URL = "https://www.tiktok.com/creator-center/upload?lang=en"
MANAGE_URL = "https://www.tiktok.com/creator-center/content?lang=en"

# 图文模式入口(视频为默认页;部分区域无此入口则图文可能不被支持)
_PHOTO_TAB = [
    '[data-e2e="upload-photo"]',
    'button:has-text("photo mode")',
    'text=Switch to photo mode',
    'text=Upload photos',
]
# caption 富文本(TikTok 无独立标题,标题并入 caption 首行)
_CAPTION_SEL = [
    'div[contenteditable="true"]',
    '[data-e2e="caption-editor"] [contenteditable="true"]',
    '[data-e2e="caption"] div[contenteditable="true"]',
    'div[data-placeholder*="caption"]',
    'div[data-placeholder*="describe"]',
]
_POST_BTN = [
    '[data-e2e="post-button"]',
    '[data-e2e="post"]',
    'button:has-text("Post")',
    'div[role="button"]:has-text("Post")',
]
# 视频仍在处理时弹窗里的「继续发布」确认(同一次提交的确认,不是二次发布)
_CONFIRM_BTN = [
    'button:has-text("Post now")',
    'button:has-text("Continue to post")',
]
_VISIBILITY_LABEL = {
    "public": ("Public", "Everyone"),
    "friends": ("Friends", "Friends can view"),
    "private": ("Only you", "Only me", "Private"),
}

_SUCCESS_KW = (
    "Your video is being uploaded to TikTok",
    "Your photo is being uploaded",
    "You're all set",
    "Manage your posts",
    "Video posted successfully",
    "Photo posted successfully",
)
_VERIFY_URL = ("/captcha", "/verify", "/whale", "/security/verify",
               "verifycenter")
_VERIFY_KW = ("Verification", "verification", "slider", "puzzle",
              "human verification", "verify to continue", "人机验证",
              "安全验证", "拖动滑块")
# 最终提交接口:commit/create 才算「平台已受理」
_COMMIT_PATH = ("/commit/item", "/aweme/post", "/web/aweme/post",
                "/creator/content/post", "/creator/item/create")
_LOGIN_PARTS = ("/login", "passport")

_DEBUG_DIR = Path("./data/debug")
_CAPTION_LIMIT = 2200


def _log(msg: str) -> None:
    print(f"[tt-publish] {msg}", flush=True)


def build_tiktok_caption(title: str, desc: str, topics: str) -> str:
    """标题 + 正文 + 话题合并为单条 caption(TikTok 上限 2200)。"""
    parts = []
    if title and title.strip():
        parts.append(title.strip())
    if desc and desc.strip():
        parts.append(desc.strip())
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",")
            if t.strip()]
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    return "\n".join(parts).strip()[:_CAPTION_LIMIT]


def _is_login_url(url: str) -> bool:
    return any(part in str(url or "").lower() for part in _LOGIN_PARTS)


def _is_verify_url(url: str) -> bool:
    return any(part in str(url or "").lower() for part in _VERIFY_URL)


async def _visible_any(page, keywords) -> str:
    for kw in keywords:
        try:
            if await page.get_by_text(kw, exact=False).first.is_visible():
                return kw
        except Exception:
            continue
    return ""


async def _dump(page, tag: str) -> str:
    """截图 + URL/文本快照落盘,供改版后校准选择器。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"tt_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            txt = await page.inner_text("body")
        except Exception:
            txt = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{txt[:4000]}", encoding="utf-8")
        _log(f"已存诊断快照 tag={tag} url={page.url} png={png}")
        return png
    except Exception as e:
        _log(f"存快照失败: {e!r}")
        return ""


async def _click_first(page, selectors, timeout=2500) -> bool:
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _click_confirm_if_present(page) -> bool:
    """弹窗确认按钮仅在出现时点击;count() 立即返回,不空等 timeout。"""
    for sel in _CONFIRM_BTN:
        try:
            loc = page.locator(sel)
            if await loc.count() and await loc.first.is_visible():
                await loc.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


async def _fill_caption(page, text: str, timeout_ms: int = 4000) -> bool:
    """填 contenteditable caption:点中后全选清空再键入。"""
    for sel in _CAPTION_SEL:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout_ms)
            await el.click(timeout=timeout_ms)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.keyboard.type(text[:_CAPTION_LIMIT], delay=12)
            # 话题联想浮层不处理,按 Escape 收起,避免后续误点
            await page.keyboard.press("Escape")
            _log(f"caption 已填入(选择器 {sel!r})")
            return True
        except Exception:
            continue
    return False


async def _primary_post_button(page):
    """挑可见、可用、文本恰为 Post 的提交按钮,取靠后者(表单底部)。"""
    try:
        btns = page.locator('button, [role="button"]')
        n = await btns.count()
    except Exception:
        return None
    cand = None
    for i in range(n):
        b = btns.nth(i)
        try:
            if not await b.is_visible():
                continue
            t = ((await b.inner_text()) or "").strip()
            if t in ("Post", "Publish now"):
                cand = b
        except Exception:
            continue
    if cand is not None:
        return cand
    # data-e2e 兜底
    for sel in _POST_BTN[:2]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def _choose_by_exact_text(page, labels) -> bool:
    """在 radio/label/可点项里安全点中整项文本恰为目标的元素。"""
    for label in labels:
        # role=radio
        try:
            loc = page.get_by_role("radio", name=label, exact=True)
            if await loc.count():
                await loc.first.scroll_into_view_if_needed(timeout=2000)
                try:
                    await loc.first.check(timeout=2500)
                except Exception:
                    await loc.first.click(timeout=2500)
                _log(f"已选可见范围「{label}」(role)")
                return True
        except Exception:
            pass
        # label / 单选组件 / 下拉菜单项
        try:
            items = page.locator(
                'label, [role="radio"], [role="menuitemradio"], '
                '[role="option"], [class*="radio"], [class*="option"]')
            n = await items.count()
            for i in range(n):
                it = items.nth(i)
                try:
                    if not await it.is_visible():
                        continue
                    if ((await it.inner_text()) or "").strip() != label:
                        continue
                    await it.scroll_into_view_if_needed(timeout=2000)
                    await it.click(timeout=2000)
                    _log(f"已选可见范围「{label}」(item)")
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


async def _apply_visibility(page, visibility: str) -> None:
    """public 为默认不动;friends/private 才点,失败不阻断(保留页面当前值)。"""
    if visibility == "public":
        return
    labels = _VISIBILITY_LABEL.get(visibility)
    if not labels:
        return
    # 新版是自定义下拉:先尝试展开当前权限控件再选
    for trigger in ("Public", "Everyone", "Friends", "Only you", "Only me"):
        try:
            trig = page.get_by_text(trigger, exact=True)
            for i in range(await trig.count() - 1, -1, -1):
                cand = trig.nth(i)
                if not await cand.is_visible():
                    continue
                await cand.click(timeout=1500)
                await page.wait_for_timeout(400)
                if await _choose_by_exact_text(page, labels):
                    return
        except Exception:
            continue
    if not await _choose_by_exact_text(page, labels):
        _log(f"[warn] 未点中可见范围 {visibility}（已安全跳过,沿用页面当前值）")


async def _disable_download_if_present(page) -> None:
    """allow_save=False 时尽力关闭「允许下载」开关;该控件各版本不稳定,永不阻断。"""
    try:
        candidates = page.get_by_text("Download", exact=False)
        for i in range(await candidates.count()):
            cand = candidates.nth(i)
            if not await cand.is_visible():
                continue
            txt = ((await cand.inner_text()) or "").strip().lower()
            if "download" not in txt or len(txt) > 40:
                continue
            # 找同行开关/复选框点一次(只点包含文案的最小容器)
            row = cand
            for _ in range(3):
                try:
                    parent = row.locator("xpath=..")
                    if await parent.count():
                        row = parent.first
                except Exception:
                    break
            switch = row.locator(
                'input[type="checkbox"], [role="switch"], [class*="switch"]').first
            try:
                checked = await switch.get_attribute("aria-checked")
                if checked in ("true", "mixed"):
                    await switch.click(timeout=1500)
                    _log("已关闭「允许下载」开关")
                return
            except Exception:
                continue
    except Exception:
        return


def _extract_item_id(payload: dict) -> str:
    for key in ("aweme_id", "item_id", "itemId", "video_id", "id"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text.isdigit():
            return text
    # 部分回包嵌在 item/itemStruct 里
    for nested_key in ("item", "itemStruct", "aweme_detail"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            nested_id = _extract_item_id(nested)
            if nested_id:
                return nested_id
    return ""


def _published_url(payload: Any) -> str:
    """从 commit 回包提取作品地址;取不到 id 返回空串。"""
    if not isinstance(payload, dict):
        return ""
    item_id = _extract_item_id(payload)
    if not item_id:
        return ""
    author_obj = payload.get("author")
    author = ""
    if isinstance(author_obj, dict):
        author = str(author_obj.get("uniqueId") or "").strip()
    elif author_obj:
        author = str(author_obj).strip().lstrip("@")
    if author:
        return f"https://www.tiktok.com/@{author}/video/{item_id}"
    return f"{MANAGE_URL}#item{item_id}"


async def publish_tiktok(mgr: BrowserManager, identity: Identity,
                         storage_state_json: str, media_type: str, title: str,
                         desc: str, media_paths: List[str], topics: str = "",
                         *, visibility: str = "public",
                         allow_save: bool = True, headed: bool = True,
                         timeout_seconds: int = 300,
                         on_submit=None) -> Tuple[bool, str, str]:
    """发布一条 TikTok 作品,返回 ``(ok, result_url, error)``。

    on_submit:在唯一一次 Post 点击前同步调用的回调(可为 async),
    引擎用它落 write_submitted 防重发标记;点击之后只有「确认成功」与
    「待确认」两种结局,不存在自动重发。
    """
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    if media_type not in ("video", "images"):
        return False, "", f"不支持的作品类型: {media_type}"
    caption = build_tiktok_caption(title, desc, topics)

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()

    # 意外 filechooser 一律取消(上传只走 set_input_files)。
    def _fc_guard(fc):
        # 正常路径走 set_input_files,不会触发 filechooser;
        # 触发即说明页面弹了系统选择框(通常被脚本点击意外唤起),忽略该事件。
        _log(f"意外触发文件选择框,已忽略: {fc}")
    try:
        page.on("filechooser", _fc_guard)
    except Exception:
        pass

    commit = {"seen": False, "ok": False, "message": "", "url": ""}

    async def on_response(response):
        if commit["seen"]:
            return
        try:
            if response.request.method != "POST":
                return
            path = urlparse(response.url).path.lower()
            if not any(marker in path for marker in _COMMIT_PATH):
                return
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        commit["seen"] = True
        code = payload.get("status_code", payload.get("statusCode"))
        if code is None:
            # 个别回包省略状态码但直接带作品 id —— 按成功处理
            code = 0 if _extract_item_id(payload) else -1
        if code == 0:
            commit["ok"] = True
            commit["url"] = _published_url(payload)
        else:
            commit["message"] = str(
                payload.get("status_msg") or payload.get("message")
                or payload.get("status_msg_detail") or "平台拒绝发布")

    try:
        page.on("response", on_response)
    except Exception:
        pass

    ok, result_url, error = False, "", ""
    submitted = False
    try:
        _log(f"打开创作者中心上传页 media_type={media_type}, files={len(files)}")
        await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=40000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        if _is_login_url(page.url):
            await _dump(page, "loggedout")
            return False, "", "TikTok 登录态已失效，请重新登录后再发布"
        if _is_verify_url(page.url):
            await _dump(page, "verify-upload")
            return False, "", ("TikTok 要求完成人机验证后才能打开发布页，"
                               "请稍后在弹出的窗口中完成验证再重试")

        if media_type == "images":
            if await _click_first(page, _PHOTO_TAB, timeout=2500):
                await page.wait_for_timeout(800)
                _log("已切换图文模式")
            else:
                _log("未找到图文模式入口,尝试直接选择图片(区域可能不支持)")

        try:
            want = files if media_type == "images" else files[:1]
            await page.locator('input[type="file"]').first.set_input_files(
                want, timeout=20000)
            _log("已提交文件,等待上传/处理…")
        except Exception as e:
            await _dump(page, "nofileinput")
            return False, "", f"上传文件失败(未找到文件输入框?): {e!r}"

        edit_timeout = 150000 if media_type == "video" else 60000
        editor_ready = False
        for _ in range(max(1, edit_timeout // 1000)):
            for sel in _CAPTION_SEL:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        editor_ready = True
                        break
                except Exception:
                    continue
            if editor_ready:
                break
            if _is_login_url(page.url):
                await _dump(page, "loggedout")
                return False, "", "TikTok 登录态已失效，请重新登录后再发布"
            await page.wait_for_timeout(1000)
        if not editor_ready:
            await _dump(page, "noeditor")
            return False, "", ("上传后未进入编辑页(视频可能仍在处理或上传失败),"
                               "请在弹出的窗口里查看后重试")
        _log(f"已进入编辑页 url={page.url}")
        await page.wait_for_timeout(1200)

        if caption:
            if not await _fill_caption(page, caption):
                await _dump(page, "nocaption")
                return False, "", "未找到 caption 输入框(发布页可能改版)"
            await page.wait_for_timeout(500)

        await _apply_visibility(page, visibility)
        if not allow_save:
            await _disable_download_if_present(page)
        await page.wait_for_timeout(500)

        # 等 Post 按钮可用(视频未处理完时 disabled),最多 ~150s
        btn = None
        for _ in range(75):
            btn = await _primary_post_button(page)
            if btn is not None:
                try:
                    if await btn.is_enabled():
                        break
                except Exception:
                    break
            await page.wait_for_timeout(2000)
        if btn is None:
            await _dump(page, "nobtn")
            return False, "", "未找到 Post 按钮(发布页可能改版),已存诊断快照"

        # ── 防重发边界:回调落库后只允许一次点击,之后永不自动重发 ──
        if on_submit is not None:
            outcome = on_submit()
            if asyncio.iscoroutine(outcome):
                await outcome
        submitted = True
        try:
            await btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            await btn.click(timeout=5000)
            _log("已点击 Post(仅此一次)")
        except Exception as e:
            await _dump(page, "clickfail")
            return False, "", (f"write_uncertain:Post 点击已派发但连接中断: {e!r}")

        # 点击后:成功证据 / 业务拒绝 / 人工验证被动等待 / 超时待确认
        deadline = max(int(timeout_seconds), 300)
        waited, verify_notified = 0, False
        while waited < deadline:
            if commit["seen"]:
                if commit["ok"]:
                    ok = True
                    break
                # 明确业务拒绝:提交已发生,仍落待确认由人工核对,绝不重发
                error = ("write_uncertain:TikTok 返回业务结果「"
                         + commit["message"] + "」,请到作品管理核对是否已发")
                break
            lowered = str(page.url or "").lower()
            if "/creator-center/content" in lowered or \
                    ("/content" in lowered and "/upload" not in lowered):
                ok = True
                break
            hit = await _visible_any(page, _SUCCESS_KW)
            if hit:
                _log(f"命中成功文案「{hit}」")
                ok = True
                break
            # 「视频仍在处理,是否继续」确认弹窗(同一次提交)
            try:
                await _click_confirm_if_present(page)
            except Exception:
                pass
            verify_now = _is_verify_url(page.url) or bool(
                await _visible_any(page, _VERIFY_KW))
            if verify_now and not verify_notified:
                verify_notified = True
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                await _dump(page, "verify-post")
                _log("【需人工】TikTok 要求人工验证 —— 请在弹出窗口完成,流程会自动继续")
            await page.wait_for_timeout(2000)
            waited += 2

        if ok:
            result_url = commit["url"] or (
                str(page.url) if "/content" in str(page.url).lower()
                else MANAGE_URL)
            _log(f"发布成功 url={result_url}")
        elif not error:
            if verify_notified:
                png = await _dump(page, "verify-timeout")
                error = ("write_uncertain:已点 Post 但 TikTok 人工验证未在等待时间内"
                         f"完成,请到作品管理核对;诊断截图: {png or 'data/debug'}")
            else:
                png = await _dump(page, "unconfirmed")
                error = ("write_uncertain:已点 Post 但未确认到成功信号,"
                         "请到 TikTok 作品管理核对是否已发布(系统不会自动重发);"
                         f"诊断截图: {png or 'data/debug'}")
    except asyncio.CancelledError:
        if submitted:
            error = "write_uncertain:发布已中断(可能已提交),结果需到平台核对"
        try:
            await _dump(page, "cancelled")
        except Exception:
            pass
        raise
    except Exception as e:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
        if submitted:
            error = f"write_uncertain:Post 已点击后发生异常: {e!r}"
        else:
            error = f"发布异常: {e!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
