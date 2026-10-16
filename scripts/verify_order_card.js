#!/usr/bin/env node
/**
 * 订单名片 · 对话内支付 · 追问建议（任务书五·3 + 四·1 闭环）验收脚本 —— 前端侧
 *
 * 做法与 verify_product_search.js 相同：用 Node vm 真加载 frontend/app.js
 * （Vue / document / window / localStorage 用桩替掉），然后：
 *
 *   A. 直接调文件头部的顶层纯函数（orderStatusClass / hasAction / moneyOf / suggestFor）
 *   B. 调 setup()，拿真实返回值，断言「模板用到的每个 handler 都真的被 return 了」
 *      —— 这是前端最常见的隐形事故：模板写了 @click="payOrder(msg)"，
 *         组件没把它 return，运行时才炸「payOrder is not a function」。
 *   C. 静态断言卡片分支与跨页闭环链接确实存在
 *
 * 运行：node scripts/verify_order_card.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'frontend', 'app.js');
const INDEX = path.join(__dirname, '..', 'frontend', 'index.html');
const ORDER = path.join(__dirname, '..', 'frontend', 'order.html');

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}

// ── 1. 沙箱 ────────────────────────────────────────────────────
const captured = { options: null };
const noop = () => {};
const Vue = {
    createApp(options) { captured.options = options; return { mount() { /* 不真挂载 */ } }; },
    ref: (v) => ({ value: v }),
    nextTick: (fn) => { if (typeof fn === 'function') fn(); },
    onMounted: noop,
};
const sandbox = {
    Vue,
    console,
    setTimeout, clearTimeout,
    document: {
        querySelector: () => null,
        getElementById: () => null,
        addEventListener: noop,
        createElement: () => ({ style: {} }),
    },
    window: { location: { href: '' }, addEventListener: noop, AUTH: null },
    location: { search: '', pathname: '/index.html', hash: '' },
    history: { replaceState: noop },
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    URLSearchParams,
    captured,
};
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

try {
    vm.runInContext(fs.readFileSync(APP, 'utf8'), ctx, { filename: 'app.js' });
} catch (e) {
    console.log('✗ app.js 在沙箱里执行报错：' + e.message);
    process.exit(1);
}

console.log('='.repeat(62));
console.log('  订单名片 / 对话内支付 / 追问建议 验收 · 前端');
console.log('='.repeat(62));

// ── 2. 顶层纯函数（A 组） ──────────────────────────────────────
const { orderStatusClass, hasAction, moneyOf, suggestFor } = sandbox;

console.log('\n[A] 顶层纯函数');

check('orderStatusClass 导出', typeof orderStatusClass === 'function');
check('已完成 → 品牌绿', orderStatusClass({ status: '已完成' }).includes('brand'));
check('已发货 → 湖青', orderStatusClass({ status: '已发货' }).includes('lake'));
check('未发货 → 日落色（提示还有下一步）', orderStatusClass({ status: '未发货' }).includes('sunset'));
check('已取消 → 中性灰', orderStatusClass({ status: '已取消' }).includes('stone'));
check('订单为空对象时不抛错', typeof orderStatusClass({}) === 'string');

check('hasAction 导出', typeof hasAction === 'function');
check('含 pay 动作 → true', hasAction({ actions: ['pay', 'cancel'] }, 'pay') === true);
check('不含该动作 → false', hasAction({ actions: ['cancel'] }, 'pay') === false);
check('actions 缺失 → false（不抛错）', hasAction({}, 'pay') === false);
check('order 为 null → false', hasAction(null, 'pay') === false);
check('actions 非数组 → false', hasAction({ actions: 'pay' }, 'pay') === false);

check('moneyOf 导出', typeof moneyOf === 'function');
check('moneyOf(5) = "5.00"', moneyOf(5) === '5.00', moneyOf(5));
check('moneyOf(12.5) = "12.50"', moneyOf(12.5) === '12.50', moneyOf(12.5));
check('moneyOf(undefined) = "0.00"', moneyOf(undefined) === '0.00', moneyOf(undefined));
check('moneyOf("8") = "8.00"', moneyOf('8') === '8.00', moneyOf('8'));

