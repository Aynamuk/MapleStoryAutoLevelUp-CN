'''
离线自检：打怪 / 转向 / 回正 / 巡逻闭环 / 路线校验（2026-09-12）

覆盖的坑：
  ① 索敌范围内没有怪时还在疯狂打空气（cmd_action 跨帧残留）
  ② 角色不会转向、后方的怪打不到（出招与转向同一帧）
  ③ 回正线接不上时既不走也不打（「回正优先」缺出口 → 死局）
  ④ 路线录废了却只在运行时才发作（改为加载时判废）
  ⑤ 巡逻线闭不上环，走完最后一段卡在终点
  ⑥ 打怪时位移小于看门狗阈值，被误判卡死抽随机动作打断战斗

v0.9 **第二版**「回正线」判定（本文件【8】~【11】【20】起）：
    d_home = 角色 → 最近「已武装」回正线像素 的曼哈顿距离
    d_main = 角色 → 最近主线像素 的曼哈顿距离（全图）
    d_home < d_main - home_dist_tol(2)  →  进 / 保持回正
    否则                                →  主线优先（相等也是主线优先）
    进入 / 保持 / 退出**同一个表达式**（home_should_return），口径唯一。

★ 死锁硬断言（【30】）：最大**连续**处于回正状态的帧数 ≤ ceil(home_timeout × fps) + 1。
  这是"8s 超时有没有被同一帧的老路径抵消"的**直接证据** —— 比看日志可靠得多：
  第一版就是因为只看日志（日志一切正常）才漏掉的。

不开游戏、不抢焦点，把引擎里这几个方法挂到 stub 上直接跑。

用法：
    python -m tools.verify_navigation

⚠️ 2026-09-12：定点模式（stand）已删除，本文件由 verify_stand_attack.py 改名而来，
   定点专属用例（直连 / 滞回 / 安全点）已随功能一起移除。
'''
import copy
import os
import shutil
import sys
import tempfile
import time as _real_time

import numpy as np

# ── 直接复用引擎里的真实实现，避免"测的和跑的不是同一份代码" ──────────────
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.home_route import DEFAULT_GOAL_RGB, home_should_return
from src.utils.common import edge_guard_step_cap, load_yaml

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


class Stub:
    '''只带被测方法所需字段的最小宿主。'''


Stub.try_return_to_mainline = MapleStoryAutoBot.try_return_to_mainline
Stub.update_cmd_by_mob_detection = MapleStoryAutoBot.update_cmd_by_mob_detection
Stub.check_reach_goal = MapleStoryAutoBot.check_reach_goal
Stub.get_face_direction = MapleStoryAutoBot.get_face_direction
Stub.get_attack_direction = MapleStoryAutoBot.get_attack_direction

#: v0.9 第二版回正参数（与 config_default.yaml 的 route: 段默认值一致）
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
}

BASE_CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    # turn_frames：2026-09-17 加的「转身保持帧数」，显式写出来避免依赖默认值
    "directional_attack": {"range_x": 350, "range_y": 70, "cooldown": 0.9,
                           "turn_frames": 3},
    "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
    "monster_detect": {"search_box_margin": 50},
    "key": {"teleport": ""},
    "route": dict({"search_range": 10, "rescue_range": 40}, **HOME_PARAMS),
    "watchdog": {"range": 10, "timeout": 10, "attack_grace": 5},
}

#: 「终点内缩」**关掉**的对照配置（endpoint_margin = 0）。
# ⚠️ 为什么需要它：【18】要验的是「一帧跨过 goal 也必须切段」，跟终点内缩是**两件事**。
#    BASE_CFG 没配 endpoint_margin → 取默认 3 → seg1 的终点 111 会被内缩成 109。
#    如果直接把【18】的期望值改成 109，这条用例就同时测了两件事：以后「内缩坏了」
#    和「切段坏了」会红在同一条上，看不出到底是哪个。
#    所以【18】改用这份 margin=0 的配置，还原成「只测切段」；
#    内缩本身由【35】单独验收（那里才需要 margin=3 的 BASE_CFG）。
NOMARGIN_CFG = copy.deepcopy(BASE_CFG)
NOMARGIN_CFG["route"]["endpoint_margin"] = 0

#: margin=4 / 8 的对照配置（【35】⑦⑧ 用：验「短路线能收满」+「调爆了也不塌成一点」）
MARGIN4_CFG = copy.deepcopy(BASE_CFG)
MARGIN4_CFG["route"]["endpoint_margin"] = 4
MARGIN8_CFG = copy.deepcopy(BASE_CFG)
MARGIN8_CFG["route"]["endpoint_margin"] = 8


def new_bot(cfg=None, player=(500, 300), player_global=(100, 100), cls=Stub):
    b = cls()
    b.cfg = cfg if cfg is not None else BASE_CFG
    b.loc_player = player                  # 画面坐标
    b.loc_player_global = player_global    # 小地图（全局）坐标
    b.img_frame = np.zeros((600, 1000, 3), dtype=np.uint8)
    b.monsters = []
    b._monsters = []                       # get_monsters_in_range 的返回值
    b.cmd_move_x = "none"
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.cmd_move_x_last = "none"
    b._turn_frames_left = 0        # 2026-09-17 转身状态机（见 update_cmd_by_mob_detection）
    b._turn_dir = None
    b.is_using_home_route = False
    b.is_on_ladder = False
    b.img_route_home = None            # 回正线（用例按需自己填）
    b.img_route = None
    b.color_code = {}                      # 主线色码表（try_return_to_mainline 用）
    b.color_code_up_down = {}
    # ── v0.9 第二版回正跨帧状态 ────────────────────────────────────────
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
    b.home_reenter_lock_t = 0.0        # 回正失败冷静期起点（0 = 没锁）
    b.t_last_home_lock_log = 0.0
    b.img_routes = []
    b.t_last_attack = 0.0                  # 很久以前 → 保证 cooldown 一定满足
    b.t_last_home_fail_log = 0.0           # 回正失败告警节流
    b.idx_routes = 0
    b.route_called = False
    b.route_reverse = False
    b._pingpong_start = None
    b._seg_goals = []
    b._seg_dir = []
    b._seg_span = []
    b._seg_goal_gap = 0            # 相邻两段终点间距（窄平台降级用它，2026-09-15）
    b._goal_armed = True
    b.is_show_debug_window = False
    b.img_route_debug = None
    b.loc_last_route_pixel = None
    b.t_route_rescue_log = 0.0
    return b


def _get_monsters_in_range(self, top_left, bottom_right):
    '''引擎真实签名是 (x0,y0),(x1,y1)；这里只回预置的怪，不真的做检测。'''
    return list(self._monsters)


def _get_nearest_monster(self, is_left=True):
    for m in self.monsters:
        cx = m["position"][0] + m["size"][0] // 2
        if is_left and cx < self.loc_player[0]:
            return m
        if not is_left and cx > self.loc_player[0]:
            return m
    return None


def _get_nearest_color_code(self):
    '''只桩掉"像素搜索"这一层，导航逻辑本身跑引擎真实代码。

    route_cmd = None 表示"这条线上一个像素都找不到"（回正线接不上的场景）。

    ⚠️ route_cmd_on_home（可选）：**切到回正线之后**该返回什么。
       为什么要它：真实运行时"主线找不找得到"和"回正线找不找得到"是**两次独立**
       的搜索；只用一个常量桩的话，B 路径（走丢了 → 进回正）要么永远不触发
       （返回了指令 → 不进），要么一进去就立刻走「回正线接不上」出口退出
       （返回 None）→ 测不出"进了回正、正在往回走"这个中间态。
       默认回落到 route_cmd，所以既有用例行为不变。
    '''
    self.route_called = True
    cmd = getattr(self, "route_cmd", None)
    if self.img_route is not None and self.img_route is getattr(self, "img_route_home", None):
        cmd = getattr(self, "route_cmd_on_home", cmd)
    if not cmd:
        return None, None
    return {"command": cmd, "distance": 1, "pixel": (0, 0)}, None


Stub.get_monsters_in_range = _get_monsters_in_range
Stub.get_nearest_monster = _get_nearest_monster
Stub.get_nearest_color_code = _get_nearest_color_code
# ⚠️ 用引擎真实的 update_cmd_by_route（2026-09-12）：回正"接不上就退出"的出口逻辑
#    就写在真实方法里；再用假方法测，等于测了另一份代码，出口有没有生效都测不出来。
Stub.update_cmd_by_route = MapleStoryAutoBot.update_cmd_by_route
Stub.validate_route_image = MapleStoryAutoBot.validate_route_image
Stub.is_player_stuck = MapleStoryAutoBot.is_player_stuck
Stub.check_route_loop_closure = MapleStoryAutoBot.check_route_loop_closure
Stub._route_pixels = MapleStoryAutoBot._route_pixels
Stub._calc_pingpong_start = MapleStoryAutoBot._calc_pingpong_start
Stub._calc_seg_goals = MapleStoryAutoBot._calc_seg_goals
# 上一轮把 _calc_seg_goals 拆成「入口 wrapper + _calc_seg_goals_with(margin)」之后
# 没给 Stub 补新方法 → 【18】起全线 AttributeError（本文件后半段根本跑不到）。
Stub._calc_seg_goals_with = MapleStoryAutoBot._calc_seg_goals_with
Stub._near_seg_goal = MapleStoryAutoBot._near_seg_goal
Stub._pingpong_enabled = MapleStoryAutoBot._pingpong_enabled
Stub._apply_pingpong = MapleStoryAutoBot._apply_pingpong
# ── 平台边缘硬围栏（2026-09-13 加）────────────────────────────────────────
# update_cmd_by_route 末尾会调 _apply_edge_guard：Stub 不绑就会 AttributeError
# 崩掉整条指令链路（【11】之后的用例全跑不了）。
Stub._apply_edge_guard = MapleStoryAutoBot._apply_edge_guard
Stub._edge_guard_rows = MapleStoryAutoBot._edge_guard_rows
Stub._edge_guard_range = MapleStoryAutoBot._edge_guard_range
Stub._edge_guard_step = MapleStoryAutoBot._edge_guard_step
Stub._log_edge_guard = MapleStoryAutoBot._log_edge_guard
# ── v0.9 第二版回正状态机：全部挂**引擎真实方法**（另写假逻辑等于测了另一份代码）──
Stub._build_mainline_cache = MapleStoryAutoBot._build_mainline_cache
Stub._scan_home_lines = MapleStoryAutoBot._scan_home_lines
Stub._log_home_health = MapleStoryAutoBot._log_home_health
Stub._home_dist = MapleStoryAutoBot._home_dist
Stub._main_dist = MapleStoryAutoBot._main_dist
Stub._home_line_keep = MapleStoryAutoBot._home_line_keep
Stub._home_line_pick = MapleStoryAutoBot._home_line_pick
Stub._home_line_armed = MapleStoryAutoBot._home_line_armed
Stub._enter_home_route = MapleStoryAutoBot._enter_home_route
Stub._exit_home_route = MapleStoryAutoBot._exit_home_route
Stub._home_dwell_ok = MapleStoryAutoBot._home_dwell_ok
Stub._home_reenter_allowed = MapleStoryAutoBot._home_reenter_allowed
# ── 回正起跳埋点（2026-09-14 加）──────────────────────────────────────────
# update_cmd_by_route 末尾会调 _trace_home_cmd：Stub 不绑就 AttributeError，
# 【11】之后的用例全线崩 —— 跟上面 _apply_edge_guard 那次是**同一个坑**。
Stub._trace_home_cmd = MapleStoryAutoBot._trace_home_cmd
# ── 主循环性能埋点（2026-09-14 加）──────────────────────────────────────────
# _perf 会在 update_cmd_by_route / update_cmd_by_mob_detection 里被调用：
# Stub 不绑就 AttributeError，【11】之后的用例全线崩 —— 跟 _apply_edge_guard、
# _trace_home_cmd 是**同一个坑**（这是第四次了，别再靠崩了才发现）。
Stub._perf = MapleStoryAutoBot._perf
# ── 回正成功后「归位到安全区中心」（2026-09-14 加）──────────────────────────
# _apply_recenter 在 update_cmd_by_route 里被调用，Stub 不绑就 AttributeError。
# 同一个坑的**第五次**了（_calc_seg_goals_with / _apply_edge_guard /
# _trace_home_cmd / _perf / _apply_recenter）—— 加新方法时先来这里挂一下。
Stub._apply_recenter = MapleStoryAutoBot._apply_recenter
# ── 回正起跳前先停稳（2026-09-14 加）：同一个坑的第六次，加新方法先来挂一下 ──
Stub._apply_jump_brake = MapleStoryAutoBot._apply_jump_brake
# ── 窄平台降级（2026-09-15 加）：同一个坑的**第七次** ────────────────────
# update_cmd_by_route 里每帧会调 _patrol_degraded_now / _log_patrol_degraded，
# is_player_stuck 里也读 _patrol_degraded。不挂 → AttributeError → 【11】之后的
# 用例全线崩（本次就是这么崩的：跑到 line 491 直接 traceback 退出）。
Stub._patrol_degraded_now = MapleStoryAutoBot._patrol_degraded_now
Stub._log_patrol_degraded = MapleStoryAutoBot._log_patrol_degraded

