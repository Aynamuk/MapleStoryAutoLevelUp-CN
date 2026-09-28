@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  打包【发布版】—— 干净、可直接给别人
rem
rem  与「打包_个人版.bat」的区别：
rem    本脚本**不带**任何个人资源（配置方案 / 名字模板 / 地图路线 / 怪物模板），
rem    只放出厂默认配置，打包结果可以直接分发给别人。
rem
rem  依赖：PyInstaller（pip install pyinstaller）
rem  产物：dist\冒险岛自动练级\   —— 整个文件夹拷走即可运行
rem
rem  ⚠️ --hidden-import 里列的是**打包版运行时要自调用**的子工具
rem     （见 src/main.py 的 _run_tool：exe 不认 -m，走 `exe --tool tools.xxx`）。
rem     漏一个，用户点对应按钮就会 "No module named ..." —— 而用户没有 Python，
rem     敲不了命令行补救。measure_window 是「一键按当前窗口大小改配置」用的，
rem     正是给没有 Python 的打包版用户兜底，**必须在列**。
rem ============================================================

set PY=
if defined MAPLEBOT_PY if exist "%MAPLEBOT_PY%" set PY=%MAPLEBOT_PY%
if not defined PY for /f "delims=" %%i in ('where python.exe 2^>nul') do (
    if not defined PY set PY=%%i
)
if not defined PY (
    echo [错误] 没找到 Python。请安装 Python 3.12 或设置 MAPLEBOT_PY。
    pause
    exit /b 1
)

set NAME=冒险岛自动练级
set OUT=dist\%NAME%

echo ============================================================
echo  打包【发布版】干净、可直接给别人
echo  Python: %PY%
echo ============================================================
echo.

echo [1/4] 打包主程序（约 1~2 分钟）...
"%PY%" -m PyInstaller --noconfirm --noconsole --onedir src\main.py -p . ^
  --icon=%~dp0media\icon.ico ^
  --hidden-import=tools.routeRecorder --hidden-import=tools.calibrate_nametag --hidden-import=tools.template_capture --hidden-import=tools.diagnose --hidden-import=tools.mob_template_qa --hidden-import=tools.homeRouteDrawer --hidden-import=tools.measure_window ^
  -n "%NAME%" --workpath build_user --specpath build_user --distpath dist
if errorlevel 1 goto fail

echo.
echo [2/4] 建资源目录骨架...
for %%D in (config nametag monster minimaps log media monster_lib) do (
    if not exist "%OUT%\%%D" mkdir "%OUT%\%%D"
)

echo.
echo [3/4] 复制资源...
copy /Y config\config_default.yaml "%OUT%\config\" >nul
copy /Y config\config_data.blank.yaml "%OUT%\config\config_data.yaml" >nul
copy /Y config\config_custom.yaml "%OUT%\config\" >nul
copy /Y media\icon.ico "%OUT%\media\" >nul
copy /Y media\icon.png "%OUT%\media\" >nul
echo     - 只放出厂默认配置（不带任何个人设置与模板）
echo     - 怪素材库 monster_lib\ 随包分发（用户选图后可直接勾选登记，免手抠）
if exist monster_lib (
    xcopy /Y /E /I /Q monster_lib "%OUT%\monster_lib" >nul
    echo     - 已复制 monster_lib（现成怪物模板库）
) else (
    echo     [警告] 没找到 monster_lib\ —— 用户将无法从素材库选怪，只能自己截
)

echo.
echo [4/4] 完成
echo.
echo 产物目录: %OUT%
echo 把这个**整个文件夹**拷走就能用（不要只拷 exe）。
echo.
goto end

:fail
echo.
echo [失败] 打包出错，看上面的红色报错。

:end
pause
