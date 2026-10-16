"""/api/pay/* —— 模拟收银台的 HTTP 面。

四个接口，两两一组：

    PC 端（要登录，JWT 优先于 session，与 /api/me/* 同一套身份解析）
      POST /api/pay/<order_no>/ticket/    开票：拿一次性 token + 二维码 + 可扫地址
      GET  /api/pay/<order_no>/status/    轮询：订单付掉了没（手机确认后 PC 自动刷新）

    手机端（**不需要登录** —— 二维码本身就是授权，真实扫码支付同理）
      GET  /api/pay/ticket/<token>/       看订单摘要（脱敏）与凭据状态
      POST /api/pay/confirm/<token>/      确认付款 → 核销凭据 → 订单落「已支付」

为什么手机端可以不登录：能扫到这张码，说明用户正对着自己的屏幕；而 token 是
256 位随机、一次性、5 分钟失效，并且只出现在该账号自己发起的收银台上。
换来的是**不需要在手机上再登录一次**的顺滑体验（真实扫码支付也是如此）。
如果哪天后端要给 token 加更多权限，这条"不登录"的前提就必须重新评估。
"""

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from agent.cashier import (
    CashierError,
    create_ticket,
    expire_stale_tickets,
    load_ticket,
    order_pay_state,
    order_summary,
    pay_url,
    qr_svg,
    redeem_ticket,
    ticket_payload,
)
from agent.me_helpers import my_orders_q
from agent.models import Orders, PayTicket
from agent.views import _resolve_request_user, _server_error

# 订单状态把"能不能付款"这件事说清楚了 → 用 HTTP 语义表达，前端好分支：
#   404 找不到订单/凭据；409 订单状态与请求冲突（已付/已退/已取消/历史单）；400 其它
_STATE_STATUS = {
    "not_found": 404,
    "missing": 404,
    "paid": 409,
    "refunded": 409,
    "settled": 409,
    "closed": 409,
    "abnormal": 409,
}


def _error(message, code="invalid", *, status=None):
    return JsonResponse(
        {"success": False, "message": message, "code": code},
        status=status or _STATE_STATUS.get(code, 400),
    )


def _find_order(request, order_no, me):
    """按归属规则取订单：只能在**自己的**订单上开收银台。

    不存在与无权统一 404，不泄露"这个订单号确实存在，只是不属于你"。
    管理员（is_staff）可代开，便于后台协助排查。
    """
    qs = Orders.objects.all()
    if not getattr(me, "is_staff", False):
        qs = qs.filter(my_orders_q(me))
    return qs.filter(order_no=order_no).first()


# ════════════════════════════════════════════════════════════════
# PC 端：开票
# ════════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
def create_ticket_api(request, order_no):
    """POST /api/pay/<order_no>/ticket/ —— 开一张收银凭据（在有效期内复用）。"""
    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return _error("请先登录后再付款", "login_required", status=401)

    try:
        order = _find_order(request, order_no, me)
        if order is None:
            return _error("订单不存在，或不属于当前账号", "not_found", status=404)

        ticket = create_ticket(order, me)
        url = pay_url(request, ticket)
        return JsonResponse({
            "success": True,
            "message": "已生成支付二维码，请用手机扫码完成付款",
            "ticket": ticket_payload(ticket),
            "pay_url": url,
            "qr_svg": qr_svg(url),
            "amount": str(ticket.amount),
            "order": order_summary(order),
        })
    except CashierError as e:
        return _error(e.message, e.code)
    except Exception as e:  # noqa: BLE001
        return _server_error(e)


@csrf_exempt
@require_GET
def ticket_status_api(request, order_no):
    """GET /api/pay/<order_no>/status/ —— 收银台轮询用。

    PC 端每几秒问一次：手机那边确认付款后，这里要能自己变过来
    （不用用户手动刷新）。副作用是顺手把过期票标掉 —— 惰性清理的落点之一。
    """
    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return _error("请先登录后再查看支付状态", "login_required", status=401)

    try:
        order = _find_order(request, order_no, me)
        if order is None:
            return _error("订单不存在，或不属于当前账号", "not_found", status=404)

        expire_stale_tickets(order.order_no)
        ticket = (
            PayTicket.objects.filter(order_no=order.order_no)
            .order_by("-id").first()
        )
        state = order_pay_state(order)
        return JsonResponse({
            "success": True,
            "paid": state == "paid",
            "pay_state": state,
            "message": "已支付" if state == "paid" else "",
            "ticket": ticket_payload(ticket) if ticket else None,
            "order": order_summary(order),
        })
    except Exception as e:  # noqa: BLE001
        return _server_error(e)


# ════════════════════════════════════════════════════════════════
# 手机端：确认页（不需要登录）
# ════════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
def ticket_info_api(request, token):
    """GET /api/pay/ticket/<token>/ —— 手机确认页的订单摘要（脱敏）。"""
    try:
        ticket = load_ticket(token)
        order = Orders.objects.filter(order_no=ticket.order_no).first()
        return JsonResponse({
            "success": True,
            "ticket": ticket_payload(ticket),
            "order": order_summary(order, mask=True),
        })
    except CashierError as e:
        return _error(e.message, e.code)
    except Exception as e:  # noqa: BLE001
        return _server_error(e)


@csrf_exempt
@require_POST
def confirm_ticket_api(request, token):
    """POST /api/pay/confirm/<token>/ —— 确认付款（核销凭据）。

    不要求登录：token 即授权。若这次请求恰好带着有效身份（同一浏览器登录着），
    会把「核销账号」记下来，方便事后追溯"是谁在手机上点的确认"。
    """
    try:
        by_user, _ok = _resolve_request_user(request)
        result = redeem_ticket(token, by_user=by_user)
        ticket = result.get("ticket")
        order = result.get("order")
        return JsonResponse({
            "success": bool(result["ok"]),
            "code": result["code"],
            "message": result["message"],
            "already": result["already"],
            "ticket": ticket_payload(ticket) if ticket else None,
            "order": order_summary(order, mask=True) if order else {},
        }, status=200 if result["ok"] else _STATE_STATUS.get(result["code"], 400))
    except CashierError as e:
        return _error(e.message, e.code)
    except Exception as e:  # noqa: BLE001
        return _server_error(e)
