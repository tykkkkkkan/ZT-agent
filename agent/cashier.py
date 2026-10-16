"""模拟收银台 —— 「开票 → 扫码 → 入账」这条闭环的唯一出口。

真实世界里这条链路由支付平台承担：

    商户服务器下单 → 平台返回支付链接/二维码 → 用户在手机侧付款
      → 平台**异步回调**商户服务器 → 商户落「已支付」→ 解锁发货

本项目没有企业资质、没有商户号、没有备案域名与公网 HTTPS 回调（见方案书的
「支付边界」说明），所以把这条链**自建**出来，但刻意保持三段式结构不变：

    ① 开票   `create_ticket()`      PC 端发起 → 一次性 token → 前端渲染成二维码
    ② 扫码   `load_ticket()` / `redeem_ticket()`   手机打开确认页 → 点「确认付款」
    ③ 入账   `redeem_ticket()` 内部调 `services.mark_order_paid()`

同一个「入账」出口还有第二条路：**余额支付**（`pay_with_balance()`）。
它不出票、不扫码，直接从用户钱包扣款 —— 因为「用户自己的钱」不需要
向任何外部渠道求证；出票那套（一次性 token + 有效期 + 防重放）是为
**"这个操作确实来自本人"** 服务的，余额扣款本身就在已登录的会话里完成。
两条路最终都汇到 `mark_order_paid()`，所以订单侧的规则只有一份。

这样答辩时能讲清楚：**我们模拟的是「收款动作」，不是「支付渠道」**。
将来换成真实渠道，要改的只有 ③ 由谁触发（回调 URL 取代手机页面），
①② 与订单侧的状态机完全不用动。

三个必须做对的地方
------------------
1. **一次性 + 加锁**：核销走 `select_for_update`，两个标签页同时提交只成功一次；
   无状态签名做不到这一点（它只能验签，不能表达"已经被用掉了"）。
2. **有有效期**：默认 5 分钟。二维码挂在屏幕上无人看管，长期有效等于把
   付款权限一直摊开着。
3. **金额快照**：票上记的是开票时的应付金额。订单金额后来若被改动，
   "我明明看到 199 才扫的"才有据可查。
"""

import os
import secrets
import socket
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from agent import user_wallet
from agent.models import (
    OrderStatus,
    Orders,
    PayChannel,
    PaymentStatus,
    PayTicket,
    PayTicketStatus,
)
from agent.services import OrderTransitionError, mark_order_paid

# 二维码有效期：够慢吞吞地掏出手机扫一下，又不至于让一张挂屏的码长时间有效。
# 可用环境变量覆盖（演示时想快点看到过期，设成 30 秒即可）。
TICKET_TTL_SECONDS = max(30, int(os.getenv("PAY_TICKET_TTL_SECONDS") or 300))
# 同一订单最多同时存在几张在途票 —— 防"刷新狂刷出一堆码"。
MAX_OPEN_TICKETS_PER_ORDER = 6
# 手机确认页展示的地址只露前若干字，够核对又不过度暴露
ADDRESS_PEEK = 6


class CashierError(Exception):
    """收银台的业务异常（视图层据此回 4xx + 可读 message）。"""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def _now():
    return timezone.now()


def _mask_phone(phone: str) -> str:
    """手机号中间四位打码 —— 手机确认页要能被本人核对，但不该在别人手里成为一份通讯录。"""
    p = (phone or "").strip()
    if len(p) < 8:
        return p
    return f"{p[:3]}****{p[-4:]}"


def _mask_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    return n[0] + ("*" * (len(n) - 1))


# ════════════════════════════════════════════════════════════════
# ① 开票
# ════════════════════════════════════════════════════════════════
def order_pay_state(order: Orders) -> str:
    """订单当前能不能付款 —— 一个字符串回答，供开票与前端展示共用。

    返回 `payable` / `paid` / `refunded` / `settled` / `closed` / `abnormal`。
    口径与 `services.mark_order_paid` 严格一致，避免两处判断长出两套规则。
    """
    if order is None:
        return "missing"
    if order.payment_status == PaymentStatus.PAID:
        return "paid"
    if order.payment_status == PaymentStatus.REFUNDED:
        return "refunded"
    if order.payment_status == "":
        # 支付功能上线前的老订单 → 视为已结算，不该出现收银台
        return "settled"
    if order.status in (OrderStatus.CANCELLED, OrderStatus.RETURNED):
        return "closed"
    if order.status == OrderStatus.PENDING:
        return "payable"
    # 已发货/已完成却仍是未支付 —— 流程异常，交后台核对，不在这里"补一笔"
    return "abnormal"


