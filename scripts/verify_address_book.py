"""
契约验证：收货地址簿 —— ZT-agent

覆盖「地址簿」这条新增链路与个人中心接口、下单页预填口径的一致性：

  1. **鉴权**：未登录访问列表 / 新增 / 编辑 / 删除 / 设默认 一律 401
  2. 新增：首条**自动成为默认**（否则用户新填一条、下单页却带不出来）
  3. 新增：显式 `is_default=true` 抢占默认，旧默认被清掉
  4. 参数校验：收货人空 / 电话非数字或长度越界 / 地址空 / 超长 → 400，且不落库
  5. **归属隔离**：他人地址在 编辑 / 删除 / 设默认 三处都是 404（不泄露存在性），
     列表里也看不到他人的地址
  6. 设默认：幂等（本来就是默认不报错）；任意时刻每个账号**至多一条默认**
  7. 删除：删默认 → 自动把剩下最新的一条补为默认；删非默认 → 默认不变
  8. 条数上限：超过 MAX_PER_USER → 400
  9. 列表排序：默认地址排最前
 10. 个人中心 `/api/me/profile/` 下发 `default_address` 与 `address_count`
     （下单页靠它预填，不必再手填一遍）
 11. 序列化**不输出 user_id**（地址是私密数据，前端也不需要）
 12. 数据库层：`user_address` 表 / `idx_addr_user_default` 索引 /
     `user_id → auth_user` 外键真的存在

自造场景 → 断言 → 精确还原：脚本自建两个临时用户，结束时把地址与用户全部删除
（不触碰任何既有账号的数据，也不动钱包）。

用法：./.venv/Scripts/python.exe scripts/verify_address_book.py
"""
import json
import os
import sys
import uuid

import django

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402

from agent.addresses import MAX_PER_USER, default_address  # noqa: E402
from agent.models import Address  # noqa: E402

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


def ensure_test_host():
    if "testserver" not in settings.ALLOWED_HOSTS:
        settings.ALLOWED_HOSTS.append("testserver")


