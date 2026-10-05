"""Visible, page-native Xiaohongshu writes with explicit ambiguity semantics."""
from __future__ import annotations

import inspect
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlencode, urlsplit

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .media import validate_publish_files
from ...browser.xhs_selectors import (
    candidate_locator,
    find_present,
    find_visible,
    selector_candidates,
    selector_diagnostic,
)


PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
PUBLISH_MANAGER_URL = "https://creator.xiaohongshu.com/new/note-manager"
_SUCCESS_TEXTS = ("发布成功", "发布完成")


def _publish_url(media_type: str) -> str:
    target = "video" if media_type == "video" else "image"
    return f"{PUBLISH_URL}&from=tab_switch&target={target}"


def _node_attributes(node: dict) -> dict[str, str]:
    raw = list(node.get("attributes") or ())
    return {
        str(raw[index]).lower(): str(raw[index + 1])
        for index in range(0, len(raw) - 1, 2)
    }


def _node_children(node: dict):
    yield from node.get("children") or ()
    yield from node.get("shadowRoots") or ()
    content = node.get("contentDocument")
    if isinstance(content, dict):
        yield content


def _node_text(node: dict) -> str:
    value = str(node.get("nodeValue") or "")
    return value + "".join(_node_text(child) for child in _node_children(node))


def _find_publish_component_button(root: dict) -> dict | None:
    """Return the real button even when it lives in a closed shadow root."""
    hosts: list[dict] = []

    def collect(node: dict) -> None:
        if str(node.get("nodeName") or "").upper() == "XHS-PUBLISH-BTN":
            hosts.append(node)
        for child in _node_children(node):
            collect(child)

    collect(root)
    for host in hosts:
        buttons: list[dict] = []

        def find_buttons(node: dict) -> None:
            if str(node.get("nodeName") or "").upper() == "BUTTON":
                buttons.append(node)
            for child in _node_children(node):
                find_buttons(child)

        for shadow in host.get("shadowRoots") or ():
            find_buttons(shadow)
        if not buttons:
            continue
        enabled = [
            button for button in buttons
            if "disabled" not in _node_attributes(button)
        ] or buttons
        for button in enabled:
            if "发布" in _node_text(button):
                return button
        return enabled[0]
    return None


def _quad_center(model: dict) -> tuple[float, float] | None:
    box = model.get("model") or {}
    quad = box.get("border") or box.get("content") or ()
    if len(quad) < 8:
        return None
    xs = [float(quad[index]) for index in range(0, 8, 2)]
    ys = [float(quad[index]) for index in range(1, 8, 2)]
    return sum(xs) / 4, sum(ys) / 4


async def _click_closed_shadow_publish_button(page: Any) -> None:
    """Dispatch one trusted click to the closed-shadow publish button."""
    session = await page.context.new_cdp_session(page)
    try:
        await session.send("DOM.enable")
        document = await session.send(
            "DOM.getDocument", {"depth": -1, "pierce": True})
        button = _find_publish_component_button(document.get("root") or {})
        if button is None:
            raise RuntimeError("新版发布组件的内部按钮尚未就绪")
        backend_node_id = button.get("backendNodeId")
        if backend_node_id is None:
            raise RuntimeError("新版发布组件缺少可点击节点")
        with suppress(Exception):
            await session.send(
                "DOM.scrollIntoViewIfNeeded",
                {"backendNodeId": backend_node_id},
            )
        center = _quad_center(await session.send(
            "DOM.getBoxModel", {"backendNodeId": backend_node_id}))
        if center is None:
            raise RuntimeError("新版发布按钮当前没有可见点击区域")
        x, y = center
        await session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
        })
        await session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
        await session.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
    finally:
        with suppress(Exception):
            await session.detach()


