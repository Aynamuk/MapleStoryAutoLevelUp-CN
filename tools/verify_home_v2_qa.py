# -*- coding: utf-8 -*-
'''
QA 独立验收（严过关）—— v0.9 回正线第二版「谁近走谁」（2026-09-13）

本文件**独立于**工程师的 verify_home_route.py / verify_home_route_qa.py /
verify_navigation.py，**不覆盖**它们，只做交叉验证：
  · 工程师说 IS_PASS 不算数 —— 这里用**自己写的**用例与自己的桩，重新验一遍。
  · 重点不是重跑工程师的用例，而是找他**没测到**的、看起来能跑、实际会翻车的地方。

四段：
  【A】新判定独立验证（坑底 15×2 / 主线 15 / 交汇点 / 相等）
  【B】死锁四道防御 D1~D4 逐条 + 241 帧独立复现（自带假时钟）
  【C】手绘器 L 形端到端（make_blob → 渲染 → 引擎加载链路 → 三个落点 / 汇聚编码 / 反例）
  【D】边界（0 条可用 / tol=0 或负 / 多条废一条不连坐 / 中文路径 / 走丢回退路径）

用法：
    python -m tools.verify_home_v2_qa
'''
from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import time as _rt      # 真实时钟：假时钟没定义的方法（如 perf_counter）透传给它

import cv2
import numpy as np

import src.engine.MapleStoryAutoLevelUp as ENG
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as Bot
from src.utils.home_route import (
    DEFAULT_HOME_PARAMS,
    collect_pixels,
    home_params,
    home_should_return,
    paint_converging_hseg,
    render_home_image,
    simulate_coverage,
)
from src.utils.common import imwrite_unicode, imread_unicode, mask_route_colors
from tools.homeRouteDrawer import make_blob

FAIL = []
NOTE = []


def check(name, got, want):
    ok = got == want
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
# 真实地形常量（废都南方工地，小地图 268×187）
# ══════════════════════════════════════════════════════════════════════
SHAPE = (187, 268)
MAIN_Y = 122
MAIN_X0, MAIN_X1 = 92, 106
PIT_Y = 128
PIT_X0, PIT_X1 = 94, 108
CORNER_X = 101                       # 本文件按任务书：拐角 x=101

BLUE = (0, 0, 255)        # right none none
RED = (255, 0, 0)         # left none none
MAGENTA = (255, 0, 255)   # none none jump
GRAY = (127, 127, 127)    # none up none
YELLOW = (255, 255, 0)    # none none goal

CC = {
    RED: "left none none",
    BLUE: "right none none",
    MAGENTA: "none none jump",
    GRAY: "none up none",
    YELLOW: "none none goal",
}

HOME_PARAMS = dict(DEFAULT_HOME_PARAMS)


def cc_str(cc):
    return {f"{int(c[0])},{int(c[1])},{int(c[2])}": v for c, v in cc.items()}


BASE_CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9},
    "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
    "monster_detect": {"search_box_margin": 50},
    "key": {"teleport": ""},
    "route": dict({"search_range": 10, "rescue_range": 40,
                   "color_code": cc_str(CC)}, **HOME_PARAMS),
    "watchdog": {"range": 10, "timeout": 10, "attack_grace": 5},
}


# ══════════════════════════════════════════════════════════════════════
# 最小宿主 + 挂引擎真实方法（自己写，不复用工程师的桩）
# ══════════════════════════════════════════════════════════════════════
class Stub:
    pass


def make_bot(player_global=(100, 100), cfg=None):
    b = Stub()
    b.cfg = cfg if cfg is not None else BASE_CFG
    b.loc_player = (500, 300)
    b.loc_player_global = player_global
    b.img_frame = np.zeros((600, 1000, 3), dtype=np.uint8)
    b.monsters = []
    b._monsters = []
    b.cmd_move_x = b.cmd_move_y = b.cmd_action = "none"
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


