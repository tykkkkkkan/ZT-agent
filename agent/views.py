"""
agent/views.py — HTTP 视图层

职责：接收请求、参数校验、SSE 格式化、返回响应。
Agent 的推理与工具调用逻辑已下沉到 agent/agent.py，本文件保持「薄」。

P0 增强：补全 Products / Inventory / Orders 的 CRUD，
         接入 service 层实现订单状态变更时的库存联动。
P2 增强：管理类 API 加 staff 鉴权（session auth，同源 cookie 自动携带）；
         公开 API（chat/contact/custom/history/产品列表/订单查询）保持公开。
"""
import json
import logging
import random
import uuid
from datetime import datetime
from functools import wraps

from django.db import transaction
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET, require_http_methods

from agent.agent import agent_stream, _save_conversation
from agent.safety import check_message
from agent.services import (
    OrderTransitionError, InsufficientStockError,
    transition_order, reserve_inventory,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════
# 管理类 API 鉴权（P2）
# ════════════════════════════════════════════════════════════════
def _jwt_user(request):
    """尝试从 Authorization: Bearer <access> 解析 JWT 用户（不限 staff）。

    返回 (user, ok)：
      - token 合法且用户未停用 → (user, True)
      - 无 Authorization 头 / token 缺失 / 非法 / 过期 / 用户已停用 → (None, False)
    无 Authorization 头时返回 (None, False)，由调用方回退到 session 认证。
    """
    from django.contrib.auth import get_user_model
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.lower().startswith("bearer "):
        return None, False
    token = header[7:].strip()
    if not token:
        return None, False
    try:
        from rest_framework_simplejwt.tokens import AccessToken
        user_id = AccessToken(token).get("user_id")
        user = get_user_model().objects.get(pk=user_id)
        return (user, True) if user.is_active else (None, False)
    except Exception:
        return None, False


def _jwt_staff_user(request):
    """要求 JWT 用户为 staff。返回 (user, ok)；非 staff / 未登录 → (None, False)。"""
    user, ok = _jwt_user(request)
    if ok and user.is_staff:
        return user, True
    return None, False


def _resolve_request_user(request):
    """统一解析请求身份（session 优先，其次 JWT）。返回 (user, ok)。"""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user, True
    return _jwt_user(request)


def login_required(view_func):
    """要求「已登录用户」，session 与 JWT 双通道（P2 增强）。

    用途：C 端涉及隐私或产生业务数据的接口（下单、订单查询、留言、定制、AI 对话、
    会话历史）。未登录一律返回 401 JSON，前端 auth.js 据此跳转登录页。

    - session 通道：管理员登录 /admin/ 后，同源请求自动携带 cookie 也可通过；
    - JWT 通道：前端页面经 auth.js 的 authFetch 带 `Authorization: Bearer <access>`。
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        _, ok = _resolve_request_user(request)
        if not ok:
            return JsonResponse(
                {"success": False, "code": "LOGIN_REQUIRED", "message": "请先登录后再操作"},
                status=401,
            )
        return view_func(request, *args, **kwargs)
    return wrapper


def staff_required(view_func):
    """要求 staff（后台管理员）登录，session 与 JWT 双通道。

    - session 通道：浏览器访问 admin 登录后，同源请求自动带 session cookie
    - JWT 通道：前端管理页通过 auth.js 带 `Authorization: Bearer <access>`
    - 非 staff / 未登录 → 401 JSON
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user.is_authenticated and user.is_staff):
            # session 未过 → 尝试 JWT
            user, ok = _jwt_staff_user(request)
            if not ok:
                return JsonResponse(
                    {"success": False, "message": "需要管理员权限，请先登录后台。"},
                    status=401,
                )
        return view_func(request, *args, **kwargs)
    return wrapper

# SSE 事件序列化（统一 JSON 格式）
def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _server_error(e):
    """统一服务端异常处理：记录日志，向客户端返回通用错误（不泄露内部细节）。"""
    logger.exception("服务端未预期异常：%s", e)
    return JsonResponse({"success": False, "message": "服务器开小差了，请稍后重试。"}, status=500)


