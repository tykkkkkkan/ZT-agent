"""
契约验证：AI 回答反馈闭环（👍 / 👎 → 知识库补条目）—— ZT-agent

背景
----
「我有 RAG」和「我的 RAG 在被差评后变得更准」是两件事。后者需要一条
每个环节都有落点的链路：

    用户在气泡下点一下  →  服务端留档（含当时的一问一答 + 引用来源）
    →  后台看到「这句话为什么答歪了」  →  一键去补知识库条目

本脚本守住六条口径：

  1. **必须能定位到具体一条 assistant 消息**：message_id + session_id 双校验，
     只给 session 不够（运营会不知道是哪句话答错了）
  2. **不能越权评价别人的对话**：message_id 属于另一个 session 时一律 404，
     否则改个数字就能给任意回答打差评
  3. **只允许评价 AI 回答**：拿 user 消息的 id 提交要被拒
  4. **同一条消息重复评价 = 更新而不是新增**：允许改主意，不允许刷数据
  5. **闭环真的通**：「补进知识库」的 URL 打开后，标题输入框里确实是用户原问
     —— 只生成了一个链接不算闭环，落地页预填成功才算
  6. **来源快照区分两种差评**：sources 为空 = 知识库缺这条；
     sources 有却不相关 = 条目在，是表述/切分的问题。修法不同，必须留

用法：./.venv/Scripts/python.exe scripts/verify_feedback.py
"""
import json
import os
import re
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib import admin as dj_admin  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from agent.models import Conversations, MessageFeedback  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'√' if cond else '×'} {label}" + (f"  <- {detail}" if detail else ""))


print("=" * 62)
print("  AI 回答反馈闭环验收")
print("=" * 62)

URL = "/api/agent/feedback/"

# ══════════════════════════════════════════════════════════════
# 【1】模型约束
# ══════════════════════════════════════════════════════════════
print("\n【1】模型约束")
meta = MessageFeedback._meta
check("表名为 message_feedback", meta.db_table == "message_feedback", meta.db_table)
check("Rating 只有 1 / -1 两档",
      sorted(v for v, _ in MessageFeedback.Rating.choices) == [-1, 1],
      str(MessageFeedback.Rating.choices))
uniq = [c for c in meta.constraints if c.name == "uniq_feedback_message_user"]
check("同一消息 + 同一用户唯一（防刷）", len(uniq) == 1, str(meta.constraints))
check("message_id 可空（消息被清理后反馈仍在）", meta.get_field("message_id").null)
check("sources 是 JSON 字段（存当时的来源快照）",
      meta.get_field("sources").get_internal_type() == "JSONField")
check("默认按时间倒序（最新差评在最上面）", meta.ordering == ("-created_at",), str(meta.ordering))

# ══════════════════════════════════════════════════════════════
# 【2】准备数据
# ══════════════════════════════════════════════════════════════
User = get_user_model()
user = User.objects.filter(is_superuser=False).first() or User.objects.filter(is_superuser=True).first()
admin_user = User.objects.filter(is_superuser=True).first()

SESSION = "vfb-" + uuid.uuid4().hex[:12]
OTHER = "vfb-other-" + uuid.uuid4().hex[:8]
TAG = "VERIFY_FEEDBACK_"

# 一问一答（顺序很重要：question 是靠"往前找最近一条 user 消息"回溯的）
q_msg = Conversations.objects.create(session_id=SESSION, role="user", content=TAG + "鱼竿断了能换吗")
a_msg = Conversations.objects.create(
    session_id=SESSION, role="assistant",
    content=TAG + "抱歉，这个我不太清楚，建议联系客服。")
# 第二个会话：用来验证越权
o_q = Conversations.objects.create(session_id=OTHER, role="user", content=TAG + "别人的问题")
o_a = Conversations.objects.create(
    session_id=OTHER, role="assistant", content=TAG + "别人的回答")

client = Client()
client.force_login(user)
admin_client = Client()
admin_client.force_login(admin_user)

# ══════════════════════════════════════════════════════════════
# 【3】拒绝分支 —— 每种非法输入都要被拦住，且不留下脏数据
# ══════════════════════════════════════════════════════════════
print("\n【3】非法请求一律拒绝")
before = MessageFeedback.objects.count()

anon = Client()
r = anon.post(URL, data=json.dumps({"message_id": a_msg.id, "session_id": SESSION, "rating": -1}),
              content_type="application/json")
# 登录墙：/api/* 上的 login_required 被中间件改写成 401 JSON（不是 302 跳登录页），
# 前端才能拿到明确错误而不是被重定向成一坨 HTML
check("未登录不能评价", r.status_code in (302, 401, 403), f"HTTP {r.status_code}")

