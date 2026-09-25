# -*- coding: utf-8 -*-
"""
名字标签定位 —— 离线验证器

用途
----
改完「名字模板 / nametag.offset / 检测模式」之后，**不开游戏**就能判断够不够用。

做法（关键）
-----------
不重写匹配逻辑。用 `object.__new__` 绕过引擎构造函数（因此不需要游戏窗口、不需要
输入驱动、不需要模板以外的任何资源），把帧和配置灌进去，然后**直接调用引擎自己的
`get_player_location_by_nametag()`**；顺手把 `find_pattern_sqdiff` 包一层，记录它
返回的匹配分数。
→ 测的就是线上跑的那段代码，而不是"看起来差不多"的复刻。

判据
----
1. 每一帧的 score 都应 < `nametag.diff_thres`（默认 0.2），且越低越好；
2. `loc_nametag` 应逐帧跟着角色跑，不能原地不动；
3. 只要有一帧 score 逼近阈值，就说明模板/模式不稳 —— 早晚会丢定位，
   而丢定位的直接后果是引擎以为你还在原地（假性 stuck）。

用法
----
  python -m tools.verify_nametag                      # 跑 debug 里全部 capture_raw_*.png
  python -m tools.verify_nametag --frames 5           # 只跑最近 5 张
  python -m tools.verify_nametag --mode white_mask    # 换个检测模式对比（不改配置）
  python -m tools.verify_nametag --image debug/xxx.png
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

import src.engine.MapleStoryAutoLevelUp as eng          # noqa: E402
from src.utils.common import (load_yaml, override_cfg,   # noqa: E402
                              load_image, crop_frame_to_client,
                              imread_unicode, imwrite_unicode,
                              active_config_path)

OUT_DIR = os.path.join(REPO_ROOT, "debug", "analysis")


def load_cfg():
    # 当前活动方案跟随主界面「配置方案」（2026-09-12 改造后 config_custom.yaml
    # 已被命名方案如 某个方案.yaml 取代，读固定旧路径会 FileNotFoundError）
    custom = active_config_path()
    cfg = override_cfg(load_yaml(os.path.join(REPO_ROOT, "config", "config_default.yaml")),
                       load_yaml(custom) if custom else {})
    try:
        cfg = override_cfg(cfg, load_yaml(os.path.join(REPO_ROOT, "config", "config_data.yaml")))
    except FileNotFoundError:
        pass
    return cfg


def build_probe(cfg):
    """造一个只带 nametag 定位所需属性的引擎对象（绕过 __init__，不碰窗口/输入）。"""
    bot = object.__new__(eng.MapleStoryAutoBot)
    bot.cfg = cfg
    name = cfg["nametag"]["name"]
    bot.img_nametag = load_image(f"nametag/{name}.png")
    bot.img_nametag_gray = load_image(f"nametag/{name}.png", cv2.IMREAD_GRAYSCALE)
    bot.loc_nametag = (0, 0)
    bot.is_first_frame = True
    # 引擎在 get_player_location_by_nametag 里会累加这两个计数（定位健康度告警用）。
    # 本工具绕过 __init__，所以必须自己带上，否则引擎那行 `self.nametag_miss_streak += 1`
    # 会 AttributeError（2026-09-10 加计数时踩到）。
    bot.nametag_miss_streak = 0
    bot.nametag_hit = False
    bot.nametag_ever_hit = False
    return bot


def run_one(bot, cfg, raw):
    """
    在一帧上跑引擎真方法。
    返回 (loc_nt, loc_player, score, 标注图, 错误信息)。
    帧尺寸与配置不符时，错误信息非空、其余全为 None —— 不让工具崩在半路。
    """
    client, msg = crop_frame_to_client(raw, tuple(cfg["game_window"]["size"]),
                                       cfg["game_window"]["title_bar_height"])
    if client is None or client.size == 0:
        # 尺寸不符的帧（换了分辨率 / 不是一整帧）直接跳过，别把 None 喂给下游
        return None, None, None, None, msg
    bot.img_frame_gray = cv2.cvtColor(client, cv2.COLOR_BGR2GRAY)
    bot.img_frame_debug = client.copy()
    bot.is_first_frame = True          # 每帧独立评估，不用上一帧的缓存
    bot.loc_nametag = (0, 0)

    state = {"best": None}
    orig = eng.find_pattern_sqdiff

    def wrapper(img, img_pattern, **kw):
        loc, score, cached = orig(img, img_pattern, **kw)
        if state["best"] is None or score < state["best"][0]:
            state["best"] = (score, loc, cached)
        return loc, score, cached

    eng.find_pattern_sqdiff = wrapper
    try:
        loc_player = bot.get_player_location_by_nametag()
    finally:
        eng.find_pattern_sqdiff = orig

    dbg = bot.img_frame_debug
    # 把定位结果再画清楚一点（引擎自己已画了绿框）
    if loc_player:
        cv2.drawMarker(dbg, (int(loc_player[0]), int(loc_player[1])),
                       (0, 0, 255), cv2.MARKER_CROSS, 28, 2)
    return (bot.loc_nametag, loc_player,
            (state["best"][0] if state["best"] else None), dbg, None)


def main():
    ap = argparse.ArgumentParser(description="名字标签定位离线验证（调用引擎真方法）")
    ap.add_argument("--image", default=None, help="只跑这一张（默认跑 debug 里全部抓帧）")
    ap.add_argument("--frames", type=int, default=0, help="只跑最近 N 张（0=全部）")
    ap.add_argument("--mode", default=None,
                    choices=["grayscale", "white_mask", "histogram_eq"],
                    help="临时覆盖检测模式做对比（不改配置文件）")
    a = ap.parse_args()

    cfg = load_cfg()
    if a.mode:
        print(f"[信息] 临时把 nametag.mode 覆盖为 {a.mode}（不写回配置）")
        cfg["nametag"]["mode"] = a.mode

    thres = cfg["nametag"]["diff_thres"]
    off = cfg["nametag"]["offset"]
    print("=" * 68)
    print(" 名字标签定位离线验证")
    print("=" * 68)
    print(f" 模板        : nametag/{cfg['nametag']['name']}.png")
    print(f" 检测模式    : {cfg['nametag']['mode']}")
    print(f" 判定阈值    : score < {thres}      offset = {list(off)}")
    print(f" ui_y_start  : {cfg['ui_coords']['ui_y_start']}（相机区 = 帧的上这么多行）")

    if a.image:
        paths = [a.image if os.path.isabs(a.image)
                 else os.path.join(REPO_ROOT, a.image)]
    else:
        paths = sorted(glob.glob(os.path.join(REPO_ROOT, "debug", "capture_raw_*.png")),
                       key=os.path.getmtime)
        if a.frames:
            paths = paths[-a.frames:]
    if not paths:
        print("\n[错误] 没有可用的抓帧。先跑 python -m tools.grab_frame 抓一张。")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)
    bot = build_probe(cfg)

    print()
    print(f"{'帧':<34}{'score':>8}  {'loc_nametag':>16}  {'推导角色位置':>16}  判定")
    print("-" * 68)
    scores, fails, locs, n_eval = [], 0, [], 0
    hit_scores, miss_frames = [], []
    for p in paths:
        raw = imread_unicode(p, cv2.IMREAD_COLOR)   # 中文路径必须走这个，cv2.imread 会读不到
        if raw is None:
            print(f"{os.path.basename(p):<34}{'读不到':>8}")
            continue
        loc_nt, loc_player, score, dbg, err = run_one(bot, cfg, raw)
        if err is not None:
            # 尺寸不符的帧（例如换过分辨率/窗口尺寸时期留下的旧抓帧）不计入评估
            print(f"{os.path.basename(p):<34}{'尺寸不符':>8}  {err}")
            continue
        n_eval += 1
        # 命中判据必须**同时**看分数和位置：
        #   分数达标 ≠ 定位有效 —— 实测过模板在**没有角色**的亮背景帧上也能拿 0.05 的低分，
        #   而且匹配点会落到引擎的图像填充边（出现负坐标）。只比分数会把这类假阳性判成 OK。
        ch, cw = cfg["game_window"]["size"]
        y_start = cfg["ui_coords"]["ui_y_start"]
        scored_ok = (score is not None and score < thres)
        pos_ok = (loc_nt != (0, 0)
                  and 0 <= loc_nt[0] < cw and 0 <= loc_nt[1] < y_start)
        ok = scored_ok and pos_ok
        if not ok:
            fails += 1
        if score is not None:
            scores.append(score)
            if ok:
                hit_scores.append(score)
            else:
                miss_frames.append((os.path.basename(p), score))
        if ok:
            locs.append(loc_nt)
        s = f"{score:.4f}" if score is not None else "None"
        if ok:
            tag = "OK"
        elif scored_ok and not pos_ok:
            tag = "**位置可疑**（分数够但落点不合法）"
        else:
            tag = "**未命中**"
        print(f"{os.path.basename(p):<34}{s:>8}  {str(loc_nt):>16}  {str(loc_player):>16}  {tag}")
        imwrite_unicode(os.path.join(OUT_DIR, "verify_nt_" + os.path.basename(p)), dbg)
        if ok:
            latest_ok = os.path.join(OUT_DIR, "verify_nt_" + os.path.basename(p))

    print("-" * 68)
    if n_eval == 0:
        print(" 【失败】 一帧都没读进来，无法得出结论（路径/权限问题？）。")
        return 1
    print(f" 共 {len(paths)} 个文件，实际评估 {n_eval} 帧："
          f"命中 {len(hit_scores)}，未命中 {fails}")
    if scores:
        print(f" score：最小 {min(scores):.4f} / 中位 {np.median(scores):.4f} / 最大 {max(scores):.4f}"
              f"　（阈值 {thres}）")
    if len(locs) > 1:
        xs = [l[0] for l in locs]
        ys = [l[1] for l in locs]
        print(f" 命中帧里定位框的跨度：x {min(xs)}..{max(xs)}（{max(xs)-min(xs)}px）、"
              f"y {min(ys)}..{max(ys)}（{max(ys)-min(ys)}px）")
        if max(xs) - min(xs) == 0 and max(ys) - min(ys) == 0:
            print(" 【注意】 定位框完全不动 —— 很可能所有帧都命中了同一处静态图案，不是角色。")
    print()

    # ---- 结论：看「命中 vs 未命中」的分数**分离度**，而不是数有几个失败帧
    #   【注意】 不能因为"有帧没命中"就判不够用 —— 抓帧目录里本来就混着**不含角色**的画面
    #      （登录界面、别的窗口、抓错窗口的帧），它们未命中是**正确**行为。
    #      真正要看的只有一个：命中的最差分 与 未命中的最好分 之间有没有干净间隔。
    if not hit_scores:
        print(" 结论：【失败】 一帧都没命中 —— 模板基本不可用。")
        print("       排查：① 模板是不是把随背景变的像素留下了（绿底占比应 >10%）")
        print("             ② nametag.name / offset 是否对得上")
        print("             ③ 换 --mode white_mask 对比")
    elif not miss_frames:
        margin = (thres / max(hit_scores)) if max(hit_scores) > 0 else float("inf")
        if margin >= 2.0:
            print(f" 结论：【OK】 够用。全部帧命中；最差分 {max(hit_scores):.4f} 距阈值还有 "
                  f"{margin:.1f} 倍余量。")
        else:
            print(f" 结论：【注意】 全部命中，但最差分 {max(hit_scores):.4f} 离阈值 {thres} 只剩 "
                  f"{margin:.1f} 倍 —— 背景/角度一变就可能丢，建议重截模板。")
    else:
        worst_hit = max(hit_scores)
        cand = [s for _, s in miss_frames if s is not None]
        best_miss = min(cand) if cand else None
        if best_miss is None:
            print(" 结论：【存疑】 未命中的帧都没拿到分数，无法算分离度。")
        else:
            ratio = (best_miss / worst_hit) if worst_hit > 0 else float("inf")
            print(f" 分离度：命中的最差分 {worst_hit:.4f}　vs　未命中的最好分 {best_miss:.4f}"
                  f"　→ 相差 {ratio:.1f} 倍")
            print(" 未命中的帧（按常理应是不含你角色的画面；请自行核对）：")
            for nm, sc in miss_frames:
                print(f"   · {nm}  score={'None' if sc is None else '%.4f' % sc}")
            print()
            if worst_hit == 0 or ratio >= 3.0:
                print(f" 结论：【OK】 够用。命中与未命中的分数分离干净（{ratio:.1f} 倍），"
                      f"阈值 {thres} 卡在中间是安全的。")
                print("       前提：上面那些未命中的帧确实不含你的角色。")
            elif ratio >= 1.5:
                print(f" 结论：【注意】 勉强够用 —— 分离只有 {ratio:.1f} 倍，阈值卡得偏紧。")
                print("       建议把框再框紧一点重截，或按实测把 diff_thres 调到两者之间。")
            else:
                print(f" 结论：【失败】 不够用 —— 命中与未命中的分数**区间重叠**（只差 {ratio:.1f} 倍），"
                      f"阈值怎么设都会误判。")
                print("       排查：① 模板里是否混进了随背景变的像素（绿底占比应 >10%）")
                print("             ② 重截模板，只框名字那一行（别带称号）")
    print()
    print(f" 标注图已存到：{os.path.relpath(OUT_DIR, REPO_ROOT)}\\verify_nt_*.png")

    # 自动打开「能识别到角色的那张」标注图（2026-09-12 用户要求：只看最新命中的，
    # 不拼接全部）。命中过才打开；一帧都没命中则不打扰。
    latest_ok = locals().get('latest_ok', None)
    if latest_ok and os.path.exists(latest_ok):
        print(f" 验证结果图已自动打开: {os.path.relpath(latest_ok, REPO_ROOT)}"
              f"（绿点=定位到的角色）")
        try:
            os.startfile(latest_ok)
        except Exception:
            pass
    return 0



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
