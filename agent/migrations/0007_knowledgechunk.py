# 0007_knowledgechunk.py
#
# RAG 知识库数据化（P2）：新增 knowledge_chunks 表（managed=True，Django 自动建表）。
#
# 说明：本表由 Django 建表（非 managed=False），执行 `migrate agent 0007` 即可。
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0006_orders_snapshot_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='KnowledgeChunk',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(db_index=True, max_length=100, verbose_name='标题')),
                ('category', models.CharField(
                    db_index=True, default='general', max_length=50,
                    verbose_name='分类（季节钓法/鱼种钓法/钓法技巧/企业政策/其他）',
                )),
                ('keywords', models.CharField(blank=True, default='', max_length=255, verbose_name='检索关键词（逗号分隔）')),
                ('content', models.TextField(verbose_name='内容（回答文本）')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='启用')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
            ],
            options={
                'verbose_name': '知识库条目',
                'verbose_name_plural': '知识库条目',
                'db_table': 'knowledge_chunks',
                'indexes': [
                    models.Index(fields=['category', 'is_active'], name='idx_kc_cat_active'),
                ],
            },
        ),
    ]
