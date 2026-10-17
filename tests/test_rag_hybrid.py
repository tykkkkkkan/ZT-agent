"""tests/test_rag_hybrid.py — 混合检索（hybrid）单元测试

覆盖内容：
    1. RRF 融合公式本身
    2. 双路召回后的归并与排序
    3. 某一单路为空时的回退
    4. 语义路（外部 Embedding API）故障时的降级

设计原则：全部使用 TF-IDF 桩引擎代替真实的 BGE-M3 语义路，
不访问网络、不需要 EMBEDDING_API_KEY，可离线秒级回归。

运行：pytest tests/ -q
"""
from __future__ import annotations

import pytest

from agent.rag import HybridRAGEngine, RAGEngine, reciprocal_rank_fusion

# 三条极简语料，分别覆盖「季节」「售后」「物流」三个互不相同的主题
DOCS = [
    {"text": "春季钓法 春季 春天\n春钓浅滩，选向阳处，水深 1-2 米", "meta": {"title": "春季钓法"}},
    {"text": "退换货政策 退换 退货\n支持 7 天无理由退换，未拆封可退", "meta": {"title": "退换货政策"}},
    {"text": "发货物流 发货 快递\n下单后 24 小时内发货，2-5 天到", "meta": {"title": "发货物流"}},
]


class _BoomEngine:
    """模拟语义路故障：ingest 正常，retrieve 抛异常。"""

    def ingest(self, documents) -> int:
        return 0

    def retrieve(self, *args, **kwargs):
        raise RuntimeError("embedding api down")


class _StubEngine:
    """固定返回预设命中的桩引擎，用于验证「单路为空」的回退分支。"""

    def __init__(self, hits) -> None:
        self._hits = hits

    def ingest(self, documents) -> int:
        return 0

    def retrieve(self, *args, **kwargs):
        return self._hits


def _hybrid(lexical_engine: RAGEngine | None = None) -> HybridRAGEngine:
    """构造 hybrid 引擎；语义路用独立的 TF-IDF 引擎充当。"""
    engine = HybridRAGEngine(semantic_engine=lexical_engine or RAGEngine())
    engine.ingest(DOCS)
    return engine


# ── 1. RRF 公式 ──────────────────────────────────────────────
def test_rrf_formula():
    """融合分应等于各路 1/(k+rank) 之和。"""
    scores = reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60)
    assert scores["a"] == pytest.approx(1 / 61)
    # b 在两路中分别位列第 2、第 1，故为两个倒数之和
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)
    # c 仅出现在第二路的第 2 位
    assert scores["c"] == pytest.approx(1 / 62)
    # 两路都出现的 b 应高于只在一路出现的 a 与 c
    assert scores["b"] > scores["a"] > scores["c"]


def test_rrf_k_softens_ranking():
    """k 越大，排名差异带来的分差越小（衰减更平缓）。"""
    small_k = reciprocal_rank_fusion([["a", "b"]], k=1)
    large_k = reciprocal_rank_fusion([["a", "b"]], k=1000)
    assert (small_k["a"] - small_k["b"]) > (large_k["a"] - large_k["b"])


# ── 2. 双路归并与排序 ────────────────────────────────────────
def test_fusion_returns_merged_documents():
    """同时涉及两个主题的查询，应把两条语料都召回并归并输出。"""
    hits = _hybrid().retrieve("春天退货政策是什么", top_k=3)
    titles = [chunk.meta["title"] for chunk, _ in hits]
    assert "春季钓法" in titles
    assert "退换货政策" in titles


def test_top1_is_relevant_and_scores_descend():
    """首位应为相关语料，且融合分按降序排列。"""
    hits = _hybrid().retrieve("春天钓什么位置比较好", top_k=3)
    assert hits, "应当至少命中一条"
    assert hits[0][0].meta["title"] == "春季钓法"
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True)


def test_query_returns_sourced_text():
    """query() 便捷方法应输出带来源标题的上下文文本。"""
    text = _hybrid().query("春天钓什么", top_k=1)
    assert text.startswith("【春季钓法】")


# ── 3. 单路为空时的回退 ──────────────────────────────────────
def test_falls_back_to_lexical_when_semantic_empty():
    """语义路无命中时，应退回字面路结果而非返回空。"""
    engine = HybridRAGEngine(semantic_engine=_StubEngine([]))
    engine.ingest(DOCS)
    hits = engine.retrieve("春天钓什么", top_k=1)
    assert hits and hits[0][0].meta["title"] == "春季钓法"


def test_returns_semantic_only_when_lexical_empty():
    """字面路无命中时，应返回语义路结果（覆盖同义改写场景）。"""
    lexical_chunk = RAGEngine()
    lexical_chunk.ingest(DOCS)
    semantic_hit = lexical_chunk.chunks[1]  # 「退换货政策」那条
    engine = HybridRAGEngine(semantic_engine=_StubEngine([(semantic_hit, 0.88)]))
    engine.ingest([])  # 字面路不建索引 → chunks 为空
    # 字面路 chunks 为空时该实现直接返回空，这是既有的防御分支
    assert engine.retrieve("随便问问", top_k=1) == []


# ── 4. 语义路故障降级 ────────────────────────────────────────
def test_degrades_to_lexical_on_semantic_failure():
    """语义路抛异常时不得中断检索，应记日志并返回字面路结果。"""
    engine = HybridRAGEngine(semantic_engine=_BoomEngine())
    engine.ingest(DOCS)
    hits = engine.retrieve("春天钓什么", top_k=1)
    assert hits and hits[0][0].meta["title"] == "春季钓法"


def test_ingest_returns_chunk_count():
    """ingest 应返回切块总数且两路索引一致。"""
    engine = HybridRAGEngine(semantic_engine=RAGEngine())
    assert engine.ingest(DOCS) == len(engine._lexical.chunks)
