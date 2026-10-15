#!/usr/bin/env node
/**
 * 产品搜索筛选（任务书二·4）验收脚本 —— 前端侧
 *
 * 做法与 verify_auth_flow.js 相同：用 Node vm 把 products.html 的**内联脚本**真加载
 * 起来（Vue / axios / document / window 用桩替掉），然后直接断言页面里那两个纯函数
 * 的各分支，而不是"读源码看有没有写"。
 *
 * 断言两层：
 *   A. 纯函数 matchProduct / filterProducts / fishOptionsOf / categoryOptionsOf
 *   B. setup() 的真实接线（refs + computed + clearFilters 是否都返回、联动是否正确）
 *
 * 运行：node scripts/verify_product_search.js
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

// ── 1. 抽出内联脚本（跳过 <script src=...>） ──────────────────
function inlineScripts(file) {
    const html = fs.readFileSync(file, 'utf8');
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
    const out = [];
    let m;
    while ((m = re.exec(html))) out.push(m[1]);
    return out;
}

// ── 2. 造沙箱：Vue 用桩，ref/computed 只要能读就够 ─────────────
function makeSandbox() {
    const captured = { options: null };
    const Vue = {
        createApp(options) {
            captured.options = options;
            return { mount() { /* 不真挂载，避免碰 DOM */ } };
        },
        ref: (v) => ({ value: v }),
        computed: (fn) => ({ get value() { return fn(); } }),
    };
    const doc = {
        getElementById: () => ({ classList: { add() {}, remove() {} } }),
        addEventListener() {},
    };
    const sandbox = {
        Vue,
        axios: { get: async () => ({ data: {} }) },
        document: doc,
        window: { addEventListener() {} },
        console,
        setTimeout, clearTimeout,
        captured,
    };
    sandbox.globalThis = sandbox;
    return sandbox;
}

const scripts = inlineScripts(HTML);
if (scripts.length === 0) {
    console.log('✗ 没有从 products.html 里抽到内联脚本，无法验证');
    process.exit(1);
}
const sandbox = makeSandbox();
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
console.log('  产品搜索筛选（任务书二·4）验收 · 前端');
console.log('='.repeat(62));

const { matchProduct, filterProducts, fishOptionsOf, categoryOptionsOf } = sandbox;

// ── 3. 测试数据（与库内真实商品对齐） ──────────────────────────
const P = [
    { id: 1, name: '红虫颗粒', sku: 'SKU-0001', category: '浓腥饵', target_fish: '鲫鱼、鲤鱼', spec: '200g/包', description: '高蛋白浓腥味，野钓黑坑通用' },
    { id: 2, name: '九一八', sku: 'SKU-0002', category: '广谱饵', target_fish: '鲫鱼、鲤鱼', spec: '150g/包', description: '经典广谱饵，四季可用' },
    { id: 3, name: '螺鲤3号', sku: 'SKU-0003', category: '薯香饵', target_fish: '鲤鱼', spec: '200g/包', description: '薯香味浓，专攻大鲤鱼' },
    { id: 4, name: '蓝鲫X5', sku: 'SKU-0004', category: '腥香饵', target_fish: '鲫鱼、鲤鱼', spec: '100g/包', description: '腥香结合，诱鱼快' },
    { id: 5, name: '速攻2号', sku: 'SKU-0005', category: '奶香饵', target_fish: '鲫鱼', spec: '200g/包', description: '奶香浓郁，秋冬鲫鱼必备' },
    { id: 10, name: '钓鱼王', sku: '1', category: '', target_fish: '鲫鱼', spec: '袋', description: '很好用' },
];
const ids = (list) => list.map((p) => p.id);

console.log('\n【A1】matchProduct：关键词能命中哪些字段');
check('函数已从页面加载出来', typeof matchProduct === 'function');
check('null 产品 → false', matchProduct(null, 'x', '', '') === false);
check('无条件 → 全部通过', P.every((p) => matchProduct(p, '', '', '')));
check('关键词命中**名称**', matchProduct(P[0], '红虫', '', ''));
check('关键词命中**商品编码**且忽略大小写', matchProduct(P[2], 'sku-0003', '', ''));
check('关键词命中**分类**', matchProduct(P[2], '薯香', '', ''));
check('关键词命中**适用鱼种**', matchProduct(P[2], '鲤鱼', '', ''));
check('关键词命中**描述**', matchProduct(P[2], '专攻大鲤鱼', '', ''));
check('关键词命中**规格**', matchProduct(P[0], '200g', '', ''));
check('关键词前后空格被 trim', matchProduct(P[0], '  红虫  ', '', ''));
check('不相关的关键词 → false', matchProduct(P[0], '不存在的商品', '', '') === false);
check('空的「未分类」商品在无关键词时也不被误杀', matchProduct(P[5], '', '', ''));

