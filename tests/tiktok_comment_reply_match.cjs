// Task 10: 离线验证 TikTok 评论/回复定位 JS 的强证据闸门。
// 安全语义与抖音 douyin_comment_reply_match.cjs 一致:
// 假找到(nick-only/短巧合子串)必须拒绝,cid 直接命中,嵌套叶子优先。
const assert = require('node:assert/strict');
const fs = require('node:fs');

const pySource = fs.readFileSync('app/platforms/tiktok/auto_comment.py', 'utf8');
function extractJs(name) {
  const m = new RegExp(name + '\\s*=\\s*r?"""([\\s\\S]*?)"""').exec(pySource);
  assert.ok(m, name + ' JS must exist in auto_comment.py');
  return eval('(' + m[1].trim() + ')');
}
const findItem = extractJs('_TT_FIND_COMMENT_ITEM');
const findVisibleEditor = extractJs('_TT_FIND_VISIBLE_EDITOR');
const findReplyButton = extractJs('_TT_FIND_REPLY_BUTTON');
const findReplyEditor = extractJs('_TT_FIND_REPLY_EDITOR');
const findSendButton = extractJs('_TT_FIND_SEND_BUTTON');
const findExpand = extractJs('_TT_FIND_EXPAND_REPLIES');
const verifyPost = extractJs('_TT_VERIFY_COMMENT_POST');

// ── DOM 桩 ──────────────────────────────────────────────────────────

function query(kids, sel) {
  const all = [];
  const walk = n => {
    if (n.nodeType !== 1) return;
    all.push(n);
    (n.childNodes || []).forEach(walk);
  };
  (kids || []).forEach(walk);
  if (sel === '*') return all;
  const e2e = /^\[data-e2e="(.+)"\]$/.exec(sel);
  if (e2e) return all.filter(n => n.getAttribute('data-e2e') === e2e[1]);
  if (sel.includes('comment-item'))
    return all.filter(n => n.getAttribute('data-e2e') === 'comment-item');
  if (sel.includes('contenteditable'))
    return all.filter(n => n.getAttribute('contenteditable') === 'true');
  if (sel.includes('img') || sel.includes('emoji'))
    return all.filter(n => n.tagName === 'IMG' || n.hasAttribute('data-emoji'));
  if (sel.includes('button')) return all;
  return [];
}

function node({ tag = 'DIV', text = '', attrs = {}, kids = [],
                visible = true, cls = '', outer = '' } = {}) {
  const map = new Map(Object.entries(attrs));
  const el = {
    nodeType: 1,
    tagName: tag,
    className: cls,
    innerText: text,
    textContent: text,
    disabled: !!attrs.disabled,
    parentElement: null,
    childNodes: [],
    closest: () => null,
    getAttribute: k => (map.has(k) ? map.get(k) : null),
    setAttribute(k, v) { map.set(k, v); sync(); },
    removeAttribute(k) { map.delete(k); sync(); },
    hasAttribute: k => map.has(k),
    matches(sel) {
      return sel.split(',').some(s => {
        s = s.trim();
        const m = /^\[data-e2e="(.+)"\]$/.exec(s);
        if (m) return map.get('data-e2e') === m[1];
        if (s === 'button') return tag === 'BUTTON';
        if (s === '[role="button"]') return map.get('role') === 'button';
        return false;
      });
    },
    getBoundingClientRect: () =>
      visible ? { width: 50, height: 20 } : { width: 0, height: 0 },
    querySelector(sel) { return el.querySelectorAll(sel)[0] || null; },
    querySelectorAll(sel) { return query(kids, sel); },
  };
  function sync() {
    el.attributes = [...map.entries()].map(([name, value]) => ({ name, value }));
    el.outerHTML = outer ||
      [...map.entries()].map(([k, v]) => k + '="' + v + '"').join(' ') +
      '|' + text;
  }
  sync();
  if (text) el.childNodes.push({ nodeType: 3, nodeValue: text });
  el.childNodes.push(...kids);
  return el;
}

