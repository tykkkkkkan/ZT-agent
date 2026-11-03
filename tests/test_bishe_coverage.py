"""
任务书功能覆盖的新增能力测试（不连真实数据库）。

覆盖 2026-11 补齐的四块：
  1. 用饵量测算（任务书三·3「买多少够用」）—— 两套口径 + 包数换算
  2. 引导下单工具与「去下单」卡片（任务书四·1）
  3. 备货中 / 待付款 状态文案（任务书五·1、六·1）
  4. 未登录凭手机号查订单（任务书五·2）与发货后可评价（任务书六·4）

与 tests/test_views.py 同一套写法：业务表是 managed=False，
所以只 mock 查询层，不建库。
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import django
from django.test import RequestFactory

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from agent import feeding, reviews, tools, views  # noqa: E402
from agent.models import OrderStatus, PaymentStatus  # noqa: E402
from agent.order_presenter import status_label  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 1. 用饵量测算
# ══════════════════════════════════════════════════════════════
def test_package_grams_parses_common_specs():
    assert feeding.package_grams("200g/包，1箱=10包") == 200
    assert feeding.package_grams("5kg/袋") == 5000
    assert feeding.package_grams("150克/包") == 150
    # 规格写得不规范时按默认值兜底，不能让整个测算失败
    assert feeding.package_grams("") == feeding.DEFAULT_PACKAGE_GRAMS
    assert feeding.package_grams("标准装") == feeding.DEFAULT_PACKAGE_GRAMS


def test_stock_estimate_follows_yield_times_fcr():
    """养殖口径：总需饵 = 亩产 × 面积 × 饵料系数。"""
    r = feeding.estimate_stock("鲤鱼", 5, package_grams_override=200)
    assert r["mode"] == "养殖"
    assert r["yield_kg"] == 5000                      # 1000 kg/亩 × 5 亩
    assert r["total_kg"] == 9000                      # × FCR 1.8
    assert r["bags"] == 45000                         # 9000 kg ÷ 200 g
    assert r["boxes"] == 4500                         # 10 包/箱
    assert round(r["total_ton"], 2) == 9.0


def test_stock_estimate_marks_assumed_area():
    r = feeding.estimate_stock("鲤鱼", 0)
    assert r["area_mu"] == 1.0
    assert r["area_assumed"] is True


def test_angling_estimate_uses_trips_and_people():
    r = feeding.estimate_angling("鲤鱼", trips=3, people=2)
    assert r["mode"] == "作钓"
    assert r["bags"] == 3 * 2 * feeding.ANGLE_PACKS_TYPICAL
    assert r["bag_range"] == (6, 18)


def test_recommend_dispatches_by_arguments():
    # 给了面积 → 养殖口径
    assert feeding.recommend("鲤鱼", area_mu=2)["mode"] == "养殖"
    # 只给次数 → 作钓口径
    assert feeding.recommend("鲤鱼", trips=2)["mode"] == "作钓"
    # 什么都没给 → 按 1 次作钓（不能按 1 亩养殖算，会得出几千包）
    r = feeding.recommend("鲤鱼")
    assert r["mode"] == "作钓"
    assert r["bags"] == feeding.ANGLE_PACKS_TYPICAL


def test_explain_states_the_formula():
    """回答里必须带上计算口径，否则答辩问「这个数怎么来的」答不上。"""
    text = feeding.explain(feeding.estimate_stock("鲤鱼", 5))
    assert "饵料系数" in text
    assert "亩产" in text
    assert "吨" in text


def test_lookup_profile_falls_back_to_default():
    prof, key = feeding.lookup_profile("四大家鱼混养")
    assert key == ""
    assert prof is feeding.DEFAULT_PROFILE


def test_tool_recommend_quantity_is_registered_and_callable():
    assert "recommend_quantity" in tools.TOOL_REGISTRY
    schema_names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert "recommend_quantity" in schema_names
    # 无商品参数时不查库，纯计算
    out = tools.call_tool("recommend_quantity", fish="鲤鱼", trips=2)
    assert "包" in out


# ══════════════════════════════════════════════════════════════
# 2. 引导下单（任务书四·1）
# ══════════════════════════════════════════════════════════════
def test_guide_to_order_registers_entry_without_product():
    tools.reset_pending_order_entry()
    text = tools.guide_to_order()
    entry = tools.get_pending_order_entry()
    assert entry is not None
    assert entry["url"] == "order.html"
    assert "下单" in text
    # 数量/地址/交期由客户自己填，AI 不代客下单
    assert "收货地址" in text


def test_guide_to_order_entry_is_registered_once_per_turn():
    tools.reset_pending_order_entry()
    tools.guide_to_order()
    first = tools.get_pending_order_entry()
    tools.guide_to_order(qty=5)
    assert tools.get_pending_order_entry() is first


def test_guide_to_order_schema_registered():
    assert "guide_to_order" in tools.TOOL_REGISTRY
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert "guide_to_order" in names
    # registry 与 schema 必须一一对应，否则模型能调到不存在的工具
    assert names == set(tools.TOOL_REGISTRY)


# ══════════════════════════════════════════════════════════════
# 3. 状态文案（任务书五·1「备货」、六·1「查看备货进度」）
# ══════════════════════════════════════════════════════════════
def _order(status, payment=""):
    return SimpleNamespace(status=status, payment_status=payment)


def test_status_label_paid_not_shipped_is_packing():
    assert status_label(_order(OrderStatus.PENDING.value, PaymentStatus.PAID.value)) == "备货中"


def test_status_label_unpaid_is_awaiting_payment():
    assert status_label(_order(OrderStatus.PENDING.value, PaymentStatus.UNPAID.value)) == "待付款"


def test_status_label_legacy_order_is_pending_shipment():
    assert status_label(_order(OrderStatus.PENDING.value, "")) == "待发货"


def test_status_label_others_unchanged():
    assert status_label(_order(OrderStatus.SHIPPED.value)) == "已发货"
    assert status_label(_order(OrderStatus.COMPLETED.value)) == "已完成"


# ══════════════════════════════════════════════════════════════
# 4. 发货后可评价（任务书六·4「收到货后评价」）
# ══════════════════════════════════════════════════════════════
@patch("agent.reviews.has_review", return_value=False)
def test_review_allowed_after_shipping(_has_review):
    order = SimpleNamespace(status=OrderStatus.SHIPPED.value, product_id=1)
    assert reviews.reviewable_reason(order) == ""
    assert reviews.can_review(order) is True


def test_review_blocked_before_shipping():
    order = SimpleNamespace(status=OrderStatus.PENDING.value, product_id=1)
    assert "已发货" in reviews.reviewable_reason(order)


# ══════════════════════════════════════════════════════════════
# 5. 未登录凭手机号查订单（任务书五·2）
# ══════════════════════════════════════════════════════════════
class _QS:
    """只实现 `_guest_order_query` 用到的最小查询链。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def select_related(self, *a, **k):
        return self

    def filter(self, **lookups):
        rows = self.rows
        for key, value in lookups.items():
            if key == "phone":
                rows = [r for r in rows if r.phone == value]
            elif key == "order_no__iexact":
                rows = [r for r in rows if r.order_no.lower() == str(value).lower()]
            elif key == "order_no__iendswith":
                rows = [r for r in rows if r.order_no.lower().endswith(str(value).lower())]
        return _QS(rows)

    def first(self):
        return self.rows[0] if self.rows else None


