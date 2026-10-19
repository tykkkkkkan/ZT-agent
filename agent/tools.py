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
import contextvars
import logging
import os

from django.db.models import Q

from agent.models import Inventory, Orders, OrderStatus, PaymentStatus, Products

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# 本轮对话「被工具介绍过」的商品 → 前端产品名片卡
# ──────────────────────────────────────────────────────────────
# 用 ContextVar 而不是模块级 list：Django 开发服务器是多线程的，
# 模块级容器会把 A 用户的商品泄漏进 B 用户的这一轮对话。
_referenced_products = contextvars.ContextVar("referenced_products", default=None)


def reset_referenced_products() -> None:
    """每轮对话开始前清空（由 agent_stream 调用）。"""
    _referenced_products.set([])


def get_referenced_products() -> list:
    """取本轮工具介绍过的商品（按出现顺序，已按 id 去重）。"""
    return list(_referenced_products.get() or [])


def _remember_product(product) -> None:
    """登记一个被工具介绍过的商品，供前端渲染「产品名片」。

    字段口径必须与 /api/products/ 一致（can_buy 一律取 purchase_guard 判定），
    否则又会出现「卡片说能买、下单页说不能买」的自相矛盾。
    """
    try:
        bucket = _referenced_products.get()
        if bucket is None:
            bucket = []
            _referenced_products.set(bucket)
        if any(item["id"] == product.id for item in bucket):
            return
        v = _buyability(product)
        bucket.append({
            "id": product.id,
            "name": product.name,
            "sku": product.sku or "",
            "spec": product.spec or "",
            "target_fish": product.target_fish or "",
            "retail_price": float(product.retail_price or 0),
            "wholesale_price": float(product.wholesale_price or 0),
            "available_stock": v["available_stock"],
            "can_buy": v["can_buy"],
            "buy_block_reason": v["reason"],
            "buy_block_message": v["message"],
            "customer_notice": v.get("customer_notice") or "",
        })
    except Exception:
        # 名片登记失败不影响问答主流程
        logger.exception("登记产品名片失败（product=%s）", getattr(product, "id", None))


# ──────────────────────────────────────────────────────────────
# 本轮对话「被工具查过」的订单 → 前端订单名片卡
# ──────────────────────────────────────────────────────────────
# 与商品名片同理用 ContextVar：多线程下模块级容器会把 A 客户的订单
# 泄漏进 B 客户的这一轮对话 —— 订单比商品敏感得多，这条不能省。
_referenced_orders = contextvars.ContextVar("referenced_orders", default=None)


def reset_referenced_orders() -> None:
    """每轮对话开始前清空（由 agent_stream 调用）。"""
    _referenced_orders.set([])


def get_referenced_orders() -> list:
    """取本轮工具查过的订单（按出现顺序，已按订单号去重）。"""
    return list(_referenced_orders.get() or [])


def _remember_order(order) -> None:
    """登记一笔被工具查过的订单，供前端渲染「订单名片」。

    结构出自 `agent/order_presenter.order_card`（唯一出口）——
    名片上的按钮与个人中心 / 查订单页共用 `order_actions()` 判定，
    因此不会出现「聊天名片能点支付、个人中心说不能付」。
    """
    try:
        bucket = _referenced_orders.get()
        if bucket is None:
            bucket = []
            _referenced_orders.set(bucket)
        if any(item["order_no"] == order.order_no for item in bucket):
            return
        from agent.order_presenter import order_card

        bucket.append(order_card(order))
    except Exception:
        # 名片登记失败不影响问答主流程
        logger.exception("登记订单名片失败（order=%s）", getattr(order, "order_no", None))


# ──────────────────────────────────────────────────────────────
# 本轮对话「知识库命中了哪几条」→ 前端「参考来源」折叠条
# ──────────────────────────────────────────────────────────────
# 引用溯源（对齐 RAGFlow grounded citations / Dify 引用预览）：
# 检索结果原本只被拼成一段给模型看的文本，用户看不到出处，也就无法判断
# 这句话是查到的还是编的。这里把 title / 分数 / 命中路径留下来，
# 由 agent_stream 在回答结束后作为 `sources` 事件推给前端。
# 同样用 ContextVar：多线程下 A 客户的检索结果不能串到 B 客户的气泡里。
_referenced_sources = contextvars.ContextVar("referenced_sources", default=None)

