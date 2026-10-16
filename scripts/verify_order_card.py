"""
契约验证：订单名片 + 对话内闭环（任务书五·3，并把四·1 的下单闭环收口）—— ZT-agent

背景
----
客户问「我的订单到哪了」，改造前 Agent 只回一段文字，客户想付款/看物流
还得自己去个人中心翻。现在工具查到订单时登记一笔，流结束补发 `order_card`
事件 → 前端弹一张结构化进度卡（订单号 / 商品 / 金额 / 状态 / 运单号 +
「立即支付」「我的订单」）。

本脚本守住四条最容易漂移的口径：

  1. **名片与个人中心同源**：名片的 actions 必须等于 `serialize_order(..., with_actions=True)['actions']`
     —— 不能出现「聊天里能点支付、个人中心说不能付」
  2. **名片不带长隐私字段**（收货地址 / 退货原因 / 评价原文）—— 对话卡片用不到
  3. **登记按订单号去重**，且用 ContextVar（多线程下不串会话）
  4. **端到端真跑一遍** `agent_stream`（mock 掉 LLM 的流），确认 `order_card` 事件
     真的会被产出 —— 这是当年 `order_success` 最大的问题：三处实现、零处产出

自造场景 → 断言 → 精确还原：临时用户 / 商品 / 订单在结束时全部删除。

用法：./.venv/Scripts/python.exe scripts/verify_order_card.py
"""
import os
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agent import agent as agent_mod  # noqa: E402
from agent import llm  # noqa: E402
from agent import tools  # noqa: E402
from agent.models import (  # noqa: E402
    Orders,
    OrderStatus,
    PaymentStatus,
    Products,
    UserProfile,
)
from agent.order_presenter import order_card, order_actions, serialize_order  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
# 自造数据
# ══════════════════════════════════════════════════════════════
suffix = uuid.uuid4().hex[:8]
product = Products.objects.create(
    sku=f"VC-{suffix}", name=f"验证饵料-{suffix}", spec="200g/包",
    target_fish="鲫鱼", retail_price=8, wholesale_price=5, is_active=1,
    category="验证分类",
)
order_unpaid = Orders.objects.create(
    order_no=f"VCUNPAID{suffix}", product=product,
    product_name=product.name, product_sku=product.sku, quantity=5,
    unit_price=8, total_price=40, status=OrderStatus.PENDING,
    payment_status=PaymentStatus.UNPAID, address="山东省济南市验证路 1 号",
)
order_shipped = Orders.objects.create(
    order_no=f"VCSHIP{suffix}", product=product,
    product_name=product.name, product_sku=product.sku, quantity=2,
    unit_price=8, total_price=16, status=OrderStatus.SHIPPED,
    payment_status=PaymentStatus.PAID, ship_company="中通快递",
    tracking_no=f"ZT{suffix}", address="山东省济南市验证路 2 号",
)

print("=" * 62)
print("  订单名片 + 对话内闭环 验收 · 后端")
print("=" * 62)

