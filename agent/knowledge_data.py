"""
agent/knowledge_data.py — 企业知识库语料

P2 增强（RAG 知识库数据化）：
- KNOWLEDGE_BASE 保留为「种子语料」（首次启动自动落库）
- 新增 knowledge_chunks 表（agent.models.KnowledgeChunk），后台可在线增删改
- get_knowledge_rag() 优先从 DB 构建索引；DB 不可用时回退静态语料
- seed_knowledge_to_db()：幂等地把种子语料写入 DB（已存在跳过）
"""

import os

from agent.rag import RAGEngine

# ──────────────────────────────────────────────────────────────
# 知识库语料（企业政策 + 钓鱼知识）
# 每条：title 为来源标题，keywords 用于检索命中，answer 为对外回答内容
# ──────────────────────────────────────────────────────────────
KNOWLEDGE_BASE = [
    # ── 季节钓法 ──
    {
        "title": "春季钓法",
        "keywords": ["春季", "春天", "春钓", "三月", "四月", "五月"],
        "answer": '春季钓鱼讲究「春钓浅滩、夏钓深潭」。春天气温回升，鱼儿开始活跃，推荐：\n1. 钓位选浅滩、向阳处，水深 1-2 米\n2. 饵料用腥香型，红虫颗粒或九一八腥版\n3. 早晚温差大，上午 9-11 点、下午 3-5 点是黄金时段\n4. 钓具选 4.5 米短竿，主线 1.0 号，子线 0.6 号'
    },
    {
        "title": "夏季钓法",
        "keywords": ["夏季", "夏天", "夏钓", "六月", "七月", "八月"],
        "answer": '夏季钓鱼要避暑：\n1. 早晚钓：早 5-8 点、晚 6-9 点，避开正午高温\n2. 钓位选树荫下、深水处（2.5-4 米）\n3. 饵料用清淡型，螺鲤3号+速攻2号，或发酵饵料\n4. 目标鱼：草鱼、鲤鱼、鲢鳙活跃，可钓浮\n5. 注意防暑、防暴雨'
    },
    {
        "title": "秋季钓法",
        "keywords": ["秋季", "秋天", "秋钓", "九月", "十月", "十一月"],
        "answer": '秋季是钓鱼黄金季，俗称「金九银十」：\n1. 全年最佳钓期，鱼口最猛，爆护率最高\n2. 钓位：深浅交接处、回水湾、水草边缘\n3. 饵料：腥香结合，红虫颗粒+九一八+螺鲤3号\n4. 钓法：底钓为主，调 4 钓 2，抓顿口\n5. 目标鱼：鲤鱼、鲫鱼、草鱼、黄尾鲴，全部活跃'
    },
    {
        "title": "冬季钓法",
        "keywords": ["冬季", "冬天", "冬钓", "十二月", "一月", "二月", "寒冷", "结冰"],
        "answer": '冬季钓鱼讲究「冬钓阳、钓深水」：\n1. 选晴天、正午前后出钓（10-14 点）\n2. 钓位选向阳避风、深水 3-5 米\n3. 饵料：浓腥，红虫、蚯蚓或红虫颗粒\n4. 钓法：传统钓，调钝钓钝，等黑漂\n5. 鲫鱼仍可钓，鲤鱼基本闭口\n6. 冰钓需专业装备，注意安全'
    },
    # ── 鱼种钓法 ──
    {
        "title": "钓鲤鱼",
        "keywords": ["鲤鱼", "钓鲤", "大鲤鱼", "鲤鱼饵"],
        "answer": '钓鲤鱼要点：\n1. 鲤鱼喜弱光、怕惊扰，选水深 2-3 米的安静水域\n2. 饵料：螺鲤3号+九一八+速攻2号，按 1:1:0.5 搭配，饵水比 1:0.9\n3. 钓法：重窝打底，提前 30 分钟打窝，钓钝抓死口\n4. 线组：主线 2.0-3.0 号，子线 1.0-1.5 号，钩用 5-7 号袖钩\n5. 提竿要稳，鲤鱼发力要借竿卸力'
    },
    {
        "title": "钓鲫鱼",
        "keywords": ["鲫鱼", "钓鲫", "鲫鱼饵", "土鲫"],
        "answer": '钓鲫鱼要点：\n1. 鲫鱼喜浅水、水草边，水深 1-2 米最佳\n2. 饵料：蓝鲫 X5 + 速攻2号，冬春腥香、夏秋清淡\n3. 钓法：调灵钓灵，调 5 钓 2，抓连续下顿\n4. 线组：主线 1.0-1.2 号，子线 0.4-0.6 号，袖钩 3-5 号\n5. 打窝要勤，少量多次，保持窝点有鱼'
    },
    {
        "title": "钓草鱼",
        "keywords": ["草鱼", "草鲫", "大草鱼"],
        "answer": '钓草鱼要点：\n1. 草鱼在上中层活动，钓浮为主，水深 2-3 米\n2. 饵料：草叶、玉米、发酵麦粒，或商品饵九一八+草霸\n3. 钓法：打大窝，一次性多打，草鱼成群觅食\n4. 线组：主线 3.0-4.0 号，子线 1.5-2.0 号，伊势尼 7-9 号\n5. 草鱼爆发力强，必须用失手绳'
    },
    {
        "title": "钓鲢鳙",
        "keywords": ["鲢鳙", "白鲢", "花鲢", "胖头"],
        "answer": '钓鲢鳙要点：\n1. 鲢鳙在水层中上层，钓浮 1.5-2 米\n2. 饵料：酸臭、发酵味型，鲢鳙饵+白粉+醋\n3. 钓法：打雾化窝，保持水面持续雾化\n4. 线组：主线 2.5-3.5 号，子线 1.2-1.5 号\n5. 抓黑漂或大顶漂，提竿要快'
    },
    {
        "title": "钓黄尾",
        "keywords": ["黄尾", "黄尾鲴", "黄尾鱼"],
        "answer": '钓黄尾要点：\n1. 黄尾喜沙底、流水，水深 1.5-2.5 米\n2. 饵料：腥香型，蓝鲫 X5 + 南极虾粉\n3. 钓法：钓底，调 3 钓 1，抓小顿口\n4. 线组：主线 0.8-1.0 号，子线 0.4-0.5 号\n5. 黄尾嘴小钩大，用 2-3 号袖钩，轻提轻遛'
    },
    # ── 钓法技巧 ──
    {
        "title": "打窝技巧",
        "keywords": ["打窝", "窝料", "窝点", "发窝"],
        "answer": '打窝技巧：\n1. 定点打窝：用打窝器精准投入，不要手抛\n2. 一次不要打太多，鲫鱼窝 50-100g，鲤鱼窝 200-500g\n3. 发窝时间：春秋 20-30 分钟，夏冬 30-60 分钟\n4. 补窝：鱼口变慢时少量补窝，保持窝点持续诱鱼\n5. 窝料选择：酒米+商品饵混合，效果最佳'
    },
    {
        "title": "调漂技巧",
        "keywords": ["调漂", "调几钓几", "浮漂", "漂"],
        "answer": '调漂方法：\n1. 半水调漂：先让漂目露出水面固定数（如调 5 目）\n2. 挂饵后漂下沉，往上推漂座至露出 2-3 目（钓目）\n3. 调 4 钓 2：通用，不灵不顿\n4. 调 6 钓 2：钓鲫鱼，灵敏抓口\n5. 调 2 钓 1：钓鲤鱼，抓死口\n6. 风天调钝，静水调灵'
    },
    {
        "title": "线组搭配",
        "keywords": ["线组", "主线", "子线", "绑钩"],
        "answer": '线组搭配：\n1. 冬春：细线小钩（主线 0.8-1.2，子线 0.4-0.6）\n2. 夏秋：粗线大钩（主线 1.5-3.0，子线 0.8-1.5）\n3. 钓大鱼：主线 3.0+，子线 1.5+，配失手绳\n4. 子线长度：鲫鱼 15-20cm，鲤鱼 20-30cm\n5. 绑钩要整齐，子线不打折'
    },
    {
        "title": "饵料搭配",
        "keywords": ["饵料搭配", "饵水比", "开饵", "配饵"],
        "answer": '饵料搭配原则：\n1. 基础饵（50%）+ 状态饵（30%）+ 味型饵（20%）\n2. 冬春腥香：腥饵 70% + 香饵 30%\n3. 夏秋清淡：香饵 70% + 腥饵 30%\n4. 饵水比：搓饵 1:0.8-1.0，拉饵 1:1.0-1.2\n5. 醒饵 3-5 分钟，不要急着用\n6. 状态不对时加状态粉调整'
    },
    {
        "title": "选钓位",
        "keywords": ["钓位", "选钓位", "钓鱼位置", "哪里钓"],
        "answer": '选钓位口诀：\n1. 浅滩草边钓鲫鱼，深潭坎边钓鲤鱼\n2. 回水湾、洄湾处是黄金钓位\n3. 树阴下、桥洞边夏钓最佳\n4. 下风口、进水口氧气足\n5. 安静避风处 > 人多嘈杂处\n6. 有鱼星/泡冒处就是好位置'
    },
    # ── 常见问题（企业政策） ──
    {
        "title": "退换货政策",
        "keywords": ["无理由", "退换", "退货", "换货", "售后", "退钱", "退款", "不满意", "能退", "能换", "退货政策", "退款吗"],
        "answer": '我们支持 7 天无理由退换货，只要产品未拆封、包装完好、不影响二次销售即可。'
    },
    {
        "title": "发货物流",
        "keywords": ["发货", "物流", "快递", "几天到", "送达"],
        "answer": '一般下单后 24 小时内发货，默认中通/圆通快递。大订单走物流专线，运费另算。全国大部分地区 2-5 天可到。'
    },
    {
        "title": "运费政策",
        "keywords": ["运费", "包邮", "快递费"],
        "answer": '满 99 元包邮（偏远地区除外），未满 99 元收取 8 元运费。批量订货走物流，运费按实际结算。'
    },
    {
        "title": "付款方式",
        "keywords": ["付款", "支付", "结账", "对公"],
        "answer": '支持对公转账、微信、支付宝等多种付款方式。新客户需款到发货，老客户可月结。'
    },
    {
        "title": "批发政策",
        "keywords": ["批发", "经销", "拿货", "进货", "阶梯价"],
        "answer": '批发价根据订货量阶梯定价：\n1. 10-50 箱：享受批发价\n2. 50-200 箱：享受区域代理价\n3. 200 箱以上：享受底价\n具体价格请联系客服 400-xxx-xxxx 洽谈。'
    },
    {
        "title": "加盟招商",
        "keywords": ["加盟", "经销商", "区域代理", "代理"],
        "answer": '我们诚招区域经销商，支持：\n1. 一件代发，无库存压力\n2. 区域保护政策，独家经营\n3. 厂家直接供货，价格优势\n4. 提供宣传物料和技术支持\n欢迎来电咨询详细政策。'
    },
    {
        "title": "营业时间",
        "keywords": ["营业时间", "客服时间", "几点"],
        "answer": '客服营业时间：早 8:00 ~ 晚 20:00，全年无休。下单系统 7×24 小时开放。'
    },
    {
        "title": "打招呼",
        "keywords": ["你好", "在吗", "hello", "hi", "您好"],
        "answer": '您好！我是中渔小助 🐟 有什么可以帮您的？产品咨询、库存查询、报价下单、钓鱼技巧都可以问我哦~'
    },
]