_PAY_STATE_MESSAGE = {
    "paid": "该订单已支付，无需重复付款。",
    "refunded": "该订单款项已退回，无法再次支付。",
    "settled": "该订单创建于支付功能上线之前，视为已结算，无需付款。",
    "closed": "订单已取消或已退货，无需付款。",
    "abnormal": "该订单已是发货后状态却没有支付记录，请联系客服核对款项。",
    "missing": "订单不存在，请联系客服核对。",
}


def expire_stale_tickets(order_no: str = None) -> int:
    """把已过期的「待支付」票标成「已过期」，返回影响条数。

    惰性清理而不是定时任务：演示项目不引入 Celery，每次开票/轮询顺手扫一遍就够。
    查询条件与 `idx_pay_order_status` 配合（某订单 + 某状态），不会全表扫。
    """
    qs = PayTicket.objects.filter(status=PayTicketStatus.PENDING, expires_at__lte=_now())
    if order_no:
        qs = qs.filter(order_no=order_no)
    return qs.update(status=PayTicketStatus.EXPIRED)


def create_ticket(order: Orders, user, *, channel: str = PayChannel.QR, ttl: int = None) -> PayTicket:
    """为某笔订单开一张（或复用一张）收银凭据。

    **复用**是刻意的：收银台页面会轮询、用户会刷新、会点两次「重新生成二维码」——
    每次都新开一张票，几分钟内就能攒出几十行垃圾数据，而且屏幕上的码、
    数据库里的票、用户手里扫到的码会不一致。所以在有效期内的同一渠道
    在途票直接沿用。
    """
    if order is None:
        raise CashierError("订单不存在，请联系客服核对。", code="not_found")

    state = order_pay_state(order)
    if state != "payable":
        raise CashierError(_PAY_STATE_MESSAGE.get(state, "当前订单不可支付。"), code=state)

    amount = order.total_price or Decimal("0.00")
    if amount <= 0:
        raise CashierError("订单金额为 0，无需付款。", code="zero_amount")

    channel = channel or PayChannel.QR
    expire_stale_tickets(order.order_no)

    existing = (
        PayTicket.objects
        .filter(order_no=order.order_no, status=PayTicketStatus.PENDING,
                channel=channel, expires_at__gt=_now())
        .order_by("-id")
        .first()
    )
    if existing is not None:
        return existing

    open_count = PayTicket.objects.filter(
        order_no=order.order_no, status=PayTicketStatus.PENDING, expires_at__gt=_now(),
    ).count()
    if open_count >= MAX_OPEN_TICKETS_PER_ORDER:
        raise CashierError(
            "该订单待支付的二维码过多，请稍等 5 分钟后再试，或直接联系客服。",
            code="too_many",
        )

    return PayTicket.objects.create(
        order_no=order.order_no,
        user=user if (user is not None and getattr(user, "is_authenticated", False)) else None,
        token=secrets.token_urlsafe(32)[:64],
        status=PayTicketStatus.PENDING,
        channel=channel,
        amount=amount,
        expires_at=_now() + timedelta(seconds=ttl or TICKET_TTL_SECONDS),
    )


# ════════════════════════════════════════════════════════════════
# ② 扫码：读凭据
# ════════════════════════════════════════════════════════════════
def load_ticket(token: str) -> PayTicket:
    """按 token 取票。找不到 → `CashierError(code='not_found')`。"""
    tk = (token or "").strip()
    if not tk:
        raise CashierError("支付凭据缺失，请重新扫码。", code="not_found")
    ticket = PayTicket.objects.filter(token=tk).select_related("user", "redeemed_by").first()
    if ticket is None:
        raise CashierError("支付凭据无效或已被清理，请回到收银台重新获取二维码。", code="not_found")
    return ticket


def ticket_payload(ticket: PayTicket) -> dict:
    """凭据 → JSON（PC 端与手机端共用，字段只增不减）。"""
    remain = 0
    if ticket.expires_at:
        remain = max(0, int((ticket.expires_at - _now()).total_seconds()))
    return {
        "token": ticket.token,
        "status": ticket.status,
        "channel": ticket.channel,
        "amount": str(ticket.amount),
        "order_no": ticket.order_no,
        "expires_at": ticket.expires_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.expires_at else "",
        "remain_seconds": remain,
        "open": ticket.is_open,
        "redeemed_at": ticket.redeemed_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.redeemed_at else "",
    }