function commentItem(opts) {
  return node({ ...opts, attrs: { 'data-e2e': 'comment-item',
                                  ...(opts.attrs || {}) } });
}

function img(attrs) {
  return node({ tag: 'IMG', attrs });
}

function installDocument({ items = [], editors = [], buttons = [],
                           spans = [] } = {}) {
  const all = [...items, ...editors, ...buttons, ...spans];
  const bySel = sel => {
    if (sel.includes('mmm-target'))
      return all.filter(el => el.getAttribute('data-mmm-target') === '1');
    if (sel.includes('mmm-editor'))
      return all.filter(el => el.getAttribute('data-mmm-editor') === '1');
    if (sel.includes('mmm-send'))
      return all.filter(el => el.getAttribute('data-mmm-send') === '1');
    if (sel.includes('comment-item')) return items;
    if (sel.includes('comment-input')) return editors;
    if (sel.includes('contenteditable')) return editors;
    if (sel.includes('button') || sel.includes('span'))
      return [...buttons, ...spans];
    return [];
  };
  global.document = {
    querySelectorAll: bySel,
    querySelector: sel => bySel(sel)[0] || null,
    contains: el => !!el,
  };
}

// ── _TT_FIND_COMMENT_ITEM ───────────────────────────────────────────

// 1) 全文命中:打标并返回真实评论数
{
  const target = commentItem({ text: 'Great tutorial, learned a lot' });
  const other = commentItem({ text: 'first' });
  installDocument({ items: [other, target] });
  const [hit, count] = findItem({ text: 'Great tutorial, learned a lot' });
  assert.equal(hit, true);
  assert.equal(count, 2);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
  assert.equal(other.getAttribute('data-mmm-target'), null);
}

// 2) 假找到拒绝:前缀中段巧合命中(非对齐)且无 nick 佐证
//    (B 机误回复事故教训:中等证据必须另有佐证)
{
  const item = commentItem({ text: '真的不是这样的好事啊大家说对吧' });
  installDocument({ items: [item] });
  const [hit] = findItem({ text: '这样的好事啊大家说对吗' });
  assert.equal(hit, false);
  assert.equal(item.getAttribute('data-mmm-target'), null);
}

// 3) 同样的中等证据,有 nick 佐证则接受
{
  const item = commentItem({ text: ' camper 真的不是这样的好事啊大家说对吧' });
  installDocument({ items: [item] });
  const [hit, , why] = findItem({ text: '这样的好事啊大家说对吗',
                                  nick: 'camper' });
  assert.equal(hit, true);
  assert.equal(why, 'prefix');
}

// 4) nick-only 一律拒绝:昵称从不构成正文证据
{
  const item = commentItem({ text: 'some random comment by camper' });
  installDocument({ items: [item] });
  const [hit] = findItem({ text: '', nick: 'camper' });
  assert.equal(hit, false);
  assert.equal(item.getAttribute('data-mmm-target'), null);
}

// 5) 长对齐前缀(>=10)无 nick 也接受
{
  const item = commentItem({ text: '这是一个非常长的评论内容啊哈哈哈' });
  installDocument({ items: [item] });
  const [hit, , why] = findItem({ text: '这是一个非常长的评论内容啊好吧' });
  assert.equal(hit, true);
  assert.equal(why, 'prefix');
}

// 6) cid 直接命中(1000 分),正文不匹配也接受
{
  const item = commentItem({ text: 'whatever',
                             attrs: { 'data-cid': '741000111222' } });
  installDocument({ items: [item] });
  const [hit, , why] = findItem({ text: '不存在的内容', cid: '741000111222' });
  assert.equal(hit, true);
  assert.equal(why, 'cid');
}

// 7) cid-html 兜底:属性查不到但 outerHTML 含 cid
{
  const item = commentItem({ text: 'plain text',
                             outer: 'data-x="cid-999888777666"' });
  installDocument({ items: [item] });
  const [hit, , why] = findItem({ cid: '999888777666' });
  assert.equal(hit, true);
  assert.equal(why, 'cid-html');
}

