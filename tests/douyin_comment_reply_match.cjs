// 离线验证抖音「回复目标评论」定位 JS 对纯文本/纯表情/手势评论都能命中。
// 真实页面里表情常渲染成 <img alt="[赞]"> 而非文本节点,旧的 has_text 定位
// 会对这类评论直接失败("未找到目标评论回复区")。
const assert = require('node:assert/strict');
const fs = require('node:fs');

const pySource = fs.readFileSync('app/browser/fetcher.py', 'utf8');
const m = /_FIND_COMMENT_ITEM\s*=\s*r?"""([\s\S]*?)"""/.exec(pySource);
assert.ok(m, '_FIND_COMMENT_ITEM JS must exist in fetcher.py');
const findItem = eval('(' + m[1].trim() + ')');

function imgNode(attrs) {
  const map = new Map(Object.entries(attrs || {}));
  return {
    getAttribute: k => (map.has(k) ? map.get(k) : null),
    setAttribute(k, v) { map.set(k, v); },
    removeAttribute(k) { map.delete(k); },
    querySelectorAll: () => [],
  };
}

function commentItem({ text = '', imgs = [] }) {
  const map = new Map();
  map.set('data-e2e', 'comment-item');
  return {
    innerText: text,
    textContent: text,
    imgs,
    getAttribute: k => (map.has(k) ? map.get(k) : null),
    setAttribute(k, v) { map.set(k, v); },
    removeAttribute(k) { map.delete(k); },
    querySelectorAll: () => imgs,
    // 简化版:从 imgs 里找第一个带任一查询属性的节点
    querySelector() {
      for (const im of imgs) {
        for (const a of ['data-cid', 'data-comment-id', 'cid']) {
          if (im.getAttribute(a)) return im;
        }
      }
      return null;
    },
  };
}

function installDocument(items) {
  global.document = {
    querySelectorAll(sel) {
      if (sel.includes('data-mmm-target')) {
        return items.filter(el => el.getAttribute('data-mmm-target') === '1');
      }
      if (sel.includes('comment-item')) return items;
      return [];
    },
  };
}

// 1) 普通文本评论:子串包含即命中,并打标记;返回 [hit, 页面真实评论数]
// v1.6.5 起中等证据(短前缀)需 nick 佐证,故同时传入评论作者昵称
{
  const target = commentItem({ text: '某博主 博主说得太对了,学习了' });
  const other = commentItem({ text: '前排围观' });
  installDocument([other, target]);
  assert.deepEqual(findItem({ text: '学习了', nick: '某博主' })[0], true);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
  assert.equal(other.getAttribute('data-mmm-target'), null);
}

// 2) 纯手势表情评论:接口文本是 [赞],DOM 里只有 <img alt="[赞]">
{
  const target = commentItem({ imgs: [imgNode({ alt: '[赞]' })] });
  const other = commentItem({ text: '哈哈哈哈' });
  installDocument([other, target]);
  assert.equal(findItem('[赞]')[0], true);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
}

// 3) Unicode emoji(带变体选择符 U+FE0F)也能命中 DOM 里的裸 emoji
{
  const target = commentItem({ text: '\u{1F44D}' }); // 👍
  installDocument([target]);
  assert.equal(findItem('\u{1F44D}\uFE0F')[0], true); // 👍️
}

// 4) 表情 img 的 alt 是不带括号的裸名 "赞" 时,目标 [赞] 仍命中
{
  const target = commentItem({ imgs: [imgNode({ alt: '赞' })] });
  installDocument([target]);
  assert.equal(findItem('[赞]')[0], true);
}

// 5) 多个手势短代码:只标记真正匹配的那条
{
  const clap = commentItem({ imgs: [imgNode({ alt: '[鼓掌]' })] });
  const thumbs = commentItem({ imgs: [imgNode({ alt: '[赞]' })] });
  installDocument([clap, thumbs]);
  assert.equal(findItem('[赞]')[0], true);
  assert.equal(clap.getAttribute('data-mmm-target'), null);
  assert.equal(thumbs.getAttribute('data-mmm-target'), '1');
}

// 6) 重新查找时先清掉旧标记,避免上一轮的命中残留
{
  const first = commentItem({ text: '上一轮目标' });
  const second = commentItem({ text: '这一轮目标' });
  installDocument([first, second]);
  assert.equal(findItem('上一轮目标')[0], true);
  assert.equal(first.getAttribute('data-mmm-target'), '1');
  assert.equal(findItem('这一轮目标')[0], true);
  assert.equal(first.getAttribute('data-mmm-target'), null);
  assert.equal(second.getAttribute('data-mmm-target'), '1');
}

