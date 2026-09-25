# -*- coding: utf-8 -*-
"""
量游戏窗口的真实尺寸 —— 所有坐标的地基

## 为什么需要它

项目里有两个互相矛盾的尺寸来源（交接简报 §八）：

  · 引擎裁剪目标：`config` 的 `game_window.size`（上游默认 1366x768）
  · 坐标归一化基准：`normalize_pixel_coordinate()` 里写死的 `693x1282`

而调用方（如 `get_login_button_location`）是**直接用归一化后的坐标去切实际画面**的
→ 坐标整体偏移。这两个值该取什么，只能拿真机窗口尺寸来定。本工具就是去量。

## 它量什么

全部为**物理像素**（已开启 DPI 感知，不受系统缩放干扰）：

  1. 窗口外框尺寸（含标题栏 + 边框）
  2. **客户区尺寸**（= 真实游戏画面，不含标题栏）→ 这就是 `game_window.size` 该填的值
  3. 真实标题栏高度（客户区顶端 - 窗口外框顶端）
  4. 用**项目自己的抓帧链路**（`GameWindowCapturor`）实抓一帧，看它到底含不含标题栏
     → 直接决定 `config` 的 `title_bar_height` 该填 0 还是真实值
  5. 当前 `game_window.size` / `title_bar_height` 与实测值的差异，以及会造成的后果

## 用法

  python -m tools.measure_window              # 标题从 config 的 game_window.title 读
  python -m tools.measure_window --token 冒险岛
  python -m tools.measure_window --no-capture # 只量窗口，不实抓（游戏没开也能看窗口）

抓到的画面存到 `debug/window_measure_<时间戳>.png`
"""
import argparse
import ctypes
import datetime
import os
import sys
import time

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cv2                                       # noqa: E402
import yaml                                      # noqa: E402
import win32api                                  # noqa: E402
import win32con                                  # noqa: E402
import win32gui                                  # noqa: E402

from src.input.GameWindowCapturor import GameWindowCapturor   # noqa: E402
from src.utils.common import (                   # noqa: E402
    find_game_window_hwnd, list_windows_like_game, load_yaml, imwrite_unicode)
from src.utils.paths import ensure_parent, resource_path      # noqa: E402

# 上游写死的归一化基准（common.normalize_pixel_coordinate）
UPSTREAM_STD_H, UPSTREAM_STD_W = 693, 1282


# ------------------------------------------------------------------ 基础

def enable_dpi_awareness():
    """
    让本进程按物理像素取窗口尺寸。
    不开启的话，系统缩放（如 125%）会让 GetWindowRect 返回被缩放过的逻辑值，
    量出来的尺寸比真实的小，坐标全废。
    """
    ok = None
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        ok = "per-monitor v2 (shcore)"
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            ok = "system aware (user32)"
        except Exception as e:
            ok = f"失败: {e}"
    return ok


def get_window_dpi(hwnd):
    """窗口所在显示器的 DPI（96 = 100% 缩放）"""
    try:
        return ctypes.windll.user32.GetDpiForWindow(hwnd)
    except Exception:
        return None


def describe_style(style):
    """把窗口样式翻译成人能看懂的话"""
    flags = []
    if style & win32con.WS_CAPTION:
        flags.append("有标题栏")
    if style & win32con.WS_THICKFRAME:
        flags.append("可调大小(有边框)")
    if style & win32con.WS_POPUP:
        flags.append("弹出式(WS_POPUP)")
    if style & win32con.WS_MAXIMIZE:
        flags.append("最大化")
    if not flags:
        flags.append("无标题栏/无边框（无边框窗口）")
    return "、".join(flags)