r = client.get(URL)
check("GET 被拒（只接受 POST）", r.status_code == 405, f"HTTP {r.status_code}")

r = client.post(URL, data="{bad json", content_type="application/json")
check("非 JSON 请求体 → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"message_id": a_msg.id, "session_id": SESSION}),
                content_type="application/json")
check("缺 rating → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"message_id": a_msg.id, "session_id": SESSION, "rating": 5}),
                content_type="application/json")
check("rating 只允许 1 / -1 → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"session_id": SESSION, "rating": -1}),
                content_type="application/json")
check("缺 message_id → 400（否则不知道评价的是哪句话）", r.status_code == 400,
      f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"message_id": 99999999, "session_id": SESSION, "rating": -1}),
                content_type="application/json")
check("不存在的消息 → 404", r.status_code == 404, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"message_id": o_a.id, "session_id": SESSION, "rating": -1}),
                content_type="application/json")
check("拿别会话的消息 id 评价 → 404（防越权）", r.status_code == 404, f"HTTP {r.status_code}")

r = client.post(URL, data=json.dumps({"message_id": q_msg.id, "session_id": SESSION, "rating": -1}),
                content_type="application/json")
check("拿用户提问的 id 评价 → 404（只能评价 AI 回答）", r.status_code == 404,
      f"HTTP {r.status_code}")

check("以上拒绝都没留下脏数据", MessageFeedback.objects.count() == before,
      f"{before} -> {MessageFeedback.objects.count()}")

# ══════════════════════════════════════════════════════════════
# 【4】正常提交：落库 + 问题回溯 + 来源快照
# ══════════════════════════════════════════════════════════════
print("\n【4】正常提交与落档")
SOURCES = [{"title": f"来源{i}", "score": round(0.9 - i * 0.1, 2), "snippet": "不该被存的长文"}
           for i in range(8)]
r = client.post(URL, data=json.dumps({
    "message_id": a_msg.id, "session_id": SESSION, "rating": -1,
    "reason": "没查到", "comment": "客服电话是多少", "sources": SOURCES,
}), content_type="application/json")
body = r.json() if r.get("Content-Type", "").startswith("application/json") else {}
check("正常提交 200", r.status_code == 200, f"HTTP {r.status_code}")
check("返回 created=True", body.get("created") is True, str(body))
check("返回 feedback_id", isinstance(body.get("feedback_id"), int), str(body))

fb = MessageFeedback.objects.filter(message_id=a_msg.id).first()
check("落库成功", fb is not None)
if fb:
    check("rating 存成 -1", fb.rating == -1, str(fb.rating))
    check("question 回溯到上一条用户消息",
          fb.question.startswith(TAG + "鱼竿断了能换吗"), fb.question)
    check("answer 存的是那条 AI 回答", fb.answer.startswith(TAG + "抱歉"), fb.answer[:40])
    check("session_id 一并存下（便于跳会话）", fb.session_id == SESSION, fb.session_id)
    check("reason 存下", fb.reason == "没查到", fb.reason)
    check("初始未处理（进待优化清单）", fb.handled is False)
    check("来源最多留 5 条（URL 与列表不被撑爆）", len(fb.sources) == 5, str(len(fb.sources)))
    check("来源只留 title / score（不沉淀整段原文）",
          all(set(s.keys()) == {"title", "score"} for s in fb.sources),
          str(fb.sources[:1]))
    check("第一条来源标题正确", fb.sources[0]["title"] == "来源0", str(fb.sources[:1]))

# ══════════════════════════════════════════════════════════════
# 【5】重复评价 = 更新（允许改主意，不允许刷数据）
# ══════════════════════════════════════════════════════════════
print("\n【5】重复评价走更新")
n_before = MessageFeedback.objects.filter(message_id=a_msg.id).count()
r = client.post(URL, data=json.dumps({
    "message_id": a_msg.id, "session_id": SESSION, "rating": 1, "reason": "",
}), content_type="application/json")
body = r.json()
check("再评一次 200", r.status_code == 200, f"HTTP {r.status_code}")
check("返回 created=False（是更新不是新增）", body.get("created") is False, str(body))
check("表里仍只有一行", MessageFeedback.objects.filter(message_id=a_msg.id).count() == n_before,
      str(MessageFeedback.objects.filter(message_id=a_msg.id).count()))
fb.refresh_from_db()
check("评价已被改成 1", fb.rating == 1, str(fb.rating))

