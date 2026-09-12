"""后台自定义模板标签。

`admin/index.html` 覆盖页需要几个"待办计数"，但默认的 admin index 视图
不提供额外 context（改 context 得自定义 AdminSite，成本更高）。
这里用模板标签直接取数，失败一律降级为 0 —— 后台首页绝不能因为
某个业务表查询异常而 500。
"""
from django import template

register = template.Library()


def _safe(fn, default=0):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — 后台首页必须容错
        return default


@register.simple_tag
def zy_pending_counts():
    """返回后台首页快捷入口用的待办计数字典。

    键：pending_orders 待发货订单 / low_stock 库存预警 / unread 未读留言+定制
    """
    from django.db.models import F
    from agent.models import Orders, Inventory, ContactMessage, CustomRequest

    return {
        "pending_orders": _safe(lambda: Orders.objects.filter(status="未发货").count()),
        "low_stock": _safe(lambda: Inventory.objects.filter(
            stock__lte=F("alert_line")).count()),
        "unread": _safe(lambda: (
            ContactMessage.objects.filter(is_read=False).count()
            + CustomRequest.objects.filter(is_read=False).count()
        )),
    }
