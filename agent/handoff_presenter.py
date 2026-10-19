"""转人工工单的对外呈现（唯一出处）—— 与 `order_presenter.py` 同一条纪律。

为什么单独一个模块：「工单长什么样」被三处需要 ——
  ① `tools.handoff_to_human` 要给模型一句可以直接转述的话；
  ② `agent._handoff_card_payload` 要发 SSE 卡片给前端；
  ③ `views.handoff` 要回 JSON 给点按钮的顾客。
三处各写一遍的结果一定是漂移：卡片上说「30 分钟内联系」，JSON 里写「2 小时」，
顾客截图给客服时对不上。所以承诺时限、受理人、安抚话术只在这里定义。

⚠️ 这些字段都是**给顾客看的**。`transcript` / `tool_trace` / `resolution`
是内部资料，绝不下发到浏览器。
"""

# 按原因给出「谁受理 / 承诺多少分钟内联系 / 一句安抚」。
# 时限不是随口写的：售后与投诉必须当天响应，物流/支付次之，其它按普通档。
_REASON_CONTEXT = {
    "after_sale": ("售后专员", 30, "已记录您的情况，售后专员会核对订单与凭证后给出处理方案。"),
    "logistics": ("物流专员", 30, "物流专员会去查件并同步最新进度。"),
    "payment": ("财务专员", 60, "财务会核对款项与订单，尽快给您明确答复。"),
    "complaint": ("客服主管", 30, "已升级给客服主管，会有人专门跟进您的意见。"),
    "user_request": ("人工客服", 30, "已为您转接人工客服。"),
}
_DEFAULT_CONTEXT = ("人工客服", 30, "已为您转接人工客服，请稍候。")


def context_hint(reason: str) -> tuple:
    """返回 (受理角色, 承诺分钟数, 安抚话术)。"""
    return _REASON_CONTEXT.get(reason, _DEFAULT_CONTEXT)


def ticket_payload(ticket) -> dict:
    """工单 → 前端卡片 / 接口响应共用的字段。"""
    owner, sla_minutes, comfort = context_hint(ticket.reason)
    return {
        "ticket_no": ticket.ticket_no,
        "reason": ticket.get_reason_display(),
        "status": ticket.get_status_display(),
        "owner": owner,
        "sla_minutes": sla_minutes,
        "comfort": comfort,
        # 有没有留电话决定卡片上要不要提示「补一个联系方式，方便客服回电」
        "has_phone": bool(ticket.contact_phone),
        "created_at": ticket.created_at.strftime("%Y-%m-%d %H:%M") if ticket.created_at else "",
    }


def ticket_line(ticket) -> str:
    """给模型的一句可直接转述的话（要求它不要展开、不要让顾客重复描述）。"""
    owner, sla_minutes, comfort = context_hint(ticket.reason)
    return (
        f"已为顾客创建工单 {ticket.ticket_no}，将由{owner}跟进，承诺 {sla_minutes} 分钟内联系。"
        f"{comfort}\n"
        f"请用一句话告知顾客「已记录，工单号 {ticket.ticket_no}，客服会尽快联系」，"
        f"不要再让顾客重复描述问题。"
    )