def order_summary(order: Orders, *, mask: bool = False) -> dict:
    """订单摘要。`mask=True` 用于手机确认页（脱敏）。"""
    if order is None:
        return {}
    data = {
        "order_no": order.order_no,
        "product_name": order.product_name or "",
        "product_sku": order.product_sku or "",
        "quantity": order.quantity,
        "unit_price": str(order.unit_price if order.unit_price is not None else ""),
        "total_price": str(order.total_price if order.total_price is not None else ""),
        "status": order.status,
        "payment_status": order.payment_status,
        "created_at": order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
        "expected_date": order.expected_date.strftime("%Y-%m-%d") if order.expected_date else "",
    }
    if mask:
        data["customer_name"] = _mask_name(order.customer_name)
        data["phone"] = _mask_phone(order.phone)
        data["address"] = (order.address or "")[:ADDRESS_PEEK] + (
            "…" if len(order.address or "") > ADDRESS_PEEK else ""
        )
    else:
        data["customer_name"] = order.customer_name or ""
        data["phone"] = order.phone or ""
        data["address"] = order.address or ""
    return data


# ════════════════════════════════════════════════════════════════
# ③ 入账：核销
# ════════════════════════════════════════════════════════════════
def _fail(ticket, code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message, "ticket": ticket, "order": None, "already": False}


