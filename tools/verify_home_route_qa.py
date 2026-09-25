# -*- coding: utf-8 -*-
'''
QA 自检：v0.9 回正线**第二版**（方案重构：删落点锚 → 「谁近走谁」）（2026-09-13）

★ 本文件在 v0.9 第二版被**整体重写**。
  原版 1489 行全部是围绕「落点锚 127,0,127」写的（QA-1~QA-22），
  第二版把锚点整个删掉了，那些用例的前提不复存在 —— 留着只会一直红。

新的核心判据（进入 / 保持 / 退出**同一个表达式**，口径唯一）：
    d_home = 角色 → 最近「已武装」回正线像素 的曼哈顿距离
    d_main = 角色 → 最近主线像素 的曼哈顿距离（**全图**，不受 search_range 限制）
    d_home < d_main - home_dist_tol(2)  →  进 / 保持回正
    否则                                →  主线优先（相等也是主线优先）

与 tools/verify_home_route.py 的**分工**：
    那个测「纯函数对不对」；本文件测「装进引擎后，角色在游戏里会不会翻车」。

三条硬原则（沿用第一版，仍然成立）：
  1. 一律挂**引擎真实方法**，像素搜索那一层用"读脚下像素"短路（见 _get_nearest_color_code），
     保证「回正线接不上 → 退出」这个出口仍然能被走到。
  2. 逐帧**离线模拟**角色真实移动（进回正 → 发指令 → 移动 → 再判定），
     而不是只调一次函数看返回值 —— 卡死 / 抢跑 / 横跳都是**跨帧**现象。
  3. 断言一律用真实数据（废都南方工地，中文路径）。

不开游戏、不抢焦点、不初始化驱动，也不再往 minimaps/ 下写任何临时目录
（第一版那套 make_tmp_map 三重保险已不需要 —— 本文件不落盘）。

用法：
    python -m tools.verify_home_route_qa
'''
import os
import shutil
import sys
import tempfile
import time as _real_time

import numpy as np
import cv2

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as Bot
from src.utils.home_route import (
    DEFAULT_GOAL_RGB,
    DEFAULT_HOME_PARAMS,
    LEGACY_ANCHOR_RGB,
    MSG_COVER,
    MSG_GOAL_FAR,
    MSG_NO_GOAL,
    MSG_NO_MAINLINE,
    MSG_NO_MOVE,
    MSG_SPAN,
    calc_line_geom,
    collect_pixels,
    color_code_maps,
    far_segment_stats,
    home_params,
    home_pixel_clash,
    home_should_return,
    paint_converging_hseg,
    render_home_image,
    simulate_coverage,
    split_components,
    validate_home_line,
    wipe_legacy_anchor,
)

FAIL = []
NOTE = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def soft(name, got, want):
    '''只标红提醒、不算失败的核对项（数值随资源变化，人工看一眼即可）。'''
    ok = got == want
    print(f"  [{'核对' if ok else '★核对'}] {name}: got={got!r} want={want!r}")
    if not ok:
        NOTE.append(name)


def sec(name):
    print(f"\n{name}")


# ══════════════════════════════════════════════════════════════════════
# 地形常量（真实数据：平台 y=122，坑底 y=128~129）
# ══════════════════════════════════════════════════════════════════════
MAIN_Y = 122                 # 平台（主线）所在行
MAIN_X0, MAIN_X1 = 92, 106   # 平台横向范围（31 px，用户实测就这么短）
PIT_Y = 128                  # 坑底
PIT_X0, PIT_X1 = 94, 108     # 坑的横向范围
CORNER_X = 99                # L 形拐角（竖段所在的列）
SHAPE = (187, 268)           # 小地图尺寸（h, w）

GOAL_RGB = DEFAULT_GOAL_RGB              # 黄 (255,255,0) = 终点
JUMP_RGB = (255, 0, 255)                 # 洋红 = none none jump
BLUE_RGB = (0, 0, 255)                   # 蓝 = right none none
RED_RGB = (255, 0, 0)                    # 红 = left none none

CC_HOME = {
    RED_RGB: "left none none",
    BLUE_RGB: "right none none",
    JUMP_RGB: "none none jump",
    GOAL_RGB: "none none goal",
}

HOME_PARAMS = {
    "home_dist_tol": 2,
    "home_min_cover_span": 5,
    "home_cover_far_tol": 2,
    "home_goal_mainline_tol": 1,
    "home_min_return_span": 5,
    "home_min_leave": 0,
    "home_min_frames": 0,
    "home_timeout": 8,
    "home_reenter_lock": 10,
    "home_rearm_margin": 4,
    "home_rearm_timeout": 30,
    # ── 起跳前先停稳（2026-09-15 补，**测试配置保真**）──────────────────────
    # ⚠️ 之前没有这一项 → 引擎 `_apply_jump_brake` 读不到就用默认 **True**
    #    → 这条**真机上已经关掉**的功能在测试里是开着的：角色走到起跳点那一帧
    #    因为"还在移动"被清成 `none none none`，本脚本的循环到那儿就停
    #    → 假失败「★ 最终走回主线并自动退出回正 / ★ 退出后 img_route 已交回主线」。
    #    真实配置 config_default.yaml 里 home_jump_brake = false（实测合并后也是 False），
    #    所以测试必须跟着关 —— 否则测的是"真机上不存在的行为"。
    "home_jump_brake": False,
    "home_jump_brake_frames": 3,
}