// 8) 嵌套评论:叶子项优先于含折叠预览的父项
{
  const child = commentItem({ text: '重复内容' });
  const parent = commentItem({ text: '重复内容', kids: [child] });
  installDocument({ items: [parent, child] });
  assert.equal(findItem('重复内容')[0], true);
  assert.equal(child.getAttribute('data-mmm-target'), '1');
  assert.equal(parent.getAttribute('data-mmm-target'), null);
}

// 9) 同分可见项优先
{
  const hidden = commentItem({ text: 'same text', visible: false });
  const shown = commentItem({ text: 'same text' });
  installDocument({ items: [hidden, shown] });
  assert.equal(findItem('same text')[0], true);
  assert.equal(shown.getAttribute('data-mmm-target'), '1');
  assert.equal(hidden.getAttribute('data-mmm-target'), null);
}

// 10) 表情短代码:DOM 里 <img alt="[fire]"> 命中
{
  const item = commentItem({ kids: [img({ alt: '[fire]' })] });
  installDocument({ items: [item] });
  assert.equal(findItem('[fire]')[0], true);
  assert.equal(item.getAttribute('data-mmm-target'), '1');
}

// 11) 重新查找先清旧标记
{
  const first = commentItem({ text: 'round one target' });
  const second = commentItem({ text: 'round two target' });
  installDocument({ items: [first, second] });
  assert.equal(findItem('round one target')[0], true);
  assert.equal(first.getAttribute('data-mmm-target'), '1');
  assert.equal(findItem('round two target')[0], true);
  assert.equal(first.getAttribute('data-mmm-target'), null);
  assert.equal(second.getAttribute('data-mmm-target'), '1');
}

// 12) 未命中返回真实计数(区分"评论区没加载"与"没匹配到")
{
  installDocument({ items: [commentItem({ text: 'a' }),
                            commentItem({ text: 'b' })] });
  const [hit, count] = findItem('zzz 不存在 zzz');
  assert.equal(hit, false);
  assert.equal(count, 2);
}

// ── _TT_FIND_VISIBLE_EDITOR ─────────────────────────────────────────

// 13) 可见 contenteditable + 可见评论计数
{
  const editor = node({ attrs: { contenteditable: 'true',
                                 'data-placeholder': 'Add comment...' } });
  installDocument({
    editors: [editor],
    items: [commentItem({ text: 'a' }),
            commentItem({ text: 'b', visible: false })],
  });
  const r = findVisibleEditor();
  assert.equal(r.editor, true);
  assert.equal(r.items, 1);
  assert.equal(editor.getAttribute('data-mmm-editor'), '1');
}

// 14) 搜索框(placeholder 含 search)必须排除
{
  const search = node({ attrs: { contenteditable: 'true',
                                 placeholder: 'Search' } });
  installDocument({ editors: [search], items: [] });
  assert.equal(findVisibleEditor().editor, false);
  assert.equal(search.getAttribute('data-mmm-editor'), null);
}

// ── _TT_FIND_REPLY_BUTTON ───────────────────────────────────────────

// 15) 目标项内 data-e2e="comment-reply" 优先
{
  const btn = node({ tag: 'BUTTON', text: 'Reply',
                     attrs: { 'data-e2e': 'comment-reply' } });
  const target = commentItem({ text: 't', kids: [btn] });
  target.setAttribute('data-mmm-target', '1');
  installDocument({ items: [target] });
  const r = findReplyButton();
  assert.equal(r.found, true);
  assert.equal(r.how, 'e2e');
  assert.equal(btn.getAttribute('data-mmm-replybtn'), '1');
}

// 16) 无 e2e 时按文本 "Reply" 兜底;无目标标记时报 no-target-mark
{
  const span = node({ tag: 'SPAN', text: 'Reply' });
  const target = commentItem({ text: 't', kids: [span] });
  target.setAttribute('data-mmm-target', '1');
  installDocument({ items: [target] });
  const r = findReplyButton();
  assert.equal(r.found, true);
  assert.equal(r.how, 'text');

  installDocument({ items: [commentItem({ text: 'x' })] });
  assert.equal(findReplyButton().reason, 'no-target-mark');
}

