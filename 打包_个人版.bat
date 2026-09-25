@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  打包【个人版】—— 含你自己的配置与模板，仅供自己用
rem
rem  ⚠️ 产物里包含：
rem     config\ 下你自己的配置方案（可能含角色名、按键、地图）
rem     nametag\ 你的角色名模板
rem     minimaps\ 你录的路线
rem     monster\ 你截的怪物模板
rem     以及 tools\ 源码
rem  因此**不要把这个产物公开发布或上传网盘**。
rem  要发给别人，请用『打包_用户版.bat』。
rem
rem  依赖：PyInstaller（pip install pyinstaller）
rem  产物：dist\冒险岛自动练级-个人版\
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

set NAME=冒险岛自动练级-个人版
set OUT=dist\%NAME%

echo ============================================================
echo  打包【个人版】含个人资源与全部工具（不要外发）
echo  Python: %PY%
echo ============================================================
echo.

echo [1/4] 打包主程序（约 1~2 分钟）...
"%PY%" -m PyInstaller --noconfirm --noconsole --onedir src\main.py -p . ^
  --icon=%~dp0media\icon.ico ^
  --hidden-import=tools.routeRecorder --hidden-import=tools.calibrate_nametag --hidden-import=tools.template_capture --hidden-import=tools.diagnose --hidden-import=tools.mob_template_qa --hidden-import=tools.homeRouteDrawer ^
  -n "%NAME%" --workpath build_dev --specpath build_dev --distpath dist
if errorlevel 1 goto fail

echo.
echo [2/4] 建资源目录骨架...
for %%D in (config nametag monster minimaps log media) do (
    if not exist "%OUT%\%%D" mkdir "%OUT%\%%D"
)

echo.
echo [3/4] 复制资源...
xcopy "config" "%OUT%\config\" /E /I /Y >nul
xcopy "nametag" "%OUT%\nametag\" /E /I /Y >nul
xcopy "monster" "%OUT%\monster\" /E /I /Y >nul
xcopy "minimaps" "%OUT%\minimaps\" /E /I /Y >nul
xcopy "media" "%OUT%\media\" /E /I /Y >nul
xcopy "tools\*.py" "%OUT%\tools\" /I /Y >nul
if exist "docs" xcopy "docs" "%OUT%\docs\" /E /I /Y >nul
echo     - 含个人配置/名字/怪模板/地图 + tools 源码 + docs

echo.
echo [4/4] 完成
echo.
echo 产物目录: %OUT%
echo 提醒：本产物含个人数据，仅自用，不要外发。
echo.
goto end

:fail
echo.
echo [失败] 打包出错，看上面的红色报错。

:end
pause
