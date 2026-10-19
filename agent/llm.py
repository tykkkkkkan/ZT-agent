"""
agent/llm.py — LLM 客户端统一封装（DeepSeek，OpenAI 兼容接口）

职责：
- 集中管理 API Key / base_url / model 配置（读取 Django settings 或环境变量）
- chat()：非流式调用（用于工具判定等一次性调用）
- chat_stream()：真·流式调用（stream=True），供 SSE 逐字转发
- 统一承担超时、指数退避重试、token 用量与耗时记录（对齐项目 LLM 调用规范）

所有对 openai SDK 的依赖收敛在本模块，其它模块不直接 import openai。
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from django.conf import settings

logger = logging.getLogger(__name__)

# 重试次数与退避基数：失败后按 0.5s、1s、2s…指数退避，耗尽才抛出
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BASE_DELAY = float(os.getenv("LLM_RETRY_BASE_DELAY", "0.5"))


def _max_tokens() -> int:
    """单次调用最大输出 token 数。

    规范要求每次调用显式传入 max_tokens，避免模型返回超长内容导致
    费用与响应时间失控；默认 2048（客服问答场景足够），可经环境变量调整。
    """
    return int(getattr(settings, "LLM_MAX_TOKENS", 0) or os.getenv("LLM_MAX_TOKENS", "2048"))


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


def _retry(call_name: str, fn):
    """带指数退避的重试执行；重试耗尽后抛出最后一次异常。

    只打印异常类型与摘要，不打印 prompt 正文（日志脱敏约定）。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 需对任何网络/服务端异常统一退避重试
            last_exc = exc
            if attempt >= LLM_MAX_RETRIES:
                break
            delay = LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "%s 第 %d/%d 次调用失败（%s: %s），%.1fs 后重试",
                call_name, attempt, LLM_MAX_RETRIES, type(exc).__name__, exc, delay,
            )
            time.sleep(delay)
    logger.error("%s 重试耗尽：%s: %s", call_name, type(last_exc).__name__, last_exc)
    raise last_exc  # type: ignore[misc]


def _log_usage(call_name: str, resp: Any, started: float) -> None:
    """记录 token 消耗与耗时（对账用）；usage 缺失时记为 unknown。"""
    usage = getattr(resp, "usage", None)
    logger.info(
        "%s 完成 耗时=%.0fms prompt_tokens=%s completion_tokens=%s total_tokens=%s",
        call_name,
        (time.perf_counter() - started) * 1000,
        getattr(usage, "prompt_tokens", "unknown"),
        getattr(usage, "completion_tokens", "unknown"),
        getattr(usage, "total_tokens", "unknown"),
    )


def chat(
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    tools: Optional[List[Dict]] = None,
    tool_choice: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 30,
    max_tokens: Optional[int] = None,
):
    """非流式对话，返回 OpenAI 兼容的 completion 对象。

    显式传入 model / temperature / timeout / max_tokens（项目 LLM 规范要求），
    失败按指数退避重试，成功/失败均记录耗时与 token 用量。
    """
    _, _, default_model = _config()
    kwargs: Dict[str, Any] = {
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        "timeout": timeout,
        "max_tokens": max_tokens or _max_tokens(),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    started = time.perf_counter()
    resp = _retry("llm.chat", lambda: _client().chat.completions.create(**kwargs))
    _log_usage("llm.chat", resp, started)
    return resp


def _usage_value(usage: Any, field_name: str) -> int:
    """从 usage 对象/字典里取一个计数字段，取不到返回 0。

    不同兼容端点返回形态不一（OpenAI SDK 对象 / 裸 dict / 直接省略），
    这里统一收口，避免上层到处写 getattr。
    """
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(field_name)
    else:
        value = getattr(usage, field_name, None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class ChatStream:
    """流式响应包装：透传 chunk，同时把 usage 与耗时留在对象上。

    为什么需要它：原来是纯生成器，`for chunk in stream` 结束后什么都不剩，
    token 用量只在日志里一行文本 —— 想统计"这一轮花了多少"只能去 grep 日志。
    现在迭代结束后读 `stream.total_tokens` / `duration_ms` 即可。

    为什么不在 llm 层直接写库：llm 不知道 session_id，也不该知道。
    它只负责"把度量挂出来"，记到哪由 agent 层决定。
    """

    def __init__(self, stream: Any, started: float, model: str) -> None:
        self._stream = stream
        self._started = started
        self.model = model
        self.usage: Any = None
        self.duration_ms: int = 0
        self.closed = False

    def __iter__(self):
        try:
            for chunk in self._stream:
                # 流式 usage 在最后一个 chunk（choices 为空）里，此处顺手接住
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    self.usage = usage
                yield chunk
        finally:
            self.duration_ms = int((time.perf_counter() - self._started) * 1000)
            logger.info(
                "llm.chat_stream 结束 耗时=%dms prompt_tokens=%d completion_tokens=%d",
                self.duration_ms, self.prompt_tokens, self.completion_tokens,
            )
            self.close()

    def close(self) -> None:
        """释放连接（提前中断时生成器的 finally 也会调到这里）。"""
        if self.closed:
            return
        self.closed = True
        close = getattr(self._stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响已产出的内容
                logger.debug("llm.chat_stream 关闭流失败：%s", exc)

    @property
    def prompt_tokens(self) -> int:
        return _usage_value(self.usage, "prompt_tokens")

    @property
    def completion_tokens(self) -> int:
        return _usage_value(self.usage, "completion_tokens")

    @property
    def total_tokens(self) -> int:
        total = _usage_value(self.usage, "total_tokens")
        # 部分端点只给 prompt/completion，total 缺省时自己加，避免记成 0
        return total or (self.prompt_tokens + self.completion_tokens)


def _open_stream(kwargs: Dict[str, Any], want_usage: bool):
    """建立流式连接；首次带 stream_options，端点不认就退回不带。

    为什么要试两次：`stream_options={"include_usage": True}` 是 OpenAI 标准参数，
    DeepSeek 支持，但本地/第三方兼容实现不一定认。不认得就直接 400，
    而对话主流程不该因为"想多记一个 token 数"而整体不可用。
    此时连接尚未产出内容，重试一次是安全的（不会重复输出）。
    """
    if want_usage:
        try:
            return _client().chat.completions.create(
                **kwargs, stream_options={"include_usage": True}
            )
        except Exception as exc:  # noqa: BLE001 - 参数不被支持 / 网络抖动都退回
            logger.debug("端点不接受 stream_options，退回不带 usage 统计：%s", exc)
    return _client().chat.completions.create(**kwargs)


def chat_stream(
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    tools: Optional[List[Dict]] = None,
    tool_choice: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 60,
    max_tokens: Optional[int] = None,
) -> ChatStream:
    """流式对话（stream=True），返回 ChatStream（可直接 for 迭代）。

    建立连接阶段失败会退避重试；已开始产出后不再重试（避免重复内容）。
    迭代结束后可从返回对象上读到 usage / duration_ms —— 调用链（trace）依赖它。
    """
    _, _, default_model = _config()
    used_model = model or default_model
    kwargs: Dict[str, Any] = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "timeout": timeout,
        "max_tokens": max_tokens or _max_tokens(),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    started = time.perf_counter()
    want_usage = os.getenv("LLM_STREAM_USAGE", "1").strip().lower() not in ("0", "false", "no")
    stream = _retry("llm.chat_stream", lambda: _open_stream(kwargs, want_usage))
    return ChatStream(stream, started, used_model)
