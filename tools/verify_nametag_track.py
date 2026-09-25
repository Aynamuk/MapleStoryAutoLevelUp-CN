# -*- coding: utf-8 -*-
"""
名字标签定位 —— 轨迹连续性验证器

用途
----
verify_nametag.py 回答「能不能认出我的名字」；本工具回答「连续玩的时候锁的
一直是同一个角色吗」——把一组**按时间顺序的连拍帧**逐帧过引擎真方法，
检查相邻帧位移是否平缓。

原理
----
真锁住角色：相邻帧位移应与走位速度同量级（实测约 57px/秒，镜头跟随会让
屏幕坐标变化更小）。单帧跳变上百像素 = 大概率匹配到了别人/别的图案。
注意：镜头跟随时屏幕位置几乎不动是**正常**的，不能照搬 verify_nametag
里「位置完全不动 = 锁静态图案」的警告（那条针对的是无时间关系的散帧）。

判据
----
1. 命中帧的 score 全部 < diff_thres（同 verify_nametag）；
2. 相邻命中帧位移 <= --jump 阈值（默认 60px，约为实测走位单帧位移的 10 倍）；
3. 未命中帧允许存在（遮挡时引擎沿用上一帧），但连续 miss 段过后的第一个
   命中帧若出现大位移，要人工核对图。

用法
----
  python -m tools.verify_nametag_track                       # 跑最新一组连拍，跑完自动弹轨迹图
  python -m tools.verify_nametag_track --frames 10           # 组内只取最近 10 张
  python -m tools.verify_nametag_track --prefix capture_raw_2026-09-10_18-38
  python -m tools.verify_nametag_track --all                 # 跑 debug 里全部帧（含旧组）
  python -m tools.verify_nametag_track --no-show             # 不自动打开轨迹图
  python -m tools.verify_nametag_track --jump 30             # 收紧跳变阈值
"""
import argparse
import glob
import os

import sys

import cv2
import numpy as np

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# 直接复用 verify_nametag 的探针与单帧执行器 —— 跑的是同一份代码
from tools.verify_nametag import load_cfg, build_probe, run_one, OUT_DIR  # noqa: E402
from src.utils.common import imread_unicode, imwrite_unicode              # noqa: E402

# capture_raw_2026-09-11_23-40-45_10.png —— 注意时间戳是**该帧拍摄时刻**（每帧递增），
# 不是会话开始时间，所以没法按文件名前缀分会话；改按文件修改时间切组：
# 相邻帧间隔超过 SESSION_GAP 秒即视为两次抓帧会话。
SESSION_GAP = 60.0


def latest_group(paths):
    """输入按 mtime 排序的帧路径，返回 (最后一组帧, 组的起始文件名)。"""
    groups = []
    for p in sorted(paths, key=os.path.getmtime):
        if groups and os.path.getmtime(p) - os.path.getmtime(groups[-1][-1]) <= SESSION_GAP:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups[-1], os.path.basename(groups[-1][0])