// 7) 不存在的目标返回 [false, 真实评论数],且无误标记(用于区分
// "评论区没加载(0条)"与"加载了但没匹配到")
// 注意:v1.5.4 起 _vis 从硬过滤改为软加分,隐藏项也能命中,
// 所以这里用完全不存在的目标(非 [赞] 这种可能命中隐藏表情的)
{
  const a = commentItem({ text: '今天天气不错' });
  const b = commentItem({ imgs: [imgNode({ alt: '[比心]' })] });
  installDocument([a, b]);
  assert.equal(findItem('完全不存在的评论内容xyz')[0], false);
  assert.equal(a.getAttribute('data-mmm-target'), null);
  assert.equal(b.getAttribute('data-mmm-target'), null);
}

// 8) v1.6.5 安全收紧:nick 单独命中【不得】接受。旧逻辑 nick+30 可见
// +500,一条"回复 @目标昵称"的子评论正文完全不对也会被当成目标并回复
// (B 机第三次回投"假找到"事故的根因)
{
  const target = commentItem({ text: '博主这条视频讲得太好了' });
  const mention = commentItem({ text: '回复 博主:哈哈哈你说得对' });
  installDocument([mention, target]);
  // text 完全不匹配,只靠 nick 出现在 @提及子评论里 → 必须 false
  assert.equal(findItem({ text: '不存在的句子xyz', nick: '博主' })[0],
    false, 'nick-only match must be rejected');
  assert.equal(mention.getAttribute('data-mmm-target'), null);
  assert.equal(target.getAttribute('data-mmm-target'), null);
}

// 9) 长评论被 DOM 截断时,短前缀+nick 佐证仍能命中
{
  const target = commentItem({ text: '博主说得太对了' });
  installDocument([target]);
  // 传入比 DOM 更长的 text(模拟接口返回完整、页面只渲染前半句)
  assert.equal(
    findItem({ text: '博主说得太对了,后面还有一大段被截断了',
               nick: '博主' })[0], true);
}

// 10) cid 精确命中(优先级最高,即使 text/nick 都不匹配)
{
  const cidNode = imgNode({ 'data-cid': '731234567890' });
  const target = commentItem({ text: '随便什么内容', imgs: [cidNode] });
  installDocument([target]);
  assert.equal(
    findItem({ text: '完全不相关的文字', nick: '陌生人',
               cid: '731234567890' })[0], true);
}

// 11) _CLICK_COMMENT_ENTRY:能点到评论入口(图标按钮优先)
{
  const py2 = fs.readFileSync('app/browser/fetcher.py', 'utf8');
  const m2 = /_CLICK_COMMENT_ENTRY\s*=\s*r?"""([\s\S]*?)"""/.exec(py2);
  assert.ok(m2, '_CLICK_COMMENT_ENTRY must exist');
  const clickEntry = eval('(' + m2[1].trim() + ')');

  // 构造一个模拟页面:有带 data-e2e="comment-icon" 的按钮
  let clicked = false;
  const iconBtn = {
    offsetWidth: 20,
    click() { clicked = true; },
    getAttribute: () => 'comment-icon',
  };
  global.document = {
    querySelector(sel) {
      if (sel === '[data-e2e="comment-icon"]') return iconBtn;
      return null;
    },
    querySelectorAll: () => [],
  };
  assert.equal(clickEntry(), true);
  assert.equal(clicked, true);

  // 回退:无图标按钮时,按文本「评论」命中
  clicked = false;
  const textBtn = {
    offsetWidth: 20,
    innerText: '评论',
    click() { clicked = true; },
  };
  global.document = {
    querySelector: () => null,
    querySelectorAll: (sel) => sel.includes('button') ? [textBtn] : [],
  };
  assert.equal(clickEntry(), true);
  assert.equal(clicked, true);
}

