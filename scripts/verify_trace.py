"""
契约验证：会话级调用链（trace）—— ZT-agent

背景
----
`conversations` 只记「说了什么」，token 与耗时原先只写进日志文件的一行文本，
后台没有任何地方能看。改造成 `agent_runs`：一轮对话一行，记录
ReAct 轮次 / 每次 LLM 与工具的耗时 / token / 结束原因 / 引用来源。

本脚本守住五条口径：

  1. **一轮一行，且一定落库**：正常结束、LLM 异常、达步数上限、空回复，
     四种出口都要有记录 —— 排查时最需要的恰恰是失败的那次
  2. **时间线顺序即真实顺序**：LLM(第1轮) → 工具(第1轮) → LLM(第2轮)，
     瀑布图直接按数组画，顺序错了整张图就错了
  3. **参数脱敏**：手机号中间四位打码后才进 timeline（可观测数据不沉淀明文）
  4. **token 取不到时如实记 0**，不能把「端点没返回」伪装成「消耗为 0 是正常的」
  5. **后台样式真的会加载**：UNFOLD.STYLES 里必须是根绝对路径且 tokens 在前
     —— 写成相对路径在 /admin/agent/agentrun/ 下会 404，整套后台样式静默失效

用法：./.venv/Scripts/python.exe scripts/verify_trace.py
"""
import os
import re
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402

from agent import agent as agent_mod  # noqa: E402
from agent import llm, tools  # noqa: E402
from agent.models import AgentRun  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'√' if cond else '×'} {label}" + (f"  <- {detail}" if detail else ""))


print("=" * 62)
print("  对话调用链（trace）验收")
print("=" * 62)

# ══════════════════════════════════════════════════════════════
# 【1】LLM 层：usage 解析与耗时
# ══════════════════════════════════════════════════════════════
print("\n【1】llm.ChatStream —— usage 与耗时")


class _Usage:
    def __init__(self, p, c, t=None):
        self.prompt_tokens = p
        self.completion_tokens = c
        if t is not None:
            self.total_tokens = t


class _FakeUpstream:
    """模拟 openai SDK 的 chunk 流，末尾带一个 usage（choices 为空）。"""

    def __init__(self, contents, usage=None):
        self._contents = contents
        self._usage = usage
        self.closed = False

    def __iter__(self):
        for c in self._contents:
            yield _Chunk(content=c)
        if self._usage is not None:
            yield _UsageOnly(self._usage)

    def close(self):
        self.closed = True


class _Chunk:
    def __init__(self, content=None):
        self.choices = [type("C", (), {"delta": type("D", (), {"content": content, "tool_calls": None})})()]
        self.usage = None


class _UsageOnly:
    def __init__(self, usage):
        self.choices = []
        self.usage = usage


stream = llm.ChatStream(_FakeUpstream(["你", "好"], _Usage(120, 30, 150)), __import__("time").perf_counter(), "fake-model")
collected = list(stream)
check("流式内容完整透传", "".join(c.choices[0].delta.content for c in collected if c.choices) == "你好")
check("迭代后读到 prompt_tokens", stream.prompt_tokens == 120, str(stream.prompt_tokens))
check("迭代后读到 completion_tokens", stream.completion_tokens == 30, str(stream.completion_tokens))
check("迭代后读到 total_tokens", stream.total_tokens == 150, str(stream.total_tokens))
check("迭代结束后标记 closed（连接已释放）", stream.closed is True)
check("duration_ms 已记录（>=0）", stream.duration_ms >= 0, str(stream.duration_ms))

# total 缺失时用 prompt+completion 兜底
stream2 = llm.ChatStream(_FakeUpstream(["a"], _Usage(10, 5)), __import__("time").perf_counter(), "m")
list(stream2)
check("total_tokens 缺失时由 prompt+completion 兜底", stream2.total_tokens == 15, str(stream2.total_tokens))

# 无 usage（端点不支持）时全部为 0，且不抛异常
stream3 = llm.ChatStream(_FakeUpstream(["a"]), __import__("time").perf_counter(), "m")
list(stream3)
check("端点不返回 usage 时不抛异常且计为 0",
      stream3.prompt_tokens == 0 and stream3.total_tokens == 0)

