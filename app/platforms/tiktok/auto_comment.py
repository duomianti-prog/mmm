"""TikTok 网页评论/回复发布(浏览器证据化,参照抖音 post_comment_browser)。

链路:打开作品页 → 展开评论区 → (回复模式)强证据闸门定位目标评论 →
点「Reply」打开内联框 → 输入 → 点「Post」→ 拦截 ``/api/comment/publish``
回包作为权威判据。

三态约定(与抖音一致):
- ``(True, "")`` 成功;
- ``(False, "write_uncertain:...")`` 请求已发出但未捕获到平台回包,
  绝不自动重试(防重复评论);
- ``(False, "logged_out:...")`` 登录态失效;
- 其余 ``(False, <原因>)`` 业务硬失败,不重试。

UI 文案固定 en-US(账号画像 pin_context_locale,Task 2)。
选择器集中在模块顶部常量,平台改版只需改一处。
"""
from __future__ import annotations

import json
import time
from typing import List, Optional, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

TT_COMMENT_PUBLISH_API = "/api/comment/publish"

# ─── 页面端 JS(离线契约测试:tests/tiktok_comment_reply_match.cjs)───

# 滚动评论区一屏:从可见评论项向上找可滚动容器,增量滚动(虚拟列表只认增量)。
_TT_SCROLL_COMMENTS = """
() => {
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  let item = null;
  const all = document.querySelectorAll('[data-e2e="comment-item"]');
  for (const it of all) { if (vis(it)) { item = it; break; } }
  if (!item) { window.scrollBy(0, 3000); return false; }
  let el = item;
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
      const step = Math.max(600, Math.round(el.clientHeight * 1.2));
      el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
      return true;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 3000);
  return false;
}
"""