// 12) _FIND_VISIBLE_EDITOR:抖音预渲染隐藏评论区 DOM,只数 DOM 存在会误判
// "已展开"而永远不点评论按钮。必须严格按可见性,并排除顶部搜索框。
{
  const m3 = /_FIND_VISIBLE_EDITOR\s*=\s*"""/.exec(pySource);
  assert.ok(m3, '_FIND_VISIBLE_EDITOR must exist');
  const body = pySource.slice(m3.index).split('"""')[1];
  const findEditor = eval('(' + body.trim() + ')');
  function node(spec) {
    const attrs = new Map(Object.entries(spec.attrs || {}));
    return {
      w: spec.w || 0, h: spec.h || 0, inPanel: !!spec.inPanel,
      cls: spec.cls || '',
      get className() { return this.cls; },
      getBoundingClientRect() { return { width: this.w, height: this.h }; },
      getAttribute: k => (attrs.has(k) ? attrs.get(k) : null),
      setAttribute(k, v) { attrs.set(k, v); },
      removeAttribute(k) { attrs.delete(k); },
      closest: () => (this.inPanel ? {} : null),
    };
  }
  function install(all) {
    global.document = {
      querySelectorAll(sel) {
        if (sel.includes('data-mmm-editor'))
          return all.filter(e => e.getAttribute('data-mmm-editor') === '1');
        if (sel.includes('comment-item'))
          return all.filter(e => e.getAttribute('data-e2e') === 'comment-item');
        return all.filter(e => e.editable);
      },
    };
  }
  // a) 全隐藏的预渲染 DOM:既无可见输入框也无可见评论
  {
    const hiddenItem = node({ attrs: { 'data-e2e': 'comment-item' } });
    const hiddenInput = node({ attrs: { 'data-e2e': 'comment-input' } });
    hiddenInput.editable = true;
    install([hiddenItem, hiddenInput]);
    const r = findEditor();
    assert.equal(r.editor, false);
    assert.equal(r.items, 0);
  }
  // b) 可见面板 + 可见评论输入框:命中并给输入框打标记
  {
    const item = node({ attrs: { 'data-e2e': 'comment-item' }, w: 300, h: 80 });
    const inp = node({
      attrs: { 'data-e2e': 'comment-input', 'data-placeholder': '留下你的友善评论吧' },
      w: 320, h: 36, inPanel: true,
    });
    inp.editable = true;
    install([item, inp]);
    const r = findEditor();
    assert.equal(r.editor, true);
    assert.equal(r.items, 1);
    assert.equal(inp.getAttribute('data-mmm-editor'), '1');
  }
  // c) 页面上只有可见搜索框:不得误选为评论输入框
  {
    const search = node({ attrs: { 'data-placeholder': '搜索' }, w: 200, h: 32 });
    search.editable = true;
    install([search]);
    assert.equal(findEditor().editor, false);
  }
}

// 13) _FIND_SEND_BUTTON:隐藏预渲染按钮不选;文本按钮与红色箭头图标按钮
// 都要能识别;禁用态(灰/disabled class)返回 enabled=false。
{
  const m4 = /_FIND_SEND_BUTTON\s*=\s*r?"""/.exec(pySource);
  assert.ok(m4, '_FIND_SEND_BUTTON must exist');
  const body4 = pySource.slice(m4.index).split('"""')[1];
  const findSend = eval('(' + body4.trim() + ')');
  function btn(spec) {
    const attrs = new Map(Object.entries(spec.attrs || {}));
    const node = {
      tagName: spec.tag || 'DIV',
      w: spec.w || 0, h: spec.h || 0,
      cls: spec.cls || '',
      innerText: spec.text || '',
      disabled: !!spec.disabled,
      parentElement: null,
      _color: spec.color || 'rgb(150, 150, 150)',
      _icon: !!spec.icon,
      get className() { return this.cls; },
      getBoundingClientRect() {
        return { width: this.w, height: this.h, left: 0, top: 0 };
      },
      getAttribute: k => (attrs.has(k) ? attrs.get(k) : null),
      setAttribute(k, v) { attrs.set(k, v); },
      removeAttribute(k) { attrs.delete(k); },
      querySelector(sel) {
        // 图标按钮内有一个 svg
        if (this._icon && /svg|xg-icon|img|icon/i.test(sel))
          return { tagName: 'svg' };
        return null;
      },
    };
    return node;
  }
  function install4(all) {
    global.document = {
      querySelector: () => null,      // 无 editor 锚点,走全局收集
      elementFromPoint: () => null,
      querySelectorAll(sel) {
        if (sel.includes('data-mmm-send'))
          return all.filter(e => e.getAttribute('data-mmm-send') === '1');
        if (sel.includes('comment-publish]'))
          return all.filter(e => e.getAttribute('data-e2e') === 'comment-publish');
        if (sel.includes('sendbtn') || sel.includes('publishbtn')
            || sel.includes('submit') || sel.includes('"send"'))
          return all.filter(e => /send|publish|submit/i.test(e.cls));
        // button/[role=button]/a/[tabindex] 组:全部候选
        return all;
      },
    };
    global.getComputedStyle = el => ({
      display: '', visibility: '', opacity: '1',
      color: (el && el._color) || 'rgb(150, 150, 150)',
      fill: 'rgb(0, 0, 0)',
    });
  }
  // a) 只有隐藏的预渲染发送按钮:找不到
  {
    const hidden = btn({ tag: 'DIV', w: 0, h: 0,
      attrs: { 'data-e2e': 'comment-publish' } });
    install4([hidden]);
    assert.deepEqual(findSend(), { found: false });
  }
  // b) 可见文本按钮:打标,enabled
  {
    const ok = btn({ tag: 'DIV', w: 60, h: 32, text: '发送',
      attrs: { 'data-e2e': 'comment-publish' } });
    install4([ok]);
    const r = findSend();
    assert.equal(r.found, true);
    assert.equal(r.enabled, true);
    assert.equal(ok.getAttribute('data-mmm-send'), '1');
  }
  // c) 可见但禁用(cls 含 disabled):found 但 enabled=false
  {
    const dis = btn({ tag: 'DIV', w: 60, h: 32, text: '发送', cls: 'btn-disabled',
      attrs: { 'data-e2e': 'comment-publish' } });
    install4([dis]);
    const r = findSend();
    assert.equal(r.found, true);
    assert.equal(r.enabled, false);
  }
  // d) 无文字的红色向上箭头图标按钮:必须能找到并判 enabled
  //    (v1.5.6 核心修复:旧逻辑只认"发送"文本,永远点不到箭头)
  {
    const arrow = btn({ tag: 'DIV', w: 36, h: 36, text: '', icon: true,
      color: 'rgb(254, 44, 84)' });
    install4([arrow]);
    const r = findSend();
    assert.equal(r.found, true);
    assert.equal(r.enabled, true);
    assert.equal(r.red, true);
    assert.equal(arrow.getAttribute('data-mmm-send'), '1');
  }
  // e) 灰色箭头:found 但 enabled=false(输入前状态,Python 侧继续轮询)
  {
    const grey = btn({ tag: 'DIV', w: 36, h: 36, text: '', icon: true,
      color: 'rgb(150, 150, 150)' });
    install4([grey]);
    const r = findSend();
    assert.equal(r.found, true);
    assert.equal(r.enabled, false);
  }
}

