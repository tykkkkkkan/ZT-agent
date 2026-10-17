"""
agent/reviews.py — 商品评价（任务书六·4）的读写逻辑

职责边界
--------
· 本模块只管**评价本身**：能不能评、怎么评、评分怎么聚合；
· 不碰库存、不碰订单状态机（那是 `services.py` 的事）；
· 视图层只做鉴权与参数格式校验，业务规则全部收在这里。

三条硬规则
----------
1. **只有「已完成」的订单能评价**。理由很直白：货还没收到就打分没有意义，
   而且任务书写的是"对已完成订单发起评价"。已发货/退货申请中/已取消/已退货
   一律拒绝 —— 已退货的订单如果还能打分，会污染商品评分。
2. **一单一评，靠数据库唯一约束兜底**。`Review.order` 是 OneToOneField，
   这里再做一次存在性预检只是为了给用户一句人话提示（"这笔订单已经评价过了"），
   真正的防重是约束本身 —— 并发下预检会双双通过。
3. **评价人姓名脱敏后才对外展示**。商品页的评价列表是公开的，
   不能把客户全名亮出去（与「查订单」页的脱敏口径保持一致）。
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count

from agent.models import Orders, OrderStatus, Review

logger = logging.getLogger(__name__)

MIN_RATING = 1
MAX_RATING = 5
MAX_CONTENT_LEN = 500


class ReviewError(Exception):
    """评价相关的业务校验失败（视图层据此返回 4xx）。"""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# ════════════════════════════════════════════════════════════════
# 能不能评
# ════════════════════════════════════════════════════════════════
def has_review(order: Orders) -> bool:
    """该订单是否已评价。

    反向 OneToOne 访问不存在时 Django 抛 `Review.DoesNotExist`
    （实际是 `RelatedObjectDoesNotExist`，它是 DoesNotExist 的子类）。
    ⚠️ 调用方若已 `select_related('review')`，这里走的是缓存，不会产生额外查询；
    订单列表逐条调用本函数，别在未 prefetch 的循环里用。
    """
    try:
        # 访问反向 OneToOne 描述符即完成一次存在性探测，结果本身不需要保留
        _ = order.review
        return True
    except Review.DoesNotExist:
        return False


def order_review(order: Orders):
    """取该订单的评价；没有则返回 None（不抛异常）。

    给序列化层用 —— 那里只想知道"有没有"，不想写 try/except。
    ⚠️ 同样受 `select_related('review')` 缓存影响：预取了就不多查一次。
    """
    try:
        return order.review
    except Review.DoesNotExist:
        return None


def reviewable_reason(order: Orders) -> str:
    """返回"不能评价"的原因；可以评价时返回空字符串。

    单一出口：视图层、`_order_actions`、AI 工具层都用它判断，
    避免"前端显示能评、后端拒绝"这种口径漂移。
    """
    if order.status != OrderStatus.COMPLETED:
        return (f"订单当前为「{order.status}」，"
                f"只有已完成（已确认收货）的订单才能评价。")
    if not order.product_id:
        # 历史订单可能没有关联商品（product 可空），无法归到某个商品的评分上
        return "该订单没有关联商品，暂不支持评价。"
    if has_review(order):
        return "这笔订单已经评价过了，每笔订单只能评价一次。"
    return ""


def can_review(order: Orders) -> bool:
    return reviewable_reason(order) == ""


# ════════════════════════════════════════════════════════════════
# 写评价
# ════════════════════════════════════════════════════════════════
def create_review(
    order: Orders, *, rating, content: str = "", user=None,
) -> Review:
    """为订单创建评价。

    :param rating: 1–5 的整数（字符串数字也接受，前端表单常传字符串）
    :param content: 评价内容，可留空（允许"只打分不写字"）
    :param user: 评价人；一般传 `order.user`，历史订单可能为空
    :raises ReviewError: 订单状态不符 / 重复评价 / 评分非法
    """
    # ① 评分：先做类型与范围校验，别把脏数据丢给数据库
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        # 原始异常只是解析噪声，用户需要的是可读提示，故不保留异常链
        raise ReviewError("请先打分（1–5 星）", code="invalid_rating") from None
    if not (MIN_RATING <= rating <= MAX_RATING):
        raise ReviewError(f"评分需在 {MIN_RATING}–{MAX_RATING} 星之间",
                          code="invalid_rating")

    content = (content or "").strip()
    if len(content) > MAX_CONTENT_LEN:
        raise ReviewError(f"评价内容最多 {MAX_CONTENT_LEN} 个字",
                          code="content_too_long")

    # ② 状态与重复检查（给人话提示；真正的防重是下面的唯一约束）
    reason = reviewable_reason(order)
    if reason:
        raise ReviewError(reason, code="not_reviewable")

    owner = user or order.user
    try:
        with transaction.atomic():
            review = Review.objects.create(
                order=order,
                product_id=order.product_id,
                user_id=owner.id if owner else None,
                rating=rating,
                content=content,
            )
    except IntegrityError as exc:
        # 唯一约束拦住并发下的第二次提交 —— 这正是我们想要的行为，
        # 转成一句人话告诉用户，而不是抛 500；保留异常链便于日志排查。
        raise ReviewError("这笔订单已经评价过了，每笔订单只能评价一次。",
                          code="duplicate_review") from exc

    logger.info("订单 %s 已评价：%s 星", order.order_no, rating)
    return review


# ════════════════════════════════════════════════════════════════
# 读评价
# ════════════════════════════════════════════════════════════════
def mask_name(name: str) -> str:
    """评价人姓名脱敏：保留首字，其余打码。

    「张三」→「张*」    「李小明」→「李**」    「李」→「李」    空 → 「匿名客户」
    与「查订单」页只露部分信息的口径一致 —— 商品页是公开页面。
    """
    name = (name or "").strip()
    if not name:
        return "匿名客户"
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def rating_summary(product_ids=None) -> dict:
    """批量取商品评分：{product_id: {"rating_avg": 4.7, "review_count": 12}}

    一次 group by 拿全部商品的均分与条数，供列表页避免 N+1。
    `product_ids` 传 None 表示全量。
    """
    qs = Review.objects.values("product_id").annotate(
        avg=Avg("rating"), cnt=Count("id"),
    )
    if product_ids is not None:
        ids = list(product_ids)
        if not ids:
            return {}
        qs = qs.filter(product_id__in=ids)
    return {
        row["product_id"]: {
            "rating_avg": round(float(row["avg"] or 0), 1),
            "review_count": int(row["cnt"] or 0),
        }
        for row in qs
    }


def serialize_review(review: Review, *, include_content: bool = True) -> dict:
    """评价序列化（对外展示用，姓名已脱敏）。"""
    data = {
        "id": review.id,
        "product_id": review.product_id,
        "rating": review.rating,
        "stars": review.stars,
        "nickname": mask_name(
            (review.order.customer_name if review.order_id else "") or ""
        ),
        "created_at": (review.created_at.strftime("%Y-%m-%d %H:%M:%S")
                       if review.created_at else ""),
    }
    if include_content:
        data["content"] = review.content or ""
    return data


def product_reviews(product_id: int, *, limit: int = 20) -> dict:
    """单个商品的评分概览 + 最近评价列表（商品页用）。

    评价条数一律返回**真实总数**，不拿"本次列出的条数"冒充 —— 历史踩过
    「订单超过 20 笔却谎报共 20 笔」的坑，这里显式区分 `review_count` 与 `showing`。
    """
    limit = max(1, min(int(limit or 20), 50))
    qs = Review.objects.filter(product_id=product_id).select_related("order")
    total = qs.count()
    rows = list(qs.order_by("-created_at", "-id")[:limit])
    avg = qs.aggregate(avg=Avg("rating"))["avg"]
    return {
        "review_count": total,
        "showing": len(rows),
        "rating_avg": round(float(avg or 0), 1),
        "reviews": [serialize_review(r) for r in rows],
    }
