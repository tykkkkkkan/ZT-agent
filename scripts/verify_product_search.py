#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""产品搜索筛选（任务书二·4）验收脚本 —— 后端侧

覆盖：
  ① 数据库层：`products.category` 列与索引真实存在（不是只在模型里声明）
  ② 模型往返：category 能写能读
  ③ 接口层：`/api/products/`（**公开**）每行都带 `category`，且与库内一致
  ④ 迁移回填：映射表内的商品有分类；未在映射里的**保持空**（不猜、不编）
  ⑤ 管理端：staff 可通过 PATCH 修改 category（建临时商品，用完删净）
  ⑥ 后台配置：分类进 列表列 / 筛选器 / 搜索框 / 表单
  ⑦ AI 工具层：`query_product` 支持「名称 / 分类 / 适用鱼种」三种查法，输出带「分类：」

前端筛选逻辑（纯函数分支）由 `scripts/verify_product_search.js` 单独断言。

运行：.venv/Scripts/python.exe scripts/verify_product_search.py
"""
import json
import os
import sys

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings                        # noqa: E402
from django.contrib.admin.sites import AdminSite        # noqa: E402
from django.contrib.auth import get_user_model          # noqa: E402
from django.db import connection                        # noqa: E402
from django.test import Client                          # noqa: E402

from agent import tools                                 # noqa: E402
from agent.admin import ProductsAdmin                   # noqa: E402
from agent.models import Products                       # noqa: E402
from agent.views import _serialize_product               # noqa: E402

# test Client 的 Host 是 testserver，不在默认 ALLOWED_HOSTS 里 → 否则一律 400（DisallowedHost）
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS.append("testserver")

PASS, FAIL = [], []
TMP_SKU = "SKU-VERIFY-SEARCH"


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jget(c, url, auth=None):
    r = c.get(url, **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def jpatch(c, url, payload, auth=None):
    r = c.patch(url, data=json.dumps(payload), content_type="application/json", **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


print("=" * 62)
print("  产品搜索筛选（任务书二·4）验收 · 后端")
print("=" * 62)

# ══════════════════════════════════════════════════════════════
print("\n【1】数据库层：列与索引真实存在")
with connection.cursor() as cur:
    cur.execute(
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products' AND COLUMN_NAME = 'category'"
    )
    col = cur.fetchone()
    cur.execute(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products' "
        "AND INDEX_NAME = 'idx_products_category'"
    )
    idx = cur.fetchall()

check("products.category 列已建", col is not None, str(col))
check("列类型为 varchar(50)", bool(col) and "varchar(50)" in col[1].lower(), col[1] if col else "")
check("列 NOT NULL 且默认空串（兼容历史行）",
      bool(col) and col[2] == "NO" and (col[3] == "" or col[3] is None), str(col[2:]) if col else "")
check("idx_products_category 索引已建（按分类筛选走索引）", bool(idx), str(idx))

# ══════════════════════════════════════════════════════════════
print("\n【2】模型往返：category 能写能读")
probe = Products.objects.filter(sku=TMP_SKU).first()
if probe:
    probe.delete()
probe = Products.objects.create(sku=TMP_SKU, name="__验收临时商品__", category="", is_active=False)
try:
    probe.category = "临时分类"
    probe.save(update_fields=["category"])
    probe.refresh_from_db()
    check("写入后读回一致", probe.category == "临时分类", repr(probe.category))
    check("空串是合法初值（未分类商品不被拒）", Products.objects.filter(category="").exists(),
          f"{Products.objects.filter(category='').count()} 款未分类")
finally:
    probe.delete()
check("临时商品已清理", not Products.objects.filter(sku=TMP_SKU).exists())

# ══════════════════════════════════════════════════════════════
print("\n【3】接口层：/api/products/ 公开返回 category 且与库一致")
c = Client()
status, data = jget(c, "/api/products/")
check("公开接口不带 token 也 200（产品页要用）", status == 200, f"HTTP {status}")
rows = data.get("products") or []
check("返回了产品行", len(rows) > 0, f"{len(rows)} 行")
check("每行都含 category 键（缺键会让前端筛不出来）",
      all("category" in r for r in rows),
      f"缺键行数={sum(1 for r in rows if 'category' not in r)}")
db_map = {p.id: (p.category or "") for p in Products.objects.all()}
mismatch = [r["id"] for r in rows if r.get("category") != db_map.get(r["id"])]
check("接口值与库内逐个一致", not mismatch, f"不一致 id={mismatch}")

# ══════════════════════════════════════════════════════════════
print("\n【4】迁移回填：只填已知商品，不猜不编")
filled = {p.name: p.category for p in Products.objects.all() if p.category}
check("至少回填了 5 款（迁移映射表）", len(filled) >= 5, str(filled))
check("回填值与该商品描述吻合（红虫颗粒=浓腥饵）", filled.get("红虫颗粒") == "浓腥饵",
      repr(filled.get("红虫颗粒")))
uncategorized = [p.name for p in Products.objects.all() if not p.category]
check("映射表外的商品保持空（钓鱼王应仍未分类）", "钓鱼王" in uncategorized,
      f"未分类={uncategorized}")

# ══════════════════════════════════════════════════════════════
print("\n【5】管理端：staff 可 PATCH 修改分类")
User = get_user_model()
staff = User.objects.filter(is_staff=True).order_by("id").first()
tmp = Products.objects.create(sku=TMP_SKU, name="__验收临时商品__", category="", is_active=False)
try:
    check("存在 staff 账号可测", staff is not None, getattr(staff, "username", ""))
    auth = bearer(staff)
    st, body = jpatch(c, f"/api/products/{tmp.id}/", {"category": "验收分类"}, auth)
    tmp.refresh_from_db()
    check("PATCH category 返回 200", st == 200, f"HTTP {st} {body.get('message', '')}")
    check("分类已落库", tmp.category == "验收分类", repr(tmp.category))
    st2, body2 = jpatch(c, f"/api/products/{tmp.id}/", {"category": "验收分类2"}, auth)
    tmp.refresh_from_db()
    check("再次修改生效（可反复维护）", tmp.category == "验收分类2", repr(tmp.category))
    # 管理端序列化不下发裁剪，且带 category
    ser = _serialize_product(tmp, mask_pricing=False)
    check("管理端序列化含 category", ser.get("category") == "验收分类2", repr(ser.get("category")))
    st3, _ = jpatch(c, f"/api/products/{tmp.id}/", {"category": "x"})
    check("未登录改分类被拒（401）", st3 == 401, f"HTTP {st3}")
finally:
    tmp.delete()

# ══════════════════════════════════════════════════════════════
print("\n【6】后台配置：分类进 列表列 / 筛选器 / 搜索框 / 表单")
admin_obj = ProductsAdmin(Products, AdminSite())
check("list_display 含 category", "category" in admin_obj.list_display)
check("list_filter 含 category（后台可按分类筛）", "category" in admin_obj.list_filter)
check("search_fields 含 category（后台可搜分类）", "category" in admin_obj.search_fields)
form = admin_obj.get_form(None)
check("后台表单含 category（分类唯一维护入口）", "category" in form.base_fields,
      str(sorted(form.base_fields.keys())))

# ══════════════════════════════════════════════════════════════
print("\n【7】AI 工具层：名称 / 分类 / 适用鱼种 三种查法")
out_by_name = tools.query_product("红虫颗粒")
check("按名称查得到", "红虫颗粒" in out_by_name, out_by_name.splitlines()[0] if out_by_name else "")
check("输出带「分类：」行（与产品页口径一致）", "分类：" in out_by_name,
      next((ln for ln in out_by_name.splitlines() if ln.startswith("分类")), "无"))

out_by_cat = tools.query_product("薯香饵")
check("按**分类**查得到（此前只有名称匹配，会查不到）",
      "螺鲤3号" in out_by_cat or "产品：" in out_by_cat,
      out_by_cat.splitlines()[0] if out_by_cat else "")

out_by_fish = tools.query_product("鲫鱼")
check("按**适用鱼种**查得到（多值字段 target_fish）",
      "产品：" in out_by_fish and "没找到" not in out_by_fish,
      f"命中={out_by_fish.splitlines()[0] if out_by_fish else ''}")

out_none = tools.query_product("不存在的商品名ZZZ")
check("查不到时给出可操作的提示（建议换名称/分类/鱼种）",
      "没找到" in out_none and "鱼种" in out_none, out_none)

out_paused_safe = tools.query_product("红虫颗粒")
check("仍带可购性结论（扩检索范围没弄丢原有信息）",
      ("可正常接单" in out_paused_safe) or ("暂不可下单" in out_paused_safe)
      or ("当前可下单" in out_paused_safe),
      out_paused_safe.splitlines()[-1][:50] if out_paused_safe else "")

# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print(f"    ✗ {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