BASE_CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9},
    "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
    "monster_detect": {"search_box_margin": 50},
    "key": {"teleport": ""},
    "route": dict({"search_range": 10, "rescue_range": 40, "color_code": {
        "255,0,0": "left none none",
        "0,0,255": "right none none",
        "255,0,255": "none none jump",
        "255,255,0": "none none goal",
    }}, **HOME_PARAMS),
    "watchdog": {"range": 10, "timeout": 10, "attack_grace": 5},
}


class Stub:
    '''只带被测方法所需字段的最小宿主（方法全挂引擎真实实现）。'''


def new_bot(player_global=(100, 100), cfg=None):
    b = Stub()
    b.cfg = cfg if cfg is not None else BASE_CFG
    b.loc_player = (500, 300)
    b.loc_player_global = player_global
    b.img_frame = np.zeros((600, 1000, 3), dtype=np.uint8)
    b.monsters = []
    b._monsters = []
    b.cmd_move_x = "none"
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.cmd_move_x_last = "none"
    b._turn_frames_left = 0        # 2026-09-17 转身状态机（见 update_cmd_by_mob_detection）
    b._turn_dir = None
    b.is_using_home_route = False
    b.is_on_ladder = False
    b.img_route_home = None
    b.img_route = None
    b.img_routes = []
    b.color_code = dict(CC_HOME)
    b.color_code_up_down = {}
    # ── v0.9 第二版跨帧状态 ──────────────────────────────────────────
    b.home_lines = []
    b.home_line = None
    b.home_d_home = None
    b.home_d_main = None
    b._home_line_near = None
    b._main_seg_i = 0
    b._main_pts_xy = None
    b._main_pts_seg = None
    b.home_enter_pos = None
    b.home_enter_t = 0.0
    b.home_frame = 0
    b.home_last_goal_log = 0.0
    b.home_reenter_lock_t = 0.0
    b.t_last_home_lock_log = 0.0
    b.t_last_attack = 0.0
    b.t_last_home_fail_log = 0.0
    b.idx_routes = 0
    b.route_called = False
    b.route_reverse = False
    b._pingpong_start = None
    b._seg_goals = []
    b._seg_dir = []
    b._seg_span = []
    b._goal_armed = True
    b.is_show_debug_window = False
    b.img_route_debug = None
    b.loc_last_route_pixel = None
    b.t_route_rescue_log = 0.0
    return b


def _get_nearest_color_code(self):
    '''桩掉"像素搜索"这一层：**直接读角色脚下那个像素的颜色**。

    ⚠️ 为什么不用常量桩（第一版的教训）：
       用常量桩时，"主线找不找得到"和"回正线找不找得到"变成同一个答案，
       测不出「A 说保持、B 说退出」的口径分裂。这里照着**真实图像内容**取色，
       等于把 get_nearest_color_code 的"找最近像素"这一步短路成"脚下这个"，
       既确定性、又保留了图像编码的语义（`回正线接不上 → 退出` 这条出口照样走得到）。
    '''
    img = self.img_route
    if img is None or not self.loc_player_global:
        return None, None
    x, y = int(self.loc_player_global[0]), int(self.loc_player_global[1])
    h, w = img.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return None, None
    rgb = tuple(int(v) for v in img[y, x][:3])
    cmd = self.color_code.get(rgb) or self.color_code_up_down.get(rgb)
    if not cmd:
        return None, None
    return {"command": cmd, "distance": 0, "pixel": (x, y), "color": rgb}, None


def _get_monsters_in_range(self, _tl, _br):
    return list(self._monsters)


def _get_nearest_monster(self, is_left=True):
    for m in self.monsters:
        cx = m["position"][0] + m["size"][0] // 2
        if is_left and cx < self.loc_player[0]:
            return m
        if not is_left and cx > self.loc_player[0]:
            return m
    return None


# ── 自动挂**全部**引擎真实方法（以后再加方法也不会一挂就崩）────────────────
# ⚠️ 这里原来写的是**手写白名单**（注释写着"全量挂"，实现却是白名单）。
#    2026-09-14 引擎加了 `_perf` / `_apply_recenter` / `_apply_jump_brake` /
#    `_trace_home_cmd` 之后没人来补名单 → **本脚本从那天起静默跑不了**：
#    一跑到 update_cmd_by_route 里的 `_perf(...)` 就 AttributeError。
#    （同一个坑在 verify_navigation.py 里已经踩了 7 次，那边靠"记得来挂"，
#      这边干脆改成自动 —— 引擎有、Stub 没有的，一律挂真实实现。）
#    ① 只补**缺失**的 → 上面三个故意桩掉的方法（像素搜索 / 索敌）不会被覆盖
#    ② 以后引擎再加方法，这里自动跟上，不用再记着来改
Stub.get_monsters_in_range = _get_monsters_in_range
Stub.get_nearest_monster = _get_nearest_monster
Stub.get_nearest_color_code = _get_nearest_color_code
for _name in dir(Bot):
    if _name.startswith("__") or hasattr(Stub, _name):
        continue
    _attr = getattr(Bot, _name)
    if callable(_attr):
        setattr(Stub, _name, _attr)


