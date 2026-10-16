"""
契约验证：模拟收银台（任务书五·1）—— ZT-agent

覆盖「扫码支付」这条闭环的**每一段**与它周围的口径：

  1. 表结构：`pay_tickets` 存在；`token` UNIQUE（防重放的基础）；
     `(order_no, status)` 复合索引；外键指向 auth_user
  2. 鉴权边界：PC 端两个接口未登录 401；**手机端两个接口不要求登录**
     （token 即授权）—— 这条边界一旦被"顺手加上登录"就会让扫码的人白跑一趟
  3. 开票：凭据字段正确（64 位内随机 token / 待支付 / 金额快照 / 到期时间）
  4. **复用**：同一订单重复开票拿到同一个 token（刷新不该刷出一堆码）
  5. 归属：他人订单 404（不泄露存在性）；订单不存在 404
  6. 订单不可付款的四种状态各自给出 409 + 可读原因
     （已支付 / 已退款 / 历史订单 / 已取消已退货 / 发货后未支付）
  7. 二维码：SVG 非空；**可扫地址绝不能是 localhost**
     （PC 上是 localhost，编进二维码手机扫出来只会打开手机自己的 localhost）
  8. 缺依赖降级：`qrcode` 不可用时接口照常 200、仍给出可复制的明文链接
  9. 手机端摘要**脱敏**：手机号中间四位打码、姓名只留姓、地址截断
 10. 核销：订单落「已支付」+ `paid_at`；凭据落「已支付」+ `redeemed_at`；
     **同一订单的其他在途票立刻作废**（屏幕上不该还挂着第二张能付的码）
 11. **防重放**：同一 token 再核销 → 幂等返回，订单与凭据都不再变化
 12. 过期：过期票被标「已过期」并**留在库里**（异常也要有据可查），订单仍未支付
 13. 作废票：返回 cancelled，订单仍未支付
 14. 轮询：手机确认后 PC 轮询接口立刻看到 paid=True
 15. 边界：订单状态机不动（付完仍是「未发货」）、库存不动、
     **不写公司钱包**（发货才记收入，收银台不该动经营账）

自造场景 → 断言 → 精确还原：脚本自建临时用户 / 商品 / 库存，通过真实下单接口
产生订单，结束时删掉订单、凭据、商品、用户，并把钱包余额还原。

用法：./.venv/Scripts/python.exe scripts/verify_cashier.py
"""
import json
import os
import sys
import uuid
from datetime import timedelta

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from agent.cashier import (  # noqa: E402
    CashierError,
    create_ticket,
    expire_stale_tickets,
    pay_url,
    qr_svg,
    reachable_base_url,
)
from agent.models import (  # noqa: E402
    Inventory,
    Orders,
    OrderStatus,
    PayChannel,
    PaymentStatus,
    PayTicket,
    PayTicketStatus,
    Products,
    Transaction,
    Wallet,
)

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


