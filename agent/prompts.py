"""
agent/prompts.py — Prompt 版本管理

把 System Prompt 从代码中剥离，落到 prompts/ 目录的 Markdown 文件，
支持按名称加载与版本切换，便于 Prompt 实验、调优与 A/B 测试（对齐实习 JD「Prompt 工程」）。

约定：
- 文件名即 Prompt 名称，如 system_prompt_agent → prompts/system_prompt_agent.md
- 未来可扩展为 system_prompt_agent_v2.md 等，通过 load_prompt(name) 指定版本
"""

from pathlib import Path

from django.conf import settings

PROMPT_DIR = Path(settings.BASE_DIR) / "prompts"

# 内置兜底 Prompt（当目标文件不存在时使用，保证服务不因缺文件而中断）
_FALLBACK_PROMPT = """你是「中渔小助」，中渔天下渔具公司的 AI 客服。
对于涉及公司产品价格、库存、报价、订单、政策的问题，必须先调用工具查询，再回答；
钓鱼技巧、闲聊等问题可直接回答。回复 3-6 句话，口语化，不编造数据。"""


def load_prompt(name: str = "system_prompt_agent") -> str:
    """加载 prompts/{name}.md；不存在则回退到内置兜底。"""
    path = PROMPT_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return _FALLBACK_PROMPT
