"""
契约验证：商品图入库（任务书四·2「产品名片含图片」的落点）—— ZT-agent

改造前的状态
------------
`products` 表没有图片字段，商品图靠**前端按商品名写死的映射**：

    const PRODUCT_IMAGES = { '红虫颗粒': 'assets/images/product-hongchong.png', … }

于是有三个问题：
  1. 新商品永远只有通用占位图 —— 想配图必须改代码、发版；
  2. 映射写的是 `.png`，而资源早就迁移成 `.webp` → 五张图全部 404（静默回退占位）；
  3. 后台"产品管理"里没有图这一项，运营没有任何维护入口。

本脚本守的口径
--------------
  A. 迁移真的把列加上了（managed=False 的表靠手写 DDL，不是 CreateModel）
  B. 模型字段与库里列一致（长度、非空、默认值）
  C. **每一条非空的 image 都指向真实存在的静态文件** —— 填了路径但文件不存在，
     前台就是一张白图，而且不报错。这是本脚本最重要的一条。
  D. `/api/products/` 与 Agent 产品名片都下发 image_url，且与库里的值一致
  E. 后台有缩略图列与预览字段（运营能看见、能改）

用法：python scripts/verify_product_images.py
"""
import os
import sys

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.admin.sites import AdminSite  # noqa: E402
from django.test import Client  # noqa: E402

from agent import tools  # noqa: E402
from agent.admin import ProductsAdmin  # noqa: E402
from agent.models import Products  # noqa: E402

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def static_file_of(url: str) -> str:
    """把 /assets/images/x.webp 映射到磁盘路径（前端静态资源由 serve_frontend_asset 托管）。"""
    path = url.strip()
    if path.startswith("http://") or path.startswith("https://"):
        return ""                       # 外链不做本地存在性检查
    if path.startswith("/assets/"):
        return os.path.join(BASE, "frontend", "assets", path[len("/assets/"):])
    if path.startswith("assets/"):
        return os.path.join(BASE, "frontend", path)
    return os.path.join(BASE, "frontend", path.lstrip("/"))


print("=" * 62)
print("  商品图入库 验收")
print("=" * 62)

# ── A. 迁移把列加上了 ──────────────────────────────────────────
print("\n[A] 数据库列（managed=False 手写 DDL）")

from django.db import connection  # noqa: E402

with connection.cursor() as cur:
    cur.execute(
        "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT FROM information_schema.COLUMNS "
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products' AND COLUMN_NAME = 'image'"
    )
    col = cur.fetchone()

check("products.image 列存在", col is not None)
if col:
    check("类型是 varchar(255)", col[0].lower().startswith("varchar(255)"), str(col[0]))
    check("NOT NULL（避免 ORM 拿到 None）", col[1] == "NO", str(col[1]))

# ── B. 模型字段口径 ───────────────────────────────────────────
print("\n[B] 模型字段")

f = Products._meta.get_field("image")
check("字段在模型上（不是只在库里）", f is not None)
check("max_length=255", f.max_length == 255, str(f.max_length))
check("blank=True（后台可不填）", f.blank is True)
check("default=''（不是 NULL）", f.default == "", repr(f.default))
check("verbose_name 是中文", f.verbose_name == "产品图片", str(f.verbose_name))

# ── C. 每条非空 image 都指向真实文件 ──────────────────────────
print("\n[C] 图片路径指向真实文件（白图的根因）")

rows = list(Products.objects.all().order_by("id").values("id", "name", "image"))
check("能读出 image 列（查询不报 Unknown column）", isinstance(rows, list), f"{len(rows)} 行")

filled = [r for r in rows if (r["image"] or "").strip()]
check("存量商品图已回填（至少 5 条）", len(filled) >= 5, f"{len(filled)}/{len(rows)}")

missing = []
bad_prefix = []
for r in filled:
    url = r["image"].strip()
    if not (url.startswith("/assets/") or url.startswith("http")):
        bad_prefix.append(f"{r['name']}={url}")
        continue
    disk = static_file_of(url)
    if disk and not os.path.isfile(disk):
        missing.append(f"{r['name']}={url}")

check("路径一律根绝对路径（/assets/... 或外链）", not bad_prefix, "; ".join(bad_prefix))
check("每个路径都能在磁盘上找到文件", not missing, "; ".join(missing))

# ── D. 接口与名片下发 ─────────────────────────────────────────
print("\n[D] 接口与产品名片都会下发 image_url")

client = Client()
resp = client.get("/api/products/")
check("GET /api/products/ 返回 200", resp.status_code == 200, str(resp.status_code))
payload = resp.json()
api_rows = {p["id"]: p for p in payload.get("products", [])}
check("接口返回了商品列表", bool(api_rows), f"{len(api_rows)} 条")
check("每一行都带 image_url 键", all("image_url" in p for p in api_rows.values()))

mismatch = []
for pid, p in api_rows.items():
    db_val = next((r["image"] for r in rows if r["id"] == pid), None)
    if (p.get("image_url") or "") != (db_val or ""):
        mismatch.append(f"{pid}: api={p.get('image_url')!r} db={db_val!r}")
check("image_url 与库里的值一致（不做二次加工）", not mismatch, "; ".join(mismatch))

# 工具层：名片带上 image_url
target = filled[0] if filled else None
if target:
    tools.reset_referenced_products()
    tools.query_product(target["name"])
    cards = tools.get_referenced_products()
    check("工具查询产出了名片", len(cards) == 1, str(len(cards)))
    if cards:
        check("名片带 image_url 且与库里一致",
              cards[0].get("image_url") == target["image"].strip(),
              f"{cards[0].get('image_url')} vs {target['image']}")
    tools.reset_referenced_products()

# ── E. 后台可维护 ─────────────────────────────────────────────
print("\n[E] 后台可看见、可维护")

site = AdminSite()
admin_obj = ProductsAdmin(Products, site)
check("列表有缩略图列 image_thumb", "image_thumb" in admin_obj.list_display,
      str(admin_obj.list_display))
check("详情有只读预览 image_preview", "image_preview" in admin_obj.readonly_fields,
      str(admin_obj.readonly_fields))
check("image 不在只读里（否则运营改不了图）", "image" not in admin_obj.readonly_fields)

form_fields = list(admin_obj.get_form(None).base_fields.keys()) if True else []
check("编辑表单包含 image 字段", "image" in form_fields, str(form_fields))

if filled:
    html = admin_obj.image_thumb(Products.objects.get(id=target["id"]))
    check("缩略图渲染出 img 标签", "<img" in html, html[:60])
blank = next((r for r in rows if not (r["image"] or "").strip()), None)
if blank:
    html = admin_obj.image_thumb(Products.objects.get(id=blank["id"]))
    check("无图商品不渲染 img（显示占位符）", "<img" not in html, html[:60])

print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for x in FAIL:
        print("    · " + x)
print("=" * 62)
sys.exit(1 if FAIL else 0)