def make_mainline(pts=None, shape=SHAPE):
    '''主线 RGB 图（蓝 = right none none）。默认 = 真实平台那一段。'''
    if pts is None:
        pts = [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)]
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    for (x, y) in pts:
        img[y, x] = BLUE_RGB
    return img


def make_l_home(corner_x=CORNER_X, pit_y=PIT_Y, main_y=MAIN_Y,
                x0=PIT_X0, x1=PIT_X1, shape=SHAPE):
    '''标准 L 形回正线：横段沿坑底（逐像素汇聚着色）+ 竖段跳回主线 + goal 贴主线。

    ⚠️ 横段必须用 paint_converging_hseg **逐像素**着色（不能用 cv2.line 整段一色）：
       落点在拐角左边要往右走、在右边要往左走，一色的话一半的落点会被带偏。
    '''
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, pit_y) for x in range(x0, x1 + 1)],
                          (corner_x, pit_y), CC_HOME, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(main_y + 1, pit_y + 1):       # 竖段（不含 main_y 那一行）
        img[y, corner_x] = JUMP_RGB
    img[main_y, corner_x] = GOAL_RGB             # 终点**贴在主线上**
    return img


def new_home_bot(player_global, mainline_img=None, home_img=None):
    b = new_bot(player_global=player_global)
    b.color_code = dict(CC_HOME)
    b.color_code_up_down = {}
    b.img_routes = [mainline_img if mainline_img is not None else make_mainline()]
    b.img_route_home = home_img if home_img is not None else make_l_home()
    b.img_route = b.img_routes[0]
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("QA自检", b.img_route_home, b.img_routes)
    return b


# ══════════════════════════════════════════════════════════════════════
sec("【QA-1】★ 地形闸门：坑底**每个**落点都接得住 / 主线上**每个**点都不误进")
# ⚠️ 为什么逐点验（设计 11-⑬）：坑是 15px 宽，用户在任意一格掉下去都要能接住。
#    只看"中心点接得住"的话，坑两端（x=94 / x=108）没接住是**静默**的 ——
#    角色掉在边上就一直站着打怪，日志一切正常，极难查。
# ⚠️ 反方向同样重要：主线上巡逻时一个都不许误进（否则 = 巡逻被回正打断）。
# ══════════════════════════════════════════════════════════════════════
_l_home = make_l_home()
_main = make_mainline()
_home_pts = collect_pixels(_l_home, set(CC_HOME))
_main_pts = collect_pixels(_main, set(CC_HOME))

cov = simulate_coverage(_home_pts, _main_pts,
                        [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)], tol=2)
check("坑底 y=128 的 15 个落点全覆盖（覆盖率 15/15）",
      (cov["total"], cov["covered"]), (15, 15))
check("没有漏掉的落点", cov["missed"], [])

# 坑底再低一行（y=129，实测地板有 1px 抖动）也要接得住
cov2 = simulate_coverage(_home_pts, _main_pts,
                         [(x, PIT_Y + 1) for x in range(PIT_X0, PIT_X1 + 1)], tol=2)
check("坑底 y=129（低 1px）同样 15/15", (cov2["total"], cov2["covered"]), (15, 15))

# 主线上**一个都不许**进回正
cov_main = simulate_coverage(
    _home_pts, _main_pts,
    [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)], tol=2)
check("主线上 15 个点**全部不**进回正（不误触发）", cov_main["covered"], 0)
check("主线上被漏判的点数 = 全部 15 个（= 都没进）",
      len(cov_main["missed"]), len(range(MAIN_X0, MAIN_X1 + 1)))

# 用**引擎真实**方法再走一遍（上面是纯数学，这里是真状态机）
bad_pit, bad_main = [], []
for x in range(PIT_X0, PIT_X1 + 1):
    b = new_home_bot((x, PIT_Y), home_img=make_l_home())
    if not b.home_lines:
        bad_pit.append((x, "没有可用回正线"))
        continue
    b.update_cmd_by_route()
    if not b.is_using_home_route:
        bad_pit.append((x, PIT_Y))
for x in range(MAIN_X0, MAIN_X1 + 1):
    b = new_home_bot((x, MAIN_Y), home_img=make_l_home())
    b.update_cmd_by_route()
    if b.is_using_home_route:
        bad_main.append((x, MAIN_Y))
check("★ 引擎真实状态机：坑底 15 个落点全部进入回正", bad_pit, [])
check("★ 引擎真实状态机：主线上 15 个点全部**不**进入回正", bad_main, [])

# ══════════════════════════════════════════════════════════════════════
sec("【QA-2】判废五条：逐条命中（第二版新判据，见设计 4.3）")
# ══════════════════════════════════════════════════════════════════════
codes = set(CC_HOME)
main_imgs = [make_mainline()]


def _paint(img, pts, rgb):
    for (x, y) in pts:
        img[y, x] = rgb
    return img


