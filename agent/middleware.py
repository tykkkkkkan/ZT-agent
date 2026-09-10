"""
生产化中间件（P0 — 上线前置）：

1. RequestIDMiddleware
   为每个请求分配 / 透传 request_id，注入日志上下文与响应头，
   便于跨服务、跨日志行串联一次完整请求链路。

2. RateLimitMiddleware
   对 /api/ 与 /test-ai/ 做固定窗口限流（按 用户/IP），
   防刷接口、防 API 成本失控；计数后端为 Django cache（生产 Redis，本地 LocMem）。
"""
import contextvars
import logging
import os
import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse

logger = logging.getLogger(__name__)

# 跨调用链传递 request_id。
# 同步请求中，中间件 __call__ 与视图在同一线程内执行，contextvar 在请求生命周期内有效。
_request_id_var = contextvars.ContextVar("zt_request_id", default="-")


class RequestIDFilter(logging.Filter):
    """给每条日志记录追加 request_id 字段（取自 contextvar）。"""

    def filter(self, record):
        record.request_id = _request_id_var.get()
        return True


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.request_id = rid
        token = _request_id_var.set(rid)
        try:
            response = self.get_response(request)
        finally:
            _request_id_var.reset(token)
        response["X-Request-ID"] = rid
        return response


class RateLimitMiddleware:
    """固定窗口限流。

    规则（URL 前缀 → 次数 / 窗口秒），从 settings 读取（可经环境变量覆盖）：
      /api/agent/chat/  → RATE_CHAT_LIMIT / RATE_CHAT_WINDOW
      /api/             → RATE_API_LIMIT  / RATE_API_WINDOW
      /test-ai/         → RATE_API_LIMIT  / RATE_API_WINDOW

    排除：/api/schema/（文档）、/api/auth/login/、/api/auth/register/
          （后两者已有独立账号锁定逻辑，避免双重拦截）。

    计数键：rl:<prefix>:<client>，client 优先取已登录用户 id，否则取客户端 IP。
    计数后端：cache（生产 Redis，保证多 worker 共享；本地回退 LocMem）。
    """

    EXCLUDE_PREFIXES = ("/api/schema/", "/api/auth/login/", "/api/auth/register/")

    def __init__(self, get_response):
        self.get_response = get_response
        self.rules = [
            ("/api/agent/chat/", settings.RATE_CHAT_LIMIT, settings.RATE_CHAT_WINDOW),
            ("/api/", settings.RATE_API_LIMIT, settings.RATE_API_WINDOW),
            ("/test-ai/", settings.RATE_API_LIMIT, settings.RATE_API_WINDOW),
        ]

    @staticmethod
    def _client_key(request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return f"uid:{user.pk}"
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        ip = xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")
        return f"ip:{ip}"

    def __call__(self, request):
        path = request.path

        if any(path.startswith(p) for p in self.EXCLUDE_PREFIXES):
            return self.get_response(request)

        matched = None
        for prefix, limit, window in self.rules:
            if path.startswith(prefix):
                matched = (prefix, limit, window)
                break
        if matched is None:
            return self.get_response(request)

        prefix, limit, window = matched
        client = self._client_key(request)
        key = f"rl:{prefix}:{client}"
        now = time.time()

        # data = [窗口起点时间戳, 累计次数]
        data = cache.get(key)
        if not isinstance(data, list) or now - data[0] >= window:
            data = [now, 0]
        data[1] += 1

        if data[1] > limit:
            retry = int(window - (now - data[0])) + 1
            logger.warning(
                "限流触发 path=%s client=%s count=%s limit=%s",
                path, client, data[1], limit,
            )
            return JsonResponse(
                {"error": "请求过于频繁，请稍后再试", "retry_after": retry},
                status=429,
                headers={"Retry-After": str(retry)},
            )

        cache.set(key, data, timeout=window)
        return self.get_response(request)


# ════════════════════════════════════════════════════════════════
# 轻量级可观测性（P2 — 零第三方依赖）
# 进程内计数器：总请求数 / 错误数 / 慢请求数，配合 /metrics 端点暴露。
# 多进程（多 worker）部署时需外部聚合（如 Prometheus），此处面向单进程调试/健康观察。
# ════════════════════════════════════════════════════════════════
from collections import defaultdict
import threading

_metrics_lock = threading.Lock()
_metrics = {
    "total": 0,
    "errors": 0,
    "slow": 0,
    "by_path": defaultdict(int),
}


def get_metrics() -> dict:
    """返回当前指标快照（/metrics 端点调用）。"""
    with _metrics_lock:
        return {
            "total": _metrics["total"],
            "errors": _metrics["errors"],
            "slow": _metrics["slow"],
            "by_path": dict(_metrics["by_path"]),
        }


class MetricsMiddleware:
    """记录请求计数、错误计数与慢请求（>= SLOW_REQUEST_MS 记一次告警日志）。"""

    SLOW_THRESHOLD_MS = float(os.getenv("SLOW_REQUEST_MS", "2000"))

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.time()
        try:
            response = self.get_response(request)
        except Exception:
            with _metrics_lock:
                _metrics["errors"] += 1
            raise
        elapsed_ms = (time.time() - start) * 1000
        with _metrics_lock:
            _metrics["total"] += 1
            _metrics["by_path"][request.path] += 1
            if elapsed_ms >= self.SLOW_THRESHOLD_MS:
                _metrics["slow"] += 1
                logger.warning(
                    "慢请求 path=%s cost=%.0fms status=%s",
                    request.path, elapsed_ms, getattr(response, "status_code", "?"),
                )
        return response


def metrics_view(request):
    """Prometheus 风格文本指标端点（GET /metrics）。"""
    m = get_metrics()
    lines = [
        "# HELP zt_requests_total 总请求数",
        "# TYPE zt_requests_total counter",
        f"zt_requests_total {m['total']}",
        "# HELP zt_errors_total 视图抛异常的错误响应数",
        "# TYPE zt_errors_total counter",
        f"zt_errors_total {m['errors']}",
        f"# HELP zt_slow_requests_total 慢请求数(>={int(MetricsMiddleware.SLOW_THRESHOLD_MS)}ms)",
        "# TYPE zt_slow_requests_total counter",
        f"zt_slow_requests_total {m['slow']}",
    ]
    for path, cnt in sorted(m["by_path"].items(), key=lambda x: -x[1])[:20]:
        safe = path.replace("\\", "/").replace('"', '\\"')
        lines.append(f'zt_requests_by_path{{path="{safe}"}} {cnt}')
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")
