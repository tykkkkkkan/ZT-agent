"""
库存 ↔ 前台可购性 一致性验证

覆盖用户反馈的「后端库存是够的，前端却没有库存」两类根因：

  A. **断货暂停死锁**：营销 Agent 因断货下发 pause_product（当时可用库存 0），
     运营补货后那条覆盖**永远不会自己解除** → 后台显示库存充足、前台永远买不了。
     真凶是缺一个"补货即恢复"的回钩 + 缺 pause_reason 区分人工/断货。

  B. **预占泄漏**：`reserved_stock` 与「未发货订单」数量之和对不上
     → 总库存够，可用库存却少 → 前台买不了。

另验证：人工暂停不自动解除（安全边界）、可用仍为 0 时不解除（避免刚补一点就放出）、
后台列表筛选与前台可购性口径一致、后台「恢复接单」按钮接口可用。

用法：./.venv/Scripts/python.exe scripts/verify_inventory_sync.py
"""
import json
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model                # noqa: E402
from django.test import Client, override_settings             # noqa: E402
from django.urls import reverse                               # noqa: E402

from agent.models import (                                    # noqa: E402
    Inventory, Orders, OrderStatus, ProductCoordination, Products,
)
from agent.purchase_guard import evaluate                     # noqa: E402
from agent.services import adjust_inventory_stock, auto_resume_stockout_pause  # noqa: E402

PASS, FAIL = [], []
SNAPSHOTS = []          # 用于 finally 复原真实数据


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def snapshot(coord):
    if coord is None:
        return None
    return (coord.pk, coord.purchase_paused, coord.pause_reason,
            coord.customer_notice, coord.updated_by)


def restore():
    for snap in SNAPSHOTS:
        if snap is None:
            continue
        pk, paused, reason, notice, by = snap
        ProductCoordination.objects.filter(pk=pk).update(
            purchase_paused=paused, pause_reason=reason or "",
            customer_notice=notice or "", updated_by=by or "",
        )


