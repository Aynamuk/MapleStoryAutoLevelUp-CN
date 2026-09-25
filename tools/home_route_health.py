# -*- coding: utf-8 -*-
'''回正线体检器 —— 不开游戏、不上机，直接判"这条回正线能不能把人带回来"。

用法：
    python -m tools.home_route_health                 # 用当前活动配置里的地图
    python -m tools.home_route_health 废弃南方工地     # 指定地图

## 为什么要它

2026-09-15 复盘"复杂地形能不能回来"时得出：**"回正线录得对"只是必要条件**，
还有 4 类风险是"录得再对也过不去"的。这个工具把那 4 类风险全部量化，
画完线先跑它，**不用上机就知道会不会卡**。

## 4 类风险 → 本工具的 4 项体检

| 风险 | 判据 | 出处 |
|---|---|---|
| **关 5 超时** | 路线长度 ÷ 保守速度(15px/s) 必须 < `route.home_timeout`(8s) | 超时 → 退出 + 30s 不再接人 |
| **关 3 提前踢出** | 回正线离**主线像素**的最近距离必须 ≥ `tol + 1`（tol 默认 2 → ≥3px） | `d_home < d_main - tol` 要保持回正 |
| **关 2 落点覆盖** | 横段（离主线最远那批）的 x 跨度要盖住平台 x 范围 | 引擎只在周围 `search_range`(10px) 找像素 |
| **关 4 跳跃 / 梯子** | 含 jump 段 → 落点偏 ±4~6px，目标平台要有余量；含 up/down → 依赖 `is_on_ladder` 识别 | 实测落点 = 起跳点 ± 4~6px |

另加一项加分体检：**主线站立行是否唯一**（多层地形时 `_main_stand_y` 取众数会算错）。

⚠️ 本工具**只读**，不改任何文件、不连游戏。
'''
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from src.utils.common import load_yaml
from src.utils.home_route import collect_pixels, home_params, goal_codes


# ── 实测基准（2026-09-15 废都南方工地，3 次真机）────────────────────────────
# 52px 的回正线，实测走了 1.2 / 2.2 / 3.3 秒 → 约 15.8 ~ 43.3 px/秒。
# 取**最慢**的 15 px/s 当保守下界（宁可低估速度、高估耗时）。
MEASURED_PX = 52
MEASURED_FAST = 1.2
MEASURED_SLOW = 3.3
SPEED_CONSERVATIVE = MEASURED_PX / MEASURED_SLOW      # ≈ 15.8 px/s
SPEED_OPTIMISTIC = MEASURED_PX / MEASURED_FAST        # ≈ 43.3 px/s

OK, WARN, BAD = "✅ 通过", "⚠️ 注意", "❌ 不通过"


def _merge(base, custom):
    for k, v in custom.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _load_bot(map_name=None):
    '''用**引擎自己的 load_config** 把真实资源读进来（保证和运行时同一份数据）。'''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as Bot
    cfg = _merge(load_yaml("config/config_default.yaml"),
                 load_yaml("config/config_custom.yaml")
                 if os.path.exists("config/config_custom.yaml") else {})
    # 活动方案优先（和界面/引擎一致）
    from src.utils.common import active_config_path
    ap = active_config_path()
    if ap and os.path.exists(ap):
        cfg = _merge(cfg, load_yaml(ap))
    if map_name:
        cfg.setdefault("bot", {})["map"] = map_name
    b = Bot.__new__(Bot)
    b.args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                   debug=False, disable_viz=True)
    b.cfg = None
    b.data = load_yaml("config/config_data.yaml")
    b.monsters_info = {}
    for _k, _v in (("img_routes", []), ("img_route_home", None), ("img_map", None),
                   ("img_route", None), ("color_code", {}), ("color_code_up_down", {}),
                   ("is_show_debug_window", False), ("img_route_debug", None),
                   ("idx_routes", 0), ("is_using_home_route", False),
                   ("is_terminated", False), ("kb", None), ("capture", None)):
        setattr(b, _k, _v)
    ret = b.load_config(cfg)
    # 运行期字段（__new__ 绕过了 __init__）：跑 `_home_line_pick` 需要这几个
    for _k, _v in (("home_reenter_lock_t", 0.0), ("t_last_home_lock_log", 0.0),
                   ("home_frame", 0), ("home_enter_t", 0.0), ("home_enter_pos", None),
                   ("home_d_home", None), ("home_d_main", None), ("_home_line_near", None),
                   ("_recenter_active", False), ("_recenter_n", 0),
                   ("_recenter_last_px", None), ("loc_player_global", (0, 0))):
        if not hasattr(b, _k):
            setattr(b, _k, _v)
    return b, ret


