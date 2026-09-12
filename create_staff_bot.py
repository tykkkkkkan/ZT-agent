"""为营销 Agent 创建专用「员工级」机器人账号。

与 admin 的区别：
- is_staff=True  → 可调用 ZT-agent 的 staff_required 管理接口（补库/发货/退货/取消）
- is_superuser=False → 进不了 /admin/ 后台、改不了系统配置，权限最小

用途：让「人操作（admin）」与「Agent 自动操作（mkt_bot）」在审计里可区分。
用法：python create_staff_bot.py
"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

USERNAME = "mkt_bot"
EMAIL = "mkt_bot@zhongyu.local"
PASSWORD = "mkt_bot@2026"

if User.objects.filter(username=USERNAME).exists():
    u = User.objects.get(username=USERNAME)
    # 幂等：保证权限正确（防止手动被改坏）
    u.is_staff = True
    u.is_superuser = False
    u.is_active = True
    u.set_password(PASSWORD)
    u.save()
    print(f"机器人账号 {USERNAME} 已存在，已重置为最小权限员工账号。")
else:
    u = User.objects.create_user(
        username=USERNAME,
        email=EMAIL,
        password=PASSWORD,
        is_staff=True,        # 可调用管理接口
        is_superuser=False,   # 不可进后台、不可改系统
        is_active=True,
    )
    print(f"机器人账号创建成功！")
    print(f"  用户名：{USERNAME}")
    print(f"  密码：  {PASSWORD}")
    print(f"  权限：  is_staff=True / is_superuser=False（最小权限，仅调用管理接口）")
