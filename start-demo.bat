@echo off
rem ---------------------------------------------------------------------------
rem  Zhongyu Tianxia - one-click demo launcher
rem
rem  Double-click this file. It runs the whole demo stack in ONE terminal:
rem      self-check  ->  start the server bound to 0.0.0.0
rem  then open the cashier page, issue a ticket, and let the phone scan it.
rem
rem  Add a public tunnel so the phone can scan from ANY network (4G, campus,
rem  dorm, whatever) - still just this one window:
rem      start-demo.bat --tunnel
rem
rem  Other useful args: --port 8001   --force
rem
rem  NOTE: keep this file ASCII-only. cmd.exe reads a .bat using the OEM
rem  codepage (cp936 on this machine), so UTF-8 Chinese text here would show
rem  up as garbage.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem  Prefer Python 3.12 through the "py" launcher.
rem  Why it matters: the Windows Firewall inbound allow-rule for python.exe
rem  matches on the executable *path*, and it is registered for the system
rem  Python 3.12. Launching a venv python instead would miss that rule, and
rem  the phone would not be able to connect while local testing still works.
set "PY=py -3.12"
where py >nul 2>nul
if errorlevel 1 set "PY=python"

echo.
echo   Zhongyu Tianxia - demo launcher
echo   Interpreter : %PY%
echo   Working dir : %CD%
echo.

%PY% manage.py lancheck --serve %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo   [exit code %RC%] Something went wrong - read the messages above.
    echo   Press any key to close this window.
    pause >nul
)
endlocal
