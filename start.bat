@echo off
chcp 65001 >nul
title 中渔天下 · AI 客服后台
echo.
echo  ╔══════════════════════════════════════╗
echo  ║     中渔天下 · Django 开发服务器      ║
echo  ╚══════════════════════════════════════╝
echo.
echo  启动中，首次启动较慢请稍候...
echo.

REM 检测端口是否已被占用
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo  [提示] 8000 端口已被占用，直接打开浏览器
    start "" http://127.0.0.1:8000/
    exit /b 0
)

REM 启动服务器（后台运行）
start "ZTYServer" /min cmd /c "python manage.py runserver 127.0.0.1:8000"

REM 等待服务器就绪
echo  等待服务器就绪...
setlocal enabledelayedexpansion
for /L %%i in (1,1,15) do (
    timeout /t 1 /nobreak >nul
    curl.exe -s -o NUL -w "%%{http_code}" http://127.0.0.1:8000/ 2>nul | findstr "200" >nul
    if !errorlevel!==0 (
        echo  [OK] 服务器已就绪，正在打开浏览器...
        start "" http://127.0.0.1:8000/
        echo.
        echo  ╔══════════════════════════════════════╗
        echo  ║  首页: http://127.0.0.1:8000/        ║
        echo  ║  后台: http://127.0.0.1:8000/admin/  ║
        echo  ║  关闭此窗口不会停止服务器            ║
        echo  ║  停止服务器请运行 stop.bat           ║
        echo  ╚══════════════════════════════════════╝
        pause
        exit /b 0
    )
)

echo  [错误] 服务器启动超时，请检查 Python 和依赖是否安装
pause
