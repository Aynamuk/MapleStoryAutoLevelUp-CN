# -*- coding: utf-8 -*-
"""
跳爬像素体检 —— 判定「边走边跳为什么录不全」

## 这个工具回答什么问题

用户报告「边走边跳录不进去」，怀疑是录制器的 `blob_cooldown: 0.7` 把第 2 跳吃掉了。
本工具**不下结论、只摆证据**，把 route*.png 上的跳跃像素全找出来，
按「簇」（相互距离 ≤ CLUSTER_GAP 的算一簇）分组，打印每颗的坐标与相互间距。

判定口径（脚本会自己算给你看，不用你换算）：

  · 一次真实的「跳」在路线里应该是 **1 颗**（或 1 小簇）像素；
  · 如果同一段路上，簇与簇之间的间距 **明显小于 `blob_cooldown` × 角色速度**，
    说明「本该记下来的第二跳」缺失 —— 想验证冷却效应就录一段
    **故意连跳**（每次间隔约 0.3s）的线，再看簇的数量；
  · 如果一段连跳录出来是 1 簇而你要的是 3 跳 → 冷却吃了；
  · 如果是 3 簇 → 冷却没吃，问题在别处（引擎执行端 `jump_cooldown`）。

## 用法（三种，按需选）

  # 1. 报告一张已有的路线图
  python -m tools.jump_pixel_report minimaps/地图名/route1.png

  # 2. 报告整张地图的全部 route*.png + route_home.png
  python -m tools.jump_pixel_report --map 地图名

  # 3. 列出所有有路线图的地图（不知道图名叫什么时先用这个）
  python -m tools.jump_pixel_report --list
"""
from __future__ import annotations

import argparse
import os
import sys

import cv2
import numpy as np

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                            # noqa: BLE001
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.logger import logger  # noqa: E402

# ── 跳跃色（config 的 route.color_code 里 action 含 jump 的那几条）────────────
# 与 config/config_default.yaml 的 color_code 保持一致；改了配置这里要跟着改，
# 所以下面 _load_jump_colors() 优先从真配置读，读不到才用这份兜底。
FALLBACK_JUMP_COLORS = {
    (255, 127, 0):   "left none jump",
    (0, 255, 255):   "right none jump",
    (127, 255, 0):   "none down jump",
    (255, 0, 255):   "none none jump",
}

# 同一簇的判定间距（像素）：两颗跳跃像素离得比这近，就算同一次跳
CLUSTER_GAP = 5


def _load_jump_colors():
    """从项目配置读「带 jump 的色码」→ {RGB: action}。

    为什么读配置而不是写死：色码表是 config 的一部分，用户/上游都可能调整。
    写死会在配置一改时**静默漏报**（少认一种跳跃色 = 报告看着没问题）。

    读取口径与录制器**完全一致**（tools/routeRecorder.py:466-488）：
      default.yaml 打底 → 叠 active_config_path() 选中的用户层。
    两条路不一致时，会出现「录制器按新色码画、本工具按旧色码找」的静默错位。
    """
    try:
        from src.utils.common import (active_config_path, load_yaml,
                                      override_cfg)
        cfg = load_yaml("config/config_default.yaml")
        custom = active_config_path()
        if custom:
            cfg = override_cfg(cfg, load_yaml(custom))
        out = {}
        for k, v in (cfg.get("route", {}).get("color_code") or {}).items():
            if "jump" in str(v):
                out[tuple(int(x) for x in str(k).split(","))] = str(v)
        if out:
            logger.info(f"[跳爬体检] 色码表来源：{custom or '纯 config_default.yaml'}")
            return out
        logger.warning("[跳爬体检] 配置里没有带 jump 的色码，改用内置兜底表")
        return dict(FALLBACK_JUMP_COLORS)
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"[跳爬体检] 读配置失败（{e}），改用内置兜底色码表")
        return dict(FALLBACK_JUMP_COLORS)


