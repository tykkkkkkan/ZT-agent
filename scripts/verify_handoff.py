"""
契约验证：转人工工单（handoff）—— ZT-agent

背景
----
「AI 解决不了怎么办」这一环原先只有 `guide_to_contact`（引导顾客去留言），
等于让顾客把问题**重新描述一遍**，前面跟 AI 说的十几轮全丢了。
工单的核心不是一张待办表，而是**上下文交接**：顾客说过什么、AI 查过哪些
商品/订单、知识库命中没有 —— 坐席打开就能接话。

本脚本守住七条口径：

  1. **工单必须带快照**：最近若干轮对话 + 本轮工具痕迹 + 命中的知识条目 +
     涉及的商品/订单。没有这些，转人工就退化成"再问一遍"
  2. **单号可口头播报**：`T20261019-0007`，由自增 pk 生成，并发不撞号
  3. **状态流转唯一出口**：三个时间戳（分配/首响/解决）由 `apply_transition`
     统一维护 —— 否则「状态已回复但首响时间为空」，客服 KPI 直接失真
  4. **重新打开要清解决时间**：否则工单「已解决还挂着」，首响时长被永久定格
  5. **顾客点按钮和 AI 主动转出产出同一种单**：复用同一个工具，不写两套
  6. **不重复建单**：本会话已有未完结工单时返回那一张，连点两下不会多出两张
  7. **对顾客只下发能看的字段**：对话快照/内部备注绝不出现在接口响应里

用法：./.venv/Scripts/python.exe scripts/verify_handoff.py
"""
import json
import os
import re
import sys
import time
import uuid
from datetime import timedelta

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib import admin as dj_admin  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402
from django.utils import timezone  # noqa: E402

from agent import agent as agent_mod  # noqa: E402
from agent import llm, tools  # noqa: E402
from agent.models import Conversations, SupportTicket  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'√' if cond else '×'} {label}" + (f"  <- {detail}" if detail else ""))


print("=" * 62)
print("  转人工工单验收")
print("=" * 62)

# ══════════════════════════════════════════════════════════════
# 【1】模型约束
# ══════════════════════════════════════════════════════════════
print("\n【1】模型约束")
meta = SupportTicket._meta
check("表名为 support_tickets", meta.db_table == "support_tickets", meta.db_table)
check("默认状态 = 待受理",
      meta.get_field("status").default == SupportTicket.Status.OPEN,
      str(meta.get_field("status").default))
check("四种优先级齐备",
      [c[0] for c in SupportTicket.Priority.choices] == ["low", "normal", "high", "urgent"],
      str(SupportTicket.Priority.choices))
check("SLA 按优先级分档",
      SupportTicket.SLA_MINUTES == {"urgent": 15, "high": 30, "normal": 120, "low": 480},
      str(SupportTicket.SLA_MINUTES))
check("会话快照 / 工具痕迹 / 来源都是 JSON 字段",
      all(meta.get_field(f).get_internal_type() == "JSONField"
          for f in ("transcript", "tool_trace", "sources",
                    "related_orders", "related_products")))

# ══════════════════════════════════════════════════════════════
# 【2】造数据
# ══════════════════════════════════════════════════════════════
User = get_user_model()
user = User.objects.filter(is_superuser=False).first() or User.objects.filter(is_superuser=True).first()
admin_user = User.objects.filter(is_superuser=True).first()

TAG = "VERIFY_HANDOFF_"
SUF = uuid.uuid4().hex[:8]
SID = f"vh-{SUF}"
SID2 = f"vh2-{SUF}"

for role, text in (
    ("user", TAG + "我收到的鱼竿竿稍是断的"),
    ("assistant", TAG + "很抱歉，这个需要人工核实一下"),
    ("user", TAG + "你们怎么赔"),
):
    Conversations.objects.create(session_id=SID, role=role, content=text)

# ══════════════════════════════════════════════════════════════
# 【3】工具：上下文快照
# ══════════════════════════════════════════════════════════════
print("\n【3】handoff_to_human —— 快照与单号")
tools.set_current_user(user)
tools.set_current_session(SID)
tools.reset_handoff()

