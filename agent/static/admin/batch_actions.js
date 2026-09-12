/**
 * 通用批量操作弹窗：勾选行后，右下角浮出操作栏（列出所有可用 action），
 * 点某操作 → 弹确认框 → 提交，替代"底部下拉 + 点运行"的繁琐流程。
 * 注入所有 admin 列表页（凡是有 action 下拉的页面）。
 */
(function () {
    if (document.getElementById('zy-batch-style')) return;
    // 仅在有 action 下拉的列表页生效
    var actionSelect = document.querySelector('select[name="action"]');
    if (!actionSelect) return;

    var style = document.createElement('style');
    style.id = 'zy-batch-style';
    style.textContent = [
        '#zy-batch-bar{position:fixed;right:24px;bottom:24px;z-index:9998;background:#fff;border:1px solid #e2e5e2;',
        'border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:12px 16px;display:none;min-width:220px}',
        '#zy-batch-bar.open{display:block}',
        '#zy-batch-bar .zy-count{font-size:13px;color:#5b6670;margin-bottom:8px;font-weight:600}',
        '#zy-batch-bar .zy-actions{display:flex;flex-direction:column;gap:6px}',
        '#zy-batch-bar button{display:block;width:100%;text-align:left;padding:8px 12px;border:none;border-radius:8px;',
        'background:#f4f6f5;color:#1a1a1a;font-size:13px;cursor:pointer}',
        '#zy-batch-bar button:hover{background:#e8f0ec}',
        '#zy-batch-bar button.zy-danger{background:#fbeae5;color:#C73320}',
        '#zy-batch-bar button.zy-danger:hover{background:#f6d9d1}',
    ].join('');
    document.head.appendChild(style);

    var bar = document.createElement('div');
    bar.id = 'zy-batch-bar';
    bar.innerHTML = '<div class="zy-count" id="zy-batch-count"></div><div class="zy-actions" id="zy-batch-actions"></div>';
    document.body.appendChild(bar);

    function checkedCount() {
        return document.querySelectorAll('input[name="_selected_action"]:checked').length;
    }
    function getActions() {
        // 读取 action 下拉里的选项（排除空值占位）
        var opts = [];
        Array.prototype.forEach.call(actionSelect.options, function (o) {
            if (o.value) opts.push({ value: o.value, label: o.textContent.trim() });
        });
        return opts;
    }
    function render() {
        var n = checkedCount();
        if (n === 0) { bar.classList.remove('open'); return; }
        document.getElementById('zy-batch-count').textContent = '已选中 ' + n + ' 条';
        var box = document.getElementById('zy-batch-actions');
        box.innerHTML = '';
        getActions().forEach(function (a) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = a.label;
            // 删除类操作标红
            if (/删除/i.test(a.label)) btn.classList.add('zy-danger');
            btn.addEventListener('click', function () {
                var isDelete = /删除/i.test(a.label);
                // 「下一步」类动作只是跳到表单页（如发货页还要填物流），
                // 在那边仍有取消按钮 → 这里不再多一次确认，少点一次。
                var isNavigate = /下一步/.test(a.label);
                if (!isNavigate) {
                    var msg = isDelete
                        ? '⚠️ 确认删除选中的 ' + n + ' 条记录？此操作不可恢复！'
                        : '确认对选中的 ' + n + ' 条执行「' + a.label + '」？';
                    if (!window.confirm(msg)) return;
                }
                actionSelect.value = a.value;
                var form = document.getElementById('changelist-form');
                if (form) form.submit();
            });
            box.appendChild(btn);
        });
        bar.classList.add('open');
    }
    document.addEventListener('change', function (e) {
        if (e.target && (e.target.name === '_selected_action' || e.target.id === 'action-toggle')) render();
    });
    // 初始渲染（处理"全选"后刷新）
    render();
})();
