"""用户侧钱包：`user_wallet` + `user_wallet_transactions`。

为什么与 `company_wallet` 分开建表？
-----------------------------------
`company_wallet` 是**经营账**：全站只有一条记录（id=1），记的是"商家收了多少钱"，
订单发货记收入、退货记支出。本迁移建的是**预存账**：一个账号一本，记的是
"这个用户自己充进来、又被花掉的余额"。同一个业务动作在两本账上方向是相反的
（用户余额支付 100 元 → 公司账 +100、用户账 −100），共用一张表会让报表口径必错。

表结构要点
----------
· `user_id` 是**真外键 + 唯一约束**（OneToOneField）—— "一人一本"由数据库保证，
  而不是靠应用层 `filter().first()`（并发下会各建一行）。
· `balance_after` 冗余记下"这笔之后的余额"，让流水可独立验算，不依赖任何缓存。
· 金额一律**正数**，方向由 `kind` 决定，与 `wallet_transactions` 保持同一口径。

不涉及既有表 —— 纯新增，对历史数据零影响；也就没有回填步骤。
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0020_user_address'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='UserWallet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('balance', models.DecimalField(db_comment='账户余额', decimal_places=2, default=0, help_text='充值（+）与退款（+）减支付（−）的累计结果', max_digits=12, verbose_name='当前余额')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('user', models.OneToOneField(db_column='user_id', db_comment='所属用户(一对一)', on_delete=django.db.models.deletion.CASCADE, related_name='wallet', to=settings.AUTH_USER_MODEL, verbose_name='所属账号')),
            ],
            options={
                'verbose_name': '用户钱包',
                'verbose_name_plural': '用户钱包',
                'db_table': 'user_wallet',
            },
        ),
        migrations.CreateModel(
            name='UserTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('充值', '充值'), ('支付', '支付'), ('退款', '退款')], db_comment='充值/支付/退款', max_length=10, verbose_name='类型')),
                ('amount', models.DecimalField(db_comment='发生额(正数)', decimal_places=2, help_text='正数；方向由 kind 决定（充值/退款为加，支付为减）', max_digits=12, verbose_name='金额')),
                ('balance_after', models.DecimalField(db_comment='该笔记账后的余额', decimal_places=2, max_digits=12, verbose_name='记账后余额')),
                ('order_no', models.CharField(blank=True, db_comment='关联订单号(余额支付/退款来源)', db_index=True, default='', max_length=50, verbose_name='关联订单号')),
                ('note', models.CharField(blank=True, db_comment='备注', default='', max_length=200, verbose_name='备注')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_comment='记账时间', db_index=True, verbose_name='时间')),
                ('wallet', models.ForeignKey(db_comment='所属用户钱包', on_delete=django.db.models.deletion.CASCADE, related_name='transactions', to='agent.userwallet', verbose_name='钱包')),
            ],
            options={
                'verbose_name': '用户余额流水',
                'verbose_name_plural': '用户余额流水',
                'db_table': 'user_wallet_transactions',
                'ordering': ('-created_at', '-id'),
                'indexes': [models.Index(fields=['wallet', '-created_at'], name='idx_utx_wallet_time'), models.Index(fields=['kind', '-created_at'], name='idx_utx_kind_time')],
            },
        ),
    ]
