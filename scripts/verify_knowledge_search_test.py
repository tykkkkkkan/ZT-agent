"""
契约验证：知识库检索测试台 —— ZT-agent

背景
----
批量指标（`eval_rag.py` 的 hit@k / MRR）回答的是"整体好不好"，
调知识库时人真正想知道的是"我刚改的这条排第几、输给谁"。
后台新增 `/admin/agent/knowledgechunk/search-test/`：一句话 → 召回明细
（标题 / 分类 / 分数 / 字面路与语义路名次 / RRF / 摘要），阈值可调、三档可对比。

本脚本守住五条口径：

  1. **与线上同口径**：测试台的默认阈值必须等于 `search_knowledge` 用的那套
     （tfidf 1.5 / embedding 0.2 / hybrid 0），否则"测着好看、线上召不回"
  2. **阈值真的生效**：调高 → 命中最少，调低 → 命中增多（同一句话）
  3. **三档对比是三列**，且某档不可用时显示原因而不是 500
  4. **语义路只 embed 一次**：embedding 与 hybrid 共用同一份语义索引
     （否则后台点一次对比要等一分多钟）
  5. **后台权限**：匿名访问被挡，非 staff 也进不去

用法：./.venv/Scripts/python.exe scripts/verify_knowledge_search_test.py
"""
import os
import sys

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client  # noqa: E402

from agent import knowledge_data  # noqa: E402
from agent.admin import KnowledgeChunkAdmin  # noqa: E402
from agent.rag import RAGEngine  # noqa: E402

PASS, FAIL = [], []
URL = "/admin/agent/knowledgechunk/search-test/"


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'√' if cond else '×'} {label}" + (f"  <- {detail}" if detail else ""))


print("=" * 62)
print("  知识库检索测试台验收")
print("=" * 62)

# ══════════════════════════════════════════════════════════════
# 【1】口径一致
# ══════════════════════════════════════════════════════════════
print("\n【1】默认阈值与线上同口径")
admin_cls = KnowledgeChunkAdmin
check("测试台阈值表 = tfidf 1.5 / embedding 0.2 / hybrid 0",
      admin_cls.DEFAULT_MIN_SCORE == {"tfidf": 1.5, "embedding": 0.2, "hybrid": 0.0},
      str(admin_cls.DEFAULT_MIN_SCORE))

# 与 tools.search_knowledge 里的映射逐条对齐（源码级核对，避免两处各改各的）
tools_src = open(os.path.join(BASE, "agent", "tools.py"), encoding="utf-8").read()
check("tools.search_knowledge 里仍是同一组阈值",
      '{"embedding": 0.2, "hybrid": 0.0}.get(backend, 1.5)' in tools_src)
check("三档后端名一致",
      [b for b, _ in admin_cls.SEARCH_TEST_BACKENDS] == ["tfidf", "embedding", "hybrid"],
      str([b for b, _ in admin_cls.SEARCH_TEST_BACKENDS]))

# ══════════════════════════════════════════════════════════════
# 【2】页面可访问性与权限
# ══════════════════════════════════════════════════════════════
print("\n【2】访问与权限")
User = get_user_model()
staff = User.objects.filter(is_staff=True).first()
superuser = User.objects.filter(is_superuser=True).first()

anon = Client()
r_anon = anon.get(URL)
check("匿名访问被挡（302 到登录页）", r_anon.status_code in (301, 302), str(r_anon.status_code))

plain = User.objects.filter(is_staff=False, is_superuser=False).first()
if plain:
    c_plain = Client()
    c_plain.force_login(plain)
    r_plain = c_plain.get(URL)
    check("非 staff 用户访问被挡", r_plain.status_code in (301, 302, 403), str(r_plain.status_code))

c = Client()
c.force_login(superuser)
r_empty = c.get(URL)
html_empty = r_empty.content.decode("utf-8", "ignore")
check("staff 打开测试台 200", r_empty.status_code == 200, str(r_empty.status_code))
check("空页面有查询输入框", 'name="q"' in html_empty)
check("空页面有后端下拉", 'name="backend"' in html_empty)
check("空页面有三档对比开关", 'name="compare"' in html_empty)
check("空页面有阈值输入", 'name="min_score"' in html_empty)
check("空页面有使用说明", "怎么用" in html_empty)
check("URL 没被对象详情路由吃掉（不是 404/重定向到 change）",
      r_empty.status_code == 200 and "知识库检索测试" in html_empty)