def main():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")

    User = get_user_model()
    sfx = uuid.uuid4().hex[:8]
    me = User.objects.create_user(username=f"cash_{sfx}", password="Cashier!23456")
    other = User.objects.create_user(username=f"cash_o_{sfx}", password="Cashier!23456")
    product = Products.objects.create(
        sku=f"CASH{sfx[:5].upper()}", name=f"收银台验证饵料{sfx}", spec="100g",
        target_fish="鲫鱼", retail_price=10, wholesale_price=6, is_active=True,
    )
    inv = Inventory.objects.create(product=product, stock=100, reserved_stock=0)

    wallet = Wallet.objects.first()
    wallet_snapshot = wallet.balance if wallet else None

    c = Client()
    auth = bearer(me)
    auth_other = bearer(other)
    made_orders = []

    def place(qty, phone="13800000001", name="收银验证"):
        st, d = jreq(c, "post", "/api/orders/", {
            "customer_name": name, "phone": phone,
            "address": "山东省济南市历下区测试路 1 号",
            "product_id": product.id, "quantity": qty,
        }, auth)
        assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
        made_orders.append(d["order_no"])
        return d["order_no"]

    def get_order(no):
        return Orders.objects.filter(order_no=no).first()

    def cleanup():
        oids = list(Orders.objects.filter(order_no__in=made_orders).values_list("id", flat=True))
        PayTicket.objects.filter(order_no__in=made_orders).delete()
        Transaction.objects.filter(order_id__in=oids).delete()
        Orders.objects.filter(id__in=oids).delete()
        if wallet is not None and wallet_snapshot is not None:
            Wallet.objects.filter(pk=wallet.pk).update(balance=wallet_snapshot)
        Inventory.objects.filter(product=product).delete()
        Products.objects.filter(pk=product.pk).delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户 / 商品 / 库存 / 订单 / 凭据已清理，钱包余额已还原)")

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】表结构：pay_tickets / token 唯一 / 复合索引 / 外键")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute("SELECT TABLE_NAME FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='pay_tickets'")
            check("pay_tickets 表存在", cur.fetchone() is not None)
            cur.execute("SELECT NON_UNIQUE FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='pay_tickets' "
                        "AND COLUMN_NAME='token'")
            rows = cur.fetchall()
            check("token 有唯一约束（防重放的基础）", any(r[0] == 0 for r in rows), str(rows))
            cur.execute("SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                        "AND TABLE_NAME='pay_tickets' AND INDEX_NAME='idx_pay_order_status'")
            r = cur.fetchone()
            check("(order_no, status) 复合索引存在", r and r[0] == "order_no,status", str(r))
            cur.execute(
                "SELECT k.COLUMN_NAME, k.REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE k "
                "WHERE k.TABLE_SCHEMA=DATABASE() AND k.TABLE_NAME='pay_tickets' "
                "AND k.REFERENCED_TABLE_NAME IS NOT NULL")
            fks = dict(cur.fetchall())
            check("user_id / redeemed_by_id → auth_user 外键存在",
                  fks.get("user_id") == "auth_user" and fks.get("redeemed_by_id") == "auth_user",
                  str(fks))

        # ════════════════════════════════════════════════════════
        print("\n【2】鉴权边界：PC 端要登录，手机端不要")
        # ════════════════════════════════════════════════════════
        st, _ = jreq(c, "post", "/api/pay/ZT-NOPE/ticket/")
        check("未登录开票 → 401", st == 401, f"实际 {st}")
        st, _ = jreq(c, "get", "/api/pay/ZT-NOPE/status/")
        check("未登录轮询 → 401", st == 401, f"实际 {st}")

        no1 = place(2, "13800000001")
        st, body = jreq(c, "post", f"/api/pay/{no1}/ticket/", auth=auth)
        check("登录后可开票（200）", st == 200, f"{st} {body.get('message')}")
        tok = (body.get("ticket") or {}).get("token", "")

        st, _ = jreq(c, "get", f"/api/pay/ticket/{tok}/")
        check("手机端读凭据**不要求登录**（200）", st == 200, f"实际 {st}")
        # 关键区别：不登录取一个不存在的凭据，得到的应是 404（凭据本身无效）
        # 而不是 401（要求登录）。用无效 token 探边界，避免把真凭据用掉。
        st, _ = jreq(c, "post", "/api/pay/confirm/not-a-real-token/")
        check("手机端确认付款不要求登录（无效凭据是 404，不是 401）", st == 404, f"实际 {st}")

        # ════════════════════════════════════════════════════════
        print("\n【3】开票字段")
        # ════════════════════════════════════════════════════════
        t = body.get("ticket") or {}
        check("凭据状态为「待支付」", t.get("status") == "待支付", str(t.get("status")))
        check("付款方式默认「扫码支付」", t.get("channel") == "扫码支付", str(t.get("channel")))
        check("token 长度 40~64（token_urlsafe(32)）", 40 <= len(tok) <= 64, str(len(tok)))
        check("金额是订单总价（2 × ¥10 = 20.00）", t.get("amount") == "20.00", str(t.get("amount")))
        check("剩余有效期 > 0 且 ≤ 300 秒", 0 < t.get("remain_seconds", 0) <= 300, str(t.get("remain_seconds")))
        check("凭据是可用的（open=True）", t.get("open") is True)
        check("返回里带了订单摘要", (body.get("order") or {}).get("order_no") == no1)
        check("顶级 amount 与凭据金额一致", body.get("amount") == "20.00", str(body.get("amount")))

        # ════════════════════════════════════════════════════════
        print("\n【4】复用：同一订单重复开票拿到同一个 token")
        # ════════════════════════════════════════════════════════
        st, body2 = jreq(c, "post", f"/api/pay/{no1}/ticket/", auth=auth)
        check("第二次开票也是 200", st == 200)
        check("**拿到同一个 token**（刷新不该刷出一堆码）",
              (body2.get("ticket") or {}).get("token") == tok, str((body2.get("ticket") or {}).get("token"))[:12])
        check("库里这个订单只有 1 张在途票",
              PayTicket.objects.filter(order_no=no1, status=PayTicketStatus.PENDING).count() == 1,
              str(PayTicket.objects.filter(order_no=no1).count()))

        # ════════════════════════════════════════════════════════
        print("\n【5】归属与不存在")
        # ════════════════════════════════════════════════════════
        st, body = jreq(c, "post", f"/api/pay/{no1}/ticket/", auth=auth_other)
        check("他人订单开票 → 404（不泄露存在性）", st == 404, f"实际 {st}")
        st, _ = jreq(c, "get", f"/api/pay/{no1}/status/", auth=auth_other)
        check("他人订单轮询 → 404", st == 404, f"实际 {st}")
        st, _ = jreq(c, "post", "/api/pay/ZT-00000000000000/ticket/", auth=auth)
        check("订单号不存在 → 404", st == 404, f"实际 {st}")

        # ════════════════════════════════════════════════════════
        print("\n【6】不可付款的订单：各给 409 + 可读原因")
        # ════════════════════════════════════════════════════════
        no_paid = place(1, "13800000002")
        Orders.objects.filter(order_no=no_paid).update(payment_status=PaymentStatus.PAID)
        st, body = jreq(c, "post", f"/api/pay/{no_paid}/ticket/", auth=auth)
        check("已支付 → 409", st == 409, f"实际 {st}")
        check("已支付给出可读原因", body.get("code") == "paid" and "已支付" in (body.get("message") or ""),
              f"{body.get('code')} / {body.get('message')}")

        no_refund = place(1, "13800000003")
        Orders.objects.filter(order_no=no_refund).update(payment_status=PaymentStatus.REFUNDED)
        st, body = jreq(c, "post", f"/api/pay/{no_refund}/ticket/", auth=auth)
        check("已退款 → 409 + code=refunded", st == 409 and body.get("code") == "refunded",
              f"{st} {body.get('code')}")

        no_legacy = place(1, "13800000004")
        Orders.objects.filter(order_no=no_legacy).update(payment_status="")
        st, body = jreq(c, "post", f"/api/pay/{no_legacy}/ticket/", auth=auth)
        check("历史订单（payment_status 为空）→ 409 + code=settled",
              st == 409 and body.get("code") == "settled", f"{st} {body.get('code')}")

        no_cancel = place(1, "13800000005")
        Orders.objects.filter(order_no=no_cancel).update(status=OrderStatus.CANCELLED)
        st, body = jreq(c, "post", f"/api/pay/{no_cancel}/ticket/", auth=auth)
        check("已取消 → 409 + code=closed", st == 409 and body.get("code") == "closed",
              f"{st} {body.get('code')}")

        no_ship = place(1, "13800000006")
        Orders.objects.filter(order_no=no_ship).update(status=OrderStatus.SHIPPED)
        st, body = jreq(c, "post", f"/api/pay/{no_ship}/ticket/", auth=auth)
        check("已发货却未支付 → 409 + code=abnormal（交人工核对，不在这里补一笔）",
              st == 409 and body.get("code") == "abnormal", f"{st} {body.get('code')}")

        # ════════════════════════════════════════════════════════
        print("\n【7】二维码与可扫地址")
        # ════════════════════════════════════════════════════════
        url = body2.get("pay_url") or ""
        svg = body2.get("qr_svg") or ""
        check("二维码 SVG 非空", bool(svg) and "<svg" in svg, f"{len(svg)} 字符")
        check("SVG 是矢量的（有 viewBox）", "viewBox" in svg)
        check("可扫地址里带 token", tok in url, url)
        check("可扫地址指向确认页", "/pay/confirm/" in url, url)
        host = url.split("//")[-1].split("/")[0].split(":")[0]
        check("**可扫地址不是 localhost**（否则手机扫开的是它自己）",
              host not in ("localhost", "127.0.0.1", "0.0.0.0"), host)
        check("可扫地址是 http(s)://", url.startswith("http://") or url.startswith("https://"), url)

        # 环境变量覆盖（部署到公网/内网域名时用）
        os.environ["PUBLIC_BASE_URL"] = "https://fish.example.com/"
        check("PUBLIC_BASE_URL 优先且去掉尾斜杠",
              reachable_base_url(None) == "https://fish.example.com", reachable_base_url(None))
        del os.environ["PUBLIC_BASE_URL"]

        # ════════════════════════════════════════════════════════
        print("\n【8】依赖降级：qrcode 不在也能走完流程")
        # ════════════════════════════════════════════════════════
        saved = sys.modules.get("qrcode", "__absent__")
        sys.modules["qrcode"] = None          # 让 import 失败
        try:
            check("缺 qrcode 时 qr_svg 退化为空串（不抛异常）", qr_svg("http://x/") == "")
        finally:
            if saved == "__absent__":
                sys.modules.pop("qrcode", None)
            else:
                sys.modules["qrcode"] = saved
        check("恢复后二维码又能生成", "<svg" in qr_svg("http://x/"))
        check("空文本不生成二维码", qr_svg("") == "")

        # ════════════════════════════════════════════════════════
        print("\n【9】手机端摘要脱敏")
        # ════════════════════════════════════════════════════════
        no_m = place(1, "13912345678", name="汤伊轲")
        st, body = jreq(c, "post", f"/api/pay/{no_m}/ticket/", auth=auth)
        tok_m = (body.get("ticket") or {}).get("token")
        check("PC 端摘要**不**脱敏（自己看自己的订单）",
              (body.get("order") or {}).get("phone") == "13912345678",
              str((body.get("order") or {}).get("phone")))

        st, body = jreq(c, "get", f"/api/pay/ticket/{tok_m}/")
        o = body.get("order") or {}
        check("手机端读凭据 200", st == 200, str(st))
        check("手机端手机号中间四位打码", o.get("phone") == "139****5678", str(o.get("phone")))
        check("手机端姓名只留姓", o.get("customer_name") == "汤**", str(o.get("customer_name")))
        check("手机端地址被截断",
              len(o.get("address") or "") <= 8 and (o.get("address") or "").endswith("…"),
              str(o.get("address")))
        check("手机端响应里没有出现完整手机号", "13912345678" not in json.dumps(body, ensure_ascii=False))

        # ════════════════════════════════════════════════════════
        print("\n【10】核销：订单落已支付 + 凭据留痕 + 兄弟票作废")
        # ════════════════════════════════════════════════════════
        no_ok = place(3, "13800000007")
        st, body = jreq(c, "post", f"/api/pay/{no_ok}/ticket/", auth=auth)
        tok_ok = (body.get("ticket") or {}).get("token")
        # 手工再插一张在途票，用来验证"兄弟票被作废"
        sibling = PayTicket.objects.create(
            order_no=no_ok, user=me, token="sibling-" + sfx, status=PayTicketStatus.PENDING,
            channel=PayChannel.QR, amount=get_order(no_ok).total_price,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        inv_before = Inventory.objects.get(pk=inv.pk)
        res_before = (inv_before.stock, inv_before.reserved_stock)
        co_before = (Wallet.get_solo().balance, Transaction.objects.count())

        st, body = jreq(c, "post", f"/api/pay/confirm/{tok_ok}/")
        check("手机端确认付款 → 200", st == 200, f"{st} {body.get('message')}")
        check("返回 ok 且不是 already", body.get("success") is True and not body.get("already"))

        o = get_order(no_ok)
        check("订单 payment_status = 已支付", o.payment_status == PaymentStatus.PAID, o.payment_status)
        check("paid_at 已落库", o.paid_at is not None, str(o.paid_at))
        check("订单状态仍是「未发货」（支付与发货是正交的两个维度）",
              o.status == OrderStatus.PENDING, o.status)

        tk = PayTicket.objects.get(token=tok_ok)
        check("凭据状态 = 已支付", tk.status == PayTicketStatus.PAID, tk.status)
        check("凭据 redeemed_at 已落库", tk.redeemed_at is not None)
        check("未登录核销时 redeemed_by 为空（如实记录，不硬塞一个账号）",
              tk.redeemed_by_id is None, str(tk.redeemed_by_id))
        sibling.refresh_from_db()
        check("同订单的另一张在途票已被作废（屏幕上不该还挂着一张能付的码）",
              sibling.status == PayTicketStatus.CANCELLED, sibling.status)

        inv_now = Inventory.objects.get(pk=inv.pk)
        check("库存与预占都没有被动过（收银台不碰货）",
              (inv_now.stock, inv_now.reserved_stock) == res_before, f"{(inv_now.stock, inv_now.reserved_stock)} vs {res_before}")
        co_after = (Wallet.get_solo().balance, Transaction.objects.count())
        check("**没有写公司钱包**（发货才记收入，收银台不该动经营账）",
              co_after == co_before, f"{co_before} → {co_after}")

        # ════════════════════════════════════════════════════════
        print("\n【11】防重放")
        # ════════════════════════════════════════════════════════
        paid_at_1 = get_order(no_ok).paid_at
        st, body = jreq(c, "post", f"/api/pay/confirm/{tok_ok}/")
        check("同一 token 再核销 → 幂等成功（不报错）", st == 200 and body.get("success") is True,
              f"{st} {body.get('message')}")
        check("标记为 already（前端据此显示「这笔已支付」）", body.get("already") is True)
        check("paid_at 没有被刷新（连点两次不该出现两个支付时间）",
              get_order(no_ok).paid_at == paid_at_1, str(get_order(no_ok).paid_at))
        check("凭据仍只有一条，没有重复核销的痕迹",
              PayTicket.objects.filter(token=tok_ok).count() == 1)

        st, body = jreq(c, "post", f"/api/pay/{no_ok}/ticket/", auth=auth)
        check("已支付的订单再开票 → 409（前端据此直接切成功态）",
              st == 409 and body.get("code") == "paid", f"{st} {body.get('code')}")

        # ════════════════════════════════════════════════════════
        print("\n【12】过期票：留下痕迹，订单不动")
        # ════════════════════════════════════════════════════════
        no_exp = place(1, "13800000008")
        st, body = jreq(c, "post", f"/api/pay/{no_exp}/ticket/", auth=auth)
        tok_exp = (body.get("ticket") or {}).get("token")
        PayTicket.objects.filter(token=tok_exp).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        st, body = jreq(c, "post", f"/api/pay/confirm/{tok_exp}/")
        check("过期票核销 → 4xx + code=expired", st >= 400 and body.get("code") == "expired",
              f"{st} {body.get('code')}")
        check("过期票被标记为「已过期」并留在库里（异常也要有据可查）",
              PayTicket.objects.get(token=tok_exp).status == PayTicketStatus.EXPIRED,
              PayTicket.objects.get(token=tok_exp).status)
        check("过期核销**没有**把订单标成已支付",
              get_order(no_exp).payment_status == PaymentStatus.UNPAID,
              get_order(no_exp).payment_status)
        check("过期的凭据不会变成可用的（open=False）",
              not PayTicket.objects.get(token=tok_exp).is_open)

        # 惰性清理
        no_stale = place(1, "13800000009")
        stale = PayTicket.objects.create(
            order_no=no_stale, user=me, token="stale-" + sfx, status=PayTicketStatus.PENDING,
            channel=PayChannel.QR, amount=10, expires_at=timezone.now() - timedelta(seconds=5),
        )
        n = expire_stale_tickets(no_stale)
        stale.refresh_from_db()
        check("expire_stale_tickets 把过期待支付票标掉", n >= 1 and stale.status == PayTicketStatus.EXPIRED,
              f"{n} 条 / {stale.status}")

        # ════════════════════════════════════════════════════════
        print("\n【13】作废票与无效 token")
        # ════════════════════════════════════════════════════════
        no_c = place(1, "13800000010")
        st, body = jreq(c, "post", f"/api/pay/{no_c}/ticket/", auth=auth)
        tok_c = (body.get("ticket") or {}).get("token")
        PayTicket.objects.filter(token=tok_c).update(status=PayTicketStatus.CANCELLED)
        st, body = jreq(c, "post", f"/api/pay/confirm/{tok_c}/")
        check("作废票核销 → 4xx + code=cancelled", st >= 400 and body.get("code") == "cancelled",
              f"{st} {body.get('code')}")
        check("作废票不会把订单标成已支付",
              get_order(no_c).payment_status == PaymentStatus.UNPAID, get_order(no_c).payment_status)

        st, body = jreq(c, "post", "/api/pay/confirm/definitely-not-a-token/")
        check("无效 token → 404", st == 404, f"{st}")
        st, body = jreq(c, "get", "/api/pay/ticket/definitely-not-a-token/")
        check("无效 token 读凭据 → 404", st == 404, f"{st}")

        # ════════════════════════════════════════════════════════
        print("\n【14】PC 端轮询")
        # ════════════════════════════════════════════════════════
        no_poll = place(1, "13800000011")
        st, body = jreq(c, "post", f"/api/pay/{no_poll}/ticket/", auth=auth)
        tok_poll = (body.get("ticket") or {}).get("token")
        st, body = jreq(c, "get", f"/api/pay/{no_poll}/status/", auth=auth)
        check("未支付时轮询 paid=False", st == 200 and body.get("paid") is False,
              f"{st} {body.get('paid')} / {body.get('pay_state')}")
        check("轮询带回凭据状态（前端据此画倒计时）",
              (body.get("ticket") or {}).get("status") == "待支付")
        jreq(c, "post", f"/api/pay/confirm/{tok_poll}/")
        st, body = jreq(c, "get", f"/api/pay/{no_poll}/status/", auth=auth)
        check("手机确认后 PC 轮询立刻看到 paid=True（不用手动刷新）",
              body.get("paid") is True and body.get("pay_state") == "paid",
              f"{body.get('paid')} / {body.get('pay_state')}")
        check("轮询也能反映凭据已核销",
              (body.get("ticket") or {}).get("status") == "已支付")

        # ════════════════════════════════════════════════════════
        print("\n【15】金额快照：票上写的就该是多少")
        # ════════════════════════════════════════════════════════
        no_snap = place(2, "13800000012")
        st, body = jreq(c, "post", f"/api/pay/{no_snap}/ticket/", auth=auth)
        tk_snap = PayTicket.objects.get(token=(body.get("ticket") or {}).get("token"))
        check("开票时金额 = 20.00", str(tk_snap.amount) == "20.00", str(tk_snap.amount))
        Orders.objects.filter(order_no=no_snap).update(total_price=5)
        tk_snap.refresh_from_db()
        check("订单金额被改动后，凭据上仍是开票时的 20.00（用户看到多少就该按多少算）",
              str(tk_snap.amount) == "20.00", str(tk_snap.amount))
        check("凭据金额与订单当前金额确实不同（证明读的是快照而非实时值）",
              str(tk_snap.amount) != str(get_order(no_snap).total_price),
              f"{tk_snap.amount} vs {get_order(no_snap).total_price}")

        # ════════════════════════════════════════════════════════
        print("\n【16】业务层直接调用：create_ticket 的守卫")
        # ════════════════════════════════════════════════════════
        try:
            create_ticket(None, me)
            check("create_ticket(None) 抛 CashierError", False, "没有抛异常")
        except CashierError as e:
            check("create_ticket(None) 抛 CashierError", e.code == "not_found", e.code)

        no_zero = place(1, "13800000013")
        Orders.objects.filter(order_no=no_zero).update(total_price=0)
        try:
            create_ticket(get_order(no_zero), me)
            check("金额为 0 的订单拒绝开票", False, "没有抛异常")
        except CashierError as e:
            check("金额为 0 的订单拒绝开票", e.code == "zero_amount", e.code)

        check("pay_url 与 qr_svg 是纯函数（同一输入同一输出）",
              pay_url(None, tk_snap) == pay_url(None, tk_snap)
              and qr_svg("abc") == qr_svg("abc"))
        check("不同内容的二维码不同", qr_svg("http://a/") != qr_svg("http://b/"))

    finally:
        cleanup()

    print("\n" + "=" * 62)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"    ✗ {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