# 每条来源在气泡里展示的原文长度（太长会把折叠条撑开，太短看不出是否命中）
SOURCE_SNIPPET_CHARS = 160


def reset_referenced_sources() -> None:
    """每轮对话开始前清空（由 agent_stream 调用）。"""
    _referenced_sources.set([])


def get_referenced_sources() -> list:
    """取本轮检索命中的知识库条目（按出现顺序，已按标题+序号去重）。"""
    return list(_referenced_sources.get() or [])


def _remember_source(hit: dict) -> None:
    """登记一条检索命中，供前端"参考来源"展示。

    hit 来自 `RAGEngine.retrieve_detailed()`：
        {"chunk": Chunk, "score": float, "routes": {...}}
    """
    try:
        bucket = _referenced_sources.get()
        if bucket is None:
            bucket = []
            _referenced_sources.set(bucket)

        chunk = hit.get("chunk")
        meta = getattr(chunk, "meta", {}) or {}
        answer = meta.get("answer") or getattr(chunk, "text", "") or ""
        title = meta.get("title") or "未命名条目"
        # 同一条被多轮检索命中时只留第一次（分数最高的那次在前）
        if any(item["title"] == title for item in bucket):
            return
        routes = hit.get("routes") or {}
        bucket.append({
            "title": title,
            "score": round(float(hit.get("score") or 0), 4),
            "category": meta.get("category") or "",
            "snippet": answer.strip().replace("\n", " ")[:SOURCE_SNIPPET_CHARS],
            "backend": routes.get("backend") or "",
            "rank": routes.get("rank"),
            "lexical_rank": routes.get("lexical_rank"),
            "semantic_rank": routes.get("semantic_rank"),
        })
    except Exception:
        # 溯源登记失败不影响问答主流程
        logger.exception("登记引用来源失败")


# ──────────────────────────────────────────────────────────────
# 当前登录用户 → 工具侧可见（工具函数拿不到 request）
# ──────────────────────────────────────────────────────────────
# 视图层在调用 agent_stream 之前注入；`query_my_orders` 依赖它按归属规则
# 查「我的订单」，客户因此不必再报订单号或手机号。
# 同样用 ContextVar：多线程下模块级变量会把 A 用户的身份泄漏给 B 用户。
_current_user = contextvars.ContextVar("current_user", default=None)


def set_current_user(user) -> None:
    """由视图层在本轮对话开始前注入当前登录用户（可为 None = 未登录）。"""
    _current_user.set(user)


def get_current_user():
    """取本轮对话的登录用户；未登录返回 None。"""
    return _current_user.get()


# ──────────────────────────────────────────────────────────────
# 转人工：会话上下文（session_id + 本轮转出的工单）
# ──────────────────────────────────────────────────────────────
# `handoff_to_human` 需要知道「是哪个会话」才能把最近几轮对话一起交给人工；
# 工单建好后还要把结果传给 agent 层去发 handoff_card 事件 —— 两处都用 ContextVar，
# 与其它几个上下文变量同一条纪律：多线程下不串号。
_current_session = contextvars.ContextVar("current_session", default="")
_last_handoff = contextvars.ContextVar("last_handoff", default=None)


def set_current_session(session_id: str) -> None:
    """由 agent 层在本轮开始前注入当前会话 ID。"""
    _current_session.set(session_id or "")


def get_current_session() -> str:
    return _current_session.get() or ""


def reset_handoff() -> None:
    """每轮对话开始前清空：否则上一轮的工单会被重复发一次卡片。"""
    _last_handoff.set(None)


def drain_handoff():
    """取走本轮新建的工单（只返回一次），供 agent 层转成 handoff_card 事件。"""
    ticket = _last_handoff.get()
    _last_handoff.set(None)
    return ticket


# ──────────────────────────────────────────────────────────────
# 共用口径助手（唯一来源：purchase_guard + 统一的售后状态描述）
# ──────────────────────────────────────────────────────────────
def _buyability(product, inv=None) -> dict:
    """取可购性判定（复用 agent.purchase_guard，绝不自己算）。

    inv 仅为兼容调用方已有实例；库存与协调覆盖统一从 load_context 取，
    保证与前台 /api/products/ 读的是同一份数据。
    """
    from agent.purchase_guard import evaluate, load_context

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


