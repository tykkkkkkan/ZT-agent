"""agent/vectorstores/chroma_store.py — Chroma 向量库（替换 InMemoryVectorStore）

对齐实习 JD「向量数据库（Chroma/Milvus/FAISS）的基本使用」：
- 实现与 InMemoryVectorStore 相同的接口（add / search），
  上层 RAGEngine 无需改动即可切换到持久化向量库。
- 每次实例化都会重建一个空 collection（保证 ingest 幂等，与 InMemory 语义一致）。
- chromadb 为可选依赖，惰性导入：未安装/未启用 embedding 后端时不影响 TF-IDF 路径。

安装：pip install chromadb
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple


class ChromaStore:
    """基于 Chroma 的持久化向量存储（余弦空间）。"""

    def __init__(
        self,
        collection_name: str = "zhongyu_kb",
        persist_dir: Optional[str] = None,
    ) -> None:
        import chromadb  # 惰性导入（可选依赖）

        self._collection_name = collection_name
        self._persist_dir = persist_dir or os.getenv(
            "CHROMA_PERSIST_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "chroma_data")
        )
        self._client = chromadb.PersistentClient(path=self._persist_dir)
        # 重建：先删旧 collection 再建新的，保证每次 ingest 都是干净索引
        try:
            self._client.delete_collection(collection_name)
        except Exception:
            pass
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._count = 0

    def add(self, vector) -> None:
        """追加一个向量（稠密 list），自增 id 对应 chunk 下标。"""
        idx = self._count
        self._collection.add(ids=[str(idx)], embeddings=[vector])
        self._count += 1

    def search(self, query_vec, top_k: int) -> List[Tuple[int, float]]:
        """返回 [(index, similarity)]，按相似度降序，仅保留相似度 > 0。"""
        if self._count == 0 or top_k <= 0:
            return []
        res = self._collection.query(
            query_embeddings=[query_vec],
            n_results=min(top_k, self._count),
        )
        ids = res.get("ids", [[]])[0]
        distances = res.get("distances", [[]])[0]
        results: List[Tuple[int, float]] = []
        for id_str, dist in zip(ids, distances):
            sim = 1.0 - float(dist)  # 余弦距离 → 相似度
            if sim > 0:
                results.append((int(id_str), sim))
        results.sort(key=lambda x: -x[1])
        return results