def _one(pts_map, shape=SHAPE):
    '''按 {rgb: [(x,y)...]} 画一张**单组件**回正线，返回 ((ok, why), comp, img)。'''
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    for rgb, pts in pts_map.items():
        _paint(img, pts, rgb)
    comps = split_components(img, codes)
    assert len(comps) == 1, f"期望 1 个连通域，实际 {len(comps)}"
    return validate_home_line(comps[0], img, main_imgs, codes, BASE_CFG), comps[0], img


# ① 无 goal（只有动作像素）
(ok1, why1), _, _ = _one({JUMP_RGB: [(99, y) for y in range(124, 129)]})
check("① 没有黄点 → 判废", ok1, False)
check("① 报的是「没有终点标记」", why1, MSG_NO_GOAL)

# ② 只有 goal、没有动作像素
(ok2, why2), _, _ = _one({GOAL_RGB: [(99, 122)]})
check("② 只有终点、没有动作像素 → 判废", ok2, False)
check("② 报的是「一个动作像素都没有」", why2, MSG_NO_MOVE)

# ③ span < 5（原地起头原地收尾）
(ok3, why3), _, _ = _one({BLUE_RGB: [(99, 126), (100, 126), (101, 126)],
                          GOAL_RGB: [(101, 126)]})
check("③ 起点终点只差 2px → 判废", ok3, False)
print(f"       判废原因：{why3}")
check("③ 报的是「原地起头原地收尾」", why3.startswith(MSG_SPAN.split("{")[0]), True)

# ④ 只画竖线（PRD 2.4 反例）：span 够，但起始段覆盖跨度 = 1
(ok4, why4), comp4, _img4 = _one({JUMP_RGB: [(100, y) for y in range(123, 129)],
                                  GOAL_RGB: [(100, 122)]})
check("④ 只画竖线 → 判废（PRD 2.4 反例）", ok4, False)
_far4 = far_segment_stats(comp4, main_imgs, codes, BASE_CFG)
check("④ 起始段覆盖跨度确实是 1", _far4["cover_span"], 1)
check("④ 报的是「只接得住一个点」", why4, MSG_COVER.format(x=100))

# ⑤ 终点没贴主线（goal 画在半空中，离主线 5px）→ **判废**，不是警告
(ok5, why5), _, _ = _one(
    {JUMP_RGB: [(x, 133) for x in range(94, 106)] + [(99, y) for y in range(128, 134)],
     GOAL_RGB: [(99, 127)]})
check("⑤ 终点没画在主线上 → 判废（不是警告）", ok5, False)
print(f"       判废原因：{why5}")
check("⑤ 报的是「终点没画在主线上」",
      why5.startswith(MSG_GOAL_FAR.split("（")[0]), True)

# 没有主线 → 量不出 d_main → 判废（明确行为，不能崩、也不能静默放行）
# ⚠️ 终点放在**最下面**（y=128）：起点取"行序最靠前"那个（y=118），
#    这样 span = 10 ≥ 5，能一路走到判据 4 前面那条「有没有主线」的判断。
_nm_img = np.zeros(SHAPE + (3,), dtype=np.uint8)
_paint(_nm_img, [(99, y) for y in range(118, 128)], JUMP_RGB)
_nm_img[128, 99] = GOAL_RGB
_no_main = validate_home_line(split_components(_nm_img, codes)[0],
                              _nm_img, [], codes, BASE_CFG)
check("没有主线路线 → 判废（不崩）", _no_main[0], False)
check("报的是「请先录一段主线」", _no_main[1], MSG_NO_MAINLINE)

# 合法 L 形 → 全部通过
(ok7, why7), comp7, img7 = _one(
    {BLUE_RGB: [(x, PIT_Y) for x in range(PIT_X0, CORNER_X)],
     RED_RGB: [(x, PIT_Y) for x in range(CORNER_X + 1, PIT_X1 + 1)],
     JUMP_RGB: [(CORNER_X, y) for y in range(MAIN_Y + 1, PIT_Y + 1)],
     GOAL_RGB: [(CORNER_X, MAIN_Y)]})
check("合法 L 形 → 判有效", ok7, True)
check("判有效时原因为空", why7, "")

# 判废④ 的验算（设计 4.3 那两行，写死在这里防止以后改歪）
_comp_good = split_components(_l_home, codes)[0]
_far_good = far_segment_stats(_comp_good, main_imgs, codes, BASE_CFG)
check("L 形：起始段覆盖跨度 = 15（整条横段）", _far_good["cover_span"], 15)
_geom_good = calc_line_geom(_comp_good, main_imgs, codes, BASE_CFG, img_rgb=_l_home)
check("L 形：起点在坑底最右端（离主线最远）", _geom_good["start"], (PIT_X1, PIT_Y))
check("L 形：终点贴在主线上", _geom_good["goal"], (CORNER_X, MAIN_Y))
check("L 形：含跳跃", _geom_good["has_jump"], True)