def _delivery_parts(order) -> list:
    """收货地址与期望交期（任务书四·4：客户在下单界面填写）。"""
    parts = []
    address = getattr(order, "address", "") or ""
    if address:
        parts.append(f"收货地址 {address}")
    expected = getattr(order, "expected_date", None)
    if expected:
        parts.append(f"期望 {expected:%Y-%m-%d} 前到货")
    return parts


def _payment_parts(order) -> list:
    """支付维度（任务书五·1）。

    为什么 AI 必须知道支付状态：客户问「我的订单怎么还没发货」时，
    最常见的原因就是**还没付款**。如果工具层只报「未发货」，AI 只能笼统回
    「商家还没发货」——听起来像在敷衍。带上支付状态，AI 才能给出可执行的答案：
    「这笔订单还没支付，需要先完成支付商家才会安排发货」。

    空值（支付功能上线前的历史订单）不展示，避免凭空给老订单安一个支付状态。
    """
    status = getattr(order, "payment_status", "") or ""
    if status == PaymentStatus.UNPAID:
        return ["支付状态 未支付（需完成支付后商家才会发货）"]
    if status == PaymentStatus.PAID:
        paid_at = getattr(order, "paid_at", None)
        return [f"支付状态 已支付（{paid_at:%Y-%m-%d %H:%M}）" if paid_at else "支付状态 已支付"]
    if status == PaymentStatus.REFUNDED:
        return ["支付状态 已退款"]
    return []


def _rating_line(product) -> list:
    """商品评价概览（任务书六·4）。

    客户问「这个饵料好用吗 / 口碑怎么样」时，AI 不该凭空夸产品 ——
    把真实均分与评价条数交给它，让它给出有依据的回答。
    没有评价时返回空列表：不主动说"暂无评价"，免得听起来像在暗示商品不好。
    """
    from agent import reviews as review_helper

    try:
        summary = review_helper.rating_summary([product.id]).get(product.id)
    except Exception:                                    # noqa: BLE001
        return []
    if not summary or not summary.get("review_count"):
        return []
    return [f"客户评价：{summary['rating_avg']} 分（{summary['review_count']} 条评价）"]


def _price_lines(product, user) -> list:
    """价格行（任务书二·5：身份差异化价格）。

    唯一出口是 `agent/pricing.py` —— 这里不自己判断身份，也不自己比价。
    个体钓友**看不到批发价这一行**：AI 看不到就不会说出口，比"叮嘱它别说"可靠。
    """
    from agent import pricing

    lines = [f"零售价：{pricing.retail_of(product)} 元/包"]
    if pricing.is_wholesale(user):
        wp = pricing.wholesale_of(product)
        if wp is not None:
            lines.append(
                f"批发价：{wp} 元/包（{pricing.WHOLESALE_MIN_QTY} 包起）"
            )
    return lines


