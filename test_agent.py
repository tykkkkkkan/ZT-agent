"""
Day2 裸调验证脚本：Agent Prompt 设计 + 裸调 DeepSeek 验证
================================================================
功能：
  1. 用 OpenAI 兼容 SDK 建 client，base_url 指向 DeepSeek
  2. 读取 prompts/system_prompt.md 作为 system 消息
  3. 内置 mock_tools 字典模拟 5 个 Agent 工具的返回值
  4. 跑 4 个测试问题，命中 mock 关键词时把 mock 数据拼进 user 消息模拟工具返回
  5. 调 client.chat.completions.create，temperature=0.7，打印 用户问题 + AI 回答

说明：
  本脚本是 Day2 阶段的裸调验证，不接数据库，所有产品/库存/订单/政策数据均为硬编码 mock。
  Day4 会把这些 mock 换成 Django 查 MySQL 的真实工具调用（query_product/check_inventory/
  calculate_quote/get_order_status/search_knowledge 真正落地）。

运行步骤（PowerShell）：
  pip install openai python-dotenv
  # 在项目根目录 .env 文件写入：DEEPSEEK_API_KEY=sk-xxx（.env 已被 .gitignore 忽略）
  python test_agent.py
"""

import os
from dotenv import load_dotenv
from openai import OpenAI

# ===== 1. 从 .env 加载 API Key（.env 已被 .gitignore 忽略，key 不进版本库）=====
load_dotenv()
API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = "https://api.deepseek.com"
MODEL_PRIMARY = "deepseek-v4-pro"      # 优先模型；失效回退
MODEL_FALLBACK = "deepseek-chat"       # 回退模型

if not API_KEY:
    raise SystemExit(
        "未检测到 DEEPSEEK_API_KEY，请先设置环境变量：\n"
        '  PowerShell: $env:DEEPSEEK_API_KEY="sk-xxx"\n'
        "  或在项目根目录 .env 文件写入：DEEPSEEK_API_KEY=sk-xxx"
    )

# 防御：API Key 必须是纯 ASCII。若误设成占位符（如「你的key」含中文），
# httpx 在构造 Authorization 头时会因 ascii 编码失败而抛 UnicodeEncodeError，
# 在此提前给出友好提示，避免踩坑。
if not API_KEY.isascii():
    raise SystemExit(
        f"DEEPSEEK_API_KEY 含非 ASCII 字符（当前值前 6 位：{API_KEY[:6]!r}…），\n"
        "很可能误用了占位符（例如注释里的「你的key」）。请重设为真实的 DeepSeek API Key（形如 sk-xxx）：\n"
        '  PowerShell: $env:DEEPSEEK_API_KEY="sk-真实key"\n'
        "  或在 .env 写入：DEEPSEEK_API_KEY=sk-真实key"
    )

# ===== 2. 读取 system prompt =====
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "system_prompt.md")
with open(PROMPT_PATH, "r", encoding="utf-8") as f:
    system_prompt = f.read()

# ===== 3. mock 工具数据（模拟 Agent 工具返回） =====
mock_products = {
    "红虫颗粒": {
        "规格": "200g/包，1箱=10包",
        "适用鱼": "鲫鱼、鲤鱼",
        "零售价": "6元/包",
        "批发价": "4元/包",
        "库存": "320包（32箱）",
    },
    "九一八": {
        "规格": "150g/包，1箱=10包",
        "适用鱼": "鲫鱼、鲤鱼",
        "零售价": "5元/包",
        "批发价": "3.5元/包",
        "库存": "80包（8箱）",
    },
    "螺鲤3号": {
        "规格": "200g/包，1箱=10包",
        "适用鱼": "鲤鱼",
        "零售价": "7元/包",
        "批发价": "5元/包",
        "库存": "30包（3箱）",      # <50，触发库存预警
    },
    "蓝鲫X5": {
        "规格": "100g/包，1箱=10包",
        "适用鱼": "鲫鱼、鲤鱼",
        "零售价": "8元/包",
        "批发价": "6元/包",
        "库存": "150包（15箱）",
    },
    "速攻2号": {
        "规格": "200g/包，1箱=10包",
        "适用鱼": "鲫鱼",
        "零售价": "6元/包",
        "批发价": "4.5元/包",
        "库存": "0包（暂无库存，预计3天后到货）",
    },
}

mock_order = {
    "订单号": "DD20240801",
    "客户": "张老板（经销商）",
    "商品": "红虫颗粒 100包 + 螺鲤3号 50包",
    "状态": "已发货",
    "物流": "圆通速运，运单号 YT20240803001",
    "预计送达": "2024年8月3日 18:00 前",
}

mock_knowledge = (
    "【工具返回 search_knowledge】政策片段：\n"
    "《中渔天下售后政策 v2》第3条：饵料类商品因保质期问题，"
    "签收7天内未开封可申请退货，需联系业务员开具退货单；"
    "已拆封商品非质量问题不退不换。"
)


def build_user_message(question: str) -> str:
    """命中 mock 关键词时，把对应 mock 字符串拼进 user 消息模拟工具返回。"""
    extra = []

    # 命中产品名 → 模拟 query_product
    for name, info in mock_products.items():
        if name in question:
            lines = "\n".join(f"  - {k}: {v}" for k, v in info.items())
            extra.append(f"[工具调用 query_product 返回] {name}：\n{lines}")

    # 命中订单号/订单关键词 → 模拟 get_order_status
    if "DD20240801" in question or "订单" in question:
        lines = "\n".join(f"  - {k}: {v}" for k, v in mock_order.items())
        extra.append(f"[工具调用 get_order_status 返回]：\n{lines}")

    # 命中售后/退/政策 → 模拟 search_knowledge
    if ("退" in question) or ("售后" in question) or ("政策" in question):
        extra.append(mock_knowledge)

    if extra:
        return question + "\n\n" + "\n\n".join(extra) + "\n\n[以上为工具返回的 mock 数据，请基于此回答用户]"
    return question


# ===== 4. 建客户端 =====
client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


def chat_with_fallback(messages, temperature=0.7):
    """先用 deepseek-v4-pro，失效（如 model 不存在）回退到 deepseek-chat。"""
    try:
        return client.chat.completions.create(
            model=MODEL_PRIMARY,
            messages=messages,
            temperature=temperature,
        )
    except Exception as e:
        print(f"[模型 {MODEL_PRIMARY} 调用失败，回退 {MODEL_FALLBACK}] {e}")
        return client.chat.completions.create(
            model=MODEL_FALLBACK,
            messages=messages,
            temperature=temperature,
        )


# ===== 5. 跑 4 个测试问题 =====
questions = [
    "钓鲤鱼用啥饵",
    "红虫颗粒批发价，拿10箱多少钱",
    "查订单 DD20240801",
    "饵料拆封能退吗",
]

print("=" * 60)
print("Day2 裸调验证：中渔小助 + DeepSeek")
print(f"模型优先：{MODEL_PRIMARY}，回退：{MODEL_FALLBACK}")
print("=" * 60)

for i, q in enumerate(questions, 1):
    user_msg = build_user_message(q)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    print(f"\n【问题{i}】{q}")
    print("-" * 60)
    resp = chat_with_fallback(messages, temperature=0.7)
    answer = resp.choices[0].message.content
    print(f"【AI 回答】\n{answer}")
    print("-" * 60)

print("\nDay2 裸调验证结束。")
