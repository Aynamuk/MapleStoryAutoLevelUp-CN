'''
控制台输出安全 —— 让 print / logging 在 Windows GBK 控制台下**永不因编码崩溃**。

背景（2026-09-10 实测踩坑）
--------------------------
cmd 的默认代码页是 936(GBK)。Python 的输出一旦被重定向或走管道
（含 subprocess 捕获、`> log.txt`、部分终端宿主），就会按 locale 编码(cp936)写字节流。
此时字符串里只要有 GBK 编不出来的字符（【注意】 【OK】 【失败】 【说明】 等 emoji），就会直接抛：

    UnicodeEncodeError: 'gbk' codec can't encode character '\u26a0' ...

危害分两种：

- **print() —— 致命**：异常直接抛出、脚本中断。实测「标定名字标签」「验证名字定位」两个工具
  都在**写模板/写配置之前**崩掉，等于整个功能作废。
  更阴的是：**双击 bat 时控制台可能是 Windows 控制台 API（不吃编码），不会崩**，
  所以这个 bug **时好时坏、很难复现** —— 我第一次测时又恰好带着 `PYTHONUTF8=1`，
  于是误判成"跑通了"。
- **logging —— 噪声**：logging 会吞掉处理器异常，但每一行都往 stderr 打一段
  `--- Logging error ---` 回溯，把日志刷废（`MSLogger` 挂了 StreamHandler 到 stderr）。

对策（两层，缺一不可）
--------------------
1. 本模块：把 stdout/stderr 的编码错误处理改成 `replace` —— 编不出的字符显示成 `?`，
   绝不抛异常。**这是兜底，保证"不崩"。**
2. 源码里的输出文案已经换成 GBK 可编码的写法（`【注意】【OK】` 等）——
   正常路径下根本不会触发第 1 层，用户在 GBK 控制台看到的是正确中文。

两个都做，是因为只做 2 会留隐患（以后谁随手加个 emoji 又崩），只做 1 会让用户看到 `??`。
'''
import sys


def make_console_safe():
    '''
    把标准输出的编码错误处理改成 replace。幂等，可重复调用。

    注意：logging 的 StreamHandler 默认抓的就是 `sys.stderr` 这个对象，
    这里 reconfigure 是**就地**改同一个对象，所以已经在用的 handler 同样受保护。
    '''
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            # 某些被替换掉的流对象（如打包器的包装）没有 reconfigure —— 忽略即可，
            # 真要出问题也只是回到"原来的行为"，不会更糟。
            pass
    return True


# ======================================================================
# 「没有控制台」这件事，统一在这里处理
#
# 背景（2026-09-12 / 2026-09-26 两次踩同一个坑）
# --------------------------------------------------
# 界面（F2 标定名字 / F3 截怪 / F4 录路线）都是**另起一个进程**去跑 tools/ 下的
# 工具。当主程序是 `--noconsole` 打包时，子进程可能**拿不到控制台**，此时：
#
#   · `input()` 立刻抛 EOFError    → 工具**当场崩掉**，且父进程看不见（表现为
#                                    「点了没反应」/「鼠标转圈闪一下」）
#   · `sys.stdin.isatty()` 为假     → 可以用来提前判断「我有没有真终端」
#
# 2026-09-12 在 routeRecorder 里修过一次（那次是录制器没控制台）；
# 2026-09-26 用户报的 issue #1（F3 截怪点了没反应）是**同一个坑的第二处**——
# 说明「一个工具一个工具手写防御」会漏。所以抽到本模块统一提供：
#
#   · wait_enter_or_skip()  —— 结束时「按回车关闭窗口」这类**装饰性**等待：
#                              没控制台就跳过，绝不影响功能。
#   · require_console()     —— 真的需要用户敲字的地方：没控制台时给出**明确
#                              可读的报错**，而不是静默崩溃或死等。
#
# ⚠️ 新增 tools/ 下的工具时，凡是要 input() 的地方都改用这两个函数。
# ======================================================================

def has_real_console():
    """当前进程是否挂着一个**真正的交互式终端**（能读用户输入）。

    `isatty()` 为真 ⇔ 有真终端。被重定向、走管道、或无控制台时都为假。
    任何判断「能不能问用户」的地方都该先过这一关。
    """
    try:
        stdin = sys.stdin
        return stdin is not None and bool(stdin.isatty())
    except Exception:
        # 某些被替换过的流对象没有 isatty —— 保守认为"没有控制台"
        return False


def wait_enter_or_skip(message="\n[结束] 按回车关闭窗口 "):
    """「按回车关闭窗口」——**有控制台才等，没有就跳过**。

    专用于工具跑完时那个让窗口别一闪而过的等待：跳过它对功能毫无影响，
    所以这里静默处理，绝不抛异常、绝不卡住。

    返回 True 表示真的等到了一次回车，False 表示跳过了。
    """
    if not has_real_console():
        return False
    try:
        input(message)
        return True
    except Exception:
        # 用户关掉了窗口 / 流被抢走 —— 都不该让工具崩在最后一步
        return False


def require_console(prompt):
    """真的需要用户输入时用它：**没有控制台就抛出可读的报错**。

    与 wait_enter_or_skip 的区别：这个不能跳过（跳过工具就没法用），
    所以宁可抛错让上层捕获、把原因打在日志里，也不要：
      · 静默崩溃（用户只看到「没反应」）
      · 永远卡在 input() 上（用户只看到窗口不动）

    返回用户输入的字符串（已 strip）。没有控制台时抛 RuntimeError。
    """
    if not has_real_console():
        raise RuntimeError(
            "这个工具需要控制台来向你提问，但当前进程没有可用的控制台。\n"
            "如果你是通过主界面启动它：请确认程序是正常启动的（不是被其它程序"
            "以无终端方式拉起），并查看日志里本工具的输出。\n"
            f"（原提问：{prompt.strip()}）")
    return input(prompt).strip()