# ── 自动挂**全部**引擎真实方法（2026-09-15 改，对齐 verify_home_route_qa2.py）──
# ⚠️ 这里原来是**手写白名单**（28 个方法名）。引擎每加一个新方法，白名单就漏一个 →
#    一调到就 AttributeError → **整个脚本 exit=1 被截断**，而它又不打印"通过/失败"
#    总结行，看起来就像"没报错"。
#    2026-09-15 实测：只跑完【A】，**A3「坑底 30 个落点」及之后一条都没跑** ——
#    等于"落点覆盖"这件事根本没验过。缺的正是引擎 2026-09-14 之后新加的
#    `_perf` / `_apply_recenter` / `_apply_jump_brake` / `_trace_home_cmd` /
#    `_patrol_degraded_now` 这一批。
#    同仓 qa2 早就改成"自动挂全部"，这里对齐 —— 以后引擎再加方法**不用再来补名单**。
#    （verify_navigation.py 那边同一个坑踩了七次，注释原话"别再靠崩了才发现"。）
for _n in dir(Bot):
    if _n.startswith("__") or hasattr(Stub, _n):
        continue
    _attr = getattr(Bot, _n)
    if callable(_attr):
        setattr(Stub, _n, _attr)


def make_mainline(x0=MAIN_X0, x1=MAIN_X1, y=MAIN_Y):
    img = np.zeros(SHAPE + (3,), dtype=np.uint8)
    for x in range(x0, x1 + 1):
        img[y, x] = BLUE
    return img


def build_l_home(corner_x=CORNER_X, pit_y=PIT_Y, main_y=MAIN_Y,
                 x0=PIT_X0, x1=PIT_X1):
    '''L 形回正线：横段逐像素汇聚着色 + 竖段跳 + goal 贴主线。'''
    img = np.zeros(SHAPE + (3,), dtype=np.uint8)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, pit_y) for x in range(x0, x1 + 1)],
                          (corner_x, pit_y), CC, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(main_y + 1, pit_y + 1):
        img[y, corner_x] = MAGENTA
    img[main_y, corner_x] = YELLOW
    return img


def new_home_bot(player_global, mainline=None, home=None):
    b = make_bot(player_global=player_global)
    b.img_routes = [mainline if mainline is not None else make_mainline()]
    b.img_route_home = home if home is not None else build_l_home()
    b.img_route = b.img_routes[0]
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("QA独立", b.img_route_home, b.img_routes)
    return b


# ══════════════════════════════════════════════════════════════════════
# 假时钟（与工程师各自的实现，互不依赖）
# ══════════════════════════════════════════════════════════════════════
class FakeClock:
    def __init__(self, fps=30):
        self.t = 10000.0
        self.dt = 1.0 / float(fps)

    def time(self):
        return self.t

    def tick(self):
        self.t += self.dt

    def sleep(self, _s):
        self.t += self.dt

    def __getattr__(self, name):
        # ⚠️ 引擎里新增的 time.xxx 调用必须能**透传**（2026-09-15 踩坑）：
        #    引擎 update_cmd_by_route 新加了 `time.perf_counter()`（性能埋点），
        #    本假时钟没这个方法 → AttributeError 把整个脚本打断
        #    → 实测 **exit=1，只跑完【A】，A3「坑底 30 个落点」及之后一条都没跑**，
        #      表面看像"没报错"，其实等于**没验**。
        #    同仓 verify_home_route_qa2.py:90 早就有这个透传写法，这里补齐、保持一致。
        return getattr(_rt, name)


CLOCK = FakeClock(30)
ENG.time = CLOCK      # 引擎里所有 time.time() 走虚拟时钟

_l_home = build_l_home()
_main = make_mainline()
_home_pts = collect_pixels(_l_home, set(CC))
_main_pts = collect_pixels(_main, set(CC))


# ══════════════════════════════════════════════════════════════════════
sec("【A】新判定独立验证：坑底 / 主线 / 交汇点 / 相等")
# ══════════════════════════════════════════════════════════════════════
# A1 纯数学：坑底两行各 15 点必须全进
_cov128 = simulate_coverage(_home_pts, _main_pts,
                            [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)], tol=2)
