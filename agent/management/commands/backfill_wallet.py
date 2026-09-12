"""补记公司钱包的历史钱款流水。

用法：
    python manage.py backfill_wallet                      # 预览（默认不改数据）
    python manage.py backfill_wallet --apply              # 实际补记
    python manage.py backfill_wallet --apply --reconcile   # 同时补平"流水累计 ≠ 余额"的差额
    python manage.py backfill_wallet --since 2026-08-01    # 只处理该日期之后发货的订单

背景：
    「订单发货自动记收入」是后加的能力。在此之前已发货的订单，钱其实收了，但钱包
    里既没有这笔收入、也没进余额——于是出现两种病症：
      ① 公司钱包看不到这笔钱（历史欠账）；
      ② 流水累计与钱包余额对不上（账实不符）。
    本命令为「已发货但没有订单收入流水」的订单补一笔「订单收入」，记账时间取订单的
    发货时间（没有则取下单时间），保证账本时间线正确。

安全性：
    - 默认 dry-run，只打印将要补记的内容，不动数据库；加 --apply 才写库。
    - 幂等：已经记过的订单不会被重复补记。
    - 走 services._record_wallet_tx，与在线业务同一套加锁/事务逻辑，余额与流水同步。
"""
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum, Q
from django.utils import timezone

from agent.models import Orders, Transaction, Wallet, TxType, TxCategory, OrderStatus
from agent.services import _record_wallet_tx


