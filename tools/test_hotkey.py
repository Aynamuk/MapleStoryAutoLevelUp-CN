# -*- coding: utf-8 -*-
"""
按键监听自检 —— 定位「录制器按 F3/F4 没反应」的根因。

背景（2026-09-12）：路线录制器在游戏里按 F3/F4 存不出文件，日志证明按键标志
从未置位。头号假说：游戏客户端以管理员权限运行时，Windows UIPI 会**静默屏蔽**
普通权限进程的键盘钩子（pynput 收不到任何键）——游戏聚焦时收不到、
切到普通窗口（如本工具窗口）就恢复。

用法（直接跑 python -m tools.test_hotkey；需要管理员权限）：
  双击 bat 后按提示做两轮测试，把输出截图/抄下来即可。
"""
import ctypes

from pynput import keyboard

try:
    IS_ADMIN = bool(ctypes.windll.shell32.IsUserAnAdmin())
except Exception:
    IS_ADMIN = False

print("=" * 64)
print(" 按键监听自检")
print("=" * 64)
print(f" 本工具当前权限：{'管理员' if IS_ADMIN else '普通（非管理员）'}")
print()
print(" 下面按提示做两轮测试，每按一个键这里都会打印一行「收到按键」。")
print()
print(" 第 1 轮：点一下本控制台窗口，然后按 F3、F4、方向右键 各一次")
print("         —— 应该每键都显示。")
print()
print(" 第 2 轮：点一下游戏窗口（让游戏在前台），再按 F3、方向右键")
print("         —— 如果这里【什么都不显示】而第 1 轮正常：")
print("            说明游戏以管理员权限运行，屏蔽了普通权限的按键监听。")
print("            解法：以管理员身份运行本程序（右键「启动界面.bat」→ 以管理员身份运行），再录路线。")
print()
print(" 按 ESC 结束本工具。")
print("-" * 64)
print()


def key_name(key):
    try:
        return repr(key.char)
    except AttributeError:
        return str(key).replace("Key.", "")


def on_press(key):
    print(f"  收到按键: {key_name(key)}", flush=True)


def on_release(key):
    if key == keyboard.Key.esc:
        print()
        print(" 结束。请把上面全部输出发给 AI（或截图）。")
        return False


with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
