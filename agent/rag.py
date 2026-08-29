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

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
    ) -> None:
        self.embedder = embedder or TfidfEmbedder()
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunks: List[Chunk] = []
        self._store = InMemoryVectorStore()
        self._bm25: Optional[BM25] = None

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
        self._store = InMemoryVectorStore()
        for c in new_chunks:
            self._store.add(self.embedder.transform(c.text))
        self._bm25 = BM25([c.tokens for c in new_chunks])
        return len(new_chunks)

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        recall_k: Optional[int] = None,
        min_score: float = 0.0,
    ) -> List[Tuple[Chunk, float]]:
        """检索入口：召回（余弦 Top-N）+ 重排（BM25），返回 [(Chunk, score)]。

        min_score：低于该相似度分数的结果被丢弃，用于过滤无关查询的低分噪声
        （默认 0 表示不启用，向后兼容）。调用方可传如 1.5 做阈值过滤。
        """
        if not self.chunks:
            return []

        recall_k = recall_k or max(top_k * 2, 6)
        qvec = self.embedder.transform(query)

        # 阶段一：余弦相似度召回
        recall = self._store.search(qvec, recall_k)

        # 阶段二：BM25 重排（lexical rerank），与余弦分数合并
        qtokens = tokenize(query)
        reranked: List[Tuple[float, int]] = []
        for idx, cos_s in recall:
            bm = self._bm25.score(qtokens, idx) if self._bm25 else 0.0
            final = cos_s + bm * 0.5
            reranked.append((final, idx))
        reranked.sort(key=lambda x: -x[0])

        results = [(self.chunks[idx], s) for s, idx in reranked[:top_k] if s > 0]
        if min_score > 0:
            results = [(c, s) for c, s in results if s >= min_score]
        return results

    def query(self, query: str, top_k: int = 3) -> str:
        """便捷方法：返回检索到的上下文的拼接文本（供工具层直接使用）。"""
        hits = self.retrieve(query, top_k=top_k)
        if not hits:
            return ""
        parts = []
        for i, (chunk, score) in enumerate(hits, 1):
            title = chunk.meta.get("title", "")
            label = f"【{title}】" if title else f"【片段{i}】"
            parts.append(f"{label}（相关度 {score:.3f}）\n{chunk.text}")
        return "\n\n".join(parts)
