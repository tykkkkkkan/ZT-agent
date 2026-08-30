"""
agent/manage_views.py — 独立的管理员登录入口（/manage/）

跟 /admin/ 是两个独立入口，方便管理员从外部直接进管理后台；
不是 admin 路径，避免被 admin 站内链接绕到。
- 登录：is_staff 校验（普通用户即使有账号也进不了）
- 登录成功：跳到 /admin/ 索引页（运营概览）
- 退出：清 session 回到 /manage/login/
"""
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect


@csrf_protect
@require_http_methods(["GET", "POST"])
def manage_login(request):
    # 已登录且是 staff，直接跳 admin 索引
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('admin:index')

    error = None
    username = ''
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        next_url = (request.POST.get('next') or request.GET.get('next') or '').strip()
        if not username or not password:
            error = '请输入账号和密码。'
        else:
            user = authenticate(request, username=username, password=password)
            if user is None:
                error = '账号或密码错误。'
            elif not user.is_staff:
                error = '此账号没有后台管理权限，请联系超级管理员。'
            else:
                login(request, user)
                # next 仅允许跳同站路径，避免开放重定向
                if next_url.startswith('/') and not next_url.startswith('//'):
                    return redirect(next_url)
                return redirect('admin:index')

    return render(request, 'manage/login.html', {
        'error': error,
        'username': username,
    })


@login_required
def manage_logout(request):
    logout(request)
    return redirect('manage_login')
