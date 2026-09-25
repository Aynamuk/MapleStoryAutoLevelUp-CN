# -*- coding: utf-8 -*-
'''
独立验收（v0.9 第二版「谁近走谁」）—— QA 严过关，2026-09-13

⚠️ 本文件**不复用** tools/verify_home_route_qa.py 的任何用例（那是上一版的）。
   它独立复现团队负责人点名的三个硬指标 + 五个高风险点 + 工程师没测的边界。

被测对象：真实引擎 MapleStoryAutoBot 的**真实方法**（不另写假逻辑），
         只把「像素搜索」那一层短路成"读脚下像素"，保证
         「回正线接不上 → 退出」这条出口仍然走得到。

时间：用**假时钟**替换引擎模块里的 time.time()，才能离线把 8s / 10s / 30s
      这类"秒级"出口在毫秒内跑完（否则一个用例要真等 30s）。

用法：
    python -m tools.verify_home_route_qa2
'''
import copy
import math
import sys
import types

import numpy as np
import cv2
import time as _rt

import src.engine.MapleStoryAutoLevelUp as eng
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as Bot
from src.utils.home_route import (
    DEFAULT_GOAL_RGB,
    LEGACY_ANCHOR_RGB,
    MSG_COVER,
    MSG_GOAL_FAR,
    MSG_NO_MOVE,
    collect_pixels,
    far_segment_stats,
    home_params,
    home_should_return,
    paint_converging_hseg,
    split_components,
    validate_home_line,
    wipe_legacy_anchor,
)

FAIL = []
NOTE = []


def _same(a, b):
    '''ndarray 安全的相等比较。

    ⚠️ 为什么需要它：`img_route_home` 是整张图的 ndarray，
       `ndarray == None` 得到的是**逐元素布尔数组**，`if ok:` 会直接抛
       "The truth value of an array with more than one element is ambiguous"
       —— 脚本当场崩掉，后面的断言一条都跑不到（2026-09-13 实测）。
    '''
    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        return np.array_equal(a, b)
    return a == b


def check(name, got, want):
    ok = _same(got, want)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def soft(name, got, want):
    ok = got == want
    print(f"  [{'核对' if ok else '★核对'}] {name}: got={got!r} want={want!r}")
    if not ok:
        NOTE.append(name)


def sec(name):
    print(f"\n{name}")


# ══════════════════════════════════════════════════════════════════════
# 假时钟（替换引擎模块的 time，让秒级出口离线可跑）
# ══════════════════════════════════════════════════════════════════════
class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def time(self):
        return self.t

    def __getattr__(self, name):
        return getattr(_rt, name)


FC = FakeClock()
_ORIG_TIME = eng.time
eng.time = FC


# ══════════════════════════════════════════════════════════════════════
# 地形常量（真实数据）
# ══════════════════════════════════════════════════════════════════════
MAIN_Y = 122
MAIN_X0, MAIN_X1 = 92, 106
PIT_Y = 128
PIT_X0, PIT_X1 = 94, 108
CORNER_X = 99
SHAPE = (187, 268)

GOAL = DEFAULT_GOAL_RGB          # 黄
JUMP = (255, 0, 255)             # 洋红（原地跳）
BLUE = (0, 0, 255)               # 蓝（right）
RED = (255, 0, 0)                # 红（left）
CC = {RED: "left none none", BLUE: "right none none",
      JUMP: "none none jump", GOAL: "none none goal"}

HOME_PARAMS = {
    "home_dist_tol": 2, "home_min_cover_span": 5, "home_cover_far_tol": 2,
    "home_goal_mainline_tol": 2, "home_min_return_span": 5,
    "home_min_leave": 0, "home_min_frames": 0, "home_timeout": 8,
    "home_reenter_lock": 10, "home_rearm_margin": 4, "home_rearm_timeout": 30,
    # ── 起跳前先停稳（2026-09-15 补，**测试配置保真**）──────────────────────
    # ⚠️ 之前这里**没有**这一项 → 引擎 `_apply_jump_brake` 读不到就用默认 **True**
    #    → 这条**真机上已经关掉**的功能在测试里是开着的：
    #    角色走到起跳点那一帧因为"还在移动"被清成 `none none none`，
    #    而本脚本的 walk() 遇到全 none 就 break → 永远等不到"等 3 帧后照跳"那一帧
    #    → 假失败：「起点 (94,128)/(108,128)：走回主线并退出」。
    #    实测（2026-09-15）：补上本项后三个起点**全部通过**
    #    （(94,128) 走 9 步、(108,128) 走 13 步，都在 (99,128) 起跳 → (99,122) → 退出回正）。
    #    真实配置 config_default.yaml 里 home_jump_brake = false（实测合并后也是 False），
    #    所以测试必须跟着关 —— 否则测的是"真机上不存在的行为"。
    "home_jump_brake": False, "home_jump_brake_frames": 3,
}
BASE_CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9},
    "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
    "monster_detect": {"search_box_margin": 50, "match_all_mobs": True},
    "key": {"teleport": ""},
    "route": dict({"search_range": 10, "rescue_range": 40,
                   "pingpong_single_route": False,
                   "color_code": {"255,0,0": "left none none",
                                  "0,0,255": "right none none",
                                  "255,0,255": "none none jump",
                                  "255,255,0": "none none goal"}}, **HOME_PARAMS),
    "watchdog": {"range": 10, "timeout": 10, "attack_grace": 5},
}


class Stub:
    '''最小宿主（方法全挂引擎真实实现）。'''


