"""
agent/admin.py — 注册所有模型到 Django Admin 后台
产品 / 库存 / 订单 / 对话记录 / 用户留言 / 定制需求 / 知识库条目 / 钱包 / 流水

优化点（相较初版）：
- 库存：新增「库存状态」计算列（正常/预警/缺货 + 颜色），替换无意义的 alert_line 精确筛选
- 留言/定制：新增「✅ 标记为已读」批量动作 + 已读状态布尔图标
- 订单：新增「⬇️ 导出CSV」批量动作 + 发货状态/今日快捷筛选
- 产品：价格支持列表内直接编辑；新增「➕ 当前页内新增」弹窗
- 对话：增加时间层级导航与分页，便于按日期回溯
- 知识库（P2）：RAG 语料在线维护（title/category/keywords/content/is_active）
- 钱包：Wallet 单例 + Transaction 流水；订单已发货/退货自动记账
- 用户：自定义 UserAdmin（Unfold 主题，id 升序，删除等动作齐全）
"""
import csv
from decimal import Decimal
from django.db.models import F, Sum, Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.urls import path, reverse
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from unfold.admin import ModelAdmin as _UnfoldModelAdmin

from agent.models import (
    Products, Inventory, Orders, Conversations, ContactMessage, CustomRequest,
    KnowledgeChunk, Wallet, Transaction, TxType, TxCategory, OrderStatus,
)
from agent.services import transition_order, revert_shipped_to_pending, manual_wallet_adjust


# ══════════════════════════════════════════════════════════════
# 全局基类：注入「勾选即弹操作浮层」的批量操作弹窗（替代底部下拉+运行）
# ══════════════════════════════════════════════════════════════
class ZYModelAdmin(_UnfoldModelAdmin):
    """所有列表页统一基类：注入批量操作弹窗 JS；changelist 标题中文化。"""

    class Media:
        js = ('admin/batch_actions.js',)

    def changelist_view(self, request, extra_context=None):
        # Django 默认 title 是 "Select <verbose_name> to change" → zh-hans 翻译成
        # 生硬的 "选择 产品 来修改"。直接用 verbose_name_plural 当 title（更自然，且
        # 避免 "订单管理管理" 之类重复）。例如 "产品" / "订单管理" / "用户留言"。
        extra = extra_context or {}
        if not extra.get('title'):
            extra = dict(extra)
            extra['title'] = self.model._meta.verbose_name_plural
        return super().changelist_view(request, extra)


# ══════════════════════════════════════════════════════════════
# 通用批量动作
# ══════════════════════════════════════════════════════════════
@admin.action(description='✅ 标记为已读')
def mark_as_read(modeladmin, request, queryset):
    n = queryset.update(is_read=True)
    modeladmin.message_user(request, f'已将 {n} 条标记为已读。')


# ══════════════════════════════════════════════════════════════
# 产品管理
# ══════════════════════════════════════════════════════════════
@admin.register(Products)
class ProductsAdmin(ZYModelAdmin):
    list_display = ('id', 'sku', 'name', 'spec', 'target_fish', 'retail_price',
                    'wholesale_price', 'is_active_display')
    search_fields = ('sku', 'name', 'spec', 'description')    # P0：sku 进搜索
    list_filter = ('target_fish', 'is_active')               # P0：可按下架筛选
    list_editable = ('retail_price', 'wholesale_price')      # 列表内直接改价
    ordering = ('id',)                                       # id 升序（1,2,3...）
    list_per_page = 25

    @admin.display(description='在售', boolean=True)
    def is_active_display(self, obj):
        return obj.is_active


# ══════════════════════════════════════════════════════════════
# 库存管理 — 状态可视化
# ══════════════════════════════════════════════════════════════
class StockStateFilter(admin.SimpleListFilter):
    title = '库存状态'
    parameter_name = 'stock_state'

    def lookups(self, request, model_admin):
        return (('out', '缺货(≤0)'), ('low', '低于预警线'), ('ok', '正常'))

    def queryset(self, request, queryset):
        if self.value() == 'out':
            return queryset.filter(stock__isnull=False, stock__lte=0)
        if self.value() == 'low':
            return queryset.filter(stock__isnull=False, alert_line__isnull=False,
                                    stock__gt=0, stock__lte=F('alert_line'))
        if self.value() == 'ok':
            return queryset.filter(stock__isnull=False, alert_line__isnull=False,
                                    stock__gt=F('alert_line'))
        return queryset


