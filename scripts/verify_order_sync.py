"""
契约验证：C 端「下单 / 订单 / 我的」数据口径一致性（ZT-agent）

覆盖用户反馈的「数据不同步」四条主链路：
  1. 前台产品接口与下单接口对「能不能买」判定是否一致（营销侧暂停购买是否真的生效）
  2. 「查订单」与「我的订单」订单集合是否一致（同一用户在两端不应看到不同结果）
  3. 「查订单」是否拒绝他人手机号（越权）
  4. 「订单详情」是否做归属校验

鉴权说明：`/api/me/*` 是 DRF 视图，只认 JWT；session（force_login）对它无效。
因此本脚本统一用 JWT 访问所有接口，让两端跑在同一条鉴权通道上 ——
否则比对会因 401 产生假失败。

用法：./.venv/Scripts/python.exe scripts/verify_order_sync.py
"""
import json
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model          # noqa: E402
from django.test import Client, override_settings       # noqa: E402

from agent.models import Orders, Products, UserProfile  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    """给请求构造 JWT 鉴权头"""
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jget(c, url, auth):
    r = c.get(url, **auth)
    try:
        return r.status_code, json.loads(r.content)
    except Exception:
        return r.status_code, {}


def jpost(c, url, payload, auth):
    r = c.post(url, data=json.dumps(payload), content_type="application/json", **auth)
    try:
        return r.status_code, json.loads(r.content)
    except Exception:
        return r.status_code, {}


def pick_user_with_phone():
    """优先挑一个「已绑定手机号」的真实用户，归属规则才被真正走通。"""
    prof = UserProfile.objects.exclude(phone__isnull=True).exclude(phone="").first()
    if prof:
        return prof.user, prof.phone
    su = get_user_model().objects.filter(is_superuser=True).first()
    p = UserProfile.objects.filter(user=su).first()
    return su, (p.phone if p else "")


