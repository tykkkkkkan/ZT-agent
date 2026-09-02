@echo off
chcp 65001 >nul
title 停止中渔天下服务器
echo.
echo  正在停止 8000 端口上的 Django 服务器...
echo.
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    if !errorlevel!==0 (
        echo  [OK] 已停止进程 PID=%%a
    ) else (
        echo  [提示] 进程 %%a 无法终止，可能需要管理员权限
    )
)
echo.
echo  已完成。
timeout /t 2 /nobreak >nul
