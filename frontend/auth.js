/**
 * frontend/auth.js — 前端认证模块（多页共享）
 *
 * ═══════════════════════════════════════════════════════════════
 * 登录态策略（v2，2026-09 修订）
 * ═══════════════════════════════════════════════════════════════
 *  1. Token 默认存 sessionStorage（会话级）：
 *     - 关闭浏览器 / 关闭标签页 → 登录态自动失效，不再「一打开首页就是已登录」；
 *     - 同一标签页内刷新、跳转仍然保持登录。
 *  2. 仅当用户在登录页勾选「记住我」时，token 才落到 localStorage（跨会话保持）。
 *  3. 页面加载统一走 AUTH.init()：先向后端 /api/auth/me/ 校验 token 真实有效，
 *     校验不过就地清除并渲染为未登录态 —— 杜绝「本地残留 token 假装已登录」。
 *  4. 退出登录 = 后端 refresh 进黑名单 + 清空两处存储 + 跳登录页，
 *     确保「每次退出后必须重新登录」。
 *  5. 涉及隐私/业务数据的操作走 AUTH.ensureLogin() 或 AUTH.requireAuth()，
 *     未登录一律跳登录页并带上 next 回跳参数。
 *
 * 用法：页面 <script src="auth.js"></script> 后，通过全局 AUTH 对象调用。
 */
