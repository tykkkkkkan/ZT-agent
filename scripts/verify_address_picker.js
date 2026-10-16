#!/usr/bin/env node
/**
 * 历史收货地址（地址簿）下单页接线验收 —— 前端侧
 *
 * 做法与 verify_product_search.js 一致：用 Node vm 把 order.html 的**内联脚本**真跑起来
 * （Vue / axios / AUTH / document / location / window 全部用桩替掉），断言 setup() 的
 * 真实接线与各分支行为，而不是"读源码看有没有写"。
 *
 * 为什么需要它：地址簿后端有 78 项验收，但"后端对了、页面没接上"是一类独立故障 ——
 * 控件忘了加、setup 忘了 return、v-model 绑错字段、存地址把下单流程卡住，
 * 后端测试一个都拦不住。
 *
 * 断言五层：
 *   A. 地址簿接线：拉取 + 默认地址自动带出（且覆盖 profile 的粗粒度预填）
 *   B. onAddressPick：选中一条 → 收货人/电话/地址三件套一起换
 *   C. canOfferSaveAddress：按 (收货人, 电话, 地址) 三元组判重
 *   D. 走真实 submitOrder：下单成功后自动存地址，字段与 is_default 口径正确
 *   E. 模板里控件确实接上了（防"逻辑写了但控件没加进页面"）
 *
 * 运行：node scripts/verify_address_picker.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = path.join(__dirname, '..', 'frontend', 'order.html');

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}

function inlineScripts(file) {
    const html = fs.readFileSync(file, 'utf8');
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    const out = [];
    let m;
    while ((m = re.exec(html))) out.push(m[1]);
    return out;
}

const PRODUCT = {
    id: 1, name: '红虫颗粒', spec: '200g/包', retail_price: 10, wholesale_price: 6,
    can_buy: true, available_stock: 100, alert_line: 5, target_fish: '鲫鱼',
    can_see_wholesale: false, wholesale_min_qty: 10,
};

// ── 桩：Vue 只要能读值就够；AUTH / axios 记录调用并给出可控响应 ──
function makeSandbox(seed) {
    const captured = { options: null };
    const Vue = {
        createApp(options) {
            captured.options = options;
            return { mount() { /* 不真挂载 */ } };
        },
        ref: (v) => ({ value: v }),
        computed: (fn) => ({ get value() { return fn(); } }),
        reactive: (o) => o,
    };

    const state = { addresses: seed.addresses.map((a) => Object.assign({}, a)), savedCalls: [], orderCalls: [] };

    const jsonResp = (ok, payload) => ({ ok, json: async () => payload });

    const AUTH = {
        isLoggedIn: () => seed.loggedIn !== false,
        requireAuth() {},
        ensureLogin: async () => true,
        getAccess: () => 'stub-token',
        authFetch: async (url, init) => {
            const method = (init && init.method) || 'GET';
            if (url === '/api/me/addresses/' && method === 'GET') {
                return jsonResp(true, { success: true, count: state.addresses.length, addresses: state.addresses });
            }
            if (url === '/api/me/addresses/' && method === 'POST') {
                const body = JSON.parse(init.body);
                state.savedCalls.push(body);
                if (body.is_default) state.addresses.forEach((a) => { a.is_default = false; });
                const created = Object.assign({
                    id: 900 + state.savedCalls.length,
                    summary: body.receiver + ' ' + body.phone + ' ' + body.address,
                }, body);
                state.addresses = state.addresses.concat([created]);
                return jsonResp(true, {
                    success: true, message: '已新增收货地址',
                    address: created, addresses: state.addresses,
                });
            }
            if (url === '/api/me/profile/') {
                return jsonResp(true, {
                    success: true, user: seed.user || {},
                    default_address: null, address_count: state.addresses.length,
                });
            }
            return jsonResp(false, {});
        },
    };

    const axios = {
        interceptors: { request: { use() {} }, response: { use() {} } },
        get: async () => ({ data: { success: true, products: [Object.assign({}, PRODUCT)] } }),
        post: async (url, payload) => {
            state.orderCalls.push({ url, payload });
            return {
                data: Object.assign({
                    success: true, message: '下单成功', order_no: 'ZT-TEST-0001',
                    unit_price: 10, total_price: 10, quantity: 1,
                    product_name: PRODUCT.name, spec: PRODUCT.spec,
                    price_tier: '零售价', payment_status: '未支付',
                }, payload || {}),
            };
        },
    };

    const doc = {
        getElementById: () => ({ classList: { add() {}, remove() {} } }),
        addEventListener() {},
    };

    const win = {
        addEventListener() {},
        scrollTo() {},
        AUTH,
    };

    const sandbox = {
        Vue, axios, AUTH,
        document: doc,
        window: win,
        location: { search: seed.search || '' },
        URLSearchParams,
        console, setTimeout, clearTimeout, Promise,
        JSON, Date, Math, Number, String, Object, Array, RegExp, Error,
        parseInt, parseFloat, isNaN,
        captured, state,
    };
    win.location = sandbox.location;
    sandbox.globalThis = sandbox;
    return sandbox;
}

