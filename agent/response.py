"""agent/response.py — 统一响应格式构造器

统一 API 响应为 {code, message, data} 结构：
- code = 0 表示成功；非 0 表示错误（取 HTTP 状态码或业务码）
- message = 人类可读的提示文案
- data = 业务数据（可为 None）

使用示例：
    from agent.response import ok, fail
    return ok(data={"products": [...]})
    return fail("产品不存在", code=404)

约定与边界：
1. SSE 流式接口（/api/agent/chat/）不适用此结构，保持 SSE 事件流格式。
2. 为兼容现有前端（依赖 success/message 字段），已有接口在迁移时可在
   data 之外附带 success 字段；新接口一律用本构造器。
"""
from django.http import JsonResponse


def ok(data=None, message="success"):
    """成功响应：{code: 0, message, data}。"""
    return JsonResponse({"code": 0, "message": message, "data": data})


def fail(message, code=400, status=None, data=None):
    """失败响应：{code, message, data}，HTTP 状态码默认 = code。"""
    http_status = status if status is not None else code
    return JsonResponse(
        {"code": code, "message": message, "data": data},
        status=http_status,
    )