# ══════════════════════════════════════════════════════════════
# AI 聊天接口（SSE 真·流式）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def chat(request):
    """AI 聊天接口：POST /api/agent/chat/（SSE 流式）【需登录】"""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体必须是 JSON"}, status=400)

    user_message = (body.get("message") or "").strip()
    if not user_message:
        return JsonResponse({"error": "message 不能为空"}, status=400)

    session_id = (body.get("session_id") or "").strip() or uuid.uuid4().hex

    # 输入安全审核（C 端客服合规前置）：命中脏话 / 注入 / 刷屏 / 超长 → 拒绝
    ok, reason = check_message(user_message)
    if not ok:
        logger.warning("聊天输入被安全审核拦截 reason=%s session_id=%s", reason, session_id)
        return JsonResponse(
            {"error": "您的输入包含不当内容或异常，请调整后重试。如确需人工协助，请拨打客服热线。"},
            status=400,
        )

    logger.info("收到聊天请求 session_id=%s msg_len=%d", session_id, len(user_message))

    # 先落库用户消息
    _save_conversation(session_id, "user", user_message)

    def event_stream():
        # 防代理缓冲：先发送 4KB+ 的注释行
        yield ":" + " " * 4096 + "\n\n"
        # 先回 session_id，前端用于持久化会话
        yield _sse({"session_id": session_id})

        parts = []
        try:
            for ev in agent_stream(session_id, user_message):
                if ev.get("type") in ("order_success", "guide_card"):
                    yield _sse(ev)          # 订单成功卡片 / 引导转化卡片
                    continue
                if "content" in ev:
                    parts.append(ev["content"])
                    yield _sse({"content": ev["content"]})
        except Exception as e:
            # 详细错误只进日志，不对前端泄露内部信息（如 key/DB 报错细节）
            logger.exception("AI 聊天流处理出错：%s", e)
            parts.append("⚠️ 服务开小差了，请稍后重试。")
            yield _sse({"content": "⚠️ 服务开小差了，请稍后重试。"})

        # 流结束后落库 AI 回复
        _save_conversation(session_id, "assistant", "".join(parts))
        yield _sse({"done": True, "session_id": session_id})

    response = StreamingHttpResponse(
        event_stream(), content_type="text/event-stream; charset=utf-8"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ══════════════════════════════════════════════════════════════
# 历史查询接口
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@login_required
def history(request, session_id):
    """查询对话历史：GET /api/agent/history/<session_id>/【需登录】"""
    try:
        from agent.models import Conversations
        records = Conversations.objects.filter(
            session_id=session_id
        ).order_by("created_at").values("role", "content", "created_at")
        data = [
            {
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r["created_at"] else "",
            }
            for r in records
        ]
        return JsonResponse({"success": True, "session_id": session_id, "messages": data})
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 联系表单接口
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def contact_submit(request):
    """联系表单接口：POST /api/agent/contact/【需登录】"""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    email = (body.get("email") or "").strip()
    msg = (body.get("msg") or "").strip()

    if not name or not phone:
        return JsonResponse({"success": False, "message": "姓名和电话不能为空"}, status=400)
    if len(name) > 50:
        return JsonResponse({"success": False, "message": "姓名最长50个字符"}, status=400)
    if len(phone) > 20:
        return JsonResponse({"success": False, "message": "电话最长20个字符"}, status=400)
    if len(email) > 100:
        return JsonResponse({"success": False, "message": "邮箱最长100个字符"}, status=400)

    from agent.models import ContactMessage
    ContactMessage.objects.create(name=name, phone=phone, email=email, message=msg)
    return JsonResponse({"success": True, "message": "留言已收到"})


# ══════════════════════════════════════════════════════════════
# 定制表单接口
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def custom_submit(request):
    """定制表单接口：POST /api/agent/custom/【需登录】"""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    company = (body.get("company") or "").strip()
    quantity = (body.get("quantity") or "").strip()
    description = (body.get("description") or "").strip()

    if not name or not phone or not quantity or not description:
        return JsonResponse(
            {"success": False, "message": "姓名、电话、需求量、需求描述为必填项"}, status=400
        )
    if len(name) > 50:
        return JsonResponse({"success": False, "message": "姓名最长50个字符"}, status=400)
    if len(phone) > 20:
        return JsonResponse({"success": False, "message": "电话最长20个字符"}, status=400)

    from agent.models import CustomRequest
    CustomRequest.objects.create(
        name=name, phone=phone, company=company,
        quantity=quantity, description=description,
    )
    return JsonResponse({"success": True, "message": "定制需求已提交"})


# ══════════════════════════════════════════════════════════════
# 产品列表（下单页用）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
def product_list(request):
    """GET /api/products/ — 返回产品列表（用于下单页下拉框）

    P0 增强：
      - 新增 sku、is_active、available_stock（join inventory 实时算）
      - 默认只返回 is_active=True 的在售商品
      - ?include_inactive=1 可看全部（含下架）
    """
    from agent.models import Products, Inventory
    try:
        qs = Products.objects.all().order_by("id")
        if request.GET.get("include_inactive") != "1":
            qs = qs.filter(is_active=True)
        # 一次性把 inventory 拿出来，避免 N+1
        inv_map = {i.product_id: i for i in Inventory.objects.all()}
        data = []
        for p in qs:
            inv = inv_map.get(p.id)
            data.append({
                "id": p.id,
                "sku": p.sku,                                     # 新增：商品编码
                "name": p.name,
                "spec": p.spec or "",
                "target_fish": p.target_fish or "",
                "retail_price": float(p.retail_price or 0),
                "wholesale_price": float(p.wholesale_price or 0),
                "description": p.description or "",
                "is_active": p.is_active,                          # 新增
                "stock": (inv.stock or 0) if inv else 0,          # 新增
                "available_stock": (inv.available_stock if inv else 0),  # 新增：可用库存
                "alert_line": (inv.alert_line or 0) if inv else 0,
            })
        return JsonResponse({"success": True, "products": data})
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 创建订单（下单页提交）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def create_order_api(request):
    """POST /api/orders/ — 前端下单页提交订单【需登录】

    Body: {customer_name, phone, product_id, quantity}
    Returns: {success, order_no, product_name, quantity, total_price, message}
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    customer_name = (body.get("customer_name") or "").strip()
    phone = (body.get("phone") or "").strip()
    product_id = body.get("product_id")
    quantity_raw = body.get("quantity")

    # 校验
    if not customer_name:
        return JsonResponse({"success": False, "message": "请填写姓名"}, status=400)
    if len(customer_name) > 50:
        return JsonResponse({"success": False, "message": "姓名最长50个字符"}, status=400)
    if not phone:
        return JsonResponse({"success": False, "message": "请填写联系电话"}, status=400)
    if len(phone) > 20:
        return JsonResponse({"success": False, "message": "电话最长20个字符"}, status=400)
    if not product_id:
        return JsonResponse({"success": False, "message": "请选择产品"}, status=400)
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "数量必须是整数"}, status=400)
    if quantity <= 0:
        return JsonResponse({"success": False, "message": "数量必须大于 0"}, status=400)

    from agent.models import Products, Orders

    # 查产品
    try:
        product = Products.objects.get(id=int(product_id))
    except Products.DoesNotExist:
        return JsonResponse({"success": False, "message": "所选产品不存在"}, status=404)
    except Exception as e:
        return _server_error(e)

    if not product.is_active:
        return JsonResponse({"success": False, "message": "该产品已下架，暂不可下单"}, status=400)

    # 计算金额（按零售价）
    unit_price = float(product.retail_price or 0)
    total_price = round(unit_price * quantity, 2)

    # 生成订单号：DD + 年月日时分秒 + 4 位随机数（时间戳降低碰撞概率，随机位防同秒重复）
    now = datetime.now()
    order_no = None
    for _ in range(5):  # 极小概率重号（同秒同 4 位随机），重试 5 次
        candidate = f"DD{now.strftime('%Y%m%d%H%M%S')}{random.randint(1000, 9999)}"
        if not Orders.objects.filter(order_no=candidate).exists():
            order_no = candidate
            break
    if order_no is None:
        return JsonResponse({"success": False, "message": "订单号生成失败，请重试"}, status=500)

    # 写入订单 + 预占库存（P0 修复：事务 + 行级锁）
    # 个人中心改造：写入 user_id（订单归属登录用户），并在手机号未被他人占用时
    # 自动绑定到该用户资料 —— 这样用户下次进个人中心即使不手动填手机号也能看到订单。
    me, _ = _resolve_request_user(request)
    try:
        with transaction.atomic():
            order = Orders.objects.create(
                order_no=order_no,
                customer_name=customer_name,
                phone=phone,
                user_id=me.id if me else None,
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,                  # 冗余快照（P0：便于历史追溯）
                quantity=quantity,
                unit_price=unit_price,                    # 新增：单价快照
                total_price=total_price,
                status="未发货",                          # OrderStatus.PENDING.value
                created_at=timezone.now(),
            )
            # 预占库存：会校验可用库存是否足够
            try:
                reserve_inventory(order)
            except InsufficientStockError as e:
                # 库存不足：标记本事务回滚（订单不创建），返回业务可读错误
                transaction.set_rollback(True)
                return JsonResponse({
                    "success": False,
                    "message": e.message,
                    "code": e.code,
                    "available": e.available,
                    "requested": e.requested,
                }, status=400)
    except Exception as e:
        return _server_error(e)

    # 绑定手机号 / 记住收货人（失败不影响下单结果，仅用于个人中心体验）
    if me is not None:
        try:
            from agent.me_helpers import bind_phone_if_free
            bind_phone_if_free(me, phone, nickname=customer_name)
        except Exception:  # noqa: BLE001 — 绑定手机号属于增强体验，绝不能影响下单主流程
            logger.warning("下单后自动绑定手机号失败：user=%s phone=%s", me.id, phone, exc_info=True)

    return JsonResponse({
        "success": True,
        "order_no": order_no,
        "product_name": product.name,
        "product_sku": product.sku,                      # 新增
        "spec": product.spec or "",
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "message": "下单成功！我们会尽快与您联系确认发货。",
    })


# ══════════════════════════════════════════════════════════════
# 订单查询（按手机号查该手机号的订单，隐私脱敏）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def query_orders(request):
    """POST /api/orders/query/ — 按手机号查询该手机号的所有订单（脱敏，不返回姓名/电话）【需登录】

    Body: {"phone": "13800138000", "order_no": "DD20260827xxxx"} (order_no 可选)
    - 仅传 phone → 列出该手机号最近 50 条订单的核心字段
    - 同时传 phone + order_no → 验证匹配后返回该订单详情
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    phone = (body.get("phone") or "").strip()
    order_no = (body.get("order_no") or "").strip()

    if not phone:
        return JsonResponse({"success": False, "message": "请输入下单手机号"}, status=400)
    if len(phone) > 20:
        return JsonResponse({"success": False, "message": "手机号格式有误"}, status=400)

    from agent.models import Orders

    try:
        # 单订单详情查询
        if order_no:
            order = Orders.objects.filter(order_no=order_no, phone=phone).first()
            if not order:
                return JsonResponse({"success": False, "message": "订单号或手机号不匹配"}, status=404)
            return JsonResponse({
                "success": True,
                "order": _serialize_order(order),
            })

        # 列出该手机号所有订单
        orders = Orders.objects.filter(phone=phone).order_by("-created_at")[:50]
        return JsonResponse({
            "success": True,
            "phone": phone,
            "count": len(orders),
            "orders": [_serialize_order(o) for o in orders],
        })
    except Exception as e:
        return _server_error(e)


def _order_actions(order):
    """按当前状态推导「用户可执行的操作」列表。

    规则集中在这里，前端只按返回的按钮名渲染，避免前后端各写一套状态判断而漂移。
    """
    from agent.models import OrderStatus, ReturnStatus

    s = order.status
    acts = []
    if s == OrderStatus.PENDING:
        acts.append('cancel')                       # 未发货：可取消
    if s == OrderStatus.SHIPPED:
        acts.append('confirm')                      # 已发货：可确认收货
        acts.append('return')                       #        可申请退货
    if s == OrderStatus.COMPLETED:
        acts.append('return')                       # 已完成：仍可申请售后
    if s == OrderStatus.RETURNING and order.return_status == ReturnStatus.PENDING:
        acts.append('withdraw_return')              # 退货待审核：可自行撤销
    return acts


def _serialize_order(order, with_actions=False):
    """订单脱敏序列化（不返回姓名/电话等隐私字段，含物流信息与售后状态）

    :param with_actions: 是否附带「用户可执行操作」列表（个人中心用）
    """
    data = {
        "order_no": order.order_no,
        "product_id": order.product_id,
        "product_sku": order.product_sku or "",            # P0 新增：SKU 快照
        "product_name": order.product_name,
        "quantity": order.quantity,
        "unit_price": float(order.unit_price or 0),         # P0 新增：单价快照
        "total_price": float(order.total_price or 0),
        "status": order.status,
        "ship_company": order.ship_company or '',
        "tracking_no": order.tracking_no or '',
        "shipped_at": order.shipped_at.strftime("%Y-%m-%d %H:%M:%S") if order.shipped_at else '',
        "cancelled_at": order.cancelled_at.strftime("%Y-%m-%d %H:%M:%S") if order.cancelled_at else '',
        "cancel_reason": order.cancel_reason or '',        # P0 新增
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M:%S") if order.created_at else '',
        # ── 售后 / 收货闭环（个人中心展示 + 后台反馈） ──
        "completed_at": order.completed_at.strftime("%Y-%m-%d %H:%M:%S") if order.completed_at else '',
        "return_status": order.return_status or '',
        "return_reason": order.return_reason or '',
        "return_requested_at": (order.return_requested_at.strftime("%Y-%m-%d %H:%M:%S")
                                if order.return_requested_at else ''),
        "return_handled_at": (order.return_handled_at.strftime("%Y-%m-%d %H:%M:%S")
                              if order.return_handled_at else ''),
        "return_note": order.return_note or '',
    }
    if with_actions:
        data["actions"] = _order_actions(order)
    return data


# ══════════════════════════════════════════════════════════════
# 查询单个订单（下单成功页查询）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@login_required
def order_detail(request, order_no):
    """GET /api/orders/<order_no>/ — 查询单个订单详情（脱敏，不返回姓名/电话）【需登录】"""
    from agent.models import Orders
    try:
        order = Orders.objects.get(order_no=order_no)
        return JsonResponse({"success": True, "order": _serialize_order(order)})
    except Orders.DoesNotExist:
        return JsonResponse({"success": False, "message": "订单不存在"}, status=404)
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 订单状态变更（核心 P0：触发库存联动）
# ══════════════════════════════════════════════════════════════
# 注：以下三个端点均调用 agent.services.transition_order，
#     在事务内完成"改状态 + 动库存"。库存不足 / 非法跃迁 → 整体回滚。
#
# 前端调用样例（发货）：
#   POST /api/orders/<order_no>/ship/
#   Body: {"ship_company": "顺丰", "tracking_no": "SF1234567890"}
#
@csrf_exempt
@require_POST
@staff_required
def ship_order_api(request, order_no):
    """POST /api/orders/<order_no>/ship/ — 标记发货（扣减库存）【需管理员】"""
    from agent.models import Orders
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    ship_company = (body.get("ship_company") or "").strip()
    tracking_no = (body.get("tracking_no") or "").strip()
    try:
        order = Orders.objects.get(order_no=order_no)
    except Orders.DoesNotExist:
        return JsonResponse({"success": False, "message": "订单不存在"}, status=404)

    try:
        result = transition_order(
            order, to_status="已发货",
            ship_company=ship_company, tracking_no=tracking_no,
        )
    except InsufficientStockError as e:
        return JsonResponse({
            "success": False, "message": e.message,
            "code": e.code, "available": e.available, "requested": e.requested,
        }, status=400)
    except OrderTransitionError as e:
        return JsonResponse({"success": False, "message": e.message, "code": e.code},
                            status=400 if e.code == "invalid_transition" else 500)
    return JsonResponse({"success": True, "order": _serialize_order(result.order),
                         "message": result.message})


@csrf_exempt
@require_POST
@staff_required
def cancel_order_api(request, order_no):
    """POST /api/orders/<order_no>/cancel/ — 取消订单（释放预占库存）【需管理员】"""
    from agent.models import Orders
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    cancel_reason = (body.get("cancel_reason") or "").strip()[:200]
    try:
        order = Orders.objects.get(order_no=order_no)
    except Orders.DoesNotExist:
        return JsonResponse({"success": False, "message": "订单不存在"}, status=404)

    try:
        result = transition_order(
            order, to_status="已取消", cancel_reason=cancel_reason,
        )
    except OrderTransitionError as e:
        return JsonResponse({"success": False, "message": e.message, "code": e.code},
                            status=400 if e.code == "invalid_transition" else 500)
    return JsonResponse({"success": True, "order": _serialize_order(result.order),
                         "message": result.message})


@csrf_exempt
@require_POST
@staff_required
def return_order_api(request, order_no):
    """POST /api/orders/<order_no>/return/ — 退货（库存回滚）【需管理员】"""
    from agent.models import Orders
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    cancel_reason = (body.get("cancel_reason") or "").strip()[:200] or "客户退货"
    try:
        order = Orders.objects.get(order_no=order_no)
    except Orders.DoesNotExist:
        return JsonResponse({"success": False, "message": "订单不存在"}, status=404)

    try:
        result = transition_order(
            order, to_status="已退货", cancel_reason=cancel_reason,
        )
    except OrderTransitionError as e:
        return JsonResponse({"success": False, "message": e.message, "code": e.code},
                            status=400 if e.code == "invalid_transition" else 500)
    return JsonResponse({"success": True, "order": _serialize_order(result.order),
                         "message": result.message})


# ══════════════════════════════════════════════════════════════
# 产品 CRUD（P0 补齐）
# ══════════════════════════════════════════════════════════════
def _serialize_product(p, inv_map=None):
    """产品序列化（含 SKU 与实时库存），inv_map 用于批量避免 N+1"""
    inv = (inv_map or {}).get(p.id)
    return {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        "spec": p.spec or "",
        "target_fish": p.target_fish or "",
        "retail_price": float(p.retail_price or 0),
        "wholesale_price": float(p.wholesale_price or 0),
        "description": p.description or "",
        "is_active": p.is_active,
        "stock": (inv.stock if inv else 0),
        "reserved_stock": (inv.reserved_stock if inv else 0),
        "available_stock": (inv.available_stock if inv else 0),
        "alert_line": (inv.alert_line if inv else 0),
        "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
        "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M:%S") if p.updated_at else "",
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
@staff_required
def products_collection(request):
    """GET /api/products/all/   列出全部产品（含下架）【需管理员】
    POST /api/products/all/  新建产品【需管理员】
    """
    from agent.models import Products, Inventory

    if request.method == "GET":
        try:
            inv_map = {i.product_id: i for i in Inventory.objects.all()}
            qs = Products.objects.all().order_by("-created_at", "-id")
            return JsonResponse({
                "success": True,
                "products": [_serialize_product(p, inv_map) for p in qs],
            })
        except Exception as e:
            return _server_error(e)

    # POST 新建
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    sku = (body.get("sku") or "").strip()
    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"success": False, "message": "产品名称必填"}, status=400)

    # 若未传 sku，自动生成
    if not sku:
        last = Products.objects.order_by("-id").first()
        next_id = (last.id if last else 0) + 1
        sku = f"SKU-{next_id:04d}"
        # 防重号
        while Products.objects.filter(sku=sku).exists():
            next_id += 1
            sku = f"SKU-{next_id:04d}"

    if Products.objects.filter(sku=sku).exists():
        return JsonResponse({"success": False, "message": f"SKU「{sku}」已存在"}, status=400)

    try:
        with transaction.atomic():
            p = Products.objects.create(
                sku=sku,
                name=name,
                spec=(body.get("spec") or "").strip() or None,
                target_fish=(body.get("target_fish") or "").strip() or None,
                retail_price=body.get("retail_price") or 0,
                wholesale_price=body.get("wholesale_price") or 0,
                description=(body.get("description") or "").strip() or None,
                is_active=bool(body.get("is_active", True)),
            )
            # 同时建一条库存记录
            initial_stock = int(body.get("stock") or 0)
            Inventory.objects.create(
                product=p, stock=initial_stock, reserved_stock=0,
                alert_line=int(body.get("alert_line") or 50),
            )
        return JsonResponse({"success": True, "product": _serialize_product(p)},
                            status=201)
    except Exception as e:
        return _server_error(e)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "PUT", "DELETE"])
