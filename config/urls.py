"""
URL configuration for config project.

Day4: 临时测试视图 /test-ai/
Day5: 注册 agent app 路由 /api/agent/
Day6: 托管前端 frontend/ 目录
Day8: 新增 /api/products/、/api/orders/ 直连路由（下单页用）
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse, HttpResponse, FileResponse
from django.db import connection
from django.conf import settings

import os
import mimetypes
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Windows 自带的 mimetypes 库不认识 .webp（guess_type 返回 None），
# 会让下方 serve_frontend_asset 退回 application/octet-stream，
# 浏览器据此拒绝渲染图片（表现为裂图）。显式注册该 MIME 类型。
mimetypes.add_type("image/webp", ".webp")

from agent import views as views_agent
from agent import views_cashier
from agent import auth_views
from agent import me_views
from agent.middleware import metrics_view
from agent.views_admin import admin_dashboard
from agent.manage_views import manage_login, manage_logout
from rest_framework_simplejwt.views import TokenRefreshView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

FRONTEND_DIR = Path(settings.BASE_DIR) / "frontend"


def serve_frontend_file(request, filename):
    """通用前端文件服务（HTML/CSS/JS）"""
    filepath = FRONTEND_DIR / filename
    if filepath.exists():
        if filename.endswith(".html"):
            return HttpResponse(filepath.read_text(encoding="utf-8"), content_type="text/html; charset=utf-8")
        elif filename.endswith(".css"):
            return HttpResponse(filepath.read_bytes(), content_type="text/css")
        elif filename.endswith(".js"):
            return HttpResponse(filepath.read_bytes(), content_type="application/javascript")
        return HttpResponse(filepath.read_bytes(), content_type="application/octet-stream")
    return HttpResponse("文件不存在", status=404)


def serve_cashier_page(request, order_no=""):
    """收银台页面（PC 端）。

    订单号留在 URL **路径**里而不是查询串：收银台是要刷新、要往回退、
    要贴给别人看的页面，路径更稳（也不会有 `?order=` 被截断的意外）。
    `order_no` 参数只是吃掉路径里的那段，页面自己去 `location.pathname` 解析。
    """
    return serve_frontend_file(request, "pay.html")


def serve_pay_confirm_page(request, token=""):
    """手机确认页 —— 二维码扫出来的就是这个地址，token 就在路径里。"""
    return serve_frontend_file(request, "pay_confirm.html")


def serve_frontend_asset(request, filepath):
    """服务 frontend/assets/ 下的静态资源（图片/SVG 等）。

    前端页面通过相对路径 assets/images/xxx.webp 引用图片，
    Django 需为这些文件提供路由，否则图片会 404。
    """
    base = (FRONTEND_DIR / "assets").resolve()
    full = (base / filepath).resolve()
    # 防路径穿越：解析后必须仍位于 assets/ 目录内
    if not full.is_relative_to(base) or not full.is_file():
        return HttpResponse("文件不存在", status=404)
    content_type = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
    return FileResponse(full.open("rb"), content_type=content_type)


def homepage(request):
    """首页：返回 frontend/index.html"""
    return serve_frontend_file(request, "index.html")


def test_ai(request):
    """临时测试视图：调用 DeepSeek API 返回 JSON，验证连通性。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return JsonResponse({"error": "DEEPSEEK_API_KEY 未配置"}, status=500)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": "你好，请用一句话介绍自己"}],
            temperature=0.7,
        )
        answer = resp.choices[0].message.content
        return JsonResponse({
            "status": "ok",
            "model": "deepseek-chat",
            "answer": answer,
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def healthz(request):
    """健康检查（K8s / 负载均衡探活）：校验关键依赖，返回 200/503 JSON。

    依赖判定：
      - database：必须可用，否则 503；
      - chroma_store：目录存在则 ok，缺失仅告警（仍可降级为纯 LLM 模式）；
      - redis：仅当配置了 REDIS_URL 时检查，未配置则跳过。
    """
    import os
    checks = {}
    # 1) 数据库
    try:
        connection.ensure_connection()
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["database"] = f"error: {e}"

    # 2) 向量库持久化目录
    chroma_dir = settings.BASE_DIR / "chroma_data"
    checks["chroma_store"] = "ok" if chroma_dir.exists() else "missing(optional)"

    # 3) Redis（仅当配置了 REDIS_URL）
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            from django.core.cache import cache
            cache.set("__health__", "1", 5)
            cache.get("__health__")
            checks["redis"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["redis"] = f"error: {e}"
    else:
        checks["redis"] = "not_configured(skip)"

    ok = str(checks.get("database", "")).startswith("ok")
    payload = {"status": "ok" if ok else "degraded", "checks": checks}
    return JsonResponse(payload, status=200 if ok else 503)


urlpatterns = [
    # 健康检查（K8s / 负载均衡探活）
    path("healthz", healthz, name="healthz"),
    # 轻量级指标（Prometheus 风格文本）
    path("metrics", metrics_view, name="metrics"),
    # Swagger / OpenAPI 文档
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger-ui/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # 后台数据看板（须放在 admin/ 之前，避免被 admin.site.urls 抢匹配）
    path("admin/dashboard/", admin_dashboard, name="admin_dashboard"),
    path("admin/", admin.site.urls),
    # 独立的管理员登录入口（与 /admin/login/ 分离，方便外部直接进入后台）
    path("manage/login/", manage_login, name="manage_login"),
    path("manage/logout/", manage_logout, name="manage_logout"),
    path("test-ai/", test_ai, name="test_ai"),
    path("api/agent/", include("agent.urls")),
    # 个人中心（C 端「我的」）：资料 / 改密码 / 我的订单 / 用户侧售后与收货操作
    path("api/me/profile/", me_views.MeProfileView.as_view(), name="me_profile"),
    path("api/me/password/", me_views.MePasswordView.as_view(), name="me_password"),
    path("api/me/orders/", me_views.MeOrderListView.as_view(), name="me_orders"),
    path("api/me/contact-messages/", me_views.MeContactMessageListView.as_view(),
         name="me_contact_messages"),
    path("api/me/orders/<str:order_no>/<str:action>/",
         me_views.MeOrderActionView.as_view(), name="me_order_action"),
    # 收货地址簿（C 端「我的地址」）：列表/新增 → 设为默认 → 单条编辑/删除
    # 注意：<id>/default/ 要放在 <id>/ 之前（虽然 Django 靠结尾锚点也能区分，
    # 但顺序写清楚后，将来加子路径不会踩"被通用规则抢匹配"的坑）
    # 我的余额（C 端钱包）：只读概览 + 流水；充值走模拟收银台
    path("api/me/wallet/", me_views.MeWalletView.as_view(), name="me_wallet"),
    path("api/me/wallet/recharge/", me_views.MeWalletRechargeView.as_view(),
         name="me_wallet_recharge"),
    path("api/me/addresses/", me_views.MeAddressListView.as_view(), name="me_addresses"),
    path("api/me/addresses/<int:address_id>/default/",
         me_views.MeAddressDefaultView.as_view(), name="me_address_default"),
    path("api/me/addresses/<int:address_id>/",
         me_views.MeAddressDetailView.as_view(), name="me_address_detail"),
    # 认证（JWT：注册/登录/刷新/退出/当前用户）
    path("api/auth/register/", auth_views.RegisterView.as_view(), name="auth_register"),
    path("api/auth/login/", auth_views.LoginView.as_view(), name="auth_login"),
    path("api/auth/refresh/", TokenRefreshView.as_view(), name="auth_refresh"),
    path("api/auth/logout/", auth_views.LogoutView.as_view(), name="auth_logout"),
    path("api/auth/me/", auth_views.MeView.as_view(), name="auth_me"),
    # 下单页直连 API（与 /api/agent/* 同源同一视图）
    path("api/products/", views_agent.product_list, name="api_product_list"),
    # 产品全量管理（含下架）+ 单产品 CRUD（P0 补齐）
    path("api/products/all/", views_agent.products_collection, name="api_products_all"),
    # 商品评价列表【公开】—— 必须放在 <int:product_id>/ 之前，语义更清晰（Django 也不会误匹配）
    path("api/products/<int:product_id>/reviews/", views_agent.product_reviews_api, name="api_product_reviews"),
    path("api/products/<int:product_id>/", views_agent.product_detail_api, name="api_product_detail"),
    # 库存列表 + 库存调整（P0 补齐）
    path("api/inventory/", views_agent.inventory_list_api, name="api_inventory_list"),
    path("api/inventory/<int:product_id>/", views_agent.inventory_update_api, name="api_inventory_update"),
    # L5 多智能体编排：接收侧「接收钩子」（仅营销 Agent 经 mkt_bot JWT 调）
    path("api/coordination/inbound/", views_agent.coordination_inbound_api, name="api_coordination_inbound"),
    # 订单相关
    path("api/orders/", views_agent.create_order_api, name="api_create_order"),
    # 注意：query/ 与 ship/cancel/return 三个子路径必须放在 <str:order_no>/ 之前，否则会被它抢匹配
    path("api/orders/query/", views_agent.query_orders, name="api_query_orders"),
    # 订单状态变更（核心 P0：触发库存联动）
    path("api/orders/<str:order_no>/ship/", views_agent.ship_order_api, name="api_ship_order"),
    path("api/orders/<str:order_no>/cancel/", views_agent.cancel_order_api, name="api_cancel_order"),
    path("api/orders/<str:order_no>/return/", views_agent.return_order_api, name="api_return_order"),
    # 模拟支付（任务书五·1）：C 端「立即支付」按钮。归属校验与订单详情同源。
    path("api/orders/<str:order_no>/pay/", views_agent.pay_order_api, name="api_pay_order"),
    # 商品评价（任务书六·4）：对已完成订单发表评价【需登录 + 归属校验】
    path("api/orders/<str:order_no>/review/", views_agent.create_review_api, name="api_create_review"),
    path("api/orders/<str:order_no>/", views_agent.order_detail, name="api_order_detail"),
    # ── 模拟收银台（任务书五·1）：PC 出码、手机扫码、回调入账 ──
    # 手机端两个接口**不要求登录** —— token 即授权（256 位随机、一次性、5 分钟失效），
    # 这样扫码的人不必在手机上再登录一次；PC 端两个接口要登录且做归属校验。
    path("api/pay/<str:order_no>/ticket/", views_cashier.create_ticket_api, name="api_pay_ticket"),
    path("api/pay/<str:order_no>/status/", views_cashier.ticket_status_api, name="api_pay_ticket_status"),
    # 余额支付：不出票、不扫码，登录后直接扣用户钱包（与扫码同一条订单入账出口）
    path("api/pay/<str:order_no>/balance/", views_cashier.balance_pay_api, name="api_pay_balance"),
    path("api/pay/ticket/<str:token>/", views_cashier.ticket_info_api, name="api_pay_ticket_info"),
    path("api/pay/confirm/<str:token>/", views_cashier.confirm_ticket_api, name="api_pay_confirm"),
    # 收银台页面：`/pay/confirm/<token>/` 必须排在 `/pay/<order_no>/` 之前（更具体优先）
    path("pay/confirm/<str:token>/", serve_pay_confirm_page, name="pay_confirm_page"),
    path("pay/<str:order_no>/", serve_cashier_page, name="pay_cashier_page"),
    path("index.html", serve_frontend_file, {"filename": "index.html"}),
    path("products.html", serve_frontend_file, {"filename": "products.html"}),
    path("factory.html", serve_frontend_file, {"filename": "factory.html"}),
    path("custom.html", serve_frontend_file, {"filename": "custom.html"}),
    path("contact.html", serve_frontend_file, {"filename": "contact.html"}),
    path("order.html", serve_frontend_file, {"filename": "order.html"}),
    path("order_query.html", serve_frontend_file, {"filename": "order_query.html"}),
    path("profile.html", serve_frontend_file, {"filename": "profile.html"}),
    path("login.html", serve_frontend_file, {"filename": "login.html"}),
    path("register.html", serve_frontend_file, {"filename": "register.html"}),
    path("auth.js", serve_frontend_file, {"filename": "auth.js"}),
    path("app.js", serve_frontend_file, {"filename": "app.js"}),
    path("assets/theme.css", serve_frontend_file, {"filename": "assets/theme.css"}),
    # 静态资源（图片/SVG 等）catch-all 路由，需放在精确路由之后
    path("assets/<path:filepath>", serve_frontend_asset, name="frontend_asset"),
    path("", homepage, name="home"),
]
