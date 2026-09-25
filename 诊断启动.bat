@echo off
chcp 65001 >nul
title 冒险岛自动练级 - 诊断启动（会显示报错）
cd /d "%~dp0"

rem 与『启动界面.bat』同一套 Python 定位逻辑，区别是用带控制台的 python.exe，
rem 并把报错留在窗口里给你看。

set PY=
if defined MAPLEBOT_PY if exist "%MAPLEBOT_PY%" set PY=%MAPLEBOT_PY%

if not defined PY for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    if not defined PY set PY=%%i
)

echo.
echo   ============================================================
echo    诊断模式：这个窗口会一直留着，报错信息直接显示在这里
echo   ============================================================
echo.

if not defined PY (
    echo   [错误] 没找到 Python（python.exe）。
    echo   请安装 Python 3.12 并勾选 "Add python.exe to PATH"，
    echo   或设置环境变量 MAPLEBOT_PY 指向你的 python.exe。
    echo.
    pause
    exit /b 1
)

if not exist "run_gui.py" (
    echo   [错误] 当前目录下找不到 run_gui.py
    echo   请把本 bat 放在项目根目录再运行。
    echo.
    pause
    exit /b 1
)

echo   Python: %PY%
echo.

"%PY%" run_gui.py

echo.
echo   ============================================================
echo    程序已退出，退出码：%ERRORLEVEL%
echo    正常退出码是 0；非 0 说明启动过程中出错了，请看上面的信息。
echo    报错详情也会存到项目 log 文件夹里。
echo   ============================================================
echo.
pause
exit /b 0
