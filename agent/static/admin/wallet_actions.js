/**
 * 钱包「充值 / 扣款」按钮
 * 在钱包总览页（/admin/agent/wallet/...）与收支账本页（/admin/agent/transaction/）
 * 右下角注入「充值」「扣款」两个按钮：弹窗输入金额 + **必填**的款项说明，
 * fetch 提交到 /admin/agent/wallet/adjust/，成功后刷新。
 *
 * 说明必填的原因：公司钱包要"每一笔钱款都有据可查"，只记金额不记来源，
 * 事后对账时无法判断这笔钱是哪来的。
 */
(function () {
    if (document.getElementById('zy-wallet-style')) return;
    // 钱包总览页 + 收支账本页都启用
    var p = window.location.pathname;
    if (p.indexOf('/admin/agent/wallet/') !== 0 && p.indexOf('/admin/agent/transaction/') !== 0) return;

    var style = document.createElement('style');
    style.id = 'zy-wallet-style';
    style.textContent = [
        '#zy-wallet-ops{position:fixed;right:24px;bottom:24px;z-index:9999;display:flex;gap:10px}',
        '#zy-wallet-ops button{border:none;border-radius:999px;padding:0 20px;height:46px;font-size:14px;font-weight:600;cursor:pointer;color:#fff}',
        '#zy-income-btn{background:#1F6B54}',
        '#zy-expense-btn{background:#E2703A}',
        '#zy-wmodal{position:fixed;inset:0;z-index:10000;display:none;align-items:center;justify-content:center;background:rgba(15,23,20,.45)}',
        '#zy-wmodal.open{display:flex}',
        '#zy-wmodal .zy-panel{width:92%;max-width:380px;background:#fff;border-radius:16px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,.25)}',
        '#zy-wmodal h3{margin:0 0 16px;font-size:17px}',
        '#zy-wmodal label{display:block;font-size:12px;color:#5b6670;margin:10px 0 4px}',
        '#zy-wmodal input{width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid #d7dce0;border-radius:8px;font-size:15px}',
        '#zy-wmodal input:focus{outline:none;border-color:#1F6B54;box-shadow:0 0 0 3px rgba(31,107,84,.12)}',
        '#zy-wmodal .zy-err{color:#C73320;font-size:13px;margin-top:10px;display:none}',
        '#zy-wmodal .zy-btns{display:flex;gap:12px;justify-content:flex-end;margin-top:20px}',
        '#zy-wmodal .zy-btns button{padding:9px 18px;border-radius:8px;font-size:14px;cursor:pointer;border:none}',
        '#zy-wmodal .zy-cancel{background:#f0f2f4;color:#333}',
        '#zy-wmodal .zy-submit{background:#1F6B54;color:#fff;font-weight:600}',
    ].join('');
    document.head.appendChild(style);

    var ops = document.createElement('div');
    ops.id = 'zy-wallet-ops';
    ops.innerHTML = '<button id="zy-income-btn" type="button">＋ 充值</button>' +
                    '<button id="zy-expense-btn" type="button">－ 扣款</button>';
    document.body.appendChild(ops);

    var modal = document.createElement('div');
    modal.id = 'zy-wmodal';
    modal.innerHTML =
        '<div class="zy-panel">' +
        '<h3 id="zy-wtitle">充值</h3>' +
        '<label>金额(元) *</label><input id="zy-amount" type="number" step="0.01" min="0.01">' +
        '<label>款项说明 * <span style="color:#9AA89F;font-weight:400">（这笔钱从哪来 / 花到哪去）</span></label>' +
        '<input id="zy-note" placeholder="如：客户张老板货款 / 采购饵料支出 / 提现到公户">' +
        '<div class="zy-err" id="zy-werr"></div>' +
        '<div class="zy-btns">' +
        '<button type="button" class="zy-cancel" id="zy-wcancel">取消</button>' +
        '<button type="button" class="zy-submit" id="zy-wsubmit">确认</button>' +
        '</div></div>';
    document.body.appendChild(modal);

    var curDirection = 'income';
    function getCSRF() {
        var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }
    function open(direction) {
        curDirection = direction;
        document.getElementById('zy-wtitle').textContent = direction === 'income' ? '充值（入账）' : '扣款（出账）';
        document.getElementById('zy-amount').value = '';
        document.getElementById('zy-note').value = '';
        document.getElementById('zy-werr').style.display = 'none';
        modal.classList.add('open');
        document.getElementById('zy-amount').focus();
    }
    function close() { modal.classList.remove('open'); }
    function err(msg) { var e = document.getElementById('zy-werr'); e.textContent = msg; e.style.display = 'block'; }

    document.getElementById('zy-income-btn').addEventListener('click', function () { open('income'); });
    document.getElementById('zy-expense-btn').addEventListener('click', function () { open('expense'); });
    document.getElementById('zy-wcancel').addEventListener('click', close);
    modal.addEventListener('click', function (e) { if (e.target === modal) close(); });

    document.getElementById('zy-wsubmit').addEventListener('click', function () {
        var amount = document.getElementById('zy-amount').value;
        var note = document.getElementById('zy-note').value.trim();
        if (!amount || parseFloat(amount) <= 0) { err('请输入大于 0 的金额'); return; }
        if (!note) { err('请填写款项说明（这笔钱从哪来 / 花到哪去）'); return; }
        var btn = document.getElementById('zy-wsubmit');
        btn.disabled = true; btn.textContent = '处理中…';
        fetch('/admin/agent/wallet/adjust/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRF() },
            body: JSON.stringify({
                direction: curDirection,
                amount: amount,
                note: note,
            }),
        }).then(function (r) { return r.json(); }).then(function (d) {
            if (d.success) { window.location.reload(); }
            else { err(d.message || '操作失败'); btn.disabled = false; btn.textContent = '确认'; }
        }).catch(function () {
            err('网络异常，请重试'); btn.disabled = false; btn.textContent = '确认';
        });
    });
})();