def _get_nearest_color_code(self):
    '''忠实复刻真实引擎的 get_nearest_color_code：在角色周围 **search_range** 半径内
    找最近的路线色码；找不到再用 **rescue_range** 兜。

    ⚠️ 上一版 QA 的桩只读"脚下那一个像素"，那会让 y=129（坑底低 1px）这种
       "像素差 1px"的情形被误判成"接不上"—— 与真实引擎行为不符。
       这里按引擎真实逻辑搜索，才测得出「坑底 y=128/129 都接得住」。
    '''
    img = self.img_route
    if img is None or not self.loc_player_global:
        return None, None
    h, w = img.shape[:2]
    px, py = int(self.loc_player_global[0]), int(self.loc_player_global[1])

    def _scan(radius):
        n = nd = None
        md = mud = float("inf")
        for y in range(max(0, py - radius), min(h, py + radius)):
            for x in range(max(0, px - radius), min(w, px + radius)):
                rgb = tuple(int(v) for v in img[y, x][:3])
                d = abs(x - px) + abs(y - py)
                c1 = self.color_code.get(rgb)
                if c1 and d < md:
                    n = {"command": c1, "distance": d, "pixel": (x, y), "color": rgb}
                    md = d
                c2 = self.color_code_up_down.get(rgb)
                if c2 and d < mud:
                    nd = {"command": c2, "distance": d, "pixel": (x, y), "color": rgb}
                    mud = d
        return n, nd

    sr = int(self.cfg["route"]["search_range"])
    n, nd = _scan(sr)
    if n is None and nd is None:
        rr = int(self.cfg["route"].get("rescue_range", 40))
        if rr > sr:
            n, nd = _scan(rr)
    return n, nd


def _get_monsters_in_range(self, _tl, _br):
    return []


def _get_nearest_monster(self, is_left=True):
    return None


# ── 自动挂**全部**引擎真实方法（同 verify_home_route_qa.py，2026-09-15 改）──
# 原来是手写白名单，2026-09-14 引擎加 `_perf` 等 4 个方法后没人来补 →
# 本脚本从那天起静默跑不了（`_perf(...)` 一调就 AttributeError）。
Stub.get_monsters_in_range = _get_monsters_in_range
Stub.get_nearest_monster = _get_nearest_monster
Stub.get_nearest_color_code = _get_nearest_color_code
for _n in dir(Bot):
    if _n.startswith("__") or hasattr(Stub, _n):
        continue
    _attr = getattr(Bot, _n)
    if callable(_attr):
        setattr(Stub, _n, _attr)


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
    b.color_code = dict(CC)
    b.color_code_up_down = {}
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


def make_mainline(pts=None, shape=SHAPE):
    if pts is None:
        pts = [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)]
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    for (x, y) in pts:
        img[y, x] = BLUE
    return img


def make_l_home(corner_x=CORNER_X, pit_y=PIT_Y, main_y=MAIN_Y,
                x0=PIT_X0, x1=PIT_X1, shape=SHAPE):
    '''标准 L 形：横段逐像素汇聚着色 + 竖段 jump + goal 贴主线。'''
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, pit_y) for x in range(x0, x1 + 1)],
                          (corner_x, pit_y), CC, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(main_y + 1, pit_y + 1):
        img[y, corner_x] = JUMP
    img[main_y, corner_x] = GOAL
    return img


def new_home_bot(player_global, mainline_img=None, home_img=None, cfg=None):
    b = new_bot(player_global=player_global, cfg=cfg)
    b.color_code = dict(CC)
    b.color_code_up_down = {}
    b.img_routes = [mainline_img if mainline_img is not None else make_mainline()]
    b.img_route_home = home_img if home_img is not None else make_l_home()
    b.img_route = b.img_routes[0]
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("QA2", b.img_route_home, b.img_routes)
    return b


def _step(pos, cmd):
    mx, my, act = cmd.split()
    x, y = pos
    if mx == "left":
        x -= 1
    elif mx == "right":
        x += 1
    if act == "jump":
        y -= 6
    return (x, y)


# ══════════════════════════════════════════════════════════════════════
sec("【A】硬指标①：8s 超时不会退化成「每帧重置」—— 死锁有界")
# 场景：角色掉进坑底后**一直动不了**（回正线接不上），看会不会「超时→下一帧立刻重进」死循环。
# 判据：最大**连续**处于回正态的帧数 ≤ ceil(home_timeout × fps) + 1。
# ══════════════════════════════════════════════════════════════════════
FC.t = 1_000_000.0
FPS = 30
b = new_home_bot((PIT_X0, PIT_Y))
max_consec = cur = entered = 0
lock_seen = False
N = int(120 * FPS)
for _i in range(N):
    b.update_cmd_by_route()
    if b.is_using_home_route:
        cur += 1
        entered += 1
        max_consec = max(max_consec, cur)
    else:
        cur = 0
    if b.home_reenter_lock_t:
        lock_seen = True
    FC.t += 1.0 / FPS
bound = int(math.ceil(8 * FPS)) + 1
check("场景成立：出现过进入回正", entered > 0, True)
check("不是永远卡着：出现过退出回正", (N - entered) > 0, True)
check("超时退出后确实上了冷静期（D1）", lock_seen, True)
print(f"       最大连续回正帧数 = {max_consec}（上限 {bound}）")
check(f"★ 最大连续回正 ≤ ceil(8s×{FPS}fps)+1 = {bound}", max_consec <= bound, True)
# 至少完成过一整轮「进→超时退→冷静期→（线还没回来）」而不是每帧重进
check("★ 至少完成过 1 次超时退出（进/出都发生过）", (entered > 0 and entered < N), True)


