"""
URL configuration for config project.

Day4: 临时测试视图 /test-ai/ 用于验证 DeepSeek API 连通性。
Day5: 注册 agent app 路由 /api/agent/
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse

import os
from dotenv import load_dotenv

load_dotenv()


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


urlpatterns = [
    path("admin/", admin.site.urls),
    path("test-ai/", test_ai, name="test_ai"),
    path("api/agent/", include("agent.urls")),
]