// 14) _FIND_REPLY_EDITOR / _FIND_REPLY_BUTTON:无目标标记时安全返回
// found:false(不得抛异常——Python 侧每轮轮询依赖它稳定返回)
{
  for (const name of ['_FIND_REPLY_EDITOR', '_FIND_REPLY_BUTTON']) {
    const m = new RegExp(name + '\\s*=\\s*"""').exec(pySource);
    assert.ok(m, name + ' must exist');
    const fn = eval('(' + pySource.slice(m.index).split('"""')[1].trim() + ')');
    global.document = {
      querySelector: () => null,
      querySelectorAll: () => [],
    };
    const r = fn();
    assert.equal(r.found, false);
    assert.equal(r.reason, 'no-target-mark');
  }
}

// 15) _READBACK_EDITOR:焦点和打标都不对时,面板扫描找到真正含文本的
// contenteditable 并重标(v1.5.6:下方面板字进了内联框、句柄却指向主框)
{
  const m = /_READBACK_EDITOR\s*=\s*"""/.exec(pySource);
  assert.ok(m, '_READBACK_EDITOR must exist');
  const readback = eval('(' + pySource.slice(m.index).split('"""')[1].trim() + ')');
  const main = {   // 底部主框:空
    innerText: '', textContent: '',
    getBoundingClientRect: () => ({ width: 300, height: 30 }),
    getAttribute: () => null, setAttribute() {},
  };
  const inline = {  // 内联框:含已输入文本
    innerText: '回复这条评论的内容', textContent: '回复这条评论的内容',
    getBoundingClientRect: () => ({ width: 300, height: 30 }),
    attrs: new Map(),
    getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; },
    setAttribute(k, v) { this.attrs.set(k, v); },
    removeAttribute(k) { this.attrs.delete(k); },
  };
  global.document = {
    activeElement: main,
    querySelector(sel) {
      if (sel.includes('data-mmm-editor')) return main;
      return null;
    },
    querySelectorAll(sel) {
      if (sel.includes('data-mmm-editor'))
        return [main, inline].filter(e => e.attrs && e.attrs.has('data-mmm-editor'));
      if (sel.includes('contenteditable')) return [main, inline];
      return [];
    },
  };
  const r = readback('回复这条评论');
  assert.equal(r.found, true);
  assert.equal(r.where, 'panel-scan');
  assert.equal(inline.attrs.get('data-mmm-editor'), '1');
}

// 16) 表情短代码前缀评论(203 条扫不到的真实现象 [666][666]柳州欢迎您！):
// DOM 里表情是 <img alt="[666]">、文字在后续文本节点,旧实现把 alt 和
// 文字当独立片段用 \n 拼接永远拼不成连续串。文档序逻辑文本必须命中。
{
  const txtNode = s => ({ nodeType: 3, nodeValue: s });
  const imgEmoji = alt => ({
    nodeType: 1, tagName: 'IMG', childNodes: [],
    attributes: [{ name: 'alt', value: alt }],
    getAttribute(k) { return k === 'alt' ? alt : null; },
  });
  const elNode = (tag, kids) => ({
    nodeType: 1, tagName: tag || 'DIV', childNodes: kids || [],
    attributes: [], getAttribute: () => null,
  });
  // 目标项:昵称 + 两个表情 img + 文字;innerText 故意不含短代码
  const target = elNode('DIV', [
    elNode('DIV', [txtNode('『魚先森』')]),
    elNode('DIV', [imgEmoji('[666]'), imgEmoji('[666]'),
                   txtNode('柳州欢迎您！5天前 分享 回复')]),
  ]);
  target.innerText = target.textContent = '『魚先森』柳州欢迎您！5天前 分享 回复';
  const tAttrs = new Map([['data-e2e', 'comment-item']]);
  target.getAttribute = k => (tAttrs.has(k) ? tAttrs.get(k) : null);
  target.setAttribute = (k, v) => tAttrs.set(k, v);
  target.removeAttribute = k => tAttrs.delete(k);
  target.querySelectorAll = () =>
    [target.childNodes[1].childNodes[0], target.childNodes[1].childNodes[1]];
  target.getBoundingClientRect = () =>
    ({ width: 757, height: 134, top: 100, bottom: 234 });
  // 对照项:纯文字包含后半句(plain 兜底也会给分,但分数必须低于精确项)
  const other = commentItem({ text: '柳州欢迎您！但我是另一个人说的' });
  installDocument([other, target]);
  const r = findItem({ text: '[666][666]柳州欢迎您！',
                       nick: '『魚先森』' });
  assert.equal(r[0], true);
  assert.equal(r[1], 2);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
  assert.equal(other.getAttribute('data-mmm-target'), null);
}

