"""
agent/admin.py — 注册所有模型到 Django Admin 后台
产品 / 库存 / 订单 / 对话记录 / 用户留言 / 定制需求 / 知识库条目 / 钱包 / 流水

操作方式优化（本轮重点：把「多步点击」压成「一两步」）：
- 订单：列表新增「快速操作」列（发货/取消/退货/撤回），一步进「订单操作页」
  直接填物流公司 + 逐单运单号即可发货——不再需要「先在列表里编辑物流 → 保存
  → 勾选 → 选动作 → 选状态 → 确认」六步；发货页单笔/批量共用。
- 库存：stock/alert_line 支持列表内直接改（批量盘点）；每行「± 调整」弹窗按
  增量即改即生效；补充「预占 / 可用库存」列。
- 留言/定制：行内「标记已读/未读」一键切换；产品：行内「上架/下架」一键切换。
- 后台首页：覆盖 admin/index.html，去掉 Django 默认的「最近动作」面板。
"""
import csv
import json
import threading
from decimal import Decimal
from django.db.models import F, Sum, Q, Count
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
    KnowledgeChunk, Wallet, Transaction, TxType, TxCategory, OrderStatus, ReturnStatus,
)
from agent.services import (
    transition_order, revert_shipped_to_pending, manual_wallet_adjust,
    adjust_inventory_stock, reject_return_request, OrderTransitionError,
)
from agent.knowledge_data import invalidate_rag_cache


