"""
agent/views.py

Day5: Agent 聊天接口 —— 接收用户消息，调用 DeepSeek，解析工具调用并返回回复。
"""
import json
import os
import re

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from dotenv import load_dotenv

load_dotenv()

# System Prompt：描述可用工具，要求 AI 用 JSON 格式输出工具调用
SYSTEM_PROMPT = """你是「中渔小助」，中渔天下饵料公司的 AI 客服助手。

【可用工具】
1. query_product — 查询产品信息（规格、价格、适用鱼种）。参数：产品名。
2. check_inventory — 查询库存。参数：产品名。
3. get_order_status — 查询订单状态。参数：订单号。

【重要规则】
当用户提问涉及产品信息、价格、库存、订单时，你必须且只能输出纯 JSON，格式如下（不要加任何其他文字、解释或问候）：
{"tool": "query_product", "arg": "红虫颗粒"}
{"tool": "check_inventory", "arg": "螺鲤3号"}
{"tool": "get_order_status", "arg": "DD20240801"}

如果用户只是闲聊或打招呼，不需要调工具，直接用一句话自然回复即可。

【示例】
用户：红虫颗粒多少钱？
你输出：{"tool": "query_product", "arg": "红虫颗粒"}

用户：螺鲤3号还有多少库存？
你输出：{"tool": "check_inventory", "arg": "螺鲤3号"}

用户：查订单DD20240801
你输出：{"tool": "get_order_status", "arg": "DD20240801"}

用户：你好
你输出：你好！我是中渔小助，有什么可以帮您的？"""


def _extract_tool_call(ai_text: str):
    """从 AI 回复中提取 JSON 格式的工具调用。
    支持两种情况：
    1. AI 直接输出 {"tool": "...", "arg": "..."}
    2. AI 在回复中包含 JSON 片段
    """
    # 尝试直接解析整个回复
    try:
        data = json.loads(ai_text.strip())
        if isinstance(data, dict) and "tool" in data and "arg" in data:
            return data["tool"], data["arg"]
    except json.JSONDecodeError:
        pass

    # 用正则提取 JSON 片段
    match = re.search(r'\{[^}]*"tool"[^}]*\}', ai_text, re.IGNORECASE)
    if match:
        try:
            data = json.loads(match.group())
            return data.get("tool"), data.get("arg")
        except json.JSONDecodeError:
            pass

    return None, None


def _call_deepseek(messages):
    """调用 DeepSeek API，返回 AI 的文本回复。"""
    from openai import OpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "API Key 未配置，无法调用 AI。"

    try:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"AI 调用失败：{e}"


@csrf_exempt
@require_POST
def chat(request):
    """AI 聊天接口：POST /api/agent/chat/"""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "请求体必须是 JSON，如 {\"message\": \"你好\"}"}, status=400)

    user_message = body.get("message", "").strip()
    if not user_message:
        return JsonResponse({"error": "message 不能为空"}, status=400)

    from agent.tools import call_tool

    # 先尝试 AI 输出 JSON 工具调用
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    ai_reply = _call_deepseek(messages)
    tool_name, tool_arg = _extract_tool_call(ai_reply)

    # 兜底：关键词匹配（AI 没按 JSON 格式输出时使用）
    if not tool_name:
        tool_name, tool_arg = _keyword_match(user_message)

    if tool_name:
        # 调工具，把结果喂给 AI 生成自然回复
        tool_result = call_tool(tool_name, tool_arg)
        messages.append({"role": "assistant", "content": ai_reply})
        messages.append({
            "role": "user",
            "content": f"[工具 {tool_name} 返回结果]\n{tool_result}\n\n请根据以上工具结果，用自然语言回答用户的问题，不要提及工具调用过程。",
        })
        final_reply = _call_deepseek(messages)
    else:
        # AI 直接给出了自然语言回复
        final_reply = ai_reply

    return JsonResponse({"reply": final_reply})


def _keyword_match(message: str):
    """关键词匹配兜底：当 AI 没输出 JSON 时，用简单规则识别意图。"""
    # 订单查询：包含订单号或"订单""查单"关键词
    if "订单" in message or "查单" in message or "DD" in message.upper():
        import re
        m = re.search(r'DD\d+', message.upper())
        if m:
            return "get_order_status", m.group()
        # 尝试提取可能的订单号
        return "get_order_status", message.strip()

    # 库存查询：包含"库存""有没有货""还有多少""缺货"
    if any(kw in message for kw in ["库存", "有没有货", "还有多少", "缺货", "备货"]):
        # 尝试提取产品名
        product = _extract_product_name(message)
        if product:
            return "check_inventory", product

    # 产品查询：包含"多少钱""价格""规格""介绍""什么饵""推荐"
    if any(kw in message for kw in ["多少钱", "价格", "规格", "介绍", "什么饵", "推荐", "钓"]):
        product = _extract_product_name(message)
        if product:
            return "query_product", product

    # 无法匹配
    return None, None


def _extract_product_name(message: str) -> str:
    """从用户消息中提取产品名。"""
    known = ["红虫颗粒", "九一八", "螺鲤3号", "蓝鲫X5", "速攻2号"]
    for name in known:
        if name in message:
            return name
    # 没匹配到已知产品，返回整个消息让工具处理（会返回"没找到"）
    return message.strip()
