#!/usr/bin/env node
/**
 * 商品图前端出口（任务书四·2）验收脚本
 *
 * 改造前两个前端文件各写一套「按商品名 → 图片」的映射，且路径还是 `.png`
 * （资源早已迁移成 `.webp`）→ 五张图全部 404、静默回退通用占位，
 * 而代码看起来"配了图"。这类事故不报错、不看网络面板发现不了，只能靠断言守。
 *
 * 本脚本验三件事：
 *   A. app.js 的 productImage：商品对象优先用 image_url；字符串向后兼容；
 *      未知商品回退 fallback；映射后缀必须是 .webp
 *   B. products.html 的 productImageOf：同一套优先级，空入参返回空串
 *      （空串才让模板走"设计化占位"，而不是塞一张通用图）
 *   C. 静态断言：index.html 传的是商品对象（不是 .name），页面里没有 .png 映射
 *
 * 运行：node scripts/verify_product_images.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'frontend', 'app.js');
const PRODUCTS = path.join(__dirname, '..', 'frontend', 'products.html');
const INDEX = path.join(__dirname, '..', 'frontend', 'index.html');

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}

const noop = () => {};

// ── 1. app.js 沙箱（与 verify_order_card.js 同一手法） ─────────
function loadApp() {
    const captured = { options: null };
    const Vue = {
        createApp(options) { captured.options = options; return { mount() {} }; },
        ref: (v) => ({ value: v }),
        nextTick: (fn) => { if (typeof fn === 'function') fn(); },
        onMounted: noop,
    };
    const sandbox = {
        Vue,
        console,
        setTimeout, clearTimeout,
        document: { querySelector: () => null, getElementById: () => null, addEventListener: noop, createElement: () => ({ style: {} }) },
        window: { location: { href: '' }, addEventListener: noop, AUTH: null },
        location: { search: '', pathname: '/index.html', hash: '' },
        history: { replaceState: noop },
        localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
        URLSearchParams,
        captured,
    };
    sandbox.globalThis = sandbox;
    const ctx = vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(APP, 'utf8'), ctx, { filename: 'app.js' });
    return { sandbox, ctx };
}

// ── 2. products.html 沙箱（内联脚本） ─────────────────────────
function loadProducts() {
    const html = fs.readFileSync(PRODUCTS, 'utf8');
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    const scripts = [];
    let m;
    while ((m = re.exec(html))) scripts.push(m[1]);
    const sandbox = {
        Vue: {
            createApp: () => ({ mount() {} }),
            ref: (v) => ({ value: v }),
            computed: (fn) => ({ get value() { return fn(); } }),
        },
        axios: { get: async () => ({ data: {} }) },
        document: { getElementById: () => null, addEventListener: noop },
        window: { addEventListener: noop, location: { href: '' } },
        console, setTimeout, clearTimeout,
    };
    sandbox.globalThis = sandbox;
    const ctx = vm.createContext(sandbox);
    scripts.forEach((code, i) => vm.runInContext(code, ctx, { filename: `products.html#script${i + 1}` }));
    return sandbox;
}

console.log('='.repeat(62));
console.log('  商品图前端出口 验收');
console.log('='.repeat(62));

const FALLBACK = 'assets/images/fallback.svg';

// ── A. app.js ─────────────────────────────────────────────────
console.log('\n[A] app.js · productImage');

const app = loadApp();
check('setup() 可执行', !!(app.sandbox.captured.options && app.sandbox.captured.options.setup));
const state = app.sandbox.captured.options.setup();
const productImage = state.productImage;
check('productImage 已 return（模板要用）', typeof productImage === 'function');

check('对象 + image_url → 用库里的图',
    productImage({ name: '红虫颗粒', image_url: '/assets/images/a.webp' }) === '/assets/images/a.webp');
check('image_url 有空格 → trim 后使用',
    productImage({ name: '红虫颗粒', image_url: '  /assets/images/a.webp ' }) === '/assets/images/a.webp');
check('对象无 image_url → 回落到名称映射',
    productImage({ name: '红虫颗粒' }) === 'assets/images/product-hongchong.webp',
    productImage({ name: '红虫颗粒' }));
check('名称映射是 .webp（改造前写 .png，五张图全 404）',
    productImage({ name: '蓝鲫X5' }).endsWith('.webp'), productImage({ name: '蓝鲫X5' }));
check('字符串入参向后兼容（老调用点）',
    productImage('九一八') === 'assets/images/product-jiuyiba.webp', productImage('九一八'));
check('部分包含也能命中（「蓝鲫X5 新款」）',
    productImage({ name: '蓝鲫X5 新款' }) === 'assets/images/product-lanji.webp');
check('未知商品 → fallback', productImage({ name: '没见过的饵料' }) === FALLBACK);
check('空对象 → fallback（不抛错）', productImage({}) === FALLBACK);
check('null → fallback', productImage(null) === FALLBACK);
check('undefined → fallback', productImage(undefined) === FALLBACK);
check('image_url 为空串时不会返回空（名片必须有图）',
    productImage({ name: '红虫颗粒', image_url: '' }) === 'assets/images/product-hongchong.webp');

// ── B. products.html ─────────────────────────────────────────
console.log('\n[B] products.html · productImageOf');

const prod = loadProducts();
const productImageOf = prod.productImageOf;
check('productImageOf 是顶层函数（模板直接用）', typeof productImageOf === 'function');
check('对象 + image_url → 用库里的图',
    productImageOf({ name: '红虫颗粒', image_url: '/assets/images/b.webp' }) === '/assets/images/b.webp');
check('对象无 image_url → 回落名称映射',
    productImageOf({ name: '螺鲤3号' }) === 'assets/images/product-luoli.webp');
check('未知商品 → **空串**（让模板走设计化占位，而不是通用图）',
    productImageOf({ name: '没见过的饵料' }) === '');
check('null → 空串', productImageOf(null) === '');
check('字符串入参兼容', productImageOf('速攻2号') === 'assets/images/product-sugong.webp');
check('detailImageOf 与 productImageOf 同源',
    prod.detailImageOf({ name: '红虫颗粒', image_url: '/assets/images/x.webp' }) === '/assets/images/x.webp'
    && prod.detailImageOf({ name: '没见过的饵料' }) === '');

// ── C. 静态断言 ───────────────────────────────────────────────
console.log('\n[C] 模板不再按名字猜图');

const indexHtml = fs.readFileSync(INDEX, 'utf8');
const productsHtml = fs.readFileSync(PRODUCTS, 'utf8');
const appJs = fs.readFileSync(APP, 'utf8');

check('index.html 把商品对象传给 productImage',
    indexHtml.indexOf('productImage(msg.product)') !== -1);
check('index.html 不再传 msg.product.name（那样拿不到 image_url）',
    indexHtml.indexOf('productImage(msg.product.name)') === -1);
check('products.html 列表用 productImageOf(p)',
    productsHtml.indexOf('productImageOf(p)') !== -1);
check('两个文件里都没有 .png 产品图映射',
    !/product-[a-z]+\.png/.test(appJs) && !/product-[a-z]+\.png/.test(productsHtml));

console.log('\n' + '='.repeat(62));
console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
if (fails.length) fails.forEach((f) => console.log('    ✗ ' + f));
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