# ──────────────────────────────────────────────────────────────
# 产品查询
# ──────────────────────────────────────────────────────────────
def query_product(name: str) -> str:
    """按**名称 / 分类 / 适用鱼种**查询产品（任务书二·4），返回中文描述
    （含 SKU、分类、库存、**可购性**与按身份裁剪的价格）。

    检索范围此前只有 `name__icontains`，客户问「鲫鱼用什么」会得到"没找到"；
    现在与产品页的三种查找方式对齐。
    """
    try:
        product = (
            Products.objects.filter(
                Q(name__icontains=name) | Q(category__icontains=name) | Q(target_fish__icontains=name)
            )
            .order_by("id")          # 顺序确定，避免同名/同类时结果漂移
            .first()
        )
    except Exception as e:
        return f"查询产品出错：{e}"

    if not product:
        return f"没找到「{name}」相关的产品，麻烦换个名称、分类或鱼种试试？"

    # 登记到本轮引用列表 —— 回答完由 agent_stream 补发「产品名片」事件（任务书四·2）
    _remember_product(product)

    inv = Inventory.objects.filter(product=product).first()
    lines = [
        f"产品：{product.name}",
        f"编码：{product.sku or '（未设置）'}",
        f"分类：{product.category or '未分类'}",
        f"规格：{product.spec or '无'}",
        f"适用鱼种：{product.target_fish or '无'}",
    ]
    # 价格：按当前登录身份裁剪（个体钓友没有批发价这一行）
    lines.extend(_price_lines(product, get_current_user()))
    if inv:
        lines.append(f"可用库存：{inv.available_stock} 包（已预占 {inv.reserved_stock}）")
    # 可购性：与前台产品页/下单页同源判定，避免 AI 承诺了却下不了单
    lines.append(_availability_line(product, inv))
    # 真实口碑（任务书六·4）——AI 引用它回答"好不好用"，而不是自己夸
    lines.extend(_rating_line(product))
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
        # 登记到本轮引用列表（库存为 0 也要出名片，让客户看到"售罄"而不是什么都没有）
        _remember_product(product)
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

    # 订单名片：客户问「到哪了」时，回答之外再给一张结构化进度卡（可直接去支付/看物流）
    _remember_order(order)

    lines = [
        f"订单号：{order.order_no}",
        f"客户：{order.customer_name or '无'}",
        f"商品：{order.product_name or '无'}",
        f"数量：{order.quantity or 0}",
        f"总价：{order.total_price or 0} 元",
        f"状态：{order.status or '未知'}",
        f"下单时间：{order.created_at or '未知'}",
    ]
    # 支付状态（任务书五·1）：未支付是"为什么还没发货"的最常见答案
    lines.extend(_payment_parts(order))
    # 收货地址与期望交期（客户下单时填写，任务书四·4）
    delivery = _delivery_parts(order)
    if delivery:
        lines.append("｜".join(delivery))
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
    """按手机号查询该客户**自己**的订单与发货状态（需登录，且只能查本人绑定的号码）。

    隐私保护：结果只返回订单号/商品/数量/金额/状态/时间，
    不返回客户姓名与完整手机号（与 query_orders 视图的脱敏口径一致）。

    归属校验：与网页端 `/api/orders/query/` **同一口径**（复用 me_helpers.my_orders_q）。
    改造前这里只按 `phone=手机号` 裸查，任何未登录的人知道一个手机号就能拿到
    他人订单的订单号、商品、金额与物流单号 —— 属越权。现在：
      · 未登录 → 拒绝并引导登录（或改用 get_order_status 按订单号查）；
      · 已登录但未绑定手机号 → 引导先绑定；
      · 手机号不是本人绑定的 → 拒绝，且不透露该号码是否存在订单。

    条数口径修正：原实现用 `len(截断后的列表)` 当"共 N 笔"，
    订单超过 20 笔时会谎报数量 —— 现在分开报「共 X 笔」与「本次列出 Y 笔」。
    """
    phone = (phone or "").strip()

    user = get_current_user()
    if user is None:
        return (
            "为保护客户隐私，按手机号查订单需要先登录。\n"
            "请引导客户登录后使用「我的订单」；也可以请客户提供订单号，"
            "我用 get_order_status 帮他查。"
        )

    from agent.me_helpers import get_profile, my_orders_q

    my_phone = (get_profile(user).phone or "").strip()
    if not my_phone:
        return (
            "该客户账号还没有绑定手机号，无法按手机号查订单。\n"
            "请引导客户到「个人中心」绑定下单时使用的手机号；"
            "也可以请客户提供订单号，我用 get_order_status 直接查。"
        )
    if phone and phone != my_phone:
        masked_own = my_phone[:3] + "****" + my_phone[-4:] if len(my_phone) >= 7 else my_phone
        return (
            f"只能查询当前账号绑定的手机号（{masked_own}）名下的订单，不能查询他人订单。\n"
            "若客户要查另一个号码，请引导他到「个人中心」更换绑定手机号。"
        )
    phone = my_phone            # 客户未报号码时，默认查自己绑定的号码

    try:
        qs = Orders.objects.filter(my_orders_q(user)).order_by("-created_at", "-id")
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
        _remember_order(o)              # 订单名片（最多前几张，由 agent_stream 截断）
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
        elif status == OrderStatus.PENDING and \
                (getattr(o, "payment_status", "") or "") == PaymentStatus.UNPAID:
            # 待发货且未支付：把"为什么没发"说清楚（任务书五·1）
            line += "｜未支付（需付款后安排发货）"
        # 售后进展：退货申请中/已同意/已拒绝 是客户最常追问的
        after_sale = _after_sale_line(o)
        if after_sale:
            line += f"｜{after_sale}"
        lines.append(line)
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 我的订单（登录态直查，无需订单号/手机号）
# ──────────────────────────────────────────────────────────────
def query_my_orders(limit: int = 3) -> str:
    """查询**当前登录客户自己**的最近订单（含物流与售后进展）。

    任务书五·3：「用户向 Agent 询问自己的订单发到哪了，Agent 查询后给出实时进度」。
    此前 Agent 只有 get_order_status（要订单号）与 query_orders_by_phone（要手机号），
    客户问「我的订单到哪了」时必须先报一串号码 —— 这个工具把最后一跳补上了。

    归属规则与「查订单」页、个人中心**完全一致**（复用 me_helpers.my_orders_q），
    绝不按纯手机号查，避免越权看到他人订单。
    """
    user = get_current_user()
    if user is None:
        return (
            "当前会话没有识别到登录用户，无法直接查「我的订单」。\n"
            "请引导客户先登录，再回个人中心查看；"
            "也可以请客户提供订单号，我用 get_order_status 帮他查。"
        )

    try:
        limit = int(limit or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 10))

    from agent.me_helpers import my_orders_q

    try:
        qs = Orders.objects.filter(my_orders_q(user)).order_by("-created_at", "-id")
        total = qs.count()
        orders = list(qs[:limit])
    except Exception as e:
        return f"查询我的订单出错：{e}"

    if not orders:
        return (
            "该客户名下暂时没有查询到订单。\n"
            "若客户刚下过单，请先确认下单时填的手机号是否与账号绑定的手机号一致；"
            "也可以请客户提供订单号，我用 get_order_status 直接查。"
        )

    head = (
        f"共 {total} 笔订单：" if total <= len(orders)
        else f"共 {total} 笔订单（以下列出最近 {len(orders)} 笔）："
    )
    lines = [f"【该客户自己的订单】{head}"]
    for o in orders:
        _remember_order(o)              # 订单名片（最多前几张，由 agent_stream 截断）
        dt = o.created_at.strftime("%Y-%m-%d") if o.created_at else "未知"
        seg = [
            f"订单号 {o.order_no}",
            f"{o.product_name or '商品'} × {o.quantity or 0}",
            f"{o.total_price or 0} 元",
            f"状态 {o.status or '未知'}",
            f"下单 {dt}",
        ]
        logistics = _logistics_parts(o)
        if logistics:
            seg.append("，".join(logistics))
        elif o.status == OrderStatus.PENDING:
            # 未发货时补一句原因 —— 最常见的就是"还没付款"（任务书五·1）。
            # 只说"尚未发货"会让客户以为商家拖着不发，带着原因才答得上话。
            seg.append("尚未发货（未支付，需付款后安排）"
                       if (getattr(o, "payment_status", "") or "") == PaymentStatus.UNPAID
                       else "尚未发货")
        after_sale = _after_sale_line(o)
        if after_sale:
            seg.append(after_sale)
        lines.append("｜".join(seg))
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

            # 报价里的商品同样出名片（客户看完报价即可一键下单）
            _remember_product(product)

            # 任务书二·5：单价按**当前客户身份**取 —— 个体钓友走零售价，
            # 经销商达到起订量才走批发价。改造前这里写死 `wholesale_price`，
            # 于是"AI 报批发价、下单按零售价结算"，客户会认为报价不算数。
            from agent import pricing
            user = get_current_user()
            unit_price = pricing.price_for(product, user, qty)
            tier = "批发价" if pricing.is_wholesale(user) and unit_price == pricing.wholesale_of(product) \
                else "零售价"
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
                f"  {product.name} × {qty} — 单价 {unit_price} 元（{tier}）— 小计 {subtotal} 元{mark}"
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
# 转人工（工单）
# ──────────────────────────────────────────────────────────────
# 与 guide_to_contact 的分工必须分清，否则模型会乱用一个：
#   guide_to_contact = 「顾客想买/想定制」→ 引导去填需求单（业务流程正常往下走）
#   handoff_to_human = 「AI 办不了/顾客不满意/情绪激动」→ 建工单交给人（异常兜底）
# 两者都会结束本轮，但前者是转化，后者是止损。
HANDOFF_TRANSCRIPT_MSGS = 12      # 工单里带最近 N 条消息：够看懂前因，又不至于几百行
HANDOFF_MAX_CHARS = 400           # 单条消息进快照的最长字数