def main():
    ensure_test_host()
    User = get_user_model()
    suffix = uuid.uuid4().hex[:8]

    me = User.objects.create_user(username=f"addr_{suffix}", password="AddrTest!23456")
    other = User.objects.create_user(username=f"addr_o_{suffix}", password="AddrTest!23456")

    c = Client()
    auth = bearer(me)
    auth_other = bearer(other)

    def cleanup():
        Address.objects.filter(user_id__in=[me.pk, other.pk]).delete()
        # §10 里的临时账号（正常路径下已被 delete 掉，异常路径下兜底清理）
        User.objects.filter(username=f"addr_v_{suffix}").delete()
        User.objects.filter(pk__in=[me.pk, other.pk]).delete()
        print("\n    (临时用户与其地址已清理)")

    def defaults_of(user):
        return Address.objects.filter(user=user, is_default=True).count()

    try:
        # ════════════════════════════════════════════════════════
        print("\n【1】鉴权：未登录一律 401")
        # ════════════════════════════════════════════════════════
        for method, url, payload in (
            ("get", "/api/me/addresses/", None),
            ("post", "/api/me/addresses/", {"receiver": "张三", "phone": "13800000000", "address": "上海市浦东新区 1 号"}),
            ("patch", "/api/me/addresses/1/", {"receiver": "李四"}),
            ("delete", "/api/me/addresses/1/", None),
            ("post", "/api/me/addresses/1/default/", None),
        ):
            st, _ = jreq(c, method, url, payload)
            check(f"未登录 {method.upper()} {url} → 401", st == 401, str(st))
        check("未登录时没有产生任何地址", Address.objects.filter(user=me).count() == 0)

        # ════════════════════════════════════════════════════════
        print("\n【2】新增：首条自动成为默认")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "汤一条", "phone": "13800000001", "address": "上海市浦东新区临港大道 1 号",
        }, auth)
        check("新增成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        a1 = d.get("address") or {}
        check("首条自动 is_default=True", a1.get("is_default") is True, str(a1.get("is_default")))
        check("提示语说明已设为默认", "默认" in (d.get("message") or ""), str(d.get("message")))
        check("返回体带全量列表", len(d.get("addresses") or []) == 1, str(d.get("count")))
        check("default_id 指向首条", d.get("default_id") == a1.get("id"), str(d.get("default_id")))
        check("max 已下发", d.get("max") == MAX_PER_USER, str(d.get("max")))
        check("summary 拼好了（收货人+电话+地址）",
              a1.get("summary") == "汤一条 13800000001 上海市浦东新区临港大道 1 号", str(a1.get("summary")))
        check("序列化不含 user_id", "user_id" not in a1 and "user" not in a1, str(sorted(a1)))
        check("数据库里恰有 1 条默认", defaults_of(me) == 1, str(defaults_of(me)))

        # 第二条：默认 false
        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "汤二条", "phone": "13800000002", "address": "江苏省苏州市工业园区星湖街 2 号",
        }, auth)
        a2 = d.get("address") or {}
        check("第二条默认 is_default=False", a2.get("is_default") is False, str(a2.get("is_default")))
        check("两条并存，仍只有 1 条默认", Address.objects.filter(user=me).count() == 2 and defaults_of(me) == 1)
        check("列表默认在前", (d.get("addresses") or [])[0]["id"] == a1["id"],
              str([x["is_default"] for x in d.get("addresses") or []]))

        # ════════════════════════════════════════════════════════
        print("\n【3】新增时显式抢占默认")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "汤三条", "phone": "13800000003",
            "address": "浙江省杭州市余杭区文一西路 3 号", "is_default": True,
        }, auth)
        a3 = d.get("address") or {}
        check("显式要求默认 → is_default=True", a3.get("is_default") is True, str(a3.get("is_default")))
        check("旧默认已被清掉（仍只有 1 条默认）", defaults_of(me) == 1, str(defaults_of(me)))
        check("旧默认 a1 现在是 False",
              Address.objects.get(pk=a1["id"]).is_default is False)
        check("字符串 'true' 也认", (d.get("default_id") == a3.get("id")), str(d.get("default_id")))

        # ════════════════════════════════════════════════════════
        print("\n【4】参数校验：400 且不落库")
        # ════════════════════════════════════════════════════════
        before = Address.objects.filter(user=me).count()
        bad_cases = [
            ({"receiver": "", "phone": "13800000004", "address": "上海市 X 路 4 号"}, "收货人"),
            ({"receiver": "王五", "phone": "", "address": "上海市 X 路 4 号"}, "电话"),
            ({"receiver": "王五", "phone": "138-0000", "address": "上海市 X 路 4 号"}, "电话"),
            ({"receiver": "王五", "phone": "123456", "address": "上海市 X 路 4 号"}, "电话"),
            ({"receiver": "王五", "phone": "13800000004", "address": "   "}, "地址"),
            ({"receiver": "王" * 51, "phone": "13800000004", "address": "上海市 X 路 4 号"}, "收货人"),
            ({"receiver": "王五", "phone": "13800000004", "address": "沪" * 256}, "地址"),
        ]
        for payload, label in bad_cases:
            st, d = jreq(c, "post", "/api/me/addresses/", payload, auth)
            check(f"非法{label} → 400", st == 400 and d.get("success") is False,
                  f"{st} {d.get('message')}")
        check("校验失败没有落库", Address.objects.filter(user=me).count() == before,
              f"{before} -> {Address.objects.filter(user=me).count()}")

        # PATCH 同样受校验
        st, d = jreq(c, "patch", f"/api/me/addresses/{a2['id']}/", {"phone": "abc"}, auth)
        check("PATCH 非法电话 → 400", st == 400, f"{st} {d.get('message')}")
        check("校验失败没有改库", Address.objects.get(pk=a2["id"]).phone == "13800000002")

        # ════════════════════════════════════════════════════════
        print("\n【5】归属隔离：他人的地址一律 404，列表互不可见")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "别人家", "phone": "13900000009", "address": "广东省深圳市南山区 9 号",
        }, auth_other)
        other_addr = (d.get("address") or {})

        st, d = jreq(c, "get", "/api/me/addresses/", None, auth_other)
        check("他人列表只有自己的 1 条", d.get("count") == 1, str(d.get("count")))

        st, d = jreq(c, "get", "/api/me/addresses/", None, auth)
        check("我的列表看不到他人地址",
              all(x["receiver"] != "别人家" for x in (d.get("addresses") or [])),
              str([x["receiver"] for x in d.get("addresses") or []]))

        st, d = jreq(c, "patch", f"/api/me/addresses/{other_addr['id']}/", {"receiver": "越权改"}, auth)
        check("编辑他人地址 → 404", st == 404, str(st))
        st, d = jreq(c, "delete", f"/api/me/addresses/{other_addr['id']}/", None, auth)
        check("删除他人地址 → 404", st == 404, str(st))
        st, d = jreq(c, "post", f"/api/me/addresses/{other_addr['id']}/default/", None, auth)
        check("设他人地址为默认 → 404", st == 404, str(st))
        check("越权操作后他人地址毫发无损",
              Address.objects.get(pk=other_addr["id"]).receiver == "别人家")

        st, d = jreq(c, "post", "/api/me/addresses/99999999/default/", None, auth)
        check("不存在的 id → 404", st == 404, str(st))

        # ════════════════════════════════════════════════════════
        print("\n【6】设为默认：幂等 + 唯一性")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "post", f"/api/me/addresses/{a1['id']}/default/", None, auth)
        check("切换默认成功", st == 200 and d.get("success"), f"{st} {d.get('message')}")
        check("切换后 a1 是默认", Address.objects.get(pk=a1["id"]).is_default is True)
        check("切换后 a3 不再是默认", Address.objects.get(pk=a3["id"]).is_default is False)
        check("切换后仍只有 1 条默认", defaults_of(me) == 1, str(defaults_of(me)))
        check("列表第一条就是新的默认", (d.get("addresses") or [])[0]["id"] == a1["id"])

        st, d = jreq(c, "post", f"/api/me/addresses/{a1['id']}/default/", None, auth)
        check("重复设为默认幂等（仍 200）", st == 200 and d.get("success"), str(st))
        check("重复设置后仍只有 1 条默认", defaults_of(me) == 1, str(defaults_of(me)))

        # PATCH is_default=true 与专用接口同效
        st, d = jreq(c, "patch", f"/api/me/addresses/{a2['id']}/", {"is_default": True}, auth)
        check("PATCH is_default=true 也能切默认", st == 200 and d.get("success"), str(st))
        check("切换后 a2 是默认", Address.objects.get(pk=a2["id"]).is_default is True)
        check("PATCH 切默认后仍只有 1 条", defaults_of(me) == 1, str(defaults_of(me)))

        # PATCH is_default=false 被忽略（编辑表单整份回传不该把默认清掉）
        st, d = jreq(c, "patch", f"/api/me/addresses/{a2['id']}/",
                     {"receiver": "汤二条改", "is_default": False}, auth)
        check("PATCH is_default=false 被忽略（默认还在）",
              st == 200 and Address.objects.get(pk=a2["id"]).is_default is True, str(st))
        check("同一次 PATCH 的其它字段照常生效",
              Address.objects.get(pk=a2["id"]).receiver == "汤二条改")
        check("仍只有 1 条默认", defaults_of(me) == 1, str(defaults_of(me)))

        # ════════════════════════════════════════════════════════
        print("\n【7】删除：默认自动补位 / 非默认不动默认")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "delete", f"/api/me/addresses/{a3['id']}/", None, auth)  # a3 非默认
        check("删非默认 → 成功", st == 200 and d.get("success"), str(st))
        check("删非默认后默认不变（还是 a2）",
              Address.objects.get(pk=a2["id"]).is_default is True)
        check("删非默认不返回新默认", d.get("new_default_id") is None, str(d.get("new_default_id")))

        st, d = jreq(c, "delete", f"/api/me/addresses/{a1['id']}/", None, auth)  # a1 非默认，剩 a2
        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "汤四条", "phone": "13800000004", "address": "北京市朝阳区建国路 4 号",
        }, auth)
        a4 = d.get("address") or {}

        st, d = jreq(c, "delete", f"/api/me/addresses/{a2['id']}/", None, auth)  # a2 是默认
        check("删默认 → 成功", st == 200 and d.get("success"), str(st))
        check("删默认后自动补位", d.get("new_default_id") == a4["id"], str(d.get("new_default_id")))
        check("补位后 a4 是默认且唯一", Address.objects.get(pk=a4["id"]).is_default is True
              and defaults_of(me) == 1, str(defaults_of(me)))
        check("提示语说明了补位", "默认" in (d.get("message") or ""), str(d.get("message")))

        # 删掉最后一条 → 没有默认，也不该报错
        st, d = jreq(c, "delete", f"/api/me/addresses/{a4['id']}/", None, auth)
        check("删掉最后一条 → 成功", st == 200 and d.get("success"), str(st))
        check("地址清空后 new_default_id 为空", d.get("new_default_id") is None, str(d.get("new_default_id")))
        check("地址清空后 default_address 返回 None", default_address(me) is None)

        # ════════════════════════════════════════════════════════
        print("\n【8】条数上限")
        # ════════════════════════════════════════════════════════
        for i in range(MAX_PER_USER):
            st, d = jreq(c, "post", "/api/me/addresses/", {
                "receiver": f"上限{i}", "phone": "13800000005", "address": f"上海市测试路 {i} 号",
            }, auth)
            assert st == 200, f"第 {i + 1} 条就失败了：{st} {d}"
        check(f"恰好存满 {MAX_PER_USER} 条", Address.objects.filter(user=me).count() == MAX_PER_USER)

        st, d = jreq(c, "post", "/api/me/addresses/", {
            "receiver": "超一条", "phone": "13800000005", "address": "上海市测试路 99 号",
        }, auth)
        check("超上限 → 400", st == 400, str(st))
        check("提示语给出上限数字", str(MAX_PER_USER) in (d.get("message") or ""), str(d.get("message")))
        check("超上限没有落库", Address.objects.filter(user=me).count() == MAX_PER_USER)

        # ════════════════════════════════════════════════════════
        print("\n【9】个人中心下发默认地址（下单页靠它预填）")
        # ════════════════════════════════════════════════════════
        st, d = jreq(c, "get", "/api/me/profile/", None, auth)
        check("profile 200", st == 200 and d.get("success"), str(st))
        check("下发 address_count", d.get("address_count") == MAX_PER_USER, str(d.get("address_count")))
        da = d.get("default_address") or {}
        check("下发 default_address", bool(da), str(da)[:80])
        check("default_address 与库内默认一致",
              da.get("id") == Address.objects.get(user=me, is_default=True).pk, str(da.get("id")))
        check("default_address 带 summary（前端直接填表）", bool(da.get("summary")), str(da.get("summary")))

        # 清掉地址后 profile 应给 null，而不是报错
        Address.objects.filter(user=me).delete()
        st, d = jreq(c, "get", "/api/me/profile/", None, auth)
        check("无地址时 default_address 为 null 且不报错",
              st == 200 and d.get("default_address") is None and d.get("address_count") == 0,
              f"{st} {d.get('default_address')}")

        # ════════════════════════════════════════════════════════
        print("\n【10】数据库层：表 / 索引 / 外键真的存在")
        # ════════════════════════════════════════════════════════
        with connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_address'"
            )
            check("user_address 表存在", cur.fetchone()[0] == 1)

            cur.execute(
                "SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_address' "
                "AND INDEX_NAME='idx_addr_user_default'"
            )
            check("idx_addr_user_default 复合索引存在", cur.fetchone()[0] == 1)

            cur.execute(
                "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='user_address' "
                "AND COLUMN_NAME='user_id' AND REFERENCED_TABLE_NAME='auth_user'"
            )
            check("user_id → auth_user 外键存在", cur.fetchone()[0] == 1)

            # ⚠️ 这里**不能**断言 DB 层的 DELETE_RULE='CASCADE'：
            #   Django 把 on_delete 实现在 **ORM 层**，建外键时不生成 ON DELETE 子句
            #   （所以查出来是 NO ACTION）。本仓库里 `orders_user_fk` 带
            #   ON DELETE SET NULL，是因为它由 0011 的**手写 DDL** 创建，走的不是
            #   Django 的建表路径 —— 两者不是一回事，别拿它推断 Django 的行为。
            #   所以级联要用**真实行为**验证：删账号后地址是否随之消失。
            cur.execute(
                "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME='user_address'"
            )
            row = cur.fetchone()
            check("外键确实由 Django 创建（无 ON DELETE 子句）",
                  row and row[0] in ("NO ACTION", "RESTRICT"), str(row))

        # 真实行为：ORM 删账号 → 地址被带走（不留孤儿行）
        victim = User.objects.create_user(username=f"addr_v_{suffix}", password="AddrTest!23456")
        Address.objects.create(user=victim, receiver="要被带走的",
                               phone="13800000006", address="上海市测试路 6 号", is_default=True)
        victim_id = victim.pk                       # delete() 之后 pk 会被置空，先留一份
        others_before = Address.objects.filter(user_id__in=[me.pk, other.pk]).count()

        victim.delete()

        check("ORM 删账号 → 其地址被级联删除（ORM 层 CASCADE 生效）",
              Address.objects.filter(user_id=victim_id).count() == 0)
        check("删账号没有误伤别人的地址",
              Address.objects.filter(user_id__in=[me.pk, other.pk]).count() == others_before,
              f"{others_before} -> {Address.objects.filter(user_id__in=[me.pk, other.pk]).count()}")

    finally:
        cleanup()

    print("\n" + "═" * 62)
    print(f"  通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