# ══════════════════════════════════════════════════════════════════════
sec("【QA-3】一图 3 条线，中间那条废 → **只**抹中间那条（不连坐）")
# ⚠️ 判废粒度是**组件**不是整图：左边一个坑、右边一个坑，一条画废了
#    不能把另外两条一起干掉（第一版这块是对的，第二版重写后必须验回来）。
# ══════════════════════════════════════════════════════════════════════
img3 = np.zeros(SHAPE + (3,), dtype=np.uint8)
# 线 A（合法 L 形，坑在左）
maskA = np.any(_l_home != (0, 0, 0), axis=-1)
img3[maskA] = _l_home[maskA]
# 线 B（废：只画竖线，x=150）
for y in range(60, 68):
    img3[y, 150] = JUMP_RGB
img3[60, 150] = GOAL_RGB
# 线 C（合法 L 形，坑在右）
# ⚠️ 必须**给它配一段自己的主线**（x=196~218）：判据④/⑤都是相对主线量的，
#    右边这条线离左边那条主线 90px 开外，不配主线的话它自己会被判废 ——
#    那是"数据画错了"，不是"判废粒度的 bug"，会掩盖本用例真要验的东西。
_l2 = make_l_home(corner_x=205, pit_y=128, main_y=122, x0=200, x1=214)
maskC = np.any(_l2 != (0, 0, 0), axis=-1)
img3[maskC] = _l2[maskC]

b3 = new_bot()
b3.color_code = dict(CC_HOME)
b3.img_routes = [make_mainline(),
                 make_mainline([(x, MAIN_Y) for x in range(196, 219)])]
b3._build_mainline_cache()
lines3 = b3._scan_home_lines("QA-3", img3, b3.img_routes)
check("3 条里留下 2 条", len(lines3), 2)
check("留下的是 A 和 C（不是中间那条 B）",
      sorted(ln["goal"][0] for ln in lines3), [99, 205])
check("废线 B 的像素被**就地涂黑**",
      len(collect_pixels(img3[55:70, 145:155], codes)), 0)
check("线 A / 线 C 的像素没被误伤",
      len(collect_pixels(img3, codes)),
      len(_home_pts) + len(collect_pixels(_l2, codes)))

# ══════════════════════════════════════════════════════════════════════
sec("【QA-4】旧图迁移（127,0,127 涂黑）+ 撞色自检改数**回正线像素**")
# ⚠️ 127,0,127 **不在**任何色码表里 → 角色站上去取不到指令 → 站着不动且不报错。
#    所以旧 PNG 上残留的锚点必须在加载期就地涂黑（Q6 裁决）。
# ⚠️ 撞色自检第二版改成数"回正线像素总数"（第一版只数锚点像素 = 等于没自检）。
# ══════════════════════════════════════════════════════════════════════
legacy = make_l_home()
legacy[MAIN_Y, CORNER_X] = GOAL_RGB
legacy[PIT_Y, CORNER_X] = LEGACY_ANCHOR_RGB      # 旧版落点锚残留在拐角
wiped = wipe_legacy_anchor(legacy)
check("旧锚点被清掉 1 个", len(wiped), 1)
check("清掉的正是 (99,128)", wiped, [(CORNER_X, PIT_Y)])
_remain = {tuple(int(v) for v in legacy[y, x][:3])
           for (x, y) in collect_pixels(legacy, codes)}
check("127,0,127 已经不在图上了", LEGACY_ANCHOR_RGB in _remain, False)
check("LEGACY_ANCHOR_RGB 常量只用于迁移，值仍是 127,0,127",
      LEGACY_ANCHOR_RGB, (127, 0, 127))
_cc_tbl, _ud_tbl = color_code_maps(BASE_CFG)
check("127,0,127 不在任何一张色码表里",
      LEGACY_ANCHOR_RGB in (set(_cc_tbl) | set(_ud_tbl)), False)
check("色码表只有两张（不再有第三张锚点表）", len(color_code_maps(BASE_CFG)), 2)

# 撞色自检：底图里出现同色 → 被 mask 吃掉 → 数得出来 + 列坐标
raw = np.zeros(SHAPE + (3,), dtype=np.uint8)
_paint(raw, [(x, PIT_Y) for x in range(94, 109)], BLUE_RGB)
_paint(raw, [(99, y) for y in range(123, 129)], JUMP_RGB)
raw[MAIN_Y, 99] = GOAL_RGB
masked = raw.copy()
masked[PIT_Y, 100] = (0, 0, 0)          # 模拟被底图同色吃掉 2 个像素
masked[PIT_Y, 101] = (0, 0, 0)
clash = home_pixel_clash(raw, masked, codes)
check("撞色自检：数出被吃掉的 2 个像素", len(clash), 2)
check("列的坐标是对的", clash, [(100, PIT_Y), (101, PIT_Y)])
check("没被吃的时候自检不误报", home_pixel_clash(raw, raw, codes), [])

# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
# 废线夹具：自己画一条「非 L 形 / 终点没贴主线」的非法回正线
# ══════════════════════════════════════════════════════════════════════
#: 废线①：**只画一条竖线**（非 L 形）—— PRD 2.4 的反例 → 判废④（覆盖跨度 = 1）
WASTE_VERTICAL = {
    "segs": [((150, 130), (150, 140), JUMP_RGB)],
    "goal": (150, 130),
}
#: 废线②：L 形画对了，但**终点停在半空**（离主线 4px）→ 判废⑤（终点没贴主线）
WASTE_GOAL_FAR = {
    "segs": [((86, 128), (110, 128), BLUE_RGB), ((98, 124), (98, 128), JUMP_RGB)],
    "goal": (98, 127),
}