_cov129 = simulate_coverage(_home_pts, _main_pts,
                            [(x, PIT_Y + 1) for x in range(PIT_X0, PIT_X1 + 1)], tol=2)
check("A1 坑底 y=128 的 15 个落点全进回正", (_cov128["total"], _cov128["covered"]), (15, 15))
check("A1 坑底 y=129（低 1px）同样 15/15", (_cov129["total"], _cov129["covered"]), (15, 15))
check("A1 坑底无漏点", _cov128["missed"] + _cov129["missed"], [])

# A2 主线 15 点一个都不许误进
_cov_main = simulate_coverage(_home_pts, _main_pts,
                              [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)], tol=2)
check("A2 主线上 15 个点全部**不**进回正", _cov_main["covered"], 0)
check("A2 主线漏判数 = 15（= 都没进）", len(_cov_main["missed"]), 15)

# A3 引擎真状态机：坑底 15×2 逐个进
_pit_ok = []
for yy in (PIT_Y, PIT_Y + 1):
    for x in range(PIT_X0, PIT_X1 + 1):
        b = new_home_bot((x, yy))
        b.update_cmd_by_route()
        _pit_ok.append(b.is_using_home_route)
check("A3 引擎状态机：坑底 30 个落点全部进回正", (_pit_ok.count(True), len(_pit_ok)), (30, 30))

# A4 引擎真状态机：主线 15 个点逐个不误进
_main_ok = []
for x in range(MAIN_X0, MAIN_X1 + 1):
    b = new_home_bot((x, MAIN_Y))
    b.update_cmd_by_route()
    _main_ok.append(b.is_using_home_route)
check("A4 引擎状态机：主线 15 个点全部不误进", (_main_ok.count(True), len(_main_ok)), (0, 15))

# A5 交汇点（回正线终点画在主线上 → d_home = d_main = 0）→ 必须退出回正
b = new_home_bot((CORNER_X, MAIN_Y))
b.is_using_home_route = True
b.img_route = b.img_route_home
b.home_d_home = 0
b.home_d_main = 0
check("A5 交汇点 d_home=d_main=0 → _home_line_keep=False（主线优先）",
      b._home_line_keep(0, 0), False)
# ⚠️ 必须调**两次**（2026-09-15 修：原来只调一次 → 假失败）：
#    引擎 try_return_to_mainline 里有一道硬闸 —— 要求「**连续两帧 y 相同**」才算
#    真的站定。为什么必须这么严：人起跳时 y 会从 126 一路经过 124 / 123 / 122，
#    只判"y 落在站立行 ±1"的话，**人还在半空**就会被判「已回主线」踢出回正，
#    主线立刻接管给 left/right → 空中被横向带偏 → 摔回坑底（引擎 1890-1898 行
#    有完整踩坑实录）。所以第一帧只是"记住上一帧 y"、**必然返回 False**。
#    实测：第 1 次 False / 第 2 次 True。原来那条断言写"应该退出"却只调一次，
#    等于要求引擎放弃这道硬闸 —— 断言本身写错了，不是引擎错。
check("A5 交汇点 第 1 帧不退出（引擎要求连续两帧 y 相同才算站定）",
      (b.try_return_to_mainline(), b.is_using_home_route), (False, True))
check("A5 交汇点 第 2 帧 → 成功退出回正", b.try_return_to_mainline(), True)
check("A5 退出后 is_using_home_route=False", b.is_using_home_route, False)

# A6 相等时主线优先（严格小于号）
check("A6 纯函数 d_home==d_main → 不进", home_should_return(5, 5, 2), False)
check("A6 纯函数 差=tol（d_main-d_home=2）→ 不进", home_should_return(5, 7, 2), False)
check("A6 纯函数 差>tol（d_main-d_home=3）→ 进", home_should_return(5, 8, 2), True)
check("A6 d_home=None → 不进", home_should_return(None, 8, 2), False)
check("A6 d_main=None → 不进", home_should_return(0, None, 2), False)


