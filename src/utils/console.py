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
