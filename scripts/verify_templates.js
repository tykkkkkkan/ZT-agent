#!/usr/bin/env node
/**
 * Vue 模板编译校验（全站页面）
 *
 * 为什么需要它：`verify_product_search.js` 用桩替掉了 Vue，所以它**不会**编译模板。
 * 一旦页面模板里有未闭合标签、写错的表达式语法，线上表现是**整个页面白屏** ——
 * 这类问题必须在提交前拦住。本脚本用项目自带的 `vue.global.prod.js`
 * 把每个页面的 `<main>` 当模板**真编译一遍**。
 *
 * ⚠️ Vue 的编译器在属性值含 `&` 时（例如 `v-if="a && b"`）会走 DOM 实体解码，
 * 所以 Node 侧必须给 `document.createElement('div')` 补一个能解析
 * `<div foo="...">` 的 `innerHTML` 桩，否则会误报
 * `Cannot read properties of undefined (reading '0')`。
 *
 * 运行：node scripts/verify_templates.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const VUE = path.join(ROOT, 'frontend', 'assets', 'vendor', 'vue.global.prod.js');

// ── DOM 桩：只要能满足 Vue 编译器 + 实体解码即可 ────────────────
function decodeEntities(text) {
    const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
    return String(text).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, body) => {
        if (body[0] === '#') {
            const code = body[1] === 'x' || body[1] === 'X'
                ? parseInt(body.slice(2), 16)
                : parseInt(body.slice(1), 10);
            return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
        }
        return Object.prototype.hasOwnProperty.call(named, body) ? named[body] : whole;
    });
}

function makeElement() {
    const el = {
        style: {},
        _html: '',
        children: [],
        textContent: '',
        setAttribute(k, v) { el['__attr_' + k] = v; },
        getAttribute(k) { return el['__attr_' + k]; },
        appendChild() {},
        addEventListener() {},
    };
    Object.defineProperty(el, 'innerHTML', {
        get() { return el._html; },
        set(v) {
            el._html = String(v);
            // Vue 传进来的形态是 `<div foo="<escaped>">`
            const m = el._html.match(/^<div\s+foo="([\s\S]*)"\s*>$/);
            if (m) {
                const child = makeElement();
                child.setAttribute('foo', decodeEntities(m[1]));
                el.children = [child];
            }
            el.textContent = decodeEntities(el._html.replace(/<[^>]*>/g, ''));
        },
    });
    return el;
}

function loadVue() {
    const sandbox = { console, setTimeout, clearTimeout };
    sandbox.window = sandbox;
    sandbox.self = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.navigator = { userAgent: 'node' };
    sandbox.document = {
        createElement: () => makeElement(),
        createTextNode: () => ({}),
        querySelector: () => null,
        addEventListener() {},
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(VUE, 'utf8'), sandbox, { filename: 'vue.global.prod.js' });
    return sandbox.Vue;
}

const Vue = loadVue();
let pass = 0;
const fails = [];

console.log('='.repeat(62));
console.log('  Vue 模板编译校验（全站页面）');
console.log('='.repeat(62));
console.log(`  编译器：Vue ${Vue.version}（项目自带 vue.global.prod.js）\n`);

const pages = fs.readdirSync(path.join(ROOT, 'frontend'))
    .filter((f) => f.endsWith('.html'))
    .sort();

/**
 * 从 `html[startIdx]`（指向某个开标签）起，按**标签深度配平**取出该元素的内部 HTML。
 * 不能用非贪婪 `([\s\S]*?)</div>`——它会在第一个 `</div>` 就停，把模板截断，
 * 于是"只校验了前半段"却报通过（本项目 order.html 就踩过：只截到 174 字符）。
 */
function innerHtmlOf(html, tagStart, tagName) {
    const token = new RegExp(`<${tagName}\\b[^>]*?/?>|</${tagName}\\s*>`, 'gi');
    token.lastIndex = tagStart;
    let depth = 0;
    let m;
    let contentStart = -1;
    while ((m = token.exec(html))) {
        const isClose = /^<\//.test(m[0]);
        const isSelf = /\/>$/.test(m[0]);
        if (isClose) {
            depth--;
            if (depth === 0) {
                return { inner: html.slice(contentStart, m.index), end: m.index + m[0].length };
            }
        } else if (!isSelf) {
            depth++;
            if (depth === 1) contentStart = m.index + m[0].length;
        }
    }
    return null;
}

/** 按 `.mount('#id')` 找挂载容器；找不到就退回 `<main>`。 */
function extractTemplate(html) {
    const mount = html.match(/\.mount\(\s*['"]#([\w-]+)['"]\s*\)/);
    if (mount) {
        const id = mount[1];
        const openTag = new RegExp(`<([a-zA-Z][\\w-]*)\\b[^>]*\\bid="${id}"[^>]*>`, 'i');
        const m = html.match(openTag);
        if (m) {
            const res = innerHtmlOf(html, m.index, m[1]);
            if (res) return { tpl: res.inner, how: `#${id}` };
        }
    }
    const mainOpen = html.match(/<main\b[^>]*>/i);
    if (mainOpen) {
        const res = innerHtmlOf(html, mainOpen.index, 'main');
        if (res) return { tpl: res.inner, how: '<main>' };
    }
    return null;
}

for (const name of pages) {
    const file = path.join(ROOT, 'frontend', name);
    const html = fs.readFileSync(file, 'utf8');
    const found = extractTemplate(html);
    if (!found) {
        console.log(`  – ${name}（非 Vue 挂载页，跳过）`);
        continue;
    }
    try {
        Vue.compile(found.tpl);
        pass++;
        console.log(`  ✓ ${name}  模板编译通过（挂载点 ${found.how}，${found.tpl.length} 字符）`);
    } catch (e) {
        fails.push(name);
        console.log(`  ✗ ${name}  模板编译失败：${e.message}`);
    }
}

console.log('\n' + '='.repeat(62));
console.log(`  通过 ${pass} 页 / 失败 ${fails.length} 页`);
if (fails.length) fails.forEach((f) => console.log(`    ✗ ${f}`));
console.log('='.repeat(62));
process.exit(fails.length ? 1 : 0);
