// Task 21 前端契约:视频号不可行能力的直达路径防护(TR-21.1)。
// 用最小 DOM stub 在 Node VM 里运行发货代码中的 switchHubTab,并对
// switchTab / index.html 做静态契约断言,无需浏览器与网络。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const js = fs.readFileSync('app/web/app.js', 'utf8');
const html = fs.readFileSync('app/web/index.html', 'utf8');

// ── 静态契约:switchTab 主导航能力门 ─────────────────────────────
const switchTabSrc = /function switchTab\(name[\s\S]*?\n}/.exec(js)[0];
assert.ok(switchTabSrc.includes('.navitem[data-tab="${name}"]'),
  'switchTab 必须按 data-tab 定位目标导航');
assert.ok(switchTabSrc.includes('classList.contains("hidden")'),
  'switchTab 必须在目标导航被平台隐藏时回退(杜绝 hash 直达空转)');
assert.ok(switchTabSrc.includes('name = "overview"'),
  'switchTab 能力门必须回退到工作概览');

// ── 静态契约:index.html 视频号不可行入口带能力标记/隐藏类 ───────
for (const [tab, cap] of [['following', 'follow_sync'], ['fans', 'follow_sync'],
  ['dm', 'dm']]) {
  const btn = new RegExp(
    `<button[^>]*data-hubtab="${tab}"[^>]*>`).exec(html);
  assert.ok(btn, `${tab} 子标签必须存在`);
  assert.ok(btn[0].includes(`data-cap="${cap}"`), `${tab} 子标签必须声明 data-cap="${cap}"`);
  assert.ok(btn[0].includes('notsh-only'), `${tab} 子标签必须带 notsh-only 隐藏类`);
}
// 平台说明必须逐项点名不可行能力及原因
assert.ok(html.includes('他人作品监控'), '平台说明须列出他人作品监控不可用');
assert.ok(html.includes('关键词采集'), '平台说明须列出关键词采集不可用');
assert.ok(html.includes('主动私信'), '平台说明须列出主动私信不可用');

// ── 行为契约:switchHubTab 能力门(运行真实发货函数) ─────────────
function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(js);
  assert.ok(match, name);
  const next = js.indexOf('\n', match.index) + 1;
  if (js.slice(match.index, next).trimEnd().endsWith('}')) return js.slice(match.index, next);
  const end = /^}\r?$/m.exec(js.slice(next));
  return js.slice(match.index, next + end.index + 1);
}
const capsConst = /const HUB_TAB_CAPS = \{[\s\S]*?\};\r?\n/.exec(js)[0];

function runHubTab(platformCaps) {
  const sandbox = {
    PLATFORM: platformCaps.platform,
    HUB_TAB: '',
    pfCan: (pf, cap) => pf === platformCaps.platform
      && platformCaps.caps.includes(cap),
    localStorage: { setItem() {} },
    document: { querySelectorAll: () => [] },
    startDmStream() {}, stopDmStream() {}, refreshHubPanel() {},
    console,
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(capsConst + fn('switchHubTab'), context);
  return name => { context.switchHubTab(name); return context.HUB_TAB; };
}

// 视频号:只有 own_works/publish/creator_login(与 registry.py 一致)
const channels = runHubTab({ platform: 'shipinhao',
  caps: ['own_works', 'publish', 'creator_login'] });
assert.equal(channels('dm'), 'myworks', '视频号直达私信必须回到我的作品');
assert.equal(channels('following'), 'myworks', '视频号直达关注必须回到我的作品');
assert.equal(channels('fans'), 'myworks', '视频号直达粉丝必须回到我的作品');
assert.equal(channels('myworks'), 'myworks', '视频号我的作品正常停留');
assert.equal(channels('stats'), 'stats', '视频号数据页不受能力门影响');

// 抖音对照:全部子标签可达
const douyin = runHubTab({ platform: 'douyin',
  caps: ['own_works', 'follow_sync', 'dm', 'publish'] });
assert.equal(douyin('dm'), 'dm');
assert.equal(douyin('following'), 'following');
assert.equal(douyin('fans'), 'fans');

console.log('channels capability gates and UI contract passed');
