# -*- coding: utf-8 -*-
"""
抓取游戏窗口画面并存档 —— 只抓图，不开任何交互窗口

两种用法：
  ① 单张（默认）
     抓当前这一帧，存到 debug/capture_raw_<时间戳>.png
     用来判断怪物检测该用哪种模式（有没有黑描边、什么颜色、多大）。

  ② 连拍（--count N --interval S）
     在 N*S 秒内每 S 秒抓一张，存成 debug/capture_raw_<时间戳>_NN.png
     用途：**边走边抓**，一次拿到同一张地图里多个位置的画面 ——
     用来验证「角色在屏幕上的位置会不会随走位移动」，以及统计名字被别的玩家
     挡住（匹配失败）的实际比例。

命令行：
  python -m tools.grab_frame                       # 抓一张
  python -m tools.grab_frame --count 10            # 连拍 10 张，每秒一张
  python -m tools.grab_frame --count 12 --interval 0.5

工具箱的「保存诊断包」也会自动抓 3 张（它内部就是调本工具），排查问题通常用那个就够。
"""
import argparse
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.utils.common import load_yaml, imwrite_unicode            # noqa: E402
from tools.template_capture import grab_game_frame, save_raw_snapshot  # noqa: E402


def burst(token, count, interval):
    """
    保持一条抓帧会话，按 interval 存 count 张。
    为什么不循环调用 grab_game_frame：那个函数每次都会重建窗口捕获会话，
    连拍时开销大、还可能因为重连丢帧。
    """
    from windows_capture import WindowsCapture
    from src.utils.common import find_game_window_hwnd

    hwnd = find_game_window_hwnd(token)
    if not hwnd:
        print("   [错误] 没找到标题里含「%s」的游戏窗口。" % token)
        print("          请先把游戏开起来（窗口模式、不要最小化）。")
        return 1
    print("   [信息] 游戏窗口 hwnd=%s" % hwnd)

    holder = {"latest": None, "frames": 0}
    cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, draw_border=False)

    @cap.event
    def on_frame_arrived(frame, capture_control):
        holder["latest"] = frame.frame_buffer.copy()
        holder["frames"] += 1

    @cap.event
    def on_closed():
        pass

    control = cap.start_free_threaded()
    t0 = time.time()
    # 等首帧
    while holder["latest"] is None and time.time() - t0 < 8.0:
        time.sleep(0.05)
    if holder["latest"] is None:
        print("   [错误] 8 秒内没抓到画面（窗口最小化了？）")
        try:
            control.stop()
        except Exception:
            pass
        return 1

    outdir = os.path.join(ROOT, "debug")
    os.makedirs(outdir, exist_ok=True)
    saved = []
    next_t = time.time()
    t_end = time.time() + interval * count
    print()
    print("   >>> 现在请开始走位（脚本在后台自动抓，抓完会自己停）<<<")
    print()
    while len(saved) < count and time.time() < t_end:
        if time.time() >= next_t:
            img = holder["latest"]
            if img is not None and img.ndim == 3 and img.shape[2] == 4:
                import cv2
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            if img is None:
                print("   [警告] 第 %d 张没取到画面，跳过" % (len(saved) + 1))
            else:
                ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                p = os.path.join(outdir, "capture_raw_%s_%02d.png" % (ts, len(saved) + 1))
                imwrite_unicode(p, img)
                saved.append(p)
                print("   已抓 %d/%d  (%dx%d)  %s"
                      % (len(saved), count, img.shape[1], img.shape[0],
                         os.path.basename(p)))
            next_t += interval
        time.sleep(0.02)

    try:
        control.stop()
    except Exception:
        pass

    print()
    print("   ============================================")
    print("   连拍结束：这次抓到 %d 张（共收到 %d 帧）" % (len(saved), holder["frames"]))
    print("   都在 debug\\ 目录下，文件名形如 capture_raw_<时间戳>_NN.png")
    print("   ============================================")
    return 0 if saved else 1


def main():
    ap = argparse.ArgumentParser(description="抓游戏画面（单张 / 连拍）")
    ap.add_argument("--count", type=int, default=1,
                    help="抓多少张（默认 1；>1 进入连拍模式）")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="连拍间隔秒数（默认 1.0；太短会撞文件名，建议 >=1）")
    a = ap.parse_args()

    try:
        cfg = load_yaml(os.path.join(ROOT, "config", "config_default.yaml"))
        token = cfg["game_window"]["title"]
    except Exception as e:
        print("[错误] 读配置失败：%s" % e)
        return 2

    if a.count > 1:
        if a.interval < 1.0:
            print("   [提示] 间隔小于 1 秒时文件名的秒级时间戳可能重复，"
                  "已自动改成 1.0 秒。")
            a.interval = 1.0
        print("   连拍模式：共 %d 张，每 %.1f 秒一张（约 %.0f 秒）"
              % (a.count, a.interval, a.count * a.interval))
        return burst(token, a.count, a.interval)

    print("   正在从标题含「%s」的窗口抓取画面 ..." % token)
    print()
    img = grab_game_frame(token)
    if img is None:
        print("   没抓到画面。请确认：")
        print("     1) 游戏已经打开，并且不是最小化状态")
        print("     2) 游戏窗口标题里包含「%s」" % token)
        return 1

    path = save_raw_snapshot(img)
    h, w = img.shape[:2]

    print()
    print("   ============================================")
    print("   画面尺寸：%d x %d" % (w, h))
    print("   已保存到：%s" % path)
    print("   ============================================")
    print()
    print("   把上面这个路径告诉 AI，就可以开始分析画面特征了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
