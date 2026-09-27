@echo off
chcp 65001 >nul
title 冒险岛自动练级 - 停止界面
cd /d "%~dp0"

echo.
echo   正在关闭自动练级界面 ...
echo.

rem ⚠️★ 2026-09-27 本文件必须满足两条硬约束，改之前先读完：
rem   ① 换行必须是 CRLF —— 曾经用 LF-only 写过一版，cmd 会把 rem 行粘在一起、
rem      连下面的 powershell 命令一起吃掉 ⇒ 双击后窗口一闪就退（用户报"闪退"）。
rem      实测判据：本文件 CRLF 计数必须 > 0、纯 LF 计数必须 = 0。
rem      （项目里 .gitattributes 已有防复发设置，但手工写文件时仍会踩。）
rem   ② PowerShell 逻辑不能内联在本文件里 —— **cmd 会吃掉 $ 符号**，
rem      写 -Command "$_.Id..." 传到 PowerShell 时 $ 已经丢了，必然报解析错误。
rem      实测两次失败：^ 续行 + 中文 ⇒ ParserError: TerminatorExpectedAtEndOfString；
rem      单行 ⇒ $sel/$p 里的 $ 被吃掉 ⇒ UnexpectedToken。
rem   ⇒ 正确形态：逻辑放 _stop_ui.ps1（-File 调用），本文件只做转发。
rem     另：_stop_ui.ps1 必须存成 **UTF-8 with BOM**（Windows PowerShell 5.1
rem     默认按 ANSI 读 .ps1，无 BOM 时中文乱码并报 ParserError）。

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_stop_ui.ps1"

echo.
echo   [OK] 已处理（本来就没开的话，这里不会有任何变化）。
echo.
pause
exit /b 0
