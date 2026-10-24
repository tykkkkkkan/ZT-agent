"""
products.image —— 把商品图从「前端按名字写死的映射」落进数据库

为什么手写迁移
--------------
`products` 是 inspectdb 出来的 `managed=False` 既存表，autodetector 会跳过
unmanaged 模型的字段变化，因此沿用 0014 / 0015 / 0018 的既有约定**手写 DDL**，
并用 information_schema 探测做成幂等（重复执行不报错）。

回填策略
--------
只回填 `image` 为空的行 —— 运营在后台换过图之后再跑一次（比如别人 clone 后
从头 migrate），不会被存量映射盖回去。`钓鱼王` 没有实拍图 → 留空，前端走占位。
"""
from django.db import migrations

TABLE = "products"
COLUMN = "image"

ADD_COLUMN_SQL = (
    "ALTER TABLE `products` "
    "ADD COLUMN `image` VARCHAR(255) NOT NULL DEFAULT '' "
    "COMMENT '产品图片路径（/assets/images/xxx.webp 或完整 URL，留空走占位图）'"
)
DROP_COLUMN_SQL = "ALTER TABLE `products` DROP COLUMN `image`"

# 与 frontend/assets/images/ 下的真实文件名一致（PNG→WebP 迁移后的名字）
BACKFILL = [
    ("红虫颗粒", "/assets/images/product-hongchong.webp"),
    ("九一八", "/assets/images/product-jiuyiba.webp"),
    ("螺鲤3号", "/assets/images/product-luoli.webp"),
    ("蓝鲫X5", "/assets/images/product-lanji.webp"),
    ("速攻2号", "/assets/images/product-sugong.webp"),
]


def _has_column(cursor):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        [TABLE, COLUMN],
    )
    return cursor.fetchone()[0] > 0


def forwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    if not _has_column(cursor):
        cursor.execute(ADD_COLUMN_SQL)
    # 幂等回填：只补空值，不动运营已经维护过的图
    filled = []
    for name, path in BACKFILL:
        cursor.execute(
            "UPDATE `products` SET `image` = %s "
            " WHERE `name` = %s AND (`image` IS NULL OR `image` = '')",
            [path, name],
        )
        if cursor.rowcount:
            filled.append(name)
    print(f"  [0026] 回填商品图：{filled or '无（无需回填）'}")


def backwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    if _has_column(cursor):
        cursor.execute(DROP_COLUMN_SQL)


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0025_supportticket"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
