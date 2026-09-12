# 0011_orders_user_and_after_sale.py
#
# 个人中心（C 端）数据层改造：
#   1. orders.user_id —— 订单归属用户，个人中心「我的订单」的核心依据
#        · 登录状态下单时写入（create_order_api）
#        · 历史订单为空，靠 UserProfile.phone 匹配手机号归属
#   2. orders.completed_at —— 用户确认收货时间
#   3. 退货申请闭环四件套：
#        · return_status       待审核 / 已同意 / 已拒绝
#        · return_reason       用户填写的退货原因
#        · return_requested_at 申请时间
#        · return_handled_at / return_note  商家处理时间与备注（含拒绝理由）
#
# 说明：orders 是 managed=False 的既存表，Django 的 autodetector 会跳过 unmanaged
# 模型，所以字段变更不进状态图，必须像 0006 那样手工写 DDL（沿用本 app 的既有约定）。
# 这里用 RunPython + information_schema 探测，做成**幂等**，可重复执行不报错。
from django.db import migrations

# 新增列：(列名, DDL 片段)
COLUMNS = [
    ("user_id", "ADD COLUMN `user_id` INT NULL DEFAULT NULL COMMENT '下单用户(登录用户)'"),
    ("completed_at", "ADD COLUMN `completed_at` DATETIME NULL DEFAULT NULL COMMENT '用户确认收货时间'"),
    ("return_status", "ADD COLUMN `return_status` VARCHAR(20) NOT NULL DEFAULT '' COMMENT '退货处理结果(待审核/已同意/已拒绝)'"),
    ("return_reason", "ADD COLUMN `return_reason` VARCHAR(200) NOT NULL DEFAULT '' COMMENT '用户填写的退货原因'"),
    ("return_requested_at", "ADD COLUMN `return_requested_at` DATETIME NULL DEFAULT NULL COMMENT '用户申请退货时间'"),
    ("return_handled_at", "ADD COLUMN `return_handled_at` DATETIME NULL DEFAULT NULL COMMENT '商家处理退货时间'"),
    ("return_note", "ADD COLUMN `return_note` VARCHAR(200) NOT NULL DEFAULT '' COMMENT '商家处理退货备注/拒绝理由'"),
]

# 新增索引/外键：(名称, 探测类型, DDL)
INDEXES = [
    ("idx_orders_user", "INDEX", "ADD INDEX `idx_orders_user` (`user_id`)"),
    ("idx_orders_return_status", "INDEX", "ADD INDEX `idx_orders_return_status` (`return_status`)"),
    ("orders_user_fk", "FOREIGN KEY",
     "ADD CONSTRAINT `orders_user_fk` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`) ON DELETE SET NULL"),
]


def _existing(cursor, table, kind, name):
    """探测列 / 索引 / 外键是否已存在（information_schema）。"""
    if kind == "COLUMN":
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            [table, name],
        )
    elif kind == "INDEX":
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s",
            [table, name],
        )
    else:  # FOREIGN KEY
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND CONSTRAINT_NAME = %s AND CONSTRAINT_TYPE = 'FOREIGN KEY'",
            [table, name],
        )
    return cursor.fetchone()[0] > 0


def forwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    added = []
    for col, ddl in COLUMNS:
        if _existing(cursor, "orders", "COLUMN", col):
            continue
        cursor.execute(f"ALTER TABLE `orders` {ddl}")
        added.append(col)
    for name, kind, ddl in INDEXES:
        if _existing(cursor, "orders", kind, name):
            continue
        cursor.execute(f"ALTER TABLE `orders` {ddl}")
        added.append(name)
    print(f"  [0011] orders 新增：{added or '无（已是最新）'}")


def backwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for name, kind, _ in reversed(INDEXES):
        if kind == "FOREIGN KEY":
            cursor.execute(f"ALTER TABLE `orders` DROP FOREIGN KEY `{name}`")
        else:
            cursor.execute(f"ALTER TABLE `orders` DROP INDEX `{name}`")
    for col, _ in reversed(COLUMNS):
        cursor.execute(f"ALTER TABLE `orders` DROP COLUMN `{col}`")


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0010_userprofile"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
