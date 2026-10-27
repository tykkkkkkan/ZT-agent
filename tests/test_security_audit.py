"""第一轮安全审计的回归测试：只使用替身，不连接真实业务库。"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
from django.test import RequestFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agent import services, tools, views_cashier  # noqa: E402
from agent.models import OrderStatus, PayTicketStatus  # noqa: E402


class FakeAtomic:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_agent_order_lookup_rejects_order_from_another_user():
    """聊天里的订单号查询必须复用当前用户的订单归属规则。"""
    user = SimpleNamespace(id=7, is_authenticated=True)
    manager = MagicMock()
    manager.filter.return_value.first.return_value = None
    fake_q = object()

    with patch.object(tools, "get_current_user", return_value=user), patch(
        "agent.me_helpers.my_orders_q", return_value=fake_q
    ), patch.object(tools.Orders, "objects", manager), patch.object(
        tools, "_remember_order"
    ) as remember:
        result = tools.get_order_status("DD-FOREIGN")

    assert "订单不存在或不属于当前账号" in result
    manager.filter.assert_called_once_with(fake_q, order_no="DD-FOREIGN")
    remember.assert_not_called()


def test_agent_order_lookup_requires_authenticated_user():
    """未登录工具上下文不得通过订单号查询订单。"""
    manager = MagicMock()

    with patch.object(tools, "get_current_user", return_value=None), patch.object(
        tools.Orders, "objects", manager
    ):
        result = tools.get_order_status("DD-1")

    assert "请先登录" in result
    manager.filter.assert_not_called()


def test_transition_rechecks_status_after_acquiring_order_lock():
    """并发请求中，第二个请求拿到锁后不能继续处理已发货订单。"""
    stale = SimpleNamespace(pk=1, status=OrderStatus.PENDING, order_no="DD-1")
    locked = SimpleNamespace(pk=1, status=OrderStatus.SHIPPED, order_no="DD-1")
    manager = MagicMock()
    manager.select_for_update.return_value.get.return_value = locked

    with patch.object(services.transaction, "atomic", return_value=FakeAtomic()), patch.object(
        services.Orders, "objects", manager
    ):
        try:
            services.transition_order(
                stale,
                to_status=OrderStatus.SHIPPED,
                ship_company="测试物流",
                tracking_no="T-1",
            )
        except services.OrderTransitionError as exc:
            assert exc.code == "invalid_transition"
        else:
            raise AssertionError("已发货订单不应再次进入发货分支")


def test_expired_payment_ticket_does_not_return_order_summary():
    """过期二维码只返回失效提示，不再泄露订单摘要。"""
    ticket = SimpleNamespace(
        status=PayTicketStatus.PENDING,
        is_expired=True,
        order_no="DD-EXPIRED",
        save=MagicMock(),
    )
    request = RequestFactory().get("/api/pay/ticket/expired/")

    with patch.object(views_cashier, "load_ticket", return_value=ticket):
        response = views_cashier.ticket_info_api(request, "expired")

    body = json.loads(response.content.decode("utf-8"))
    assert response.status_code == 400
    assert body["code"] == "expired"
    assert "order" not in body
    ticket.save.assert_called_once_with(update_fields=["status"])