# 强证据闸门:在评论列表中定位目标评论并打标 data-mmm-target="1"。
# 安全门槛(继承抖音 B 机事故教训):正文证据与可见性分离;nick 只作佐证
# 不计正文分;中等证据(前缀/截断)必须有 nick 佐证或长对齐前缀;宁可
# 报失败也绝不回复错评论。
_TT_FIND_COMMENT_ITEM = r"""
(arg) => {
  const text = (arg && typeof arg === 'object') ? (arg.text || '') : (arg || '');
  const nick = (arg && typeof arg === 'object' && arg.nick) || '';
  const cid = (arg && typeof arg === 'object' && arg.cid) || '';
  const norm = s => (s || '').replace(/[\uFE00-\uFE0F\u200B-\u200D\uFEFF]/g, '').replace(/\s+/g, '');
  const t = norm(text);
  const n = norm(nick);
  const _vis = el => {
    if (!(el.innerText || el.textContent || '').trim() && !el.querySelectorAll('img,[data-emoji]').length) return false;
    if (el && typeof el.getBoundingClientRect === 'function') {
      const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    }
    return true;
  };
  const items = [...document.querySelectorAll('[data-e2e="comment-item"]')];
  document.querySelectorAll('[data-mmm-target="1"]')
    .forEach(el => el.removeAttribute('data-mmm-target'));
  if (!t && !n && !cid) return [false, items.length];
  const prefix = t.slice(0, Math.min(14, Math.max(4, t.length - 2)));
  const m = /^\[(.+)\]$/.exec(t);
  const bare = m ? m[1] : '';
  // 表情短代码前缀评论:DOM 里表情是 <img alt="...">、文字在后续文本节点,
  // 按文档序遍历拼接逻辑连续文本,alt 占位表情。
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
      if (tag === 'IMG') { takeAlt(node); return; }
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
  const tokens = (t.match(/\[[^\]]*\]/g) || []);
  const plain = t.replace(/\[[^\]]*\]/g, '');
  const cidOnAttrs = (node, depth) => {
    if (!node || !cid) return false;
    if (node.attributes) {
      const attrs = node.attributes;
      for (let i = 0; i < attrs.length; i++) {
        const v = attrs[i].value || '';
        if (v === cid || (cid.length > 8 && v.indexOf(cid) !== -1)) return true;
      }
    }
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
    const it_prefix = logical.slice(0, 14);
    let cs = 0;
    let why = '';
    let alignPrefix = false;
    if (t && logical.indexOf(t) !== -1) { cs = 100; why = 'full-logical'; }
    else if (t && blob.indexOf(t) !== -1) { cs = 90; why = 'full-blob'; }
    else if (plain.length >= 4) {
      const blobPlain = blob.replace(/\[[^\]]*\]/g, '');
      const logicalPlain = logical.replace(/\[[^\]]*\]/g, '');
      if (logicalPlain.indexOf(plain) !== -1
          || blobPlain.indexOf(plain) !== -1) {
        cs = 85; why = 'plain';
        if (tokens.length && tokens.every(tk => logical.indexOf(tk) !== -1))
          { cs += 10; why = 'plain+tokens'; }
      }
    }
    let midWhy = '';
    if (!cs) {
      if (t && prefix && logical.indexOf(prefix) !== -1) { cs = 45; midWhy = 'prefix'; }
      else if (t && prefix && blob.indexOf(prefix) !== -1) { cs = 40; midWhy = 'blob-prefix'; }
      else if (bare && blob.indexOf(bare) !== -1) { cs = 55; midWhy = 'bare'; }
      else if (t && it_prefix.length >= 6) {
        const pos = t.indexOf(it_prefix);
        if (pos !== -1 && pos < 40) { cs = 50; midWhy = 'item-prefix'; }
      }
      why = midWhy;
      alignPrefix = !!(t && prefix && logical.indexOf(prefix) === 0)
        || (it_prefix.length >= 10 && t.indexOf(it_prefix) === 0);
    }
    const nickHit = !!(n && (logical.indexOf(n) !== -1
                             || blob.indexOf(n) !== -1));
    const cidHit = !!(cid && cidOnAttrs(el, 1));
    if (cidHit) { cs = 1000; why = 'cid'; }
    if (cs <= 0) continue;
    // 接受门槛:强证据(>=85 或 cid)直接接受;中等证据必须 nick 佐证或
    // 长对齐前缀;纯表情目标(整体一个短代码)沿用裸名即接受。
    const strong = cs >= 85;
    const pureEmoji = !!bare && plain.length === 0;
    const alignedLong = alignPrefix && (prefix.length >= 10
                                        || it_prefix.length >= 10);
    const accepted = strong || pureEmoji
      || (midWhy && (nickHit || alignedLong));
    if (!accepted) continue;
    // 同分时优先可见项与不含嵌套评论的叶子项(父项 innerText 含折叠子评论
    // 预览,叶子项保证回复到精确楼层)
    const leaf = !el.querySelector
      || !el.querySelector('[data-e2e="comment-item"]');
    let score = cs + (leaf ? 2 : 0);
    if (_vis(el)) score += 3;
    if (nickHit) score += 1;
    cand.push({ el, score, why });
  }
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

_TT_CLEAR_TARGET_MARK = """
() => {
  document.querySelectorAll('[data-mmm-target="1"]')
    .forEach(n => n.removeAttribute('data-mmm-target'));
}
"""

# 评论区可见状态与可见评论输入框(TikTok 预渲染隐藏 DOM,必须按可见性判断)。
_TT_FIND_VISIBLE_EDITOR = """
() => {
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  let items = 0;
  document.querySelectorAll('[data-e2e="comment-item"]').forEach(el => {
    if (vis(el)) items += 1;
  });
  let editor = false;
  const sels = ['[data-e2e="comment-input"]', '[contenteditable="true"]'];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (!vis(el)) continue;
      const ph = (el.getAttribute('data-placeholder')
        || el.getAttribute('placeholder') || '').toLowerCase();
      if (ph.indexOf('search') !== -1 || ph.indexOf('搜索') !== -1) continue;
      el.setAttribute('data-mmm-editor', '1');
      editor = true;
      break;
    }
    if (editor) break;
  }
  return { editor, items };
}
"""

# 目标评论已打标时,在其内部找可见「Reply」按钮打标 data-mmm-replybtn。
_TT_FIND_REPLY_BUTTON = """
() => {
  const target = document.querySelector('[data-mmm-target="1"]');
  if (!target) return { found: false, reason: 'no-target-mark' };
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  const direct = target.querySelector('[data-e2e="comment-reply"]');
  if (direct && vis(direct)) {
    direct.setAttribute('data-mmm-replybtn', '1');
    return { found: true, how: 'e2e' };
  }
  const cands = target.querySelectorAll('button,[role="button"],span,div,p');
  for (const el of cands) {
    const txt = (el.innerText || el.textContent || '').trim();
    if (txt === 'Reply' && vis(el)) {
      el.setAttribute('data-mmm-replybtn', '1');
      return { found: true, how: 'text' };
    }
  }
  return { found: false, reason: 'no-reply-btn' };
}
"""

# 点「Reply」后等内联回复框出现:优先目标项内部的 contenteditable,
# 其次 placeholder 含 "Reply to" 的编辑器。返回 reason 供排障。
_TT_FIND_REPLY_EDITOR = """
() => {
  document.querySelectorAll('[data-mmm-editor]')
    .forEach(n => n.removeAttribute('data-mmm-editor'));
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  const target = document.querySelector('[data-mmm-target="1"]');
  if (!target) return { found: false, reason: 'no-target-mark' };
  const inTarget = target.querySelectorAll('[contenteditable="true"]');
  for (const el of inTarget) {
    if (vis(el)) {
      el.setAttribute('data-mmm-editor', '1');
      return { found: true, how: 'inline-target' };
    }
  }
  const all = document.querySelectorAll('[contenteditable="true"]');
  for (const el of all) {
    const ph = (el.getAttribute('data-placeholder')
      || el.getAttribute('placeholder') || '');
    if (ph.indexOf('Reply to') !== -1 && vis(el)) {
      el.setAttribute('data-mmm-editor', '1');
      return { found: true, how: 'reply-placeholder' };
    }
  }
  return { found: false, reason: 'no-reply-editor' };
}
"""

# 发送按钮:TikTok 是「Post」文本按钮(可访问性 aria-label="Post")。
# 禁用态(未输入)返回 enabled=false,Python 侧继续轮询。
_TT_FIND_SEND_BUTTON = r"""
() => {
  document.querySelectorAll('[data-mmm-send]')
    .forEach(n => n.removeAttribute('data-mmm-send'));
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  const editor = document.querySelector('[data-mmm-editor="1"]');
  const scope = editor
    ? (editor.closest('form') || editor.parentElement || document)
    : document;
  const roots = scope === document ? [document] : [scope, document];
  for (const root of roots) {
    const cands = root.querySelectorAll(
      '[data-e2e="comment-post"],button,[role="button"],div,span');
    for (const el of cands) {
      if (!vis(el)) continue;
      const txt = (el.innerText || el.textContent || '').trim();
      const aria = (el.getAttribute('aria-label') || '').trim();
      const isPost = el.matches && el.matches('[data-e2e="comment-post"]')
        || txt === 'Post' || aria === 'Post';
      if (!isPost) continue;
      const cls = (typeof el.className === 'string' ? el.className : '');
      const disabled = el.disabled
        || /disabled|disable/i.test(cls)
        || el.getAttribute('aria-disabled') === 'true';
      el.setAttribute('data-mmm-send', '1');
      return { found: true, enabled: !disabled };
    }
  }
  return { found: false };
}
"""

# 折叠子评论「View N replies / View more replies」逐个打标,Python 侧点击。
_TT_FIND_EXPAND_REPLIES = r"""
() => {
  document.querySelectorAll('[data-mmm-expand]')
    .forEach(n => n.removeAttribute('data-mmm-expand'));
  const vis = el => {
    try { const r = el.getBoundingClientRect(); return r.width >= 2 && r.height >= 2; }
    catch (e) { return true; }
  };
  const cands = document.querySelectorAll('button,[role="button"],span,div,p');
  for (const el of cands) {
    const txt = (el.innerText || el.textContent || '').trim();
    if (/^View (more |\d+ )?repl/i.test(txt) && vis(el)) {
      el.setAttribute('data-mmm-expand', '1');
      return { found: true };
    }
  }
  return { found: false };
}
"""

# 发表后 DOM 佐证:编辑器卸载且回复文本出现在评论区 → settled。
# 编辑器仍存活且含文本 → posted=false(不得误判成功)。
_TT_VERIFY_COMMENT_POST = r"""
(text) => {
  const norm = s => (s || '').replace(/\s+/g, '');
  const want = norm(text);
  const editor = document.querySelector('[data-mmm-editor="1"]');
  const alive = !!(editor && document.contains(editor));
  const empty = alive ? !norm(editor.innerText || editor.textContent || '') : true;
  let items = 0, hit = 0;
  document.querySelectorAll('[data-e2e="comment-item"]').forEach(el => {
    items += 1;
    const blob = norm((el.innerText || '') + '\n' + (el.textContent || ''));
    if (want && blob.indexOf(want) !== -1) hit += 1;
  });
  const posted = !alive && hit > 0;
  return { posted, settled: posted, alive, empty, items, nRoots: hit };
}
"""

# 诊断:导出页面可交互元素概况,评论区打不开时随错误返回。
_TT_DIAG_INPUTS = """
() => {
  const out = [];
  document.querySelectorAll('[contenteditable],textarea,input').forEach(el => {
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {};
    out.push([el.tagName, el.getAttribute('data-e2e') || '',
      el.getAttribute('data-placeholder') || el.getAttribute('placeholder') || '',
      Math.round(r.width || 0), Math.round(r.height || 0)].join('|'));
  });
  return out.slice(0, 12).join(' ; ');
}
"""


def tiktok_video_url(aweme_id: str, handle: str = "") -> str:
    """作品页 URL:有 handle 用规范形态,否则裸 /video/<id>。"""
    handle = (handle or "").strip().lstrip("@")
    if handle:
        return f"https://www.tiktok.com/@{handle}/video/{aweme_id}"
    return f"https://www.tiktok.com/video/{aweme_id}"


async def comment_tiktok_browser(
        mgr: BrowserManager,
        identity: Identity,
        aweme_id: str,
        content: str,
        *,
        handle: str = "",
        reply_to_text: str = "",
        target_nick: str = "",
        target_cid: str = "",
        require_reply: bool = False,
        headed: bool = True,
        settle_ms: int = 1800,
        verify_wait_seconds: int = 300,
        on_submit=None,
) -> Tuple[bool, str]:
    """用账号持久 profile(已含登录态)打开作品页,发评论/回复评论。

    on_submit:点「Post」后立刻回调(引擎据此把任务标记为已提交,
    进程崩溃也不会重复发)。
    返回 (ok, error);error 以 ``write_uncertain:`` 开头表示请求已发出
    但未捕获平台回包,绝不自动重试。
    """
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    aweme_id = str(aweme_id or "").strip()
    if not aweme_id:
        return False, "missing_aweme_id"
    if require_reply and not (reply_to_text or "").strip():
        return False, "缺少目标评论原文，已跳过回复"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)

    # 权威判据:拦截 comment/publish 接口回包。seen 在 URL 命中即置位:
    # 即使回包解析失败,也说明请求已发出、结果未知(uncertain),绝不能
    # 当作"未提交"重试。
    pub = {"seen": False, "known": False, "ok": False, "code": None, "msg": "",
           "http": None, "url": "", "raw": ""}

    async def on_response(resp):
        if TT_COMMENT_PUBLISH_API not in resp.url or pub["seen"]:
            return
        pub["seen"] = True
        try:
            pub["http"] = resp.status
            pub["url"] = resp.url[-60:]
        except Exception:
            pass
        raw = ""
        try:
            raw = (await resp.text()).strip().lstrip("﻿")
        except Exception:
            raw = ""
        pub["raw"] = raw[:120]
        data = None
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    j = raw.find("{")
                    if j >= 0:
                        data = json.loads(raw[j:])
                except Exception:
                    data = None
        if isinstance(data, dict):
            pub["known"] = True
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            code = data.get("status_code", inner.get("status_code"))
            if code is None:
                code = data.get("statusCode", inner.get("statusCode"))
            pub["code"] = code
            pub["ok"] = code == 0
            pub["msg"] = str(data.get("status_msg")
                             or data.get("statusMsg")
                             or inner.get("status_msg")
                             or inner.get("statusMsg") or "")

    page.on("response", on_response)

    try:
        await page.goto(tiktok_video_url(aweme_id, handle),
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(min(settle_ms, 1200))
        last_url = page.url
        if "/login" in last_url or "passport" in last_url:
            return False, "logged_out:账号未登录,无法发评论"
        # 等评论区出现(视频页评论区默认展开,不依赖额外点击入口)
        editor_ready = False
        for _ in range(10):
            try:
                st = await page.evaluate(_TT_FIND_VISIBLE_EDITOR) or {}
            except Exception:
                st = {}
            if st.get("editor"):
                editor_ready = True
                break
            await page.wait_for_timeout(500)

        editor = None
        shown_count = 0
        if reply_to_text or target_nick or target_cid:
            # 回复模式:强证据闸门定位目标评论。滚动翻页直到命中或双停滞
            # (数量不增+近底部);未命中则展开折叠子评论再扫一轮。
            found = False
            hit_why = ""
            per_phase = 60

            async def _scan(rounds: int) -> Tuple[bool, int, str]:
                last_count = -1
                stale = 0
                why = ""
                for j in range(rounds):
                    try:
                        hit_n = await page.evaluate(_TT_FIND_COMMENT_ITEM, {
                            "text": (reply_to_text or "")[:60],
                            "nick": target_nick or "",
                            "cid": target_cid or "",
                        })
                    except Exception:
                        hit_n = [False, 0]
                    hit = bool(hit_n and hit_n[0])
                    cnt = int((hit_n or [False, 0])[1] or 0)
                    if hit:
                        why = str((hit_n or [])[2] or "")
                        return True, cnt, why
                    try:
                        await page.evaluate(_TT_SCROLL_COMMENTS)
                    except Exception:
                        pass
                    await page.wait_for_timeout(450)
                    if cnt == last_count:
                        stale += 1
                    else:
                        stale = 0
                    last_count = cnt
                    if stale >= 4 and j >= 6:
                        break
                return False, max(last_count, 0), ""

            found, phase_n, hit_why = await _scan(per_phase)
            shown_count = max(shown_count, phase_n)
            if not found:
                # 折叠子评论兜底:逐个展开「View N replies」再扫
                expanded = 0
                for _eb in range(24):
                    try:
                        er = await page.evaluate(_TT_FIND_EXPAND_REPLIES) or {}
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
                    found, phase_n, hit_why = await _scan(24)
                    shown_count = max(shown_count, phase_n)
            if found:
                try:
                    await page.evaluate(
                        "() => { const el = document.querySelector"
                        "('[data-mmm-target=\"1\"]');"
                        " if (el) el.scrollIntoView({block:'center'}); }")
                except Exception:
                    pass
                await page.wait_for_timeout(400)
                try:
                    reply_marked = False
                    try:
                        rb = await page.evaluate(_TT_FIND_REPLY_BUTTON) or {}
                        reply_marked = bool(rb.get("found"))
                    except Exception:
                        rb = {}
                    if reply_marked:
                        await page.locator(
                            '[data-mmm-replybtn="1"]').first.click(timeout=2500)
                    else:
                        item = page.locator(
                            '[data-e2e="comment-item"][data-mmm-target="1"]').first
                        rbtn = item.locator(
                            '[data-e2e="comment-reply"]').first
                        if not await rbtn.count():
                            rbtn = item.get_by_text("Reply", exact=True).first
                        await rbtn.click(timeout=2500)
                    reply_info = {}
                    for _ in range(6):
                        reply_info = await page.evaluate(
                            _TT_FIND_REPLY_EDITOR) or {}
                        if reply_info.get("found"):
                            break
                        await page.wait_for_timeout(500)
                    if reply_info.get("found"):
                        marked = page.locator('[data-mmm-editor="1"]').first
                        if await marked.count():
                            editor = marked
                except Exception:
                    editor = None
                finally:
                    try:
                        await page.evaluate(_TT_CLEAR_TARGET_MARK)
                    except Exception:
                        pass
            if editor is None and require_reply:
                if shown_count <= 0:
                    diag = ""
                    try:
                        diag = await page.evaluate(_TT_DIAG_INPUTS)
                    except Exception:
                        pass
                    return False, (
                        "评论区未能加载(0 条评论可见),无法定位回复目标。"
                        f"页面输入元素:{diag or '无'}")
                return False, (
                    f"未找到目标评论回复区(已加载约 {shown_count} 条评论,"
                    "强证据闸门未命中),已跳过本次回复")
        else:
            # 顶层评论:直接用底部主输入框
            if editor_ready:
                marked = page.locator('[data-mmm-editor="1"]').first
                if await marked.count():
                    editor = marked
            if editor is None:
                try:
                    loc = page.locator('[data-e2e="comment-input"]').first
                    if await loc.count():
                        editor = loc
                except Exception:
                    pass
        if editor is None:
            diag = ""
            try:
                diag = await page.evaluate(_TT_DIAG_INPUTS)
            except Exception:
                pass
            return False, f"未找到评论输入框。页面输入元素:{diag or '无'}"

        # 输入文案:点击聚焦 → fill 兜底逐键输入
        await editor.click(timeout=3000)
        await page.wait_for_timeout(200)
        typed = False
        try:
            await editor.fill(content, timeout=3000)
            typed = True
        except Exception:
            typed = False
        if not typed:
            try:
                await editor.press("Control+a")
                await editor.press("Backspace")
            except Exception:
                pass
            await page.keyboard.type(content, delay=20)
        await page.wait_for_timeout(400)

        # 等发送按钮可用并点击
        posted_clicked = False
        for _ in range(8):
            try:
                sb = await page.evaluate(_TT_FIND_SEND_BUTTON) or {}
            except Exception:
                sb = {}
            if sb.get("found") and sb.get("enabled"):
                try:
                    await page.locator('[data-mmm-send="1"]').first.click(
                        timeout=2500)
                    posted_clicked = True
                    break
                except Exception:
                    pass
            await page.wait_for_timeout(400)
        if not posted_clicked:
            return False, "发送按钮不可用(输入后「Post」未激活)"
        if callable(on_submit):
            try:
                on_submit()
            except Exception:
                pass

        # 裁决:优先接口回包(权威),无回包时用 DOM 佐证区分成功/未知。
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not pub["seen"]:
            await page.wait_for_timeout(250)
        if pub["seen"]:
            if pub["known"]:
                if pub["ok"]:
                    return True, ""
                return False, (
                    f"平台拒绝(status_code={pub['code']}"
                    f"{', ' + pub['msg'] if pub['msg'] else ''})")
            return False, (
                "write_uncertain:评论请求已发出但平台回包无法解析"
                f"(http={pub['http']}, raw={pub['raw'][:80]!r})")
        # 无回包:DOM 佐证(回复文本出现在评论区且编辑器已卸载=成功)
        vdeadline = time.monotonic() + min(verify_wait_seconds, 20)
        dom_settled = False
        while time.monotonic() < vdeadline:
            try:
                v = await page.evaluate(_TT_VERIFY_COMMENT_POST, content) or {}
            except Exception:
                v = {}
            if v.get("posted"):
                dom_settled = True
                break
            if v.get("alive") and not v.get("empty"):
                await page.wait_for_timeout(500)
                continue
            break
        if dom_settled:
            return True, ""
        return False, (
            "write_uncertain:评论已提交但未捕获到平台回包,"
            "DOM 佐证亦未确认,请勿重试")
    finally:
        try:
            await page.close()
        except Exception:
            pass
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass
