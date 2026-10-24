#!/usr/bin/env node
/**
 * 商品详情弹窗（任务书二·4 商品信息 / 六·4 评价）验收脚本 —— 前端侧
 *
 * 为什么需要它
 * ------------
 * 改造前产品页是「单页卡片」：介绍被 line-clamp-2 截断，卡片上唯一的点击项
 * 是「N 条评价」—— 客户想看完整介绍 / 参数 / 全部评价，没有第二个出口。
 * 这类问题不会报错、不会 404，只是「点了没反应」，所以必须由脚本守：
 *
 *   A. 纯函数：detailImageOf（库里图片优先 → 老映射回落 → 空）
 *              money2（后端 float 直接渲染会出现「6」而不是「6.00」）
 *              lockBodyScroll（在 Node vm 里没有 document.body，也不能抛错）
 *   B. setup() 真实接线：openDetail 真的请求评价接口、closeDetail 复位、
 *      openReviews 会滚到评价区（防"逻辑写了但按钮没接"）
 *   C. 模板静态断言：弹窗存在、三处入口（图/标题/查看详情）、完整介绍不再截断
 *
 * 运行：node scripts/verify_product_detail.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = path.join(__dirname, '..', 'frontend', 'products.html');

let pass = 0;
const fails = [];
function check(label, cond, detail) {
    if (cond) { pass++; console.log('  ✓ ' + label + (detail ? '  ← ' + detail : '')); }
    else { fails.push(label); console.log('  ✗ ' + label + (detail ? '  ← ' + detail : '')); }
}

// ── 1. 抽内联脚本（跳过 <script src=...>） ────────────────────
function inlineScripts(file) {
    const html = fs.readFileSync(file, 'utf8');
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    const out = [];
    let m;
    while ((m = re.exec(html))) out.push(m[1]);
    return out;
}

// ── 2. 沙箱：Vue / axios / document 全部用桩，记录调用 ─────────
const captured = { options: null };
const calls = { axiosUrls: [], scrolled: 0 };
const scrolledEl = {
    scrollIntoView() { calls.scrolled++; },
};
const Vue = {
    createApp(options) {
        captured.options = options;
        return { mount() { /* 不真挂载 */ } };
    },
    ref: (v) => ({ value: v }),
    computed: (fn) => ({ get value() { return fn(); } }),
};
const sandbox = {
    Vue,
    axios: {
        get: async (url) => {
            calls.axiosUrls.push(url);
            return {
                data: {
                    success: true, review_count: 3, showing: 2, rating_avg: 4.7,
                    reviews: [
                        { id: 1, stars: '★★★★★', nickname: '王**', content: '好用', created_at: '2026-10-01 10:00' },
                        { id: 2, stars: '★★★★☆', nickname: '李**', content: '', created_at: '2026-10-02 10:00' },
                    ],
                },
            };
        },
    },
    document: {
        getElementById: (id) => (id === 'detail-reviews' ? scrolledEl : { classList: { add() {}, remove() {} } }),
        addEventListener() {},
    },
    window: { addEventListener() {}, location: { href: '' } },
    console,
    setTimeout, clearTimeout,
};

const scripts = inlineScripts(HTML);
if (scripts.length === 0) {
    console.log('✗ 没有从 products.html 里抽到内联脚本，无法验证');
    process.exit(1);
}
const ctx = vm.createContext(sandbox);
scripts.forEach((code, i) => {
    try {
        vm.runInContext(code, ctx, { filename: `products.html#script${i + 1}` });
    } catch (e) {
        console.log(`✗ 内联脚本 #${i + 1} 执行报错：${e.message}`);
        fails.push(`script#${i + 1} 执行`);
    }
});

console.log('='.repeat(62));
console.log('  商品详情弹窗 验收 · 前端');
console.log('='.repeat(62));

const { detailImageOf, money2, lockBodyScroll, matchProductImage } = sandbox;

// ── A. 纯函数 ─────────────────────────────────────────────────
console.log('\n[A] 纯函数');

check('detailImageOf 已从页面加载', typeof detailImageOf === 'function');
check('库里有 image_url → 用库里的',
    detailImageOf({ name: '红虫颗粒', image_url: '/assets/images/product-lanji.webp' }) === '/assets/images/product-lanji.webp');
check('image_url 前后空格被 trim',
    detailImageOf({ name: '红虫颗粒', image_url: '  /assets/images/x.webp  ' }) === '/assets/images/x.webp');
check('image_url 为空 → 回落到老映射',
    detailImageOf({ name: '红虫颗粒', image_url: '' }) === matchProductImage('红虫颗粒'),
    detailImageOf({ name: '红虫颗粒', image_url: '' }));
check('image_url 缺失 → 同样回落（不报错）',
    detailImageOf({ name: '九一八' }) === matchProductImage('九一八'));
check('未知商品且无图 → 空串（由模板走设计化占位）',
    detailImageOf({ name: '没见过的饵料' }) === '');
check('传 null → 空串', detailImageOf(null) === '');
check('传 undefined → 空串', detailImageOf(undefined) === '');

check('money2 已导出', typeof money2 === 'function');
check('money2(6) = "6.00"（不再渲染成「6」）', money2(6) === '6.00', money2(6));
check('money2(6.5) = "6.50"', money2(6.5) === '6.50', money2(6.5));
check('money2("8") = "8.00"', money2('8') === '8.00', money2('8'));
check('money2(undefined) = "0.00"', money2(undefined) === '0.00', money2(undefined));
check('money2(null) = "0.00"', money2(null) === '0.00', money2(null));

check('lockBodyScroll 在没有 document.body 的环境里不抛错',
    (() => { try { lockBodyScroll(true); lockBodyScroll(false); return true; } catch (e) { return false; } })());

// ── B. setup() 真实接线 ───────────────────────────────────────
console.log('\n[B] setup() 接线');

const opts = captured.options;
check('createApp 拿到了组件配置', !!opts && typeof opts.setup === 'function');
const s = (opts && typeof opts.setup === 'function') ? opts.setup() : null;
check('setup() 可执行', !!s);

if (s) {
    ['detailOpen', 'detailProduct', 'detailData', 'detailReviewsLoading',
     'detailImage', 'onDetailImgError', 'money2',
     'openDetail', 'closeDetail', 'openReviews', 'scrollToReviews', 'starsOf']
        .forEach((k) => check(`setup 返回了 ${k}`, s[k] !== undefined));

    check('初始弹窗是关闭的', s.detailOpen.value === false);
    check('初始没有选中商品', s.detailProduct.value === null);
    check('初始图片为空（无商品）', s.detailImage.value === '');

    // openDetail：真发请求、真灌数据
    const p = { id: 4, name: '蓝鲫X5', category: '腥香饵', spec: '100g/包', target_fish: '鲫鱼、鲤鱼',
        description: '腥香结合，诱鱼快', effective_price: 8, can_see_wholesale: false,
        in_stock: true, stock_label: '✓ 现货 80 包', sku: 'SKU-0004', image_url: '' };

    (async () => {
        calls.axiosUrls.length = 0;
        await s.openDetail(p);
        check('openDetail 打开了弹窗', s.detailOpen.value === true);
        check('openDetail 记下了当前商品', s.detailProduct.value === p);
        check('openDetail 请求了该商品的评价接口',
            calls.axiosUrls.length === 1 && calls.axiosUrls[0] === '/api/products/4/reviews/',
            calls.axiosUrls.join(','));
        check('openDetail 灌入了评价数据', s.detailData.value.review_count === 3 && s.detailData.value.reviews.length === 2,
            JSON.stringify({ n: s.detailData.value.review_count, len: s.detailData.value.reviews.length }));
        check('加载态已复位', s.detailReviewsLoading.value === false);
        check('detailImage 回落到老映射（image_url 为空）',
            s.detailImage.value === matchProductImage('蓝鲫X5'), s.detailImage.value);

        // 图片失败 → 走占位
        s.onDetailImgError();
        check('图片加载失败后 detailImage 置空（走设计化占位）', s.detailImage.value === '');

        // 再开另一个商品：失败标记要复位
        await s.openDetail({ id: 10, name: '钓鱼王', image_url: '/assets/images/a.webp' });
        check('换商品后图片失败标记复位', s.detailImage.value === '/assets/images/a.webp', s.detailImage.value);

        // openReviews：打开 + 滚到评价区
        calls.scrolled = 0;
        s.openReviews({ id: 4, name: '蓝鲫X5' });
        await new Promise((r) => setTimeout(r, 150));
        check('openReviews 也打开了同一个弹窗', s.detailOpen.value === true);
        check('openReviews 滚到了评价区', calls.scrolled >= 1, String(calls.scrolled));

        // 纯滚动入口可单独调用
        calls.scrolled = 0;
        s.scrollToReviews();
        check('scrollToReviews 直接调用也能定位', calls.scrolled === 1);

        // closeDetail：复位
        s.closeDetail();
        check('closeDetail 关闭弹窗', s.detailOpen.value === false);
        check('closeDetail 清掉当前商品', s.detailProduct.value === null);

        // 边界：空商品不该发请求
        calls.axiosUrls.length = 0;
        await s.openDetail(null);
        await s.openDetail({});
        check('空商品 openDetail 不发请求、不开弹窗',
            calls.axiosUrls.length === 0 && s.detailOpen.value === false);

        check('money2 已接到模板', s.money2 === money2);
        check('starsOf 5 分 → 五星', s.starsOf(5) === '★★★★★');
        check('starsOf 0 分 → 五星空', s.starsOf(0) === '☆☆☆☆☆');

        finish();
    })();
}

// ── C. 模板静态断言 ───────────────────────────────────────────
function staticChecks() {
    console.log('\n[C] 模板里真的有这个弹窗');
    const html = fs.readFileSync(HTML, 'utf8');
    check('详情弹窗存在', html.indexOf('v-if="detailOpen && detailProduct"') !== -1);
    check('点遮罩可关闭', html.indexOf('@click.self="closeDetail()"') !== -1);
    check('有独立的关闭按钮', html.indexOf('aria-label="关闭商品详情"') !== -1);
    check('列表图可点进详情', /media-ratio--contain[\s\S]{0,120}@click="openDetail\(p\)"/.test(html));
    check('标题可点进详情', /@click="openDetail\(p\)"[^>]*>\{\{ p\.name \}\}/.test(html));
    check('有「查看详情」按钮', html.indexOf('查看详情') !== -1);
    check('「N 条评价」走同一套弹窗', html.indexOf('openReviews(p)') !== -1);
    check('详情展示完整介绍（不截断）',
        /detailProduct\.description[\s\S]{0,160}whitespace-pre-line/.test(html));
    check('详情有评价区锚点', html.indexOf('id="detail-reviews"') !== -1);
    check('详情有商品编码/SKU', html.indexOf('detailProduct.sku') !== -1);
    check('详情有库存状态', html.indexOf('detailProduct.stock_label') !== -1);
    check('详情有不可购原因说明', html.indexOf('detailProduct.buy_block_message') !== -1);
    check('详情有「立即下单」入口', /detailProduct\.in_stock[\s\S]{0,200}order\.html\?product=/.test(html));
    // 「批量定制」离价格区较远，按弹窗片段判断而不是固定字符窗口
    const detailBlock = html.slice(
        html.indexOf('v-if="detailOpen && detailProduct"'),
        html.indexOf('</main>')
    );
    check('详情有「批量定制」入口', detailBlock.indexOf('custom.html') !== -1 && detailBlock.indexOf('批量定制') !== -1);
    check('详情有「咨询 AI」兜底入口（不可购时）', detailBlock.indexOf('index.html#ai-section') !== -1);
    check('列表卡片仍保留两行摘要（列表场景需要）', html.indexOf('line-clamp-2') !== -1);
}

function finish() {
    staticChecks();
    console.log('\n' + '='.repeat(62));
    console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
    if (fails.length) fails.forEach((f) => console.log('    ✗ ' + f));
    console.log('='.repeat(62));
    process.exit(fails.length ? 1 : 0);
}
