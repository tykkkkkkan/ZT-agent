"""
agent/me_views.py — 个人中心（C 端「我的」）接口

设计要点
--------
1. **一律走 JWT 鉴权**：DRF + `IsAuthenticated`，与 /api/auth/* 同一套签发体系。
2. **每个接口都做归属校验**：单笔操作前先确认该订单属于当前用户
   （见 me_helpers.my_orders_q 的归属规则），否则返回 404 而不是 403 ——
   不泄露"这个订单号确实存在，只是不属于你"。
3. **状态变更全部走 services 状态机**，本层只做参数校验与归属判断，
   绝不直接改 order.status（否则库存/记账联动会被绕过）。
4. **可执行操作由后端下发**（_serialize_order 的 actions 字段），前端不重写规则。

接口清单
--------
    GET    /api/me/profile/                        个人资料 + 订单统计
    PATCH  /api/me/profile/                        更新绑定手机号 / 收货人姓名
    POST   /api/me/password/                       修改密码（可选拉黑当前 refresh）
    GET    /api/me/orders/?status=&page=           我的订单（分页）
    POST   /api/me/orders/<order_no>/confirm/      确认收货 → 已完成
    POST   /api/me/orders/<order_no>/return/       申请退货 → 退货申请中
    POST   /api/me/orders/<order_no>/cancel/       取消订单（仅未发货）
    POST   /api/me/orders/<order_no>/withdraw-return/  撤销退货申请
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q, Sum

from rest_framework import views
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from agent.me_helpers import get_profile, my_orders_q, phone_taken_by_other
from agent.models import OrderStatus, Orders
from agent.services import (
    OrderTransitionError, transition_order, withdraw_return_request,
)
from agent.views import _serialize_order

PAGE_SIZE = 10

# 前端筛选页签 → 订单状态集合（"全部" 由空值表示）
STATUS_GROUPS = {
    'pending':   [OrderStatus.PENDING],
    'shipped':   [OrderStatus.SHIPPED],
    'completed': [OrderStatus.COMPLETED],
    'returning': [OrderStatus.RETURNING],
    'closed':    [OrderStatus.CANCELLED, OrderStatus.RETURNED],
}
ALL_STATUSES = [c.value for c in OrderStatus]


def _err(message, code=400):
    return Response({"success": False, "message": message}, status=code)


def _my_order_or_none(user, order_no):
    """按归属规则取订单；不属于我 / 不存在 → None（统一 404，不泄露存在性）。"""
    return Orders.objects.filter(my_orders_q(user), order_no=order_no).select_related('product').first()


def _user_payload(user, profile):
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email or "",
        "date_joined": user.date_joined.strftime("%Y-%m-%d") if user.date_joined else "",
        "last_login": user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "",
        "is_staff": bool(user.is_staff),
        "phone": profile.phone or "",
        "nickname": profile.nickname or "",
    }


def _order_stats(user):
    """订单统计（一次查询聚合，个人中心顶部概览卡用）。"""
    agg = Orders.objects.filter(my_orders_q(user)).aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status=OrderStatus.PENDING)),
        shipped=Count('id', filter=Q(status=OrderStatus.SHIPPED)),
        completed=Count('id', filter=Q(status=OrderStatus.COMPLETED)),
        returning=Count('id', filter=Q(status=OrderStatus.RETURNING)),
        closed=Count('id', filter=Q(status__in=[OrderStatus.CANCELLED, OrderStatus.RETURNED])),
        # 只统计真正成交的金额（已完成 + 已发货在途），取消/退货不计入
        total_amount=Sum('total_price', filter=Q(status__in=[
            OrderStatus.SHIPPED, OrderStatus.COMPLETED, OrderStatus.RETURNING,
        ])),
    )
    agg['total_amount'] = float(agg['total_amount'] or 0)
    return agg


class MeProfileView(views.APIView):
    """/api/me/profile/ —— 查看 / 更新个人资料"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = get_profile(request.user)
        return Response({
            "success": True,
            "user": _user_payload(request.user, profile),
            "stats": _order_stats(request.user),
        })

    def patch(self, request):
        """更新绑定手机号 / 收货人姓名。

        手机号必须唯一：一个手机号只能归一个账号，否则会出现两个用户都能看到
        同一批历史订单的情况。

        ⚠️ 手机号直接决定「我的订单」的可见范围（归属规则的第二条就是
        「phone == 我绑定的号 且 订单未归属任何账号」）。改造前接口对这一点
        完全沉默，用户换了手机号就"订单凭空消失"，看起来像数据不同步。
        现在返回体带 `visible_orders` 与 `visible_delta`，换绑变少时给出明确提示。
        """
        profile = get_profile(request.user)
        data = request.data or {}
        changed = []
        visible_before = Orders.objects.filter(my_orders_q(request.user)).count()

        if 'phone' in data:
            phone = (data.get('phone') or '').strip()
            if phone and not (phone.isdigit() and 7 <= len(phone) <= 20):
                return _err("手机号格式有误：请输入 7~20 位数字")
            if phone and phone_taken_by_other(phone, request.user):
                return _err("该手机号已被其它账号绑定，请更换或联系客服")
            if profile.phone != phone:
                profile.phone = phone
                changed.append('phone')

        if 'nickname' in data:
            nickname = (data.get('nickname') or '').strip()
            if len(nickname) > 50:
                return _err("收货人姓名最长 50 个字符")
            if profile.nickname != nickname:
                profile.nickname = nickname
                changed.append('nickname')

        if changed:
            profile.save(update_fields=changed + ['updated_at'])

        visible_after = Orders.objects.filter(my_orders_q(request.user)).count()
        delta = visible_after - visible_before

        if 'phone' in changed:
            message = f"手机号已更新，当前可查看 {visible_after} 笔订单"
            if delta < 0:
                message += f"（比之前少 {abs(delta)} 笔 —— 原手机号名下未认领的订单已不再展示）"
            elif delta > 0:
                message += f"（新认领了 {delta} 笔历史订单）"
        else:
            message = "资料已更新" if changed else "没有需要更新的内容"

        return Response({
            "success": True,
            "message": message,
            "user": _user_payload(request.user, profile),
            "visible_orders": visible_after,
            "visible_delta": delta,
            "changed": changed,
        })


