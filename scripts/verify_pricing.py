"""
契约验证：身份差异化价格（任务书二·5）—— ZT-agent

这条链路最容易出的不是"功能没做"，而是**同一个价格在不同出口说法不一致**。
改造前的真实状态就是这么拧巴的：

    · 产品页 / 下单落库 → 一律零售价
    · AI 报价工具       → 一律批发价
    · AI 查询产品       → 把零售价和批发价一起抛给客户
    · 系统提示词        → 写着"批发价只对经销商展示"

于是既有**泄露**（个体钓友也能看到批发价），又有**报价与成交不符**
（AI 按批发价报，客户下单按零售价结算）。所以本脚本重点验"各处是否同源"：

  1. 身份判定：未登录 / 无资料 / 脏值**一律降级为个体钓友**（宁可少给，不可多给）
  2. `price_for`：个体零售价；经销商达标批发价、未达标零售价；无批发价则零售价
  3. **接口层裁剪**：非经销商的响应体里**根本不含 `wholesale_price` 这个键**
  4. 管理端不裁剪（运营要维护批发价），且 `viewer=None` 的访客必须裁剪
  5. 下单落库价按身份（这是"报价即成交"的落点）
  6. AI 工具层：个体钓友的查询结果里没有批发价这一行、报价按零售价
  7. 个人中心可查看/切换身份，切换后报价立刻变
  8. **历史订单单价是快照**：换身份不回溯改旧单（审计要求）
  9. 后台可核定身份

用法：./.venv/Scripts/python.exe scripts/verify_pricing.py
"""
import json
import os
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings                        # noqa: E402
from django.contrib.admin.sites import AdminSite        # noqa: E402
from django.contrib.auth import get_user_model          # noqa: E402
from django.test import Client                          # noqa: E402

from agent import pricing, tools                        # noqa: E402
from agent.admin import UserProfileAdmin                # noqa: E402
from agent.models import Inventory, Orders, Products, UserProfile  # noqa: E402

PASS, FAIL = [], []
RETAIL, WHOLESALE = 10.0, 6.0


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def _req(c, method, url, payload=None, auth=None):
    kw = dict(auth or {})
    if payload is None:
        r = getattr(c, method)(url, **kw)
    else:
        r = getattr(c, method)(url, data=json.dumps(payload),
                               content_type="application/json", **kw)
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def jget(c, url, auth=None):
    return _req(c, "get", url, None, auth)


def jpost(c, url, payload, auth=None):
    return _req(c, "post", url, payload, auth)


def jpatch(c, url, payload, auth=None):
    return _req(c, "patch", url, payload, auth)


def ensure_test_host():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")


def row_of(payload, product_id):
    for p in (payload.get("products") or []):
        if p.get("id") == product_id:
            return p
    return {}