_rag_cache = None
_seeded = False


# ──────────────────────────────────────────────────────────────
# 标题 → 分类映射（种子语料落库用）
# ──────────────────────────────────────────────────────────────
_CATEGORY_MAP = {
    "春季钓法": "季节钓法", "夏季钓法": "季节钓法", "秋季钓法": "季节钓法", "冬季钓法": "季节钓法",
    "钓鲤鱼": "鱼种钓法", "钓鲫鱼": "鱼种钓法", "钓草鱼": "鱼种钓法",
    "钓鲢鳙": "鱼种钓法", "钓黄尾": "鱼种钓法",
    "打窝技巧": "钓法技巧", "调漂技巧": "钓法技巧", "线组搭配": "钓法技巧",
    "饵料搭配": "钓法技巧", "选钓位": "钓法技巧",
    "退换货政策": "企业政策", "发货物流": "企业政策", "运费政策": "企业政策",
    "付款方式": "企业政策", "批发政策": "企业政策", "加盟招商": "企业政策",
    "营业时间": "企业政策", "打招呼": "其他",
}


def _category_for(title: str) -> str:
    return _CATEGORY_MAP.get(title, "general")


# ──────────────────────────────────────────────────────────────
# 落库（幂等 seed）
# ──────────────────────────────────────────────────────────────
def seed_knowledge_to_db(force: bool = False) -> int:
    """把 KNOWLEDGE_BASE 种子语料写入 knowledge_chunks 表。

    - 幂等：title + content 已存在则跳过
    - force=True 时强制重写（content 更新为新语料）
    - 返回本次新增/更新的条数；DB 不可用返回 0
    """
    global _seeded
    try:
        from agent.models import KnowledgeChunk
    except Exception:
        return 0

    changed = 0
    for item in KNOWLEDGE_BASE:
        title = item.get("title") or ""
        content = item.get("answer", "")
        kw = ",".join(item.get("keywords", []))
        cat = _category_for(title)
        if force:
            obj, created = KnowledgeChunk.objects.update_or_create(
                title=title,
                defaults={"category": cat, "keywords": kw, "content": content, "is_active": True},
            )
            changed += 1 if created or obj.content != content else 0
        elif not KnowledgeChunk.objects.filter(title=title, content=content).exists():
            KnowledgeChunk.objects.create(
                title=title, category=cat, keywords=kw, content=content, is_active=True,
            )
            changed += 1
    _seeded = True
    return changed


