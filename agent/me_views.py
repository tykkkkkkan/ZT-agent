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
    POST   /api/me/orders/<order_no>/pay/          模拟支付（仅未发货 + 未支付）
                                                   ⚠️ C 端已改用收银台，此接口仅作状态机直达入口
    POST   /api/me/orders/<order_no>/review/       评价已完成订单（1–5 星，一单一评）
    POST   /api/me/orders/<order_no>/withdraw-return/  撤销退货申请
    GET    /api/me/addresses/                      收货地址簿（默认在前）
    POST   /api/me/addresses/                      新增收货地址
    PATCH  /api/me/addresses/<id>/                 编辑收货地址
    DELETE /api/me/addresses/<id>/                 删除收货地址
    POST   /api/me/addresses/<id>/default/         设为默认地址
    GET    /api/me/wallet/                         我的余额 + 最近流水
    POST   /api/me/wallet/recharge/                模拟充值入账（真实系统须由支付回调触发）
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q, Sum

from rest_framework import views
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from agent.me_helpers import get_profile, my_orders_q, phone_taken_by_other
from agent.models import OrderStatus, Orders, UserProfile
from agent import pricing
from agent.addresses import (
    MAX_PER_USER,
    AddressError,
    create_address,
    default_address,
    delete_address,
    list_addresses,
    serialize_address,
    set_default,
    update_address,
)
from agent.reviews import ReviewError, create_review, serialize_review
from agent.services import (
    OrderTransitionError, mark_order_paid, transition_order, withdraw_return_request,
)
from agent.user_wallet import (
    MAX_TOPUP,
    UserWalletError,
    list_transactions,
    recharge,
    serialize_transaction,
    summarize,
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
    return Orders.objects.filter(my_orders_q(user), order_no=order_no).select_related('product', 'review').first()


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
        # 任务书二·5：身份差异化价格。身份由 pricing 判定（不在视图里自己读字段），
        # 保证个人中心、产品页、下单、AI 报价看到的是同一个身份。
        "customer_type": pricing.customer_type_of(user),
        "customer_type_label": pricing.customer_type_label(user),
        "customer_type_source": profile.customer_type_source or "",
        # 批发起订量（前端不硬编码"10 包起"，统一读接口下发）
        "wholesale_min_qty": pricing.WHOLESALE_MIN_QTY,
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
        # 顺带下发默认收货地址：下单页要用它预填（避免用户每次重填）。
        # 放在 user 之外而不是塞进 _user_payload —— 那是"账号资料"，
        # 地址簿是另一份可变数据，混在一起以后不好加第二第三个地址字段。
        addr = default_address(request.user)
        return Response({
            "success": True,
            "user": _user_payload(request.user, profile),
            "stats": _order_stats(request.user),
            "default_address": serialize_address(addr) if addr else None,
            "address_count": len(list_addresses(request.user)),
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

        # 任务书二·5：切换客户类型 → 立刻改变该账号能看到/成交的价格
        if 'customer_type' in data:
            ct = (data.get('customer_type') or '').strip()
            if ct not in set(UserProfile.CustomerType.values):
                return _err("客户类型只能是 individual（个体钓友）或 wholesale（经销商）")
            if profile.customer_type != ct:
                profile.customer_type = ct
                profile.customer_type_source = UserProfile.TypeSource.SELF
                changed.extend(['customer_type', 'customer_type_source'])

        if changed:
            profile.save(update_fields=changed + ['updated_at'])

        visible_after = Orders.objects.filter(my_orders_q(request.user)).count()
        delta = visible_after - visible_before

        if 'customer_type' in changed:
            message = (f"账户类型已改为「{pricing.customer_type_label(request.user)}」，"
                       f"商品价格与 AI 报价将按新身份展示")
            if 'phone' in changed:
                message = f"手机号已更新（可查看 {visible_after} 笔订单）；" + message
        elif 'phone' in changed:
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
        qs = Orders.objects.filter(my_orders_q(request.user)).select_related('product', 'review')

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
            elif action == 'pay':
                # 模拟支付（任务书五·1）。不改订单状态机，只落支付状态与支付时间。
                result = mark_order_paid(order, operator=request.user)
            elif action == 'review':
                # 商品评价（任务书六·4）。同样不碰状态机，另建一条评价记录。
                # 这里直接 return —— 返回体与下面的通用分支不同（没有 result.message）。
                review = create_review(
                    order,
                    rating=request.data.get('rating'),
                    content=request.data.get('content') or '',
                    user=request.user,
                )
                return Response({
                    "success": True,
                    "message": "评价提交成功，感谢你的反馈！",
                    "review": serialize_review(review),
                    "order": _serialize_order(order, with_actions=True),
                })
            elif action == 'withdraw-return':
                result = withdraw_return_request(order)
            else:
                return _err(f"不支持的操作：{action}", code=404)
        except ReviewError as e:
            # 评分非法 / 未完成 / 重复评价 —— 都是用户可自行修正的问题，返回 400 与原因
            return _err(e.message, code=400)
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


# ════════════════════════════════════════════════════════════════
# 收货地址簿（C 端「我的」）
# ════════════════════════════════════════════════════════════════
# 设计要点
# --------
# · **归属只认请求者本人**：地址没有历史包袱，不需要手机号兜底认领，
#   所以一律 `user=request.user` 查询，别人给的 id 直接当"不存在"处理。
# · **每次写操作都回全量列表**：前端一次刷新即可，不用自己把返回的
#   单条往本地数组里插/删/改（那种前端逻辑最容易和默认地址标记不同步）。
# · 业务规则全在 `agent/addresses.py`，本层只做 JSON ↔ 参数的搬运。
def _address_list_payload(user):
    """地址列表的统一返回体（列表 / 默认 id / 条数上限）。"""
    rows = list_addresses(user)
    return {
        "count": len(rows),
        "max": MAX_PER_USER,
        "default_id": next((a.pk for a in rows if a.is_default), None),
        "addresses": [serialize_address(a) for a in rows],
    }


class MeAddressListView(views.APIView):
    """/api/me/addresses/ —— 收货地址簿：列表 + 新增"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"success": True, **_address_list_payload(request.user)})

    def post(self, request):
        data = request.data or {}
        try:
            addr = create_address(
                request.user,
                receiver=data.get("receiver"),
                phone=data.get("phone"),
                address=data.get("address"),
                # 前端多传/少传都当 False；字符串 "true"/"1" 也认（表单控件常见）
                is_default=str(data.get("is_default", "")).lower() in ("1", "true", "yes", "on"),
            )
        except AddressError as e:
            return _err(e.message, code=400)

        return Response({
            "success": True,
            "message": ("已新增收货地址，并设为默认" if addr.is_default else "已新增收货地址"),
            "address": serialize_address(addr),
            **_address_list_payload(request.user),
        })


class MeAddressDetailView(views.APIView):
    """/api/me/addresses/<id>/ —— 编辑（PATCH）/ 删除（DELETE）"""

    permission_classes = [IsAuthenticated]

    def patch(self, request, address_id):
        data = request.data or {}
        # 只把**出现过的**键传下去：None 表示"不改这个字段"
        fields = {k: data[k] for k in ("receiver", "phone", "address") if k in data}
        # is_default 只认 True（取消默认请改用「设为默认」到别的地址，或删除本条）
        if "is_default" in data:
            fields["is_default"] = str(data.get("is_default")).lower() in ("1", "true", "yes", "on")

        try:
            addr = update_address(request.user, address_id, **fields)
        except AddressError as e:
            return _err(e.message, code=404 if e.code == "not_found" else 400)

        return Response({
            "success": True,
            "message": "已设为默认地址" if (fields.get("is_default") and addr.is_default)
                       else "收货地址已更新",
            "address": serialize_address(addr),
            **_address_list_payload(request.user),
        })

    def delete(self, request, address_id):
        try:
            result = delete_address(request.user, address_id)
        except AddressError as e:
            return _err(e.message, code=404 if e.code == "not_found" else 400)

        message = "收货地址已删除"
        if result["new_default_id"]:
            message += "；原默认地址已删除，已把最新的一条设为默认"
        return Response({
            "success": True,
            "message": message,
            **result,
            **_address_list_payload(request.user),
        })


class MeAddressDefaultView(views.APIView):
    """/api/me/addresses/<id>/default/ —— 设为默认地址（幂等）"""

    permission_classes = [IsAuthenticated]

    def post(self, request, address_id):
        try:
            addr = set_default(request.user, address_id)
        except AddressError as e:
            return _err(e.message, code=404 if e.code == "not_found" else 400)

        return Response({
            "success": True,
            "message": "已设为默认地址，下单时会自动带入",
            "address": serialize_address(addr),
            **_address_list_payload(request.user),
        })


# ════════════════════════════════════════════════════════════════
# 我的余额（C 端钱包）
# ════════════════════════════════════════════════════════════════
def _wallet_payload(user, limit: int = 50) -> dict:
    """钱包页的统一返回体（概览 + 流水）。

    与地址簿同样的口径：**每次都回全量概览**，前端不做本地加减。
    余额这种东西，"前端算出来 = 后端记着"的错觉最容易在退款/补单后露馅。
    """
    info = summarize(user)
    return {
        "balance": str(info["balance"]),
        "recharged": str(info["recharged"]),
        "paid": str(info["paid"]),
        "refunded": str(info["refunded"]),
        "tx_count": info["tx_count"],
        "topup_max": str(MAX_TOPUP),
        "transactions": [serialize_transaction(t) for t in list_transactions(user, limit)],
    }


class MeWalletView(views.APIView):
    """GET /api/me/wallet/ —— 我的余额 + 最近流水。

    汇总口径全部来自 `agent/user_wallet.summarize`（业务层唯一出口），
    本层只做参数收敛与搬运 —— 避免"视图里又算一遍余额"这种双口径。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        return Response({"success": True, **_wallet_payload(request.user, limit)})


class MeWalletRechargeView(views.APIView):
    """POST /api/me/wallet/recharge/ —— **模拟**充值入账。

    ⚠️ 真实系统里余额只可能由「支付平台的异步回调」增加 —— 绝不可能存在一个
    前端可以直接调、说加钱就加钱的接口。本项目没有商户号 / 备案域名 / HTTPS 回调
    （见方案书的支付边界说明），所以收银台是**自建模拟**的：
    用户在收银台点「确认付款」= 收款成功 = 这里入账。

    把这层写在注释里而不是悄悄省掉，是为了让答辩时"钱从哪来"这条线有明确交代：
    我们模拟的是**收款动作**，不是**支付渠道**；一旦换成真实渠道，
    要动的只有"谁来调 `recharge()`"这一处。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        try:
            tx = recharge(
                request.user,
                data.get("amount"),
                note=str(data.get("note") or "").strip(),
            )
        except UserWalletError as e:
            return _err(e.message, code=401 if e.code == "unauthenticated" else 400)

        return Response({
            "success": True,
            "message": f"充值成功，当前余额 ¥{tx.balance_after:,.2f}",
            "transaction": serialize_transaction(tx),
            **_wallet_payload(request.user),
        })
