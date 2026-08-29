"""agent/embeddings/openai_embedder.py — 真实语义 Embedding（替换 TF-IDF 稀疏向量）

对齐实习 JD「了解 Embedding 模型及相似度检索」：
- 实现与 TfidfEmbedder 相同的接口（fit / transform / cosine），
  上层 RAGEngine 无需改动即可切换到语义向量。
- 通过环境变量配置：
    EMBEDDING_MODEL     默认 text-embedding-3-small（OpenAI 兼容均可，如 BAAI/bge-m3）
    EMBEDDING_BASE_URL  默认 https://api.openai.com/v1（硅基流动/阿里云等兼容端点可覆盖）
    EMBEDDING_API_KEY   embedding 服务的 key
- 带内存缓存：同一文本只调用一次 API（ingest + 检索可复用）。
- 注意：DeepSeek 目前无公开 embedding 接口，故默认指向 OpenAI 兼容端点，
  用户可自行切换到任何 OpenAI 兼容的 embedding 服务。
"""
from __future__ import annotations

import math
import os
from typing import Dict, List, Optional


class OpenaiEmbedder:
    """OpenAI 兼容 Embedding（稠密向量）。"""

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        self.base_url = base_url or os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
        self.api_key = api_key or os.getenv("EMBEDDING_API_KEY", "")
        self._cache: Dict[str, List[float]] = {}

    def fit(self, corpus: List[str]) -> "OpenaiEmbedder":
        """真实 Embedding 无需训练，占位保持接口一致。"""
        return self

    def transform(self, text: str) -> List[float]:
        """调用 embedding API 返回稠密向量，带缓存。"""
        text = text or ""
        if text in self._cache:
            return self._cache[text]
        if not self.api_key:
            raise RuntimeError(
                "EMBEDDING_API_KEY 未配置。请在 .env 设置，或改用默认 TF-IDF 后端。"
            )
        from openai import OpenAI  # 惰性导入，未启用时不影响 TF-IDF 路径

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        resp = client.embeddings.create(model=self.model, input=text)
        vec: List[float] = resp.data[0].embedding
        self._cache[text] = vec
        return vec

    @staticmethod
    def cosine(a, b) -> float:
        """稠密向量余弦相似度。"""
        if not a or not b:
            return 0.0
        if isinstance(a, dict) or isinstance(b, dict):
            # 兼容稀疏向量（dict）的情况，退回点积逻辑
            keys = set(a) & set(b)
            dot = sum(a[k] * b[k] for k in keys)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
        else:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
