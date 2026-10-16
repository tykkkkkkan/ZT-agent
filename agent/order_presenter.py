"""
agent/order_presenter.py — 订单「对外呈现」的唯一出口

为什么单独成模块
----------------
订单要露给三个地方：个人中心 `/api/me/orders/`、查订单页 `/api/orders/*`、
以及**AI 对话里的订单名片**（`agent/tools.py` 登记 → `agent/agent.py` 发 `order_card` 事件）。

如果三处各写一套「这笔能不能支付 / 能不能评价」，就会出现
「聊天名片上有『立即支付』按钮，点进去个人中心说不能付」这类自相矛盾 ——
和此前价格口径分裂（AI 报一个价、结算另一个价）是同一类坑。

因此把两件事收在这里、只写一次：

  · `order_actions()`   —— 按状态推导用户此刻能做什么（返回动作名列表）
  · `serialize_order()` —— 订单脱敏序列化（个人中心 / 订单页用，含 actions）
  · `order_card()`      —— 订单名片的精简结构（对话卡片用，同样带 actions）

`agent/views.py` 仍以 `_order_actions` / `_serialize_order` 的名字重新导出，
因此 `agent/admin.py`、`agent/me_views.py` 与既有验证脚本无需改动。
"""

from agent.models import OrderStatus, PaymentStatus, ReturnStatus


# ══════════════════════════════════════════════════════════════
# 用户可执行操作（唯一规则出口）
# ══════════════════════════════════════════════════════════════
def order_actions(order) -> list:
    """按当前状态推导「用户可执行的操作」列表。

    规则集中在这里，前端只按返回的按钮名渲染，避免前后端各写一套状态判断而漂移。
    """
    from agent import reviews as review_helper

    s = order.status
    acts = []
    if s == OrderStatus.PENDING:
        # 任务书五·1：未发货且未支付 → 可（模拟）支付。
        # 历史订单 payment_status 为空（未跟踪），不显示支付按钮 —— 那批单本来就没走支付流程。
        if getattr(order, 'payment_status', '') == PaymentStatus.UNPAID:
            acts.append('pay')
        acts.append('cancel')                       # 未发货：可取消
    if s == OrderStatus.SHIPPED:
        acts.append('confirm')                      # 已发货：可确认收货
        acts.append('return')                       #        可申请退货
    if s == OrderStatus.COMPLETED:
        acts.append('return')                       # 已完成：仍可申请售后
        # 任务书六·4：已完成（=已确认收货）且尚未评价 → 可评价。
        # 判定统一问 reviews.can_review，不在这里复写"已完成 + 未评价"的条件。
        if review_helper.can_review(order):
            acts.append('review')
    if s == OrderStatus.RETURNING and order.return_status == ReturnStatus.PENDING:
        acts.append('withdraw_return')              # 退货待审核：可自行撤销
    return acts


# ══════════════════════════════════════════════════════════════
# 订单脱敏序列化（个人中心 / 查订单页 / 下单成功页）
# ══════════════════════════════════════════════════════════════
def serialize_order(order, with_actions: bool = False) -> dict:
    """订单脱敏序列化（不返回姓名/电话等隐私字段，含物流信息与售后状态）

    :param with_actions: 是否附带「用户可执行操作」列表（个人中心与名片用）

    ⚠️ 三个调用方（/api/orders/query/、/api/orders/<no>/、/api/me/orders/）**都已做归属校验**，
    因此这里可以返回收货地址与期望交期 —— 它们是「订单本人的数据」，
    而不是「凭手机号就能看到的他人信息」（后者由 `me_helpers.my_orders_q` 兜住）。
    """
    data = {
        "order_no": order.order_no,
        "product_id": order.product_id,
        "product_sku": order.product_sku or "",            # P0 新增：SKU 快照
        "product_name": order.product_name,
        "quantity": order.quantity,
        "unit_price": float(order.unit_price or 0),         # P0 新增：单价快照
        "total_price": float(order.total_price or 0),
        "status": order.status,
        # 任务书四·4：下单时填写的收货地址与期望交期
        "address": getattr(order, "address", "") or "",
        "expected_date": (order.expected_date.strftime("%Y-%m-%d")
                          if getattr(order, "expected_date", None) else ""),
        # 任务书五·1：支付维度（与 status 正交）。空字符串 = 支付功能上线前的历史订单。
        "payment_status": getattr(order, "payment_status", "") or "",
        "paid_at": (order.paid_at.strftime("%Y-%m-%d %H:%M:%S")
                    if getattr(order, "paid_at", None) else ""),
        "ship_company": order.ship_company or '',
        "tracking_no": order.tracking_no or '',
        "shipped_at": order.shipped_at.strftime("%Y-%m-%d %H:%M:%S") if order.shipped_at else '',
        "cancelled_at": order.cancelled_at.strftime("%Y-%m-%d %H:%M:%S") if order.cancelled_at else '',
        "cancel_reason": order.cancel_reason or '',        # P0 新增
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else '',
        # ── 售后 / 收货闭环（个人中心展示 + 后台反馈） ──
        "completed_at": order.completed_at.strftime("%Y-%m-%d %H:%M:%S") if order.completed_at else '',
        "return_status": order.return_status or '',
        "return_reason": order.return_reason or '',
        "return_requested_at": (order.return_requested_at.strftime("%Y-%m-%d %H:%M:%S")
                                if order.return_requested_at else ''),
        "return_handled_at": (order.return_handled_at.strftime("%Y-%m-%d %H:%M:%S")
                              if order.return_handled_at else ''),
        "return_note": order.return_note or '',
    }
    # 任务书六·4：评价。已完成订单才可能评价，但历史已评价的订单（万一状态被回退过）
    # 也要把评价原文带出来，免得用户"评价过却看不到"。
    from agent import reviews as review_helper
    rv = review_helper.order_review(order)
    data["review"] = review_helper.serialize_review(rv) if rv else None
    data["review_block_reason"] = (
        review_helper.reviewable_reason(order)
        if order.status == OrderStatus.COMPLETED and not rv else ""
    )
    if with_actions:
        data["actions"] = order_actions(order)
    return data


# ══════════════════════════════════════════════════════════════
# 订单名片（AI 对话卡片）
# ══════════════════════════════════════════════════════════════
def order_card(order) -> dict:
    """订单名片的精简结构 —— 只带卡片要展示与要动作的字段。

    与 `serialize_order` 的区别：这里**不返回收货地址、评价原文、退货原因**等
    对话里用不到、又偏敏感的长字段；名片只回答客户最关心的三件事：
    「这单是哪笔 / 到哪了 / 我下一步能做什么」。

    `actions` 仍取自 `order_actions()` —— 名片上的按钮与个人中心完全同源。
    """
    spec = ""
    product = getattr(order, "product", None)
    if product is not None:
        spec = product.spec or ""
    return {
        "order_no": order.order_no,
        "product_id": order.product_id,
        "product_name": order.product_name or "",
        "spec": spec,
        "quantity": order.quantity or 0,
        "unit_price": float(order.unit_price or 0),
        "total_price": float(order.total_price or 0),
        "status": order.status or "",
        "payment_status": getattr(order, "payment_status", "") or "",
        "tracking_no": order.tracking_no or "",
        "ship_company": order.ship_company or "",
        "shipped_at": order.shipped_at.strftime("%Y-%m-%d") if order.shipped_at else "",
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
        "cancelled_at": order.cancelled_at.strftime("%Y-%m-%d %H:%M") if order.cancelled_at else "",
        "cancel_reason": order.cancel_reason or "",
        "return_status": order.return_status or "",
        "actions": order_actions(order),
    }