@admin.register(Inventory)
class InventoryAdmin(ZYModelAdmin):
    list_display = ('id', 'product_name_display', 'product_id', 'stock', 'alert_line', 'stock_status')
    search_fields = ('product__name',)
    list_select_related = ('product',)
    list_filter = (StockStateFilter,)
    ordering = ('id',)                                       # id 升序

    @admin.display(description='产品名')
    def product_name_display(self, obj):
        return obj.product.name if obj.product else '—'

    @admin.display(description='库存状态', ordering='stock')
    def stock_status(self, obj):
        if obj.stock is None or obj.alert_line is None:
            return format_html('<span style="color:#738079;">—</span>')
        if obj.stock <= 0:
            return format_html('<span style="color:#B5481C;font-weight:600;">● 缺货</span>')
        if obj.stock <= obj.alert_line:
            return format_html('<span style="color:#E2703A;font-weight:600;">● 预警</span>')
        return format_html('<span style="color:#1F6B54;font-weight:600;">● 正常</span>')


# ══════════════════════════════════════════════════════════════
# 订单管理 — 导出 + 快捷筛选
# ══════════════════════════════════════════════════════════════
class ShipStateFilter(admin.SimpleListFilter):
    title = '发货状态'
    parameter_name = 'ship'

    def lookups(self, request, model_admin):
        return (('pending', '待发货'), ('shipped', '已发货'))

    def queryset(self, request, queryset):
        if self.value() == 'pending':
            return queryset.filter(status='未发货')
        if self.value() == 'shipped':
            return queryset.filter(status='已发货')
        return queryset


class TodayOrderFilter(admin.SimpleListFilter):
    title = '时间范围'
    parameter_name = 'range'

    def lookups(self, request, model_admin):
        return (('today', '今日'), ('week', '近 7 天'))

    def queryset(self, request, queryset):
        from datetime import datetime, timedelta
        now = datetime.now()
        if self.value() == 'today':
            return queryset.filter(created_at__date=now.date())
        if self.value() == 'week':
            return queryset.filter(created_at__gte=now - timedelta(days=7))
        return queryset


@admin.action(description='⬇️ 导出选中订单为CSV')
def export_orders_csv(modeladmin, request, queryset):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="orders_export.csv"'
    response.write('\ufeff'.encode('utf-8'))  # BOM，保证 Excel 正确识别中文
    writer = csv.writer(response)
    writer.writerow(['订单号', '客户', '电话', '产品', '数量', '总价', '状态', '物流公司', '运单号', '发货时间', '下单时间'])
    for o in queryset:
        writer.writerow([
            o.order_no, o.customer_name, o.phone, o.product_name,
            o.quantity, o.total_price, o.status,
            o.ship_company or '', o.tracking_no or '',
            o.shipped_at.strftime('%Y-%m-%d %H:%M') if o.shipped_at else '',
            o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
        ])
    return response