// 17) cid 挂在子节点任意 data-* 属性(新版 DOM 位置漂移)也能精确命中
{
  const cidChild = {
    attributes: [{ name: 'data-id', value: '7691196036673340195' }],
    getAttribute(k) { return k === 'data-id' ? '7691196036673340195' : null; },
  };
  const target = commentItem({ text: '随便什么内容' });
  target.querySelectorAll = sel =>
    (String(sel).includes('emoji') || String(sel).includes('sticker'))
      ? [] : [cidChild];
  installDocument([target]);
  assert.equal(findItem({ text: '完全不相关的文字', nick: '陌生人',
                          cid: '7691196036673340195' })[0], true);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
}

// 18) _VERIFY_COMMENT_POST:正在编辑(内容在 data-mmm-editor 子树)不得
// 误判成功;发送后编辑器卸载且回复进入评论区 → settled=true
{
  const mv = /_VERIFY_COMMENT_POST\s*=\s*r?"""/.exec(pySource);
  assert.ok(mv, '_VERIFY_COMMENT_POST must exist');
  const verify = eval('(' + pySource.slice(mv.index)
    .split('"""')[1].trim() + ')');
  const item = {
    nodeType: 1, tagName: 'DIV',
    innerText: '『魚先森』 [666][666]柳州欢迎您！ 回复的内容就是这句',
    attributes: [], hasAttribute: () => false,
    getAttribute: () => null,
    childNodes: [],
    getBoundingClientRect: () => ({ width: 700, height: 120 }),
  };
  const editor = {
    nodeType: 1, tagName: 'DIV',
    innerText: '回复的内容就是这句', textContent: '回复的内容就是这句',
  };
  // A) 编辑器存活且在评论项内:必须返回 posted=false
  item.querySelector = sel =>
    (String(sel).includes('data-mmm-editor') ? editor : null);
  global.document = {
    contains: n => n === editor,
    querySelector: sel =>
      (String(sel).includes('data-mmm-editor') ? editor : null),
    querySelectorAll: () => [item],
  };
  let r = verify('回复的内容就是这句');
  assert.equal(r.posted, false);
  assert.equal(r.settled, false);
  assert.equal(r.alive, true);
  assert.equal(r.empty, false);
  // B) 编辑器卸载,回复留在评论项内:settled=true(DOM 发表成功)
  item.querySelector = () => null;
  global.document = {
    contains: () => false,
    querySelector: () => null,
    querySelectorAll: () => [item],
  };
  r = verify('回复的内容就是这句');
  assert.equal(r.posted, true);
  assert.equal(r.settled, true);
  assert.equal(r.alive, false);
  assert.equal(r.nRoots, 1);
  assert.equal(r.items, 1);
}

