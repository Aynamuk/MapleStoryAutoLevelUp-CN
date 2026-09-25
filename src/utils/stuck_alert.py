# -*- coding: utf-8 -*-
'''卡死告警：醒目日志 + **置顶弹窗**（2026-09-15 加）。

## 为什么有它

看门狗原来在判"卡死"之后抽一个**纯随机**动作
（`random.choice(["left","right","none"]) × ["down","none"] × ["jump","none"]`
共 12 种组合，**完全不知道悬崖在哪**）。用户 2026-09-15 实测判定它
"没有好处只有坏处"，两条证据：
  ① 窄平台上乱抽 = 直接掉下去（那次抽到 `right none jump`，而右边正好是悬崖）；
  ② 它把真因**盖住**了 —— 13:58 那次 `stuck 10.08s` 后随机跳，把角色从
     `(100,122)` 踹到 `(100,118)`，症状从"在原地蹭"变成"跳到另一层"，
     日志上看"它动了"，反而更难查。

⇒ 用户定：**别乱动，弹一个置顶窗口告诉我**。

## 实现要点（都踩过坑，别改）

1. **弹窗跑在 daemon 线程里**：`MessageBoxW` 会**阻塞到用户点掉**，
   直接在主循环里调 = 整个挂机卡死。
2. **只给 `MB_TOPMOST`，不给 `MB_SETFOREGROUND`**：加后者会把前台从游戏抢走，
   按键就送不进游戏了（引擎有"前台守卫"）。**置顶 + 不抢焦点**才是对的。
   ⚠️ 2026-09-16 补：**光有 MB_TOPMOST 不够** —— 用户实测"听得到声音、看不到框"。
   后台进程弹的框会被 Windows 前台锁压在游戏下面，所以弹出后还要
   `SetWindowPos(HWND_TOPMOST)` 顶一次 + `FlashWindowEx` 闪任务栏
   （见 `_raise_and_flash`）。**顶上去 + 闪，但依然不抢焦点。**
3. **单实例 + 冷却**：人还卡着时看门狗每 `timeout`(10s) 会再报一次，不拦就弹一串窗口。
   弹窗**没关之前**不再弹；关掉之后也要过 `alert_cooldown` 秒。
4. **任何异常都不许往外抛**：告警失败绝不能把主循环带崩（全程 try/except 兜底）。
5. **非 Windows / 弹窗不可用 → 只记日志**：离线自检、其它平台上跑不会炸。
'''
from __future__ import annotations

import logging
import sys
import threading
import time

logger = logging.getLogger(__name__)

_TITLE = "挂机引擎：角色卡住了"

# 模块级状态（单实例 + 冷却）
_lock = threading.Lock()
_popup_open = False
_t_last_popup = 0.0

# Win32 MessageBox 标志
_MB_OK = 0x00000000
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000
# ⚠️ 故意**不**用 MB_SETFOREGROUND(0x00010000)：那会把前台从游戏抢走（见文件头第 2 条）


def _messagebox_topmost(title: str, text: str) -> None:
    '''Windows 下弹一个**置顶但不抢前台**的消息框（**会阻塞到用户关闭**）。'''
    import ctypes                                    # 只在 win32 上 import
    ctypes.windll.user32.MessageBoxW(
        None, str(text), str(title), _MB_OK | _MB_ICONWARNING | _MB_TOPMOST)


