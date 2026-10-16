"""
契约验证：支付方式选择 + 入口统一（任务书五·1）—— ZT-agent

#72 把「付钱」这件事收成**一个入口、两种方式**：

    入口只有收银台 `/pay/<order_no>/`
    方式两种：扫码支付（出一次性码 → 手机确认）、余额支付（登录后直接扣钱包）

本脚本验两件事：

  A. **后端**：余额支付这条路走得通、且走得对
     1. 鉴权：未登录 401；他人订单 / 不存在的订单统一 404（不泄露存在性）
     2. 余额不足：400 + code=insufficient + 当前余额 + 缺口金额，
        且**订单一个字段没动、一条流水没写** —— "失败不留痕"是资金操作的基本要求
     3. 余额充足：扣款金额 == 订单应付、流水 kind=支付且带订单号、
        balance_after 与余额一致、订单落已支付 + paid_at
     4. 幂等：再调一次返回 already，余额与流水条数都不变
     5. 金额为 0 → zero_amount（业务层守卫）
     6. 订单处于不可付款状态 → 409（这里用「已取消」验证）
     7. **付完之后，同订单在途的二维码立刻作废**（屏上不该还挂着能付的码）
     8. 只动用户钱包，**不写公司账**（发货才记收入）
     9. 流水可逐条验算：上一条余额 ± 本条金额 == 本条余额

  B. **前端入口统一**（静态断言，防"改了三处漏一处"）
     · 对话订单名片(app.js) / 下单成功页(order.html) / 个人中心(profile.html)
       三处「立即支付」都必须**跳转到 /pay/<order_no>/**，
       不得再直接 POST 旧的 `/api/orders/<no>/pay/`
     · 收银台页有支付方式切换、余额面板、余额不足时的充值引导
     · 个人中心有钱包卡片（余额 / 模拟充值 / 流水）与 #wallet 锚点

自造场景 → 断言 → 精确还原：脚本自建临时用户 / 商品 / 库存，通过真实下单接口
产生订单，结束时删掉订单、凭据、流水、商品、用户，并还原公司钱包余额。

用法：./.venv/Scripts/python.exe scripts/verify_pay_methods.py
"""
import io
import json
import os
import sys
import uuid
from decimal import Decimal

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402

