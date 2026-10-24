"""
契约验证：产品名片的价格口径（任务书四·2 名片含价格 + 二·5 身份差异化）—— ZT-agent

为什么单独验这张卡
------------------
产品名片不是「AI 说的话」，而是**直接下发到浏览器的结构化数据**。
所以它有两类事故，靠肉眼在页面上看不出来：

  1. **批发价泄露**：个体钓友的名片里带着 `wholesale_price`，前端就算不显示，
     按 F12 也能看见 —— 与 `pricing.py` 第 1 条约束（接口层裁剪）直接冲突。
     改造前这里正是无条件把零售价 + 批发价一起塞进卡片。
  2. **名片价与成交价不一致**：卡片只认 `retail_price`，经销商达到起订量后
     实际按批发价成交，卡片却还写着零售价 —— 客户会以为系统算错钱。

本脚本守的口径
--------------
  A. 身份裁剪：个体 / 未登录 / 无批发价商品的经销商，响应体里**没有**
     `wholesale_price` 这个键（不是设成 None）；经销商才有。
  B. 同源：名片的 `effective_price` / `retail_price` 必须等于
     `pricing.price_for()` / `pricing.retail_of()` 的结果，不许自己算。
  C. 可购性：`can_buy` 等于 `purchase_guard.evaluate()` 的判定（卡片说能买就必须真能买）。
  D. 去重：同一商品在一轮里只登记一次。
  E. 端到端：真的跑一遍 `agent_stream`（mock LLM 流），确认名片事件里的价格
     随身份变化 —— 单测通过但事件不带字段，是这套链路最容易漏的一环。

自造场景 → 断言 → 精确还原：临时用户 / 商品 / 库存结束时全部删除。

用法：python scripts/verify_product_card.py
"""
import os
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from agent import agent as agent_mod  # noqa: E402
from agent import llm, pricing, tools  # noqa: E402
from agent.models import Inventory, Products, UserProfile  # noqa: E402
from agent.purchase_guard import evaluate, load_context  # noqa: E402

PASS, FAIL = [], []
RETAIL, WHOLESALE = 9.5, 6.25


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def card_of(product):
    """调工具 → 取本轮登记的名片（走真实入口，不直接构造 dict）。"""
    tools.reset_referenced_products()
    tools.query_product(product.name)
    cards = tools.get_referenced_products()
    return cards[0] if cards else {}


User = get_user_model()
suffix = uuid.uuid4().hex[:8]
users, products = [], []

print("=" * 62)
print("  产品名片价格口径 验收")
print("=" * 62)