# ──────────────────────────────────────────────────────────────
# 文档源：优先 DB，回退静态
# ──────────────────────────────────────────────────────────────
def _load_docs():
    """从 knowledge_chunks 表加载活跃条目（首次自动 seed）。

    DB 不可用时（如独立单测无 Django 环境）回退到静态 KNOWLEDGE_BASE。
    """
    try:
        from agent.models import KnowledgeChunk
        if not _seeded:
            seed_knowledge_to_db()
        qs = KnowledgeChunk.objects.filter(is_active=True)
        if qs.exists():
            docs = []
            for c in qs:
                kw_list = [k.strip() for k in (c.keywords or "").split(",") if k.strip()]
                text = f"{c.title} {' '.join(kw_list)}\n{c.content}"
                docs.append({
                    "text": text,
                    "meta": {"title": c.title, "answer": c.content, "category": c.category},
                })
            return docs
    except Exception:
        pass

    # fallback：静态语料（无 Django / DB 未初始化）
    docs = []
    for item in KNOWLEDGE_BASE:
        title = item.get("title") or (item.get("keywords") or [""])[0]
        text = title + " " + " ".join(item.get("keywords", [])) + "\n" + item.get("answer", "")
        docs.append({
            "text": text,
            "meta": {"title": title, "answer": item.get("answer", "")},
        })
    return docs


def get_knowledge_rag() -> RAGEngine:
    """构建并缓存企业知识库 RAG 引擎（惰性初始化，首次调用构建索引）。

    P2 增强：索引数据来自 DB（knowledge_chunks），可在线维护；
    DB 不可用时回退静态 KNOWLEDGE_BASE。

    后端切换（环境变量 RAG_BACKEND）：
    - 'tfidf'（默认）：TfidfEmbedder + InMemoryVectorStore（零依赖）
    - 'embedding'：OpenaiEmbedder（语义向量）+ ChromaStore（持久化向量库）
    """
    global _rag_cache
    if _rag_cache is None:
        backend = os.getenv('RAG_BACKEND', 'tfidf').strip().lower()
        if backend == 'embedding':
            from agent.embeddings.openai_embedder import OpenaiEmbedder
            from agent.vectorstores.chroma_store import ChromaStore
            engine = RAGEngine(embedder=OpenaiEmbedder(), store_factory=ChromaStore)
        else:
            engine = RAGEngine()
        engine.ingest(_load_docs())
        _rag_cache = engine
    return _rag_cache
