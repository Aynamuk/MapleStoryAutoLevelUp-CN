# -*- coding: utf-8 -*-
"""路线体检：一张图的路线到底能不能用（2026-09-12）。

为什么单独一个模块：这段逻辑**界面和命令行都要用** ——
  · 录制结束的那一刻弹窗报告（ui.py）
  · 挂机前的体检 / 出问题时自查（以后接）
塞在 ui.py 里命令行就用不了，所以抽出来。

体检项：
  1. 每条主线的像素数够不够（一个 goal 圆点 = 13 像素，≤13 = 这段一个移动像素都没画上）
  2. 有没有 goal 标记（没有 = 走到头也不会切段）
  3. 多段时闭环接不接得上（>40px 走完最后一段会卡住）
  4. 回正线是否有效

判废用的就是引擎加载时的那套 validate_route_image —— **体检和运行时必须是同一份
判定**，否则会出现"体检说没问题、跑起来还是废的"。

⚠️ v0.9（2026-09-13）修正：上面这条"同一份判定"只对**主线**成立。
   回正线从 v0.9 起改用 utils/home_route.py 的**逐条组件判废**
   （8-连通域 → 现算几何 + 覆盖跨度 → validate_home_line）。
   本模块原来仍拿主线的三条老判据（≥20 像素 / bbox 跨度 ≥10px）判回正线，
   于是「一条 8px 的合法短回正线」会出现：**引擎正常加载、界面体检却报它废**
   —— 口径不一致。现在回正线走 home_route 的同一条链路（见 _audit_home_line）。

⚠️ v0.9 **第二版**（2026-09-13 方案重构）：**落点锚（127,0,127）整个删掉**。
   回正线的判废改成五条新判据（无 goal / 无动作像素 / span<5 / 起始段覆盖跨度<5 /
   终点必须贴主线），撞色自检也从"数锚点像素"改成"数回正线像素总数"。
   本模块同步：`find_anchor_pixels` → `collect_pixels`、`calc_line_stats` → `calc_line_geom`
   + `far_segment_stats`、`suggest_alt_anchor_color` → 已取消（改用 home_pixel_clash）。
"""
import os

import numpy as np
import cv2

from src.utils.paths import resource_path
from src.utils.common import load_image, mask_route_colors, imwrite_unicode
from src.utils.home_route import (
    calc_line_geom, cover_stats, goal_codes, home_pixel_clash,
    split_components, validate_home_line, wipe_legacy_anchor,
)