console.log('\n【A2】matchProduct：分类是精确相等，不是子串');
check('分类精确命中', matchProduct(P[2], '', '薯香饵', ''));
check('分类相近但不相同 → 不通过（薯香饵 ≠ 腥香饵）',
    matchProduct(P[3], '', '薯香饵', '') === false);
check('分类传空串 = 不过滤（未分类商品也会出现）', matchProduct(P[5], '', '', ''));

console.log('\n【A3】matchProduct：鱼种按「包含」匹配（target_fish 是多值字段）');
check('选「鲤鱼」能命中「鲫鱼、鲤鱼」的商品', matchProduct(P[0], '', '', '鲤鱼'));
check('选「鲤鱼」命中「鲤鱼」单值商品', matchProduct(P[2], '', '', '鲤鱼'));
check('选「鲫鱼」不命中只有「鲤鱼」的商品', matchProduct(P[2], '', '', '鲫鱼') === false);
check('鱼种传空串 = 不过滤', matchProduct(P[2], '', '', ''));

console.log('\n【B】filterProducts：组合筛选');
check('空列表 → []', filterProducts([], 'x', '', '').length === 0);
check('传非数组 → []（不抛异常）', filterProducts(null, 'x', '', '').length === 0);
check('无条件 → 全部 6 款', filterProducts(P, '', '', '').length === 6);
check('按名称 → 红虫颗粒', JSON.stringify(ids(filterProducts(P, '红虫', '', ''))) === '[1]');
check('按分类 → 螺鲤3号', JSON.stringify(ids(filterProducts(P, '', '薯香饵', ''))) === '[3]');
check('按鱼种「鲤鱼」→ 4 款', filterProducts(P, '', '', '鲤鱼').length === 4,
    JSON.stringify(ids(filterProducts(P, '', '', '鲤鱼'))));
check('按鱼种「鲫鱼」→ 5 款', filterProducts(P, '', '', '鲫鱼').length === 5,
    JSON.stringify(ids(filterProducts(P, '', '', '鲫鱼'))));
check('关键词 + 鱼种组合 → 蓝鲫X5',
    JSON.stringify(ids(filterProducts(P, 'X5', '', '鲤鱼'))) === '[4]');
check('关键词 + 分类组合 → 速攻2号',
    JSON.stringify(ids(filterProducts(P, '香', '奶香饵', ''))) === '[5]');
check('无匹配 → []', filterProducts(P, '不存在ZZZ', '', '').length === 0);
check('组合筛不出 → []', filterProducts(P, '', '薯香饵', '鲫鱼').length === 0);
check('筛选不改变原数组（纯函数，无副作用）', P.length === 6);

console.log('\n【C】下拉选项生成');
const fishOpts = fishOptionsOf(P);
check('鱼种从「鲫鱼、鲤鱼」拆成两项', fishOpts.length === 2, JSON.stringify(fishOpts));
check('鱼种顺序按首次出现', fishOpts[0] === '鲫鱼' && fishOpts[1] === '鲤鱼');
check('鱼种去重（多款商品重复出现不叠加）', new Set(fishOpts).size === fishOpts.length);
const catOpts = categoryOptionsOf(P);
check('分类选项排除空值（未分类的不进下拉）', catOpts.length === 5, JSON.stringify(catOpts));
check('分类选项不含空串', catOpts.indexOf('') === -1);
check('分类选项去重', new Set(catOpts).size === catOpts.length);
check('非数组输入不抛异常', fishOptionsOf(null).length === 0 && categoryOptionsOf(undefined).length === 0);