def _main_dist(main_pts, px, py):
    if main_pts.shape[0] == 0:
        return None
    return int((np.abs(main_pts[:, 0] - px) + np.abs(main_pts[:, 1] - py)).min())


def _walk_len(xs, ys, goal):
    '''**沿线的实际走行距离**（8-连通测地线）：从终点出发做 BFS，取最远点。

    ⚠️ 为什么不能用 `n_pts`（画了多少像素）当路线长度（2026-09-17 修）：
        "落点覆盖"式画法是**一条很宽的横带**（横着铺满整个坑底），
        但角色只会从**自己落点**走到梯子口，**不会**把横带从头走到尾。
        真机反例：废都南方工地 118 px 的线 —— 其中 51 px 是坑底横带，
        实际最坏走行只有 ~82 px。用 n_pts 算 → 7.5s（逼近 8s 上限，误报"注意"）；
        用测地线算 → 5.2s（正确判"通过"）。

    Returns:
        int: 最坏情况走行步数（8-连通）；点集为空 → 0
    '''
    from collections import deque
    pts = {(int(a), int(b)) for a, b in zip(xs, ys)}
    if not pts:
        return 0
    src = (int(goal[0]), int(goal[1])) if goal is not None else None
    if src is None or src not in pts:
        # 终点不在点集里（合成的测试线常有）→ 取离它最近的那个像素当起点
        if src is None:
            src = next(iter(pts))
        else:
            src = min(pts, key=lambda q: abs(q[0] - src[0]) + abs(q[1] - src[1]))
    dist = {src: 0}
    q = deque([src])
    while q:
        x, y = q.popleft()
        nd = dist[(x, y)] + 1
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nb = (x + dx, y + dy)
                if nb in pts and nb not in dist:
                    dist[nb] = nd
                    q.append(nb)
    return max(dist.values())


