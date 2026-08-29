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
from agent.services import (
    OrderTransitionError, InsufficientStockError,
    transition_order, reserve_inventory,
)


# ════════════════════════════════════════════════════════════════
# 管理类 API 鉴权（P2）
# ════════════════════════════════════════════════════════════════
def staff_required(view_func):
    """要求 staff（后台管理员）登录。

    使用 Django 内置 SessionAuth：
    - 浏览器访问 admin 登录后，同源请求自动带 session cookie
    - 非 staff / 未登录 → 401 JSON
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated or not user.is_staff:
            return JsonResponse(
                {"success": False, "message": "需要管理员权限，请先登录后台。"},
                status=401,
            )
        return view_func(request, *args, **kwargs)
    return wrapper

# SSE 事件序列化（统一 JSON 格式）
def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ══════════════════════════════════════════════════════════════
# AI 聊天接口（SSE 真·流式）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
def chat(request):
    """AI 聊天接口：POST /api/agent/chat/（SSE 流式）"""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体必须是 JSON"}, status=400)

    user_message = (body.get("message") or "").strip()
    if not user_message:
        return JsonResponse({"error": "message 不能为空"}, status=400)

    session_id = (body.get("session_id") or "").strip() or uuid.uuid4().hex

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
            parts.append(f"⚠️ 服务出错：{e}")
            yield _sse({"content": f"⚠️ 服务出错：{e}"})

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
def history(request, session_id):
    """查询对话历史：GET /api/agent/history/<session_id>/"""
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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════
# 联系表单接口
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
def contact_submit(request):
    """联系表单接口：POST /api/agent/contact/"""
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
def custom_submit(request):
    """定制表单接口：POST /api/agent/custom/"""
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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════
# 创建订单（下单页提交）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_POST
def create_order_api(request):
    """POST /api/orders/ — 前端下单页提交订单

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
        return JsonResponse({"success": False, "message": f"查询产品出错：{e}"}, status=500)

    if not product.is_active:
        return JsonResponse({"success": False, "message": "该产品已下架，暂不可下单"}, status=400)

    # 计算金额（按零售价）
    unit_price = float(product.retail_price or 0)
    total_price = round(unit_price * quantity, 2)

    # 生成订单号：DD + 年月日 + 4 位随机数
    today = datetime.now().strftime("%Y%m%d")
    for _ in range(5):  # 极小概率重号，重试 5 次
        order_no = f"DD{today}{random.randint(1000, 9999)}"
        if not Orders.objects.filter(order_no=order_no).exists():
            break
    else:
        return JsonResponse({"success": False, "message": "订单号生成失败，请重试"}, status=500)

    # 写入订单 + 预占库存（P0 修复：事务 + 行级锁）
    try:
        with transaction.atomic():
            order = Orders.objects.create(
                order_no=order_no,
                customer_name=customer_name,
                phone=phone,
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
        return JsonResponse({"success": False, "message": f"创建订单出错：{e}"}, status=500)

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
def query_orders(request):
    """POST /api/orders/query/ — 按手机号查询该手机号的所有订单（脱敏，不返回姓名/电话）

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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


def _serialize_order(order):
    """订单脱敏序列化（不返回姓名/电话等隐私字段，含物流信息）"""
    return {
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
    }


# ══════════════════════════════════════════════════════════════
# 查询单个订单（下单成功页查询）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
def order_detail(request, order_no):
    """GET /api/orders/<order_no>/ — 查询单个订单详情（脱敏，不返回姓名/电话）"""
    from agent.models import Orders
    try:
        order = Orders.objects.get(order_no=order_no)
        return JsonResponse({"success": True, "order": _serialize_order(order)})
    except Orders.DoesNotExist:
        return JsonResponse({"success": False, "message": "订单不存在"}, status=404)
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


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
            return JsonResponse({"success": False, "message": str(e)}, status=500)

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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


# ══════════════════════════════════════════════════════════════
# 库存 CRUD（P0 补齐）
# ══════════════════════════════════════════════════════════════
@csrf_exempt
@require_GET
@staff_required
def inventory_list_api(request):
    """GET /api/inventory/ — 库存列表（含产品信息）【需管理员】"""
    from agent.models import Products, Inventory
    try:
        items = []
        for inv in Inventory.objects.select_related("product").order_by("product_id"):
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
            })
        return JsonResponse({"success": True, "inventory": items})
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


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
        inv = Inventory.objects.select_for_update().get(product_id=product_id)
    except Inventory.DoesNotExist:
        return JsonResponse({"success": False, "message": "该产品无库存记录"}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "请求体必须是 JSON"}, status=400)

    with transaction.atomic():
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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


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
        return JsonResponse({"success": False, "message": str(e)}, status=500)


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