def _find_pixels(img, colors):
    """在路线图里找出所有跳跃色像素 → {rgb: [(x, y), ...]}

    ⚠️★ 通道序：**直接拿 RGB 三元组跟数组比，不要转 BGR**（2026-09-27 实测确认）。
       踩过的坑：本工具第一版按"cv2 读回来是 BGR"的常识把 RGB 键转成了 BGR，
       结果在两张真实路线图上**全部漏报**（明明有 13 个青色像素，报"一个都没有"）。
       实测口径（`minimaps/废都南方工地/route1.png`）：
         · 磁盘文件里的字节序是 **RGB**（录制器画的时候是 BGR，
           但 imwrite_unicode 走 cv2.imencode，PNG 按 RGB 存）；
         · imread_unicode 走 cv2.imdecode(IMREAD_COLOR) 读回来，**字节序保持不变**
           ⇒ 数组里 RGB(0,255,255) 就还是 (0,255,255)，按 BGR(255,255,0) 找会命中 0 个。
       验证方法：`np.all(img == [0,255,255], axis=2).sum()` → 13（真值）。
       ⇒ 结论：**按 RGB 直接比**。别"好心"再转一次通道。
    """
    found = {}
    for rgb in colors:
        probe = np.array(rgb, dtype=np.uint8)
        mask = np.all(img == probe, axis=2)
        ys, xs = np.where(mask)
        if len(xs):
            found[rgb] = sorted(zip(xs.tolist(), ys.tolist()))
    return found


def _cluster(points, gap=CLUSTER_GAP):
    """把点按「相互距离 ≤ gap」聚成簇（单链聚类，够用且不引依赖）。

    Returns:
        list[list[(x,y)]]，每个内层是一簇
    """
    if not points:
        return []
    pts = list(points)
    used = [False] * len(pts)
    clusters = []
    for i in range(len(pts)):
        if used[i]:
            continue
        stack, comp = [i], []
        used[i] = True
        while stack:
            k = stack.pop()
            comp.append(pts[k])
            for j in range(len(pts)):
                if used[j]:
                    continue
                dx = pts[k][0] - pts[j][0]
                dy = pts[k][1] - pts[j][1]
                if dx * dx + dy * dy <= gap * gap:
                    used[j] = True
                    stack.append(j)
        clusters.append(comp)
    return clusters


def _all_route_colors(img):
    """数出图里**每一种路线色**各有多少像素 → {action: (RGB, count)}。

    为什么「没找到跳跃像素」时必须报这个：
      只报"一个都没有"是无法定性的 —— 到底是**真没跳**，还是**本工具认错了颜色**？
      把图里实际出现的色码列出来，一眼就能分辨：
        · 有 up/down 灰黄、有左右红蓝  ⇒ 路线录进去了，只是**确实没记跳跃**（真没跳）
        · 连左右红蓝都没有            ⇒ 存的不是路线图 / 白板是空的（另一种病）
    """
    from src.utils.common import load_yaml, override_cfg, active_config_path
    table = {}
    try:
        cfg = load_yaml("config/config_default.yaml")
        custom = active_config_path()
        if custom:
            cfg = override_cfg(cfg, load_yaml(custom))
        table = dict(cfg.get("route", {}).get("color_code") or {})
        table.update(cfg.get("route", {}).get("color_code_up_down") or {})
    except Exception:                                        # noqa: BLE001
        return {}
    out = {}
    for k, action in table.items():
        rgb = tuple(int(x) for x in str(k).split(","))
        n = int(np.all(img == np.array(rgb, dtype=np.uint8), axis=2).sum())
        if n:
            out[str(action)] = (rgb, n)
    return out


