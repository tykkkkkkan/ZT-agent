/**
 * 后台列表「行内快速操作」通用脚本
 *
 * 支持三类元素（由 admin.py 的列方法输出，无需各页写 JS）：
 *   1. <a data-zy-back="1" href="...?ids=..&op=..">
 *      订单快速操作链接：自动把当前列表的筛选条件（?ship=pending 等）拼成
 *      back 参数，操作完能回到"筛选后的列表"而不是全量列表。
 *   2. <button data-zy-post="1" data-zy-url=".." data-zy-id="..">
 *      一键翻转类（留言已读、产品上下架）：POST JSON {id}，成功后整页刷新。
 *   3. <button data-zy-stock="1" data-zy-url=".." data-zy-id=".." data-zy-now="..">
 *      库存「± 调整」：弹窗输增量（正数入库 / 负数出库），提交后刷新。
 *   4. <button data-zy-coord="1" data-zy-url=".." data-zy-id=".." data-zy-action="resume|pause">
 *      接单状态「恢复/暂停接单」：暂停时弹窗收客户提示语；恢复时直接提交。
 *      用途：跨 Agent 覆盖导致"后台有货但前台买不了"时，运营可在此一键恢复。
 */
(function () {
    'use strict';

    function getCSRF() {
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        if (m) return decodeURIComponent(m[1]);
        var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }

    function postJSON(url, payload, done, fail) {
        fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRF() },
            body: JSON.stringify(payload || {}),
        }).then(function (r) {
            return r.json().catch(function () { return { success: false, message: '响应解析失败' }; });
        }).then(function (d) {
            if (d && d.success) { done(d); } else { fail((d && d.message) || '操作失败'); }
        }).catch(function () { fail('网络异常，请重试'); });
    }

    /* ── 1. 订单快速操作链接：带上当前筛选条件 ─────────────── */
    function bindBackLinks() {
        var links = document.querySelectorAll('a[data-zy-back="1"]');
        if (!links.length) return;
        var search = window.location.search.replace(/^\?/, '');
        Array.prototype.forEach.call(links, function (a) {
            if (!search) return;
            a.href += (a.href.indexOf('?') === -1 ? '?' : '&') + 'back=' + encodeURIComponent(search);
        });
    }

    /* ── 2. 一键翻转（已读 / 上下架） ───────────────────────── */
    function bindPostButtons() {
        var btns = document.querySelectorAll('button[data-zy-post="1"]');
        Array.prototype.forEach.call(btns, function (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                var old = btn.textContent;
                btn.disabled = true;
                btn.textContent = '…';
                postJSON(btn.getAttribute('data-zy-url'), { id: btn.getAttribute('data-zy-id') },
                    function () { window.location.reload(); },
                    function (msg) {
                        alert(msg);
                        btn.disabled = false;
                        btn.textContent = old;
                    });
            });
        });
    }

    /* ── 3. 库存 ± 调整弹窗 ─────────────────────────────────── */
    function bindStockButtons() {
        var btns = document.querySelectorAll('button[data-zy-stock="1"]');
        if (!btns.length) return;

        var style = document.createElement('style');
        style.textContent = [
            '#zy-smodal{position:fixed;inset:0;z-index:10000;display:none;align-items:center;',
            'justify-content:center;background:rgba(15,23,20,.45)}',
            '#zy-smodal.open{display:flex}',
            '#zy-smodal .zy-panel{width:92%;max-width:400px;background:#fff;border-radius:16px;',
            'padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.25)}',
            '#zy-smodal h3{margin:0 0 4px;font-size:17px}',
            '#zy-smodal .zy-sub{font-size:12.5px;color:#738079;margin-bottom:14px}',
            '#zy-smodal .zy-chips{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 12px}',
            '#zy-smodal .zy-chips button{border:1px solid #D8D2C0;background:#fff;border-radius:999px;',
            'padding:4px 12px;font-size:12.5px;cursor:pointer;color:#3A473F}',
            '#zy-smodal .zy-chips button:hover{background:#F3EFE4}',
            '#zy-smodal label{display:block;font-size:12px;color:#5B6670;margin:10px 0 4px}',
            '#zy-smodal input{width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid #D7DCE0;',
            'border-radius:8px;font-size:15px}',
            '#zy-smodal input:focus{outline:none;border-color:#1F6B54;box-shadow:0 0 0 3px rgba(31,107,84,.12)}',
            '#zy-smodal .zy-preview{font-size:13px;color:#185647;background:#DCEDE6;border-radius:8px;',
            'padding:8px 12px;margin-top:12px}',
            '#zy-smodal .zy-err{color:#C73320;font-size:13px;margin-top:10px;display:none}',
            '#zy-smodal .zy-btns{display:flex;gap:12px;justify-content:flex-end;margin-top:18px}',
            '#zy-smodal .zy-btns button{padding:9px 18px;border-radius:8px;font-size:14px;cursor:pointer;border:none}',
            '#zy-smodal .zy-cancel{background:#F0F2F4;color:#333}',
            '#zy-smodal .zy-submit{background:#1F6B54;color:#fff;font-weight:600}',
        ].join('');
        document.head.appendChild(style);

        var modal = document.createElement('div');
        modal.id = 'zy-smodal';
        modal.innerHTML =
            '<div class="zy-panel">' +
            '<h3>调整库存</h3>' +
            '<div class="zy-sub" id="zy-sname"></div>' +
            '<label>增减数量（正数入库 / 负数出库）</label>' +
            '<input id="zy-delta" type="number" step="1" placeholder="如 100 或 -20">' +
            '<div class="zy-chips">' +
            '<button type="button" data-d="10">+10</button>' +
            '<button type="button" data-d="50">+50</button>' +
            '<button type="button" data-d="100">+100</button>' +
            '<button type="button" data-d="-10">-10</button>' +
            '<button type="button" data-d="-50">-50</button>' +
            '</div>' +
            '<label>备注（选填）</label>' +
            '<input id="zy-note" type="text" placeholder="如：到货入库 100 包 / 盘点盘亏">' +
            '<div class="zy-preview" id="zy-preview"></div>' +
            '<div class="zy-err" id="zy-serr"></div>' +
            '<div class="zy-btns">' +
            '<button type="button" class="zy-cancel" id="zy-scancel">取消</button>' +
            '<button type="button" class="zy-submit" id="zy-ssubmit">确认调整</button>' +
            '</div></div>';
        document.body.appendChild(modal);

        var cur = { url: '', id: '', now: 0 };
        var deltaEl = modal.querySelector('#zy-delta');
        var noteEl = modal.querySelector('#zy-note');
        var errEl = modal.querySelector('#zy-serr');

        function preview() {
            var d = parseInt(deltaEl.value, 10);
            var box = modal.querySelector('#zy-preview');
            if (isNaN(d) || d === 0) { box.textContent = '当前库存：' + cur.now + ' 包'; return; }
            var after = cur.now + d;
            box.textContent = '当前 ' + cur.now + ' 包 → 调整后 ' + after + ' 包'
                + (after < 0 ? '（库存不能为负，会被拒绝）' : '');
        }

        function close() { modal.classList.remove('open'); }

        Array.prototype.forEach.call(btns, function (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                cur.url = btn.getAttribute('data-zy-url');
                cur.id = btn.getAttribute('data-zy-id');
                cur.now = parseInt(btn.getAttribute('data-zy-now'), 10) || 0;
                modal.querySelector('#zy-sname').textContent = btn.getAttribute('data-zy-name') || '';
                deltaEl.value = '';
                noteEl.value = '';
                errEl.style.display = 'none';
                preview();
                modal.classList.add('open');
                deltaEl.focus();
            });
        });

        deltaEl.addEventListener('input', preview);
        Array.prototype.forEach.call(modal.querySelectorAll('.zy-chips button'), function (b) {
            b.addEventListener('click', function () {
                deltaEl.value = b.getAttribute('data-d');
                preview();
            });
        });

        modal.querySelector('#zy-scancel').addEventListener('click', close);
        modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal.classList.contains('open')) close();
        });

        modal.querySelector('#zy-ssubmit').addEventListener('click', function () {
            var d = parseInt(deltaEl.value, 10);
            if (isNaN(d) || d === 0) { alert('请输入非 0 的增减数量'); return; }
            var submit = modal.querySelector('#zy-ssubmit');
            submit.disabled = true;
            submit.textContent = '处理中…';
            postJSON(cur.url, { id: cur.id, delta: d, note: noteEl.value.trim() },
                function () { window.location.reload(); },
                function (msg) {
                    errEl.textContent = msg;
                    errEl.style.display = 'block';
                    submit.disabled = false;
                    submit.textContent = '确认调整';
                });
        });
    }

    /* ── 4. 接单状态：恢复 / 暂停接单 ──────────────────────── */
    function bindCoordButtons() {
        var btns = document.querySelectorAll('button[data-zy-coord="1"]');
        if (!btns.length) return;

        Array.prototype.forEach.call(btns, function (btn) {
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                var action = btn.getAttribute('data-zy-action');
                var name = btn.getAttribute('data-zy-name') || '该商品';
                var payload = { id: btn.getAttribute('data-zy-id'), action: action };

                if (action === 'pause') {
                    // 暂停是人工决定（质量问题等），补货不会自动恢复，需让操作者知情
                    var notice = window.prompt(
                        '暂停「' + name + '」接单。\n' +
                        '提示：人工暂停不会随补货自动恢复，需在此手动恢复。\n\n' +
                        '给客户看的提示语（可留空用默认）：', '');
                    if (notice === null) return;          // 取消
                    payload.notice = notice;
                } else if (!window.confirm(
                        '恢复「' + name + '」接单？\n恢复后前台即可正常下单。')) {
                    return;
                }

                var old = btn.textContent;
                btn.disabled = true;
                btn.textContent = '…';
                postJSON(btn.getAttribute('data-zy-url'), payload,
                    function (d) {
                        // 顺带把返回的说明提示出来，操作完刷新可见新状态
                        if (d && d.message) { window.alert(d.message); }
                        window.location.reload();
                    },
                    function (msg) {
                        alert(msg);
                        btn.disabled = false;
                        btn.textContent = old;
                    });
            });
        });
    }

    function init() {
        try { bindBackLinks(); } catch (e) { /* 忽略，不影响其它交互 */ }
        try { bindPostButtons(); } catch (e) { /* 同上 */ }
        try { bindStockButtons(); } catch (e) { /* 同上 */ }
        try { bindCoordButtons(); } catch (e) { /* 同上 */ }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