# ══════════════════════════════════════════════════════════════════════
sec("【B】死锁四道防御 D1~D4 + 241 帧独立复现")
# ══════════════════════════════════════════════════════════════════════
# B0 源码存在性（读源码，逐条确认四道真的在）
_src_pick = inspect.getsource(Bot._home_line_pick)
_src_dist = inspect.getsource(Bot._home_dist)
_src_update = inspect.getsource(Bot.update_cmd_by_route)
_src_exit = inspect.getsource(Bot._exit_home_route)

check("B·D1 _home_line_pick 里调了 _home_reenter_allowed（新入口也上锁）",
      "_home_reenter_allowed" in _src_pick, True)
check("B·D2 _home_dist 内部按 armed 过滤（不是外层判）",
      "armed" in _src_dist and "continue" in _src_dist, True)
check("B·D3 超时分支清指令（cmd_move_x = none）",
      'cmd_move_x = "none"' in _src_update, True)
check("B·D3 超时分支显式 return（本帧交还打怪）",
      "本帧到此为止" in _src_update and "return" in _src_update, True)
check("B·D3 超时退出走 disarm=True",
      "disarm=True" in _src_update, True)
check("B·D4 去武装绑在「线」上（_exit_home_route 写 ln[armed]）",
      'ln["armed"] = False' in _src_exit, True)
check("B·D4 home_params 有 floors（关不掉）",
      home_params({"route": dict(HOME_PARAMS, home_timeout=0)})["home_timeout"], 8)
check("B·D4 home_reenter_lock 负 → 回落 10",
      home_params({"route": dict(HOME_PARAMS, home_reenter_lock=-1)})["home_reenter_lock"], 10)
check("B·D4 home_rearm_timeout 0 → 回落 30",
      home_params({"route": dict(HOME_PARAMS, home_rearm_timeout=0)})["home_rearm_timeout"], 30)
check("B·D4 home_dist_tol 负 → 回落 2",
      home_params({"route": dict(HOME_PARAMS, home_dist_tol=-9)})["home_dist_tol"], 2)

# B1 死锁场景：角色卡在坑底（跳失败），回正线就在脚边，主线在 70px 外（> rescue 40）
#    → 超时退出后角色**仍在坑底**、d_home 仍是 0 → 若无 D1/D2 下一帧立刻重进。
def make_deadlock_bot():
    main = np.zeros(SHAPE + (3,), dtype=np.uint8)
    for x in range(94, 109):
        main[100, x] = BLUE                       # 主线在 y=100（离坑底 70px）
    home = np.zeros(SHAPE + (3,), dtype=np.uint8)
    for x in range(94, 109):
        home[170, x] = MAGENTA                    # 横段（坑底 y=170）
    for y in range(100, 171):
        home[y, 99] = MAGENTA                     # 竖段（x=99）
    home[100, 99] = YELLOW                        # goal 贴主线 → 合法线
    b = make_bot(player_global=(99, 170))
    b.img_routes = [main]
    b.img_route_home = home
    b.img_route = main
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("死锁QA", home, b.img_routes)
    return b


b_dl = make_deadlock_bot()
check("B1 死锁场景这条回正线是**合法**的（1 条可用）", len(b_dl.home_lines), 1)

