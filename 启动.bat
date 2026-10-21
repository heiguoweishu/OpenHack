@echo off
setlocal enabledelayedexpansion
title OpenHack - 黑苹果一键装机

cd /d "%~dp0"

echo.
echo   OpenHack  黑苹果一键装机
echo   ================================================
echo.

set "PYW=%~dp0runtime\Scripts\pythonw.exe"
set "PYS=%~dp0runtime\Scripts\python.exe"
set "READY=%~dp0data\server.ready"
set "DATA=%~dp0data"

if not exist "%PYS%" goto noruntime

REM 清掉上一次残留的就绪标记, 否则会误判为已启动
if exist "%READY%" del /q "%READY%" >nul 2>&1

echo   正在启动本地服务 ...
start "" /min "%PYW%" -m app.server --port 8733

REM 等就绪文件出现 (最多 40 秒)
set /a TRY=0
:wait
set /a TRY+=1
if !TRY! gtr 40 goto timeout
if exist "%READY%" goto ready
ping -n 2 127.0.0.1 >nul 2>&1
goto wait

:ready
set /p PORT=<"%READY%"
echo   服务已就绪 (端口 !PORT!)
echo.

set "URL=http://127.0.0.1:!PORT!/"

REM 优先用 Edge / Chrome 的 app 模式, 得到干净独立窗口
REM 注意 ProgramFiles(x86) 含括号, 必须提前取出到变量, 否则会破坏 if 的括号解析
set "PF86=%ProgramFiles(x86)%"
set "PF64=%ProgramFiles%"
set "EDGE_A=%PF86%\Microsoft\Edge\Application\msedge.exe"
set "EDGE_B=%PF64%\Microsoft\Edge\Application\msedge.exe"
set "CHR_A=%PF64%\Google\Chrome\Application\chrome.exe"
set "CHR_B=%PF86%\Google\Chrome\Application\chrome.exe"

if exist "%EDGE_B%" goto use_edge_b
if exist "%EDGE_A%" goto use_edge_a
if exist "%CHR_A%" goto use_chr_a
if exist "%CHR_B%" goto use_chr_b
goto use_default

:use_edge_b
start "" "%EDGE_B%" --app="%URL%" --window-size=1320,900
goto started

:use_edge_a
start "" "%EDGE_A%" --app="%URL%" --window-size=1320,900
goto started

:use_chr_a
start "" "%CHR_A%" --app="%URL%" --window-size=1320,900
goto started

:use_chr_b
start "" "%CHR_B%" --app="%URL%" --window-size=1320,900
goto started

:use_default
start "" "%URL%"

:started
echo   界面已打开。关闭本窗口不会停止服务。
echo   若要执行分区操作, 请右键以管理员身份运行本程序。
echo.
timeout /t 4 /nobreak >nul 2>&1
exit /b 0

:noruntime
echo   [错误] 没有找到运行环境 runtime\Scripts\python.exe
echo.
echo   请先执行:
echo      python -m venv runtime
echo      runtime\Scripts\python -m pip install -r requirements.txt
echo.
if not "%OPENHACK_TEST%"=="1" pause
exit /b 1

:timeout
echo   [错误] 服务启动超时。请尝试手动运行:
echo      runtime\Scripts\python -m app.server --port 8733
echo.
if not "%OPENHACK_TEST%"=="1" pause
exit /b 1