# ── ★ 收口：自动挂**全部**引擎真实方法（2026-09-15 加，同一个坑的第八次）──────
# 上面那份**手写白名单**已经踩了七次：引擎每加一个在 `update_cmd_by_route` 里被
# 调用的方法，白名单就漏一个 → AttributeError → 本文件后半段**全线崩、且不打印
# 总结行**（看着像"没报错"）。第八次是 `_home_below_mainline`（2026-09-15 进入侧
# 硬闸），症状：跑到 line 822 直接 traceback，223 条用例只跑到 73 条。
# ⇒ 不再维护白名单。下面这段把引擎里**所有**还没被绑定的可调用方法一次挂上，
#   以后引擎再加方法**不用回来补**（同 verify_home_route_qa2.py / verify_home_route.py
#   / verify_home_v2_qa.py 的做法）。
# ⚠️ 必须放在**所有自定义桩之后**：`hasattr(Stub, ...)` 保证不会覆盖上面手写的
#    `_get_monsters_in_range` / `_get_nearest_monster` / `_get_nearest_color_code`
#    这几个"故意和引擎不一样"的桩。
for _n in dir(MapleStoryAutoBot):
    if _n.startswith("__") or hasattr(Stub, _n):
        continue
    _attr = getattr(MapleStoryAutoBot, _n)
    if callable(_attr):
        setattr(Stub, _n, _attr)


def mob(x, y, w=40, h=40):
    return {"position": (x, y), "size": (w, h)}


# ══════════════════════════════════════════════════════════════════════
print("\n【1】没有怪时必须清掉残留的 attack（原 bug：疯狂打空气）")
b = new_bot()
b._monsters = [mob(300, 300)]        # 怪在玩家(500,300)左边
# 2026-09-17：转向改成「按住方向键 turn_frames 帧」再出招（旧实现只转 1 帧）
TF = int(BASE_CFG["directional_attack"].get("turn_frames", 3))
for _i in range(TF):
    b.update_cmd_by_mob_detection()
    b.cmd_move_x_last = b.cmd_move_x     # 模拟 hunting.on_frame 的记录
check(f"转向 {TF} 帧内不出招", b.cmd_action, "none")
check("转向期间一直按着目标方向", b.cmd_move_x, "left")
b.update_cmd_by_mob_detection()          # 转向结束 → 出招
check("有怪且已转向 → 出招", b.cmd_action, "attack")
check("★ 出招帧必须**松开**方向键（否则技能朝旧朝向飞）", b.cmd_move_x, "none")

b._monsters = []                     # 怪没了
b.cmd_move_x_last = b.cmd_move_x
b.update_cmd_by_mob_detection()
check("怪消失后 cmd_action 被清", b.cmd_action, "none")

def patrol_frame(b):
    '''模拟巡逻模式的一整帧：清指令 → update_cmd_by_mob_detection
    → hunting.on_frame 记录本帧实际方向。

    ⚠️ 这里**不预置路线像素**（route_cmd 为 None），所以路线不给任何走位指令
       （引擎这时会保持上一帧的指令，见 update_cmd_by_route 的 return 分支）——
       等于"角色站在原地"，正好用来单独观察打怪逻辑的转向行为。
    '''
    b.cmd_move_x = "none"
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.update_cmd_by_mob_detection()
    b.cmd_move_x_last = b.cmd_move_x


print("\n【2】先转向、后出招（原 bug：技能朝旧朝向飞走）")
b = new_bot()
b._monsters = [mob(700, 300)]        # 怪在右边，角色初始朝向未知
for _i in range(1, TF + 1):
    patrol_frame(b)
    check(f"转向第{_i}帧只发方向键、不出招",
          (b.cmd_move_x, b.cmd_action), ("right", "none"))
patrol_frame(b)
check("★ 转向结束才出招，且松开方向键",
      (b.cmd_move_x, b.cmd_action), ("none", "attack"))

print("\n【3】怪换到另一侧 → 重新先转身")
b.t_last_attack = 0.0                # 冷却已过（否则本帧什么都不做，测不到转向）
b._monsters = [mob(200, 300)]        # 怪跑到左边
for _i in range(1, TF + 1):
    patrol_frame(b)
    check(f"换边后第{_i}帧重新转身",
          (b.cmd_move_x, b.cmd_action), ("left", "none"))
patrol_frame(b)
check("★ 换边转向结束 → 松开方向键出招",
      (b.cmd_move_x, b.cmd_action), ("none", "attack"))

print("\n【3.5】★ 真实场景：路线一直往左、怪在右边（用户报「一直往前面砍空气」）")
# 这是本次优化要治的症状，必须钉住：
#   旧实现 → 每周期「转 1 帧 + 出招帧仍按着 right」⇒ 技能朝转身前的朝向（= 路线方向）飞
#   新实现 → 按住 right 转 TF 帧 ⇒ 松开方向键出招 ⇒ 技能朝右飞，打得着后面的怪
_cfg_fast = copy.deepcopy(BASE_CFG)
_cfg_fast["directional_attack"]["cooldown"] = 0.01   # 冷却压到最小，压缩帧数
b = new_bot(_cfg_fast)
b._monsters = [mob(700, 300)]                        # 怪在右
_seq = []
for _f in range(12):
    b.cmd_move_x = "left"                            # 路线每帧给的方向（往左走）
    b.cmd_move_y = "none"
    b.cmd_action = "none"
    b.update_cmd_by_mob_detection()                  # 打怪逻辑在路线**之后**跑
    b.cmd_move_x_last = b.cmd_move_x                 # hunting.on_frame 记录
    _seq.append((b.cmd_move_x, b.cmd_action))
# 出招帧：必须 (none, attack) —— 松开方向键
_atk = [i for i, s in enumerate(_seq) if s[1] == "attack"]
check("12 帧内出过招", len(_atk) > 0, True)
check("★ 每次出招都**没有**按着方向键",
      all(_seq[i][0] == "none" for i in _atk), True)
# 出招**前一帧**必须是转向帧（按着 right，压过路线的 left）
check("★ 出招前一帧按着 right（转身压过路线方向）",
      all(_seq[i - 1][0] == "right" for i in _atk if i > 0), True)
# 转向帧必须真的压过路线给的 left（不是被路线拽回 left）
_turn = [i for i, s in enumerate(_seq) if s[0] == "right"]
check("★ 转向帧把路线的 left 顶掉了", len(_turn) > 0, True)
check("★ 没有任何一帧是「按着 left 出招」（旧 bug 的砍空气）",
      [i for i in _atk if _seq[i][0] == "left"], [])

print("\n【4】AOE 模式也要面向最近的怪（原：完全不设朝向）")
cfg = dict(BASE_CFG)
cfg["bot"] = {"attack": "aoe_skill"}
b = new_bot(cfg)
b._monsters = [mob(700, 300)]
b.update_cmd_by_mob_detection()
check("AOE 同帧出招 + 转向", (b.cmd_move_x, b.cmd_action), ("right", "attack"))

print("\n【5】怪在正上方/正下方 → 不强行转身，且清残留")
b = new_bot()
# ⚠️ mob() 收的是左上角，(480,200)+40x40 → 中心 (500,220)，x 正好等于玩家 x=500
b._monsters = [mob(480, 200)]        # 正上方（中心 x 与玩家相同）
b.cmd_action = "attack"              # 预置残留
b.update_cmd_by_mob_detection()
check("正上方不下方向指令", b.cmd_move_x, "none")
check("正上方时残留被清", b.cmd_action, "none")

# ══════════════════════════════════════════════════════════════════════
print("\n【6】梯子上打怪不能抢方向键（否则会脱离梯子掉下去）")
b = new_bot()
b.is_on_ladder = True
b._monsters = [mob(700, 300)]        # 怪在右边
b.update_cmd_by_mob_detection()
check("梯子上照常出招", b.cmd_action, "attack")
check("梯子上不抢方向", b.cmd_move_x, "none")

print("\n【7】回正期间**完全不**改指令（用户实测：掉下去后跑去打怪不回安全点）")
b = new_bot()
b.is_using_home_route = True             # 正在走回正线
b._monsters = [mob(700, 300)]            # 右边有怪
b.cmd_move_x = "left"                    # 回正线让它往左走
b.update_cmd_by_mob_detection()
check("回正方向没被怪拽走", b.cmd_move_x, "left")
check("回正期间不出招", b.cmd_action, "none")
check("但怪仍然被记下来（框和日志要用）", len(b.monsters), 1)

# ══════════════════════════════════════════════════════════════════════
# v0.9 第二版：try_return_to_mainline 的新内核（d_home vs d_main）
# ══════════════════════════════════════════════════════════════════════
import cv2  # noqa: E402  （真实资源那条用例要用）

GOAL_RGB = DEFAULT_GOAL_RGB              # (255,255,0)  终点
JUMP_RGB = (255, 0, 255)                 # 洋红 = none none jump
BLUE_RGB = (0, 0, 255)                   # 蓝 = right none none
MAIN_Y = 122                             # 平台（主线）
MAIN_X0, MAIN_X1 = 92, 106
PIT_Y = 128                              # 坑底
PIT_X0, PIT_X1 = 94, 108
CORNER_X = 99
SHAPE = (187, 268)

CC_HOME = {
    (255, 0, 0): "left none none",
    (0, 0, 255): "right none none",
    (255, 0, 255): "none none jump",
    (255, 255, 0): "none none goal",
}


def make_mainline(pts, shape=SHAPE):
    '''造一张主线 RGB 图（蓝色 = right none none）。'''
    img = np.zeros((shape[0], shape[1], 3), dtype=np.uint8)
    for x, y in pts:
        img[y, x] = BLUE_RGB
    return img


def make_l_home():
    '''标准 L 形回正线：横段沿坑底（汇聚着色）+ 竖段跳回主线 + goal 贴主线。'''
    from src.utils.home_route import paint_converging_hseg
    img = np.zeros((SHAPE[0], SHAPE[1], 3), dtype=np.uint8)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    paint_converging_hseg(bgr, [(x, PIT_Y) for x in range(PIT_X0, PIT_X1 + 1)],
                          (CORNER_X, PIT_Y), CC_HOME, "walk")
    img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    for y in range(MAIN_Y + 1, PIT_Y + 1):
        img[y, CORNER_X] = JUMP_RGB
    img[MAIN_Y, CORNER_X] = GOAL_RGB
    return img


def new_home_bot(player_global, mainline_img=None, home_img=None):
    '''主线 + L 形回正线 + 已解析好的 home_lines。'''
    b = new_bot(player_global=player_global)
    b.color_code = dict(CC_HOME)
    b.color_code_up_down = {}
    b.img_routes = [mainline_img if mainline_img is not None
                    else make_mainline([(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)])]
    b.img_route_home = home_img if home_img is not None else make_l_home()
    b.img_route = b.img_routes[0]
    b._build_mainline_cache()
    b.home_lines = b._scan_home_lines("自检", b.img_route_home, b.img_routes)
    # 桩掉"像素搜索"：回正线**接得上**（角色脚下就是回正线像素）
    b.route_cmd = "none none jump"
    return b


