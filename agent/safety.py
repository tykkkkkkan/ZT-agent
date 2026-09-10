"""
agent/safety.py — 对话输入安全审核（C 端客服合规前置）

轻量级、零第三方依赖的规则审核，覆盖三类风险：
  1. 辱骂 / 脏话（profanity）
  2. Prompt 注入（诱导 AI 忽略指令 / 泄露系统提示 / 越权）
  3. 刷屏 / 异常输入（超长、重复字符）

开关：环境变量 CONTENT_MODERATION（默认开启）。关闭后仅做长度校验，直接放行。
敏感词可在 .env 通过 SENSITIVE_WORDS 以逗号追加（合并到默认清单）。

不依赖 Django，可独立单测（CI 中直接 import）。

对外：check_message(text) -> (ok: bool, reason: str)
  - ok=True  → 放行
  - ok=False → 拒绝；reason 仅用于日志，对前端应返回统一友好文案。
"""
import os
import re

# ── 默认脏话清单（保守、常见，避免误伤正常钓鱼咨询） ──
_DEFAULT_PROFANITY = [
    "傻逼", "煞笔", "妈的", "草你", "操你", "废物", "垃圾", "垃圾人",
    "sb", "fuck", "shit", "bitch", "asshole", "idiot",
]

# ── Prompt 注入常见模式（中英文） ──
_INJECTION_PATTERNS = [
    re.compile(r"忽略.{0,12}(之前|上面|前面|所有|以上|先前).{0,8}(指令|提示|要求|规则|设定)"),
    re.compile(r"ignore (all |previous |above )?(instructions|prompts?|rules?)", re.IGNORECASE),
    re.compile(r"(你|你现在|你如今).{0,6}(是|变成|扮演|充当).{0,10}(另一个|不同的|无限制|dan|d(?:a|e)none)"),
    re.compile(r"(说出|告诉我|泄露|输出|列出|展示).{0,10}(系统提示|system prompt|你的指令|你的设定|你的规则)"),
    re.compile(r"(jailbreak|越狱|无过滤|不受限制|解除限制)"),
]

MAX_INPUT_LEN = int(os.getenv("MAX_INPUT_LEN", "2000"))
_REPEAT_RE = re.compile(r"(.)\1{20,}")  # 同一字符重复 20+ 次（刷屏）

# 审核开关：默认开启；设置为 0 / false / False 关闭
CONTENT_MODERATION = os.getenv("CONTENT_MODERATION", "1") not in ("0", "false", "False", "")


def _load_profanity():
    words = list(_DEFAULT_PROFANITY)
    extra = os.getenv("SENSITIVE_WORDS", "")
    if extra:
        words.extend([w.strip() for w in extra.split(",") if w.strip()])
    return words


_PROFANITY = _load_profanity()


def check_message(text: str):
    """审核单条用户输入，返回 (ok, reason)。

    reason 取值（仅用于日志，不直接回显用户）：
      input_too_long / repetition_spam / profanity / prompt_injection
    """
    if not text or not text.strip():
        return True, ""

    # 长度上限（始终校验，无论审核开关）
    if len(text) > MAX_INPUT_LEN:
        return False, f"input_too_long:{len(text)}"

    # 刷屏：同一字符重复过多
    if _REPEAT_RE.search(text):
        return False, "repetition_spam"

    # 审核开关（默认开启）
    if not CONTENT_MODERATION:
        return True, ""

    lowered = text.lower()

    # 1) 脏话
    for w in _PROFANITY:
        if w.lower() in lowered:
            return False, "profanity"

    # 2) Prompt 注入
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return False, "prompt_injection"

    return True, ""
