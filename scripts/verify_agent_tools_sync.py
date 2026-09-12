"""
AI 客服工具层口径一致性验证（agent/tools.py）

为什么单独验这一层：AI 客服是**直接对客户说话**的入口。它说"库存充足、建议下单"，
客户就会去下单然后失败；它说"共 3 笔订单"而个人中心显示 6 笔，客户会投诉数据不对。
这层原先自己算口径（`inv.stock == 0` 判缺货、不读跨 Agent 暂停、截断列表当总数），
本脚本确保它已改为复用 `purchase_guard` 与统一归属规则。

覆盖：
  ① 暂停接单的商品：query_product / check_inventory / calculate_quote
     都必须明确"不可下单"，且**不得**出现"库存充足 / 建议尽快下单"等误导表述
  ② 可购商品：工具结论必须与 purchase_guard 判定一致（不会一边说能买一边拦）
  ③ 区分总库存与可用库存（被订单预占的部分不能算"能卖"）
  ④ get_order_status：发过货的订单要给物流（含已完成），有售后的要给售后进展
  ⑤ query_orders_by_phone：报的是**真实总笔数**，不是截断后的条数
  ⑥ calculate_quote：不可购 / 超量的商品必须带出提醒

用法：./.venv/Scripts/python.exe scripts/verify_agent_tools_sync.py
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agent.models import Inventory, Orders, OrderStatus, ProductCoordination, Products  # noqa: E402
from agent.purchase_guard import evaluate                                              # noqa: E402
from agent import tools                                                                # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


# 这些词在「不可购」场景下出现就会误导客户去下单
MISLEADING = ("库存充足", "建议尽快下单", "可正常接单")


def _temp_paused(product, reason=None):
    """上下文管理器：临时把某商品置为「暂停接单」，退出时精确还原。

    为什么要自己造场景：本节原先依赖"库里存在被暂停的商品"，结果那条死锁
    被修掉之后，**本节 7 项断言全部静默消失**（测试总数 26 → 19）——
    而它恰恰是这一层最关键的用例（AI 不得对客户承诺买不到的东西）。
    """
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        coord = ProductCoordination.objects.filter(product_id=product.id).first()
        snap = None if coord is None else (
            coord.pk, coord.purchase_paused, coord.pause_reason,
            coord.customer_notice, coord.updated_by)
        created = False
        if coord is None:
            coord = ProductCoordination.objects.create(product_id=product.id)
            created = True
        coord.purchase_paused = True
        coord.pause_reason = reason or ProductCoordination.PAUSE_MANUAL
        coord.customer_notice = "「%s」暂时缺货，可先收藏或咨询客服。" % product.name
        coord.updated_by = "verify_agent_tools_sync"
        coord.save()
        try:
            yield
        finally:
            if created:
                coord.delete()
            else:
                pk, paused, rsn, notice, by = snap
                ProductCoordination.objects.filter(pk=pk).update(
                    purchase_paused=paused, pause_reason=rsn or "",
                    customer_notice=notice or "", updated_by=by or "")

    return _ctx()


def main():
    # ── ① 暂停接单商品（自己造场景，不依赖库里碰巧有的数据）──────
    paused = next(
        (p for p in Products.objects.filter(is_active=True)
         if Inventory.objects.filter(product_id=p.id, stock__gt=0).exists()),
        None,
    )

    if paused is not None:
        with _temp_paused(paused):
            inv = Inventory.objects.filter(product_id=paused.id).first()
            coord = ProductCoordination.objects.filter(product_id=paused.id).first()
            verdict = evaluate(paused, inv, coord)
            print(f"① 暂停接单商品（临时构造，用后还原）：#{paused.id} {paused.name}"
                  f"（stock={inv.stock if inv else '-'} reserved={inv.reserved_stock if inv else '-'}）")
            check("构造生效：该商品已不可购", verdict["can_buy"] is False,
                  f"can_buy={verdict['can_buy']} reason={verdict['reason']}")

            out = tools.query_product(paused.name)
            check("query_product 明确告知不可下单", "暂不可下单" in out or "暂停接单" in out,
                  out.splitlines()[-1][:60])
            check("query_product 不出现误导表述",
                  not any(w in out for w in MISLEADING),
                  next((w for w in MISLEADING if w in out), "无"))

            out2 = tools.check_inventory(paused.name)
            check("check_inventory 明确告知不可购", "⛔" in out2 or "暂不可下单" in out2,
                  out2.splitlines()[-1][:60])
            check("check_inventory 不出现误导表述",
                  not any(w in out2 for w in MISLEADING),
                  next((w for w in MISLEADING if w in out2), "无"))
            check("check_inventory 引导换购/留资，而非让客户下单",
                  "换购" in out2 or "补货" in out2, out2.splitlines()[-1][:60])

            out3 = tools.calculate_quote([{"name": paused.name, "qty": 2}])
            check("calculate_quote 标注不可下单", "不可下单" in out3 or "无法按本单数量" in out3,
                  out3.splitlines()[-1][:60])
            check("calculate_quote 仍给出金额（报价不因缺货消失）", "合计" in out3)
        print("    (已还原该商品原先的接单状态)")
    else:
        print("① 无在售且有库存的商品，无法构造暂停场景，跳过")

    # ── ② / ③ 可购商品：工具结论 == purchase_guard 判定 ──────────
    print("\n②③ 可购商品：工具结论与 purchase_guard 一致")
    for prod in Products.objects.filter(is_active=True)[:6]:
        inv = Inventory.objects.filter(product_id=prod.id).first()
        v = evaluate(prod, inv, ProductCoordination.objects.filter(
            product_id=prod.id).first())
        out_i = tools.check_inventory(prod.name)
        out_p = tools.query_product(prod.name)

        if v["can_buy"]:
            ok_i = "可正常接单" in out_i or "库存充足" in out_i
            ok_p = "当前可下单" in out_p
        else:
            ok_i = ("⛔" in out_i) and not any(w in out_i for w in MISLEADING)
            ok_p = "暂不可下单" in out_p
        check(f"#{prod.id} {prod.name}（{'可购' if v['can_buy'] else '不可购'}）两工具一致",
              ok_i and ok_p,
              f"check_inventory={'OK' if ok_i else '不一致'} query_product={'OK' if ok_p else '不一致'}")

        # 可用库存数字必须一致（工具不能自己算 stock）
        if inv and v["can_buy"]:
            check(f"#{prod.id} 可用库存数字与判定一致",
                  str(v["available_stock"]) in out_i and str(v["available_stock"]) in out_p,
                  f"guard={v['available_stock']}")

    # 预占场景：总库存 != 可用库存时必须区分（同样自己造，不靠现有数据）
    print("\n③ 总库存与可用库存的区分（临时构造预占）")
    target = Inventory.objects.filter(stock__gt=3).select_related('product').first()
    if target is None:
        print("  (无可用库存基数足够的商品，跳过)")
    else:
        old_reserved = target.reserved_stock or 0
        try:
            Inventory.objects.filter(pk=target.pk).update(reserved_stock=old_reserved + 3)
            target.refresh_from_db()
            p = target.product
            out = tools.check_inventory(p.name)
            avail = target.available_stock
            check("工具同时给出总库存与可用库存",
                  "总库存" in out and "可用库存" in out
                  and f"预占 {target.reserved_stock}" in out,
                  f"{p.name}: stock={target.stock} reserved={target.reserved_stock} avail={avail}")
            check("可用库存 = 总库存 − 预占（工具没自己算错）",
                  avail == (target.stock or 0) - (target.reserved_stock or 0),
                  f"{target.stock} - {target.reserved_stock} = {avail}")
        finally:
            Inventory.objects.filter(pk=target.pk).update(reserved_stock=old_reserved)
            print(f"    (已还原 {target.product.name} 的预占为 {old_reserved})")

    # ── ④ 订单状态：物流（含已完成）+ 售后进展 ──────────────────
    print("\n④ get_order_status 的物流与售后")
    done = Orders.objects.filter(status=OrderStatus.COMPLETED).exclude(
        tracking_no__isnull=True).exclude(tracking_no="").first()
    if done:
        out = tools.get_order_status(done.order_no)
        check("已完成订单仍返回物流运单号（原先只在'已发货'时给）",
              done.tracking_no in out, f"{done.order_no} 运单 {done.tracking_no}")
    else:
        print("  (无已完成的带运单订单，跳过)")

    ret = Orders.objects.exclude(return_status="").first()
    if ret:
        out = tools.get_order_status(ret.order_no)
        check("有售后的订单会报出售后进展",
              ret.return_status in out and "售后进展" in out,
              f"{ret.order_no} return_status={ret.return_status}")
        if ret.return_reason:
            check("售后进展含客户退货原因", ret.return_reason in out, ret.return_reason[:30])
    else:
        print("  (无带售后状态的订单，跳过)")

    # ── ⑤ 按手机号查订单：总数必须真实 ──────────────────────────
    print("\n⑤ query_orders_by_phone 的条数口径")
    from django.db.models import Count
    top = (Orders.objects.exclude(phone__isnull=True).exclude(phone="")
           .values("phone").annotate(n=Count("id")).order_by("-n").first())
    if top:
        phone, real_n = top["phone"], top["n"]
        out = tools.query_orders_by_phone(phone)
        cap = 20
        if real_n > cap:
            check(f"超过 {cap} 笔时报出真实总笔数 {real_n}",
                  f"共 {real_n} 笔" in out and f"最近 {cap} 笔" in out,
                  out.splitlines()[0][:70])
        else:
            check(f"未超过 {cap} 笔时报出真实总笔数 {real_n}",
                  f"共 {real_n} 笔" in out, out.splitlines()[0][:70])
        check("手机号已脱敏（不出现完整号码）", phone not in out, phone)
    else:
        print("  (库中无带手机号的订单，跳过)")

    # ── ⑥ calculate_quote 超量提醒 ──────────────────────────────
    print("\n⑥ calculate_quote 的超量提醒")
    buyable = None
    for prod in Products.objects.filter(is_active=True):
        inv = Inventory.objects.filter(product_id=prod.id).first()
        v = evaluate(prod, inv, ProductCoordination.objects.filter(
            product_id=prod.id).first())
        if v["can_buy"] and v["available_stock"] > 0:
            buyable = (prod, v["available_stock"])
            break
    if buyable:
        prod, avail = buyable
        out = tools.calculate_quote([{"name": prod.name, "qty": avail + 1000}])
        check("超量报价会提示超出可用库存",
              "超出可用库存" in out and "无法按本单数量成交" in out,
              out.strip().splitlines()[-1][:70])
        out_ok = tools.calculate_quote([{"name": prod.name, "qty": 1}])
        check("正常数量报价不误报提醒",
              "无法按本单数量成交" not in out_ok, out_ok.strip().splitlines()[-1][:50])
    else:
        print("  (无可购商品，跳过)")

    print("\n" + "=" * 64)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for f in FAIL:
        print("  ✗", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