def collect_metrics(hwnd):
    """采集窗口的全部尺寸指标"""
    win_l, win_t, win_r, win_b = win32gui.GetWindowRect(hwnd)
    cli_l, cli_t, cli_r, cli_b = win32gui.GetClientRect(hwnd)
    cli_origin = win32gui.ClientToScreen(hwnd, (0, 0))
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)

    dpi = get_window_dpi(hwnd)
    monitor = win32api.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    try:
        mon_info = win32api.GetMonitorInfo(monitor)
        mon_rect = mon_info.get("Monitor")
        work_rect = mon_info.get("Work")
    except Exception:
        mon_rect = work_rect = None

    return {
        "hwnd": hwnd,
        "title": win32gui.GetWindowText(hwnd),
        "class": win32gui.GetClassName(hwnd),
        "style": style,
        "style_text": describe_style(style),
        "maximized": win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED,
        "win_rect": (win_l, win_t, win_r, win_b),
        "win_w": win_r - win_l,
        "win_h": win_b - win_t,
        "client_w": cli_r - cli_l,
        "client_h": cli_b - cli_t,
        "client_origin": cli_origin,
        "border_left": cli_origin[0] - win_l,
        "title_h": cli_origin[1] - win_t,
        "dpi": dpi,
        "scale": round(dpi / 96.0, 3) if dpi else None,
        "mon_rect": mon_rect,
        "mon_size": (mon_rect[2] - mon_rect[0], mon_rect[3] - mon_rect[1]) if mon_rect else None,
        "work_size": (work_rect[2] - work_rect[0], work_rect[3] - work_rect[1]) if work_rect else None,
    }