FPS = 30
HOME_TIMEOUT_S = float(HOME_PARAMS["home_timeout"])          # 8
LOCK_S = float(HOME_PARAMS["home_reenter_lock"])             # 10
REARM_S = float(HOME_PARAMS["home_rearm_timeout"])           # 30
MAX_ALLOWED = int(-(-HOME_TIMEOUT_S * FPS // 1)) + 1         # ceil(8*30)+1 = 241
TOTAL = int((HOME_TIMEOUT_S + LOCK_S + 3) * FPS)             # 630

CLOCK.t = 10000.0
n_consec = 0
max_consec = 0
n_out = 0
was_home = False
lock_at_exit = None
for _ in range(TOTAL):
    CLOCK.tick()
    b_dl.loc_player_global = (99, 170)           # 角色卡住不动（跳跃失败）
    b_dl.cmd_move_x = b_dl.cmd_move_y = b_dl.cmd_action = "none"
    b_dl.update_cmd_by_route()
    if b_dl.is_using_home_route:
        n_consec += 1
        max_consec = max(max_consec, n_consec)
        was_home = True
    else:
        if was_home:
            lock_at_exit = b_dl.home_reenter_lock_t > 0
            was_home = False
        n_consec = 0
        n_out += 1

print(f"       {TOTAL} 帧（fps={FPS}）：回正 {TOTAL - n_out} 帧 / 退出 {n_out} 帧；"
      f"**最大连续回正 {max_consec} 帧**，上限 {MAX_ALLOWED}")
check(f"B1 ★ 最大连续回正 ≤ {MAX_ALLOWED}（不复活死锁）", max_consec <= MAX_ALLOWED, True)
check("B1 ★ 确实出现过「不在回正」的帧（超时真退出去了）", n_out > 0, True)
check("B1 ★ 超时退出那一帧立刻上冷静期（D1）", lock_at_exit, True)
check("B1 ★ 630 帧跑完仍在外面（任何入口都没拉回）", b_dl.is_using_home_route, False)
check("B1 ★ 该线仍去武装（30s 兜底还没到）", b_dl.home_lines[0]["armed"], False)
soft("B1 复现工程师报的 241 帧（软核对）", max_consec, MAX_ALLOWED)

# B2 D1/D2/D3 行为化确认（在 630 帧之后继续验）
check("B2 D2：唯一线去武装 → _home_dist 返回 (None, None)",
      b_dl._home_dist(), (None, None))
check("B2 D2：解锁冷静期也不进（线仍去武装）",
      (b_dl._home_reenter_allowed(), b_dl._home_line_pick())[1], None)

# D1：超时那一帧的出口是否清干净（单独构造一帧，看指令与状态）
b_d3 = make_deadlock_bot()
b_d3.update_cmd_by_route()
check("B2 D3：坑底进入回正", b_d3.is_using_home_route, True)
b_d3.home_enter_t = CLOCK.time() - (HOME_TIMEOUT_S + 1)      # 强制已超时
b_d3.cmd_move_x = b_d3.cmd_move_y = b_d3.cmd_action = "jump"  # 故意留脏指令
b_d3.update_cmd_by_route()
check("B2 D3：超时帧退出回正", b_d3.is_using_home_route, False)
check("B2 D3：超时帧指令全清 none（不留 jump / goal）",
      (b_d3.cmd_move_x, b_d3.cmd_move_y, b_d3.cmd_action), ("none", "none", "none"))
check("B2 D3：超时帧给所有入口上冷静期", b_d3.home_reenter_lock_t > 0, True)
check("B2 D2：超时帧把该线去武装", b_d3.home_lines[0]["armed"], False)
b_d3.update_cmd_by_route()
check("B2 D1：冷静期内下一帧任何入口都进不去", b_d3.is_using_home_route, False)

# D2 恢复路径①：角色离开该线 bbox ≥ home_rearm_margin(4)
b_d3.loc_player_global = (99, 170 + 9)
check("B2 D2 恢复①：离开范围 ≥4px → 重新武装",
      b_d3._home_line_armed(b_d3.home_lines[0]), True)
# D2 恢复路径②：30s 兜底（角色一直在坑里晃也不能永远不接）
b_d3.home_lines[0]["armed"] = False
b_d3.home_lines[0]["disarm_t"] = CLOCK.time() - (REARM_S + 1)
b_d3.loc_player_global = (99, 170)
check("B2 D2 恢复②：30s 兜底 → 无条件重新武装",
      b_d3._home_line_armed(b_d3.home_lines[0]), True)


# ══════════════════════════════════════════════════════════════════════
sec("【C】手绘器 L 形端到端：make_blob → 渲染 → 引擎加载链路")
# ══════════════════════════════════════════════════════════════════════
# 模拟需求方在面板上的真实点击序列（动作由单选决定，终点最后显式点）：
#   走过去(94,128) → 走过去(108,128) → 走过去(101,128) → 跳一下(101,123) → 🎯(101,122)
_points = [(94, PIT_Y), (108, PIT_Y), (101, PIT_Y), (101, MAIN_Y + 1), (101, MAIN_Y)]
_actions = ["walk", "walk", "jump", "walk"]
_blob = make_blob(_points, _actions, goal_set=True)
check("C0 make_blob 拐角 = 第 4 个点（x=101）", _blob["corner"], (101, MAIN_Y + 1))
check("C0 make_blob 终点 = 最后一点（贴在主线上）", _blob["goal"], (101, MAIN_Y))

_base_bgr = cv2.cvtColor(np.zeros(SHAPE + (3,), dtype=np.uint8), cv2.COLOR_RGB2BGR)
_rend_bgr = render_home_image(_base_bgr, [_blob], CC, goal_radius=1)
_rend_rgb = cv2.cvtColor(_rend_bgr, cv2.COLOR_BGR2RGB)

# C1 汇聚编码逐像素核对
check("C1 横段左半 (95,128) = right（蓝）",
      tuple(int(v) for v in _rend_rgb[PIT_Y, 95]), BLUE)
check("C1 横段左半 (100,128) = right（蓝）",
      tuple(int(v) for v in _rend_rgb[PIT_Y, 100]), BLUE)
check("C1 横段右半 (102,128) = left（红）",
      tuple(int(v) for v in _rend_rgb[PIT_Y, 102]), RED)
check("C1 横段右半 (107,128) = left（红）",
      tuple(int(v) for v in _rend_rgb[PIT_Y, 107]), RED)
check("C1 拐角列 (101,128) 留给竖段 = 跳（洋红）",
      tuple(int(v) for v in _rend_rgb[PIT_Y, 101]), MAGENTA)
check("C1 终点 (101,122) = goal（黄）",
      tuple(int(v) for v in _rend_rgb[MAIN_Y, 101]), YELLOW)
check("C1 竖段中段 (101,125) = 跳（洋红）",
      tuple(int(v) for v in _rend_rgb[125, 101]), MAGENTA)

# C2 中文路径往返：渲染图 → 落盘 → 读回 → mask → 引擎判废
_tmp = tempfile.mkdtemp(prefix="qa_v2_")
_cn_dir = os.path.join(_tmp, "中文地图_废都南方工地")
os.makedirs(_cn_dir, exist_ok=True)
_cn_path = os.path.join(_cn_dir, "route_home.png")
try:
    ok_write = imwrite_unicode(_cn_path, _rend_bgr)
    check("C2 中文路径写 PNG 成功（imwrite_unicode）", ok_write, True)
    _back_bgr = imread_unicode(_cn_path)
    check("C2 中文路径读回非 None（imdecode 绕开编码）", _back_bgr is not None, True)
    _back_rgb = cv2.cvtColor(_back_bgr, cv2.COLOR_BGR2RGB)
    check("C2 往返后像素一致（(95,128) 仍为蓝）",
          tuple(int(v) for v in _back_rgb[PIT_Y, 95]), BLUE)

    _black_map = np.zeros(SHAPE + (3,), dtype=np.uint8)
    _masked = mask_route_colors(_black_map, _back_rgb.copy(), cc_str(CC))
    b_draw = make_bot(player_global=(100, PIT_Y))
    b_draw.img_routes = [make_mainline()]
    b_draw._build_mainline_cache()
    _lines = b_draw._scan_home_lines("中文地图_废都南方工地", _masked, b_draw.img_routes)
    check("C2 引擎加载链路读回 → **不是废线**（1 条可用）", len(_lines), 1)
    if _lines:
        print(f"       体检：#1 起点 {_lines[0]['start']} → 终点 {_lines[0]['goal']}，"
              f"落点覆盖 {_lines[0]['cover_xs']}，含跳跃={_lines[0]['has_jump']}")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# C3 三个落点（横段左端 / 中间 / 右端）都要能进回正
_land_ok = {}
for lx in (95, 101, 107):
    b = new_home_bot((lx, PIT_Y), home=_rend_rgb)
    b.update_cmd_by_route()
    _land_ok[lx] = b.is_using_home_route
check("C3 落点左端 (95,128) 进回正", _land_ok[95], True)
check("C3 落点中间 (101,128) 进回正", _land_ok[101], True)
check("C3 落点右端 (107,128) 进回正", _land_ok[107], True)

# C4 反例对照：只画竖线（无横段）→ 判废
_vert_blob = make_blob([(101, PIT_Y), (101, MAIN_Y + 1), (101, MAIN_Y)],
                       ["jump", "walk"], goal_set=True)
_vert_bgr = render_home_image(_base_bgr, [_vert_blob], CC, goal_radius=1)
_vert_rgb = cv2.cvtColor(_vert_bgr, cv2.COLOR_BGR2RGB)
b_vert = make_bot(player_global=(100, PIT_Y))
b_vert.img_routes = [make_mainline()]
b_vert._build_mainline_cache()
_vert_lines = b_vert._scan_home_lines("只画竖线", _vert_rgb.copy(), b_vert.img_routes)
check("C4 反例：只画竖线 → 判废（0 条可用）", len(_vert_lines), 0)


# ══════════════════════════════════════════════════════════════════════
sec("【D】边界：0 条可用 / tol=0 或负 / 多条废一条不连坐 / 走丢回退")
# ══════════════════════════════════════════════════════════════════════
# D1 现网就是 0 条可用：d_home=None 不能崩
b0 = make_bot(player_global=(101, PIT_Y))
b0.img_routes = [make_mainline()]
b0.img_route_home = None
b0.home_lines = []
b0.img_route = b0.img_routes[0]
b0._build_mainline_cache()
check("D1 0 条可用 → _home_dist() = (None, None)", b0._home_dist(), (None, None))
check("D1 0 条可用 → _home_line_pick() = None", b0._home_line_pick(), None)
_crash = None
try:
    for _ in range(5):
        CLOCK.tick()
        b0.loc_player_global = (101, PIT_Y)
        b0.update_cmd_by_route()
except Exception as e:  # noqa: BLE001
    _crash = repr(e)
check("D1 0 条可用时 update_cmd_by_route 连跑 5 帧不抛异常", _crash, None)
check("D1 0 条可用时不进回正", b0.is_using_home_route, False)

# D2 home_dist_tol 被改成 0 / 负数
check("D2 tol=0：合法（不回落，允许 0）",
      home_params({"route": dict(HOME_PARAMS, home_dist_tol=0)})["home_dist_tol"], 0)
check("D2 tol=-3：回落默认 2",
      home_params({"route": dict(HOME_PARAMS, home_dist_tol=-3)})["home_dist_tol"], 2)
check("D2 tol=0 时 交汇点(0,0) 仍主线优先", home_should_return(0, 0, 0), False)
check("D2 tol=0 时 坑底(0,6) 仍进回正", home_should_return(0, 6, 0), True)
_cfg0 = dict(BASE_CFG)
_cfg0["route"] = dict(BASE_CFG["route"], home_dist_tol=0)
b_t0 = new_home_bot((PIT_X0, PIT_Y))
b_t0.cfg = _cfg0
b_t0.update_cmd_by_route()
check("D2 tol=0 时坑底照样进回正（引擎）", b_t0.is_using_home_route, True)

# D3 一条图里多条回正线：废一条**不连坐**其它
_main_long = make_mainline(x0=92, x1=160)
_comp = np.zeros(SHAPE + (3,), dtype=np.uint8)


def _paint_l(img, cx, pit_y=PIT_Y, main_y=MAIN_Y, x0=None, x1=None):
    x0 = cx - 7 if x0 is None else x0
    x1 = cx + 7 if x1 is None else x1
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, pit_y) for x in range(x0, x1 + 1)],
                          (cx, pit_y), CC, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(main_y + 1, pit_y + 1):
        img[y, cx] = MAGENTA
    img[main_y, cx] = YELLOW
    return img