def check_line(b, line, tol, home_timeout, search_range):
    '''对**一条**回正线做 4+1 项体检。返回 (结果列表, 汇总)。'''
    xs = np.asarray(line.get("xs"), dtype=int).reshape(-1)
    ys = np.asarray(line.get("ys"), dtype=int).reshape(-1)
    n_pts = len(xs)
    out = []
    codes = set(b.color_code) | set(b.color_code_up_down)
    main = b._main_pts_xy
    main_pts = (np.stack([main[0], main[1]], axis=1) if main is not None
                else np.zeros((0, 2), dtype=int))

    # ── 关 5：时间够吗（量**实际走行距离**，不是画了多少像素）────────────
    n_walk = _walk_len(xs, ys, line.get("goal"))
    t_slow = n_walk / SPEED_CONSERVATIVE
    t_fast = n_walk / SPEED_OPTIMISTIC
    limit_px = int(home_timeout * SPEED_CONSERVATIVE)
    if t_slow < home_timeout * 0.7:
        v5 = OK
    elif t_slow < home_timeout:
        v5 = WARN
    else:
        v5 = BAD
    out.append((f"关 5 超时", v5,
                f"最坏走行 {n_walk}px（画了 {n_pts}px）→ 预估 {t_fast:.1f}~{t_slow:.1f}s"
                f"（上限 {home_timeout:.0f}s ÷ {SPEED_CONSERVATIVE:.1f}px/s ≈ {limit_px}px）"))

    # ── 关 3：会不会被提前踢出 ──────────────────────────────────────
    # 退出回正要**同时**满足两条（`try_return_to_mainline`）：
    #   ① `|角色 y − 主线站立行| ≤ 1`（同一高度）
    #   ② `d_home ≥ d_main − tol`（离主线不比离回正线远）
    # 角色正走在回正线上时 d_home = 0 → ② 退化成 `d_main ≤ tol`。
    # ⇒ **只查"离主线近"是不够的**：坑底离主线 2px 但 y 差 4px → 条件① 不成立，
    #   照样安全（这正是本图 (94,126) 的情况）。必须两条一起判。
    # ⚠️ 还要**排除终点区**：判废规则⑤要求「终点必须贴主线」，那儿退出是**设计意图**。
    # ★ 2026-09-17 修两个误报（真机 118px 线一次踩出 31 个假红点）：
    #   ① 终点是**一整条黄带**（不是 1 个点），原来只排除了「离 goal 点曼哈顿 ≤3」
    #      → 黄带其余部分全被当成"半路踢出"。改成**按 goal 色排除整条带**。
    #   ② `d_main == 0` 的点 = 角色**正踩在主线像素上** → 引擎判"已回主线"退出，
    #      这是**正确行为**，不是提前踢出。只有 `0 < d_main ≤ tol`
    #      （离主线 1~2px 但还没踩上去）才算"半路踢出"。
    goal = line.get("goal")
    _gset = goal_codes(codes)
    _img = getattr(b, "img_route_home", None)
    _mid = []
    for _x, _y in zip(xs, ys):
        _x, _y = int(_x), int(_y)
        if goal is not None and (abs(_x - int(goal[0]))
                                 + abs(_y - int(goal[1])) <= 3):
            continue                       # 终点圆点附近，按老规矩排除
        if (_img is not None and _gset
                and 0 <= _y < _img.shape[0] and 0 <= _x < _img.shape[1]):
            _rgb = tuple(int(v) for v in _img[_y, _x][:3])
            if _rgb in _gset:
                continue                   # ★ 整条 goal 黄带都排除
        _mid.append((_x, _y))
    stand_y = getattr(b, "_main_stand_y", None)
    early = []
    for _x, _y in _mid:
        _dm = _main_dist(main_pts, _x, _y)
        if _dm is None or _dm == 0:        # ★ 踩在主线像素上 → 本来就到家了
            continue
        if stand_y is not None and abs(_y - int(stand_y)) <= 1 and _dm <= tol:
            early.append((_x, _y, _dm))
    v3 = OK if not early else BAD
    # ★ 2026-09-17 再修一层（真机 118px 线剩下的那 1 个点）：
    #   梯子口**必然**落在站立行上、且离主线 1px —— 因为"爬上去就是主线"。
    #   这种"**到达区**擦到主线"是**正确行为**（引擎判已回主线 → 退出，人就到家了），
    #   不该报红。真正该报红的是「**整条线沿着主线走**」（画了等于没画）。
    #   所以按**比例**分级：偶尔擦到 = 通过；大段贴着 = 不通过。
    _flag_ok = max(2, int(round(n_pts * 0.05)))       # 5% 以内算"到达区擦到"
    _flag_bad = max(_flag_ok + 1, int(round(n_pts * 0.20)))
    if not early:
        v3 = OK
    elif len(early) <= _flag_ok:
        v3 = OK
    elif len(early) < _flag_bad:
        v3 = WARN
    else:
        v3 = BAD
    out.append(("关 3 提前踢出", v3,
                f"中途**同时**满足「y 在站立行±1」且「离主线 1~tol({tol})」的点："
                f"{len(early)} 个（共 {n_pts}px，{len(early) / max(n_pts, 1):.0%}）"
                + (f" → {early[:12]}（这些点会被判成『已回主线』、半路踢出）"
                   if early else "（终点黄带 + 踩在主线上的点已排除；那里退出是设计意图）")
                + (f"；≤{_flag_ok} 个算『到达区擦到』（梯子口必然贴着主线）→ 放行"
                   if 0 < len(early) <= _flag_ok else "")))

    # ── 关 2：落点覆盖 —— 直接问**引擎**：坑底每个落点进不进得了回正 ──────
    # ⚠️ 不要自己比"横段跨度 vs 平台宽度"：引擎进回正靠的是
    #    `d_home < d_main - tol`（坑底 tol 会收成 0），而 `d_home` **没有半径上限** ——
    #    落点在回正线外几像素、十几像素都还能进（本图实测 x=86 离横段左端 8px 也进得去）。
    #    所以"横段没盖满平台"不等于接不住。**唯一可靠的判据是调引擎自己判。**
    pit_y = int(np.bincount(ys).argmax())        # 坑底行 = 回正线像素 y 的众数
    if main_pts.shape[0]:
        x0, x1 = int(main_pts[:, 0].min()) - 8, int(main_pts[:, 0].max()) + 8
    else:
        x0, x1 = int(xs.min()) - 8, int(xs.max()) + 8
    bad = []
    for _yy in (pit_y, pit_y + 1, pit_y + 2):
        for _xx in range(x0, x1 + 1):
            b.loc_player_global = (_xx, _yy)
            b.home_d_main = None                 # 强制重算，别拿上一个落点的缓存
            b.home_d_home = None
            b._home_line_near = None
            b.is_using_home_route = False
            if b._home_line_pick() is None:
                bad.append((_xx, _yy))
    total = 3 * (x1 - x0 + 1)
    v2 = OK if not bad else (WARN if len(bad) <= total * 0.1 else BAD)
    out.append(("关 2 落点覆盖", v2,
                f"坑底 y={pit_y}~{pit_y + 2} × x={x0}~{x1} 共 {total} 个落点，"
                f"进不了回正 {len(bad)} 个"
                + (f" → {bad[:12]}{' …' if len(bad) > 12 else ''}" if bad else "")))

    # ── 关 4：跳跃 / 梯子 ─────────────────────────────────────────
    cmd_cnt = {}
    img = b.img_route_home
    if img is not None:
        for rgb, cmd in list(b.color_code.items()) + list(b.color_code_up_down.items()):
            k = collect_pixels(img, {rgb})
            if k:
                cmd_cnt[str(cmd)] = cmd_cnt.get(str(cmd), 0) + len(k)
    n_jump = sum(v for k, v in cmd_cnt.items() if "jump" in k)
    n_ladder = sum(v for k, v in cmd_cnt.items() if ("up" in k or "down" in k))
    if n_ladder:
        if n_jump:
            v4, note = WARN, (f"含跳跃 {n_jump}px、**梯子 {n_ladder}px** —— "
                              f"梯子段依赖 is_on_ladder 识别，需真机确认")
        else:
            # ★ 2026-09-17 加（真机踩出来的静默失效）：
            #   有 up/down（要爬管/爬梯）却**一个 jump 像素都没有**。
            #   最常见原因不是"你没跳"，而是**录制器没把跳记下来** ——
            #   旧版录制器判定跳跃写死成 `if "space" in key_press`，
            #   只要 config 的 key.jump 不是空格（比如 = c），录制时一次跳都记不到。
            #   后果：机器人走到管柱底下只会按 up，管柱入口偏高就**上不去**。
            _jk = str((b.cfg or {}).get("key", {}).get("jump") or "space").strip().lower()
            v4, note = BAD, (f"**梯子/管道 {n_ladder}px 却一个跳跃像素都没有** —— "
                             f"多半是录制时那一跳**没记下来**。"
                             f"先确认 config 的 `key.jump`（现在是 '{_jk}'）："
                             f"旧版录制器只认 'space'，配成别的键会静默漏掉所有跳。"
                             f"修好后重录，或用手绘器补一个跳跃段。")
    elif n_jump:
        v4, note = WARN, (f"含跳跃 {n_jump}px —— 落点 = 起跳点 ± 4~6px（实测），"
                          f"跳跃对面的平台要有余量接住")
    else:
        v4, note = OK, "无跳跃 / 无梯子（纯走位）"
    out.append(("关 4 跳跃梯子", v4, note))

    # ── 加分：主线站立行是否唯一（多层地形会算错）──────────────────
    if main_pts.shape[0]:
        vals, cnts = np.unique(main_pts[:, 1], return_counts=True)
        order = np.argsort(-cnts)
        top = [(int(vals[i]), int(cnts[i])) for i in order[:3]]
        share = cnts[order[0]] / cnts.sum()
        v_extra = OK if share >= 0.6 else (WARN if share >= 0.4 else BAD)
        note = (f"主线像素 y 分布前 3 = {top}，众数 y={top[0][0]} 占比 {share:.0%}"
                f"（`_main_stand_y` 取众数；多层地形占比低 → 站立行会认错）")
    else:
        v_extra, note = WARN, "主线像素读不到"
    out.append(("加分 站立行唯一", v_extra, note))
    return out


