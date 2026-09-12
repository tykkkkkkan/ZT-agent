"""
身份解析一致性验证（_resolve_request_user）

用户报告的两个现象：① 下单后「我的」看不到订单（后台管理员能看到）
                    ② 查订单时手机号明明和「我的」里一致，却查不出来

根因是同一个：**同一浏览器里登录过 /admin/ 时，后端把不同接口解析成了不同身份。**

  · `/api/me/*` 走 DRF + JWTAuthentication → 认 **JWT 里的 C 端用户**
  · `/api/orders/*`、`/api/agent/*` 走 `_resolve_request_user` → 原先**优先读 session**
    → 浏览器里的管理员 session 赢了 → 被当成管理员

后果（均已实测）：
  · 下单：`Orders.user_id` 写成管理员的 id → C 端「我的」按 user_id 找不到，
    手机号兜底又被「user_id IS NULL」条件排除 → 订单在前端"消失"
  · 查订单：`my_phone = get_profile(管理员).phone`（空字符串）与用户输入的真实手机号
    比对必然不等 → 403

修复原则：**显式携带的 JWT 优先于 session**。前端每次请求都带
`Authorization: Bearer <token>`，这是调用方"声明的身份"；session cookie 是环境性的，
可能来自同浏览器里另一个登录（管理员）。

用法：./.venv/Scripts/python.exe scripts/verify_identity_resolution.py
"""
import json
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model                 # noqa: E402
from django.test import Client, override_settings              # noqa: E402

from agent.models import Orders, OrderStatus, UserProfile      # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def main():
    User = get_user_model()
    admin = User.objects.filter(is_superuser=True).first()
    # 挑一个「非 staff 且已绑定手机号、且有可见订单」的真实用户
    cust = None
    for prof in UserProfile.objects.exclude(phone="").select_related("user"):
        if prof.user.is_staff:
            continue
        if Orders.objects.filter(user_id=prof.user_id).exists() or \
           Orders.objects.filter(phone=prof.phone, user__isnull=True).exists():
            cust = prof.user
            cust_phone = prof.phone
            break
    if cust is None:
        print("库中没有「非 staff + 已绑手机号 + 有订单」的账号，无法验证")
        return 1

    print(f"管理员：{admin.username} (id={admin.id})，其资料手机号="
          f"{UserProfile.objects.filter(user=admin).values_list('phone', flat=True).first()!r}")
    print(f"C 端账号：{cust.username} (id={cust.id})，绑定手机号={cust_phone}")

    with override_settings(ALLOWED_HOSTS=["testserver"]):
        # ── ① 身份优先级：session 是管理员 + JWT 是 C 端用户 ────────
        print("\n① 同一请求同时带「管理员 session」与「C 端用户 JWT」")
        c = Client()
        c.force_login(admin)
        auth = bearer(cust)

        r = c.get("/api/auth/me/", **auth)
        me = json.loads(r.content) if r.status_code == 200 else {}
        check("DRF 接口（/api/auth/me/）解析为 C 端用户",
              me.get("username") == cust.username, f"HTTP {r.status_code} user={me.get('username')}")

        # 这是关键断言：普通视图必须与 DRF 给出同一身份
        r2 = c.post("/api/orders/query/", data=json.dumps({"phone": cust_phone}),
                    content_type="application/json", **auth)
        d2 = json.loads(r2.content)
        check("查订单解析为同一 C 端用户（不再被管理员 session 顶掉）",
              r2.status_code == 200,
              f"HTTP {r2.status_code} code={d2.get('code')} msg={(d2.get('message') or '')[:34]}")
        if r2.status_code == 200:
            expect = Orders.objects.filter(
                __import__("agent.me_helpers", fromlist=["my_orders_q"]).my_orders_q(cust)
            ).count()
            check("查订单返回的条数与归属规则一致",
                  d2.get("total") == expect, f"{d2.get('total')} vs {expect}")

        # ── ② 下单归属：必须记到 C 端用户，而不是管理员 ─────────────
        print("\n② 下单归属（session=管理员 + JWT=C 端用户）")
        from agent.models import Inventory, Products
        prod = None
        for p in Products.objects.filter(is_active=True):
            inv = Inventory.objects.filter(product_id=p.id).first()
            if inv and (inv.available_stock or 0) > 0:
                prod = p
                break
        created = None
        if prod:
            r3 = c.post("/api/orders/", data=json.dumps({
                "customer_name": "身份解析测试", "phone": cust_phone,
                "product_id": prod.id, "quantity": 1,
            }), content_type="application/json", **auth)
            d3 = json.loads(r3.content)
            check("下单成功", r3.status_code == 200 and d3.get("success"),
                  f"HTTP {r3.status_code} {d3.get('message', '')[:40]}")
            if d3.get("order_no"):
                created = Orders.objects.get(order_no=d3["order_no"])
                check("订单归属到 C 端用户，而不是管理员 session",
                      created.user_id == cust.id,
                      f"user_id={created.user_id}（{cust.id}=C端 / {admin.id}=管理员）")
                check("订单立即出现在「我的」（无需手机号兜底）",
                      Orders.objects.filter(
                          user_id=cust.id).filter(pk=created.pk).exists())
        else:
            print("  (无可购商品，跳过下单归属验证)")

        # ── ③ 无 JWT 时仍回落 session（不能把后台功能弄坏）──────────
        print("\n③ 无 Authorization 头时回落 session 认证")
        r4 = c.get("/api/inventory/")            # staff_required 接口
        check("后台（session）接口仍可用", r4.status_code == 200, f"HTTP {r4.status_code}")
        r5 = c.get("/admin/dashboard/")
        check("后台看板仍可访问", r5.status_code == 200, f"HTTP {r5.status_code}")

        # ── ④ 纯 JWT（无 session）不得被误拒 ────────────────────────
        print("\n④ 只有 JWT、没有 session")
        c2 = Client()
        r6 = c2.post("/api/orders/query/", data=json.dumps({"phone": cust_phone}),
                     content_type="application/json", **bearer(cust))
        check("纯 JWT 可正常查询", r6.status_code == 200, f"HTTP {r6.status_code}")

        # ── ⑤ 越权防护不能被削弱 ────────────────────────────────────
        print("\n⑤ 越权防护仍生效")
        other = Orders.objects.exclude(phone=cust_phone).exclude(phone__isnull=True) \
            .exclude(phone="").values_list("phone", flat=True).first()
        if other:
            r7 = c.post("/api/orders/query/", data=json.dumps({"phone": other}),
                        content_type="application/json", **bearer(cust))
            check(f"查他人手机号 {other} 仍被拒绝", r7.status_code == 403, f"HTTP {r7.status_code}")

        # ── ⑥ 清理测试订单（不污染真实数据）────────────────────────
        if created is not None:
            from django.db import transaction as _tx
            with _tx.atomic():
                # 下单会预占库存；删除前先把预占还回去，避免库存虚占
                from agent.models import Inventory as _Inv
                inv = _Inv.objects.filter(product_id=created.product_id).first()
                if inv:
                    inv.reserved_stock = max(0, (inv.reserved_stock or 0) - (created.quantity or 0))
                    inv.save(update_fields=["reserved_stock", "updated_at"])
                from agent.models import Transaction as _Tx
                _Tx.objects.filter(order_id=created.pk).delete()
                created.delete()
            print(f"\n已清理测试订单 {created.order_no}，并把预占库存还回")

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