_comp = _paint_l(_comp, 101)      # 合法 L ①
_comp = _paint_l(_comp, 137)      # 合法 L ②
for y in range(150, 157):         # 废线：只画竖线（另一处）
    _comp[y, 160] = MAGENTA
_comp[150, 160] = YELLOW

b_multi = make_bot(player_global=(100, PIT_Y))
b_multi.img_routes = [_main_long]
b_multi._build_mainline_cache()
_multi_lines = b_multi._scan_home_lines("多线", _comp, b_multi.img_routes)
check("D3 三条里两条合法、一条废 → 保留 2 条", len(_multi_lines), 2)
check("D3 保留的是 x=101 与 x=137 两条",
      sorted([ln["goal"][0] for ln in _multi_lines]), [101, 137])
check("D3 废线像素被就地涂黑（(160,153) 已黑）",
      tuple(int(v) for v in _comp[153, 160]), (0, 0, 0))
check("D3 合法线① 的像素没被连坐（(101,128) 仍是跳色）",
      tuple(int(v) for v in _comp[PIT_Y, 101]), MAGENTA)

# D4 走丢回退路径：主线够不着 + 附近有回正线 → 进回正；主线够不着 + 回正线也够不着 → 有出口不卡死
b_lost_far = new_home_bot((200, 60))     # 离主线、离回正线都 > search_range
b_lost_far.img_route = b_lost_far.img_routes[0]
_n_frames = 6
_stuck_home = 0
for _ in range(_n_frames):
    CLOCK.tick()
    b_lost_far.loc_player_global = (200, 60)
    b_lost_far.update_cmd_by_route()
    if b_lost_far.is_using_home_route:
        _stuck_home += 1
