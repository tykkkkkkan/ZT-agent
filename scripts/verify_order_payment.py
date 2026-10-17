"""
契约验证：模拟支付（任务书五·1）—— ZT-agent

覆盖「支付维度」这一条正交链路，确认它与订单状态机、库存预占、
AI 工具层三处的口径都一致：

  1. 下单 → 新订单默认「未支付」，预占库存立即生效
  2. **未支付不可发货**：`transition_order(SHIPPED)` 必须抛 code=unpaid
     且不能留下任何副作用（状态不变、库存不扣）
  3. 模拟支付接口 `/api/orders/<no>/pay/` 的鉴权与归属（未登录 401 / 他人 404）
  4. 支付成功 → payment_status=已支付 + paid_at 落库；重复支付幂等
  5. 支付后可正常发货（库存扣减 + 预占释放）
  6. **历史订单（payment_status 为空）不被拦截** —— 这是最容易踩的回归点：
     若把默认值写成「未支付」，上线第一次跑批就会把老订单批量取消
  7. AI 工具层能说出"未支付"这个发货受阻的真实原因
  8. 超时未支付自动取消并**释放预占库存**（预演/执行两态）
  9. 迁移 0015 的 DDL 幂等（重复执行不报错）

自造场景 → 断言 → 精确还原：脚本自建临时用户/商品/库存，结束时全部删除，
并把钱包余额恢复原值（发货会触发自动记账，属于真实财务副作用）。

用法：./.venv/Scripts/python.exe scripts/verify_order_payment.py
"""
import importlib.util
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
from django.test import Client, override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from agent.models import (  # noqa: E402
    Inventory,
    Orders,
    OrderStatus,
    PaymentStatus,
    Products,
    Transaction,
    Wallet,
)
from agent.services import (  # noqa: E402
    OrderTransitionError,
    cancel_unpaid_orders,
    mark_order_paid,
    transition_order,
    unpaid_expired_orders,
)
from agent.views import _order_actions  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jpost(c, url, payload, auth):
    r = c.post(url, data=json.dumps(payload), content_type="application/json", **auth)
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def ensure_test_host():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")


def place_order(c, auth, product_id, qty, phone, name="支付验证"):
    st, d = jpost(c, "/api/orders/", {
        "customer_name": name, "phone": phone,
        "address": "山东省济南市历下区测试路 1 号", "product_id": product_id, "quantity": qty,
    }, auth)
    assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
    return d