#: 废线夹具一览（子目录中文名 → 夹具、人话原因）
WASTE_FIXTURES = (
    ("只画竖线", WASTE_VERTICAL, "非 L 形 / 只画一条竖线"),
    ("终点悬空", WASTE_GOAL_FAR, "L 形但终点没贴主线"),
)


def waste_home_image(map_img, cc_dict, sub, spec):
    '''自绘一张「废线」route_home.png —— **落盘到中文路径再读回**。

    ⚠️ 为什么必须用夹具、不能直接读 `minimaps/废都南方工地/route_home.png`：
       那条线 2026-09-13 被重画成了**合法**的 L 形（横段 y=128 汇聚着色 +
       竖段 x=98 跳跃 + 终点 (98,122) 贴主线，34 px / 1 条可用）。于是
       "废线必须被判废"这条规则被绑死在**现网资源当时的临时状态**上 ——
       用户一画好就必然红。断言要的是"规则成立"，不是"文件此刻长这样"，
       所以夹具自己画，跟现网文件怎么改都无关。

    ⚠️ 为什么仍然要落盘到**中文路径**再读回：`cv2.imread` 在中文路径上
       **静默返回 None**（不抛异常）。原用例顺带覆盖了这条真实存在的坑，
       改用夹具后这条覆盖不能丢 —— 落盘走 `imwrite_unicode`、读回走
       `load_image`（内部是 `cv2.imdecode`），和线上是同一套。

    Returns:
        RGB 图（已 mask），可直接喂给 `bot._scan_home_lines`
    '''
    from src.utils.common import imwrite_unicode, load_image, mask_route_colors
    from src.utils.home_route import stamp_goal

    tmp = os.path.join(tempfile.gettempdir(), "回正线自检_废线夹具", sub)
    os.makedirs(tmp, exist_ok=True)
    try:
        bgr = np.zeros(SHAPE + (3,), dtype=np.uint8)
        for (x0, y0), (x1, y1), rgb in spec["segs"]:
            cv2.line(bgr, (x0, y0), (x1, y1),
                     (int(rgb[2]), int(rgb[1]), int(rgb[0])), thickness=1)
        stamp_goal(bgr, spec["goal"], GOAL_RGB, 1)
        path = os.path.join(tmp, "route_home.png")
        imwrite_unicode(path, bgr)
        img = cv2.cvtColor(load_image(path), cv2.COLOR_BGR2RGB)
        return mask_route_colors(
            map_img, img, {f"{r},{g},{b}": v for (r, g, b), v in cc_dict.items()})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:                                   # 父目录空了就一并清掉，不留空壳
            os.rmdir(os.path.dirname(tmp))
        except OSError:
            pass


sec("【QA-5】真实资源 + 中文路径 + 废线夹具（废都南方工地）")
# ══════════════════════════════════════════════════════════════════════
try:
    from src.utils.common import load_image, mask_route_colors
    # ⚠️ 冻结夹具，不读 `minimaps/废都南方工地/` 活文件（2026-09-16 改）：
    #    用户重录主线后像素数从 42 变成 118，本节断言当场红 —— 红的是数据不是代码。
    #    夹具 = 断言写定时对着的那份数据，见 tools/test_fixtures/。
    _map = load_image("tools/test_fixtures/废都南方工地/map.png")
    _cc_str = {f"{r},{g},{bl}": v for (r, g, bl), v in CC_HOME.items()}
    _routes = []
    for _n in ("route1.png", "route2.png"):
        _r = cv2.cvtColor(load_image(f"tools/test_fixtures/废都南方工地/{_n}"), cv2.COLOR_BGR2RGB)
        _routes.append(mask_route_colors(_map, _r, _cc_str))
    b5 = new_bot()
    b5.color_code = dict(CC_HOME)
    b5.img_routes = _routes
    b5._build_mainline_cache()
    check("中文路径下主线读得到（route1+route2 共 42 px）",
          sum(len(b5._route_pixels(r)) for r in _routes), 42)

    # ── ① 废线夹具（自绘，与现网文件无关）────────────────────────────
    for _sub, _spec, _why in WASTE_FIXTURES:
        _waste = waste_home_image(_map, CC_HOME, _sub, _spec)
        _n0 = len(collect_pixels(_waste, set(CC_HOME)))
        check(f"夹具「{_why}」中文路径落盘→读回后像素还在（{_n0} px）",
              _n0 > 0, True)
        check(f"★ 废线「{_why}」→ 0 条可用",
              len(b5._scan_home_lines("废线夹具", _waste, _routes)), 0)
        check(f"★ 废线「{_why}」像素被逐条抹掉",
              len(collect_pixels(_waste, set(CC_HOME))), 0)

    # ── ② 现网 route_home.png：只断言"状态无关"的不变量 ───────────────
    # 用户随时会重画这条线（废→合法、合法→废都正常），所以**不写死几条**：
    # 写死 0 会在画好后红，写死 1 会在重画/删线后红。
    _home = cv2.cvtColor(load_image("tools/test_fixtures/废都南方工地/route_home.png"),
                         cv2.COLOR_BGR2RGB)
    check("现网 route_home.png 在中文路径下读得到",
          None if _home is None else _home.shape, SHAPE + (3,))
    _home = mask_route_colors(_map, _home, _cc_str)
    _got5 = b5._scan_home_lines("废都南方工地", _home, _routes)
    _left5 = split_components(_home, set(CC_HOME))
    print(f"       现网 route_home.png → 可用回正线 {len(_got5)} 条"
          f"（扫描后图上剩 {len(_left5)} 个连通域）")
    check("★ 扫出来的条数 == 扫描后剩下的连通域数（判废不漏、也不连坐）",
          len(_got5), len(_left5))
    check("★ 扫出来的每一条都是合法回正线（终点贴主线 / 覆盖跨度够）",
          all(validate_home_line(c, _home, _routes, set(CC_HOME), b5.cfg)[0]
              for c in _left5), True)
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")