# ══════════════════════════════════════════════════════════════════════
sec("【B】硬指标①的补刀：新闸「无已武装线 → 老兜底也不许进」有没有绕过")
# 工程师新加的闸：老兜底（主线像素找不到）进回正前，必须先拿到一条**已武装**的线。
# 这里正面验它 + 反面试"能不能绕过"。
# ══════════════════════════════════════════════════════════════════════
FC.t = 2_000_000.0
# 让「去武装后不再自动重新武装」稳定成立：把重新武装门槛设成极大。
# 场景 = 走丢了：角色离主线、回正线都 >40px（rescue_range）→ 才会走到老兜底分支。
_cfgB = copy.deepcopy(BASE_CFG)
_cfgB["route"]["home_rearm_margin"] = 9999
_cfgB["route"]["home_rearm_timeout"] = 9999
b = new_home_bot((200, 180), cfg=_cfgB)
ln = b.home_lines[0]
ln["armed"] = False
ln["disarm_t"] = FC.t
check("走丢 + 该线已去武装 → _home_dist 返回 None", b._home_dist(), (None, None))
b.update_cmd_by_route()
check("★ 老兜底（>40px 找不到任何路线像素）也不许进回正（那道闸生效）",
      b.is_using_home_route, False)
check("★ 该线没被悄悄重新武装（闸没漏）", ln["armed"], False)
# 反证：把线重新武装 → 闸的判据（_home_dist 返回线）不再成立，说明闸是"活的"
ln["armed"] = True
_d, _ln = b._home_dist()
check("对照：线已武装 → 闸条件不再成立（_home_dist 返回该线）", _ln is ln, True)

# 绕过尝试：缓存里握着「已去武装」的线 —— 靠 _home_line_pick 的重算兜住
b2 = new_home_bot((PIT_X0, PIT_Y))
ln2 = b2.home_lines[0]
ln2["armed"] = False
ln2["disarm_t"] = FC.t
b2.home_d_home = 0
b2.home_d_main = 6
b2._home_line_near = ln2                # 人为造一个"过期缓存"
check("★ 绕过尝试：缓存说最近线是它，但它未武装 → 重算后拒绝", b2._home_line_pick(), None)

# 合法恢复路径：离开 bbox ≥ home_rearm_margin(4) → 重新武装 → 可以再接人
b3 = new_home_bot((PIT_X0, PIT_Y))
ln3 = b3.home_lines[0]
ln3["armed"] = False
ln3["disarm_t"] = FC.t
b3.loc_player_global = (PIT_X1 + 4, PIT_Y)     # x=112，离 bbox 右缘 4px
_d, _line = b3._home_dist()
check("离开 bbox ≥4px → 重新武装（唯一合法恢复路径）", _line is ln3, True)
# 模拟插入点①：两距离都重算一遍，再看新入口是否放行
b3.home_d_main, b3._main_seg_i = b3._main_dist()
b3.home_d_home, b3._home_line_near = b3._home_dist()
check("重新武装后新入口放行（d_home=4 < d_main=12-tol）", b3._home_line_pick() is ln3, True)


# ══════════════════════════════════════════════════════════════════════
sec("【C】硬指标②：坑底每个位置都进 / 主线每个位置都不进")
# ══════════════════════════════════════════════════════════════════════
bad_pit = []
for y in (PIT_Y, PIT_Y + 1):
    for x in range(PIT_X0, PIT_X1 + 1):
        bb = new_home_bot((x, y))
        if not bb.home_lines:
            bad_pit.append((x, y, "没有可用回正线"))
            continue
        bb.update_cmd_by_route()
        if not bb.is_using_home_route:
            bad_pit.append((x, y))
check("坑底 x94..108 × y128/129 共 30 点**全部**进回正", bad_pit, [])

bad_main = []
for x in range(MAIN_X0, MAIN_X1 + 1):
    bb = new_home_bot((x, MAIN_Y))
    bb.update_cmd_by_route()
    if bb.is_using_home_route:
        bad_main.append((x, MAIN_Y))
check("主线 x92..106 共 15 点**全部不**进回正（不误触发）", bad_main, [])


# ══════════════════════════════════════════════════════════════════════
sec("【D】硬指标③：交汇点（终点在主线上）→ 主线优先、退出，不卡终点")
# ══════════════════════════════════════════════════════════════════════
FC.t = 3_000_000.0
b = new_home_bot((CORNER_X, MAIN_Y))
b.update_cmd_by_route()
check("站在交汇点（d_home=d_main=0）→ 不进回正", b.is_using_home_route, False)

b = new_home_bot((CORNER_X, PIT_Y))
b.update_cmd_by_route()
check("先正常进入回正", b.is_using_home_route, True)
b.loc_player_global = (CORNER_X, MAIN_Y)      # 走到终点（在主线上）
b.update_cmd_by_route()
b.update_cmd_by_route()   # 第二帧：退出回正要求「连续两帧 y 相同」
check("★ 走到交汇点 → 自动退出回正（不卡终点）", b.is_using_home_route, False)
check("★ 成功退出不留冷静期", b.home_reenter_lock_t, 0.0)
check("★ 退出后该线仍已武装", b.home_lines[0]["armed"], True)
check("★ 退出后导航图交回主线", b.img_route is b.img_routes[0], True)


# ══════════════════════════════════════════════════════════════════════
sec("【E】高风险①：横段汇聚编码 —— 左端 / 拐角正上方 / 右端都能到拐角并跳上去")
# ══════════════════════════════════════════════════════════════════════
_l = make_l_home()
check("横段左半 = 蓝（right，朝拐角）", tuple(_l[PIT_Y, 94][:3]), BLUE)
check("横段右半 = 红（left，朝拐角）", tuple(_l[PIT_Y, 108][:3]), RED)
check("拐角列 = 洋红（jump）", tuple(_l[PIT_Y, CORNER_X][:3]), JUMP)

FC.t = 4_000_000.0


