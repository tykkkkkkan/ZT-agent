# 0006_orders_snapshot_fields.py
#
# 给 orders 加两个冗余快照字段：
#   - product_sku：商品编码快照（保护历史订单不受产品改名/SKU 变更影响）
#   - unit_price：下单时单价（防止价格调整后历史订单金额失真）
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0005_populate_sku_and_unique'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            ALTER TABLE `orders`
                ADD COLUMN `product_sku` VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT ''
                COMMENT '产品 SKU 快照' AFTER `product_name`,
                ADD COLUMN `unit_price` DECIMAL(10, 2) NULL DEFAULT NULL
                COMMENT '下单时单价' AFTER `product_sku`,
                ADD INDEX `idx_orders_product_sku`(`product_sku`);
            """,
            reverse_sql="""
            ALTER TABLE `orders`
                DROP INDEX `idx_orders_product_sku`,
                DROP COLUMN `unit_price`,
                DROP COLUMN `product_sku`;
            """,
        ),
    ]