# 先塞一点「本轮查过的东西」，验证会被带进工单。
# 注意：get_referenced_products() 返回的是**副本**（list(...)），改它不会影响
# ContextVar —— 这里直接写 ContextVar，模拟工具登记后的状态。
tools._referenced_products.set([{"id": 1, "name": "红虫颗粒"}])
tools._referenced_orders.set([{"order_no": "ZY20261019000001", "status": "已支付"}])
tools._referenced_sources.set([{"title": "售后政策", "score": 1.2}])

text = tools.handoff_to_human(reason="after_sale", detail="顾客说竿稍断裂，要求补发")
ticket = tools.drain_handoff()

check("工具返回里带工单号", bool(ticket) and ticket.ticket_no in text, text[:60])
check("工具要求「不要让顾客重复描述」", "不要" in text and "重复" in text, text[-40:])
check("drain 之后不再返回（一张单只发一次卡片）", tools.drain_handoff() is None)

check("工单已落库", ticket is not None and SupportTicket.objects.filter(pk=ticket.pk).exists())
if ticket:
    check("单号格式 T<8位日期>-<4位序号>",
          bool(re.fullmatch(r"T\d{8}-\d{4,}", ticket.ticket_no or "")), str(ticket.ticket_no))
    check("售后自动提优先级为「高」",
          ticket.priority == SupportTicket.Priority.HIGH, ticket.priority)
    check("来源标记为 AI 主动转出",
          ticket.source == SupportTicket.Source.AGENT, ticket.source)
    check("对话快照带了 3 条消息",
          ticket.transcript_count == 3, str(ticket.transcript_count))
    check("快照按时间正序（第一条是最早的提问）",
          (ticket.transcript[0].get("content") or "").startswith(TAG + "我收到的"),
          str(ticket.transcript[:1]))
    check("问题取最后一条用户消息",
          ticket.question.startswith(TAG + "你们怎么赔"), ticket.question)
    check("带上了本轮查过的商品", "红虫颗粒" in ticket.related_products,
          str(ticket.related_products))
    check("带上了本轮查过的订单",
          "ZY20261019000001" in ticket.related_orders, str(ticket.related_orders))
    check("带上了知识库命中记录",
          any(s.get("title") == "售后政策" for s in ticket.sources), str(ticket.sources))
    check("带上了工具痕迹（商品 → query_product）",
          any(c.get("name") == "query_product" for c in ticket.tool_trace),
          str(ticket.tool_trace))
    check("detail 原样留给人工", "竿稍断裂" in ticket.detail, ticket.detail)

# 非法 reason 要归一到 unknown，而不是报错或写脏值
tools.reset_handoff()
tools.handoff_to_human(reason="我想静静", detail="")
t2 = tools.drain_handoff()
check("非法 reason 归一到 unknown",
      t2 is not None and t2.reason == SupportTicket.Reason.UNKNOWN,
      getattr(t2, "reason", "无单"))

# 没有会话上下文也不能崩（比如从后台/脚本调用）
tools.set_current_session("")
tools.reset_handoff()
tools.handoff_to_human(reason="other", detail="无会话上下文")
t3 = tools.drain_handoff()
check("没有 session 时不崩、快照为空",
      t3 is not None and t3.transcript == [], str(getattr(t3, "transcript", "无单")))

created_sids = [SID, SID2]

# ══════════════════════════════════════════════════════════════
# 【4】状态流转与三个时间戳
# ══════════════════════════════════════════════════════════════
print("\n【4】apply_transition —— 时间戳唯一出口")
t = SupportTicket.objects.create(session_id=SID, question="测试流转",
                                 reason=SupportTicket.Reason.OTHER)
check("新建单：未分配、首响为空、未解决",
      t.assigned_at is None and t.first_reply_at is None and t.resolved_at is None)
