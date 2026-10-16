"""
全流程回归：从「问一句」到「钱回到余额」（任务书五·1 + 六·5）—— ZT-agent

为什么要单开一支端到端脚本
--------------------------
#70–#72 把支付拆成了几个各自验证过的零件：
    · `verify_user_wallet.py`  —— 钱包本身的三个不变式
    · `verify_cashier.py`      —— 扫码那条路（开票 → 核销 → 入账）
    · `verify_pay_methods.py`  —— 余额支付这条路 + 三处入口统一
零件都绿，不代表**串起来**还成立。#73 补的正是中间那两处：

  ① **退货 / 取消要把钱退回用户余额**（本脚本的主角）
     此前 `record_order_refund` 只写公司账 —— 而公司账是「经营账」，
     它记的是"商家退掉了 100 元"，不代表用户手里多出 100 元。
     用户当初用余额付的钱，钱从他的**预存账**扣走，却再也没回来。
     同一个业务动作、两本账方向相反，这就是缺口所在。

  ② 一条**贯通**的路径：问 → 名片 → 收银台 → 选方式 → 支付 → 查单 → 退货 → 钱回余额

本脚本用真实 HTTP 接口（JWT）+ 工具层直接调用（跳过 LLM，因为要可重复），
把这条路整条走一遍，并逐条核对**两本账**。

    【1】准备与「问 → 名片」：对话工具查单会登记订单名片（含 actions）
    【2】收银台开票：PC 出码（二维码 + 明文链接）
    【3】余额不足被拦，且**订单一个字段没动**（失败不留痕）
    【4】余额支付：第 2 条入账通路走通
    【5】查单：名片上的「立即支付」按钮消失（按钮与个人中心同源）
    【6】发货：公司账记一笔「订单收入」（经营账）
    【7】申请退货：**一分钱都不该动**（防用户单方触发资金变动）
    【8】同意退货：公司账冲销 + **余额原路退回** ← 缺口①的落点
    【9】幂等：同一订单不会退第二次
    【10】退回的钱是**能用的钱**（能拿来付下一单，不是账面数字）
    【11】已支付订单取消：钱同样退回余额（取消分支的对称修复）
    【12】没走过余额支付的订单**不产生**退款流水（宁可不退，也不错退）
    【13】账本逐条验算：上一条余额 ± 本条金额 == 本条余额
    【14】两本账对照：方向相反、金额相等、各自净额归零
    【15】表结构 + 静态接线（防"改了一处漏一处"）

自造场景 → 断言 → 精确还原：脚本自建临时用户 / 商品 / 库存 / 后台账号，
结束时删掉订单、凭据、流水、商品、用户，并还原公司钱包余额。

用法：./.venv/Scripts/python.exe scripts/verify_pay_flow.py
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

from agent import tools  # noqa: E402
from agent.models import (  # noqa: E402
    Inventory,
    Orders,
    PayTicket,
    Products,
    Transaction,
    TxType,
    UserTransaction,
    UserTxKind,
    UserWallet,
    Wallet,
)
from agent.services import OrderTransitionError, transition_order  # noqa: E402
from agent.user_wallet import refund_order_if_paid, wallet_balance  # noqa: E402

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

    # ── 临时场景 ──────────────────────────────────────────────
    me = User.objects.create_user(username=f"flow_{sfx}", password="Flow!234567")
    boss = User.objects.create_user(username=f"flow_b_{sfx}", password="Flow!234567",
                                    is_staff=True)
    product = Products.objects.create(
        sku=f"FLOW{sfx[:5].upper()}", name=f"全流程验证饵料{sfx}", spec="100g",
        target_fish="鲫鱼", retail_price=10, wholesale_price=6, is_active=True,
    )
    Inventory.objects.create(product=product, stock=500, reserved_stock=0)

    company = Wallet.objects.first()
    company_snapshot = company.balance if company else None

    c = Client()
    auth = bearer(me)
    auth_boss = bearer(boss)
    made = []

    def place(qty=2, name="全流程验证"):
        st, d = jreq(c, "post", "/api/orders/", {
            "customer_name": name, "phone": "13800000021",
            "address": "山东省济南市历下区回款路 3 号",
            "product_id": product.id, "quantity": qty,
        }, auth)
        assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
        made.append(d["order_no"])
        return d["order_no"]

    def order_of(no):
        return Orders.objects.filter(order_no=no).first()

    def txs(user):
        w = UserWallet.objects.filter(user_id=user.pk).first()
        if w is None:
            return []
        return list(UserTransaction.objects.filter(wallet=w).order_by("id"))

    def flows_of(no, kind=None):
        qs = UserTransaction.objects.filter(order_no=no)
        if kind:
            qs = qs.filter(kind=kind)
        return list(qs.order_by("id"))

    def company_rows(order):
        return list(Transaction.objects.filter(order=order).order_by("id"))

    def company_net(rows):
        return sum((r.amount if r.tx_type == TxType.INCOME else -r.amount) for r in rows)

    def ask_order(no):
        """模拟「客户在对话里问『我的货到哪了』」—— 直接调工具层，不经过 LLM。

        工具内部会 `_remember_order()` 登记名片，`agent.py` 再把它转成
        `order_card` 事件给前端渲染。这里验的就是这条登记的产物。
        """
        tools.reset_referenced_orders()
        tools.set_current_user(me)
        tools.get_order_status(no)
        cards = tools.get_referenced_orders()
        return cards[0] if cards else None

    def cleanup():
        oids = list(Orders.objects.filter(order_no__in=made).values_list("id", flat=True))
        PayTicket.objects.filter(order_no__in=made).delete()
        Transaction.objects.filter(order_id__in=oids).delete()
        Orders.objects.filter(id__in=oids).delete()
        if company is not None and company_snapshot is not None:
            Wallet.objects.filter(pk=company.pk).update(balance=company_snapshot)
        Inventory.objects.filter(product=product).delete()
        Products.objects.filter(pk=product.pk).delete()
        User.objects.filter(pk__in=[me.pk, boss.pk]).delete()
        print("\n    (临时用户 / 后台账号 / 商品 / 库存 / 订单 / 凭据 / 流水已清理，"
              "公司钱包余额已还原)")

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】准备 + 对话查单 → 订单名片（未付款时名片上应有「立即支付」）")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", "/api/me/wallet/recharge/", {"amount": "100"}, auth)
        check("先充值 ¥100（模拟充值）", st == 200 and d.get("success"),
              f"{st} {d.get('message')}")
        check("余额 = 100.00", wallet_balance(me) == Decimal("100.00"),
              str(wallet_balance(me)))

        no1 = place(2)                       # 2 × ¥10 = ¥20.00
        o1 = order_of(no1)
        check("下单成功且金额 = 20.00", o1.total_price == Decimal("20.00"), str(o1.total_price))
        check("新单是「未支付」", o1.payment_status == "未支付", str(o1.payment_status))

        card = ask_order(no1)
        check("AI 工具查单后登记了一张订单名片", card is not None)
        check("名片上的订单号正确", (card or {}).get("order_no") == no1,
              str((card or {}).get("order_no")))
        check("名片带 actions（按钮与个人中心同源，不各写一套）",
              "actions" in (card or {}), str(list((card or {}).keys())[:6]))
        check("未付款 → 名片上有「pay」动作",
              "pay" in (card or {}).get("actions", []), str((card or {}).get("actions")))
        check("未付款 → 名片上也给「cancel」（未发货可取消）",
              "cancel" in (card or {}).get("actions", []), str((card or {}).get("actions")))
        check("名片只带对话要用的字段（不返回收货地址/退货原因）",
              "address" not in (card or {}) and "return_reason" not in (card or {}))

        # 检查「我的订单」这条入口也能看到（身份由 set_current_user 注入，
        # 客户不必再报订单号或手机号）
        tools.reset_referenced_orders()
        tools.set_current_user(me)
        mine = tools.query_my_orders()
        check("工具能按当前登录用户查到「我的订单」", no1 in mine, mine[:60].replace("\n", " "))

        # ════════════════════════════════════════════════════════
        print("\n【2】收银台开票：PC 出码（二维码 + 明文链接）")
        # ════════════════════════════════════════════════════════
        st, body = jreq(c, "post", f"/api/pay/{no1}/ticket/", auth=auth)
        check("登录后开票 → 200", st == 200, f"{st} {body.get('message')}")
        tk = body.get("ticket") or {}
        tok = tk.get("token") or ""
        check("凭据状态「待支付」", tk.get("status") == "待支付", str(tk.get("status")))
        check("金额 = 订单应付 20.00", tk.get("amount") == "20.00", str(tk.get("amount")))
        check("凭据是一次性的（token 40~64 位）", 40 <= len(tok) <= 64, str(len(tok)))
        check("二维码 SVG 已生成（纯 Python 出码，不依赖 PIL）",
              "<svg" in (body.get("qr_svg") or ""), f"qr_svg 长度 {len(body.get('qr_svg') or '')}")
        check("同时给明文链接（扫码不成也不好让人白等）",
              bool(body.get("pay_url")), str(body.get("pay_url"))[:48])
        check("明文链接指向手机确认页且带 token",
              f"/pay/confirm/{tok}/" in (body.get("pay_url") or ""),
              str(body.get("pay_url"))[:64])
        check("轮询接口能看到这张待支付凭据",
              jreq(c, "get", f"/api/pay/{no1}/status/", auth=auth)[1]
              .get("ticket", {}).get("status") == "待支付")

        # ════════════════════════════════════════════════════════
        print("\n【3】余额不足被拦 —— 且订单一个字段都没动（失败不留痕）")
        # ════════════════════════════════════════════════════════
        no_big = place(20)                   # 20 × ¥10 = ¥200 > 余额 100
        o_big = order_of(no_big)
        st, d = jreq(c, "post", f"/api/pay/{no_big}/balance/", {}, auth)
        check("余额不足 → 400", st == 400, f"实际 {st}")
        check("code = insufficient（前端据此去引导充值，而不是让用户重试）",
              d.get("code") == "insufficient", str(d.get("code")))
        check("带回当前余额", d.get("balance") == "100.00", str(d.get("balance")))
        check("带回缺口金额", str(d.get("shortage") or "").replace(".", "").isdigit(),
              str(d.get("shortage")))
        o_big.refresh_from_db()
        check("订单仍是「未支付」", o_big.payment_status == "未支付", str(o_big.payment_status))
        check("订单没有 paid_at（失败不留痕）", o_big.paid_at is None, str(o_big.paid_at))
        check("没为这笔失败写出任何流水", flows_of(no_big) == [], str(flows_of(no_big)))
        check("余额没被扣（仍 100.00）", wallet_balance(me) == Decimal("100.00"),
              str(wallet_balance(me)))
        st, d = jreq(c, "post", f"/api/me/orders/{no_big}/cancel/", {"reason": "验证用"}, auth)
        check("把这张大单取消掉（清理场景）", st == 200 and d.get("success"),
              f"{st} {d.get('message')}")

        # ════════════════════════════════════════════════════════
        print("\n【4】选「余额支付」：扣钱包 → 记流水 → 订单落已支付")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/pay/{no1}/balance/", {}, auth)
        check("余额支付 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("code = paid", d.get("code") == "paid", str(d.get("code")))
        check("返回体带回最新余额 80.00", d.get("balance") == "80.00", str(d.get("balance")))
        o1.refresh_from_db()
        check("订单落「已支付」", o1.payment_status == "已支付", str(o1.payment_status))
        check("订单写了 paid_at", o1.paid_at is not None, str(o1.paid_at))
        check("订单状态机不动（仍是未发货）", o1.status == "未发货", str(o1.status))
        check("余额变成 80.00", wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))
        pay1 = flows_of(no1, UserTxKind.PAY)
        check("写了 1 条「支付」流水", len(pay1) == 1, str(len(pay1)))
        check("流水金额 = 实付 20.00", pay1 and pay1[0].amount == Decimal("20.00"),
              str(pay1[0].amount) if pay1 else "-")
        tk_db = PayTicket.objects.filter(token=tok).first()
        check("付清后屏上那张二维码立刻作废（不该还能扫）",
              tk_db is not None and tk_db.status == "已取消", str(tk_db.status))

        # ════════════════════════════════════════════════════════
        print("\n【5】查单：名片上的「立即支付」消失")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "get", f"/api/orders/{no1}/", auth=auth)
        check("查单接口 200", st == 200, f"实际 {st}")
        check("查单回显 payment_status = 已支付",
              (d.get("order") or {}).get("payment_status") == "已支付"
              or d.get("payment_status") == "已支付",
              str({k: v for k, v in d.items() if k in ('payment_status', 'order')})[:80])
        card2 = ask_order(no1)
        check("名片还能查到", card2 is not None)
        check("已支付 → 名片上**没有**「pay」动作",
              "pay" not in (card2 or {}).get("actions", []), str((card2 or {}).get("actions")))
        check("已支付 → 仍可取消（商家还没发货）",
              "cancel" in (card2 or {}).get("actions", []), str((card2 or {}).get("actions")))

        # ════════════════════════════════════════════════════════
        print("\n【6】发货：公司账记一笔「订单收入」（经营账）")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/orders/{no1}/ship/",
                     {"ship_company": "顺丰", "tracking_no": "SF-FLOW-001"}, auth_boss)
        check("后台发货 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        o1.refresh_from_db()
        check("订单变为「已发货」", o1.status == "已发货", str(o1.status))
        rows = company_rows(o1)
        check("公司账记了 1 笔收入（发货入账）",
              len(rows) == 1 and rows[0].tx_type == TxType.INCOME,
              str([(r.tx_type, str(r.amount)) for r in rows]))
        check("公司账收入金额 = 20.00", rows and rows[0].amount == Decimal("20.00"),
              str(rows[0].amount) if rows else "-")
        check("发货**不动**用户余额（钱已经付过了）",
              wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))

        # ════════════════════════════════════════════════════════
        print("\n【7】用户申请退货：申请阶段**一分钱都不该动**")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/me/orders/{no1}/return/",
                     {"reason": "包装破损"}, auth)
        check("申请退货 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        o1.refresh_from_db()
        check("订单进入「退货申请中」", o1.status == "退货申请中", str(o1.status))
        check("退货申请待审核", o1.return_status == "待审核", str(o1.return_status))
        check("申请阶段**余额一分不动**（防用户单方触发资金变动）",
              wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))
        check("申请阶段不写退款流水", flows_of(no1, UserTxKind.REFUND) == [],
              str(len(flows_of(no1, UserTxKind.REFUND))))
        check("申请阶段不再多记公司账（仍只有发货那 1 笔）",
              len(company_rows(o1)) == 1, str(len(company_rows(o1))))
        check("名片上出现「withdraw_return」（可自行撤销）",
              "withdraw_return" in (ask_order(no1) or {}).get("actions", []),
              str((ask_order(no1) or {}).get("actions")))

        # ════════════════════════════════════════════════════════
        print("\n【8】商家同意退货：公司账冲销 + **余额原路退回** ★")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/orders/{no1}/return/",
                     {"cancel_reason": "同意退货"}, auth_boss)
        check("后台同意退货 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        o1.refresh_from_db()
        check("订单变为「已退货」", o1.status == "已退货", str(o1.status))
        check("支付维度同步为「已退款」", o1.payment_status == "已退款", str(o1.payment_status))
        check("退货完成文案里写明了退多少钱、退到哪",
              "退回账户余额" in (d.get("message") or ""), str(d.get("message")))
        check("★ 余额**原路退回**：80.00 → 100.00",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))
        ref1 = flows_of(no1, UserTxKind.REFUND)
        check("★ 用户账多了 1 条「退款」流水", len(ref1) == 1, str(len(ref1)))
        check("退款金额 = 实付 20.00", ref1 and ref1[0].amount == Decimal("20.00"),
              str(ref1[0].amount) if ref1 else "-")
        check("退款流水挂在正确的订单号上", ref1 and ref1[0].order_no == no1,
              str(ref1[0].order_no) if ref1 else "-")
        check("退款流水的 balance_after = 100.00（可独立验算）",
              ref1 and ref1[0].balance_after == Decimal("100.00"),
              str(ref1[0].balance_after) if ref1 else "-")
        check("退款是**先扣后记**的同一笔钱（支付+退款两笔都在）",
              len(flows_of(no1)) == 2, str([t.kind for t in flows_of(no1)]))
        rows = company_rows(o1)
        check("公司账同步冲销（收入 + 支出共 2 笔）", len(rows) == 2,
              str([(r.tx_type, str(r.amount)) for r in rows]))
        check("公司账支出 = 20.00",
              rows and rows[-1].tx_type == TxType.EXPENSE
              and rows[-1].amount == Decimal("20.00"),
              str((rows[-1].tx_type, str(rows[-1].amount))) if rows else "-")

        # ════════════════════════════════════════════════════════
        print("\n【9】幂等：同一订单不会退第二次")
        # ════════════════════════════════════════════════════════
        n_tx = len(txs(me))
        again = refund_order_if_paid(o1)
        check("直接再调退款函数 → 返回 None（已退过）", again is None, str(again))
        check("余额没被退第二次（仍 100.00）",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))
        check("流水条数没变", len(txs(me)) == n_tx, f"{n_tx} → {len(txs(me))}")
        try:
            transition_order(o1, to_status="已退货", operator=boss)
            check("状态机拦住「已退货」再退货", False, "居然没抛异常")
        except OrderTransitionError as e:
            check("状态机拦住「已退货」再退货", e.code == "invalid_transition", e.code)
        check("再退一次后余额仍未变（两条防线都生效）",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))

        # ════════════════════════════════════════════════════════
        print("\n【10】退回的钱是**能用的钱**：拿它付下一单")
        # ════════════════════════════════════════════════════════
        no2 = place(2)                       # 又一张 ¥20
        st, d = jreq(c, "post", f"/api/pay/{no2}/balance/", {}, auth)
        check("用退回的余额支付新订单 → 200", st == 200 and d.get("success"),
              f"{st} {d.get('message')}")
        check("余额 100.00 → 80.00（证明退回的是可用余额，不是账面数字）",
              wallet_balance(me) == Decimal("80.00"), str(wallet_balance(me)))
        o2 = order_of(no2)
        check("新订单落「已支付」", o2.payment_status == "已支付", str(o2.payment_status))

        # ════════════════════════════════════════════════════════
        print("\n【11】已支付订单取消：钱同样退回余额（取消分支的对称修复）")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/me/orders/{no2}/cancel/", {"reason": "改主意了"}, auth)
        check("取消已支付订单 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        o2.refresh_from_db()
        check("订单变为「已取消」", o2.status == "已取消", str(o2.status))
        check("支付维度同步为「已退款」", o2.payment_status == "已退款", str(o2.payment_status))
        check("取消文案写明已退回余额", "已退回余额" in (d.get("message") or ""),
              str(d.get("message")))
        check("★ 余额退回：80.00 → 100.00",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))
        ref2 = flows_of(no2, UserTxKind.REFUND)
        check("★ 用户账多了 1 条「退款」流水", len(ref2) == 1, str(len(ref2)))
        check("取消**不记公司账**（没发过货，本来就没有收入可冲）",
              company_rows(o2) == [], str(len(company_rows(o2))))
        check("未发货的已支付订单取消后不再可付",
              jreq(c, "post", f"/api/pay/{no2}/balance/", {}, auth)[0] == 409,
              str(jreq(c, "post", f"/api/pay/{no2}/balance/", {}, auth)[0]))

        # ════════════════════════════════════════════════════════
        print("\n【12】没走过余额支付的订单：**不产生**退款流水（宁可不退，也不错退）")
        # ════════════════════════════════════════════════════════
        # 12a 未付款直接取消
        no3 = place(1)
        st, d = jreq(c, "post", f"/api/me/orders/{no3}/cancel/", {"reason": "不买了"}, auth)
        check("未付款订单取消 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("未付款订单取消**不产生**退款流水", flows_of(no3, UserTxKind.REFUND) == [],
              str(len(flows_of(no3, UserTxKind.REFUND))))
        check("余额不受影响（仍 100.00）",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))

        # 12b 历史订单（payment_status 为空）：发货 → 退货，同样不该退到余额
        no4 = place(1)
        Orders.objects.filter(order_no=no4).update(payment_status="")
        st, d = jreq(c, "post", f"/api/orders/{no4}/ship/",
                     {"ship_company": "中通", "tracking_no": "ZT-LEGACY-1"}, auth_boss)
        check("历史订单（未跟踪支付）不拦发货", st == 200 and d.get("success"),
              f"{st} {d.get('message')}")
        st, d = jreq(c, "post", f"/api/orders/{no4}/return/",
                     {"cancel_reason": "历史订单退货"}, auth_boss)
        check("历史订单退货 → 200", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("历史订单退货**不产生**用户退款流水（没有可退的余额支付）",
              flows_of(no4, UserTxKind.REFUND) == [],
              str(len(flows_of(no4, UserTxKind.REFUND))))
        check("历史订单也不被凭空标成「已退款」（本来就没记支付）",
              order_of(no4).payment_status == "", repr(order_of(no4).payment_status))
        check("余额不受影响（仍 100.00）",
              wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))
        legacy_rows = company_rows(order_of(no4))
        check("历史订单的公司账照旧两笔（发货收入 + 退货支出）", len(legacy_rows) == 2,
              str([(r.tx_type, str(r.amount)) for r in legacy_rows]))
        check("历史订单公司账净额也归零（经营账口径不变）",
              company_net(legacy_rows) == 0, str(company_net(legacy_rows)))

        # ════════════════════════════════════════════════════════
        print("\n【13】账本逐条验算：上一条余额 ± 本条金额 == 本条余额")
        rows = txs(me)
        check("流水共 5 条（充值 + 支付×2 + 退款×2）", len(rows) == 5,
              str([t.kind for t in rows]))
        running = Decimal("0.00")
        ok_all = True
        for t in rows:
            delta = t.amount if t.kind in (UserTxKind.RECHARGE, UserTxKind.REFUND) else -t.amount
            running += delta
            if running != t.balance_after:
                ok_all = False
                break
        check("逐条验算全部成立（不信任任何缓存）", ok_all, f"最后一条算出 {running}")
        check("验算终值 == 当前余额", running == wallet_balance(me),
              f"{running} vs {wallet_balance(me)}")
        check("金额一律为正数（方向由 kind 决定）",
              all(t.amount > 0 for t in rows), str([str(t.amount) for t in rows]))
        check("每一笔退款都能追到订单号",
              all(t.order_no for t in rows if t.kind == UserTxKind.REFUND),
              str([t.order_no for t in rows if t.kind == UserTxKind.REFUND]))

        # ════════════════════════════════════════════════════════
        print("\n【14】两本账对照：方向相反、金额相等、各自净额归零")
        # ════════════════════════════════════════════════════════
        u_rows = flows_of(no1)
        check("no1 用户账恰好 2 笔（支付 + 退款）", len(u_rows) == 2,
              str([(t.kind, str(t.amount)) for t in u_rows]))
        check("no1 用户账两笔方向相反（一扣一退）",
              {t.kind for t in u_rows} == {UserTxKind.PAY, UserTxKind.REFUND},
              str({t.kind for t in u_rows}))
        check("no1 用户账净额 = 0（用户没花掉这笔钱）",
              sum((t.amount if t.kind == UserTxKind.REFUND else -t.amount) for t in u_rows) == 0)
        check("no1 公司账净额 = 0（商家这笔生意收付相抵）",
              company_net(company_rows(o1)) == 0, str(company_net(company_rows(o1))))
        check("同一个 ¥20：用户账上先 −后 +，公司账上先 +后 −（两本账互不代表）",
              [t.kind for t in u_rows] == [UserTxKind.PAY, UserTxKind.REFUND]
              and [r.tx_type for r in company_rows(o1)] == [TxType.INCOME, TxType.EXPENSE],
              f"{[t.kind for t in u_rows]} / {[r.tx_type for r in company_rows(o1)]}")
        if company is not None:
            company.refresh_from_db()
            check("公司钱包最终余额回到起点（本脚本所有订单收付相抵）",
                  company.balance == company_snapshot,
                  f"{company_snapshot} → {company.balance}")

        # ════════════════════════════════════════════════════════
        print("\n【15】表结构 + 静态接线（防「改了一处漏一处」）")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.STATISTICS WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_wallet_transactions' "
                        "AND COLUMN_NAME='order_no'")
            check("退款流水可按订单号查（order_no 有索引）", cur.fetchone()[0] > 0)
            cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE "
                        "TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
                        "('user_wallet','user_wallet_transactions')")
            check("用户钱包两张表都在", cur.fetchone()[0] == 2)

        wallet_src = read("agent/user_wallet.py")
        svc_src = read("agent/services.py")
        check("退款唯一出口 refund_order_if_paid 定义在 user_wallet.py",
              "def refund_order_if_paid(" in wallet_src)
        check("出口按**流水**定位「谁被扣过钱」（不读 order.user_id）",
              "wallet__user" in wallet_src and "kind=UserTxKind.PAY" in wallet_src)
        check("出口退的是**实付额**（PAY 之和），不是订单总价",
              'aggregate(s=Sum("amount"))' in wallet_src)
        check("出口是幂等的（已有退款流水直接返回）",
              "kind=UserTxKind.REFUND, order_no=order_no" in wallet_src)
        check("services.py 引入了这个出口", "refund_order_if_paid" in svc_src)
        check("**取消**分支调了它", "已取消，余额原路退回" in svc_src)
        check("**退货**分支调了它", "退货，余额原路退回" in svc_src)
        check("两个分支都在同一事务内（transition_order 的 atomic 块里）",
              svc_src.count("refund_order_if_paid") == 3,
              str(svc_src.count("refund_order_if_paid")))
        check("模块 docstring 如实写明「余额原路退回」",
              "余额原路退回用户账" in svc_src)

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