def capture_one_frame(window_title, wait=1.5):
    """用项目自身的抓帧链路抓一帧（和引擎跑起来时是同一套代码）"""
    cfg = {
        "system": {"fps_limit_window_capturor": 15},
        "game_window": {"title": window_title},
    }
    cap = GameWindowCapturor(cfg, window_title=window_title)
    t0 = time.time()
    img = None
    while time.time() - t0 < wait:
        img = cap.get_frame()
        if img is not None:
            break
        time.sleep(0.1)
    try:
        cap.stop()
    except Exception:
        pass
    return img


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="量游戏窗口真实尺寸")
    ap.add_argument("--token", default=None, help="窗口标题关键词（默认从 config 读）")
    ap.add_argument("--hwnd", type=int, default=0, help="直接指定窗口句柄（多候选时用）")
    ap.add_argument("--list", action="store_true", help="只列出候选窗口，不做测量")
    ap.add_argument("--no-capture", action="store_true", help="跳过实抓一帧")
    ap.add_argument("--write-config", action="store_true",
                    help="把实测值写进 config/config_custom.yaml")
    args = ap.parse_args()

    bar = "=" * 62
    print()
    print("  " + bar)
    print("   游戏窗口尺寸实测（分辨率链的地基）")
    print("  " + bar)

    # ---- 读配置 ----
    try:
        cfg = load_yaml(os.path.join(REPO_ROOT, "config", "config_default.yaml"))
        cfg_size = list(cfg["game_window"]["size"])          # [height, width]
        cfg_title_h = cfg["game_window"]["title_bar_height"]
        cfg_token = cfg["game_window"]["title"]
    except Exception as e:
        print(f"  [错误] 读 config/config_default.yaml 失败：{e}")
        return 2

    token = args.token or cfg_token
    print()
    print(f"  ① 环境")
    print(f"     DPI 感知：{enable_dpi_awareness()}")
    print(f"     系统主屏分辨率：{win32api.GetSystemMetrics(0)} x {win32api.GetSystemMetrics(1)}")
    print(f"     配置里的窗口标题关键词：{token!r}")

    # ---- 找窗口 ----
    candidates = list_windows_like_game(token)
    if args.list:
        print()
        print(f"  含「{token}」的候选窗口（已排除资源管理器/桌面/任务栏等外壳窗口）：")
        if not candidates:
            print("     （无）")
        for c in candidates:
            print(f"     hwnd={c['hwnd']:<10} {c['size'][0]}x{c['size'][1]:<6} "
                  f"class={c['class']:<24} title={c['title']!r}")
        return 0

    hwnd = 0
    if args.hwnd:
        hwnd = args.hwnd
        if not win32gui.IsWindow(hwnd):
            print(f"  [错误] --hwnd {hwnd} 不是有效窗口句柄")
            return 1
    else:
        hwnd = find_game_window_hwnd(token)

    if not hwnd:
        print()
        print("  [错误] 没找到标题里含「%s」的窗口。" % token)
        if candidates:
            print("         （有候选但没通过筛选，用 --list 看细节）")
        print()
        print("  请先做这几件事：")
        print("     1) 打开游戏，并进入**窗口模式**（不是全屏）")
        print("     2) 别把它最小化")
        print("     3) 如果窗口标题和「%s」不一样，用 --token 指定，"
              "或改 config 的 game_window.title" % token)
        return 1

    if len(candidates) > 1 and not args.hwnd:
        print()
        print(f"  [注意] 含「{token}」的窗口有 {len(candidates)} 个，本次量的是面积最大的那个：")
        print(f"         {win32gui.GetWindowText(hwnd)!r}")
        print("         如果量错了，用 --list 看清候选、再用 --hwnd 指定。")

    m = collect_metrics(hwnd)

    # ---- ② 窗口尺寸 ----
    print()
    print("  ② 窗口尺寸（物理像素）")
    print(f"     窗口标题：{m['title']!r}")
    print(f"     窗口类名：{m['class']!r}")
    print(f"     窗口状态：{m['style_text']}"
          + ("　【当前最大化】" if m["maximized"] else ""))
    print(f"     外框尺寸（含标题栏+边框）：{m['win_w']} x {m['win_h']}")
    print(f"     客户区尺寸（真实游戏画面）：{m['client_w']} x {m['client_h']}   ← 关键值")
    print(f"     客户区屏幕原点：{m['client_origin']}")
    print(f"     左边框宽度：{m['border_left']} px")
    print(f"     真实标题栏高度：{m['title_h']} px")
    if m["dpi"]:
        print(f"     窗口 DPI：{m['dpi']}（缩放 {int(m['scale']*100)}%）")
    if m["mon_size"]:
        print(f"     所在显示器：{m['mon_size'][0]} x {m['mon_size'][1]}"
              f"，可用工作区 {m['work_size'][0]} x {m['work_size'][1]}")
        if m["win_w"] >= m["mon_size"][0] and m["win_h"] >= m["mon_size"][1]:
            print("     【注意】 窗口尺寸 ≈ 整屏 → 这是无边框全屏，请切成窗口模式再量")

    # ---- ③ 实抓一帧 ----
    cap_w = cap_h = None
    saved = None
    if not args.no_capture:
        print()
        print("  ③ 实抓一帧（用项目自己的抓帧链路 GameWindowCapturor）")
        print("     正在抓 ...")
        try:
            img = capture_one_frame(win32gui.GetWindowText(hwnd))
        except Exception as e:
            img = None
            print(f"     [警告] 抓帧报错：{type(e).__name__}: {e}")
        if img is None:
            print("     [警告] 没抓到画面（窗口最小化 / 被抓帧接口挡住）。")
            print("            窗口尺寸仍然有效，但「抓帧含不含标题栏」这次没测出来。")
        else:
            cap_h, cap_w = img.shape[:2]
            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            saved = ensure_parent(f"debug/window_measure_{ts}.png")
            imwrite_unicode(saved, img)
            print(f"     抓到的帧尺寸：{cap_w} x {cap_h}")
            print(f"     已存到：{saved}")
    else:
        print()
        print("  ③ 实抓一帧：已跳过（--no-capture）")

    # ---- ④ 结论 ----
    print()
    print("  ④ 结论：抓到的帧到底含不含标题栏？")
    verdict = None
    if cap_w is None:
        print("     未抓到帧，本项无法判定（窗口尺寸仍然有效）。")
    else:
        extra_w = cap_w - m["client_w"]
        extra_h = cap_h - m["client_h"]
        print(f"     帧比客户区多出：宽 {extra_w:+d}px　高 {extra_h:+d}px")
        if extra_w < 0 or extra_h < 0:
            verdict = "other"
            print("     【存疑】 帧比客户区还小，不符合预期，需要人工判断：")
            print(f"        客户区 {m['client_w']}x{m['client_h']}"
                  f"　外框 {m['win_w']}x{m['win_h']}"
                  f"　抓帧 {cap_w}x{cap_h}")
        elif (extra_w, extra_h) == (0, 0):
            verdict = "client"
            print("     【OK】 帧 == 客户区 → 抓帧已排除标题栏。")
            print("        → title_bar_height 必须为 0。")
            if cfg_title_h:
                print(f"        【注意】 现在是 {cfg_title_h}：会白切掉顶部 {cfg_title_h} 行真实画面，"
                      f"所有 y 坐标偏高 {cfg_title_h}px。")
        elif extra_w <= 4 and 0 < m["title_h"] <= extra_h <= m["title_h"] + 4:
            verdict = "titlebar"
            side = extra_w // 2
            bottom = extra_h - m["title_h"]
            print(f"     【OK】 帧 = 客户区 + 标题栏 + 窗口边框 → **含标题栏**（实测口径）。")
            print(f"        帧内布局：顶部 {m['title_h']}px 标题栏"
                  f"，左右各 {side}px 边框，底部 {bottom}px 边框")
            print(f"        → title_bar_height 必须填**真实标题栏高度** {m['title_h']}（不能填 0）。")
            print("        引擎 get_img_frame() 会：切掉标题栏 → 锚定左上角裁到 game_window.size"
                  " → 落点正好是客户区。")
            if cfg_title_h != m["title_h"]:
                print(f"        【注意】 现在填的是 {cfg_title_h}，与实测 {m['title_h']} 差 "
                      f"{cfg_title_h - m['title_h']:+d}px，y 坐标会整体偏这么多。")
        else:
            verdict = "other"
            print("     【存疑】 不符合已知口径，需要人工判断：")
            print(f"        客户区 {m['client_w']}x{m['client_h']}"
                  f"　外框 {m['win_w']}x{m['win_h']}"
                  f"　抓帧 {cap_w}x{cap_h}"
                  f"　实测标题栏高 {m['title_h']}")
            print("        常见原因：窗口处于**最大化 / 无边框全屏**，系统改写了标题栏几何")
            print("        （最常见的就是标题栏高量出 0）。这种窗口量出来的值不可信。")
            print("        请切成**普通窗口模式**（不要最大化、不要全屏）再量一次。")

    if m["maximized"]:
        print()
        print("     【注意】 当前窗口是**最大化**状态：最大化窗口的标题栏几何会被系统改写，")
        print("        量出来的标题栏高度不可信。请还原成普通窗口再量。")
    elif m["mon_size"] and m["win_w"] >= m["mon_size"][0] and m["win_h"] >= m["mon_size"][1]:
        print()
        print("     【注意】 窗口铺满了整个显示器（无边框全屏或铺满屏），同样量不到真实标题栏。")
        print("        必须切成普通窗口再量。")

    # 该往 config 里写多少
    if verdict == "client":
        title_bar_out = 0
    elif verdict == "titlebar":
        title_bar_out = m["title_h"]
    else:
        title_bar_out = m["title_h"]

    # ---- ⑤ 与配置对比 + 建议 ----
    real_h, real_w = m["client_h"], m["client_w"]
    print()
    print("  ⑤ 与当前 config 对比")
    print(f"     实测客户区（高, 宽）= [{real_h}, {real_w}]")
    print(f"     配置 game_window.size = {cfg_size}（注释是 [height, width]）")
    if cfg_size == [real_h, real_w]:
        print("     【OK】 一致，不用改。")
    else:
        dw = cfg_size[1] - real_w
        dh = cfg_size[0] - real_h
        print(f"     【失败】 不一致：宽差 {dw:+d}、高差 {dh:+d}")
        if dw > 0 or dh > 0:
            print(f"        → get_img_frame() 会把帧锚定左上角裁掉多余的 {dw} x {dh} 像素，"
                  f"客户端画面比配置小、右侧与底部内容被丢弃。")
        else:
            print(f"        → 画面比配置大，get_img_frame() 不裁剪，"
                  f"实际帧仍是 {real_w}x{real_h}，与配置声明不符，坐标会整体失准。")

    # 归一化基准
    print()
    print(f"     ui_coords 录制基准：config 的 game_window.coord_base_size"
          f"（留 ~ 表示同 game_window.size → 恒等换算）")
    print(f"     实测客户区宽高比 {real_w/real_h:.4f}"
          f"　上游曾写死的 1282x693 基准宽高比 {UPSTREAM_STD_W/UPSTREAM_STD_H:.4f}"
          f"　相差 {abs(real_w/real_h - UPSTREAM_STD_W/UPSTREAM_STD_H):.4f}")
    print(f"     → 上游那套 1282x693 的 ui_coords 对国服已作废，必须按真实画面尺寸重录。")

    print()
    print("  ⑥ 建议写入 config/config_custom.yaml：")
    print()
    print("     game_window:")
    print(f"       size: [{real_h}, {real_w}]          # 实测客户区 [高, 宽]，不含标题栏")
    print(f"       title_bar_height: {title_bar_out}"
          + ("           # 抓帧已排除标题栏，填 0" if verdict == "client"
             else "           # 抓帧含标题栏，填实测标题栏高度" if verdict == "titlebar"
             else "           # 抓帧口径未判定，先按实测标题栏高度填，待确认"))
    print()

    if args.write_config:
        full_screen = (m["maximized"]
                       or (m["mon_size"] and m["win_w"] >= m["mon_size"][0]
                           and m["win_h"] >= m["mon_size"][1]))
        if full_screen or verdict in (None, "other"):
            why = ("当前窗口是最大化 / 铺满全屏，标题栏几何被系统改写，量出来的值不可信"
                   if full_screen else
                   "抓帧口径没判定出来，量出来的值不可信")
            print(f"  【拒绝】 拒绝写入配置：{why}。")
            print("     请把游戏还原成**普通窗口**（不要最大化、不要全屏）后重新运行。")
            print()
            print("  " + bar)
            print()
            return 1
        write_config(real_h, real_w, title_bar_out)
    else:
        print("     想直接应用，重跑并加 --write-config：")
        print("       python -m tools.measure_window --write-config")
        print()

    print("  " + bar)
    print()
    return 0


def write_config(real_h, real_w, title_bar_h):
    """
    把实测值写进 config/config_custom.yaml 的 game_window 段（用户覆盖层）。

    为什么写到 custom 而不是 config_default.yaml：
      default 是「上游默认值 + 注释文档」，会被上游更新覆盖；custom 是用户自己的
      覆盖层，界面关闭时本来就往这里写差异，属于既定的正确位置。
    """
    path = resource_path("config/config_custom.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    except Exception as e:
        print(f"     [错误] 读 {path} 失败：{e}")
        return

    gw = data.get("game_window")
    if not isinstance(gw, dict):
        gw = {}
        data["game_window"] = gw
    gw["size"] = [real_h, real_w]
    gw["title_bar_height"] = title_bar_h

    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False,
                           sort_keys=False)
    except Exception as e:
        print(f"     [错误] 写 {path} 失败：{e}")
        return

    print(f"     【OK】 已写入 {path}")
    print(f"        game_window.size = [{real_h}, {real_w}]")
    print(f"        game_window.title_bar_height = {title_bar_h}")
    print("        （config_default.yaml 未被改动，custom 会覆盖它）")


if __name__ == "__main__":
    sys.exit(main())