class _PublishWebComponentLocator:
    """Locator facade for the creator-center closed-shadow publish control."""

    def __init__(self, page: Any, host: Any):
        self._page = page
        self._host = host

    def __getattr__(self, name: str):
        return getattr(self._host, name)

    async def is_enabled(self) -> bool:
        try:
            disabled = str(
                await self._host.get_attribute("submit-disabled") or ""
            ).strip().lower()
            loading = str(
                await self._host.get_attribute("submit-loading") or ""
            ).strip().lower()
            return disabled not in {"true", "1"} and loading not in {
                "true", "1"}
        except Exception:
            return True

    async def click(self, *_args, **_kwargs):
        await _click_closed_shadow_publish_button(self._page)


@dataclass(frozen=True)
class XhsWriteOutcome:
    status: Literal["success", "failed", "uncertain"]
    result: str = ""
    error: str = ""
    method: str = "browser"

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def legacy(self) -> tuple[bool, str, str]:
        if self.status == "success":
            return True, self.result, ""
        if self.status == "uncertain":
            detail = self.error or "页面提交后未取得明确成功证据，请先到平台核对"
            return False, "", f"write_uncertain:{detail}"
        return False, "", self.error or "页面操作失败"


async def _wait_until_enabled(locator: Any, interaction: Any,
                              *, attempts: int = 80) -> bool:
    for _ in range(max(1, attempts)):
        try:
            if await locator.count() and await locator.is_visible() \
                    and await locator.is_enabled():
                return True
        except Exception:
            pass
        await interaction.pause(0.2, 0.45)
    return False


async def _wait_for_visible(page: Any, group: str, interaction: Any,
                            *, attempts: int = 40) -> tuple[Any | None, str]:
    """Poll editors/widgets that are mounted after asynchronous upload work."""
    for index in range(max(1, attempts)):
        locator, name = await find_visible(page, group)
        if locator is not None:
            return locator, name
        if index + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


async def _wait_for_present(page: Any, group: str, interaction: Any,
                            *, attempts: int = 60) -> tuple[Any | None, str]:
    """Poll hidden controls that are mounted shortly after a tab transition."""
    for index in range(max(1, attempts)):
        locator, name = await find_present(page, group)
        if locator is not None:
            return locator, name
        if index + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


_VISIBILITY_LABELS = {
    "public": "公开可见",
    "private": "仅自己可见",
    "friends": "仅互关好友可见",
}


async def _set_publish_visibility(page: Any, interaction: Any,
                                  visibility: str) -> None:
    desired = _VISIBILITY_LABELS.get(str(visibility or "public"), "公开可见")
    candidates = page.locator(".d-select-description")
    current = None
    for index in range(int(await candidates.count())):
        candidate = candidates.nth(index)
        try:
            if await candidate.is_visible():
                current = candidate
                break
        except Exception:
            continue
    if current is not None:
        try:
            if str(await current.inner_text()).strip() == desired:
                return
        except Exception:
            pass
        await interaction.click_visible(current)
        await interaction.pause(0.15, 0.3)
    else:
        raise RuntimeError("未找到可见范围设置")

    options = page.get_by_text(desired, exact=True)
    for index in range(int(await options.count())):
        option = options.nth(index)
        try:
            cls = str(await option.get_attribute("class") or "")
            if await option.is_visible() and "name" in cls.split():
                await interaction.click_visible(option)
                await interaction.pause(0.15, 0.3)
                if str(await current.inner_text()).strip() != desired:
                    raise RuntimeError("可见范围设置后读回不一致")
                return
        except RuntimeError:
            raise
        except Exception:
            continue
    raise RuntimeError(f"未找到可见范围选项：{desired}")


async def _upload_is_busy(page: Any) -> bool:
    for candidate in selector_candidates("publish.progress"):
        try:
            locator = candidate_locator(page, candidate)
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_upload_complete(page: Any, interaction: Any,
                                *, attempts: int = 120) -> bool:
    stable = 0
    for _ in range(max(1, attempts)):
        if not await _upload_is_busy(page):
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        await interaction.pause(0.25, 0.55)
    return False


