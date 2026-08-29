"""
agent/services.py — 库存联动 + 订单状态机（事务安全）

设计目标
--------
1. **库存与订单状态严格同步**：状态变更、库存变动必须在同一事务内
   完成（`transaction.atomic` + `select_for_update` 锁行）。
2. **业务规则集中**：所有"订单 ↔ 库存"的耦合逻辑放这里，
   views/tools/admin 都不直接操作 Inventory。
3. **可调用可测试**：纯函数式 API，无 Django request 依赖。

状态机
------

    [PENDING] --发货--> [SHIPPED]    （扣减 stock 和 reserved_stock）
        │
        └---取消--> [CANCELLED]      （释放 reserved_stock）
        └---退货--> [RETURNED]       （视具体实现，见 return_inventory）

调用样例
--------
    from agent.services import transition_order, OrderTransitionError

    try:
        transition_order(
            order, to_status=OrderStatus.SHIPPED,
            ship_company="顺丰", tracking_no="SF1234567890",
            actor="admin",
        )
    except OrderTransitionError as e:
        # 业务校验失败（库存不足、状态非法等）
        ...
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from agent.models import Inventory, Orders, OrderStatus, Wallet, Transaction, TxType, TxCategory


# ════════════════════════════════════════════════════════════════
# 异常
# ════════════════════════════════════════════════════════════════
class OrderTransitionError(Exception):
    """订单状态机非法跃迁 / 业务规则校验失败。"""
    def __init__(self, message: str, code: str = "invalid_transition"):
        super().__init__(message)
        self.code = code
        self.message = message


class InsufficientStockError(OrderTransitionError):
    """库存不足，无法完成本次状态变更。"""
    def __init__(self, message: str, available: int, requested: int):
        super().__init__(message, code="insufficient_stock")
        self.available = available
        self.requested = requested


# ════════════════════════════════════════════════════════════════
# 库存操作原语（行级锁）
# ════════════════════════════════════════════════════════════════
def _lock_inventory(product_id: int) -> Optional[Inventory]:
    """对 product_id 加行级锁，返回 Inventory（或 None）。

    必须在 transaction.atomic 块内调用。MySQL 下为 `SELECT ... FOR UPDATE`。
    """
    return Inventory.objects.select_for_update().filter(product_id=product_id).first()


def reserve_inventory(order: Orders) -> Inventory:
    """订单创建后调用：把订单数量从可用库存预占出去（reserved_stock += qty）。

    不会减少 stock，只标记"被占"——发货时再真正扣减。
    若可用库存不足抛 InsufficientStockError。
    """
    with transaction.atomic():
        inv = _lock_inventory(order.product_id)
        if inv is None:
            raise OrderTransitionError(
                f"产品 {order.product_id} 暂无库存记录，无法下单。", code="no_inventory",
            )
        if inv.available_stock < order.quantity:
            raise InsufficientStockError(
                f"「{inv.product.name}」库存不足：可用 {inv.available_stock} 包，"
                f"订单需 {order.quantity} 包。",
                available=inv.available_stock, requested=order.quantity,
            )
        inv.reserved_stock = (inv.reserved_stock or 0) + order.quantity
        inv.save(update_fields=["reserved_stock", "updated_at"])
        return inv


def deduct_inventory_on_ship(order: Orders) -> Inventory:
    """订单发货时调用：真正扣减库存（stock -= qty, reserved_stock -= qty）。

    已预占的订单：reserved_stock 先释放、再扣 stock。
    未预占的订单（兼容老数据）：只扣 stock，不动 reserved_stock。
    """
    with transaction.atomic():
        inv = _lock_inventory(order.product_id)
        if inv is None:
            raise OrderTransitionError(
                f"产品 {order.product_id} 库存记录缺失，无法发货。", code="no_inventory",
            )
        qty = order.quantity or 0
        if (inv.stock or 0) < qty:
            raise InsufficientStockError(
                f"「{inv.product.name}」库存不足：现有 {inv.stock} 包，发货需 {qty} 包。",
                available=inv.stock or 0, requested=qty,
            )
        inv.stock = (inv.stock or 0) - qty
        inv.reserved_stock = max(0, (inv.reserved_stock or 0) - qty)
        inv.save(update_fields=["stock", "reserved_stock", "updated_at"])
        return inv


def release_inventory_on_cancel(order: Orders) -> Optional[Inventory]:
    """订单取消时调用：释放预占库存（reserved_stock -= qty）。

    若订单未预占过（兼容老数据），不做任何事。stock 不会变（因为本来就没扣）。
    """
    with transaction.atomic():
        inv = _lock_inventory(order.product_id)
        if inv is None:
            return None
        qty = order.quantity or 0
        if (inv.reserved_stock or 0) >= qty:
            inv.reserved_stock = (inv.reserved_stock or 0) - qty
            inv.save(update_fields=["reserved_stock", "updated_at"])
        return inv


def restock_inventory_on_return(order: Orders) -> Inventory:
    """订单退货时调用：库存回滚（stock += qty）。

    已发货后被退货：需要把货重新入库。reserved_stock 不变。
    """
    with transaction.atomic():
        inv = _lock_inventory(order.product_id)
        if inv is None:
            raise OrderTransitionError(
                f"产品 {order.product_id} 库存记录缺失，无法退货入库。", code="no_inventory",
            )
        inv.stock = (inv.stock or 0) + (order.quantity or 0)
        inv.save(update_fields=["stock", "updated_at"])
        return inv


# ════════════════════════════════════════════════════════════════
# 钱包操作（订单状态自动记账的"金流"侧）
# ════════════════════════════════════════════════════════════════
def _record_wallet_tx(
    tx_type: str,
    category: str,
    amount,
    order: Optional[Orders] = None,
    note: str = "",
    operator=None,
) -> Transaction:
    """在事务内同时更新钱包余额 + 写一条流水。

    :param tx_type: TxType.INCOME / TxType.EXPENSE
    :param category: TxCategory.*
    :param amount: Decimal（正数；tx_type 决定加减方向）
    """
    amt = Decimal(str(amount or 0))
    if amt <= 0:
        # 金额为 0 或负：跳过记账（保留接口幂等），但返回 None 以便调用方判断
        return None
    wallet = Wallet.get_solo()
    with transaction.atomic():
        # 重新对钱包行加锁，防并发
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if tx_type == TxType.INCOME:
            wallet.balance = (wallet.balance or Decimal("0")) + amt
        else:
            wallet.balance = (wallet.balance or Decimal("0")) - amt
        wallet.save(update_fields=["balance", "updated_at"])
        tx = Transaction.objects.create(
            wallet=wallet,
            tx_type=tx_type,
            category=category,
            amount=amt,
            order=order,
            note=note or "",
            operator=operator,
        )
    return tx


def record_order_income(order: Orders, operator=None) -> Optional[Transaction]:
    """订单发货 → 自动记一笔「订单收入」。"""
    return _record_wallet_tx(
        tx_type=TxType.INCOME,
        category=TxCategory.ORDER_INCOME,
        amount=order.total_price or 0,
        order=order,
        note=f"订单 {order.order_no} 发货入账",
        operator=operator,
    )


def record_order_refund(order: Orders, operator=None, note: str = "") -> Optional[Transaction]:
    """订单退货 / 取消 → 自动记一笔「订单退款/支出」。"""
    return _record_wallet_tx(
        tx_type=TxType.EXPENSE,
        category=TxCategory.ORDER_REFUND,
        amount=order.total_price or 0,
        order=order,
        note=note or f"订单 {order.order_no} 退款",
        operator=operator,
    )


def manual_wallet_adjust(amount, *, direction: str = "income", note: str = "", operator=None) -> Optional[Transaction]:
    """后台手动调账：充值（income）或扣款/提现（expense）。

    :param amount: 正数金额（Decimal 或字符串）
    :param direction: 'income' 充值 / 'expense' 扣款
    :param note: 备注（说明这笔钱从哪来 / 用到哪去）
    :param operator: 操作人（auth.User）
    :return: 生成的流水，金额<=0 时返回 None（幂等跳过）
    """
    if direction == "expense":
        return _record_wallet_tx(
            tx_type=TxType.EXPENSE,
            category=TxCategory.MANUAL,
            amount=amount,
            note=note or "手动扣款",
            operator=operator,
        )
    return _record_wallet_tx(
        tx_type=TxType.INCOME,
        category=TxCategory.MANUAL,
        amount=amount,
        note=note or "手动充值",
        operator=operator,
    )


# ════════════════════════════════════════════════════════════════
# 订单状态机
# ════════════════════════════════════════════════════════════════
@dataclass
class OrderTransitionResult:
    order: Orders
    inventory: Optional[Inventory]
    message: str


def transition_order(
    order: Orders,
    to_status: str,
    *,
    ship_company: str = "",
    tracking_no: str = "",
    cancel_reason: str = "",
) -> OrderTransitionResult:
    """统一订单状态机入口（在事务内完成"改状态 + 动库存"）。

    :param order: 待变更订单（会重新 select_for_update 加锁）
    :param to_status: 目标状态（OrderStatus.*）
    :param ship_company / tracking_no: 发货时必填
    :param cancel_reason: 取消时建议填
    :raises OrderTransitionError: 非法跃迁 / 业务校验失败
    """
    allowed = {
        OrderStatus.SHIPPED: {OrderStatus.PENDING},
        OrderStatus.CANCELLED: {OrderStatus.PENDING},
        OrderStatus.RETURNED: {OrderStatus.SHIPPED},
    }
    if to_status not in allowed:
        raise OrderTransitionError(f"未知目标状态：{to_status}", code="unknown_status")
    if order.status not in allowed[to_status]:
        raise OrderTransitionError(
            f"订单 {order.order_no} 当前状态为「{order.status}」，"
            f"无法跃迁到「{to_status}」。",
            code="invalid_transition",
        )

    with transaction.atomic():
        # 行级锁：防止并发发货/取消同一订单
        order = Orders.objects.select_for_update().get(pk=order.pk)

        inv: Optional[Inventory] = None
        if to_status == OrderStatus.SHIPPED:
            if not ship_company or not tracking_no:
                raise OrderTransitionError(
                    "发货必须填写「物流公司」与「运单号」。", code="missing_ship_info",
                )
            order.ship_company = ship_company
            order.tracking_no = tracking_no
            order.shipped_at = timezone.now()
            inv = deduct_inventory_on_ship(order)
            # 自动记一笔「订单收入」（嵌套事务，安全）
            record_order_income(order)
            msg = f"订单已发货，库存扣减 {order.quantity} 包（剩余 {inv.stock}）。"

        elif to_status == OrderStatus.CANCELLED:
            order.cancelled_at = timezone.now()
            order.cancel_reason = cancel_reason or ""
            inv = release_inventory_on_cancel(order)
            msg = f"订单已取消，预占库存已释放。" if inv else "订单已取消。"

        elif to_status == OrderStatus.RETURNED:
            order.cancelled_at = timezone.now()  # 复用为退货时间
            order.cancel_reason = cancel_reason or "客户退货"
            inv = restock_inventory_on_return(order)
            # 退货：库存回滚 + 自动记一笔「订单退款」
            record_order_refund(order, note=f"订单 {order.order_no} 退货退款")
            msg = f"订单已退货，库存已回滚 {order.quantity} 包。"

        order.status = to_status
        order.save(update_fields=[
            "status", "ship_company", "tracking_no", "shipped_at",
            "cancelled_at", "cancel_reason", "updated_at",
        ])

    return OrderTransitionResult(order=order, inventory=inv, message=msg)


def revert_shipped_to_pending(order: Orders, operator=None) -> OrderTransitionResult:
    """把已发货订单退回"待发货"（admin 纠错用）。

    若之前已记过「订单收入」，会自动记一笔「订单退款」冲账。
    """
    if order.status != OrderStatus.SHIPPED:
        raise OrderTransitionError(
            f"订单 {order.order_no} 当前状态为「{order.status}」，无法撤回。",
            code="invalid_transition",
        )
    with transaction.atomic():
        order = Orders.objects.select_for_update().get(pk=order.pk)
        prev_income = Transaction.objects.filter(
            order=order, tx_type=TxType.INCOME, category=TxCategory.ORDER_INCOME,
        ).exists()
        order.status = OrderStatus.PENDING
        order.ship_company = ""
        order.tracking_no = ""
        order.shipped_at = None
        order.save(update_fields=[
            "status", "ship_company", "tracking_no", "shipped_at", "updated_at",
        ])
        if prev_income:
            record_order_refund(order, operator=operator, note=f"订单 {order.order_no} 撤回发货冲账")
    return OrderTransitionResult(
        order=order, inventory=None,
        message="已撤回到待发货，金额已自动冲账。" if prev_income else "已撤回到待发货。",
    )