def _parse_json_body(request):
    """解析 POST 的 JSON 请求体；非法/unexpected 时返回 None（调用方回 400）。"""
    try:
        data = json.loads(request.body or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _post_json_id(request):
    """从 JSON 体里取 id（后台行内快捷操作统一约定字段名 `id`）。"""
    data = _parse_json_body(request)
    if data is None:
        return None, JsonResponse({'success': False, 'message': '请求体必须是 JSON'}, status=400)
    try:
        return int(data.get('id')), None
    except (TypeError, ValueError):
        return None, JsonResponse({'success': False, 'message': '缺少合法的 id'}, status=400)


# ══════════════════════════════════════════════════════════════
# 全局基类：注入「勾选即弹操作浮层」的批量操作弹窗（替代底部下拉+运行）
# ══════════════════════════════════════════════════════════════
class ZYModelAdmin(_UnfoldModelAdmin):
    """所有列表页统一基类：注入批量操作弹窗 + 行内快捷操作 JS/CSS；changelist 标题中文化。"""

    class Media:
        css = {'all': ('admin/zy-admin-ops.css',)}
        js = ('admin/batch_actions.js', 'admin/row_ops.js')

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
# 行内「一键开关」列表基类
# ══════════════════════════════════════════════════════════════
class ZYQuickToggleAdmin(ZYModelAdmin):
    """给列表加一列「快速操作」：一键翻转某个布尔字段。

    适合「留言/定制的已读」「产品的上下架」这类高频小操作——
    原来要勾选 → 底部选动作 → 点运行（3 步），现在点一下即改。
    子类只需声明 toggle_field / toggle_url_name / 两个按钮文案。
    """

    toggle_field = ''            # 模型上的布尔字段名，如 'is_read' / 'is_active'
    toggle_url_name = ''         # 该 admin 下的 URL name（须全局唯一）
    toggle_on_label = '开启'      # 当前为 False 时按钮显示（点了变 True）
    toggle_off_label = '关闭'     # 当前为 True 时按钮显示（点了变 False）

    def get_urls(self):
        urls = super().get_urls()
        if not self.toggle_url_name:
            return urls
        custom = [
            path('quick-toggle/', self.admin_site.admin_view(self.quick_toggle_view),
                 name=self.toggle_url_name),
        ]
        return custom + urls

    def quick_toggle_view(self, request):
        """POST JSON {id} → 翻转布尔字段；返回新值供前端提示。"""
        if request.method != 'POST':
            return JsonResponse({'success': False, 'message': '仅支持 POST'}, status=405)
        pk, err = _post_json_id(request)
        if err is not None:
            return err
        obj = self.model.objects.filter(pk=pk).first()
        if obj is None:
            return JsonResponse({'success': False, 'message': '记录不存在'}, status=404)
        new_val = not bool(getattr(obj, self.toggle_field))
        setattr(obj, self.toggle_field, new_val)
        obj.save(update_fields=[self.toggle_field])
        return JsonResponse({
            'success': True,
            'value': new_val,
            'message': f'已{self.toggle_on_label if new_val else self.toggle_off_label}。',
        })

    @admin.display(description='快速操作')
    def quick_ops(self, obj):
        is_on = bool(getattr(obj, self.toggle_field))
        label = f'↩ {self.toggle_off_label}' if is_on else f'✓ {self.toggle_on_label}'
        tone = 'mute' if is_on else 'go'
        return format_html(
            '<button type="button" class="zy-op-btn zy-op-{}"'
            ' data-zy-post="1" data-zy-url="{}" data-zy-id="{}">{}</button>',
            tone, reverse(f'admin:{self.toggle_url_name}'), obj.pk, label,
        )


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
class ProductsAdmin(ZYQuickToggleAdmin):
    list_display = ('id', 'sku', 'name', 'spec', 'target_fish', 'retail_price',
                    'wholesale_price', 'is_active_display', 'quick_ops')
    search_fields = ('sku', 'name', 'spec', 'description')    # P0：sku 进搜索
    list_filter = ('target_fish', 'is_active')               # P0：可按下架筛选
    list_editable = ('retail_price', 'wholesale_price')      # 列表内直接改价
    ordering = ('id',)                                       # id 升序（1,2,3...）
    list_per_page = 25

    # 行内「上架 / 下架」一键切换
    toggle_field = 'is_active'
    toggle_url_name = 'agent_products_quick_toggle'
    toggle_on_label = '上架'
    toggle_off_label = '下架'

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
    """库存管理。

    操作方式优化（原：每条都要点进详情页改数字再保存，太麻烦）：
      1) `stock` / `alert_line` 放进 list_editable → 列表里直接改数字，
         改多行后点一次「保存」即可（批量盘点/一次补多个 SKU 的主路径）；
      2) 每行「± 调整」按钮 → 弹窗输增量（+100 / -20）即改即生效，
         适合日常零散补货，不用进详情页；
      3) 补充「预占库存 / 可用库存」列，避免只看 stock 就误判可卖数量。
    """
    list_display = ('id', 'product_name_display', 'stock', 'reserved_display',
                    'available_display', 'alert_line', 'stock_status', 'quick_ops')
    search_fields = ('product__name',)
    list_select_related = ('product',)
    list_filter = (StockStateFilter,)
    list_editable = ('stock', 'alert_line')     # 列表内直接改（批量盘点主路径）
    ordering = ('id',)                                       # id 升序
    list_per_page = 25

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('quick-adjust/', self.admin_site.admin_view(self.quick_adjust_view),
                 name='agent_inventory_quick_adjust'),
        ]
        return custom + urls

    def quick_adjust_view(self, request):
        """POST JSON：{id, delta, note} → 按增量调整库存（正数入库 / 负数出库）。"""
        if request.method != 'POST':
            return JsonResponse({'success': False, 'message': '仅支持 POST'}, status=405)
        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'success': False, 'message': '请求体必须是 JSON'}, status=400)
        sku_id = data.get('id')
        try:
            delta = int(data.get('delta') or 0)
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'message': '增减数量必须是整数'}, status=400)
        if delta == 0:
            return JsonResponse({'success': False, 'message': '增减数量不能为 0'}, status=400)
        inv = Inventory.objects.filter(pk=sku_id).select_related('product').first()
        if inv is None:
            return JsonResponse({'success': False, 'message': '库存记录不存在'}, status=404)
        try:
            inv = adjust_inventory_stock(inv, delta, note=(data.get('note') or '').strip())
        except OrderTransitionError as e:
            return JsonResponse({'success': False, 'message': e.message}, status=400)
        return JsonResponse({
            'success': True,
            'message': f"「{inv.product.name}」库存已{'增加' if delta > 0 else '减少'} "
                       f"{abs(delta)}，现为 {inv.stock} 包。",
            'stock': inv.stock,
        })

    @admin.display(description='产品名')
    def product_name_display(self, obj):
        return obj.product.name if obj.product else '—'

    @admin.display(description='预占', ordering='reserved_stock')
    def reserved_display(self, obj):
        n = obj.reserved_stock or 0
        if n == 0:
            return format_html('<span style="color:#738079;">0</span>')
        return format_html('<span style="color:#B5481C;font-weight:600;">{}</span>', n)

    @admin.display(description='可用(=库存-预占)')
    def available_display(self, obj):
        avail = obj.available_stock
        color = '#B5481C' if avail <= 0 else '#1F6B54'
        return format_html('<b style="color:{};">{}</b>', color, avail)

    @admin.display(description='库存状态', ordering='stock')
    def stock_status(self, obj):
        if obj.stock is None or obj.alert_line is None:
            return format_html('<span style="color:#738079;">—</span>')
        if obj.stock <= 0:
            return format_html('<span style="color:#B5481C;font-weight:600;">● 缺货</span>')
        if obj.stock <= obj.alert_line:
            return format_html('<span style="color:#E2703A;font-weight:600;">● 预警</span>')
        return format_html('<span style="color:#1F6B54;font-weight:600;">● 正常</span>')

    @admin.display(description='快速操作')
    def quick_ops(self, obj):
        """行内「± 调整」：弹窗输增量，即改即生效，不用进详情页。"""
        name = obj.product.name if obj.product else f'#{obj.product_id}'
        return format_html(
            '<button type="button" class="zy-op-btn zy-op-go"'
            ' data-zy-stock="1"'
            ' data-zy-url="{}"'
            ' data-zy-id="{}"'
            ' data-zy-name="{}"'
            ' data-zy-now="{}">± 调整</button>',
            reverse('admin:agent_inventory_quick_adjust'),
            obj.pk, name, obj.stock or 0,
        )


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


