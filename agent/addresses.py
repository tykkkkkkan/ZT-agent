"""
agent/addresses.py — 收货地址簿的业务逻辑（唯一出口）

职责边界
--------
· 本模块只管**地址簿本身**：能不能加、能否改、默认地址归谁；
· 不碰订单 —— 下单仍把地址**快照**写进 `orders.address`（见 agent/views.py），
  地址簿只负责"把上次填过的带出来"；
· 视图层（`me_views.MeAddress*`）只做鉴权与归属查询，规则全部收在这里。

三条硬规则
----------
1. **归属只认 `user_id`，绝不按手机号匹配**。
   与「我的订单」不同 —— 订单有历史包袱（当年还没有登录体系，只能靠手机号
   认领），地址簿没有。所以这里不引入手机号兜底：否则知道一个手机号就能
   读到别人的收货地址，这比读到订单更敏感（姓名 + 电话 + 门牌一次给全）。

2. **默认地址有且只有一个**。三条配套规则：
   · 首条新增自动成为默认 —— 否则用户刚填一条却发现下单页带不出来，很困惑；
   · 抢占默认时先清旧默认，保证任一时刻至多一条 `is_default`；
   · 删除默认地址后自动把剩下最新的一条补为默认，不允许出现"有地址但没默认"。
   唯一性由「事务 + `select_for_update`」兜底：MySQL 没有部分唯一索引，
   数据库层拦不住两条 `is_default=True`（见 `models.Address` 的注释）。

3. **对外只序列化必要字段，不带 `user_id`**。
   地址是私密数据，前端也不需要知道它挂在哪个账号下。
"""
from __future__ import annotations

import logging

from django.db import transaction

from agent.models import Address

logger = logging.getLogger(__name__)

# 一个账号最多存几条（够用即可，也防刷）
MAX_PER_USER = 20
MAX_RECEIVER_LEN = 50
MAX_PHONE_LEN = 20
MAX_ADDRESS_LEN = 255