def main():
    User = get_user_model()
    user, phone = pick_user_with_phone()
    if user is None:
        print("库中无任何用户，无法验证")
        return 1
    auth = bearer(user)
    print(f"测试账号：{user.username}  绑定手机号={phone or '(未绑定)'}")
    print(f"该账号名下：user_id 归属 {Orders.objects.filter(user=user).count()} 笔"
          f"｜同号未认领 {Orders.objects.filter(user__isnull=True, phone=phone).count()} 笔"
          f"｜同号全部 {Orders.objects.filter(phone=phone).count()} 笔")

    with override_settings(ALLOWED_HOSTS=["testserver"]):
        c = Client()

        # ── ① 可购买性口径一致（营销侧暂停购买） ────────────────
        print("\n① 前台产品接口 × 下单接口：可购买性口径")
        code, data = jget(c, "/api/products/", auth)
        prods = {p["id"]: p for p in data.get("products", [])}
        check("产品接口可访问", code == 200, f"HTTP {code}")
        check("产品接口返回 can_buy 字段", bool(prods) and all("can_buy" in p for p in prods.values()))
        check("产品接口返回 purchase_paused 字段",
              bool(prods) and all("purchase_paused" in p for p in prods.values()))

        paused = [p for p in prods.values() if p.get("purchase_paused")]
        print(f"    当前被暂停购买的商品：{[(p['id'], p['name']) for p in paused]}")
        if paused:
            p0 = paused[0]
            check(f"暂停商品 #{p0['id']} 在接口里标记为不可购", p0["can_buy"] is False,
                  f"can_buy={p0['can_buy']}")
            check("不可购原因码为 purchase_paused",
                  p0["buy_block_reason"] == "purchase_paused", p0["buy_block_reason"])
            st, res = jpost(c, "/api/orders/", {
                "customer_name": "口径测试", "phone": phone or "13800000000",
                "product_id": p0["id"], "quantity": 1,
            }, auth)
            check("对暂停商品下单被拒绝", st == 400, f"HTTP {st} {res.get('message', '')}")
            check("拒绝原因码与产品接口一致",
                  res.get("code") == "purchase_paused", res.get("code", ""))
        else:
            print("    (当前无暂停商品，跳过对比；字段契约已校验)")

        from agent.purchase_guard import check_buyable
        buyable = next((p for p in prods.values() if p.get("can_buy")), None)
        if buyable:
            prod = Products.objects.get(pk=buyable["id"])
            ok, _, msg = check_buyable(prod, quantity=1)
            check("可购商品守卫放行", ok, msg)
            ok2, code2, msg2 = check_buyable(prod, quantity=10 ** 6)
            check("超量下单被守卫拦截", not ok2 and code2 == "insufficient_stock", msg2)

        # ── ② 两端订单集合一致 ──────────────────────────────────
        print("\n② 「查订单」× 「我的订单」：订单集合一致性")
        st, mine = jget(c, "/api/me/orders/?page=1", auth)
        check("「我的订单」接口可访问（JWT 放行）", st == 200, f"HTTP {st}")
        my_nos = {o["order_no"] for o in mine.get("orders", [])}
        my_total = mine.get("stats", {}).get("total", 0)

        st2, q = jpost(c, "/api/orders/query/", {"phone": phone}, auth)
        q_nos = {o["order_no"] for o in q.get("orders", [])}
        print(f"    「我的订单」total={my_total} 本页={len(my_nos)}；"
              f"「查订单」count={q.get('count')} total={q.get('total')}")
        check("查订单返回真实 total 字段", "total" in q, str(list(q.keys())))
        check("查订单 total 与「我的订单」total 一致", q.get("total") == my_total,
              f"{q.get('total')} vs {my_total}")
        check("两端订单号集合完全一致", my_nos == q_nos,
              f"仅我的有={sorted(my_nos - q_nos)[:3]} 仅查订单有={sorted(q_nos - my_nos)[:3]}")
        check("查订单 count 与返回条数相符", q.get("count") == len(q_nos))

        # ── ③ 越权查询他人手机号 ────────────────────────────────
        print("\n③ 「查订单」越权防护")
        other = (Orders.objects.exclude(phone__isnull=True).exclude(phone="")
                 .exclude(phone=phone).values_list("phone", flat=True).first())
        if other:
            st3, res3 = jpost(c, "/api/orders/query/", {"phone": other}, auth)
            check(f"查询他人手机号 {other} 被拒绝", st3 == 403, f"HTTP {st3}")
            check("拒绝时未泄露订单内容", not res3.get("orders"), str(res3.get("code")))
        else:
            print("    (库中无其他手机号订单，跳过)")
        st3b, res3b = jpost(c, "/api/orders/query/", {"phone": "19999999999"}, auth)
        check("查询不存在的他人手机号同样 403（不泄露存在性）", st3b == 403, f"HTTP {st3b}")

        # ── ④ 订单详情归属校验 ──────────────────────────────────
        print("\n④ 订单详情归属校验")
        foreign = Orders.objects.exclude(phone=phone).exclude(user=user).first()
        if foreign:
            staff = User.objects.filter(is_staff=True).exclude(pk=user.pk).first() or user
            c3 = Client()
            st4, _ = jget(c3, f"/api/orders/{foreign.order_no}/", bearer(staff))
            check("staff 查看任意订单详情放行（后台排障需要）", st4 == 200, f"HTTP {st4}")

            plain = User.objects.filter(is_staff=False, is_superuser=False).first()
            if plain:
                c2 = Client()
                st5, _ = jget(c2, f"/api/orders/{foreign.order_no}/", bearer(plain))
                check("普通用户查看他人订单详情被拒（404 不泄露存在性）", st5 == 404, f"HTTP {st5}")
            else:
                print("    (无普通用户可测，跳过)")
        else:
            print("    (无他人订单可测，跳过)")

        # ── ⑤ 归属规则自检 ──────────────────────────────────────
        print("\n⑤ 归属规则（my_orders_q）自检")
        from agent.me_helpers import my_orders_q
        qs = Orders.objects.filter(my_orders_q(user))
        by_user = qs.filter(user=user).count()
        by_phone = qs.filter(user__isnull=True, phone=phone).count()
        check("可见订单 = user_id 归属 + 未认领同号", qs.count() == by_user + by_phone,
              f"{qs.count()} == {by_user} + {by_phone}")
        assigned_other = Orders.objects.filter(phone=phone, user__isnull=False) \
            .exclude(user=user).count() if phone else 0
        if assigned_other:
            print(f"    注：同号有 {assigned_other} 笔已被其它账号认领 → 按规则不展示（预期隔离），"
                  f"这正是改造前「查订单」比「我的」多出来的那几笔")
            old_style = Orders.objects.filter(phone=phone).count()
            check("改造后查订单不再返回被他人认领的同号订单",
                  old_style != qs.count() and q.get("total") == qs.count(),
                  f"旧口径 {old_style} 笔 / 新口径 {qs.count()} 笔")

        # ── ⑥ 下单后资料认领不再覆盖手机号 ──────────────────────
        print("\n⑥ 下单后自动认领：不覆盖已绑手机号")
        from agent.me_helpers import bind_phone_if_free
        before = UserProfile.objects.get(user=user).phone
        # nickname 传空串：本用例只验手机号策略，避免写入真实收货人姓名污染数据
        changed = bind_phone_if_free(user, "13000000001", nickname="")
        after = UserProfile.objects.get(user=user).phone
        if phone:
            check("已绑手机号不被下单手机号覆盖", after == before,
                  f"{before!r} → {after!r}（changed={changed}）")
        else:
            check("未绑手机号时正常认领", after == "13000000001", f"{before!r} → {after!r}")
            # 复原，避免污染真实数据
            p = UserProfile.objects.get(user=user)
            p.phone = ""
            p.save(update_fields=["phone", "updated_at"])
            print("    (已复原测试写入的手机号)")

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
