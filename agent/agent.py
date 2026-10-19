"""
agent/agent.py — ReAct 式 Agent 循环 + 真·流式输出

对齐实习 JD「Agent 开发（ReAct / Function Calling / Tool Use）」与「流式输出」：

流程（每轮 ReAct）：
    1. 流式调用 LLM，携带 tools（Function Calling，tool_choice=auto）
    2. 逐 chunk yield content delta（真·打字机效果）
    3. 同时累积 tool_calls
    4. 一轮结束：
       - 无 tool_calls → 流式结束
       - 有 tool_calls → 工具结果以 role=tool 喂回 → 回到第 1 步（最多 3 轮）

对外：agent_stream(session_id, user_message) 为生成器，逐条 yield 事件 dict：
    {"content": "..."}                            文本增量（兜底清洗后，逐 chunk）
    {"type": "product_card", "product": {...}}    产品名片（名称/规格/图片/价格/库存，可一键下单）
    {"type": "order_card", "order": {...}}        订单名片（订单号/商品/金额/状态/物流，可去支付）
    {"type": "guide_card"}                        引导转化卡片（去定制服务 / 联系我们）
    {"type": "sources", "sources": [...]}         引用来源（标题/相关度/命中路径），最后一律补发

历史加载 / 存库也收敛在本模块，视图层只负责 HTTP 与 SSE 格式化。
"""
import json
import logging
import re

from django.utils import timezone

from agent import llm
from agent.prompts import load_prompt

logger = logging.getLogger(__name__)

MAX_TOOL_STEPS = 3
HISTORY_MAX_MSGS = 10
# 一轮对话最多补发几张产品名片（防止 calculate_quote 罗列十几种商品时刷屏）
MAX_PRODUCT_CARDS = 3
# 订单名片同理：客户问「我的订单」时可能一次查回 20 笔，只给最近几张
MAX_ORDER_CARDS = 3

# 兜底：去除 AI 输出中
# 1) Markdown 加粗 **xx**
# 2) LLM 偶尔泄露的内部/工具调用前缀（function_results: / tool_result: 等）
# 3) XML 风格的 <tool_result>...</tool_result>
# Prompt 已要求 AI 不要输出这些，这里作为最后防线
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INTERNAL_PREFIX_RE = re.compile(
    r"(?:function_results|tool_results?|function_call|tool_call)\s*[:：]?",
    re.IGNORECASE,
)
_XML_TOOL_RE = re.compile(r"</?tool[_\-]?result[^>]*>", re.IGNORECASE)


def _clean_content(text: str) -> str:
    """流式 chunk 兜底清洗：去 **加粗**、内部前缀、XML 工具标签。"""
    if not text:
        return text
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _INTERNAL_PREFIX_RE.sub("", text)
    text = _XML_TOOL_RE.sub("", text)
    return text


# ══════════════════════════════════════════════════════════════
# 购买/批发/定制/下单意图检测（确定性兜底引导）
# ══════════════════════════════════════════════════════════════
# 说明：guide_to_contact 是「收尾工具」，依赖 LLM 在查完信息后主动再次调用，
# 但 ReAct 循环有轮次上限，LLM 常在前几轮查完信息就文字收尾，跳过引导工具，
# 导致最关键的「引导人工承接」转化环节失效。因此在代码层做确定性兜底：
# 只要用户输入命中购买/定制意图，且全程未触发 guide_card，就在流结束后强制补发。
_PURCHASE_KEYWORDS = (
    "批发", "订货", "拿货", "进货", "采购", "下单", "定制", "代发", "囤货",
    "想买", "要买", "我要买", "买点", "来点",
    "怎么下单", "如何下单", "怎么订", "如何订货", "怎么订购",
    "多少钱能拿", "报个价", "给个报价",
)
# 数量 + 单位模式：来/要/订/进/买 100 箱、5 包、2 吨 等（含中文数字）
_QTY_RE = re.compile(
    r"(?:来|要|订|进|买)\s*(?:\d+|[一二三四五六七八九十百千万]+)\s*(?:箱|包|袋|吨|件|斤|公斤|万|批)"
)


