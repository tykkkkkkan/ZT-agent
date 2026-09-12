"""
库存与跨 Agent 暂停状态对账修复

解决两类「后端说够、前端说没有」的问题
--------------------------------------
A. **预占泄漏**：`inventory.reserved_stock` 与「未发货订单」的数量之和对不上。
   不变式：只有「未发货(PENDING)」的订单才应该持有预占。
   预占只增不减的常见原因：订单被直接改库/删行、历史数据未走服务层、
   调试期间手工改状态等 —— 结果就是**可用库存凭空变少**，
   总库存够、可用库存不够，前端就买不了。

B. **断货暂停死锁**：营销 Agent 因断货下发 `pause_product` 后，
   运营把库存补齐，但覆盖表里 `purchase_paused` 仍是 True
   → 后台库存显示充足，前台却永远显示不可购买。
   本命令扫描「已有可用库存、但仍处于断货型暂停」的商品并解除。

顺带回填 `pause_reason`（该列后加，历史行为空；空值按 stockout 处理）。

安全设计
--------
· 默认**预演**，只有 `--apply` 才写库
· B 类修复只处理「断货型暂停」（`is_auto_resumable`）；人工暂停(manual)不碰
· A 类修复只把 reserved_stock **调整为**未发货订单之和（不猜、不额外增减）

用法
----
    python manage.py reconcile_inventory              # 预演
    python manage.py reconcile_inventory --apply      # 执行
    python manage.py reconcile_inventory --only reservation
    python manage.py reconcile_inventory --only pause
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from agent.models import Inventory, OrderStatus, Orders, ProductCoordination


class Command(BaseCommand):
    help = ('库存对账：修正预占泄漏（reserved_stock 与未发货订单不一致），'
            '并解除「库存已恢复但仍暂停接单」的死锁（默认预演）')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正写库（默认只预演，不改任何数据）')
        parser.add_argument('--only', choices=['reservation', 'pause'], default='',
                            help='只处理其中一类（默认两类都处理）')

    # ── A. 预占泄漏 ────────────────────────────────────────────
    def _fix_reservations(self, apply):
        from collections import Counter

        pending_qty = Counter()
        for o in Orders.objects.filter(status=OrderStatus.PENDING) \
                .exclude(product_id__isnull=True):
            pending_qty[o.product_id] += (o.quantity or 0)

        drifted = []
        for inv in Inventory.objects.select_related('product').order_by('product_id'):
            expect = pending_qty.get(inv.product_id, 0)
            actual = inv.reserved_stock or 0
            if expect != actual:
                drifted.append((inv, expect, actual))

        self.stdout.write(self.style.MIGRATE_HEADING('A. 预占对账（reserved_stock vs 未发货订单）'))
        self.stdout.write(f'  不变量：reserved_stock == Σ「未发货」订单数量')
        if not drifted:
            self.stdout.write(self.style.SUCCESS('  ✓ 全部一致，无需修正'))
            return 0

        self.stdout.write(self.style.WARNING(f'  发现 {len(drifted)} 项不一致：'))
        for inv, expect, actual in drifted:
            name = inv.product.name if inv.product else f'#{inv.product_id}'
            self.stdout.write(
                f'    {name}：预占 {actual} → 应为 {expect}'
                f'（可用库存 {inv.available_stock} → {max(0, (inv.stock or 0) - expect)}）'
            )

        if not apply:
            return len(drifted)

        with transaction.atomic():
            for inv, expect, _actual in drifted:
                locked = Inventory.objects.select_for_update().get(pk=inv.pk)
                locked.reserved_stock = expect
                locked.save(update_fields=['reserved_stock', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'  ✓ 已修正 {len(drifted)} 项预占'))
        self.stdout.write('    （只调整预占数字，未改动总库存 stock，也不影响任何订单）')
        return len(drifted)

    # ── B. 断货暂停死锁 ────────────────────────────────────────
    def _fix_pause_deadlock(self, apply):
        from agent.services import auto_resume_stockout_pause

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('B. 跨 Agent 暂停对账（库存已恢复但仍暂停接单）'))
        coords = list(ProductCoordination.objects.filter(purchase_paused=True))
        if not coords:
            self.stdout.write(self.style.SUCCESS('  ✓ 当前没有暂停接单的商品'))
            return 0, 0

        inv_map = {i.product_id: i for i in Inventory.objects.all()}
        deadlocked, manual = [], []
        for c in coords:
            inv = inv_map.get(c.product_id)
            avail = inv.available_stock if inv else 0
            name = inv.product.name if (inv and inv.product) else f'#{c.product_id}'
            if not c.is_auto_resumable:
                manual.append((name, c))
                continue
            if avail > 0:
                deadlocked.append((name, c, inv))
            else:
                manual.append((name, c))       # 断货型但确实还没货 → 暂不处理

        self.stdout.write(
            f'  暂停接单商品 {len(coords)} 个：可自动恢复 {len(deadlocked)} 个，'
            f'保留暂停 {len(manual)} 个'
        )
        if deadlocked:
            self.stdout.write(self.style.WARNING('  【可自动恢复】库存已够，但仍在暂停接单：'))
            for name, c, inv in deadlocked:
                self.stdout.write(
                    f'    {name}：可用库存 {inv.available_stock} 包，'
                    f'暂停原因={c.pause_reason or "(空→按断货处理)"}，'
                    f'由 {c.updated_by} 于 {c.updated_at:%Y-%m-%d %H:%M} 设置'
                )
        if manual:
            self.stdout.write('  【保留暂停】')
            for name, c in manual:
                why = ('人工暂停，不自动解除' if not c.is_auto_resumable
                       else '断货型，但当前可用库存仍为 0')
                self.stdout.write(f'    {name}：{why}')

        if not deadlocked:
            return 0, len(manual)

        if not apply:
            return len(deadlocked), len(manual)

        with transaction.atomic():
            for _name, c, inv in deadlocked:
                locked = Inventory.objects.select_for_update().get(pk=inv.pk)
                msg = auto_resume_stockout_pause(locked)
                if msg:
                    self.stdout.write(f'  ✓ {msg}')
        return len(deadlocked), len(manual)

    # ── 回填 pause_reason ─────────────────────────────────────
    def _backfill_reason(self, apply):
        stale = ProductCoordination.objects.filter(purchase_paused=True, pause_reason='')
        n = stale.count()
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('C. 回填 pause_reason（该列后加，历史行为空）'))
        if not n:
            self.stdout.write(self.style.SUCCESS('  ✓ 无需回填'))
            return 0
        self.stdout.write(f'  {n} 行为空 —— 这些全部由营销侧断货巡检写入，按 stockout 回填')
        if apply:
            stale.update(pause_reason=ProductCoordination.PAUSE_STOCKOUT)
            self.stdout.write(self.style.SUCCESS(f'  ✓ 已回填 {n} 行'))
        return n

    def handle(self, *args, **options):
        apply = options['apply']
        only = options['only']

        self.stdout.write(self.style.MIGRATE_HEADING('=' * 70))
        self.stdout.write(f'库存对账{"（执行）" if apply else "（预演，不改数据）"}')
        self.stdout.write(self.style.MIGRATE_HEADING('=' * 70))

        n_res = 0
        n_pause = 0
        if only in ('', 'reservation'):
            n_res = self._fix_reservations(apply)
            self.stdout.write('')
        if only in ('', 'pause'):
            n_pause, _kept = self._fix_pause_deadlock(apply)
            if apply and n_pause:
                self._backfill_reason(apply)

        self.stdout.write('')
        if not apply:
            self.stdout.write(self.style.NOTICE(
                f'预演结束，未改动任何数据。\n'
                f'待修正：预占 {n_res} 项、暂停死锁 {n_pause} 项。\n'
                f'确认无误后执行：\n'
                f'  python manage.py reconcile_inventory --apply'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'完成：修正预占 {n_res} 项、解除暂停死锁 {n_pause} 项。'
            ))