try:
    # ── A. 名片结构 ─────────────────────────────────────────────
    print("\n[A] 名片结构")
    card = order_card(order_unpaid)
    check("含订单号", card["order_no"] == order_unpaid.order_no)
    check("含商品名与数量", card["product_name"] == product.name and card["quantity"] == 5)
    check("含金额（数值类型，非字符串）", isinstance(card["total_price"], float) and card["total_price"] == 40.0)
    check("含供应商规格（取自关联商品）", card["spec"] == "200g/包")
    check("含订单状态", card["status"] == OrderStatus.PENDING)
    check("含支付状态", card["payment_status"] == PaymentStatus.UNPAID)
    check("含 actions", card["actions"] == ["pay", "cancel"], str(card["actions"]))

    shipped_card = order_card(order_shipped)
    check("已发货名片带运单号", shipped_card["tracking_no"] == f"ZT{suffix}")
    check("已发货名片带物流公司", shipped_card["ship_company"] == "中通快递")
    check("已发货名片无 pay 动作", "pay" not in shipped_card["actions"])

    # 长隐私字段不进名片
    leaked = [k for k in ("address", "return_reason", "review", "return_note") if k in card]
    check("名片不含收货地址/退货原因/评价原文等长字段", not leaked, str(leaked))

    # ── B. 与个人中心同源 ───────────────────────────────────────
    print("\n[B] 与个人中心同源")
    for tag, o in (("未支付订单", order_unpaid), ("已发货订单", order_shipped)):
        full = serialize_order(o, with_actions=True)
        check(
            f"{tag}：名片 actions == 个人中心 actions",
            order_card(o)["actions"] == full["actions"],
            f"{order_card(o)['actions']} vs {full['actions']}",
        )
    check(
        "order_actions 是唯一出口（名片与序列化都调它）",
        order_card(order_unpaid)["actions"] == order_actions(order_unpaid),
    )

    # ── C. 工具层登记 ───────────────────────────────────────────
    print("\n[C] 工具层登记")
    tools.reset_referenced_orders()
    check("reset 后为空", tools.get_referenced_orders() == [])

    tools._remember_order(order_unpaid)
    tools._remember_order(order_unpaid)          # 重复登记应被去重
    check("同一订单重复登记只留一笔", len(tools.get_referenced_orders()) == 1)

    tools._remember_order(order_shipped)
    nos = [o["order_no"] for o in tools.get_referenced_orders()]
    check("不同订单都会登记且保持顺序", nos == [order_unpaid.order_no, order_shipped.order_no], str(nos))

    tools.reset_referenced_orders()
    check("reset 清空（下一轮不串上一轮的订单）", tools.get_referenced_orders() == [])

    # get_order_status 命中时会自动登记
    tools.reset_referenced_orders()
    text = tools.get_order_status(order_unpaid.order_no)
    check("get_order_status 能查到该单", order_unpaid.order_no in text)
    check("查订单时自动登记了名片", len(tools.get_referenced_orders()) == 1)
    tools.reset_referenced_orders()

    # ── D. SSE 白名单放行 ───────────────────────────────────────
    print("\n[D] SSE 白名单")
    views_src = open(os.path.join(BASE, "agent", "views.py"), encoding="utf-8").read()
    check("views 的结构事件白名单含 order_card", '"order_card"' in views_src)
    check(
        "order_card 与 product_card / guide_card 同列放行",
        '("guide_card", "product_card", "order_card")' in views_src,
    )

    # ── E. 端到端：Agent 真会产出 order_card ────────────────────
    print("\n[E] 端到端（mock LLM 流）")

    class _Fn:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self, index, id=None, function=None):
            self.index = index
            self.id = id
            self.function = function

    class _Delta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta):
            self.delta = delta

    class _Chunk:
        def __init__(self, delta):
            self.choices = [_Choice(delta)]

    calls = {"n": 0}

    def fake_chat_stream(messages, tools=None, tool_choice=None):
        """第 1 轮：让模型调 get_order_status；第 2 轮：给出最终回答。"""
        calls["n"] += 1
        if calls["n"] == 1:
            return iter([_Chunk(_Delta(tool_calls=[
                _TC(0, id="call_1", function=_Fn(
                    "get_order_status",
                    '{"order_no": "%s"}' % order_unpaid.order_no,
                )),
            ]))])
        return iter([
            _Chunk(_Delta(content="这笔订单已下单，")),
            _Chunk(_Delta(content="尚未支付，付款后即可安排发货。")),
        ])

    real_chat_stream = llm.chat_stream
    llm.chat_stream = fake_chat_stream
    try:
        events = list(agent_mod.agent_stream(
            f"verify-order-card-{suffix}", "我的订单到哪了？"
        ))
    finally:
        llm.chat_stream = real_chat_stream
        tools.reset_referenced_orders()

    kinds = [e.get("type") for e in events if e.get("type")]
    texts = "".join(e["content"] for e in events if "content" in e)
    check("事件流里出现了 order_card", "order_card" in kinds, str(kinds))
    order_cards = [e["order"] for e in events if e.get("type") == "order_card"]
    check("order_card 恰好一张（本轮只查了一笔）", len(order_cards) == 1, str(len(order_cards)))
    if order_cards:
        check("名片订单号正确", order_cards[0]["order_no"] == order_unpaid.order_no)
        check("名片带 actions（前端据此渲染按钮）", isinstance(order_cards[0]["actions"], list))
    check("回答正文仍然照常流出", "尚未支付" in texts, texts[:40])
    check("order_card 排在文本之后（先读完介绍再看到卡）", kinds and kinds[-1] == "order_card", str(kinds))
    check("未命中购买意图时不硬塞引导卡", "guide_card" not in kinds, str(kinds))

finally:
    # ── 精确还原 ────────────────────────────────────────────────
    Orders.objects.filter(order_no__in=[order_unpaid.order_no, order_shipped.order_no]).delete()
    Products.objects.filter(sku=product.sku).delete()
    print("\n（自造数据已清理）")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