# 订单操作页支持的 4 个动作：op → 页面文案 + 允许的前置状态
ORDER_ACTIONS = {
    'ship': {
        'title': '📦 订单发货', 'btn': '确认发货', 'done': '发货',
        'from_status': (OrderStatus.PENDING,),
        'hint': '发货会真实扣减库存并自动计入收入流水；物流公司统一选，运单号可逐单填写。',
    },
    'cancel': {
        'title': '❌ 取消订单', 'btn': '确认取消', 'done': '取消',
        'from_status': (OrderStatus.PENDING,),
        'hint': '取消后释放该订单预占的库存；已发货订单请改用「退货」。',
    },
    'return': {
        'title': '↩️ 订单退货', 'btn': '确认退货', 'done': '退货',
        'from_status': (OrderStatus.SHIPPED,),
        'hint': '退货后库存回滚，若已记过收入会自动生成一笔退款冲账。',
    },
    'pending': {
        'title': '↳ 撤回为待发货', 'btn': '确认撤回', 'done': '撤回',
        'from_status': (OrderStatus.SHIPPED,),
        'hint': '把已发货订单退回「待发货」（相当于撤销发货），库存与账目一并回滚。',
    },
    'approve_return': {
        'title': '✓ 同意退货申请', 'btn': '确认同意退货', 'done': '同意退货',
        'from_status': (OrderStatus.RETURNING,),
        'hint': '同意后库存自动回滚、款项原路退回账户；请先与买家确认货品已寄回或达成退款协议。',
    },
    'reject_return': {
        'title': '✕ 拒绝退货申请', 'btn': '确认拒绝', 'done': '拒绝退货',
        'from_status': (OrderStatus.RETURNING,),
        'hint': '拒绝后订单回到「已发货 / 已完成」，可在下方填写拒绝理由告知买家。',
    },
}

# 常用快递公司（下拉选择，减少手打错字；最后一项可用于填其他）
SHIP_COMPANIES = [
    '顺丰速运', '京东物流', '中通快递', '圆通速递', '申通快递', '韵达快递',
    '邮政EMS', '极兔速递', '德邦快递', '安能物流', '其它',
]