check("D4 走丢且回正线也够不着 → 帧末不在回正（有出口，不卡死）",
      b_lost_far.is_using_home_route, False)
soft("D4 走丢远处每帧的「进→接不上→退」次数（0~1 理想）", _stuck_home <= 1, True)


# ══════════════════════════════════════════════════════════════════════
sec("【E】新发现的边界：竖段画到平台外也能过验证（验证的盲区）")
# ══════════════════════════════════════════════════════════════════════
# 真实地形：平台（主线像素）x∈[92,106]，坑 x∈[94,108] —— 坑比平台**右伸 2px**。
# 判据⑤只要求「goal 像素集合到主线 min ≤ home_goal_mainline_tol(2)」，
# 而 goal 是半径 1 的 5px 十字 → 圆心离主线 3px 时，十字边缘那格仍只有 2px → **判据通过**。
# 后果：竖段若画在 x=107/108（平台外、无地板），验证通过但角色跳上去会落空 → 8s 空转后超时。
# ⚠️ 这是「验证无法覆盖地形实况」的盲区，不是判据写错；用户把终点点在主线像素上时不会触发。
def _l_with_vertical_at(cx):
    img = np.zeros(SHAPE + (3,), dtype=np.uint8)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)],
                          (cx, PIT_Y), CC, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(MAIN_Y + 1, PIT_Y + 1):
        img[y, cx] = MAGENTA
    img[MAIN_Y, cx] = YELLOW
    return img