class _SubmitLocator:
    """Mark the exact boundary where Patchright dispatches the sole submit click."""

    def __init__(self, locator: Any, state: dict, on_submit: Any = None):
        self._locator = locator
        self._state = state
        self._on_submit = on_submit

    def __getattr__(self, name: str):
        return getattr(self._locator, name)

    async def click(self, *args, **kwargs):
        if self._on_submit is not None:
            result = self._on_submit()
            if inspect.isawaitable(result):
                await result
        self._state["clicked"] = True
        return await self._locator.click(*args, **kwargs)


def _publish_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        try:
            url = str(response.url or "")
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if submitted["clicked"] and method == "POST" \
                    and 200 <= status < 300 and any(
                    marker in url.lower() for marker in (
                        "/publish", "/note/create", "/web_api/sns/v2/note")):
                evidence["responses"].append(response)
        except Exception:
            return
    return on_response


def _publish_payload_accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    if payload.get("success") is True:
        return True
    for key in ("code", "result_code"):
        if key not in payload:
            continue
        value = payload[key]
        if value == 0 or str(value).strip().lower() in {"0", "ok", "success"}:
            return True
    data = payload.get("data")
    return bool(isinstance(data, dict) and data.get("success") is True)


async def _consume_publish_responses(evidence: dict) -> bool:
    while evidence["responses"]:
        response = evidence["responses"].pop(0)
        try:
            payload = await response.json()
        except Exception:
            continue
        if _publish_payload_accepted(payload):
            evidence["accepted"] = True
            evidence["url"] = str(getattr(response, "url", "") or "")
            return True
        evidence["business_rejected"] = True
    return bool(evidence["accepted"])


async def _visible_success(page: Any) -> bool:
    raw_url = str(getattr(page, "url", "") or "")
    lower_url = raw_url.lower()
    if any(marker in lower_url
           for marker in ("/publish/success", "publish_success")):
        return True
    # Current creator-center variants return to content management rather than
    # a dedicated success route. This check only runs after the one submit
    # click, and excludes auth/risk/error routes from success evidence.
    try:
        parsed = urlsplit(raw_url)
        path = (parsed.path or "/").lower()
        if parsed.hostname == "creator.xiaohongshu.com" \
                and "/publish/publish" not in path \
                and not any(marker in lower_url for marker in (
                    "login", "passport", "verify", "captcha", "error")):
            return True
    except Exception:
        pass
    for text in _SUCCESS_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _confirm_published_title(page: Any, title: str, interaction: Any,
                                   *, attempts: int = 40) -> bool:
    """Confirm the submitted note is present in creator note management."""
    title = str(title or "").strip()
    if not title:
        return False
    try:
        await page.goto(
            PUBLISH_MANAGER_URL,
            wait_until="domcontentloaded",
            timeout=40_000,
        )
    except Exception:
        return False
    for index in range(max(1, attempts)):
        try:
            match = page.get_by_text(title, exact=True).first
            if await match.count() and await match.is_visible():
                return True
        except Exception:
            pass
        if index + 1 < attempts:
            await interaction.pause(0.45, 0.8)
            with suppress(Exception):
                await page.reload(wait_until="domcontentloaded", timeout=40_000)
    return False