def walk(start, maxf=80):
    bb = new_home_bot(start)
    entered = False
    trail = [start]
    for _ in range(maxf):
        bb.update_cmd_by_route()
        if bb.is_using_home_route:
            entered = True
        cmd = f"{bb.cmd_move_x} {bb.cmd_move_y} {bb.cmd_action}"
        if cmd.strip() == "none none none":
            break
        bb.loc_player_global = _step(bb.loc_player_global, cmd)
        trail.append(bb.loc_player_global)
        if entered and not bb.is_using_home_route:
            break
    return entered, bb.is_using_home_route, bb.loc_player_global, trail


for _start in ((PIT_X0, PIT_Y), (CORNER_X, PIT_Y), (PIT_X1, PIT_Y)):
    _entered, _still, _end, _trail = walk(_start)
    check(f"起点 {_start}：进过回正", _entered, True)
    check(f"起点 {_start}：走回主线并退出（终点 y={MAIN_Y}）",
          (_still, _end[1]), (False, MAIN_Y))
    # 不许朝反方向跑：左端出发 x 不减小，右端出发 x 不增大
    _xs = [p[0] for p in _trail]
    if _start[0] == PIT_X0:
        check(f"起点 {_start}：不会往左跑出坑", min(_xs) >= PIT_X0, True)
    if _start[0] == PIT_X1:
        check(f"起点 {_start}：不会往右跑出坑", max(_xs) <= PIT_X1, True)


# ══════════════════════════════════════════════════════════════════════
sec("【F】高风险②：去武装只认「线」，_home_dist 内部过滤（不内部过滤就会误进坏线）")
# ══════════════════════════════════════════════════════════════════════
FC.t = 5_000_000.0
_img = make_l_home()
_l2 = make_l_home(corner_x=205, pit_y=128, main_y=122, x0=200, x1=214)
_home2 = _img.copy()
_m2 = np.any(_l2 != (0, 0, 0), axis=-1)
_home2[_m2] = _l2[_m2]
b = new_bot()
b.color_code = dict(CC)
b.img_routes = [make_mainline(),
                make_mainline([(x, MAIN_Y) for x in range(196, 219)])]
b._build_mainline_cache()
b.home_lines = b._scan_home_lines("QA2-F", _home2, b.img_routes)
check("两条线都加载", len(b.home_lines), 2)
_near = min(b.home_lines, key=lambda L: L["goal"][0])
_far = max(b.home_lines, key=lambda L: L["goal"][0])
b.loc_player_global = (PIT_X0, PIT_Y)
_d, _line = b._home_dist()
check("坑底最近的是近线", _line is _near, True)
_near["armed"] = False
_near["disarm_t"] = FC.t
_d, _line = b._home_dist()
check("★ 近线去武装后 _home_dist **不再返回它**（内部过滤）", _line is not _near, True)
check("★ 返回的是另一条已武装的线", _line is _far, True)
check("★ 若不过滤（d_home 会=0）→ 判定会误进坏线；过滤后 d_home 变大",
      home_should_return(0, 6, 2), True)
check("过滤后 d_home 已远大于主线 → 不进坏线",
      home_should_return(_d, 6, 2), False)


# ══════════════════════════════════════════════════════════════════════
sec("【G】高风险③：判废⑤（终点必须贴主线）是**判废**，而且它真的挡着一个 8s 卡死")
# ══════════════════════════════════════════════════════════════════════
_bad = np.zeros(SHAPE + (3,), dtype=np.uint8)
for x in range(PIT_X0, PIT_X1 + 1):
    _bad[PIT_Y, x] = BLUE
for y in range(123, PIT_Y + 1):
    _bad[y, CORNER_X] = JUMP
_bad[127, CORNER_X] = GOAL                 # 终点离主线 |127-122| = 5px
_comp = split_components(_bad, set(CC))[0]
_ok, _why = validate_home_line(_comp, _bad, [make_mainline()], set(CC), BASE_CFG)
check("终点离主线 5px → 判废（不是警告）", _ok, False)
check("报的是「终点没画在主线上」", _why.startswith(MSG_GOAL_FAR.split("（")[0]), True)
print(f"       人话：{_why[:60]}…")

# 后果演示：**手工注入**一条这样的线（绕过判废），角色站到终点上会怎样
FC.t = 6_000_000.0
_lineA = {"id": 1, "start": (PIT_X1, PIT_Y), "goal": (CORNER_X, 127), "span": 10,
          "has_jump": True, "n_pts": len(_comp),
          "xs": np.asarray([p[0] for p in _comp], dtype=np.int32),
          "ys": np.asarray([p[1] for p in _comp], dtype=np.int32),
          "armed": True, "disarm_t": 0.0, "bbox": (PIT_X0, 122, PIT_X1, PIT_Y)}
b = new_bot()
b.color_code = dict(CC)
b.img_routes = [make_mainline()]
b._build_mainline_cache()
b.img_route_home = _bad
b.home_lines = [_lineA]
b.img_route = _bad
b.loc_player_global = (CORNER_X, 127)      # 正好站在终点上
b.is_using_home_route = True
b.home_line = _lineA
b.home_enter_pos = (CORNER_X, PIT_Y)
b.home_enter_t = FC.t
b.home_frame = 0
b.update_cmd_by_route()
check("★ 站在「离主线5px」的终点上 → 仍判回正更近 → 卡住（只能干等 8s 超时）",
      b.is_using_home_route, True)

