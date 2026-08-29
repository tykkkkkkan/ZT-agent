/**
 * frontend/auth.js — 前端认证模块（多页共享）
 *
 * 职责：
 *  1. Token 管理（localStorage 存 access + refresh + 用户信息）
 *  2. 请求拦截：自动加 Authorization: Bearer <access>
 *  3. 401 自动刷新：access 过期时用 refresh 换新，失败则清 token
 *  4. 路由守卫：requireAuth()（未登录跳 /login.html）、redirectIfLoggedIn()（已登录跳首页）
 *  5. logout()：调后端退出 + 清 token
 *
 * 用法：页面 <script src="auth.js"></script> 后，通过全局 AUTH 对象调用。
 */
window.AUTH = (function () {
    var ACCESS_KEY = 'zy_access';
    var REFRESH_KEY = 'zy_refresh';
    var USER_KEY = 'zy_user';

    function getAccess() { return localStorage.getItem(ACCESS_KEY); }
    function getRefresh() { return localStorage.getItem(REFRESH_KEY); }
    function getUser() {
        try { return JSON.parse(localStorage.getItem(USER_KEY)); } catch (e) { return null; }
    }
    function setTokens(access, refresh) {
        if (access) localStorage.setItem(ACCESS_KEY, access);
        if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
    }
    function setUser(u) { localStorage.setItem(USER_KEY, JSON.stringify(u)); }
    function clear() {
        localStorage.removeItem(ACCESS_KEY);
        localStorage.removeItem(REFRESH_KEY);
        localStorage.removeItem(USER_KEY);
    }
    function isLoggedIn() { return !!getAccess(); }

    // 带 token 的 fetch；401 时自动尝试 refresh 后重试一次
    async function authFetch(url, options) {
        options = options || {};
        options.headers = Object.assign({}, options.headers);
        var access = getAccess();
        if (access) options.headers['Authorization'] = 'Bearer ' + access;

        var resp = await fetch(url, options);

        // 401 且不是认证接口本身 → 尝试 refresh
        if (resp.status === 401 && !/\/api\/auth\/(login|register)/.test(url)) {
            var ok = await tryRefresh();
            if (ok) {
                options.headers['Authorization'] = 'Bearer ' + getAccess();
                resp = await fetch(url, options);
            }
        }
        return resp;
    }

    async function tryRefresh() {
        var refresh = getRefresh();
        if (!refresh) return false;
        try {
            var r = await fetch('/api/auth/refresh/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh: refresh })
            });
            if (!r.ok) { clear(); return false; }
            var d = await r.json();
            setTokens(d.access, d.refresh);
            return true;
        } catch (e) { return false; }
    }

    // 路由守卫：未登录跳登录页，带 next 回跳参数
    function requireAuth() {
        if (!isLoggedIn()) {
            var next = encodeURIComponent(location.pathname.replace(/^\//, '') + location.search);
            location.href = '/login.html?next=' + next;
            return false;
        }
        return true;
    }

    // 已登录访问登录/注册页 → 跳首页（或 next）
    function redirectIfLoggedIn() {
        if (isLoggedIn()) {
            var next = new URLSearchParams(location.search).get('next');
            location.href = next ? decodeURIComponent(next) : '/index.html';
            return true;
        }
        return false;
    }

    // 同步刷新导航栏登录状态（登录/注册后、页面加载时调用）
    function syncNav() {
        var user = getUser();
        document.querySelectorAll('[data-auth-nav="guest"]').forEach(function (el) {
            el.classList.toggle('hidden', isLoggedIn());
        });
        document.querySelectorAll('[data-auth-nav="user"]').forEach(function (el) {
            el.classList.toggle('hidden', !isLoggedIn());
        });
        document.querySelectorAll('[data-auth-username]').forEach(function (el) {
            el.textContent = user ? user.username : '';
        });
    }

    async function logout() {
        try {
            await fetch('/api/auth/logout/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + getAccess()
                },
                body: JSON.stringify({ refresh: getRefresh() })
            });
        } catch (e) { /* 网络异常也照常本地退出 */ }
        clear();
        location.href = '/index.html';
    }

    return {
        getAccess: getAccess,
        getRefresh: getRefresh,
        getUser: getUser,
        setTokens: setTokens,
        setUser: setUser,
        clear: clear,
        isLoggedIn: isLoggedIn,
        authFetch: authFetch,
        tryRefresh: tryRefresh,
        requireAuth: requireAuth,
        redirectIfLoggedIn: redirectIfLoggedIn,
        syncNav: syncNav,
        logout: logout
    };
})();
