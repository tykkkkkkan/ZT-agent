"""
test_rag.py — RAG 引擎独立单测（不依赖 Django / MySQL / API Key）

运行：
    .venv/Scripts/python.exe test_rag.py

覆盖：
    1. 文档切块（chunking）
    2. TF-IDF 向量化 + 余弦相似度
    3. 召回 + BM25 重排
    4. 企业知识库 RAG 检索（search_knowledge 数据源）
"""
import os
import sys

# 允许直接 import agent 包（agent/__init__.py 为空，rag.py 纯标准库）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.rag import RAGEngine, chunk_text, tokenize
from agent.knowledge_data import get_knowledge_rag


def test_chunking():
    text = "第一句。第二句，比较长，这里继续写很多内容。" * 10
    chunks = chunk_text(text, max_chars=80, overlap=20)
    assert chunks, "切块结果为空"
    assert all(len(c) <= 80 for c in chunks), "存在超过 max_chars 的块"
    print(f"[PASS] 切块：共 {len(chunks)} 块，单块最长 {max(len(c) for c in chunks)} 字符")


def test_tokenize():
    toks = tokenize("红虫颗粒 春季钓鲤鱼 Test123")
    assert "红虫" in toks and "春季" in toks and "test123" in toks
    print(f"[PASS] 分词：{toks[:12]}...")


def test_retrieval():
    docs = [
        {"text": "退换货 售后 无理由 我们支持7天无理由退换货，未拆封可退", "meta": {"title": "售后政策"}},
        {"text": "发货 物流 快递 下单后24小时内发货，2-5天可到", "meta": {"title": "发货政策"}},
        {"text": "春季 春钓 春天钓鱼选浅滩向阳处", "meta": {"title": "春季钓法"}},
        {"text": "调漂 浮漂 半水调漂先调5目", "meta": {"title": "调漂技巧"}},
    ]
    rag = RAGEngine()
    n = rag.ingest(docs)
    assert n >= len(docs)
    print(f"[PASS] 入库：{n} 个切块")

    hits = rag.retrieve("拆封了能退吗", top_k=2)
    assert hits, "退换货问题未召回任何结果"
    top_title = hits[0][0].meta.get("title")
    assert top_title == "售后政策", f"召回首位应为售后政策，实际 {top_title}"
    print(f"[PASS] 召回+重排：'拆封了能退吗' → 首位【{top_title}】(分 {hits[0][1]:.3f})")


def test_knowledge_base():
    rag = get_knowledge_rag()
    # 政策类
    hits = rag.retrieve("饵料拆封了还能退货吗", top_k=1)
    assert hits, "售后问题未召回"
    print(f"[PASS] 知识库-售后：→ 【{hits[0][0].meta.get('title')}】")

    # 钓鱼知识类
    hits = rag.retrieve("春天钓鲤鱼要注意什么", top_k=1)
    assert hits, "钓鱼知识未召回"
    print(f"[PASS] 知识库-钓法：→ 【{hits[0][0].meta.get('title')}】")


if __name__ == "__main__":
    print("=" * 60)
    print("RAG 引擎单测")
    print("=" * 60)
    test_tokenize()
    test_chunking()
    test_retrieval()
    test_knowledge_base()
    print("=" * 60)
    print("全部通过 ✅")