@admin.register(Orders)
class OrdersAdmin(ZYModelAdmin):
    """订单管理。

    操作方式优化：原来发一笔货要「在列表里逐格编辑物流公司/运单号 → 点保存
    → 勾选行 → 底部选动作 → 点运行 → 选状态字符串 → 确认」六步。现在：
      · 每行一个「快速操作」列，点「发货」直接进「订单操作页」填物流 → 提交；
      · 勾选多行 → 右下浮层点「📦 发货（下一步填物流）」→ 同一页批量填；
      · 取消 / 退货 / 撤回各自独立入口，不再需要在下拉里挑状态字符串。
    物流信息改为在发货时录入（因此移除 list_editable，列表更干净，
    也避免"改了物流忘记保存"这种隐性坑）。
    """

    list_display = ('order_no', 'status_badge', 'quick_ops', 'product_name',
                    'customer_name', 'phone', 'quantity', 'total_price',
                    'logistics_display', 'return_display', 'created_at')
    search_fields = ('order_no', 'customer_name', 'phone', 'product_name',
                     'product_sku', 'tracking_no')
    list_filter = (ShipStateFilter, TodayOrderFilter, 'status', 'return_status', 'created_at')
    # 注意：状态一律经 service 层变更（保证库存联动 + 自动记账），因此不放
    # list_editable；发货统一走「订单操作页」。
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('order_no', 'customer_name', 'phone', 'product', 'product_name',
                       'product_sku', 'unit_price', 'quantity', 'total_price',
                       'shipped_at', 'cancelled_at', 'cancel_reason', 'created_at', 'status')
    list_per_page = 25

    # ── 行内「快速操作」列 ────────────────────────────────────
    @admin.display(description='快速操作')
    def quick_ops(self, obj):
        base = reverse('admin:agent_orders_action')

        def link(op, label, tone):
            return (
                f'<a class="zy-op-link zy-op-{tone}" data-zy-back="1" '
                f'href="{base}?ids={obj.pk}&op={op}">{label}</a>'
            )

        if obj.status == OrderStatus.PENDING:
            html = link('ship', '📦 发货', 'go') + link('cancel', '✕ 取消', 'danger')
        elif obj.status == OrderStatus.SHIPPED:
            html = link('return', '↩ 退货', 'warn') + link('pending', '↳ 撤回', 'mute')
        elif obj.status == OrderStatus.RETURNING:
            # 用户已提交退货申请（return_status=待审核）：商家一键同意/拒绝
            html = link('approve_return', '✓ 同意退货', 'go') + link('reject_return', '✕ 拒绝', 'danger')
        else:
            html = '<span class="zy-op-none">—</span>'
        return format_html('<div class="zy-op">{}</div>', mark_safe(html))

    @admin.display(description='物流信息')
    def logistics_display(self, obj):
        """把「物流公司 + 运单号 + 发货时间」合成一列，省掉两列宽度（少横向滚动）。"""
        company = obj.ship_company or ''
        tracking = obj.tracking_no or ''
        if not company and not tracking:
            return format_html('<span style="color:#9AA89F;">—</span>')
        suffix = format_html(
            '<span style="color:#9AA89F;font-size:11.5px;"> · {}</span>',
            obj.shipped_at.strftime('%m-%d %H:%M'),
        ) if obj.shipped_at else ''
        return format_html(
            '{}<br><span style="font-family:ui-monospace,Menlo,monospace;font-size:12px;'
            'color:#185647;">{}</span>{}',
            company or '—', tracking or '—', suffix,
        )

    @admin.display(description='退货申请')
    def return_display(self, obj):
        """把用户的退货申请（状态 + 理由 + 申请时间）合成一列，让商家一眼看到待处理请求。"""
        if not obj.return_status:
            return format_html('<span style="color:#9AA89F;">—</span>')
        reason = (obj.return_reason or '').strip()
        requested = obj.return_requested_at.strftime('%m-%d %H:%M') if obj.return_requested_at else ''
        color = {
            ReturnStatus.PENDING: ('#FDF0E8', '#7A2E0E'),
            ReturnStatus.APPROVED: ('#DCEDE6', '#134435'),
            ReturnStatus.REJECTED: ('#E2E5E2', '#3A473F'),
        }.get(obj.return_status, ('#EEE', '#333'))
        bg, fg = color
        badge = format_html(
            '<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
            'background:{};color:{};font-size:11.5px;font-weight:600;">{}</span>',
            bg, fg, obj.return_status,
        )
        if reason:
            badge = format_html('{}<br><span style="font-size:12px;color:#5A6B62;">{}</span>', badge, reason)
        if requested:
            badge = format_html('{}<br><span style="font-size:11px;color:#9AA89F;">{} 申请</span>', badge, requested)
        if obj.return_note:
            badge = format_html('{}<br><span style="font-size:11px;color:#B5481C;">处理：{}</span>', badge, obj.return_note)
        return badge

    @admin.display(description='订单状态', ordering='status')
    def status_badge(self, obj):
        # 状态 → (背景色, 文字色, 标签)
        style_map = {
            '未发货': ('#F4B495', '#7A2E0E', '● 待发货'),
            '已发货': ('#DCEDE6', '#134435', '● 已发货'),
            '已完成': ('#DCE9FF', '#13407A', '● 已完成'),
            '退货申请中': ('#FDF0E8', '#7A2E0E', '● 退货申请中'),
            '已取消': ('#E2E5E2', '#3A473F', '● 已取消'),
            '已退货': ('#F7D9CF', '#7A2E0E', '● 已退货'),
        }
        bg, fg, label = style_map.get(obj.status, ('#EEE', '#333', obj.status or '—'))
        return format_html(
            '<span style="display:inline-block;padding:3px 10px;border-radius:999px;'
            'background:{};color:{};font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, fg, label,
        )

    # ── 订单操作页（发货 / 取消 / 退货 / 撤回 四合一） ──────────
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('action/', self.admin_site.admin_view(self.order_action_view),
                 name='agent_orders_action'),
        ]
        return custom + urls

    @staticmethod
    def _back_url(request):
        """操作完成后回到"带筛选条件的列表"，避免用户筛了一半又被打回全量列表。"""
        if request.method == 'GET':
            qs = request.GET.urlencode()
            # 行内按钮的链接里带了 ids/op/back 三个控制参数，回跳链接只保留真正的筛选条件
            qs = '&'.join(p for p in qs.split('&')
                          if not p.startswith(('ids=', 'op=', 'back=')))
        else:
            qs = request.POST.get('back', '') or request.GET.urlencode()
        qs = (qs or '').lstrip('?').replace('\n', '').replace('\r', '')
        base = reverse('admin:agent_orders_changelist')
        return f'{base}?{qs}' if qs else base

    def order_action_view(self, request):
        """统一订单操作页：单笔（列表行内按钮）与批量（勾选后 action）共用。"""
        from django.shortcuts import render

        op = (request.POST.get('op') or request.GET.get('op') or '').strip()
        meta = ORDER_ACTIONS.get(op)
        back_url = self._back_url(request)
        if meta is None:
            self.message_user(request, '未知的订单操作。', level='ERROR')
            return HttpResponseRedirect(back_url)

        ids_raw = request.POST.get('ids') or request.GET.get('ids', '')
        id_list = [int(x) for x in ids_raw.split(',') if x.strip().isdigit()]
        orders = list(Orders.objects.filter(pk__in=id_list).order_by('created_at'))
        if not orders:
            self.message_user(request, '未选中任何订单。', level='WARNING')
            return HttpResponseRedirect(back_url)

        if request.method == 'POST':
            ship_company = (request.POST.get('ship_company') or '').strip()
            tracking_all = (request.POST.get('tracking_all') or '').strip()
            reason = (request.POST.get('cancel_reason') or '').strip()

            ok, fail, msgs = 0, 0, []
            for order in orders:
                try:
                    if op == 'ship':
                        tracking = tracking_all or (request.POST.get(f'tracking_{order.pk}') or '').strip()
                        if not ship_company:
                            raise OrderTransitionError('请先选择物流公司', code='ship_company_required')
                        if not tracking:
                            raise OrderTransitionError('请填写运单号', code='tracking_required')
                        transition_order(order, to_status=OrderStatus.SHIPPED,
                                         ship_company=ship_company, tracking_no=tracking)
                    elif op == 'cancel':
                        transition_order(order, to_status=OrderStatus.CANCELLED,
                                         cancel_reason=reason or '管理员取消')
                    elif op == 'return':
                        transition_order(order, to_status=OrderStatus.RETURNED,
                                         cancel_reason=reason or '管理员退货')
                    elif op == 'approve_return':
                        # 用户退货申请 → 同意：库存回滚 + 自动退款（与服务端一致）
                        transition_order(order, to_status=OrderStatus.RETURNED,
                                         operator=request.user,
                                         cancel_reason=reason or '同意退货申请')
                    elif op == 'reject_return':
                        # 用户退货申请 → 拒绝：回到原状态并记录理由
                        reject_return_request(order, reason=reason, operator=request.user)
                    else:   # pending：已发货 → 待发货（库存回滚 + 自动冲账）
                        revert_shipped_to_pending(order, operator=request.user)
                    ok += 1
                except OrderTransitionError as e:
                    fail += 1
                    msgs.append(f'✗ {order.order_no}：{e.message}')
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    msgs.append(f'✗ {order.order_no}：{e}')

            if ok:
                self.message_user(
                    request,
                    f'✓ 已{meta["done"]} {ok} 笔订单' + (f'，{fail} 笔失败。' if fail else '。'),
                )
            elif not fail:
                self.message_user(request, '没有需要处理的订单。', level='WARNING')
            for m in msgs[:10]:
                self.message_user(request, m, level='ERROR')
            return HttpResponseRedirect(back_url)

        return render(request, 'admin/orders_action.html', {
            **self.admin_site.each_context(request),
            'title': meta['title'],
            'op': op,
            'meta': meta,
            'orders': orders,
            # 状态不符合前置条件的挑出来在页面上提示，避免"点了才发现不行"
            'bad_orders': [o for o in orders if o.status not in meta['from_status']],
            'ship_companies': SHIP_COMPANIES,
            'ids': ','.join(str(o.pk) for o in orders),
            'back_qs': back_url.split('?', 1)[1] if '?' in back_url else '',
            'back_url': back_url,
        })

    # ── 批量动作 ──────────────────────────────────────────────
    actions = ['ship_selected', 'cancel_selected', 'return_selected',
               'reset_pending_selected', export_orders_csv]

    def _bulk_transition(self, request, queryset, to_status, **fixed_kwargs):
        """批量状态机：逐单走 service.transition_order（每单独立事务）。"""
        ok, fail = 0, 0
        msgs = []
        for order in queryset:
            kwargs = dict(fixed_kwargs)
            if to_status == '已发货':
                kwargs.setdefault('ship_company', order.ship_company or '')
                kwargs.setdefault('tracking_no', order.tracking_no or '')
            try:
                transition_order(order, to_status=to_status, **kwargs)
                ok += 1
            except OrderTransitionError as e:
                fail += 1
                msgs.append(f"  ✗ {order.order_no}: {e.message}")
        if ok:
            self.message_user(request, f"✓ 成功 {ok} 个 / 失败 {fail} 个")
        if msgs:
            self.message_user(request, "\n".join(msgs[:10]), level='WARNING')

    @admin.action(description='📦 发货（下一步填物流）')
    def ship_selected(self, request, queryset):
        """跳转到发货页统一填物流公司 + 逐单运单号（不再要求先改列表里的字段）。"""
        ids = ','.join(str(pk) for pk in queryset.values_list('pk', flat=True))
        if not ids:
            self.message_user(request, '请先勾选订单。', level='WARNING')
            return None
        url = reverse('admin:agent_orders_action') + f'?ids={ids}&op=ship'
        back = request.GET.urlencode()
        if back:
            url += f'&{back}'
        return HttpResponseRedirect(url)

    @admin.action(description='❌ 取消订单（释放预占库存）')
    def cancel_selected(self, request, queryset):
        self._bulk_transition(request, queryset, '已取消', cancel_reason='管理员取消')

    @admin.action(description='↩️ 退货（库存回滚）')
    def return_selected(self, request, queryset):
        self._bulk_transition(request, queryset, '已退货', cancel_reason='管理员退货')

    @admin.action(description='↳ 撤回为待发货（回滚库存 + 冲账）')
    def reset_pending_selected(self, request, queryset):
        ok, fail, msgs = 0, 0, []
        for order in queryset:
            try:
                revert_shipped_to_pending(order, operator=request.user)
                ok += 1
            except OrderTransitionError as e:
                fail += 1
                msgs.append(f'✗ {order.order_no}：{e.message}')
        if ok:
            self.message_user(request, f'✓ 已撤回 {ok} 笔订单'
                                       + (f'，{fail} 笔失败。' if fail else '。'))
        for m in msgs[:10]:
            self.message_user(request, m, level='ERROR')


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
class ContactMessageAdmin(ZYQuickToggleAdmin):
    list_display = ('id', 'name', 'phone', 'short_message', 'read_status', 'quick_ops', 'created_at')
    search_fields = ('name', 'phone')
    list_filter = ('is_read',)
    list_per_page = 25
    ordering = ('id',)                                       # id 升序
    actions = [mark_as_read]

    # 行内「标记已读 / 未读」一键切换（替代勾选+运行动作）
    toggle_field = 'is_read'
    toggle_url_name = 'agent_contactmessage_quick_toggle'
    toggle_on_label = '标记已读'
    toggle_off_label = '标记未读'

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
class CustomRequestAdmin(ZYQuickToggleAdmin):
    list_display = ('id', 'name', 'phone', 'company', 'quantity', 'read_status',
                    'quick_ops', 'created_at')
    search_fields = ('name', 'phone', 'company')
    list_filter = ('is_read',)
    list_per_page = 25
    ordering = ('id',)                                       # id 升序
    actions = [mark_as_read]

    # 行内「标记已读 / 未读」一键切换
    toggle_field = 'is_read'
    toggle_url_name = 'agent_customrequest_quick_toggle'
    toggle_on_label = '标记已读'
    toggle_off_label = '标记未读'

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

    # 知识库热更新（P1-7）：保存/删除后立即使内存 RAG 索引缓存失效，
    # 下次请求自动从 DB 重建索引，无需重启 Django 进程。
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        invalidate_rag_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        invalidate_rag_cache()

    # 编辑时提示：keywords 用逗号分隔；保存后索引自动重建（无需重启）
    fieldsets = (
        (None, {'fields': ('title', 'category', 'keywords', 'content', 'is_active')}),
        ('提示', {'fields': (), 'description':
            'keywords 多个词用英文逗号分隔（如：退换,退货,退款）。'
            '保存或删除后索引会自动重建（无需重启进程）；也可在服务器执行 '
            '`python manage.py rebuild_rag` 手动全量重建。'}),
    )


