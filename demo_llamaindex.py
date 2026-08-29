"""LlamaIndex 对照 demo —— 用框架搭一个 RAG 问答（证明"我也会用框架"）

背景：ZT-agent 的 RAG 是自研的（切块/TF-IDF/余弦/BM25），这个 demo 用 LlamaIndex
框架实现同样的"文档 → 检索 → 问答"，作为对照，说明"先手写理解原理，再上框架提效"。

说明：llama-index 0.14 自带的 OpenAI 组件用枚举限制了 model 名，对第三方
OpenAI 兼容服务（DeepSeek / 硅基流动）支持变差，故这里用 CustomLLM / BaseEmbedding
自定义扩展——这恰好也演示了 llama-index 的可扩展性。

依赖：llama-index llama-index-llms-openai llama-index-embeddings-openai
运行：.venv/Scripts/python.exe demo_llamaindex.py
"""
import os
from typing import List

from dotenv import load_dotenv
from pydantic import Field

load_dotenv()

from llama_index.core import Document, Settings, VectorStoreIndex
from llama_index.core.embeddings import BaseEmbedding
from llama_index.core.llms import CompletionResponse, LLMMetadata
from llama_index.core.llms.callbacks import llm_completion_callback
from llama_index.core.llms.custom import CustomLLM


class DeepSeekLLM(CustomLLM):
    """自定义 LLM（DeepSeek，OpenAI 兼容）。"""

    model_name: str = Field(default="deepseek-chat")
    api_key: str = Field(default="")
    api_base: str = Field(default="https://api.deepseek.com")

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(context_window=8192, num_output=4096, model_name=self.model_name)

    @llm_completion_callback()
    def complete(self, prompt: str, formatted: bool = False, **kwargs):
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.api_base)
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
        )
        return CompletionResponse(text=resp.choices[0].message.content)

    @llm_completion_callback()
    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        yield self.complete(prompt)


class SiliconFlowEmbedding(BaseEmbedding):
    """自定义 OpenAI 兼容 embedding（硅基流动 BAAI/bge-m3）。"""

    model_name: str = Field(default="BAAI/bge-m3")
    api_key: str = Field(default="")
    api_base: str = Field(default="https://api.siliconflow.cn/v1")

    def _get_query_embedding(self, query: str) -> List[float]:
        return self._embed(query)

    def _get_text_embedding(self, text: str) -> List[float]:
        return self._embed(text)

    async def _aget_query_embedding(self, query: str) -> List[float]:
        return self._embed(query)

    async def _aget_text_embedding(self, text: str) -> List[float]:
        return self._embed(text)

    def _embed(self, text: str) -> List[float]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.api_base)
        return client.embeddings.create(model=self.model_name, input=text).data[0].embedding


# 配置 LLM 与 Embedding
Settings.llm = DeepSeekLLM(api_key=os.getenv("DEEPSEEK_API_KEY"))
Settings.embed_model = SiliconFlowEmbedding(api_key=os.getenv("EMBEDDING_API_KEY"))

# 3 条企业知识文档（等价于自研 knowledge_data.py 里的语料）
docs = [
    Document(text="我们支持 7 天无理由退换货，只要产品未拆封、包装完好、不影响二次销售。"),
    Document(text="一般下单后 24 小时内发货，默认中通/圆通快递，全国大部分地区 2-5 天可到。"),
    Document(text="满 99 元包邮（偏远地区除外），未满 99 元收取 8 元运费。"),
]

# 建索引 + 查询（等价于自研 RAGEngine 的 ingest + retrieve + 生成）
index = VectorStoreIndex.from_documents(docs)
query_engine = index.as_query_engine()

print("问：拆封了能退吗？")
print("答：", query_engine.query("拆封了能退吗？"))