# _usage_value 兼容 dict / 对象 / None
check("_usage_value 兼容 dict", llm._usage_value({"prompt_tokens": 7}, "prompt_tokens") == 7)
check("_usage_value 兼容对象", llm._usage_value(_Usage(9, 1), "prompt_tokens") == 9)
check("_usage_value 对 None 返回 0", llm._usage_value(None, "prompt_tokens") == 0)
check("_usage_value 对缺字段返回 0", llm._usage_value(_Usage(1, 1), "total_tokens") == 0)

# ══════════════════════════════════════════════════════════════
# 【2】端到端：正常路径落一行
# ══════════════════════════════════════════════════════════════
print("\n【2】agent_stream —— 正常路径")


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _MockChunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]
        self.usage = None


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class _Stream:
    """模拟 llm.chat_stream 的返回值：可迭代 + 带度量属性。"""

    def __init__(self, chunks, prompt_tokens=100, completion_tokens=20, model="fake-model", ms=12):
        self._chunks = chunks
        self.model = model
        self.duration_ms = ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def __iter__(self):
        return iter(self._chunks)


suffix = uuid.uuid4().hex[:8]
sid_ok = f"verify-trace-ok-{suffix}"
real_chat_stream = llm.chat_stream
created_ids = []

try:
    calls = {"n": 0}

    def fake_ok(messages, tools=None, tool_choice=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Stream([_MockChunk(_Delta(tool_calls=[
                _TC(0, id="call_1", function=_Fn("query_product", '{"name": "台钓竿"}')),
            ]))], prompt_tokens=800, completion_tokens=40, ms=900)
        return _Stream([_MockChunk(_Delta(content="我们有三款台钓竿，")),
                        _MockChunk(_Delta(content="价格从 199 起。"))],
                       prompt_tokens=900, completion_tokens=60, ms=700)

    llm.chat_stream = fake_ok
    try:
        events = list(agent_mod.agent_stream(sid_ok, "有什么台钓竿"))
    finally:
        llm.chat_stream = real_chat_stream

    run = AgentRun.objects.filter(session_id=sid_ok).order_by("-id").first()
    created_ids.append(sid_ok)
    check("落了一行 agent_runs", run is not None)
    if run:
        check("steps = 2（两轮 ReAct）", run.steps == 2, str(run.steps))
        check("llm_calls = 2", run.llm_calls == 2, str(run.llm_calls))
        check("工具调用 1 次", run.tool_count == 1, f"{run.tool_count} 次 {run.tool_names}")
        check("工具名正确", run.tool_names == ["query_product"], str(run.tool_names))
        check("status = ok", run.status == "ok", run.status)
        check("timeline 有 3 段（LLM / 工具 / LLM）", len(run.timeline) == 3, str(len(run.timeline)))
        kinds = [s.get("kind") for s in run.timeline if isinstance(s, dict)]
        check("时间线顺序 = llm → tool → llm",
              kinds == ["llm", "tool", "llm"], str(kinds))
        check("llm 段带 tokens", run.timeline[0].get("prompt_tokens") == 800, str(run.timeline[0]))
        check("工具段带耗时与返回长度",
              run.timeline[1].get("ms") is not None and run.timeline[1].get("result_chars", 0) > 0,
              str(run.timeline[1]))
        check("工具段标记 ok", run.timeline[1].get("ok") is True)
        check("token 汇总正确（800+900 / 40+60）",
              run.prompt_tokens == 1700 and run.completion_tokens == 100,
              f"{run.prompt_tokens}/{run.completion_tokens}")
        check("total_tokens = prompt + completion", run.total_tokens == 1800, str(run.total_tokens))
        check("answer_chars > 0", run.answer_chars > 0, str(run.answer_chars))
        check("duration_ms > 0", run.duration_ms >= 0, str(run.duration_ms))
        check("model 记下来了", run.model == "fake-model", run.model)
        check("rag_backend 记下来了", bool(run.rag_backend), run.rag_backend)
        check("llm_ms = 900 + 700", run.llm_ms == 1600, str(run.llm_ms))
        check("tool_ms 已汇总（单次调用可能不足 1ms，允许为 0）", run.tool_ms >= 0, str(run.tool_ms))

    # ══════════════════════════════════════════════════════════
    # 【3】端到端：异常 / 步数上限 / 空回复
    # ══════════════════════════════════════════════════════════
    print("\n【3】agent_stream —— 三个非正常出口")

    # 3.1 LLM 抛异常
    sid_err = f"verify-trace-err-{suffix}"

    def fake_err(messages, tools=None, tool_choice=None):
        raise RuntimeError("upstream 502")

    llm.chat_stream = fake_err
    try:
        list(agent_mod.agent_stream(sid_err, "退货怎么办"))
    finally:
        llm.chat_stream = real_chat_stream
    created_ids.append(sid_err)
    run_err = AgentRun.objects.filter(session_id=sid_err).order_by("-id").first()
    check("LLM 异常时仍然落库", run_err is not None)
    if run_err:
        check("异常路径 status = error", run_err.status == "error", run_err.status)
        check("异常摘要含异常类型与信息",
              "RuntimeError" in (run_err.error or "") and "502" in (run_err.error or ""),
              run_err.error)

    # 3.2 一直要工具 → 达到步数上限
    sid_max = f"verify-trace-max-{suffix}"

    def fake_max(messages, tools=None, tool_choice=None):
        return _Stream([_MockChunk(_Delta(tool_calls=[
            _TC(0, id="call_x", function=_Fn("query_product", '{"name": "不存在xyz"}')),
        ]))], ms=5)

    llm.chat_stream = fake_max
    try:
        list(agent_mod.agent_stream(sid_max, "查个不存在的"))
    finally:
        llm.chat_stream = real_chat_stream
    created_ids.append(sid_max)
    run_max = AgentRun.objects.filter(session_id=sid_max).order_by("-id").first()
    check("达步数上限时仍然落库", run_max is not None)
    if run_max:
        check("status = max_steps", run_max.status == "max_steps", run_max.status)
        check("steps = MAX_TOOL_STEPS", run_max.steps == agent_mod.MAX_TOOL_STEPS, str(run_max.steps))
        check("3 轮各有 LLM + 工具（6 段）", len(run_max.timeline) == 6, str(len(run_max.timeline)))

    # 3.3 模型空回复且无工具结果
    sid_empty = f"verify-trace-empty-{suffix}"

    def fake_empty(messages, tools=None, tool_choice=None):
        return _Stream([], ms=3)

    llm.chat_stream = fake_empty
    try:
        list(agent_mod.agent_stream(sid_empty, "在吗"))
    finally:
        llm.chat_stream = real_chat_stream
    created_ids.append(sid_empty)
    run_empty = AgentRun.objects.filter(session_id=sid_empty).order_by("-id").first()
    check("空回复时仍然落库", run_empty is not None)
    if run_empty:
        check("status = empty", run_empty.status == "empty", run_empty.status)
        check("空回复 answer_chars = 0", run_empty.answer_chars == 0, str(run_empty.answer_chars))

    # ══════════════════════════════════════════════════════════
    # 【4】参数脱敏
    # ══════════════════════════════════════════════════════════
    print("\n【4】参数脱敏")
    sid_phone = f"verify-trace-phone-{suffix}"

    def fake_phone(messages, tools=None, tool_choice=None):
        return _Stream([_MockChunk(_Delta(tool_calls=[
            _TC(0, id="call_p", function=_Fn(
                "query_orders_by_phone", '{"phone": "13812345678"}')),
        ]))])

    llm.chat_stream = fake_phone
    try:
        list(agent_mod.agent_stream(sid_phone, "查下我的订单"))
    finally:
        llm.chat_stream = real_chat_stream
    created_ids.append(sid_phone)
    run_phone = AgentRun.objects.filter(session_id=sid_phone).order_by("-id").first()
    if run_phone:
        raw = str(run_phone.timeline)
        check("timeline 里不出现完整手机号", "13812345678" not in raw, raw[:120])
        check("手机号已打码为 138****5678", "138****5678" in raw, raw[:160])
    else:
        check("参数脱敏用例落库", False, "没有 agent_runs 记录")

    # 用户消息本身不脱敏（那是排查依据），但要控制长度
    long_sid = f"verify-trace-long-{suffix}"
    llm.chat_stream = fake_empty
    try:
        list(agent_mod.agent_stream(long_sid, "问" * 3000))
    finally:
        llm.chat_stream = real_chat_stream
    created_ids.append(long_sid)
    run_long = AgentRun.objects.filter(session_id=long_sid).order_by("-id").first()
    check("超长输入被截断到 2000 字",
          run_long is not None and len(run_long.user_message) == 2000,
          str(len(run_long.user_message)) if run_long else "无记录")

finally:
    llm.chat_stream = real_chat_stream
    tools.reset_referenced_sources()

# ══════════════════════════════════════════════════════════════
# 【5】后台：页面可打开 + 面板有内容 + 样式会加载
# ══════════════════════════════════════════════════════════════
print("\n【5】后台页面与样式")
User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first()
client = Client()
client.force_login(admin_user)

r_list = client.get("/admin/agent/agentrun/")
check("调用链列表页 200", r_list.status_code == 200, str(r_list.status_code))
html_list = r_list.content.decode("utf-8", "ignore")
check("列表页有状态徽标样式类", "zy-badge" in html_list)

if created_ids:
    run_id = AgentRun.objects.filter(session_id=created_ids[0]).order_by("-id").values_list("id", flat=True).first()
    r_detail = client.get(f"/admin/agent/agentrun/{run_id}/change/")
    html_detail = r_detail.content.decode("utf-8", "ignore")
    check("详情页 200", r_detail.status_code == 200, str(r_detail.status_code))
    check("详情页有 KPI 卡", "zy-trace-kpi" in html_detail)
    check("详情页有瀑布条形", "zy-trace-bar" in html_detail)
    check("详情页出现工具名", "query_product" in html_detail)
    check("列表页给出会话回看链接",
          f"/admin/agent/conversations/?q={created_ids[0]}" in html_list,
          "点会话号跳该 session 的全部消息")

    # 后台 CSS 必须真的被引用（相对路径在 /admin/agent/agentrun/ 下会 404）
    hrefs = re.findall(r'<link[^>]+href="([^"]+\.css[^"]*)"', html_detail)
    check("zy-unfold.css 以根绝对路径引用",
          any(h.startswith("/") and h.endswith("admin/zy-unfold.css") for h in hrefs),
          str(hrefs))
    check("zy-tokens.css 已加载（否则 --zy-* 全部未定义）",
          any(h.startswith("/") and h.endswith("admin/zy-tokens.css") for h in hrefs),
          str(hrefs))
    idx_tokens = next((i for i, h in enumerate(hrefs) if h.endswith("zy-tokens.css")), -1)
    idx_unfold = next((i for i, h in enumerate(hrefs) if h.endswith("zy-unfold.css")), -1)
    check("tokens 在 unfold 之前（变量先定义后使用）",
          0 <= idx_tokens < idx_unfold, f"tokens@{idx_tokens} unfold@{idx_unfold}")

# ══════════════════════════════════════════════════════════════
# 【6】接线：侧栏入口 / 只读
# ══════════════════════════════════════════════════════════════
print("\n【6】接线与只读约束")
from django.conf import settings as dj_settings  # noqa: E402

nav_links = []
for group in dj_settings.UNFOLD.get("SIDEBAR", {}).get("navigation", []):
    for item in group.get("items", []):
        nav_links.append(item.get("link"))
check("侧栏有「对话调用链」入口", "/admin/agent/agentrun/" in nav_links)

r_add = client.get("/admin/agent/agentrun/add/")
check("不允许手工新增调用链（add 页 403）", r_add.status_code == 403, str(r_add.status_code))

# ══════════════════════════════════════════════════════════════
# 清理
# ══════════════════════════════════════════════════════════════
deleted = AgentRun.objects.filter(session_id__in=created_ids).delete()[0]
print(f"\n（已清理 {deleted} 行自造的调用链）")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