const tick = (ms) => new Promise((r) => setTimeout(r, ms === undefined ? 5 : ms));

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
    const scripts = inlineScripts(HTML);
    let err = '';
    scripts.forEach((code, i) => {
        try { vm.runInContext(code, ctx, { filename: `order.html#script${i + 1}` }); }
        catch (e) { err = e.message; }
    });
    return { sb, ctx, scripts, err };
}

(async function main() {
    console.log('='.repeat(62));
    console.log('  历史收货地址（下单页）验收 · 前端');
    console.log('='.repeat(62));

    // ════════════════════════════════════════════════════════
    console.log('\n【A】地址簿接线：拉取 + 默认地址自动带出');
    // ════════════════════════════════════════════════════════
    const b1 = boot({
        loggedIn: true,
        addresses: [A_DEFAULT, A_SECOND],
        user: { nickname: '汤账号', phone: '13911112222' },
    });
    check('内联脚本全部执行无异常', b1.err === '', b1.err);
    const opts = b1.sb.captured.options;
    check('createApp 拿到组件配置', !!opts && typeof opts.setup === 'function');
    if (!opts) { console.log('\n无法继续：组件配置未捕获'); process.exit(1); }

    const s = opts.setup();
    await tick();

    ['savedAddresses', 'pickedAddressId', 'saveToBook', 'canOfferSaveAddress', 'onAddressPick']
        .forEach((k) => check(`setup 返回了 ${k}（模板里要用）`, s[k] !== undefined));

    check('地址簿已拉取到 2 条', s.savedAddresses.value.length === 2, String(s.savedAddresses.value.length));
    check('下拉框自动选中默认地址', s.pickedAddressId.value === A_DEFAULT.id, String(s.pickedAddressId.value));
    check('默认地址的收货人被带入', s.form.customer_name === A_DEFAULT.receiver, s.form.customer_name);
    check('默认地址的电话被带入', s.form.phone === A_DEFAULT.phone, s.form.phone);
    check('默认地址的详细地址被带入', s.form.address === A_DEFAULT.address, s.form.address);
    check('地址比 profile 的 nickname/phone 更具体（后加载的覆盖先生效的）',
        s.form.customer_name !== '汤账号' && s.form.phone !== '13911112222',
        `${s.form.customer_name} / ${s.form.phone}`);
    check('产品列表也已加载（下游校验依赖它）', s.products.value.length === 1);

    // ════════════════════════════════════════════════════════
    console.log('\n【B】onAddressPick：选中一条 → 三件套一起换');
    // ════════════════════════════════════════════════════════
    s.pickedAddressId.value = A_SECOND.id;
    s.onAddressPick();
    check('收货人换成所选地址的', s.form.customer_name === A_SECOND.receiver, s.form.customer_name);
    check('电话换成所选地址的', s.form.phone === A_SECOND.phone, s.form.phone);
    check('详细地址换成所选地址的', s.form.address === A_SECOND.address, s.form.address);

    const before = { n: s.form.customer_name, p: s.form.phone, a: s.form.address };
    s.pickedAddressId.value = 999999;
    s.onAddressPick();
    check('选中不存在的 id → 不改动表单（不把用户填好的清空）',
        s.form.customer_name === before.n && s.form.phone === before.p && s.form.address === before.a);

    s.pickedAddressId.value = '';
    s.onAddressPick();
    check('选回占位项 → 也不清空（手填内容不被吞掉）', s.form.address === before.a);

    // ════════════════════════════════════════════════════════
    console.log('\n【C】canOfferSaveAddress：按 (收货人, 电话, 地址) 三元组判重');
    // ════════════════════════════════════════════════════════
    s.form.customer_name = '';
    s.form.phone = '';
    s.form.address = '';
    check('地址为空 → 不提示保存', s.canOfferSaveAddress.value === false);

    s.form.customer_name = A_DEFAULT.receiver;
    s.form.phone = A_DEFAULT.phone;
    s.form.address = A_DEFAULT.address;
    check('与已有地址完全一致 → 不重复提示保存', s.canOfferSaveAddress.value === false);

    s.form.customer_name = '换个代收人';
    check('同地址同电话但换了收货人 → 仍提示保存（三元组判重，不是只比地址）',
        s.canOfferSaveAddress.value === true);

    s.form.customer_name = A_DEFAULT.receiver;
    s.form.phone = '13800009999';
    check('同地址同收货人但换了电话 → 仍提示保存', s.canOfferSaveAddress.value === true);

    s.form.customer_name = '全新的人';
    s.form.phone = '13800008888';
    s.form.address = '北京市朝阳区建国路 8 号';
    check('全新地址 → 提示保存', s.canOfferSaveAddress.value === true);

    // ════════════════════════════════════════════════════════
    console.log('\n【D】走真实 submitOrder：下单成功后自动存地址');
    // ════════════════════════════════════════════════════════
    // D1：地址簿为空的全新用户 → 应自动保存，且首条 is_default=true
    const b2 = boot({ loggedIn: true, addresses: [], user: {} });
    check('D1 内联脚本执行无异常', b2.err === '', b2.err);
    const s2 = b2.sb.captured.options.setup();
    await tick();
    check('空地址簿时 savedAddresses 为空', s2.savedAddresses.value.length === 0);
    check('空地址簿时不自动带出（表单仍为空）', s2.form.address === '' && s2.form.customer_name === '');
    check('默认勾选「存入地址簿」', s2.saveToBook.value === true);

    s2.form.customer_name = '首单用户';
    s2.form.phone = '13800007777';
    s2.form.address = '山东省济南市历下区经十路 1 号';
    s2.form.product_id = PRODUCT.id;
    s2.form.quantity = 2;
    check('填完地址后提示保存', s2.canOfferSaveAddress.value === true);

    await s2.submitOrder();
    await tick(20);

    check('下单请求已发出', b2.sb.state.orderCalls.length === 1, String(b2.sb.state.orderCalls.length));
    check('下单 payload 仍带地址（任务书四·4 未被破坏）',
        b2.sb.state.orderCalls[0].payload.address === '山东省济南市历下区经十路 1 号',
        JSON.stringify(b2.sb.state.orderCalls[0].payload.address));
    check('下单成功后自动发起保存地址', b2.sb.state.savedCalls.length === 1,
        `savedCalls=${b2.sb.state.savedCalls.length}`);
    const saved = b2.sb.state.savedCalls[0] || {};
    check('保存的收货人与表单一致', saved.receiver === '首单用户', String(saved.receiver));
    check('保存的电话与表单一致', saved.phone === '13800007777', String(saved.phone));
    check('保存的地址与表单一致', saved.address === '山东省济南市历下区经十路 1 号', String(saved.address));
    check('字段名与后端一致（receiver/phone/address）',
        ['receiver', 'phone', 'address'].every((k) => k in saved), JSON.stringify(Object.keys(saved)));
    check('首条地址 is_default=true', saved.is_default === true, String(saved.is_default));
    check('保存后 savedAddresses 就地更新（"继续下单"即可直接选）',
        s2.savedAddresses.value.length === 1 && s2.savedAddresses.value[0].is_default === true,
        JSON.stringify(s2.savedAddresses.value.length));
    check('提交后进入成功态', s2.submitted.value === true);

    // D2：已有地址时再存 → is_default 必须是 false（不能把已有默认抢掉）
    const b3 = boot({ loggedIn: true, addresses: [A_DEFAULT], user: {} });
    const s3 = b3.sb.captured.options.setup();
    await tick();
    check('D2 已有默认地址被带出', s3.form.address === A_DEFAULT.address);
    s3.form.customer_name = '第二个人';
    s3.form.phone = '13800006666';
    s3.form.address = '浙江省杭州市余杭区文一西路 3 号';
    s3.form.product_id = PRODUCT.id;
    s3.form.quantity = 1;
    await s3.submitOrder();
    await tick(20);
    const saved3 = b3.sb.state.savedCalls[0] || {};
    check('D2 也自动存了地址', b3.sb.state.savedCalls.length === 1, String(b3.sb.state.savedCalls.length));
    check('D2 已有地址时 is_default=false（不抢默认）', saved3.is_default === false, String(saved3.is_default));

    // D3：取消勾选 → 不存
    const b4 = boot({ loggedIn: true, addresses: [A_DEFAULT], user: {} });
    const s4 = b4.sb.captured.options.setup();
    await tick();
    s4.saveToBook.value = false;
    s4.form.customer_name = '不想存的人';
    s4.form.phone = '13800005555';
    s4.form.address = '广东省深圳市南山区科苑路 5 号';
    s4.form.product_id = PRODUCT.id;
    s4.form.quantity = 1;
    await s4.submitOrder();
    await tick(20);
    check('D3 取消勾选后不发起保存', b4.sb.state.savedCalls.length === 0, String(b4.sb.state.savedCalls.length));
    check('D3 下单本身照常成功', s4.submitted.value === true);

    // D4：地址已在地址簿里 → 不重复存
    const b5 = boot({ loggedIn: true, addresses: [A_DEFAULT], user: {} });
    const s5 = b5.sb.captured.options.setup();
    await tick();
    s5.form.product_id = PRODUCT.id;
    s5.form.quantity = 1;
    // 表单此刻就是默认地址的内容（已被自动带出）
    await s5.submitOrder();
    await tick(20);
    check('D4 地址已存在时不重复保存', b5.sb.state.savedCalls.length === 0, String(b5.sb.state.savedCalls.length));

    // ════════════════════════════════════════════════════════
    console.log('\n【E】模板里控件确实接上了（防"逻辑写了但控件没加进页面"）');
    // ════════════════════════════════════════════════════════
    const raw = fs.readFileSync(HTML, 'utf8');
    const has = (t) => raw.indexOf(t) !== -1;
    check('下拉框绑定 v-model="pickedAddressId"', has('v-model="pickedAddressId"'));
    check('切换时调 onAddressPick', has('@change="onAddressPick"'));
    check('选项来自 savedAddresses', has('v-for="a in savedAddresses"'));
    check('无地址时整个下拉不渲染（不出现空选择框）', has('v-if="savedAddresses.length"'));
    check('选项文本用后端下发的 summary（不自己拼）', has('{{ a.summary }}'));
    check('默认地址在选项里带 ★ 标记', has("{{ a.is_default ? '★ ' : '' }}"));
    check('勾选框绑定 v-model="saveToBook"', has('v-model="saveToBook"'));
    check('勾选框受 canOfferSaveAddress 控制', has('v-if="canOfferSaveAddress"'));
    check('无地址簿时给出「填一次就会存下来」的提示', has('下次下单直接选，不用再填'));
    check('**仍保留**手填地址输入框（任务书四·4 不能被地址簿挤掉）', has('v-model="form.address"'));
    check('地址仍是必填（required）',
        /v-model="form\.address"[^>]*required/.test(raw.replace(/\s+/g, ' ')));
    check('期望交期字段未被误删', has('v-model="form.expected_date"'));
    check('地址校验提示仍在下发（errors.address）', has('v-if="errors.address"'));
    check('地址随订单一起提交（form.address 进 payload）', has('address: form.address.trim()'));
    check('resetForm 后重新拉地址簿（"继续下单"不用再填一遍）',
        /resetForm\(\)[\s\S]{0,800}?loadAddressBook\(\)/.test(raw));
    check('存地址不阻塞下单反馈（不 await、静默 catch）',
        /if \(saveToBook\.value && canOfferSaveAddress\.value\) saveAddressToBook\(\);/.test(raw));
    check('下单成功分支里确实调了 saveAddressToBook',
        /submitted\.value = true;[\s\S]{0,300}?saveAddressToBook\(\)/.test(raw));
    check('地址簿失败不弹错（catch 里静默注释）', /地址簿属增强体验，失败静默/.test(raw));

    console.log('\n' + '='.repeat(62));
    console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
    if (fails.length) fails.forEach((f) => console.log('    ✗ ' + f));
    console.log('='.repeat(62));
    process.exit(fails.length ? 1 : 0);
})();
