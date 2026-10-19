"""
agent/rag.py — 轻量级 RAG（检索增强生成）引擎

纯标准库实现，零第三方依赖，完整演示 RAG Pipeline 的四个阶段：
    1. 文档切块（chunking）
    2. 向量化存储（TF-IDF 稀疏向量 + 内存向量库）
    3. 召回（余弦相似度 Top-N）
    4. 重排（BM25 重排序）

可插拔架构（对齐实习 JD「向量数据库 / Embedding 模型」方向）：
    - Embedder 接口：默认 TfidfEmbedder（字符 n-gram 稀疏向量），可替换为真实
      Embedding API（OpenAI / DashScope text-embedding 等），只需实现
      fit(corpus) / transform(text) / cosine(a, b)。
    - VectorStore 接口：默认 InMemoryVectorStore，可替换为 Chroma / FAISS / Milvus，
      只需实现 add(vector) / search(vector, top_k)。

用法示例：
    rag = RAGEngine()
    rag.ingest([{"text": "退换货政策：签收7天内未拆封可退……", "meta": {"title": "售后政策"}}])
    hits = rag.retrieve("拆封了能退吗", top_k=3)
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# BM25 重排权重：cosine 召回分与 BM25 字面分的混合比例（0~1），可经环境变量调节
RAG_BM25_WEIGHT = float(os.getenv("RAG_BM25_WEIGHT", "0.5"))

# RRF 融合常数 k：文献常用 60，控制"排名靠前优先"的衰减速度
RRF_K = float(os.getenv("RAG_RRF_K", "60"))

# ---------------------------------------------------------------------------
# 分词：中文按「单字 + 相邻 bigram」切分，英文/数字按词切分（零依赖）
# ---------------------------------------------------------------------------
_ALNUM_RE = re.compile(r"[a-zA-Z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """把文本切分为检索 token 列表。

    中文采用「字符 bigram」切分（避免依赖 jieba 等分词库）。
    相比单字（unigram），bigram 区分度更高、噪声更低，是零依赖中文检索的标准做法；
    英文/数字按连续词提取。用于 TF-IDF 与 BM25 的统一底层表示。
    """
    text = (text or "").lower()
    tokens: List[str] = []
    # 英文 / 数字连续串
    for m in _ALNUM_RE.finditer(text):
        tokens.append(m.group())
    # 中文连续段 → 相邻 bigram（单字段直接保留单字）
    for m in _CJK_RE.finditer(text):
        seg = m.group()
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


# ---------------------------------------------------------------------------
# 文档切块
# ---------------------------------------------------------------------------
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])")


def chunk_text(text: str, max_chars: int = 200, overlap: int = 40) -> List[str]:
    """按句子边界把长文本切成不超过 max_chars 的块，块间保留 overlap 字符重叠。

    重叠用于避免切块破坏语义边界（例如答案被从中截断）。
    """
    text = (text or "").strip()
    if not text:
        return []
    sentences = [s for s in _SENT_SPLIT_RE.split(text) if s.strip()]

    chunks: List[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) <= max_chars:
            buf += s
            continue
        if buf:
            chunks.append(buf)
        # 单句超过上限 → 硬切并保留重叠
        while len(s) > max_chars:
            chunks.append(s[:max_chars])
            s = s[max_chars - overlap:]
        buf = s
    if buf:
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------------------
# Embedder：TF-IDF 稀疏向量
# ---------------------------------------------------------------------------
class TfidfEmbedder:
    """TF-IDF 向量化器（稀疏向量，dict: {token_id: weight}）。

    作为默认 Embedder，零依赖即可产出可计算余弦相似度的向量；
    如需语义级向量，替换为真实 Embedding API 实现同样接口即可。
    """

    def __init__(self) -> None:
        self.vocab: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self.n_docs: int = 0

    def fit(self, corpus: List[str]) -> "TfidfEmbedder":
        df: Counter = Counter()
        for doc in corpus:
            for t in set(tokenize(doc)):
                df[t] += 1
        self.n_docs = max(len(corpus), 1)
        self.vocab = {t: i for i, t in enumerate(sorted(df))}
        self.idf = {t: math.log((self.n_docs + 1) / (df[t] + 1)) + 1.0 for t in df}
        return self

    def transform(self, text: str) -> Dict[int, float]:
        tf: Counter = Counter(tokenize(text))
        total = sum(tf.values())
        if total == 0:
            return {}
        vec: Dict[int, float] = {}
        for t, c in tf.items():
            idx = self.vocab.get(t)
            if idx is not None:
                vec[idx] = (c / total) * self.idf[t]
        return vec

    @staticmethod
    def cosine(a: Dict[int, float], b: Dict[int, float]) -> float:
        if not a or not b:
            return 0.0
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot = sum(a[k] * b[k] for k in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)


# ---------------------------------------------------------------------------
# 重排器：BM25
# ---------------------------------------------------------------------------
class BM25:
    """BM25 评分器，用于「召回后重排」阶段（lexical rerank）。"""

    def __init__(self, corpus: List[List[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.corpus = corpus
        self.n = len(corpus)
        self.doc_len = [len(d) for d in corpus]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 1.0
        self.df: Counter = Counter()
        for d in corpus:
            for t in set(d):
                self.df[t] += 1
        self.k1 = k1
        self.b = b

    def score(self, query_tokens: List[str], idx: int) -> float:
        if idx >= self.n or not self.corpus[idx]:
            return 0.0
        d = self.corpus[idx]
        dl = self.doc_len[idx]
        tf = Counter(d)
        s = 0.0
        for t in query_tokens:
            if t not in tf:
                continue
            df = self.df[t]
            idf = math.log((self.n - df + 0.5) / (df + 0.5) + 1.0)
            num = tf[t] * (self.k1 + 1)
            den = tf[t] + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            s += idf * num / den
        return s


# ---------------------------------------------------------------------------
# 文档块 / 向量存储
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    id: str
    text: str
    meta: Dict = field(default_factory=dict)
    tokens: List[str] = field(default_factory=list)


class InMemoryVectorStore:
    """内存向量库：add / search。可替换为 Chroma / FAISS / Milvus。"""

    def __init__(self) -> None:
        self._vectors: List[Dict[int, float]] = []

    def add(self, vector: Dict[int, float]) -> None:
        self._vectors.append(vector)

    def search(self, query_vec: Dict[int, float], top_k: int) -> List[Tuple[int, float]]:
        """返回 [(index, cosine_score), ...]，按分数降序，仅保留 score > 0。"""
        scored = []
        for i, v in enumerate(self._vectors):
            s = TfidfEmbedder.cosine(query_vec, v)
            if s > 0:
                scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


def reciprocal_rank_fusion(
    ranked_lists: List[List[str]], k: float = RRF_K
) -> Dict[str, float]:
    """RRF（倒数排名融合）合并多路排序结果，返回 {文档 id: 融合分}。

    各路分数尺度不可比（字面路是余弦+BM25 混合分，语义路是 0~1 余弦）时，
    只取"排名"做融合：score = Σ 1/(k + rank_i)，k 越大排名靠前的文档优势越平缓。
    """
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def format_hits(hits: List[Tuple["Chunk", float]]) -> str:
    """把检索命中列表拼成带来源标题的上下文文本（供工具层直接使用）。"""
    if not hits:
        return ""
    parts = []
    for i, (chunk, score) in enumerate(hits, 1):
        title = chunk.meta.get("title", "")
        label = f"【{title}】" if title else f"【片段{i}】"
        parts.append(f"{label}（相关度 {score:.3f}）\n{chunk.text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# RAG 引擎：切块 → 向量化 → 召回 → 重排
# ---------------------------------------------------------------------------
class RAGEngine:
    """轻量 RAG 引擎，串联完整 Pipeline。"""

    def __init__(
        self,
        embedder: Optional[TfidfEmbedder] = None,
        chunk_size: int = 200,
        chunk_overlap: int = 40,
        store_factory=None,
    ) -> None:
        self.embedder = embedder or TfidfEmbedder()
        self.store_factory = store_factory or InMemoryVectorStore
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunks: List[Chunk] = []
        self._store = self.store_factory()
        self._bm25: Optional[BM25] = None
        # 语义向量（稠密）与 TF-IDF 稀疏向量的召回/重排策略不同：
        # 语义模式直接用语向量库的余弦相似度排序；TF-IDF 模式用余弦召回 + BM25 重排。
        self._semantic = not isinstance(self.embedder, TfidfEmbedder)

    def ingest(self, documents: List) -> int:
        """写入文档并重建索引。

        documents 每项可为：
            - str：纯文本
            - dict：{"text": str, "meta": {...}}
        返回切块总数。
        """
        new_chunks: List[Chunk] = []
        for i, doc in enumerate(documents):
            if isinstance(doc, str):
                text, meta = doc, {}
            else:
                text = doc.get("text", "")
                meta = doc.get("meta", {}) or {}
            for piece in chunk_text(text, self.chunk_size, self.chunk_overlap):
                new_chunks.append(
                    Chunk(id=f"{i}-{len(new_chunks)}", text=piece, meta=meta, tokens=tokenize(piece))
                )

        if not new_chunks:
            return 0

        self.embedder.fit([c.text for c in new_chunks])
        self.chunks = new_chunks
        self._store = self.store_factory()
        for c in new_chunks:
            self._store.add(self.embedder.transform(c.text))
        self._bm25 = BM25([c.tokens for c in new_chunks])
        return len(new_chunks)

    def retrieve_detailed(
        self,
        query: str,
        top_k: int = 3,
        recall_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> List[dict]:
        """检索入口（带过程信息）：返回 [{"chunk", "score", "routes"}]。

        routes 记录这一条是怎么被排上来的 —— 余弦分、BM25 分、在本路里的名次。
        界面要展示「这条来自知识库第几条、凭什么排第一」就必须拿到这些过程量，
        只返回 (chunk, score) 是看不到的（引用溯源 / 命中测试台都依赖它）。

        min_score：低于该分数的结果被丢弃，用于过滤无关查询的低分噪声
        （默认 0 表示不启用，向后兼容）。调用方可传如 1.5 做阈值过滤。
        """
        if not self.chunks:
            return []

        recall_k = recall_k or max(top_k * 2, 6)
        qvec = self.embedder.transform(query)

        # 阶段一：余弦相似度召回
        recall = self._store.search(qvec, recall_k)

        # 阶段二：重排
        if self._semantic:
            # 语义向量模式：向量库返回的已是余弦相似度（0~1），直接用它排序。
            # BM25 是字面匹配，与语义尺度不匹配，介入后会用字面噪声主导排序，故此处不用。
            reranked = [(cos_s, idx, cos_s, 0.0) for idx, cos_s in recall]
        else:
            # TF-IDF 模式：余弦召回 + BM25 字面重排（混合打分）
            qtokens = tokenize(query)
            reranked: List[Tuple[float, int, float, float]] = []
            for idx, cos_s in recall:
                bm = self._bm25.score(qtokens, idx) if self._bm25 else 0.0
                final = cos_s + bm * RAG_BM25_WEIGHT
                reranked.append((final, idx, cos_s, bm))
        reranked.sort(key=lambda x: -x[0])

        backend = "embedding" if self._semantic else "tfidf"
        out: List[dict] = []
        for rank, (final, idx, cos_s, bm) in enumerate(reranked[:top_k], start=1):
            if final <= 0:
                continue
            if min_score > 0 and final < min_score:
                continue
            out.append({
                "chunk": self.chunks[idx],
                "score": final,
                "routes": {
                    "backend": backend,
                    # 最终名次（1 起）与该名次对应的各路分数
                    "rank": rank,
                    "cosine": round(cos_s, 4),
                    "bm25": round(bm, 4),
                    "lexical_rank": None if self._semantic else rank,
                    "semantic_rank": rank if self._semantic else None,
                },
            })
        return out

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        recall_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> List[Tuple[Chunk, float]]:
        """检索入口：返回 [(Chunk, score)]（`retrieve_detailed` 的简化视图）。"""
        return [
            (d["chunk"], d["score"])
            for d in self.retrieve_detailed(query, top_k=top_k,
                                            recall_k=recall_k, min_score=min_score)
        ]

    def query(self, query: str, top_k: int = 3) -> str:
        """便捷方法：返回检索到的上下文的拼接文本（供工具层直接使用）。"""
        return format_hits(self.retrieve(query, top_k=top_k))


# ---------------------------------------------------------------------------
# 混合检索引擎：TF-IDF/BM25 字面路 + 语义向量路，RRF 融合
# ---------------------------------------------------------------------------
class HybridRAGEngine:
    """双路混合检索（hybrid retrieval）：两路并行召回 + RRF 融合排序。

    与 RAGEngine 保持相同接口（ingest / retrieve / query），上层可无缝替换：
    - 字面路：TF-IDF 余弦召回 + BM25 重排（内部自建 RAGEngine）
    - 语义路：BGE-M3 稠密向量 + Chroma（由调用方构建好传入 semantic_engine）
    两路 ingest 同一批文档、切块逻辑相同，因此 Chunk.id 天然对齐，融合时按 id 归并。

    容错设计：语义路依赖外部 Embedding API，检索失败时记日志并降级为字面路结果，
    不阻断对话（对齐"外部调用失败必须有降级路径"约定）。
    """

    def __init__(self, semantic_engine: RAGEngine, rrf_k: float = RRF_K) -> None:
        self._lexical = RAGEngine()          # 字面路：零依赖
        self._semantic = semantic_engine     # 语义路：BGE-M3 + Chroma
        self._rrf_k = rrf_k

    def ingest(self, documents: List) -> int:
        """向双路写同一批文档并各自建索引，返回切块总数。"""
        n = self._lexical.ingest(documents)
        self._semantic.ingest(documents)
        return n

    def retrieve_detailed(
        self,
        query: str,
        top_k: int = 3,
        recall_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> List[dict]:
        """双路并行召回 + RRF 融合，返回带三路名次的明细。

        routes 里同时给出 `lexical_rank` / `semantic_rank` / `rrf`，
        界面可以直观看到"这一条字面路排第 2、语义路排第 1，融合后排第 1"——
        这是解释混合检索为什么有效的唯一证据，光看融合分看不出来。

        容错：语义路异常降级为字面路（记日志），单路为空时退回另一路原生排序。
        """
        if not self._lexical.chunks:
            return []

        recall_k = recall_k or max(top_k * 2, 6)
        lex_hits = self._lexical.retrieve_detailed(query, top_k=recall_k)
        try:
            sem_hits = self._semantic.retrieve_detailed(query, top_k=recall_k)
        except Exception as exc:
            logger.warning("语义路检索失败，降级为字面路：%s", exc)
            sem_hits = []

        lex_rank = {d["chunk"].id: i for i, d in enumerate(lex_hits, start=1)}
        sem_rank = {d["chunk"].id: i for i, d in enumerate(sem_hits, start=1)}

        # 单路为空 → 直接返回另一路（保留其原生分数，便于上层阈值过滤）
        if not lex_hits or not sem_hits:
            single = lex_hits or sem_hits
            degraded = not sem_hits
            out: List[dict] = []
            for d in single[:top_k]:
                routes = dict(d["routes"])
                routes.update({
                    "backend": "hybrid",
                    "lexical_rank": lex_rank.get(d["chunk"].id),
                    "semantic_rank": sem_rank.get(d["chunk"].id),
                    "rrf": None,
                    "degraded": degraded,
                })
                out.append({"chunk": d["chunk"], "score": d["score"], "routes": routes})
            return out

        rrf = reciprocal_rank_fusion(
            [[d["chunk"].id for d in lex_hits], [d["chunk"].id for d in sem_hits]],
            self._rrf_k,
        )
        id2chunk = {d["chunk"].id: d["chunk"] for d in lex_hits}
        id2chunk.update({d["chunk"].id: d["chunk"] for d in sem_hits})
        # 各路原始分（只用于展示，不参与融合）
        id2scores: Dict[str, Dict[str, float]] = {}
        for d in lex_hits:
            id2scores.setdefault(d["chunk"].id, {})["lexical_score"] = round(d["score"], 4)
        for d in sem_hits:
            id2scores.setdefault(d["chunk"].id, {})["semantic_score"] = round(d["score"], 4)

        fused = sorted(
            ((id2chunk[cid], s, cid) for cid, s in rrf.items() if cid in id2chunk),
            key=lambda x: -x[1],
        )

        results: List[dict] = []
        for rank, (chunk, score, cid) in enumerate(fused[:top_k], start=1):
            if min_score > 0 and score < min_score:
                continue
            raw = id2scores.get(cid, {})
            results.append({
                "chunk": chunk,
                "score": score,
                "routes": {
                    "backend": "hybrid",
                    "rank": rank,
                    "lexical_rank": lex_rank.get(cid),
                    "semantic_rank": sem_rank.get(cid),
                    "lexical_score": raw.get("lexical_score"),
                    "semantic_score": raw.get("semantic_score"),
                    "rrf": round(score, 5),
                    "degraded": False,
                },
            })
        return results

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        recall_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> List[Tuple[Chunk, float]]:
        """双路并行召回 + RRF 融合（`retrieve_detailed` 的简化视图）。

        返回 (Chunk, RRF 融合分)；任一单路为空时退回另一路的原始排序；
        语义路异常时降级为字面路（记日志）。min_score 对 RRF 尺度无意义，
        默认 0 不过滤，调用方无需传阈值。
        """
        return [
            (d["chunk"], d["score"])
            for d in self.retrieve_detailed(query, top_k=top_k,
                                            recall_k=recall_k, min_score=min_score)
        ]

    def query(self, query: str, top_k: int = 3) -> str:
        """便捷方法：返回检索到的上下文的拼接文本（供工具层直接使用）。"""
        return format_hits(self.retrieve(query, top_k=top_k))
