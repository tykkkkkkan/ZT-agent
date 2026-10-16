# 0020_user_address.py
# 收货地址簿：新增 user_address 表（C 端「我的地址」）。
#
# 为什么之前没有这张表也能下单：
#   订单表里的 address / customer_name / phone 是**快照**，下单时现填现存。
#   所以「能下单」这条链路早就通了，缺的是"不用每次重填"这件事 ——
#   地址簿是纯增量，不动 orders 任何一列（任务书四·4 的验收项不受影响）。
#
# 数据回填为什么不迁 UserProfile：
#   UserProfile 只有 nickname（收货人姓名），**没有详细地址**。
#   拿它建地址只会得到一堆「张三 · 空地址」的废行，点一下还得让用户重填。
#   真正可用的地址只存在于历史订单里，所以只从 orders 回填。
import os

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_from_orders(apps, schema_editor):
    """把历史订单里出现过的收货信息，整理成每个账号的地址簿。

    ⚠️ 这里必须用**原生 SQL**，不能用 `apps.get_model('agent', 'Orders')`：
    orders 是 `managed=False` 的既存表，autodetector 会跳过 unmanaged 模型，
    所以它的列只在 0001_initial 里登记过一次，之后 0006 / 0011 / 0014 / 0015
    新增的 `user_id` / `address` 等列**都不在迁移状态图里** ——
    历史模型上根本查不到这些字段（会直接 FieldError）。
    这条约定本仓库 0011 的注释里已经写过一次，别再踩。

    规则：
      · 只取 `user_id` 有值（登录后下的单）且地址非空的记录 ——
        未登录的历史订单无法确定归属，不能凭手机号猜（会串号）；
      · 同一账号下按 (收货人, 电话, 地址) 去重，最多保留 5 条；
      · **默认地址取最近一单里地址最完整的那条**（详见下方 MIN_USABLE）——
        历史上有人把"湖北"这种半截地址填进过订单，选中它等于没选。
    """
    Address = apps.get_model('agent', 'Address')
    cursor = schema_editor.connection.cursor()

    # 幂等：已有人工新增过地址则不再回填（避免重复跑迁移时灌双份）
    if Address.objects.exists():
        return

    MAX_PER_USER = 5
    # 短于此长度的"地址"视为随手一填（如只写了省市），不作为默认地址的候选
    MIN_USABLE = 5

    cursor.execute(
        """
        SELECT user_id, customer_name, phone, address
          FROM orders
         WHERE user_id IS NOT NULL
           AND address IS NOT NULL
           AND address <> ''
         ORDER BY user_id ASC, created_at DESC, id DESC
        """
    )
    rows = cursor.fetchall()

    seen = {}     # user_id -> {(收货人, 电话, 地址)}
    buckets = {}  # user_id -> [(收货人, 电话, 地址)]，最近的一单在最前
    for user_id, name, phone, addr in rows:
        addr = (addr or '').strip()
        if not addr:
            continue
        key = ((name or '').strip(), (phone or '').strip(), addr)
        if key in seen.setdefault(user_id, set()):
            continue
        if len(buckets.get(user_id, [])) >= MAX_PER_USER:
            continue
        seen[user_id].add(key)
        buckets.setdefault(user_id, []).append(key)

    total = 0
    for user_id, items in buckets.items():
        # 默认地址：最近一单中第一条「像地址」的（长度 >= MIN_USABLE）；
        # 都不像地址时退回最近一单，宁可选中短地址也不留空默认。
        default_idx = next(
            (i for i, it in enumerate(items) if len(it[2]) >= MIN_USABLE), 0,
        )
        for idx, (name, phone, addr) in enumerate(items):
            Address.objects.create(
                user_id=user_id,
                receiver=name or '收货人',
                phone=phone or '',
                address=addr,
                is_default=(idx == default_idx),
            )
            total += 1

    if total:
        print(f'    [0020] 已从历史订单回填 {total} 条收货地址'
              f'（覆盖 {len(buckets)} 个账号）')
    else:
        print('    [0020] 无可回填的历史地址')


class Migration(migrations.Migration):

    # ⚠️ 为什么要做这个「存在性判断」（别删）：
    #   0019_contactmessage_replied_at_contactmessage_reply 是**并行开发**中的一张
    #   留言答复表迁移，此刻还没进仓库。Django 对图上不存在的节点是零容忍的 ——
    #   无论写在 `dependencies` 还是 `run_before` 里，都会在 build_graph →
    #   validate_consistency 阶段被当成 DummyNode 抛 NodeNotFoundError
    #   （两条路径都实测过），一旦报错 manage.py 的**任何**命令都跑不了。
    #
    #   所以这里按文件是否落地，把依赖指向"当前真正存在的最后一环"：
    #     · 0019 尚未提交（别人 clone / CI / 答辩机器）→ 挂 0018 之下；
    #     · 0019 已提交（本机现状）→ 挂 0019 之下。
    #   两种情况下 0020 都是**唯一叶子**，不会出现 two-leaf 让 makemigrations
    #   报 "Conflicting migrations detected"。
    #   （不要试图同时写 dependencies + run_before 指向同一个迁移 —— 那会构成
    #     环：0020 依赖 0019 又要求 0019 排在自己之后 → CircularDependencyError。）
    #   两张表互不相干（user_address 建表 vs contact_messages 加两列），
    #   谁先谁后都不影响数据，所以这样挂是安全的。
    _SIBLING = '0019_contactmessage_replied_at_contactmessage_reply'
    _HAS_SIBLING = os.path.exists(os.path.join(os.path.dirname(__file__), _SIBLING + '.py'))

    dependencies = [
        ('agent', _SIBLING if _HAS_SIBLING else '0018_products_category'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Address',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('receiver', models.CharField(db_comment='收货人姓名', max_length=50, verbose_name='收货人')),
                ('phone', models.CharField(db_comment='收货人电话', max_length=20, verbose_name='联系电话')),
                ('address', models.CharField(db_comment='详细收货地址', help_text='省市区 + 街道门牌，越完整越好', max_length=255, verbose_name='详细地址')),
                ('is_default', models.BooleanField(db_comment='是否默认地址(每账号至多一个，由应用层保证)', db_index=True, default=False, verbose_name='设为默认')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('user', models.ForeignKey(db_column='user_id', db_comment='所属用户', on_delete=django.db.models.deletion.CASCADE, related_name='addresses', to=settings.AUTH_USER_MODEL, verbose_name='所属账号')),
            ],
            options={
                'verbose_name': '收货地址',
                'verbose_name_plural': '收货地址',
                'db_table': 'user_address',
                'ordering': ['-is_default', '-updated_at', '-id'],
                'indexes': [models.Index(fields=['user', '-is_default'], name='idx_addr_user_default')],
            },
        ),
        migrations.RunPython(backfill_from_orders, migrations.RunPython.noop),
    ]