def _has_purchase_intent(text: str) -> bool:
    """检测用户输入是否表达购买/批发/定制/下单意图。"""
    if not text:
        return False
    t = text.strip()
    if any(k in t for k in _PURCHASE_KEYWORDS):
        return True
    return bool(_QTY_RE.search(t))


# ══════════════════════════════════════════════════════════════
# 对话持久化
# ══════════════════════════════════════════════════════════════
def _load_history(session_id: str, max_msgs: int = HISTORY_MAX_MSGS) -> list:
    """从数据库加载该会话最近的消息（旧→新）。"""
    try:
        from agent.models import Conversations
        records = Conversations.objects.filter(
            session_id=session_id
        ).order_by("-created_at")[:max_msgs]
        return [{"role": r.role, "content": r.content} for r in reversed(list(records))]
    except Exception:
        logger.exception("加载对话历史失败（session=%s）", session_id)
        return []


def _save_conversation(session_id: str, role: str, content: str) -> None:
    """保存一条对话记录（失败不影响主流程）。"""
    try:
        from agent.models import Conversations
        Conversations.objects.create(
            session_id=session_id, role=role, content=content, created_at=timezone.now()
        )
    except Exception:
        # 存库失败不阻断对话主流程，但要留下排查线索
        logger.exception("对话记录落库失败（session=%s role=%s）", session_id, role)