@staff_required
def product_detail_api(request, product_id):
    """GET/PATCH/PUT/DELETE /api/products/<id>/ — 单产品 CRUD【需管理员】"""
    from agent.models import Products, Inventory
    try:
        p = Products.objects.get(id=product_id)
    except Products.DoesNotExist:
        return JsonResponse({"success": False, "message": "产品不存在"}, status=404)

    if request.method == "GET":
        return JsonResponse({"success": True, "product": _serialize_product(p)})

    if request.method == "DELETE":
        # 软删除：is_active=False（保护历史订单）
        p.is_active = False
        p.save(update_fields=["is_active", "updated_at"])
        return JsonResponse({"success": True, "message": "已下架（软删除）",
                             "product": _serialize_product(p)})

    # PATCH / PUT
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    if "sku" in body and body["sku"] != p.sku:
        new_sku = (body["sku"] or "").strip()
        if not new_sku:
            return JsonResponse({"success": False, "message": "SKU 不能为空"}, status=400)
        if Products.objects.filter(sku=new_sku).exclude(id=p.id).exists():
            return JsonResponse({"success": False, "message": f"SKU「{new_sku}」已被其他产品占用"}, status=400)
        p.sku = new_sku
    for f in ("name", "spec", "target_fish", "description", "is_active"):
        if f in body:
            setattr(p, f, body[f])
    for f in ("retail_price", "wholesale_price"):
        if f in body:
            setattr(p, f, body[f])
    try:
        p.save()
        # 同步更新库存
        if any(k in body for k in ("stock", "alert_line")):
            inv = Inventory.objects.filter(product=p).first()
            if inv is None:
                Inventory.objects.create(
                    product=p, stock=int(body.get("stock") or 0), reserved_stock=0,
                    alert_line=int(body.get("alert_line") or 50),
                )
            else:
                if "stock" in body:
                    new_stock = max(0, int(body["stock"]))
                    if new_stock < (inv.reserved_stock or 0):
                        return JsonResponse({
                            "success": False,
                            "message": f"库存不能小于已预占数（{inv.reserved_stock}）",
                        }, status=400)
                    inv.stock = new_stock
                if "alert_line" in body:
                    inv.alert_line = max(0, int(body["alert_line"]))
                inv.save()
        return JsonResponse({"success": True, "product": _serialize_product(p)})
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 库存 CRUD（P0 补齐）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@staff_required
def inventory_list_api(request):
    """GET /api/inventory/ — 库存列表（含产品信息）【需管理员】

    L5 编排：额外带回 ProductCoordination 覆盖状态（暂停购买/客户提示），
    供 storefront 与营销 Agent 感知「多 Agent 协商后的临时状态」。
    """
    from agent.models import Products, Inventory, ProductCoordination
    try:
        coord_map = {c.product_id: c for c in ProductCoordination.objects.all()}
        items = []
        for inv in Inventory.objects.select_related("product").order_by("product_id"):
            coord = coord_map.get(inv.product_id)
            items.append({
                "id": inv.id,
                "product_id": inv.product_id,
                "product_sku": inv.product.sku if inv.product else "",
                "product_name": inv.product.name if inv.product else "(已删除)",
                "stock": inv.stock or 0,
                "reserved_stock": inv.reserved_stock or 0,
                "available_stock": inv.available_stock,
                "alert_line": inv.alert_line or 0,
                "is_low": (inv.stock or 0) <= (inv.alert_line or 0),
                "is_out": (inv.stock or 0) <= 0,
                "purchase_paused": bool(coord and coord.purchase_paused),
                "customer_notice": (coord.customer_notice if coord else "") or "",
            })
        return JsonResponse({"success": True, "inventory": items})
    except Exception as e:
        return _server_error(e)


