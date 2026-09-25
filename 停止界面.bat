@echo off
title 冒险岛自动练级 - 停止界面
cd /d "%~dp0"

echo.
echo   正在关闭自动练级界面 ...
echo.

rem 先请界面自己正常退出（这样 Qt 能干净销毁窗口，不会像直接强杀那样
rem 在屏幕上留一个点不动的残影窗口）；等两秒还没退出的，再强制结束兜底。
powershell -NoProfile -Command "$sel = {$_.Name -like 'python*' -and $_.CommandLine -match 'run_gui'}; $ps = Get-CimInstance Win32_Process | Where-Object $sel; foreach ($p in $ps) { $o = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; if ($o) { $o.CloseMainWindow() | Out-Null } }; Start-Sleep -Seconds 2; Get-CimInstance Win32_Process | Where-Object $sel | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo   [OK] 已关闭（本来就没开的话，这里不会有任何变化）。
echo.
pause
exit /b 0
