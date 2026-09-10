"""Day8 模块冒烟测试：验证 Agent 核心模块可正常导入与基础逻辑（不依赖 MySQL / API Key）

运行：
    .venv/Scripts/python.exe test_chat.py
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()


def test_imports():
    from agent.agent import agent_stream, _parse_tool_args, _load_history
    from agent.tools import TOOL_SCHEMAS, TOOL_REGISTRY, call_tool
    from agent.prompts import load_prompt
    from agent import llm

    assert len(TOOL_SCHEMAS) == 7
    assert set(TOOL_REGISTRY) == {
        "query_product", "check_inventory", "get_order_status",
        "query_orders_by_phone", "calculate_quote", "search_knowledge",
        "guide_to_contact",
    }
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