# 对照：终点贴主线时，同一逻辑立刻退出
_imgB = _bad.copy()
_imgB[127, CORNER_X] = (0, 0, 0)
_imgB[MAIN_Y, CORNER_X] = GOAL
_lineB = dict(_lineA)
_lineB["goal"] = (CORNER_X, MAIN_Y)
b2 = new_bot()
b2.color_code = dict(CC)
b2.img_routes = [make_mainline()]
b2._build_mainline_cache()
b2.img_route_home = _imgB
b2.home_lines = [_lineB]
b2.img_route = _imgB
b2.loc_player_global = (CORNER_X, MAIN_Y)
b2.is_using_home_route = True
b2.home_line = _lineB
b2.home_enter_pos = (CORNER_X, PIT_Y)
b2.home_enter_t = FC.t
b2.home_frame = 0
b2.update_cmd_by_route()
b2.update_cmd_by_route()   # 第二帧：退出回正要求「连续两帧 y 相同」
check("对照：终点贴主线 → 站上去立刻退出（不卡）", b2.is_using_home_route, False)


# ══════════════════════════════════════════════════════════════════════
sec("【H】高风险④：现网旧图迁移（127,0,127）")
# ══════════════════════════════════════════════════════════════════════
try:
    from src.utils.common import load_image
    _real = cv2.cvtColor(load_image("tools/test_fixtures/废都南方工地/route_home.png"),
                         cv2.COLOR_BGR2RGB)
    _legacy_n = int(np.sum(np.all(_real == np.array(LEGACY_ANCHOR_RGB), axis=-1)))
    print(f"       现网 route_home.png 里 127,0,127 像素数 = {_legacy_n}")
    # ⚠️ 与任务描述「含锚点像素」不符 —— 实测一个都没有（见报告）。这里如实记录。
    check("★ 实测：现网旧图**不含** 127,0,127（任务描述与磁盘不符）", _legacy_n, 0)
    _inj = _real.copy()
    _inj[130, 150] = LEGACY_ANCHOR_RGB
    _w = wipe_legacy_anchor(_inj)
    check("注入的旧锚点被 wipe 清掉", _w, [(150, 130)])
    check("清理后图上一个锚点像素都不剩",
          int(np.sum(np.all(_inj == np.array(LEGACY_ANCHOR_RGB), axis=-1))), 0)
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")


# ══════════════════════════════════════════════════════════════════════
sec("【I】高风险⑤：tol 边界（d_home = d_main - tol 正好相等 → 主线优先、不进）")
# ══════════════════════════════════════════════════════════════════════
FC.t = 7_000_000.0
check("纯函数：d_home=3, d_main=5（差= tol=2）→ 不进", home_should_return(3, 5, 2), False)
check("纯函数：d_home=2, d_main=5（差= tol+1）→ 进", home_should_return(2, 5, 2), True)
b = new_bot()
b.color_code = dict(CC)
b.img_routes = [make_mainline()]
b._build_mainline_cache()
_line = {"id": 1, "start": (CORNER_X, 124), "goal": (CORNER_X, 122), "span": 2,
         "has_jump": False, "n_pts": 1,
         "xs": np.asarray([CORNER_X], dtype=np.int32),
         "ys": np.asarray([124], dtype=np.int32),
         "armed": True, "disarm_t": 0.0, "bbox": (CORNER_X, 122, CORNER_X, 124)}
b.home_lines = [_line]
b.img_route_home = make_l_home()
b.img_route = b.img_route_home
b.loc_player_global = (CORNER_X, 124)          # d_home=0, d_main=2 → 差 = tol
_dh, _ln = b._home_dist()
_dm, _sg = b._main_dist()
check("几何边界：d_home=0, d_main=2（差= tol）", (_dh, _dm), (0, 2))
check("★ 几何边界 → _home_line_keep 为假（不进回正）", b._home_line_keep(_dh, _dm), False)
check("★ 几何边界 → _home_line_pick 返回 None（不进）", b._home_line_pick(), None)

# 差 tol+1（3px）→ 进
_line2 = dict(_line)
_line2["ys"] = np.asarray([125], dtype=np.int32)
b.home_lines = [_line2]
b.loc_player_global = (CORNER_X, 125)          # d_home=0, d_main=3
_dh, _dm = b._home_dist()[0], b._main_dist()[0]
check("几何：d_home=0, d_main=3（差= tol+1）", (_dh, _dm), (0, 3))
check("★ 差 tol+1 → _home_line_keep 为真（进回正）", b._home_line_keep(_dh, _dm), True)
check("★ 差 tol+1 → _home_line_pick 返回该线", b._home_line_pick() is _line2, True)


# ══════════════════════════════════════════════════════════════════════
sec("【J】边界：回正线只有 1px / 只有 goal / 只画竖线")
# ══════════════════════════════════════════════════════════════════════
_main = [make_mainline()]


def _one(pts_map, shape=SHAPE):
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    for rgb, pts in pts_map.items():
        for (x, y) in pts:
            img[y, x] = rgb
    comps = split_components(img, set(CC))
    assert len(comps) == 1, f"期望 1 个连通域，实际 {len(comps)}"
    return validate_home_line(comps[0], img, _main, set(CC), BASE_CFG), comps[0], img


(ok1, why1), _, _ = _one({GOAL: [(CORNER_X, MAIN_Y)]})
check("只有 1 个 goal 像素（无动作）→ 判废", (ok1, why1), (False, MSG_NO_MOVE))

(ok2, why2), _c2, _ = _one({JUMP: [(CORNER_X, y) for y in range(123, 129)],
                            GOAL: [(CORNER_X, MAIN_Y)]})
check("只画竖线（1px 宽）→ 判废", ok2, False)
check("报的是「只接得住一个点」", why2, MSG_COVER.format(x=CORNER_X))
check("竖线的起始段覆盖跨度 = 1",
      far_segment_stats(_c2, _main, set(CC), BASE_CFG)["cover_span"], 1)


