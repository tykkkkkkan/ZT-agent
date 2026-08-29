# 0008_wallet.py
# 余额子系统：新增 company_wallet（公司钱包）与 wallet_transactions（收支流水）两表。
#
# 包含一个状态修正（SeparateDatabaseAndState）：
#   0001_initial 把 Orders.id 固化为 BigAutoField，但 DB 实际是 int（AUTO_INCREMENT），
#   导致本迁移的 Transaction.order_id FK 会被生成成 bigint，与 orders.id(int) 不兼容。
#   这里把迁移状态修正为 AutoField，不动 DB（managed=False 也不会改）。
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0007_knowledgechunk'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 状态修正：把 Orders.id 在迁移图里改成 AutoField（与 DB 实际 int 对齐）
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AlterField(
                    model_name='orders',
                    name='id',
                    field=models.AutoField(primary_key=True, serialize=False, verbose_name='ID'),
                ),
            ],
        ),
        migrations.CreateModel(
            name='Wallet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('balance', models.DecimalField(
                    decimal_places=2, default=0, max_digits=14,
                    verbose_name='当前余额',
                    help_text='所有收入（+）减所有支出（-）的累计结果',
                )),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '公司钱包',
                'verbose_name_plural': '公司钱包',
                'db_table': 'company_wallet',
            },
        ),
        migrations.CreateModel(
            name='Transaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('tx_type', models.CharField(
                    max_length=10,
                    choices=[('收入', '收入'), ('支出', '支出')],
                    verbose_name='类型',
                )),
                ('category', models.CharField(
                    max_length=20,
                    choices=[
                        ('订单收入', '订单收入'),
                        ('订单退款', '订单退款'),
                        ('提现', '提现'),
                        ('手动调账', '手动调账'),
                        ('其他', '其他'),
                    ],
                    verbose_name='分类',
                )),
                ('amount', models.DecimalField(
                    decimal_places=2, max_digits=14,
                    verbose_name='金额',
                    help_text='正数；通过 tx_type 决定是收入还是支出',
                )),
                ('note', models.CharField(blank=True, default='', max_length=200, verbose_name='备注')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='时间')),
                ('operator', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to=settings.AUTH_USER_MODEL,
                    verbose_name='操作人',
                )),
                ('order', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='transactions',
                    to='agent.orders',
                    verbose_name='关联订单',
                )),
                ('wallet', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='transactions',
                    to='agent.wallet',
                    verbose_name='钱包',
                )),
            ],
            options={
                'verbose_name': '收支流水',
                'verbose_name_plural': '收支流水',
                'db_table': 'wallet_transactions',
                'ordering': ('-created_at',),
                'indexes': [
                    models.Index(fields=['tx_type', '-created_at'], name='idx_tx_type_time'),
                    models.Index(fields=['category', '-created_at'], name='idx_tx_cat_time'),
                ],
            },
        ),
    ]