from agent.cashier import CashierError, create_ticket, pay_with_balance  # noqa: E402
from agent.models import (  # noqa: E402
    Inventory,
    Orders,
    PayTicket,
    PayTicketStatus,
    Products,
    Transaction,
    UserTransaction,
    UserTxKind,
    UserWallet,
    Wallet,
)
from agent.user_wallet import wallet_balance  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jreq(c, method, url, payload=None, auth=None):
    fn = getattr(c, method)
    if payload is None:
        r = fn(url, **(auth or {}))
    else:
        r = fn(url, data=json.dumps(payload), content_type="application/json", **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def read(rel):
    with io.open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


def main():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")

    User = get_user_model()
    sfx = uuid.uuid4().hex[:8]
    me = User.objects.create_user(username=f"paym_{sfx}", password="PayM!234567")
    other = User.objects.create_user(username=f"paym_o_{sfx}", password="PayM!234567")
    product = Products.objects.create(
        sku=f"PAYM{sfx[:5].upper()}", name=f"余额支付验证饵料{sfx}", spec="100g",
        target_fish="鲫鱼", retail_price=10, wholesale_price=6, is_active=True,
    )
    Inventory.objects.create(product=product, stock=100, reserved_stock=0)

    company = Wallet.objects.first()
    company_snapshot = company.balance if company else None

    c = Client()
    auth = bearer(me)
    auth_other = bearer(other)
    made = []

    def place(qty=2, phone="13800000009", name="支付验证"):
        st, d = jreq(c, "post", "/api/orders/", {
            "customer_name": name, "phone": phone,
            "address": "山东省济南市历下区余额路 2 号",
            "product_id": product.id, "quantity": qty,
        }, auth)
        assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
        made.append(d["order_no"])
        return d["order_no"]

    def order_of(no):
        return Orders.objects.filter(order_no=no).first()

    def tx_count(user):
        w = UserWallet.objects.filter(user_id=user.pk).first()
        return 0 if w is None else UserTransaction.objects.filter(wallet=w).count()

    def txs(user):
        w = UserWallet.objects.filter(user_id=user.pk).first()
        if w is None:
            return []
        return list(UserTransaction.objects.filter(wallet=w).order_by("id"))

    def cleanup():
        oids = list(Orders.objects.filter(order_no__in=made).values_list("id", flat=True))
        PayTicket.objects.filter(order_no__in=made).delete()
        Transaction.objects.filter(order_id__in=oids).delete()
        Orders.objects.filter(id__in=oids).delete()
        if company is not None and company_snapshot is not None:
            Wallet.objects.filter(pk=company.pk).update(balance=company_snapshot)
        Inventory.objects.filter(product=product).delete()
        Products.objects.filter(pk=product.pk).delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户 / 商品 / 库存 / 订单 / 凭据 / 流水已清理，公司钱包余额已还原)")

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】鉴权：未登录 401、他人订单与不存在统一 404")
        # ════════════════════════════════════════════════════════
        st, _ = jreq(c, "post", "/api/pay/ZT-NOPE/balance/")
        check("未登录余额支付 → 401", st == 401, f"实际 {st}")

        no1 = place(2)                                  # ¥20.00
        st, _ = jreq(c, "post", f"/api/pay/{no1}/balance/", {}, auth_other)
        check("他人订单余额支付 → 404（不泄露存在性）", st == 404, f"实际 {st}")
        st, _ = jreq(c, "post", "/api/pay/DD00000000000000/balance/", {}, auth)
        check("不存在的订单 → 404", st == 404, f"实际 {st}")

        # ════════════════════════════════════════════════════════
        print("\n【2】余额不足：报错清楚，且订单/账本**一点没动**")
        # ════════════════════════════════════════════════════════
        check("初始余额为 0", wallet_balance(me) == Decimal("0.00"), str(wallet_balance(me)))
        st, d = jreq(c, "post", f"/api/pay/{no1}/balance/", {}, auth)
        check("余额不足 → 400", st == 400, f"实际 {st}")
        check("code = insufficient（前端据此走充值引导而不是重试）",
              d.get("code") == "insufficient", str(d.get("code")))
        check("带回当前余额（0.00）", d.get("balance") == "0.00", str(d.get("balance")))
        check("带回缺口金额（20.00）", d.get("shortage") == "20.00", str(d.get("shortage")))
        check("提示里写明具体数字", "20.00" in (d.get("message") or ""), str(d.get("message")))
        o = order_of(no1)
        check("订单仍是未支付（失败不留痕）", o.payment_status != "已支付", str(o.payment_status))
        check("订单没有 paid_at", o.paid_at is None, str(o.paid_at))
        check("没有写出任何流水", tx_count(me) == 0, str(tx_count(me)))

        # ════════════════════════════════════════════════════════
        print("\n【3】余额充足：扣款 → 记流水 → 订单落已支付")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", "/api/me/wallet/recharge/", {"amount": "100"}, auth)
        check("充值 100 成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("余额变为 100.00", wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))

        st, d = jreq(c, "post", f"/api/pay/{no1}/balance/", {}, auth)
        check("余额支付成功（200）", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("返回体里 code = paid", d.get("code") == "paid", str(d.get("code")))
        check("返回体里 already = False（这是首次支付）", d.get("already") is False, str(d.get("already")))
        check("返回体里带回最新余额（80.00）", d.get("balance") == "80.00", str(d.get("balance")))
        check("扣的是订单应付金额（20.00）", d.get("amount") == "20.00", str(d.get("amount")))
        check("余额确实变成 80.00", wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))

        o = order_of(no1)
        check("订单落「已支付」", o.payment_status == "已支付", str(o.payment_status))
        check("订单有 paid_at", o.paid_at is not None, str(o.paid_at))
        check("订单状态机不动（仍是未发货）", o.status == "未发货", str(o.status))

        st, d = jreq(c, "get", "/api/me/wallet/?limit=20", auth=auth)
        lst = d.get("transactions") or []
        pay_tx = [t for t in lst if t.get("kind") == "支付"]
        check("流水里有且仅有一条「支付」", len(pay_tx) == 1, str([t.get("kind") for t in lst]))
        if pay_tx:
            t = pay_tx[0]
            check("该笔流水挂在正确的订单号上", t.get("order_no") == no1, str(t.get("order_no")))
            check("该笔流水金额 = 20.00", t.get("amount") == "20.00", str(t.get("amount")))
            check("该笔流水 balance_after = 80.00（可独立验算）",
                  t.get("balance_after") == "80.00", str(t.get("balance_after")))
        check("钱包概览的累计支付 = 20.00", d.get("paid") == "20.00", str(d.get("paid")))
        check("钱包概览的累计充值 = 100.00", d.get("recharged") == "100.00", str(d.get("recharged")))
        check("钱包概览的余额 = 80.00", d.get("balance") == "80.00", str(d.get("balance")))

        # ════════════════════════════════════════════════════════
        print("\n【4】幂等：再点一次不会扣两次")
        # ════════════════════════════════════════════════════════
        n_before = tx_count(me)
        st, d = jreq(c, "post", f"/api/pay/{no1}/balance/", {}, auth)
        check("重复支付仍返回 200（幂等，不报错）", st == 200, f"实际 {st}")
        check("code = already_paid", d.get("code") == "already_paid", str(d.get("code")))
        check("already = True", d.get("already") is True, str(d.get("already")))
        check("余额仍是 80.00（没被扣第二次）",
              wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))
        check("流水条数没变", tx_count(me) == n_before, f"{n_before} → {tx_count(me)}")

        # ════════════════════════════════════════════════════════
        print("\n【5】付完之后，同一订单在途的二维码立刻作废")
        # ════════════════════════════════════════════════════════
        no2 = place(3)                                  # ¥30.00
        tk = create_ticket(order_of(no2), me)
        check("先开出一张在途二维码", tk.status == "待支付", str(tk.status))
        st, d = jreq(c, "post", f"/api/pay/{no2}/balance/", {}, auth)
        check("改用余额支付成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        tk.refresh_from_db()
        check("屏上那张码被作废（不该还能扫）",
              tk.status == PayTicketStatus.CANCELLED, str(tk.status))
        st, d = jreq(c, "post", f"/api/pay/confirm/{tk.token}/")
        check("拿作废的码去确认 → 不成功", not d.get("success"), f"{st} {d.get('code')}")
        check("订单没有被那张作废的码影响", order_of(no2).payment_status == "已支付",
              str(order_of(no2).payment_status))

        # ════════════════════════════════════════════════════════
        print("\n【6】不可付款的订单：409，并给出可读原因")
        # ════════════════════════════════════════════════════════
        no3 = place(1)
        # 注意：/api/orders/<no>/cancel/ 是**管理员**路由（@staff_required），
        # 普通用户取消订单走个人中心那条 /api/me/orders/<no>/cancel/。
        st, d = jreq(c, "post", f"/api/me/orders/{no3}/cancel/", {"reason": "验证用"}, auth)
        check("先把订单取消掉", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        st, d = jreq(c, "post", f"/api/pay/{no3}/balance/", {}, auth)
        check("已取消的订单余额支付 → 409", st == 409, f"实际 {st}")
        check("给出可读原因（code=closed）", d.get("code") == "closed", str(d.get("code")))
        check("不会扣钱", wallet_balance(me) == Decimal("50.00"), str(wallet_balance(me)))

        # ════════════════════════════════════════════════════════
        print("\n【7】业务层守卫：金额为 0 的订单拒绝扣款")
        # ════════════════════════════════════════════════════════
        no4 = place(1)
        Orders.objects.filter(order_no=no4).update(total_price=Decimal("0.00"))
        st, d = jreq(c, "post", f"/api/pay/{no4}/balance/", {}, auth)
        check("金额 0 → 400", st == 400, f"实际 {st}")
        check("code = zero_amount", d.get("code") == "zero_amount", str(d.get("code")))
        try:
            pay_with_balance(order_of(no4), me)
            check("业务层直接调用也拒绝", False, "居然没抛异常")
        except CashierError as e:
            check("业务层直接调用也拒绝", e.code == "zero_amount", e.code)

        # ════════════════════════════════════════════════════════
        print("\n【8】只动用户账：公司钱包与退款账本零变化")
        # ════════════════════════════════════════════════════════
        if company is not None:
            company.refresh_from_db()
            check("公司钱包余额未变（收款要等发货才记）",
                  company.balance == company_snapshot, f"{company_snapshot} → {company.balance}")
        check("没写出任何订单退款流水",
              not Transaction.objects.filter(order_id__in=[
                  o.id for o in Orders.objects.filter(order_no__in=made)
              ]).exists())

        # ════════════════════════════════════════════════════════
        print("\n【9】账本逐条验算：上一条余额 ± 本条金额 == 本条余额")
        rows = txs(me)
        check("流水至少 3 条（充值 + 两次支付）", len(rows) >= 3, str(len(rows)))
        running = Decimal("0.00")
        ok_all = True
        for t in rows:
            delta = t.amount if t.kind in (UserTxKind.RECHARGE, UserTxKind.REFUND) else -t.amount
            running += delta
            if running != t.balance_after:
                ok_all = False
                break
        check("逐条验算全部成立", ok_all, f"最后一条算出 {running}")
        check("验算终值 == 当前余额", running == wallet_balance(me),
              f"{running} vs {wallet_balance(me)}")
        check("金额一律为正数（方向由 kind 决定）",
              all(t.amount > 0 for t in rows), str([str(t.amount) for t in rows]))

        # ════════════════════════════════════════════════════════
        print("\n【10】表结构：user_wallet / 流水索引与唯一约束")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_wallet' "
                        "AND COLUMN_NAME='user_id' AND NON_UNIQUE=0")
            check("user_wallet.user_id 有唯一约束（一人一本账）", cur.fetchone()[0] > 0)
            cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_wallet_transactions'")
            check("user_wallet_transactions 表存在", cur.fetchone()[0] == 1)

        # ════════════════════════════════════════════════════════
        print("\n【11】前端入口统一：三处「立即支付」都跳收银台")
        # ════════════════════════════════════════════════════════
        appjs = read("frontend/app.js")
        html_order = read("frontend/order.html")
        html_profile = read("frontend/profile.html")

        check("app.js（对话订单名片）跳到 /pay/<no>/",
              "'/pay/' + encodeURIComponent(order.order_no) + '/'" in appjs)
        check("app.js 不再直接 POST 旧的 /api/orders/<no>/pay/",
              "/api/orders/' + encodeURIComponent(order.order_no) + '/pay/" not in appjs)
        check("order.html（下单成功页）跳到 /pay/<no>/",
              "'/pay/' + encodeURIComponent(result.order_no) + '/'" in html_order)
        check("order.html 不再直接 POST 旧的 /api/orders/<no>/pay/",
              "/api/orders/' + encodeURIComponent(result.order_no) + '/pay/" not in html_order)
        check("profile.html（个人中心）跳到 /pay/<no>/",
              "'/pay/' + encodeURIComponent(orderNo) + '/'" in html_profile)
        check("profile.html 不再直接 POST 旧的 /api/me/orders/<no>/pay/",
              "encodeURIComponent(orderNo) + '/' + action + '/'" in html_profile
              and "action === 'pay'" in html_profile)

        # ════════════════════════════════════════════════════════
        print("\n【12】收银台页：支付方式切换 + 余额面板 + 充值引导")
        # ════════════════════════════════════════════════════════
        pay_html = read("frontend/pay.html")
        check("页面上有支付方式容器（methodTabs）", 'id="methodTabs"' in pay_html)
        check("两种方式都在（扫码支付 / 余额支付）",
              "扫码支付" in pay_html and "余额支付" in pay_html)
        check("默认走扫码支付", "method: '扫码支付'" in pay_html)
        check("调用了余额支付接口", "'/balance/'" in pay_html)
        check("进页面先拉余额（不满就来不及提示了）", "loadBalance()" in pay_html)
        check("余额不足时给「还差多少」", "还差 ¥" in pay_html)
        check("余额不足时给充值引导（#wallet 锚点）",
              'href="profile.html#wallet"' in pay_html)
        check("倒计时改成按截止时刻算（防后台节流漂移）", "deadline" in pay_html)
        check("余额支付有提交中锁（防连点）", "state.busy" in pay_html)

        # ════════════════════════════════════════════════════════
        print("\n【13】个人中心：钱包卡片（余额 / 模拟充值 / 流水）")
        # ════════════════════════════════════════════════════════
        check("有 #wallet 锚点", 'id="wallet"' in html_profile)
        check("展示可用余额", 'id="walletBalance"' in html_profile)
        check("有充值输入框", 'id="topupAmount"' in html_profile)
        check("调用充值接口", "'/api/me/wallet/recharge/'" in html_profile)
        check("读取钱包概览 + 流水", "'/api/me/wallet/?limit=20'" in html_profile)
        check("有流水容器", 'id="txList"' in html_profile)
        check("充值说明写明「模拟」并解释边界", "模拟充值" in html_profile)
        check("充值金额快捷档只填不提交", "topup-quick" in html_profile)
        check("带 #wallet 进来时自动聚焦输入框", "location.hash === '#wallet'" in html_profile)

    finally:
        cleanup()

    print("\n" + "=" * 62)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print(f"    ✗ {f}")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