// 19) _FIND_SEND_BUTTON formBox(v1.5.8 下方面板误点红心根因):内联回复框
// 插在评论项内部,真"发送"按钮在编辑器上 2 层的表单容器里;评论项操作栏
// 的点赞红心是表单外兄弟。box 取小(editor.parentElement)时红心会以 box
// 域高分赢过真发送→点击后内联框失焦收起、零 publish。修复后必须选真
// "发送",红心不得打标。
{
  const m4b = /_FIND_SEND_BUTTON\s*=\s*r?"""/.exec(pySource);
  const findSend2 = eval('(' + pySource.slice(m4b.index)
    .split('"""')[1].trim() + ')');

  function el19(spec) {
    const attrs = new Map(Object.entries(spec.attrs || {}));
    const e = {
      tagName: spec.tag || 'DIV',
      cls: spec.cls || '',
      innerText: spec.text || '',
      parentElement: spec.parent || null,
      children: spec.kids || [],
      _color: spec.color || 'rgb(34, 34, 34)',
      _icon: !!spec.icon,
      get className() { return this.cls; },
      getBoundingClientRect() {
        return { width: spec.w || 200, height: spec.h || 30,
                 left: 0, top: spec.top || 0, bottom: (spec.top || 0) + (spec.h || 30) };
      },
      getAttribute: k => (attrs.has(k) ? attrs.get(k) : null),
      setAttribute(k, v) { attrs.set(k, v); },
      removeAttribute(k) { attrs.delete(k); },
      contains(x) { return x === this || this.children.includes(x); },
      closest(sel) {
        if (!sel || !sel.includes('comment-item')) return null;
        let p = this;
        while (p) {
          if (p.getAttribute && p.getAttribute('data-e2e') === 'comment-item')
            return p;
          p = p.parentElement;
        }
        return null;
      },
      querySelector(sel) {
        if (this._icon && /svg|xg-icon|img|icon/i.test(sel))
          return { tagName: 'svg' };
        if (/comment-publish/.test(sel))
          return this.children.find(c => c.getAttribute('data-e2e') === 'comment-publish') || null;
        return null;
      },
      querySelectorAll(sel) {
        if (/data-mmm-send/.test(sel))
          return this.children.filter(c => c.getAttribute('data-mmm-send') === '1');
        if (/comment-publish\]/.test(sel))
          return this.children.filter(c => c.getAttribute('data-e2e') === 'comment-publish');
        if (/sendbtn|publishbtn|send-btn|submit/.test(sel))
          return this.children.filter(c => /send|publish|submit/i.test(c.cls));
        if (/button|role=.button|tabindex|span,div/.test(sel))
          return this.children.slice();
        if (/svg|xg-icon|img/.test(sel))
          return this.children.filter(c => c._icon);
        return [];
      },
    };
    return e;
  }

  const editor = el19({ tag: 'DIV', text: '', w: 300, h: 30,
    attrs: { 'data-mmm-editor': '1', contenteditable: 'true' },
    color: 'rgb(34, 34, 34)' });
  const sendBtn = el19({ tag: 'DIV', text: '发送', w: 48, h: 28,
    color: 'rgb(254, 44, 84)' });
  const formBox = el19({ kids: [editor, sendBtn] });
  editor.parentElement = formBox;
  sendBtn.parentElement = formBox;
  const heart = el19({ tag: 'DIV', text: '', w: 36, h: 36, icon: true,
    color: 'rgb(254, 44, 84)', top: 200 });
  const actionBar = el19({ kids: [heart] });
  heart.parentElement = actionBar;
  const item = el19({ attrs: { 'data-e2e': 'comment-item' },
    kids: [actionBar, formBox] });
  actionBar.parentElement = item;
  formBox.parentElement = item;
  const all = [item, actionBar, heart, formBox, editor, sendBtn];

  global.document = {
    querySelector(sel) {
      if (sel.includes('data-mmm-editor')) return editor;
      return null;
    },
    elementFromPoint: () => null,
    querySelectorAll(sel) {
      if (sel.includes('data-mmm-send'))
        return all.filter(e => e.getAttribute('data-mmm-send') === '1');
      if (sel.includes('comment-publish]'))
        return all.filter(e => e.getAttribute('data-e2e') === 'comment-publish');
      if (sel.includes('sendbtn') || sel.includes('publishbtn')
          || sel.includes('submit'))
        return all.filter(e => /send|publish|submit/i.test(e.cls));
      // 全局 button/tabindex/span/div 组
      return all.filter(e => e !== item && e !== formBox && e !== actionBar);
    },
  };
  global.getComputedStyle = el => ({
    display: '', visibility: '', opacity: '1',
    color: (el && el._color) || 'rgb(34, 34, 34)', fill: 'rgb(0, 0, 0)',
  });

  const r = findSend2();
  assert.equal(r.found, true);
  assert.equal(r.sem, true);
  assert.equal(sendBtn.getAttribute('data-mmm-send'), '1');
  assert.equal(heart.getAttribute('data-mmm-send'), null);
}

