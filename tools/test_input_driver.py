'''
tools/test_input_driver.py
Interception 输入链路自检脚本

用途：验证 interception-python -> 本机驱动 -> 应用 这条链路是否真的通了。

安全保护：只有当"前台窗口"是记事本时才会发送按键，否则直接中止。
绝不往其它窗口（聊天框、IDE 等）里打字。

用法：
    1. 打开记事本，点进编辑区（保证它是当前窗口）
    2. 【重要】把输入法切到英文模式，否则 hello 会被拼音合成成中文（如"合理楼"）
    3. python -m tools.test_input_driver
    4. 记事本里应出现: "hello <空格> 123"

可选参数：
    --force   跳过前台窗口检查（不推荐，风险自负）

注意：脚本会尝试从编辑控件读回文本做闭环校验，但部分编辑器（尤其 Scintilla 系）
读不回来，属正常现象；此时看窗口标题是否出现 "* " 前缀（已修改标记）来判断按键是否生效。
'''
import sys
import time
import argparse
import ctypes

# 让输出在 GBK 控制台下不因 emoji 崩掉。本脚本不依赖 src，所以内联这段；
# 其它工具走 src/utils/console.py（解释见那里）。2026-09-10 实测踩坑：bat 里会崩。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

user32 = ctypes.windll.user32


_WM_GETTEXT = 0x000D


def read_editor_text():
    """从前台编辑窗口的编辑控件里读回文本（只读取，不改动内容）"""
    hwnd = user32.GetForegroundWindow()

    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    targets = []

    def _enum(child, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, cls, 256)
        # Scintilla(记事本4等)、Edit(标准记事本)、RichEditD2DPT(新版记事本)
        if any(k in cls.value for k in ("Scintilla", "Edit", "RichEdit")):
            targets.append((child, cls.value))
        return True

    user32.EnumChildWindows(hwnd, EnumChildProc(_enum), 0)
    if not targets:
        return None

    child, cls = targets[0]
    length = user32.SendMessageW(child, _WM_GETTEXT, 0, 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.SendMessageW(child, _WM_GETTEXT, length + 1, ctypes.byref(buf))
    return buf.value


def get_foreground_title():
    """获取当前前台窗口标题"""
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="跳过前台窗口检查")
    args = parser.parse_args()

    title = get_foreground_title()
    is_notepad = ("记事本" in title) or ("Notepad" in title)

    print(f"当前前台窗口: {title!r}")
    if not is_notepad and not args.force:
        print("\n[中止] 前台不是记事本，为安全起见不发送任何按键。")
        print("       请先打开记事本并点进编辑区后重试。")
        return 1

    print("[安全] 前台窗口确认是记事本，开始测试")
    for i in range(3, 0, -1):
        print(f"  {i} 秒后开始发送按键...")
        time.sleep(1)

    from src.input.InterceptionController import init_interception, press_key, key_down, key_up

    print("\n[1] 初始化驱动")
    init_interception()
    print("    初始化完成")

    print("[2] 点按测试 press_key() —— 应打出 hello")
    for ch in "hello":
        press_key(ch)
        time.sleep(0.05)

    time.sleep(0.3)

    print("[3] 按住/松开测试 key_down() + key_up() —— 应打出 i (走位用的就是这条路径)")
    key_down("i")
    time.sleep(0.1)
    key_up("i")

    time.sleep(0.3)

    print("[4] 配置里会用到的键名测试 —— 应打出空格和 123")
    press_key("space")
    press_key("1")
    press_key("2")
    press_key("3")

    time.sleep(0.5)
    print("\n[5] 从窗口读回内容做闭环验证")
    text = read_editor_text()
    if text is None:
        print("    读不到编辑控件内容，请肉眼确认")
    else:
        got = repr(text)
        print(f"    窗口内实际文本: {got}")
        if not got.strip():
            # Scintilla 系编辑器常读不回来，属正常
            print("    （读回为空：可能是 Scintilla 编辑器读不回来，"
                  "请改看窗口标题是否出现 '* ' 已修改标记，或直接肉眼确认）")
        else:
            expect = "hello i 123"
            if expect in text:
                print(f"    【OK】 输入链路完全正常（包含预期内容 {expect!r}）")
            else:
                print(f"    【注意】 有内容但不是预期的 {expect!r} —— 最常见原因是中文输入法把字母合成了汉字")

    print("\n完成。请与实际窗口内容核对。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