def report_image(path, colors):
    """报告单张图的跳跃像素分布 → (簇数, 可打印的多行文本)"""
    # ⚠️★ 必须用 imread_unicode，不能用 cv2.imread —— 本仓库路径含中文
    #    （`F:\AI项目\冒险岛自动练级-公开版`），cv2.imread 走的是 ACP 编码，
    #    中文路径直接返回 None，表现是「读不到文件」，而文件明明在。
    #    本项目为此专门写了这个包装（src/utils/common.py:181）。
    from src.utils.common import imread_unicode
    img = imread_unicode(path)
    if img is None:
        return 0, [f"  【读不到】{path}（文件不存在 / 不是图片）"]

    found = _find_pixels(img, colors)
    lines = [f"  图：{path}　尺寸 {img.shape[1]}x{img.shape[0]}（宽x高）"]

    if not found:
        lines.append("    ⚠️ 一个跳跃像素都没有 —— 这段路上完全没有'跳'的记录。")
        lines.append("       （爬梯子/跨缺口的地形就是靠这个像素驱动的）")
        others = _all_route_colors(img)
        if others:
            lines.append("       该图实际存在的路线色（用来排除'本工具认错色'）：")
            for act, (rgb, n) in sorted(others.items(), key=lambda kv: -kv[1][1]):
                lines.append(f"         {act:20s} RGB{rgb}  x{n}")
        else:
            lines.append("       ⚠️ 连左右移动色都没有 —— 这多半不是一张路线图"
                         "（或白板是空的，没点【开始录】就存了）。")
        return 0, lines

    total = 0
    total_clusters = 0
    for rgb, pts in sorted(found.items(), key=lambda kv: -len(kv[1])):
        clusters = _cluster(pts)
        total += len(pts)
        total_clusters += len(clusters)
        lines.append(f"    色码 RGB{rgb}（{colors[rgb]}）：{len(pts)} 颗，"
                     f"聚成 {len(clusters)} 簇")
        for ci, c in enumerate(sorted(clusters, key=lambda c: (c[0][1], c[0][0])), 1):
            xs = [p[0] for p in c]
            ys = [p[1] for p in c]
            cx, cy = sum(xs) // len(xs), sum(ys) // len(ys)
            lines.append(f"      簇{ci}: {len(c)} 颗，中心约 ({cx},{cy})")
        # 簇中心之间的最小间距 —— 这是判断「冷却有没有吃跳」的关键数字
        centers = []
        for c in clusters:
            xs = [p[0] for p in c]
            ys = [p[1] for p in c]
            centers.append((sum(xs) // len(xs), sum(ys) // len(ys)))
        if len(centers) >= 2:
            dmin = min(
                ((centers[i][0] - centers[j][0]) ** 2
                 + (centers[i][1] - centers[j][1]) ** 2) ** 0.5
                for i in range(len(centers)) for j in range(i + 1, len(centers))
            )
            lines.append(f"      → 相邻簇最近间距：{dmin:.1f}px")
    lines.append(f"    合计：{total} 颗跳跃像素，{total_clusters} 簇")
    return total_clusters, lines


def list_maps():
    """列出所有含路线图的地图 → [(地图名, [route 文件路径, ...]), ...]"""
    root = os.path.join(REPO_ROOT, "minimaps")
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        pngs = sorted(
            os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith(".png")
            and f.lower().startswith("route")
            and f.lower() != "map.png"
        )
        if pngs:
            out.append((name, pngs))
    return out


def main():
    ap = argparse.ArgumentParser(description="跳爬像素体检：看路线图里记了几颗'跳'")
    ap.add_argument("image", nargs="?", help="单张 route*.png 的路径")
    ap.add_argument("--map", dest="map_name", help="报告该地图目录下的全部路线图")
    ap.add_argument("--list", action="store_true", help="列出所有有路线图的地图")
    args = ap.parse_args()

    colors = _load_jump_colors()
    print()
    print("=" * 62)
    print("  跳爬像素体检（只读，不改任何文件）")
    print("=" * 62)
    print(f"  识别的跳跃色码：")
    for rgb, act in sorted(colors.items()):
        print(f"    RGB{rgb}  →  {act}")
    print()

    if args.list or (not args.image and not args.map_name):
        maps = list_maps()
        if not maps:
            print("  没有找到任何路线图（minimaps/<图名>/route*.png）")
            print()
            return 0
        print("  这些地图有路线图：")
        for name, pngs in maps:
            print(f"    {name}　（{len(pngs)} 张）")
        print()
        print("  用法：python -m tools.jump_pixel_report --map 地图名")
        print()
        return 0

    targets = []
    if args.image:
        targets = [args.image if os.path.isabs(args.image)
                   else os.path.join(REPO_ROOT, args.image)]
    if args.map_name:
        d = os.path.join(REPO_ROOT, "minimaps", args.map_name)
        if not os.path.isdir(d):
            print(f"  【错误】没有这个地图目录：{d}")
            print()
            return 1
        targets += sorted(
            os.path.join(d, f) for f in os.listdir(d)
            if f.lower().endswith(".png") and f.lower().startswith("route")
        )
    if not targets:
        print("  【没找到路线图】没有可报告的文件。")
        print("  用 --list 看看哪些地图有路线图。")
        print()
        return 0

    grand = 0
    for t in targets:
        n, lines = report_image(t, colors)
        grand += n
        for ln in lines:
            print(ln)
        print()

    print("-" * 62)
    print(f"  合计簇数：{grand}")
    print()
    print("  怎么读这个结果：")
    print("    · 一段「故意连跳 3 次（每次间隔约 0.3s）」的路，如果这里只有 1 簇")
    print("      → 第 2、3 跳被 blob_cooldown 吃掉了（冷却确实在挡）")
    print("    · 如果有 3 簇")
    print("      → 冷却没吃，'边走边跳录不进'的原因在别处（看引擎端 jump_cooldown）")
    print("    · 相邻簇最近间距 < 一次跳跃的飞行距离时，引擎侧容易连跳")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
