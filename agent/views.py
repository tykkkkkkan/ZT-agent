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
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from agent.agent import _save_conversation, agent_stream
from agent.safety import check_message
from agent.services import (
    InsufficientStockError,
    OrderTransitionError,
    reserve_inventory,
    transition_order,
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
    """统一解析请求身份。返回 (user, ok)。

    ⚠️ 顺序很重要：**显式携带的 JWT 优先于 session**，不是反过来。

    为什么必须这样（这是一个真实踩过的坑，症状很反直觉）：
      前端每次请求都由 auth.js 带上 `Authorization: Bearer <access>` —— 这是
      调用方**声明的身份**；而 session cookie 是**环境性**的。同一个浏览器里
      只要登录过 `/admin/`，session 就是管理员身份。如果 session 优先，就会出现：

        同一浏览器（既登录了后台、又登录了 C 端）
          → 前端下单：后端把订单记到**管理员**名下（Orders.user_id = 管理员 id）
            → 后台订单列表看得见，但 C 端「我的订单」按 user_id 查不到；
              手机号兜底那条规则又要求 `user_id IS NULL`，同样排除在外
            → **订单在前端"消失"了**
          → 前端查订单：拿管理员的 UserProfile.phone（空字符串）去和用户输入的
            真实手机号比对 → 必然不等 → 403「只能查询当前账号绑定的手机号」
            → **明明绑了号却查不出来**

      更隐蔽的是：`/api/me/*` 是 DRF 视图（只认 JWT），`/api/orders/*` 是普通视图
      （原先 session 优先）—— 同一浏览器里两个接口解析出**两个不同的人**，
      前端看到的资料与查询结果自然对不上。

    改造后的一致性保证：所有接口都拿"请求里声明的那个身份"，
    后台页面（不发 Authorization 头）依旧走 session，行为不变。
    """
    user, ok = _jwt_user(request)
    if ok:
        return user, True
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user, True
    return None, False


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

    # 把当前登录用户注入工具层上下文 —— AI 才能回答「我的订单到哪了」（任务书五·3）：
    # 工具函数拿不到 request，`query_my_orders` 依赖这个身份按归属规则查自己的订单。
    from agent.tools import set_current_user
    me, _ = _resolve_request_user(request)

    # 先落库用户消息
    _save_conversation(session_id, "user", user_message)

    def event_stream():
        # 生成器被消费时可能处在复制出的上下文中，这里再注入一次，确保工具层一定拿得到
        set_current_user(me)
        # 防代理缓冲：先发送 4KB+ 的注释行
        yield ":" + " " * 4096 + "\n\n"
        # 先回 session_id，前端用于持久化会话
        yield _sse({"session_id": session_id})

        parts = []
        try:
            for ev in agent_stream(session_id, user_message):
                if ev.get("type") in ("guide_card", "product_card", "order_card", "sources"):
                    # 结构事件：不进 parts（不是回答正文，落库会污染历史）
                    # sources = 引用来源，随回答一起展示但不参与后续对话上下文
                    yield _sse(ev)          # 名片 / 引导卡 / 引用来源
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
    """GET /api/products/ — 返回产品列表（用于下单页下拉框 / 产品页）

    返回字段说明（关键：前台必须与后台对同一商品的"能不能买"认知一致）：
      - sku / is_active / stock / available_stock / alert_line：基础字段
      - can_buy / buy_block_reason / buy_block_message：**可购买性与不可购原因**
        由 agent.purchase_guard 统一判定，与下单接口用同一套规则 ——
        营销 Agent 下发「暂停购买」后，这里会立刻变为不可购，不再出现
        「后台显示暂停、前台照样下单」的数据不同步。
      - purchase_paused / customer_notice：跨 Agent 协调覆盖状态原文，
        前端据此展示缺货提示条。

    参数：?include_inactive=1 可看全部（含下架）；默认只返回在售商品。
    """
    from agent import pricing
    from agent import reviews as review_helper
    from agent.models import Inventory, ProductCoordination, Products
    from agent.purchase_guard import evaluate
    try:
        # 任务书二·5：本接口是公开的，但**登录后会按身份裁剪价格字段**。
        # 这里不强制登录，只是"能认出你是谁就按你的身份给价"。
        viewer, _logged_in = _resolve_request_user(request)
        qs = Products.objects.all().order_by("id")
        if request.GET.get("include_inactive") != "1":
            qs = qs.filter(is_active=True)
        products = list(qs)                       # 先实体化：下面要拿 id 列表批量查评分
        # 一次性把 inventory / 协调覆盖 / 评分聚合拿出来，避免 N+1
        inv_map = {i.product_id: i for i in Inventory.objects.all()}
        coord_map = {c.product_id: c for c in ProductCoordination.objects.all()}
        rating_map = review_helper.rating_summary([p.id for p in products])
        data = []
        for p in products:
            inv = inv_map.get(p.id)
            coord = coord_map.get(p.id)
            rating = rating_map.get(p.id) or {}
            verdict = evaluate(p, inv, coord)
            row = {
                "id": p.id,
                "sku": p.sku,
                "name": p.name,
                # ── 产品分类（任务书二·4）：产品页「展开筛选」用的真实字段 ──
                "category": p.category or "",
                "spec": p.spec or "",
                "target_fish": p.target_fish or "",
                "description": p.description or "",
                "is_active": p.is_active,
                "stock": (inv.stock or 0) if inv else 0,
                "available_stock": verdict["available_stock"],
                "alert_line": (inv.alert_line or 0) if inv else 0,
                # ── 可购买性（与下单接口同源判定）──
                "can_buy": verdict["can_buy"],
                "buy_block_reason": verdict["reason"],
                "buy_block_message": verdict["message"],
                # ── 跨 Agent 协调状态（营销侧暂停购买/客户提示）──
                "purchase_paused": bool(coord and coord.purchase_paused),
                "customer_notice": verdict["customer_notice"],
                "reserved_stock": (inv.reserved_stock or 0) if inv else 0,
                # ── 商品评价（任务书六·4）：无评价时 count=0、avg=0 ──
                "rating_avg": rating.get("rating_avg", 0.0),
                "review_count": rating.get("review_count", 0),
            }
            # ── 价格（任务书二·5）：按身份裁剪，非经销商**不含** wholesale_price 键 ──
            row.update(pricing.price_fields(p, viewer))
            data.append(row)
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

    Body: {customer_name, phone, address, expected_date?, product_id, quantity}
          · address        收货地址（必填，任务书四·4）
          · expected_date  期望交期 YYYY-MM-DD（选填，留空 = 尽快发货）
    Returns: {success, order_no, product_name, quantity, total_price, message}
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    customer_name = (body.get("customer_name") or "").strip()
    phone = (body.get("phone") or "").strip()
    address = (body.get("address") or "").strip()
    expected_date_raw = (body.get("expected_date") or "").strip()
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
    # 任务书四·4：收货地址由用户在下单界面填写 —— 没有地址商家无法发货，必填
    if not address:
        return JsonResponse({"success": False, "message": "请填写收货地址"}, status=400)
    if len(address) > 255:
        return JsonResponse({"success": False, "message": "收货地址最长255个字符"}, status=400)
    # 期望交期选填：留空表示"尽快发货"
    expected_date = None
    if expected_date_raw:
        try:
            expected_date = datetime.strptime(expected_date_raw, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse(
                {"success": False, "message": "期望交期格式应为 YYYY-MM-DD"}, status=400
            )
        if expected_date < datetime.now().date():
            return JsonResponse(
                {"success": False, "message": "期望交期不能早于今天"}, status=400
            )
    if not product_id:
        return JsonResponse({"success": False, "message": "请选择产品"}, status=400)
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "数量必须是整数"}, status=400)
    if quantity <= 0:
        return JsonResponse({"success": False, "message": "数量必须大于 0"}, status=400)

    from agent.models import Orders, Products

    # 查产品
    try:
        product = Products.objects.get(id=int(product_id))
    except Products.DoesNotExist:
        return JsonResponse({"success": False, "message": "所选产品不存在"}, status=404)
    except Exception as e:
        return _server_error(e)

    if not product.is_active:
        return JsonResponse({"success": False, "message": "该产品已下架，暂不可下单"}, status=400)

    # 跨 Agent 协调 + 库存守卫（与 /api/products/ 用同一套判定，避免口径漂移）：
    # 营销 Agent 巡检发现断货会下发 pause_product，此前前台完全不读该覆盖，
    # 导致「后台显示暂停购买、客户照样下单成功」。这里落库前统一拦住。
    from agent.purchase_guard import check_buyable
    ok_buy, block_code, block_msg = check_buyable(product, quantity=quantity)
    if not ok_buy:
        if block_code == "insufficient_stock":
            return JsonResponse({
                "success": False, "message": block_msg,
                "code": "insufficient_stock",
            }, status=400)
        return JsonResponse({
            "success": False, "message": block_msg, "code": block_code,
        }, status=400)

    # 计算金额（任务书二·5：按**登录身份**取单价，与 AI 报价同一口径）
    # 改造前这里写死 retail_price，于是"AI 按批发价报价、下单按零售价结算"——
    # 客户拿着报价来下单，发现价格对不上。现在两边都走 pricing.price_for()。
    from agent import pricing
    me, _ = _resolve_request_user(request)
    unit_price = pricing.price_for(product, me, quantity)
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
    # 注：me 已在上面按身份定价时解析过，直接复用，避免二次解析出现两个身份。
    try:
        with transaction.atomic():
            order = Orders.objects.create(
                order_no=order_no,
                customer_name=customer_name,
                phone=phone,
                address=address,                          # 新增：收货地址（任务书四·4）
                expected_date=expected_date,              # 新增：期望交期（留空=尽快发货）
                user_id=me.id if me else None,
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,                  # 冗余快照（P0：便于历史追溯）
                quantity=quantity,
                unit_price=unit_price,                    # 新增：单价快照
                total_price=total_price,
                status="未发货",                          # OrderStatus.PENDING.value
                # 任务书五·1：新订单默认「未支付」。支付状态与订单状态正交 ——
                # 状态表达"货走到哪"（仍是未发货），这里表达"钱到没到"。
                # 未支付的订单不能发货，超过时限未支付会被 cancel_unpaid_orders 自动取消。
                payment_status="未支付",
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
        # 任务书二·5：回显本次成交用的是哪一档价（前端展示"经销商批发价"）
        "customer_type": pricing.customer_type_of(me),
        "price_tier": ("批发价" if unit_price == pricing.wholesale_of(product) and pricing.is_wholesale(me)
                       else "零售价"),
        "address": address,
        "expected_date": expected_date.isoformat() if expected_date else "",
        # 支付状态回显：前端据此决定「下单成功」面板显示「去支付」还是「已支付」
        "payment_status": "未支付",
        "message": "下单成功！请完成支付，我们会尽快与您联系确认发货。",
    })


