/**
 * scripts/verify_auth_flow.js — 前端登录态与守卫的回归验证（Node vm 沙箱，无需浏览器）
 *
 * 运行：node scripts/verify_auth_flow.js
 *
 * 覆盖的产品约束：
 *   ① 关闭浏览器后再打开首页 ≠ 已登录（token 默认不跨会话）
 *   ② 退出登录必须跳登录页，且两处存储与会话 ID 全部清空
 *   ③ 需要登录的操作（下单/查订单/留言/定制/AI 对话）未登录时被拦截
 *   ④ 登录态失效时清除本地态，不「假装已登录」
 *   ⑤ 网络异常不误踢用户；公开页静默降级不强制跳转
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const SRC = path.join(__dirname, '..', 'frontend', 'auth.js');
const code = fs.readFileSync(SRC, 'utf8');

let pass = 0, fail = 0;
function check(name, cond, extra) {
    if (cond) { pass++; console.log(`  \x1b[32mPASS\x1b[0m ${name}`); }
    else { fail++; console.log(`  \x1b[31mFAIL\x1b[0m ${name}${extra ? '  → ' + extra : ''}`); }
}

function makeStorage() {
    const m = new Map();
    return {
        getItem: k => (m.has(k) ? m.get(k) : null),
        setItem: (k, v) => m.set(k, String(v)),
        removeItem: k => m.delete(k),
        _dump: () => Object.fromEntries(m),
        _raw: m,
    };
}

/** 构造一个隔离的「浏览器」环境并加载 auth.js */
function boot(opts = {}) {
    const ss = makeStorage();
    const ls = makeStorage();
    if (opts.seedSession) Object.entries(opts.seedSession).forEach(([k, v]) => ss.setItem(k, v));
    if (opts.seedLocal) Object.entries(opts.seedLocal).forEach(([k, v]) => ls.setItem(k, v));

    const navigations = [];
    const location = { pathname: opts.pathname || '/index.html', search: opts.search || '' };
    Object.defineProperty(location, 'href', {
        get: () => navigations[navigations.length - 1] || '',
        set: v => navigations.push(v),
    });

    const calls = [];
    const fetchImpl = opts.fetchImpl || (async () => ({ ok: true, status: 200, json: async () => ({}) }));
    const fetchMock = async (url, init) => {
        calls.push({ url, init });
        return fetchImpl(url, init);
    };

    const window = { sessionStorage: ss, localStorage: ls, addEventListener() {} };
    const sandbox = {
        window, sessionStorage: ss, localStorage: ls,
        location, fetch: fetchMock,
        URLSearchParams, JSON, String, Object, Array, Promise,
        encodeURIComponent, decodeURIComponent,
        console,
    };
    sandbox.window = window;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox, { filename: 'auth.js' });
    return { AUTH: sandbox.window.AUTH, ss, ls, navigations, calls, location };
}

const flush = () => new Promise(r => setTimeout(r, 0));