print("\n【8】回正中、角色离主线更近 → 立刻切回主线（新判据：主线优先）")
mainline8 = make_mainline([(100, 100)])
b = new_bot()
b.color_code = {(0, 0, 255): "right none none"}
b.img_routes = [mainline8]
b._build_mainline_cache()
b.img_route = np.zeros((10, 10, 3), dtype=np.uint8)   # 当前走的是回正线
b.is_using_home_route = True
# ⚠️ 2026-09-13 改：原来放在 (100,105)（主线正下方 5px）。加了「必须站到主线
#    站立行才准退出回正」的硬闸后，角色不在 y=100 那一排就不算回到主线，会一直
#    卡在回正里。挪到 (105,100)：d_main 仍是 5、d_home 仍是 179，测试意图不变。
b.loc_player_global = (105, 100)         # d_main = 5（站立行上，横向偏 5px）
# 回正线在很远的 (10,10)~(13,13)：
#   d_home = min(|10-100|+|10-105|, |13-100|+|13-105|) = min(185, 179) = 179
#   → 179 ≥ 5-2 → 主线优先（切回主线）
b.home_lines = [{
    "id": 1, "start": (10, 10), "goal": (13, 13), "span": 6, "has_jump": True,
    "n_pts": 2, "far_pts": [], "cover_xs": (10, 13), "cover_span": 4,
    "bbox": (10, 10, 13, 13),
    "xs": np.asarray([10, 13], dtype=np.int32),
    "ys": np.asarray([10, 13], dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.home_d_main, b._main_seg_i = b._main_dist()
b.home_d_home, b._home_line_near = b._home_dist()
check("d_main = 5", b.home_d_main, 5)
check("d_home = 179（两个像素取最近那个）", b.home_d_home, 179)
# ⚠️ 2026-09-14 起退出回正要求「连续两帧 y 相同」（防止起跳途中被判已回主线），
#    所以这里要先跑一帧把 y 记下来，第二次才会真的切回。
b.try_return_to_mainline()
check("找到主线 → 切回", (b.try_return_to_mainline(), b.is_using_home_route),
      (True, False))
check("切到第 1 段", b.idx_routes, 0)
check("img_route 已换回主线", b.img_route is mainline8, True)

print("\n【9】还在回正线上（离主线远）→ 不切（避免还没回去就误判）")
b = new_bot()
b.color_code = {(0, 0, 255): "right none none"}
b.img_routes = [mainline8]
b._build_mainline_cache()
b.is_using_home_route = True
b.loc_player_global = (10, 10)           # 离主线 (100,100) 180px
b.home_lines = [{
    "id": 1, "start": (10, 10), "goal": (13, 13), "span": 6, "has_jump": True,
    "n_pts": 2, "far_pts": [], "cover_xs": (10, 13), "cover_span": 4,
    "bbox": (10, 10, 13, 13),
    "xs": np.asarray([10, 13], dtype=np.int32),
    "ys": np.asarray([10, 13], dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.home_d_main, b._main_seg_i = b._main_dist()
b.home_d_home, b._home_line_near = b._home_dist()
check("d_home = 0、d_main = 180 → 保持回正", (b.home_d_home, b.home_d_main), (0, 180))
check("太远不切", (b.try_return_to_mainline(), b.is_using_home_route),
      (False, True))

print("\n【10】白色中转点不算主线路线（否则会误判已回主线）")
wp_only = np.zeros((144, 166, 3), dtype=np.uint8)
wp_only[50, 50] = (255, 255, 255)        # 白色 = 中转点，不在 color_code 里
b.img_routes = [wp_only]
b._build_mainline_cache()
b.loc_player_global = (50, 52)
b.is_using_home_route = True
b.home_d_main, b._main_seg_i = b._main_dist()
check("主线像素缓存为空（白色不算）", b._main_pts_xy, None)
check("d_main 量不出 → 绝不判「已回主线」", b.home_d_main, None)
check("中转点不触发切回", (b.try_return_to_mainline(), b.is_using_home_route),
      (False, True))

print("\n【11】回正的退出**只看两线距离**；goal 不再是退出条件（v0.9 坑4）")
# ⚠️ 本用例在 v0.9 被**改写**（原版断言"踩到 goal → 退出回正"）。
#    为什么改：goal 圆点比一条 8px 短回正线的一半还大，而 search_range=10 > 整条线长
#    → 角色**站在落点时**最近的回正线像素可能就是 goal → 一进回正就被判"走完了"。
#    新行为：踩到 goal 只打一条日志，退不退出由「d_home vs d_main」决定。
mainline11 = make_mainline([(100, 100)])
b = new_bot(player_global=(100, 130))
b.color_code = {(0, 0, 255): "right none none"}
b.color_code_up_down = {}
b.img_routes = [mainline11]
b._build_mainline_cache()
b.img_route_home = np.zeros((144, 166, 3), dtype=np.uint8)
b.img_route = b.img_route_home
b.is_using_home_route = True
b.home_enter_pos = (100, 130)
b.home_lines = [{
    "id": 1, "start": (100, 130), "goal": (100, 100), "span": 30, "has_jump": True,
    "n_pts": 2, "far_pts": [], "cover_xs": (100, 100), "cover_span": 1,
    "bbox": (100, 100, 100, 130),
    "xs": np.asarray([100, 100], dtype=np.int32),
    "ys": np.asarray([130, 100], dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.route_cmd = "none none goal"           # 回正线已经走到头
b.update_cmd_by_route()
b.check_reach_goal()                     # hunting.on_frame 紧接着调它
check("踩到 goal 但离主线还远 → 仍在回正（不抢跑）", b.is_using_home_route, True)
check("仍然拿到了 goal 指令（不卡死）", b.cmd_action, "goal")
# 角色挪到主线附近 → 退出
# ⚠️ 2026-09-13 改：同【8】，从 (100,104) 挪到 (104,100)，站上主线那一排。
b.loc_player_global = (104, 100)         # d_main = 4，d_home = 4
b.update_cmd_by_route()
b.update_cmd_by_route()                  # 第二帧（站稳判定需连续两帧同 y）
check("离主线够近、比回正线近 → 退出回正", b.is_using_home_route, False)
check("img_route 已切回主线", b.img_route is mainline11, True)

print("\n【11b】★ 还在坑底/空中（没站上主线那一排）→ 绝不退出回正")
# 2026-09-13 实测修（这条是踩坑实录，别删）：
#   主线的 goal 圆点半径 2，会朝上下各铺 2 行。坑底到 goal 下沿可能只有 0~2px，
#   而 home_dist_tol=2 → `d_home(0) >= d_main(0) - 2` 恒成立 →
#   角色**刚进回正、下一帧就被踢出来**，回正线一步都走不完（实测每秒进出 3~4 次）。
#   跳跃途中更致命：人跳到半空就满足退出式 → 主线立刻接管给 left/right → 被带偏摔回坑底。
#   所以退出回正必须额外要求：角色的 y 落在主线**站立行** ±1 内。
b = new_bot()
b.color_code = {(0, 0, 255): "right none none"}
b.color_code_up_down = {}
b.img_routes = [make_mainline([(100, 100), (100, 104)])]  # 站立行 y=100，goal 下沿铺到 y=104
b._build_mainline_cache()
b.is_using_home_route = True
b.img_route_home = np.zeros((144, 166, 3), dtype=np.uint8)
b.img_route = b.img_route_home
b.home_lines = [{
    "id": 1, "start": (100, 104), "goal": (100, 100), "span": 4, "has_jump": True,
    "n_pts": 2, "far_pts": [], "cover_xs": (100, 100), "cover_span": 1,
    "bbox": (100, 100, 100, 104),
    "xs": np.asarray([100, 100], dtype=np.int32),
    "ys": np.asarray([104, 100], dtype=np.int32),
    "armed": True, "disarm_t": 0.0,
}]
b.loc_player_global = (100, 104)      # 正踩在 goal 下沿上
b.home_d_main, b._main_seg_i = b._main_dist()
b.home_d_home, b._home_line_near = b._home_dist()
check("构造校验：d_main = 0（goal 下沿就在脚下，旧写法会误判『已回主线』）",
      b.home_d_main, 0)
check("构造校验：d_home = 0", b.home_d_home, 0)
check("★ 主线站立行 = 100（不是 goal 下沿 104）", b._main_stand_y, 100)
check("★ d_home=0 也不退出回正（没站上站立行就是没回去）",
      b.try_return_to_mainline(), False)
check("★ 仍在回正状态", b.is_using_home_route, True)
# 对照：站上站立行就正常退出（确认硬闸没把正常出口堵死）
b.loc_player_global = (100, 100)
b.home_d_main, b._main_seg_i = b._main_dist()
b.home_d_home, b._home_line_near = b._home_dist()
b.try_return_to_mainline()               # 第一帧：记下 y
check("★ 站上主线站立行 → 正常退出回正（硬闸没堵死出口）",
      b.try_return_to_mainline(), True)

print("\n【11c】★ 起跳途中 y 经过站立行 → 绝不退出回正（2026-09-14 加）")
# 起跳轨迹：y 会从 126 一路经过 125 / 124 / 123 / 122 / 121（最高点），
# 其中 123、122、121 **全都满足**「站立行 ±1」。
# 只判 ±1 的话，**人还在半空**就被判「已回主线」踢出回正 →
# 主线立刻接管给 left/right → 空中被横向带偏 → 摔回坑底。
# 这就是用户说的「起跳了却永远上不去」。
# 所以必须再要求**连续两帧 y 相同**（只有落地站定才满足）—— 这里钉住它。
b3 = new_bot()
b3.color_code = {(0, 0, 255): "right none none"}
b3.color_code_up_down = {}
b3.img_routes = [make_mainline([(100, 100), (100, 104)])]
b3._build_mainline_cache()
b3.is_using_home_route = True
b3.img_route_home = np.zeros((144, 166, 3), dtype=np.uint8)
b3.img_route = b3.img_route_home
b3.home_lines = [{
    "id": 1, "start": (100, 130), "goal": (100, 100), "span": 30, "has_jump": True,
    "n_pts": 2, "far_pts": [], "cover_xs": (100, 100), "cover_span": 1,
    "bbox": (100, 100, 100, 130),
    "xs": np.asarray([100, 100], dtype=np.int32),
    "ys": np.asarray([106, 100], dtype=np.int32),   # 坑底 106 → 主线 100
    "armed": True, "disarm_t": 0.0,
}]
b3.home_enter_t = _real_time.time()      # 注意：本文件把 time import 成了 _real_time
b3.home_frame = 0
b3._home_stand_last_py = None
_sy3 = b3._main_stand_y                  # 主线站立行（本例 = 100）
_exits = []


def _step3(_y):
    b3.loc_player_global = (100, _y)
    b3.home_d_main, b3._main_seg_i = b3._main_dist()
    b3.home_d_home, b3._home_line_near = b3._home_dist()
    return b3.try_return_to_mainline()


# ① 起跳 → 上升 → 冲过站立行 → 第一次落回站立行：全程 y 每帧都在变
for _y in [_sy3 + 6, _sy3 + 5, _sy3 + 4, _sy3 + 3, _sy3 + 2, _sy3 + 1,
           _sy3, _sy3 - 1, _sy3]:
    if _step3(_y):
        _exits.append(_y)
check(f"★ 起跳途中 y 经过 {_sy3 + 1}/{_sy3}/{_sy3 - 1} → **一次都不许**退出回正",
      _exits, [])
check("★ 起跳途中确实仍处于回正状态（没被提前踢出去）",
      b3.is_using_home_route, True)
# ② 落地站定：再来一帧同样的 y（连续两帧相同 = 站稳了）
_step3(_sy3)
check("★ 落地站定（连续两帧同 y）→ 正常退出回正，出口没被堵死",
      b3.is_using_home_route, False)

print("\n【12】回正线接不上（一帧像素都找不到）→ 退出回正，恢复打怪（不能锁死）")
# 用户实测场景：route_home.png 只有安全点那一坨 17 个像素，
# 角色掉到 (98,122)，最近像素在 64px 外（search 10 / rescue 40 都够不着）→
# 指令真空 + 「回正期间不打怪」= 既不走也不打的死局。
b = new_bot(player_global=(98, 122))
b.img_route_home = np.zeros((10, 10, 3), dtype=np.uint8)
b.img_route = b.img_route_home
b.is_using_home_route = True         # 已经在走回正线
b.route_cmd = None                   # 回正线在角色附近一个像素都没有
b.update_cmd_by_route()
check("接不上 → 退出回正状态", b.is_using_home_route, False)
check("没乱走（指令真空）", (b.cmd_move_x, b.cmd_move_y, b.cmd_action),
      ("none", "none", "none"))
b._monsters = [mob(700, 300)]        # 怪在右边（玩家画面坐标 500,300）
b.update_cmd_by_mob_detection()
check("退出后打怪逻辑接管（不再被锁死）", b.cmd_move_x, "right")

print("\n【13】回正线接得上时，回正优先仍然成立（上面的出口不能误伤正常回正）")
b = new_bot(player_global=(98, 122))
b.img_route_home = np.zeros((10, 10, 3), dtype=np.uint8)
b.img_route = b.img_route_home
b.is_using_home_route = True
b.route_cmd = "left none jump"       # 回正线给了明确指令
b.update_cmd_by_route()
check("接得上 → 保持回正状态", b.is_using_home_route, True)
b._monsters = [mob(700, 300)]
b.update_cmd_by_mob_detection()
check("回正指令没被怪拽走", b.cmd_move_x, "left")

print("\n【14】废路线在加载时判废（像素太少 / 无 goal / 缩在一小块）")
b = new_bot()
b.color_code = {(255, 0, 0): "left none none", (255, 255, 0): "none none goal"}
b.color_code_up_down = {}
# 复刻废都南方工地的实际废线：17 个像素，全挤在 6×5 里，没有 goal
bad = np.zeros((187, 268, 3), dtype=np.uint8)
for dy in range(5):
    for dx in range(4):
        bad[54 + dy, 96 + dx] = (255, 0, 0)
for dx in range(3):
    bad[54 + dx, 100 + dx] = (255, 0, 0)
ok, why = b.validate_route_image(bad, "route_home.png")
check("17 像素无 goal → 判废", ok, False)
print(f"       判废原因：{why}")

good = np.zeros((187, 268, 3), dtype=np.uint8)
for i in range(60):
    good[100 + i, 100] = (255, 0, 0)
good[159, 100] = (255, 255, 0)      # goal
ok, why = b.validate_route_image(good, "route_home.png")
check("正常路线（60 像素 + goal）→ 判有效", ok, True)

# 有像素、有 goal，但缩在一小块 = 原地起头原地收尾
tiny = np.zeros((187, 268, 3), dtype=np.uint8)
for i in range(25):
    tiny[100, 100 + i % 3] = (255, 0, 0)
tiny[100, 100] = (255, 255, 0)
ok, why = b.validate_route_image(tiny, "route_home.png")
check("有 goal 但缩在 3px 内 → 判废", ok, False)
print(f"       判废原因：{why}")

print("\n【15】正在打怪 → 看门狗不算卡死（否则打得好好的突然乱走）")
# 2026-09-12：定点删除后，原来那条「站安全点内豁免」没了，全靠这条兜住。
b = new_bot()
b.loc_watch_dog = (100, 100)
b.loc_player_global = (100, 100)     # 一动没动
b.t_watch_dog = _real_time.time() - 30     # 已经静止 30 秒（远超 timeout=10）
b.t_last_attack = _real_time.time() - 1    # 1 秒前刚出过招（< attack_grace=5）
check("刚打过招 → 不算卡死", b.is_player_stuck(), False)
# 上面那次调用会顺带把计时器重置（合理：刚打完不算卡死，重新计时）
b.t_watch_dog = _real_time.time() - 30     # 重新摆成"又静止了 30 秒"
b.t_last_attack = _real_time.time() - 20   # 20 秒没出招了
check("很久没打 → 算卡死（真卡住还得能救）", b.is_player_stuck(), True)

print("\n【16】巡逻闭环检查：最后一段终点 → 第一段起点的距离")
# 段与段之间天然重合（录制器存段后清 last，下一段第一笔从当前位置画起），
# **唯独闭环这一步没人对齐** —— 上游完全没设计，靠像素搜索兜：
#   ≤10px 正常接上 / ≤40px 失联救援 / >40px 接不上会卡在终点
b = new_bot()
b.color_code = {(255, 0, 0): "left none none", (255, 255, 0): "none none goal"}
b.color_code_up_down = {}


def _seg(pts, goal=None):
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    for (x, y) in pts:
        img[y, x] = (255, 0, 0)
    if goal:
        img[goal[1], goal[0]] = (255, 255, 0)
    return img


seg1 = _seg([(100, 100 + i) for i in range(60)])    # 第一段：竖线，起点 (100,100)

b.img_routes = [seg1, _seg([(108, 100 + i) for i in range(40)], goal=(108, 100))]
check("终点离第一段 8px（≤search_range 10）→ 闭环正常", b.check_route_loop_closure(), 8)

b.img_routes = [seg1, _seg([(130, 100 + i) for i in range(40)], goal=(130, 100))]
check("终点离第一段 30px（10~40）→ 只能靠失联救援", b.check_route_loop_closure(), 30)

b.img_routes = [seg1, _seg([(160, 100 + i) for i in range(40)], goal=(160, 100))]
check("终点离第一段 60px（>rescue 40）→ 闭不上环", b.check_route_loop_closure(), 60)

b.img_routes = [seg1]                                # 只有一段 → 不构成闭环问题
check("单段不做闭环检查", b.check_route_loop_closure(), None)

print("\n【17】单段路线：走到终点必须掉头（否则一路冲出平台）")
# 用户实测：只录了 1 段，启动后角色直奔右边不停。
# 原因：idx_routes = (idx+1)%1 恒为 0 → 走到终点仍跟同一段走，
#       而终点附近像素的指令还是"继续往前" → 冲出平台。
seg = _seg([(100 + i, 100) for i in range(60)], goal=(159, 100))   # 横线 A→B
# _route_pixels 靠 color_code 认像素，Stub 默认是空表 → 必须设上
CC = {(255, 0, 0): "right none none", (0, 0, 255): "left none none",
      (255, 255, 0): "none none goal"}
b = new_bot(player_global=(159, 100))
b.color_code = dict(CC)
b.img_routes = [seg]
b.img_route = seg
b._calc_pingpong_start()
check("算出起点（离终点最远的像素）", b._pingpong_start, (100, 100))
check("单段时启用往返", b._pingpong_enabled(), True)
b.is_using_home_route = True
check("走回正线时不启用往返", b._pingpong_enabled(), False)
b.is_using_home_route = False

# 正向走到终点：路线给 goal → 应掉头，且不再发 goal（不然站着不动）
b.route_cmd = "none none goal"
b.update_cmd_by_route()
check("到终点 → 掉头", b.route_reverse, True)
check("goal 被清掉（不再干站着）", b.cmd_action, "none")

# 反向：路线给 right（往终点外），应反转成 left（往回走）
b = new_bot(player_global=(130, 100))
b.color_code = dict(CC)
b.img_routes = [seg]
b.img_route = seg
b._calc_pingpong_start()
b.route_reverse = True
b.route_cmd = "right none none"
b.update_cmd_by_route()
check("反向时左右取反", b.cmd_move_x, "left")
check("还远没到起点 → 不掉头", b.route_reverse, True)

# 反向走到起点附近 → 掉头
b.loc_player_global = (104, 100)     # 离起点 (100,100) 4px ≤ 15
b.update_cmd_by_route()
check("回到起点 → 掉头", b.route_reverse, False)

# 多段时不启用（靠 goal 切段循环）
b = new_bot(player_global=(130, 100))
b.color_code = dict(CC)
b.img_routes = [seg, seg]
b._calc_pingpong_start()
check("多段时不启用往返", b._pingpong_enabled(), False)

print("\n【18】短路线：一帧跨过 goal 也必须切段（否则冲出去停不下来）")
# 用户实测：平台就这么短（路线跨度 14px），走过去却没切段 →
# 冲出去后再找不到像素 → 一路往前。
# ⚠️ 原注释写的是"角色一帧能走十几像素"，那是**推断、从没实测**（2026-09-16 订正：
#    实测 ~0.3 px/帧 ≈ 4~5 px/秒，差 20~50 倍）。本组要验的是"**越过终点也必须切段**"
#    这条规则 —— 它与速度无关，别被那个错数字带偏。
seg1 = _seg([(100 + i, 100) for i in range(12)], goal=(111, 100))   # A→B，很短
# ⚠️ 用 margin=0 的配置：本组只验「一帧跨过 goal 也必须切段」，
#    不要把「终点内缩」（endpoint_margin，见【35】）混进来一起验。
b = new_bot(player_global=(100, 100), cfg=NOMARGIN_CFG)
b.color_code = dict(CC)
b.img_routes = [seg1, seg1]
b.img_route = seg1
b._calc_seg_goals()
check("算出每段终点", b._seg_goals[0], (111, 100))
check("认出本段主方向是向右", b._seg_dir[0], "right")
b.idx_routes = 0
b.cmd_action = "none"                     # 没踩到 goal 像素
check("还在起点 → 不算走到", b._near_seg_goal(), False)
b.loc_player_global = (118, 100)          # 已**越过**终点(111)
check("越过终点 → 算走到（不靠踩 goal）", b._near_seg_goal(), True)
# 上面这次调用会消耗掉"已触发"标记（边沿触发），重新武装后再验一次切段
b._goal_armed = True
b.check_reach_goal()
check("没踩到 goal 也切到下一段", b.idx_routes, 1)

# 边沿触发：切过一次后，还停在终点附近**不能**重复触发 —— 否则两段终点
# 相距小于 goal_reach_range 时会每帧来回切（模拟实测 60 帧切 60 次）。
b.loc_player_global = (120, 100)
# ⚠️ 2026-09-13 语义变更，这里**多验一格**（下面那条老断言保持不变）：
#    老语义是「换段时若已在新段终点区内 → 当作已消费（返回 False）」，
#    逐帧实测证明它会**死锁**（切到段1 后再也切不回来 → 一路走出平台掉下去，
#    帧数据见 _near_seg_goal 的注释）。新语义：进入新段后的第一个 passed=True
#    的帧**必须**触发，所以这里第一次调用是 True。
check("★ 换到新段的第一个 passed 帧 → 必须触发（老写法这里会死锁）",
      b._near_seg_goal(), True)
check("刚切过、还没离开 → 不重复触发", b._near_seg_goal(), False)
b.loc_player_global = (90, 100)           # 退回起点 → 重新武装
check("离开终点范围后重新武装（本次不触发）", b._near_seg_goal(), False)
b.loc_player_global = (118, 100)
check("再次越终点 → 又能触发", b._near_seg_goal(), True)

print("\n【19】完全找不到路线像素 → 停下，不能保持上一帧方向")
# 保持上一帧方向 = 角色一直朝最后一次的方向冲，短路线一冲出去就回不来。
b = new_bot(player_global=(200, 100))
b.color_code = dict(CC)
b.img_routes = []
b.img_route_home = None
b.img_route = np.zeros((10, 10, 3), dtype=np.uint8)   # 空图 → 一个像素都没有
b.cmd_move_x = "right"                    # 预置上一帧的方向
b.cmd_action = "none"
b.update_cmd_by_route()
check("找不到像素 → 停下（不再往前冲）", b.cmd_move_x, "none")

# ══════════════════════════════════════════════════════════════════════
# v0.9 第二版「回正线」新增用例（【20】起）
# 全部挂引擎真实方法，不另写假逻辑（设计 10.15）
# ══════════════════════════════════════════════════════════════════════

print("\n【20】★ 坑底进回正：**主线像素都看得见**也要进（新判定的核心）")
b = new_home_bot((99, PIT_Y))
check("合法 L 形回正线被判有效并加载", len(b.home_lines), 1)
ln = b.home_lines[0]
print(f"       start={ln['start']} goal={ln['goal']} span={ln['span']} "
      f"cover_span={ln['cover_span']} has_jump={ln['has_jump']}")
check("覆盖跨度 15（坑底整条横段）", ln["cover_span"], 15)
# 关键反向用例：此刻主线就在 6px 内（search_range=10 完全够得着），
# 旧版"找不到主线像素才进回正"在这里**永远不会触发**。
check("主线像素确实可见（旧版因此不进回正）",
      b._main_dist()[0], 6)
b.update_cmd_by_route()
check("站到坑底 → 进入回正", b.is_using_home_route, True)
check("img_route 已切到回正线", b.img_route is b.img_route_home, True)
check("记录了进入位置（保险丝用）", b.home_enter_pos, (99, PIT_Y))
check("本帧两个距离都记下来了", (b.home_d_home, b.home_d_main), (0, 6))

print("\n【21】★ 主线上正常巡逻、从回正线正上方经过 → **不**误触发")
b = new_home_bot((99, MAIN_Y))
check("此刻就在主线上（d_main = 0）", b._main_dist()[0], 0)
b.update_cmd_by_route()
check("不进入回正（仍在主线上巡逻）", b.is_using_home_route, False)
check("判定式：6 < 0-2 不成立", home_should_return(6, 0, 2), False)

print("\n【22】一图 2 条线、其中 1 条废 → 只禁那 1 条，另 1 条照常（不连坐）")
b = new_bot()
b.color_code = dict(CC_HOME)
b.color_code_up_down = {}
b.img_routes = [make_mainline([(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)])]
b._build_mainline_cache()
img2 = make_l_home()
for y in range(60, 66):                   # 第二条：只画竖线 → 废线
    img2[y, 150] = JUMP_RGB
img2[60, 150] = GOAL_RGB
b.img_route_home = img2
lines2 = b._scan_home_lines("自检2", img2, b.img_routes)
check("只留下 1 条可用", len(lines2), 1)
check("留下的是画对了的那条（L 形）", lines2[0]["cover_span"], 15)
check("另一条线仍然可用", lines2[0]["armed"], True)

print("\n【23】一帧位移 12px 直接跨过 goal → 仍能正常退出（不会卡在回正里）")
b = new_home_bot((99, PIT_Y))
b.update_cmd_by_route()
check("进回正", b.is_using_home_route, True)
b.loc_player_global = (CORNER_X, MAIN_Y)      # 一步跳到回正线终点（贴着主线）
b.update_cmd_by_route()
b.update_cmd_by_route()                       # 第二帧（站稳判定需连续两帧同 y）
check("到终点（d_home=d_main=0 → 主线优先）→ 退出", b.is_using_home_route, False)
check("退出后 img_route 回到主线", b.img_route is b.img_routes[0], True)

print("\n【24】回正 8s 接不上 → 超时退出 + 打怪接管 + 该线去武装")
b = new_home_bot((99, PIT_Y))
b.img_route = b.img_route_home
b.route_cmd = "left none none"       # 回正线**接得上**（在走），但角色没动窝
b._enter_home_route("自检", b.home_lines[0])
b.home_enter_t = _real_time.time() - 9     # 已经回正 9 秒了（> home_timeout=8）
b.update_cmd_by_route()
check("超时 → 退出回正（有出口，不会锁死）", b.is_using_home_route, False)
check("该线被去武装", b.home_lines[0]["armed"], False)
check("下一帧不会立刻重进（否则 = 8s 回正→重进的死循环）",
      b._home_line_pick(), None)
b._monsters = [mob(700, 300)]
b.update_cmd_by_mob_detection()
check("退出后打怪逻辑接管（不再既不走也不打）", b.cmd_move_x, "right")

print("\n【25】旧回正线色 127,0,127 已彻底删除（不进任何色码表）")
from src.utils.home_route import LEGACY_ANCHOR_RGB, color_code_maps  # noqa: E402
_cc, _ud = color_code_maps({"route": {"color_code": CC_HOME}})
check("色码表只有两张", len(color_code_maps(BASE_CFG)), 2)
check("127,0,127 不在任何色码表里", LEGACY_ANCHOR_RGB in set(_cc) | set(_ud), False)
check("_route_pixels 不再有 include_anchor 形参",
      "include_anchor" in MapleStoryAutoBot._route_pixels.__code__.co_varnames, False)

print("\n【26】角色站在回正线**拐角**像素上 → 取到 jump，不会卡住")
b = new_bot()
b.color_code = dict(CC_HOME)
b.color_code_up_down = {}
b.is_show_debug_window = False
b.img_route_debug = None
b.loc_player_global = (CORNER_X, PIT_Y)
b.img_route = make_l_home()
nearest, _ = MapleStoryAutoBot.get_nearest_color_code(b)   # **引擎真实方法**
check("拐角像素取到的是 jump 色",
      None if nearest is None else nearest["color"], JUMP_RGB)
check("解析出的动作是 jump（不是 none）",
      None if nearest is None else nearest["command"], "none none jump")

# ══════════════════════════════════════════════════════════════════════
# 废线夹具：自己画一条「非 L 形 / 终点没贴主线」的非法回正线
# ══════════════════════════════════════════════════════════════════════
#: 废线①：**只画一条竖线**（非 L 形）—— PRD 2.4 的反例 → 判废④（起始段覆盖跨度 = 1）
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

    Args:
        map_img: 底图（BGR，用来 mask 掉与底图撞色的路线像素）
        cc_dict: 色码表 {RGB: cmd}
        sub: 临时子目录名（中文）
        spec: {"segs": [((x0,y0),(x1,y1),RGB), ...], "goal": (x, y)}

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
            map_img, img, {f"{r},{g},{bl}": v for (r, g, bl), v in cc_dict.items()})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:                                   # 父目录空了就一并清掉，不留空壳
            os.rmdir(os.path.dirname(tmp))
        except OSError:
            pass


print("\n【27】真实资源 + 废线夹具：废线必须被判废，现网回正线**状态无关**")
try:
    from src.utils.common import load_image, mask_route_colors
    from src.utils.home_route import collect_pixels, split_components
    from src.utils.home_route import validate_home_line

    # ⚠️ 读的是**仓库里冻结的夹具**，不是 `minimaps/废都南方工地/` 的活文件（2026-09-16 改）。
    #    活文件是用户随时会重录的：2026-09-16 实测用户重录主线后
    #    route1/route2 从 21/21 像素变成 65/53，本节几条断言当场全红 ——
    #    **红的是数据不是代码**。本节要的是"规则在真实数据上成立"，
    #    不是"那张图此刻长什么样"（同下面 waste_home_image 的道理）。
    #    夹具 = 这些断言当初写定时对着的那份数据，放在 tools/test_fixtures/ 随仓库走。
    img_map = load_image("tools/test_fixtures/废都南方工地/map.png")
    cc_str = {f"{r},{g},{bl}": v for (r, g, bl), v in CC_HOME.items()}
    routes = []
    for name in ("route1.png", "route2.png"):
        r = cv2.cvtColor(load_image(f"tools/test_fixtures/废都南方工地/{name}"), cv2.COLOR_BGR2RGB)
        r = mask_route_colors(img_map, r, cc_str)
        routes.append(r)
    b = new_bot()
    b.color_code = dict(CC_HOME)
    b.color_code_up_down = {}
    b.img_routes = routes
    b._build_mainline_cache()
    check("主线 route1/route2 正常（各 21 像素）",
          sum(len(b._route_pixels(r)) for r in routes), 42)

    # ── ① 废线夹具（自绘，与现网文件无关）────────────────────────────
    for _sub, _spec, _why in WASTE_FIXTURES:
        _waste = waste_home_image(img_map, CC_HOME, _sub, _spec)
        _n0 = len(collect_pixels(_waste, set(CC_HOME)))
        check(f"夹具「{_why}」中文路径落盘→读回后像素还在（{_n0} px）",
              _n0 > 0, True)
        check(f"★ 废线「{_why}」→ 0 条可用（等价于没有回正线）",
              len(b._scan_home_lines("废线夹具", _waste, routes)), 0)
        check(f"★ 废线「{_why}」像素被逐条抹掉",
              len(collect_pixels(_waste, set(CC_HOME))), 0)

    # ── ② 现网 route_home.png：只断言"状态无关"的不变量 ───────────────
    # 用户随时会重画这条线（废→合法、合法→废都正常），所以**不写死几条**：
    # 写死 0 会在画好后红，写死 1 会在重画/删线后红。
    home = cv2.cvtColor(load_image("tools/test_fixtures/废都南方工地/route_home.png"),
                        cv2.COLOR_BGR2RGB)
    check("现网 route_home.png 在中文路径下读得到",
          None if home is None else home.shape, SHAPE + (3,))
    home = mask_route_colors(img_map, home, cc_str)
    got = b._scan_home_lines("废都南方工地", home, routes)
    _left = split_components(home, set(CC_HOME))
    print(f"       现网 route_home.png → 可用回正线 {len(got)} 条"
          f"（扫描后图上剩 {len(_left)} 个连通域）")
    check("★ 扫出来的条数 == 扫描后剩下的连通域数（判废不漏、也不连坐）",
          len(got), len(_left))
    check("★ 扫出来的每一条都是合法回正线（终点贴主线 / 覆盖跨度够）",
          all(validate_home_line(c, home, routes, set(CC_HOME), b.cfg)[0]
              for c in _left), True)
except FileNotFoundError as e:
    print(f"       ⚠️ 跳过：真实资源不可读（{e}）")

print("\n【28】中文路径下回正线扫描不静默失败（load_image 走 imdecode）")
img_probe = np.zeros(SHAPE + (3,), dtype=np.uint8)
img_probe[PIT_Y, CORNER_X] = JUMP_RGB
img_probe[MAIN_Y, CORNER_X] = GOAL_RGB
from src.utils.home_route import collect_pixels  # noqa: E402
check("回正线像素扫得到（坐标精确）",
      sorted(collect_pixels(img_probe, set(CC_HOME))),
      [(CORNER_X, MAIN_Y), (CORNER_X, PIT_Y)])

# ══════════════════════════════════════════════════════════════════════
# ★ 死锁硬断言（设计 §5.4 用例 A~F）
# ══════════════════════════════════════════════════════════════════════

class FakeClock:
    '''虚拟时钟：把"帧"和"秒"解耦，让 8s 超时可以被**确定性**地数帧验证。

    ⚠️ 为什么必须用假时钟：真跑 8 秒太慢，而"每帧都视作已超时"那种加速法
       会让超时在**第一帧**就触发，测不出"8s 内有没有被老路径抵消"。
       用假时钟 + 固定 fps，帧数 ↔ 秒数一一对应，断言才有意义。
    '''

    def __init__(self, fps=30):
        self.t = 10000.0
        self.dt = 1.0 / float(fps)

    def time(self):
        return self.t

    def tick(self):
        self.t += self.dt

    def sleep(self, _s):
        self.t += self.dt

    def perf_counter(self):
        '''性能埋点用（2026-09-14 加）。

        ⚠️ 为什么必须有它：主循环性能埋点调的是 `time.perf_counter()`，
           而本文件把引擎的 `time` **整个**换成了 FakeClock —— 少了这个方法，
           埋点一调用就 AttributeError，【24】之后的用例全线崩。
           假时钟下返回虚拟当前时刻即可（离线用例不关心真实耗时）。
        '''
        return self.t


import src.engine.MapleStoryAutoLevelUp as _ENGINE  # noqa: E402

FPS = 30
HOME_TIMEOUT_S = float(HOME_PARAMS["home_timeout"])
LOCK_S = float(HOME_PARAMS["home_reenter_lock"])
REARM_S = float(HOME_PARAMS["home_rearm_timeout"])
MAX_ALLOWED = int(-(-HOME_TIMEOUT_S * FPS // 1)) + 1      # ceil(8s × fps) + 1

_clock = FakeClock(FPS)
_ENGINE.time = _clock          # 引擎里的 time.time() 全部走虚拟时钟


def deadlock_bot():
    '''坑底 + 回正线就在脚边（接得上像素），但角色**卡住不动**（跳跃失败）。

    这正是 QA-20 的死锁场景：超时退出后角色仍在坑底，
    d_home = 0 < d_main - 2 **仍然成立** → 若无 D1/D2 就会下一帧立刻重进。
    '''
    main_far = make_mainline([(x, 100) for x in range(100, 121)])   # 主线在 y=100
    home_far = np.zeros(SHAPE + (3,), dtype=np.uint8)
    for x in range(94, 109):                       # 横段（坑底 y=170）
        home_far[170, x] = JUMP_RGB
    for y in range(165, 171):                      # 竖段（x=99）
        home_far[y, 99] = JUMP_RGB
    home_far[164, 99] = GOAL_RGB
    b = new_bot(player_global=(99, 170))
    b.color_code = dict(CC_HOME)
    b.color_code_up_down = {}
    b.img_routes = [main_far]
    b.img_route_home = home_far
    b.img_route = main_far
    b._build_mainline_cache()
    # ⚠️ 用**引擎真实**的 get_nearest_color_code（不桩掉像素搜索那一层）
    b.get_nearest_color_code = lambda: MapleStoryAutoBot.get_nearest_color_code(b)
    b.home_lines = [{
        "id": 1, "start": (99, 170), "goal": (99, 164), "span": 6, "has_jump": True,
        "n_pts": 21, "far_pts": [], "cover_xs": (94, 108), "cover_span": 15,
        "bbox": (94, 164, 108, 170),
        "xs": np.asarray([p[0] for p in
                          [(x, 170) for x in range(94, 109)] + [(99, y) for y in range(164, 170)]],
                         dtype=np.int32),
        "ys": np.asarray([p[1] for p in
                          [(x, 170) for x in range(94, 109)] + [(99, y) for y in range(164, 170)]],
                         dtype=np.int32),
        "armed": True, "disarm_t": 0.0,
    }]
    return b


print("\n【29·用例A】★ QA-20 死锁复现：最大连续回正帧数 ≤ ceil(8s×fps)+1")
b = deadlock_bot()
b.loc_player_global = (99, 170)
n_consec = 0
max_consec = 0
n_out = 0
n_attacked = 0
was_home = False          # 上一帧是否在回正态（用来抓"刚退出"那一帧）
lock_at_exit = False      # 退出那一帧有没有立刻上冷静期（★ D1 的直接证据）
# ⚠️ 时长取 21s（8s 回正 + 10s 冷静期 + 3s 余量）：
#    必须**停在这条线还没被 30s 兜底重新武装之前**（t=8+30=38s），
#    否则场景末尾它又进回正了，【31】「该线仍 armed=False」就没法验。
TOTAL = int((HOME_TIMEOUT_S + LOCK_S + 3) * FPS)
for _ in range(TOTAL):
    _clock.tick()
    b.loc_player_global = (99, 170)                    # 角色卡在坑底不动
    b.cmd_move_x = b.cmd_move_y = b.cmd_action = "none"
    b.update_cmd_by_route()
    if b.is_using_home_route:
        n_consec += 1
        max_consec = max(max_consec, n_consec)
        was_home = True
    else:
        # ⚠️ 冷静期必须**在退出那一帧**抓：10s 后会自然到期并把自己清成 0，
        #    跑到场景末尾再看就永远看不到（第一版就是在这儿看漏的）。
        if was_home:
            lock_at_exit = (b.home_reenter_lock_t > 0)
            was_home = False
        n_consec = 0
        n_out += 1
        # 用例B：退出期间打怪逻辑有没有真的接上（不是"既不走也不打"）
        b._monsters = [mob(700, 300)]
        b.update_cmd_by_mob_detection()
        if b.cmd_action == "attack":
            n_attacked += 1
        b.cmd_move_x_last = b.cmd_move_x       # 模拟 hunting.on_frame 的记录
print(f"       模拟 {TOTAL} 帧（fps={FPS}）：处于回正态 {TOTAL - n_out} 帧，"
      f"退出 {n_out} 帧；**最大连续回正 {max_consec} 帧**，上限 {MAX_ALLOWED} 帧")
check(f"★ 最大连续回正帧数 ≤ {MAX_ALLOWED}", max_consec <= MAX_ALLOWED, True)
check("★ 确实出现过「不在回正」的帧（超时真的退出去了）", n_out > 0, True)
check("★ 超时退出那一帧立刻上了冷静期（D1）", lock_at_exit, True)
check("★ 场景跑完仍在外面（没被任何入口拉回）", b.is_using_home_route, False)
check("★ 该线仍是未武装（D2：30s 兜底还没到）", b.home_lines[0]["armed"], False)

print("\n【30·用例B】★ 冷静期内打怪真的接管了（不是「既不走也不打」）")
check("退出回正的帧里出现过 attack 指令", n_attacked > 0, True)

print("\n【31·用例C】★ 冷静期到期解锁，但坏线仍 armed=False → 还是不进（D2）")
b.home_reenter_lock_t = _clock.time() - (LOCK_S + 1)
check("冷静期到期 → 自动解锁", b._home_reenter_allowed(), True)
check("★ 解锁了也不进（那条线还去着武装）", b._home_line_pick(), None)
check("★ 该线确实还是未武装", b.home_lines[0]["armed"], False)

print("\n【32·用例D】去武装兜底：30s 后无条件重新武装（不会永远不回正）")
b.home_lines[0]["disarm_t"] = _clock.time() - (REARM_S + 1)
check("30s 兜底 → 无条件重新武装", b._home_line_armed(b.home_lines[0]), True)
check("重新武装后新入口放行", b._home_line_pick() is not None, True)

print("\n【33·用例E+F】成功退出不留锁 + 超时帧之后不会被老路径拉回")
b2 = new_home_bot((99, PIT_Y))
b2.update_cmd_by_route()
check("进回正", b2.is_using_home_route, True)
b2.loc_player_global = (CORNER_X, MAIN_Y)     # 回主线
b2.update_cmd_by_route()
b2.update_cmd_by_route()                      # 第二帧（站稳判定需连续两帧同 y）
check("成功退出", b2.is_using_home_route, False)
check("★ 成功退出**不留**冷静期", b2.home_reenter_lock_t, 0.0)
check("★ 该线已重新武装", b2.home_lines[0]["armed"], True)
# 用例 F：超时那一帧之后，再跑一帧仍然在外面（老路径不会同帧拉回）
b3 = new_home_bot((99, PIT_Y))
b3.get_nearest_color_code = lambda: MapleStoryAutoBot.get_nearest_color_code(b3)
b3.update_cmd_by_route()                       # 先**真的**从新判定进回正
check("先进入回正（走的是新判定，不是老兜底）", b3.is_using_home_route, True)
b3.home_enter_t = _clock.time() - 100          # 再强制"已经回正了 100s"
_clock.tick()
b3.update_cmd_by_route()                       # 这一帧：超时退出
check("★ 超时出口生效", b3.is_using_home_route, False)
check("★ home_enter_t 被清成 0（不是被重置为本帧）", b3.home_enter_t, 0.0)
check("★ 超时那一帧指令全清成 none（不留 jump / goal）",
      (b3.cmd_move_x, b3.cmd_move_y, b3.cmd_action), ("none", "none", "none"))
_clock.tick()
b3.update_cmd_by_route()                       # 下一帧
check("★ 下一帧**仍然**在外面（老路径没把人拉回）", b3.is_using_home_route, False)
check("该线已去武装（D2）", b3.home_lines[0]["armed"], False)

print("\n【34·加固】★ 缓存失效时 d_home / d_main **一并重算**（不依赖调用方先算 d_main）")
# 场景：**不预先**算 d_main，直接调 `_home_line_pick()`，且缓存里全是"过期值"：
#   · home_d_home = 999   （过期：上一帧的距离，错）
#   · _home_line_near = None（过期：缓存里根本没有线）
#   · home_d_main = 0     （过期：上一帧/上一个位置的主线距离 —— 坑底其实离主线 6px）
# 老实现：只重算 d_home（=0），却**复用** home_d_main=0 → keep(0,0) 不成立 → 返回 None
#         （函数不自洽，悄悄依赖"调用方一定先算过 d_main"这个没写下来的约定）。
# 新实现：两个距离一起重算 → d_home=0、d_main=6 → keep(0,6) 成立 → 正确返回那条线。
b4 = new_home_bot((99, PIT_Y))
b4.home_d_home, b4.home_d_main, b4._home_line_near = 999, 0, None
check("★ 不预先算 d_main、直接调 _home_line_pick() → 仍能正确选中（自洽）",
      b4._home_line_pick() is not None, True)
# 反证：把"复用过期 d_main"的错法复现出来 —— 证明上面那条断言确实有牙齿
check("反证：拿过期 d_main=0 去比 → 会误判成「不进」",
      home_should_return(0, 0, 2), False)

# 正常路径（缓存新鲜）**不该**重算 —— 保证"一并失效"≠"每帧重复算"
b5 = new_home_bot((99, PIT_Y))
_calls = {"n": 0}
_orig_hd = b5._home_dist


def _count_hd():
    _calls["n"] += 1
    return _orig_hd()


b5._home_dist = _count_hd
b5.home_d_home, b5._home_line_near = b5._home_dist()      # 模拟插入点①先算好（计 1 次）
b5.home_d_main, b5._main_seg_i = b5._main_dist()
check("正常路径：缓存新鲜 → _home_line_pick 不重算 d_home（每帧计算量不变）",
      (b5._home_line_pick() is not None, _calls["n"]), (True, 1))

# ══════════════════════════════════════════════════════════════════════
print("\n【35】★ 端点安全余量 endpoint_margin：终点朝**段内**回缩（防走出平台）")
# ══════════════════════════════════════════════════════════════════════
# 背景：主线是「录到哪走到哪」，很多人一路录到平台边缘 → 终点就贴在悬崖上。
#       走到终点才掉头 + 判定有一帧延迟 + 移动惯性 = 踩空掉下平台
#       （用户实测：录制时没事，挂机久了却会走出主线掉下去）。
# 所以把终点朝**段内**收缩 margin px，让角色提前掉头。
#
# 内缩量 = min(margin, max(1, (span - 4) // 2))，只沿**主导轴**收，且不许收出段外。
#    · 上限 margin  ：防「长平台被收掉一大截，巡逻范围白白变短」；
#    · (span-4)//2  ：保底留 3px 巡逻宽度，防两端塌到同一点（= 定点，会原地抖）；
#    · 下限 1       ：防「跨度太小时收成 0，等于没保护」。
#
# ⚠️★ 上限**曾经**是「跨度的 20%」，2026-09-13 被用户实测否决，别改回去：
#    跨度 10~12px 的短路线，20% 只让收 2px，而角色冲过终点的**过冲量正好也是 2px**
#    → 内缩被过冲完全吃掉 → 照样踩到 x=94/104 的斜坡掉进坑。
#    教训：**内缩量必须明显大于过冲量才有效**，按跨度百分比封顶在短路线上必然失效。
#
# ⚠️ 【18】用的是 margin=0 的 NOMARGIN_CFG，所以**本组是 endpoint_margin 的唯一验收点**。


def _seg_goal_with(pts, goal, cfg=None):
    '''造一段路线 → 跑**引擎真实**的 _calc_seg_goals → 返回算出的段终点。'''
    _b = new_bot(player_global=(500, 300), cfg=cfg)
    _b.color_code = dict(CC)
    _b.img_routes = [_seg(pts, goal)]
    _b.img_route = _b.img_routes[0]
    _b._calc_seg_goals()
    return _b._seg_goals[0]


# ① 默认 margin=3、跨度 11 → 上限 (11-4)//2 = 3 → 收满 3 → 终点 111→108
#    防的是「终点贴在平台边缘、走到终点才掉头 → 踩空掉下去」。
check("★ 默认 margin=3、跨度 11 → 收满 3（上限 (11-4)//2=3）→ 111→108",
      _seg_goal_with([(100 + i, 100) for i in range(12)], (111, 100)), (108, 100))

# ② 长段（跨度 30）→ 上限 13 > margin 3 → 收满 3 → 130→127
#    防的是「不按 margin 封顶 → 长平台被收掉一大截，来回范围白白变短」。
check("★ 长段跨度 30 → 上限 13 超过 margin → 只收满 3 → 130→127",
      _seg_goal_with([(100 + i, 100) for i in range(31)], (130, 100)), (127, 100))

# ③ 终点在**左**端 → 必须朝**右**回缩（方向不能写死成「减」）
#    防的是「只处理了终点在右端的情况 → 反向录的路线终点被推出段外」。
check("★ 终点在左端 → 朝右回缩（100→103，方向不写死）",
      _seg_goal_with([(100 + i, 100) for i in range(12)], (100, 100)), (103, 100))

# ④ 极短段（跨度 2）→ 上限 (2-4)//2 为负 → 走下限 1 → 只收 1，且不许收过段起点
#    防的是「跨度太小时收成 0（等于没保护）」+「收到段外 → 永远走不到终点」。
_G_TINY = _seg_goal_with([(100 + i, 100) for i in range(3)], (102, 100))
check("★ 极短段跨度 2 → 上限为负 → 走下限 1 → 102→101", _G_TINY, (101, 100))
check("★ 极短段内缩后仍在段内（不越过起点 x=100）", _G_TINY[0] >= 100, True)

# ⑤ endpoint_margin=0 → 开关**能关掉**：终点一个像素都不动
#    防的是「这个旋钮关不掉」。注意：回正那几个旋钮关不掉是**故意的**（填 0 会复活
#    死锁），但端点内缩只是个可选保护，用户想关必须能关掉。
check("★ endpoint_margin=0 → 终点完全不变（开关能关掉）",
      _seg_goal_with([(100 + i, 100) for i in range(31)], (130, 100),
                     cfg=NOMARGIN_CFG), (130, 100))

# ⑥ 纵向段（y 跨度 > x 跨度）→ 收的是 **y** 而不是 x
#    防的是「只按 x 收 → 竖着的梯子/绳子终点没收，照样掉下去」。
check("★ 纵向段 → 缩 y 不缩 x（(100,111) → (100,108)）",
      _seg_goal_with([(100, 100 + i) for i in range(12)], (100, 111)), (100, 108))

# ⑦ ★★ 回归：短路线必须能**收满** margin（旧版按「跨度 20%」封顶只收 2）
#    2026-09-13 用户实测：本图跨度 10~12px，20% 封顶 → 只收 2px，
#    而角色冲过终点的过冲量正好 2px → 内缩被过冲完全吃掉 → 照样掉进坑。
#    这条钉死「按跨度百分比封顶」这个已被实测否决的设计，别改回去。
check("★ 跨度 12、margin=4 → 收满 4（旧版 20% 封顶只收 2，被过冲吃掉=白缩）",
      _seg_goal_with([(100 + i, 100) for i in range(13)], (112, 100),
                     cfg=MARGIN4_CFG), (108, 100))

# ⑧ 用户把 margin 调爆（8）也不能把两端收到塌成一点 —— 至少留 3px 巡逻宽度。
#    防的是「角色原地抖动 + 彻底失去巡逻意义」（定点模式用户早就因识别问题放弃了）。
check("★ margin 调爆（8）→ 仍封顶在 4，保住 3px 巡逻宽度（112→108 而不是 104）",
      _seg_goal_with([(100 + i, 100) for i in range(13)], (112, 100),
                     cfg=MARGIN8_CFG), (108, 100))

# ══════════════════════════════════════════════════════════════════════════
# 【36】平台边缘**硬**保护 + 切段死锁回归（2026-09-13 用户第 1 轮反馈点名要的）
#
# 背景（用户原话）：「没有平台边缘保护，挂机却会走出主线，掉到平台下面」。
# 前面那些「终点内缩 / reach 封顶」都是**软**保护，靠状态机算得对不对；
# 掉帧、坐标识别抖动、打怪位移都能让角色一帧冲出好几像素 —— 软保护算得再准
# 也追不上。所以要一道**绝对地理围栏**兜底，并且要能自证（日志看得见）。
# ══════════════════════════════════════════════════════════════════════════
print("\n【36】① 换段那一帧 passed=True → _near_seg_goal 必须为 True（死锁回归）")
_d1 = new_bot(player_global=(100, 100), cfg=NOMARGIN_CFG)
_d1.color_code = dict(CC)
_d1.img_routes = [seg1, seg1]          # 两段内容相同 → 切过去后角色仍在终点区内
_d1.img_route = seg1
_d1._calc_seg_goals()
_d1.idx_routes = 0
_d1.loc_player_global = (118, 100)
check("段0 越过终点 → 触发切段", _d1._near_seg_goal(), True)
_d1.idx_routes = 1                     # 换段（角色此刻**已经**在段1 的终点区内）
_d1.loc_player_global = (118, 100)
# ❌ 老写法 `self._goal_armed = not passed` 在这里返回 False → 之后再也切不回段0
#    → 角色一路走出主线掉下平台（逐帧实测跑到 x=81.5，平台在 92~106）。
check("★ 换段那一帧 passed=True → 必须返回 True（老写法会死锁、走出平台）",
      _d1._near_seg_goal(), True)

print("\n【36】② 高速巡逻 600 帧：角色 x 不许超出路线像素的 x 范围")


def _seg_rgb(pts, goal=None, rgb=(0, 0, 255)):
    '''造一段指定颜色的路线图（_seg 只写死红色，这里要蓝色 = left）。'''
    _img = np.zeros((200, 200, 3), dtype=np.uint8)
    for (_x, _y) in pts:
        _img[_y, _x] = rgb
    if goal:
        _img[goal[1], goal[0]] = (255, 255, 0)
    return _img


ROW = [(100 + i, 100) for i in range(31)]                    # x 100~130
SEG_R = _seg_rgb(ROW, goal=(130, 100), rgb=(255, 0, 0))      # 向右，终点在右端
SEG_L = _seg_rgb(ROW, goal=(100, 100), rgb=(0, 0, 255))      # 向左，终点在左端

_hb = new_bot(player_global=(115, 100))
_hb.color_code = dict(CC)
_hb.img_routes = [SEG_R, SEG_L]
_hb.img_route = SEG_R
_hb._calc_seg_goals()
_hb._goal_armed = True
_hb._goal_armed_seg = -1
_seg, _x, _lo, _hi = 0, 115.0, 115.0, 115.0
for _f in range(600):
    _hb.idx_routes = _seg
    _hb.loc_player_global = (int(round(_x)), 100)
    if _hb._near_seg_goal():
        _seg = (_seg + 1) % 2
    _d = _hb._seg_dir[_seg]
    _x += 3.0 if _d == "right" else -3.0     # 每帧 3px（掉帧 / 高速的真实量级）
    _lo, _hi = min(_lo, _x), max(_hi, _x)
print(f"       600 帧 × 3px 后：x 区间 = {_lo:.1f} ~ {_hi:.1f}（路线 x 范围 100~130）")
check("★ 600 帧高速巡逻：x 下限没走出路线", _lo >= 100, True)
check("★ 600 帧高速巡逻：x 上限没走出路线", _hi <= 130, True)

print("\n【36】③ 平台边缘硬围栏 _apply_edge_guard")
#: 围栏关掉的对照配置（验「开关能关掉」，跟 edge_guard_row_tol 一起在【36】验收）
NOEDGE_CFG = copy.deepcopy(BASE_CFG)
NOEDGE_CFG["route"]["edge_guard"] = False


def _guard_bot(px, cmd, cfg=None, home=False):
    '''把角色放到 px、指令设成 cmd，跑一次**引擎真实**的围栏，返回最终指令。'''
    _b = new_bot(player_global=(px, 100), cfg=cfg)
    _b.color_code = dict(CC)
    _b.img_routes = [SEG_R, SEG_L]
    _b.img_route = SEG_R
    _b.is_using_home_route = home
    _b.cmd_move_x = cmd
    _b.loc_player_global = (px, 100)
    _b._apply_edge_guard()
    return _b.cmd_move_x


# 围栏范围 = 角色这一行（y=100）的非 goal 路线像素 → x 100~130
check("围栏范围（y=100 那一行，goal 圆点不算）", _hb._edge_guard_range(100), (100, 130))
check("★ 左边界外还要往左 → 强制改向右", _guard_bot(95, "left"), "right")
check("★ 右边界外还要往右 → 强制改向左", _guard_bot(135, "right"), "left")
check("★ 边界内正常走 → 不干预（往左）", _guard_bot(120, "left"), "left")
check("★ 边界内正常走 → 不干预（往右）", _guard_bot(110, "right"), "right")
# 走回正线时**绝对不能**干预：回正线本来就要往平台外走，
# 一插手就会和回正线的方向打架，角色被按在坑底上不来。
check("★ 走回正线时不干预（否则卡在坑底上不来）",
      _guard_bot(95, "left", home=True), "left")
check("★ edge_guard: false → 不干预（开关能关掉）",
      _guard_bot(95, "left", cfg=NOEDGE_CFG), "left")
# 该行像素 < 2 个 → 不启用围栏（角色在梯子/空中时别把它按在原地）
_gb = new_bot(player_global=(115, 100))
_gb.color_code = dict(CC)
_gb.img_routes = [_seg_rgb([(115, 100)], rgb=(255, 0, 0))]
_gb.cmd_move_x = "left"
_gb.loc_player_global = (115, 100)
_gb._apply_edge_guard()
check("★ 该行只有 1 个像素 → 不启用围栏（防梯子上被误按）", _gb.cmd_move_x, "left")


# ══════════════════════════════════════════════════════════════════════
print("\n【20】★ 回正起跳埋点：只记上升沿 + 换轮清零（2026-09-14 加）")
# 这条在防什么：埋点跑在回正的**唯一出口** _exit_home_route 上，摔不得。
#   ① 若按每帧记数 → 30fps 下角色站在 jump 像素上 1 秒就记 30 次，
#      「走到起跳点几次」这个用来区分病因的关键数字直接报废；
#   ② 若进入回正不清零 → 第二轮的数字是好几轮加起来的**假数**，
#      比没有埋点还糟（会把排查带沟里）。
_tb = new_bot()
_tb.loc_player_global = (98, 126)
_tb.img_routes = []
_tb.img_route_home = np.zeros((10, 10, 3), dtype=np.uint8)  # 没线 _enter 直接 return
_tb._enter_home_route("自检：掉坑")
check("进入回正：计数从 0 开始", _tb.home_jump_n, 0)
check("进入回正：起点已记下", tuple(_tb.home_enter_pos), (98, 126))
# 连续 3 帧都是 jump（角色站在 jump 像素上没动）→ 只能算 1 次
_tb.cmd_action = "jump"
for _ in range(3):
    _tb._trace_home_cmd()
check("★ 连续 3 帧 jump → 只记 1 次（上升沿，不是每帧）", _tb.home_jump_n, 1)
# 松开再踩 → 才算第 2 次
_tb.cmd_action = "none"
_tb._trace_home_cmd()
_tb.cmd_action = "jump"
_tb._trace_home_cmd()
check("松开后再踩 → 记第 2 次", _tb.home_jump_n, 2)
check("y 区间被记下来（看跳没跳上去用）",
      (_tb.home_y_min, _tb.home_y_max), (126, 126))
# 退出 → 必须能生成小结、绝不抛异常
_tb.home_frame = 30
_tb.home_enter_t = _real_time.time() - 1.0
try:
    _tb._exit_home_route("自检退出")
    _exit_ok = True
except Exception as e:                                      # noqa: BLE001
    _exit_ok = False
    print(f"       !! 退出回正抛异常了：{e!r}")
check("★ 退出回正不抛异常（唯一出口，抛了就卡在坑底）", _exit_ok, True)
check("退出后 is_using_home_route 已复位", _tb.is_using_home_route, False)
# 再进一轮 → 计数必须归零
_tb._enter_home_route("自检第二轮")
check("★ 换一轮计数归零（不然第二轮是加起来的假数）", _tb.home_jump_n, 0)
check("换一轮 y 区间也重置", _tb.home_y_min, None)

print("\n【37】★ 超时那一刻若**已站在主线上** → 按成功处理（不再罚 30s）")
# 这条在防什么（2026-09-14 真机实测，18:02:08 与 18:02:33 各一次）：
#   小结写着「(96,126)→(92,122) | 离回正线 5px 离主线 **0px**」
#   —— 角色**就站在主线上**（y=122 正是站立行），却照样走 8s 超时出口、判失败
#   → 回正线被去武装 30s，等于"明明回去了还要罚站 30 秒"，这期间再掉下去没人接。
#   成因：正常退出要求「连续两帧 y 相同」（防空中误判的 E3 硬闸），而角色
#   **跳上去又掉下来**，y 在 122/126 之间反复（小结「期间 y 122~126」），
#   2fps 下根本凑不齐 → 8s 超时抢先触发。
_b5 = new_home_bot((99, PIT_Y))
_b5.update_cmd_by_route()
check("先进入回正", _b5.is_using_home_route, True)
_b5.loc_player_global = (CORNER_X, MAIN_Y)     # 站回主线（但还没凑齐「连续两帧同 y」）
_b5.home_enter_t = _clock.time() - 100         # 强制"已经回正了 100s"
_clock.tick()
_b5.update_cmd_by_route()                      # 这一帧：超时
check("超时后确实退出了回正", _b5.is_using_home_route, False)
check("★ 已站在主线上 → 回正线**保持**可用（没被去武装）",
      _b5.home_lines[0]["armed"], True)
check("★ 已站在主线上 → **不**上冷静期", _b5.home_reenter_lock_t, 0.0)

# 对照组：确实没回去（还在坑底）→ 该罚还得罚。
# 「8s 超时 → 立刻重进」的死循环保护**不能**被这次修改顺手改掉。
_b6 = new_home_bot((99, PIT_Y))
_b6.update_cmd_by_route()
check("（对照组）先进入回正", _b6.is_using_home_route, True)
_b6.home_enter_t = _clock.time() - 100
_clock.tick()
_b6.update_cmd_by_route()
check("（对照组）超时后退出回正", _b6.is_using_home_route, False)
check("★ 还在坑底 → 回正线仍被去武装（防死循环的保护没丢）",
      _b6.home_lines[0]["armed"], False)
check("★ 还在坑底 → 仍然上冷静期", _b6.home_reenter_lock_t > 0, True)

print("\n【38】★ 怪检测降频：每 N 帧才真跑一次（2026-09-14 加）")
# 这条在防什么：一帧 552ms 里怪检测占 **502ms（91%）**，帧率卡在 1.8fps。
# 降频后中间帧沿用上次结果。要钉住三件事：
#   ① 确实少跑了（省 CPU，这是降频的全部意义）；
#   ② 中间帧 monster 列表**不丢**（沿用上次的，不能变空导致打怪逻辑空转）；
#   ③ 降频**能关掉**（=1 时必须每帧都跑）—— 否则写错就会导致永远不打怪，
#      而日志一切正常，属于最难查的一类。
_calls = {"n": 0}


def _counting_mobs(tl, br):      # 引擎调用签名：get_monsters_in_range((x0,y0), (x1,y1))
    _calls["n"] += 1
    return [mob(300, 300)]


_b7 = new_bot()
_b7.cfg = copy.deepcopy(BASE_CFG)
_b7.cfg["monster_detect"]["every_n_frames"] = 2
_b7.get_monsters_in_range = _counting_mobs
for _ in range(6):
    _b7.update_cmd_by_mob_detection()
check("★ 6 帧只真跑了 3 次（每 2 帧一次）", _calls["n"], 3)
check("★ 跳过的帧沿用上次结果 → monster 列表不空（打怪逻辑不会空转）",
      len(_b7.monsters) > 0, True)

_calls["n"] = 0
_b7.cfg["monster_detect"]["every_n_frames"] = 1      # 关掉降频
for _ in range(6):
    _b7.update_cmd_by_mob_detection()
check("★ every_n_frames=1 → 6 帧跑 6 次（降频能关掉，不会永远不打怪）",
      _calls["n"], 6)

print("\n【39】★ 围栏补偿量封顶：窄平台不能出现「两边都不许走」的死区")
# 真机实测（2026-09-14，日志 18:28:34~53 完整复现）：
#   平台只有 **10px**(94~104)，而围栏补偿量算出来 **6px** ——
#   x=99 时 往左到 93（出界）、往右到 105（出界），can_left / can_right
#   **同时为 False** → 角色被按死在原地 → 卡住 10s → 触发脱困随机指令
#   （right down jump）→ 一脚把角色踹下平台。
# ⚠️ 判据：**step >= 半宽 就必然死区**，所以上限必须 < 半宽。
check("span=10 → 上限 4（**小于**半宽 5）", edge_guard_step_cap(6, 10), 4)
check("★ 被撞飞量到的异常值 42px 也被压到 4", edge_guard_step_cap(42, 10), 4)
check("span=30 → 上限 14", edge_guard_step_cap(50, 30), 14)
check("原本就小 → 原样返回（不放大）", edge_guard_step_cap(2, 30), 2)
check("span<=2（窄到站不住）→ 不封顶，交给调用方处理",
      edge_guard_step_cap(9, 2), 9)
check("★ 恒 >= 1", edge_guard_step_cap(0, 10), 1)
check("脏数据（step 非数字）→ 1，不抛", edge_guard_step_cap("x", 10), 1)
check("脏数据（span 非数字）→ 原样返回", edge_guard_step_cap(6, "x"), 6)

# ── 集成：真跑一次**引擎的** _apply_edge_guard ──────────────────────
# 造一个「上一帧被撞飞 50px」的极端历史，看围栏会不会把角色锁死。
_gb2 = new_bot(player_global=(115, 100))
_gb2.color_code = dict(CC)
_gb2.img_routes = [SEG_R, SEG_L]
_gb2.img_route = SEG_R
_gb2._edge_guard_step_hist = [50] * 5      # 模拟被撞飞的异常位移
_gb2._edge_guard_last_px = 115
_gb2.cmd_move_x = "left"
_gb2.loc_player_global = (115, 100)
_gb2._apply_edge_guard()
# 围栏范围 100~130（span=30）→ 封顶到 14；不封顶时 step=50 会把左边拦死
check("★ 异常位移被封顶后，x=115 仍能往左走（不会被按死在原地）",
      _gb2.cmd_move_x, "left")

print("\n【40】★ 回正成功后：先走回安全区中心，再正常巡逻（2026-09-14 加）")
# 现象（用户原话「这一走就又掉下去了」）：角色跳回主线会**横移约 4px**，
# 而回正期间边缘保护是**关掉的** → 落点完全没人管。
# 实测落到 106（安全区 94~104 之外）那次，**同一秒就又掉下去了**。
# 本图围栏范围是 (100,130) → 中心 115。


def _recentered_bot(px):
    '''造一个"刚从回正**成功**退出、人站在 px"的 bot。'''
    _b = new_bot(player_global=(px, 100))
    _b.color_code = dict(CC)
    _b.img_routes = [SEG_R, SEG_L]
    _b.img_route = SEG_R
    _b.loc_player_global = (px, 100)
    _b.cmd_move_x = "none"
    _b._recenter_active = True          # 模拟"回正刚成功"
    _b._recenter_n = 0
    return _b


_br = _recentered_bot(128)              # 落在右边缘（对应真机的 x=106）
_br._apply_recenter()
check("★ 落点靠近右边界 → 朝中心走（往左）", _br.cmd_move_x, "left")

_bl = _recentered_bot(102)              # 落在左边缘（对应真机的 x=96）
_bl._apply_recenter()
check("落点靠近左边界 → 朝中心走（往右）", _bl.cmd_move_x, "right")

_bm = _recentered_bot(115)              # 已经在中心
# ★ 2026-09-15 改：光"到中心"**不再**直接交还控制权 —— 还要"**真的停住**"
#   （连续两帧 x 相同）。原因见 _apply_recenter 的注释：
#   回正是**跑着跳**上来的，跳跃保留水平速度 → 落回平台后还会滑 4~6px。
#   旧逻辑一到中心 ±1px 就交还，人还在滑 → 从平台另一端滑出去 → 又掉进坑
#   → 反向回正 → **来回死循环**（真机实测 19:15 那次 27 秒循环 5 轮）。
#   本图安全区 94~104（10px），滑行量 4~6px ⇒ 交还得太早必然滑出去。
_taken1 = _bm._apply_recenter()
_taken2 = _bm._apply_recenter()
check("★ 已在中心但**还没停稳** → 仍接管一帧（为的是把滑行刹住）", _taken1, True)
check("★ 已在中心且**已停稳**（x 连续两帧相同）→ 不接管指令", _taken2, False)
check("★ 已停稳后 → 归位结束（不会把角色锁死在原地）",
      _bm._recenter_active, False)

# ── 三条退出条件必须都成立，否则就是重演"围栏死区按死角色"那个教训 ──
_bt = _recentered_bot(128)              # 一直走不到中心（比如被怪挡住）
_bt.cfg = copy.deepcopy(BASE_CFG)
_bt.cfg["route"]["home_recenter_frames"] = 3
for _ in range(4):
    _bt._apply_recenter()
check("★ 超过帧数上限 → 放弃归位（防把角色锁死）",
      _bt._recenter_active, False)

_bf = new_home_bot((99, PIT_Y))        # 回正**失败**（超时）的情况
_bf.update_cmd_by_route()
_bf.home_enter_t = _clock.time() - 100
_clock.tick()
_bf.update_cmd_by_route()               # 这一帧：超时失败退出
check("★ 回正失败 → **不**归位（人还在坑底，归位只会跟回正打架）",
      _bf._recenter_active, False)

_bn = new_home_bot((99, PIT_Y))        # 归位途中又掉坑了
_bn._recenter_active = True
_bn.update_cmd_by_route()               # 进回正
check("★ 一进回正就取消归位（两套指令不能打架）",
      _bn._recenter_active, False)

print("\n【41】★ 回正起跳前先停稳（垂直起跳，2026-09-14 加）")
# 现象（用户观察 + 实测吻合）：回正线画的是"走到主线正下方**原地**跳"，
#   但角色是**跑着进来**的，跳跃保留水平速度 →
#   实测「落点 = 起跳点 ± 4~6px」，一偏就贴到平台边缘，再走两步又掉下去。


def _brake_bot(px, cmd_action="jump"):
    '''造一个"回正期间、正要起跳"的 bot。'''
    _b = new_bot(player_global=(px, 126))
    _b.img_route_home = np.zeros((10, 10, 3), dtype=np.uint8)
    _b.is_using_home_route = True
    _b.loc_player_global = (px, 126)
    _b.cmd_action = cmd_action
    _b.cmd_move_x = "left"
    _b._jump_brake_last_px = None
    _b._jump_brake_n = 0
    return _b


_b1 = _brake_bot(102)                   # 第一帧没有位移参照
_b1._apply_jump_brake()
check("第一帧没有参照 → 放行（照常起跳）", _b1.cmd_action, "jump")

_b2 = _brake_bot(100)
_b2._jump_brake_last_px = 102           # 上一帧在 102，现在 100 → 还在动
_b2._apply_jump_brake()
check("★ 还在移动 → 本帧**不**起跳", _b2.cmd_action, "none")
check("★ 还在移动 → 松开方向键（让它停稳）", _b2.cmd_move_x, "none")

_b3 = _brake_bot(100)
_b3._jump_brake_last_px = 100           # 位置没变 → 停稳了
_b3._apply_jump_brake()
check("★ 已经停稳 → 正常起跳", _b3.cmd_action, "jump")

# ── 最要紧的一条：一直停不下来时**必须照跳**，绝不把角色卡死在原地 ──
_b4 = _brake_bot(100)
_b4.cfg = copy.deepcopy(BASE_CFG)
_b4.cfg["route"]["home_jump_brake_frames"] = 2
_b4._jump_brake_last_px = 108
_b4._apply_jump_brake()                 # 第 1 次等待
check("（还在动）第1次等待 → 不起跳", _b4.cmd_action, "none")
_b4.loc_player_global = (98, 126)
_b4._jump_brake_last_px = 100
_b4.cmd_action, _b4.cmd_move_x = "jump", "left"
_b4._apply_jump_brake()                 # 第 2 次等待
check("（还在动）第2次等待 → 不起跳", _b4.cmd_action, "none")
_b4.loc_player_global = (96, 126)
_b4._jump_brake_last_px = 98
_b4.cmd_action, _b4.cmd_move_x = "jump", "left"
_b4._apply_jump_brake()                 # 超上限
check("★ 超过等待上限 → 照跳（绝不把角色卡死）", _b4.cmd_action, "jump")

_b5 = _brake_bot(100)
_b5.is_using_home_route = False         # 正常巡逻中
_b5._jump_brake_last_px = 108
_b5._apply_jump_brake()
check("★ 非回正期间不生效（不能干扰正常巡逻）", _b5.cmd_action, "jump")

# ★★ 真实配置里必须**默认关闭** —— 上面测的是"打开时"的函数行为，
#    但真机上它必须关：① 松开方向键角色仍会滑行，停不下来（实测回正从
#    0.6~3.2s 成功恶化成 8.3s 超时失败）；② **两个平台中间有缺口时必须前跳**，
#    一刀切刹车会让那种地形永远过不去。
_brake_cfg = (load_yaml("config/config_default.yaml").get("route") or {}).get(
    "home_jump_brake")
print(f"       config_default.yaml: route.home_jump_brake = {_brake_cfg!r}")
check("★ 真实配置默认**关闭**（前跳跨缺口的地形不能被刹车破坏）",
      _brake_cfg, False)

print("\n【42】★ 围栏安全余量 edge_guard_inset：把边界往里收（2026-09-14 加）")
# 为什么：角色跳回主线时落点会偏 ±4~6px（跳跃保留水平速度，实测），
#   窄平台上这点偏移就足以把它送出安全区、再走一步就掉下去。
#   把围栏往里收一圈，落点偏了也还在里面，且会被强制走回中间。
# ⚠️ 值必须按地形调（窄平台 2~3、宽平台 0）—— 所以**界面上必须能改**。


def _range_with_inset(inset, px=115, py=100):
    _b = new_bot(player_global=(px, py))
    _b.color_code = dict(CC)
    _b.img_routes = [SEG_R, SEG_L]
    _b.img_route = SEG_R
    _b.cfg = copy.deepcopy(BASE_CFG)
    _b.cfg["route"]["edge_guard_inset"] = inset
    return _b._edge_guard_range(py)


check("inset=0 → 边界不收（老行为，仍是 100~130）",
      _range_with_inset(0), (100, 130))
check("★ inset=2 → 两边各收 2px（102~128）", _range_with_inset(2), (102, 128))
check("inset=5 → 105~125", _range_with_inset(5), (105, 125))
check("★ 收完站不住（inset=20，span 仅 30）→ 不收，保持 100~130（别锁死角色）",
      _range_with_inset(20), (100, 130))
check("非法值（字符串）→ 当 0，不抛", _range_with_inset("x"), (100, 130))
check("负数 → 当 0，不抛", _range_with_inset(-3), (100, 130))

_inset_cfg = (load_yaml("config/config_default.yaml").get("route") or {}).get(
    "edge_guard_inset")
print(f"       config_default.yaml: route.edge_guard_inset = {_inset_cfg!r}")
check("★ 真实配置默认 2（窄平台需要；宽平台用户自己在界面改 0）", _inset_cfg, 2)

# ── 界面必须能改到它（用户明确要求：窄平台的值不能套用到宽平台）──
try:
    from src.ui.ui import ADV_SETTINGS_HIDE, ADV_FIELD_HIDE
    check("★ 界面「整段隐藏」表里**没有** route（否则用户根本看不到这个参数）",
          'route' in ADV_SETTINGS_HIDE, False)
    check("★ edge_guard_inset **没被**段内隐藏（必须能在界面上直接改）",
          'edge_guard_inset' in (ADV_FIELD_HIDE.get('route') or []), False)
except Exception as _e:                                     # noqa: BLE001
    check(f"界面隐藏表可导入（否则无法确认参数能否被调）：{_e}", False, True)

print("\n【43】★ 窄平台降级：路太窄就不来回蹭、也不判卡死（2026-09-15 加）")
# 为什么：本图巡逻线只有 10px 长、两段终点只差 4px → 切段判定的容差被
#   `(gap-2)//2` 压到 1px → 段0 在 x>=100 触发、段1 在 x<=98 触发
#   → 角色只能在 98~100 之间**来回蹭 2px**（既走不动，又每 10s 触发一次看门狗
#   "卡死" → 随机脱困指令把角色踹到别处；实测随机到 jump，跳到上一层平台）。
# 降级后：① 不发路线给的左右移动 ② **画面里没有怪**时不判卡死
#        （用户 2026-09-15 明确：附近有怪却不打 = 真的卡死，照样救）。
from src.utils.common import route_too_narrow_for_patrol  # noqa: E402

# ── 纯函数 ──────────────────────────────────────────────────────────
check("gap=4 < 阈值24 → 降级（本图实测值）",
      route_too_narrow_for_patrol(4, 24), True)
check("gap=10（不内缩时的本图）< 24 → 也降级",
      route_too_narrow_for_patrol(10, 24), True)
check("gap=100 → 不降级（正常长度的图）",
      route_too_narrow_for_patrol(100, 24), False)
check("★ 单段路线 gap=0 → **永远不降级**（靠 pingpong 走完全程，本来就不蹭）",
      route_too_narrow_for_patrol(0, 24), False)
check("★ 阈值填 0 = 关掉降级 → 不降级",
      route_too_narrow_for_patrol(4, 0), False)
check("阈值负数 → 不降级", route_too_narrow_for_patrol(4, -5), False)
check("★ 非法值（字符串）→ 不降级、不抛（配置写错不能让全世界的图都不巡逻）",
      route_too_narrow_for_patrol("x", 24), False)
check("gap 恰好 == 阈值 → 不降级（严格小于）",
      route_too_narrow_for_patrol(24, 24), False)


def _degraded_bot(gap, cfg=None):
    _b = new_bot(cfg=cfg)
    _b._seg_goal_gap = gap
    return _b._patrol_degraded_now()


check("★ 引擎：gap=4 + 真实配置 → 降级", _degraded_bot(4), True)
check("引擎：gap=200 → 不降级", _degraded_bot(200), False)

_deg_cfg = copy.deepcopy(BASE_CFG)
_deg_cfg["route"]["patrol_min_gap"] = 0
check("★ 引擎：阈值填 0 → 降级被关掉", _degraded_bot(4, _deg_cfg), False)

_pg_cfg = (load_yaml("config/config_default.yaml").get("route") or {}).get(
    "patrol_min_gap")
print(f"       config_default.yaml: route.patrol_min_gap = {_pg_cfg!r}")
check("★ 真实配置有这个键（且 > 0，功能是开着的）",
      isinstance(_pg_cfg, int) and _pg_cfg > 0, True)


# ── 集成①：降级时**不发**路线给的左右移动 ──────────────────────────
def _narrow_route_cmd(gap):
    _b = new_bot()
    _b.cfg = copy.deepcopy(BASE_CFG)
    _b._seg_goal_gap = gap
    _b.route_cmd = "right none none"       # 桩：路线说"向右走"
    _b.update_cmd_by_route()
    return _b.cmd_move_x


check("★ 降级时：路线说 right，但引擎**不发**左右移动（不再来回蹭）",
      _narrow_route_cmd(4), "none")
check("不降级时：路线说 right 就照发（老行为没被改坏）",
      _narrow_route_cmd(200), "right")


# ── 集成①b：★ 回正期间**绝不能**降级（吞掉回正线的左右 = 走不回主线）──────
# 这条是 2026-09-15 实测抓到的：窄平台降级刚写完时没加 is_using_home_route 短路，
# 结果主图窄 → 降级 → 连**回正线**的左右也被吞 → 角色走不回主线。
# verify_home_route_qa / qa2 立刻报「起点 (94,128)：走回主线并退出」失败。
# （跟 _apply_edge_guard 里那条 `is_using_home_route` 短路是同一个道理。）
def _narrow_home_cmd(gap):
    _b = new_bot(player_global=(98, 122))
    _b.cfg = copy.deepcopy(BASE_CFG)
    _b.img_route_home = np.zeros((10, 10, 3), dtype=np.uint8)
    _b.img_route = _b.img_route_home
    _b.is_using_home_route = True
    _b._seg_goal_gap = gap
    _b.route_cmd = "left none jump"        # 回正线给的明确指令
    _b.update_cmd_by_route()
    return _b.cmd_move_x


check("★★ 降级中但**正在走回正线** → 不吞左右（否则角色走不回主线）",
      _narrow_home_cmd(4), "left")


# ── 集成②：降级时"没怪"才豁免卡死；有怪不打 = 真卡死 ────────────────
def _stuck_after_11s(gap, monsters):
    _b = new_bot()
    _b.cfg = copy.deepcopy(BASE_CFG)
    _b._seg_goal_gap = gap
    _b._patrol_degraded = _b._patrol_degraded_now()   # 模拟 update_cmd_by_route 先跑过
    _b.monsters = list(monsters)
    _b.loc_player_global = (100, 100)
    _b.loc_watch_dog = (100, 100)          # 位置一点没变
    _b.t_watch_dog = _clock.time() - 11    # 已经 11 秒没动
                                           # ⚠️ 必须用 _clock（引擎的 time 在 1073 行
                                           #    被整个换成了 FakeClock）；用真实 time
                                           #    算出来是巨大负数 → dt > timeout 恒假，
                                           #    用例会**假通过/假失败**（本次就踩了）。
    _b.t_last_attack = 0.0                 # 很久没打怪 → attack_grace 不豁免
    return _b.is_player_stuck()


check("★ 降级 + 画面无怪 → **不判卡死**（站着不动是预期行为）",
      _stuck_after_11s(4, []), False)
check("★★ 降级 + 附近有怪却不打 → **仍然判卡死**（用户：这种就是真的卡死）",
      _stuck_after_11s(4, [mob(300, 300)]), True)
check("不降级 + 无怪 → 照常判卡死（老行为没被改坏）",
      _stuck_after_11s(200, []), True)


# ══════════════════════════════════════════════════════════════════════
print("\n【44】★ 卡死时不再乱动：默认弹置顶提示（2026-09-15 加）")
# ══════════════════════════════════════════════════════════════════════
# 背景（用户实测判定旧行为"没有好处只有坏处"）：
#   ① `update_cmd_by_random` 是 12 种组合**均匀乱抽**，完全不知道悬崖在哪 ——
#      窄平台上乱抽 = 直接掉下去（实测抽到 `right none jump`，右边正好是悬崖）；
#   ② 它把真因**盖住**了：随机跳把角色从 (100,122) 踹到 (100,118)，
#      症状从"在原地蹭"变成"跳到另一层"，日志上看"它动了"，反而更难查。
# ⇒ 默认改成 `alert`：**不乱动**（保持本帧逻辑给的指令）+ 弹置顶提示窗。
#
# ⚠️ 自检里**绝不能真弹窗**（跑自检的人会被卡住）→ 把 notify_stuck 换成记录器。
from src.utils import stuck_alert as _sa          # noqa: E402

_alert_calls = []
_orig_notify = _sa.notify_stuck
_sa.notify_stuck = lambda **kw: (_alert_calls.append(kw), False)[1]


def _stuck_bot(mode="alert", cmd=("right", "none", "none"), with_key=True):
    _b = new_bot()
    _b.cfg = copy.deepcopy(BASE_CFG)
    _b.cfg.setdefault("watchdog", {})
    if with_key:
        _b.cfg["watchdog"]["on_stuck"] = mode
    _b.cmd_move_x, _b.cmd_move_y, _b.cmd_action = cmd
    _b.loc_player_global = (99, 122)
    _b.stuck_seconds = 11.0
    _b.monsters = []
    _b._patrol_degraded = False
    return _b


# ① alert（默认）：**不乱动** + 告警
_b = _stuck_bot("alert")
_b.handle_stuck()
check("★★ alert：指令**原样不动**（不再乱抽）",
      (_b.cmd_move_x, _b.cmd_move_y, _b.cmd_action), ("right", "none", "none"))
check("★ alert：调了告警，且带上位置 / 已卡秒数 / 当时指令",
      (len(_alert_calls), _alert_calls[-1].get("pos"),
       _alert_calls[-1].get("seconds"), _alert_calls[-1].get("cmd")),
      (1, (99, 122), 11.0, "right none none"))

# ② 缺键 / 非法值 → 一律回落 alert（宁可不动也别乱动）
_alert_calls.clear()
_b = _stuck_bot("alert", with_key=False)
_b.handle_stuck()
check("★ 缺 on_stuck 键 → 回落 alert（不乱动 + 告警）",
      ((_b.cmd_move_x, _b.cmd_action), len(_alert_calls)), (("right", "none"), 1))

_alert_calls.clear()
_b = _stuck_bot("乱写的值")
_b.handle_stuck()
check("★ on_stuck 取值非法 → 回落 alert（不乱动）",
      ((_b.cmd_move_x, _b.cmd_action), len(_alert_calls)), (("right", "none"), 1))

# ③ random：老行为**保留**（换图时可以先试 random 对比，别把路堵死）
_alert_calls.clear()
_b = _stuck_bot("random")
_rand_called = []
_b.update_cmd_by_random = lambda: _rand_called.append(1)
_b.handle_stuck()
check("★ random：仍调 update_cmd_by_random（老行为保留，换图可比对）",
      len(_rand_called), 1)
check("★ random：**不**弹窗（老行为不告警）", len(_alert_calls), 0)

# ④ none：既不乱动也不告警
_alert_calls.clear()
_b = _stuck_bot("none")
_b.handle_stuck()
check("★ none：既不乱动也不告警",
      ((_b.cmd_move_x, _b.cmd_action), len(_alert_calls)), (("right", "none"), 0))

# ⑤ 告警模块自身：非 Windows / 冷却 / 单实例 都要安全（不能把主循环带崩）
check("★ 告警模块：_reset_for_test 可清状态（幂等、不抛）",
      (_sa._reset_for_test() is None), True)

_sa.notify_stuck = _orig_notify

print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