// 20) v1.5.9 B 机实锤:发送按钮被选成 SPAN"发布时间:202x..."(旧
// [data-e2e*=publish] 选择器命中 video-publish-time,sem 误判 true),
// 点击后零请求、内联框收起。本场景里真发送是一个无 svg/无文字、纯 class
// 的红色 send-arrow SPAN。修复后:必须选红箭头,发布时间/红心都不得打标。
{
  const m5 = /_FIND_SEND_BUTTON\s*=\s*r?"""/.exec(pySource);
  const findSend3 = eval('(' + pySource.slice(m5.index)
    .split('"""')[1].trim() + ')');

  function el20(spec) {
    const attrs = new Map(Object.entries(spec.attrs || {}));
    const e = {
      tagName: spec.tag || 'DIV',
      cls: spec.cls || '',
      innerText: spec.text || '',
      parentElement: spec.parent || null,
      children: spec.kids || [],
      _color: spec.color || 'rgb(34, 34, 34)',
      _icon: !!spec.icon,
      disabled: false,
      get className() { return this.cls; },
      getBoundingClientRect() {
        return { width: spec.w || 24, height: spec.h || 24,
                 left: spec.left || 0, top: spec.top || 0,
                 bottom: (spec.top || 0) + (spec.h || 24) };
      },
      getAttribute: k => (attrs.has(k) ? attrs.get(k) : null),
      setAttribute(k, v) { attrs.set(k, v); },
      removeAttribute(k) { attrs.delete(k); },
      contains(x) {
        if (x === this) return true;
        const walk = n => (n.children || [])
          .some(c => c === x || walk(c));
        return walk(this);
      },
      closest(sel) {
        if (!sel || !sel.includes('comment-item')) return null;
        let p = this;
        while (p) {
          if (p.getAttribute && p.getAttribute('data-e2e') === 'comment-item')
            return p;
          p = p.parentElement;
        }
        return null;
      },
      querySelector(sel) {
        if (this._icon && /svg|xg-icon|img|icon/i.test(sel))
          return { tagName: 'svg' };
        if (/comment-publish"\]/.test(sel))
          return this.children.find(
            c => c.getAttribute('data-e2e') === 'comment-publish') || null;
        if (/sendbtn|send-arrow|send-button/.test(sel))
          return this.children.find(
            c => /send|publish|submit|arrow/i.test(c.cls)) || null;
        return null;
      },
      querySelectorAll(sel) {
        if (/data-mmm-send/.test(sel))
          return this.children.filter(
            c => c.getAttribute('data-mmm-send') === '1');
        if (/comment-publish"\]/.test(sel))
          return this.children.filter(
            c => c.getAttribute('data-e2e') === 'comment-publish');
        if (sel.includes('arrow'))
          return this.children.filter(c => /arrow/i.test(c.cls));
        if (/sendbtn|publishbtn|send-btn|submit/.test(sel))
          return this.children.filter(c => /send|publish|submit/i.test(c.cls));
        if (/button|role=.button|tabindex/.test(sel))
          return this.children.slice();
        if (/span,\s*div/.test(sel))
          return this.children.filter(c => (c.children || []).length === 0);
        return [];
      },
    };
    return e;
  }

  const editor = el20({ tag: 'DIV', text: '', w: 300, h: 30,
    attrs: { 'data-mmm-editor': '1', contenteditable: 'true' } });
  // 真发送:纯 class 红箭头 SPAN,无文字无 svg
  const arrow = el20({ tag: 'SPAN', cls: 'xg-send-arrow-active', text: '',
    w: 24, h: 24, color: 'rgb(254, 44, 84)', top: 4 });
  // 干扰项:发布时间(B 机实锤被误点的元素)
  const pubTime = el20({ tag: 'SPAN', cls: 'publish-time-tip',
    attrs: { 'data-e2e': 'video-publish-time' },
    text: '发布时间：2026-09-20', w: 130, h: 16, top: 2 });
  const formBox = el20({ cls: 'comment-input-bar',
    kids: [editor, arrow, pubTime] });
  editor.parentElement = formBox;
  arrow.parentElement = formBox;
  pubTime.parentElement = formBox;
  const heart = el20({ tag: 'DIV', cls: 'digg-icon-heart', text: '', w: 36,
    h: 36, icon: true, color: 'rgb(254, 44, 84)', top: 200 });
  const actionBar = el20({ kids: [heart] });
  heart.parentElement = actionBar;
  const item = el20({ attrs: { 'data-e2e': 'comment-item' },
    kids: [actionBar, formBox] });
  actionBar.parentElement = item;
  formBox.parentElement = item;
  const all20 = [item, actionBar, heart, formBox, editor, arrow, pubTime];

  global.document = {
    querySelector(sel) {
      if (sel.includes('data-mmm-editor')) return editor;
      return null;
    },
    elementFromPoint: () => null,
    querySelectorAll(sel) {
      if (sel.includes('data-mmm-send'))
        return all20.filter(e => e.getAttribute('data-mmm-send') === '1');
      if (sel.includes('comment-publish"]'))
        return all20.filter(e => e.getAttribute('data-e2e') === 'comment-publish');
      if (sel.includes('arrow'))
        return all20.filter(e => /arrow/i.test(e.cls));
      if (sel.includes('sendbtn') || sel.includes('publishbtn')
          || sel.includes('submit'))
        return all20.filter(e => /send|publish|submit/i.test(e.cls));
      if (/button|role=.button|tabindex/.test(sel))
        return all20.filter(e => e !== item && e !== formBox && e !== actionBar);
      if (/span,\s*div/.test(sel))
        return all20.filter(e => e !== item
          && (e.children || []).length === 0);
      return [];
    },
  };
  global.getComputedStyle = el => ({
    display: '', visibility: '', opacity: '1',
    color: (el && el._color) || 'rgb(34, 34, 34)',
    fill: 'rgb(0, 0, 0)',
  });

  const r3 = findSend3();
  assert.equal(r3.found, true, 'send arrow must be found');
  assert.equal(r3.red, true, 'picked button must be the red arrow');
  assert.equal(r3.sem, true, 'send-arrow class is authoritative');
  assert.equal(r3.tag, 'SPAN');
  assert.equal(arrow.getAttribute('data-mmm-send'), '1');
  assert.equal(pubTime.getAttribute('data-mmm-send'), null,
    'publish-time span must never be marked as send');
  assert.equal(heart.getAttribute('data-mmm-send'), null,
    'heart outside box must never be marked');
}

// 21) B 机 v1.6.4 实锤事故复现:目标是长昵称+长正文评论,另一条"回复
// @目标昵称"的子评论仅含昵称。DOM 里只有子评论时必须 false;真目标
// 出现后必须精确标记真目标而不是 @提及楼层
{
  const longNick = '假装是青年音进群勾引萝莉后被发现是烟灰缸';
  const targetText = '我一天原来能产生200升污水[捂脸][捂脸][捂脸]，这博主真夸张';
  // 干扰项:别人回复目标用户的子评论(昵称在,正文一个字不对)
  const mention = commentItem({
    text: '回复 ' + longNick + '：哈哈哈哈你太逗了吧',
  });
  installDocument([mention]);
  const arg = { text: targetText, nick: longNick,
                cid: '7691672488933720890' };
  assert.equal(findItem(arg)[0], false,
    'mention-only comment must not be accepted as target');
  assert.equal(mention.getAttribute('data-mmm-target'), null);
  // 真目标加载进 DOM 后
  const target = commentItem({
    text: longNick + ' ' + targetText + ' 5天前 分享 回复',
  });
  installDocument([mention, target]);
  const r = findItem(arg);
  assert.equal(r[0], true);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
  assert.equal(mention.getAttribute('data-mmm-target'), null);
}

// 22) 短评论文本恰好是目标长文中的某个子串(且无 nick):不得误中
{
  const short = commentItem({ text: '哈哈哈' });
  installDocument([short]);
  assert.equal(findItem({
    text: '这视频哈哈哈真的绝了我能看十遍以上还不腻歪',
  })[0], false, 'short coincidental substring must not match');
  assert.equal(short.getAttribute('data-mmm-target'), null);
}

// 23) 真实截断形态:DOM 只渲染了前缀(昵称+正文开头),传入完整原文+
// nick → 前缀中等证据 + nick 佐证,必须命中
{
  const longNick = '假装是青年音进群勾引萝莉后被发现是烟灰缸';
  const target = commentItem({
    text: longNick + ' 我一天原来能产生200升污水 5天前',
  });
  installDocument([target]);
  assert.equal(findItem({
    text: '我一天原来能产生200升污水[捂脸][捂脸][捂脸]，这博主真夸张啊啊',
    nick: longNick,
  })[0], true);
  assert.equal(target.getAttribute('data-mmm-target'), '1');
}

// 24) 嵌套楼层:父 comment-item 的 innerText 含折叠子评论预览文本,
// 叶子(真正的子评论节点)必须在同分竞争中胜出,回复到精确楼层
{
  const longNick = '认真评论的用户';
  const body = '我一天原来能产生200升污水[捂脸]太离谱了';
  const child = commentItem({ text: longNick + ' ' + body });
  const parentAttrs = new Map([['data-e2e', 'comment-item']]);
  const parent = {
    innerText: '楼主 原帖内容…… ' + longNick + ' ' + body + ' 昨天 回复',
    textContent: '楼主 原帖内容…… ' + longNick + ' ' + body,
    getAttribute: k => (parentAttrs.has(k) ? parentAttrs.get(k) : null),
    setAttribute(k, v) { parentAttrs.set(k, v); },
    removeAttribute(k) { parentAttrs.delete(k); },
    querySelectorAll: () => [child],
    querySelector(sel) {
      if (String(sel).includes('comment-item')) return child;
      return null;
    },
    getBoundingClientRect: () => ({ width: 700, height: 200 }),
  };
  child.querySelector = sel =>
    (String(sel).includes('comment-item') ? null
      : child.querySelectorAll(sel)[0] || null);
  child.getBoundingClientRect = () => ({ width: 680, height: 90 });
  global.document = {
    querySelectorAll(sel) {
      if (sel.includes('data-mmm-target'))
        return [parent, child].filter(
          e => e.getAttribute('data-mmm-target') === '1');
      if (sel.includes('comment-item')) return [parent, child];
      return [];
    },
  };
  assert.equal(findItem({ text: body, nick: longNick })[0], true);
  assert.equal(child.getAttribute('data-mmm-target'), '1',
    'leaf (nested) item must win over parent preview');
  assert.equal(parent.getAttribute('data-mmm-target'), null);
}

console.log('douyin emoji comment reply matching: 24 cases passed');
