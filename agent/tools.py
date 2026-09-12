"""
agent/tools.py — Agent 工具函数 + 结构化工具 Schema

对齐实习 JD「Function Calling / Tool Use」：
- TOOL_SCHEMAS：标准 JSON Schema，供 LLM 的 function calling 使用（tool_choice=auto）
- TOOL_REGISTRY + call_tool：本地执行入口，统一参数校验与异常兜底
- search_knowledge 已接入 RAG 引擎（见 agent/knowledge_data.py）

产品定位：AI 客服 = 咨询顾问 + 引导转化（不代客下单）。
顾客购买 / 定制意向 → 调用 guide_to_contact 引导到定制服务页 / 联系我们页。

所有工具返回中文自然语言字符串，由上层 Agent 消化后组织回答。

⚠️ 口径铁律（本文件曾因各写一套而误导客户，改动前务必先读）
--------------------------------------------------------------
AI 客服是**直接对客户说话**的入口，它说"有货"客户就真去买。因此：

  1. **可购性一律问 `agent.purchase_guard`**，不许自己拿 `inv.stock` 判断。
     曾经的 bug：`check_inventory` 用 `inv.stock == 0` 判缺货 ——
     若 stock=5 但 reserved=5（全被订单预占），可用库存其实是 0、前台已不可购，
     AI 却告诉客户"库存充足"；同理完全不读 `ProductCoordination.purchase_paused`，
     营销侧已暂停接单的商品，AI 仍在推荐。
  2. **订单状态一律带售后维度**（`return_status` / `completed_at`），
     不能只报 `status`，否则客户问"我退了货"时 AI 答不上来。
  3. **条数一律报真实总数**，不能拿截断后的列表长度当"共 N 笔"。
"""
import os

from agent.models import Products, Inventory, Orders


# ──────────────────────────────────────────────────────────────
# 共用口径助手（唯一来源：purchase_guard + 统一的售后状态描述）
# ──────────────────────────────────────────────────────────────
def _buyability(product, inv=None) -> dict:
    """取可购性判定（复用 agent.purchase_guard，绝不自己算）。

    inv 仅为兼容调用方已有实例；库存与协调覆盖统一从 load_context 取，
    保证与前台 /api/products/ 读的是同一份数据。
    """
    from agent.purchase_guard import load_context, evaluate

    inv_map, coord_map = load_context([product.id])
    return evaluate(product, inv_map.get(product.id), coord_map.get(product.id))


def _availability_line(product, inv) -> str:
    """把可购性翻译成一句面向客户的中文（AI 会引用它，所以必须与前台一致）。"""
    v = _buyability(product, inv)
    if v["can_buy"]:
        line = f"当前可下单：现货 {v['available_stock']} 包"
        if v.get("customer_notice"):
            line += f"（提示：{v['customer_notice']}）"
        return line
    # 不可购：把原因说清楚，避免 AI 自己编"有货"
    reason_text = {
        "purchase_paused": "该商品已暂停接单",
        "sold_out": "该商品已售罄",
        "no_inventory": "该商品暂无库存记录",
        "inactive": "该商品已下架",
    }.get(v["reason"], "该商品暂时无法下单")
    return f"{reason_text}，暂不可下单。{v['message']}"


def _after_sale_line(order) -> str:
    """售后/收货闭环的补充说明（status 之外的第二维度）。"""
    parts = []
    if order.return_status:
        seg = f"退货{order.return_status}"
        if order.return_reason:
            seg += f"（客户原因：{order.return_reason}）"
        if order.return_note:
            seg += f"，商家处理：{order.return_note}"
        if order.return_requested_at:
            seg += f"，{order.return_requested_at:%Y-%m-%d} 申请"
        parts.append(seg)
    if order.completed_at:
        parts.append(f"客户已于 {order.completed_at:%Y-%m-%d} 确认收货")
    return "；".join(parts)


def _logistics_parts(order) -> list:
    """物流信息（只要发过货就展示 —— 已完成的订单同样需要运单号）。"""
    parts = []
    if order.ship_company:
        parts.append(f"物流 {order.ship_company}")
    if order.tracking_no:
        parts.append(f"运单号 {order.tracking_no}")
    if order.shipped_at:
        parts.append(f"发货 {order.shipped_at:%Y-%m-%d}")
    return parts


