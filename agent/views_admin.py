"""
agent/views_admin.py — 后台数据看板（运营概览）

通过 Unfold 后台（SITE_URL / 左侧菜单「数据看板」）进入，
聚合展示核心业务指标：今日订单、待发货、未读留言、未读定制、
缺货预警、近 7 日对话量、公司收入概览（14 日收支趋势 / 收入构成 /
订单状态分布 / 最近流水），并附最近订单与未读列表，便于运营一屏掌握全局。
"""
from datetime import datetime, timedelta

from django.db.models import Sum, F, Q, Count
from django.db.models.functions import Coalesce, TruncDate
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.template.response import TemplateResponse

from agent.models import (
    Orders, ContactMessage, CustomRequest, Inventory, Conversations,
    Wallet, Transaction, TxType, TxCategory, OrderStatus,
    PAID_ORDER_STATUSES,
)


def _safe(fn, default=0):
    """任意指标查询失败（如数据库未连接）时返回默认值，避免看板 500。"""
    try:
        return fn()
    except Exception:
        return default


def _income_analytics(days: int = 14):
    """收支分析：近 N 日收支持续序列 + 收入构成 + 累计收入。

    返回 (series, max_val, composition, total_income)。
    series 每项：{label, income, expense, h_income, h_expense}（h_* 为 0-100 柱高）。
    """
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    rows = _safe(lambda: list(
        Transaction.objects
        .filter(created_at__date__gte=start)
        .annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(
            income=Sum("amount", filter=Q(tx_type=TxType.INCOME)),
            expense=Sum("amount", filter=Q(tx_type=TxType.EXPENSE)),
        )
    ), default=[]) or []
    by_day = {r["d"]: r for r in rows}

    series = []
    for i in range(days):
        d = start + timedelta(days=i)
        r = by_day.get(d, {})
        series.append({
            "label": d.strftime("%m-%d"),
            "income": float(r.get("income") or 0),
            "expense": float(r.get("expense") or 0),
        })
    max_val = max([max(s["income"], s["expense"]) for s in series], default=0) or 1.0
    for s in series:
        s["h_income"] = round(s["income"] / max_val * 100, 1)
        s["h_expense"] = round(s["expense"] / max_val * 100, 1)

    comp_rows = _safe(lambda: list(
        Transaction.objects.filter(tx_type=TxType.INCOME)
        .values("category").annotate(s=Sum("amount"))
    ), default=[]) or []
    cat_names = dict(TxCategory.choices)
    total_income = sum(float(r["s"] or 0) for r in comp_rows)
    composition = sorted(
        ({
            "category": cat_names.get(r["category"], r["category"]),
            "amount": float(r["s"] or 0),
            "pct": round(float(r["s"] or 0) / total_income * 100, 1) if total_income else 0,
        } for r in comp_rows),
        key=lambda x: -x["amount"],
    )
    return series, max_val, composition, total_income