class AddressError(Exception):
    """地址簿的业务校验失败（视图层据此返回 4xx）。

    `code` 约定：`invalid` 参数问题 / `not_found` 不存在或不属于当前账号 /
    `limit` 超出条数上限。
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# ════════════════════════════════════════════════════════════════
# 校验（唯一口径：视图层不再自己判长度）
# ════════════════════════════════════════════════════════════════
def _clean_receiver(value) -> str:
    text = (value or "").strip()
    if not text:
        raise AddressError("请填写收货人姓名")
    if len(text) > MAX_RECEIVER_LEN:
        raise AddressError(f"收货人姓名最长 {MAX_RECEIVER_LEN} 个字符")
    return text


def _clean_phone(value) -> str:
    """电话口径与个人中心改绑手机号、下单页**保持一致**（7~20 位纯数字）。

    刻意不放松成"随便填"：后端三处对同一份数据的判定一旦不同，
    就会出现"A 页面能存、B 页面报错"的错位，用户只会觉得系统坏了。
    """
    phone = (value or "").strip()
    if not phone:
        raise AddressError("请填写联系电话")
    if not (phone.isdigit() and 7 <= len(phone) <= MAX_PHONE_LEN):
        raise AddressError("联系电话格式有误：请输入 7~20 位数字")
    return phone


def _clean_address(value) -> str:
    """详细地址只要求非空 —— 与下单页同一个口径。

    不做长度下限是刻意的：改地址簿比写订单更随意，用户存一条"公司（前台代收）"
    也应当被接受；真要校验收货可行性，那是下单那一刻的事，不是存地址簿的时候。
    """
    text = (value or "").strip()
    if not text:
        raise AddressError("请填写详细地址")
    if len(text) > MAX_ADDRESS_LEN:
        raise AddressError(f"详细地址最长 {MAX_ADDRESS_LEN} 个字符")
    return text


# ════════════════════════════════════════════════════════════════
# 读
# ════════════════════════════════════════════════════════════════
def list_addresses(user) -> list:
    """该账号的全部地址（默认在前，由 Address.Meta.ordering 保证）。"""
    return list(Address.objects.filter(user=user))


def get_address(user, address_id: int) -> Address:
    """按归属取单条；不存在 / 不属于我 → 统一 `not_found`。

    与订单同一套做法：不返回 403 —— 那等于告诉对方"这条地址确实存在"。
    """
    obj = Address.objects.filter(user=user, pk=address_id).first()
    if obj is None:
        raise AddressError("地址不存在或不属于当前账号", code="not_found")
    return obj


def default_address(user):
    """当前默认地址；一条都没有时返回 None（下单页预填用）。

    兜底：若因历史数据出现"有地址但没有默认"，退而取最新一条 ——
    宁可带出一条不完全正确的地址，也不要让用户面对空表单。
    """
    return (
        Address.objects.filter(user=user, is_default=True).order_by("-updated_at", "-id").first()
        or Address.objects.filter(user=user).order_by("-updated_at", "-id").first()
    )


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def serialize_address(a: Address) -> dict:
    return {
        "id": a.id,
        "receiver": a.receiver,
        "phone": a.phone,
        "address": a.address,
        "is_default": bool(a.is_default),
        # 前端下拉框直接用这一行，省得各处自己拼（避免有的地方漏电话）
        "summary": a.summary,
        "created_at": _fmt(a.created_at),
        "updated_at": _fmt(a.updated_at),
    }


def _locked_of(user) -> list:
    """在事务内锁住该账号的全部地址行（唯一性检查与抢占默认都基于它）。

    为什么要先全取再加锁，而不是"查一下有没有默认，没有就设"：
    后者在并发下两次请求会双双通过检查，落成两条默认地址。
    MySQL 没有部分唯一索引兜底，只能靠这把行锁。
    """
    return list(Address.objects.select_for_update().filter(user=user))


# ════════════════════════════════════════════════════════════════
# 写
# ════════════════════════════════════════════════════════════════
@transaction.atomic
def create_address(user, receiver, phone, address, *, is_default: bool = False) -> Address:
    """新增一条地址。

    `is_default` 缺省 False，但**首条自动成为默认**（见模块 docstring 规则 2）。
    显式要求默认时，同事务内先把旧默认清掉。
    """
    receiver = _clean_receiver(receiver)
    phone = _clean_phone(phone)
    address = _clean_address(address)

    locked = _locked_of(user)
    if len(locked) >= MAX_PER_USER:
        raise AddressError(
            f"最多保存 {MAX_PER_USER} 条收货地址，请先删除不再使用的", code="limit",
        )

    make_default = bool(is_default) or not locked
    if make_default:
        Address.objects.filter(user=user, is_default=True).update(is_default=False)

    obj = Address.objects.create(
        user=user, receiver=receiver, phone=phone,
        address=address, is_default=make_default,
    )
    logger.info("地址簿新增：user=%s address=%s default=%s", user.id, obj.id, make_default)
    return obj


@transaction.atomic
def update_address(
    user, address_id: int, *,
    receiver=None, phone=None, address=None, is_default=None,
) -> Address:
    """编辑一条地址。

    · 只更新**显式传入**的字段（None = 不改），避免前端只提交一半时把别的清空；
    · `is_default=False` 会被**忽略**：默认地址只能切换、不能取消。
      前端编辑表单常把整份对象回传（含 is_default=false），若按字面处理，
      用户改一下电话就把默认地址改没了 —— 所以这里以"设默认"接口为唯一入口。
    """
    locked = _locked_of(user)
    obj = next((a for a in locked if a.pk == address_id), None)
    if obj is None:
        raise AddressError("地址不存在或不属于当前账号", code="not_found")

    changed = []
    if receiver is not None:
        obj.receiver = _clean_receiver(receiver)
        changed.append("receiver")
    if phone is not None:
        obj.phone = _clean_phone(phone)
        changed.append("phone")
    if address is not None:
        obj.address = _clean_address(address)
        changed.append("address")

    if changed:
        obj.save(update_fields=changed + ["updated_at"])
        logger.info("地址簿更新：user=%s address=%s fields=%s", user.id, obj.id, changed)

    if is_default:
        # 提前返回，让 set_default 负责清旧默认
        return set_default(user, address_id)
    return obj


@transaction.atomic
def set_default(user, address_id: int) -> Address:
    """把某条地址设为默认（同事务内取消其它默认）。幂等：本来就是默认则原样返回。"""
    locked = _locked_of(user)
    obj = next((a for a in locked if a.pk == address_id), None)
    if obj is None:
        raise AddressError("地址不存在或不属于当前账号", code="not_found")

    if not obj.is_default:
        for other in locked:
            if other.is_default and other.pk != obj.pk:
                other.is_default = False
                other.save(update_fields=["is_default", "updated_at"])
        obj.is_default = True
        obj.save(update_fields=["is_default", "updated_at"])
        logger.info("地址簿切换默认：user=%s -> address=%s", user.id, obj.pk)
    return obj


@transaction.atomic
def delete_address(user, address_id: int) -> dict:
    """删除一条地址；若删的正是默认地址，自动把剩下最新的一条补为默认。

    :returns: `{"deleted_id": int, "new_default_id": int|None}`
    """
    locked = _locked_of(user)
    obj = next((a for a in locked if a.pk == address_id), None)
    if obj is None:
        raise AddressError("地址不存在或不属于当前账号", code="not_found")

    was_default = bool(obj.is_default)
    deleted_id = obj.pk
    obj.delete()

    new_default_id = None
    if was_default:
        heir = Address.objects.filter(user=user).order_by("-updated_at", "-id").first()
        if heir is not None:
            heir.is_default = True
            heir.save(update_fields=["is_default", "updated_at"])
            new_default_id = heir.pk

    logger.info("地址簿删除：user=%s address=%s（原默认=%s，新默认=%s）",
                user.id, deleted_id, was_default, new_default_id)
    return {"deleted_id": deleted_id, "new_default_id": new_default_id}