def _parse_tool_args(raw: str) -> dict:
    """解析工具调用参数，容错处理空串 / 非法 JSON。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


# ══════════════════════════════════════════════════════════════
# Agent 循环（生成器）— 真·流式
# ══════════════════════════════════════════════════════════════
def agent_stream(session_id: str, user_message: str):
    """对外统一出口：跑完 ReAct 循环后，补发本轮的知识库引用来源。

    为什么放在最外层而不是循环里：`_agent_loop` 有 4 个 return 分支
    （正常收尾 / LLM 异常 / 达到步数上限），逐个加发 sources 一定会漏一个。
    用 `yield from` 包一层，只要循环结束就一定走到这里。

    :yield: 循环产出的各类事件，末尾追加 {"type": "sources", "sources": [...]}
    """
    from agent.tools import get_referenced_sources, reset_referenced_sources

    # 每轮清零：上一轮问"春季钓法"的来源不能挂到这一轮"退货政策"的回答下面
    reset_referenced_sources()
    yield from _agent_loop(session_id, user_message)

    sources = get_referenced_sources()
    if sources:
        # 只发结构化数据，渲染与折叠交互交给前端
        yield {"type": "sources", "sources": sources}


def _agent_loop(session_id: str, user_message: str):
    """ReAct 式 Agent 循环，逐 chunk yield 事件。

    每轮直接消费 LLM stream：
      - content delta → 逐 chunk yield {"content": cleaned}（真·打字机）
      - tool_calls delta → 仅在内部累积，不 yield（本轮结束后再处理）

    :param session_id: 会话 ID（用于加载历史）
    :param user_message: 当前用户输入
    :yield: {"content": str}
            或 {"type": "product_card", "product": {...}}
            或 {"type": "order_card", "order": {...}}
            或 {"type": "guide_card"}
    """
    from agent.tools import (
        TOOL_SCHEMAS,
        call_tool,
        get_referenced_orders,
        get_referenced_products,
        reset_referenced_orders,
        reset_referenced_products,
    )

    # 购买/批发/定制/下单意图：用于确定性兜底引导（不依赖 LLM 的 tool_choice 决策）
    purchase_intent = _has_purchase_intent(user_message)
    guide_emitted = False

    # 产品名片：本轮「被工具介绍过」的商品，在回答结束后补发（任务书四·2 产品名片）
    # 每轮开始清空 —— 否则上一轮问过的商品会跟着这一轮再弹一次
    reset_referenced_products()
    carded_ids: set = set()
    # 订单名片：本轮「被工具查过」的订单（问「我的订单到哪了」→ 弹结构化进度卡）
    reset_referenced_orders()
    carded_order_nos: set = set()

    def _drain_product_cards() -> list:
        """把本轮新登记的商品转成 product_card 事件。

        同一商品只发一次；最多 MAX_PRODUCT_CARDS 张，防止一次报价刷屏。
        """
        events = []
        for p in get_referenced_products():
            if p["id"] in carded_ids:
                continue
            if len(carded_ids) >= MAX_PRODUCT_CARDS:
                break
            carded_ids.add(p["id"])
            events.append({"type": "product_card", "product": p})
        return events

    def _drain_order_cards() -> list:
        """把本轮新查到的订单转成 order_card 事件（同一单只发一次，最多 MAX_ORDER_CARDS 张）。"""
        events = []
        for o in get_referenced_orders():
            if o["order_no"] in carded_order_nos:
                continue
            if len(carded_order_nos) >= MAX_ORDER_CARDS:
                break
            carded_order_nos.add(o["order_no"])
            events.append({"type": "order_card", "order": o})
        return events

    system_prompt = load_prompt("system_prompt_agent")
    history = _load_history(session_id)
    messages = (
        [{"role": "system", "content": system_prompt}]
        + history
        + [{"role": "user", "content": user_message}]
    )

    last_tool_result = ""

    for step in range(MAX_TOOL_STEPS):
        tool_calls_map: dict = {}
        raw_content_parts: list = []  # 本轮原始 content（用于 messages 完整性）

        try:
            stream = llm.chat_stream(
                messages, tools=TOOL_SCHEMAS, tool_choice="auto"
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                dc = getattr(delta, "content", None)
                if dc:
                    raw_content_parts.append(dc)
                    # 逐 chunk 兜底清洗后立即 yield（真·流式打字机）
                    cleaned = _clean_content(dc)
                    if cleaned:
                        yield {"content": cleaned}
                for tc in getattr(delta, "tool_calls", None) or []:
                    entry = tool_calls_map.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            entry["name"] = tc.function.name
                        if tc.function.arguments:
                            entry["arguments"] += tc.function.arguments
        except Exception as e:
            # 前几轮工具已查到的商品/订单仍要出名片，别因为本轮报错把整轮成果作废
            for card in _drain_product_cards():
                yield card
            for card in _drain_order_cards():
                yield card
            if purchase_intent and not guide_emitted:
                guide_emitted = True
                yield {"type": "guide_card"}
            yield {"content": f"⚠️ AI 服务调用失败：{e}"}
            return

        tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map)]

        # 无 tool_calls → 本轮就是最终回答，已流式 yield 完，结束
        if not tool_calls:
            if not raw_content_parts and last_tool_result:
                # 模型空回复时，退回最后工具结果兜底
                yield {"content": last_tool_result}
            # 顺序固定：回答正文 → 产品名片 → 订单名片 → 引导卡
            # （客户先读完介绍，再看到可点的名片与下一步入口）
            for card in _drain_product_cards():
                yield card
            for card in _drain_order_cards():
                yield card
            if purchase_intent and not guide_emitted:
                guide_emitted = True
                yield {"type": "guide_card"}
            return

        # 有 tool_calls：构造 assistant 消息（content 用原始累积，含思考文字）
        messages.append({
            "role": "assistant",
            "content": "".join(raw_content_parts) or None,
            "tool_calls": [
                {
                    "id": tc["id"] or f"call_{step}_{i}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for i, tc in enumerate(tool_calls)
            ],
        })

        # 逐个执行工具，结果以 role=tool 喂回
        for i, tc in enumerate(tool_calls):
            args = _parse_tool_args(tc["arguments"])
            result = call_tool(tc["name"], **args)
            last_tool_result = result

            # 引导转化 → 产出引导卡片事件（前端渲染「定制服务 / 联系我们」按钮）
            if tc["name"] == "guide_to_contact":
                guide_emitted = True
                yield {"type": "guide_card"}

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{step}_{i}",
                "content": result,
            })

    # 达到最大轮次仍未收敛 → 用最后工具结果兜底
    for card in _drain_product_cards():
        yield card
    for card in _drain_order_cards():
        yield card
    if purchase_intent and not guide_emitted:
        guide_emitted = True
        yield {"type": "guide_card"}
    yield {"content": last_tool_result or "抱歉，这个问题比较复杂，建议转人工处理。"}