# ════════════════════════════════════════════════════════════════
# L5 多智能体编排：ZT-agent 接收侧「接收钩子」
# 仅接受营销 Agent(mkt_bot) 经 JWT 调来的、有界的协调指令；
# 动作只写入独立的 product_coordination 覆盖表，绝不碰 products/inventory 业务表。
# ════════════════════════════════════════════════════════════════
_ALLOWED_COORD_EVENTS = {"pause_product", "resume_product", "set_notice"}


@csrf_exempt
@require_POST
@staff_required
def coordination_inbound_api(request):
    """POST /api/coordination/inbound/ — 接收营销 Agent 的跨 Agent 协调指令。

    请求体：
      {"event": "pause_product"|"resume_product"|"set_notice",
       "product_id": 5,
       "customer_notice": "该商品暂时缺货，可先收藏或咨询客服",   # 可选
       "source": "marketing-agent"}

    有界性：只写入 product_coordination 覆盖表；event 不在白名单一律 400。
    """
    from agent.models import Products, ProductCoordination

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    event = (body.get("event") or "").strip()
    product_id = body.get("product_id")
    if event not in _ALLOWED_COORD_EVENTS:
        return JsonResponse(
            {"success": False, "message": f"不支持的协调事件：{event}"}, status=400
        )
    if not isinstance(product_id, int) or product_id <= 0:
        return JsonResponse({"success": False, "message": "product_id 必须为正整数"}, status=400)
    if not Products.objects.filter(pk=product_id).exists():
        return JsonResponse(
            {"success": False, "message": f"商品 #{product_id} 不存在"}, status=404
        )

    # 解析调用方身份（JWT 用户），仅用于审计留痕
    actor = "unknown"
    user, ok = _jwt_user(request)
    if ok:
        actor = user.username

    defaults = {}
    if event == "pause_product":
        defaults["purchase_paused"] = True
    elif event == "resume_product":
        defaults["purchase_paused"] = False
    if "customer_notice" in body:
        defaults["customer_notice"] = (body.get("customer_notice") or "")[:500]

    obj, created = ProductCoordination.objects.update_or_create(
        product_id=product_id, defaults={**defaults, "updated_by": actor}
    )
    return JsonResponse({
        "success": True,
        "message": "协调指令已生效",
        "data": {
            "product_id": product_id,
            "purchase_paused": obj.purchase_paused,
            "customer_notice": obj.customer_notice or "",
            "updated_by": obj.updated_by,
            "updated_at": obj.updated_at.isoformat() if obj.updated_at else None,
        },
    })


