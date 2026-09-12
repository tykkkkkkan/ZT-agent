"""
agent/me_helpers.py — 个人中心（C 端「我的」）共用的归属与绑定逻辑

抽出来的原因：
  1. 「哪些订单属于我」这条规则同时被 profile 接口、订单列表、单笔操作（确认收货/
     退货/取消）使用，散落在各处必然漂移 —— 一处漏判就是越权访问他人订单；
  2. 「绑定手机号」的占用校验（一个手机号只能归一个账号）也需要唯一入口。

归属规则（重要，直接关系到数据隔离）
----------------------------------
    我的订单 = user_id == 我
             或 (phone == 我绑定的手机号 且 user_id 为空)

    · user_id 精确归属：登录后下的单，最可靠；
    · phone 兜底归属：下单功能上线时还没有登录体系，历史订单只有手机号，
      用户绑定手机号后即可认领这批订单；
    · 「user_id 为空」这个约束不能省：否则 A 绑定了一个手机号，就能看到
      该手机号下 B 账号已认领的订单 —— 越权。
"""
from django.db.models import Q

from agent.models import UserProfile


def get_profile(user) -> UserProfile:
    """取用户资料（不存在则懒创建）。个人中心首次访问即自动建好。"""
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


def phone_taken_by_other(phone: str, user) -> bool:
    """该手机号是否已被**其它**账号绑定。"""
    if not phone:
        return False
    return UserProfile.objects.filter(phone=phone).exclude(user=user).exists()


def bind_phone_if_free(user, phone: str, nickname: str = "") -> bool:
    """下单后把（尚未占用且自己还没绑的）手机号认领到该用户资料。

    ⚠️ 本函数**绝不覆盖已绑定的手机号**。
    改造前它写成 `if profile.phone != phone: profile.phone = phone`，
    于是出现这条隐蔽的数据不同步链路：

        用户绑定了手机号 A（据此认领了 A 名下的 6 笔历史订单）
          → 某次下单随手填了手机号 B
          → profile.phone 被改成 B
          → 那 6 笔历史订单**立刻从「我的订单」消失**（归属规则失效）

    这正是用户反馈「数据不同步」的一种：钱和货都在，但订单"不见了"。
    所以现在只在**用户尚未绑定任何手机号**时才自动认领；已绑定的号码
    只能由用户本人到「个人中心」显式更换（那里有唯一性校验与提示）。

    :returns: 是否实际写入（用于审计日志）
    """
    phone = (phone or "").strip()
    if not phone or phone_taken_by_other(phone, user):
        return False

    profile = get_profile(user)
    changed = []

    # 手机号：仅在「自己还没绑」时认领，绝不覆盖
    if not (profile.phone or "").strip() and profile.phone != phone:
        profile.phone = phone
        changed.append("phone")

    # 收货人姓名：仅当用户还没填过时补上（同样不覆盖用户已设置的值）
    nickname = (nickname or "").strip()[:50]
    if nickname and not (profile.nickname or "").strip() and profile.nickname != nickname:
        profile.nickname = nickname
        changed.append("nickname")

    if changed:
        profile.save(update_fields=changed + ["updated_at"])
    return bool(changed)


def my_orders_q(user) -> Q:
    """构造「属于该用户的订单」查询条件（见模块 docstring 的归属规则）。"""
    cond = Q(user_id=user.id)
    phone = (get_profile(user).phone or "").strip()
    if phone:
        cond |= Q(phone=phone, user__isnull=True)
    return cond
