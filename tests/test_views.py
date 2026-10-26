"""
agent/views.py 的 HTTP 业务路径测试。

这些测试故意不使用 Django TestCase：业务表是 managed=False，测试只验证视图的
输入校验、服务调用和响应契约，不应连接项目配置里的真实 MySQL。
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import django
from django.http import JsonResponse
from django.test import RequestFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import agent.handoff_presenter  # noqa: E402,F401
import agent.pricing  # noqa: E402,F401
import agent.purchase_guard  # noqa: E402,F401
import agent.reviews  # noqa: E402,F401  被 patch("agent.reviews.*") 按字符串路径解析，需先真正 import
from agent import auth_views, views  # noqa: E402
from agent.services import InsufficientStockError, OrderTransitionError  # noqa: E402


class FakeQuerySet(list):
    """只实现本文件中视图查询链需要的最小接口。"""

    def all(self):
        return FakeQuerySet(self)

    def order_by(self, *fields):
        return self

    def filter(self, **lookups):
        result = self
        for key, expected in lookups.items():
            if key.endswith("__in"):
                field = key[:-4]
                result = [item for item in result if getattr(item, field) in expected]
            else:
                result = [item for item in result if getattr(item, key) == expected]
        return FakeQuerySet(result)

    def first(self):
        return self[0] if self else None

    def exists(self):
        return bool(self)


class FakeManager(FakeQuerySet):
    def __init__(self, values=()):
        super().__init__(values)
        self.created = []

    def get(self, **lookups):
        matches = self.filter(**lookups)
        if matches:
            return matches[0]
        raise self.model.DoesNotExist()

    def create(self, **kwargs):
        obj = SimpleNamespace(**kwargs)
        self.created.append(obj)
        return obj


class FakeAtomic:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def json_body(response):
    return json.loads(response.content.decode("utf-8"))


def request_with_json(method, path, payload):
    factory = RequestFactory()
    method_l = method.lower()
    # GET 不能带 data：RequestFactory 会把 data 当查询串解析，JSON 字符串会被拆成单元素元组，
    # 抛 ValueError: not enough values to unpack（django/utils/http.py 的 QueryDict）。
    extra = {} if method_l == "get" else {"data": json.dumps(payload), "content_type": "application/json"}
    request = getattr(factory, method_l)(path, **extra)
    request.user = SimpleNamespace(
        id=7,
        is_authenticated=True,
        is_staff=False,
    )
    # 这些测试直接调用视图函数，不经过 Django 的 CSRF 中间件；
    # 使用 Django 测试约定关闭检查，专门测试业务逻辑而不是 CSRF 实现。
    request._dont_enforce_csrf_checks = True
    return request


def staff_request(method, path, payload=None):
    request = request_with_json(method, path, payload or {})
    request.user.is_staff = True
    return request


def test_csrf_boundary_distinguishes_valid_jwt_from_session():
    """JWT 走无状态认证；Session 写请求没有 CSRF Token 时必须被拒绝。"""
    @views.csrf_protect_if_session
    def endpoint(request):
        return JsonResponse({"ok": True})

    factory = RequestFactory()
    jwt_request = factory.post(
        "/api/test/",
        data="{}",
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer valid",
    )
    session_request = factory.post(
        "/api/test/",
        data="{}",
        content_type="application/json",
    )

    with patch.object(views, "_jwt_user", return_value=(SimpleNamespace(id=7), True)):
        assert endpoint(jwt_request).status_code == 200
    with patch.object(views, "_jwt_user", return_value=(None, False)):
        assert endpoint(session_request).status_code == 403


def test_register_and_login_success_without_database():
    """防止注册成功后 token 响应缺字段，或登录成功路径意外触碰真实用户表。"""
    user = SimpleNamespace(id=11, username="alice", email="alice@example.com")
    user_manager = MagicMock()
    user_manager.filter.return_value.exists.return_value = False
    user_manager.create_user.return_value = user
    fake_user_model = SimpleNamespace(objects=user_manager)
    refresh = SimpleNamespace(access_token="access-token")
    refresh.__str__ = lambda self: "refresh-token"

    with patch.object(auth_views, "User", fake_user_model), patch.object(
        auth_views.RefreshToken, "for_user", return_value=refresh
    ):
        register_response = auth_views.RegisterView().post(
            SimpleNamespace(
                data={
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "strong-password-1",
                    "confirm_password": "strong-password-1",
                }
            )
        )

        with patch.object(
            auth_views, "_authenticate_identifier", return_value=user
        ), patch.object(auth_views, "_clear_failures") as clear_failures:
            login_response = auth_views.LoginView().post(
                SimpleNamespace(
                    data={"username": "alice", "password": "strong-password-1"}
                )
            )

    assert register_response.status_code == 201
    assert register_response.data["user"]["username"] == "alice"
    assert register_response.data["access"] == "access-token"
    assert login_response.status_code == 200
    assert login_response.data["user"]["id"] == 11
    clear_failures.assert_called_once_with("alice")


def test_login_rejects_wrong_password():
    """防止错误密码被当作成功登录，并确认失败会进入登录失败计数。"""
    with patch.object(
        auth_views, "_authenticate_identifier", return_value=None
    ), patch.object(auth_views, "_record_failure") as record_failure:
        response = auth_views.LoginView().post(
            SimpleNamespace(data={"username": "alice", "password": "wrong"})
        )

    assert response.status_code == 401
    assert "detail" in response.data
    record_failure.assert_called_once_with("alice")


def test_product_list_returns_purchase_and_stock_fields():
    """防止商品列表丢失库存、可购买性等前端下单所需字段。"""
    product = SimpleNamespace(
        id=3,
        sku="SKU-3",
        name="测试鱼饵",
        category="饵料",
        spec="100g",
        target_fish="鲫鱼",
        description="测试商品",
        image="assets/images/test.webp",
        is_active=True,
    )
    inventory = SimpleNamespace(
        product_id=3,
        stock=8,
        reserved_stock=2,
        alert_line=3,
    )
    products_manager = FakeManager([product])
    products_manager.model = SimpleNamespace(DoesNotExist=LookupError)
    inventory_manager = FakeManager([inventory])
    coordination_manager = FakeManager([])
    fake_models = SimpleNamespace(
        Products=SimpleNamespace(objects=products_manager),
        Inventory=SimpleNamespace(objects=inventory_manager),
        ProductCoordination=SimpleNamespace(objects=coordination_manager),
    )
    request = RequestFactory().get("/api/products/")

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch.object(
        views, "_resolve_request_user", return_value=(None, False)
    ), patch("agent.reviews.rating_summary", return_value={}), patch(
        "agent.purchase_guard.evaluate",
        return_value={
            "available_stock": 6,
            "can_buy": True,
            "reason": "",
            "message": "",
            "customer_notice": "",
        },
    ), patch("agent.pricing.price_fields", return_value={"retail_price": "12.00"}):
        response = views.product_list(request)

    body = json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["products"][0]["sku"] == "SKU-3"
    assert body["products"][0]["stock"] == 8
    assert body["products"][0]["available_stock"] == 6
    assert body["products"][0]["can_buy"] is True


def test_product_detail_returns_complete_product_shape():
    """防止商品详情接口只返回名称而缺少 SKU、价格和库存展示结构。"""
    product = SimpleNamespace(id=3)
    products_manager = MagicMock()
    products_manager.get.return_value = product
    fake_models = SimpleNamespace(
        Products=SimpleNamespace(objects=products_manager),
        Inventory=SimpleNamespace(objects=MagicMock()),
    )
    request = staff_request("GET", "/api/products/3/")

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch(
        "agent.reviews.rating_summary", return_value={}), patch.object(
            views,
            "_serialize_product",
            return_value={
                "id": 3,
                "sku": "SKU-3",
                "name": "测试鱼饵",
                "stock": 8,
                "retail_price": "12.00",
            },
        ):
        response = views.product_detail_api(request, 3)

    body = json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["product"]["sku"] == "SKU-3"
    assert body["product"]["stock"] == 8


def test_create_order_reserves_inventory_and_returns_order_number():
    """防止下单成功响应与订单写入脱节，或成功下单没有预占库存。"""
    product = SimpleNamespace(
        id=3,
        name="测试鱼饵",
        sku="SKU-3",
        spec="100g",
        is_active=True,
    )
    user = SimpleNamespace(id=7, is_staff=False)
    orders_manager = FakeManager([])
    orders_manager.model = SimpleNamespace(DoesNotExist=LookupError)

    def reserve(order):
        order.inventory.reserved_stock += order.quantity

    def create_order(**kwargs):
        order = SimpleNamespace(**kwargs, inventory=SimpleNamespace(reserved_stock=0))
        orders_manager.created.append(order)
        return order

    orders_manager.create = create_order
    fake_models = SimpleNamespace(
        Products=SimpleNamespace(objects=MagicMock(get=MagicMock(return_value=product))),
        Orders=SimpleNamespace(objects=orders_manager),
    )
    request = request_with_json(
        "POST",
        "/api/orders/",
        {
            "customer_name": "张三",
            "phone": "13800138000",
            "address": "上海市测试路 1 号",
            "product_id": 3,
            "quantity": 2,
        },
    )

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch.object(
        views, "_resolve_request_user", return_value=(user, True)
    ), patch("agent.purchase_guard.check_buyable", return_value=(True, "", "")), patch(
        "agent.views.transaction.atomic", return_value=FakeAtomic()
    ), patch.object(views, "reserve_inventory", side_effect=reserve), patch(
        "agent.pricing.price_for", return_value=12.5
    ), patch("agent.pricing.customer_type_of", return_value="零售"), patch(
        "agent.pricing.is_wholesale", return_value=False
    ), patch("agent.pricing.wholesale_of", return_value=10):
        response = views.create_order_api(request)

    body = json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["order_no"].startswith("DD")
    assert orders_manager.created[0].quantity == 2
    assert orders_manager.created[0].inventory.reserved_stock == 2


def test_create_order_rejects_insufficient_inventory():
    """防止库存不足仍创建订单，避免形成无法履约的待发货单。"""
    product = SimpleNamespace(id=3, name="测试鱼饵", sku="SKU-3", is_active=True)
    orders_manager = FakeManager([])
    fake_models = SimpleNamespace(
        Products=SimpleNamespace(objects=MagicMock(get=MagicMock(return_value=product))),
        Orders=SimpleNamespace(objects=orders_manager),
    )
    request = request_with_json(
        "POST",
        "/api/orders/",
        {
            "customer_name": "张三",
            "phone": "13800138000",
            "address": "上海市测试路 1 号",
            "product_id": 3,
            "quantity": 5,
        },
    )

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch(
        "agent.purchase_guard.check_buyable",
        return_value=(False, "insufficient_stock", "库存不足"),
    ):
        response = views.create_order_api(request)

    body = json_body(response)
    assert response.status_code == 400
    assert body["code"] == "insufficient_stock"
    assert orders_manager.created == []


def test_ship_order_rejects_illegal_transition():
    """防止非法订单状态跳转被当成服务器故障或误写入目标状态。"""
    order = SimpleNamespace(order_no="DD-1")
    orders_manager = MagicMock()
    orders_manager.get.return_value = order
    fake_models = SimpleNamespace(Orders=SimpleNamespace(objects=orders_manager))
    transition_error = OrderTransitionError("状态不允许跳转", code="invalid_transition")
    request = staff_request(
        "POST",
        "/api/orders/DD-1/ship/",
        {"ship_company": "测试物流", "tracking_no": "T-1"},
    )

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch.object(
        views, "transition_order", side_effect=transition_error
    ):
        response = views.ship_order_api(request, "DD-1")

    body = json_body(response)
    assert response.status_code == 400
    assert body["success"] is False
    assert body["code"] == "invalid_transition"


def test_handoff_creates_ticket_and_returns_ticket_number():
    """防止转人工只返回提示而没有工单落库，确保前端拿到可追踪单号。"""
    ticket = SimpleNamespace(ticket_no="T20261026-0001", reason="user_request")
    status = SimpleNamespace(OPEN="open", ASSIGNED="assigned")
    reason = SimpleNamespace(choices=[("user_request", "用户请求")])
    priority = SimpleNamespace(NORMAL="normal", HIGH="high")
    source = SimpleNamespace(USER="user", AGENT="agent")
    support_manager = MagicMock()
    support_manager.filter.return_value.order_by.return_value.first.return_value = None
    support_manager.create.return_value = ticket
    fake_models = SimpleNamespace(
        SupportTicket=SimpleNamespace(
            objects=support_manager,
            Status=status,
            Reason=reason,
            Priority=priority,
            Source=source,
        )
    )
    request = request_with_json(
        "POST",
        "/api/agent/handoff/",
        {
            "session_id": "session-1",
            "reason": "user_request",
            "detail": "需要人工确认",
            "phone": "13800138000",
        },
    )
    tools = __import__("agent.tools", fromlist=["handoff_to_human"])

    with patch.dict(sys.modules, {"agent.models": fake_models}), patch.object(
        views, "_resolve_request_user", return_value=(request.user, True)
    ), patch.object(views, "_ticket_payload", return_value={"ticket_no": ticket.ticket_no}), patch.object(
        tools, "_ticket_transcript", return_value=[]
    ), patch.object(tools, "get_referenced_sources", return_value=[]), patch.object(
        tools, "get_referenced_orders", return_value=[]
    ), patch.object(tools, "get_referenced_products", return_value=[]), patch(
        "agent.handoff_presenter.ticket_line", return_value="已创建"
    ):
        response = views.handoff(request)

    body = json_body(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["reused"] is False
    assert body["ticket"]["ticket_no"] == "T20261026-0001"
    support_manager.create.assert_called_once()
    ticket_fields = support_manager.create.call_args.kwargs
    assert ticket_fields["session_id"] == "session-1"
    assert ticket_fields["user_id"] == request.user.id
    assert ticket_fields["contact_phone"] == "13800138000"
    assert ticket_fields["detail"] == "需要人工确认"
    assert ticket_fields["reason"] == "user_request"
    assert ticket_fields["status"] == "open"