# ══════════════════════════════════════════════════════════════
# 钱包 / 流水（让"钱从哪里来到哪里去"在后台可追溯）
# ══════════════════════════════════════════════════════════════
@admin.register(Wallet)
class WalletAdmin(ZYModelAdmin):
    """公司钱包「总览与调账」页（单例）。

    设计要点：**余额不可直接编辑**。钱包只保存一个"当前余额"结果值，任何一分钱的
    增减都必须先落成一条流水（右下角充值/扣款按钮，或订单发货/退货自动记账），
    这样"钱从哪来、到哪去"永远可追溯，也不会出现改数字不留痕的糊涂账。
    """
    list_display = ('id', 'balance_display', 'income_today_display', 'income_month_display',
                    'income_total_display', 'expense_total_display', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('balance',),
            'description': '余额由流水自动累加得出，此处不可直接修改。'
                           '要增减金额请点右下角「＋ 充值 / － 扣款」，'
                           '系统会自动写一条流水并同步余额。',
        }),
        ('📊 财务统计（今日/本月/分类汇总）', {'fields': ('stats_panel',)}),
        ('📈 近 30 日收入趋势', {'fields': ('income_trend_panel',)}),
        ('🕐 最近 10 笔流水', {'fields': ('recent_txs_panel',)}),
        ('信息', {'fields': ('updated_at',)}),
    )
    readonly_fields = ('balance', 'updated_at', 'stats_panel',
                       'income_trend_panel', 'recent_txs_panel')

    class Media:
        js = ('admin/wallet_actions.js',)   # 详情页「充值 / 扣款」按钮

    def changelist_view(self, request, extra_context=None):
        """单例模型不摆"一行列表"——直接进总览详情页。

        账本（每一笔钱款一行）在「公司钱包 · 收支账本」页，
        侧栏「公司钱包」即指向那里。
        """
        return HttpResponseRedirect(
            reverse('admin:agent_wallet_change', args=[Wallet.get_solo().pk])
        )

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
        if not note:
            # 每一笔钱款都必须写清来源/去向，否则事后无法对账
            return JsonResponse(
                {'success': False, 'message': '请填写款项说明（这笔钱从哪来 / 花到哪去）'},
                status=400,
            )
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
        total_cnt = Transaction.objects.filter(wallet=obj).count()
        ledger_url = reverse('admin:agent_transaction_changelist')
        head = format_html(
            '<div style="display:flex;justify-content:space-between;align-items:center;margin:6px 0 8px;">'
            '<div style="font-size:12.5px;color:#738079;">'
            '钱包共记录 <b style="color:#1F6B54;">{}</b> 笔钱款往来（下方为最近 10 笔）</div>'
            '<a href="{}" style="font-size:12.5px;color:#2C7C8C;text-decoration:none;font-weight:600;">'
            '查看完整账本 →</a></div>',
            total_cnt, ledger_url,
        )
        txs = list(Transaction.objects.filter(wallet=obj).order_by('-created_at')[:10])
        if not txs:
            return format_html(
                '{}<p style="color:#999;padding:8px;">暂无流水（订单已发货/退货会自动写入）</p>', head
            )
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
            '{}'
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
            head,
            mark_safe(''.join(str(r) for r in rows)),
        )


