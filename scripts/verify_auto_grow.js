/**
 * 输入框自增高逻辑验证（ZYAutoGrow）
 *
 * 用户反馈：语音较长时输入框只显示一部分。修复方式是把单行 <input> 换成
 * <textarea> 并按 scrollHeight 自适应。这里验证四条最容易写错的性质：
 *   ① 变长 → 高度增长
 *   ② 删短 → 高度**必须缩回**（不归零量 height 就只会单调增长，这是最常见的错）
 *   ③ 超过上限 → 封顶并标记 is-overflowing（转内部滚动，不把聊天区挤没）
 *   ④ 语音直接改 value、不触发按键 → 仍能重算高度（所以监听 input 而非 keyup）
 *
 * 运行：node scripts/verify_auto_grow.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'frontend', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

let pass = 0, fail = 0;
function check(label, cond, detail) {
    if (cond) { pass++; console.log(`  ✓ ${label}` + (detail ? `  ← ${detail}` : '')); }
    else { fail++; console.log(`  ✗ ${label}` + (detail ? `  ← ${detail}` : '')); }
}

/** 只取出 ZYAutoGrow 那段 IIFE 求值，避免把 Vue 依赖一起拖进来 */
const block = src.match(/\(function \(\) \{[\s\S]*?window\.ZYAutoGrow = \{[^}]*\};\s*\}\)\(\);/);
if (!block) {
    console.error('未能从 app.js 中定位 ZYAutoGrow 代码块');
    process.exit(1);
}

/** 造一个可控的假 textarea：scrollHeight 由"内容行数"决定 */
function makeEl(lineHeight = 24, maxContentPx = 4000) {
    const el = {
        style: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        value: '',
        scrollHeight: 0,
        listeners: {},
        addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
        removeEventListener(t, fn) {
            this.listeners[t] = (this.listeners[t] || []).filter(f => f !== fn);
        },
        dispatch(t) { (this.listeners[t] || []).forEach(f => f()); },
        /** 按"多少行内容"设定 scrollHeight（模拟浏览器排版结果） */
        setLines(n) { this.scrollHeight = Math.min(n * lineHeight, maxContentPx); },
        getHeight() { return parseInt(this.style.height || '0', 10); },
    };
    return el;
}

const sandbox = { window: {}, console, setTimeout, clearTimeout };
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(block[0], sandbox);
const AG = sandbox.window.ZYAutoGrow;

console.log('① 变长 → 高度增长（长语音能完整显示）');
{
    const el = makeEl();
    el.setLines(1);
    check('空内容时高度为 1 行', AG.sync(el) === 24, `${AG.sync(el)}px`);
    el.setLines(3);
    const h3 = AG.sync(el);
    check('3 行内容 → 高度 72px', h3 === 72, `${h3}px`);
    el.setLines(6);
    const h6 = AG.sync(el);
    check('6 行内容 → 高度 144px', h6 === 144, `${h6}px`);
}

console.log('\n② 删短 → 高度必须缩回（最容易写错的地方）');
{
    const el = makeEl();
    el.setLines(6); AG.sync(el);
    el.setLines(6); AG.sync(el);
    el.setLines(6); AG.sync(el);
    check('反复长高后达到 144px', el.getHeight() === 144, `${el.getHeight()}px`);
    el.setLines(1);
    const back = AG.sync(el);
    check('内容删短后高度缩回 1 行', back === 24, `${back}px（若一直 144 就是没归零量）`);
    check('未超上限时移除 is-overflowing', !el.classList.contains('is-overflowing'));
}

console.log('\n③ 超过上限 → 封顶 + 转内部滚动');
{
    const el = makeEl();
    el.setLines(20);                       // 480px 内容，远超上限
    const h = AG.sync(el);
    check('高度被封顶在 MAX', h === AG.MAX, `${h}px / 上限 ${AG.MAX}px`);
    check('打上 is-overflowing 标记（CSS 据此显示滚动条）',
          el.classList.contains('is-overflowing'));
    el.setLines(2);
    AG.sync(el);
    check('回落到上限以下后标记被清掉',
          !el.classList.contains('is-overflowing'), `高度 ${el.getHeight()}px`);
}

console.log('\n④ 语音直接改 value（不产生按键）也能重算高度');
{
    const el = makeEl();
    el.setLines(1);
    const unbind = AG.bind(el);
    check('bind() 会立即做一次初始测量', el.getHeight() === 24, `${el.getHeight()}px`);

    // 模拟语音识别：直接写 value，然后只派发 input 事件（没有 keydown）
    el.value = '很长的一段语音识别结果';
    el.setLines(5);
    el.dispatch('input');
    check('派发 input 后高度跟随增长', el.getHeight() === 120, `${el.getHeight()}px`);

    unbind();
    el.setLines(1);
    el.dispatch('input');
    check('解绑后不再响应（避免重复绑定泄漏）', el.getHeight() === 120,
          `高度未变 ${el.getHeight()}px`);
}

console.log('\n⑤ 对空/非法元素不抛异常');
{
    let threw = false;
    try { AG.sync(null); AG.bind(undefined); } catch (e) { threw = true; }
    check('sync(null) / bind(undefined) 安全返回', !threw);
}

console.log('\n' + '='.repeat(62));
console.log(`结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);