#!/usr/bin/env node
/**
 * 收银台「支付方式选择」验收 —— 前端侧（任务书五·1 · #72）
 *
 * 做法与 verify_address_manager.js 一致：用 Node vm 把 pay.html 的**内联脚本**
 * 真跑起来（document / window / AUTH / location 全部打桩），断言真实的分支行为，
 * 而不是"读源码看有没有写"。
 *
 * 为什么非要有它：余额支付最怕的不是后端算错，而是**前端把话说错**——
 *   · 余额不够却没提前说，用户点了才报错（可避免的失败被当成常态）
 *   · 余额不够时不给"去哪充"的落点，用户卡在收银台
 *   · 支付中锁失效，连点两下发出两次扣款请求
 *   · 还在读余额就把按钮画成可点（点了才发现余额是 0）
 *   · 切到余额支付时二维码没收干净，屏上留着两张"能付"的东西
 * 这些后端 79 项契约一个都拦不住。
 *
 * 难点：pay.html 的内联脚本是 IIFE（`(function () { ... })();`），内部函数不外泄。
 * 这里用**插桩**（在 IIFE 结尾前塞一行 window.__probe = {...}）拿到内部符号 ——
 * 插的是测试端的副本，仓库文件一个字符都不动。
 *
 * 断言十一层：
 *   A. 脚本结构 & 插桩成功
 *   B. money()：字符串金额格式化（NaN 安全）
 *   C. orderNoFromPath()：路径解析 + ?order= 兜底 + **不误吞手机页路径**
 *   D. loadTicket()：开票请求、订单摘要渲染、二维码、倒计时、启动轮询
 *   E. renderBalance()：余额充足 / 不足 / 未知 三种形态
 *   F. switchMethod()：来回切换、已支付后锁死
 *   G. payByBalance()：成功 / 余额不足 / 其他失败 / 网络异常 / 连点锁
 *   H. loadBalance()：成功与失败（失败不能挡住扫码支付）
 *   I. 幂等与乐观更新：后端没说 paid 就不许切成功态
 *   J. 倒计时按"截止时刻"算（后台节流不漂移）
 *   K. 模板接线：关键控件真的在页面上
 *
 * 运行：node scripts/verify_pay_console.js [pay.html 的路径]
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(__dirname, '..', 'frontend', 'pay.html');

const hostSetTimeout = setTimeout;

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}

// DOM 桩不解析 innerHTML，所以「按钮是不是禁用」只能从 **HTML 字符串**里读。
// 直接问 getElementById('balBtn').disabled 会拿到一个刚创建的空元素（恒 false）——
// 那是测桩，不是测页面。
function balDisabled(box) { return box.indexOf('id="balBtn" disabled') !== -1; }

function inlineScripts(file) {
    const html = fs.readFileSync(file, 'utf8');
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    const out = [];
    let m;
    while ((m = re.exec(html))) out.push(m[1]);
    return out;
}

// IIFE 内部符号不外泄 → 在结尾 `})();` 之前插一行导出钩子
function instrument(code) {
    const marker = '})();';
    const i = code.lastIndexOf(marker);
    if (i === -1) return code;
    const hook = '\n    window.__probe = {'
        + ' state: state, els: els, esc: esc, money: money, mmss: mmss,'
        + ' orderNoFromPath: orderNoFromPath, renderOrder: renderOrder,'
        + ' renderMethodTabs: renderMethodTabs, switchMethod: switchMethod,'
        + ' renderPayArea: renderPayArea, renderBalance: renderBalance,'
        + ' renderReady: renderReady, renderPaid: renderPaid, renderNotice: renderNotice,'
        + ' loadBalance: loadBalance, loadTicket: loadTicket, payByBalance: payByBalance,'
        + ' checkStatus: checkStatus };\n';
    return code.slice(0, i) + hook + code.slice(i);
}

function makeDom() {
    const els = new Map();
    const listeners = {};
    function makeEl(id) {
        const el = {
            id, value: '', textContent: '', innerHTML: '', checked: false,
            disabled: false, style: {}, onclick: null,
            addEventListener() {}, querySelectorAll: () => [],
        };
        el.classList = { add() {}, remove() {}, contains: () => false, toggle() {} };
        return el;
    }
    return {
        els, listeners,
        getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
        addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
        visibilityState: 'visible',
        querySelectorAll: () => [],
    };
}

const ORDER = {
    order_no: 'ZT-20260101-0001', product_name: '超诱饵料', quantity: 2,
    customer_name: '汤验证', phone: '13800000001',
    address: '上海市浦东新区临港大道 1 号', created_at: '2026-01-01 10:00',
    total_price: '20.00', status: '未发货', payment_status: '未支付',
};

function makeSandbox(seed) {
    seed = seed || {};
    const calls = [];
    const fail = seed.fail || {};
    const jsonResp = (ok, status, payload) => ({ ok, status, json: async () => payload });

    const AUTH = {
        isLoggedIn: () => true,
        init: async () => seed.authOk !== false,
        ensureLogin: async () => true,
        getAccess: () => 'stub-access',
        logout() {},
        authFetch: async (url, init) => {
            const method = (init && init.method) || 'GET';
            calls.push({ url, method });
            // 故意拖慢余额支付：用来验证「支付中」的 busy 锁真的挡住了连点
            if (seed.slowPay && url.indexOf('/balance/') !== -1) {
                await new Promise((r) => hostSetTimeout(r, 40));
            }

            if (url.indexOf('/api/me/wallet/') === 0) {
                if (fail.wallet) return jsonResp(false, 500, { message: '钱包读取失败' });
                return jsonResp(true, 200, {
                    success: true, balance: seed.balance === undefined ? '100.00' : seed.balance,
                    recharged: '100.00', paid: '0.00', refunded: '0.00',
                    tx_count: 1, topup_max: '50000.00', transactions: [],
                });
            }
            const mBal = /^\/api\/pay\/([^/]+)\/balance\/$/.exec(url);
            if (mBal && method === 'POST') {
                if (fail.network) throw new Error('模拟网络中断');
                if (fail.balance) return jsonResp(false, 400, fail.balance);
                const r = seed.balancePay || {
                    success: true, code: 'paid', already: false, amount: '20.00',
                    balance: '80.00', order: Object.assign({}, ORDER, { payment_status: '已支付' }),
                    message: '余额支付成功，商家将尽快为您安排发货。',
                };
                // HTTP 状态跟着业务成败走 —— 桩必须按真实语义返回，
                // 否则「失败」会被测成 200，页面自然走进成功分支（假绿/假红都由此而来）。
                const st = seed.balancePayStatus || (r.success === false ? 400 : 200);
                return jsonResp(r.success !== false, st, r);
            }
            const mTicket = /^\/api\/pay\/([^/]+)\/ticket\/$/.exec(url);
            if (mTicket && method === 'POST') {
                if (fail.ticket) return jsonResp(false, 409, { message: '该订单已支付，无需重复付款。' });
                return jsonResp(true, 200, {
                    success: true, message: '已生成支付二维码',
                    ticket: { token: 'tok-abc', status: seed.ticketStatus || '待支付', channel: '扫码支付',
                        amount: '20.00', order_no: ORDER.order_no, remain_seconds: 300, open: true },
                    pay_url: 'http://192.168.1.7:8001/pay/confirm/tok-abc/',
                    qr_svg: '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1H0z"/></svg>',
                    amount: '20.00', order: ORDER,
                });
            }
            const mStatus = /^\/api\/pay\/([^/]+)\/status\/$/.exec(url);
            if (mStatus && method === 'GET') {
                return jsonResp(true, 200, {
                    success: true, paid: !!seed.paid, pay_state: seed.paid ? 'paid' : 'payable',
                    message: seed.paid ? '已支付' : '',
                    ticket: { status: seed.ticketStatus || '待支付', remain_seconds: 200, open: true },
                    order: ORDER,
                });
            }
            return jsonResp(false, 404, { message: '未打桩的请求：' + method + ' ' + url });
        },
    };

    const win = {
        addEventListener() {}, AUTH,
        location: { pathname: seed.pathname || '/pay/ZT-20260101-0001/', search: seed.search || '', href: '' },
    };
    const doc = makeDom();
    const timers = { intervals: [] };

    const sandbox = {
        window: win, document: doc, location: win.location,
        AUTH, console, JSON, Date, Math, Number, String, Object, Array, RegExp, Error,
        parseInt, parseFloat, isNaN, Promise, URLSearchParams,
        setTimeout: () => 0, clearTimeout: () => {},
        setInterval: (fn) => { timers.intervals.push(fn); return timers.intervals.length; },
        clearInterval: () => {},
        navigator: {},
        calls, doc, fail, timers,
    };
    win.window = win;
    sandbox.globalThis = sandbox;
    sandbox.__timers = timers;
    return sandbox;
}

const tick = (ms) => new Promise((r) => hostSetTimeout(r, ms === undefined ? 6 : ms));

function run(sandbox) {
    const code = instrument(inlineScripts(HTML).join('\n;\n'));
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox);
    return sandbox.window.__probe;
}

async function boot(seed) {
    const sb = makeSandbox(seed);
    const probe = run(sb);
    // 触发 DOMContentLoaded（init 是 async，等微任务跑完）
    const handlers = sb.doc.listeners['DOMContentLoaded'] || [];
    for (const h of handlers) await h();
    await tick(10);
    return { sb, probe };
}

(async function main() {
    console.log('  收银台支付方式 · 前端验收（#72）');
    console.log('='.repeat(64));

    // ════════════════════════════════════════════════════════════
    console.log('\n【A】脚本结构 & 插桩');
    // ════════════════════════════════════════════════════════════
    const html = fs.readFileSync(HTML, 'utf8');
    const scripts = inlineScripts(HTML);
    check('pay.html 有内联脚本', scripts.length >= 1, String(scripts.length));
    check('内联脚本是 IIFE（可插桩）', scripts.join('').indexOf('})();') !== -1);
    check('页面引入了 auth.js（收银台要登录）', /<script\s+src="\/auth\.js"/.test(html));
    {
        const p = run(makeSandbox({}));
        const need = ['state', 'money', 'orderNoFromPath', 'renderMethodTabs', 'switchMethod',
            'renderBalance', 'renderReady', 'renderPaid', 'loadBalance', 'loadTicket', 'payByBalance', 'checkStatus'];
        const missing = need.filter((k) => !(k in p));
        check('插桩拿到全部内部符号', missing.length === 0, missing.join(', '));
        check('默认支付方式 = 扫码支付（余额是备选，不是默认）', p.state.method === '扫码支付', p.state.method);
        check('初始未支付 / 不忙', p.state.paid === false && p.state.busy === false);
        check('初始余额为 null（未读取）', p.state.balance === null, String(p.state.balance));
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【B】money()：金额格式化');
    // ════════════════════════════════════════════════════════════
    {
        const p = run(makeSandbox({}));
        check("'20.00' → '20.00'", p.money('20.00') === '20.00', p.money('20.00'));
        check('20 → \'20.00\'', p.money(20) === '20.00', p.money(20));
        check("'99.5' → '99.50'（补位）", p.money('99.5') === '99.50', p.money('99.5'));
        check('null → 0.00（不 NaN）', p.money(null) === '0.00', p.money(null));
        check("'abc' → 原样返回（不显示 NaN）", p.money('abc') === 'abc', p.money('abc'));
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【C】orderNoFromPath()：路径解析');
    // ════════════════════════════════════════════════════════════
    {
        const cases = [
            ['/pay/ZT-20260101-0001/', '', 'ZT-20260101-0001'],
            ['/pay/ZT-1', '', 'ZT-1'],
            ['/pay/DD%2D9/', '', 'DD-9'],
            ['/', '?order=ZT-9', 'ZT-9'],
            // 关键：手机确认页的路径不能被 PC 页误吞成订单号
            ['/pay/confirm/tok-abc/', '', ''],
        ];
        for (const [pn, qs, want] of cases) {
            const p = run(makeSandbox({ pathname: pn, search: qs }));
            const got = p.orderNoFromPath();
            check(`${pn}${qs} → ${want || '(空)'}`, got === want, got);
        }
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【D】loadTicket()：开票 + 摘要 + 二维码 + 轮询');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({});
        const post = sb.calls.filter((c) => /\/ticket\/$/.test(c.url) && c.method === 'POST');
        check('进页面就发了一次开票请求', post.length === 1, `实际 ${post.length}`);
        check('开票用的是订单号（不是 token）', post[0] && post[0].url === '/api/pay/ZT-20260101-0001/ticket/', post[0] && post[0].url);
        const ob = sb.doc.getElementById('orderBox').innerHTML;
        check('订单号渲染出来了', ob.indexOf('ZT-20260101-0001') !== -1);
        check('产品名与数量渲染出来了', ob.indexOf('超诱饵料 × 2') !== -1, '');
        check('收货人渲染出来了', ob.indexOf('汤验证') !== -1);
        check('应付金额 = ¥20.00', sb.doc.getElementById('amountText').textContent === '¥20.00',
            sb.doc.getElementById('amountText').textContent);
        check('支付方式标签渲染出两个 tab',
            (sb.doc.getElementById('methodTabs').innerHTML.match(/data-method=/g) || []).length === 2);
        check('标签里有「余额支付」', sb.doc.getElementById('methodTabs').innerHTML.indexOf('余额支付') !== -1);
        const pb = sb.doc.getElementById('payBox').innerHTML;
        check('二维码 SVG 已渲染', pb.indexOf('<svg') !== -1 && pb.indexOf('width:100%') !== -1);
        check('二维码是内联 SVG（没有 <img> 外链）', pb.indexOf('<img') === -1);
        check('给出可复制的支付链接', pb.indexOf('http://192.168.1.7:8001/pay/confirm/tok-abc/') !== -1);
        check('倒计时按剩余秒数显示（5:00）', pb.indexOf('5:00') !== -1, pb.match(/剩余有效期 [^<]*/) || '');
        check('启动了轮询', !!probe.state.poll);
        check('启动了倒计时', !!probe.state.timer);
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【E】renderBalance()：充足 / 不足 / 未知 三种形态');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({});
        probe.switchMethod('余额支付');
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('切换后不再显示二维码', box.indexOf('<svg') === -1);
        check('显示账户余额', box.indexOf('¥100.00') !== -1);
        check('显示本单应付', box.indexOf('¥20.00') !== -1);
        check('显示支付后余额（100 − 20 = 80）', box.indexOf('¥80.00') !== -1);
        check('余额充足 → 按钮可点', !balDisabled(box), box.match(/<button[^>]*id="balBtn"[^>]*>/) || '');
        check('按钮文案写明金额', /用余额支付 ¥20\.00/.test(box), '');
        check('充足时**不给**充值链接', box.indexOf('profile.html#wallet') === -1);
    }
    {
        const { sb, probe } = await boot({ balance: '5.00' });
        probe.switchMethod('余额支付');
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('余额不足 → 按钮禁用', balDisabled(box), box.match(/<button[^>]*id="balBtn"[^>]*>/) || '');
        check('写明还差多少（20 − 5 = 15.00）', box.indexOf('还差 ¥15.00') !== -1, '');
        check('给出充值落点（profile.html#wallet）', box.indexOf('profile.html#wallet') !== -1);
        check('支付后余额封底为 0.00（不显示负数）', box.indexOf('¥0.00') !== -1);
    }
    {
        const { sb, probe } = await boot({ fail: { wallet: true } });
        probe.switchMethod('余额支付');
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('余额读不到 → 按钮禁用（不装作能付）', balDisabled(box), box.match(/<button[^>]*id="balBtn"[^>]*>/) || '');
        check('文案是「余额读取中…」', box.indexOf('余额读取中') !== -1);
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【F】switchMethod()：来回切换与锁死');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({});
        probe.switchMethod('余额支付');
        check('切到余额 → state.method 更新', probe.state.method === '余额支付', probe.state.method);
        probe.switchMethod('扫码支付');
        check('切回扫码 → state.method 更新', probe.state.method === '扫码支付', probe.state.method);
        check('切回扫码 → 二维码恢复', sb.doc.getElementById('payBox').innerHTML.indexOf('<svg') !== -1);
        check('切换方式**不会**重新开票（复用同一张码）',
            sb.calls.filter((c) => /\/ticket\/$/.test(c.url)).length === 1,
            String(sb.calls.filter((c) => /\/ticket\/$/.test(c.url)).length));
        probe.state.paid = true;
        probe.switchMethod('余额支付');
        check('已支付后切换失效（成功态不该被换掉）', probe.state.method === '扫码支付', probe.state.method);
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【G】payByBalance()：成功 / 不足 / 失败 / 连点');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({});
        probe.switchMethod('余额支付');
        await probe.payByBalance();
        await tick(10);
        const post = sb.calls.filter((c) => /\/balance\/$/.test(c.url));
        check('发出了余额支付请求', post.length === 1, String(post.length));
        check('走 POST', post[0] && post[0].method === 'POST', post[0] && post[0].method);
        check('成功后 state.paid = true', probe.state.paid === true);
        check('成功后按钮区变成「支付成功」',
            sb.doc.getElementById('payBox').innerHTML.indexOf('支付成功') !== -1);
        check('成功后余额用返回体刷新（80.00）', probe.state.balance === '80.00', String(probe.state.balance));
        check('成功后订单摘要换成后端回传的那一份（已支付）',
            probe.state.order && probe.state.order.payment_status === '已支付',
            probe.state.order && String(probe.state.order.payment_status));
        check('成功后不再轮询', !probe.state.poll);
    }
    {
        const { sb, probe } = await boot({
            balance: '5.00',
            balancePay: {
                success: false, code: 'insufficient',
                message: '余额不足：当前 ¥5.00，本次需要 ¥20.00。请先充值。',
                balance: '5.00', shortage: '15.00',
            },
        });
        probe.switchMethod('余额支付');
        await probe.payByBalance();
        await tick(10);
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('余额不足 → 不切成功态', probe.state.paid === false);
        check('余额不足 → 提示还差多少', box.indexOf('还差 ¥15.00') !== -1, '');
        check('余额不足 → 仍给充值落点', box.indexOf('profile.html#wallet') !== -1);
        check('余额不足 → 按钮仍禁用（防连点）', balDisabled(box), '');
    }
    {
        const { sb, probe } = await boot({
            balance: '100.00',
            balancePay: { success: false, code: 'closed', message: '订单已取消或已退货，无需付款。' },
        });
        probe.switchMethod('余额支付');
        await probe.payByBalance();
        await tick(10);
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('其他业务失败 → 原样显示后端文案', box.indexOf('订单已取消或已退货') !== -1, '');
        check('其他业务失败 → 不切成功态', probe.state.paid === false);
        check('其他业务失败 → 余额仍够，按钮可再点（重试）', !balDisabled(box), '');
    }
    {
        const { sb, probe } = await boot({ fail: { network: true } });
        probe.switchMethod('余额支付');
        await probe.payByBalance();
        await tick(10);
        check('网络异常 → 有提示且不切成功态',
            probe.state.paid === false && sb.doc.getElementById('payBox').innerHTML.indexOf('网络异常') !== -1);
    }
    {
        // 连点：余额接口故意慢 40ms，期间再点一次
        const { sb, probe } = await boot({ slowPay: true });
        probe.switchMethod('余额支付');
        const p1 = probe.payByBalance();
        const p2 = probe.payByBalance();     // 第二次应被 busy 锁挡掉
        await Promise.all([p1, p2]);
        await tick(10);
        check('连点两次只发一次余额支付请求',
            sb.calls.filter((c) => /\/balance\/$/.test(c.url)).length === 1,
            String(sb.calls.filter((c) => /\/balance\/$/.test(c.url)).length));
        check('连点后 busy 已释放', probe.state.busy === false);
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【H】loadBalance()：钱包读不到也不能挡住扫码支付');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({ fail: { wallet: true } });
        check('钱包失败时不抛错（返回 null）', probe.state.balance === null);
        check('钱包失败不影响开票', sb.calls.some((c) => /\/ticket\/$/.test(c.url)));
        check('钱包失败仍渲染二维码', sb.doc.getElementById('payBox').innerHTML.indexOf('<svg') !== -1);
    }
    {
        const { probe } = await boot({ balance: '0.00' });
        check("余额 0 也被正确读成 '0.00'（不当成未读取）", probe.state.balance === '0.00', String(probe.state.balance));
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【I】不做乐观更新：只有后端说 paid 才切成功态');
    // ════════════════════════════════════════════════════════════
    {
        const { sb, probe } = await boot({});
        probe.switchMethod('余额支付');
        // 模拟：进页面时余额充足，但另一标签页刚把钱花掉
        probe.state.balance = '9';
        probe.renderBalance('');
        const box = sb.doc.getElementById('payBox').innerHTML;
        check('余额变化后重绘 → 按钮立刻变禁用（不可能点出成功态）', balDisabled(box), '');
        // 后端说 paid 时才切成功态
        probe.state.paid = true;
        probe.renderPayArea();
        check('后端说 paid → 才渲染成功态',
            sb.doc.getElementById('payBox').innerHTML.indexOf('支付成功') !== -1);
        check('成功态里给「查看我的订单」出口',
            sb.doc.getElementById('payBox').innerHTML.indexOf('profile.html') !== -1);
    }
    {
        const { sb, probe } = await boot({ paid: true, ticketStatus: '已支付' });
        check('开票时后端已说已支付 → 直接进成功态', probe.state.paid === true);
        check('已支付不再开轮询', !probe.state.poll);
        await probe.checkStatus();
        check('已支付后再轮询也不重复请求',
            sb.calls.filter((c) => /\/status\/$/.test(c.url)).length === 0,
            String(sb.calls.filter((c) => /\/status\/$/.test(c.url)).length));
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【J】倒计时按「截止时刻」算（后台节流不漂移）');
    // ════════════════════════════════════════════════════════════
    {
        const src = scripts.join('');
        check('源码里用的是 deadline（截止时刻）而不是累减', src.indexOf('deadline') !== -1);
        check('每 tick 由 deadline − now 重算剩余秒数',
            /Math\.round\(\(deadline - Date\.now\(\)\) \/ 1000\)/.test(src));
    }

    // ════════════════════════════════════════════════════════════
    console.log('\n【K】模板接线');
    // ════════════════════════════════════════════════════════════
    {
        check('有支付方式容器 #methodTabs', html.indexOf('id="methodTabs"') !== -1);
        check('有支付区 #payBox', html.indexOf('id="payBox"') !== -1);
        check('有订单摘要 #orderBox', html.indexOf('id="orderBox"') !== -1);
        check('有应付金额 #amountText', html.indexOf('id="amountText"') !== -1);
        check('标题写「订单支付」（不再是扫码支付）', html.indexOf('<title>订单支付') !== -1);
        check('说明里点出两种方式', html.indexOf('余额支付') !== -1 && html.indexOf('扫码支付') !== -1);
        check('有 role=tablist（无障碍）', html.indexOf('role="tablist"') !== -1);
        check('本页声明必须登录（收银台要能核对订单归属）',
            html.indexOf('requireAuth: true') !== -1);
    }

    console.log('\n' + '='.repeat(64));
    console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
    if (fails.length) {
        console.log('  失败项：');
        fails.forEach((f) => console.log('    ✗ ' + f));
    }
    console.log('='.repeat(64));
    process.exit(fails.length ? 1 : 0);
})();