@staff_member_required
def admin_dashboard(request):
    """后台首页数据看板

    ⚠️ 口径统一说明（改造前这里自算一套，与营销看板、C 端个人中心三方对不上）：
      · 成交额 `revenue` 原先 = `Sum(total_price)` over **全部订单**，
        把「已取消」「已退货」也算成收入 → 数字虚高，和营销侧 / 个人中心不一致。
        现在改用 `PAID_ORDER_STATUSES`（已发货 + 已完成 + 退货申请中），
        与 `me_views._order_stats.total_amount`、营销 `collect_metrics.gmv` 完全同源。
      · 库存「偏低 / 缺货」原先只看 `stock`，忽略了已被订单预占的部分 ——
        stock=5 但 reserved=5 时可用库存为 0、前台已不可购，看板却显示"正常"。
        现在统一按**可用库存**（stock − reserved_stock）判定，与营销侧
        `analytics` 的库存健康度算法一致。
      · 新增「退货申请中」待处理数：这是需要人立刻动手的事，必须出现在首页。
    """
    now = datetime.now()
    today = now.date()
    week_ago = now - timedelta(days=7)

    orders_today = _safe(lambda: Orders.objects.filter(created_at__date=today).count())
    pending_orders = _safe(lambda: Orders.objects.filter(status="未发货").count())
    total_orders = _safe(lambda: Orders.objects.count())
    # 成交额：只统计真正成交的订单（与营销看板、个人中心同口径）
    revenue = _safe(
        lambda: float(Orders.objects.filter(
            status__in=[s.value for s in PAID_ORDER_STATUSES],
        ).aggregate(s=Sum("total_price"))["s"] or 0)
    )
    # 待商家处理的退货申请 —— 首页必须一眼看到
    returning_orders = _safe(
        lambda: Orders.objects.filter(status=OrderStatus.RETURNING).count())
    refunded_orders = _safe(
        lambda: Orders.objects.filter(status=OrderStatus.RETURNED).count())
    unread_msgs = _safe(lambda: ContactMessage.objects.filter(is_read=False).count())
    unread_custom = _safe(lambda: CustomRequest.objects.filter(is_read=False).count())

    # 库存：一律按可用库存（stock − reserved_stock）判定，与营销侧一致
    low_stock = _safe(
        lambda: Inventory.objects.annotate(
            avail=F("stock") - Coalesce(F("reserved_stock"), 0),
        ).filter(
            stock__isnull=False, alert_line__isnull=False,
            avail__gt=0, avail__lte=F("alert_line"),
        ).count()
    )
    out_stock = _safe(
        lambda: Inventory.objects.annotate(
            avail=F("stock") - Coalesce(F("reserved_stock"), 0),
        ).filter(avail__lte=0).count()
    )
    conv_7d = _safe(lambda: Conversations.objects.filter(created_at__gte=week_ago).count())

    # 余额 / 流水汇总
    wallet_balance = _safe(lambda: float(Wallet.get_solo().balance))
    month_start = today.replace(day=1)
    income_month = _safe(
        lambda: float(Transaction.objects.filter(
            tx_type=TxType.INCOME, created_at__date__gte=month_start,
        ).aggregate(s=Sum("amount"))["s"] or 0)
    )
    expense_month = _safe(
        lambda: float(Transaction.objects.filter(
            tx_type=TxType.EXPENSE, created_at__date__gte=month_start,
        ).aggregate(s=Sum("amount"))["s"] or 0)
    )
    income_today = _safe(
        lambda: float(Transaction.objects.filter(
            tx_type=TxType.INCOME, created_at__date=today,
        ).aggregate(s=Sum("amount"))["s"] or 0)
    )

    # 公司收入概览（14 日趋势 / 收入构成 / 订单状态分布 / 最近流水）
    income_series, tx_max, income_composition, income_total = _income_analytics(14)
    status_dist = _safe(lambda: list(
        Orders.objects.values("status").annotate(c=Count("id"))
    ), default=[]) or []
    recent_txs = _safe(lambda: list(
        Transaction.objects.select_related("order")
        .order_by("-created_at")[:8]
        .values("tx_type", "category", "amount", "note", "created_at",
                "order_id", "order__order_no")
    ), default=[]) or []

    recent_orders = []
    try:
        recent_orders = list(
            Orders.objects.all().order_by("-created_at")[:8].values(
                "id", "order_no", "customer_name", "product_name",
                "quantity", "total_price", "status", "created_at",
            )
        )
    except Exception:
        pass

    unread_list = []
    try:
        unread_list = list(
            ContactMessage.objects.filter(is_read=False)
            .order_by("-created_at")[:6].values("id", "name", "phone", "message", "created_at")
        )
    except Exception:
        pass

    # 指标卡片均可点击直达对应后台列表（点击 → 对应筛选/详情，减少二次查找）
    metrics = [
        {"key": "orders_today", "label": "今日订单", "value": orders_today,
         "unit": "单", "icon": "shopping_cart", "tone": "jade",
         "sub": f"待发货 {pending_orders} 单",
         "href": "/admin/agent/orders/"},
        {"key": "pending", "label": "待发货订单", "value": pending_orders,
         "unit": "单", "icon": "local_shipping", "tone": "fire",
         "sub": "需尽快跟进",
         "href": "/admin/agent/orders/?ship=pending"},
        {"key": "revenue", "label": "累计销售额", "value": f"¥{revenue:,.0f}",
         "unit": "", "icon": "payments", "tone": "lake",
         "sub": f"已发货/已完成/退货中，共 {total_orders} 笔订单",
         "href": "/admin/agent/transaction/"},
        # 退货申请中：需要人立刻处理的售后，必须出现在首页（此前完全看不到）
        {"key": "returning", "label": "退货待处理", "value": returning_orders,
         "unit": "单", "icon": "assignment_return", "tone": "fire",
         "sub": f"已退货 {refunded_orders} 单 · 等商家处理",
         "href": "/admin/agent/orders/?status__exact=%E9%80%80%E8%B4%A7%E7%94%B3%E8%AF%B7%E4%B8%AD" },
        {"key": "unread_msg", "label": "未读留言", "value": unread_msgs,
         "unit": "条", "icon": "mark_email_unread", "tone": "fire",
         "sub": "客服待处理",
         "href": "/admin/agent/contactmessage/?is_read__exact=0"},
        {"key": "unread_custom", "label": "未读定制", "value": unread_custom,
         "unit": "条", "icon": "checklist", "tone": "fire",
         "sub": "定制待跟进",
         "href": "/admin/agent/customrequest/?is_read__exact=0"},
        {"key": "low_stock", "label": "库存预警", "value": low_stock + out_stock,
         "unit": "项", "icon": "warning", "tone": "fire",
         "sub": f"缺货 {out_stock} · 低于预警 {low_stock}",
         "href": "/admin/agent/inventory/?stock_state=low"},
        {"key": "wallet", "label": "公司余额", "value": f"¥{wallet_balance:,.2f}",
         "unit": "", "icon": "account_balance_wallet", "tone": "jade",
         "sub": f"今日收入 ¥{income_today:,.0f} · 本月收入 ¥{income_month:,.0f}",
         "href": "/admin/agent/wallet/1/change/"},
        {"key": "conv_7d", "label": "近7日对话", "value": conv_7d,
         "unit": "条", "icon": "forum", "tone": "lake",
         "sub": "AI 客服活跃度",
         "href": "/admin/agent/conversations/"},
    ]

    context = {
        "metrics": metrics,
        "recent_orders": recent_orders,
        "unread_list": unread_list,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        # 公司收入概览
        "income_series": income_series,
        "tx_max": tx_max,
        "income_composition": income_composition,
        "income_total": income_total,
        "status_dist": status_dist,
        "recent_txs": recent_txs,
        # 口径统一后的订单分组计数（供模板显示"口径说明"与售后待办）
        "returning_orders": returning_orders,
        "refunded_orders": refunded_orders,
        "paid_orders": _safe(lambda: Orders.objects.filter(
            status__in=[s.value for s in PAID_ORDER_STATUSES]).count()),
    }
    # 注入 Unfold 后台上下文（侧栏导航、配色变量、站点标识等），使看板继承统一后台框架
    context.update(admin.site.each_context(request))
    context["title"] = "运营看板"
    return TemplateResponse(request, "admin/dashboard.html", context)
