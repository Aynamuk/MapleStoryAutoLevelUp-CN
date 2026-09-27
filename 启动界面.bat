@echo off
chcp 65001 >nul
title 冒险岛自动练级 - 启动界面
cd /d "%~dp0"

rem ============================================================
rem  启动界面（源码模式）
rem
rem  Python 定位顺序：
rem    1) 环境变量 MAPLEBOT_PY 指定的 pythonw.exe / python.exe
rem    2) 系统 PATH 里的 pythonw.exe
rem    3) 系统 PATH 里的 python.exe
rem  都找不到时给出提示，不猜路径。
rem
rem  想固定用某个虚拟环境，先设一次环境变量即可：
rem      setx MAPLEBOT_PY "D:\venv\Scripts\pythonw.exe"
rem ============================================================

set PY=
if defined MAPLEBOT_PY if exist "%MAPLEBOT_PY%" set PY=%MAPLEBOT_PY%

if not defined PY for /f "delims=" %%i in ('where pythonw.exe 2^>nul') do (
    if not defined PY set PY=%%i
)
if not defined PY for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    if not defined PY set PY=%%i
)

if not defined PY (
    echo.
    echo   [错误] 没找到 Python。
    echo   请先安装 Python 3.12 并勾选 "Add python.exe to PATH"，
    echo   或设置环境变量 MAPLEBOT_PY 指向你的 pythonw.exe。
    echo.
    pause
    exit /b 1
)

if not exist "run_gui.py" (
    echo.
    echo   [错误] 当前目录下找不到 run_gui.py
    echo   请把本 bat 放在项目根目录（和 src 文件夹同级）再运行。
    echo.
    pause
    exit /b 1
)

rem 防重复启动：已经在跑就不再开一个
rem
rem ⚠️★ 2026-09-27 换判据（与 停止界面.bat 同一个根因）：
rem   原判据 `$_.Name -like 'python*' -and $_.CommandLine -match 'run_gui'` **双重失效**：
rem     ① 界面以**管理员身份**运行（挂机要 Interception 发按键），而 Get-CimInstance
rem        在普通权限下读管理员进程的 CommandLine 返回**空** ⇒ 匹配不到 ⇒ 防重复形同虚设；
rem     ② bat 内联 `-Command "..."` 时 **cmd 会吃掉 `$` 符号**（实测 `$_.Name` 会变成
rem        `_.Name` 之类）⇒ PowerShell 收到的是坏命令，判据更不可靠。
rem   实测（2026-09-27）：CIM 判据找到 **0** 个 run_gui 进程（界面其实开着）。
rem   ⇒ 改用**窗口标题**判断，并把逻辑放进 _ui_running.ps1（-File 调用，绕开 cmd 转义）。
rem     标题来自 src/ui/ui.py 的 setWindowTitle，改标题时要同步 _ui_running.ps1。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_ui_running.ps1"
if errorlevel 1 (
    echo.
    echo   界面已经在运行了，不用重复打开。
    echo   要关掉它，请双击『停止界面.bat』。
    echo.
    pause
    exit /b 0
)

echo.
echo   正在打开界面，请稍候 ...
echo   Python: %PY%
echo.
start "" "%PY%" run_gui.py
echo   [OK] 已启动。
echo   界面没出现的话，双击『诊断启动.bat』能看到具体报错。
echo.
rem 停留约 3 秒，让用户看清上面的提示。
rem 这里用 ping 而不是 timeout：timeout 在标准输入被重定向时会直接报错退出。
%SystemRoot%\System32\ping.exe -n 4 127.0.0.1 >nul
exit /b 0