check('suggestFor 导出', typeof suggestFor === 'function');
const sProduct = suggestFor([{ cardType: 'product' }]);
check('产品名片 → 给 3 条追问', sProduct.length === 3, sProduct.join(' / '));
check('产品名片追问含「批发价」', sProduct.some((s) => s.includes('批发价')));
const sUnpaid = suggestFor([{ cardType: 'order', order: { actions: ['pay'] } }]);
check('未支付订单 → 提示先付款', sUnpaid.some((s) => s.includes('付款')), sUnpaid.join(' / '));
const sPaid = suggestFor([{ cardType: 'order', order: { actions: [] } }]);
check('已支付订单 → 转向查物流', sPaid.some((s) => s.includes('物流')), sPaid.join(' / '));
check('无卡片 → 不给追问（不刷屏）', suggestFor([]).length === 0);
check('入参非法 → 返回空数组', Array.isArray(suggestFor(null)) && suggestFor(null).length === 0);

// ── 3. setup() 真实接线（B 组） ────────────────────────────────
console.log('\n[B] setup() 接线');

check('createApp 捕获到组件配置', !!(captured.options && typeof captured.options.setup === 'function'));
let vmState = null;
try {
    vmState = captured.options.setup();
} catch (e) {
    check('setup() 可执行', false, e.message);
}
check('setup() 可执行', !!vmState);

if (vmState) {
    // 模板里 @click / @error / @keydown.enter 用到的 handler 必须真的被 return
    const html = fs.readFileSync(INDEX, 'utf8');
    const handlers = new Set();
    let m;
    const reCall = /@(?:click|error|keydown(?:\.[a-z]+)?)(?:\.[a-z]+)?="([a-zA-Z_$][\w$]*)\s*\(/g;
    while ((m = reCall.exec(html))) handlers.add(m[1]);
    const reBare = /@(?:click|error)="([a-zA-Z_$][\w$]*)\s*"/g;
    while ((m = reBare.exec(html))) handlers.add(m[1]);

    const missing = [...handlers].filter((h) => !(h in vmState)).sort();
    check(
        `模板用到的 ${handlers.size} 个 handler 都已被 return`,
        missing.length === 0,
        missing.length ? '缺失：' + missing.join(', ') : [...handlers].sort().join(', ')
    );

    // 订单名片相关成员
    ['orderStatusClass', 'hasAction', 'money', 'payOrder', 'payingOrders', 'payErrors']
        .forEach((k) => check(`return ${k}`, k in vmState));

    check('money 指向顶层 moneyOf', vmState.money === moneyOf);
    check('payingOrders 是响应式容器', !!(vmState.payingOrders && 'value' in vmState.payingOrders));
    check('payErrors 是响应式容器', !!(vmState.payErrors && 'value' in vmState.payErrors));
    check('payOrder 是函数', typeof vmState.payOrder === 'function');

    // 追问建议入口
    check('sendQuickQuestion 已 return（点追问即发送）', typeof vmState.sendQuickQuestion === 'function');
    check('messages 是响应式容器', !!(vmState.messages && 'value' in vmState.messages));

    // payOrder 的守卫：没有订单号时应该直接返回，不发请求
    let threw = null;
    vmState.payOrder({ order: {} }).catch((e) => { threw = e; });
    check('payOrder 对空订单号安全返回', threw === null, threw ? threw.message : '未抛错');
}

// ── 4. 模板与跨页闭环（C 组） ──────────────────────────────────
console.log('\n[C] 卡片分支与闭环链接');

const indexHtml = fs.readFileSync(INDEX, 'utf8');
const orderHtml = fs.readFileSync(ORDER, 'utf8');

check('index.html 有订单名片分支', indexHtml.includes("msg.cardType === 'order'"));
check('index.html 有追问建议分支', indexHtml.includes("msg.role === 'suggest'"));
check('追问行不会被当成普通气泡', indexHtml.includes("msg.role !== 'card' && msg.role !== 'suggest'"));
check('订单名片渲染运单号', indexHtml.includes('msg.order.tracking_no'));
check('订单名片渲染支付状态', indexHtml.includes('msg.order.payment_status'));
check('订单名片带「立即支付」按钮', indexHtml.includes('payOrder(msg)'));
check('订单名片带「我的订单」入口', indexHtml.includes("goToPage('profile.html')"));

check('order.html 成功页有「回到咨询」', orderHtml.includes('回到咨询'));
check('回到咨询带上订单号（闭环最后一跳）', /index\.html\?order=[^"]*result\.order_no/.test(orderHtml));

// ── 汇总 ───────────────────────────────────────────────────────
console.log('\n' + '='.repeat(62));
console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
if (fails.length) {
    console.log('  失败项：');
    fails.forEach((f) => console.log('    · ' + f));
}
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