@csrf_exempt
@require_http_methods(["PATCH", "PUT"])
@staff_required
def inventory_update_api(request, product_id):
    """PATCH/PUT /api/inventory/<product_id>/ — 调整库存【需管理员】

    Body: {"stock": 100, "alert_line": 50, "adjust": -10, "reason": "盘点"}
    - 直设：传 stock（绝对值）
    - 增量：传 adjust（正负数），会自动加/减
    - 二选一；都不传返回 400
    """
    from agent.models import Inventory
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    with transaction.atomic():
        try:
            inv = Inventory.objects.select_for_update().get(product_id=product_id)
        except Inventory.DoesNotExist:
            return JsonResponse({"success": False, "message": "该产品无库存记录"}, status=404)
        if "stock" in body:
            new_stock = max(0, int(body["stock"]))
            if new_stock < (inv.reserved_stock or 0):
                return JsonResponse({
                    "success": False,
                    "message": f"新库存({new_stock})不能小于已预占数({inv.reserved_stock})",
                }, status=400)
            inv.stock = new_stock
        elif "adjust" in body:
            try:
                delta = int(body["adjust"])
            except (TypeError, ValueError):
                return JsonResponse({"success": False, "message": "adjust 必须是整数"}, status=400)
            new_stock = (inv.stock or 0) + delta
            if new_stock < (inv.reserved_stock or 0):
                return JsonResponse({
                    "success": False,
                    "message": f"调整后会小于已预占数({inv.reserved_stock})",
                }, status=400)
            inv.stock = max(0, new_stock)
        if "alert_line" in body:
            inv.alert_line = max(0, int(body["alert_line"]))
        inv.save()

    return JsonResponse({
        "success": True,
        "inventory": {
            "product_id": inv.product_id,
            "stock": inv.stock,
            "reserved_stock": inv.reserved_stock,
            "available_stock": inv.available_stock,
            "alert_line": inv.alert_line,
        },
    })


