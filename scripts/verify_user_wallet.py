"""
契约验证：用户侧钱包（C 端余额）—— ZT-agent

覆盖「余额」这条链路的**账本正确性**与接口形状：

  1. **表结构**：`user_wallet` / `user_wallet_transactions` 两张表真的建了；
     `user_id` 有 UNIQUE（"一人一本"必须由数据库保证，不能只靠应用层）；
     索引与外键都在；两张表与公司钱包 `company_wallet` 完全分离
  2. **鉴权**：未登录访问 `/api/me/wallet/` 与充值接口一律 401
  3. **充值**：余额增加 + 落一条 `充值` 流水，`balance_after` 正确
  4. **支付**：余额减少；**余额不足时明确拒绝，且不留半条流水**（原子性）
  5. **退款冲洗**：余额回增；同一订单的支付与退款都能按 `order_no` 查到
  6. **金额校验**：空 / 非数字 / `NaN` / `Infinity` / 0 / 负数 / 超上限
     → 全部拒绝，且**一条流水都不落**
  7. **账本对账**：按时间串起流水逐条验算
     （上一条 `balance_after` ± 本条 `amount` == 本条 `balance_after`），
     并校验 累计充值 − 累计支付 + 累计退款 == 当前余额
  8. **幂等判据**：`has_paid_order` 能识别"这个订单已经扣过款"
  9. **不许透支**：超额支付被拒；管理端纠错（`adjust`）才允许负余额
 10. **账号隔离**：A 的钱包与流水，B 既看不到也改不动
 11. **不动公司账**：写用户钱包**不会**改变 `company_wallet` 的余额
 12. **级联**：ORM 删账号 → 钱包与流水一起消失（不靠 DB 的 ON DELETE）

自造场景 → 断言 → 精确还原：脚本自建两个临时用户，结束时连同钱包、流水、
用户一起删除；不触碰任何既有账号的数据。

用法：./.venv/Scripts/python.exe scripts/verify_user_wallet.py
"""
import json
import os
import sys
import uuid
from decimal import Decimal

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402

from agent.models import Transaction, UserTransaction, UserTxKind, UserWallet, Wallet  # noqa: E402
from agent.user_wallet import (  # noqa: E402
    MAX_TOPUP,
    UserWalletError,
    clean_amount,
    has_paid_order,
    list_transactions,
    pay,
    recharge,
    refund,
    summarize,
    wallet_balance,
)

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  ← {detail}" if detail else ""))