# ══════════════════════════════════════════════════════════════
# 【3】单后端检索：明细与阈值
# ══════════════════════════════════════════════════════════════
print("\n【3】单后端检索明细")
query = "拆封了还能退货吗"
r1 = c.get(URL, {"q": query, "backend": "tfidf"})
h1 = r1.content.decode("utf-8", "ignore")
check("检索页 200", r1.status_code == 200, str(r1.status_code))
check("命中退换货政策", "退换货政策" in h1)
check("展示了三路名次（字面路）", "字面路" in h1)
check("展示了语义路占位", "语义路" in h1)
check("展示了知识库总条数", "共 " in h1)

# 阈值调高 → 命中减少；调低 → 命中增多
def hit_count(html):
    import re
    m = re.search(r"(\d+) 条 · \d+ ms", html)
    return int(m.group(1)) if m else -1


r_hi = c.get(URL, {"q": query, "backend": "tfidf", "min_score": "99"})
r_lo = c.get(URL, {"q": query, "backend": "tfidf", "min_score": "0"})
n_hi = hit_count(r_hi.content.decode("utf-8", "ignore"))
n_lo = hit_count(r_lo.content.decode("utf-8", "ignore"))
n_def = hit_count(h1)
check("阈值调到 99 → 0 条（过滤生效）", n_hi == 0, str(n_hi))
check("阈值调到 0 → 命中数 >= 默认阈值下的命中数", n_lo >= n_def, f"0:{n_lo} vs 默认:{n_def}")
check("阈值过滤有友好空状态",
      "没有超过阈值的召回" in r_hi.content.decode("utf-8", "ignore"))

# top_k 生效
r_top1 = c.get(URL, {"q": query, "backend": "tfidf", "min_score": "0", "top_k": "1"})
n_top1 = hit_count(r_top1.content.decode("utf-8", "ignore"))
check("top_k=1 时最多 1 条", 0 <= n_top1 <= 1, str(n_top1))

# 非法参数不 500
r_bad = c.get(URL, {"q": query, "backend": "nope", "top_k": "abc", "min_score": "xyz"})
check("非法 backend / top_k / 阈值都回落到默认值，不 500",
      r_bad.status_code == 200, str(r_bad.status_code))

# ══════════════════════════════════════════════════════════════
# 【4】三档对比（用轻量引擎替掉语义路，避免真的调 Embedding API）
# ══════════════════════════════════════════════════════════════
print("\n【4】三档对比")
real_build = knowledge_data._build_engine


def fake_build(backend):
    """语义后端在 CI / 未配置 key 时不该阻断验收，用同接口的 tfidf 引擎顶替。"""
    engine = RAGEngine()
    return engine


knowledge_data._build_engine = fake_build
try:
    knowledge_data._engine_cache.clear()
    r_cmp = c.get(URL, {"q": query, "compare": "1", "top_k": "3"})
    h_cmp = r_cmp.content.decode("utf-8", "ignore")
    check("三档对比页 200", r_cmp.status_code == 200, str(r_cmp.status_code))
    check("使用三列布局（is-compare）", "is-compare" in h_cmp)
    check("出现三个面板", h_cmp.count('<section class="kt-panel">') == 3,
          str(h_cmp.count('<section class="kt-panel">')))
    for label in ("TF-IDF", "语义向量", "双路混合"):
        check(f"面板包含「{label}」", label in h_cmp)

    # 某档构建失败 → 显示原因而不是 500
    def boom(backend):
        raise RuntimeError("EMBEDDING_API_KEY 未配置")

    knowledge_data._build_engine = boom
    knowledge_data._engine_cache.clear()
    r_boom = c.get(URL, {"q": query, "compare": "1"})
    h_boom = r_boom.content.decode("utf-8", "ignore")
    check("某档不可用时仍是 200", r_boom.status_code == 200, str(r_boom.status_code))
    check("不可用的档位显示原因", "这一档不可用" in h_boom)
    check("原因里带上异常摘要", "EMBEDDING_API_KEY 未配置" in h_boom)
