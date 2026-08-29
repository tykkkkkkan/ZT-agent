"""
agent/llm.py — LLM 客户端统一封装（DeepSeek，OpenAI 兼容接口）

职责：
- 集中管理 API Key / base_url / model 配置（读取 Django settings 或环境变量）
- chat()：非流式调用（用于工具判定等一次性调用）
- chat_stream()：真·流式调用（stream=True），供 SSE 逐字转发

所有对 openai SDK 的依赖收敛在本模块，其它模块不直接 import openai。
"""

import os
from typing import Any, Dict, List, Optional

from django.conf import settings


def _config() -> tuple:
    key = getattr(settings, "DEEPSEEK_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
    base_url = getattr(settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = getattr(settings, "DEEPSEEK_MODEL", "deepseek-chat")
    return key, base_url, model


def _client():
    """构建 OpenAI 兼容客户端；未配置 key 时抛清晰异常。"""
    from openai import OpenAI

    key, base_url, _ = _config()
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置，请检查 .env")
    return OpenAI(api_key=key, base_url=base_url)


def chat(
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    tools: Optional[List[Dict]] = None,
    tool_choice: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 30,
):
    """非流式对话，返回 OpenAI 兼容的 completion 对象。"""
    _, _, default_model = _config()
    kwargs: Dict[str, Any] = {
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    return _client().chat.completions.create(**kwargs)


def chat_stream(
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    tools: Optional[List[Dict]] = None,
    tool_choice: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 60,
):
    """流式对话（stream=True），返回可迭代的 chunk 流。"""
    _, _, default_model = _config()
    kwargs: Dict[str, Any] = {
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    return _client().chat.completions.create(**kwargs)
