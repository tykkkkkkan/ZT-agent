"""
取消「超时未支付」的订单（任务书五·1）

背景
----
下单即预占库存（`reserve_inventory`）保证了不会超卖，但代价是：**只要用户下了单
不付款，这部分库存就被永久占住**。没有超时机制的话，几个恶意/误操作订单就能把
某个热销商品的可用库存吃干，前台显示"缺货"，而其实一包都没卖出去 ——
这和历史上踩过的「预占泄漏」是同一类问题的另一种成因。

策略
----
· 只处理 `payment_status = '未支付'` 的订单。**历史订单（空值）绝不牵连**，
  否则第一次跑批就会把老订单批量取消。
· 只处理 `status = '未发货'` 的订单，且要求 `created_at` 早于 cutoff。
· 走 `services.transition_order(CANCELLED)` —— 顺带释放预占库存，
  绝不直接改库（直接改 status 会把 reserved_stock 留在原地）。

安全设计
--------
· 默认**预演**，只有 `--apply` 才写库；
· 单笔失败不影响整批（记入 failed 列表）；
· 扫描后被用户抢先付款/取消的订单记入 skipped（不是错误）。

用法
----
    python manage.py cancel_unpaid_orders                  # 预演（默认 30 分钟）
    python manage.py cancel_unpaid_orders --minutes 15     # 换一个超时阈值预演
    python manage.py cancel_unpaid_orders --apply          # 真正执行

定时执行
--------
本项目为「本地容器化部署」，不依赖云服务器，因此不内置定时器。
如需周期性清理，二选一：
    · Linux/macOS：crontab  `/path/.venv/bin/python manage.py cancel_unpaid_orders --apply`
    · Windows    ：任务计划程序，每 10 分钟跑一次同样命令
"""
from django.core.management.base import BaseCommand

from agent.services import cancel_unpaid_orders

DEFAULT_TIMEOUT_MINUTES = 30


class Command(BaseCommand):
    help = ('取消超过指定时长仍未支付的订单，并释放其预占库存（默认预演 30 分钟）')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='真正写库（默认只预演，不改任何数据）')
        parser.add_argument('--minutes', type=int, default=DEFAULT_TIMEOUT_MINUTES,
                            help=f'超时阈值，单位分钟（默认 {DEFAULT_TIMEOUT_MINUTES}）')

    def handle(self, *args, **options):
        apply = options['apply']
        minutes = options['minutes']
        if minutes <= 0:
            self.stderr.write(self.style.ERROR('--minutes 必须是正整数'))
            return

        result = cancel_unpaid_orders(minutes, dry_run=not apply)
        head = '预演' if not apply else '执行'
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'[{head}] 扫描「未支付且下单超过 {minutes} 分钟」的订单：'
            f'命中 {result["scanned"]} 笔'
        ))

        for no in result['cancelled']:
            mark = '待取消' if not apply else '已取消'
            self.stdout.write(f'  · {mark} {no}（预占库存已释放）')
        for no in result['skipped']:
            self.stdout.write(self.style.WARNING(f'  · 跳过 {no}（状态已变化，用户可能已付款/自行取消）'))
        for no, err in result['failed']:
            self.stderr.write(self.style.ERROR(f'  ✗ {no}：{err}'))

        if not result['scanned']:
            self.stdout.write('  没有需要处理的订单。')
        if not apply and result['scanned']:
            self.stdout.write(self.style.WARNING(
                '以上为预演结果，未改动任何数据；确认无误后加 --apply 执行。'
            ))
