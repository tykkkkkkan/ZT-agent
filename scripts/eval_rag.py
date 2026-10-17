# -*- coding: utf-8 -*-
"""scripts/eval_rag.py — RAG 检索效果评估（毕业论文实验数据）

评估对象
    `agent.rag.RAGEngine` 在「企业知识库」上的检索效果。
    知识库语料 = `agent.knowledge_data.KNOWLEDGE_BASE`（22 条），
    文档文本按 `_load_docs()` 静态分支构造：`标题 + 关键词 + "\\n" + 答案`。

指标体系（文档级，按条目去重后取前 k 个）
    Hit@1 / Hit@3 / Hit@5   正确条目是否出现在前 k 个检索结果中
    MRR                     首个正确条目排名倒数的平均值
    歧义查询用 `also` 标注可接受答案集合，命中任一即算正确。

实验组（消融）
    tfidf        TF-IDF 稀疏向量 + 纯余弦召回（关闭 BM25 重排）
    tfidf+bm25   TF-IDF 稀疏向量 + 余弦召回 + BM25 重排（`rag.py` 代码默认，w=0.5）
    semantic     BAAI/bge-m3 语义向量 + Chroma 余弦（项目 `.env` 的实际配置）
    hybrid       TF-IDF+BM25 与 BGE-M3 双路并行召回 + RRF 融合（`RAG_BACKEND=hybrid`）

用法
    python scripts/eval_rag.py                     # TF-IDF 两组（零依赖、瞬时）
    python scripts/eval_rag.py --with-semantic     # 追加语义组与 hybrid 融合组（需 EMBEDDING_API_KEY + 网络）
    python scripts/eval_rag.py --sweep             # 附 BM25 重排权重敏感性扫描
    python scripts/eval_rag.py --out docs/rag_eval_results.json
    python scripts/eval_rag.py --build-chart docs/rag_eval_chart.png
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:                                   # 读取 .env（EMBEDDING_API_KEY 等）
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except ImportError:
    pass

import agent.rag as _rag  # noqa: E402
from agent.knowledge_data import KNOWLEDGE_BASE  # noqa: E402
from agent.rag import RAGEngine  # noqa: E402

DOC_K = 5          # 评估取前 k 个不同条目
K_ALL = 500        # 单次检索取回全部候选，再按条目去重


# ══════════════════════════════════════════════════════════════
# 评测集：44 条真实用户口吻的查询（含口语化改写，用于区分字面/语义匹配）
# ══════════════════════════════════════════════════════════════
QUERIES = [
    {"q": "春天钓鱼选什么位置比较好", "gold": "春季钓法"},
    {"q": "三四月份什么时候出钓最合适", "gold": "春季钓法"},
    {"q": "夏天中午太热还能钓鱼吗", "gold": "夏季钓法"},
    {"q": "夏季钓鱼钓多深", "gold": "夏季钓法"},
    {"q": "金九银十说的是钓鱼的什么季节", "gold": "秋季钓法"},
    {"q": "秋天是不是最好钓的时候", "gold": "秋季钓法"},
    {"q": "冬天还能钓到鱼吗", "gold": "冬季钓法"},
    {"q": "冰钓要注意什么", "gold": "冬季钓法"},
    {"q": "鲤鱼要用多大的钩", "gold": "钓鲤鱼"},
    {"q": "钓鲤鱼选什么样的水域", "gold": "钓鲤鱼"},
    {"q": "鲫鱼一般在多深的水里", "gold": "钓鲫鱼"},
    {"q": "钓鲫鱼浮漂怎么调最灵", "gold": "钓鲫鱼", "also": ["调漂技巧"]},
    {"q": "草鱼活动在水层哪个位置", "gold": "钓草鱼"},
    {"q": "钓草鱼用什么打窝", "gold": "钓草鱼", "also": ["打窝技巧"]},
    {"q": "鲢鳙喜欢什么味型的饵", "gold": "钓鲢鳙"},
    {"q": "胖头鱼怎么钓", "gold": "钓鲢鳙"},
    {"q": "黄尾鱼用什么型号的钩", "gold": "钓黄尾"},
    {"q": "钓黄尾选什么水底环境", "gold": "钓黄尾"},
    {"q": "打窝一次放多少窝料合适", "gold": "打窝技巧"},
    {"q": "打完窝多久能发窝", "gold": "打窝技巧"},
    {"q": "半水调漂怎么操作", "gold": "调漂技巧"},
    {"q": "调几钓几是什么意思", "gold": "调漂技巧"},
    {"q": "冬春季节主线子线该选多粗", "gold": "线组搭配"},
    {"q": "子线一般留多长", "gold": "线组搭配"},
    {"q": "开饵的饵水比是多少", "gold": "饵料搭配"},
    {"q": "基础饵状态饵味型饵怎么配比", "gold": "饵料搭配"},
    {"q": "什么样的位置容易上鱼", "gold": "选钓位"},
    {"q": "回水湾适不适合下杆", "gold": "选钓位"},
    {"q": "东西拆开了还能退吗", "gold": "退换货政策"},
    {"q": "买回来不满意能退钱吗", "gold": "退换货政策"},
    {"q": "收到货发现不合适怎么办", "gold": "退换货政策"},
    {"q": "下单以后多久能发货", "gold": "发货物流"},
    {"q": "一般几天能收到货", "gold": "发货物流"},
    {"q": "买多少可以包邮", "gold": "运费政策"},
    {"q": "能不能对公转账", "gold": "付款方式"},
    {"q": "老客户可以月结吗", "gold": "付款方式"},
    {"q": "用手机付钱可以吗", "gold": "付款方式"},
    {"q": "拿货多少箱能享受区域代理价", "gold": "批发政策"},
    {"q": "打算订一批货，量大有没有优惠", "gold": "批发政策"},
    {"q": "我想做你们当地的经销商需要什么条件", "gold": "加盟招商"},
    {"q": "想让你们直接发给我的客户", "gold": "加盟招商"},
    {"q": "客服几点上班", "gold": "营业时间"},
    {"q": "在吗", "gold": "打招呼"},
]


def build_docs():
    """与 knowledge_data._load_docs() 的静态回退分支保持完全一致。"""
    docs = []
    for item in KNOWLEDGE_BASE:
        title = item.get("title") or (item.get("keywords") or [""])[0]
        text = title + " " + " ".join(item.get("keywords", [])) + "\n" + item.get("answer", "")
        docs.append({"text": text, "meta": {"title": title, "answer": item.get("answer", "")}})
    return docs


def make_engine(arm: str) -> RAGEngine:
    """按实验组构造引擎。"""
    if arm in ("semantic", "hybrid"):
        from agent.embeddings.openai_embedder import OpenaiEmbedder
        from agent.vectorstores.chroma_store import ChromaStore
        # 用临时目录，避免污染项目 chroma_data/
        tmp = tempfile.mkdtemp(prefix="zt_rag_eval_")
        semantic = RAGEngine(
            embedder=OpenaiEmbedder(),
            store_factory=lambda: ChromaStore(collection_name="zt_eval", persist_dir=tmp),
        )
        if arm == "hybrid":
            engine = _rag.HybridRAGEngine(semantic_engine=semantic)
        else:
            engine = semantic
    else:
        engine = RAGEngine()
    engine.ingest(build_docs())
    return engine


def doc_ranking(engine: RAGEngine, query: str, kmax: int = DOC_K):
    """检索并按条目（title）去重，返回前 kmax 个条目标题。"""
    hits = engine.retrieve(query, top_k=K_ALL, recall_k=K_ALL)
    titles = []
    for chunk, _score in hits:
        t = chunk.meta.get("title")
        if t and t not in titles:
            titles.append(t)
    return titles[:kmax]


def evaluate(engine: RAGEngine, label: str, verbose: bool = False):
    n = len(QUERIES)
    hits = {1: 0, 3: 0, 5: 0}
    rr_sum = 0.0
    misses = []
    for item in QUERIES:
        acceptable = {item["gold"]} | set(item.get("also") or [])
        ranking = doc_ranking(engine, item["q"])
        rank_hit = None
        for rank, title in enumerate(ranking, 1):
            if title in acceptable:
                rank_hit = rank
                break
        rr = 1.0 / rank_hit if rank_hit else 0.0
        for k in hits:
            if rank_hit is not None and rank_hit <= k:
                hits[k] += 1
        rr_sum += rr
        if rr == 0:
            misses.append({"q": item["q"], "gold": item["gold"], "top3": ranking[:3]})
    result = {
        "arm": label,
        "n": n,
        "hit@1": round(hits[1] / n, 4),
        "hit@3": round(hits[3] / n, 4),
        "hit@5": round(hits[5] / n, 4),
        "mrr": round(rr_sum / n, 4),
        "misses": misses,
    }
    if verbose and misses:
        print("    未命中 %d 条：" % len(misses))
        for m in misses[:12]:
            print("      · %-22s 期望【%s】 实际 top3=%s" % (m["q"], m["gold"], m["top3"]))
    return result


def sweep(semantic: bool):
    """BM25 重排权重敏感性扫描（仅在 TF-IDF 稀疏向量下有意义）。"""
    rows = []
    for w in (0.0, 0.25, 0.5, 1.0, 2.0):
        _rag.RAG_BM25_WEIGHT = w
        engine = make_engine("tfidf")
        r = evaluate(engine, "BM25 权重 w=%.2f" % w)
        rows.append({"w": w, "hit@1": r["hit@1"], "hit@3": r["hit@3"],
                     "hit@5": r["hit@5"], "mrr": r["mrr"]})
    _rag.RAG_BM25_WEIGHT = 0.5   # 还原
    return rows


def _pick_font(page):
    """优先用系统 CJK 字体（中英文混排比例正确），否则回退 PyMuPDF 内置字体。"""
    for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyh.ttf",
              "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if os.path.exists(p):
            try:
                page.insert_font(fontname="mix", fontfile=p)
                return "mix"
            except Exception:
                continue
    return "china-s"


def build_chart(results, path):
    """用 PyMuPDF 画分组柱状图（无 matplotlib 依赖）。"""
    import pymupdf
    W, H = 2000, 1120
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.draw_rect(pymupdf.Rect(0, 0, W, H), color=None, fill=(1, 1, 1))
    font = _pick_font(page)

    metrics = ["hit@1", "hit@3", "hit@5", "mrr"]
    mnames = ["Hit@1", "Hit@3", "Hit@5", "MRR"]
    colors = [(0.71, 0.83, 0.93), (0.53, 0.66, 0.87), (0.24, 0.44, 0.68), (0.71, 0.90, 0.82)]

    x0, y0, pw, ph = 250, 170, 1500, 800
    page.insert_text(
        (x0, 110),
        "RAG 检索效果对比（知识库 %d 条 / %d 条查询）" % (len(KNOWLEDGE_BASE), len(QUERIES)),
        fontsize=40, fontname=font)

    _axes(page, x0, y0, pw, ph, font)
    gw = pw / len(results)
    for i, r in enumerate(results):
        gx = x0 + i * gw
        bw = gw / (len(metrics) + 1.4)
        for j, m in enumerate(metrics):
            bx = gx + bw * 0.7 + j * bw
            bh = ph * r[m]
            page.draw_rect(pymupdf.Rect(bx, y0 + ph - bh, bx + bw * 0.82, y0 + ph),
                           color=None, fill=colors[j])
            page.insert_text((bx + bw * 0.06, y0 + ph - bh - 16), "%.3f" % r[m],
                             fontsize=26, fontname=font)
        page.insert_text((gx + gw * 0.14, y0 + ph + 60), r["arm"],
                         fontsize=32, fontname=font)

    lx = x0 + 40
    for j, nm in enumerate(mnames):
        page.draw_rect(pymupdf.Rect(lx, 30, lx + 28, 58), color=None, fill=colors[j])
        page.insert_text((lx + 40, 54), nm, fontsize=28, fontname=font)
        lx += 230

    if path.lower().endswith((".png", ".jpg", ".jpeg")):
        page.get_pixmap(dpi=150).save(path)      # 位图（论文直接可用）
    else:
        doc.save(path)
    print("图已保存:", path)


def _axes(page, x0, y0, pw, ph, font):
    import pymupdf
    for i in range(6):
        y = y0 + ph - ph * i / 5
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(x0 + pw, y),
                       color=(0.85, 0.85, 0.85), width=1)
        page.insert_text((x0 - 86, y + 9), "%.1f" % (i / 5), fontsize=24, fontname=font)
    page.draw_line(pymupdf.Point(x0, y0), pymupdf.Point(x0, y0 + ph), color=(0.3, 0.3, 0.3), width=1.5)
    page.draw_line(pymupdf.Point(x0, y0 + ph), pymupdf.Point(x0 + pw, y0 + ph), color=(0.3, 0.3, 0.3), width=1.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-semantic", action="store_true", help="追加语义向量实验组")
    ap.add_argument("--sweep", action="store_true", help="附 BM25 权重敏感性扫描")
    ap.add_argument("--out", default=os.path.join("docs", "rag_eval_results.json"))
    ap.add_argument("--build-chart", default=None, help="结果柱状图 PNG 输出路径")
    args = ap.parse_args()

    # 独立出图：不重跑实验、不覆盖已有结果，直接从 JSON 读数据画图
    if args.build_chart and not (args.with_semantic or args.sweep):
        if not os.path.exists(args.out):
            print("找不到结果文件 %s，请先运行完整实验。" % args.out)
            return
        with open(args.out, encoding="utf-8") as f:
            saved = json.load(f)["results"]
        os.makedirs(os.path.dirname(args.build_chart) or ".", exist_ok=True)
        try:
            build_chart(saved, args.build_chart)
        except ImportError:
            print("绘图失败：当前解释器缺少 pymupdf，请用系统 Python 3.12 运行。")
        return

    results = []

    print("=" * 74)
    print("RAG 检索效果评估｜知识库 %d 条｜查询 %d 条｜指标为文档级" % (len(KNOWLEDGE_BASE), len(QUERIES)))
    print("=" * 74)

    for arm, label in (("tfidf", "TF-IDF 纯余弦"), ("tfidf+bm25", "TF-IDF + BM25 重排")):
        print("\n[实验组] %s" % label)
        _rag.RAG_BM25_WEIGHT = 0.0 if arm == "tfidf" else 0.5
        results.append(evaluate(make_engine("tfidf"), label, verbose=True))
    _rag.RAG_BM25_WEIGHT = 0.5
    _print_table(results)

    if args.with_semantic:
        print("\n[实验组] BAAI/bge-m3 语义向量（调用 Embedding API，请稍候）")
        try:
            r = evaluate(make_engine("semantic"), "bge-m3 语义向量", verbose=True)
            results.append(r)
            _print_table(results)
        except Exception as ex:
            print("  语义组失败：%s: %s" % (type(ex).__name__, ex))
            print("  （不影响其他组结果；请检查 EMBEDDING_API_KEY 与网络）")

        print("\n[实验组] hybrid 双路融合（TF-IDF+BM25 × BGE-M3 并行召回 + RRF 融合）")
        try:
            r = evaluate(make_engine("hybrid"), "hybrid 双路融合", verbose=True)
            results.append(r)
            _print_table(results)
        except Exception as ex:
            print("  hybrid 组失败：%s: %s" % (type(ex).__name__, ex))
            print("  （不影响其他组结果；请检查 EMBEDDING_API_KEY 与网络）")

    payload = {"knowledge_entries": len(KNOWLEDGE_BASE), "queries": len(QUERIES),
               "doc_level_top_k": DOC_K, "results": results}

    if args.sweep:
        print("\n[附加] BM25 重排权重敏感性扫描")
        rows = sweep(args.with_semantic)
        payload["bm25_weight_sweep"] = rows
        print("  %-16s %-9s %-9s %-9s %s" % ("权重 w", "Hit@1", "Hit@3", "Hit@5", "MRR"))
        for r in rows:
            print("  %-16.2f %-9.3f %-9.3f %-9.3f %.3f"
                  % (r["w"], r["hit@1"], r["hit@3"], r["hit@5"], r["mrr"]))

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n结果已保存:", args.out)

    if args.build_chart:
        os.makedirs(os.path.dirname(args.build_chart) or ".", exist_ok=True)
        try:
            build_chart(results, args.build_chart)
        except ImportError:
            print("绘图失败：当前解释器缺少 pymupdf，请用系统 Python 3.12 运行。")


def _print_table(results):
    print("\n  %-22s %-9s %-9s %-9s %s" % ("实验组", "Hit@1", "Hit@3", "Hit@5", "MRR"))
    print("  " + "-" * 62)
    for r in results:
        print("  %-22s %-9.3f %-9.3f %-9.3f %.3f"
              % (r["arm"], r["hit@1"], r["hit@3"], r["hit@5"], r["mrr"]))


if __name__ == "__main__":
    main()