def _raise_and_flash(thread_id: int, timeout: float = 3.0) -> bool:
    '''把刚弹出的消息框**再顶一次**，并让它闪任务栏。

    ⚠️ 为什么光有 MB_TOPMOST 不够（2026-09-16 用户实测）：
    用户原话「这个卡死提示**不会弹出置顶**，我听到了声音，所以真到用的时候基本看不到」。
    原因：消息框是**后台进程**弹的、`hwndOwner=None`、又不抢前台 ——
    Windows 的**前台锁（foreground lock）**会让这种框只能"闪任务栏"，
    实际被压在游戏窗口下面；`MB_TOPMOST` 只在"同一条 topmost 带"里排队，
    游戏若也是 topmost 且更晚激活，就压在它上面。
    ⇒ 所以弹出来之后再显式 `SetWindowPos(HWND_TOPMOST)` 顶一次
      （放到 topmost 带的**最上面**），并 `FlashWindowEx` 闪任务栏按钮，
      让人**即使没切窗口也能发现**。

    ⚠️ 仍然**不**调 SetForegroundWindow：抢前台会让游戏的按键送不进去
      （引擎有"前台守卫"，见文件头第 2 条）。**顶上去 + 闪，但依然不抢焦点。**

    ⚠️ **按线程 ID 找窗口，不要按标题找**（2026-09-16 踩）：
    第一版用 `FindWindowW(None, title)`，而标题是固定的 —— 只要**上一次的框还没关掉**
    （另一个进程弹的、或用户没点），就会找到那个**旧的**去 SetWindowPos，
    新的那个依旧压在后面。实测 A/B 直接反转：该在前的排到第 28 位。
    消息框由**本线程**创建，所以用 `EnumThreadWindows(thread_id)` 找，
    只会命中"这一线程的窗口"，不可能认错。

    在独立线程里轮询（MessageBoxW 是阻塞的，本函数不能和它同线程）。
    找不到就返回 False —— 只影响"能不能更醒目"，不影响告警本身。
    '''
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    HWND_TOPMOST = -1
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    FLASHW_ALL, FLASHW_TIMERNOFG = 0x00000003, 0x0000000C

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", wintypes.UINT),
                    ("uCount", wintypes.UINT),
                    ("dwTimeout", wintypes.UINT)]

    found = []

    def cb(hwnd, lparam):
        # 只收顶层可见窗口（消息框就是）；子控件也报一遍无妨，SetWindowPos 幂等
        if user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    enum_cb = EnumProc(cb)

    deadline = time.time() + float(timeout)
    hwnd = 0
    while time.time() < deadline:
        found.clear()
        user32.EnumThreadWindows(int(thread_id), enum_cb, 0)
        if found:
            hwnd = found[0]
            break
        time.sleep(0.1)
    if not hwnd:
        return False

    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                          FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:                                # noqa: BLE001
        return False


def _popup_worker(title: str, text: str) -> None:
    global _popup_open, _t_last_popup
    try:
        import ctypes
        # ⚠️ 必须在**本线程**里取线程 ID —— 消息框就是本线程创建的
        tid = ctypes.windll.kernel32.GetCurrentThreadId()
        # 先起"顶上去 + 闪"的帮手，再去弹框（弹框会阻塞到用户点掉）
        threading.Thread(target=_raise_and_flash, args=(tid,),
                         daemon=True).start()
        _messagebox_topmost(title, text)
    except Exception as e:                           # noqa: BLE001
        logger.warning(f"[卡死告警] 弹窗失败（只留日志，不影响挂机）：{e}")
    finally:
        with _lock:
            _popup_open = False
            _t_last_popup = time.time()


def _build_text(pos=None, seconds=None, cmd=None, extra: str = "") -> str:
    lines = ["挂机引擎检测到角色卡住（一直在原地没动）。", ""]
    if pos is not None:
        lines.append(f"位置：{pos}")
    if seconds is not None:
        lines.append(f"已卡住：约 {float(seconds):.1f} 秒")
    if cmd:
        lines.append(f"当时的指令：{cmd}")
    if extra:
        lines.append(str(extra))
    lines += ["",
              "引擎**不会**再乱动（免得把你推下平台）。",
              "请切到游戏看一眼，手动处理。"]
    return "\n".join(lines)


def notify_stuck(pos=None, seconds=None, cmd=None, extra: str = "",
                 cooldown: float = 60.0) -> bool:
    '''卡死告警：打一条醒目日志，并按需弹一个**置顶**窗口。

    Args:
        pos:     角色位置 (x, y)，写进提示里
        seconds: 已卡住多少秒
        cmd:     卡死那一刻的指令（"right none none" 之类）
        extra:   额外说明（可空）
        cooldown: 两次弹窗之间的最短间隔（秒）；弹窗没关之前一律不再弹

    Returns:
        bool: 本次**是否真的弹了窗**（测试/排查用；只记日志时返回 False）
    '''
    global _popup_open, _t_last_popup
    text = _build_text(pos, seconds, cmd, extra)
    # 日志永远打（这是"卡死可观测"的底线，关掉弹窗也保留）
    try:
        logger.warning("[卡死告警] " + text.replace("\n", " | "))
    except Exception:                                # noqa: BLE001
        pass

    if sys.platform != "win32":
        return False                                 # 非 Windows：只留日志

    with _lock:
        if _popup_open:
            return False                             # 已经有一个弹窗开着 → 不弹第二个
        if time.time() - _t_last_popup < float(cooldown):
            return False                             # 冷却中
        _popup_open = True

    try:
        threading.Thread(target=_popup_worker, args=(_TITLE, text),
                         daemon=True).start()
        return True
    except Exception as e:                           # noqa: BLE001
        logger.warning(f"[卡死告警] 起弹窗线程失败（只留日志）：{e}")
        with _lock:
            _popup_open = False
        return False


def _reset_for_test() -> None:
    '''清掉单实例/冷却状态（只给自检脚本用，运行时别调）。'''
    global _popup_open, _t_last_popup
    with _lock:
        _popup_open = False
        _t_last_popup = 0.0
