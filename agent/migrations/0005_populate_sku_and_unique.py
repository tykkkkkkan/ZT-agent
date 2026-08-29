# 0005_populate_sku_and_unique.py
#
# Data migration：为存量 Products 填充 SKU-0001 / SKU-0002 ... 编号，
# 然后把 sku 改为 NOT NULL 并加 UNIQUE 约束。
#
# 必须在 0004 之后执行（依赖 0004 已加好 sku 列）。
#
# 业务规则：按 id 升序生成递增 SKU。如未来人工维护 SKU，把这一行
# RunPython 替换为"读取既有手工值"。
from django.db import migrations


def populate_sku(apps, schema_editor):
    """为所有 sku 为空的存量产品生成 SKU-XXXX。

    注意：0004 用 RunSQL 加列（非 AddField），migration state 中的 Products
    model 没有 sku 字段，因此这里用原生 SQL 更新，不依赖 ORM 字段状态。
    已手工填过 sku 的行（sku 非空）会被保留跳过。
    """
    conn = schema_editor.connection
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM `products` ORDER BY `id`")
        ids = [row[0] for row in cur.fetchall()]
        for idx, pid in enumerate(ids, start=1):
            cur.execute(
                "UPDATE `products` SET `sku` = %s WHERE `id` = %s AND (`sku` IS NULL OR `sku` = '')",
                (f"SKU-{idx:04d}", pid),
            )


def reverse_populate(apps, schema_editor):
    """反向：清空 sku。"""
    conn = schema_editor.connection
    with conn.cursor() as cur:
        cur.execute("UPDATE `products` SET `sku` = ''")


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0004_enhance_schema'),
    ]

    operations = [
        # 1) 填数据
        migrations.RunPython(populate_sku, reverse_populate),
        # 2) 改 NOT NULL + 加 UNIQUE
        migrations.RunSQL(
            sql="""
            ALTER TABLE `products`
                MODIFY COLUMN `sku` VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL
                COMMENT '商品编码（人类可读唯一标识）',
                ADD UNIQUE INDEX `uniq_products_sku`(`sku`);
            """,
            reverse_sql="""
            ALTER TABLE `products`
                DROP INDEX `uniq_products_sku`,
                MODIFY COLUMN `sku` VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT NULL
                COMMENT '商品编码（人类可读唯一标识）';
            """,
        ),
    ]