def main():
    map_name = sys.argv[1] if len(sys.argv) > 1 else None
    b, ret = _load_bot(map_name)
    name = b.cfg.get("bot", {}).get("map", "?")
    p = home_params(b.cfg)
    tol = int(p["home_dist_tol"])
    home_timeout = float(p["home_timeout"])
    search_range = int(b.cfg["route"].get("search_range", 10))

    print("=" * 72)
    print(f"回正线体检：{name}     (load_config 返回 {ret})")
    print("=" * 72)
    print(f"参数：home_dist_tol={tol}  home_timeout={home_timeout:.0f}s  "
          f"search_range={search_range}px")
    print(f"速度基准（2026-09-15 实测）：{MEASURED_PX}px 用 "
          f"{MEASURED_FAST}~{MEASURED_SLOW}s → "
          f"{SPEED_OPTIMISTIC:.1f}~{SPEED_CONSERVATIVE:.1f} px/s")

    lines = b.home_lines or []
    if not lines:
        print("\n❌ 这张图**没有可用回正线**（全判废或没画）→ 掉下去只能站桩。")
        return 1
    print(f"\n可用回正线 {len(lines)} 条")
    worst = {}
    for ln in lines:
        print(f"\n---- #{ln.get('id')}  "
              f"{ln.get('start')} → {ln.get('goal')}   "
              f"{ln.get('n_pts')} px   含跳跃={ln.get('has_jump')} ----")
        for label, verdict, note in check_line(b, ln, tol, home_timeout, search_range):
            print(f"  {verdict}  {label}：{note}")
            key = label.split(" ")[1] if " " in label else label
            rank = {OK: 0, WARN: 1, BAD: 2}[verdict]
            if key not in worst or rank > worst[key][0]:
                worst[key] = (rank, verdict)
    print("\n" + "=" * 72)
    print("汇总：" + "  ".join(f"{k}={v[1]}" for k, v in sorted(worst.items())))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
