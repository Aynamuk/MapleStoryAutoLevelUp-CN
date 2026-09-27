# 关闭「冒险岛自动练级」主界面 —— 由 停止界面.bat 调用（见那个 bat 里的说明：
# 为什么不内联在 bat 里 —— cmd 会吃掉 $ 符号，两次实测都因此失败）。
#
# ⚠️★ 2026-09-27 修「点了停止界面关不掉」：
#   原判据是 `$_.CommandLine -match 'run_gui'` —— **读不到命令行时必然失效**。
#   实测（用户现场）：界面是**以管理员身份运行**的（挂机要用 Interception 发按键，
#   必须管理员），而 Get-CimInstance Win32_Process 在**普通权限**下读管理员进程的
#   CommandLine / ExecutablePath 会返回**空字符串** ⇒ 一个进程都匹配不上
#   ⇒ 脚本打印"[OK] 已关闭"，实际上**什么都没关**，用户只能去任务管理器强杀。
#   （对照：同机普通权限跑的 qqbot 进程，CommandLine 读得到。）
#
#   ⇒ 改用**窗口标题**判断 —— 普通权限也能读到
#     （实测 MainWindowTitle = '冒险岛怀旧服 - 自动练级'），
#     且该标题是主界面独有的，不会误伤其它 python 进程（如别的项目的 bot）。
#     标题来自 src/ui/ui.py 的 setWindowTitle，**改标题时要同步这里**。

$ErrorActionPreference = 'SilentlyContinue'

$titleKey = '冒险岛怀旧服 - 自动练级'
$sel = { $_.MainWindowTitle -like ('*' + $titleKey + '*') }

$found = @(Get-Process | Where-Object $sel)
Write-Host ('  找到界面进程 ' + $found.Count + ' 个')
foreach ($p in $found) {
    Write-Host ('    PID ' + $p.Id + '  ' + $p.ProcessName + '  ' + $p.MainWindowTitle)
}

if ($found.Count -eq 0) {
    Write-Host '  （没找到运行中的界面，可能本来就没开）'
    exit 0
}

# ① 先请求正常关闭 —— Qt 能干净销毁窗口，不会像强杀那样在屏幕上留残影
foreach ($p in $found) {
    try { $p.CloseMainWindow() | Out-Null } catch { }
}

Start-Sleep -Seconds 2

# ② 还没退出的，强制结束兜底
$left = @(Get-Process | Where-Object $sel)
foreach ($p in $left) {
    try { Stop-Process -Id $p.Id -Force } catch { }
}
Write-Host ('  正常关闭 ' + ($found.Count - $left.Count) + ' 个，强制结束 ' + $left.Count + ' 个')
