"""
修复「订单被错记到后台管理员账号名下」

背景（真实事故，症状很反直觉）
------------------------------
同一浏览器里既登录了 `/admin/`（管理员）又登录了 C 端时，原先
`_resolve_request_user` **session 优先**，导致前端下的单被记到管理员名下：

    Orders.user_id = 管理员 id
      → 后台订单列表看得见（管理员本来就能看全部订单）
      → C 端「我的订单」查不到：
          · `user_id == 我` 不成立
          · 手机号兜底那条规则要求 `user_id IS NULL`，也被排除
      → 用户视角：单下了、付了、后台有记录，但「我的」里"订单不见了"

代码根因已在 `agent/views.py::_resolve_request_user` 修为「显式 JWT 优先于
session」，新订单不会再错。**但历史错记的订单不会自己回来**，本命令负责修正。

修正规则（保守，只动"明确是错的"）
----------------------------------
对每一笔订单：
  · 当前归属账号是 **staff（后台账号）**，且
  · 该订单手机号能唯一匹配到一个 **非 staff** 的 UserProfile
  → 判定为错记，改归属到该用户。

不做的事：
  · 不动 `user_id IS NULL` 的历史订单（它们已能通过"手机号 + 未认领"规则看到）
  · 手机号匹配到多个非 staff 账号时**跳过**并提示（避免猜错归属）
  · 手机号为空、或匹配不到任何账号时跳过

用法
----
    # 预演（默认，不改任何数据）
    python manage.py fix_order_owner
    # 确认无误后执行
    python manage.py fix_order_owner --apply
    # 只看某一笔
    python manage.py fix_order_owner --order-no DD202609121116514709
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from agent.models import Orders, UserProfile


class Command(BaseCommand):
    help = '把被错记到后台管理员名下的订单，改回真正的 C 端下单用户（默认预演）'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正写库（默认只预演，不改任何数据）')
        parser.add_argument('--order-no', default='',
                            help='只处理指定订单号（排查单笔时用）')

    def handle(self, *args, **options):
        apply = options['apply']
        order_no = (options['order_no'] or '').strip()

        # phone → 非 staff 用户（用于唯一匹配）；同一手机号多人绑定时标记为歧义
        phone_map: dict[str, list] = {}
        for prof in UserProfile.objects.select_related('user').all():
            phone = (prof.phone or '').strip()
            if not phone or prof.user.is_staff:
                continue
            phone_map.setdefault(phone, []).append(prof.user)

        qs = Orders.objects.filter(user__isnull=False, user__is_staff=True).select_related('user')
        if order_no:
            qs = qs.filter(order_no=order_no)
        candidates = list(qs.order_by('id'))

        self.stdout.write(self.style.MIGRATE_HEADING('=' * 68))
        self.stdout.write('扫描「归属账号是后台 staff」的订单……')
        self.stdout.write(f'  命中 {len(candidates)} 笔')
        self.stdout.write(self.style.MIGRATE_HEADING('=' * 68))

        fixable, skipped = [], []
        for o in candidates:
            phone = (o.phone or '').strip()
            if not phone:
                skipped.append((o, '订单没有手机号，无法判定真实下单人'))
                continue
            users = phone_map.get(phone, [])
            if not users:
                skipped.append((o, f'手机号 {phone} 没有任何非 staff 账号绑定'))
                continue
            if len(users) > 1:
                names = '、'.join(u.username for u in users)
                skipped.append((o, f'手机号 {phone} 被多个账号绑定（{names}），归属有歧义，跳过'))
                continue
            fixable.append((o, users[0]))

        if fixable:
            self.stdout.write(self.style.WARNING('【可修正】'))
            for o, target in fixable:
                self.stdout.write(
                    f'  {o.order_no}  ¥{o.total_price or 0}  {o.status}  '
                    f'手机号 {o.phone}\n'
                    f'      归属：{o.user.username}(staff) → {target.username}(C端 id={target.id})'
                )
        if skipped:
            self.stdout.write(self.style.NOTICE('\n【跳过】'))
            for o, why in skipped:
                self.stdout.write(f'  {o.order_no}：{why}')

        if not fixable:
            self.stdout.write(self.style.SUCCESS('\n没有需要修正的订单。'))
            return

        if not apply:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                f'预演结束，未改动任何数据。确认无误后执行：\n'
                f'  python manage.py fix_order_owner --apply'
            ))
            return

        fixed = 0
        with transaction.atomic():
            for o, target in fixable:
                before = o.user_id
                o.user_id = target.id
                o.save(update_fields=['user_id', 'updated_at'])
                fixed += 1
                self.stdout.write(
                    f'  ✓ {o.order_no}：user_id {before} → {target.id}（{target.username}）'
                )

        self.stdout.write(self.style.SUCCESS(f'\n已修正 {fixed} 笔订单的归属。'))
        self.stdout.write(
            '这些订单现在会立刻出现在对应用户的「我的订单」中'
            '（user_id 精确归属优先于手机号兜底）。\n'
            '提示：库存与钱包流水未受影响 —— 本次只改订单归属，不触碰库存/账目。'
        )