def _scan_lines_at(cx):
    _b = make_bot(player_global=(100, PIT_Y))
    _b.img_routes = [make_mainline()]
    _b._build_mainline_cache()
    return _b._scan_home_lines(f"竖段x{cx}", _l_with_vertical_at(cx).copy(), _b.img_routes)


for _cx in (101, 106, 107, 108, 109):
    _ln = _scan_lines_at(_cx)
    _on_plat = MAIN_X0 <= _cx <= MAIN_X1
    _tag = "过验证" if _ln else "判废"
    print(f"       竖段 x={_cx}（平台上? {_on_plat}）→ {_tag}")
    if _cx in (107, 108):
        soft(f"E 竖段 x={_cx} 在平台外却**过了验证**（盲区，需手绘器提示/收紧容差）",
             len(_ln), 0)     # 期望：本应判废；实际会通过 → 软核对会标红提醒
check("E 竖段画在平台内 (x=101) → 正常过验证",
      len(_scan_lines_at(101)), 1)


# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 有 {len(FAIL)} 项未通过：")
    for f in FAIL:
        print(f"   - {f}")
else:
    print("✅ 全部通过")
if NOTE:
    print(f"（另有 {len(NOTE)} 项软核对需人工看一眼：{NOTE}）")
print("=" * 60)
