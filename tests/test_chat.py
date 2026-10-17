"""Day8 模块冒烟测试：验证 Agent 核心模块可正常导入与基础逻辑（不依赖 MySQL / API Key）

运行：
    .venv/Scripts/python.exe test_chat.py
"""
import os
import sys

# 本文件在 tests/ 下，把项目根加进 sys.path 才能 import config / agent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()


def test_imports():
    from agent.agent import agent_stream, _parse_tool_args, _load_history
    from agent.tools import TOOL_SCHEMAS, TOOL_REGISTRY, call_tool
    from agent.prompts import load_prompt
    from agent import llm

    # 契约：Schema 与实现必须一一对应（工具增减不该让测试假失败，漏注册才该报警）
    schema_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert schema_names == set(TOOL_REGISTRY), (
        f"Schema 与实现不匹配 —— 仅有 Schema：{schema_names - set(TOOL_REGISTRY)}；"
        f"仅有实现：{set(TOOL_REGISTRY) - schema_names}"
    )
    # 回归保护：这几个核心工具不能悄悄消失
    assert {
        "query_product", "check_inventory", "get_order_status",
        "calculate_quote", "search_knowledge", "guide_to_contact",
    } <= set(TOOL_REGISTRY)
    assert "中渔小助" in load_prompt("system_prompt_agent")
    print("[PASS] 核心模块导入正常")


def test_parse_tool_args():
    from agent.agent import _parse_tool_args

    assert _parse_tool_args('{"name": "红虫颗粒"}') == {"name": "红虫颗粒"}
    assert _parse_tool_args("") == {}
    assert _parse_tool_args("不是 JSON") == {}
    assert _parse_tool_args("[1,2,3]") == {}  # 非 dict 参数按空处理
    print("[PASS] 工具参数解析容错正常")


if __name__ == "__main__":
    print("=" * 50)
    print("Agent 模块冒烟测试")
    print("=" * 50)
    test_imports()
    test_parse_tool_args()
    print("=" * 50)
    print("全部通过 ✅")
