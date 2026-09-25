# -*- coding: utf-8 -*-
"""
诊断包收集 —— 挂机过程出问题时点一下（工具箱「保存诊断包」按钮），
把排查需要的信息一次性收集到 debug/诊断_<时间戳>/ 下，可直接整个目录发给 AI。

收集内容（**目录结构与仓库一致**，收件人可直接把本包盖回一份仓库副本，再用现成工具复查）：

    诊断_<时间戳>/
    ├─ 摘要.txt                     ← 先看这个
    ├─ log/MSBot_*.log              最新 3 份引擎日志
    ├─ config/*.yaml                config_data / config_custom / config_default 副本
    ├─ nametag/*.png                名字模板
    ├─ monster/<名>/*.png           怪物模板
    └─ debug/capture_raw_*.png      **游戏画面**（默认现场抓 3 张）

⚠️ 为什么镜像仓库结构而不是用「抓帧/」「模板/」这种中文名：
   verify_nametag / verify_monster 这些工具是**写死路径**的
   （它们 glob `debug/capture_raw_*.png` 与 `nametag/*`）。
   镜像之后收件人不用手工搬文件，直接复用现成检查。

⚠️ 为什么画面和模板必须收（2026-09-15 加）：
   只有日志和配置，收件人**没法**判断「模板质量」—— 名字模板截得对不对、
   怪物模板会不会误报，只能看图和模板本身。而模板只有几 KB，画面 1 张约 1 MB。
   ⇒ 有了这两样，收件人才能离线跑 verify_nametag / verify_monster 那套检查，
   不必让用户自己看「分离度 / 分数 / 阈值」这类技术报告。

⚠️ 为什么默认「现场抓」而不是直接拿 debug 里的旧帧：
   debug/ 里的抓帧可能是几天前的，拿来诊断**今天**出的问题没有意义。
   现场抓失败（游戏没开 / 最小化）才退回旧帧，且会在摘要里写明「这是旧帧」。

用法：python -m tools.diagnose            # 默认现场抓 3 张画面
      python -m tools.diagnose --frames 5 # 抓 5 张
      python -m tools.diagnose --frames 0 # 不抓画面（只要日志 + 配置 + 模板）
"""
import argparse
import ctypes
import datetime
import glob
import os
import shutil
import sys
import time

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO_ROOT)

try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_game_window():
    """返回游戏窗口的 (句柄描述, 客户区尺寸) 或未找到提示。"""
    if not HAS_WIN32:
        return "win32gui 不可用，跳过窗口检查"
    try:
        hwnd = win32gui.FindWindow(None, "冒险岛怀旧服")
        if not hwnd:
            return "未找到标题为「冒险岛怀旧服」的窗口（游戏没开 / 最小化 / 标题不同）"
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        return (f"hwnd={hwnd}，客户区 {right-left}x{bottom-top} "
                f"（配置声明应为 1366x768）")
    except Exception as e:
        return f"检查窗口时出错: {e}"


def list_resources():
    lines = []
    for label, pattern in (("minimaps", "minimaps/*/*"),
                           ("monster", "monster/*/*"),
                           ("nametag", "nametag/*"),
                           ("misc", "misc/*")):
        files = sorted(glob.glob(pattern))
        lines.append(f"[{label}] {len(files)} 个文件")
        for f in files[:20]:
            lines.append(f"    {f}")
        if len(files) > 20:
            lines.append(f"    ...（其余 {len(files)-20} 个省略）")
    return "\n".join(lines)


def tail_errors(log_path, n=30):
    """提取日志里最后 n 条 ERROR / WARNING 行。"""
    if not os.path.exists(log_path):
        return "（日志文件不存在）"
    hits = []
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            if "ERROR" in line or "WARNING" in line:
                hits.append(line.rstrip())
    if not hits:
        return "（该日志中没有 ERROR / WARNING）"
    return "\n".join(hits[-n:])


# ------------------------------------------------------------------ 画面 / 模板

def capture_fresh(n, interval=1.0):
    """现场抓 n 张游戏画面（存到 debug/，沿用抓帧工具的命名）。

    Returns:
        (paths, note) —— note 是人话说明，直接写进摘要.txt
    """
    if n <= 0:
        return [], "按参数跳过现场抓帧"
    try:
        from src.utils.common import load_yaml, find_game_window_hwnd
        from tools.template_capture import grab_game_frame, save_raw_snapshot
    except Exception as e:                                # noqa: BLE001
        return [], f"抓帧模块导入失败：{e}"
    try:
        token = load_yaml(os.path.join(
            REPO_ROOT, "config", "config_default.yaml"))["game_window"]["title"]
    except Exception as e:                                # noqa: BLE001
        return [], f"读游戏窗口标题失败：{e}"

    if not find_game_window_hwnd(token):
        return [], f"没找到标题含「{token}」的游戏窗口（没开 / 最小化 / 标题不同）"

    paths = []
    for i in range(n):
        img = grab_game_frame(token)
        if img is None:
            # 第一张就失败就别再耗时间（每次失败最多要等 8 秒）
            return paths, f"第 {len(paths)+1} 张抓取失败，提前停止"
        paths.append(save_raw_snapshot(img))
        if i < n - 1:
            time.sleep(interval)
    return paths, f"现场抓取 {len(paths)} 张"