def _bbox_center(img, color_code):
    '''路线像素的包围盒中心（用来比较两条线录在哪）。没有像素返回 None。'''
    pts = [(x, y) for y in range(img.shape[0]) for x in range(img.shape[1])
           if color_code.get(tuple(int(v) for v in img[y, x][:3]))]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return ((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2)


def _count_direction_conflicts(img, color_code, radius=8):
    '''同一位置附近是不是既有「向左」又有「向右」的路线像素。

    路线是**单向**编码的：去程画的是 right，回程画的是 left。
    两者叠在同一段（route{N}.png）时，引擎"找最近像素"会在同一位置挑中
    方向相反的指令 —— 表现就是角色朝一个方向一直冲出去。

    Returns:
        (int, int, int): (冲突的位置数, 向左像素数, 向右像素数)
    '''
    left_pts, right_pts = [], []
    for y in range(img.shape[0]):
        row = img[y]
        for x in range(img.shape[1]):
            cmd = color_code.get(tuple(int(v) for v in row[x][:3]))
            if not cmd:
                continue
            parts = cmd.split()
            if parts and parts[0] == "left":
                left_pts.append((x, y))
            elif parts and parts[0] == "right":
                right_pts.append((x, y))
    if not left_pts or not right_pts:
        return 0, len(left_pts), len(right_pts)
    right_set = set(right_pts)
    conflicts = 0
    for lx, ly in left_pts:
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if abs(dx) + abs(dy) > radius:
                    continue
                if (lx + dx, ly + dy) in right_set:
                    conflicts += 1
                    break
            else:
                continue
            break
    return conflicts, len(left_pts), len(right_pts)


def _make_bot(cfg):
    """造一个只带色码表的最小引擎对象，用来复用它的判定方法。"""
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.color_code = {tuple(int(t) for t in k.split(',')): v
                    for k, v in cfg["route"]["color_code"].items()}
    b.color_code_up_down = {tuple(int(t) for t in k.split(',')): v
                            for k, v in cfg["route"]["color_code_up_down"].items()}
    b.cfg = cfg
    b.img_routes = []
    b.img_route_home = None
    b.idx_routes = 0
    b.home_lines = []          # v0.9 第二版：回正线以"线"为单位（不再有锚点）
    return b


def _audit_home_line(b, home_rgb, home_raw_rgb, img_map_bgr):
    """回正线体检（v0.9 第二版口径）：**8-连通域 → 逐条现算几何 → 逐条判废**。

    为什么不能继续用 validate_route_image（三条老判据）：
        它要求「≥20 个像素 且 bbox 跨度 ≥10px」。而合法短回正线**只有 8px**
        （落点离主线 5~15px 是本项目的常态）—— 三条判据会把它们全部误杀。
        表现就是：引擎正常加载这条线，界面体检却报"废"，两者对不上。

    Args:
        b: 最小引擎对象（拿色码表 + 已加载的主线图）
        home_rgb: **已 mask** 的回正线 RGB 图（就是引擎实际看到的那份）
        home_raw_rgb: mask 前的原始图（用来数"被地图底色吃掉"的回正线像素）
        img_map_bgr: 地图底图（**仅日志用**；撞色建议已随锚点一起取消）

    Returns:
        (bool, list[str]): (这张图有没有可用的回正线, 人能看的报告行)
    """
    cc = b.color_code
    cc_ud = b.color_code_up_down
    route_codes = set(cc) | set(cc_ud)
    out = []

    # ── 撞色自检（v0.9 第二版口径）：mask 前后各数一次**回正线像素总数** ──────
    #    ⚠️ 为什么不再只数锚点：锚点没了，被地图底色吃掉的可能是横段/竖段上
    #       任何一个像素，只数锚点等于没自检。少了多少 = 有多少像素凭空消失，
    #       而"消失"在运行时是**静默**的（只是这条线再也接不住人）。
    eaten = home_pixel_clash(home_raw_rgb, home_rgb, route_codes)
    if eaten:
        out.append(
            f"  ✗ 回正线：有 {len(eaten)} 个**回正线像素被地图底色吃掉了**"
            f"（{eaten[:8]}{' …' if len(eaten) > 8 else ''}）\n"
            f"      原因：这张图的 map.png 里恰好有回正线用到的那个颜色，"
            f"加载时会把该位置涂黑。\n"
            f"      后果：这些像素**永远走不到 / 接不住落点**（不报错，只是回不来）。\n"
            f"      修法：换一个底图里没出现过的颜色重画这几条（横段换色即可）。")

    comps = split_components(home_rgb, route_codes)
    if not comps:
        out.append("  ✗ 回正线 route_home.png：上面一个路线像素都没有（等于空图）")
        return False, out

    g_set = goal_codes(cc)
    n_ok = 0
    for i, comp in enumerate(comps, 1):
        geom = calc_line_geom(comp, b.img_routes, route_codes, b.cfg,
                              img_rgb=home_rgb)
        far = cover_stats(comp, b.img_routes, route_codes, b.cfg)
        ok, why = validate_home_line(comp, home_rgb, b.img_routes, route_codes,
                                     b.cfg)
        start = geom["start"]
        where = (f"起点 {start}" if start else
                 f"这一坨像素（{len(comp)} 个，左上角 "
                 f"{min(comp, key=lambda q: (q[1], q[0]))}）")
        cov = far.get("cover_xs")
        cov_txt = f"落点覆盖 x{cov[0]}~{cov[1]}（{far['cover_span']}px）" if cov else "落点覆盖 未知"
        if ok:
            n_ok += 1
            out.append(
                f"  ✓ 回正线 第 {i} 条：{where} → 终点 {geom['goal']}，"
                f"跨度 {geom['span']}px，{geom['n_pts']} 个像素，"
                f"{'含跳跃' if geom['has_jump'] else '无跳跃'}，{cov_txt}")
        else:
            out.append(
                f"  ✗ 回正线 第 {i} 条（{where}）判为废线，引擎会**只禁用这一条**：{why}")
    return n_ok > 0, out


def audit_map(map_name, cfg):
    """体检一张图的路线。

    Args:
        map_name: 地图目录名（minimaps/<map_name>/）
        cfg: 合并后的完整配置

    Returns:
        dict: {
          "ok": bool,          # 能否正常跑
          "lines": [str],      # 人能看的报告行
          "n_main": int,       # 有效主线段数
          "home_ok": bool,     # 回正线是否有效
          "loop_dist": int or None,
        }
    """
    lines = []
    base = resource_path(f"minimaps/{map_name}")
    if not os.path.isdir(base):
        return {"ok": False, "lines": [f"minimaps/{map_name}/ 不存在"],
                "n_main": 0, "home_ok": False, "loop_dist": None}

    map_png = os.path.join(base, "map.png")
    if not os.path.exists(map_png):
        return {"ok": False, "lines": [f"minimaps/{map_name}/map.png 不存在（地图没存）"],
                "n_main": 0, "home_ok": False, "loop_dist": None}

    b = _make_bot(cfg)
    b.img_map = load_image(map_png)

    files = sorted(f for f in os.listdir(base)
                   if f.startswith("route") and f.endswith(".png"))
    if not files:
        return {"ok": False, "lines": ["一条路线都没录（route*.png 不存在）"],
                "n_main": 0, "home_ok": False, "loop_dist": None}

    _goal_codes = None      # 延迟构造：需要 b.color_code，而 b 刚建好
    home_img = None      # 不管判没判废都留着，下面要比位置（**未 mask** 的原始图）
    home_masked = None   # mask 三连后的回正线图 —— v0.9 逐条判废用它
    for f in files:
        is_home = (f == "route_home.png")
        img = cv2.cvtColor(load_image(os.path.join(base, f)), cv2.COLOR_BGR2RGB)
        if is_home:
            home_img = img.copy()      # mask 是就地改的，先留一份原始快照
        img = mask_route_colors(b.img_map, img, cfg["route"]["color_code"])
        img = mask_route_colors(b.img_map, img, cfg["route"]["color_code_up_down"])
        # ⚠️ v0.9 第二版：**没有第三次 mask** 了 —— 落点锚那第三张色码表已删除。
        #    旧 PNG 上残留的 127,0,127 用 wipe_legacy_anchor 就地涂黑（只对回正线做）。
        if is_home:
            wiped = wipe_legacy_anchor(img)
            if wiped:
                lines.append(
                    f"  ⚠ 回正线 route_home.png：发现 {len(wiped)} 个**旧版落点标记**"
                    f"（127,0,127，{wiped[:8]}{' …' if len(wiped) > 8 else ''}）"
                    f"，已自动忽略（第二版没有锚点了，不影响使用）。")
        if is_home:
            # ⚠️ 回正线**不再**复用主线的三条判废（20 像素 / 跨度 10px）——
            #    合法短回正线只有 8px，那三条会把它全部误杀（v0.9 坑2）。
            #    留到主线都加载完再判（判废要量"离主线多远"，需要 b.img_routes）。
            home_masked = img
            continue
        ok, why = b.validate_route_image(img, f)
        pts = b._route_pixels(img)
        n_px = len(pts)
        # 终点标记（goal 圆点）本身就占 13 个像素，要扣掉才知道**有没有画出移动线**
        if _goal_codes is None:
            _goal_codes = {c for c, v in b.color_code.items()
                           if v.strip().split()[-1] == "goal"}
        n_goal = sum(1 for x, y in pts if tuple(img[y, x][:3]) in _goal_codes)
        n_move = n_px - n_goal
        if ok:
            b.img_routes.append(img)
            lines.append(f"  ✓ 主线 {f}：{n_px} 个像素（移动 {n_move}），正常")
        else:
            if n_move < 10:
                hint = (f"**几乎没画出移动线**（扣掉终点标记只剩 {n_move} 个像素）"
                        f" —— 录制时角色多半没动（游戏窗口没焦点，按键没进游戏）")
            else:
                hint = why
            lines.append(f"  ✗ 主线 {f}：{n_px} 个像素（移动 {n_move}），判废 —— {hint}")

    # ── 去程/回程有没有叠在同一段（2026-09-12 加）──────────────────────────
    # 这是「启动后直奔右边一直走」的直接原因：同一位置既有 right 又有 left 的
    # 路线像素，引擎靠"找最近像素"导航，**分不清该走哪边**。
    # 用户的直觉是「A→B 去程、B→A 回程应该算两段」，但旧版【中转点】只画白点
    # 不分段，于是整圈存成了一段。这里直接把它量出来。
    for f in files:
        if f == "route_home.png":
            continue
        p = os.path.join(base, f)
        if not os.path.exists(p):
            continue
        img = cv2.cvtColor(load_image(p), cv2.COLOR_BGR2RGB)
        n_conf, n_l, n_r = _count_direction_conflicts(img, b.color_code)
        if n_conf:
            lines.append(
                f"  ✗ {f}：**去程和回程叠在了一段** —— {n_conf} 个位置同时存在"
                f"向左和向右的路线像素（左 {n_l} / 右 {n_r}）\n"
                f"      引擎在同一位置分不清该走哪边 → 会一路朝一个方向冲出去。\n"
                f"      修法：重录，走到折返点(B)点【在这折返，存下这段】把去程回程分成两段。")

    # ── 回正线：走 v0.9 的逐条组件判废（**不复用**主线的三条老判据）──────────
    #    放这里是因为判废要量「落点离主线多远」，必须等 b.img_routes 全部加载完。
    home_ok = None
    if home_masked is not None:
        home_ok, home_lines = _audit_home_line(
            b, home_masked, home_img, b.img_map)
        lines.extend(home_lines)
        b.img_route_home = home_masked if home_ok else None
        if not home_ok:
            lines.append(
                "      回正线一条都用不了 → 本次**不启用回正**：掉出去只站桩打怪，"
                "不会尝试走回主线。\n"
                "      补一条：主界面选中这张图 →【🖊 手绘回正线（推荐）】"
                "（不用开游戏、不用真的掉下去）。")

    # ── 主线和回正线是不是录在同一个地方（2026-09-12 加）──────────────────
    # 回正线的起点应该是"掉下去的落点"，主线应该是"平台上的巡逻路径" ——
    # 两者**不该**重叠。若重叠，说明录制全程角色就没离开过那个点，
    # 典型情况：角色掉进坑里上不来，于是在坑底把两条线都录了。
    # 实测案例：角色在坑底 (98,122)，主线只画出 8 个 right 像素、跨度 12px，
    # 启动后角色跟着这 8 个 right 一路冲出去（用户报"直奔右边一直走"）。
    #
    # ⚠️ v0.9：只在回正线**用不了**的时候才查这一条。
    #    合法短回正线的落点离主线只有 5~15px，包围盒中心必然挨着主线 ——
    #    这条"中心相距 ≤20px 就是录在坑底"的老启发式会把它们全部误报。
    if home_img is not None and b.img_routes and not home_ok:
        c_home = _bbox_center(home_img, b.color_code)
        c_main = _bbox_center(b.img_routes[0], b.color_code)
        if c_home and c_main:
            d = abs(c_home[0] - c_main[0]) + abs(c_home[1] - c_main[1])
            if d <= 20:
                lines.append(
                    f"  ✗ 主线和回正线**录在同一个地方**（中心相距只有 {d}px）\n"
                    f"      回正线起点该是「掉下去的落点」，主线该是「平台上的路径」，"
                    f"两者不该重叠。\n"
                    f"      多半是角色掉进坑里没上来 —— 先手动把角色弄回平台，"
                    f"再看面板上的「当前 (x,y)」确认位置对了，重新开始录。")

    n_main = len(b.img_routes)
    loop_dist = None
    if n_main == 0:
        lines.append("")
        lines.append("  ⚠ 没有一条有效主线 → 挂机时角色**不会移动**")
    elif n_main == 1:
        lines.append("")
        lines.append("  ⚠ 只有 1 段主线：走完会循环回自己，可能停在终点。"
                     "想来回走就要录「去 + 回」两段")
    else:
        loop_dist = b.check_route_loop_closure()
        if loop_dist is None:
            pass
        elif loop_dist <= 10:
            lines.append(f"  ✓ 闭环 {loop_dist}px，接得上")
        elif loop_dist <= 40:
            lines.append(f"  ⚠ 闭环 {loop_dist}px，只能靠失联救援接上（建议 ≤10px）")
        else:
            lines.append(f"  ✗ 闭环 {loop_dist}px —— 闭不上环，走完最后一段会卡住")

    ok = n_main > 0
    return {"ok": ok, "lines": lines, "n_main": n_main,
            "home_ok": b.img_route_home is not None, "loop_dist": loop_dist}