def bearer(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def jreq(c, method, url, payload=None, auth=None):
    fn = getattr(c, method)
    if payload is None:
        r = fn(url, **(auth or {}))
    else:
        r = fn(url, data=json.dumps(payload), content_type="application/json", **(auth or {}))
    try:
        return r.status_code, json.loads(r.content)
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def expect_error(fn, *a, **kw):
    """调用应抛 UserWalletError；返回 (code, message) 或 (None, None)。"""
    try:
        fn(*a, **kw)
        return None, None
    except UserWalletError as e:
        return e.code, e.message


def main():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")

    User = get_user_model()
    sfx = uuid.uuid4().hex[:8]
    me = User.objects.create_user(username=f"uw_{sfx}", password="WalletTest!23456")
    other = User.objects.create_user(username=f"uw_o_{sfx}", password="WalletTest!23456")
    c = Client()
    auth = bearer(me)
    auth_other = bearer(other)

    def cleanup():
        UserWallet.objects.filter(user_id__in=[me.pk, other.pk]).delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户与其钱包、流水已清理)")

    def txs(user=None):
        return list(UserTransaction.objects.filter(wallet__user_id=(user or me).pk)
                    .order_by("created_at", "id"))

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】表结构：两张表 / UNIQUE / 索引 / 外键")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
                "('user_wallet','user_wallet_transactions')"
            )
            tables = {r[0] for r in cur.fetchall()}
        check("user_wallet 表存在", "user_wallet" in tables)
        check("user_wallet_transactions 表存在", "user_wallet_transactions" in tables)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_wallet' "
                "AND COLUMN_NAME='user_id'"
            )
            rows = cur.fetchall()
        uniq = [r for r in rows if r[1] == 0]
        check("user_wallet.user_id 唯一（一人一本由数据库保证）", len(uniq) >= 1,
              f"{[r[0] for r in rows]}")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_wallet_transactions'"
            )
            idx = {r[0] for r in cur.fetchall()}
        check("流水表有 wallet+时间 复合索引", "idx_utx_wallet_time" in idx, str(sorted(idx)))
        check("流水表有 kind+时间 复合索引", "idx_utx_kind_time" in idx)

        with connection.cursor() as cur:
            cur.execute(
                "SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_NAME, "
                "r.DELETE_RULE FROM information_schema.KEY_COLUMN_USAGE k "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
                "  ON r.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
                " AND r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA "
                "WHERE k.TABLE_SCHEMA=DATABASE() AND k.TABLE_NAME LIKE 'user_wallet%'"
            )
            fks = {(r[1], r[2]): r[3] for r in cur.fetchall()}
        check("user_wallet.user_id → auth_user 外键存在", ("user_id", "auth_user") in fks, str(list(fks)))
        check("流水的 wallet_id → user_wallet 外键存在", ("wallet_id", "user_wallet") in fks)
        # Django 的 on_delete 实现在 ORM 层，DB 层不带 ON DELETE（见 §12 用真实行为验证）
        check("DB 层不写 ON DELETE（真正的级联在 ORM 层，§12 验证）",
              all(v in ("NO ACTION", "RESTRICT") for v in fks.values()),
              str(set(fks.values())))

        check("用户钱包与公司钱包是两张不同的表",
              UserWallet._meta.db_table == "user_wallet"
              and Wallet._meta.db_table == "company_wallet")

        # ════════════════════════════════════════════════════════
        print("\n【2】鉴权：未登录一律 401")
        # ════════════════════════════════════════════════════════
        for method, url, payload in (
            ("get", "/api/me/wallet/", None),
            ("post", "/api/me/wallet/recharge/", {"amount": 100}),
        ):
            st, body = jreq(c, method, url, payload)
            check(f"未登录 {method.upper()} {url} → 401", st == 401, f"实际 {st}")

        # ════════════════════════════════════════════════════════
        print("\n【3】充值：余额增加 + 落一条「充值」流水")
        # ════════════════════════════════════════════════════════
        check("初始余额为 0", wallet_balance(me) == Decimal("0.00"), str(wallet_balance(me)))
        tx1 = recharge(me, "100")
        check("充值 100 后余额为 100.00", wallet_balance(me) == Decimal("100.00"), str(wallet_balance(me)))
        check("流水的 kind = 充值", tx1.kind == UserTxKind.RECHARGE, tx1.kind)
        check("流水金额是正数 100.00", tx1.amount == Decimal("100.00"), str(tx1.amount))
        check("流水的 balance_after = 100.00", tx1.balance_after == Decimal("100.00"), str(tx1.balance_after))
        check("充值备注有默认值（不是空串）", bool(tx1.note), tx1.note or "(空)")

        tx2 = recharge(me, 50.5)
        check("充值 50.5 后余额为 150.50", wallet_balance(me) == Decimal("150.50"), str(wallet_balance(me)))
        check("第二条流水的 balance_after = 150.50", tx2.balance_after == Decimal("150.50"), str(tx2.balance_after))
        check("钱包行被复用（没有重复建钱包）",
              UserWallet.objects.filter(user_id=me.pk).count() == 1)

        # ════════════════════════════════════════════════════════
        print("\n【4】支付：余额减少；余额不足时明确拒绝且不留半条流水")
        # ════════════════════════════════════════════════════════
        n_before = len(txs())
        tx3 = pay(me, "30", order_no="ZT-TEST-A1")
        check("支付 30 后余额为 120.50", wallet_balance(me) == Decimal("120.50"), str(wallet_balance(me)))
        check("流水的 kind = 支付", tx3.kind == UserTxKind.PAY, tx3.kind)
        check("流水记下了订单号", tx3.order_no == "ZT-TEST-A1", tx3.order_no)
        check("流水条数 +1", len(txs()) == n_before + 1, f"{n_before} → {len(txs())}")

        bal_before = wallet_balance(me)
        count_before = len(txs())
        code, msg = expect_error(pay, me, "999999.99", order_no="ZT-TEST-A2")
        check("余额不足时报错 code=insufficient", code == "insufficient", str(code))
        check("余额不足的提示里带具体数字（用户知道差多少）",
              "余额不足" in (msg or "") and "999,999.99" in (msg or ""), msg)
        check("余额不足时余额**没有**被动过", wallet_balance(me) == bal_before, str(wallet_balance(me)))
        check("余额不足时**不落**任何流水（原子性）", len(txs()) == count_before,
              f"{count_before} → {len(txs())}")

        # ════════════════════════════════════════════════════════
        print("\n【5】退款回流：余额回增，可按订单号追到进出的两条流水")
        # ════════════════════════════════════════════════════════
        tx4 = refund(me, "30", order_no="ZT-TEST-A1")
        check("退款 30 后余额回到 150.50", wallet_balance(me) == Decimal("150.50"), str(wallet_balance(me)))
        check("流水的 kind = 退款", tx4.kind == UserTxKind.REFUND, tx4.kind)
        pair = UserTransaction.objects.filter(wallet__user_id=me.pk, order_no="ZT-TEST-A1")
        check("同一订单的「支付 + 退款」两条都能按订单号查到", pair.count() == 2, str(pair.count()))
        check("这两条方向相反（支付 − / 退款 +）",
              {t.kind for t in pair} == {UserTxKind.PAY, UserTxKind.REFUND})

        # ════════════════════════════════════════════════════════
        print("\n【6】金额校验：7 类非法输入一律拒绝，且一条流水都不落")
        # ════════════════════════════════════════════════════════
        bad_cases = [
            ("空字符串", ""),
            ("None", None),
            ("纯空格", "   "),
            ("非数字", "abc"),
            ("NaN", "NaN"),
            ("Infinity", "Infinity"),
            ("0", "0"),
            ("负数", "-10"),
            ("超过单笔上限", str(MAX_TOPUP + Decimal("0.01"))),
        ]
        for label, val in bad_cases:
            n0 = len(txs())
            b0 = wallet_balance(me)
            code, msg = expect_error(recharge, me, val)
            check(f"充值 {label} → 拒绝", code is not None, "" if code else "未被拒（居然成功了）")
            check(f"充值 {label} → 不落流水、不动余额",
                  len(txs()) == n0 and wallet_balance(me) == b0)

        check("上限本身可充（边界含端点）",
              clean_amount(str(MAX_TOPUP), max_amount=MAX_TOPUP) == MAX_TOPUP)
        check("恰好两位小数原样保留", clean_amount("12.34") == Decimal("12.34"))
        check("一位小数补零到两位", clean_amount("12.5") == Decimal("12.50"))
        check("三位小数四舍五入（100.005 → 100.01，不是银行家舍入的 100.00）",
              clean_amount("100.005") == Decimal("100.01"), str(clean_amount("100.005")))

        # ════════════════════════════════════════════════════════
        print("\n【7】账本对账：逐条验算 balance_after 链 + 总额恒等式")
        # ════════════════════════════════════════════════════════
        rows = txs()
        running = Decimal("0.00")
        chain_ok = True
        for t in rows:
            running += t.amount if t.kind in (UserTxKind.RECHARGE, UserTxKind.REFUND) else -t.amount
            if running != t.balance_after:
                chain_ok = False
                break
        check(f"逐条验算通过（{len(rows)} 条流水串起来 == 当前余额）", chain_ok,
              f"算到 {running} / 记录 {rows[-1].balance_after if rows else '-'}")
        check("链尾等于钱包余额", running == wallet_balance(me), f"{running} vs {wallet_balance(me)}")

        info = summarize(me)
        check("累计充值 = 150.50（100 + 50.5）", info["recharged"] == Decimal("150.50"), str(info["recharged"]))
        check("累计支付 = 30.00", info["paid"] == Decimal("30.00"), str(info["paid"]))
        check("累计退款 = 30.00", info["refunded"] == Decimal("30.00"), str(info["refunded"]))
        check("恒等式 充值 − 支付 + 退款 == 余额",
              info["recharged"] - info["paid"] + info["refunded"] == info["balance"],
              str(info["balance"]))
        check("笔数与流水行数一致", info["tx_count"] == len(rows), f"{info['tx_count']} vs {len(rows)}")
        check("list_transactions 默认倒序（最新在前）",
              [t.pk for t in list_transactions(me)][0] == rows[-1].pk)

        # ════════════════════════════════════════════════════════
        print("\n【8】幂等判据：has_paid_order")
        # ════════════════════════════════════════════════════════
        check("已扣过款的订单 → True", has_paid_order(me, "ZT-TEST-A1") is True)
        check("没扣过款的订单 → False", has_paid_order(me, "ZT-NOT-EXIST") is False)
        check("空订单号 → False（不会误判成「全都付过」）", has_paid_order(me, "") is False)

        # ════════════════════════════════════════════════════════
        print("\n【9】不许透支；管理端纠错才允许负余额")
        # ════════════════════════════════════════════════════════
        code, _ = expect_error(pay, me, wallet_balance(me) + Decimal("0.01"))
        check("超出余额 1 分钱也拒绝", code == "insufficient", str(code))
        pay(me, wallet_balance(me))
        check("刚好花完 → 余额 0.00（不为负）", wallet_balance(me) == Decimal("0.00"), str(wallet_balance(me)))

        from agent.user_wallet import adjust
        tx_adj = adjust(me, "5", direction="out", note="测试：管理端纠错")
        check("管理端调账可以把余额减成负数（显式允许）",
              wallet_balance(me) == Decimal("-5.00"), str(wallet_balance(me)))
        check("调账也照样落流水，balance_after = -5.00",
              tx_adj.balance_after == Decimal("-5.00"), str(tx_adj.balance_after))
        adjust(me, "5", direction="in", note="测试：把余额调回 0")
        check("调回后余额 0.00", wallet_balance(me) == Decimal("0.00"), str(wallet_balance(me)))

        # ════════════════════════════════════════════════════════
        print("\n【10】账号隔离：A 的钱包 B 看不到也改不动")
        # ════════════════════════════════════════════════════════
        recharge(other, "7")
        st, body = jreq(c, "get", "/api/me/wallet/", auth=auth)
        check("A 的接口只返回 A 的流水", st == 200 and all(
            t["order_no"] in ("ZT-TEST-A1", "", "ZT-TEST-ISO") for t in body.get("transactions", [])),
            f"st={st}")
        st_o, body_o = jreq(c, "get", "/api/me/wallet/", auth=auth_other)
        check("B 的余额是 7.00（看不到 A 的余额）", body_o.get("balance") == "7.00", str(body_o.get("balance")))
        check("B 的流水里没有 A 的订单号",
              all(t["order_no"] != "ZT-TEST-A1" for t in body_o.get("transactions", [])))
        check("A 的每一条流水都挂在 A 自己的钱包上",
              UserTransaction.objects.filter(wallet__user_id=me.pk).count() == len(txs()))

        # ════════════════════════════════════════════════════════
        print("\n【11】不动公司账：写用户钱包不影响 company_wallet")
        # ════════════════════════════════════════════════════════
        wallet_before = Wallet.get_solo().balance
        co_tx_before = Transaction.objects.count()
        recharge(me, "123.45")
        pay(me, "23.45", order_no="ZT-TEST-ISO")
        check("公司钱包余额不变", Wallet.get_solo().balance == wallet_before,
              f"{wallet_before} → {Wallet.get_solo().balance}")
        check("公司流水条数不变", Transaction.objects.count() == co_tx_before,
              f"{co_tx_before} → {Transaction.objects.count()}")

        # ════════════════════════════════════════════════════════
        print("\n【12】级联：ORM 删账号 → 钱包与流水一起消失")
        # ════════════════════════════════════════════════════════
        wallet_pk = UserWallet.objects.get(user_id=other.pk).pk
        co_txs = UserTransaction.objects.filter(wallet_id=wallet_pk).count()
        check("删之前 B 有钱包且有流水", co_txs >= 1, f"{co_txs} 条")
        other.delete()
        check("账号删除后钱包行消失（ORM 级联）", UserWallet.objects.filter(pk=wallet_pk).count() == 0)
        check("账号删除后流水也消失（二级级联）",
              UserTransaction.objects.filter(wallet_id=wallet_pk).count() == 0)

        # ════════════════════════════════════════════════════════
        print("\n【13】HTTP 形状：字段齐全、口径与业务层一致")
        # ════════════════════════════════════════════════════════
        st, body = jreq(c, "get", "/api/me/wallet/", auth=auth)
        check("GET /api/me/wallet/ 200", st == 200, f"st={st}")
        for k in ("success", "balance", "recharged", "paid", "refunded",
                  "tx_count", "topup_max", "transactions"):
            check(f"返回体含 {k}", k in body, str(sorted(body.keys())))
        check("金额一律是字符串（避免浮点误差进前端）",
              isinstance(body.get("balance"), str), type(body.get("balance")).__name__)
        check("topup_max 与业务层常量一致", body.get("topup_max") == str(MAX_TOPUP), str(body.get("topup_max")))
        check("接口余额 == 业务层余额",
              Decimal(body["balance"]) == wallet_balance(me), f"{body['balance']} vs {wallet_balance(me)}")
        first = (body.get("transactions") or [{}])[0]
        for k in ("id", "kind", "amount", "balance_after", "order_no", "note", "created_at"):
            check(f"流水项含 {k}", k in first, str(sorted(first.keys())))

        st, body = jreq(c, "get", "/api/me/wallet/?limit=1", auth=auth)
        check("limit=1 只返回 1 条流水", st == 200 and len(body.get("transactions", [])) == 1,
              str(len(body.get("transactions", []))))
        st, body = jreq(c, "get", "/api/me/wallet/?limit=abc", auth=auth)
        check("limit 非法时退回默认值而不是 500", st == 200, f"st={st}")

        st, body = jreq(c, "post", "/api/me/wallet/recharge/", {"amount": "20"}, auth=auth)
        check("POST 充值 200", st == 200, f"st={st} body={body.get('message')}")
        check("充值返回新的余额", body.get("balance") == str(wallet_balance(me)), str(body.get("balance")))
        check("充值返回刚生成的流水", "transaction" in body and body["transaction"]["kind"] == "充值")
        st, body = jreq(c, "post", "/api/me/wallet/recharge/", {"amount": "-1"}, auth=auth)
        check("充负数 → 400（不是 500）", st == 400, f"st={st}")
        st, body = jreq(c, "post", "/api/me/wallet/recharge/", {"amount": str(MAX_TOPUP + 1)}, auth=auth)
        check(f"超上限 {MAX_TOPUP} → 400", st == 400, f"st={st}")
        st, body = jreq(c, "post", "/api/me/wallet/recharge/", {}, auth=auth)
        check("不给金额 → 400", st == 400, f"st={st}")

    finally:
        cleanup()

    print("\n" + "=" * 62)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"    ✗ {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
