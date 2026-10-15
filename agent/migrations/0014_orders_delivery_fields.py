# 0014_orders_delivery_fields.py
#
# 任务书四·4：「订单的产品数量、**收货地址**与**交期**等具体信息由用户在下单界面
# 自行填写并提交」。此前 orders 表只有姓名与电话，客户在哪收货、期望何时到货都无处记录，
# 商家发货前必须再打电话问一遍。
#
#   1. orders.address        收货地址（用户在下单页填写，必填）
#   2. orders.expected_date  期望交期 / 期望到货日期（选填，留空表示"尽快发货"）
#
# 说明：orders 是 managed=False 的既存表，Django autodetector 会跳过 unmanaged 模型，
# 字段变更不进状态图，因此沿用 0006 / 0011 的既有约定手工写 DDL，
# 并用 RunPython + information_schema 探测做成**幂等**，可重复执行不报错。
from django.db import migrations

# 新增列：(列名, DDL 片段)
COLUMNS = [
    ("address", "ADD COLUMN `address` VARCHAR(255) NOT NULL DEFAULT '' COMMENT '收货地址'"),
    ("expected_date", "ADD COLUMN `expected_date` DATE NULL DEFAULT NULL COMMENT '期望交期(期望到货日期)'"),
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
    print(f"  [0014] orders 新增：{added or '无（已是最新）'}")


def backwards(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for col, _ in reversed(COLUMNS):
        if _existing(cursor, "orders", "COLUMN", col):
            cursor.execute(f"ALTER TABLE `orders` DROP COLUMN `{col}`")


class Migration(migrations.Migration):

    dependencies = [
        ("agent", "0013_productcoordination_pause_reason"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