@admin.register(Orders)
class OrdersAdmin(ZYModelAdmin):
    list_display = ('order_no', 'status_badge', 'product_name', 'product_sku',
                    'customer_name', 'phone', 'quantity', 'total_price',
                    'ship_company', 'tracking_no', 'shipped_at', 'created_at')
    search_fields = ('order_no', 'customer_name', 'phone', 'product_name',
                     'product_sku', 'tracking_no')
    list_filter = (ShipStateFilter, TodayOrderFilter, 'status', 'created_at')
    # 注意：'status' 不在 list_editable，避免在列表里直接改状态绕过库存联动；
    # 必须通过 mark_as_shipped / cancel_selected / return_selected 三个 action 走 service 层。
    list_editable = ('ship_company', 'tracking_no')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('order_no', 'customer_name', 'phone', 'product', 'product_name',
                       'product_sku', 'unit_price', 'quantity', 'total_price',
                       'shipped_at', 'cancelled_at', 'cancel_reason', 'created_at', 'status')
    list_per_page = 25
    @admin.display(description='📝 修改状态', )
    def change_status_selected(self, request, queryset):
        """弹出一个确认页让用户选新状态（替代鸡肋的 list_editable 下拉 + 底部"保存"）。"""
        selected = list(queryset.values_list('pk', 'order_no', 'status'))
        if not selected:
            self.message_user(request, "请先勾选订单。", level='WARNING')
            return
        # 把选中的 id 通过 querystring 传给确认页
        ids = ','.join(str(pk) for pk, _, _ in selected)
        url = reverse('admin:agent_orders_change_status') + f'?ids={ids}'
        return HttpResponseRedirect(url)

    @admin.display(description='订单状态', ordering='status')
    def status_badge(self, obj):
        # 状态 → (背景色, 文字色, 标签)
        style_map = {
            '未发货': ('#F4B495', '#7A2E0E', '● 待发货'),
            '已发货': ('#DCEDE6', '#134435', '● 已发货'),
            '已取消': ('#E2E5E2', '#3A473F', '● 已取消'),
            '已退货': ('#F7D9CF', '#7A2E0E', '● 已退货'),
        }
        bg, fg, label = style_map.get(obj.status, ('#EEE', '#333', obj.status or '—'))
        return format_html(
            '<span style="display:inline-block;padding:3px 10px;border-radius:999px;'
            'background:{};color:{};font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, fg, label,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('change-status/', self.admin_site.admin_view(self.change_status_view),
                 name='agent_orders_change_status'),
        ]
        return custom + urls

    def change_status_view(self, request):
        """修改订单状态的确认页：选新状态 → 确认 → 批量应用（走 service，自动记账）。"""
        from django.shortcuts import render
        ids = request.GET.get('ids') or request.POST.get('ids', '')
        id_list = [int(x) for x in ids.split(',') if x.strip().isdigit()]
        queryset = Orders.objects.filter(pk__in=id_list)
        back_url = reverse('admin:agent_orders_changelist')

        if request.method == 'POST':
            new_status = request.POST.get('new_status', '')
            cancel_reason = request.POST.get('cancel_reason', '').strip()
            if new_status not in dict(OrderStatus.choices):
                return render(request, 'admin/orders_change_status.html', {
                    **self.admin_site.each_context(request),
                    'title': '修改订单状态',
                    'orders': queryset,
                    'status_choices': OrderStatus.choices,
                    'back_url': back_url,
                    'error': f'未知状态：{new_status}',
                })
            ok, fail, msgs = 0, 0, []
            for order in queryset:
                try:
                    if new_status == OrderStatus.SHIPPED:
                        # 发货：从订单当前字段读取物流信息（要求在列表里已编辑过）
                        if not (order.ship_company and order.tracking_no):
                            raise Exception("缺少物流公司/运单号，请先在订单列表里编辑。")
                        transition_order(order, to_status=OrderStatus.SHIPPED,
                                         ship_company=order.ship_company,
                                         tracking_no=order.tracking_no)
                    elif new_status == OrderStatus.CANCELLED:
                        transition_order(order, to_status=OrderStatus.CANCELLED,
                                         cancel_reason=cancel_reason or '管理员取消')
                    elif new_status == OrderStatus.RETURNED:
                        transition_order(order, to_status=OrderStatus.RETURNED,
                                         cancel_reason=cancel_reason or '管理员退货')
                    else:
                        # PENDING 直接置（无库存联动，但属于合法的状态写入）
                        order.status = new_status
                        order.save(update_fields=['status', 'updated_at'])
                    ok += 1
                except Exception as e:
                    fail += 1
                    msgs.append(f"  ✗ {order.order_no}: {e}")
            self.message_user(request, f"✓ 成功 {ok} 个 / 失败 {fail} 个")
            for m in msgs[:10]:
                self.message_user(request, m, level='WARNING')
            return HttpResponseRedirect(back_url)

        return render(request, 'admin/orders_change_status.html', {
            **self.admin_site.each_context(request),
            'title': '修改订单状态',
            'orders': queryset,
            'status_choices': OrderStatus.choices,
            'back_url': back_url,
        })

    actions = ['change_status_selected', 'mark_as_shipped', 'mark_as_unshipped',
               'cancel_selected', 'return_selected', export_orders_csv]

    def _bulk_transition(self, request, queryset, to_status, **fixed_kwargs):
        """批量状态机：逐单走 service.transition_order（每单独立事务）。"""
        from agent.services import transition_order, OrderTransitionError, InsufficientStockError
        ok, fail = 0, 0
        msgs = []
        for order in queryset:
            # 默认从订单当前字段读取 ship_company/tracking_no（admin 列表里已编辑过）
            kwargs = dict(fixed_kwargs)
            if to_status == '已发货':
                kwargs.setdefault('ship_company', order.ship_company or '')
                kwargs.setdefault('tracking_no', order.tracking_no or '')
            try:
                transition_order(order, to_status=to_status, **kwargs)
                ok += 1
            except (OrderTransitionError, InsufficientStockError) as e:
                fail += 1
                msgs.append(f"  ✗ {order.order_no}: {e.message}")
        if ok:
            self.message_user(request, f"✓ 成功 {ok} 个 / 失败 {fail} 个")
        if msgs:
            self.message_user(request, "\n".join(msgs[:10]), level='WARNING')

    @admin.action(description='📦 标记为已发货（走 service，自动扣库存）')
    def mark_as_shipped(self, request, queryset):
        # 物流信息校验：选中的订单若 ship_company/tracking_no 都为空则报错
        missing = [o.order_no for o in queryset
                   if not (o.ship_company and o.tracking_no)]
        if missing:
            self.message_user(
                request,
                f"以下订单缺少物流信息，请先在列表里编辑 ship_company/tracking_no 再发货："
                f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}",
                level='ERROR',
            )
            return
        self._bulk_transition(request, queryset, '已发货')

    @admin.action(description='⏳ 标记为未发货（高危：会绕过库存联动，仅紧急退回）')
    def mark_as_unshipped(self, request, queryset):
        from agent.services import transition_order, OrderTransitionError
        # 已发货的订单不能直接退回"未发货"（状态机不允许）—— 引导用「取消」或「退货」
        bad = [o.order_no for o in queryset if o.status == '已发货']
        if bad:
            self.message_user(
                request,
                f"已发货的订单 {bad[:5]} 不能直接改回未发货。"
                f"如需取消请用「❌ 取消订单」action（仅 PENDING 可取消），"
                f"已发货需走「↩️ 退货」action 库存回滚。",
                level='ERROR',
            )
            return
        # 未发货的可以直接改回（清空 shipped_at 等）
        for o in queryset:
            o.status = '未发货'
            o.shipped_at = None
            o.ship_company = ''
            o.tracking_no = ''
            o.save(update_fields=['status', 'shipped_at', 'ship_company', 'tracking_no'])
        self.message_user(request, f"已重置 {queryset.count()} 个订单为未发货。")

    @admin.action(description='❌ 取消订单（释放预占库存）')
    def cancel_selected(self, request, queryset):
        self._bulk_transition(request, queryset, '已取消', cancel_reason='管理员取消')

    @admin.action(description='↩️ 退货（库存回滚）')
    def return_selected(self, request, queryset):
        self._bulk_transition(request, queryset, '已退货', cancel_reason='管理员退货')


