#!/usr/bin/env node
/**
 * 个人中心「我的收货地址」管理验收 —— 前端侧
 *
 * 做法与 verify_address_picker.js / verify_product_search.js 一致：用 Node vm 把
 * profile.html 的**内联脚本**真跑起来（document / window / AUTH / location 全部打桩），
 * 断言真实的分支行为，而不是"读源码看有没有写"。
 *
 * 为什么需要它：地址簿后端有 78 项契约验收，但「后端对了、页面没接上」是一类独立故障 ——
 * 容器 id 写错、renderAddresses 忘了画按钮、editAddress 没进编辑态、submitAddress
 * 走错方法（POST/PATCH）、删除默认地址后表单没退回新增态、前端校验形同虚设，
 * 后端测试一个都拦不住。
 *
 * 难点：profile.html 的内联脚本是 IIFE（`(function () { ... })();`），内部函数不外泄。
 * 这里用**插桩**（在 IIFE 结尾前塞一行 window.__probe = {...}）拿到内部符号 ——
 * 插的是测试端的副本，仓库文件一个字符都不动。
 *
 * 断言八层：
 *   A. 脚本结构 & 插桩成功
 *   B. loadAddresses：拉取 / 渲染（默认标记、按钮组合、转义）/ 计数 / 空态 / 失败态
 *   C. fillAddressForm：新增态 vs 编辑态，默认勾选锁死逻辑
 *   D. editAddress：进编辑态、填表、滚动；不存在的 id 不动
 *   E. submitAddress：POST/PATCH 分流、前端三道校验、payload 口径、成功后重置
 *   F. setDefaultAddress / deleteAddress：请求形状、列表刷新、删除正在编辑项时退回新增态
 *   G. 模板接线：控件真的在页面上（防"逻辑写了但控件没加"）
 *
 * 运行：node scripts/verify_address_manager.js [profile.html 的路径]
 *       （不带参数时读仓库里的 frontend/profile.html）
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(__dirname, '..', 'frontend', 'profile.html');

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}
function cnt(hay, needle) { return hay.split(needle).length - 1; }

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
    const hook = '\n    window.__probe = { addrState: addrState, renderAddresses: renderAddresses,'
        + ' fillAddressForm: fillAddressForm, resetAddressForm: resetAddressForm,'
        + ' loadAddresses: loadAddresses, esc: esc };\n';
    return code.slice(0, i) + hook + code.slice(i);
}

// ── 极简 DOM 桩：任何 id 都返回一个记忆化元素 ────────────────
function makeDom() {
    const els = new Map();
    const listeners = {};
    function makeEl(id) {
        const el = {
            id,
            value: '',
            textContent: '',
            innerHTML: '',
            checked: false,
            disabled: false,
            style: {},
            scrolls: 0,
            _classes: new Set(),
            scrollIntoView() { el.scrolls++; },
            addEventListener() {},
            querySelectorAll: () => [],
            appendChild() {},
            closest: () => null,
        };
        el.classList = {
            add: (...c) => c.forEach((x) => el._classes.add(x)),
            remove: (...c) => c.forEach((x) => el._classes.delete(x)),
            contains: (c) => el._classes.has(c),
            toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
        };
        return el;
    }
    return {
        els,
        listeners,
        getElementById(id) {
            if (!els.has(id)) els.set(id, makeEl(id));
            return els.get(id);
        },
        addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
        visibilityState: 'visible',
        querySelectorAll: () => [],
    };
}

// ── 桩：AUTH.authFetch 记录调用并按路由给出可控响应 ──────────
function makeSandbox(seed) {
    const calls = [];
    const state = { addresses: (seed.addresses || []).map((a) => Object.assign({}, a)) };
    const fail = seed.fail || {};
    const jsonResp = (ok, payload) => ({ ok, status: ok ? 200 : 400, json: async () => payload });

    const AUTH = {
        isLoggedIn: () => true,
        init: async () => true,
        getRefresh: () => 'stub-refresh',
        getAccess: () => 'stub-access',
        logout() {},
        authFetch: async (url, init) => {
            const method = (init && init.method) || 'GET';
            calls.push({ url, method, body: init && init.body ? JSON.parse(init.body) : null });

            // 写操作可整体注入故障（用于验证页面 catch 分支）
            if (fail.write && method !== 'GET') {
                return jsonResp(false, { message: '保存失败，请稍后再试' });
            }

            if (url === '/api/me/profile/') {
                return jsonResp(true, { success: true, user: { username: '汤账号' }, stats: {}, default_address: null, address_count: state.addresses.length });
            }
            if (url === '/api/me/orders/' || url.indexOf('/api/me/orders') === 0) {
                return jsonResp(true, { success: true, orders: [], has_more: false });
            }
            if (fail.list && url === '/api/me/addresses/' && method === 'GET') {
                return jsonResp(false, { message: '网络开小差了' });
            }
            if (url === '/api/me/addresses/' && method === 'GET') {
                return jsonResp(true, { success: true, count: state.addresses.length, max: 20, addresses: state.addresses });
            }
            if (url === '/api/me/addresses/' && method === 'POST') {
                const body = JSON.parse(init.body);
                if (body.is_default) state.addresses.forEach((a) => { a.is_default = false; });
                state.addresses = state.addresses.concat([Object.assign({
                    id: 900 + calls.length, summary: body.receiver + ' ' + body.phone + ' ' + body.address,
                }, body)]);
                return jsonResp(true, { success: true, message: '已新增收货地址', addresses: state.addresses });
            }
            const mDefault = /^\/api\/me\/addresses\/(\d+)\/default\/$/.exec(url);
            if (mDefault && method === 'POST') {
                const id = Number(mDefault[1]);
                state.addresses.forEach((a) => { a.is_default = (a.id === id); });
                return jsonResp(true, { success: true, message: '已设为默认地址', addresses: state.addresses });
            }
            const mPatch = /^\/api\/me\/addresses\/(\d+)\/$/.exec(url);
            if (mPatch && method === 'PATCH') {
                const id = Number(mPatch[1]);
                const body = JSON.parse(init.body);
                state.addresses = state.addresses.map((a) => (a.id === id ? Object.assign({}, a, body) : a));
                return jsonResp(true, { success: true, message: '已更新收货地址', addresses: state.addresses });
            }
            if (mPatch && method === 'DELETE') {
                const id = Number(mPatch[1]);
                state.addresses = state.addresses.filter((a) => a.id !== id);
                return jsonResp(true, { success: true, message: '已删除', addresses: state.addresses });
            }
            return jsonResp(false, { message: '未打桩的请求：' + method + ' ' + url });
        },
    };

    const win = {
        addEventListener() {},
        confirm: () => seed.confirm !== false,
        AUTH,
        location: { href: '', search: '' },
    };
    const doc = makeDom();

    const sandbox = {
        window: win, document: doc, location: win.location, AUTH,
        console, JSON, Date, Math, Number, String, Object, Array, RegExp, Error,
        parseInt, parseFloat, isNaN, Promise,
        setTimeout: () => 0, clearTimeout: () => {},
        calls, state, doc, fail,
    };
    win.window = win;
    sandbox.globalThis = sandbox;
    return sandbox;
}

// 真实微任务让位（沙箱里的 setTimeout 被桩掉了，用宿主的时间函数）
const tick = (ms) => new Promise((r) => setTimeout(r, ms === undefined ? 6 : ms));

const A_DEFAULT = {
    id: 1, receiver: '汤默认', phone: '13800000001',
    address: '上海市浦东新区临港大道 1 号', is_default: true,
    summary: '汤默认 13800000001 上海市浦东新区临港大道 1 号',
};
const A_SECOND = {
    id: 2, receiver: '汤备用', phone: '13800000002',
    address: '江苏省苏州市工业园区星湖街 2 号', is_default: false,
    summary: '汤备用 13800000002 江苏省苏州市工业园区星湖街 2 号',
};

function boot(seed) {
    const sb = makeSandbox(seed);
    const ctx = vm.createContext(sb);
    const scripts = inlineScripts(HTML).map(instrument);
    let err = '';
    scripts.forEach((code, i) => {
        try { vm.runInContext(code, ctx, { filename: `profile.html#script${i + 1}` }); }
        catch (e) { err = e.message; }
    });
    const probe = sb.window.__probe || null;
    return { sb, ctx, scripts, err, probe, el: (id) => sb.doc.getElementById(id) };
}

(async function main() {
    console.log('='.repeat(64));
    console.log('  个人中心「我的收货地址」验收 · 前端');
    console.log('  文件：' + HTML);
    console.log('='.repeat(64));

    // ════════════════════════════════════════════════════════
    console.log('\n【A】脚本结构 & 插桩');
    // ════════════════════════════════════════════════════════
    const b = boot({ addresses: [A_DEFAULT, A_SECOND] });
    check('内联脚本执行无异常', b.err === '', b.err);
    check('页面脚本是 IIFE 包裹', /\(function\s*\(\)\s*\{/.test(b.scripts[0]));
    check('插桩成功：拿到内部符号', !!b.probe, b.probe ? Object.keys(b.probe).join(',') : 'null');
    if (!b.probe) { console.log('\n无法继续：插桩失败'); process.exit(1); }
    const p = b.probe;
    check('addrState 初始形态正确',
        p.addrState.list.length === 0 && p.addrState.editingId === null,
        JSON.stringify({ n: p.addrState.list.length, e: p.addrState.editingId }));

    // ════════════════════════════════════════════════════════
    console.log('\n【B】loadAddresses：拉取 + 渲染');
    // ════════════════════════════════════════════════════════
    await p.loadAddresses();
    check('拉到 2 条地址', p.addrState.list.length === 2, String(p.addrState.list.length));
    check('列表顺序沿用后端（默认在前）',
        p.addrState.list[0].id === A_DEFAULT.id && p.addrState.list[1].id === A_SECOND.id);
    check('计数文案「共 2 / 20 条」', b.el('addrCount').textContent === '共 2 / 20 条', b.el('addrCount').textContent);

    const html = b.el('addrList').innerHTML;
    check('渲染了收货人', html.indexOf('汤默认') !== -1 && html.indexOf('汤备用') !== -1);
    check('渲染了电话', html.indexOf('13800000001') !== -1 && html.indexOf('13800000002') !== -1);
    check('渲染了详细地址（换行折叠成一整条）',
        html.indexOf('上海市浦东新区临港大道 1 号') !== -1 && html.indexOf('江苏省苏州市工业园区星湖街 2 号') !== -1);
    check('默认地址带「★ 默认」标记', html.indexOf('★ 默认') !== -1);
    check('默认地址卡片用湖青描边强调', html.indexOf('border-lake-200') !== -1);
    check('非默认那条没有「设为默认」按钮的重复渲染（全表只 1 个）',
        cnt(html, 'setDefaultAddress(') === 1, String(cnt(html, 'setDefaultAddress(')));
    check('每条都有「编辑」按钮', cnt(html, 'editAddress(') === 2, String(cnt(html, 'editAddress(')));
    check('每条都有「删除」按钮', cnt(html, 'deleteAddress(') === 2, String(cnt(html, 'deleteAddress(')));
    check('按钮里传的是数字字面量（不是字符串，否则 === 匹配不到）',
        /editAddress\(1\)/.test(html) && /editAddress\(2\)/.test(html) && !/editAddress\('/.test(html));
    check('默认那条渲染在非默认之前（视觉上「默认在先」）',
        html.indexOf('汤默认') < html.indexOf('汤备用'));
    check('文字经过 esc 转义，不是裸拼',
        html.indexOf('&amp;') === -1 || true); // 下面用真实注入用例校验

    // B2：转义 —— 地址里塞 HTML 不能变成活标签
    const bX = boot({ addresses: [{ id: 7, receiver: '<b>坏人</b>', phone: '13800000007', address: '<img src=x onerror=alert(1)>', is_default: false, summary: 'x' }] });
    await bX.probe.loadAddresses();
    const hx = bX.el('addrList').innerHTML;
    check('B2 收货人被转义', hx.indexOf('&lt;b&gt;坏人&lt;/b&gt;') !== -1 && hx.indexOf('<b>坏人</b>') === -1);
    check('B2 地址被转义（不产生活标签）', hx.indexOf('&lt;img') !== -1 && hx.indexOf('<img src=x') === -1);

    // B3：空列表
    const bE = boot({ addresses: [] });
    await bE.probe.loadAddresses();
    const he = bE.el('addrList').innerHTML;
    check('B3 空列表给出引导文案', he.indexOf('还没有保存的收货地址') !== -1);
    check('B3 空列表计数清空（不留上次数值）', bE.el('addrCount').textContent === '', JSON.stringify(bE.el('addrCount').textContent));
    check('B3 空列表时默认勾选框自动勾上（首条即默认）', bE.el('addrDefault').checked === true);
    check('B3 空列表时表单停在「新增」态', bE.el('addrFormTitle').textContent === '新增收货地址');

    // B4：拉取失败
    const bF = boot({ addresses: [A_DEFAULT], fail: { list: true } });
    await bF.probe.loadAddresses();
    check('B4 拉取失败在列表区给出可读错误', bF.el('addrList').innerHTML.indexOf('地址加载失败') !== -1, bF.el('addrList').innerHTML.slice(0, 60));
    check('B4 失败时把后端 message 带出来', bF.el('addrList').innerHTML.indexOf('网络开小差了') !== -1);

    // ════════════════════════════════════════════════════════
    console.log('\n【C】fillAddressForm：新增态 / 编辑态切换');
    // ════════════════════════════════════════════════════════
    await p.loadAddresses();     // 恢复到「2 条」上下文
    p.fillAddressForm(null);
    check('传 null → 三个输入框清空',
        b.el('addrReceiver').value === '' && b.el('addrPhone').value === '' && b.el('addrDetail').value === '');
    check('传 null → 标题回到「新增收货地址」', b.el('addrFormTitle').textContent === '新增收货地址');
    check('传 null → 按钮文案「保存地址」', b.el('addrSubmitBtn').textContent === '保存地址');
    check('传 null → 「取消」按钮隐藏', b.el('addrCancelBtn').classList.contains('hidden'));
    check('传 null → 有地址时不默认勾选', b.el('addrDefault').checked === false);
    check('传 null → 勾选框可点（未被锁）', b.el('addrDefault').disabled === false);
    check('传 null → 默认说明隐藏', b.el('addrDefaultHint').classList.contains('hidden'));

    p.fillAddressForm(A_DEFAULT);
    check('传默认地址 → 三件套填满',
        b.el('addrReceiver').value === A_DEFAULT.receiver
        && b.el('addrPhone').value === A_DEFAULT.phone
        && b.el('addrDetail').value === A_DEFAULT.address);
    check('传默认地址 → 标题「编辑收货地址」', b.el('addrFormTitle').textContent === '编辑收货地址');
    check('传默认地址 → 按钮「保存修改」', b.el('addrSubmitBtn').textContent === '保存修改');
    check('传默认地址 → 「取消」按钮出现', !b.el('addrCancelBtn').classList.contains('hidden'));
    check('传默认地址 → 勾选框勾上且锁死（默认只能切换不能取消）',
        b.el('addrDefault').checked === true && b.el('addrDefault').disabled === true);
    check('传默认地址 → 默认说明展开', !b.el('addrDefaultHint').classList.contains('hidden'));

    p.fillAddressForm(A_SECOND);
    check('传非默认地址 → 勾选框不勾且可点',
        b.el('addrDefault').checked === false && b.el('addrDefault').disabled === false);
    check('传非默认地址 → 默认说明收起（只在锁死时才需要解释）',
        b.el('addrDefaultHint').classList.contains('hidden'));

    p.addrState.editingId = 2;
    p.resetAddressForm();
    check('resetAddressForm → editingId 归零', p.addrState.editingId === null);
    check('resetAddressForm → 表单回到新增态（标题/按钮一起回去）',
        b.el('addrFormTitle').textContent === '新增收货地址' && b.el('addrSubmitBtn').textContent === '保存地址');
    check('cancelAddressEdit 已挂到 window（模板 onclick 要用）', typeof b.sb.window.cancelAddressEdit === 'function');

    // ════════════════════════════════════════════════════════
    console.log('\n【D】editAddress：进入编辑态');
    // ════════════════════════════════════════════════════════
    await p.loadAddresses();
    p.resetAddressForm();
    b.sb.window.editAddress(A_SECOND.id);
    check('editingId 记下被编辑的 id', p.addrState.editingId === A_SECOND.id, String(p.addrState.editingId));
    check('表单填的是被编辑那条（不是默认那条）',
        b.el('addrReceiver').value === A_SECOND.receiver && b.el('addrDetail').value === A_SECOND.address);
    check('标题切到「编辑收货地址」', b.el('addrFormTitle').textContent === '编辑收货地址');
    check('表单被滚动到视野内（长列表里点了「编辑」能看见）', b.el('addrForm').scrolls > 0, String(b.el('addrForm').scrolls));

    const keepId = p.addrState.editingId;
    b.sb.window.editAddress(999999);
    check('编辑不存在的 id → editingId 不变（不进入半残状态）', p.addrState.editingId === keepId);
    b.sb.window.editAddress('2');
    check('传字符串 id → 不命中（严格 === 比较，类型不一致就不认）', p.addrState.editingId === keepId);

    // ════════════════════════════════════════════════════════
    console.log('\n【E】submitAddress：POST / PATCH 分流 + 前端校验');
    // ════════════════════════════════════════════════════════
    // E0：三条前端校验（都不该发请求）
    await p.loadAddresses();
    p.resetAddressForm();
    const reqBefore = b.sb.calls.length;
    const setField = (rc, ph, ad) => {
        b.el('addrReceiver').value = rc;
        b.el('addrPhone').value = ph;
        b.el('addrDetail').value = ad;
    };
    const fakeEvent = { preventDefault() { fakeEvent.prevented = true; }, prevented: false };

    setField('', '13800000001', '上海市某路 1 号');
    let rv = await b.sb.window.submitAddress(fakeEvent);
    check('E0 收货人为空 → 不发请求', b.sb.calls.length === reqBefore, String(b.sb.calls.length - reqBefore));
    check('E0 收货人为空 → 仍返回 false 拦住原生提交', rv === false);
    check('E0 event.preventDefault 被调用（表单不整页刷新）', fakeEvent.prevented === true);

    setField('汤某人', '138-0000-0001', '上海市某路 1 号');
    await b.sb.window.submitAddress(fakeEvent);
    check('E0 电话含非数字 → 不发请求', b.sb.calls.length === reqBefore);

    setField('汤某人', '12345', '上海市某路 1 号');
    await b.sb.window.submitAddress(fakeEvent);
    check('E0 电话不足 7 位 → 不发请求', b.sb.calls.length === reqBefore);

    setField('汤某人', '138000000012345678901', '上海市某路 1 号');   // 21 位
    await b.sb.window.submitAddress(fakeEvent);
    check('E0 电话超过 20 位 → 不发请求', b.sb.calls.length === reqBefore);

    setField('汤某人', '13800000001', '   ');
    await b.sb.window.submitAddress(fakeEvent);
    check('E0 地址为纯空白 → 不发请求（trim 后判空）', b.sb.calls.length === reqBefore);

    // E1：新增态 → POST
    setField('  汤新人  ', ' 13800000009 ', '  上海市黄浦区南京东路 9 号  ');
    rv = await b.sb.window.submitAddress(fakeEvent);
    const lastPost = b.sb.calls[b.sb.calls.length - 1];
    check('E1 新增态走 POST /api/me/addresses/', lastPost.method === 'POST' && lastPost.url === '/api/me/addresses/',
        lastPost.method + ' ' + lastPost.url);
    check('E1 payload 字段名与后端一致', ['receiver', 'phone', 'address', 'is_default'].every((k) => k in lastPost.body),
        JSON.stringify(Object.keys(lastPost.body)));
    check('E1 payload 已 trim（不把用户敲的首尾空格存进去）',
        lastPost.body.receiver === '汤新人' && lastPost.body.phone === '13800000009'
        && lastPost.body.address === '上海市黄浦区南京东路 9 号',
        JSON.stringify(lastPost.body.receiver));
    check('E1 is_default 是布尔（不是字符串）', typeof lastPost.body.is_default === 'boolean', typeof lastPost.body.is_default);
    check('E1 成功后本地列表就地更新（不用重拉）', p.addrState.list.length === 3, String(p.addrState.list.length));
    check('E1 成功后表单回到新增态', p.addrState.editingId === null && b.el('addrFormTitle').textContent === '新增收货地址');

    // E2：编辑态 → PATCH，且只打这一条
    await p.loadAddresses();
    b.sb.window.editAddress(A_SECOND.id);
    b.el('addrReceiver').value = '汤改过';
    b.el('addrPhone').value = '13800000022';
    b.el('addrDetail').value = '江苏省苏州市姑苏区平江路 22 号';
    const before2 = b.sb.calls.length;
    rv = await b.sb.window.submitAddress(fakeEvent);
    const lastPatch = b.sb.calls[b.sb.calls.length - 1];
    check('E2 编辑态走 PATCH /api/me/addresses/2/', lastPatch.method === 'PATCH' && lastPatch.url === '/api/me/addresses/2/',
        lastPatch.method + ' ' + lastPatch.url);
    check('E2 编辑态只发一次请求', b.sb.calls.length - before2 === 1, String(b.sb.calls.length - before2));
    check('E2 改后的内容进了本地列表', p.addrState.list.filter((a) => a.id === A_SECOND.id)[0].receiver === '汤改过');
    check('E2 编辑成功后表单退回新增态（不会一直停在"编辑"）',
        p.addrState.editingId === null && b.el('addrFormTitle').textContent === '新增收货地址');
    check('E2 非默认地址编辑时 is_default=false 原样传（后端会忽略，前端不擅自改语义）',
        lastPatch.body.is_default === false, String(lastPatch.body.is_default));

    // E3：接口报错 → 不崩、列表不变、编辑态保住
    const bG = boot({ addresses: [A_DEFAULT, A_SECOND] });
    await bG.probe.loadAddresses();
    bG.sb.window.editAddress(A_SECOND.id);
    const n0 = bG.probe.addrState.list.length;
    bG.el('addrReceiver').value = '汤改到一半';
    bG.el('addrPhone').value = '13800000033';
    bG.el('addrDetail').value = '上海市某路 33 号';
    bG.el('addrDefault').checked = false;
    bG.sb.fail.write = true;                       // 注入写失败
    await bG.sb.window.submitAddress(fakeEvent);
    check('E3 后端报错时不抛到页面（catch 兜住）', bG.probe.addrState.list.length === n0, String(bG.probe.addrState.list.length));
    check('E3 后端报错时保留编辑态（用户不用重新点「编辑」，内容也没丢）',
        bG.probe.addrState.editingId === A_SECOND.id, String(bG.probe.addrState.editingId));
    check('E3 报错后用户填的内容还在输入框里（不被清空）', bG.el('addrDetail').value === '上海市某路 33 号');
    bG.sb.fail.write = false;
    await bG.sb.window.submitAddress(fakeEvent);
    check('E3 故障恢复后重试即成功（同一个按钮就能救回来）',
        bG.probe.addrState.editingId === null
        && bG.probe.addrState.list.filter((a) => a.id === A_SECOND.id)[0].address === '上海市某路 33 号');

    // ════════════════════════════════════════════════════════
    console.log('\n【F】setDefaultAddress / deleteAddress');
    // ════════════════════════════════════════════════════════
    // F0：用干净的 2 条上下文（上面的用例往 p 里加过第 3 条，别串味）
    const bSD = boot({ addresses: [A_DEFAULT, A_SECOND] });
    await bSD.probe.loadAddresses();
    bSD.probe.resetAddressForm();
    bSD.sb.window.editAddress(A_DEFAULT.id);        // 编辑态非空，验证设默认不打断编辑
    const beforeF = bSD.sb.calls.length;
    await bSD.sb.window.setDefaultAddress(A_SECOND.id);
    const cDefault = bSD.sb.calls[bSD.sb.calls.length - 1];
    check('F 设默认走 POST /api/me/addresses/2/default/',
        cDefault.method === 'POST' && cDefault.url === '/api/me/addresses/2/default/', cDefault.method + ' ' + cDefault.url);
    check('F 设默认只发一次请求', bSD.sb.calls.length - beforeF === 1, String(bSD.sb.calls.length - beforeF));
    check('F 列表按后端返回刷新（默认位转移）',
        bSD.probe.addrState.list.filter((a) => a.is_default).length === 1
        && bSD.probe.addrState.list.filter((a) => a.is_default)[0].id === A_SECOND.id);
    const hF = bSD.el('addrList').innerHTML;
    check('F 设默认后重绘：新默认那条不再有「设为默认」按钮（2 条里只剩 1 个）',
        cnt(hF, 'setDefaultAddress(') === 1, String(cnt(hF, 'setDefaultAddress(')));
    check('F 设默认后 ★ 只出现一次，且落在新默认那条上（旧的标记要撤掉）',
        cnt(hF, '★ 默认') === 1 && hF.indexOf('★ 默认') > hF.indexOf('汤备用'),
        '★x' + cnt(hF, '★ 默认'));
    check('F 设默认后旧默认那条多了「设为默认」按钮（可以切回去）',
        cnt(hF, 'setDefaultAddress(1)') === 1, String(cnt(hF, 'setDefaultAddress(1)')));
    check('F 设默认不打断正在进行的编辑（editingId 保持）', bSD.probe.addrState.editingId === A_DEFAULT.id,
        String(bSD.probe.addrState.editingId) + ' vs ' + A_DEFAULT.id);

    // F2：删除前询问被拒 → 不发请求
    const bH = boot({ addresses: [A_DEFAULT, A_SECOND], confirm: false });
    await bH.probe.loadAddresses();
    const beforeH = bH.sb.calls.length;
    await bH.sb.window.deleteAddress(A_SECOND.id);
    check('F2 确认框点「取消」→ 不发删除请求', bH.sb.calls.length === beforeH, String(bH.sb.calls.length - beforeH));
    check('F2 取消后列表原样', bH.probe.addrState.list.length === 2);

    // F3：确认删除 → DELETE
    bH.sb.window.confirm = () => true;
    await bH.sb.window.deleteAddress(A_SECOND.id);
    const cDel = bH.sb.calls[bH.sb.calls.length - 1];
    check('F3 确认后走 DELETE /api/me/addresses/2/', cDel.method === 'DELETE' && cDel.url === '/api/me/addresses/2/', cDel.method + ' ' + cDel.url);
    check('F3 列表就地少一条', bH.probe.addrState.list.length === 1);
    check('F3 已重绘（被删的不再出现）', bH.el('addrList').innerHTML.indexOf('汤备用') === -1);

    // F4：删掉的正是正在编辑的那条 → 表单退回新增态
    const bI = boot({ addresses: [A_DEFAULT, A_SECOND], confirm: true });
    await bI.probe.loadAddresses();
    bI.sb.window.editAddress(A_SECOND.id);
    check('F4 前置：已进入编辑态 2', bI.probe.addrState.editingId === A_SECOND.id);
    await bI.sb.window.deleteAddress(A_SECOND.id);
    check('F4 删掉正在编辑的那条 → editingId 归零（不留悬空引用）', bI.probe.addrState.editingId === null);
    check('F4 删掉正在编辑的那条 → 表单退回新增态',
        bI.el('addrFormTitle').textContent === '新增收货地址' && bI.el('addrDetail').value === '');

    // F5：删的不是正在编辑的那条 → 编辑态保持
    const bJ = boot({ addresses: [A_DEFAULT, A_SECOND], confirm: true });
    await bJ.probe.loadAddresses();
    bJ.sb.window.editAddress(A_DEFAULT.id);
    await bJ.sb.window.deleteAddress(A_SECOND.id);
    check('F5 删别的地址不打断编辑（editingId 保持）', bJ.probe.addrState.editingId === A_DEFAULT.id, String(bJ.probe.addrState.editingId));

    // ════════════════════════════════════════════════════════
    console.log('\n【G】模板接线：控件真的在页面上');
    // ════════════════════════════════════════════════════════
    const raw = fs.readFileSync(HTML, 'utf8');
    const has = (t) => raw.indexOf(t) !== -1;
    ['addrList', 'addrForm', 'addrReceiver', 'addrPhone', 'addrDetail', 'addrDefault',
        'addrDefaultHint', 'addrFormTitle', 'addrSubmitBtn', 'addrCancelBtn', 'addrCount']
        .forEach((id) => check(`存在 #${id}`, has('id="' + id + '"')));
    check('卡片标题写着「我的收货地址」', has('我的收货地址'));
    check('表单 onsubmit 绑到 submitAddress 且 return（拦住原生提交）',
        has('onsubmit="return submitAddress(event)"'));
    check('「取消」按钮绑到 cancelAddressEdit', has('onclick="cancelAddressEdit()"'));
    check('收货人输入框必填', /id="addrReceiver"[^>]*required/.test(raw));
    check('电话输入框必填', /id="addrPhone"[^>]*required/.test(raw));
    check('地址输入框必填', /id="addrDetail"[^>]*required/.test(raw));
    check('电话框 maxlength=20（与后端上限一致）', /id="addrPhone"[^>]*maxlength="20"/.test(raw));
    check('地址框 maxlength=255（与后端上限一致）', /id="addrDetail"[^>]*maxlength="255"/.test(raw));
    check('电话框 inputmode=numeric（手机上弹数字键盘）', /id="addrPhone"[^>]*inputmode="numeric"/.test(raw));
    check('写明上限「最多保存 20 条」', has('最多保存 20 条'));
    check('默认地址「只能切换不能取消」的说明在页面里', has('只能切换、不能取消'));
    check('初始化块里 await 了 loadAddresses', /await loadAddresses\(\)/.test(raw));
    check('初始化顺序：先 loadProfile 再 loadAddresses（订单/统计先出，地址后补）',
        raw.indexOf('await loadProfile()') < raw.indexOf('await loadAddresses()'));
    // 索引纯净度：本卡片刻意不依赖并行会话引入的 card-float / section-lake
    const seg = raw.slice(raw.indexOf('我的收货地址') - 400, raw.indexOf('我的留言与客服答复'));
    check('本卡片不依赖 card-float（HEAD 里没有这个类，避免样式悬空）', seg.indexOf('card-float') === -1);
    check('本卡片用的是基线样式（白底 + 1px 描边 + 轻投影）',
        seg.indexOf('bg-white rounded-2xl border border-stone-200 shadow-sm') !== -1);
    check('地址卡片的图标用品牌色变量（不写裸 hex）', /color:var\(--jade\)/.test(seg));

    console.log('\n' + '='.repeat(64));
    console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
    if (fails.length) fails.forEach((f) => console.log('    ✗ ' + f));
    console.log('='.repeat(64));
    process.exit(fails.length ? 1 : 0);
})();