# ══════════════════════════════════════════════════════════════
# 留言/定制管理（P0 补齐：标记已读；P1 补齐：列表/删除）【全部需管理员】
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@staff_required
def contact_messages_list(request):
    """GET /api/agent/contact-msgs/?is_read=0&limit=50&offset=0 — 留言列表【需管理员】

    返回最新在前，含分页元信息（total / has_more），供前端分页渲染。
    """
    from agent.models import ContactMessage
    try:
        qs = ContactMessage.objects.all().order_by("-created_at")
        is_read = request.GET.get("is_read")
        if is_read in ("0", "1"):
            qs = qs.filter(is_read=bool(int(is_read)))
        try:
            limit = min(max(int(request.GET.get("limit", 50)), 1), 200)
            offset = max(int(request.GET.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "message": "limit/offset 必须是整数"}, status=400)

        total = qs.count()
        rows = qs[offset:offset + limit]
        data = [{
            "id": m.id, "name": m.name, "phone": m.phone, "email": m.email,
            "message": m.message, "is_read": m.is_read,
            "created_at": m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        } for m in rows]
        return JsonResponse({
            "success": True, "total": total,
            "has_more": (offset + len(rows)) < total,
            "messages": data,
        })
    except Exception as e:
        return _server_error(e)


@csrf_exempt
@require_GET
@staff_required
def custom_requests_list(request):
    """GET /api/agent/custom-reqs/?is_read=0&limit=50&offset=0 — 定制需求列表【需管理员】"""
    from agent.models import CustomRequest
    try:
        qs = CustomRequest.objects.all().order_by("-created_at")
        is_read = request.GET.get("is_read")
        if is_read in ("0", "1"):
            qs = qs.filter(is_read=bool(int(is_read)))
        try:
            limit = min(max(int(request.GET.get("limit", 50)), 1), 200)
            offset = max(int(request.GET.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "message": "limit/offset 必须是整数"}, status=400)

        total = qs.count()
        rows = qs[offset:offset + limit]
        data = [{
            "id": r.id, "name": r.name, "phone": r.phone, "company": r.company,
            "quantity": r.quantity, "description": r.description, "is_read": r.is_read,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        } for r in rows]
        return JsonResponse({
            "success": True, "total": total,
            "has_more": (offset + len(rows)) < total,
            "requests": data,
        })
    except Exception as e:
        return _server_error(e)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