class MePasswordView(views.APIView):
    """/api/me/password/ —— 修改密码

    安全说明：Django 改密码后，**已签发的 JWT 在过期前仍然有效**。
    所以这里支持前端把当前 refresh token 一起提交，改完密码即拉黑它，
    强制重新登录 —— 否则"改密码把别人踢下线"这个预期就不成立。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        old = request.data.get('old_password') or ''
        new = request.data.get('new_password') or ''
        confirm = request.data.get('confirm_password') or ''

        if not user.check_password(old):
            return _err("当前密码不正确", code=400)
        if new != confirm:
            return _err("两次输入的新密码不一致")
        if not new:
            return _err("请输入新密码")
        if old == new:
            return _err("新密码不能与当前密码相同")
        try:
            validate_password(new, user)
        except DjangoValidationError as e:
            return _err("；".join(e.messages))

        user.set_password(new)
        user.save(update_fields=['password'])

        # 拉黑当前 refresh：让本次会话失效，强制用新密码重新登录
        refresh = request.data.get('refresh') or ''
        revoked = False
        if refresh:
            try:
                from rest_framework_simplejwt.tokens import RefreshToken
                RefreshToken(refresh).blacklist()
                revoked = True
            except Exception:  # noqa: BLE001 — token 已失效/非法也视为处理完成
                pass

        return Response({
            "success": True,
            "message": "密码已修改，请用新密码重新登录" if revoked else "密码已修改",
            "revoked": revoked,
        })


class MeOrderListView(views.APIView):
    """/api/me/orders/ —— 我的订单（分页 + 状态筛选）"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_key = (request.GET.get('status') or '').strip()
        qs = Orders.objects.filter(my_orders_q(request.user)).select_related('product')

        if status_key in STATUS_GROUPS:
            qs = qs.filter(status__in=[s.value for s in STATUS_GROUPS[status_key]])
        elif status_key and status_key in ALL_STATUSES:
            qs = qs.filter(status=status_key)
        elif status_key:
            return _err(f"未知的筛选条件：{status_key}")

        qs = qs.order_by('-created_at', '-id')
        try:
            page = max(1, int(request.GET.get('page') or 1))
        except (TypeError, ValueError):
            page = 1

        total = qs.count()
        start = (page - 1) * PAGE_SIZE
        rows = list(qs[start:start + PAGE_SIZE])

        return Response({
            "success": True,
            "count": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "has_more": start + PAGE_SIZE < total,
            "stats": _order_stats(request.user),
            "orders": [_serialize_order(o, with_actions=True) for o in rows],
        })


class MeOrderActionView(views.APIView):
    """/api/me/orders/<order_no>/<action>/ —— 用户侧单笔订单操作

    通过 URL 配置传入 action，取值：confirm / return / cancel / withdraw-return。
    所有分支都先校验归属，再交给 services 状态机执行（库存与记账联动不被绕过）。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, order_no, action):
        order = _my_order_or_none(request.user, order_no)
        if order is None:
            return _err("订单不存在或不属于当前账号", code=404)

        try:
            if action == 'confirm':
                result = transition_order(order, to_status=OrderStatus.COMPLETED)
            elif action == 'cancel':
                reason = (request.data.get('reason') or '用户取消').strip()[:200]
                result = transition_order(order, to_status=OrderStatus.CANCELLED, cancel_reason=reason)
            elif action == 'return':
                reason = (request.data.get('reason') or '').strip()
                if len(reason) < 2:
                    return _err("请填写退货原因（至少 2 个字），便于我们为你处理")
                result = transition_order(
                    order, to_status=OrderStatus.RETURNING,
                    return_reason=reason, operator=request.user,
                )
            elif action == 'withdraw-return':
                result = withdraw_return_request(order)
            else:
                return _err(f"不支持的操作：{action}", code=404)
        except OrderTransitionError as e:
            return _err(e.message, code=400)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("个人中心订单操作失败：%s %s", order_no, action)
            return _err("操作失败，请稍后重试", code=500)

        return Response({
            "success": True,
            "message": result.message,
            "order": _serialize_order(result.order, with_actions=True),
        })
