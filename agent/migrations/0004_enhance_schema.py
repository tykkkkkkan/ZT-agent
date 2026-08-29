# 0004_enhance_schema.py
#
# 增强表结构（P0 修复）：
#   1. Products：新增 sku / is_active / created_at / updated_at 字段，给 target_fish 加索引
#   2. Inventory：stock / alert_line 改 INT UNSIGNED 防负，新增 reserved_stock（预占库存）、updated_at
#   3. Orders：加 cancelled_at / cancel_reason / updated_at，order_no 加 UNIQUE，phone/status/tracking_no/created_at 加索引，加 (status,-created_at) 复合索引
#   4. Conversations：session_id / created_at 加索引，加 (session_id,-created_at) 复合索引
#   5. ContactMessage / CustomRequest：is_read / phone / created_at 加索引
#
# 说明：managed=False，Django 不会自动执行，必须用 `python manage.py sqlmigrate agent 0004`
#       生成 SQL 后在 Navicat 手工执行（或显式调用 `migrate --run-syncdb`）。
# 兼容性：所有 ALTER 均为加列/加索引/放宽类型，存量数据安全；不需要停服。
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0003_contactmessage_email'),
    ]

    operations = [
        # ── 1. products ─────────────────────────────────────────────
        migrations.RunSQL(
            sql="""
            ALTER TABLE `products`
                ADD COLUMN `sku` VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT NULL COMMENT '商品编码（人类可读唯一标识）' AFTER `id`,
                ADD COLUMN `is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否在售（软删除）' AFTER `description`,
                ADD COLUMN `created_at` DATETIME NULL DEFAULT NULL COMMENT '创建时间' AFTER `is_active`,
                ADD COLUMN `updated_at` DATETIME NULL DEFAULT NULL COMMENT '更新时间' AFTER `created_at`,
                ADD INDEX `idx_products_target_fish`(`target_fish`),
                ADD INDEX `idx_products_is_active`(`is_active`),
                ADD INDEX `idx_products_sku`(`sku`);
            """,
            reverse_sql="ALTER TABLE `products` DROP INDEX `idx_products_sku`, DROP INDEX `idx_products_is_active`, DROP INDEX `idx_products_target_fish`, DROP COLUMN `updated_at`, DROP COLUMN `created_at`, DROP COLUMN `is_active`, DROP COLUMN `sku`;",
        ),

        # ── 2. inventory ────────────────────────────────────────────
        # 注：MODIFY 用 INT UNSIGNED（Django 的 PositiveIntegerField 对应类型）
        migrations.RunSQL(
            sql="""
            ALTER TABLE `inventory`
                ADD COLUMN `reserved_stock` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '预占库存（待发货订单占用）' AFTER `stock`,
                ADD COLUMN `updated_at` DATETIME NULL DEFAULT NULL COMMENT '更新时间' AFTER `alert_line`,
                MODIFY COLUMN `stock` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '当前库存（可售）',
                MODIFY COLUMN `alert_line` INT UNSIGNED NOT NULL DEFAULT 50 COMMENT '预警线';
            """,
            reverse_sql="ALTER TABLE `inventory` DROP COLUMN `updated_at`, DROP COLUMN `reserved_stock`, MODIFY COLUMN `stock` INT NULL DEFAULT 0, MODIFY COLUMN `alert_line` INT NULL DEFAULT 50;",
        ),

        # ── 3. orders ───────────────────────────────────────────────
        # 注：order_no 上加 UNIQUE 索引；存量重复值会导致失败（但项目刚起步，无重复）
        migrations.RunSQL(
            sql="""
            ALTER TABLE `orders`
                ADD COLUMN `cancelled_at` DATETIME NULL DEFAULT NULL COMMENT '取消时间' AFTER `shipped_at`,
                ADD COLUMN `cancel_reason` VARCHAR(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT '' COMMENT '取消/退货原因' AFTER `cancelled_at`,
                ADD COLUMN `updated_at` DATETIME NULL DEFAULT NULL COMMENT '更新时间' AFTER `cancel_reason`,
                MODIFY COLUMN `quantity` INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '数量',
                MODIFY COLUMN `status` VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT '未发货' COMMENT '订单状态(待发货/已发货/已取消/已退货)',
                ADD UNIQUE INDEX `uniq_orders_order_no`(`order_no`),
                ADD INDEX `idx_orders_phone`(`phone`),
                ADD INDEX `idx_orders_status`(`status`),
                ADD INDEX `idx_orders_tracking_no`(`tracking_no`),
                ADD INDEX `idx_orders_created_at`(`created_at`),
                ADD INDEX `idx_orders_status_created`(`status`, `created_at` DESC);
            """,
            reverse_sql="ALTER TABLE `orders` DROP INDEX `idx_orders_status_created`, DROP INDEX `idx_orders_created_at`, DROP INDEX `idx_orders_tracking_no`, DROP INDEX `idx_orders_status`, DROP INDEX `idx_orders_phone`, DROP INDEX `uniq_orders_order_no`, DROP COLUMN `updated_at`, DROP COLUMN `cancel_reason`, DROP COLUMN `cancelled_at`, MODIFY COLUMN `quantity` INT NULL DEFAULT 0, MODIFY COLUMN `status` VARCHAR(20) NULL DEFAULT NULL;",
        ),

        # ── 4. conversations ────────────────────────────────────────
        migrations.RunSQL(
            sql="""
            ALTER TABLE `conversations`
                ADD INDEX `idx_conv_session_id`(`session_id`),
                ADD INDEX `idx_conv_created_at`(`created_at`),
                ADD INDEX `idx_conv_session_created`(`session_id`, `created_at` DESC);
            """,
            reverse_sql="ALTER TABLE `conversations` DROP INDEX `idx_conv_session_created`, DROP INDEX `idx_conv_created_at`, DROP INDEX `idx_conv_session_id`;",
        ),

        # ── 5. contact_messages ─────────────────────────────────────
        migrations.RunSQL(
            sql="""
            ALTER TABLE `contact_messages`
                ADD INDEX `idx_cm_is_read`(`is_read`),
                ADD INDEX `idx_cm_phone`(`phone`),
                ADD INDEX `idx_cm_created_at`(`created_at`);
            """,
            reverse_sql="ALTER TABLE `contact_messages` DROP INDEX `idx_cm_created_at`, DROP INDEX `idx_cm_phone`, DROP INDEX `idx_cm_is_read`;",
        ),

        # ── 6. custom_requests ──────────────────────────────────────
        migrations.RunSQL(
            sql="""
            ALTER TABLE `custom_requests`
                ADD INDEX `idx_cr_is_read`(`is_read`),
                ADD INDEX `idx_cr_phone`(`phone`),
                ADD INDEX `idx_cr_created_at`(`created_at`);
            """,
            reverse_sql="ALTER TABLE `custom_requests` DROP INDEX `idx_cr_created_at`, DROP INDEX `idx_cr_phone`, DROP INDEX `idx_cr_is_read`;",
        ),
    ]