# 改回差评，后面要用它验证闭环
r = client.post(URL, data=json.dumps({
    "message_id": a_msg.id, "session_id": SESSION, "rating": -1,
    "reason": "没查到", "sources": SOURCES[:2],
}), content_type="application/json")
fb.refresh_from_db()
check("还能改回差评（不锁定）", fb.rating == -1, str(fb.rating))

# ══════════════════════════════════════════════════════════════
# 【6】JWT 通道：前端 authFetch 只带 Authorization 头，不带 session
# ══════════════════════════════════════════════════════════════
print("\n【6】双通道鉴权与历史回看")
from rest_framework_simplejwt.tokens import RefreshToken  # noqa: E402

jwt_client = Client()                       # 刻意不 force_login：模拟只有 token 的场景
token = str(RefreshToken.for_user(user).access_token)
r = jwt_client.post(URL, data=json.dumps({
    "message_id": a_msg.id, "session_id": SESSION, "rating": -1, "reason": "答非所问",
}), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
check("只带 JWT、无 session 也能评价（否则前端每次点都跳到登录页）",
      r.status_code == 200, f"HTTP {r.status_code}")
fb_jwt = MessageFeedback.objects.filter(message_id=a_msg.id).first()
check("JWT 通道解析出的用户与会话通道一致",
      fb_jwt is not None and fb_jwt.user_id == user.id,
      f"{getattr(fb_jwt, 'user_id', None)} vs {user.id}")
# 这次刻意没带 sources：来源快照是排查差评的唯一证据，不能因为调用方漏传就被清空
check("重评时未回传 sources 不清空已有快照",
      fb_jwt is not None and len(fb_jwt.sources) == 2,
      str(fb_jwt.sources) if fb_jwt else "无记录")

# 历史回看：刷新页面后仍要能评价（用户往往事后才想给差评）
r = client.get(f"/api/agent/history/{SESSION}/")
check("历史接口 200", r.status_code == 200, f"HTTP {r.status_code}")
hmsgs = r.json().get("messages", []) if r.status_code == 200 else []
ai_msgs = [m for m in hmsgs if m.get("role") == "assistant"]
check("历史里的 AI 回答带回 message_id（否则刷新后按钮消失）",
      bool(ai_msgs) and ai_msgs[0].get("id") == a_msg.id,
      str(ai_msgs[:1]))
check("历史带回已评状态（评过的不再重复摆按钮）",
      bool(ai_msgs) and ai_msgs[0].get("feedback") == -1,
      str(ai_msgs[0].get("feedback")) if ai_msgs else "无 AI 消息")
check("未评过的消息 feedback 为 0",
      all((m.get("feedback") or 0) == 0 for m in hmsgs if m.get("role") == "user"),
      str([m.get("feedback") for m in hmsgs if m.get("role") == "user"]))

# ══════════════════════════════════════════════════════════════
# 【7】闭环：差评 → 新建知识库条目，且落地页真的预填
# ══════════════════════════════════════════════════════════════
print("\n【7】闭环链接与落地页预填")
fb_admin = dj_admin.site._registry[MessageFeedback]
html_kb = str(fb_admin.kb_link(fb))
check("差评有「补进知识库」链接", "补进知识库" in html_kb, html_kb[:80])

m = re.search(r'href="([^"]+)"', html_kb)
check("链接指向知识库新增页", bool(m) and "knowledgechunk/add" in m.group(1),
      m.group(1)[:80] if m else "无 href")

if m:
    from html import unescape
    add_url = unescape(m.group(1))
    check("URL 里预填了用户原问", "title=" in add_url, add_url[:100])
    # 真正打开一次：预填成功才算闭环，只拼出链接不算
    # （用超管客户端：普通用户没有 add_knowledgechunk 权限，会 302 去登录页）
    r_add = admin_client.get(add_url)
    check("新增页 200", r_add.status_code == 200, f"HTTP {r_add.status_code}")
    html_add = r_add.content.decode("utf-8", "ignore")
    check("标题输入框里确实是用户原问",
          "鱼竿断了能换吗" in html_add, "落地页未预填 = 闭环只走了一半")
    check("正文没有被预填成 AI 那句错答案（避免把错误固化进知识库）",
          "抱歉，这个我不太清楚" not in html_add)

fb.rating = 1
check("好评不出现「补进知识库」（不制造无效动作）",
      "补进知识库" not in str(fb_admin.kb_link(fb)))
fb.rating = -1

# ══════════════════════════════════════════════════════════════
# 【8】后台页面、KPI 条、批量处理
# ══════════════════════════════════════════════════════════════
print("\n【8】后台页面与批量处理")
LIST = "/admin/agent/messagefeedback/"
r = admin_client.get(LIST)
check("反馈列表页 200", r.status_code == 200, f"HTTP {r.status_code}")
html_list = r.content.decode("utf-8", "ignore")
check("列表渲染了记录", fb.question[:6] in html_list or "鱼竿断了" in html_list)
check("顶部 KPI 条已渲染", "zy-fb-kpi" in html_list)
check("KPI 里有「待优化」且可点", "待优化" in html_list)
check("待优化直达链接带好筛选条件", "rating=-1" in html_list and "handled=0" in html_list,
      "KPI 数字点进去应已筛好，不用再手工勾条件")
check("列表有状态徽标样式", "zy-badge" in html_list)
check("命中来源时显示命中条数", "命中" in html_list or "未命中" in html_list)

r = admin_client.get(f"/admin/agent/messagefeedback/{fb.id}/change/")
check("详情页 200", r.status_code == 200, f"HTTP {r.status_code}")
html_detail = r.content.decode("utf-8", "ignore")
check("详情页有一问一答面板", "zy-fb-qa" in html_detail)
check("详情页有一问一答原文", "鱼竿断了能换吗" in html_detail)
check("详情页有引用来源面板", "来源0" in html_detail)
check("详情页能跳到完整会话", f"q={SESSION}" in html_detail)

r = admin_client.get("/admin/agent/messagefeedback/add/")
check("不允许手工新增反馈（add 页 403）", r.status_code == 403, f"HTTP {r.status_code}")

# 批量标记已处理
r = admin_client.post(LIST, {
    "action": "mark_feedback_handled",
    "_selected_action": [str(fb.id)],
    "select_across": "0",
    "index": "0",
}, follow=True)
fb.refresh_from_db()
check("批量「标记为已处理」生效", fb.handled is True, f"HTTP {r.status_code} handled={fb.handled}")

r = admin_client.post(LIST, {
    "action": "unmark_feedback_handled",
    "_selected_action": [str(fb.id)],
    "select_across": "0",
    "index": "0",
}, follow=True)
fb.refresh_from_db()
check("批量「撤销已处理」生效", fb.handled is False, f"handled={fb.handled}")

# 侧栏入口
from django.conf import settings as dj_settings  # noqa: E402
nav_links = [i.get("link") for g in dj_settings.UNFOLD.get("SIDEBAR", {}).get("navigation", [])
             for i in g.get("items", [])]
check("侧栏有「回答反馈」入口", "/admin/agent/messagefeedback/" in nav_links, str(nav_links[-6:]))

# ══════════════════════════════════════════════════════════════
# 【9】前端接线
# ══════════════════════════════════════════════════════════════
print("\n【9】前端接线")
paths = {
    "app": os.path.join(BASE, "frontend", "app.js"),
    "html": os.path.join(BASE, "frontend", "index.html"),
    "css": os.path.join(BASE, "frontend", "assets", "theme.css"),
}
src = {k: open(p, encoding="utf-8").read() for k, p in paths.items()}

check("app.js 调用 /api/agent/feedback/", "/api/agent/feedback/" in src["app"])
check("app.js 提交 message_id（否则不知道评价哪句）",
      re.search(r"message_id:\s*msg\.messageId", src["app"]) is not None)
check("app.js 回传当时的 sources", "sources: (msg.sources || [])" in src["app"])
check("app.js 有差评原因选项", "FB_REASONS" in src["app"])
check("评价前要求登录", "AUTH.ensureLogin('登录后即可反馈')" in src["app"])
check("提交中有 loading 保护（防连点刷数据）", "fbSending" in src["app"])

check("index.html 有反馈按钮行", "feedback-row" in src["html"])
check("index.html 两个按钮绑 sendFeedback", src["html"].count("sendFeedback") >= 2,
      str(src["html"].count("sendFeedback")))
check("index.html 有原因 chip 行", "feedback-reasons" in src["html"])
check("index.html 显示提交失败提示", "feedbackError" in src["html"])

for cls in (".feedback-row", ".feedback-btn", ".feedback-chip", ".feedback-done"):
    check(f"theme.css 定义了 {cls}", cls + " " in src["css"] or cls + "{" in src["css"]
          or cls + "," in src["css"] or cls + ":" in src["css"])

# ══════════════════════════════════════════════════════════════
# 清理
# ══════════════════════════════════════════════════════════════
deleted_fb = MessageFeedback.objects.filter(session_id__in=[SESSION, OTHER]).delete()[0]
deleted_conv = Conversations.objects.filter(session_id__in=[SESSION, OTHER]).delete()[0]
print(f"\n（已清理 {deleted_fb} 条自造反馈、{deleted_conv} 条自造消息）")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