# ══════════════════════════════════════════════════════════════════════
sec("【QA-6】四道防 D1~D4 + 长程模拟（掉下去 → 走回来 → 退出 → 巡逻）")
# ══════════════════════════════════════════════════════════════════════
# D4：这几个旋钮"关不掉"（填 0 / 负数回落默认）
for _k, _v, _d in (("home_timeout", 0, 8), ("home_reenter_lock", -1, 10),
                   ("home_rearm_timeout", 0, 30), ("home_min_cover_span", 0, 5),
                   ("home_min_return_span", 0, 5)):
    _cfg = {"route": dict(HOME_PARAMS, **{_k: _v})}
    check(f"★ D4：{_k} 填 {_v} 回落成默认 {_d}（关不掉）",
          home_params(_cfg)[_k], _d)
check("★ D4：home_dist_tol 填 -9 回落成 2",
      home_params({"route": dict(HOME_PARAMS, home_dist_tol=-9)})["home_dist_tol"], 2)
check("★ 判定式：相等时主线优先", home_should_return(6, 6, 2), False)
check("★ 判定式：差 2px（= tol）→ 仍然主线优先", home_should_return(6, 8, 2), False)
check("★ 判定式：差 3px 以上 → 回正", home_should_return(5, 8, 2), True)
check("★ 判定式：坑底实测 d_home=0 / d_main=6 → 回正",
      home_should_return(0, 6, 2), True)
check("★ 判定式：d_home 量不出 → 不进回正", home_should_return(None, 8, 2), False)
check("★ 判定式：d_main 量不出 → 不进回正", home_should_return(0, None, 2), False)

# D1 + D2 + D3：超时退出 → 清指令 + 冷静期 + 该线去武装
b6 = new_home_bot((CORNER_X, PIT_Y))
b6.update_cmd_by_route()
check("掉进坑底 → 进回正", b6.is_using_home_route, True)
b6.home_enter_t = _real_time.time() - 9        # 强制已经回正 9s（> home_timeout=8）
b6.update_cmd_by_route()
check("★ D3：超时退出（有出口，不锁死）", b6.is_using_home_route, False)
check("★ D3：超时那一帧指令全清成 none（不留 jump / goal）",
      (b6.cmd_move_x, b6.cmd_move_y, b6.cmd_action), ("none", "none", "none"))
check("★ D2：这条线被去武装", b6.home_lines[0]["armed"], False)
check("★ D1：冷静期已上锁", b6.home_reenter_lock_t > 0, True)
check("★ D1：新入口 `_home_line_pick` 被冷静期挡住", b6._home_line_pick(), None)
b6.update_cmd_by_route()
check("★ D1：锁着的那一帧**任何**入口都进不去", b6.is_using_home_route, False)
b6.home_reenter_lock_t = _real_time.time() - 11    # 冷静期自然到期
check("冷静期到期 → 解锁", b6._home_reenter_allowed(), True)
check("★ D2：解锁了也进不去（那条线还是未武装）", b6._home_line_pick(), None)

# D2 的恢复路径①：离开 bbox ≥ home_rearm_margin(4)
b6.loc_player_global = (CORNER_X, PIT_Y + 9)   # 走出包围盒 4px 以上
check("★ 离开这条线的范围 ≥4px → 重新武装", b6._home_line_armed(b6.home_lines[0]), True)
# D2 的恢复路径②：30s 兜底（角色一直在坑里晃悠也不能永远不接）
b6.home_lines[0]["armed"] = False
b6.home_lines[0]["disarm_t"] = _real_time.time() - 31
b6.loc_player_global = (CORNER_X, PIT_Y)
check("★ 30s 兜底 → 无条件重新武装（不会变成永远不回正）",
      b6._home_line_armed(b6.home_lines[0]), True)

# ── 长程模拟：真的走一遍 L 形，走到终点必须**自动退出** ──────────────────
JUMP_H = 6      # 一格跳 6px（引擎 JUMP_HEIGHT）


