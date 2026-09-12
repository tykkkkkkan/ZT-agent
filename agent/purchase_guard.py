"""
agent/purchase_guard.py — 「这个商品现在能不能买」的**唯一判定入口**

为什么单独抽出来
----------------
改造前，可购买性判断散在三处且互不一致：

  · 前台产品接口（`views.product_list`）—— 只返回 `is_active` 与库存数字，
    **完全不读** 跨 Agent 协调覆盖表 `ProductCoordination`；
  · 下单接口（`views.create_order_api`）—— 只校验 `is_active` 与可用库存，
    同样不读协调覆盖；
  · 后台/营销侧库存接口（`views.inventory_list_api`）—— 读协调覆盖，
    于是**只有后台能看到「暂停购买」，前台照卖**。

直接后果（已实测）：营销 Agent 巡检发现「速攻2号」断货 → 下发
`pause_product` → 覆盖表写入 `purchase_paused=True` 与客户提示语 →
营销侧页面显示「已下发生效」，但**前台下拉里照样能选中并下单成功**。
两边对同一商品的可售状态认知不同 → 就是「数据不同步」。

本模块把判定收敛为一处，供三处共用：

    product_list        → 用于渲染（返回 can_buy / 不可购原因 / 客户提示）
    create_order_api    → 用于拦截（不可购直接 400，落库前就拦住）
    inventory_list_api  → 用于后台展示（复用同一套原因文案）

判定优先级（自上而下，先命中的胜出）
    1. 商品已下架            → 不可购（is_active=False）
    2. 跨 Agent 暂停购买      → 不可购（ProductCoordination.purchase_paused）
    3. 可用库存 <= 0          → 不可购（stock - reserved_stock <= 0）
    4. 其余                  → 可购；若存在客户提示语则照常回带（仅作提醒）
"""
from __future__ import annotations

from typing import Iterable, Optional

# 不可购原因码（前端按码做样式，文案由后端给，避免前后端各写一套）
REASON_INACTIVE = "inactive"
REASON_PAUSED = "purchase_paused"
REASON_SOLD_OUT = "sold_out"
REASON_NO_INVENTORY = "no_inventory"

DEFAULT_PAUSED_NOTICE = "该商品暂时缺货，已暂停接单，补货后即可下单。"
DEFAULT_SOLD_OUT_NOTICE = "该商品已售罄，正在补货中，可先咨询客服。"


def load_context(product_ids: Optional[Iterable[int]] = None) -> tuple[dict, dict]:
    """一次性取出库存与协调覆盖（避免逐商品查询造成 N+1）。

    :param product_ids: 限定范围；None 表示全量
    :return: (inv_map, coord_map)，键均为 product_id
    """
    from agent.models import Inventory, ProductCoordination

    inv_qs = Inventory.objects.all()
    coord_qs = ProductCoordination.objects.all()
    if product_ids is not None:
        ids = list(product_ids)
        inv_qs = inv_qs.filter(product_id__in=ids)
        coord_qs = coord_qs.filter(product_id__in=ids)
    return (
        {i.product_id: i for i in inv_qs},
        {c.product_id: c for c in coord_qs},
    )


def evaluate(product, inv=None, coord=None) -> dict:
    """判定单个商品的可购买性。

    :param product: Products 实例
    :param inv:     对应 Inventory 实例（可为 None，表示没有库存记录）
    :param coord:   对应 ProductCoordination 实例（可为 None，表示无协调覆盖）
    :return: {
        "can_buy": bool,
        "reason": str,          # 不可购原因码；可购时为 ""
        "message": str,         # 面向用户的可读原因；可购时为 ""
        "customer_notice": str, # 客户提示语（可购时也可能有，用于「即将断货」类提醒）
        "available_stock": int,
    }
    """
    notice = ""
    if coord is not None and (coord.customer_notice or "").strip():
        notice = (coord.customer_notice or "").strip()

    available = 0
    if inv is not None:
        try:
            available = int(inv.available_stock or 0)
        except (TypeError, ValueError):
            available = 0

    def _blocked(reason: str, message: str) -> dict:
        return {
            "can_buy": False,
            "reason": reason,
            "message": message,
            "customer_notice": notice or message,
            "available_stock": available,
        }

    # 1) 已下架
    if not getattr(product, "is_active", True):
        return _blocked(REASON_INACTIVE, "该商品已下架，暂不可下单。")

    # 2) 跨 Agent 协调：暂停购买（营销侧断货时下发）
    if coord is not None and coord.purchase_paused:
        return _blocked(REASON_PAUSED, notice or DEFAULT_PAUSED_NOTICE)

    # 3) 无库存记录 / 已售罄
    if inv is None:
        return _blocked(REASON_NO_INVENTORY, "该商品暂无库存记录，请稍后再试。")
    if available <= 0:
        return _blocked(REASON_SOLD_OUT, notice or DEFAULT_SOLD_OUT_NOTICE)

    return {
        "can_buy": True,
        "reason": "",
        "message": "",
        "customer_notice": notice,
        "available_stock": available,
    }


def evaluate_map(products: Iterable) -> dict[int, dict]:
    """批量判定（含一次性上下文加载），返回 {product_id: evaluate(...)}"""
    products = list(products)
    inv_map, coord_map = load_context(p.id for p in products)
    return {
        p.id: evaluate(p, inv_map.get(p.id), coord_map.get(p.id))
        for p in products
    }


def check_buyable(product, *, quantity: int = 1) -> tuple[bool, str, str]:
    """下单前的强制校验（供 create_order_api 调用）。

    :param product:  Products 实例
    :param quantity: 本次购买数量
    :return: (can_buy, code, message)
              · code 为 "insufficient_stock" 时 message 已含可用/需求数量
    """
    from agent.models import Inventory, ProductCoordination

    inv = Inventory.objects.filter(product_id=product.id).first()
    coord = ProductCoordination.objects.filter(product_id=product.id).first()
    verdict = evaluate(product, inv, coord)
    if not verdict["can_buy"]:
        return False, verdict["reason"], verdict["message"]

    if quantity > verdict["available_stock"]:
        return False, "insufficient_stock", (
            f"「{product.name}」库存不足：可用 {verdict['available_stock']} 包，"
            f"您需要 {quantity} 包。"
        )
    return True, "", ""