def draw_track(canvas, points, labels):
    """把逐帧落点画成折线：绿=起点 红=终点 灰=命中段 橙=跨未命中段。"""
    for i in range(1, len(points)):
        p0, p1, lab = points[i - 1], points[i], labels[i]
        if p0 is None or p1 is None:
            continue
        color = (255, 128, 0) if lab == "miss" else (160, 160, 160)
        cv2.line(canvas, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])),
                 color, 1, cv2.LINE_AA)
    for i, p in enumerate(points):
        if p is None:
            continue
        color = (0, 255, 0) if i == 0 else ((0, 0, 255) if i == len(points) - 1
                                            else (255, 255, 255))
        cv2.circle(canvas, (int(p[0]), int(p[1])), 4, color, -1, cv2.LINE_AA)
        cv2.putText(canvas, str(i), (int(p[0]) + 6, int(p[1]) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser(description="名字标签定位轨迹连续性验证（调用引擎真方法）")
    ap.add_argument("--prefix", default="",
                    help="只跑文件名以该前缀开头的那组连拍（如 capture_raw_2026-09-10_18-38）")
    ap.add_argument("--frames", type=int, default=0,
                    help="组内只取最近 N 张（0=整组）")
    ap.add_argument("--all", action="store_true",
                    help="跑 debug 里全部抓帧（默认只跑最新一组连拍，避免旧帧混进轨迹）")
    ap.add_argument("--no-show", action="store_true",
                    help="跑完后不自动打开轨迹图")
    ap.add_argument("--jump", type=float, default=120.0,
                    help="相邻命中帧的位移上限 px（默认 120；连拍约 1 秒/帧、"
                         "走位约 57px/秒，再留一倍余量）")
    a = ap.parse_args()

    cfg = load_cfg()
    thres = cfg["nametag"]["diff_thres"]
    paths = sorted(glob.glob(os.path.join(REPO_ROOT, "debug", "capture_raw_*.png")))
    if a.prefix:
        paths = [p for p in paths if os.path.basename(p).startswith(a.prefix)]
    group_key = None
    if a.all:
        if a.frames:
            paths = paths[-a.frames:]
    else:
        paths, group_key = latest_group(paths)
        if a.frames:
            paths = paths[-a.frames:]
    if len(paths) < 3:
        print("[错误] 连拍帧不足 3 张。先用 python -m tools.grab_frame --count 10 边走边抓一组，再跑本工具。")
        return 2

    print("=" * 76)
    if group_key:
        print(" 名字标签定位 —— 轨迹连续性验证（组 %s，共 %d 帧）" % (group_key, len(paths)))
    else:
        print(" 名字标签定位 —— 轨迹连续性验证（共 %d 帧）" % len(paths))
    print("=" * 76)
    print(f" 判定阈值 score < {thres}    跳变阈值 {a.jump:.0f}px"
          f"    （实测走位约 57px/秒，镜头跟随时屏幕位移更小）")
    print()

    bot = build_probe(cfg)
    os.makedirs(OUT_DIR, exist_ok=True)

    rows, points, labels = [], [], []
    n_hit, n_miss, n_eval = 0, 0, 0
    jumps, miss_gaps = [], []
    last_hit_pt = None
    n_miss_since_hit = 0
    for p in paths:
        raw = imread_unicode(p, cv2.IMREAD_COLOR)
        if raw is None:
            continue
        loc_nt, loc_player, score, dbg, err = run_one(bot, cfg, raw)
        if err is not None:
            print(f"{os.path.basename(p):<40}  跳过（{err}）")
            continue
        n_eval += 1
        ok = (score is not None and score < thres and loc_nt != (0, 0))
        step = ""
        if ok:
            n_hit += 1
            if last_hit_pt is not None:
                d = float(np.hypot(loc_nt[0] - last_hit_pt[0], loc_nt[1] - last_hit_pt[1]))
                # 阈值按隔了多少帧线性放宽：角色一直在走，未命中期间引擎在沿用旧位置，
                # 隔 k 帧的位移本就该是单帧的 k+1 倍（默认单帧上限 --jump）。
                allowed = a.jump * (1 + n_miss_since_hit)
                step = f"{d:.1f}px"
                detail = (os.path.basename(p), d, last_hit_pt, loc_nt, n_miss_since_hit)
                if n_miss_since_hit and d > allowed:
                    miss_gaps.append(detail)
                elif not n_miss_since_hit and d > a.jump:
                    jumps.append(detail)
            last_hit_pt = loc_nt
            n_miss_since_hit = 0
            points.append(loc_nt)
            labels.append("hit")
        else:
            n_miss += 1
            n_miss_since_hit += 1
            points.append(last_hit_pt)   # 引擎行为：沿用上一帧 —— 轨迹图上显示为原地
            labels.append("miss")
        rows.append((os.path.basename(p), score, loc_nt, step, ok))
        # 逐帧存标注图（跳变帧人工核对就靠它；红叉=定位点，绿框=名字）
        if dbg is not None:
            imwrite_unicode(os.path.join(OUT_DIR, "track_nt_" + os.path.basename(p)), dbg)

    print(f"{'帧':<40}{'score':>8}  {'定位':>14}  {'位移':>8}  判定")
    print("-" * 76)
    for name, score, loc, step, ok in rows:
        s = "%.4f" % score if score is not None else "None"
        print(f"{name:<40}{s:>8}  {str(loc):>14}  {step:>8}  {'OK' if ok else '**未命中**'}")
    print("-" * 76)
    print(f" 评估 {n_eval} 帧：命中 {n_hit} / 未命中 {n_miss}")

    verdict = "OK"
    if n_hit < 2:
        print(" 结论：【失败】 命中帧不足 2 帧，谈不上轨迹。先解决定位本身（跑 verify_nametag）。")
        verdict = "FAIL"
    else:
        print(f" 跳变检查（帧间上限 {a.jump:.0f}px，隔 k 帧放宽 k+1 倍）："
              f"{'未发现' if not (jumps or miss_gaps) else '%d 处超阈值' % (len(jumps) + len(miss_gaps))}")
        for name, d, p0, p1, gap in jumps + miss_gaps:
            extra = f"（中间隔了 {gap} 帧未命中）" if gap else ""
            print(f"   · {name}  位移 {d:.0f}px  {p0} -> {p1}{extra}")
        if jumps or miss_gaps:
            verdict = "WARN"
            print("   核对方法：打开 debug/analysis/ 下同名的 track_nt_*.png，")
            print("   看红叉是不是落在了别的玩家/图案上（而不是你的角色）。")
            print("   【提醒】若红叉始终落在你的名牌上，跳变就是真实移动（跳跃/绳梯），")
            print("   不是锁错人 —— 此时定位可以放心用。")
        if n_miss:
            print(f" 【说明】有 {n_miss} 帧未命中（遮挡/半透明）——引擎会沿用上一帧，"
                  f"轨迹图上表现为原地停留，属正确行为；跨未命中段的位移已按帧数放宽阈值。")
        print(f" 命中帧 score 范围：{min(r[1] for r in rows if r[4]):.4f}"
              f" ~ {max(r[1] for r in rows if r[4]):.4f}（阈值 {thres}）")

    # 轨迹可视化：把折线画在最后一帧上
    raw_last = imread_unicode(paths[-1], cv2.IMREAD_COLOR)
    if raw_last is not None and n_hit >= 2:
        draw_track(raw_last, points, labels)
        out_path = os.path.join(OUT_DIR, "track_" + os.path.basename(paths[-1]))
        imwrite_unicode(out_path, raw_last)
        print(f" 轨迹图（逐帧连线，绿=起点 红=终点 黄=序号）："
              f"{os.path.relpath(out_path, REPO_ROOT)}")
        if not a.no_show:
            try:
                os.startfile(out_path)   # Windows 默认看图器，双击 bat 时轨迹自动弹出
            except Exception:
                pass                     # 打不开（非 Windows/无关联）不影响结论

    print()
    if verdict == "OK":
        print(" 结论：【OK】 整组连拍轨迹连续、无跳变 —— 锁的是同一个角色。")
        print("       轨迹图已自动弹出（白点=逐帧定位点）。若画面没弹出来，")
        print("       打开 debug\\analysis\\ 下 track_ 开头的那张 png。")
    elif verdict == "WARN":
        print(" 结论：【注意】 有跳变帧 —— 打开 debug/analysis/ 下同名的 track_nt_*.png，")
        print("       看红叉是不是落在了别的玩家/图案上（而不是你的角色）。")
        print("       【提醒】若红叉始终落在你的名牌上，跳变就是真实移动（跳跃/绳梯），")
        print("       不是锁错人 —— 此时定位可以放心用。")
    return 0 if verdict != "FAIL" else 1


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    try:
        input("\n[结束] 按回车关闭窗口 ")
    except Exception:
        pass
    sys.exit(code)
