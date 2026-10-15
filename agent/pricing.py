"""
agent/pricing.py — 价格口径的**唯一出口**（任务书二·5：身份差异化价格）

为什么需要这个模块
------------------
改造前的现状是「价格」这一个概念在不同出口各说各话：

    · 产品页 / 下单页  → 一律用 `retail_price`（下单落库也是零售价）
    · AI 报价工具      → 一律用 `wholesale_price`
    · AI 查询产品      → 把零售价与批发价一起抛给客户
    · 系统提示词       → 写着「批发价只对经销商展示；个体钓友只报零售价」

于是出现两种最坏的口径不一致：
    1. **泄露**：个体钓友在产品页和 AI 对话里都能看到批发价；
    2. **报价与成交不符**：AI 按批发价报了价，客户下单时按零售价结算 ——
       客户会觉得"你们报价不算数"，这比报错价更伤。

所以本模块把规则收成一处：

    单价 = 经销商 且 有批发价 且 数量达到起订量  →  批发价
           其余情况                            →  零售价

三条约束
--------
1. **裁剪发生在接口层**，不是前端隐藏 —— 前端隐藏等于没隐藏，抓个包就看见了。
   非经销商拿到的响应体里**根本没有 `wholesale_price` 这个键**。
2. **未登录 / 无身份的访客一律按个体钓友处理**（宁可少给，不可多给）。
3. 订单表里存的 `unit_price` 是**下单当时的快照**，历史订单不随身份变化而变化 ——
   客户今天改成经销商，不会把上个月的订单价格改掉（这是审计要求）。
"""
from __future__ import annotations

from typing import Optional

from agent.models import UserProfile

#: 批发起订量。低于这个数量即使经销商也走零售价（阶梯价的第一档）。
#: 前端不再硬编码"10 包起"，一律读接口下发的 `wholesale_min_qty`。
WHOLESALE_MIN_QTY = 10

INDIVIDUAL = UserProfile.CustomerType.INDIVIDUAL
WHOLESALE = UserProfile.CustomerType.WHOLESALE


# ════════════════════════════════════════════════════════════════
# 身份
# ════════════════════════════════════════════════════════════════
def customer_type_of(user) -> str:
    """取该用户的客户类型；未登录 / 无资料 / 脏数据一律降级为「个体钓友」。

    降级方向很关键：**宁可少给权限，不可多给**。任何一个环节出问题
    （未登录、profile 不存在、字段值非法），结果都是看不到批发价，
    而不是把批发价泄露给所有人。
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return INDIVIDUAL
    try:
        profile = UserProfile.objects.filter(user=user).first()
    except Exception:                                    # noqa: BLE001
        return INDIVIDUAL
    if profile is None:
        return INDIVIDUAL
    value = (profile.customer_type or "").strip()
    return value if value in UserProfile.CustomerType.values else INDIVIDUAL


def is_wholesale(user) -> bool:
    return customer_type_of(user) == WHOLESALE


def customer_type_label(user) -> str:
    """中文标签（个人中心与 AI 回复用）。"""
    return UserProfile.CustomerType(customer_type_of(user)).label


# ════════════════════════════════════════════════════════════════
# 单价
# ════════════════════════════════════════════════════════════════
def retail_of(product) -> float:
    return float(product.retail_price or 0)


def wholesale_of(product) -> Optional[float]:
    """批发价；未维护返回 None（不能返回 0 —— 0 元卖出去是事故）。"""
    price = product.wholesale_price
    if price is None:
        return None
    value = float(price)
    return value if value > 0 else None


def price_for(product, user=None, quantity: int = 0) -> float:
    """**成交单价**：订单落库、AI 报价都调它，保证"报多少就按多少成交"。

    :param quantity: 本单数量。经销商未达起订量时仍走零售价（阶梯价第一档）。
    """
    if is_wholesale(user):
        wp = wholesale_of(product)
        if wp is not None and int(quantity or 0) >= WHOLESALE_MIN_QTY:
            return wp
    return retail_of(product)


def price_fields(product, user=None, *, mask: bool = True, quantity: int = 0) -> dict:
    """生成产品响应里的价格字段（对外接口统一用它）。

    :param mask: True = 按身份裁剪（前台）；False = 不裁剪（管理端后台）。
        管理端必须显式传 False —— 不能靠"viewer 是不是 None"来判断，
        因为 None 也代表**未登录访客**，那是最需要裁剪的人。

    非经销商返回体里**不含 `wholesale_price` 键**（不是设成 None）。
    键不存在，前端拿到的就是 undefined，抓包也看不到批发价。
    """
    can_see = (not mask) or is_wholesale(user)
    data = {
        "retail_price": retail_of(product),
        "wholesale_min_qty": WHOLESALE_MIN_QTY,
        "customer_type": customer_type_of(user),
        "can_see_wholesale": can_see,
        # 该身份 + 该数量下的成交单价：前端据此实时算总额，与后端落库口径一致
        "effective_price": price_for(product, user, quantity),
    }
    if can_see:
        wp = wholesale_of(product)
        if wp is not None:
            data["wholesale_price"] = wp
    return data


# ════════════════════════════════════════════════════════════════
# 面向客户的一句话（AI 工具层用）
# ════════════════════════════════════════════════════════════════
def audience_line(user) -> str:
    """告诉 AI「现在对面是什么人」，避免它自己猜身份。"""
    if is_wholesale(user):
        return f"当前客户身份：经销商（{WHOLESALE_MIN_QTY} 包起享批发价）"
    return "当前客户身份：个体钓友（仅报零售价，不要透露批发价）"