try:
    # ── 自造场景 ────────────────────────────────────────────────
    ind = User.objects.create_user(username=f"pc_ind_{suffix}", password="CardTest!23456")
    whs = User.objects.create_user(username=f"pc_whs_{suffix}", password="CardTest!23456")
    users = [ind, whs]
    UserProfile.objects.update_or_create(
        user=ind, defaults={"customer_type": UserProfile.CustomerType.INDIVIDUAL})
    UserProfile.objects.update_or_create(
        user=whs, defaults={"customer_type": UserProfile.CustomerType.WHOLESALE})

    p_dual = Products.objects.create(
        sku=f"PC{suffix[:5].upper()}A", name=f"双价饵料{suffix}",
        spec="120g/包", target_fish="鲫鱼", category="验证分类",
        retail_price=RETAIL, wholesale_price=WHOLESALE, is_active=1,
        description="名片价格验证用商品",
    )
    p_retail_only = Products.objects.create(
        sku=f"PC{suffix[:5].upper()}B", name=f"仅零售饵料{suffix}",
        spec="120g/包", target_fish="鲤鱼", category="验证分类",
        retail_price=RETAIL, wholesale_price=None, is_active=1,
    )
    products = [p_dual, p_retail_only]
    for p in products:
        Inventory.objects.update_or_create(
            product=p, defaults={"stock": 80, "reserved_stock": 0, "alert_line": 5})

    # ── A. 身份裁剪 ─────────────────────────────────────────────
    print("\n[A] 批发价按身份裁剪（键不存在，而不是设成 None）")

    tools.set_current_user(ind)
    c_ind = card_of(p_dual)
    check("个体钓友：名片不含 wholesale_price 键", "wholesale_price" not in c_ind,
          str(sorted(c_ind)))
    check("个体钓友：can_see_wholesale 为 False", c_ind.get("can_see_wholesale") is False)
    check("个体钓友：customer_type == individual",
          c_ind.get("customer_type") == UserProfile.CustomerType.INDIVIDUAL,
          str(c_ind.get("customer_type")))

    tools.set_current_user(None)
    c_anon = card_of(p_dual)
    check("未登录访客：名片不含 wholesale_price 键", "wholesale_price" not in c_anon)
    check("未登录访客：按个体处理", c_anon.get("customer_type") == UserProfile.CustomerType.INDIVIDUAL)

    tools.set_current_user(whs)
    c_whs = card_of(p_dual)
    check("经销商：名片含 wholesale_price", c_whs.get("wholesale_price") == WHOLESALE,
          str(c_whs.get("wholesale_price")))
    check("经销商：含起订量 wholesale_min_qty", c_whs.get("wholesale_min_qty") == pricing.WHOLESALE_MIN_QTY,
          str(c_whs.get("wholesale_min_qty")))

    c_whs_none = card_of(p_retail_only)
    check("商品无批发价：经销商名片也不含该键（不能给 0）",
          "wholesale_price" not in c_whs_none, str(sorted(c_whs_none)))

    # ── B. 与 pricing 同源 ──────────────────────────────────────
    print("\n[B] 名片价格与 pricing 唯一出口同源")

    tools.set_current_user(ind)
    c_ind = card_of(p_dual)
    check("个体：effective_price == price_for(product, user, 1)",
          c_ind.get("effective_price") == pricing.price_for(p_dual, ind, 1),
          f"{c_ind.get('effective_price')} vs {pricing.price_for(p_dual, ind, 1)}")
    check("个体：effective_price 就是零售价", c_ind.get("effective_price") == RETAIL)

    tools.set_current_user(whs)
    c_whs = card_of(p_dual)
    check("经销商：单包价仍是零售价（未达起订量）",
          c_whs.get("effective_price") == RETAIL, str(c_whs.get("effective_price")))
    check("经销商：达到起订量后 price_for 才走批发价",
          pricing.price_for(p_dual, whs, pricing.WHOLESALE_MIN_QTY) == WHOLESALE)
    check("名片 retail_price == retail_of()",
          c_whs.get("retail_price") == pricing.retail_of(p_dual))
    check("名片价格字段是数字而不是字符串",
          isinstance(c_whs.get("effective_price"), float)
          and isinstance(c_whs.get("retail_price"), float))

    # ── C. 可购性同源 ───────────────────────────────────────────
    print("\n[C] 可购性与 purchase_guard 同源")

    inv_map, coord_map = load_context([p_dual.id])
    verdict = evaluate(p_dual, inv_map.get(p_dual.id), coord_map.get(p_dual.id))
    check("can_buy 等于 purchase_guard 判定", c_whs.get("can_buy") == verdict["can_buy"])
    check("available_stock 等于可用库存", c_whs.get("available_stock") == verdict["available_stock"],
          f"{c_whs.get('available_stock')} vs {verdict['available_stock']}")

    # ── D. 去重 ─────────────────────────────────────────────────
    print("\n[D] 同一商品一轮只登记一次")
    tools.reset_referenced_products()
    tools.query_product(p_dual.name)
    tools.query_product(p_dual.name)
    cards = tools.get_referenced_products()
    check("重复查询只留一张名片", len(cards) == 1, str(len(cards)))

    # ── E. 端到端（mock LLM 流） ────────────────────────────────
    print("\n[E] 端到端：Agent 真的会把裁剪后的价格放进名片事件")

    class _Fn:
        def __init__(self, name=None, arguments=None):
            self.name, self.arguments = name, arguments

    class _TC:
        def __init__(self, index, id=None, function=None):
            self.index, self.id, self.function = index, id, function

    class _Delta:
        def __init__(self, content=None, tool_calls=None):
            self.content, self.tool_calls = content, tool_calls

    class _Choice:
        def __init__(self, delta):
            self.delta = delta

    class _Chunk:
        def __init__(self, delta):
            self.choices = [_Choice(delta)]

    def run_stream(user, question):
        calls = {"n": 0}

        def fake_chat_stream(messages, tools=None, tool_choice=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return iter([_Chunk(_Delta(tool_calls=[
                    _TC(0, id="call_1", function=_Fn(
                        "query_product", '{"name": "%s"}' % p_dual.name)),
                ]))])
            return iter([_Chunk(_Delta(content="这款是验证用饵料。"))])

        real = llm.chat_stream
        llm.chat_stream = fake_chat_stream
        tools.set_current_user(user)
        try:
            return list(agent_mod.agent_stream(f"verify-card-{suffix}-{id(user)}", question))
        finally:
            llm.chat_stream = real

    events_whs = run_stream(whs, "双价饵料多少钱？")
    cards_whs = [e["product"] for e in events_whs if e.get("type") == "product_card"]
    check("事件流里出现了 product_card", len(cards_whs) == 1, str(len(cards_whs)))
    if cards_whs:
        card = cards_whs[0]
        check("端到端（经销商）：名片带批发价", card.get("wholesale_price") == WHOLESALE,
              str(card.get("wholesale_price")))
        check("端到端（经销商）：生效价与 price_for 一致",
              card.get("effective_price") == pricing.price_for(p_dual, whs, 1))

    events_ind = run_stream(ind, "双价饵料多少钱？")
    cards_ind = [e["product"] for e in events_ind if e.get("type") == "product_card"]
    check("端到端（个体）：同样产出名片", len(cards_ind) == 1, str(len(cards_ind)))
    if cards_ind:
        check("端到端（个体）：名片**不含** wholesale_price 键",
              "wholesale_price" not in cards_ind[0], str(sorted(cards_ind[0])))

finally:
    tools.reset_referenced_products()
    tools.set_current_user(None)
    for p in products:
        Inventory.objects.filter(product=p).delete()
    Products.objects.filter(sku__startswith=f"PC{suffix[:5].upper()}").delete()
    for u in users:
        UserProfile.objects.filter(user=u).delete()
    User.objects.filter(username__in=[u.username for u in users]).delete()
    print("\n（自造数据已清理）")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
