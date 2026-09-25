@echo off
setlocal

REM ============================================================
REM  打包脚本: 生成 dist\MapleStoryAutoLevelUp\
REM
REM  为什么用 --onedir 而不是 --onefile:
REM    程序读模板与配置时, 路径一律相对「应用根目录」解析
REM    见 src\utils\paths.py。--onefile 会把 exe 直接丢在 dist\ 根下,
REM    与 config nametag monster minimaps 等资源目录分离,
REM    双击后必然找不到资源。--onedir 让 exe 与依赖同处一个文件夹,
REM    再把资源目录复制进去, 即可整目录分发。
REM
REM  产物: dist\MapleStoryAutoLevelUp\   可整个文件夹拷给用户
REM ============================================================

cd /d "%~dp0"

echo [1/4] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/4] 调用 PyInstaller 打包, 首次约需 1 到 3 分钟...
pyinstaller --noconfirm --noconsole --onedir src\main.py -p . --icon=media\icon.ico -n MapleStoryAutoLevelUp
if errorlevel 1 goto fail

echo [3/4] 复制资源目录到输出目录...
set OUT=dist\MapleStoryAutoLevelUp
for %%D in (config nametag misc monster minimaps media) do (
    if exist "%%D" (
        xcopy "%%D" "%OUT%\%%D\" /E /I /Y >nul
        echo     - %%D 已复制
    ) else (
        echo     - %%D 不存在, 跳过
    )
)

echo [4/4] 完成
echo.
echo 产物目录: %OUT%
echo 双击 %OUT%\MapleStoryAutoLevelUp.exe 运行
echo 运行日志写在 %OUT%\log\ 下
echo.
pause
exit /b 0

:fail
echo.
echo [失败] PyInstaller 返回错误, 请查看上方输出
pause
exit /b 1
