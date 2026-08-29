"""
agent/views_admin.py — 后台数据看板（运营概览）

通过 SimpleUI 首页 (SIMPLEUI_HOME_PAGE) 与左侧菜单进入，
聚合展示核心业务指标：今日订单、待发货、未读留言、未读定制、
缺货预警、近 7 日对话量，并附最近订单与未读列表，便于运营一屏掌握全局。
"""
from datetime import datetime, timedelta

from django.db.models import Sum, F
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.template.response import TemplateResponse

from agent.models import (
    Orders, ContactMessage, CustomRequest, Inventory, Conversations, Wallet, Transaction, TxType,
)


def _safe(fn, default=0):
    """任意指标查询失败（如数据库未连接）时返回默认值，避免看板 500。"""
    try:
        return fn()
    except Exception:
        return default


@staff_member_required
def admin_dashboard(request):
    """后台首页数据看板"""
    now = datetime.now()
    today = now.date()
    week_ago = now - timedelta(days=7)

    orders_today = _safe(lambda: Orders.objects.filter(created_at__date=today).count())
    pending_orders = _safe(lambda: Orders.objects.filter(status="未发货").count())
    total_orders = _safe(lambda: Orders.objects.count())
    revenue = _safe(
        lambda: float(Orders.objects.aggregate(s=Sum("total_price"))["s"] or 0)
    )
    unread_msgs = _safe(lambda: ContactMessage.objects.filter(is_read=False).count())
    unread_custom = _safe(lambda: CustomRequest.objects.filter(is_read=False).count())
    low_stock = _safe(
        lambda: Inventory.objects.filter(
            stock__isnull=False, alert_line__isnull=False,
            stock__gt=0, stock__lte=F("alert_line"),
        ).count()
    )
    out_stock = _safe(lambda: Inventory.objects.filter(stock__lte=0).count())
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

    recent_orders = []
    try:
        recent_orders = list(
            Orders.objects.all().order_by("-created_at")[:8].values(
                "order_no", "customer_name", "product_name",
                "quantity", "total_price", "status", "created_at",
            )
        )
    except Exception:
        pass

    unread_list = []
    try:
        unread_list = list(
            ContactMessage.objects.filter(is_read=False)
            .order_by("-created_at")[:6].values("name", "phone", "message", "created_at")
        )
    except Exception:
        pass

    metrics = [
        {"key": "orders_today", "label": "今日订单", "value": orders_today,
         "unit": "单", "icon": "shopping_cart", "tone": "jade",
         "sub": f"待发货 {pending_orders} 单"},
        {"key": "pending", "label": "待发货订单", "value": pending_orders,
         "unit": "单", "icon": "local_shipping", "tone": "fire",
         "sub": "需尽快跟进"},
        {"key": "revenue", "label": "累计销售额", "value": f"¥{revenue:,.0f}",
         "unit": "", "icon": "payments", "tone": "lake",
         "sub": f"共 {total_orders} 笔订单"},
        {"key": "unread_msg", "label": "未读留言", "value": unread_msgs,
         "unit": "条", "icon": "mark_email_unread", "tone": "fire",
         "sub": "客服待处理"},
        {"key": "unread_custom", "label": "未读定制", "value": unread_custom,
         "unit": "条", "icon": "checklist", "tone": "fire",
         "sub": "定制待跟进"},
        {"key": "low_stock", "label": "库存预警", "value": low_stock + out_stock,
         "unit": "项", "icon": "warning", "tone": "fire",
         "sub": f"缺货 {out_stock} · 低于预警 {low_stock}"},
        {"key": "wallet", "label": "公司余额", "value": f"¥{wallet_balance:,.2f}",
         "unit": "", "icon": "account_balance_wallet", "tone": "jade",
         "sub": f"本月收入 ¥{income_month:,.0f} · 支出 ¥{expense_month:,.0f}"},
        {"key": "conv_7d", "label": "近7日对话", "value": conv_7d,
         "unit": "条", "icon": "forum", "tone": "lake",
         "sub": "AI 客服活跃度"},
    ]

    context = {
        "metrics": metrics,
        "recent_orders": recent_orders,
        "unread_list": unread_list,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
    }
    # 注入 Unfold 后台上下文（侧栏导航、配色变量、站点标识等），使看板继承统一后台框架
    context.update(admin.site.each_context(request))
    context["title"] = "运营看板"
    return TemplateResponse(request, "admin/dashboard.html", context)