def main():
    ensure_test_host()
    User = get_user_model()
    suffix = uuid.uuid4().hex[:8]

    # ── 自造场景 ────────────────────────────────────────────────
    ind = User.objects.create_user(username=f"pt_ind_{suffix}", password="PriceTest!23456")
    whs = User.objects.create_user(username=f"pt_whs_{suffix}", password="PriceTest!23456")
    # 匿名/无资料也要能判身份
    lonely = User.objects.create_user(username=f"pt_bare_{suffix}", password="PriceTest!23456")

    p_full = Products.objects.create(
        sku=f"PT{suffix[:5].upper()}A", name=f"双价饵料{suffix}",
        spec="100g", target_fish="鲫鱼", retail_price=RETAIL, wholesale_price=WHOLESALE,
        is_active=True,
    )
    p_retail_only = Products.objects.create(
        sku=f"PT{suffix[:5].upper()}B", name=f"仅零售饵料{suffix}",
        spec="100g", target_fish="鲤鱼", retail_price=RETAIL, wholesale_price=None,
        is_active=True,
    )
    for p in (p_full, p_retail_only):
        Inventory.objects.create(product=p, stock=500, reserved_stock=0)

    UserProfile.objects.create(user=ind, customer_type=UserProfile.CustomerType.INDIVIDUAL,
                               customer_type_source=UserProfile.TypeSource.SELF)
    UserProfile.objects.create(user=whs, customer_type=UserProfile.CustomerType.WHOLESALE,
                               customer_type_source=UserProfile.TypeSource.SELF)
    # lonely 故意不建 profile —— 验证"无资料也降级为个体钓友"

    c = Client()
    created = []

    def cleanup():
        orders = Orders.objects.filter(order_no__in=created)
        orders.delete()
        Inventory.objects.filter(product__in=[p_full, p_retail_only]).delete()
        Products.objects.filter(pk__in=[p_full.pk, p_retail_only.pk]).delete()
        UserProfile.objects.filter(user__in=[ind, whs, lonely]).delete()
        User.objects.filter(pk__in=[ind.pk, whs.pk, lonely.pk]).delete()
        print("\n    (临时用户 / 商品 / 库存 / 订单 / 资料已清理)")

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】身份判定：默认个体钓友，异常一律降级")
        # ════════════════════════════════════════════════════════
        check("未登录 → 个体钓友", pricing.customer_type_of(None) == "individual")
        check("无资料的用户 → 个体钓友",
              pricing.customer_type_of(lonely) == "individual",
              pricing.customer_type_of(lonely))
        check("显式个体 → 个体", not pricing.is_wholesale(ind))
        check("显式经销商 → 经销商", pricing.is_wholesale(whs))
        check("身份中文标签正确", pricing.customer_type_label(whs) == "经销商",
              pricing.customer_type_label(whs))

        UserProfile.objects.filter(user=ind).update(customer_type="vip")
        check("非法身份值降级为个体钓友（宁可少给不可多给）",
              pricing.customer_type_of(ind) == "individual")
        UserProfile.objects.filter(user=ind).update(customer_type="individual")

        # ════════════════════════════════════════════════════════
        print("\n【2】price_for：身份 + 数量共同决定单价")
        # ════════════════════════════════════════════════════════
        check("个体钓友 → 零售价", pricing.price_for(p_full, ind, 100) == RETAIL,
              str(pricing.price_for(p_full, ind, 100)))
        check("未登录 → 零售价", pricing.price_for(p_full, None, 100) == RETAIL)
        check("经销商 10 包（达标）→ 批发价",
              pricing.price_for(p_full, whs, 10) == WHOLESALE,
              str(pricing.price_for(p_full, whs, 10)))
        check("经销商 9 包（未达标）→ 零售价",
              pricing.price_for(p_full, whs, 9) == RETAIL,
              str(pricing.price_for(p_full, whs, 9)))
        check("经销商但商品没有批发价 → 零售价",
              pricing.price_for(p_retail_only, whs, 100) == RETAIL)
        check("批发起订量是 10", pricing.WHOLESALE_MIN_QTY == 10)

        # ════════════════════════════════════════════════════════
        print("\n【3】price_fields：裁剪发生在接口层，不是前端隐藏")
        # ════════════════════════════════════════════════════════
        f_ind = pricing.price_fields(p_full, ind)
        check("个体的价格字段**不含** wholesale_price 键", "wholesale_price" not in f_ind,
              str(sorted(f_ind)))
        check("个体的 can_see_wholesale=False", f_ind["can_see_wholesale"] is False)
        check("个体仍拿得到零售价", f_ind["retail_price"] == RETAIL)

        f_anon = pricing.price_fields(p_full, None)
        check("未登录访客同样不含批发价（viewer=None 要走裁剪）",
              "wholesale_price" not in f_anon)

        f_whs = pricing.price_fields(p_full, whs)
        check("经销商含批发价", f_whs.get("wholesale_price") == WHOLESALE, str(f_whs))
        check("经销商 can_see_wholesale=True", f_whs["can_see_wholesale"] is True)

        f_admin = pricing.price_fields(p_full, None, mask=False)
        check("mask=False（管理端）不裁剪，且与 viewer 是否为 None 无关",
              f_admin.get("wholesale_price") == WHOLESALE, str(f_admin))

        check("无批发价的商品不出现该键（不返回 0）",
              "wholesale_price" not in pricing.price_fields(p_retail_only, whs))

        # ════════════════════════════════════════════════════════
        print("\n【4】产品列表接口：同一份数据，三种身份三种可见性")
        # ════════════════════════════════════════════════════════
        st, d = jget(c, "/api/products/")
        row = row_of(d, p_full.pk)
        check("匿名访问：不含批发价", st == 200 and "wholesale_price" not in row,
              str(sorted(row))[:90])
        check("匿名访问：customer_type=individual", row.get("customer_type") == "individual")
        check("匿名访问：effective_price=零售价", row.get("effective_price") == RETAIL)
        check("匿名访问：下发起订量（前端不硬编码）", row.get("wholesale_min_qty") == 10)

        st, d = jget(c, "/api/products/", bearer(ind))
        row = row_of(d, p_full.pk)
        check("个体钓友登录后：仍不含批发价", "wholesale_price" not in row)

        st, d = jget(c, "/api/products/", bearer(whs))
        row = row_of(d, p_full.pk)
        check("经销商登录后：含批发价", row.get("wholesale_price") == WHOLESALE, str(row)[:120])

        st, d = jget(c, "/api/products/", bearer(lonely))
        row = row_of(d, p_full.pk)
        check("无资料用户：按个体处理，不含批发价", "wholesale_price" not in row)

        stub = User.objects.create_user(
            username=f"pt_staff_{suffix}", password="PriceTest!23456", is_staff=True)
        try:
            st, d = jget(c, "/api/products/all/", bearer(stub))
            row = row_of(d, p_full.pk)
            check("管理端列表不裁剪（运营要维护批发价）",
                  row.get("wholesale_price") == WHOLESALE, str(row)[:120])
        finally:
            User.objects.filter(pk=stub.pk).delete()

        # ════════════════════════════════════════════════════════
        print("\n【5】下单落库价：报价与成交必须同源")
        # ════════════════════════════════════════════════════════
        def place(user, auth, qty, phone):
            st, d = jpost(c, "/api/orders/", {
                "customer_name": "价格验证", "phone": phone,
                "address": "山东省济南市历下区测试路 1 号",
                "product_id": p_full.pk, "quantity": qty,
            }, auth)
            assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
            created.append(d["order_no"])
            return d

        d = place(ind, bearer(ind), 50, f"137{suffix[:8]}")
        check("个体钓友 50 包 → 按零售价成交", d["unit_price"] == RETAIL, str(d["unit_price"]))
        check("个体钓友 total = 零售价 × 数量", d["total_price"] == RETAIL * 50, str(d["total_price"]))
        check("个体钓友 price_tier=零售价", d["price_tier"] == "零售价", str(d["price_tier"]))

        d = place(whs, bearer(whs), 10, f"136{suffix[:8]}")
        check("经销商 10 包 → 按批发价成交", d["unit_price"] == WHOLESALE, str(d["unit_price"]))
        check("经销商 price_tier=批发价", d["price_tier"] == "批发价", str(d["price_tier"]))

        d = place(whs, bearer(whs), 2, f"135{suffix[:8]}")
        check("经销商 2 包（未达起订量）→ 仍按零售价", d["unit_price"] == RETAIL,
              str(d["unit_price"]))

        # ════════════════════════════════════════════════════════
        print("\n【6】AI 工具层：看不到就不会说出口")
        # ════════════════════════════════════════════════════════
        tools.set_current_user(ind)
        out_ind = tools.query_product(p_full.name)
        check("个体钓友查产品：结果里没有批发价这一行", "批发价" not in out_ind,
              out_ind.replace("\n", " | "))
        check("个体钓友查产品：有零售价", f"零售价：{RETAIL}" in out_ind)

        tools.set_current_user(whs)
        out_whs = tools.query_product(p_full.name)
        check("经销商查产品：有批发价与起订量",
              f"批发价：{WHOLESALE} 元/包（10 包起）" in out_whs,
              out_whs.replace("\n", " | "))

        tools.set_current_user(None)
        out_anon = tools.query_product(p_full.name)
        check("未登录查产品：不含批发价", "批发价" not in out_anon)

        tools.set_current_user(ind)
        q_ind = tools.calculate_quote([{"name": p_full.name, "qty": 20}])
        check("个体钓友报价：按零售价算", f"单价 {RETAIL} 元（零售价）" in q_ind,
              q_ind.replace("\n", " | "))
        check("个体钓友报价合计 = 零售价 × 20", f"合计：{RETAIL * 20:.2f}" in q_ind,
              q_ind.replace("\n", " | "))

        tools.set_current_user(whs)
        q_whs = tools.calculate_quote([{"name": p_full.name, "qty": 20}])
        check("经销商报价（20 包达标）：按批发价算",
              f"单价 {WHOLESALE} 元（批发价）" in q_whs, q_whs.replace("\n", " | "))
        check("经销商报价合计 = 批发价 × 20", f"合计：{WHOLESALE * 20:.2f}" in q_whs)

        q_whs_small = tools.calculate_quote([{"name": p_full.name, "qty": 3}])
        check("经销商报价（3 包未达标）：按零售价算",
              f"单价 {RETAIL} 元（零售价）" in q_whs_small,
              q_whs_small.replace("\n", " | "))
        tools.set_current_user(None)

        # ════════════════════════════════════════════════════════
        print("\n【7】个人中心：查看与切换身份")
        # ════════════════════════════════════════════════════════
        st, d = jget(c, "/api/me/profile/", bearer(ind))
        check("个人中心返回身份", (d.get("user") or {}).get("customer_type") == "individual",
              str((d.get("user") or {}).get("customer_type")))
        check("个人中心返回身份中文标签",
              (d.get("user") or {}).get("customer_type_label") == "个体钓友")
        check("个人中心返回起订量",
              (d.get("user") or {}).get("wholesale_min_qty") == 10)

        st, d = jpatch(c, "/api/me/profile/", {"customer_type": "boss"}, bearer(ind))
        check("非法身份值 → 400", st == 400, str(st))

        st, d = jpatch(c, "/api/me/profile/", {"customer_type": "wholesale"}, bearer(ind))
        check("切换为经销商成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("切换后返回新身份",
              (d.get("user") or {}).get("customer_type") == "wholesale")
        check("身份来源记为 self",
              (d.get("user") or {}).get("customer_type_source") == "self")

        st, d = jget(c, "/api/products/", bearer(ind))
        row = row_of(d, p_full.pk)
        check("切换后产品列表立刻带上批发价", row.get("wholesale_price") == WHOLESALE,
              str(row)[:120])

        st, d = jpost(c, "/api/orders/", {
            "customer_name": "价格验证", "phone": f"134{suffix[:8]}",
            "address": "山东省济南市历下区测试路 1 号",
            "product_id": p_full.pk, "quantity": 10,
        }, bearer(ind))
        created.append(d["order_no"])
        check("切换身份后下单立刻按批发价",
              d.get("unit_price") == WHOLESALE, str(d.get("unit_price")))

        st, d = jpatch(c, "/api/me/profile/", {"customer_type": "individual"}, bearer(ind))
        check("切回个体钓友成功", st == 200 and (d.get("user") or {}).get("customer_type") == "individual")

        # ════════════════════════════════════════════════════════
        print("\n【8】历史订单是快照：换身份不回溯改旧单")
        # ════════════════════════════════════════════════════════
        order_whs = Orders.objects.get(order_no=created[1])   # 经销商 10 包那笔
        before_price = float(order_whs.unit_price)
        UserProfile.objects.filter(user=whs).update(customer_type="individual")
        order_whs.refresh_from_db()
        check("换身份后历史订单单价不变（审计要求）",
              float(order_whs.unit_price) == before_price == WHOLESALE,
              f"{before_price} → {order_whs.unit_price}")
        UserProfile.objects.filter(user=whs).update(customer_type="wholesale")

        # ════════════════════════════════════════════════════════
        print("\n【9】后台：身份可核定")
        # ════════════════════════════════════════════════════════
        ua = UserProfileAdmin(UserProfile, AdminSite())
        check("后台列表含客户类型列", "customer_type_badge" in ua.list_display, str(ua.list_display))
        check("后台列表含身份来源列", "source_badge" in ua.list_display)
        check("后台可按客户类型筛选", "customer_type" in ua.list_filter)
        field_names = [f for _title, cfg in ua.fieldsets for f in cfg["fields"]]
        check("后台字段组含 customer_type", "customer_type" in field_names, str(field_names))
        check("后台可搜索手机号", "phone" in ua.search_fields)

    finally:
        tools.set_current_user(None)
        cleanup()

    print("\n" + "═" * 62)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
