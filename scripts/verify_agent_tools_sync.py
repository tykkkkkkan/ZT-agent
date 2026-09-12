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


def main():
    # ── ① 暂停接单商品 ──────────────────────────────────────────
    paused = None
    for coord in ProductCoordination.objects.filter(purchase_paused=True):
        p = Products.objects.filter(pk=coord.product_id).first()
        if p and p.is_active:
            paused = p
            break

    if paused:
        inv = Inventory.objects.filter(product_id=paused.id).first()
        verdict = evaluate(paused, inv, ProductCoordination.objects.filter(
            product_id=paused.id).first())
        print(f"① 暂停接单商品：#{paused.id} {paused.name}"
              f"（stock={inv.stock if inv else '-'} reserved={inv.reserved_stock if inv else '-'}）")

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
    else:
        print("① 当前无「暂停接单」商品，跳过（可通过营销侧下发 pause_product 造场景）")

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

    # 预占场景：总库存 != 可用库存时必须区分
    print("\n③ 总库存与可用库存的区分")
    reserved_case = Inventory.objects.filter(reserved_stock__gt=0, stock__gt=0).first()
    if reserved_case:
        p = reserved_case.product
        out = tools.check_inventory(p.name)
        avail = reserved_case.available_stock
        check("工具同时给出总库存与可用库存",
              "总库存" in out and "可用库存" in out and f"预占 {reserved_case.reserved_stock}" in out,
              f"{p.name}: stock={reserved_case.stock} reserved={reserved_case.reserved_stock} avail={avail}")
    else:
        print("  (当前无预占中的库存，跳过)")

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