window.AUTH = (function () {
    var ACCESS_KEY = 'zy_access';
    var REFRESH_KEY = 'zy_refresh';
    var USER_KEY = 'zy_user';
    var REMEMBER_KEY = 'zy_remember';   // '1' = 勾选过「记住我」，token 落 localStorage

    var ss = window.sessionStorage;
    var ls = window.localStorage;

    // ── 一次性兼容迁移 ──────────────────────────────────────────
    // 旧版本把 token 无条件写进 localStorage，导致「关掉浏览器再开首页仍是已登录」。
    // 这里把「没有记住我标记」的历史 token 彻底清掉，强制走一次正常登录。
    (function migrateLegacyStorage() {
        try {
            var legacy = ls.getItem(ACCESS_KEY) || ls.getItem(REFRESH_KEY);
            if (legacy && ls.getItem(REMEMBER_KEY) !== '1') {
                ls.removeItem(ACCESS_KEY);
                ls.removeItem(REFRESH_KEY);
                ls.removeItem(USER_KEY);
            }
        } catch (e) { /* 隐私模式下 storage 不可用，忽略 */ }
    })();

    // ── 底层读写：会话级优先，其次「记住我」的持久层 ──────────────
    function _read(key) {
        try {
            var v = ss.getItem(key);
            if (v !== null && v !== undefined) return v;
            return ls.getItem(key);
        } catch (e) { return null; }
    }
    function _writeAll(key, val) {
        try {
            if (isRemembered()) {
                ls.setItem(key, val);
                ss.removeItem(key);
            } else {
                ss.setItem(key, val);
                ls.removeItem(key);
            }
        } catch (e) { /* ignore */ }
    }
    function _drop(key) {
        try { ss.removeItem(key); } catch (e) {}
        try { ls.removeItem(key); } catch (e) {}
    }

    function isRemembered() {
        try { return ls.getItem(REMEMBER_KEY) === '1'; } catch (e) { return false; }
    }

    function getAccess() { return _read(ACCESS_KEY); }
    function getRefresh() { return _read(REFRESH_KEY); }
    function getUser() {
        try { return JSON.parse(_read(USER_KEY)); } catch (e) { return null; }
    }

    /**
     * 写入 token。
     * @param {string} access  access token
     * @param {string} refresh refresh token
     * @param {boolean} [remember] 是否跨会话保持（登录页「记住我」）
     */
    function setTokens(access, refresh, remember) {
        try {
            if (remember) ls.setItem(REMEMBER_KEY, '1');
            else ls.removeItem(REMEMBER_KEY);
        } catch (e) {}
        if (access) _writeAll(ACCESS_KEY, access);
        if (refresh) _writeAll(REFRESH_KEY, refresh);
    }

    function setUser(u) {
        if (!u) { _drop(USER_KEY); return; }
        _writeAll(USER_KEY, JSON.stringify(u));
    }

    /**
     * 清空登录态。
     * 除 token / 用户信息外，一并清掉「AI 客服会话 ID」——
     * 否则同一标签页换账号登录后会看到上一位用户的聊天记录（隐私泄漏）。
     */
    function clear() {
        _drop(ACCESS_KEY);
        _drop(REFRESH_KEY);
        _drop(USER_KEY);
        _drop('zyt_session_id');
        try { ls.removeItem(REMEMBER_KEY); } catch (e) {}
    }

    /**
     * 本地是否具备登录凭证。
     * 注意：这只代表「本地有 token」，真实有效性以 verify() / 后端 401 为准。
     */
    function isLoggedIn() { return !!getAccess(); }

    // ── 跳转登录页 ─────────────────────────────────────────────
    function redirectToLogin(reason) {
        var next = encodeURIComponent(location.pathname.replace(/^\//, '') + location.search);
        var url = '/login.html?next=' + next;
        if (reason) url += '&reason=' + encodeURIComponent(reason);
        location.href = url;
    }

    // ── 刷新 token ────────────────────────────────────────────
    async function tryRefresh() {
        var refresh = getRefresh();
        if (!refresh) { clear(); return false; }
        try {
            var r = await fetch('/api/auth/refresh/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh: refresh })
            });
            if (!r.ok) { clear(); return false; }
            var d = await r.json();
            // 保持原存储位置（保持勾选的「记住我」语义）
            var remember = isRemembered();
            setTokens(d.access, d.refresh, remember);
            return true;
        } catch (e) {
            // 网络异常不算登录失效，避免断网时被踢出
            return false;
        }
    }

    // ── 带 token 的 fetch：401 自动刷新后重试一次 ────────────────
    /**
     * @param {string} url
     * @param {Object} [options] 同 fetch options
     * @param {Object} [cfg]
     * @param {boolean} [cfg.silentAuthFail] true = 刷新仍 401 时只清 token、不跳登录页
     *        （公开页面的历史/展示类请求用，由调用方自行降级为未登录态，
     *          避免「登录已过期」的历史访客一进首页就被弹去登录页）
     */
    async function authFetch(url, options, cfg) {
        cfg = cfg || {};
        options = options || {};
        options.headers = Object.assign({}, options.headers);
        var access = getAccess();
        if (access) options.headers['Authorization'] = 'Bearer ' + access;

        var resp = await fetch(url, options);

        if (resp.status === 401 && !/\/api\/auth\/(login|register|refresh)/.test(url)) {
            var ok = await tryRefresh();
            if (ok) {
                options.headers['Authorization'] = 'Bearer ' + getAccess();
                resp = await fetch(url, options);
            }
            if (resp.status === 401) {
                clear();
                if (!cfg.silentAuthFail) {
                    redirectToLogin('登录状态已失效，请重新登录');
                }
            }
        }
        return resp;
    }

    /**
     * 向服务端校验当前登录态是否真实有效。
     * 无效（后端 401 且刷新失败）→ 就地清除本地状态并返回 false。
     * 网络异常 → 保守返回本地判断结果，不误踢用户。
     */
    async function verify() {
        if (!getAccess()) { clear(); return false; }
        try {
            var resp = await fetch('/api/auth/me/', {
                headers: { 'Authorization': 'Bearer ' + getAccess() }
            });
            if (resp.ok) return true;
            if (resp.status === 401) {
                if (await tryRefresh()) {
                    var r2 = await fetch('/api/auth/me/', {
                        headers: { 'Authorization': 'Bearer ' + getAccess() }
                    });
                    if (r2.ok) return true;
                }
                clear();
                return false;
            }
            // 5xx / 403 等：服务端问题，保留登录态
            return true;
        } catch (e) {
            return isLoggedIn();
        }
    }

    /**
     * 页面统一初始化（替代旧的直接 syncNav）。
     *
     * @param {Object} [opts]
     * @param {boolean} [opts.requireAuth] 本页是否必须登录（true = 未登录直接跳登录页）
     * @param {string}  [opts.reason]      跳登录页时展示的提示文案
     * @returns {Promise<boolean>} 是否处于有效登录态
     */
    async function init(opts) {
        opts = opts || {};

        if (opts.requireAuth && !isLoggedIn()) {
            redirectToLogin(opts.reason || '登录后即可访问该页面');
            return false;
        }

        var ok = await verify();
        syncNav();

        if (opts.requireAuth && !ok) {
            redirectToLogin(opts.reason || '登录状态已失效，请重新登录');
            return false;
        }
        return ok;
    }

    /** 同步路由守卫：本地无 token 即跳登录页（用于页面顶部抢先拦截，避免闪现内容） */
    function requireAuth(reason) {
        if (!isLoggedIn()) {
            redirectToLogin(reason || '登录后即可访问该页面');
            return false;
        }
        return true;
    }

    /**
     * 异步操作守卫：用于「点击按钮 / 提交表单」前校验。
     * 未登录或登录态已失效 → 跳登录页并返回 false，调用方直接 return。
     *
     * @param {string} [reason] 跳登录页时展示的提示文案
     */
    async function ensureLogin(reason) {
        if (!isLoggedIn()) {
            redirectToLogin(reason || '登录后即可继续操作');
            return false;
        }
        var ok = await verify();
        if (!ok) {
            redirectToLogin(reason || '登录状态已失效，请重新登录');
            return false;
        }
        return true;
    }

    /** 已登录访问登录/注册页 → 跳首页（或 next） */
    function redirectIfLoggedIn() {
        if (isLoggedIn()) {
            var next = new URLSearchParams(location.search).get('next');
            location.href = next ? decodeURIComponent(next) : '/index.html';
            return true;
        }
        return false;
    }

    /** 同步刷新导航栏登录状态 */
    function syncNav() {
        var user = getUser();
        var loggedIn = isLoggedIn();
        document.querySelectorAll('[data-auth-nav="guest"]').forEach(function (el) {
            el.classList.toggle('hidden', loggedIn);
        });
        document.querySelectorAll('[data-auth-nav="user"]').forEach(function (el) {
            if (loggedIn) {
                el.classList.remove('hidden');
                // 桌面端容器带 items-center / gap-* 类，需以 flex 呈现（hidden 只做显隐，不声明布局）
                if (/(^|\s)(items-center|gap-\S+)/.test(el.className)) el.style.display = 'flex';
            } else {
                el.classList.add('hidden');
                el.style.display = '';
            }
        });
        document.querySelectorAll('[data-auth-username]').forEach(function (el) {
            el.textContent = user && loggedIn ? (user.username || user.email || '') : '';
        });
    }

    /**
     * 退出登录：后端拉黑 refresh + 清空本地 + 回登录页。
     * 刻意跳转登录页而非首页 —— 保证「退出后必须重新登录」。
     */
    async function logout() {
        var access = getAccess();
        var refresh = getRefresh();
        try {
            await fetch('/api/auth/logout/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + access
                },
                body: JSON.stringify({ refresh: refresh })
            });
        } catch (e) { /* 网络异常也照常本地退出 */ }
        clear();
        location.href = '/login.html?logout=1';
    }

    /** 给 axios 等自建请求补上 Authorization 头 */
    function authHeaders(headers) {
        headers = headers || {};
        var access = getAccess();
        if (access) headers['Authorization'] = 'Bearer ' + access;
        return headers;
    }

    // ── 从浏览器「后退」缓存（bfcache）恢复时，重新校验登录态 ──────
    // 场景：退出登录 → 点后退 → 页面若从缓存恢复会短暂显示已登录 UI。
    window.addEventListener('pageshow', function (e) {
        if (!e.persisted) return;
        if (!isLoggedIn()) { syncNav(); return; }
        verify().then(syncNav);
    });

    return {
        getAccess: getAccess,
        getRefresh: getRefresh,
        getUser: getUser,
        isRemembered: isRemembered,
        setTokens: setTokens,
        setUser: setUser,
        clear: clear,
        isLoggedIn: isLoggedIn,
        authFetch: authFetch,
        authHeaders: authHeaders,
        tryRefresh: tryRefresh,
        verify: verify,
        init: init,
        requireAuth: requireAuth,
        ensureLogin: ensureLogin,
        redirectIfLoggedIn: redirectIfLoggedIn,
        redirectToLogin: redirectToLogin,
        syncNav: syncNav,
        logout: logout
    };
})();