@staff_required
def contact_message_api(request, message_id):
    """GET/PATCH/DELETE /api/agent/contact-msg/<id>/ — 留言详情/标记已读/删除【需管理员】"""
    from agent.models import ContactMessage
    try:
        msg = ContactMessage.objects.get(id=message_id)
    except ContactMessage.DoesNotExist:
        return JsonResponse({"success": False, "message": "留言不存在"}, status=404)

    if request.method == "DELETE":
        msg.delete()
        return JsonResponse({"success": True, "message": "留言已删除"})

    if request.method == "PATCH":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            body = {}
        if "is_read" in body:
            msg.is_read = bool(body["is_read"])
            msg.save(update_fields=["is_read"])
    return JsonResponse({
        "success": True,
        "message": {
            "id": msg.id, "name": msg.name, "phone": msg.phone, "email": msg.email,
            "message": msg.message, "is_read": msg.is_read,
            "created_at": msg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        },
    })


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
@staff_required
def custom_request_api(request, request_id):
    """GET/PATCH/DELETE /api/agent/custom-req/<id>/ — 定制需求详情/标记已读/删除【需管理员】"""
    from agent.models import CustomRequest
    try:
        r = CustomRequest.objects.get(id=request_id)
    except CustomRequest.DoesNotExist:
        return JsonResponse({"success": False, "message": "定制需求不存在"}, status=404)

    if request.method == "DELETE":
        r.delete()
        return JsonResponse({"success": True, "message": "定制需求已删除"})

    if request.method == "PATCH":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            body = {}
        if "is_read" in body:
            r.is_read = bool(body["is_read"])
            r.save(update_fields=["is_read"])
    return JsonResponse({
        "success": True,
        "request": {
            "id": r.id, "name": r.name, "phone": r.phone, "company": r.company,
            "quantity": r.quantity, "description": r.description, "is_read": r.is_read,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        },
    })