@admin.register(Transaction)
class TransactionAdmin(ZYModelAdmin):
    """公司钱包 · 收支账本 —— **每一笔钱款一行**，可筛选、可搜索、可导出。

    之前「公司钱包」页只有一行余额汇总，看不到钱是怎么进来的；本页把每一笔往来
    都列出来（含该笔之后的账户余额），账目只读、不可改不可删，保证可追溯。
    """
    list_display = ('created_at', 'tx_type_badge', 'category', 'amount_display',
                    'balance_after_display', 'note', 'order_link', 'operator')
    list_display_links = ('created_at',)
    list_filter = ('tx_type', 'category', 'created_at')
    search_fields = ('order__order_no', 'note', 'amount')
    date_hierarchy = 'created_at'
    list_per_page = 30
    ordering = ('-created_at', '-id')
    list_before_template = 'admin/agent/transaction/list_before.html'
    readonly_fields = ('wallet', 'tx_type', 'category', 'amount', 'order', 'note', 'operator', 'created_at')
    actions = ('export_ledger_csv',)

    class Media:
        # 账本页右下角也提供「充值 / 扣款」入口（与钱包总览页同一套弹窗）
        js = ('admin/wallet_actions.js',)

    # ── 账本顶部：钱包概览 + 对账状态 + 导出/总览入口 ──────────────
    def changelist_view(self, request, extra_context=None):
        # /admin/agent/transaction/?export=csv → 导出「当前筛选结果」为 CSV
        if request.GET.get('export') == 'csv':
            # 注意：admin 的 ChangeList 会把不认识的 GET 参数当作模型字段查询，
            # 直接带着 export=csv 建 ChangeList 会抛 IncorrectLookupParameters。
            # 所以先临时摘掉该参数，拿到过滤后的 queryset 再导出。
            original_get = request.GET
            clean_get = original_get.copy()
            del clean_get['export']
            request.GET = clean_get
            try:
                cl = self.get_changelist_instance(request)
                queryset = cl.get_queryset(request)
            finally:
                request.GET = original_get
            return self._ledger_csv_response(queryset)

        extra = dict(extra_context or {})
        extra.setdefault('title', '公司钱包 · 收支账本')
        extra['wallet_panel'] = self.ledger_overview_panel()
        return super().changelist_view(request, extra)

    # ── 余额快照：流水 id → 该笔之后的余额（整本账正序累加） ────────
    _balance_local = threading.local()

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # 预先算好整本账的累计余额（"余额"列用）。只按时间正序累加一次，
        # 因此**筛选/搜索都不会让余额列失真**（若用 SQL 窗口函数，WHERE 会先
        # 生效导致筛出来的行余额全错）。
        self._balance_local.map = self._build_balance_map()
        return qs

    @staticmethod
    def _build_balance_map():
        """{流水id: 该笔之后的钱包余额}，按 created_at, id 正序累加。"""
        bal = Decimal('0')
        out = {}
        rows = (Transaction.objects
                .order_by('created_at', 'id')
                .values_list('id', 'tx_type', 'amount')
                .iterator(chunk_size=2000))
        for tid, ttype, amt in rows:
            bal += (amt if ttype == TxType.INCOME else -amt)
            out[tid] = bal
        return out

    def _balance_map(self):
        mp = getattr(self._balance_local, 'map', None)
        if mp is None:
            mp = self._build_balance_map()
            self._balance_local.map = mp
        return mp

    @admin.display(description='余额')
    def balance_after_display(self, obj):
        bal = self._balance_map().get(obj.pk)
        if bal is None:
            return '—'
        color = '#1F6B54' if bal >= 0 else '#B5481C'
        return format_html('<span style="color:{};font-weight:600;">¥{}</span>',
                           color, f"{bal:,.2f}")

    # ── 顶部概览面板 ──────────────────────────────────────────────
    def ledger_overview_panel(self):
        from datetime import date, timedelta
        wallet = Wallet.get_solo()
        agree = Transaction.objects.filter(wallet=wallet)
        agg = {}
        for ttype in (TxType.INCOME, TxType.EXPENSE):
            agg[ttype] = agree.filter(tx_type=ttype).aggregate(
                s=Sum('amount'), n=Count('id'))
        in_sum = agg[TxType.INCOME]['s'] or Decimal('0')
        out_sum = agg[TxType.EXPENSE]['s'] or Decimal('0')
        in_cnt = agg[TxType.INCOME]['n'] or 0
        out_cnt = agg[TxType.EXPENSE]['n'] or 0
        total_cnt = in_cnt + out_cnt
        led_sum = in_sum - out_sum
        balance = wallet.balance or Decimal('0')
        diff = balance - led_sum

        today = date.today()
        month_start = today.replace(day=1)
        day_in = (agree.filter(tx_type=TxType.INCOME, created_at__date=today)
                  .aggregate(s=Sum('amount'))['s'] or Decimal('0'))
        month_in = (agree.filter(tx_type=TxType.INCOME, created_at__date__gte=month_start)
                    .aggregate(s=Sum('amount'))['s'] or Decimal('0'))
        month_out = (agree.filter(tx_type=TxType.EXPENSE, created_at__date__gte=month_start)
                     .aggregate(s=Sum('amount'))['s'] or Decimal('0'))
        week_in = (agree.filter(tx_type=TxType.INCOME,
                                created_at__date__gte=today - timedelta(days=6))
                   .aggregate(s=Sum('amount'))['s'] or Decimal('0'))

        bal_color = '#1F6B54' if balance >= 0 else '#B5481C'

        def card(label, value, color, bg, note=''):
            note_html = (f'<div style="color:#9AA89F;font-size:11px;margin-top:2px;">{note}</div>'
                         if note else '')
            return (
                f'<div style="background:{bg};padding:11px 14px;border-radius:10px;'
                f'border-left:4px solid {color};">'
                f'<div style="color:#5C6A63;font-size:12px;">{label}</div>'
                f'<div style="color:{color};font-size:20px;font-weight:700;line-height:1.45;">{value}</div>'
                f'{note_html}</div>'
            )

        cards = ''.join([
            card('当前余额', f'¥{balance:,.2f}', bal_color, '#F6F1E7',
                 f'{total_cnt} 笔往来'),
            card('今日收入', f'+¥{day_in:,.2f}', '#1F6B54', '#EAF6F1'),
            card('近 7 日收入', f'+¥{week_in:,.2f}', '#2C7C8C', '#E2F3F7'),
            card('本月收入', f'+¥{month_in:,.2f}', '#1F6B54', '#EAF6F1'),
            card('本月支出', f'-¥{month_out:,.2f}', '#B5481C', '#F7D9CF'),
            card('累计收入 / 支出', f'{in_sum:,.2f} / {out_sum:,.2f}', '#2C7C8C', '#E2F3F7',
                 f'{in_cnt} 收 / {out_cnt} 支'),
        ])

        # 对账状态：流水累计 vs 钱包余额
        if abs(diff) < Decimal('0.005'):
            recon = ('<div style="background:#EAF6F1;border-left:4px solid #1F6B54;'
                     'padding:9px 12px;border-radius:8px;font-size:12.5px;color:#134435;">'
                     '✓ 账实相符：流水累计与钱包余额一致（'
                     f'¥{led_sum:,.2f}）。每一笔变动都有记录可查。</div>')
        else:
            recon = ('<div style="background:#FFF3E5;border-left:4px solid #E2703A;'
                     'padding:9px 12px;border-radius:8px;font-size:12.5px;color:#7A2E0E;">'
                     f'⚠ 账实不符：流水累计 ¥{led_sum:,.2f}，钱包余额 ¥{balance:,.2f}，'
                     f'差额 <b>¥{diff:,.2f}</b>。多为历史数据在"自动记账"上线前已发生变动所致，'
                     '可执行 <code>python manage.py backfill_wallet --apply --reconcile</code> 补平。</div>')

        export_url = reverse('admin:agent_transaction_changelist') + '?export=csv'
        overview_url = reverse('admin:agent_wallet_change', args=[wallet.pk])
        return format_html(
            '<div style="margin:4px 0 14px;">'
            '<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;">{cards}</div>'
            '<div style="margin-top:10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">'
            '<div style="flex:1;min-width:280px;">{recon}</div>'
            '<a href="{overview}" style="font-size:12.5px;color:#2C7C8C;text-decoration:none;font-weight:600;">'
            '钱包总览与调账 →</a>'
            '<a href="{export}" style="font-size:12.5px;color:#2C7C8C;text-decoration:none;font-weight:600;">'
            '⬇ 导出当前筛选为 CSV</a>'
            '</div>'
            '<div style="margin-top:8px;font-size:11.5px;color:#9AA89F;">'
            '「余额」列为该笔钱款入账/出账之后的钱包余额，便于逐笔对账。'
            '账目为只读：不可改、不可删——要更正只能再记一笔反向流水。'
            '</div></div>',
            cards=mark_safe(cards), recon=mark_safe(recon),
            overview=overview_url, export=export_url,
        )

    # ── CSV 导出 ──────────────────────────────────────────────────
    @admin.action(description='⬇ 导出选中流水为 CSV')
    def export_ledger_csv(self, request, queryset):
        return self._ledger_csv_response(queryset)

    def _ledger_csv_response(self, queryset):
        from datetime import datetime
        resp = HttpResponse(content_type='text/csv; charset=utf-8-sig')
        stamp = datetime.now().strftime('%Y%m%d%H%M')
        resp['Content-Disposition'] = f'attachment; filename="wallet_ledger_{stamp}.csv"'
        resp.write('\ufeff')  # BOM：Excel 直接双击不乱码
        writer = csv.writer(resp)
        writer.writerow(['流水号', '时间', '类型', '分类', '金额', '余额',
                         '说明', '关联订单', '操作人'])
        bmap = self._build_balance_map()
        total_in = total_out = Decimal('0')
        for t in queryset.order_by('created_at', 'id'):
            is_in = t.tx_type == TxType.INCOME
            if is_in:
                total_in += t.amount
            else:
                total_out += t.amount
            writer.writerow([
                t.pk,
                # 本项目 USE_TZ=False，created_at 是朴素本地时间，不能再 localtime()
                t.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                t.tx_type, t.category,
                f'{"+" if is_in else "-"}{t.amount:.2f}',
                f'{bmap.get(t.pk, Decimal("0")):.2f}',
                t.note, t.order.order_no if t.order else '',
                t.operator.username if t.operator else '',
            ])
        writer.writerow([])
        writer.writerow(['合计', '', f'收入 {total_in:.2f}', '', '', '',
                         f'支出 {total_out:.2f}', f'净额 {total_in - total_out:.2f}', ''])
        return resp

    def has_add_permission(self, request):
        return False  # 流水只能由系统/服务写入，避免人为乱账

    def has_delete_permission(self, request, obj=None):
        return False  # 账目不可删（删了就查不到钱去哪了）

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
