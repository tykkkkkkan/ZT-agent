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
from django.views.static import serve

import os
import mimetypes
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from agent import views as views_agent
from agent import auth_views
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


def serve_frontend_asset(request, filepath):
    """服务 frontend/assets/ 下的静态资源（图片/SVG 等）。

    前端页面通过相对路径 assets/images/xxx.png 引用图片，
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
    path("api/products/<int:product_id>/", views_agent.product_detail_api, name="api_product_detail"),
    # 库存列表 + 库存调整（P0 补齐）
    path("api/inventory/", views_agent.inventory_list_api, name="api_inventory_list"),
    path("api/inventory/<int:product_id>/", views_agent.inventory_update_api, name="api_inventory_update"),
    # 订单相关
    path("api/orders/", views_agent.create_order_api, name="api_create_order"),
    # 注意：query/ 与 ship/cancel/return 三个子路径必须放在 <str:order_no>/ 之前，否则会被它抢匹配
    path("api/orders/query/", views_agent.query_orders, name="api_query_orders"),
    # 订单状态变更（核心 P0：触发库存联动）
    path("api/orders/<str:order_no>/ship/", views_agent.ship_order_api, name="api_ship_order"),
    path("api/orders/<str:order_no>/cancel/", views_agent.cancel_order_api, name="api_cancel_order"),
    path("api/orders/<str:order_no>/return/", views_agent.return_order_api, name="api_return_order"),
    path("api/orders/<str:order_no>/", views_agent.order_detail, name="api_order_detail"),
    path("index.html", serve_frontend_file, {"filename": "index.html"}),
    path("products.html", serve_frontend_file, {"filename": "products.html"}),
    path("factory.html", serve_frontend_file, {"filename": "factory.html"}),
    path("custom.html", serve_frontend_file, {"filename": "custom.html"}),
    path("contact.html", serve_frontend_file, {"filename": "contact.html"}),
    path("order.html", serve_frontend_file, {"filename": "order.html"}),
    path("order_query.html", serve_frontend_file, {"filename": "order_query.html"}),
    path("login.html", serve_frontend_file, {"filename": "login.html"}),
    path("register.html", serve_frontend_file, {"filename": "register.html"}),
    path("auth.js", serve_frontend_file, {"filename": "auth.js"}),
    path("style.css", serve_frontend_file, {"filename": "style.css"}),
    path("app.js", serve_frontend_file, {"filename": "app.js"}),
    path("assets/theme.css", serve_frontend_file, {"filename": "assets/theme.css"}),
    # 静态资源（图片/SVG 等）catch-all 路由，需放在精确路由之后
    path("assets/<path:filepath>", serve_frontend_asset, name="frontend_asset"),
    path("", homepage, name="home"),
]