async def publish_xhs_browser(
        mgr: BrowserManager, identity: Identity, media_type: str,
        title: str, desc: str, topics: Sequence[str], files: Sequence[str],
        *, timeout_seconds: int = 180,
        visibility: str = "public",
        on_submit: Any = None) -> XhsWriteOutcome:
    """Submit one note once; never retry or switch transport after submission."""
    try:
        paths = validate_publish_files(media_type, files)
    except ValueError as exc:
        return XhsWriteOutcome("failed", error=str(exc))
    title = (title or "").strip()[:20]
    tags = [str(topic).strip().lstrip("#") for topic in topics if str(topic).strip()]
    body = ((desc or "") + (
        "\n" + " ".join(f"#{tag}" for tag in tags) if tags else ""
    )).strip()[:1000]
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence = {
        "accepted": False,
        "url": "",
        "responses": [],
        "business_rejected": False,
    }
    on_response = _publish_response_handler(evidence, submitted)

    try:
        async with mgr.visible_page(
                identity, url=_publish_url(media_type),
                keep_context=False) as page:  # 一次性写任务:结束即关该账号 Chrome
            if "login" in page.url or "passport" in page.url:
                return XhsWriteOutcome("failed", error="logged_out:创作平台未登录")

            # The URL already selects image/video mode.  New builds render the
            # active tab's text inside an overlaying child, so clicking that
            # text can be intercepted by its own active tab.  Do not click an
            # already-selected mode; wait for its file input instead.

            file_input, _ = await _wait_for_present(
                page, "publish.file", interaction)
            if file_input is None:
                diagnostic = await selector_diagnostic(page, "publish.file")
                return XhsWriteOutcome(
                    "failed", error=f"未找到媒体上传控件(发布页可能改版)；{diagnostic}")
            await file_input.set_input_files(paths, timeout=15_000)
            if not await _wait_upload_complete(page, interaction):
                return XhsWriteOutcome("failed", error="媒体上传在限定时间内未完成")

            if title:
                title_input, _ = await _wait_for_visible(
                    page, "publish.title", interaction)
                if title_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.title")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到标题输入框(发布页可能改版)；{diagnostic}")
                await interaction.type_short(title_input, title)
            if body:
                body_input, _ = await _wait_for_visible(
                    page, "publish.body", interaction)
                if body_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.body")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到正文输入框(发布页可能改版)；{diagnostic}")
                await interaction.insert_long(body_input, body, page=page)

            try:
                await _set_publish_visibility(page, interaction, visibility)
            except Exception as exc:
                return XhsWriteOutcome(
                    "failed", error=f"设置可见范围失败: {exc}")

            publish_button, publish_selector = await _wait_for_visible(
                page, "publish.submit", interaction)
            if publish_button is None:
                diagnostic = await selector_diagnostic(page, "publish.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到发布按钮(发布页可能改版)；{diagnostic}")
            if publish_selector == "publish_web_component":
                publish_button = _PublishWebComponentLocator(
                    page, publish_button)
            if not await _wait_until_enabled(
                    publish_button, interaction,
                    attempts=max(4, min(120, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="发布按钮一直不可用，请检查页面提示")

            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                # This wrapper records the precise click-dispatch boundary.  There
                # is intentionally no second click and no API fallback below it.
                await interaction.click_visible(
                    _SubmitLocator(publish_button, submitted, on_submit))
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"发布已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"发布按钮点击失败: {exc!r}")

            attempts = max(4, min(80, int(timeout_seconds * 2)))
            for _ in range(attempts):
                await _consume_publish_responses(evidence)
                if await _visible_success(page):
                    result = str(
                        getattr(page, "url", "") or evidence["url"])
                    await interaction.pause(0.8, 1.2)
                    confirmed = await _confirm_published_title(
                        page, title, interaction,
                        attempts=max(
                            4,
                            min(
                                120,
                                timeout_seconds if media_type == "video"
                                else timeout_seconds // 3,
                            ),
                        ),
                    )
                    if confirmed:
                        return XhsWriteOutcome("success", result=result)
                    return XhsWriteOutcome(
                        "uncertain",
                        error=("平台进入了发布成功页，但笔记管理中未找到"
                               "本次标题，请到平台核对"),
                    )
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.25, 0.55)
            detail = (
                "平台返回了业务拒绝结果，但提交状态仍需核对"
                if evidence["business_rejected"]
                else "发布按钮已点击一次，但未取得明确成功证据"
            )
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "发布已提交后页面异常" if submitted["clicked"] else "发布页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def _locator_visible(locator: Any) -> bool:
    try:
        return bool(await locator.count() and await locator.is_visible())
    except Exception:
        return False


# 锚定目标评论的“整行根容器”并打 data-mmm-xhs-root 标记。
# 教训:[data-comment-id] 往往只挂在内层内容节点上,而“回复”操作按钮在
# 外层兄弟节点;exact 文本匹配又会被昵称/@/子评论干扰。改为页面端:
# 从 cid 锚点(或文本命中节点)向上找第一个“内含可见‘回复’叶子”的祖先。
_XHS_ANCHOR_REPLY_ROOT = r"""
(args) => {
  const [cid, text] = args;
  const ZW = /[\s\u200B-\u200F\uFEFF]/g;
  const norm = s => (s || '').replace(ZW, '');
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
  const isReplyLeaf = el => {
    const t = (el.innerText || el.textContent || '').trim();
    return t === '回复' || t === '回 复';
  };
  // 该子树内是否存在可见且文本恰为“回复”的叶子
  const subtreeHasReply = root => {
    let hit = false;
    root.querySelectorAll('span,a,button,p,div,em').forEach(el => {
      if (hit) return;
      if (isReplyLeaf(el) && vis(el)) hit = true;
    });
    return hit;
  };
  document.querySelectorAll('[data-mmm-xhs-root]')
    .forEach(el => el.removeAttribute('data-mmm-xhs-root'));
  let anchor = null;
  let via = '';
  if (cid) {
    const safe = String(cid).replace(/"/g, '\\"');
    const nodes = document.querySelectorAll('[data-comment-id="' + safe + '"]');
    for (const n of nodes) { if (vis(n) || n.querySelector('*')) { anchor = n; break; } }
    if (anchor) via = 'cid';
  }
  const want = norm(text).slice(0, 12);
  if (!anchor && want) {
    // 文本兜底:在评论行候选里找内容包含片段的节点
    const rows = document.querySelectorAll(
      '[data-comment-id],[class*="comment-item"],[class*="commentItem"],'
      + '[class*="parent-comment"],[class*="comment-inner"]');
    for (const row of rows) {
      if (norm(row.innerText || '').indexOf(want) !== -1) { anchor = row; via = 'text'; break; }
    }
  }
  if (!anchor) {
    const n = document.querySelectorAll('[data-comment-id]').length;
    return { found: false, reason: 'no-target', nComments: n };
  }
  let chosen = null;
  let el = anchor;
  for (let i = 0; el && i < 9; i++, el = el.parentElement) {
    if (subtreeHasReply(el)) { chosen = el; break; }
  }
  if (!chosen) {
    // 向上未找到含回复按钮的祖先:退回最深的评论行样式祖先
    el = anchor;
    for (let i = 0; el && i < 6; i++, el = el.parentElement) {
      const cls = String(el.className || '');
      if (/comment[-_]?item|commentItem|parent-comment|comment-inner/.test(cls)) {
        chosen = el;
        break;
      }
    }
  }
  if (!chosen) chosen = anchor;
  chosen.setAttribute('data-mmm-xhs-root', '1');
  return {
    found: true, via: via || 'fallback',
    snippet: (chosen.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80),
  };
}
"""

# 在已锚定的评论根内找“可见可点且未被遮挡”的回复按钮并打标。
# 小红书桌面端回复按钮常需 hover 评论行后才渲染/显色,因此 Python 侧会先
# 真实 hover;此 JS 也负责在 hover 后做最终判定,并排除“展开 N 条回复”。
_XHS_FIND_REPLY_BUTTON = r"""
() => {
  const root = document.querySelector('[data-mmm-xhs-root="1"],[data-mmm-xhs-root]');
  if (!root) return { found: false, reason: 'no-root-mark' };
  document.querySelectorAll('[data-mmm-xhs-reply]')
    .forEach(el => el.removeAttribute('data-mmm-xhs-reply'));
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
  const t = el => (el.innerText || el.textContent || '').trim();
  const BAD = /查看回复|展开|收起|条回复|更多回复|回复\s*[(（]?\s*\d|回复数/;
  const cands = [];
  root.querySelectorAll('span,a,button,p,em,i,[class*="reply"],[class*="operation"] *')
    .forEach(el => {
      const text = t(el);
      const attrHit = /reply/i.test(
        String(el.className || '') + ' ' + String(el.id || '')
        + ' ' + String(el.getAttribute('data-name') || ''));
      const textHit = (text === '回复' || text === '回 复');
      if (!textHit && !attrHit) return;
      if (BAD.test(text)) return;
      // class 命中但文本是一整块容器(如整条评论)时跳过,只留小叶子
      if (!textHit && text.length > 8) return;
      cands.push({ el, textHit, attrHit,
                   area: (() => { const r = el.getBoundingClientRect();
                                  return r.width * r.height; })() });
    });
  cands.sort((a, b) => {
    if (a.textHit !== b.textHit) return a.textHit ? -1 : 1;
    if (a.attrHit !== b.attrHit) return a.attrHit ? -1 : 1;
    return a.area - b.area;
  });
  for (const c of cands) {
    const el = c.el;
    if (!vis(el)) continue;
    try {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(
        r.left + r.width / 2, r.top + r.height / 2);
      if (top && top !== el && !el.contains(top)
          && !(top.contains && top.contains(el))) continue;
    } catch (e) {}
    el.setAttribute('data-mmm-xhs-reply', '1');
    return { found: true, tag: el.tagName || '',
             text: t(el).slice(0, 10), n: cands.length };
  }
  // 诊断:回复区长什么样
  let diag = '';
  try {
    const ops = root.querySelector('[class*="operation"],[class*="footer"],[class*="interact"]');
    diag = (ops || root).innerText.replace(/\s+/g, ' ').trim().slice(0, 160);
  } catch (e) {}
  return { found: false, reason: 'no-visible-reply-btn',
           nCand: cands.length, diag: diag };
}
"""

# hover 评论行:触发 React 的 mouseenter/mousemove,使操作按钮显色。
_XHS_HOVER_ROOT = r"""
() => {
  const root = document.querySelector('[data-mmm-xhs-root="1"],[data-mmm-xhs-root]');
  if (!root) return false;
  try {
    const r = root.getBoundingClientRect();
    const x = Math.max(2, r.left + Math.min(r.width / 2, 120));
    const y = r.top + Math.min(r.height / 2, 40);
    const fire = (type, target) => {
      try {
        target.dispatchEvent(new MouseEvent(type, {
          bubbles: true, cancelable: true, view: window,
          clientX: x, clientY: y, relatedTarget: root }));
      } catch (e) {}
    };
    let node = root;
    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
      fire('mouseover', node);
      fire('mousemove', node);
      fire('mouseenter', node);
    }
    return true;
  } catch (e) { return false; }
}
"""


async def _open_xhs_reply_entry(page: Any, interaction: Any,
                                comment_id: str, target_text: str,
                                max_scrolls: int) -> tuple[bool, str]:
    """定位目标评论并点开它的“回复”入口。成功返回 (True, '')。"""
    anchor = None
    n_comments = 0
    for _ in range(max(0, int(max_scrolls)) + 1):
        try:
            anchor = await page.evaluate(
                _XHS_ANCHOR_REPLY_ROOT, [comment_id or "", target_text or ""])
        except Exception as exc:
            return False, f"评论定位脚本异常: {exc!r}"
        if anchor and anchor.get("found"):
            break
        n_comments = int((anchor or {}).get("nComments") or 0)
        await interaction.scroll_step(page)
    if not anchor or not anchor.get("found"):
        return False, (
            f"未找到目标评论,已停止且不会降级为顶层评论"
            f"(当前页评论节点 {n_comments} 个)")
    root = page.locator('[data-mmm-xhs-root]').first
    with suppress(Exception):
        await root.scroll_into_view_if_needed(timeout=4000)
    # hover 评论行使“回复”按钮出现(真实鼠标事件 + JS 合成事件双保险),
    # 然后在多轮内重新判定按钮可见性。
    for attempt in range(4):
        with suppress(Exception):
            await root.hover(force=True, timeout=3000)
        with suppress(Exception):
            await page.evaluate(_XHS_HOVER_ROOT)
        await interaction.pause(0.35, 0.7)
        found = None
        with suppress(Exception):
            found = await page.evaluate(_XHS_FIND_REPLY_BUTTON)
        if found and found.get("found"):
            reply = page.locator('[data-mmm-xhs-reply="1"]').first
            await interaction.click_visible(reply)
            await interaction.pause(0.2, 0.45)
            return True, ""
        if attempt == 3 and found:
            return False, (
                "已找到目标评论,但未找到该评论的回复入口"
                f"(候选 {found.get('nCand', 0)} 个;操作区: {found.get('diag', '')[:120]})")
    return False, "已找到目标评论,但未找到该评论的回复入口(hover 后仍不可见)"


async def _find_comment_input(page: Any):
    locator, _ = await find_visible(page, "comment.editor")
    return locator


def _comment_response_handler(evidence: dict):
    def on_response(response):
        try:
            url = str(response.url or "").lower()
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if method == "POST" and 200 <= status < 300 and any(
                    marker in url for marker in (
                        "/comment/post", "/comment/create", "/comment/add")):
                evidence["accepted"] = True
        except Exception:
            return
    return on_response


async def comment_xhs_browser(
        mgr: BrowserManager, identity: Identity, note_id: str, xsec_token: str,
        content: str, *, target_comment_id: str = "",
        target_text: str = "", max_scrolls: int = 16,
        timeout_seconds: int = 30,
        on_submit: Any = None) -> XhsWriteOutcome:
    """Post one visible comment/reply once, preserving an uncertain result."""
    note_id = str(note_id or "").strip()
    content = str(content or "").strip()
    if not note_id:
        return XhsWriteOutcome("failed", error="缺少目标笔记 ID")
    if not content:
        return XhsWriteOutcome("failed", error="评论内容为空")
    query = urlencode({
        "xsec_token": xsec_token or "", "xsec_source": "pc_comment"})
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?{query}"
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence = {"accepted": False}
    on_response = _comment_response_handler(evidence)
    page = None

    try:
        async with mgr.visible_page(
                identity, url=url,
                keep_context=False) as page:  # 写完即关该账号 Chrome,不残留窗口
            if "login" in page.url or "passport" in page.url:
                return XhsWriteOutcome("failed", error="logged_out:账号未登录")

            if target_comment_id:
                ok_entry, entry_error = await _open_xhs_reply_entry(
                    page, interaction, target_comment_id, target_text,
                    max_scrolls=max_scrolls)
                if not ok_entry:
                    return XhsWriteOutcome("failed", error=entry_error)

            editor = await _find_comment_input(page)
            for _ in range(max(0, int(max_scrolls))):
                if editor is not None:
                    break
                await interaction.scroll_step(page)
                editor = await _find_comment_input(page)
            if editor is None:
                diagnostic = await selector_diagnostic(page, "comment.editor")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论输入框(页面可能改版)；{diagnostic}")
            if len(content) <= 80:
                await interaction.type_short(editor, content)
            else:
                await interaction.insert_long(editor, content, page=page)

            send, _ = await find_visible(page, "comment.submit")
            if send is None:
                diagnostic = await selector_diagnostic(page, "comment.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论发送按钮(页面可能改版)；{diagnostic}")
            if not await _wait_until_enabled(
                    send, interaction,
                    attempts=max(4, min(60, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="评论发送按钮当前不可用")
            # 拟人停顿:文字输入完到点击发送之间随机停 0.8-1.8 秒再提交,
            # 不影响按钮激活判定(上面已事件驱动地等待 enabled)。
            await interaction.pause(0.8, 1.8)
            existing_matches = 0
            try:
                existing_matches = int(await page.get_by_text(
                    content, exact=True).count())
            except Exception:
                pass
            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                await interaction.click_visible(
                    _SubmitLocator(send, submitted, on_submit))
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"评论已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"评论发送失败: {exc!r}")

            for _ in range(max(4, min(60, timeout_seconds * 2))):
                own_text_added = False
                try:
                    own_text = page.get_by_text(content, exact=True)
                    own_text_added = int(await own_text.count()) > existing_matches
                except Exception:
                    pass
                if own_text_added:
                    return XhsWriteOutcome("success", result="ok")
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.2, 0.45)
            detail = ("接口已接受但页面未显示新评论" if evidence["accepted"]
                      else "评论按钮已点击一次，但未取得明确成功证据")
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "评论提交后页面异常" if submitted["clicked"] else "评论页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
