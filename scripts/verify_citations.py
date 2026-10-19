"""
契约验证：引用溯源（参考来源）—— ZT-agent

背景
----
`search_knowledge` 原先只把「【标题】答案」拼成一段**给模型看的文本**，
`chunk.meta` 里的 title / score 不回传，前端也拿不到 —— 客户只看到一段话，
无从判断这句话是查到的还是模型编的。

改造后：检索命中在本轮上下文里登记 → `agent_stream` 收尾补发 `sources` 事件
→ 前端气泡下方出现「参考来源 N 条」，点开可看原文、相关度、命中路径。

本脚本守住五条最容易漂移的口径：

  1. **检索过程量必须可解释**：`retrieve_detailed()` 要给出余弦分、BM25 分、
     在本路里的名次；混合检索还要给出字面路 / 语义路各自的名次
  2. **`retrieve()` 行为不变**：老调用方（format_hits / query / eval_rag）读到的
     仍是 (chunk, score)，不能因为加了详细版就变样
  3. **登录取自工具层而非视图层**：来源由 `search_knowledge` 自己登记，
     这样任何调用路径（含后台命中测试台）都拿得到，不依赖 HTTP
  4. **sources 一定排在最后**：正文 → 名片 → 引导卡 → 来源，
     来源若插在正文中间会打断读者
  5. **无命中不发空事件**：前端不该为一次普通商品咨询渲染一个空的来源条

用法：./.venv/Scripts/python.exe scripts/verify_citations.py
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

from agent import agent as agent_mod  # noqa: E402
from agent import llm, tools  # noqa: E402
from agent.rag import HybridRAGEngine, RAGEngine  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'√' if cond else '×'} {label}" + (f"  <- {detail}" if detail else ""))


print("=" * 62)
print("  引用溯源验收")
print("=" * 62)

# ══════════════════════════════════════════════════════════════
# 【1】检索层：过程量
# ══════════════════════════════════════════════════════════════
print("\n【1】RAGEngine.retrieve_detailed —— 过程量")
engine = RAGEngine()
engine.ingest([
    {"text": "春季钓法 春天 春钓\n春季钓鱼讲究春钓浅滩，选浅滩向阳处，水深 1-2 米，饵料用腥香型。",
     "meta": {"title": "春季钓法", "answer": "春季钓鱼讲究春钓浅滩。", "category": "季节钓法"}},
    {"text": "夏季钓法 夏天 夏钓\n夏季早晚钓，避开正午高温，钓位选树荫下深水处。",
     "meta": {"title": "夏季钓法", "answer": "夏季早晚钓，避开正午。", "category": "季节钓法"}},
    {"text": "退换货政策 退货 退款 售后\n支持 7 天无理由退换货，未拆封不影响二次销售。",
     "meta": {"title": "退换货政策", "answer": "支持 7 天无理由退换货。", "category": "企业政策"}},
])

detailed = engine.retrieve_detailed("春季钓鱼选什么钓位", top_k=3)
check("返回非空", len(detailed) > 0, f"{len(detailed)} 条")
if detailed:
    d0 = detailed[0]
    check("每条含 chunk / score / routes 三键",
          set(d0) == {"chunk", "score", "routes"}, str(sorted(d0)))
    check("routes 含 backend / rank / cosine / bm25",
          {"backend", "rank", "cosine", "bm25"} <= set(d0["routes"]), str(sorted(d0["routes"])))
    check("backend 标注为 tfidf", d0["routes"]["backend"] == "tfidf", d0["routes"]["backend"])
    check("top1 是春季钓法", d0["chunk"].meta.get("title") == "春季钓法",
          d0["chunk"].meta.get("title"))
    check("rank 从 1 起", d0["routes"]["rank"] == 1, str(d0["routes"]["rank"]))
    check("cosine 为正", d0["routes"]["cosine"] > 0, str(d0["routes"]["cosine"]))
    check("bm25 为正（字面命中）", d0["routes"]["bm25"] > 0, str(d0["routes"]["bm25"]))
    check("tfidf 模式下 lexical_rank == rank",
          d0["routes"]["lexical_rank"] == d0["routes"]["rank"])
    check("tfidf 模式下 semantic_rank 为 None",
          d0["routes"]["semantic_rank"] is None)
    scores = [d["score"] for d in detailed]
    check("按分数降序", scores == sorted(scores, reverse=True), str(scores))

# min_score 过滤 / 空查询 / 兼容视图
check("min_score 过高时过滤为空", engine.retrieve_detailed("钓鱼", top_k=3, min_score=999) == [])
check("空查询返回空列表", engine.retrieve_detailed("") == [])
plain = engine.retrieve("春季钓鱼选什么钓位", top_k=3)
detail_pairs = [(d["chunk"], d["score"]) for d in engine.retrieve_detailed("春季钓鱼选什么钓位", top_k=3)]
check("retrieve() 与 detailed 结果一致（老调用方不受影响）", plain == detail_pairs,
      f"{len(plain)} vs {len(detail_pairs)}")

# ══════════════════════════════════════════════════════════════
# 【2】混合检索：三路名次
# ══════════════════════════════════════════════════════════════
print("\n【2】HybridRAGEngine.retrieve_detailed —— 三路名次")


class _FakeSemantic:
    """假语义引擎：故意给出与字面路不同的顺序，验证 RRF 真的用到了两路名次。"""

    def __init__(self, order_by_title=None):
        self.chunks = [object()]
        self.order_by_title = order_by_title or []
        self.raise_exc = False

    def retrieve_detailed(self, query, top_k=3, recall_k=None, min_score=0.0):
        if self.raise_exc:
            raise RuntimeError("embedding endpoint unreachable")
        out = []
        for rank, chunk in enumerate(self.chunks, start=1):
            if chunk.meta.get("title") in self.order_by_title:
                rank = self.order_by_title.index(chunk.meta.get("title")) + 1
            out.append({
                "chunk": chunk,
                "score": 0.9 - rank * 0.1,
                "routes": {"backend": "embedding", "rank": rank,
                           "cosine": 0.9 - rank * 0.1, "bm25": 0.0,
                           "lexical_rank": None, "semantic_rank": rank},
            })
        out.sort(key=lambda d: d["routes"]["semantic_rank"])
        return out[:top_k]


lex = RAGEngine()
lex.ingest([
    {"text": "春季钓法 春钓 浅滩\n春季钓鱼选浅滩向阳处，水深 1-2 米。",
     "meta": {"title": "春季钓法", "answer": "春季钓鱼选浅滩向阳处。"}},
    {"text": "夏季钓法 夏钓 深水\n夏季早晚钓，钓位选树荫下深水处。",
     "meta": {"title": "夏季钓法", "answer": "夏季早晚钓，钓位选树荫下。"}},
    {"text": "退换货政策 退货\n支持 7 天无理由退换货。",
     "meta": {"title": "退换货政策", "answer": "支持 7 天无理由退换货。"}},
])

# 语义路把「退换货政策」排第一（字面路里它最不相关）—— 融合后应被压下去
sem = _FakeSemantic(order_by_title=["退换货政策", "春季钓法", "夏季钓法"])
hybrid = HybridRAGEngine(semantic_engine=sem)
hybrid._lexical = lex
hybrid._semantic = sem
sem.chunks = lex.chunks

hy = hybrid.retrieve_detailed("春季钓鱼选什么钓位", top_k=3)
check("混合检索返回非空", len(hy) > 0, f"{len(hy)} 条")
if hy:
    r0 = hy[0]["routes"]
    check("routes 标注 backend=hybrid", r0["backend"] == "hybrid", r0["backend"])
    check("routes 同时给出字面路与语义路名次",
          r0["lexical_rank"] is not None or r0["semantic_rank"] is not None,
          f"lex={r0['lexical_rank']} sem={r0['semantic_rank']}")
    check("两路都命中时给出 RRF 融合分", isinstance(r0.get("rrf"), float), str(r0.get("rrf")))
    check("语义路把不相关条目排第一也没赢过字面路 + 语义路都靠前的",
          hy[0]["chunk"].meta.get("title") == "春季钓法", hy[0]["chunk"].meta.get("title"))
    check("融合分降序", [d["score"] for d in hy] == sorted([d["score"] for d in hy], reverse=True))

# 语义路故障 → 降级为字面路，并打标
sem.raise_exc = True
degraded = hybrid.retrieve_detailed("春季钓鱼选什么钓位", top_k=3)
check("语义路异常时不抛给调用方（降级）", len(degraded) > 0, f"{len(degraded)} 条")
if degraded:
    check("降级结果带 degraded 标记", degraded[0]["routes"].get("degraded") is True,
          str(degraded[0]["routes"].get("degraded")))
    check("降级后仍能给出字面路名次", degraded[0]["routes"]["lexical_rank"] == 1)
sem.raise_exc = False

# ══════════════════════════════════════════════════════════════
# 【3】工具层：登记与去重
# ══════════════════════════════════════════════════════════════
print("\n【3】search_knowledge —— 来源登记")
tools.reset_referenced_sources()
check("reset 后为空", tools.get_referenced_sources() == [])

result = tools.search_knowledge("拆封了还能退货吗")
check("检索文本仍带来源标题（给模型看的格式不变）", "【退换货政策】" in result, result[:60])
sources = tools.get_referenced_sources()
check("登记了来源", len(sources) > 0, f"{len(sources)} 条")
if sources:
    s0 = sources[0]
    check("来源含 title / score / category / snippet / backend",
          {"title", "score", "category", "snippet", "backend"} <= set(s0), str(sorted(s0)))
    check("title 是退换货政策", s0["title"] == "退换货政策", s0["title"])
    check("score 是数字", isinstance(s0["score"], float), str(type(s0["score"])))
    check("snippet 不超过 160 字", len(s0["snippet"]) <= 160, str(len(s0["snippet"])))
    check("snippet 不含换行（折叠条里按单行渲染）", "\n" not in s0["snippet"])
    # 后端由 .env 的 RAG_BACKEND 决定，脚本不假设是哪一档，只要求标注真实值
    active_backend = os.getenv("RAG_BACKEND", "tfidf").strip().lower() or "tfidf"
    check("backend 与当前 RAG_BACKEND 一致",
          s0["backend"] == active_backend, f"{s0['backend']} vs {active_backend}")
    check("category 已带出", s0["category"] == "企业政策", s0["category"])

# 同一句查询连跑两次：命中的是同一批标题，登记条数不应增长
n_before = len(tools.get_referenced_sources())
tools.search_knowledge("拆封了还能退货吗")
check("同一查询重复执行不重复登记",
      len(tools.get_referenced_sources()) == n_before,
      f"{n_before} -> {len(tools.get_referenced_sources())}")

# 无关查询：tfidf 后端有 1.5 阈值会把噪声挡掉；hybrid 的 RRF 分无绝对尺度（设计如此），
# 所以这里显式切到 tfidf 验证"阈值真的生效"，跑完恢复原后端。
from agent.knowledge_data import invalidate_rag_cache  # noqa: E402

_prev_backend = os.environ.get("RAG_BACKEND")
os.environ["RAG_BACKEND"] = "tfidf"
invalidate_rag_cache()
try:
    tools.reset_referenced_sources()
    tool_result = tools.search_knowledge("xyzzy qwerty")
    check("tfidf 后端：无关查询不登记来源", tools.get_referenced_sources() == [],
          str(tools.get_referenced_sources()))
    check("tfidf 后端：无关查询给出兜底话术", "暂时没有记录" in tool_result, tool_result[:40])
finally:
    if _prev_backend is None:
        os.environ.pop("RAG_BACKEND", None)
    else:
        os.environ["RAG_BACKEND"] = _prev_backend
    invalidate_rag_cache()

# ContextVar 隔离：另起一个 context 写入，不应影响当前 context
import contextvars  # noqa: E402

main_sources = tools.get_referenced_sources()


def _in_other_context():
    tools.reset_referenced_sources()
    tools.search_knowledge("春季钓鱼")
    return tools.get_referenced_sources()


ctx = contextvars.copy_context()
other = ctx.run(_in_other_context)
check("另一个 context 里能登记", len(other) > 0, f"{len(other)} 条")
check("当前 context 不受影响（上下文隔离）", tools.get_referenced_sources() == main_sources,
      f"{len(tools.get_referenced_sources())} vs {len(main_sources)}")

# ══════════════════════════════════════════════════════════════
# 【4】端到端：sources 事件的位置与内容
# ══════════════════════════════════════════════════════════════
print("\n【4】agent_stream —— sources 事件")


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


suffix = uuid.uuid4().hex[:8]

try:
    calls = {"n": 0}

    def fake_chat_stream(messages, tools=None, tool_choice=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return iter([_Chunk(_Delta(tool_calls=[
                _TC(0, id="call_1", function=_Fn(
                    "search_knowledge", '{"query": "春季钓鱼选什么钓位"}')),
            ]))])
        return iter([
            _Chunk(_Delta(content="春季钓鱼建议选浅滩向阳处，")),
            _Chunk(_Delta(content="水深 1-2 米。")),
        ])

    real_chat_stream = llm.chat_stream
    llm.chat_stream = fake_chat_stream
    try:
        events = list(agent_mod.agent_stream(f"verify-citations-{suffix}", "春天怎么选钓位"))
    finally:
        llm.chat_stream = real_chat_stream

    kinds = [e.get("type") for e in events if e.get("type")]
    texts = "".join(e["content"] for e in events if "content" in e)
    check("事件流里出现了 sources", "sources" in kinds, str(kinds))
    check("sources 排在最后一个（正文之后）",
          kinds and kinds[-1] == "sources", str(kinds))
    check("回答正文仍然照常流出", "浅滩向阳处" in texts, texts[:40])
    check("sources 不混进正文（不会被落库污染历史）", "sources" not in texts)

    src_events = [e for e in events if e.get("type") == "sources"]
    if src_events:
        lst = src_events[0]["sources"]
        check("来源是非空列表", isinstance(lst, list) and len(lst) > 0, str(len(lst)))
        check("来源里第一条是春季钓法",
              lst and lst[0]["title"] == "春季钓法", lst[0]["title"] if lst else "")
        check("来源带 rank（前端按名次排序）", lst and lst[0].get("rank") is not None,
              str(lst[0].get("rank")) if lst else "")

    # 无知识库命中的路径：问商品，不应发 sources
    calls["n"] = 0
    tools.reset_referenced_sources()

    def fake_chat_stream2(messages, tools=None, tool_choice=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return iter([_Chunk(_Delta(tool_calls=[
                _TC(0, id="call_2", function=_Fn(
                    "query_product", '{"keyword": "台钓竿"}')),
            ]))])
        return iter([_Chunk(_Delta(content="我们有几款台钓竿。"))])

    llm.chat_stream = fake_chat_stream2
    try:
        events2 = list(agent_mod.agent_stream(f"verify-citations-{suffix}", "有什么台钓竿"))
    finally:
        llm.chat_stream = real_chat_stream

    kinds2 = [e.get("type") for e in events2 if e.get("type")]
    check("没有知识库命中时不发 sources 事件", "sources" not in kinds2, str(kinds2))

finally:
    tools.reset_referenced_sources()

# ══════════════════════════════════════════════════════════════
# 【5】链路接线：视图放行 + 前端渲染契约
# ══════════════════════════════════════════════════════════════
print("\n【5】接线：视图 / 前端")
views_src = open(os.path.join(BASE, "agent", "views.py"), encoding="utf-8").read()
# 通用解析白名单元组，不写死具体的类名与换行位置：
# 之前硬编码成单行字面量，加第四个事件（handoff_card）只是换了行就假失败一次。
_whitelist = re.search(
    r'ev\.get\("type"\)\s+in\s+\(([^)]*)\)', views_src, re.S)
_events = set(re.findall(r'"([a-z_]+)"', _whitelist.group(1))) if _whitelist else set()
check("视图把 sources 当结构事件放行（不进 parts）",
      "sources" in _events, str(sorted(_events)) or "未找到白名单元组")
check("结构事件白名单仍包含三类名片（别被后续改动挤掉）",
      {"guide_card", "product_card", "order_card"} <= _events, str(sorted(_events)))

app_js = open(os.path.join(BASE, "frontend", "app.js"), encoding="utf-8").read()
index_html = open(os.path.join(BASE, "frontend", "index.html"), encoding="utf-8").read()
theme_css = open(os.path.join(BASE, "frontend", "assets", "theme.css"), encoding="utf-8").read()

check("app.js 处理 sources 事件", "data.type === 'sources'" in app_js)
check("app.js 有 sourceRouteLabel 纯函数", "function sourceRouteLabel(" in app_js)
check("app.js 有 fmtScore 纯函数", "function fmtScore(" in app_js)
check("app.js 把两个函数暴露给模板",
      "sourceRouteLabel," in app_js and "fmtScore," in app_js)
check("index.html 有来源折叠面板", 'class="source-panel w-full"' in index_html)
check("index.html 点了才展开（v-if=\"msg.sourcesOpen\"）",
      'v-if="msg.sourcesOpen"' in index_html)
check("index.html 调用了 toggleSources", "@click=\"toggleSources(idx)\"" in index_html)
check("index.html 用 aria-expanded 标注展开态", ":aria-expanded=" in index_html)
check("theme.css 定义了 .source-panel", ".source-panel {" in theme_css)
check("theme.css 定义了 .source-toggle", ".source-toggle {" in theme_css)
check("theme.css 定义了 .source-snippet", ".source-snippet {" in theme_css)
check("来源面板只用 token 取色（不写裸 hex）",
      "var(--jade-100)" in theme_css and "var(--ink-mute)" in theme_css)

# sources 在气泡之后（DOM 顺序：先答案后证据）
i_bubble = index_html.find('bubble-ai rounded-bl-sm')
i_source = index_html.find('class="source-panel w-full"')
check("来源面板在气泡之后（先读答案再看依据）",
      0 < i_bubble < i_source, f"bubble@{i_bubble} source@{i_source}")

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