def _ticket_transcript(session_id: str, limit: int = HANDOFF_TRANSCRIPT_MSGS) -> list:
    """取会话最近 N 条消息（时间正序）。

    为什么要从 DB 读而不是用内存里的 messages：工单要能被坐席在**任意时间**
    打开还原现场，存进 JSON 快照才算数；而且当前这轮 user 消息此时已落库。
    """
    if not session_id:
        return []
    try:
        from agent.models import Conversations
        rows = list(
            Conversations.objects.filter(session_id=session_id)
            .order_by("-id")[:limit]
            .values("role", "content", "created_at")
        )
        rows.reverse()                      # 取最近 N 条后翻回正序
        return [
            {
                "role": r["role"],
                "content": (r["content"] or "")[:HANDOFF_MAX_CHARS],
                "at": r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("工单对话快照读取失败（session=%s）", session_id)
        return []


def handoff_to_human(reason: str = "unknown", detail: str = "", contact_phone: str = "") -> str:
    """把本轮会话转给人工客服，生成一张带完整上下文的工单。

    调用时机（模型据此自行判断，prompt 里也写了）：
      - 知识库明确没有、AI 连续查不到答案；
      - 顾客明确要求「转人工 / 找真人」；
      - 顾客情绪不满、投诉；
      - 售后纠纷、物流异常、支付问题等需要人工核实凭证的事。

    工单里会带上：最近若干轮对话、本轮工具调用、知识库命中情况、
    涉及的商品与订单 —— 目的就是让坐席**不用让顾客重说一遍**。
    """
    from agent.models import SupportTicket

    reason = (reason or "unknown").strip().lower()
    valid_reasons = {c[0] for c in SupportTicket.Reason.choices}
    if reason not in valid_reasons:
        reason = "unknown"

    session_id = get_current_session()
    user = get_current_user()
    user_id = getattr(user, "id", None)

    # 顾客问题 = 会话里最后一条 user 消息（当前这轮就是触发转人工的那句）
    question = ""
    transcript = _ticket_transcript(session_id)
    for msg in reversed(transcript):
        if msg.get("role") == "user":
            question = msg.get("content") or ""
            break

    # 能用绑定手机号就用，省得坐席再问一次「您的电话是」
    phone = (contact_phone or "").strip()
    name = ""
    if not phone and user_id:
        try:
            from agent.models import UserProfile
            profile = UserProfile.objects.filter(user_id=user_id).first()
            if profile:
                phone = (getattr(profile, "phone", "") or "").strip()
                name = (getattr(profile, "nickname", "") or "").strip()
        except Exception:
            logger.exception("工单读取用户资料失败（user=%s）", user_id)

    # 售后 / 投诉默认提优先级：这两类拖不得
    priority = {
        "after_sale": SupportTicket.Priority.HIGH,
        "complaint": SupportTicket.Priority.HIGH,
        "payment": SupportTicket.Priority.HIGH,
        "logistics": SupportTicket.Priority.NORMAL,
    }.get(reason, SupportTicket.Priority.NORMAL)

    try:
        ticket = SupportTicket.objects.create(
            session_id=session_id,
            user_id=user_id,
            contact_name=name[:50],
            contact_phone=phone[:20],
            question=question[:2000],
            detail=(detail or "")[:1000],
            reason=reason,
            priority=priority,
            status=SupportTicket.Status.OPEN,
            source=(SupportTicket.Source.USER if reason == "user_request"
                    else SupportTicket.Source.AGENT),
            transcript=transcript,
            # 这一轮 AI 已经查过什么：坐席据此判断「是不是查了也没有」
            tool_trace=_handoff_tool_hint(),
            sources=get_referenced_sources(),
            related_orders=[o.get("order_no") for o in get_referenced_orders() if o.get("order_no")],
            related_products=[p.get("name") for p in get_referenced_products() if p.get("name")],
        )
    except Exception:
        logger.exception("转人工工单创建失败（session=%s）", session_id)
        return (
            "转人工暂时失败，请让顾客稍后重试，或引导其前往【联系我们】页留言并留下电话。"
        )

    _last_handoff.set(ticket)
    logger.info("已创建转人工工单 %s（session=%s reason=%s）", ticket.ticket_no, session_id, reason)

    from agent.handoff_presenter import ticket_line
    return ticket_line(ticket)


def _handoff_tool_hint() -> list:
    """本轮工具调用的精简记录。

    agent 层会在 time_line 里记全量轨迹，但工单是**同步**建的（此刻还不知道
    后面还会调什么），所以这里只带上「已经发生」的那部分：商品 / 订单引用。
    轨迹的完整版由 agent 层在落 trace 时补写（见 _persist_run 之后的 handler）。
    """
    hints = []
    for p in get_referenced_products():
        hints.append({"name": "query_product", "args": {"name": p.get("name")}})
    for o in get_referenced_orders():
        hints.append({"name": "get_order_status", "args": {"order_no": o.get("order_no")}})
    return hints[:8]


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
        # - hybrid：RRF 融合分仅与排名相关（top1 约 0.03 量级），无法用绝对阈值，故 0（不过滤）
        backend = os.getenv("RAG_BACKEND", "tfidf").strip().lower()
        min_score = {"embedding": 0.2, "hybrid": 0.0}.get(backend, 1.5)
        # 用 detailed 版本：除了给模型的文本，还要留下"这条凭什么被选中"的过程信息
        detailed = rag.retrieve_detailed(query, top_k=3, min_score=min_score)
    except Exception as e:
        return f"知识库检索出错：{e}"

    if not detailed:
        return (
            "抱歉，这个问题我暂时没有记录。"
            "您可以告诉我更多细节，或者拨打客服热线 400-xxx-xxxx 咨询。"
        )

    # 引用溯源：把命中条目登记进本轮上下文，回答结束后由 agent_stream 推给前端
    for hit in detailed:
        _remember_source(hit)

    lines = ["[知识库检索结果]"]
    for hit in detailed:
        chunk = hit["chunk"]
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
    "query_my_orders": query_my_orders,
    "calculate_quote": calculate_quote,
    "search_knowledge": search_knowledge,
    "guide_to_contact": guide_to_contact,
    "handoff_to_human": handoff_to_human,
}

# 标准 JSON Schema，供 LLM Function Calling（tool_choice=auto）使用
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": "查询中渔天下某款产品的分类、规格、适用鱼种、零售价、批发价，以及当前是否可下单（含暂停接单/售罄/下架）。关键词可以是**产品名、分类或适用鱼种**（如「红虫颗粒」「薯香饵」「鲫鱼」）。回答客户「有货吗、能买吗、鲫鱼用什么」前应先调用本工具，不要凭记忆回答库存",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "产品名称 / 分类 / 适用鱼种，如「红虫颗粒」「薯香饵」「鲫鱼」"},
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
            "description": "按顾客**本人绑定的手机号**查询其名下订单，返回订单号、商品、数量、金额、状态、物流与售后进展。**前提：顾客已登录，且该手机号就是账号绑定的号码**——未登录、未绑定号码或填了别人的号码都会被拒绝（出于隐私保护，与网页端查订单页同一口径）。被拒绝时**不要重复调用**，改为引导顾客登录/绑定手机号，或请顾客提供订单号改用 get_order_status。若订单较多，返回里会同时说明总笔数与本次列出的笔数，回答时请用总笔数",
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
            "name": "query_my_orders",
            "description": "查询**当前已登录客户自己**的最近订单（含商品、数量、金额、订单状态、物流与售后进展）。当客户说「我的订单」「我买的东西到哪了」「发货了吗」这类**不提供订单号也不提供手机号**的问法时，优先用这个工具。无需任何参数。若返回提示未登录或没有订单，再引导客户登录或提供订单号",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回最近几笔，默认 3，最多 10"},
                },
                "required": [],
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
    {
        "type": "function",
        "function": {
            "name": "handoff_to_human",
            "description": (
                "转人工：当 AI 确实办不了时才调用 —— 知识库查不到且无法回答、"
                "顾客明确要求转人工/找真人、顾客表达不满或投诉、"
                "涉及售后纠纷/物流异常/支付与退款等需要人工核实凭证的事。"
                "调用后会自动生成工单并把这轮对话上下文一并交给客服，"
                "顾客不需要再重复描述问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["unknown", "user_request", "after_sale",
                                 "logistics", "payment", "complaint", "other"],
                        "description": "转人工原因分类",
                    },
                    "detail": {
                        "type": "string",
                        "description": "一句话说明卡在哪（给人工看的补充，如「顾客说收到的竿稍是断的」）",
                    },
                    "contact_phone": {
                        "type": "string",
                        "description": "顾客留的电话（顾客主动给出时才填，没有就留空）",
                    },
                },
                "required": ["reason"],
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