def _guest_request(body):
    return RequestFactory().post("/api/orders/query/", data=json.dumps(body),
                                 content_type="application/json")


def _payload(resp):
    return json.loads(resp.content.decode())


def test_guest_query_requires_phone():
    resp = views._guest_order_query(_guest_request({}), "", "DD001")
    assert resp.status_code == 400
    assert _payload(resp)["code"] == "PHONE_REQUIRED"


def test_guest_query_requires_order_no():
    resp = views._guest_order_query(_guest_request({}), "13800138000", "")
    assert resp.status_code == 400
    assert _payload(resp)["code"] == "ORDER_NO_REQUIRED"


def test_guest_query_matches_by_tail_four():
    order = SimpleNamespace(phone="13800138000", order_no="DD202611030001")
    fake = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: _QS([order]).filter(**kw)))
    with patch("agent.models.Orders", fake), \
         patch("agent.views._serialize_order", return_value={"order_no": order.order_no}):
        resp = views._guest_order_query(_guest_request({}), "13800138000", "0001")
    assert resp.status_code == 200
    data = _payload(resp)
    assert data["success"] is True
    assert data["guest"] is True
    assert data["count"] == 1


def test_guest_query_rejects_wrong_order_no():
    order = SimpleNamespace(phone="13800138000", order_no="DD202611030001")
    fake = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: _QS([order]).filter(**kw)))
    with patch("agent.models.Orders", fake):
        resp = views._guest_order_query(_guest_request({}), "13800138000", "9999")
    assert resp.status_code == 404
    assert _payload(resp)["code"] == "NOT_FOUND"


def test_guest_query_hides_address_and_actions():
    order = SimpleNamespace(phone="13800138000", order_no="DD202611030001")
    fake = SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: _QS([order]).filter(**kw)))
    with patch("agent.models.Orders", fake), \
         patch("agent.views._serialize_order",
               return_value={"order_no": order.order_no, "address": "某小区 1 号", "actions": ["pay"]}):
        resp = views._guest_order_query(_guest_request({}), "13800138000", "DD202611030001")
    data = _payload(resp)
    assert data["order"]["address"] == ""
    assert data["order"]["actions"] == []