def main():
    ensure_test_host()
    User = get_user_model()
    suffix = uuid.uuid4().hex[:8]

    # ── 自造场景 ────────────────────────────────────────────────
    me = User.objects.create_user(username=f"paytest_{suffix}", password="PayTest!23456")
    other = User.objects.create_user(username=f"paytest_o_{suffix}", password="PayTest!23456")
    product = Products.objects.create(
        sku=f"PAY{suffix[:5].upper()}", name=f"支付验证饵料{suffix}",
        spec="100g", target_fish="鲫鱼",
        retail_price=10, wholesale_price=6, is_active=True,
    )
    inv = Inventory.objects.create(product=product, stock=100, reserved_stock=0)

    wallet = Wallet.objects.first()
    wallet_snapshot = wallet.balance if wallet else None

    c = Client()
    auth = bearer(me)
    created = []          # 本脚本创建的订单号

    def cleanup():
        """精确还原：先删流水与订单（含外键），再回滚钱包余额，最后删商品/用户。"""
        order_ids = list(Orders.objects.filter(order_no__in=created).values_list("id", flat=True))
        Transaction.objects.filter(order_id__in=order_ids).delete()
        Orders.objects.filter(id__in=order_ids).delete()
        if wallet is not None and wallet_snapshot is not None:
            Wallet.objects.filter(pk=wallet.pk).update(balance=wallet_snapshot)
        Inventory.objects.filter(product=product).delete()
        Products.objects.filter(pk=product.pk).delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户 / 商品 / 库存 / 订单已清理，钱包余额已还原)")

    try:
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            # ══ 1. 下单 → 未支付 ════════════════════════════════
            print("\n[1] 下单后订单处于「未支付」")
            d1 = place_order(c, auth, product.id, 2, "13800000001")
            no1 = d1["order_no"]
            created.append(no1)
            check("下单响应回显 payment_status=未支付", d1.get("payment_status") == "未支付",
                  str(d1.get("payment_status")))
            o1 = Orders.objects.get(order_no=no1)
            check("库中 payment_status = 未支付", o1.payment_status == PaymentStatus.UNPAID)
            check("库中 paid_at 为空", o1.paid_at is None)
            check("预占库存已生效（reserved=2）",
                  Inventory.objects.get(pk=inv.pk).reserved_stock == 2)

            # ══ 2. 未支付不可发货 ════════════════════════════════
            print("\n[2] 未支付不可发货")
            try:
                transition_order(o1, to_status=OrderStatus.SHIPPED,
                                 ship_company="顺丰", tracking_no="SF-PAY-TEST")
                check("未支付时发货被拦截", False, "竟然发货成功了")
            except OrderTransitionError as e:
                check("未支付时发货被拦截（code=unpaid）", e.code == "unpaid", f"code={e.code}")
            o1 = Orders.objects.get(order_no=no1)
            inv_now = Inventory.objects.get(pk=inv.pk)
            check("拦截后订单仍为「未发货」", o1.status == OrderStatus.PENDING)
            check("拦截后库存未被扣减（stock=100）", inv_now.stock == 100, str(inv_now.stock))
            check("拦截后预占未被释放（reserved=2）", inv_now.reserved_stock == 2)
            check("未支付时会下发 pay 操作", "pay" in _order_actions(o1))

            # ══ 3. 支付接口的鉴权与归属 ══════════════════════════
            print("\n[3] 模拟支付接口的鉴权与归属")
            st, _ = jpost(c, f"/api/orders/{no1}/pay/", {}, {})
            check("未登录支付被拒（401）", st == 401, f"HTTP {st}")
            st, _ = jpost(c, f"/api/orders/{no1}/pay/", {}, bearer(other))
            check("他人订单不能代付（404，不泄露存在性）", st == 404, f"HTTP {st}")
            st, _ = jpost(c, "/api/orders/DD00000000000000/pay/", {}, auth)
            check("不存在的订单号同样 404", st == 404, f"HTTP {st}")

            # ══ 4. 支付成功 + 幂等 ═══════════════════════════════
            print("\n[4] 模拟支付成功且幂等")
            st, d = jpost(c, f"/api/orders/{no1}/pay/", {}, auth)
            check("支付成功（200 + success）", st == 200 and d.get("success"), f"HTTP {st} {d}")
            o1 = Orders.objects.get(order_no=no1)
            check("payment_status = 已支付", o1.payment_status == PaymentStatus.PAID)
            check("paid_at 已落库", o1.paid_at is not None)
            check("支付响应回传订单支付状态",
                  (d.get("order") or {}).get("payment_status") == "已支付")
            check("支付后不再下发 pay 操作", "pay" not in _order_actions(o1))
            first_paid_at = o1.paid_at

            st, d = jpost(c, f"/api/orders/{no1}/pay/", {}, auth)
            check("重复支付仍返回 200（幂等，不报错）", st == 200 and d.get("success"))
            check("重复支付不刷新支付时间",
                  Orders.objects.get(order_no=no1).paid_at == first_paid_at)

            # ══ 4b. 个人中心的支付入口 ═══════════════════════════
            # 前端有两处支付按钮：下单页走 /api/orders/<no>/pay/（普通视图，
            # session + JWT 双通道），个人中心走 /api/me/orders/<no>/<action>/
            # （DRF 视图，只认 JWT）。两条通道都必须能付，否则"个人中心付不了款"。
            print("\n[4b] 个人中心的支付入口（DRF / JWT 通道）")
            d5 = place_order(c, auth, product.id, 1, "13800000009")
            no5 = d5["order_no"]
            created.append(no5)
            st, d = jpost(c, f"/api/me/orders/{no5}/pay/", {}, auth)
            check("个人中心 pay 动作可用（200）", st == 200 and d.get("success"),
                  f"HTTP {st} {d.get('message')}")
            check("个人中心支付后 payment_status=已支付",
                  Orders.objects.get(order_no=no5).payment_status == PaymentStatus.PAID)
            st, _ = jpost(c, f"/api/me/orders/{no5}/pay/", {}, bearer(other))
            check("他人不能通过个人中心接口代付（404）", st == 404, f"HTTP {st}")
            # 该单已支付，留在库中不影响后续（后续超时任务只挑未支付）

            # ══ 5. 支付后可发货 ═════════════════════════════════
            print("\n[5] 支付后可以正常发货")
            inv_before = Inventory.objects.get(pk=inv.pk)
            res = transition_order(Orders.objects.get(order_no=no1),
                                   to_status=OrderStatus.SHIPPED,
                                   ship_company="顺丰", tracking_no="SF-PAY-OK")
            check("支付后发货成功", res.order.status == OrderStatus.SHIPPED)
            inv_now = Inventory.objects.get(pk=inv.pk)
            check("发货后 stock 扣减 2", inv_now.stock == inv_before.stock - 2,
                  f"{inv_before.stock} → {inv_now.stock}")
            check("发货后预占释放 2", inv_now.reserved_stock == inv_before.reserved_stock - 2,
                  f"{inv_before.reserved_stock} → {inv_now.reserved_stock}")

            # ══ 6. 历史订单不被牵连 ═════════════════════════════
            print("\n[6] 历史订单（支付状态为空）不受新规则约束")
            d2 = place_order(c, auth, product.id, 1, "13800000002")
            no2 = d2["order_no"]
            created.append(no2)
            # 模拟"支付功能上线前的老订单"：清空支付状态
            Orders.objects.filter(order_no=no2).update(payment_status="")
            o2 = Orders.objects.get(order_no=no2)
            check("历史订单不发 pay 操作", "pay" not in _order_actions(o2))
            res2 = transition_order(o2, to_status=OrderStatus.SHIPPED,
                                    ship_company="顺丰", tracking_no="SF-LEGACY-OK")
            check("历史订单可直接发货（不被未支付闸门拦下）",
                  res2.order.status == OrderStatus.SHIPPED)

            # ══ 7. AI 工具层口径 ════════════════════════════════
            print("\n[7] AI 工具层能说出真实原因")
            from agent import tools
            d3 = place_order(c, auth, product.id, 1, "13800000003")
            no3 = d3["order_no"]
            created.append(no3)
            txt = tools.get_order_status(no3)
            check("未支付订单：工具输出含「未支付」", "未支付" in txt)
            check("未支付订单：工具说明付款后才会发货", "支付" in txt and "发货" in txt)
            tools.set_current_user(me)
            mine = tools.query_my_orders(limit=5)
            check("「我的订单」工具能列出该订单", no3 in mine)
            check("「我的订单」工具标注了未支付", "未支付" in mine)
            tools.set_current_user(None)

            st, _ = jpost(c, f"/api/orders/{no3}/pay/", {}, auth)
            txt2 = tools.get_order_status(no3)
            check("支付后：工具输出含「已支付」", "已支付" in txt2)

            # ══ 8. 超时未支付自动取消 ═══════════════════════════
            print("\n[8] 超时未支付自动取消并释放预占")
            d4 = place_order(c, auth, product.id, 3, "13800000004")
            no4 = d4["order_no"]
            created.append(no4)
            reserved_before = Inventory.objects.get(pk=inv.pk).reserved_stock

            r0 = cancel_unpaid_orders(30, dry_run=True)
            check("刚下单的订单不在超时范围内", no4 not in r0["cancelled"])

            # 把下单时间往前拨 1 小时，模拟"超时"
            Orders.objects.filter(order_no=no4).update(
                created_at=timezone.now() - timedelta(hours=1))
            r1 = cancel_unpaid_orders(30, dry_run=True)
            check("预演能命中超时未支付订单", no4 in r1["cancelled"])
            check("预演不改动任何数据",
                  Orders.objects.get(order_no=no4).status == OrderStatus.PENDING)
            check("预演不释放预占",
                  Inventory.objects.get(pk=inv.pk).reserved_stock == reserved_before)

            outsiders = [o.order_no for o in unpaid_expired_orders(30)
                         if o.order_no not in created]
            if outsiders:
                check("跳过写入测试（库中存在非本脚本的超时未支付订单）", False, str(outsiders))
            else:
                r2 = cancel_unpaid_orders(30, dry_run=False)
                o4 = Orders.objects.get(order_no=no4)
                check("执行后订单变为「已取消」", o4.status == OrderStatus.CANCELLED)
                check("超时取消写入了取消原因", "超时未支付" in (o4.cancel_reason or ""),
                      o4.cancel_reason)
                check("超时取消释放了预占（reserved -3）",
                      Inventory.objects.get(pk=inv.pk).reserved_stock == reserved_before - 3,
                      f"{reserved_before} → {Inventory.objects.get(pk=inv.pk).reserved_stock}")
                check("已支付订单不被超时任务牵连",
                      no3 not in r2["cancelled"] and no3 not in r2["skipped"])
                check("已发货订单不被超时任务牵连",
                      no1 not in r2["cancelled"] and no2 not in r2["cancelled"])
                # 已取消的订单不能再支付
                st_pay, d_pay = jpost(c, f"/api/orders/{no4}/pay/", {}, auth)
                check("已取消订单不能支付（400）", st_pay == 400, f"HTTP {st_pay} {d_pay}")

            # ══ 9. 后台可见支付状态 ═════════════════════════════
            print("\n[9] 后台订单列表展示支付状态")
            su = User.objects.filter(is_superuser=True).first()
            if su:
                c_admin = Client()
                c_admin.force_login(su)
                html = c_admin.get("/admin/agent/orders/").content.decode("utf-8", "ignore")
                check("订单列表出现「支付状态」列", "支付状态" in html)
            else:
                check("存在超管用于后台检查", False, "库中没有 is_superuser 用户")

        # ══ 10. 迁移 DDL 幂等 ══════════════════════════════════
        print("\n[10] 迁移 0015 的 DDL 幂等")
        spec = importlib.util.spec_from_file_location(
            "mig0015", os.path.join(BASE, "agent", "migrations", "0015_orders_payment_fields.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class _SE:
            connection = connection

        try:
            mod.forwards(None, _SE())          # 列/索引都已存在 → 应直接跳过
            check("重复执行 forwards 不报错（幂等）", True)
        except Exception as e:  # noqa: BLE001
            check("重复执行 forwards 不报错（幂等）", False, f"{type(e).__name__}: {e}")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
                "AND COLUMN_NAME IN ('payment_status','paid_at')")
            cols = {r[0] for r in cur.fetchall()}
        check("orders 表确有 payment_status / paid_at 两列",
              cols == {"payment_status", "paid_at"}, str(cols))

    finally:
        cleanup()

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
