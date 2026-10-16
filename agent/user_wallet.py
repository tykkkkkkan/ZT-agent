"""用户侧钱包（C 端余额）—— **唯一的记账出口**。

与公司钱包（`agent/models.py` 的 `Wallet` / 表 `company_wallet`）严格分离：

    · 公司钱包记「**商家**收了多少钱」：订单发货记一笔收入、退货记一笔支出，
      全站只有一个账户（id=1），是**经营账**。
    · 用户钱包记「**这个账号**自己充进来、又被花掉的余额」，一人一本，是**预存账**。

两者靠 `order_no` 串起来看，但**互不代表**：
「用户用余额支付了 100 元」在公司账上是「订单收入 +100」，
在用户账上却是「余额支付 −100」。同一个业务动作、两本账上方向相反 ——
这也是为什么不能让它们共用一个模型：一旦共用，报表口径必错。

三条不变式（每一次写操作都必须同时成立）：

    ① **同事务**：余额变动与流水插入要么都成功、要么都回滚。
       单靠 `save()` 很容易写出「钱扣了但没记流水」这类对不上账的脏数据。
    ② **`balance_after` 是事实**：它必须等于记完这一笔后的真实余额。
       对账时把流水按时间串起来，逐条验算 `上一条 balance_after ± amount`
       是否等于本条的 `balance_after`，就能在不信任任何缓存的情况下验明账本。
    ③ **余额不为负**：支付前先查余额，不足直接拒绝，**不给透支**。
       （唯一的例外是管理端纠错，那时由调用方显式传 `allow_negative=True`，
       并且在备注里写明原因 —— 让"不得不为负"这件事在流水里留痕。）

为什么金额一律存**正数**、方向由 `kind` 决定？
    与公司钱包的 `Transaction` 保持同一口径。负数金额会在聚合时诱导出
    "金额求和 = 净流量"的错误写法，而实际语义要按 `kind` 分组才能算对。

与订单的桥接（**一处，别在别处重写**）
--------------------------------------
订单终结（取消 / 退货）时，钱该回到谁手里？答案是**当初被扣钱的那个钱包**。
这个判定收在 `refund_order_if_paid()` 一个函数里，判定依据是**流水**而不是订单字段：

    · 「付过没有」→ `UserTransaction(kind='支付', order_no=…)` 是否存在
      （扫码支付同样让订单变「已支付」，但它没有用户余额流水，也就没有钱可退）
    · 「退给谁」  → 支付流水所属的钱包
      （凭手机号下的单 `user_id` 可能为空，只有流水能说明钱从哪个钱包出的）
    · 「退多少」  → 该订单在该钱包下的 PAY 流水之和（实付额，不是订单总价）

`agent/services.py` 的取消 / 退货分支只调这一个函数，不自己判支付状态 ——
否则「哪些单该退、退多少」会长出第二套口径，和价格口径分裂是同一类坑。
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from django.db import transaction
from django.db.models import Q, Sum

from agent.models import UserTransaction, UserTxKind, UserWallet

# 单笔充值上限：本项目是模拟收银台，不需要支持大额；
# 设上限是为了让"手滑多打几个 0"当场被拒，而不是安静地把余额撑成一串天文数字。
MAX_TOPUP = Decimal("50000.00")
MAX_NOTE_LEN = 200
MAX_ORDER_NO_LEN = 50


class UserWalletError(Exception):
    """用户钱包的业务异常（视图层据此回 4xx + 可读 message）。"""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# ════════════════════════════════════════════════════════════════
# 校验
# ════════════════════════════════════════════════════════════════
def clean_amount(value, *, max_amount: Decimal = None) -> Decimal:
    """把用户传来的金额收敛成两位小数的正 Decimal。

    接受的输入：`Decimal` / `float` / `int` / `"12.5"` / `"12.50"`。
    拒绝：空、非数字、`NaN` / `Infinity`、0、负数、超过上限。

    两个坑值得写下来：
      · `Decimal("NaN")` 与 `Decimal("Infinity")` **能解析成功**，而 `NaN <= 0`
        恒为 False —— 只用 `if amt <= 0` 挡不住它们，必须显式 `is_finite()`。
        （放进去的后果是库里出现一个既非正也非负的余额，之后所有比较都错。）
      · 舍入显式用 `ROUND_HALF_UP`（四舍五入），而不是 Python 默认的
        `ROUND_HALF_EVEN`（银行家舍入）—— 后者会把 `100.005` 变成 `100.00`，
        对"我明明付了 100.005"的用户来说就是少了一分钱，说不清。
    """
    if value is None:
        raise UserWalletError("请填写金额")
    raw = str(value).strip()
    if not raw:
        raise UserWalletError("请填写金额")
    try:
        amt = Decimal(raw)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        # 带上 from exc：用户看到的是"金额格式有误"，但日志里要能追到
        # 究竟是 Decimal 抛了什么（比如传进来的是 None 而不是数字）。
        raise UserWalletError("金额格式有误：请填写数字，如 100 或 99.9") from exc
    if not amt.is_finite():
        raise UserWalletError("金额格式有误：请填写有效的数字")
    try:
        amt = amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError) as exc:
        raise UserWalletError("金额格式有误：最多两位小数") from exc
    if amt <= 0:
        raise UserWalletError("金额必须大于 0")
    if max_amount is not None and amt > max_amount:
        raise UserWalletError(f"单笔金额不能超过 ¥{max_amount:,.2f}")
    return amt


def _require_user(user):
    if user is None or not getattr(user, "pk", None):
        raise UserWalletError("请先登录后再使用余额", code="unauthenticated")
    if not getattr(user, "is_active", True):
        raise UserWalletError("该账号已停用", code="inactive")
    return user


# ════════════════════════════════════════════════════════════════
# 读
# ════════════════════════════════════════════════════════════════
def get_wallet(user, *, create: bool = True):
    """取某个账号的钱包（默认不存在就建，余额 0）。

    惰性创建而不是在注册时预建：绝大多数账号一辈子不会充值，
    给每个注册用户插一行空钱包只是白占空间、还会让"有钱包"失去信息量。
    """
    _require_user(user)
    if create:
        wallet, _ = UserWallet.objects.get_or_create(
            user_id=user.pk, defaults={"balance": Decimal("0.00")},
        )
        return wallet
    return UserWallet.objects.filter(user_id=user.pk).first()


def wallet_balance(user) -> Decimal:
    """当前余额（不创建钱包，纯读数）。"""
    wallet = get_wallet(user, create=False)
    return (wallet.balance if wallet else Decimal("0.00")) or Decimal("0.00")


def list_transactions(user, limit: int = 50):
    """最近的流水（时间倒序），供个人中心列表展示。"""
    _require_user(user)
    qs = UserTransaction.objects.filter(wallet__user_id=user.pk).order_by("-created_at", "-id")
    if limit and limit > 0:
        return list(qs[:limit])
    return list(qs)


def has_paid_order(user, order_no: str) -> bool:
    """这个订单是否**已经**用余额扣过款。

    收银台（`/pay/<order_no>/`）被刷新、被重复提交、被两个标签页同时打开
    都是常态，扣款前必须先问这一句 —— 幂等判据放在业务层，
    而不是指望前端只点一次。
    """
    if not order_no:
        return False
    return UserTransaction.objects.filter(
        wallet__user_id=user.pk, kind=UserTxKind.PAY, order_no=order_no,
    ).exists()


def summarize(user) -> dict:
    """钱包概览：余额 + 三类累计 + 笔数（个人中心顶部的数字都从这里来）。"""
    _require_user(user)
    wallet = get_wallet(user)
    agg = UserTransaction.objects.filter(wallet_id=wallet.pk).aggregate(
        recharged=Sum("amount", filter=Q(kind=UserTxKind.RECHARGE)),
        paid=Sum("amount", filter=Q(kind=UserTxKind.PAY)),
        refunded=Sum("amount", filter=Q(kind=UserTxKind.REFUND)),
    )
    z = Decimal("0.00")
    return {
        "balance": wallet.balance or z,
        "recharged": agg["recharged"] or z,
        "paid": agg["paid"] or z,
        "refunded": agg["refunded"] or z,
        "tx_count": UserTransaction.objects.filter(wallet_id=wallet.pk).count(),
    }


def serialize_transaction(tx: UserTransaction) -> dict:
    """流水 → 前端 JSON（统一格式，视图层不再自己拼字典）。"""
    return {
        "id": tx.pk,
        "kind": tx.kind,
        "amount": str(tx.amount),
        "balance_after": str(tx.balance_after),
        "order_no": tx.order_no or "",
        "note": tx.note or "",
        "created_at": tx.created_at.strftime("%Y-%m-%d %H:%M:%S") if tx.created_at else "",
    }


# ════════════════════════════════════════════════════════════════
# 写（三条不变式的落点）
# ════════════════════════════════════════════════════════════════
@transaction.atomic
def _apply(
    user,
    kind: str,
    amount,
    *,
    order_no: str = "",
    note: str = "",
    allow_negative: bool = False,
    max_amount: Decimal = None,
) -> UserTransaction:
    """记账的唯一实现：加锁 → 算新余额 → 落库 → 写流水。

    所有对外的写函数都走这里，好处是"余额不为负"这类规则只写一遍，
    不会出现「充值做了检查、退款漏了检查」这种不对称漏洞。
    """
    _require_user(user)
    amt = clean_amount(amount, max_amount=max_amount)

    wallet = get_wallet(user)
    # 重新对钱包行加锁：并发请求（连点两次支付 / 两个标签页同时提交）
    # 必须串行化，否则两条流水会基于同一个旧余额各算一次。
    wallet = UserWallet.objects.select_for_update().get(pk=wallet.pk)

    delta = amt if kind in (UserTxKind.RECHARGE, UserTxKind.REFUND) else -amt
    new_balance = (wallet.balance or Decimal("0.00")) + delta
    if new_balance < 0 and not allow_negative:
        raise UserWalletError(
            f"余额不足：当前 ¥{wallet.balance or Decimal('0.00'):,.2f}，"
            f"本次需要 ¥{amt:,.2f}。请先充值。",
            code="insufficient",
        )

    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])

    return UserTransaction.objects.create(
        wallet=wallet,
        kind=kind,
        amount=amt,
        balance_after=new_balance,
        order_no=(order_no or "")[:MAX_ORDER_NO_LEN],
        note=(note or "")[:MAX_NOTE_LEN],
    )


def recharge(user, amount, *, note: str = "") -> UserTransaction:
    """充值（模拟收银台确认收款后调用）。"""
    return _apply(
        user, UserTxKind.RECHARGE, amount,
        note=note or "余额充值", max_amount=MAX_TOPUP,
    )


def pay(user, amount, *, order_no: str = "", note: str = "") -> UserTransaction:
    """余额支付扣款。余额不足抛 `UserWalletError(code='insufficient')`。"""
    return _apply(
        user, UserTxKind.PAY, amount,
        order_no=order_no,
        note=note or (f"订单 {order_no} 余额支付" if order_no else "余额支付"),
    )


def refund(user, amount, *, order_no: str = "", note: str = "", allow_negative: bool = False) -> UserTransaction:
    """退款回流（退货通过 / 订单取消时把钱退回余额）。"""
    return _apply(
        user, UserTxKind.REFUND, amount,
        order_no=order_no,
        note=note or (f"订单 {order_no} 退款退回余额" if order_no else "退款退回余额"),
        allow_negative=allow_negative,
    )


@transaction.atomic
def refund_order_if_paid(order, *, note: str = "") -> Optional[UserTransaction]:
    """订单终结（取消 / 退货）时，把**当初从余额扣走的那笔钱**原路退回余额。

    为什么必须有这一步
    ------------------
    订单退款在公司账上只是一笔「支出 −100」（`services.record_order_refund`），
    而公司账是**经营账**：它记的是"商家退掉了 100 元"，
    并不代表用户手里多出 100 元。用户当初若用余额付款，钱是从他自己的
    预存账里扣的 —— 只有把退款流水写回**用户钱包**，这 100 元才真的回到他账上。
    两本账互不代表：缺了这一步，钱出了用户账就再也回不来（真发生过的坑）。

    为什么判定全部基于流水、而不是订单字段
    --------------------------------------
    · **付过没有** → 查 `UserTransaction(kind=支付, order_no=…)`。
      用 `order.payment_status` 判会错：扫码支付的订单同样是「已支付」，
      但它没有用户余额流水，本来就没有钱可退（那笔走渠道原路退回，本项目不模拟）。
    · **退给谁**   → 按**支付流水所属的钱包**倒查，不读 `order.user_id`。
      凭手机号下单的订单 `user_id` 可能为空，只有流水能回答"钱从哪个钱包出去的"。
    · **退多少**   → 该订单在该钱包下所有 PAY 流水之和（**实付额**），
      不读 `order.total_price`：订单总价是"应付"、流水才是"实付"，
      两者万一不一致时以实付为准，绝不退多。

    幂等：同一订单已经有过退款流水就直接返回 None。
    收银台会被刷新、后台可能连点两次、状态机可能被人工扭转 ——
    幂等判据放在业务层，而不是指望调用方只调一次。

    :return: 写出的退款流水；返回 None 的三种情况都属"本来就不该退"，不是失败 ——
             订单号为空 / 这单没走过余额支付 / 已经退过了。
    """
    order_no = str(getattr(order, "order_no", "") or "").strip()
    if not order_no:
        return None

    # ① 已退过 → 不再退第二次
    if UserTransaction.objects.filter(
        kind=UserTxKind.REFUND, order_no=order_no,
    ).exists():
        return None

    # ② 定位"当初被扣钱的那个钱包"
    paid = (
        UserTransaction.objects
        .filter(kind=UserTxKind.PAY, order_no=order_no)
        .select_related("wallet__user")
        .order_by("id")
        .first()
    )
    if paid is None or paid.wallet_id is None or getattr(paid, "wallet", None) is None:
        return None
    user = getattr(paid.wallet, "user", None)
    if user is None or not getattr(user, "pk", None):
        return None

    # ③ 退实付额（同一钱包、同一订单的 PAY 之和）
    amount = (
        UserTransaction.objects
        .filter(wallet_id=paid.wallet_id, kind=UserTxKind.PAY, order_no=order_no)
        .aggregate(s=Sum("amount"))
    )["s"] or Decimal("0.00")
    if amount <= 0:
        return None

    return refund(
        user, amount, order_no=order_no,
        note=note or f"订单 {order_no} 退款，余额原路退回",
    )


@transaction.atomic
def adjust(user, amount, *, direction: str = "in", note: str = "") -> UserTransaction:
    """管理端纠错：按 `direction` 加钱或减钱，**允许把余额减成负数**。

    只在流水对不上、需要人工介入时使用；备注必须写清原因（视图层强制非空）。
    """
    kind = UserTxKind.RECHARGE if direction == "in" else UserTxKind.PAY
    return _apply(
        user, kind, amount,
        note=note or "管理端调账", allow_negative=True,
    )