# ══════════════════════════════════════════════════════════════
# 对话记录
# ══════════════════════════════════════════════════════════════
@admin.register(Conversations)
class ConversationsAdmin(ZYModelAdmin):
    list_display = ('id', 'session_id', 'role', 'short_content', 'created_at')
    search_fields = ('session_id', 'content')
    list_filter = ('role',)
    date_hierarchy = 'created_at'
    list_per_page = 50
    ordering = ('id',)                                       # id 升序

    @admin.display(description='内容预览')
    def short_content(self, obj):
        return (obj.content[:80] + '…') if obj.content and len(obj.content) > 80 else obj.content


# ══════════════════════════════════════════════════════════════
# 用户留言 — 已读管理
# ══════════════════════════════════════════════════════════════
@admin.register(ContactMessage)
class ContactMessageAdmin(ZYModelAdmin):
    list_display = ('id', 'name', 'phone', 'short_message', 'read_status', 'created_at')
    search_fields = ('name', 'phone')
    list_filter = ('is_read',)
    list_per_page = 25
    ordering = ('id',)                                       # id 升序
    actions = [mark_as_read]

    @admin.display(description='内容预览')
    def short_message(self, obj):
        return (obj.message[:50] + '…') if obj.message and len(obj.message) > 50 else obj.message

    @admin.display(description='已读', boolean=True)
    def read_status(self, obj):
        return obj.is_read


# ══════════════════════════════════════════════════════════════
# 定制需求 — 已读管理
# ══════════════════════════════════════════════════════════════
@admin.register(CustomRequest)
class CustomRequestAdmin(ZYModelAdmin):
    list_display = ('id', 'name', 'phone', 'company', 'quantity', 'read_status', 'created_at')
    search_fields = ('name', 'phone', 'company')
    list_filter = ('is_read',)
    list_per_page = 25
    ordering = ('id',)                                       # id 升序
    actions = [mark_as_read]

    @admin.display(description='已读', boolean=True)
    def read_status(self, obj):
        return obj.is_read


# ══════════════════════════════════════════════════════════════
# 知识库管理（P2：RAG 知识库数据化，后台在线维护语料）
# ══════════════════════════════════════════════════════════════
@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(ZYModelAdmin):
    list_display = ('id', 'title', 'category', 'is_active', 'updated_at', 'created_at')
    search_fields = ('title', 'keywords', 'content')
    list_filter = ('category', 'is_active')
    list_per_page = 25
    ordering = ('-updated_at',)

    # 编辑时提示：keywords 用逗号分隔；改动后需重启进程重建 RAG 索引（当前为内存缓存）
    fieldsets = (
        (None, {'fields': ('title', 'category', 'keywords', 'content', 'is_active')}),
        ('提示', {'fields': (), 'description':
            'keywords 多个词用英文逗号分隔（如：退换,退货,退款）。'
            '保存后需重启 Django 进程才会重建 RAG 索引（当前为内存缓存）。'}),
    )