def _step(pos, cmd):
    mx, my, act = cmd.split()
    x, y = pos
    if mx == "left":
        x -= 1
    elif mx == "right":
        x += 1
    if act == "jump":
        y -= JUMP_H
    return (x, y)


b7 = new_home_bot((PIT_X0, PIT_Y))            # 从坑的**最左端**掉下去
trail = []
frames_home = 0
for _ in range(60):
    b7.update_cmd_by_route()
    if b7.is_using_home_route:
        frames_home += 1
    cmd = f"{b7.cmd_move_x} {b7.cmd_move_y} {b7.cmd_action}"
    trail.append((b7.loc_player_global, cmd, b7.is_using_home_route))
    if not b7.is_using_home_route and frames_home > 0:
        break
    if cmd.strip() == "none none none":
        break
    b7.loc_player_global = _step(b7.loc_player_global, cmd)
print(f"       走了 {len(trail)} 帧：{trail[0][0]} → {trail[-1][0]}")
check("★ 从坑最左端出发也能进回正", frames_home > 0, True)
check("★ 最终走回主线并自动退出回正", b7.is_using_home_route, False)
check("★ 退出后 img_route 已交回主线", b7.img_route is b7.img_routes[0], True)
check("★ 成功退出**不留**冷静期（下次真掉下去要马上接住）",
      b7.home_reenter_lock_t, 0.0)
check("★ 成功退出后这条线是**已武装**的", b7.home_lines[0]["armed"], True)
check("★ 没有出现「既不走也不打」的空转（每帧都有指令）",
      all(t[1].strip() != "none none none" for t in trail[:-1]), True)

# 抖动回归：站在竖段中段时 d_home=0、d_main 很小 → 不应每帧反复进出
b8 = new_home_bot((CORNER_X, MAIN_Y + 3))     # 竖段中段（y=125）
_flips = 0
_prev = None
for _ in range(30):
    b8.update_cmd_by_route()
    if _prev is not None and b8.is_using_home_route != _prev:
        _flips += 1
    _prev = b8.is_using_home_route
soft("竖段中段 30 帧内的进出翻转次数（0~2 都算不抖动）", _flips <= 2, True)

# ══════════════════════════════════════════════════════════════════════
sec("【QA-7】签名契约（设计 3.6）没被改坏")
# ══════════════════════════════════════════════════════════════════════
check("_route_pixels 不再有 include_anchor 形参",
      "include_anchor" in Bot._route_pixels.__code__.co_varnames, False)
check("render_home_image 不再有 anchor 形参",
      "anchor" in render_home_image.__code__.co_varnames, False)
for _m in ("_scan_home_lines", "_home_dist", "_main_dist", "_home_line_keep",
           "_home_line_pick", "_home_line_armed", "_enter_home_route",
           "_exit_home_route", "_home_dwell_ok", "_home_reenter_allowed",
           "_build_mainline_cache", "_log_home_health"):
    check(f"引擎有 {_m}()", hasattr(Bot, _m), True)
for _dead in ("_scan_home_anchors", "_home_anchor_armed", "_home_anchor_triggered",
              "_home_return_radius", "_home_anchor_dist"):
    check(f"旧方法 {_dead}() 已删除", hasattr(Bot, _dead), False)
check("DEFAULT_HOME_PARAMS 不再有任何一个 home_anchor_* 键",
      [k for k in DEFAULT_HOME_PARAMS if "anchor" in k], [])
check("DEFAULT_HOME_PARAMS 有 11 个键", len(DEFAULT_HOME_PARAMS), 11)
_src = __import__("inspect").getsource(Bot.__init__)
for _old in ("home_anchors", "home_anchor", "color_code_home_anchor"):
    check(f"__init__ 源码里已无 {_old}", _old in _src, False)

# ══════════════════════════════════════════════════════════════════════
sec("【QA-8】纯函数层：home_should_return / 覆盖模拟的边界")
# ══════════════════════════════════════════════════════════════════════
check("tol=2、d_home=3、d_main=5 → 相等（3<3 不成立）→ 主线优先",
      home_should_return(3, 5, 2), False)
check("tol=2、d_home=2、d_main=5 → 回正", home_should_return(2, 5, 2), True)
check("tol 传 None/非法 → 回落 2", home_should_return(0, 5, None), True)
check("tol 负数 → 回落 2", home_should_return(0, 1, -5), False)
check("d_home=0、d_main=0（站在终点上）→ 主线优先（自动退出）",
      home_should_return(0, 0, 2), False)
check("模拟覆盖：空回正线 → 全部漏掉",
      simulate_coverage([], _main_pts, [(99, PIT_Y)])["missed"], [(99, PIT_Y)])
check("模拟覆盖：空主线 → 全部漏掉（量不出 d_main 就不进）",
      simulate_coverage(_home_pts, [], [(99, PIT_Y)])["covered"], 0)
check("模拟覆盖：落点区间为空 → 0/0",
      simulate_coverage(_home_pts, _main_pts, [])["total"], 0)

print("\n" + "=" * 60)
if NOTE:
    print(f"⚠️ 需要人工看一眼的核对项 {len(NOTE)} 条：")
    for n in NOTE:
        print(f"   - {n}")
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