check("新建单 waiting_minutes >= 0", t.waiting_minutes >= 0, str(t.waiting_minutes))
check("新建单 first_response_minutes 为 None（0 和「还没响」是两件事）",
      t.first_response_minutes is None, str(t.first_response_minutes))

t.apply_transition(assignee=admin_user, status=SupportTicket.Status.ASSIGNED)
t.refresh_from_db()
check("认领后状态 = 已分配", t.status == SupportTicket.Status.ASSIGNED, t.status)
check("认领后 assigned_at 有值", t.assigned_at is not None)
check("认领即算首响（不要求必须点「标记已回复」）",
      t.first_reply_at is not None)
check("首响时长可算出来", t.first_response_minutes == 0, str(t.first_response_minutes))

first_reply = t.first_reply_at
t.apply_transition(status=SupportTicket.Status.RESOLVED)
t.refresh_from_db()
check("解决后 resolved_at 有值", t.resolved_at is not None)
check("解决后 waiting 不再增长（按解决时间算）",
      t.waiting_minutes == max(int((t.resolved_at - t.created_at).total_seconds() // 60), 0))
check("已解决不算超时", t.is_overdue is False)

t.apply_transition(status=SupportTicket.Status.OPEN)
t.refresh_from_db()
check("重新打开后 resolved_at 被清空（否则「已解决还挂着」）",
      t.resolved_at is None)
check("重新打开不清首响时间（顾客确实被回过）",
      t.first_reply_at == first_reply)

# 超时判定
overdue = SupportTicket.objects.create(session_id=SID2, question="超时测试",
                                       priority=SupportTicket.Priority.LOW)
SupportTicket.objects.filter(pk=overdue.pk).update(
    created_at=timezone.now() - timedelta(minutes=SupportTicket.SLA_MINUTES["low"] + 30))
overdue.refresh_from_db()
check("超过 SLA 且未回复 → is_overdue", overdue.is_overdue is True,
      f"waiting={overdue.waiting_minutes} sla={overdue.sla_minutes}")
check("已回复的不算超时", t.is_overdue is False)

# ══════════════════════════════════════════════════════════════
# 【5】接口：/api/agent/handoff/
# ══════════════════════════════════════════════════════════════
print("\n【5】POST /api/agent/handoff/")
URL = "/api/agent/handoff/"
client = Client()
client.force_login(user)
admin_client = Client()
admin_client.force_login(admin_user)

r = Client().post(URL, data=json.dumps({"session_id": SID}),
                  content_type="application/json")
check("未登录不能转人工", r.status_code in (302, 401, 403), f"HTTP {r.status_code}")

r = client.get(URL)
check("GET 被拒（只接受 POST）", r.status_code == 405, f"HTTP {r.status_code}")

r = client.post(URL, data="bad json", content_type="application/json")
check("非 JSON → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({}), content_type="application/json")
check("缺 session_id → 400", r.status_code == 400, f"HTTP {r.status_code}")

# 先把之前工具造的未完结单清掉，确保这次走「新建」
SupportTicket.objects.filter(session_id=SID).delete()

r = client.post(URL, data=json.dumps({"session_id": SID}), content_type="application/json")
body = r.json() if r.status_code == 200 else {}
check("正常转人工 200", r.status_code == 200, f"HTTP {r.status_code} {body.get('message', '')[:40]}")
check("返回 reused=False（是新建）", body.get("reused") is False, str(body.get("reused")))
payload = body.get("ticket") or {}
check("返回工单号", bool(payload.get("ticket_no")), str(payload)[:60])
check("返回受理角色与承诺时限",
      bool(payload.get("owner")) and isinstance(payload.get("sla_minutes"), int),
      str(payload)[:80])
check("返回值只有给顾客看的字段（不含对话快照/工具痕迹）",
      "transcript" not in payload and "tool_trace" not in payload,
      str(list(payload.keys())))

t_api = SupportTicket.objects.filter(session_id=SID).order_by("-id").first()
check("接口转出的单也带对话快照（复用同一个工具）",
      t_api is not None and t_api.transcript_count == 3,
      str(getattr(t_api, "transcript_count", None)))
check("接口转出的单来源 = 后台/AI 之外的单一路径（不写成 admin）",
      t_api is not None and t_api.source in (SupportTicket.Source.AGENT,
                                             SupportTicket.Source.USER),
      getattr(t_api, "source", None))

n_before = SupportTicket.objects.filter(session_id=SID).count()
r = client.post(URL, data=json.dumps({"session_id": SID}), content_type="application/json")
body2 = r.json()
check("重复点不新建单（返回已有那张）", body2.get("reused") is True, str(body2.get("reused")))
check("重复点后总数不变",
      SupportTicket.objects.filter(session_id=SID).count() == n_before,
      str(SupportTicket.objects.filter(session_id=SID).count()))

# 已解决之后再点 → 应该新建（新问题值得新单）
t_api.apply_transition(status=SupportTicket.Status.RESOLVED)
r = client.post(URL, data=json.dumps({"session_id": SID}), content_type="application/json")
check("上一张已解决后再点 → 新建（reused=False）",
      r.json().get("reused") is False, str(r.json().get("reused")))

# ══════════════════════════════════════════════════════════════
# 【6】端到端：SSE 里必须出现 handoff_card，且不再推引导卡
# ══════════════════════════════════════════════════════════════
print("\n【6】agent_stream —— handoff_card 事件")


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
    def __init__(self, chunks, ms=10):
        self._chunks = chunks
        self.model = "fake-model"
        self.duration_ms = ms
        self.prompt_tokens = 100
        self.completion_tokens = 20
        self.total_tokens = 120

    def __iter__(self):
        return iter(self._chunks)


SID3 = f"vh3-{SUF}"
for role, text in (("user", TAG + "我要投诉，收到的货少了两包"),):
    Conversations.objects.create(session_id=SID3, role=role, content=text)

real_chat_stream = llm.chat_stream
try:
    calls = {"n": 0}

    def fake(messages, tools=None, tool_choice=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Stream([_MockChunk(_Delta(tool_calls=[
                _TC(0, id="call_h1", function=_Fn(
                    "handoff_to_human",
                    '{"reason": "complaint", "detail": "顾客反馈少件"}')),
            ]))])
        # 第二轮回答里刻意带上"联系我们"，验证不会再推引导卡
        return _Stream([_MockChunk(_Delta(content="已为您登记，工单号稍后客服会联系您。"))])

    llm.chat_stream = fake
    try:
        events = list(agent_mod.agent_stream(SID3, "我要投诉，收到的货少了两包"))
    finally:
        llm.chat_stream = real_chat_stream
    created_sids.append(SID3)

    types = [e.get("type") for e in events if e.get("type")]
    check("发出了 handoff_card 事件", "handoff_card" in types, str(types))
    card = next((e for e in events if e.get("type") == "handoff_card"), None)
    check("卡片带工单号", bool(card and card["ticket"].get("ticket_no")),
          str(card)[:80] if card else "无卡片")
    check("卡片不含对话快照（隐私）",
          bool(card) and "transcript" not in card["ticket"],
          str(list(card["ticket"].keys())) if card else "无卡片")
    check("转人工后不再推 guide_card（两个动作互相抵消）",
          "guide_card" not in types, str(types))
    check("同一轮只发一张工单卡", types.count("handoff_card") == 1, str(types))

    t3_created = SupportTicket.objects.filter(session_id=SID3).first()
    check("端到端确实建了单", t3_created is not None)
    check("投诉类自动提高优先级",
          t3_created is not None and t3_created.priority == SupportTicket.Priority.HIGH,
          getattr(t3_created, "priority", None))
finally:
    llm.chat_stream = real_chat_stream
    tools.reset_handoff()

# ══════════════════════════════════════════════════════════════
# 【7】后台
# ══════════════════════════════════════════════════════════════
print("\n【7】后台工单页")
LIST = "/admin/agent/supportticket/"
r = admin_client.get(LIST)
check("工单列表页 200", r.status_code == 200, f"HTTP {r.status_code}")
html_list = r.content.decode("utf-8", "ignore")
check("顶部 KPI 条已渲染", "zy-kpi-bar" in html_list)
check("KPI 含「超时未接」入口", "超时未接" in html_list)
check("列表显示工单号",
      bool(t3_created and t3_created.ticket_no in html_list),
      getattr(t3_created, "ticket_no", ""))
check("未完结筛选可用", "progress=pending" in html_list)

detail_id = t3_created.pk if t3_created else (ticket.pk if ticket else None)
r = admin_client.get(f"{LIST}{detail_id}/change/")
check("详情页 200", r.status_code == 200, f"HTTP {r.status_code}")
html_detail = r.content.decode("utf-8", "ignore")
check("详情页有对话快照区", "zy-tk-thread" in html_detail)
check("快照里能看到顾客原话", "少了两包" in html_detail or "竿稍是断的" in html_detail)
check("详情页有工具调用区", "工具调用" in html_detail)

# 类名写对了不等于样式能加载：后台曾因 UNFOLD.STYLES 相对路径 404 导致整套
# 样式静默失效（页面能开、但像没穿衣服）。
# 这里用 staticfiles finder 定位真实文件（test client 不走 runserver 的静态处理器，
# 直接 GET /static/... 会 404，那测的是环境不是事实），再读内容确认规则在里面。
from django.contrib.staticfiles import finders  # noqa: E402

css_path = finders.find("admin/zy-admin-ops.css")
check("工单页 CSS 能被 staticfiles 定位到（不是只有类名）", bool(css_path), str(css_path))
if css_path:
    css_text = open(css_path, encoding="utf-8").read()
    for cls in (".zy-tk-thread", ".zy-tk-msg", ".zy-badge-mute", ".zy-kpi-warn"):
        check(f"CSS 里确实定义了 {cls}", cls in css_text)

# 快照里的换行必须渲染成真的换行；把带 <br> 的字符串直接塞进 format_html
# 会被转义成 &lt;br&gt;，页面上看到的是字面量（这个坑踩过一次）
multi = SupportTicket.objects.create(
    session_id=SID2, question="换行渲染测试",
    transcript=[{"role": "user", "content": "第一行\n第二行", "at": "2026-10-19 10:00"}],
)
r = admin_client.get(f"{LIST}{multi.pk}/change/")
h_multi = r.content.decode("utf-8", "ignore")
check("快照换行渲染为 <br>（不是字面量 &lt;br&gt;）",
      "第一行<br>第二行" in h_multi and "第一行&lt;br&gt;第二行" not in h_multi)
multi.delete()

# 行内认领
r = admin_client.post(reverse("admin:agent_supportticket_claim"),
                      data=json.dumps({"id": detail_id}), content_type="application/json")
check("行内认领成功", r.status_code == 200 and r.json().get("success") is True,
      f"HTTP {r.status_code}")
claimed = SupportTicket.objects.get(pk=detail_id)
check("认领后受理人 = 当前管理员", claimed.assignee_id == admin_user.id, str(claimed.assignee_id))
check("认领后状态 = 已分配", claimed.status == SupportTicket.Status.ASSIGNED, claimed.status)

# 别人再认领 → 409（不能抢单）
# 用临时 staff 账号：普通用户连 admin 都进不去（会被 302 到登录页），
# 那样测的就不是「抢单被拒」而是「没有后台权限」，两件事不能混为一谈
other = User.objects.create_user(
    username=f"vh_staff_{SUF}", password="verify-only-tmp", is_staff=True)
try:
    c2 = Client()
    c2.force_login(other)
    r = c2.post(reverse("admin:agent_supportticket_claim"),
                data=json.dumps({"id": detail_id}), content_type="application/json")
    check("已被认领的单不能被别人抢（409）", r.status_code == 409, f"HTTP {r.status_code}")
    check("抢单失败后受理人没被改掉",
          SupportTicket.objects.get(pk=detail_id).assignee_id == admin_user.id,
          str(SupportTicket.objects.get(pk=detail_id).assignee_id))
finally:
    other.delete()

# 批量动作
r = admin_client.post(LIST, {
    "action": "resolve_tickets",
    "_selected_action": [str(detail_id)],
    "select_across": "0",
    "index": "0",
}, follow=True)
claimed.refresh_from_db()
check("批量「标记为已解决」生效", claimed.status == SupportTicket.Status.RESOLVED, claimed.status)
check("解决时间已写入", claimed.resolved_at is not None)

r = admin_client.get("/admin/agent/supportticket/add/")
check("允许后台手建工单（如电话来访）", r.status_code == 200, f"HTTP {r.status_code}")

from django.conf import settings as dj_settings  # noqa: E402
nav_links = [i.get("link") for g in dj_settings.UNFOLD.get("SIDEBAR", {}).get("navigation", [])
             for i in g.get("items", [])]
check("侧栏有「转人工工单」入口", "/admin/agent/supportticket/" in nav_links, "未找到")

# ══════════════════════════════════════════════════════════════
# 【8】工具注册与提示词
# ══════════════════════════════════════════════════════════════
print("\n【8】工具注册与提示词口径")
check("handoff_to_human 已进实现注册表", "handoff_to_human" in tools.TOOL_REGISTRY)
schema_names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
check("Schema 与实现一一对应（漏注册会在这里报）", schema_names == set(tools.TOOL_REGISTRY),
      str(schema_names ^ set(tools.TOOL_REGISTRY)))
schema = next((s for s in tools.TOOL_SCHEMAS
               if s["function"]["name"] == "handoff_to_human"), None)
check("required 只要求 reason（其余可省，别让模型卡在参数上）",
      schema is not None and schema["function"]["parameters"].get("required") == ["reason"],
      str(schema and schema["function"]["parameters"].get("required")))
check("reason 给了枚举（避免模型自由发挥写出脏值）",
      schema is not None and "enum" in schema["function"]["parameters"]["properties"]["reason"])

prompt = open(os.path.join(BASE, "prompts", "system_prompt_agent.md"), encoding="utf-8").read()
check("提示词写了何时该转人工", "handoff_to_human" in prompt and "转人工" in prompt)
check("提示词区分了「成交引导」与「异常兜底」",
      "guide_to_contact" in prompt and "异常兜底" in prompt)
check("提示词要求不要把工单信息展开成内部标记",
      "不要再让顾客重复描述" in prompt)

# ══════════════════════════════════════════════════════════════
# 【9】前端接线
# ══════════════════════════════════════════════════════════════
print("\n【9】前端接线")
app_js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()
html = open(os.path.join(BASE, "frontend", "index.html"), encoding="utf-8").read()
css = open(os.path.join(BASE, "frontend", "assets", "theme.css"), encoding="utf-8").read()

check("app.js 调用 /api/agent/handoff/", "/api/agent/handoff/" in app_js)
check("app.js 处理 handoff_card 事件", "handoff_card" in app_js)
check("转人工前要求登录", "AUTH.ensureLogin('登录后即可转人工')" in app_js)
check("提交中禁用（防连点重复建单）", "handoffSending" in app_js)
check("index.html 有转人工入口", "handoff-bar" in html)
check("index.html 有工单卡分支", "cardType === 'handoff'" in html)
check("工单卡显示工单号", "msg.ticket.ticket_no" in html)
check("没有电话时提示补联系方式", "has_phone" in html)
check("theme.css 定义了 .handoff-link", ".handoff-link" in css)

# ══════════════════════════════════════════════════════════════
# 清理
# ══════════════════════════════════════════════════════════════
deleted_t = SupportTicket.objects.filter(session_id__in=[SID, SID2, SID3, ""]).delete()[0]
deleted_t += SupportTicket.objects.filter(question__in=["测试流转", "超时测试"]).delete()[0]
deleted_c = Conversations.objects.filter(session_id__in=[SID, SID2, SID3]).delete()[0]
print(f"\n（已清理 {deleted_t} 张自造工单、{deleted_c} 条自造消息）")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