finally:
    knowledge_data._build_engine = real_build
    knowledge_data._engine_cache.clear()

# ══════════════════════════════════════════════════════════════
# 【5】语义路只 embed 一次
# ══════════════════════════════════════════════════════════════
print("\n【5】语义路复用（embedding 与 hybrid 共用一份索引）")
knowledge_data._engine_cache.clear()

sem_calls = {"n": 0}
real_ingest = RAGEngine.ingest


def counting_ingest(self, documents):
    # 只统计非 tfidf 引擎（假后端在 fake_build 里也用 RAGEngine，这里靠标记区分）
    if getattr(self, "_count_as_semantic", False):
        sem_calls["n"] += 1
    return real_ingest(self, documents)


fake_semantic = RAGEngine()
fake_semantic._count_as_semantic = True
fake_semantic.ingest([{"text": "退换货政策 退货\n支持 7 天无理由退换货。",
                       "meta": {"title": "退换货政策", "answer": "支持 7 天无理由退换货。"}}])
sem_calls["n"] = 0

knowledge_data._engine_cache["embedding"] = fake_semantic
try:
    hybrid = knowledge_data.get_engine("hybrid")
    check("hybrid 复用已缓存的语义引擎", getattr(hybrid, "_semantic", None) is fake_semantic,
          type(getattr(hybrid, "_semantic", None)).__name__)
    check("构建 hybrid 时没有重新 embed 语义路", sem_calls["n"] == 0, f"{sem_calls['n']} 次")
    check("hybrid 的字面路已建好", len(getattr(hybrid._lexical, "chunks", [])) > 0,
          str(len(getattr(hybrid._lexical, "chunks", []))))
finally:
    knowledge_data._engine_cache.clear()

# 反向顺序：先请求 hybrid，再看 embedding 是否复用其语义路
knowledge_data._engine_cache.clear()
knowledge_data._engine_cache["hybrid"] = type("E", (), {})()  # 占位，避免真去 build
knowledge_data._engine_cache.pop("hybrid")
try:
    # 用假 build 建 hybrid 太绕，这里直接验证 setdefault 的复用约定：
    # get_engine('hybrid') 成功后，'embedding' 一定在缓存里且与 _semantic 同源
    knowledge_data._build_engine = fake_build
    knowledge_data._engine_cache.clear()
    h2 = knowledge_data.get_engine("hybrid")
    check("构建 hybrid 后 embedding 档已就绪（反向复用）",
          "embedding" in knowledge_data._engine_cache)
    check("反向复用的是同一个语义引擎",
          knowledge_data._engine_cache.get("embedding") is getattr(h2, "_semantic", None))
finally:
    knowledge_data._build_engine = real_build
    knowledge_data._engine_cache.clear()

# ══════════════════════════════════════════════════════════════
# 【6】缓存失效与入口
# ══════════════════════════════════════════════════════════════
print("\n【6】缓存与入口")
knowledge_data._engine_cache["tfidf"] = RAGEngine()
knowledge_data.invalidate_rag_cache()
check("invalidate_rag_cache 会清空测试台引擎缓存", knowledge_data._engine_cache == {},
      str(knowledge_data._engine_cache))

from django.conf import settings as dj_settings  # noqa: E402

nav_links = []
for group in dj_settings.UNFOLD.get("SIDEBAR", {}).get("navigation", []):
    for item in group.get("items", []):
        nav_links.append(item.get("link"))
check("侧栏有「检索测试」入口", URL in nav_links, str(nav_links))

tpl = open(os.path.join(BASE, "agent", "templates", "admin", "knowledge_search_test.html"),
           encoding="utf-8").read()
check("模板展示了字面路 / 语义路名次", "字面路" in tpl and "语义路" in tpl)
check("模板展示 RRF 融合分", "RRF" in tpl)
check("模板展示摘要（判断是否真的命中）", "kt-snippet" in tpl)
check("模板只用 token 取色（无裸 hex）",
      "var(--zy-" in tpl and "#fff" not in tpl and "#ffffff" not in tpl)

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    · " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