class Command(BaseCommand):
    help = "为已发货但缺少收入流水的订单补记公司钱包流水（默认 dry-run）"

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='真正写入数据库（不加此参数只预览，不改任何数据）',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='显式预览模式（与不加 --apply 等效，便于脚本里写明意图）',
        )
        parser.add_argument(
            '--reconcile', action='store_true',
            help='补记后若"流水累计 ≠ 钱包余额"，再记一笔「账目核对调整」把差额补平',
        )
        parser.add_argument(
            '--since', type=str, default=None,
            help='只处理发货时间（无发货时间则按下单时间）不早于该日期的订单，如 2026-08-01',
        )

    # ── 工具 ──────────────────────────────────────────────────────
    def _parse_since(self, raw):
        if not raw:
            return None
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d'):
            try:
                d = datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
            # 本项目 USE_TZ = False，库里存的是朴素本地时间，这里也用朴素 datetime
            # 比对（否则 make_aware/aware 与 naive 比较会抛异常）。
            return datetime.combine(d, datetime.min.time())
        raise CommandError(f'--since 日期格式无法识别：{raw}（请用 2026-08-01 这种写法）')

    @staticmethod
    def _booked_at(order):
        """这笔收入应该记在哪一天：优先发货时间，其次下单时间。"""
        return order.shipped_at or order.created_at or timezone.now()

    def _ledger_sum(self):
        """整本流水的累计净额（收入 - 支出）。"""
        agg = {}
        for ttype in (TxType.INCOME, TxType.EXPENSE):
            agg[ttype] = (Transaction.objects.filter(tx_type=ttype)
                          .aggregate(s=Sum('amount'))['s'] or 0)
        return agg[TxType.INCOME] - agg[TxType.EXPENSE]

    def _print_state(self, title):
        wallet = Wallet.get_solo()
        led = self._ledger_sum()
        diff = (wallet.balance or 0) - led
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(f'── {title} ──'))
        self.stdout.write(f'  钱包余额          ¥{wallet.balance or 0:>12,.2f}')
        self.stdout.write(f'  流水累计净额      ¥{led:>12,.2f}  （共 {Transaction.objects.count()} 笔）')
        if abs(diff) < 0.005:
            self.stdout.write(self.style.SUCCESS('  对账结果          ✓ 账实相符'))
        else:
            self.stdout.write(self.style.WARNING(
                f'  对账结果          ⚠ 差额 ¥{diff:,.2f}（余额 − 流水累计）'
            ))
        return wallet, led, diff

    # ── 主流程 ────────────────────────────────────────────────────
    def handle(self, *args, **options):
        apply = options['apply']
        reconcile = options['reconcile']
        since = self._parse_since(options.get('since'))

        self.stdout.write(self.style.MIGRATE_HEADING('【中渔天下】公司钱包历史流水补记'))
        self.stdout.write(f"  模式：{'★ 实际写入' if apply else '预览（不会改数据，加 --apply 生效）'}")
        if since:
            self.stdout.write(f'  范围：{since:%Y-%m-%d} 之后')

        self._print_state('补记前')

        # 1) 找出"钱已收到但没有订单收入流水"的订单
        #    已发货 / 已完成 → 货款已经收到，应当有收入流水
        #    已退货 → 收了又退了，净额 0，不补收入（避免虚增）
        #    已取消 / 未发货 / 退货申请中 → 钱还没到账，不补
        in_money_statuses = (OrderStatus.SHIPPED, OrderStatus.COMPLETED)
        shipped = Orders.objects.filter(status__in=in_money_statuses)
        if since:
            shipped = shipped.filter(
                Q(shipped_at__gte=since) | Q(shipped_at__isnull=True, created_at__gte=since)
            )
        already = Transaction.objects.filter(
            category=TxCategory.ORDER_INCOME, order__isnull=False,
        ).values_list('order_id', flat=True)
        missing = list(shipped.exclude(id__in=list(already)).order_by('shipped_at', 'id'))

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('── 待补记的已发货订单 ──'))
        if not missing:
            self.stdout.write('  无：所有已发货订单都已有收入流水，无需补记。')
        else:
            total = 0
            for o in missing:
                amt = o.total_price or 0
                total += amt
                self.stdout.write(
                    f"  #{o.id:<4} {o.order_no:<22} {self._booked_at(o):%Y-%m-%d}  ¥{amt:>10,.2f}"
                )
            self.stdout.write(self.style.WARNING(
                f'  共 {len(missing)} 笔，合计 ¥{total:,.2f}'
            ))

        if not apply:
            wallet0 = Wallet.get_solo()
            led0 = self._ledger_sum()
            hint = ' --reconcile' if abs((wallet0.balance or 0) - led0) >= 0.005 else ''
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                '预览结束，未改动任何数据。确认无误后执行：\n'
                f'  python manage.py backfill_wallet --apply{hint}'
            ))
            return

        # 2) 写入补记流水
        created = 0
        skipped = 0
        for o in missing:
            amt = o.total_price or 0
            if amt <= 0:
                skipped += 1
                self.stdout.write(self.style.WARNING(f'  #{o.id} 金额为 0，跳过'))
                continue
            with transaction.atomic():
                tx = _record_wallet_tx(
                    tx_type=TxType.INCOME,
                    category=TxCategory.ORDER_INCOME,
                    amount=amt,
                    order=o,
                    note=f'历史订单 {o.order_no} 发货补记',
                )
                if tx is None:
                    skipped += 1
                    continue
                # auto_now_add 会把 created_at 覆盖成"现在"，这里改回订单的发货时间，
                # 让账本时间线与业务时间一致
                booked = self._booked_at(o)
                Transaction.objects.filter(pk=tx.pk).update(created_at=booked)
            created += 1
            self.stdout.write(f'  ✔ #{o.id} {o.order_no}  +¥{amt:,.2f}  （记入 {booked:%Y-%m-%d}）')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'补记完成：新增 {created} 笔，跳过 {skipped} 笔。'
        ))

        # 3) 可选：补平账实差额
        wallet, led, diff = self._print_state('补记后')
        if reconcile and abs(diff) >= 0.005:
            # 差额的成因：历史上有过"余额被改动但没留流水"的操作（早期版本允许
            # 直接改余额、或流水被删过），所以余额 ≠ 流水累计。
            #
            # 这里**只向账本补一笔「账目核对调整」流水，不再动余额**：
            # 差额 = 余额 − 流水累计，把这笔差额本身记进流水后，两边正好对齐。
            # （若走 _record_wallet_tx 会同时改余额和流水，差额永远追不平。）
            direction = TxType.INCOME if diff > 0 else TxType.EXPENSE
            wallet = Wallet.get_solo()
            with transaction.atomic():
                Transaction.objects.create(
                    wallet=wallet,
                    tx_type=direction,
                    category=TxCategory.MANUAL,
                    amount=abs(diff),
                    note='账目核对调整（历史差额，自动补齐）',
                )
            self.stdout.write(self.style.WARNING(
                f'  已向账本补记一笔「账目核对调整」：'
                f'{"+" if diff > 0 else "-"}¥{abs(diff):,.2f}（余额不变，仅对齐账本）'
            ))
            self._print_state('调整后')
        elif abs(diff) >= 0.005:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'  仍有差额 ¥{diff:,.2f} 未处理。如需补平，加 --reconcile 再跑一次。'
            ))