# ══════════════════════════════════════════════════════════════════════
sec("【K】边界：一张图 2 条线，其中 1 条废 → 只废那 1 条（不连坐）")
# ══════════════════════════════════════════════════════════════════════
_img3 = make_l_home()
_lbad = make_l_home(corner_x=205, pit_y=128, main_y=122, x0=200, x1=214)
_mb = np.any(_lbad != (0, 0, 0), axis=-1)
_img3[_mb] = _lbad[_mb]
_img3[MAIN_Y, 205] = (0, 0, 0)          # 把右边那条的终点挪离主线
_img3[127, 205] = GOAL
b = new_bot()
b.color_code = dict(CC)
b.img_routes = [make_mainline(),
                make_mainline([(x, MAIN_Y) for x in range(196, 219)])]
b._build_mainline_cache()
_lines = b._scan_home_lines("QA2-K", _img3, b.img_routes)
check("2 条里只留 1 条（合法的那条）", len(_lines), 1)
check("留下的是左边（终点贴主线 (99,122)）", _lines[0]["goal"], (CORNER_X, MAIN_Y))
check("废线像素被就地抹掉（右边 205 列附近）",
      len(collect_pixels(_img3[120:135, 195:220], set(CC))), 0)


# ══════════════════════════════════════════════════════════════════════
sec("【L】边界：home_timeout / home_reenter_lock / home_rearm_timeout 配 0 → 引擎层仍回落默认")
# ══════════════════════════════════════════════════════════════════════
FC.t = 8_000_000.0
_cfg0 = copy.deepcopy(BASE_CFG)
_cfg0["route"].update({"home_timeout": 0, "home_reenter_lock": 0, "home_rearm_timeout": 0})
check("home_params 把 0 全部回落默认",
      (home_params(_cfg0)["home_timeout"], home_params(_cfg0)["home_reenter_lock"],
       home_params(_cfg0)["home_rearm_timeout"]), (8, 10, 30))

b = new_home_bot((PIT_X0, PIT_Y), cfg=_cfg0)
b.update_cmd_by_route()
check("配 0 也照样进回正", b.is_using_home_route, True)
b.home_enter_t = FC.t - 9               # 已超 9s > 回落后的 8s
b.update_cmd_by_route()
check("★ home_timeout=0 仍回落 8 → 超时出口生效（没被关掉）", b.is_using_home_route, False)
check("★ home_reenter_lock=0 仍回落 10 → 已上锁", b.home_reenter_lock_t > 0, True)
check("★ 冷静期内新入口被挡", b._home_line_pick(), None)
FC.t += 11                              # 冷静期到期
check("冷静期到期 → 解锁", b._home_reenter_allowed(), True)
_d, _ln = b._home_dist()
check("★ home_rearm_timeout=0 仍回落 30 → 11s 时坏线还没武装", _ln, None)
FC.t += 20                              # 共 31s
_d, _ln = b._home_dist()
check("★ 31s 后坏线无条件重新武装（不会永远不回正）",
      _ln is b.home_lines[0], True)