console.log('\n【D】setup() 真实接线');
const opts = sandbox.captured && sandbox.captured.options;
check('createApp 拿到了组件配置', !!opts && typeof opts.setup === 'function');
if (opts && typeof opts.setup === 'function') {
    const s = opts.setup();
    ['kw', 'catFilter', 'fishFilter', 'filtering', 'filteredProducts',
     'categoryOptions', 'fishOptions', 'clearFilters'].forEach((k) => {
        check(`setup 返回了 ${k}（模板里要用）`, s[k] !== undefined);
    });
    check('初始 filteredProducts = 空（还没加载数据）', s.filteredProducts.value.length === 0);
    check('初始 filtering = false', s.filtering.value === false);

    // 灌入数据，验证联动
    s.products.value = P.map((p, i) => Object.assign({}, p, { is_main: i === 0 }));
    check('灌数据后 filteredProducts = 6', s.filteredProducts.value.length === 6);
    check('categoryOptions 跟随数据生成', s.categoryOptions.value.length === 5);
    check('fishOptions 跟随数据生成', s.fishOptions.value.length === 2);

    s.kw.value = '红虫';
    check('设关键词后 filtering = true', s.filtering.value === true);
    check('设关键词后 filteredProducts 收敛为 1', s.filteredProducts.value.length === 1,
        JSON.stringify(ids(s.filteredProducts.value)));

    s.kw.value = '';
    s.catFilter.value = '腥香饵';
    check('清关键词、选分类 → 1 款', s.filteredProducts.value.length === 1);

    s.catFilter.value = '';
    s.fishFilter.value = '鲤鱼';
    check('只选鱼种 → 4 款', s.filteredProducts.value.length === 4);

    s.fishFilter.value = '';       // 先清掉鱼种，单独验"只输空格"
    s.kw.value = '  ';
    check('只输空格不算筛选（trim 后为空）', s.filtering.value === false);
    check('只输空格仍返回全部 6 款（不会莫名其妙筛空）', s.filteredProducts.value.length === 6);

    s.clearFilters();
    check('clearFilters 清空全部三个条件',
        s.kw.value === '' && s.catFilter.value === '' && s.fishFilter.value === '');
    check('clearFilters 后回到全部 6 款', s.filteredProducts.value.length === 6);
    check('clearFilters 后 filtering = false', s.filtering.value === false);
}

console.log('\n【E】模板里控件确实接上了（防"逻辑写了但控件没加进页面"）');
const rawHtml = fs.readFileSync(HTML, 'utf8');
const hasAll = (arr) => arr.every((s) => rawHtml.indexOf(s) !== -1);
check('搜索框绑定 v-model="kw"', rawHtml.indexOf('v-model="kw"') !== -1);
check('分类下拉绑定 v-model="catFilter"', rawHtml.indexOf('v-model="catFilter"') !== -1);
check('鱼种下拉绑定 v-model="fishFilter"', rawHtml.indexOf('v-model="fishFilter"') !== -1);
check('分类选项来自 categoryOptions', rawHtml.indexOf('v-for="c in categoryOptions"') !== -1);
check('鱼种选项来自 fishOptions', rawHtml.indexOf('v-for="f in fishOptions"') !== -1);
check('列表渲染的是 filteredProducts（不是 products）',
    rawHtml.indexOf('v-for="p in filteredProducts"') !== -1);
check('清空按钮绑 clearFilters', rawHtml.indexOf('@click="clearFilters"') !== -1);
check('主推角标改用 p.is_main（不再用 v-for 的 idx）',
    rawHtml.indexOf('v-if="p.is_main"') !== -1 && rawHtml.indexOf('v-if="idx === 0"') === -1);
check('有「没有符合条件的产品」空状态', rawHtml.indexOf('没有符合条件的产品') !== -1);
check('分类徽标已渲染（p.category）', rawHtml.indexOf('{{ p.category }}') !== -1);
check('筛选条独立成块（不在 v-if/v-else 链中间打断）',
    /products\.length > 0"[\s\S]{0,80}mb-8/.test(rawHtml));
check('三处候选绑定齐全（防漏一个）', hasAll(['v-model="kw"', 'v-model="catFilter"', 'v-model="fishFilter"']));

console.log('\n' + '='.repeat(62));
console.log(`  通过 ${pass} 项 / 失败 ${fails.length} 项`);
if (fails.length) fails.forEach((f) => console.log('    ✗ ' + f));
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