@transaction.atomic
def redeem_ticket(token: str, *, by_user=None) -> dict:
    """核销凭据 → 订单落「已支付」。

    **不抛异常表示业务态**（过期/已取消/订单异常都用返回值表达），原因是：
    过期票需要把状态改成「已过期」并**留下这条记录**，若用异常退出，
    整个事务会回滚，连"这张码为什么没付成功"的痕迹都没了 —— 那等于没记。
    只有"凭据根本不存在"这种没有东西可写的场景才由 `load_ticket` 抛异常。

    返回值：`{ok, code, message, ticket, order, already}`
    """
    tk = (token or "").strip()
    ticket = PayTicket.objects.select_for_update().filter(token=tk).first()

    if ticket is None:
        return _fail(None, "not_found", "支付凭据无效，可能已被清理。请回到收银台重新获取二维码。")

    if ticket.status == PayTicketStatus.PAID:
        order = Orders.objects.filter(order_no=ticket.order_no).first()
        return {
            "ok": True, "code": "already_paid", "already": True, "ticket": ticket, "order": order,
            "message": "该笔款项已支付，无需重复付款。",
        }
    if ticket.status == PayTicketStatus.CANCELLED:
        return _fail(ticket, "cancelled",
                     "这张二维码已作废（同一订单已在别处完成支付）。请回到收银台确认订单状态。")
    if ticket.status == PayTicketStatus.EXPIRED or ticket.is_expired:
        if ticket.status != PayTicketStatus.EXPIRED:
            ticket.status = PayTicketStatus.EXPIRED
            ticket.save(update_fields=["status"])
        minutes = max(1, TICKET_TTL_SECONDS // 60)
        return _fail(ticket, "expired",
                     f"二维码已过期（有效期 {minutes} 分钟）。请回到收银台刷新后重新扫码。")

    order = Orders.objects.select_for_update().filter(order_no=ticket.order_no).first()
    if order is None:
        return _fail(ticket, "not_found", "关联订单不存在，请联系客服核对后再试。")

    try:
        result = mark_order_paid(order, operator=by_user if getattr(by_user, "is_authenticated", False) else ticket.user)
    except OrderTransitionError as e:
        return _fail(ticket, e.code or "invalid", e.message)

    ticket.status = PayTicketStatus.PAID
    ticket.redeemed_at = _now()
    ticket.redeemed_by = by_user if getattr(by_user, "is_authenticated", False) else None
    ticket.save(update_fields=["status", "redeemed_at", "redeemed_by"])

    # 同一订单的其他在途票立刻作废 —— 避免屏幕上还挂着第二张能付的码
    PayTicket.objects.filter(
        order_no=ticket.order_no, status=PayTicketStatus.PENDING,
    ).exclude(pk=ticket.pk).update(status=PayTicketStatus.CANCELLED)

    return {
        "ok": True, "code": "paid", "already": False,
        "ticket": ticket, "order": result.order,
        "message": result.message or "支付成功，商家将尽快为您安排发货。",
    }


# ════════════════════════════════════════════════════════════════
# ③′ 入账（余额支付）—— 不经过二维码，直接从用户钱包扣款
# ════════════════════════════════════════════════════════════════
@transaction.atomic
def pay_with_balance(order: Orders, user, *, note: str = "") -> dict:
    """用**用户钱包余额**支付订单：扣余额 + 记流水 + 订单落「已支付」。

    与扫码支付的一句话区别：**扣款这一步没有"客人操作"的环节**。
    扫码是「PC 开票 → 客人在手机上按确认 → 我们才动账」，中间有一次外部确认；
    余额支付是「客人点了按钮 → 我们直接动账」，没有可以等待的外部信号。

    于是它多两条约束：

    · **扣款与落单同事务**：钱扣了单没落 → 用户白付；单落了钱没扣 →
      商家收不到钱。同一 `transaction.atomic` 里办完，中途任何一步失败全回滚。
    · **扣款必须在落单之前**：余额不足时 `user_wallet.pay` 抛
      `UserWalletError(code='insufficient')`，异常直接冲出事务 → 订单**一点没动**。
      反过来的顺序（先 mark_order_paid 再扣钱）一旦余额不够，订单已经付掉了，
      只能靠回滚掩盖，日志里也看不出"是钱不够导致的失败"。

    为什么不用 `has_paid_order` 做幂等判据：`order_pay_state(order) == 'paid'`
    已经把"这笔付过了"说全了（无论当初是扫码还是余额付的），
    再查一遍流水只会多一次查询、多一个可能与状态不一致的分支。
    重复调用返回 `already=True`，与 `mark_order_paid` 的幂等口径一致。

    返回 `{ok, code, already, order, transaction, amount, message}`；
    **不可付款的状态抛 `CashierError`**（这里没有"要留痕的失败态"，
    与 `redeem_ticket` 的取舍不同 —— 那张票的存在本身就是要留的痕）。
    """
    locked = Orders.objects.select_for_update().filter(pk=order.pk).first()
    if locked is None:
        raise CashierError("订单不存在，请联系客服核对。", code="not_found")

    state = order_pay_state(locked)
    if state == "paid":
        return {
            "ok": True, "code": "already_paid", "already": True,
            "order": locked, "transaction": None,
            "amount": str(locked.total_price or 0),
            "message": _PAY_STATE_MESSAGE["paid"],
        }
    if state != "payable":
        raise CashierError(
            _PAY_STATE_MESSAGE.get(state, "该订单当前无法付款。"), code=state,
        )

    amount = Decimal(locked.total_price or 0)
    if amount <= 0:
        raise CashierError("该订单金额为 0，无需支付。", code="zero_amount")

    # ① 先扣钱（余额不足在这里就中断，订单一个字段都没动）
    tx = user_wallet.pay(
        user, amount, order_no=locked.order_no,
        note=note or f"余额支付订单 {locked.order_no}",
    )

    # ② 再落单
    result = mark_order_paid(locked, operator=user)

    # ③ 屏上若还挂着这张单的二维码，一并作废 —— 已经付清了，那张码不该还能扫
    PayTicket.objects.filter(
        order_no=locked.order_no, status=PayTicketStatus.PENDING,
    ).update(status=PayTicketStatus.CANCELLED)

    return {
        "ok": True, "code": "paid", "already": False,
        "order": result.order, "transaction": tx, "amount": str(amount),
        "message": "余额支付成功，商家将尽快为您安排发货。",
    }


# ════════════════════════════════════════════════════════════════
# 二维码与可扫地址
# ════════════════════════════════════════════════════════════════
def lan_ip() -> str:
    """本机在局域网里的出口 IP。

    不发任何包 —— `connect` 一个 UDP 地址只是让内核挑一条路由并把本地地址
    填进 socket，用来"问一句我从哪个网卡出去"。取不到就返回空串。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def reachable_base_url(request) -> str:
    """能被**手机**扫到的站点根地址。

    这一步不是可有可无的：PC 上访问的是 `http://localhost:8001/`，
    把它原样编进二维码，手机扫出来只会打开**手机自己的** localhost —— 必然打不开。
    所以本地开发时要把 host 换成局域网 IP，端口保持不变（8001）。

    优先级：环境变量 `PUBLIC_BASE_URL` > 请求里的真实域名 > 局域网 IP。
    `request` 允许为 None（离线脚本/命令行里也要能算出地址）。
    """
    override = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if override:
        return override

    scheme = getattr(request, "scheme", "") or "http"
    host = request.get_host() if request is not None else ""
    hostname, _, port = host.partition(":")
    if hostname and hostname not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver"):
        return f"{scheme}://{host}"

    ip = lan_ip()
    if ip:
        return f"{scheme}://{ip}" + (f":{port}" if port else "")
    return f"{scheme}://{host}"


def pay_url(request, ticket: PayTicket) -> str:
    """手机扫码后要打开的确认页地址。"""
    return f"{reachable_base_url(request)}/pay/confirm/{ticket.token}/"


def qr_svg(text: str) -> str:
    """把任意文本编成二维码 SVG（矢量，放大不糊）。

    对 `qrcode` 缺依赖做**优雅降级**：返回空串，调用方照常把明文 URL 给出去，
    用户仍可手动输入或复制链接 —— 少一个依赖不该让整条支付链路瘫掉。
    """
    if not text:
        return ""
    try:
        import io

        import qrcode
        import qrcode.image.svg
    except Exception:  # noqa: BLE001 — 缺依赖/版本异常都退化为"没有图"
        return ""
    try:
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:  # noqa: BLE001 — 编码失败同样降级，不把 500 抛给用户
        return ""
