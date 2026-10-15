# 0018_products_category.py
#
# 任务书二·4：「按名称、分类或适用鱼种查找」——产品检索需要「分类」这一维。
#
# 做两件事：
#   1. products 加 `category` 列（+ 索引，产品页按分类筛选要能走索引）
#   2. 为存量商品回填初始分类（**只填空值**，不覆盖后台已改过的分类）
#
# 为什么不是前端写死映射
# ----------------------
# 「分类」如果由前端按商品名映射，那它就是页面里的假数据：答辩被问
# "你这个分类字段在哪、谁维护" 会站不住。落成真实字段后，后台可增删改，
# 产品页按真实数据筛选，与「后台能维护」的项目基调一致。
#
# 说明：products 是 managed=False 的既存表，autodetector 会跳过 unmanaged 模型，
# 因此沿用 0014 / 0015 的既有约定**手写 DDL**，并用 information_schema 探测做成幂等。
from django.db import migrations

# (列名, DDL 片段)
COLUMNS = [
    ("category",
     "ADD COLUMN `category` VARCHAR(50) NOT NULL DEFAULT '' "
     "COMMENT '产品分类（如 浓腥饵/广谱饵/薯香饵，后台可维护）'"),
]

# (索引名, DDL 片段)
INDEXES = [
    ("idx_products_category", "ADD INDEX `idx_products_category` (`category`)"),
]

# 存量商品的初始分类：按**商品名**匹配，取值为「味型 / 品类」这一真实维度
# （依据各商品自己的 description 归纳，例如「高蛋白浓腥味」→ 浓腥饵）。
# 匹配不到的商品保持空串，后台可自行归类 —— 不猜、不编。
INITIAL_CATEGORY = {
    "红虫颗粒": "浓腥饵",
    "九一八": "广谱饵",
    "螺鲤3号": "薯香饵",
    "蓝鲫X5": "腥香饵",
    "速攻2号": "奶香饵",
}


def _has_column(cursor, table, name):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        [table, name],
    )
    return cursor.fetchone()[0] > 0


def _has_index(cursor, table, name):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
        [table, name],
    )
    return cursor.fetchone()[0] > 0


def forwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()

    added = []
    for col, ddl in COLUMNS:
        if _has_column(cursor, "products", col):
            continue
        cursor.execute(f"ALTER TABLE `products` {ddl}")
        added.append(col)
    for name, ddl in INDEXES:
        if _has_index(cursor, "products", name):
            continue
        cursor.execute(f"ALTER TABLE `products` {ddl}")
        added.append(name)
    print(f"  [0018] products 新增：{added or '无（已是最新）'}")

    # 回填初始分类：⚠️ 只改空值，不覆盖已有分类（迁移可重复执行且不冲掉人工维护结果）
    #
    # 这里**故意用原生 SQL 而不是 apps.get_model()**：
    # products 是 managed=False 的既存表，字段变更没有对应的 state operation，
    # 因此迁移状态里的 Products 模型**不含 category**，用 ORM 会直接
    # `FieldError: Cannot resolve keyword 'category'`（本轮踩过）。
    filled = []
    for name, cat in INITIAL_CATEGORY.items():
        cursor.execute(
            "UPDATE `products` SET `category` = %s "
            "WHERE `name` = %s AND (`category` = '' OR `category` IS NULL)",
            [cat, name],
        )
        if cursor.rowcount:
            filled.append(f"{name}→{cat}")
    print(f"  [0018] 回填分类：{filled or '无（无需回填）'}")


def backwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for name, _ in reversed(INDEXES):
        if _has_index(cursor, "products", name):
            cursor.execute(f"ALTER TABLE `products` DROP INDEX `{name}`")
    for col, _ in reversed(COLUMNS):
        if _has_column(cursor, "products", col):
            cursor.execute(f"ALTER TABLE `products` DROP COLUMN `{col}`")


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0017_user_profile_customer_type"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