// ── _TT_FIND_REPLY_EDITOR ───────────────────────────────────────────

// 17) 目标项内 contenteditable 优先;否则 placeholder 含 "Reply to"
{
  const inline = node({ attrs: { contenteditable: 'true' } });
  const target = commentItem({ text: 't', kids: [inline] });
  target.setAttribute('data-mmm-target', '1');
  installDocument({ items: [target] });
  const r = findReplyEditor();
  assert.equal(r.found, true);
  assert.equal(r.how, 'inline-target');
}
{
  const target = commentItem({ text: 't' });
  target.setAttribute('data-mmm-target', '1');
  const ed = node({ attrs: { contenteditable: 'true',
                             'data-placeholder': 'Reply to camper' } });
  installDocument({ items: [target], editors: [ed] });
  const r = findReplyEditor();
  assert.equal(r.found, true);
  assert.equal(r.how, 'reply-placeholder');
  assert.equal(ed.getAttribute('data-mmm-editor'), '1');
}

// ── _TT_FIND_SEND_BUTTON ────────────────────────────────────────────

// 18) "Post" 按钮:启用/禁用两态
{
  const post = node({ tag: 'BUTTON', text: 'Post' });
  installDocument({ buttons: [post] });
  const r = findSendButton();
  assert.equal(r.found, true);
  assert.equal(r.enabled, true);
  assert.equal(post.getAttribute('data-mmm-send'), '1');
}
{
  const post = node({ tag: 'BUTTON', text: 'Post', cls: 'disabled' });
  installDocument({ buttons: [post] });
  const r = findSendButton();
  assert.equal(r.found, true);
  assert.equal(r.enabled, false);
}

// 19) aria-label="Post" 也认;无关按钮不误中
{
  const post = node({ tag: 'DIV', attrs: { 'aria-label': 'Post' } });
  const other = node({ tag: 'BUTTON', text: 'Share' });
  installDocument({ buttons: [other, post] });
  const r = findSendButton();
  assert.equal(r.found, true);
  assert.equal(post.getAttribute('data-mmm-send'), '1');
  assert.equal(other.getAttribute('data-mmm-send'), null);
}

// ── _TT_FIND_EXPAND_REPLIES ─────────────────────────────────────────

// 20) "View N replies" / "View more replies" 命中,其他文本不误中
{
  const expand = node({ tag: 'SPAN', text: 'View 12 replies' });
  installDocument({ spans: [expand] });
  assert.equal(findExpand().found, true);
  assert.equal(expand.getAttribute('data-mmm-expand'), '1');
}
{
  const expand = node({ tag: 'SPAN', text: 'View more replies' });
  const noise = node({ tag: 'SPAN', text: 'View profile' });
  installDocument({ spans: [noise, expand] });
  assert.equal(findExpand().found, true);
  installDocument({ spans: [noise] });
  assert.equal(findExpand().found, false);
}

// ── _TT_VERIFY_COMMENT_POST ─────────────────────────────────────────

// 21) 编辑器已卸载且回复文本入评论区 → posted
{
  installDocument({ items: [commentItem({ text: 'my reply text' })] });
  assert.equal(verifyPost('my reply text').posted, true);
}

// 22) 编辑器仍存活且含文本 → posted=false(不得误判成功)
{
  const editor = node({ text: 'my reply text',
                        attrs: { contenteditable: 'true' } });
  editor.setAttribute('data-mmm-editor', '1');
  installDocument({ editors: [editor], items: [] });
  const r = verifyPost('my reply text');
  assert.equal(r.posted, false);
  assert.equal(r.alive, true);
  assert.equal(r.empty, false);
}

// 23) 编辑器卸载但文本未入评论区 → posted=false
{
  installDocument({ items: [commentItem({ text: 'someone else' })] });
  assert.equal(verifyPost('my reply text').posted, false);
}

console.log('tiktok_comment_reply_match: all 23 cases passed');