def collect_frames(out_dir, n):
    """把 debug/ 里**最新的 n 张**抓帧收进包里（现场刚抓的自然排在最前）。"""
    if n <= 0:
        return []
    frames = sorted(glob.glob("debug/capture_raw_*.png"), key=os.path.getmtime)[-n:]
    if not frames:
        return []
    fdir = os.path.join(out_dir, "debug")
    os.makedirs(fdir, exist_ok=True)
    for src in frames:
        shutil.copy2(src, fdir)
    return frames


def collect_templates(out_dir):
    """把名字模板与怪物模板收进包里（保持 nametag/ 与 monster/<名>/ 结构）。"""
    copied = []
    for pattern in ("nametag/*.png", "monster/*/*.png"):
        for src in sorted(glob.glob(pattern)):
            dst = os.path.join(out_dir, src)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(src)
    return copied


def fmt_time(p):
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:                                     # noqa: BLE001
        return "时间未知"


def main():
    ap = argparse.ArgumentParser(description="收集诊断包（日志 / 配置 / 画面 / 模板）")
    ap.add_argument("--frames", type=int, default=3,
                    help="现场抓几张游戏画面（默认 3；填 0 = 不抓也不收画面）")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="现场抓帧的间隔秒数（默认 1.0）")
    a = ap.parse_args()

    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_dir = os.path.join("debug", f"诊断_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    # 1. 最新 3 份日志
    logs = sorted(glob.glob("log/MSBot_*.log"), key=os.path.getmtime)[-3:]
    latest = logs[-1] if logs else None
    log_dir = os.path.join(out_dir, "log")
    os.makedirs(log_dir, exist_ok=True)
    for src in logs:
        shutil.copy2(src, log_dir)

    # 2. 配置副本
    cfg_dir = os.path.join(out_dir, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    for cfg in ("config/config_data.yaml", "config/config_custom.yaml",
                "config/config_default.yaml"):
        if os.path.exists(cfg):
            shutil.copy2(cfg, cfg_dir)

    # 3. 现场抓帧（先抓；第 4 步按「最新」取，抓到的自然就是最新的）
    if a.frames > 0:
        print(f"[诊断] 正在抓游戏画面（{a.frames} 张，间隔 {a.interval}s）...")
    fresh, cap_note = capture_fresh(a.frames, a.interval)
    print(f"[诊断] 抓帧结果：{cap_note}")

    # 4. 画面 + 模板
    frames = collect_frames(out_dir, a.frames)
    templates = collect_templates(out_dir)

    # 5. 摘要
    summary = ["=" * 60, f"诊断包生成时间: {ts}", "=" * 60, ""]
    summary.append(f"Python: {sys.version.split()[0]}  （管理员: {'是' if is_admin() else '否'}）")
    summary.append(f"游戏窗口: {check_game_window()}")
    summary.append("")
    summary.append("---- 本次收集 ----")
    summary.append(f"日志      : {len(logs)} 份")
    summary.append(f"游戏画面  : {len(frames)} 张（{cap_note}）")
    for f in frames:
        summary.append(f"    {os.path.basename(f)}    拍摄时间 {fmt_time(f)}")
    summary.append(f"模板图    : {len(templates)} 张")
    for t in templates:
        summary.append(f"    {t}")
    if frames and not fresh:
        summary.append("")
        summary.append("⚠️ 上面这些画面是 debug/ 里的**旧帧**（现场抓帧失败，原因见上），")
        summary.append("   未必是出问题当时的画面 —— 若需要现场画面，请开好游戏后重跑一次。")
    summary.append("")
    summary.append("---- 收件人怎么用（离线复现检查）----")
    summary.append("本包目录结构与仓库一致，可直接盖回一份仓库副本后复用现成工具：")
    summary.append(r"    xcopy /E /Y <本目录>\*  <仓库副本>\ ")
    summary.append("然后在新控制台里跑（都在仓库副本根目录）：")
    summary.append("    python -m tools.verify_nametag     # 名字定位够不够（用本包 debug/ 里的画面）")
    summary.append("    python -m tools.verify_monster     # 怪物模板会不会误报（同上）")
    summary.append("    python -m tools.home_route_health  # 回正线体检（只查路线资源，与画面无关）")
    summary.append("")
    summary.append("---- 资源清单 ----")
    summary.append(list_resources())
    summary.append("")
    if latest:
        summary.append(f"---- 最新日志 {latest} 的 ERROR/WARNING（最后 30 条）----")
        summary.append(tail_errors(latest))
    with open(os.path.join(out_dir, "摘要.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary))

    print(f"[诊断] 已收集 {len(logs)} 份日志 + 配置 + {len(frames)} 张画面 "
          f"+ {len(templates)} 张模板")
    print(f"[诊断] 输出目录: {os.path.abspath(out_dir)}")
    print("[诊断] 排查问题时把这个目录整个发给 AI 即可。")
    try:
        os.startfile(os.path.abspath(out_dir))
    except Exception:                                     # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
