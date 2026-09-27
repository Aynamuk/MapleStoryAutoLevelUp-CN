# 判断主界面是否已在运行 —— 由 启动界面.bat 调用（防重复启动）。
# 退出码：0 = 没在跑（可以启动）；1 = 已在跑（别重复开）。
#
# ⚠️★ 2026-09-27 为什么改成"按窗口标题"判断（原来是 CIM + CommandLine）：
#   原判据 `$_.Name -like 'python*' -and $_.CommandLine -match 'run_gui'` **双重失效**：
#     ① 界面以**管理员身份**运行（挂机要用 Interception 发按键），而
#        Get-CimInstance 在**普通权限**下读管理员进程的 CommandLine 返回**空**
#        ⇒ 匹配不到 ⇒ 防重复形同虚设；
#     ② 原判据内联在 bat 的 `-Command "..."` 里，而 **cmd 会吃掉 `$` 符号**
#        ⇒ PowerShell 收到坏命令，判据更不可靠。
#   实测（2026-09-27）：CIM 判据找到 **0** 个 run_gui 进程（而界面其实开着），
#     窗口标题判据能找到（MainWindowTitle = '冒险岛怀旧服 - 自动练级'）。
#
#   ⇒ 改用窗口标题。标题来自 src/ui/ui.py 的 setWindowTitle，**改标题时要同步这里**。
#   ⚠️ 本文件必须存成 **UTF-8 with BOM** —— Windows PowerShell 5.1 默认按 ANSI 读 .ps1，
#      无 BOM 时中文会乱码、字符串引号失配并报 ParserError（2026-09-27 实测踩到）。

$ErrorActionPreference = 'SilentlyContinue'

$titleKey = '冒险岛怀旧服 - 自动练级'
$found = @(Get-Process | Where-Object { $_.MainWindowTitle -like ('*' + $titleKey + '*') })

if ($found.Count -gt 0) {
    $pids = ($found | ForEach-Object { $_.Id }) -join ', '
    Write-Host ('  检测到界面已在运行（PID ' + $pids + '）')
    exit 1
}
exit 0