def main():
    prod = Products.objects.filter(is_active=True).first()
    inv0 = Inventory.objects.filter(product_id=prod.id).first()
    if not prod or not inv0:
        print("库中无可用商品/库存，无法验证")
        return 1

    print(f"测试商品：#{prod.id} {prod.name}（stock={inv0.stock} 预占={inv0.reserved_stock}）")

    coord0 = ProductCoordination.objects.filter(product_id=prod.id).first()
    SNAPSHOTS.append(snapshot(coord0))
    stock0 = inv0.stock

    try:
        # ── ① 补货后自动解除「断货暂停」 ────────────────────────
        print("\n① 补货后自动解除断货暂停（核心：修死锁）")
        ProductCoordination.objects.update_or_create(
            product_id=prod.id,
            defaults={"purchase_paused": True,
                      "pause_reason": ProductCoordination.PAUSE_STOCKOUT,
                      "customer_notice": "「%s」暂时缺货，可先收藏或咨询客服。" % prod.name,
                      "updated_by": "mkt_bot"},
        )
        inv = Inventory.objects.select_related('product').get(pk=inv0.pk)
        v = evaluate(prod, inv, ProductCoordination.objects.get(product_id=prod.id))
        check("造出死锁现场：库存够但被标记暂停接单", v["can_buy"] is False,
              f"stock={inv.stock} 可用={inv.available_stock} reason={v['reason']}")

        adjust_inventory_stock(inv, 10, note="验证：补货应自动恢复接单")
        coord = ProductCoordination.objects.get(product_id=prod.id)
        inv.refresh_from_db()
        v2 = evaluate(prod, inv, coord)
        check("补货后自动解除暂停", coord.purchase_paused is False,
              f"purchase_paused={coord.purchase_paused}")
        check("前台恢复可购买", v2["can_buy"] is True, f"can_buy={v2['can_buy']}")
        check("陈旧的「暂时缺货」提示语被清掉", not (coord.customer_notice or "").strip(),
              repr(coord.customer_notice or ""))
        check("留痕说明是系统自动恢复", "auto_resume" in (coord.updated_by or ""),
              coord.updated_by or "")

        # ── ② 人工暂停不自动解除（安全边界）────────────────────
        print("\n② 人工暂停（manual）不随补货自动解除")
        ProductCoordination.objects.filter(product_id=prod.id).update(
            purchase_paused=True, pause_reason=ProductCoordination.PAUSE_MANUAL,
            customer_notice="该商品因质量问题暂停发货，请联系客服",
            updated_by="admin",
        )
        inv = Inventory.objects.select_related('product').get(pk=inv0.pk)
        adjust_inventory_stock(inv, 10, note="验证：人工暂停不应被自动解除")
        coord = ProductCoordination.objects.get(product_id=prod.id)
        check("人工暂停保持不变", coord.purchase_paused is True,
              f"purchase_paused={coord.purchase_paused} reason={coord.pause_reason}")
        check("人工暂停的客户提示语未被清空", bool((coord.customer_notice or "").strip()),
              (coord.customer_notice or "")[:30])

        # ── ③ 可用库存仍为 0 时不解除 ───────────────────────────
        print("\n③ 断货型暂停但可用库存仍为 0 → 保留暂停（不放出）")
        ProductCoordination.objects.filter(product_id=prod.id).update(
            purchase_paused=True, pause_reason=ProductCoordination.PAUSE_STOCKOUT,
            customer_notice="暂时缺货", updated_by="mkt_bot",
        )
        inv = Inventory.objects.select_related('product').get(pk=inv0.pk)
        original_stock = inv.stock
        inv.stock = inv.reserved_stock or 0          # 制造"总库存=预占"，可用为 0
        inv.save(update_fields=["stock"])
        try:
            msg = auto_resume_stockout_pause(Inventory.objects.get(pk=inv0.pk))
            coord = ProductCoordination.objects.get(product_id=prod.id)
            check("可用为 0 时不解除暂停", coord.purchase_paused is True and msg is None,
                  f"paused={coord.purchase_paused} msg={msg}")
        finally:
            Inventory.objects.filter(pk=inv0.pk).update(stock=original_stock)

        # ── ④ 后台列表内直接改 stock 也触发自动恢复 ─────────────
        print("\n④ 列表内直接改 stock（list_editable）也要自动恢复")
        ProductCoordination.objects.filter(product_id=prod.id).update(
            purchase_paused=True, pause_reason=ProductCoordination.PAUSE_STOCKOUT,
            customer_notice="暂时缺货", updated_by="mkt_bot",
        )
        from django.contrib import admin as dj_admin
        admin_obj = dj_admin.site._registry[Inventory]
        inv = Inventory.objects.get(pk=inv0.pk)
        inv.stock = (inv.stock or 0) + 5
        admin_obj.save_model(_FakeRequest(), inv, form=None, change=True)
        coord = ProductCoordination.objects.get(product_id=prod.id)
        check("save_model 补货也解除暂停（否则在列表里补货仍会留下死锁）",
              coord.purchase_paused is False, f"purchase_paused={coord.purchase_paused}")

        # ── ⑤ 后台渲染与筛选口径 ────────────────────────────────
        print("\n⑤ 后台库存页：接单状态列 / 筛选 / 恢复接单接口")
        su = get_user_model().objects.filter(is_superuser=True).first()
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            c = Client()
            c.force_login(su)
            r = c.get("/admin/agent/inventory/")
            h = r.content.decode("utf-8", "ignore")
            check("库存列表可访问且含接单状态列", r.status_code == 200 and "接单状态" in h,
                  f"HTTP {r.status_code}")
            check("筛选器含「已暂停接单」", "已暂停接单" in h)
            r2 = c.get("/admin/agent/inventory/?coord_state=paused")
            check("按「已暂停接单」筛选可用", r2.status_code == 200, f"HTTP {r2.status_code}")
            r3 = c.get("/admin/agent/inventory/?coord_state=buyable")
            check("按「可下单」筛选可用", r3.status_code == 200, f"HTTP {r3.status_code}")

            # 暂停 → 恢复：走后台上的一键接口
            ProductCoordination.objects.filter(product_id=prod.id).update(
                purchase_paused=True, pause_reason=ProductCoordination.PAUSE_MANUAL,
                customer_notice="人工暂停测试", updated_by="admin")
            url = reverse("admin:agent_inventory_coord_toggle")
            r4 = c.post(url, data=json.dumps({"id": inv0.pk, "action": "pause",
                                              "notice": "测试暂停"}),
                        content_type="application/json")
            check("后台「暂停接单」接口可用", r4.status_code == 200, f"HTTP {r4.status_code}")
            coord = ProductCoordination.objects.get(product_id=prod.id)
            check("暂停写入的是 manual 原因（不会自动解除）",
                  coord.pause_reason == ProductCoordination.PAUSE_MANUAL, coord.pause_reason)

            r5 = c.post(url, data=json.dumps({"id": inv0.pk, "action": "resume"}),
                        content_type="application/json")
            coord = ProductCoordination.objects.get(product_id=prod.id)
            inv_now = Inventory.objects.get(pk=inv0.pk)
            check("后台「恢复接单」接口可用", r5.status_code == 200, f"HTTP {r5.status_code}")
            check("恢复后覆盖被清空", coord.purchase_paused is False,
                  f"paused={coord.purchase_paused} notice={coord.customer_notice!r}")
            check("恢复后前台可购买",
                  evaluate(prod, inv_now, coord)["can_buy"] is True)

        # ── ⑥ 前后台口径一致（逐商品）───────────────────────────
        print("\n⑥ 逐商品：前台 can_buy 与后台应显示的状态一致")
        inv_map = {i.product_id: i for i in Inventory.objects.all()}
        coord_map = {c.product_id: c for c in ProductCoordination.objects.all()}
        mismatch = []
        for p in Products.objects.filter(is_active=True):
            v = evaluate(p, inv_map.get(p.id), coord_map.get(p.id))
            # 后台列表里 coord_status 的判定应与前台 can_buy 完全同源
            i = inv_map.get(p.id)
            cd = coord_map.get(p.id)
            backend_buyable = (not (cd and cd.purchase_paused)) and (i.available_stock > 0 if i else False)
            if v["can_buy"] != backend_buyable:
                mismatch.append(p.name)
        check("不存在前后台判定相反的商品", not mismatch, str(mismatch))

    finally:
        restore()
        Inventory.objects.filter(pk=inv0.pk).update(stock=stock0)
        print(f"\n已复原测试数据（库存回到 {stock0}、覆盖表恢复原状）")

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


class _MsgStore:
    """最小可用的 messages 存储：Django 的 message_user 需要 request._messages.add()"""

    def __init__(self):
        self.items = []

    def add(self, level, message, extra_tags=""):
        self.items.append((level, message))


class _FakeRequest:
    """save_model 只用它取 user 与加 message（本用例不校验消息内容）。"""

    def __init__(self):
        from django.contrib.auth import get_user_model as _g
        self.user = _g().objects.filter(is_superuser=True).first()
        self._messages = _MsgStore()


if __name__ == "__main__":
    sys.exit(main())