# ──────────────────────────────────────────────────────────────
# 产品查询
# ──────────────────────────────────────────────────────────────
def query_product(name: str) -> str:
    """按产品名查询，返回中文描述（含 SKU、库存与**可购性**）。"""
    try:
        product = Products.objects.filter(name__icontains=name).first()
    except Exception as e:
        return f"查询产品出错：{e}"

    if not product:
        return f"没找到叫「{name}」的产品，麻烦确认下名字？"

    inv = Inventory.objects.filter(product=product).first()
    lines = [
        f"产品：{product.name}",
        f"编码：{product.sku or '（未设置）'}",
        f"规格：{product.spec or '无'}",
        f"适用鱼种：{product.target_fish or '无'}",
        f"零售价：{product.retail_price} 元/包",
        f"批发价：{product.wholesale_price} 元/包",
    ]
    if inv:
        lines.append(f"可用库存：{inv.available_stock} 包（已预占 {inv.reserved_stock}）")
    # 可购性：与前台产品页/下单页同源判定，避免 AI 承诺了却下不了单
    lines.append(_availability_line(product, inv))
    if product.description:
        lines.append(f"描述：{product.description}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 库存查询
# ──────────────────────────────────────────────────────────────
def check_inventory(name: str) -> str:
    """按产品名查库存，低于预警线要预警。

    以**可用库存**（stock − reserved_stock）为"能不能卖"的依据，
    并叠加跨 Agent 暂停状态 —— 这两点此前都缺失。
    """
    try:
        product = Products.objects.filter(name__icontains=name).first()
        if not product:
            return f"没找到叫「{name}」的产品，无法查库存。"
        inv = Inventory.objects.filter(product=product).first()
    except Exception as e:
        return f"查询库存出错：{e}"

    if not inv:
        return f"「{product.name}」暂无库存记录，暂不可下单。"

    verdict = _buyability(product, inv)
    total = inv.stock or 0
    reserved = inv.reserved_stock or 0
    available = verdict["available_stock"]
    alert = inv.alert_line or 0

    lines = [
        f"产品：{product.name}",
        f"总库存：{total} 包（其中已被订单预占 {reserved} 包）",
        f"可用库存：{available} 包",
        f"预警线：{alert} 包",
    ]
    if not verdict["can_buy"]:
        # 不可购时不能再说"建议尽快下单"，那会引导客户去下单然后失败
        lines.append(f"⛔ {_availability_line(product, inv)}")
        lines.append("请引导客户换购同类商品，或留下联系方式等补货后回访。")
    elif available <= 0:
        lines.append("⚠️ 可用库存为 0（可能已被订单预占），客户当前下不了单，请核实。")
    elif available < alert:
        lines.append(f"⚠️ 库存偏少（可用 {available} 包 < 预警线 {alert}），建议尽快补货。")
    else:
        lines.append("库存充足，可正常接单。")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 订单状态查询
# ──────────────────────────────────────────────────────────────
def get_order_status(order_no: str) -> str:
    """按订单号查询订单状态（含物流与售后进展）。"""
    try:
        order = Orders.objects.filter(order_no=order_no).first()
    except Exception as e:
        return f"查询订单出错：{e}"

    if not order:
        return f"没找到订单号「{order_no}」，麻烦确认下号码？"

    lines = [
        f"订单号：{order.order_no}",
        f"客户：{order.customer_name or '无'}",
        f"商品：{order.product_name or '无'}",
        f"数量：{order.quantity or 0}",
        f"总价：{order.total_price or 0} 元",
        f"状态：{order.status or '未知'}",
        f"下单时间：{order.created_at or '未知'}",
    ]
    # 物流：只要发过货就给（已完成订单同样需要运单号；原先只在"已发货"时给）
    logistics = _logistics_parts(order)
    if logistics:
        lines.append("｜".join(logistics))
    # 售后/收货：客户问"我申请退货了"时必须答得出来
    after_sale = _after_sale_line(order)
    if after_sale:
        lines.append(f"售后进展：{after_sale}")
    if order.status == '已取消' and order.cancel_reason:
        lines.append(f"取消原因：{order.cancel_reason}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 按手机号查询订单（含发货情况）
# ──────────────────────────────────────────────────────────────
def query_orders_by_phone(phone: str) -> str:
    """按手机号查询该号码名下的所有订单与发货状态。

    隐私保护：结果只返回订单号/商品/数量/金额/状态/时间，
    不返回客户姓名与完整手机号（与 query_orders 视图的脱敏口径一致）。

    条数口径修正：原实现用 `len(截断后的列表)` 当"共 N 笔"，
    订单超过 20 笔时会谎报数量 —— 现在分开报「共 X 笔」与「本次列出 Y 笔」。
    """
    phone = (phone or "").strip()
    if not phone:
        return "请提供要查询的手机号。"

    try:
        qs = Orders.objects.filter(phone=phone).order_by("-created_at")
        total = qs.count()
        orders = list(qs[:20])
    except Exception as e:
        return f"查询订单出错：{e}"

    if not orders:
        return f"没有找到手机号 {phone} 名下的订单，请确认号码是否正确，或确认是否已下单。"

    masked = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone
    if total > len(orders):
        lines = [f"手机号 {masked} 名下共 {total} 笔订单（以下列出最近 {len(orders)} 笔）："]
    else:
        lines = [f"手机号 {masked} 名下共 {total} 笔订单："]

    for o in orders:
        status = o.status or "未知"
        dt = o.created_at.strftime("%Y-%m-%d") if o.created_at else "未知"
        line = (
            f"订单号 {o.order_no}｜{o.product_name or '商品'} × {o.quantity or 0}｜"
            f"{o.total_price or 0} 元｜{status}｜下单 {dt}"
        )
        # 物流：只要发过货就给（含已完成），不再限定"已发货"
        logistics = _logistics_parts(o)
        if logistics:
            line += "｜" + "，".join(logistics)
        # 售后进展：退货申请中/已同意/已拒绝 是客户最常追问的
        after_sale = _after_sale_line(o)
        if after_sale:
            line += f"｜{after_sale}"
        lines.append(line)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 报价计算
# ──────────────────────────────────────────────────────────────
def calculate_quote(items: list) -> str:
    """根据商品列表计算报价。

    items: [{"name": "红虫颗粒", "qty": 5}, ...]

    报价同时给出**可购性提醒**：报了价却下不了单（暂停接单/售罄/超量）
    是客户体验最差的一种口径不一致，必须提前说明。
    """
    if not items or not isinstance(items, list):
        return "报价参数错误，需要商品列表。"

    lines = ["📋 报价清单："]
    total = 0
    warnings = []

    for item in items:
        name = item.get("name", "")
        try:
            qty = int(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if not name or qty <= 0:
            continue

        try:
            product = Products.objects.filter(name__icontains=name).first()
            if not product:
                lines.append(f"  {name} × {qty} — ❌ 未找到该产品")
                continue

            unit_price = float(product.wholesale_price or product.retail_price or 0)
            subtotal = round(unit_price * qty, 2)
            total += subtotal

            inv = Inventory.objects.filter(product=product).first()
            verdict = _buyability(product, inv)
            mark = ""
            if not verdict["can_buy"]:
                mark = "　⛔ 当前不可下单"
                warnings.append(f"{product.name}：{_availability_line(product, inv)}")
            elif qty > verdict["available_stock"]:
                mark = f"　⚠️ 超出可用库存（仅 {verdict['available_stock']} 包）"
                warnings.append(
                    f"{product.name}：可用库存仅 {verdict['available_stock']} 包，"
                    f"本单需 {qty} 包，需先补货或拆单"
                )
            lines.append(
                f"  {product.name} × {qty} — 单价 {unit_price} 元 — 小计 {subtotal} 元{mark}"
            )
        except Exception as e:
            lines.append(f"  {name} × {qty} — 查询出错：{e}")

    if total == 0:
        lines.append("  （无有效商品）")
    else:
        lines.append(f"\n💰 合计：{total:.2f} 元")
    if warnings:
        lines.append("\n⚠️ 以下商品当前无法按本单数量成交，请先与客户确认：")
        for w in warnings:
            lines.append(f"  · {w}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 引导转化（新增）
# ──────────────────────────────────────────────────────────────
def guide_to_contact() -> str:
    """当顾客表达购买 / 定制意向时调用，引导其前往定制服务页或联系我们页。

    本工具是 AI 客服对话的「收尾动作」：AI 不代客下单，
    顾客的下一步行动由前端按钮卡片承接（去定制服务 / 联系我们）。
    """
    return (
        "好的，产品情况就介绍到这里啦。\n"
        "如果您确定了意向，可以前往【定制服务】页提交具体需求（配方、规格、数量、联系方式），"
        "我们的业务员会第一时间和您对接；"
        "也可以到【联系我们】页留言，留下您的电话，我们会尽快回电沟通。"
    )


# ──────────────────────────────────────────────────────────────
# 知识库问答（RAG 检索增强）
# ──────────────────────────────────────────────────────────────
def search_knowledge(query: str) -> str:
    """基于企业知识库做 RAG 检索，返回相关答案片段（含来源标题）。"""
    if not query:
        return "请告诉我您想了解什么？"

    try:
        from agent.knowledge_data import get_knowledge_rag
        rag = get_knowledge_rag()
        # 阈值随 RAG 后端自适应：
        # - tfidf：召回+BM25 混合分数尺度较大（实测"今天天气"误命中"春季钓法"约 1.29 分），用 1.5 过滤噪声
        # - embedding：语义余弦相似度仅 0~1，须用低阈值（0.2），否则永远过滤为空 → 知识库检索失效
        backend = os.getenv("RAG_BACKEND", "tfidf").strip().lower()
        min_score = 0.2 if backend == "embedding" else 1.5
        hits = rag.retrieve(query, top_k=3, min_score=min_score)
    except Exception as e:
        return f"知识库检索出错：{e}"

    if not hits:
        return (
            "抱歉，这个问题我暂时没有记录。"
            "您可以告诉我更多细节，或者拨打客服热线 400-xxx-xxxx 咨询。"
        )

    lines = ["[知识库检索结果]"]
    for chunk, _score in hits:
        answer = chunk.meta.get("answer") or chunk.text
        title = chunk.meta.get("title", "")
        label = f"【{title}】" if title else ""
        lines.append(f"{label}{answer}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 工具注册表 & 统一调用
# ──────────────────────────────────────────────────────────────
TOOL_REGISTRY = {
    "query_product": query_product,
    "check_inventory": check_inventory,
    "get_order_status": get_order_status,
    "query_orders_by_phone": query_orders_by_phone,
    "calculate_quote": calculate_quote,
    "search_knowledge": search_knowledge,
    "guide_to_contact": guide_to_contact,
}

# 标准 JSON Schema，供 LLM Function Calling（tool_choice=auto）使用
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": "查询中渔天下某款产品的规格、适用鱼种、零售价、批发价，以及当前是否可下单（含暂停接单/售罄/下架）。回答客户「有货吗、能买吗」前应先调用本工具，不要凭记忆回答库存",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "产品名称，如「红虫颗粒」"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_inventory",
            "description": "查询某款产品的库存情况：总库存、已被订单预占的数量、可用库存（真正能卖多少）以及是否可接单。注意区分总库存与可用库存——总库存不等于能卖；若返回「暂不可下单」，不要向客户承诺有货，应引导换购或等补货",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "产品名称，如「螺鲤3号」"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "按订单号查询订单状态、金额、下单时间、物流信息，以及售后进展（退货申请中/已同意/已拒绝、客户是否已确认收货）。客户问「我的货到哪了」「我申请的退货怎么样了」时都应调用本工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "description": "订单号，如「DD20240801」"},
                },
                "required": ["order_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_orders_by_phone",
            "description": "按顾客下单时的手机号查询其名下所有订单，返回每笔订单的订单号、商品、数量、金额、订单状态（未发货/已发货/已完成/退货申请中/已取消/已退货）、物流信息与售后进展。当顾客想查自己的订单或发货情况但只记得手机号、不记得订单号时，优先用这个工具。若订单较多，返回里会同时说明总笔数与本次列出的笔数，回答时请用总笔数",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "顾客的下单手机号，如「13800138000」"},
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_quote",
            "description": "根据商品清单计算总价（批发价优先），返回报价清单，并**标注当前不可下单或超出可用库存的商品**。若返回里出现「不可下单」或「超出可用库存」提醒，必须一并告知客户，不要只报总价",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "商品列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "产品名称"},
                                "qty": {"type": "integer", "description": "数量"},
                            },
                            "required": ["name", "qty"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索企业知识库，回答政策、售后、退换货、发货、运费、钓鱼技巧等问题",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的问题原文"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "guide_to_contact",
            "description": "当顾客表达购买、定制、下单意向时调用：引导顾客前往「定制服务」页提交需求或「联系我们」页留言，结束本轮咨询（AI 不代客下单）",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


def call_tool(tool_name: str, **kwargs) -> str:
    """统一工具调用入口。

    - 按 tool_name 查找注册函数并执行
    - 兼容旧版单参数调用（input）
    - 统一参数错误 / 执行异常兜底
    """
    func = TOOL_REGISTRY.get(tool_name)
    if not func:
        return f"未知工具：{tool_name}，支持的工具：{list(TOOL_REGISTRY.keys())}"

    # 旧版兼容：仅传 input 时按位置参数传入
    if "input" in kwargs and len(kwargs) == 1:
        return func(kwargs["input"])

    try:
        return func(**kwargs)
    except TypeError as e:
        return f"工具 {tool_name} 参数错误：{e}"
    except Exception as e:
        return f"工具 {tool_name} 执行出错：{e}"