# ══════════════════════════════════════════════════════════════
# 钱包 / 流水（让"钱从哪里来到哪里去"在后台可追溯）
# ══════════════════════════════════════════════════════════════
@admin.register(Wallet)
class WalletAdmin(ZYModelAdmin):
    """公司钱包：始终只显示/编辑 id=1 那一条（singleton）。"""
    list_display = ('id', 'balance_display', 'income_today_display', 'income_month_display',
                    'income_total_display', 'expense_total_display', 'updated_at')
    fieldsets = (
        (None, {'fields': ('balance',)}),
        ('📊 财务统计（今日/本月/分类汇总）', {'fields': ('stats_panel',)}),
        ('📈 近 30 日收入趋势', {'fields': ('income_trend_panel',)}),
        ('🕐 最近 10 笔流水', {'fields': ('recent_txs_panel',)}),
        ('信息', {'fields': ('updated_at',)}),
    )
    readonly_fields = ('updated_at', 'stats_panel', 'income_trend_panel', 'recent_txs_panel')

    class Media:
        js = ('admin/wallet_actions.js',)   # 详情页「充值 / 扣款」按钮

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('adjust/', self.admin_site.admin_view(self.adjust_view),
                 name='agent_wallet_adjust'),
        ]
        return custom + urls

    def adjust_view(self, request):
        """手动充值/扣款（POST JSON：{direction: income|expense, amount, note}）。"""
        if request.method != 'POST':
            return JsonResponse({'success': False, 'message': '仅支持 POST'}, status=405)
        import json
        try:
            data = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'success': False, 'message': '请求体必须是 JSON'}, status=400)
        direction = data.get('direction', 'income')
        if direction not in ('income', 'expense'):
            return JsonResponse({'success': False, 'message': '方向必须是 income 或 expense'}, status=400)
        try:
            amount = Decimal(str(data.get('amount') or 0))
        except Exception:
            return JsonResponse({'success': False, 'message': '金额必须是数字'}, status=400)
        if amount <= 0:
            return JsonResponse({'success': False, 'message': '金额必须大于 0'}, status=400)
        note = (data.get('note') or '').strip()
        tx = manual_wallet_adjust(amount, direction=direction, note=note, operator=request.user)
        if tx is None:
            return JsonResponse({'success': False, 'message': '记账失败（金额无效）'}, status=400)
        label = '充值' if direction == 'income' else '扣款'
        wallet = Wallet.get_solo()
        return JsonResponse({
            'success': True,
            'message': f'已{label} ¥{amount:,.2f}，当前余额 ¥{wallet.balance:,.2f}',
            'balance': str(wallet.balance),
        })

    def has_add_permission(self, request):
        return not Wallet.objects.exists()  # 首次未创建时允许新建一条

    def get_queryset(self, request):
        return Wallet.objects.filter(pk=1)

    @admin.display(description='当前余额')
    def balance_display(self, obj):
        color = '#1F6B54' if obj.balance >= 0 else '#B5481C'
        val = f"{obj.balance or 0:,.2f}"
        return format_html('<b style="color:{};font-size:15px;">¥{}</b>', color, val)

    @admin.display(description='累计收入')
    def income_total_display(self, obj):
        s = Transaction.objects.filter(wallet=obj, tx_type=TxType.INCOME).aggregate(t=Sum('amount'))['t'] or 0
        return format_html('<span style="color:#1F6B54;">¥{}</span>', f"{s:,.2f}")

    @admin.display(description='今日收入')
    def income_today_display(self, obj):
        from datetime import date
        s = Transaction.objects.filter(
            wallet=obj, tx_type=TxType.INCOME, created_at__date=date.today()
        ).aggregate(t=Sum('amount'))['t'] or 0
        return format_html('<span style="color:#1F6B54;font-weight:600;">+¥{}</span>', f"{s:,.2f}")

    @admin.display(description='本月收入')
    def income_month_display(self, obj):
        from datetime import date
        s = Transaction.objects.filter(
            wallet=obj, tx_type=TxType.INCOME,
            created_at__date__gte=date.today().replace(day=1),
        ).aggregate(t=Sum('amount'))['t'] or 0
        return format_html('<span style="color:#2C7C8C;font-weight:600;">+¥{}</span>', f"{s:,.2f}")

    @admin.display(description='累计支出')
    def expense_total_display(self, obj):
        s = Transaction.objects.filter(wallet=obj, tx_type=TxType.EXPENSE).aggregate(t=Sum('amount'))['t'] or 0
        return format_html('<span style="color:#B5481C;">¥{}</span>', f"{s:,.2f}")

    @admin.display(description='财务统计')
    def stats_panel(self, obj):
        from datetime import date, timedelta
        today = date.today()
        month_start = today.replace(day=1)
        _in = Transaction.objects.filter(wallet=obj, tx_type=TxType.INCOME)
        _out = Transaction.objects.filter(wallet=obj, tx_type=TxType.EXPENSE)
        day_in = _in.filter(created_at__date=today).aggregate(t=Sum('amount'))['t'] or 0
        yesterday_in = _in.filter(
            created_at__date=today - timedelta(days=1)
        ).aggregate(t=Sum('amount'))['t'] or 0
        month_in = _in.filter(created_at__date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0
        month_out = _out.filter(created_at__date__gte=month_start).aggregate(t=Sum('amount'))['t'] or 0
        # 分类汇总
        cat_rows_html = ''
        for cat_value, cat_label in TxCategory.choices:
            for tx_type in (TxType.INCOME, TxType.EXPENSE):
                s = Transaction.objects.filter(
                    wallet=obj, tx_type=tx_type, category=cat_value
                ).aggregate(t=Sum('amount'))['t'] or 0
                if s > 0:
                    color = '#1F6B54' if tx_type == TxType.INCOME else '#B5481C'
                    sign = '+' if tx_type == TxType.INCOME else '-'
                    type_label = '收入' if tx_type == TxType.INCOME else '支出'
                    cat_rows_html += (
                        f'<tr><td style="padding:6px 10px;">{cat_label}</td>'
                        f'<td style="padding:6px 10px;color:{color};">{type_label}</td>'
                        f'<td style="padding:6px 10px;text-align:right;color:{color};font-weight:600;">{sign}¥{s:,.2f}</td></tr>'
                    )
        if not cat_rows_html:
            cat_rows_html = '<tr><td colspan="3" style="padding:8px;color:#999;text-align:center;">暂无流水</td></tr>'
        net = (month_in or 0) - (month_out or 0)
        net_color = '#1F6B54' if net >= 0 else '#B5481C'
        bal = float(obj.balance or 0)
        bal_color = '#1F6B54' if bal >= 0 else '#B5481C'
        return format_html(
            '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:6px 0 18px;">'
            # 余额
            '<div style="background:#F6F1E7;padding:12px 14px;border-radius:10px;border-left:4px solid {bc};">'
            '<div style="color:#738079;font-size:12px;">当前余额</div>'
            '<div style="color:{bc};font-size:22px;font-weight:700;line-height:1.4;">¥{bal}</div></div>'
            # 今日收入
            '<div style="background:#EAF6F1;padding:12px 14px;border-radius:10px;border-left:4px solid #1F6B54;">'
            '<div style="color:#134435;font-size:12px;">今日收入</div>'
            '<div style="color:#1F6B54;font-size:22px;font-weight:700;line-height:1.4;">+¥{ti}</div></div>'
            # 昨日收入
            '<div style="background:#E2F3F7;padding:12px 14px;border-radius:10px;border-left:4px solid #2C7C8C;">'
            '<div style="color:#134435;font-size:12px;">昨日收入</div>'
            '<div style="color:#2C7C8C;font-size:22px;font-weight:700;line-height:1.4;">+¥{yi}</div></div>'
            # 本月收入
            '<div style="background:#FFF3E5;padding:12px 14px;border-radius:10px;border-left:4px solid #E2703A;">'
            '<div style="color:#7A2E0E;font-size:12px;">本月收入</div>'
            '<div style="color:#E2703A;font-size:22px;font-weight:700;line-height:1.4;">+¥{mi}</div></div>'
            # 本月支出
            '<div style="background:#F7D9CF;padding:12px 14px;border-radius:10px;border-left:4px solid #B5481C;">'
            '<div style="color:#7A2E0E;font-size:12px;">本月支出</div>'
            '<div style="color:#B5481C;font-size:22px;font-weight:700;line-height:1.4;">-¥{mo}</div></div>'
            # 本月净流
            '<div style="background:#E2F3F7;padding:12px 14px;border-radius:10px;border-left:4px solid {nc};">'
            '<div style="color:#134435;font-size:12px;">本月净流</div>'
            '<div style="color:{nc};font-size:22px;font-weight:700;line-height:1.4;">¥{net}</div></div>'
            '</div>'
            '<div style="font-size:13px;font-weight:600;color:#15211D;margin:6px 0 4px;">📂 分类汇总</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e8e4d8;border-radius:8px;overflow:hidden;">'
            '<thead><tr style="background:#F3EFE4;color:#15211D;">'
            '<th style="text-align:left;padding:8px 10px;">分类</th>'
            '<th style="text-align:left;padding:8px 10px;">类型</th>'
            '<th style="text-align:right;padding:8px 10px;">累计金额</th>'
            '</tr></thead>'
            '<tbody>{cat_rows}</tbody>'
            '</table>',
            bc=bal_color, bal=f"{bal:,.2f}", ti=f"{day_in or 0:,.2f}", yi=f"{yesterday_in or 0:,.2f}",
            mi=f"{month_in or 0:,.2f}", mo=f"{month_out or 0:,.2f}",
            net=f"{net:,.2f}", nc=net_color, cat_rows=mark_safe(cat_rows_html),
        )

    @admin.display(description='近 30 日收入趋势')
    def income_trend_panel(self, obj):
        """近 30 日收入柱状趋势（纯 CSS，无需图表库）+ 全部流水入口。"""
        from datetime import date, timedelta
        from django.db.models.functions import TruncDate
        days = 30
        today = date.today()
        start = today - timedelta(days=days - 1)
        rows = list(
            Transaction.objects
            .filter(wallet=obj, tx_type=TxType.INCOME, created_at__date__gte=start)
            .annotate(d=TruncDate('created_at'))
            .values('d').annotate(s=Sum('amount'))
        )
        by_day = {r['d']: float(r['s'] or 0) for r in rows}
        series = [(start + timedelta(days=i), by_day.get(start + timedelta(days=i), 0.0)) for i in range(days)]
        max_v = max((v for _, v in series), default=0) or 1.0
        bars = ''.join(
            f'<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;" '
            f'title="{d.strftime("%m-%d")} 收入 +¥{v:,.2f}">'
            f'<div style="height:{max(v / max_v * 100, 1.5):.1f}%;border-radius:3px 3px 0 0;'
            f'background:{"linear-gradient(180deg,#2C8466,#1F6B54)" if v > 0 else "#ECE4D4"};"></div></div>'
            for d, v in series
        )
        total_30 = sum(v for _, v in series)
        link = reverse('admin:agent_transaction_changelist')
        return format_html(
            '<div style="margin:6px 0 18px;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
            '<div style="font-size:12.5px;color:#738079;">'
            '近 30 日收入合计 <b style="color:#1F6B54;font-size:15px;">+¥{total}</b>'
            ' · 单日峰值 <b style="color:#2C7C8C;">¥{peak}</b></div>'
            '<a href="{link}" style="font-size:12.5px;color:#2C7C8C;text-decoration:none;font-weight:600;">'
            '查看全部收支流水 →</a></div>'
            '<div style="display:flex;align-items:flex-end;gap:2px;height:110px;'
            'background:#FBF8F1;border:1px solid #e8e4d8;border-radius:10px;padding:10px;">{bars}</div>'
            '<div style="display:flex;justify-content:space-between;font-size:10.5px;color:#9AA89F;margin-top:4px;">'
            '<span>{d0}</span><span>{dm}</span><span>{d1}</span></div></div>',
            total=f"{total_30:,.2f}", peak=f"{max_v:,.2f}", link=link,
            bars=mark_safe(bars), d0=start.strftime('%m-%d'),
            dm=(start + timedelta(days=days // 2)).strftime('%m-%d'), d1=today.strftime('%m-%d'),
        )

    @admin.display(description='最近 10 笔流水')
    def recent_txs_panel(self, obj):
        txs = list(Transaction.objects.filter(wallet=obj).order_by('-created_at')[:10])
        if not txs:
            return format_html('<p style="color:#999;padding:8px;">暂无流水（订单已发货/退货会自动写入）</p>')
        rows = []
        for t in txs:
            is_in = t.tx_type == TxType.INCOME
            sign = '+' if is_in else '-'
            color = '#1F6B54' if is_in else '#B5481C'
            order_cell = '—'
            if t.order:
                order_cell = format_html(
                    '<a href="{}" style="color:#2C7C8C;">{}</a>',
                    reverse('admin:agent_orders_change', args=[t.order.pk]),
                    t.order.order_no,
                )
            note = (t.note or '').replace('|', '｜')
            op_name = t.operator.username if t.operator else '—'
            rows.append(format_html(
                '<tr>'
                '<td style="padding:7px 10px;color:#738079;white-space:nowrap;">{}</td>'
                '<td style="padding:7px 10px;white-space:nowrap;"><span style="color:{};font-weight:700;">{}¥{:,.2f}</span></td>'
                '<td style="padding:7px 10px;">{}</td>'
                '<td style="padding:7px 10px;color:#15211D;">{}</td>'
                '<td style="padding:7px 10px;">{}</td>'
                '<td style="padding:7px 10px;color:#738079;">{}</td>'
                '</tr>',
                t.created_at.strftime('%Y-%m-%d %H:%M'),
                color, sign, float(t.amount),
                t.category,
                note or '—',
                order_cell,
                op_name,
            ))
        return format_html(
            '<table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e8e4d8;border-radius:8px;overflow:hidden;">'
            '<thead><tr style="background:#F3EFE4;color:#15211D;">'
            '<th style="text-align:left;padding:8px 10px;">时间</th>'
            '<th style="text-align:left;padding:8px 10px;">金额</th>'
            '<th style="text-align:left;padding:8px 10px;">分类</th>'
            '<th style="text-align:left;padding:8px 10px;">备注</th>'
            '<th style="text-align:left;padding:8px 10px;">关联订单</th>'
            '<th style="text-align:left;padding:8px 10px;">操作人</th>'
            '</tr></thead>'
            '<tbody>{}</tbody>'
            '</table>',
            mark_safe(''.join(str(r) for r in rows)),
        )


@admin.register(Transaction)
class TransactionAdmin(ZYModelAdmin):
    """收支流水：只读为主（让账目可追溯，不可随意改），自动写入不可删。"""
    list_display = ('created_at', 'tx_type_badge', 'category', 'amount_display',
                    'order_link', 'note', 'operator')
    list_filter = ('tx_type', 'category', 'created_at')
    search_fields = ('order__order_no', 'note', 'amount')
    date_hierarchy = 'created_at'
    list_per_page = 30
    readonly_fields = ('wallet', 'tx_type', 'category', 'amount', 'order', 'note', 'operator', 'created_at')

    def has_add_permission(self, request):
        return False  # 流水只能由系统/服务写入，避免人为乱账

    def has_delete_permission(self, request, obj=None):
        return False  # 账目不可删

    @admin.display(description='类型')
    def tx_type_badge(self, obj):
        if obj.tx_type == TxType.INCOME:
            return format_html('<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
                               'background:#DCEDE6;color:#134435;font-weight:600;">收入</span>')
        return format_html('<span style="display:inline-block;padding:2px 10px;border-radius:999px;'
                           'background:#F4D9CF;color:#7A2E0E;font-weight:600;">支出</span>')

    @admin.display(description='金额', ordering='amount')
    def amount_display(self, obj):
        sign = '+' if obj.tx_type == TxType.INCOME else '-'
        color = '#1F6B54' if obj.tx_type == TxType.INCOME else '#B5481C'
        return format_html('<b style="color:{};">{}¥{}</b>', color, sign, f"{obj.amount:,.2f}")

    @admin.display(description='关联订单')
    def order_link(self, obj):
        if not obj.order:
            return '—'
        url = reverse('admin:agent_orders_change', args=[obj.order.pk])
        return format_html('<a href="{}">{}</a>', url, obj.order.order_no)


# ══════════════════════════════════════════════════════════════
# 自定义 User 管理（id 升序、补全删除等动作、Unfold 主题）
# ══════════════════════════════════════════════════════════════
@admin.action(description='🔒 停用选中用户')
def deactivate_users(modeladmin, request, queryset):
    n = queryset.update(is_active=False)
    modeladmin.message_user(request, f'已停用 {n} 个用户。')


@admin.action(description='✅ 启用选中用户')
def activate_users(modeladmin, request, queryset):
    n = queryset.update(is_active=True)
    modeladmin.message_user(request, f'已启用 {n} 个用户。')


User = get_user_model()
# 注销 Django 默认 UserAdmin，改用我们自己的
admin.site.unregister(User)
# 注销 Django 默认 GroupAdmin（它的 changelist 标题还是"选择 组 来修改"），换成继承 ZYModelAdmin 的版本
try:
    from django.contrib.auth.models import Group
    from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
    admin.site.unregister(Group)
    @admin.register(Group)
    class ZYGroupAdmin(ZYModelAdmin, DjangoGroupAdmin):
        list_display = ('name',)
        search_fields = ('name',)
        ordering = ('id',)
except Exception:
    pass

# 隐藏 token_blacklist（JWT 内部模块，业务后台不需要；模型仍可用，只是不在 admin 露出来）
try:
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
    admin.site.unregister(OutstandingToken)
    admin.site.unregister(BlacklistedToken)
except Exception:
    pass


@admin.register(User)
class ZYUserAdmin(ZYModelAdmin, DjangoUserAdmin):
    """中渔天下用户管理：id 升序、显式动作（含删除）、更紧凑的列表展示。"""
    list_display = ('id', 'username', 'email', 'is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login')
    list_filter = ('is_staff', 'is_superuser', 'is_active', 'groups')
    search_fields = ('username', 'email', 'first_name', 'last_name')
    ordering = ('id',)  # 关键：id 升序
    list_per_page = 25
    actions = ['delete_selected', deactivate_users, activate_users]

    # 详情页：用 DjangoUserAdmin 的 fieldsets（保持原样）
    fieldsets = DjangoUserAdmin.fieldsets
    add_fieldsets = DjangoUserAdmin.add_fieldsets