# ══════════════════════════════════════════════════════════════
# 订单查询（按手机号查该手机号的订单，隐私脱敏）
# ══════════════════════════════════════════════════════════════
# 「查订单」页单次最多返回条数（超出时按 total 提示用户去「我的订单」分页看）
QUERY_PAGE_LIMIT = 50


def _err(message, code=400):
    return JsonResponse({"success": False, "message": message}, status=code)


@csrf_exempt
@require_POST
@login_required
def query_orders(request):
    """POST /api/orders/query/ — 查询「我的」订单（脱敏，不返回姓名/电话）【需登录】

    Body: {"phone": "13800138000", "order_no": "DD20260827xxxx"}（order_no 可选）

    归属规则与「我的订单」(/api/me/orders/) **完全一致**，统一走
    `agent.me_helpers.my_orders_q`：

        我的订单 = user_id == 我
                 或 (phone == 我绑定的手机号 且 user_id 为空)

    改造前这里只按 `phone=手机号` 过滤，造成两个真实问题：
      1. **同一用户两个页面看到的订单不一样** —— 实测某用户「我的」显示 6 单、
         「查订单」显示 8 单（差的 2 单是已被其它账号认领的同号订单）；
      2. **越权** —— 任何登录用户填别人的手机号即可拿到他人订单的
         物流单号、退货原因等。

    因此现在：
      · phone 只能等于**自己绑定的手机号**（未填则默认用自己的）；
        填了别人的手机号一律 403，且不透露该号码是否存在订单。
      · count 返回**真实总数**（此前用截断后的列表长度，超过 50 条会显示错）。
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    phone = (body.get("phone") or "").strip()
    order_no = (body.get("order_no") or "").strip()

    if len(phone) > 20:
        return JsonResponse({"success": False, "message": "手机号格式有误"}, status=400)

    from agent.me_helpers import get_profile, my_orders_q
    from agent.models import Orders

    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return JsonResponse(
            {"success": False, "code": "LOGIN_REQUIRED", "message": "请先登录后再查询"},
            status=401,
        )

    my_phone = (get_profile(me).phone or "").strip()
    if phone and phone != my_phone:
        # 只允许查自己的手机号；用 403 明确拒绝，且不泄露该号码是否存在订单。
        # 但要把「你绑定的是哪个号」告知调用方（这是其本人的资料，不涉隐私），
        # 否则用户会遇到"我明明绑了号却查不出来"却不知道错在哪。
        if my_phone:
            masked = my_phone[:3] + "****" + my_phone[-4:] if len(my_phone) >= 7 else my_phone
            hint = f"当前账号绑定的是 {masked}，请先用该号码查询，或到「个人中心」更换绑定。"
        else:
            hint = "当前账号还没有绑定手机号，请先到「个人中心」绑定。"
        return JsonResponse({
            "success": False,
            "code": "PHONE_NOT_BOUND",
            "message": f"只能查询当前账号绑定的手机号订单。{hint}",
            "bound_phone_masked": (my_phone[:3] + "****" + my_phone[-4:]
                                   if len(my_phone) >= 7 else my_phone),
        }, status=403)

    try:
        qs = Orders.objects.filter(my_orders_q(me)).select_related("product", "review")

        # 单订单详情查询
        if order_no:
            order = qs.filter(order_no=order_no).first()
            if not order:
                return JsonResponse(
                    {"success": False, "message": "订单不存在，或不属于当前账号"}, status=404,
                )
            return JsonResponse({
                "success": True,
                "order": _serialize_order(order, with_actions=True),
                "orders": [_serialize_order(order, with_actions=True)],
                "count": 1,
                "total": 1,
                "truncated": False,
                "phone": my_phone,
            })

        # 列出我的订单
        total = qs.count()
        rows = list(qs.order_by("-created_at", "-id")[:QUERY_PAGE_LIMIT])
        return JsonResponse({
            "success": True,
            "phone": my_phone,
            "count": len(rows),
            "total": total,
            "truncated": total > len(rows),
            "limit": QUERY_PAGE_LIMIT,
            "orders": [_serialize_order(o, with_actions=True) for o in rows],
        })
    except Exception as e:
        return _server_error(e)


# ── 订单「对外呈现」下沉到 agent/order_presenter.py（对话名片 / 个人中心 / 订单页同源）──
# 仍在**原位置以原名导出**，因此 `agent/admin.py`（`_order_actions`）、
# `agent/me_views.py`（`_serialize_order`）以及既有验证脚本
# （scripts/verify_order_payment.py、verify_reviews.py）的 `from agent.views import ...`
# 继续有效，一处都不用改。AI 对话的订单名片也共用同一套 actions 判定，避免口径漂移。
from agent.order_presenter import (  # noqa: E402
    order_actions as _order_actions,
    serialize_order as _serialize_order,
)


# ══════════════════════════════════════════════════════════════
# 查询单个订单（下单成功页查询）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@login_required
def order_detail(request, order_no):
    """GET /api/orders/<order_no>/ — 查询单个订单详情（脱敏）【需登录 + 归属校验】

    改造前用 `Orders.objects.get(order_no=...)`，**任何登录用户只要猜到/拿到
    订单号就能读到他人订单的物流单号、退货原因**。现在改为：
      · 该订单必须属于当前用户（同 `my_orders_q` 归属规则）→ 否则 404；
      · staff 可直接查看（后台排障需要）。
    返回体同时补上 `actions`（用户端可执行操作），让前端不必自己推导状态规则。
    """
    from agent.me_helpers import my_orders_q
    from agent.models import Orders

    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return JsonResponse(
            {"success": False, "code": "LOGIN_REQUIRED", "message": "请先登录后再查询"},
            status=401,
        )

    try:
        qs = Orders.objects.all()
        if not me.is_staff:
            qs = qs.filter(my_orders_q(me))
        order = qs.filter(order_no=order_no).select_related("product", "review").first()
        if order is None:
            # 不存在与无权访问统一 404：不泄露"这个订单号确实存在"
            return JsonResponse(
                {"success": False, "message": "订单不存在，或不属于当前账号"}, status=404,
            )
        return JsonResponse({
            "success": True,
            "order": _serialize_order(order, with_actions=True),
        })
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 模拟支付（任务书五·1）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def pay_order_api(request, order_no):
    """POST /api/orders/<order_no>/pay/ — 模拟支付【需登录 + 归属校验】

    本项目是毕业设计作品，**不接入真实支付渠道**（不依赖云服务器、不需要企业资质），
    因此用「模拟支付」把订单的支付环节走完整：落 `paid_at` 与
    `payment_status='已支付'` → 商家侧解锁发货。

    ⚠️ **C 端不再调用本接口**（自 #72 起）：网页上的「立即支付」全部跳到收银台
    `/pay/<order_no>/`，在那里选「扫码支付」或「余额支付」—— 支付方式的实现只在
    收银台一处，避免长出第二套口径。本接口保留为**状态机层面的直达入口**，
    供后台协助排查与自动化验收使用（`scripts/verify_order_payment.py` 靠它
    验证支付维度的状态机本身，与"用户从哪个按钮进来"无关）。

    归属校验与 `order_detail` 完全一致：只能支付自己的订单，且不存在/无权统一 404，
    不泄露"这个订单号确实存在，只是不属于你"。
    """
    from agent.me_helpers import my_orders_q
    from agent.models import Orders
    from agent.services import mark_order_paid

    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return JsonResponse(
            {"success": False, "code": "LOGIN_REQUIRED", "message": "请先登录后再支付"},
            status=401,
        )

    try:
        qs = Orders.objects.all()
        if not me.is_staff:
            qs = qs.filter(my_orders_q(me))
        order = qs.filter(order_no=order_no).select_related("product", "review").first()
        if order is None:
            return JsonResponse(
                {"success": False, "message": "订单不存在，或不属于当前账号"}, status=404,
            )

        result = mark_order_paid(order, operator=me)
        return JsonResponse({
            "success": True,
            "message": result.message,
            "order": _serialize_order(result.order, with_actions=True),
        })
    except OrderTransitionError as e:
        return JsonResponse({"success": False, "message": e.message, "code": e.code}, status=400)
    except Exception as e:
        return _server_error(e)


# ══════════════════════════════════════════════════════════════
# 商品评价（任务书六·4）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
@login_required
def create_review_api(request, order_no):
    """POST /api/orders/<order_no>/review/ — 对已完成订单发表评价【需登录 + 归属校验】

    Body: {rating: 1-5, content?: string}
      · rating  必填，1–5 星
      · content 选填，允许"只打分不写字"

    业务规则（谁能评、一单一评、评分范围）全部在 `agent.reviews` 里，
    本视图只负责「鉴权 → 拿到属于我的订单 → 转交 → 包装返回」，
    保证网页端与个人中心（DRF）两条入口的判断完全一致。

    归属校验与 `order_detail` 一致：staff 可跨用户，普通用户只能评自己的订单；
    不存在与无权统一 404，不泄露订单号是否存在。
    """
    from agent import reviews as review_helper
    from agent.me_helpers import my_orders_q
    from agent.models import Orders

    me, ok = _resolve_request_user(request)
    if not ok or me is None:
        return JsonResponse(
            {"success": False, "code": "LOGIN_REQUIRED", "message": "请先登录后再评价"},
            status=401,
        )

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    rating = body.get("rating")
    content = body.get("content") or ""

    try:
        qs = Orders.objects.all()
        if not me.is_staff:
            qs = qs.filter(my_orders_q(me))
        order = qs.filter(order_no=order_no).select_related("product", "review").first()
        if order is None:
            return JsonResponse(
                {"success": False, "message": "订单不存在，或不属于当前账号"}, status=404,
            )

        review = review_helper.create_review(
            order, rating=rating, content=content, user=me,
        )
        return JsonResponse({
            "success": True,
            "message": "评价提交成功，感谢你的反馈！",
            "review": review_helper.serialize_review(review),
            "order": _serialize_order(order, with_actions=True),
        })
    except review_helper.ReviewError as e:
        return JsonResponse({"success": False, "message": e.message, "code": e.code}, status=400)
    except Exception as e:
        return _server_error(e)


@csrf_exempt
@require_GET
def product_reviews_api(request, product_id):
    """GET /api/products/<product_id>/reviews/ — 商品评分概览与评价列表【公开】

    参数：?limit=20（1–50，默认 20）

    商品页是公开页面，评价也对外公开（姓名已脱敏），因此**不加登录校验** ——
    与 `/api/products/` 同一口径。返回 `review_count` 为真实总数、`showing` 为本次
    返回条数，两个数分开报，避免「实际 37 条却说共 20 条」的历史问题重演。
    """
    from agent import reviews as review_helper

    raw = request.GET.get("limit") or 20
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "limit 必须是整数"}, status=400)

    try:
        data = review_helper.product_reviews(product_id, limit=limit)
    except Exception as e:
        return _server_error(e)
    return JsonResponse({"success": True, "product_id": product_id, **data})


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
        # `unpaid` 是业务规则拦截（未支付不许发货），属调用方可修正的问题 → 4xx。
        # 若归到 500，营销 Agent 侧会把「这单还没付款」记成服务端故障，
        # 排查时被误导成 ZT 挂了。
        return JsonResponse({"success": False, "message": e.message, "code": e.code},
                            status=400 if e.code in ("invalid_transition", "unpaid") else 500)
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
def _serialize_product(p, inv_map=None, rating_map=None, viewer=None, mask_pricing=True):
    """产品序列化（含 SKU 与实时库存），inv_map 用于批量避免 N+1

    :param rating_map: {product_id: {"rating_avg","review_count"}}，来自
        `reviews.rating_summary()`；不传则评分字段按「暂无评价」返回。
    :param viewer: 请求者，用于按身份裁剪价格（任务书二·5）。
    :param mask_pricing: True = 按身份裁剪（前台接口）；
        False = 完整下发（**管理端必须显式传 False**，因为 `viewer=None` 也可能是未登录访客，
        不能靠"有没有 viewer"来判断该不该裁）。
    """
    from agent import pricing

    inv = (inv_map or {}).get(p.id)
    rating = (rating_map or {}).get(p.id) or {}
    data = {
        "id": p.id,
        "sku": p.sku,
        "name": p.name,
        # 产品分类（任务书二·4）：产品页「按分类筛选」用的真实字段，后台可维护
        "category": p.category or "",
        "spec": p.spec or "",
        "target_fish": p.target_fish or "",
        "description": p.description or "",
        "is_active": p.is_active,
        "stock": (inv.stock if inv else 0),
        "reserved_stock": (inv.reserved_stock if inv else 0),
        "available_stock": (inv.available_stock if inv else 0),
        "alert_line": (inv.alert_line if inv else 0),
        # 商品评价（任务书六·4）
        "rating_avg": rating.get("rating_avg", 0.0),
        "review_count": rating.get("review_count", 0),
        "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "",
        "updated_at": p.updated_at.strftime("%Y-%m-%d %H:%M:%S") if p.updated_at else "",
    }
    # 价格（任务书二·5）：统一由 pricing 下发，前台按身份裁剪
    data.update(pricing.price_fields(p, viewer, mask=mask_pricing))
    return data


@csrf_exempt
@require_http_methods(["GET", "POST"])
@staff_required
def products_collection(request):
    """GET /api/products/all/   列出全部产品（含下架）【需管理员】
    POST /api/products/all/  新建产品【需管理员】
    """
    from agent import reviews as review_helper
    from agent.models import Inventory, Products

    if request.method == "GET":
        try:
            products = list(Products.objects.all().order_by("-created_at", "-id"))
            inv_map = {i.product_id: i for i in Inventory.objects.all()}
            rating_map = review_helper.rating_summary([p.id for p in products])
            return JsonResponse({
                "success": True,
                "products": [_serialize_product(p, inv_map, rating_map, mask_pricing=False) for p in products],
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
                category=(body.get("category") or "").strip(),
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
        return JsonResponse({"success": True, "product": _serialize_product(p, mask_pricing=False)},
                            status=201)
    except Exception as e:
        return _server_error(e)


@csrf_exempt
@require_http_methods(["GET", "PATCH", "PUT", "DELETE"])
@staff_required
def product_detail_api(request, product_id):
    """GET/PATCH/PUT/DELETE /api/products/<id>/ — 单产品 CRUD【需管理员】"""
    from agent import reviews as review_helper
    from agent.models import Inventory, Products
    try:
        p = Products.objects.get(id=product_id)
    except Products.DoesNotExist:
        return JsonResponse({"success": False, "message": "产品不存在"}, status=404)

    if request.method == "GET":
        rating_map = review_helper.rating_summary([p.id])
        return JsonResponse({"success": True, "product": _serialize_product(p, None, rating_map, mask_pricing=False)})

    if request.method == "DELETE":
        # 软删除：is_active=False（保护历史订单）
        p.is_active = False
        p.save(update_fields=["is_active", "updated_at"])
        return JsonResponse({"success": True, "message": "已下架（软删除）",
                             "product": _serialize_product(p, mask_pricing=False)})

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
    for f in ("name", "category", "spec", "target_fish", "description", "is_active"):
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
        return JsonResponse({"success": True, "product": _serialize_product(p, mask_pricing=False)})
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
    from agent.models import Inventory, ProductCoordination
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
    from agent.models import ProductCoordination, Products

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
        # 记录暂停原因，决定"补货后能否自动解除"：
        #   营销侧的 pause_product 只由「可用库存 ≤ 0」触发 → 默认 stockout；
        #   补货后 services.auto_resume_stockout_pause 会自动恢复接单，
        #   否则会形成"后台库存够了、前台却永远买不了"的死锁。
        #   调用方显式传 reason=manual 时视为人工暂停，永不自动解除。
        req_reason = (body.get("reason") or "").strip().lower()
        defaults["pause_reason"] = (
            ProductCoordination.PAUSE_MANUAL if req_reason == "manual"
            else ProductCoordination.PAUSE_STOCKOUT
        )
    elif event == "resume_product":
        defaults["purchase_paused"] = False
        defaults["pause_reason"] = ""
        defaults["customer_notice"] = ""       # 恢复接单时清掉"暂时缺货"旧文案
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
            "pause_reason": obj.pause_reason,
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
def _serialize_contact_message(msg) -> dict:
    """留言序列化（列表与详情共用，含任务书六·5 的客服答复字段）。"""
    return {
        "id": msg.id, "name": msg.name, "phone": msg.phone, "email": msg.email,
        "message": msg.message, "is_read": msg.is_read,
        "reply": msg.reply or "",
        "replied_at": (msg.replied_at.strftime("%Y-%m-%d %H:%M:%S")
                       if msg.replied_at else None),
        "created_at": msg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


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
        data = [_serialize_contact_message(m) for m in rows]
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
        fields = []
        if "is_read" in body:
            msg.is_read = bool(body["is_read"])
            fields.append("is_read")
        if "reply" in body:
            # 任务书六·5：客服受理后给出答复。答复留痕 + 自动打时间戳；
            # 传空串 = 撤回答复，同时清掉答复时间，避免残留错误时间。
            reply = (body.get("reply") or "").strip()
            if reply != (msg.reply or ""):
                msg.reply = reply
                msg.replied_at = timezone.now() if reply else None
                fields += ["reply", "replied_at"]
            if reply and not msg.is_read:
                msg.is_read = True          # 有答复即视为已受理
                fields.append("is_read")
        fields = list(dict.fromkeys(fields))  # 去重，update_fields 不接受重复项
        if fields:
            msg.save(update_fields=fields)
    return JsonResponse({
        "success": True,
        "message": _serialize_contact_message(msg),
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