(async () => {
    console.log('\n\u001b[1m【1】一次性兼容迁移：旧版本遗留在 localStorage 的 token 必须清掉\u001b[0m');
    {
        const env = boot({ seedLocal: { zy_access: 'old-access', zy_refresh: 'old-refresh', zy_user: '{"username":"tom"}' } });
        check('旧 localStorage token 被清除', env.ls.getItem('zy_access') === null && env.ls.getItem('zy_refresh') === null);
        check('旧用户信息被清除', env.ls.getItem('zy_user') === null);
        check('isLoggedIn() = false（打开首页不再自动登录）', env.AUTH.isLoggedIn() === false);
    }

    console.log('\n\u001b[1m【2】不勾选「记住我」→ token 存 sessionStorage\u001b[0m');
    {
        const env = boot();
        env.AUTH.setTokens('A1', 'R1', false);
        check('access 落在 sessionStorage', env.ss.getItem('zy_access') === 'A1');
        check('localStorage 未落任何 token', env.ls.getItem('zy_access') === null && env.ls.getItem('zy_refresh') === null);
        check('isLoggedIn() = true（当前会话内有效）', env.AUTH.isLoggedIn() === true);

        // 模拟：关闭浏览器 → sessionStorage 清空、localStorage 保留
        const reopen = boot({ seedLocal: env.ls._dump(), seedSession: {} });
        check('★ 关浏览器重开首页 → 未登录（本次改造核心）', reopen.AUTH.isLoggedIn() === false);
    }

    console.log('\n\u001b[1m【3】勾选「记住我」→ token 落 localStorage（跨会话保持）\u001b[0m');
    {
        const env = boot();
        env.AUTH.setTokens('A2', 'R2', true);
        check('access 落在 localStorage', env.ls.getItem('zy_access') === 'A2');
        check('sessionStorage 未残留', env.ss.getItem('zy_access') === null);
        check('isRemembered() 标记已写入', env.AUTH.isRemembered() === true);
        const reopen = boot({ seedLocal: env.ls._dump(), seedSession: {} });
        check('关浏览器重开 → 仍登录（用户主动勾选的结果）', reopen.AUTH.isLoggedIn() === true);
    }

    console.log('\n\u001b[1m【4】退出登录：清空两处存储 + 清会话 ID + 跳登录页\u001b[0m');
    {
        const env = boot({ seedLocal: { zyt_session_id: 'chat-sid' } });
        env.AUTH.setTokens('A3', 'R3', false);
        env.AUTH.setUser({ username: 'tom' });
        env.AUTH.logout();
        await flush();
        check('sessionStorage 内 token 已清', env.ss.getItem('zy_access') === null && env.ss.getItem('zy_refresh') === null);
        check('localStorage 内 token 已清', env.ls.getItem('zy_access') === null && env.ls.getItem('zy_refresh') === null);
        check('用户信息已清', env.ss.getItem('zy_user') === null && env.ls.getItem('zy_user') === null);
        check('AI 会话 ID 已清（防换账号看到上一人聊天记录）', env.ls.getItem('zyt_session_id') === null);
        check('★ 跳转到登录页而非首页', /^\/login\.html\?logout=1$/.test(env.navigations.at(-1)), env.navigations.at(-1));
        check('isLoggedIn() = false', env.AUTH.isLoggedIn() === false);
    }

    console.log('\n\u001b[1m【5】路由守卫：未登录访问 order.html 被拦截\u001b[0m');
    {
        const env = boot({ pathname: '/order.html', search: '' });
        const ok = env.AUTH.requireAuth('登录后即可在线下单');
        const url = env.navigations.at(-1);
        check('requireAuth() 返回 false', ok === false);
        check('跳转携带 next=order.html', url.includes('next=order.html'), url);
        check('跳转携带 reason 提示', url.includes(encodeURIComponent('登录后即可在线下单')), url);

        check('ensureLogin() 同样拦截', (await env.AUTH.ensureLogin('x')) === false);
    }

    console.log('\n\u001b[1m【6】操作守卫：持有效 token 时放行\u001b[0m');
    {
        const env = boot({
            fetchImpl: async url => url.includes('/api/auth/me/')
                ? { ok: true, status: 200, json: async () => ({ id: 1, username: 'tom' }) }
                : { ok: true, status: 200, json: async () => ({}) },
        });
        env.AUTH.setTokens('A4', 'R4', false);
        const ok = await env.AUTH.ensureLogin('需要登录');
        check('ensureLogin() = true', ok === true);
        check('未发生跳转', env.navigations.length === 0);
        check('调用了 /api/auth/me/ 做服务端校验', env.calls.some(c => c.url.includes('/api/auth/me/')));
    }

    console.log('\n\u001b[1m【7】token 已失效：清除本地态并跳登录\u001b[0m');
    {
        const env = boot({
            fetchImpl: async () => ({ ok: false, status: 401, json: async () => ({ detail: 'expired' }) }),
        });
        env.AUTH.setTokens('A5', 'R5', false);
        const ok = await env.AUTH.ensureLogin('需要登录');
        check('ensureLogin() = false', ok === false);
        check('本地 token 已清（不再假装已登录）', env.AUTH.isLoggedIn() === false);
        check('跳转登录页并提示失效', /reason=/.test(env.navigations.at(-1) || ''), env.navigations.at(-1));
    }

    console.log('\n\u001b[1m【8】access 过期但 refresh 可用：静默续期后放行\u001b[0m');
    {
        let meHits = 0;
        const env = boot({
            fetchImpl: async url => {
                if (url.includes('/api/auth/refresh/')) {
                    return { ok: true, status: 200, json: async () => ({ access: 'NEW-A', refresh: 'NEW-R' }) };
                }
                if (url.includes('/api/auth/me/')) {
                    meHits++;
                    // 第一次用旧 token → 401；续期后第二次 → 200
                    if (meHits === 1) return { ok: false, status: 401, json: async () => ({}) };
                    return { ok: true, status: 200, json: async () => ({ id: 1, username: 'tom' }) };
                }
                return { ok: true, status: 200, json: async () => ({}) };
            },
        });
        env.AUTH.setTokens('OLD-A', 'OLD-R', false);
        const ok = await env.AUTH.ensureLogin('需要登录');
        check('续期后放行（未误踢用户）', ok === true);
        check('access 已更新为新 token', env.AUTH.getAccess() === 'NEW-A', env.AUTH.getAccess());
        check('未发生跳转', env.navigations.length === 0);
    }

    console.log('\n\u001b[1m【9】init({requireAuth:true})：未登录直接跳登录页\u001b[0m');
    {
        const env = boot({ pathname: '/order_query.html' });
        env.location.pathname = '/order_query.html';
        const ok = await env.AUTH.init({ requireAuth: true, reason: '订单查询需登录' });
        check('init() 返回 false', ok === false);
        check('跳转登录页且带 next', /\/login\.html\?next=order_query\.html/.test(env.navigations.at(-1) || ''), env.navigations.at(-1));
    }

    console.log('\n\u001b[1m【10】网络异常时不误清登录态（离线保护）\u001b[0m');
    {
        const env = boot({ fetchImpl: async () => { throw new Error('network down'); } });
        env.AUTH.setTokens('A6', 'R6', false);
        const ok = await env.AUTH.verify();
        check('verify() 保留本地登录态', ok === true);
        check('token 未被清除', env.AUTH.getAccess() === 'A6');
    }

    console.log('\n\u001b[1m【11】公开页静默降级：silentAuthFail 不跳登录页\u001b[0m');
    {
        const env = boot({ fetchImpl: async () => ({ ok: false, status: 401, json: async () => ({}) }) });
        env.AUTH.setTokens('A7', 'R7', false);
        const resp = await env.AUTH.authFetch('/api/agent/history/sid/', {}, { silentAuthFail: true });
        check('返回 401 供调用方降级', resp.status === 401);
        check('★ 未跳转登录页（历史访客不被打扰）', env.navigations.length === 0, env.navigations.at(-1));
        check('失效 token 已清', env.AUTH.isLoggedIn() === false);
    }

    console.log('\n' + '='.repeat(60));
    console.log(`结果：\x1b[32m${pass} 通过\x1b[0m / ${fail ? '\x1b[31m' + fail + ' 失败\x1b[0m' : '0 失败'}`);
    console.log('='.repeat(60));
    process.exit(fail ? 1 : 0);
})();