# ══════════════════════════════════════════════════════════════════════
sec("【M】边界：中文路径整轮 load_config（废都南方工地）")
# ══════════════════════════════════════════════════════════════════════
try:
    from src.utils.common import load_yaml

    def _merge(base, custom):
        for k, v in custom.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v)
            else:
                base[k] = v
        return base

    _args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                  debug=False, disable_viz=True)
    fb = Bot.__new__(Bot)
    fb.args = _args
    fb.cfg = None
    fb.data = load_yaml("config/config_data.yaml")
    fb.monsters_info = {}
    fb.img_routes = []
    fb.img_route_home = None
    fb.img_map = None
    fb.img_route = None
    fb.color_code = {}
    fb.color_code_up_down = {}
    fb.is_show_debug_window = False
    fb.img_route_debug = None
    fb.idx_routes = 0
    fb.is_using_home_route = False
    fb.is_terminated = False
    fb.kb = None
    fb.capture = None
    # 活动配置方案：默认用 config_custom.yaml（出厂回退值）。
    _cfg_real = _merge(load_yaml("config/config_default.yaml"),
                       load_yaml("config/config_custom.yaml"))
    _ret = fb.load_config(_cfg_real)
    check("中文路径 load_config 返回 0（不抛异常）", _ret, 0)
    check("中文路径下主线加载到（≥1 段）", len(fb.img_routes) >= 1, True)
    # ── 回正线：**状态无关**的写法 ────────────────────────────────────
    # ⚠️ 原断言写死「现网旧回正线全判废 → img_route_home 为 None / home_lines 为空」。
    #    那条线 2026-09-13 被重画成了**合法** L 形（34 px / 1 条可用），于是：
    #      ① 断言必然红（把"资源当时的临时状态"当成了不变量）；
    #      ② 更糟的是 check 里 `ndarray == None` 抛 "ambiguous truth value"，
    #         脚本**当场崩掉**，后面的收尾一条都跑不到（2026-09-13 实测 exit=1）。
    #    用户随时会重画这条线（废→合法、合法→废都正常），所以只断言**自洽性**。
    _codes = set(fb.color_code) | set(fb.color_code_up_down)
    _n_lines = len(fb.home_lines or [])
    check("★ 状态自洽：home_lines 非空 ⇔ img_route_home 不为 None"
          "（废线必须当成没有回正线）",
          fb.img_route_home is not None, _n_lines > 0)
    _left = (split_components(fb.img_route_home, _codes)
             if fb.img_route_home is not None else [])
    print(f"       现网 route_home.png → 可用回正线 {_n_lines} 条"
          f"（扫描后图上剩 {len(_left)} 个连通域）")

    # ── 测完 load_config 之后，把数据换成**仓库里冻结的夹具** ────────────────
    # ⚠️ 2026-09-16 加：下面几段（坑底落点覆盖 / 归位闭环）**复用**上面这个 fb，
    #    而 fb 来自**真实 load_config** —— 它读的是"当前活动地图"的**活文件**。
    #    于是用户一重录、或那张图的 route_home.png 丢了（2026-09-16 真实发生：
    #    重录主线把回正线删了），下面这几条立刻变红 —— **红的是数据，不是代码**。
    #    本节要验的是「中文路径下整轮 load_config 跑得通」，不是「那张图此刻长什么样」，
    #    所以验完 load_config 就把数据换成冻结夹具，跟现网文件怎么改都无关
    #    （同上面 waste_home_image 的道理：断言要的是规则成立）。
    #    ⚠️ 位置很关键：必须在上面几条 load_config 断言**之后**换，
    #       否则等于没验真实加载链。
    from src.utils.common import load_image, mask_route_colors
    _fx = "tools/test_fixtures/废都南方工地"
    # ⚠️ mask_route_colors 要的是**字符串键**的原始色码表（'255,0,0'），
    #    而 load_config 之后 fb.color_code 的键已经转成元组了 ——
    #    直接传它会在 color_str.split(',') 上 AttributeError（踩过）。
    _cc_raw = _cfg_real["route"]["color_code"]
    _cc_raw_ud = _cfg_real["route"]["color_code_up_down"]
    fb.img_map = load_image(f"{_fx}/map.png")
    _fx_routes = []
    for _n in ("route1.png", "route2.png"):
        _rr = cv2.cvtColor(load_image(f"{_fx}/{_n}"), cv2.COLOR_BGR2RGB)
        _rr = mask_route_colors(fb.img_map, _rr, _cc_raw)
        _rr = mask_route_colors(fb.img_map, _rr, _cc_raw_ud)
        _fx_routes.append(_rr)
    fb.img_routes = _fx_routes
    fb.img_route_home = cv2.cvtColor(load_image(f"{_fx}/route_home.png"),
                                     cv2.COLOR_BGR2RGB)
    fb.home_lines = fb._scan_home_lines("夹具", fb.img_route_home, fb.img_routes)
    fb._build_mainline_cache()
    # ⚠️★ 2026-09-17 修：`_left` / `_n_lines` 必须在**换成夹具之后**重算。
    #    原来它们在 758 行就算好了 —— 那时 `fb` 里还是**现网活文件**，
    #    而下面 796/799 却拿它们去跟**夹具图**比对/复验 → 坐标系不是同一张图。
    #    以前一直没红，是因为**现网那张图当时没有可用回正线**：
    #    `_left` 是空列表，`all(空)` 恒为 True（假通过）。
    #    2026-09-17 用户补了一条真的回正线，`_left` 非空 → 立刻暴露。
    _n_lines = len(fb.home_lines or [])
    _left = split_components(fb.img_route_home, _codes)
    print(f"       ↓ 下面改用冻结夹具（tools/test_fixtures/）："
          f"主线 {len(fb.img_routes)} 段 / 回正线 {_n_lines} 条"
          f"（扫描后图上剩 {len(_left)} 个连通域）"
          f" → 与现网文件怎么改都无关")
    check("★ 扫出来的条数 == 扫描后剩下的连通域数（判废不漏、也不连坐）",
          _n_lines, len(_left))
    check("★ 留下的每条都是合法回正线（终点贴主线 / 覆盖跨度够）",
          all(validate_home_line(c, fb.img_route_home, fb.img_routes,
                                 _codes, fb.cfg)[0] for c in _left), True)

    # ══════════════════════════════════════════════════════════════════
    sec("【N】★★ 落点覆盖：坑底**每个**落点都必须能进回正（调**引擎真实判定**）")
    # ══════════════════════════════════════════════════════════════════
    # 为什么要这条（2026-09-15 真机实测踩出来的）：
    #   用户从平台**左边缘**掉下去后，角色**原地不动、也不报卡死**，永远出不来。
    #   量出来是这样：回正线横段只铺到 **x=94~108**，而平台是 **x=92~106**；
    #   更关键的是判定式 `d_home < d_main - tol` 在**平台两端**算不成立 ——
    #   主线在 x=94 / x=104 有斜坡像素落在 **y=124**，离坑底 y=126 只有 **2px**
    #   → `d_home(0) < d_main(2) - 2` 即 `0 < 0` → 判"你在主线上" → **不进回正**。
    #   而"最可能掉下去的位置"恰恰就是这两个边缘 ⇒ 掉下去就再也回不来。
    #   根因：判定式没错，是**坑底离主线比设计假设的近**（设计按 6px 验算，
    #   实测只有 4px、两端更只有 2px，tol=2 的预算被吃光）。
    #
    # ⚠️★ 必须调 `_home_line_pick()`（引擎真实入口），**不许自己复刻判定式** ——
    #    本条用例第一版就是把 `d_home < d_main - tol` 抄了一遍，于是引擎补了硬闸
    #    之后它**照样红**（测的是旧公式，不是引擎）。复刻 = 测了另一份代码。
    _eb = new_bot()
    _eb.cfg = fb.cfg
    _eb.color_code = dict(fb.color_code)
    _eb.color_code_up_down = dict(fb.color_code_up_down)
    _eb.img_routes = list(fb.img_routes)
    _eb.img_route_home = fb.img_route_home
    _eb.img_route = fb.img_route_home
    _eb.home_lines = list(fb.home_lines)
    _eb.home_reenter_lock_t = 0.0
    _eb.t_last_home_lock_log = 0.0
    _eb.is_using_home_route = False
    _eb._build_mainline_cache()

    _scan_ys = (126, 127, 128)       # 真机实测坑底落在 y=126
    _scan_xs = list(range(84, 117))  # 平台两端各留 8px 余量
    _bad2 = []
    for _yy in _scan_ys:
        for _xx in _scan_xs:
            _eb.loc_player_global = (_xx, _yy)
            _eb.home_d_main = None    # 强制重算，别拿上一个落点的缓存
            _eb.home_d_home = None
            _eb._home_line_near = None
            _eb.is_using_home_route = False
            if _eb._home_line_pick() is None:
                _eb.loc_player_global = (_xx, _yy)
                _dh = _eb._home_dist()[0]
                _dm = _eb._main_dist()[0]
                _bad2.append((_xx, _yy, _dh, _dm))
    print(f"       坑底 y={list(_scan_ys)} × x={_scan_xs[0]}~{_scan_xs[-1]}"
          f"（{len(_scan_ys) * len(_scan_xs)} 个落点）里，进不了回正的有 {len(_bad2)} 个")
    if _bad2:
        print(f"       → (x, y, d_home, d_main) = {_bad2[:16]}"
              f"{' …' if len(_bad2) > 16 else ''}")
    check("★★ 坑底每个落点都能进回正（漏一个 = 那一片掉下去就永远出不来）",
          _bad2, [])

    # ── 反面：主线站立行 + 主线斜坡行**都不许**进回正（防硬闸放太松 → "抢跑"）──
    # 为什么必须配这条：进入侧那道硬闸（_home_below_mainline）放宽了"主线优先"，
    # 万一门槛写松了，角色在平台边缘踩到斜坡行就会被拽进回正 → 巡逻被打断。
    # 实测门槛：站立行 122、斜坡行 124、坑底 126 ⇒ 用 `> 124` 正好分开。
    _bad_onmain = []
    for _yy in (122, 123, 124):
        for _xx in range(88, 111):
            _eb.loc_player_global = (_xx, _yy)
            _eb.home_d_main = None
            _eb.home_d_home = None
            _eb._home_line_near = None
            _eb.is_using_home_route = False
            if _eb._home_line_pick() is not None:
                _bad_onmain.append((_xx, _yy))
    print(f"       主线上 y=122~124 × x=88~110（69 个点）里，被误拉进回正的有"
          f" {len(_bad_onmain)} 个")
    if _bad_onmain:
        print(f"       → {_bad_onmain[:16]}{' …' if len(_bad_onmain) > 16 else ''}")
    check("★★ 主线上 / 主线斜坡行都不进回正（防进入侧硬闸放太松 → 抢跑）",
          _bad_onmain, [])

    # ══════════════════════════════════════════════════════════════════
    sec("【O】★ 归位：必须「真的停住」才交还控制权（否则落回平台后会滑出去）")
    # ══════════════════════════════════════════════════════════════════
    # 真机实测（2026-09-15 19:15，27 秒循环 5 轮）：回正是**跑着跳**上来的，
    # 跳跃保留水平速度 → 落回平台后还会**滑 4~6px**。旧逻辑一到中心 ±1px 就交还
    # 控制权，人还在滑 → 从平台另一端滑出去 → 又掉进坑 → 反向回正 → **来回死循环**。
    # 安全区只有 94~104（10px），滑行量 4~6px ⇒ 交还得太早必然滑出去。
    _rng2 = _eb._edge_guard_range(122)
    _ctr2 = (int(_rng2[0]) + int(_rng2[1])) // 2 if _rng2 else None
    print(f"       安全区 x={_rng2} → 中心 {_ctr2}")
    _eb._recenter_active = True
    _eb._recenter_n = 0
    _eb._recenter_last_px = None
    _eb.loc_player_global = (_ctr2, 122)
    _r1 = _eb._apply_recenter()
    _r2 = _eb._apply_recenter()
    check("★ 归位第 1 帧：在中心但还没停稳 → 仍归位生效（继续朝中心给方向）",
          _r1, True)
    check("★ 归位第 2 帧：x 没变（停稳了）→ 交还正常巡逻", _r2, False)
    check("★ 交还后 _recenter_active 已关", _eb._recenter_active, False)

    # ── 真实滑行轨迹：落回平台后往右滑 99→104，再走回中心停住 ──────────────
    # 轨迹取自真机实测（19:15）：落点 99~100 → 滑到 104（右边缘）→ 掉下去。
    # 要验的**不是**"能不能走到中心"，而是"**滑行期间绝不许交还控制权**" ——
    # 交还了角色就没人管，一路滑出边缘（这就是用户看到的那一幕）。
    _eb._recenter_active = True
    _eb._recenter_n = 0
    _eb._recenter_last_px = None
    _trail = [99, 101, 103, 104, 102, 100, 99, 99]
    _cmds = []
    for _px in _trail:
        _eb.loc_player_global = (_px, 122)
        _eb.cmd_move_x = "none"
        _eb._apply_recenter()
        _cmds.append(_eb.cmd_move_x)
    print(f"       滑行轨迹 {_trail}")
    print(f"       每帧指令 {_cmds}")
    check("★ 滑行期间每一帧都在往回拉（left），一帧都不许交还",
          _cmds[:7], ["left"] * 7)
    # 第 8 帧（x 连续两帧相同 = 停稳）→ 不接管（指令保持 none）+ 归位结束
    check("★ 滑回中心并停稳后 → 才交还正常巡逻（不接管指令 + 归位结束）",
          (_cmds[7], _eb._recenter_active), ("none", False))
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")


# ══════════════════════════════════════════════════════════════════════
eng.time = _ORIG_TIME
print("\n" + "=" * 60)
if NOTE:
    print(f"⚠️ 需要人工看一眼的核对项 {len(NOTE)} 条：")
    for _n in NOTE:
        print(f"   - {_n}")
if FAIL:
    print(f"❌ 独立验收失败 {len(FAIL)} 项：")
    for _n in FAIL:
        print(f"   - {_n}")
    sys.exit(1)
print("✅ 独立验收全部通过")
sys.exit(0)
