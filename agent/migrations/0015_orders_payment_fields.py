# 0015_orders_payment_fields.py
#
# 任务书五·1：「支付与订单跟踪」——订单需记录支付状态与支付时间。
#
# 采用**轻量正交方案**（不改订单状态机）：
#   1. orders.payment_status  支付状态（空=历史订单 / 未支付 / 已支付 / 已退款）
#   2. orders.paid_at         支付时间
#   3. idx_orders_pay_status_created  复合索引（支付状态 + 下单时间）
#
# 为什么默认值是空字符串而不是「未支付」
# ------------------------------------
# orders 是既存表，库里已有大量历史订单。若新列默认「未支付」，这些老订单会
# 一夜之间变成"未支付"，被「未支付不许发货」闸门拦下、还会被超时任务批量取消 ——
# 属于典型的"上线即回滚"。所以 DB 默认 ''（=未跟踪，视为已结算），
# 只有**支付功能上线后新建的订单**才由应用层显式写入「未支付」。
#
# 说明：orders 是 managed=False 的既存表，Django autodetector 会跳过 unmanaged 模型，
# 字段变更不进状态图，因此沿用 0006 / 0011 / 0014 的既有约定手工写 DDL，
# 并用 RunPython + information_schema 探测做成**幂等**，可重复执行不报错。
from django.db import migrations

# 新增列：(列名, DDL 片段)
COLUMNS = [
    ("payment_status",
     "ADD COLUMN `payment_status` VARCHAR(10) NOT NULL DEFAULT '' "
     "COMMENT '支付状态(空=历史订单/未支付/已支付/已退款)'"),
    ("paid_at",
     "ADD COLUMN `paid_at` DATETIME NULL DEFAULT NULL COMMENT '支付时间'"),
]

# 新增索引：(索引名, DDL 片段)
# 超时取消任务按 `payment_status='未支付' AND status='未发货' AND created_at < 截止` 扫描，
# 这个复合索引让它走索引而不是全表扫描。
INDEXES = [
    ("idx_orders_pay_status_created",
     "ADD INDEX `idx_orders_pay_status_created` (`payment_status`, `created_at`)"),
]


def _existing(cursor, table, kind, name):
    """探测列 / 索引是否已存在（information_schema）。"""
    if kind == "COLUMN":
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
            [table, name],
        )
    else:  # INDEX
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
        if _existing(cursor, "orders", "COLUMN", col):
            continue
        cursor.execute(f"ALTER TABLE `orders` {ddl}")
        added.append(col)
    for name, ddl in INDEXES:
        if _existing(cursor, "orders", "INDEX", name):
            continue
        cursor.execute(f"ALTER TABLE `orders` {ddl}")
        added.append(name)
    print(f"  [0015] orders 新增：{added or '无（已是最新）'}")


def backwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for name, _ in reversed(INDEXES):
        if _existing(cursor, "orders", "INDEX", name):
            cursor.execute(f"ALTER TABLE `orders` DROP INDEX `{name}`")
    for col, _ in reversed(COLUMNS):
        if _existing(cursor, "orders", "COLUMN", col):
            cursor.execute(f"ALTER TABLE `orders` DROP COLUMN `{col}`")


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0014_orders_delivery_fields"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
