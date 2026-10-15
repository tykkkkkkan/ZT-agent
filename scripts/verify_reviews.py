"""
契约验证：商品评价（任务书六·4）—— ZT-agent

覆盖「评价」这条链路与订单状态机、库存、商品接口、AI 工具层四处的口径一致性：

  1. **可评价性**：只有「已完成」的订单能评；未发货/已发货/退货申请中/已取消/
     已退货/无关联商品 全部拒绝，且原因说得清楚（而不是一律"不可评价"）
  2. 已完成订单 → `_order_actions` 出现 `review` 动作（前端据此渲染按钮）
  3. 接口鉴权与归属：未登录 401、他人订单 404、订单不存在 404
  4. 参数校验：评分缺失/超范围/非数字 → 400；内容超 500 字 → 400
  5. 评价成功：落库字段正确（订单/商品/用户/评分/内容），订单序列化带出评价
  6. **一单一评**：第二次提交被拒（预检给出人话提示），且数据库唯一约束兜底
  7. 评分聚合：`rating_summary` 均分与条数正确；`/api/products/` 与
     `/api/products/<id>/reviews/` 与之一致，真实总数与已展示条数分开报
  8. **姓名脱敏**：对外评价列表只露首字，不泄露全名（商品页是公开页面）
  9. 评价列表接口**公开可读**（不要求登录，与 /api/products/ 同口径）
 10. AI 工具层：`query_product` 能带出真实评分，让客服不必凭空夸产品
 11. 后台 `ReviewAdmin`：只读、禁止人工新增（评价必须来自真实订单）
 12. 数据库层：`product_reviews` 表存在且 `order_id` 上有唯一索引

自造场景 → 断言 → 精确还原：脚本自建临时用户/商品/库存，结束时全部删除，
并把钱包余额恢复原值（确认收货前的发货会触发自动记账，属真实财务副作用）。

用法：./.venv/Scripts/python.exe scripts/verify_reviews.py
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
from django.db import IntegrityError, connection        # noqa: E402
from django.test import Client                          # noqa: E402

from agent.admin import ReviewAdmin                     # noqa: E402
from agent.models import (                              # noqa: E402
    Inventory, Orders, OrderStatus, Products, Review, Transaction, Wallet,
)
from agent.reviews import (                             # noqa: E402
    create_review, mask_name, product_reviews, rating_summary,
    reviewable_reason, serialize_review,
)
from agent.services import transition_order             # noqa: E402
from agent.tools import query_product                   # noqa: E402
from agent.views import _order_actions, _serialize_order  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jpost(c, url, payload, auth=None):
    r = c.post(url, data=json.dumps(payload), content_type="application/json", **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def jget(c, url, auth=None):
    r = c.get(url, **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def ensure_test_host():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")


def place_order(c, auth, product_id, qty, phone, name="评价验证"):
    st, d = jpost(c, "/api/orders/", {
        "customer_name": name, "phone": phone,
        "address": "山东省济南市历下区测试路 1 号",
        "product_id": product_id, "quantity": qty,
    }, auth)
    assert st == 200 and d.get("success"), f"下单失败：{st} {d}"
    return d


def main():
    ensure_test_host()
    User = get_user_model()
    suffix = uuid.uuid4().hex[:8]

    # ── 自造场景 ────────────────────────────────────────────────
    me = User.objects.create_user(username=f"revtest_{suffix}", password="RevTest!23456")
    other = User.objects.create_user(username=f"revtest_o_{suffix}", password="RevTest!23456")
    product = Products.objects.create(
        sku=f"REV{suffix[:5].upper()}", name=f"评价验证饵料{suffix}",
        spec="100g", target_fish="鲫鱼",
        retail_price=10, wholesale_price=6, is_active=True,
    )
    Inventory.objects.create(product=product, stock=200, reserved_stock=0)

    wallet = Wallet.objects.first()
    wallet_snapshot = wallet.balance if wallet else None

    c = Client()
    auth = bearer(me)
    created = []

    def cleanup():
        """精确还原：先删评价与流水，再删订单，回滚钱包余额，最后删商品/用户。"""
        order_ids = list(Orders.objects.filter(order_no__in=created).values_list("id", flat=True))
        Review.objects.filter(order_id__in=order_ids).delete()
        Review.objects.filter(product_id=product.pk).delete()
        Transaction.objects.filter(order_id__in=order_ids).delete()
        Orders.objects.filter(id__in=order_ids).delete()
        if wallet is not None and wallet_snapshot is not None:
            Wallet.objects.filter(pk=wallet.pk).update(balance=wallet_snapshot)
        Inventory.objects.filter(product=product).delete()
        Products.objects.filter(pk=product.pk).delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户 / 商品 / 库存 / 订单 / 评价已清理，钱包余额已还原)")

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】可评价性：只有「已完成」的订单能评")
        # ════════════════════════════════════════════════════════
        o1 = place_order(c, auth, product.pk, 1, f"139{suffix[:8]}")
        created.append(o1["order_no"])
        order1 = Orders.objects.get(order_no=o1["order_no"])

        check("未发货（待支付）订单不可评价", reviewable_reason(order1) != "")
        check("未发货的拒绝原因指向订单状态",
              "只有已完成" in reviewable_reason(order1), reviewable_reason(order1))
        check("未发货订单的 actions 不含 review", "review" not in _order_actions(order1))

        # 用一个订单逐一切换状态，确认每种状态都被挡住（直接改 status，不走状态机）
        def status_reason(st):
            Orders.objects.filter(pk=order1.pk).update(status=st)
            order1.refresh_from_db()
            return reviewable_reason(order1)

        for st in (OrderStatus.PENDING, OrderStatus.SHIPPED,
                   OrderStatus.RETURNING, OrderStatus.CANCELLED, OrderStatus.RETURNED):
            check(f"「{st}」订单不可评价", status_reason(st) != "")

        Orders.objects.filter(pk=order1.pk).update(status=OrderStatus.PENDING)
        order1.refresh_from_db()

        # ════════════════════════════════════════════════════════
        print("\n【2】走完整链路：支付 → 发货 → 确认收货 → 出现评价入口")
        # ════════════════════════════════════════════════════════
        st, d = jpost(c, f"/api/orders/{order1.order_no}/pay/", {}, auth)
        check("模拟支付成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")

        order1.refresh_from_db()
        transition_order(order1, to_status=OrderStatus.SHIPPED,
                         ship_company="顺丰", tracking_no=f"SF{suffix}")
        order1.refresh_from_db()
        transition_order(order1, to_status=OrderStatus.COMPLETED)
        order1.refresh_from_db()
        check("订单已走到「已完成」", order1.status == OrderStatus.COMPLETED, order1.status)

        acts = _order_actions(order1)
        check("已完成且未评价 → actions 含 review", "review" in acts, str(acts))
        check("已完成仍可申请售后（return 未被挤掉）", "return" in acts, str(acts))

        # ════════════════════════════════════════════════════════
        print("\n【3】鉴权与归属：未登录 401 / 他人订单 404 / 不存在 404")
        # ════════════════════════════════════════════════════════
        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/", {"rating": 5})
        check("未登录评价 → 401", st == 401, str(st))
        check("未登录返回 LOGIN_REQUIRED", (d.get("code") == "LOGIN_REQUIRED"), str(d.get("code")))

        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/", {"rating": 5}, bearer(other))
        check("评价他人订单 → 404（不泄露订单存在性）", st == 404, str(st))

        st, d = jpost(c, "/api/orders/ZT-NOT-EXIST-9999/review/", {"rating": 5}, auth)
        check("评价不存在的订单 → 404", st == 404, str(st))

        # ════════════════════════════════════════════════════════
        print("\n【4】参数校验：评分与内容")
        # ════════════════════════════════════════════════════════
        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/", {}, auth)
        check("缺评分 → 400", st == 400 and d.get("code") == "invalid_rating", f"{st} {d}")

        for bad in (0, 6, -1, "abc"):
            st, d = jpost(c, f"/api/orders/{order1.order_no}/review/", {"rating": bad}, auth)
            check(f"评分 {bad!r} → 400 invalid_rating",
                  st == 400 and d.get("code") == "invalid_rating", f"{st} {d.get('code')}")

        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/",
                      {"rating": 5, "content": "字" * 501}, auth)
        check("评价内容超 500 字 → 400", st == 400 and d.get("code") == "content_too_long",
              f"{st} {d.get('code')}")
        check("校验失败不产生评价记录",
              Review.objects.filter(order_id=order1.pk).count() == 0)

        # ════════════════════════════════════════════════════════
        print("\n【5】评价成功：落库 + 订单序列化带出评价")
        # ════════════════════════════════════════════════════════
        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/",
                      {"rating": 5, "content": "雾化效果不错，野钓连杆。"}, auth)
        check("评价提交成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")

        rv = Review.objects.filter(order_id=order1.pk).first()
        check("评价已落库", rv is not None)
        check("评分正确", rv and rv.rating == 5, str(rv and rv.rating))
        check("商品 ID 归属正确", rv and rv.product_id == product.pk, str(rv and rv.product_id))
        check("评价人正确", rv and rv.user_id == me.pk, str(rv and rv.user_id))
        check("内容正确", rv and "雾化效果" in rv.content, str(rv and rv.content))
        check("星串展示正确（5 星 → ★★★★★）", rv and rv.stars == "★★★★★", str(rv and rv.stars))

        order1.refresh_from_db()
        data = _serialize_order(order1, with_actions=True)
        check("订单序列化带出 review", bool(data.get("review")), str(data.get("review"))[:60])
        check("已评价订单不再下发 review 动作", "review" not in data["actions"], str(data["actions"]))
        check("已评价订单不再给 review_block_reason", data.get("review_block_reason") == "",
              repr(data.get("review_block_reason")))
        check("评价内容是订单本人的数据，可完整返回",
              (data.get("review") or {}).get("content", "").startswith("雾化效果"))

        # ════════════════════════════════════════════════════════
        print("\n【6】一单一评：预检给提示 + 数据库唯一约束兜底")
        # ════════════════════════════════════════════════════════
        st, d = jpost(c, f"/api/orders/{order1.order_no}/review/", {"rating": 1}, auth)
        check("重复评价 → 400", st == 400, str(st))
        check("重复评价的错误码是 not_reviewable（预检拦下）",
              d.get("code") == "not_reviewable", str(d.get("code")))
        check("重复评价有人话提示", "已经评价过" in (d.get("message") or ""), str(d.get("message")))
        check("重复提交没有产生第二条记录",
              Review.objects.filter(order_id=order1.pk).count() == 1)

        try:
            Review.objects.create(order=order1, product_id=product.pk, user=me, rating=3)
            check("绕过视图直接写库也被唯一约束拦住", False, "居然写进去了")
        except IntegrityError:
            check("绕过视图直接写库也被唯一约束拦住", True)

        # ════════════════════════════════════════════════════════
        print("\n【7】评分聚合：与商品接口口径一致")
        # ════════════════════════════════════════════════════════
        summary = rating_summary([product.pk]).get(product.pk) or {}
        check("rating_summary 均分正确", summary.get("rating_avg") == 5.0, str(summary))
        check("rating_summary 条数正确", summary.get("review_count") == 1, str(summary))

        st, d = jget(c, "/api/products/")
        row = next((p for p in (d.get("products") or []) if p["id"] == product.pk), None)
        check("商品列表接口返回评分", row is not None and row.get("rating_avg") == 5.0,
              str(row and {k: row[k] for k in ("rating_avg", "review_count")}))
        check("商品列表接口返回评价条数", row is not None and row.get("review_count") == 1)

        # ════════════════════════════════════════════════════════
        print("\n【8】评价列表：公开可读 + 姓名脱敏 + 真实总数")
        # ════════════════════════════════════════════════════════
        st, d = jget(c, f"/api/products/{product.pk}/reviews/")          # 匿名访问
        check("评价列表匿名可读（商品页是公开页面）", st == 200 and d.get("success"), str(st))
        check("返回真实总数 review_count", d.get("review_count") == 1, str(d.get("review_count")))
        check("返回已展示条数 showing", d.get("showing") == 1, str(d.get("showing")))
        first = (d.get("reviews") or [{}])[0]
        check("列表里有评分星串", first.get("stars") == "★★★★★", str(first.get("stars")))
        check("评价人姓名已脱敏（不出现全名）",
              first.get("nickname") == mask_name("评价验证"), str(first.get("nickname")))

        st, d = jget(c, f"/api/products/{product.pk}/reviews/?limit=abc")
        check("limit 非整数 → 400", st == 400, str(st))

        st, d = jget(c, f"/api/products/{product.pk}/reviews/?limit=999")
        check("limit 超范围被夹到上限（不报错）", st == 200, str(st))

        check("mask_name：两字名 → 张*", mask_name("张三") == "张*", mask_name("张三"))
        check("mask_name：三字名 → 李**", mask_name("李小明") == "李**", mask_name("李小明"))
        check("mask_name：单字名保留", mask_name("李") == "李", mask_name("李"))
        check("mask_name：空 → 匿名客户", mask_name("") == "匿名客户", mask_name(""))

        # 脱敏在序列化层就生效（不是靠前端遮）
        serialized = serialize_review(rv)
        check("serialize_review 已脱敏", serialized.get("nickname") == mask_name("评价验证"),
              serialized.get("nickname"))
        check("serialize_review 不返回手机号等隐私字段",
              "phone" not in serialized and "order_no" not in serialized, str(sorted(serialized)))

        # ════════════════════════════════════════════════════════
        print("\n【9】个人中心（DRF）通道：与网页通道同规则")
        # ════════════════════════════════════════════════════════
        o2 = place_order(c, auth, product.pk, 1, f"138{suffix[:8]}")
        created.append(o2["order_no"])
        order2 = Orders.objects.get(order_no=o2["order_no"])

        st, d = jpost(c, f"/api/me/orders/{order2.order_no}/review/", {"rating": 4}, auth)
        check("未完成订单走个人中心评价 → 400", st == 400, str(st))

        jpost(c, f"/api/orders/{order2.order_no}/pay/", {}, auth)
        order2.refresh_from_db()
        transition_order(order2, to_status=OrderStatus.SHIPPED, ship_company="中通",
                         tracking_no=f"ZT{suffix}")
        order2.refresh_from_db()
        transition_order(order2, to_status=OrderStatus.COMPLETED)
        order2.refresh_from_db()

        st, d = jpost(c, f"/api/me/orders/{order2.order_no}/review/",
                      {"rating": 4, "content": "性价比可以"}, auth)
        check("个人中心通道评价成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("个人中心返回体带 review", bool(d.get("review")))
        check("个人中心返回体带订单（含 actions）", bool((d.get("order") or {}).get("actions")))

        st, d = jpost(c, f"/api/me/orders/{order2.order_no}/review/", {"rating": 2}, auth)
        check("个人中心重复评价 → 400", st == 400, str(st))

        st, d = jpost(c, f"/api/me/orders/{order2.order_no}/review/", {"rating": 7}, bearer(other))
        check("他人订单走个人中心 → 404", st == 404, str(st))

        # 两条评价 → 均分 (5+4)/2 = 4.5
        summary = rating_summary([product.pk]).get(product.pk) or {}
        check("两条评价均分 4.5", summary.get("rating_avg") == 4.5, str(summary))
        check("两条评价条数为 2", summary.get("review_count") == 2, str(summary))

        # ════════════════════════════════════════════════════════
        print("\n【10】AI 工具层：客服能引用真实评分")
        # ════════════════════════════════════════════════════════
        text = query_product(product.name)
        check("query_product 输出含评分", "客户评价：4.5 分（2 条评价）" in text, text.replace("\n", " | "))
        check("query_product 仍带可购性（没被评价挤掉）", "可下单" in text or "现货" in text)

        no_review = Products.objects.create(
            sku=f"REVN{suffix[:4].upper()}", name=f"无评价饵料{suffix}",
            retail_price=8, wholesale_price=5, is_active=True,
        )
        Inventory.objects.create(product=no_review, stock=10, reserved_stock=0)
        text2 = query_product(no_review.name)
        check("无评价的商品不输出评分行（不暗示商品不好）", "客户评价" not in text2)
        Inventory.objects.filter(product=no_review).delete()
        Products.objects.filter(pk=no_review.pk).delete()

        # ════════════════════════════════════════════════════════
        print("\n【11】后台：只读、禁止人工新增")
        # ════════════════════════════════════════════════════════
        ra = ReviewAdmin(Review, AdminSite())
        check("后台不允许新增评价", ra.has_add_permission(None) is False)
        check("后台评分字段只读", "rating" in ra.readonly_fields, str(ra.readonly_fields))
        check("后台内容字段只读", "content" in ra.readonly_fields)
        check("后台列表含商品列", "product_link" in ra.list_display, str(ra.list_display))
        check("后台列表含评分列", "stars_display" in ra.list_display)
        check("后台可按评分筛选", "rating" in ra.list_filter, str(ra.list_filter))

        # ════════════════════════════════════════════════════════
        print("\n【12】数据库层：表与唯一索引真的存在")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='product_reviews'"
            )
            check("product_reviews 表存在", cur.fetchone()[0] == 1)

            cur.execute(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='product_reviews' "
                "AND COLUMN_NAME='order_id' AND NON_UNIQUE=0"
            )
            check("order_id 上有唯一约束（一单一评的最终防线）", cur.fetchone()[0] >= 1)

            cur.execute(
                "SELECT COUNT(*) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='product_reviews' "
                "AND INDEX_NAME='idx_review_prod_created'"
            )
            check("商品+时间复合索引存在（商品页拉评价不慢）", cur.fetchone()[0] >= 1)

    finally:
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
