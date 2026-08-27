"""
创建 Django 管理员账号
用法：python create_admin.py
"""
import os
import sys

# 设置 Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

USERNAME = "admin"
PASSWORD = "admin123"

if User.objects.filter(username=USERNAME).exists():
    print(f"管理员 {USERNAME} 已存在，跳过创建。")
else:
    User.objects.create_superuser(
        username=USERNAME,
        email="admin@zhongyu.com",
        password=PASSWORD,
    )
    print(f"管理员账号创建成功！")
    print(f"  用户名：{USERNAME}")
    print(f"  密码：  {PASSWORD}")
    print(f"  后台：  http://127.0.0.1:8000/admin/")
