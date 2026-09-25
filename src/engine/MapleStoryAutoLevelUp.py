'''
Execute this script:
python mapleStoryAutoLevelUp.py --map cloud_balcony --monster brown_windup_bear,pink_windup_bear
'''
# Standard import
import time
import random
import argparse
import glob
import sys
import logging
import os
import datetime
import threading
import shutil
import tempfile
import traceback

# Library import
import numpy as np
import cv2
import yaml

# Local import
from src.utils.logger import (
    logger, jump_trace_on, perf_trace_on, perf_summary_line)
from src.utils.paths import resource_path, ensure_dir
from src.utils.common import (find_pattern_sqdiff, draw_rectangle, screenshot, nms,
    load_image, get_mask, get_minimap_loc_size, get_player_location_on_minimap,
    override_cfg, load_yaml,
    click_in_game_window, mask_route_colors,
    activate_game_window, normalize_pixel_coordinate,
    get_window_client_size, crop_frame_to_client, put_text_cn,
    monster_detect_interval, edge_guard_step_cap,
    route_too_narrow_for_patrol,
)
from src.input.KeyBoardController import KeyBoardController, press_key
from src.input.KeyBoardListener import KeyBoardListener
from src.input.GameWindowCapturor import GameWindowCapturor
from src.engine.FiniteStateMachine import FiniteStateMachine
from src.states.hunting import HuntingState
# ── 回正线领域层（v0.9 第二版「谁近走谁」）──────────────────────────────────
# 连通域划分 / 几何现算 / 覆盖检查 / 逐条判废 / 距离 全是纯函数，
# 放在 utils 里是为了让「引擎跑的」和「离线自检测的」是**同一份代码**。
from src.utils.home_route import (
    manhattan, home_params, dist_to_pixels, home_should_return,
    split_components, collect_pixels,
    calc_line_geom, far_segment_stats, cover_stats, validate_home_line,
    home_pixel_clash, wipe_legacy_anchor, DEFAULT_HOME_PARAMS,
    home_summary_line,
)
# ── 卡死告警（2026-09-15 加）：判定卡死后**弹置顶提示窗**，不再乱动 ──────────
# 见 config_default.yaml 的 `watchdog.on_stuck` 与 src/utils/stuck_alert.py 的文件头。
from src.utils import stuck_alert

class MapleStoryAutoBot:
    '''
    MapleStoryAutoBot
    '''
    def __init__(self, args):
        '''
        Init MapleStoryAutoBot
        '''
        self.args = args # User args
        self.cfg = None # Configuration
        self.idx_routes = 0 # Index of route map
        self.monsters_info = {} # monster information
        self.monsters = [] # monster detected in current frame
        self.fps = 0 # Frame per second
        self.video_writer = None # For video recording feature
        self.video_tmp_path = None   # 实际写入的临时文件（纯 ASCII 路径）
        self.video_final_path = None # 最终要落到的 video/ 位置
        self.color_code = {} # For color code instruction
        self.color_code_up_down = {} # Color code only contain 'up' and 'down'
        self.thread_auto_bot = None # thread for running autobot
        self.cmd_move_x = "none" # "left" "right"
        self.cmd_move_y = "none" # "up" "down"
        self.cmd_action = "none" # "jump" "attack" ....
        # 失联救援锚点：最近一次成功匹配的路线色码像素（get_nearest_color_code 维护）
        self.loc_last_route_pixel = None
        # Signals (for UI)
        self.image_debug_signal = None
        self.route_map_viz_signal = None
        # Flags
        self.is_first_frame = True # first frame flag
        self.is_terminated = False # Close all object and thread if True
        self.is_on_ladder = False # Character is on ladder or not
        self.is_show_debug_window = not args.disable_viz #
        self.is_need_show_debug_window = not args.disable_viz #
        self.is_disable_control = args.disable_control
        self.is_ui = args.is_ui # Whether is using UI framework to invoke engine
        self.is_frame_done = False #
        # Coordinate (top-left coordinate)
        self.loc_nametag = (0, 0) # nametag location on game screen
        self.loc_minimap = (0, 0) # minimap location on game screen
        self.loc_player = (0, 0) # player location on game screen
        # 名字定位健康度（2026-09-10 加）：连续多少帧没匹配上角色名名牌。
        # 匹配失败时 loc_nametag 保持旧值 → 位置会「冻住」却毫无提示，
        # 所以由主循环在连续失败时告警（见 update 里的 [名字定位] 告警）。
        self.nametag_miss_streak = 0
        self.nametag_hit = False
        # 开局至今有没有成功命中过。用来区分两种严重程度完全不同的情况：
        #   命中过再失败 → 无害（同图内角色屏幕位置几乎是死的，沿用上次结果即可）
        #   一次都没命中 → 严重（位置停在初始值，攻击框跑到画面左上角，且不报错）
        self.nametag_ever_hit = False
        self.loc_player_minimap = (0, 0) # player location on minimap
        self.loc_minimap_global = (0, 0) # minimap location on global map
        self.loc_player_global = (0, 0) # player location on global map
        self.loc_watch_dog = (0, 0) # watch dog location on global map
        # Images
        self.frame = None # raw image
        self.img_frame = None # game window frame
        self.img_frame_gray = None # game window frame graysale
        self.img_frame_debug = None # game window frame for visualization
        self.img_route = None # route map
        # 回正线（minimaps/<图>/route_home.png）：偏离主线 / 掉出平台后走的「回家的路」。
        # 单独一份、不进 img_routes —— 主线是巡逻/驻守路径，回正线是回到主线的路径，
        # 两者语义不同，混用会导致「掉出去后沿巡逻路线走，反而越走越远」。
        # 2026-09-12 新增（思路与上游 legacy 的 route_rest.png 相同）。
        self.img_route_home = None
        self.is_using_home_route = False   # 当前是否在走回正线
        # ── v0.9 第二版回正跨帧状态（全部只在 _enter/_exit_home_route 里维护）──
        # ⚠️ 纪律：新增状态必须配一个明确出口，否则会造出「既不走也不打」的死局。
        #    这些字段的出口就是 _exit_home_route（正常回主线 / 接不上 / 8s 超时 三条路）。
        self.home_lines = []        # list[dict]：加载期从 route_home.png 扫出的回正线（已剔废线）
        self.home_line = None       # 当前这次回正走的是哪条线（dict | None）
        # 【新】本帧的两个距离 —— **每帧只在 update_cmd_by_route 插入点①算一次**，
        # 插入点②（超时 / 驻留 / try_return）只读不算，保证一帧内口径唯一。
        self.home_d_home = None     # 到最近「已武装」回正线的距离（None = 没有可用回正线）
        self.home_d_main = None     # 到最近主线像素的距离（None = 量不出 / 没有主线）
        self._home_line_near = None # 插入点①缓存：最近的那条已武装回正线
        self._main_seg_i = 0        # 插入点①缓存：最近主线像素属于第几段（退出时切回它）
        self._main_pts_xy = None    # (xs, ys) np.int32：主线全部色码像素（load 时建一次）
        self._main_pts_seg = None   # 每个主线像素属于第几段（与上面等长）
        self.home_enter_pos = None  # 进入回正时的角色全局坐标 (x, y)（最小驻留门用）
        self.home_enter_t = 0.0     # 进入回正的时刻（8s 超时出口用；0 = 没在回正）
        self.home_frame = 0         # 已处于回正状态的帧数（最小驻留门用）
        self.home_last_goal_log = 0.0   # 「踩到回正线终点」日志节流
        # ⚠️ 回正**失败**后的入口冷静期（QA 实测必修，见 _exit_home_route 的说明）：
        #    没有它，超时退出的那一帧会被「主线附近找不到像素」的老路径**立刻**拉回
        #    回正状态 → home_enter_t / home_frame 归零 → 8s 超时永远走不到 → 死循环。
        self.home_reenter_lock_t = 0.0  # 上次"回正失败"的时刻；0 = 没锁
        self.t_last_home_lock_log = 0.0 # 「冷静期内不回正」日志节流
        self.img_route_debug = None # route map for visualization
        self.img_minimap = np.zeros((10, 10, 3), dtype=np.uint8) # minimap on game screen
        # Timers
        self.t_last_frame = time.time() # Last frame timer, for fps calculation
        self.t_watch_dog = time.time() # Last movement timer
        self.t_last_attack = time.time() # Last attack timer for cooldown
        self.t_last_minimap_update = time.time()
        # 运行状态日志（2026-09-12 加）：主循环每 5 秒打一行关键状态。
        # ⚠️ 为什么必须有这一行：主循环原来**一帧不打日志**，于是「点开始、角色不动」
        #    这件事在日志里完全查不到 —— 用户原话「日志里啥都没有」。
        #    有了它，「循环在跑但指令是 none」和「循环已经死了」一眼可分。
        self.n_frame = 0 # 主循环已处理帧数
        self.t_last_status_log = time.time() # 上次打运行状态的时间
        self.n_loop_err = 0 # 主循环连续异常帧数
        self.t_last_loop_err = 0.0 # 上次打异常告警的时间（节流用）
        # ── 主循环性能埋点（2026-09-14 加，见 _perf 与 perf_summary_line）────
        # 背景：配置 fps_limit_main=30，**真机实测只有 1.8~2 fps**，而全项目原来
        # 没有任何地方记录主循环的实际帧率（键盘线程会打印 fps=，主循环不会）
        # → "一帧 500ms 到底花在哪"完全靠猜。这几个累加器就是量它用的。
        # perf_on 在 load_config 末尾用 perf_trace_on(cfg) 赋**一次**（别每帧 try）。
        self.perf_on = False        # 只影响打不打日志，不影响任何判定与行为
        self.perf_acc = {}          # {阶段名: 本窗口累计秒}
        # ⚠️ 实际执行次数：怪检测会**降频**（每 N 帧才跑一次），它的次数 < 帧数。
        #    汇总时用**它**当分母，日志里显示的才是"跑一次要多久"，
        #    而不是被摊薄后的假数字（见 _perf / 汇总处）。
        self.perf_cnt = {}          # {阶段名: 实际执行次数}
        self.perf_max = {}          # {阶段名: 单帧峰值秒}
        self.perf_n = 0             # 本窗口帧数（在 loop 里 +1，不在 run_once）
        self.perf_gap = 0.0         # 本窗口帧间隔累计秒（含限帧 sleep）
        self.t_last_perf_log = time.time()
        self.t_perf_prev = time.time()

        # 上一帧**实际发出**的左右指令（2026-09-12 加）
        # ⚠️ 为什么需要它：出招必须**先转身再按技能键**，否则技能会朝旧朝向飞出去，
        #    表现就是「后方的怪打不到」。上游 config 里躺着 character_turn_delay: 0.02，
        #    但整个 src/ 里一处引用都没有 —— 是没做完的半成品，这里把它补上。
        # 由 hunting.on_frame 在 set_command 之后更新。
        self.cmd_move_x_last = "none"
        # ★ 2026-09-17：转身状态机（「回头打怪」用，见 update_cmd_by_mob_detection）
        #   _turn_frames_left：还要按住方向键**转向**多少帧（>0 时不出招）
        #   _turn_dir        ：上一帧转向的目标方向（怪换边时重新计时）
        #
        #   为什么需要它：原来「朝向不对 → 本帧只发方向键、下一帧就出招」，
        #   转向只维持 1 帧（≈33ms）。而**出招那一帧仍然按着方向键**
        #   —— 技能是按「当前朝向」飞出去的，同帧按方向+技能，技能会朝**转身前**
        #   的朝向飞走。于是每次攻击都朝路线前进方向甩出去，后方的怪永远打不到，
        #   表现就是用户说的「后面有怪却一直在往前面砍空气」。
        self._turn_frames_left = 0
        self._turn_dir = None
        # 上次打「回正线接不上」告警的时间（节流用，避免每帧刷屏）
        self.t_last_home_fail_log = 0.0
        # 单段路线自动往返（2026-09-12 加）：只录了一段时，走到头自动掉头往回走。
        # 详见 _apply_pingpong 的说明。
        self.route_reverse = False
        self._pingpong_start = None
        # 每一段的终点（goal 圆点的质心）+ 主方向 + 跨度 —— 用来判断"这段走完了"。
        # ⚠️ 不能只靠"踩到 goal 像素"：goal 半径才 2px，而玩家坐标是**质心**、
        #    踩不中就判不到 —— 短路线（平台就那么短）会直接走过去 → 永远不切段 →
        #    冲出平台（用户实测：到终点停不下来一直往前走）。
        #    ⚠️★ 2026-09-16 订正：这里原来写的是"角色一帧能走十几像素"，**那是推断、
        #       从没实测**，实测只有 ~0.3 px/帧（≈4~5 px/秒，实测依据见 _near_seg_goal
        #       里"上限三"那段），差了 20~50 倍。症状是真的，但这个数字是猜的 ——
        #       别再拿它当依据（它正是 goal_reach_range=15 的来源，而那 15px 后来
        #       把短路线的巡逻行程吞成了 2px）。
        #       至于"踩不中"的真正机理：没查实（可能是坐标取整/质心偏差），
        #       但"越过终点也算到"（方向感知）这个修法与速度无关，是对的。
        self._seg_goals = []
        self._seg_dir = []
        self._seg_span = []
        self._goal_armed = True
        # Images
        self.img_map = None
        self.img_routes = []
        self.img_nametag = None
        self.img_nametag_gray = None
        self.img_login_button = None

        # Database
        self.data = load_yaml("config/config_data.yaml")
        # Threads & Objects
        self.kb = None # Keyboard controller
        self.capture = None # Game window capturor

        # Finite State Machine
        # 国服怀旧服无符文系统（已核实客户端元数据）；巡逻模式已移除，状态机只剩「打怪」一态
        self.fsm = FiniteStateMachine()
        self.fsm.add_state(HuntingState    ("hunting"     , self))
        self.fsm.set_init_state("hunting")

    def update_signals(self, image_debug_signal, route_map_viz_signal):
        '''
        Update signal from UI framework.
        For debug window viz
        '''
        self.image_debug_signal = image_debug_signal
        self.route_map_viz_signal = route_map_viz_signal

    def _check_cfg_completeness(self, cfg):
        '''
        拿 config_default.yaml 当基准，检查传进来的 cfg 有没有缺键，缺了就报 ERROR。

        ⚠️ 为什么必须查（2026-09-12 实测踩坑，代价是一整天的排查）：
           界面保存的用户配置是「局部配置」——只写用户改过的键。一旦某条路径把某个段
           **整段替换**成局部版本，config_default 里的默认键就整段丢了。
           本次实例：nametag 段只剩 name/offset，引擎第一帧读 cfg["nametag"]["mode"]
           → KeyError → 主循环线程**静默死亡** → 症状「点开始、角色不动、日志全静默」。
           在这里把它报成 ERROR，就把「静默崩溃」变成了「开跑第一眼就能看见」。

        返回缺失键列表（空列表 = 配置完整）。
        '''
        try:
            base = load_yaml("config/config_default.yaml")
        except Exception as e:
            logger.warning(f"[配置校验] 读不到 config_default.yaml，跳过校验: {e}")
            return []
        missing = []

        def walk(b, c, path):
            for k, v in b.items():
                p = f"{path}.{k}" if path else k
                if not isinstance(c, dict) or k not in c:
                    missing.append(p)
                elif isinstance(v, dict):
                    walk(v, c[k], p)

        walk(base, cfg, "")
        if missing:
            shown = ", ".join(missing[:12])
            more = f" …（共 {len(missing)} 个）" if len(missing) > 12 else ""
            logger.error(
                f"[配置校验] 配置缺少 {len(missing)} 个必需键：{shown}{more}\n"
                f"        这些键本该由 config/config_default.yaml 提供 —— 说明保存配置时"
                f"把默认值丢了（典型原因：用户配置只写了部分键，合并时按「整段替换」处理）。\n"
                f"        不修的话引擎会在运行中因缺键崩溃，症状是「角色不动 + 日志突然静默」。")
        return missing

    def _route_pixels(self, img):
        '''收集路线图上的色码像素坐标（validate_route_image / 闭环检查共用）。

        ⚠️ 第二版只有**两张**色码表（color_code / color_code_up_down）：
           第一版的「落点锚」第三张表已删除，这里不再需要 include_anchor 形参。
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        hits = []
        for y in range(img.shape[0]):
            row = img[y]
            for x in range(img.shape[1]):
                if tuple(row[x][:3]) in codes:
                    hits.append((x, y))
        return hits

    def check_route_loop_closure(self):
        '''
        巡逻闭环检查（2026-09-12 加）：最后一段的终点，能不能接回第一段？

        段与段之间本来不需要容差 —— 录制器存完一段会 `_reset_route_canvas()`
        清掉 `loc_player_global_last`，下一段的第一笔就从**按 F3 那一刻站的位置**
        开始画，天然重合。

        **唯独闭环那一步没人对齐**：最后一段的终点 A′ 走完 goal 后循环回第一段，
        而第一段的起点 A 是**录制时**你站的那个位置，A′ 是你走回来的位置，
        两者不可能完全重合。上游对这一步**完全没有设计**，全靠引擎通用的像素搜索兜：

            ≤ search_range(10px)  正常接上，无感
            ≤ rescue_range(40px)  靠失联救援接上，日志打「路线失联」
            > 40px                接不上 → 指令真空 → 干站 → watchdog 抽随机动作乱走

        所以这里在**加载时**把这个距离量出来告诉用户（同样是"加载时报错"而不是
        "运行时瞎猜"的套路），省得跑起来卡在终点才发现。

        Returns:
            int or None: 最后一段终点到第一段最近像素的曼哈顿距离；
                         段数不足 / 缺 goal / 第一段无像素时返回 None（不做检查）。
        '''
        if len(self.img_routes) < 2:
            # 单段路线不构成"闭环"问题（但会在终点停住，见 check_reach_goal）
            return None
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        if not goal_codes:
            return None
        # 最后一段的 goal 像素 → 取质心（stamp 是个圆点，质心足够代表"终点"）
        last = self.img_routes[-1]
        gx = gy = 0
        n_goal = 0
        for y in range(last.shape[0]):
            row = last[y]
            for x in range(last.shape[1]):
                if tuple(row[x][:3]) in goal_codes:
                    gx += x
                    gy += y
                    n_goal += 1
        if n_goal == 0:
            return None   # 没有 goal：validate_route_image 已经把它判废剔除了
        gx, gy = gx // n_goal, gy // n_goal

        first_pts = self._route_pixels(self.img_routes[0])
        if not first_pts:
            return None
        best = min(abs(x - gx) + abs(y - gy) for x, y in first_pts)   # 曼哈顿距离

        search_range = self.cfg["route"]["search_range"]
        rescue = self.cfg["route"].get("rescue_range", 40)
        if best > rescue:
            logger.error(
                f"[巡逻闭环] 这条巡逻线**闭不上环**：最后一段终点 ({gx},{gy}) 到第一段"
                f"最近像素 {best}px，超过失联救援范围 {rescue}px。\n"
                f"        后果：走完最后一段会循环回第一段但接不上 → 指令真空 → "
                f"干站不动 → 看门狗 {self.cfg['watchdog']['timeout']}s 后抽随机动作乱走。\n"
                f"        修法：重录最后一段，走回起点**10px 以内**再按 F3（最多别超 40px）。")
        elif best > search_range:
            logger.warning(
                f"[巡逻闭环] 最后一段终点 ({gx},{gy}) 离第一段 {best}px —— "
                f"超过正常搜索范围 {search_range}px，只能靠失联救援（{rescue}px）接上，"
                f"每次循环都会打一条「路线失联」警告。建议重录时走回起点 10px 内。")
        else:
            logger.info(f"[巡逻闭环] 最后一段终点离第一段 {best}px，闭环正常 ✓")
        return best

    def validate_route_image(self, img, name):
        '''
        路线有效性校验（2026-09-12 加）——**用「加载时校验一次」换掉「运行时兜底一堆」**。

        为什么要有它（用户 2026-09-12 的质疑命中了要害：「你加的条件会不会越来越多，
        导致索敌逻辑越来越复杂、牵一发而动全身？」）：会的，而且这次「又不攻击了」
        的根因**根本不在逻辑里，而在数据里** ——

            minimaps/废都南方工地/route_home.png 上只有 **17 个色码像素**，
            全挤在安全点 (98,56) 周围 6×5 的一小块里，**而且没有 goal 像素**。
            → 角色掉到 (98,122)，最近的回正线像素在 **64px** 外
              （search_range=10 / rescue_range=40 都够不着）→ 一帧都找不到
            → 指令真空，而「回正期间不打怪」又把攻击一起掐掉 → 既不走也不打 = 死局。

        这种"录废了的线"在**运行时是查不出来的**：它跟「暂时没接上」长得一模一样，
        只能靠不断堆超时去猜，也就是用户担心的"条件越来越多"。
        所以改在**加载时**一次性验完：废线直接**不启用**，引擎连回正状态都进不去。

        判据（任一不满足即判废）：
          1. 色码像素太少（< 20）—— 真实路线一条至少几十上百个像素；
          2. 没有 goal 标记 —— 录制器收尾一定会盖 goal（_stamp_goal），没有 = 没录完；
          3. 像素全挤在一小块（bbox 跨度 < 10px）—— 典型症状就是"原地起头就收尾了"。

        Args:
            img: RGB 路线图（已经过 mask_route_colors 处理）
            name: 只用于日志显示的文件名

        Returns:
            (bool, str): (是否有效, 无效原因；有效时原因为 "")
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        if not goal_codes:
            # 配置里连 goal 色码都没有 → 没法判断，跳过校验不误伤
            return True, ""

        hits = self._route_pixels(img)
        has_goal = any(tuple(img[y, x][:3]) in goal_codes for x, y in hits)

        n = len(hits)
        if n == 0:
            return False, "上面一个路线像素都没有（等于空图）"
        if n < 20:
            return False, f"只有 {n} 个路线像素（真实路线一条至少几十个）"
        if not has_goal:
            return False, "没有 goal 标记（= 录制时没走到收尾那一步）"
        xs = [p[0] for p in hits]
        ys = [p[1] for p in hits]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span < 10:
            return False, (f"路线像素全挤在 {span}px 的小块里"
                           f"（bbox x[{min(xs)},{max(xs)}] y[{min(ys)},{max(ys)}]）"
                           f"—— 像是原地起头、原地收尾")
        logger.debug(f"[路线校验] {name}：{n} 个路线像素，跨度 {span}px，有 goal ✓")
        return True, ""

    def load_config(self, cfg):
        '''
        load_config
        '''
        # ⚠️ 必须在所有"读 self.cfg"的方法之前赋值（2026-09-12 踩坑）：
        # 之前在末尾才设 self.cfg=cfg，但中段要调用的 check_route_loop_closure
        # 会读 self.cfg["route"]["search_range"]，那时 self.cfg 还是 None → TypeError
        # 直接抛掉整条 load_config（verify_load_config 当时挂在这）。
        # 提前到第一行后下面所有 self.cfg[...] 都安全；末尾再赋一次是为了向
        # verify_load_config 的【4】`self.cfg 就是传入的 cfg` 断言交代（虽然
        # 赋了同一个对象结果一样，但显式赋值更稳）。
        self.cfg = cfg
        # 先校验配置完整性（缺键会让主循环在运行中静默崩溃，见 _check_cfg_completeness）
        self._check_cfg_completeness(cfg)

        # Parse color code in config
        self.color_code = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code"].items()
        }
        self.color_code_up_down = {
            tuple(map(int, k.split(','))): v
            for k, v in cfg["route"]["color_code_up_down"].items()
        }
        # ⚠️ 第二版没有落点锚，也就没有第三张色码表（color_code_home_anchor 已删除）。
        # ⚠️ 引擎实例是**跨多次「开始/暂停」复用**的：换图后上一张图的回正线必须清干净，
        #    否则会用旧图的线在新图里触发回正（静默的错误行为，和 monsters_info 同批坑）。
        self.home_lines = []
        self.home_line = None
        self.home_d_home = None
        self.home_d_main = None
        self._home_line_near = None
        self._main_seg_i = 0
        self._main_pts_xy = None
        self._main_pts_seg = None
        self.home_enter_pos = None
        self.home_enter_t = 0.0
        self.home_frame = 0
        self.home_reenter_lock_t = 0.0

        # 2026-09-12：定点模式（stand）已按用户要求删除，只剩巡逻（normal）。
        # ⚠️ 旧配置里可能还残留 mode: stand —— 那样下面的地图/路线**一行都不加载**，
        #    角色站着不动又不报错，属于最难查的静默失败。这里明确喊出来。
        mode = cfg["bot"].get("mode")
        if mode != "normal":
            logger.error(
                f"[配置] bot.mode = {mode!r} 不是合法值 —— 定点模式（stand）已于 "
                f"2026-09-12 删除，现在只有巡逻（normal）。\n"
                f"        当前不会加载任何地图 / 路线 / 回正线，角色**不会动也打不到怪**。\n"
                f"        修法：主界面「挂机方式」选回「巡逻绕圈」，或直接把配置里的 "
                f"bot.mode 改成 normal。")
        if mode == "normal":
            map_name = cfg['bot']['map']
            # ⚠️ 登记表必须在**每次开跑时重读**（2026-09-12 用户实测踩坑）。
            #    self.data 是界面启动那一刻的快照（见 __init__），而录制器是**独立子进程**，
            #    录完路线会自动把新图写进 config/config_data.yaml。界面那边的地图列表每 5 秒
            #    重读一次（所以新图**立刻能在下拉里选到**），引擎这边不重读就出现诡异现象：
            #    「列表里能选、一开跑就 Invalid map name: X. Not supported in config_data.yaml」。
            #    修法：在这个唯一的消费点重读，不管从哪个入口调用都不会再拿到过期登记表。
            self.data = load_yaml("config/config_data.yaml")
            # Check if the map is supported in config_data.yaml
            if not map_name:
                logger.info("地图未选择，跳过地图加载")
                return
            if map_name not in self.data["map_mobs_mapping"]:
                text = f"Invalid map name: {map_name}. "\
                        "Not supported in config/config_data.yaml."
                logger.error(text)
                return -1
                # raise RuntimeError(text)

            # Load map.png from minimaps/
            self.img_map = load_image(f"minimaps/{map_name}/map.png",
                                      cv2.IMREAD_COLOR)
            # Load route*.png from minimaps/
            route_files = sorted(glob.glob(resource_path(f"minimaps/{map_name}/route*.png")))
            # route_rest.png 是上游遗留的「休息路线」；route_home.png 是本分支的回正线
            # ——两者都不属于主线巡逻列表，必须排除，否则回正线会被当成普通路线来回走。
            route_files = [p for p in route_files
                           if not p.endswith("route_rest.png")
                           and not p.endswith("route_home.png")]
            self.img_routes = []
            for route_file in route_files:
                img = cv2.cvtColor(load_image(route_file), cv2.COLOR_BGR2RGB)
                # Remove pixel in map that is color code
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code"])
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code_up_down"])
                # 废线不进列表（见 validate_route_image 的说明）：与其让它在运行时
                # 把角色卡死、再去猜，不如加载时就说清楚。
                ok, why = self.validate_route_image(img, os.path.basename(route_file))
                if not ok:
                    logger.error(
                        f"[路线校验] minimaps/{map_name}/{os.path.basename(route_file)} "
                        f"判为废线，已跳过不加载：{why}\n"
                        f"        请在录制器里重录这一段：走到起点按 F3 开始，"
                        f"一路走到终点再按 F3 保存（收尾会自动盖上 goal 标记）。")
                    continue
                self.img_routes.append(img)

            # ── 移动指令自检（2026-09-14 加）──────────────────────────────────
            # 录制器定位失败（坐标卡 0,0）时会录出「整段全是 goal、一个 left/right
            # 都没有」的废路线：2026-09-13 用户重录的 route1 就是 13 个像素全是 goal。
            # 这种线能过 validate_route_image（像素数够、跨度够），但 bot 拿到之后
            # **根本不知道该往哪走** —— 表现为站着不动或乱走，而且**不报错**，极难排查。
            for _i, _img in enumerate(self.img_routes):
                _mv = 0
                for _c, _cmd in (list(self.color_code.items())
                                 + list(self.color_code_up_down.items())):
                    _parts = str(_cmd).strip().split()
                    if _parts and _parts[0] in ("left", "right"):
                        _mv += int(np.count_nonzero(
                            np.all(_img == np.asarray(_c, dtype=np.uint8), axis=-1)))
                if _mv == 0:
                    logger.error(
                        f"[路线校验] 第 {_i + 1} 段主线上**一个移动指令（left/right）都没有**，"
                        f"全是 goal/none —— 这是**废路线**，bot 拿它不知道该往哪走。\n"
                        f"        几乎可以肯定是**录的时候定位就是坏的**（界面上坐标显示 (0,0)）。\n"
                        f"        请重录这一段，并且录之前先确认界面坐标**不是** (0,0)。")

            # 算出每一段的终点（goal 质心），供"这段走完了"判定 —— 见 _calc_seg_goals
            self._calc_seg_goals()

            # 多段巡逻线：量一下最后一段能不能接回第一段（见 check_route_loop_closure）
            self.check_route_loop_closure()
            # 单段路线：算出"另一端"，供往返走时判断该掉头了
            self._calc_pingpong_start()
            # 主线像素缓存（运行期算 d_main 用，**全图最近像素**，向量化，不是每帧全图扫）
            self._build_mainline_cache()

            # 回正线（可选资源）：有就加载，没有则 None（此时掉出去只能站住或沿主线走）
            self.img_route_home = None
            home_path = resource_path(f"minimaps/{map_name}/route_home.png")
            if os.path.exists(home_path):
                # ── v0.9 第二版：回正线不再复用主线的三条判废（20 像素 / 跨度 10px）──
                #    合法短回正线只有 8~15px，那三条判据会把它全部误杀（坑2）。
                #    改成"分连通域 → 逐条现算 → 逐条判废 → 抹掉废的那条"。
                img = cv2.cvtColor(load_image(home_path), cv2.COLOR_BGR2RGB)
                # Q6 旧图迁移：第一版画的 127,0,127 落点锚，这里直接涂黑。
                # ⚠️ 为什么必须涂黑：该色不在 color_code 里 → 角色站上去时
                #    get_nearest_color_code 取不到 → 那一帧没指令 → 站着不动（不报错）。
                legacy = wipe_legacy_anchor(img)
                if legacy:
                    logger.info(
                        f"[回正线] 发现 {len(legacy)} 个**旧版落点标记**（127,0,127），已自动忽略"
                        f"（v0.9 第二版不再需要落点锚）：{legacy[:8]}"
                        f"{' …' if len(legacy) > 8 else ''}\n"
                        f"        不用管它；如果这条线跑起来接不住人，用【🖊 手绘回正线】重画一条"
                        f"（先沿坑底横着画一段，再竖着跳回主线）。")
                route_codes = set(self.color_code) | set(self.color_code_up_down)
                # ⚠️ mask 前**先数一次**回正线像素。mask_route_colors 是**就地改**数组的
                #    （img_route[mask] = (0,0,0)），所以"mask 前那份"必须在这里抓快照，
                #    不能事后用同一个数组比 —— 那永远是 0 个被吃掉（自检形同虚设）。
                raw_home = img.copy()
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code"])
                img = mask_route_colors(self.img_map, img, cfg["route"]["color_code_up_down"])
                # 撞色自检：回正线像素被地图底色吃掉 → 静默失效（这条线再也接不住人）
                eaten = home_pixel_clash(raw_home, img, route_codes)
                if eaten:
                    logger.error(
                        f"[回正线] 有 {len(eaten)} 个回正线像素被**地图底色吃掉了**：{eaten[:8]}"
                        f"{' …' if len(eaten) > 8 else ''}\n"
                        f"        原因：mask_route_colors 会把「地图底图里出现同色」的位置涂黑，"
                        f"而这张图的 map.png 里恰好有这些颜色。\n"
                        f"        后果：这些位置**永远接不住角色**（不报错、只是回不来）。\n"
                        f"        修法：把这几段线往旁边挪 1px 重画，"
                        f"或换一个底图里没出现过的色码后重画。")
                self.home_lines = self._scan_home_lines(map_name, img, self.img_routes)
                if self.home_lines:
                    self.img_route_home = img
                    self._log_home_health()
                else:
                    # ⚠️ 废回正线**必须当成没有回正线**，不能"加载了但用不了"：
                    #    后者会让引擎进入回正状态、又一帧像素都找不到 → 指令真空，
                    #    再加上「回正期间不打怪」= 既不走也不打的死局（2026-09-12 实测）。
                    self.img_route_home = None
                    logger.error(
                        f"[回正线] minimaps/{map_name}/route_home.png 上**没有一条可用的回正线**，"
                        f"本次**不启用回正**（掉出去后只站桩打怪、不会尝试走回）。\n"
                        f"        逐条判废的原因见上方 ERROR。\n"
                        f"        补一条：主界面选中这张图 →【🖊 手绘回正线（推荐）】"
                        f"（不用开游戏、不用真的掉下去）；或【补录回正线】"
                        f"（先让角色真的掉下去一次，站落点上按 F6 起头，走回主线再按 F6 保存）。")
            else:
                # ⚠️★ 2026-09-14 加：文件不存在时原来**静默跳过**，没有任何提示。
                #    后果很严重且极难排查：2026-09-13 用户重录路线时录制器把
                #    route_home.png 删了，bot 照常启动、照常巡逻，就是**掉下去再也回不来**
                #    —— 用户连测三轮都以为是我改的逻辑没生效，其实是回正线文件根本不存在。
                #    所以这里必须**响亮地**报出来。
                self.img_route_home = None
                self.home_lines = []
                logger.error(
                    f"[回正线] ⚠️ minimaps/{map_name}/route_home.png **文件不存在** ——"
                    f"**本次不启用回正**，角色掉下平台后**不会自己走回来**。\n"
                    f"        如果你记得以前画过一条，那是被删掉了（重录主线 / 手绘回正线"
                    f"都会重建这个目录）。\n"
                    f"        补一条：主界面选中这张图 →【🖊 手绘回正线（推荐）】"
                    f"（不用开游戏）；或【补录回正线】（真掉一次，落点按 F6 起头，"
                    f"走回主线再按 F6 保存）。")

            # Load monsters images from monster/<monster_name>
            # ⚠️ 必须先清空：self.monsters_info 只在 __init__ 里初始化过一次，引擎是
            #    **跨多次「开始/暂停」复用的同一个实例**。不清空的话换图后上一张图的怪
            #    会留在表里 —— 例：先在 henesys_west 跑（绿蘑菇），再换到怪物列表为空的
            #    地图，引擎仍会拿绿蘑菇模板去匹配，属于静默的错误行为。
            #    （2026-09-12 与"登记表快照"同批发现，都是引擎状态跨次启动没重置。）
            self.monsters_info = {}
            # ── 怪物模板的加载范围（2026-09-12 改）────────────────────────────
            # 以前只加载「这张图登记的」怪（map_mobs_mapping[图]），于是同一种怪
            # （比如三眼章鱼）每换一张新图都要重新登记一次 —— 用户实测质疑得对：
            #   「怪物不是通用的吗，几张图都有三眼章鱼」。
            # 现在**默认加载 monster/ 下的全部模板**，真正做到「截一次，所有图通用」。
            # 代价：每帧要多匹配几种怪。只有当你截了很多种（>8 种）、帧率被拖下来时，
            #      才把 monster_detect.match_all_mobs 关掉，退回按图过滤。
            # ⚠️ 必须用**参数 cfg**，不能写 self.cfg（2026-09-12 血的教训）：
            #    self.cfg 是在本函数**末尾**（约 402 行）才赋值的，这里还是 None，
            #    self.cfg.get(...) 会抛 AttributeError。而 load_config 里没有
            #    try/except，异常不进日志 → 界面上就是「点开始没反应」，无从查起。
            match_all = cfg.get("monster_detect", {}).get("match_all_mobs", True)
            if match_all:
                monster_root = resource_path("monster")
                mob_names = sorted(
                    d for d in os.listdir(monster_root)
                    if os.path.isdir(os.path.join(monster_root, d)))
            else:
                mob_names = list(self.data["map_mobs_mapping"][map_name])

            for monster_name in mob_names:
                imgs = []
                for file in glob.glob(resource_path(f"monster/{monster_name}/{monster_name}*.png")):
                    # Add original image
                    img = load_image(file)
                    imgs.append((img, get_mask(img, (0, 255, 0))))
                    # Add flipped image
                    img_flip = cv2.flip(img, 1)
                    imgs.append((img_flip, get_mask(img_flip, (0, 255, 0))))
                if imgs:
                    self.monsters_info[monster_name] = imgs
                elif match_all:
                    # 全量扫描会扫到空目录之类的东西 —— 跳过就行，不能因此拒绝启动
                    logger.debug(f"[怪物] monster/{monster_name}/ 下没有 "
                                 f"{monster_name}*.png，已跳过")
                else:
                    logger.error(f"No images found in monster/{monster_name}/{monster_name}*")
                    return -1
                    # raise RuntimeError(f"No images found in monster/{monster_name}/{monster_name}*")
            if not self.monsters_info:
                # ⚠️ 必须报错（2026-09-12）：一个模板都加载不到 → 画面里一只怪都
                #    认不出来，症状是「站桩/走路、一次都不打怪」，而原实现只打一行
                #    INFO 的 Loaded monsters: []，用户完全无从查起（本次卡住的元凶）。
                if match_all:
                    logger.error(
                        f"[怪物] monster/ 目录里**没有任何怪物模板** —— "
                        f"一只怪都认不出来，角色会一直站桩/走路、一次都不打怪，而且不报错。\n"
                        f"        怎么补：界面上选中地图 → 点【截取怪物模板】→ 输入怪名 → "
                        f"框住一只怪存下来。\n"
                        f"        （模板是全局通用的，截一次之后所有图都能用。）")
                else:
                    logger.error(
                        f"[怪物] 地图「{map_name}」在 config/config_data.yaml 里"
                        f"**没有登记任何怪物**（map_mobs_mapping.{map_name} 是空列表），"
                        f"而当前是按图过滤模式（monster_detect.match_all_mobs: false）。\n"
                        f"        → 引擎没有可用的怪模板：角色会一直站桩/走路、"
                        f"一次都不打怪，**而且不报错**。\n"
                        f"        怎么补：界面上选中这张图 → 点【🐾 打哪几种怪…】勾选；\n"
                        f"              或者把 monster_detect.match_all_mobs 改回 true"
                        f"（加载全部模板，不用逐图登记）。")
            else:
                logger.info(f"Loaded monsters: {list(self.monsters_info.keys())}"
                            f"（{'全部模板' if match_all else f'按图过滤：{map_name}'}）")
                if len(self.monsters_info) > 8:
                    logger.warning(
                        f"[怪物] 当前每帧要匹配 {len(self.monsters_info)} 种怪的模板 —— "
                        f"如果帧率被拖下来，把 monster_detect.match_all_mobs 设为 false，"
                        f"改用「按图登记」只匹配这张图会出现的怪。")

            # 没有回正线时明确提示（回正线是可选资源，但掉了没有它就回不来）。
            # ⚠️ 措辞要分开「文件不存在」和「文件在但判废」—— 后者上面已经打过
            #    ERROR 了，这里再说"还没有回正线"会让人以为文件没生成，
            #    对着一个存在的文件反复排查（2026-09-12）。
            if self.img_route_home is None:
                home_file_exists = os.path.exists(
                    resource_path(f"minimaps/{map_name}/route_home.png"))
                if home_file_exists:
                    logger.warning(
                        f"[回正线] 文件**存在但已被判为废线**（见上方 ERROR），本次不启用"
                        f" —— 掉出平台后会原地打怪、走不回主线。")
                else:
                    logger.warning(
                        f"[回正线] 这张图还没有回正线（minimaps/{map_name}/route_home.png）——"
                        f"掉出平台后可能走不回主线。请在录制器里按 F6 录一条："
                        f"先让角色真的掉下去、站在**落点**上按 F6 起头，"
                        f"再一路走回主线按 F6 保存。")

        # Load player's name tag
        # 国服没有组队血条，角色名标签是画面内唯一的角色定位依据，属于必要资源
        try:
            self.img_nametag = load_image(f"nametag/{cfg['nametag']['name']}.png")
            self.img_nametag_gray = load_image(f"nametag/{cfg['nametag']['name']}.png",
                                               cv2.IMREAD_GRAYSCALE)
        except (FileNotFoundError, ValueError):
            logger.error(
                f"[初始化失败] 找不到角色名标签模板：nametag/{cfg['nametag']['name']}.png。"
                f"请先进入游戏截取自己角色头顶的名字标签存成该文件，"
                f"或在 config 里把 nametag.name 改成实际文件名（不含 .png 后缀）。"
            )
            raise

        # Load misc image
        # 国服适配（2026-09-12）：登录按钮图只用于「掉线自动重登」兜底（小地图
        # 30 秒找不到时的自动点登录），手动登录的挂机流程完全用不到 ——
        # 缺文件时降级为警告，不再让 load_config 整个崩掉（用户实测被迫去截登录界面）。
        lang = cfg["system"]["language"]
        login_button_path = resource_path(f"misc/login_button_{lang}.png")
        if os.path.exists(login_button_path):
            self.img_login_button = load_image(f"misc/login_button_{lang}.png")
        else:
            self.img_login_button = None
            logger.warning(
                f"[load_config] misc/login_button_{lang}.png 不存在，"
                f"「掉线自动重登」功能不可用（手动登录的挂机不受影响）。"
                f"需要时用界面 F3 截取模板（选「界面按钮」），名称填 login_button_{lang}。")

        # UI 坐标换算：把「录制时尺寸」的坐标换算到「当前画面尺寸」。
        # coord_base_size 不填（None）时 = game_window.size，即恒等变换。
        # 国服这套 ui_coords 需要按真实画面尺寸重新录制（见 tools/measure_window.py）。
        base_size = cfg["game_window"].get("coord_base_size") or cfg["game_window"]["size"]
        target_size = cfg["game_window"]["size"]
        if base_size != target_size:
            logger.info(f"[load_config] ui_coords 由 {base_size} 换算到 {target_size}")
        cfg['ui_coords']['login_button_top_left'] = normalize_pixel_coordinate(
            cfg['ui_coords']['login_button_top_left'], base_size, target_size)
        cfg['ui_coords']['login_button_bottom_right'] = normalize_pixel_coordinate(
            cfg['ui_coords']['login_button_bottom_right'], base_size, target_size)

        # Print mode on log
        logger.info(f"[load_config] Config AutoBot as {cfg['bot']['mode']} mode")

        # Update cfg
        self.cfg = cfg
        # 性能埋点开关只在这里读**一次**（此时 cfg 已就位）。
        # 别放到每帧去 try —— 白白烧 CPU，还容易在 cfg 半初始化时读到脏值。
        self.perf_on = perf_trace_on(cfg)

        return 0 # load successfully

    def start(self):
        '''
        Start all threads
        '''
        # Start keyboard controller thread
        self.kb = KeyBoardController(self.cfg)
        if self.is_disable_control:
            self.kb.disable() # Disable keyboard controller for debugging

        # Start game window capturing thread
        if self.args.test_image == '':
            self.capture = GameWindowCapturor(self.cfg)
        else:
            self.capture = GameWindowCapturor(self.cfg, self.args.test_image)

        # 把抓帧器认定的游戏窗口句柄交给键盘控制器。
        # ⚠️ 判「游戏是否在前台」必须按 hwnd —— 标题判据会被**本界面自己的标题**骗到
        #    （界面标题 '冒险岛怀旧服 - 自动练级' 含游戏标题 '冒险岛怀旧服' 这个子串），
        #    详见 KeyBoardController.is_game_window_active 的说明。
        self.kb.game_hwnd = getattr(self.capture, "window_hwnd", None)
        logger.info(f"[前台守卫] 游戏窗口句柄 = {self.kb.game_hwnd}")

        # Reset all timers
        self.t_last_frame = time.time()
        self.t_watch_dog = time.time()
        self.t_last_attack = time.time()
        self.t_last_minimap_update = time.time()

        # Set init state
        if self.args.init_state != "":
            self.fsm.set_init_state(self.args.init_state) # For debugging
        else:
            self.fsm.set_init_state("hunting")

        # Start Auto Bot main thread
        self.thread_auto_bot = threading.Thread(target=self.loop)
        self.thread_auto_bot.start()
        self.is_first_frame = True

        logger.info("[MapleStoryAutoBot] Started")

    def pause(self):
        '''
        Terminate thread except main thread
        '''
        self.terminate_threads()

    def enable_viz(self):
        '''
        Enable AutoBot to generate debug image
        '''
        self.is_need_show_debug_window = True
        logger.debug("[enable_viz] is_show_debug_window = True")

    def disable_viz(self):
        '''
        Disable AutoBot to generate debug image
        '''
        self.is_need_show_debug_window = False
        logger.debug("[disable_viz] is_show_debug_window = False")

    def start_record(self):
        '''
        Start record
        '''
        # Prepare video writer if need to record
        if not self.is_show_debug_window:
            self.enable_viz()

        # Make sure video/ exist
        video_dir = ensure_dir("video")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(video_dir, f"{timestamp}.mp4")

        # cv2.VideoWriter 与 imread/imwrite 一样走 ANSI 路径：项目路径含中文时，
        # 它会「不报错、不生成文件」地静默失败。折中办法是先写到系统临时目录
        # （纯 ASCII），停止录制时再搬到 video/ 下。
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')  # mp4 codec
        self.video_final_path = path
        self.video_tmp_path = os.path.join(tempfile.gettempdir(),
                                           f"msbot_record_{timestamp}.mp4")
        # VideoWriter 的尺寸参数是 (width, height)，配置里 size 是 [height, width]
        _wh = (self.cfg["game_window"]["size"][1],
               self.cfg["game_window"]["size"][0])
        self.video_writer = cv2.VideoWriter(self.video_tmp_path, fourcc, 10, _wh)

        logger.info(f"[start_record] Record video to {path}")

    def stop_record(self):
        '''
        Stop Record
        '''
        if self.video_writer is not None:
            # 原来只把引用置 None，没 release —— 视频会缺尾部数据甚至根本没写完
            self.video_writer.release()
            self.video_writer = None
            # 把临时文件搬到项目 video/ 下（中文路径写不进去，只能这样绕）
            try:
                if self.video_tmp_path and os.path.exists(self.video_tmp_path):
                    shutil.move(self.video_tmp_path, self.video_final_path)
                    logger.info(f"[stop_record] 视频已保存: {self.video_final_path}")
            except Exception as e:
                logger.error(f"[stop_record] 搬移视频失败: {e}")
                logger.error(f"           临时文件仍在: {self.video_tmp_path}")
        logger.info("[stop_record] Stop recording")

    def get_player_location_by_nametag(self):
        '''
        Detects the player's location based on the nametag position in the game window.

        This function works by:
        - Extracting a vertical region of interest (ROI) where the nametag is expected.
        - Padding the ROI to avoid template matching edge issues.
        - Using template matching to locate the nametag, split into left and right halves
        to improve robustness against partial occlusion.
        - Selecting the best match (left or right) based on score and cache status.
        - Computing the player's center position by applying a fixed offset to the nametag.

        Returns:
            loc_player (tuple): The (x, y) coordinates of the player's estimated location.
        '''
        # Get camera region in the game window
        img_camera = self.img_frame_gray[
            :self.cfg["ui_coords"]["ui_y_start"], :]

        # Get nametag image and search image
        if self.cfg["nametag"]["mode"] == "white_mask":
            # Apply Gaussian blur for smoother white detection
            img_camera = cv2.GaussianBlur(img_camera, (3, 3), 0)
            img_nametag = cv2.GaussianBlur(self.img_nametag_gray, (3, 3), 0)
            lower_white, upper_white = (150, 255)
            img_roi = cv2.inRange(img_camera, lower_white, upper_white)
            img_nametag  = cv2.inRange(img_nametag, lower_white, upper_white)
        elif self.cfg["nametag"]["mode"] == "grayscale":
            img_roi = img_camera
            img_nametag = self.img_nametag_gray
        elif self.cfg["nametag"]["mode"] == "histogram_eq":
            # Apply histogram equalization
            img_nametag_eq = cv2.equalizeHist(self.img_nametag_gray)
            img_camera_eq = cv2.equalizeHist(img_camera)

            # Apply global (fixed) threshold
            _, img_nametag = cv2.threshold(img_nametag_eq, 150, 255, cv2.THRESH_BINARY)
            _, img_roi = cv2.threshold(img_camera_eq, 150, 255, cv2.THRESH_BINARY)
        else:
            logger.error(f"Unsupported nametag detection mode: {self.cfg['nametag']['mode']}")
            return
        # cv2.imshow("img_roi", img_roi)
        # cv2.imshow("img_nametag", img_nametag)

        # Pad search region to deal with fail detection when player is at map edge
        (pad_y, pad_x) = self.img_nametag.shape[:2]
        img_roi = cv2.copyMakeBorder(
            img_roi,
            pad_y, pad_y, pad_x, pad_x,
            borderType=cv2.BORDER_REPLICATE  # replicate border for safe matching
        )

        # Get last frame name tag location
        if self.is_first_frame:
            last_result = None
        else:
            last_result = (
                self.loc_nametag[0] + pad_x,
                self.loc_nametag[1] + pad_y
            )

        # Get number of splits
        h, w = img_nametag.shape

        # Get nametag's background mask
        mask = get_mask(self.img_nametag, (0, 255, 0))

        # ---- 两遍匹配（2026-09-10 加）：先用**整块**，整块认不出再退到**切块**
        #
        # 为什么要这样：名字底板会被**别的玩家的名牌压住**。实测（20 帧人堆走位帧）：
        #   整块匹配只有 7/20 帧认得出；切成半块后变成 11/20 帧，而且救回来的 4 帧
        #   落点全在合理轨迹上（整块 0.0174 刚过阈值被拒的那帧，半块拿到 0.0059）。
        #
        # 为什么不干脆把 split_width 调小（等于永远用切块）：那样判别力反而变薄 ——
        #   实测半块左半的真位置分 0.0092，离阈值 0.01 只剩 1.09 倍。
        #   而上游原本切块就是为了抗遮挡（其注释原话 "To avoid occlusion from ladders
        #   or background objects"），只是一直被当成默认路径在用。
        #   所以正确形态是：**整块优先（判别力最强）+ 失败才退切块（抗遮挡）**。
        #
        # 两遍用**同一个阈值**：实测救回来的那 4 帧分数是 0.0002~0.0059，都在 0.01 以内；
        #   而认不出的帧最低也有 0.0174 —— 一个阈值就能切开，不需要给兜底单独放宽。
        passes = [(1, False)]
        _fb_split = int(self.cfg["nametag"].get("split_width_fallback", 0) or 0)
        if _fb_split > 0:
            _fb_n = max(1, w // max(1, _fb_split))
            if _fb_n > 1:
                passes.append((_fb_n, True))

        _thres = self.cfg["nametag"]["diff_thres"]
        _accepted = None      # 这一遍就达标了
        _best_try = None      # 都没达标时留一个最好的，供调试图显示

        for num_splits, is_fallback in passes:
            w_split = w // num_splits

            # Vertically split the nametag image
            nametag_splits = {}
            for i in range(num_splits):
                x_s = i * w_split
                x_e = (i + 1) * w_split if i < num_splits - 1 else w
                nametag_splits[f"{i+1}/{num_splits}"] = {
                    "img": img_nametag[:, x_s:x_e],
                    "mask": mask[:, x_s:x_e],
                    "last_result": (
                        (last_result[0] + x_s, last_result[1]) if last_result else None
                    ),
                    "score_penalty": 0.0,
                    "offset_x": x_s
                }

            # Match tempalte
            matches = []
            for tag_type, split in nametag_splits.items():
                loc, score, is_cached = find_pattern_sqdiff(
                    img_roi,
                    split["img"],
                    last_result=split["last_result"],
                    mask=split["mask"],
                    global_threshold=self.cfg["nametag"]["global_diff_thres"]
                )
                w_match = split["img"].shape[1]
                h_match = split["img"].shape[0]
                score += split["score_penalty"]
                matches.append((tag_type, loc, score, w_match, h_match,
                                is_cached, split["offset_x"]))

            # Select best match and fix offset:
            matches.sort(key=lambda x: (not x[5], x[2]))  # prefer cached, then low score
            tag_type, loc_nt, score, w_match, h_match, is_cached, offset_x = matches[0]

            # Adjust match location back to full nametag coordinates
            loc_nt = (loc_nt[0] - offset_x, loc_nt[1])
            loc_nt = (loc_nt[0] - pad_x, loc_nt[1] - pad_y)

            cand = (tag_type, loc_nt, score, w_match, h_match, is_cached, is_fallback)
            if _best_try is None or score < _best_try[2]:
                _best_try = cand
            if score < _thres:
                _accepted = cand
                break                        # 整块够了就别用更弱的切块

        tag_type, loc_nametag, score, w_match, h_match, is_cached, used_fallback = \
            _accepted or _best_try

        # Only update nametag location when score is good enough
        if score < self.cfg["nametag"]["diff_thres"]:
            self.loc_nametag = loc_nametag
            self.nametag_miss_streak = 0
            self.nametag_hit = True
            if not self.nametag_ever_hit:
                self.nametag_ever_hit = True
                logger.info(
                    f"[名字定位] 首次命中，位置锚点 = "
                    f"({self.loc_nametag[0] + w // 2}, "
                    f"{self.loc_nametag[1] - self.cfg['nametag']['offset'][1]})"
                    f"，score={score:.4f}")
        else:
            # 匹配失败：loc_nametag 保持上一帧的值 —— 位置会被「冻住」。
            # 这里只累加计数，告警交给主循环（那边才知道要不要按帧刷屏）。
            self.nametag_miss_streak += 1
            self.nametag_hit = False

        loc_player = (
            self.loc_nametag[0] + w // 2,
            self.loc_nametag[1] - self.cfg["nametag"]["offset"][1]
        )

        # 2026-09-12：原来这里会画一个绿色名牌框，并在下面写一行
        #   "NameTag,<score>,cached/missed,<type>[,fb]"
        # 按用户要求去掉（画面上只留 Attack Range + Mob Detection Box 两个框）。
        # 名字有没有匹配上、score 多少，看日志的 [运行状态]（里面带"名字命中/miss"计数）。
        # 下面这几个变量仍保留计算，供日志与后续逻辑使用。
        _ = (score, is_cached, tag_type, used_fallback)

        return loc_player

    def get_player_location_on_global_map(self):
        '''
        get_player_location_on_global_map
        '''
        self.loc_minimap_global, score, _ = find_pattern_sqdiff(
                                        self.img_map,
                                        self.img_minimap)
        # ── 埋点：记下小地图模板匹配的置信度 ────────────────────────────
        # 原来这个 score 被直接扔掉。留着它的用处：怀疑「坐标是不是定位错了」
        # 时能有一句客观依据 —— 回正小结那行会带上「小地图匹配度」。
        # ⚠️ 纯赋值，不参与任何判定，不影响行为
        #   （find_pattern_sqdiff 永远返回 min_loc，哪怕错配 —— 所以没有 score
        #    就完全看不出"这次定位到底准不准"）。
        self.minimap_score = score

        x_offset, y_offset = self.cfg["minimap"]["offset"]
        loc_player_global = (
            self.loc_minimap_global[0] + self.loc_player_minimap[0] + x_offset,
            self.loc_minimap_global[1] + self.loc_player_minimap[1] + y_offset
        )

        # Draw local minimap rectangle + player dot —— 只在调试可视化打开时画
        # ⚠️ 2026-09-12 崩过：原来**没判** is_show_debug_window，viz 关掉且
        # img_route_debug 没就绪（load_config 里置过 None）→ cv2.rectangle(None,...)
        # 抛 AttributeError，把主循环带崩。draw_rectangle / put_text_cn 现在也加了
        # None 兜底（防御性），但这里直接判 viz 关掉就不画，省 CPU 也避免出现「
        # 画了但没人看」的浪费。
        if self.is_show_debug_window and self.img_route_debug is not None:
            camera_bottom_right = (
                self.loc_minimap_global[0] + self.img_minimap.shape[1],
                self.loc_minimap_global[1] + self.img_minimap.shape[0]
            )
            cv2.rectangle(self.img_route_debug, self.loc_minimap_global,
                          camera_bottom_right, (0, 255, 255), 1)
            put_text_cn(
                self.img_route_debug,
                f"小地图 匹配度({round(score, 2)})",
                (self.loc_minimap_global[0], self.loc_minimap_global[1] + 15),
                (0, 255, 255), font_size=14, thickness=1
            )
            cv2.circle(self.img_route_debug,
                       loc_player_global, radius=2,
                       color=(0, 255, 255), thickness=-1)

        return loc_player_global

    def get_nearest_color_code(self):
        '''
        Searches for the nearest color-coded action marker
        around the player on the route map.

        This function:
        - Scans each pixel in the search box to find nearest color code
        - Tracks the closest matching pixel using Manhattan distance (|dx| + |dy|).
        - Returns a dictionary containing the nearest matching
          pixel's position, color, action label, and distance.

        Returns:
            dict or None: Dictionary containing:
                - "pixel": (x, y) coordinate of the matched pixel
                - "color": matched RGB color tuple
                - "action": corresponding action string from config
                - "distance": Manhattan distance from player
            Returns None if no matching color is found within the region.
        '''
        x0, y0 = self.loc_player_global
        h, w = self.img_route.shape[:2]

        def _scan(radius, center):
            """在 center 周围给定半径内找最近的路线色码，返回 (nearest, nearest_up_down, 扫描框)。"""
            cx, cy = center
            bx_min = max(0, cx - radius)
            bx_max = min(w, cx + radius)
            by_min = max(0, cy - radius)
            by_max = min(h, cy + radius)
            n, n_ud, md, md_ud = None, None, float('inf'), float('inf')
            for y in range(by_min, by_max):
                for x in range(bx_min, bx_max):
                    pixel = tuple(self.img_route[y, x])  # (R, G, B)
                    dist = abs(x - cx) + abs(y - cy)
                    # Get nearest color
                    if pixel in self.color_code and dist < md:
                        n = {
                            "pixel": (x, y),
                            "color": pixel,
                            "command": self.color_code[pixel],
                            "distance": dist
                        }
                        md = dist
                    # Get nearest color (up, dowm)
                    if pixel in self.color_code_up_down and dist < md_ud:
                        n_ud = {
                            "pixel": (x, y),
                            "color": pixel,
                            "command": self.color_code_up_down[pixel],
                            "distance": dist
                        }
                        md_ud = dist
            return n, n_ud, (bx_min, by_min, bx_max, by_max)

        nearest, nearest_up_down, scan_box = _scan(self.cfg["route"]["search_range"],
                                                   self.loc_player_global)

        # 失联救援（2026-09-12）：被怪碰撞/打怪位移很容易偏离路线超过正常搜索半径
        # （默认仅 10px）→ 指令真空、干站发呆，只能等 watchdog 抽随机动作（实测）。
        # 两级救援（用户设计：平时优先接回所在段，严重偏移才接受其它段）——
        #   Level 2：以「失联前最后跟随的路线像素」为锚点搜索。折返段即使空间上
        #            离角色更近，只要锚点附近还有当前段的像素，就接回当前段；
        #   Level 3：锚点附近确实没有像素（严重偏移，如掉下台子）才以角色为
        #            中心接受任何段。
        # 正常态坚持小半径，是因为搜得太宽会抓到隔壁折返段的像素、被引去错误分支。
        if nearest is None and nearest_up_down is None:
            rescue = self.cfg["route"].get("rescue_range", 40)
            if rescue > self.cfg["route"]["search_range"]:
                level = None
                anchor = getattr(self, "loc_last_route_pixel", None)
                if anchor is not None:
                    nearest, nearest_up_down, scan_box = _scan(rescue, anchor)
                    if nearest is not None or nearest_up_down is not None:
                        level = f"接回当前段（锚点 {anchor}）"
                if nearest is None and nearest_up_down is None:
                    nearest, nearest_up_down, scan_box = _scan(rescue, self.loc_player_global)
                    if nearest is not None or nearest_up_down is not None:
                        level = "严重偏移，接入最近段"
                if level is not None and \
                        time.time() - getattr(self, "t_route_rescue_log", 0) > 5:
                    self.t_route_rescue_log = time.time()
                    hit = nearest if nearest is not None else nearest_up_down
                    logger.warning(
                        f"[路线失联] 角色已偏离路线（小地图位置 {self.loc_player_global}），"
                        f"{level}，重新接引：{hit['command']}"
                        f"（距离 {hit['distance']}px）")

        # Debug 绘制：搜到路线像素时画搜索框 + 角色到路线的连接线。
        # ⚠️ 同 get_player_location_on_global_map：原来没判 viz 状态，img_route_debug
        # 为 None 时 cv2.line(None,...) 会抛 AttributeError 把主循环带崩。现在统一判。
        if self.is_show_debug_window and self.img_route_debug is not None:
            draw_rectangle(
                self.img_route_debug,
                (scan_box[0], scan_box[1]),
                (scan_box[2] - scan_box[0],
                 scan_box[3] - scan_box[1]),
                (0, 0, 255), "", text_height=0.4, thickness=1,
            )
            # Draw a straigt line from map_loc_player to color_code["pixel"]
            if nearest is not None:
                cv2.line(
                    self.img_route_debug,
                    self.loc_player_global, # start point
                    nearest["pixel"],       # end point
                    (0, 255, 0),            # green line
                    1                       # thickness
                )
                # 2026-09-12：原来这里会在游戏画面上写 "Route Action: ..." 和
                # "Route Index: ..." 两行，按用户要求去掉（画面上只留两个框）。
                # 当前指令 / 段号看日志的 [运行状态]（每 5 秒一条）。

            if nearest_up_down is not None:
                # 2026-09-12：原来这里会再写一行 "Route Action: ..."（上下方向），已去掉。
                cv2.line(
                    self.img_route_debug,
                    self.loc_player_global,  # start point
                    nearest_up_down["pixel"],# end point
                    (0, 0, 255),             # green line
                    1                        # thickness
                )

        # 记录最近跟随的路线像素，作为下次失联救援的锚点（见上面的两级救援说明）
        if nearest is not None:
            self.loc_last_route_pixel = nearest["pixel"]
        elif nearest_up_down is not None:
            self.loc_last_route_pixel = nearest_up_down["pixel"]

        return nearest, nearest_up_down  # if not found return none

    def get_attack_range(self, is_left=True):
        '''
        get_attack_range
        '''
        if self.cfg["bot"]["attack"] == "aoe_skill":
            dx = self.cfg["aoe_skill"]["range_x"] // 2
            dy = self.cfg["aoe_skill"]["range_y"] // 2
            x0 = max(0, self.loc_player[0] - dx)
            x1 = min(self.img_frame.shape[1], self.loc_player[0] + dx)
            y0 = max(0, self.loc_player[1] - dy)
            y1 = min(self.img_frame.shape[0], self.loc_player[1] + dy)

        elif self.cfg["bot"]["attack"] == "directional":
            if is_left:
                x0 = self.loc_player[0] - self.cfg["directional_attack"]["range_x"]
                x1 = self.loc_player[0]
            else:
                x0 = self.loc_player[0]
                x1 = x0 + self.cfg["directional_attack"]["range_x"]
            y0 = self.loc_player[1] - self.cfg["directional_attack"]["range_y"] // 2
            y1 = y0 + self.cfg["directional_attack"]["range_y"]
        else:
            raise RuntimeError(f"Unsupported attack mode: {self.cfg['bot']['attack']}")

        return (x0, y0, x1, y1)

    def get_nearest_monster(self, is_left=True):
        '''
        Finds the nearest monster within the player's attack range.

        This function:
        - Defines an attack box relative to the player position,
            depending on the facing direction (`is_left`).
        - Iterates through all detected monsters and checks which ones overlap
          with the attack box.
        - Returns the closest valid monster that meets the overlap criteria.

        Args:
            is_left (bool): If True, assume the player is facing left;
                            adjusts attack box accordingly.
        Returns:
            dict or None: The nearest monster's info dict, or None if no valid match.
        '''

        x0, y0, x1, y1 = self.get_attack_range(is_left=is_left)

        nearest_monster = None
        min_distance = float('inf')
        for monster in self.monsters:
            mx1, my1 = monster["position"]
            mw, mh = monster["size"]
            mx2 = mx1 + mw
            my2 = my1 + mh

            # Calculate intersection
            ix1 = max(x0, mx1)
            iy1 = max(y0, my1)
            ix2 = min(x1, mx2)
            iy2 = min(y1, my2)

            iw = max(0, ix2 - ix1)
            ih = max(0, iy2 - iy1)
            inter_area = iw * ih

            # 取所有怪物模板中最小的面积作下限；空模板时退回 0，
            # 避免对空序列 min() 抛 ValueError（与 with_enemy_hp_bar 处同样的埋雷）
            _areas = [im.shape[0] * im.shape[1]
                      for _, _imgs in self.monsters_info.items() for im, _ in _imgs]
            min_mob_area = min(_areas) if _areas else 0
            inter_area_thres = min(min_mob_area, self.cfg['monster_detect']['max_mob_area_trigger'])
            if inter_area >= inter_area_thres:
                # Compute distance to player center
                monster_center = (mx1 + mw // 2, my1 + mh // 2)
                dx = monster_center[0] - self.loc_player[0]
                dy = monster_center[1] - self.loc_player[1]
                distance = abs(dx) + abs(dy)  # Manhattan distance

                if distance < min_distance:
                    min_distance = distance
                    nearest_monster = monster

        return nearest_monster

    def get_monsters_in_range(self, top_left, bottom_right):
        '''
        get_monsters_in_range
        '''
        x0, y0 = top_left
        x1, y1 = bottom_right

        img_roi = self.img_frame[y0:y1, x0:x1]

        # Shift player's location into ROI coordinate system
        px, py = self.loc_player
        px_in_roi = px - x0
        py_in_roi = py - y0

        # Define rectangle range around player (in ROI coordinate)
        char_x_min = max(0, px_in_roi - self.cfg["character"]["width"] // 2)
        char_x_max = min(img_roi.shape[1], px_in_roi + self.cfg["character"]["width"] // 2)
        char_y_min = max(0, py_in_roi - self.cfg["character"]["height"] // 2)
        char_y_max = min(img_roi.shape[0], py_in_roi + self.cfg["character"]["height"] // 2)

        monsters = []
        # ── contour_only 模式：ROI 侧的预处理**整帧只做一次**（2026-09-14 优化）──
        # ⚠️ 原来这几行放在下面的模板循环里 → 同一张 ROI 被重复处理 N 遍
        #    （N = 模板数量，本图 4 种怪共约 20 张）。实测怪检测一帧要 ~490ms，
        #    这部分重复计算占了不小一块。
        #    而它只跟「当前画面 + 角色位置」有关、跟具体用哪个模板**完全无关**
        #    → 提出来是**纯赚**：结果一模一样，只是少算十几遍。
        _mode = self.cfg["monster_detect"]["mode"]
        _blur = self.cfg["monster_detect"]["contour_blur"]
        _roi_blur = None
        if _mode == "contour_only":
            _mask_roi = np.all(img_roi == [0, 0, 0], axis=2).astype(np.uint8) * 255
            # Zero out mask inside this region (ignore player's own character)
            _mask_roi[char_y_min:char_y_max, char_x_min:char_x_max] = 0
            _roi_blur = cv2.GaussianBlur(_mask_roi, (_blur, _blur), 0)

        for monster_name, monster_imgs in self.monsters_info.items():
            for img_monster, mask_monster in monster_imgs:
                if _mode == "contour_only":
                    # Use only black lines contour to detect monsters
                    # Create masks (already grayscale)
                    mask_pattern = np.all(img_monster == [0, 0, 0], axis=2).astype(np.uint8) * 255

                    # Apply Gaussian blur (soften the masks)
                    img_monster_blur = cv2.GaussianBlur(mask_pattern, (_blur, _blur), 0)
                    # ROI 侧**不再重算** —— 直接复用上面那一份（见上面的说明）
                    img_roi_blur = _roi_blur

                    # Check template vs ROI size before matching
                    h_roi, w_roi = img_roi_blur.shape[:2]
                    h_temp, w_temp = img_monster_blur.shape[:2]

                    if h_temp > h_roi or w_temp > w_roi:
                        return []  # template bigger than roi, skip this matching

                    # Perform template matching
                    res = cv2.matchTemplate(img_roi_blur, img_monster_blur, cv2.TM_SQDIFF_NORMED)

                    # Apply soft threshold
                    match_locations = np.where(res <= self.cfg["monster_detect"]["diff_thres"])

                    h, w = img_monster.shape[:2]
                    for pt in zip(*match_locations[::-1]):
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            "size": (h, w),
                            "score": res[pt[1], pt[0]],
                        })
                elif self.cfg["monster_detect"]["mode"] == "grayscale":
                    img_monster_gray = cv2.cvtColor(img_monster, cv2.COLOR_BGR2GRAY)
                    img_roi_gray = cv2.cvtColor(img_roi, cv2.COLOR_BGR2GRAY)
                    res = cv2.matchTemplate(
                            img_roi_gray,
                            img_monster_gray,
                            cv2.TM_SQDIFF_NORMED,
                            mask=mask_monster)
                    match_locations = np.where(res <= self.cfg["monster_detect"]["diff_thres"])
                    h, w = img_monster.shape[:2]
                    for pt in zip(*match_locations[::-1]):
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            "size": (h, w),
                            "score": res[pt[1], pt[0]],
                    })
                elif self.cfg["monster_detect"]["mode"] == "color":
                    res = cv2.matchTemplate(
                            img_roi,
                            img_monster,
                            cv2.TM_SQDIFF_NORMED,
                            mask=mask_monster)
                    match_locations = np.where(res <= self.cfg["monster_detect"]["diff_thres"])
                    h, w = img_monster.shape[:2]
                    for pt in zip(*match_locations[::-1]):
                        monsters.append({
                            "name": monster_name,
                            "position": (pt[0] + x0, pt[1] + y0),
                            "size": (h, w),
                            "score": res[pt[1], pt[0]],
                    })
                else:
                    logger.error(f"Unexpected camera localization mode: {self.cfg['monster_detect']['mode']}")
                    return []

        # Apply Non-Maximum Suppression to monster detection
        monsters = nms(monsters, iou_threshold=0.4)

        # Detect monster via health bar
        if self.cfg["monster_detect"]["with_enemy_hp_bar"]:
            # Create color mask for Monsters' HP bar
            mask = cv2.inRange(img_roi,
                               np.array(self.cfg["monster_detect"]["hp_bar_color"]),
                               np.array(self.cfg["monster_detect"]["hp_bar_color"]))

            # Find connected components (each cluster of green pixels)
            num_labels, labels, stats, centroids = \
                cv2.connectedComponentsWithStats(mask, connectivity=8)

            for i in range(1, num_labels):  # skip background (label 0)
                x, y, w, h, area = stats[i]
                if area < 3:  # small noise filter
                    continue

                # Guess a monster bounding box
                y += 10
                x = max(0, x)
                y = max(0, y)
                w = 70
                # 原来这里直接 min() 所有模板的高度 —— 一个模板都没有时会抛 ValueError。
                # 改成：有模板就取最矮的那个当高度，没有就退回到经验值，
                # 这样「只用血条、不做模板」的用法也能跑起来。
                all_h = [img.shape[0] for _, imgs in self.monsters_info.items()
                         for img, _ in imgs]
                h = min(all_h) if all_h else 100

                monsters.append({
                    "name": "Health Bar",
                    "position": (x0 + x, y0 + y),
                    "size": (h, w),
                    "score": 1.0,
                })

        # Debug
        # Draw attack detection range
        draw_rectangle(
            self.img_frame_debug, (x0, y0), (y1-y0, x1-x0),
            (255, 0, 0), "索敌范围"
        )

        # 2026-09-12：原来这里会给每个检测到的怪画一个小框
        # （黄=血条检出、绿=模板检出），按用户要求去掉
        # （画面上只留 Attack Range 红框 + Mob Detection Box 蓝框两个框）。
        # 检测到几只怪，看日志的 [运行状态]（每 5 秒一条，含 "怪<N>"）。

        return monsters

    def get_img_frame(self):
        '''
        get_img_frame

        把抓到的原始帧裁成「游戏客户区」，并**保证帧的像素坐标系 == 客户区坐标系**。
        这一条是全部坐标（模板、ui_coords、路线图、点击）能对上的前提。

        ── 抓帧口径（2026-09-10 实测确认，见 tools/measure_window.py）────────────
        `windows_capture` 抓**窗口**时，帧里是**含标题栏和 1px 窗口边框**的：
          帧宽 = 客户区宽 + 左右各 1px
          帧高 = 客户区高 + 标题栏高 + 底部 1px
        实测（800x600 客户区的对照窗口）：帧 802x632，标题栏 31px，内容从 (1, 31) 开始。

        ── 裁剪规则 ──────────────────────────────────────────────────────────
        1. 切掉顶部 `title_bar_height` 行 → 帧内容左上角 == 客户区左上角
        2. 水平方向按「左右边框对称」取左偏移 `(帧宽 - 目标宽) // 2`，锚在左上角裁到目标尺寸

        为什么不用原来的「居中裁」：居中裁在多余像素左右/上下不对称时会整体偏移
        （底部多余 1px 时 y 偏移 0 是巧合，多余 2px 就会偏 1px）。锚定左上角则
        天然只丢弃底部/右侧的边框像素，不会动到内容。
        config: game_window.size = [height, width]
        '''
        # Get window game raw frame
        self.frame = self.capture.get_frame()
        if self.frame is None:
            logger.warning("Failed to capture game frame.")
            return

        raw_h, raw_w = self.frame.shape[:2]
        title_bar = self.cfg["game_window"]["title_bar_height"]
        target_h, target_w = self.cfg["game_window"]["size"]   # [height, width]

        frame_cropped, msg = crop_frame_to_client(
            self.frame, (target_h, target_w), title_bar, tag="引擎")

        self._verify_frame_geometry_once(raw_w, raw_h, title_bar, msg)

        if frame_cropped is None:
            logger.error(f"[get_img_frame] {msg}")
            return None

        return frame_cropped

    def _verify_frame_geometry_once(self, raw_w, raw_h, title_bar, crop_msg):
        '''
        第一帧到手后，把「抓帧几何」和「窗口实测客户区」交叉核对一次并留日志。

        背景：config 的 game_window.size 一旦和真实窗口不一致，所有坐标都会失准，
        而且症状是「看不出来」（画面正常，只是打得偏）。所以在第一帧就把尺寸摊开对照，
        让问题一眼可见，而不是等跑起来打不着怪再回头查。
        '''
        if getattr(self, "_frame_geom_logged", False):
            return
        self._frame_geom_logged = True

        target_h, target_w = self.cfg["game_window"]["size"]
        logger.info(f"[帧几何] {crop_msg}")

        # 与窗口实测客户区对照
        client = None
        try:
            client = get_window_client_size(self.capture.window_title)
        except Exception as e:
            logger.debug(f"[帧几何] 取窗口客户区失败：{e}")

        if client is None:
            logger.warning("[帧几何] 取不到窗口客户区尺寸，无法交叉核对。"
                           "请确认游戏窗口还在（没被最小化/关闭）。")
            return

        client_w, client_h = client
        if (client_w, client_h) == (target_w, target_h):
            logger.info(f"[帧几何] 【OK】窗口实测客户区 {client_w}x{client_h} 与配置一致，"
                        f"帧坐标 == 客户区坐标。")
        else:
            logger.error(
                f"[帧几何] 【失败】 窗口实测客户区 {client_w}x{client_h} 与配置的 "
                f"{target_w}x{target_h} 不一致 —— 所有坐标都会失准！\n"
                f"        修法：把游戏窗口调成 {target_w}x{target_h}（不含标题栏），"
                f"或运行 `python -m tools.measure_window --write-config` 按实测值改配置。")

    def is_player_stuck(self):
        """
        Checks whether the player is stuck (not moving)
        based on their global position on map.

        This function:
        - Compares the player's current position with their last known position
          tracked by the watchdog.
        - If the player has moved beyond a threshold (`watch_dog_range`),
          it resets the watchdog timer.
        - If the player hasn't moved and the elapsed time exceeds (`watch_dog_timeout`),
          it flags the player as stuck and resets the watchdog.

        Returns:
            bool: True if the player is stuck, False otherwise.
        """
        dx = abs(self.loc_player_global[0] - self.loc_watch_dog[0])
        dy = abs(self.loc_player_global[1] - self.loc_watch_dog[1])

        # ── 正在打怪 → 不算卡死（2026-09-12 加）──────────────────────────────
        # 打怪时角色本来就该站着不动（技能前摇 + 冷却），连续打同一只怪的位移
        # 经常 < watchdog.range(10px) → 10 秒后被误判卡死、抽随机动作乱走，
        # 把正在进行的战斗打断（表现：打得好好的突然乱跑）。
        #
        # ⚠️ 原来靠「站在安全点内豁免」兜住站桩打怪，但那是 stand 模式专属的；
        #    2026-09-12 删掉定点模式后，换成这条与模式无关的豁免。
        grace = self.cfg["watchdog"].get("attack_grace", 5)
        if time.time() - self.t_last_attack < grace:
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = time.time()
            return False

        # ── 窄平台降级：站着不动是**预期行为**，不是卡死（2026-09-15 加）──────
        # 见 route_too_narrow_for_patrol：这条路窄到没法巡逻，引擎主动不巡逻了。
        # ⚠️ 只豁免「**画面里没有认出来的怪**」的情况（用户 2026-09-15 明确：
        #    「附近有模板怪但是不打」= 真的卡死）。有怪却不打时**不豁免**，
        #    照样往下走 → 看门狗触发 → 脱困随机指令去救。
        if getattr(self, "_patrol_degraded", False) and not getattr(self, "monsters", None):
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = time.time()
            return False

        current_time = time.time()
        if dx + dy > self.cfg["watchdog"]["range"]:
            # Player moved, reset watchdog timer
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = current_time
            return False

        dt = current_time - self.t_watch_dog
        if dt > self.cfg["watchdog"]["timeout"]:
            # watch dog idle for too long, player stuck
            self.loc_watch_dog = self.loc_player_global
            self.t_watch_dog = current_time
            # 记下已卡多少秒：`handle_stuck` 要把它写进提示窗（2026-09-15 加）
            self.stuck_seconds = round(dt, 2)
            logger.warning(f"[is_player_stuck] Player stuck for {self.stuck_seconds} seconds.")
            return True
        return False

    def handle_stuck(self):
        '''判定卡死后**做什么** —— 唯一入口（2026-09-15 加）。

        配置 `watchdog.on_stuck`（完整理由见 config_default.yaml）：
            random = 老的"抽随机动作"。实测两条坏处：
                     ① 窄平台上乱抽 = 直接掉下去（抽到 `right none jump`，右边正好是悬崖）；
                     ② 把真因**盖住** —— 随机跳把角色从 (100,122) 踹到 (100,118)，
                        症状从"在原地蹭"变成"跳到另一层"，日志上看"它动了"，更难查。
            alert  = ★默认：**不乱动**（保持本帧逻辑给的指令），
                     改为**弹置顶提示窗** + 醒目日志，让人来处理。
            none   = 什么都不做（只留 is_player_stuck 那条 WARNING）。

        ⚠️ 取值非法 / 缺键 / 读配置抛异常 → 一律回落 **alert**（宁可不动也别乱动）。
        ⚠️ alert 模式**故意不清指令**：清成 none 反而会让角色停下（本来可能只是在
           "朝一个方向顶着走不动"），而"顶着走"至少方向是逻辑算出来的、是安全的。
        '''
        try:
            mode = str(self.cfg.get("watchdog", {}).get("on_stuck", "alert") or "alert")
        except Exception:                                    # noqa: BLE001
            mode = "alert"
        mode = mode.strip().lower()
        if mode not in ("random", "alert", "none"):
            logger.warning(f"[卡死处理] watchdog.on_stuck 取值非法（{mode!r}）"
                           f"→ 按 alert 处理（不乱动）")
            mode = "alert"

        if mode == "random":
            self.update_cmd_by_random()
            return
        if mode == "none":
            return

        # ── alert：不乱动，只告警 ──────────────────────────────────────────
        try:
            cooldown = float(self.cfg.get("watchdog", {}).get("alert_cooldown", 60))
        except (TypeError, ValueError):
            cooldown = 60.0
        stuck_alert.notify_stuck(
            pos=getattr(self, "loc_player_global", None),
            seconds=getattr(self, "stuck_seconds", None),
            cmd=f"{self.cmd_move_x} {self.cmd_move_y} {self.cmd_action}",
            extra=(f"回正状态={bool(getattr(self, 'is_using_home_route', False))}"
                   f" / 窄平台降级={bool(getattr(self, '_patrol_degraded', False))}"
                   f" / 附近怪={len(getattr(self, 'monsters', None) or [])}"),
            cooldown=cooldown)

    def screenshot_img_frame(self):
        '''
        Save self.img_frame
        '''
        if self.img_frame is None:
            logger.error("[screenshot_img_frame] Failed, game window is not available")
        else:
            screenshot(self.img_frame, "img_frame")

        if self.img_frame_debug is None:
            pass
        else:
            screenshot(self.img_frame_debug, "img_frame_debug")

        if self.frame is None:
            pass
        else:
            screenshot(self.frame, "frame")

    def update_info_on_img_frame_debug(self):
        '''
        update_info_on_img_frame_debug
        '''
        # ── 2026-09-12 按用户要求精简画面 ────────────────────────────────
        # 这里原本有一组左下角文字（FPS / State / Resolution / 按键提示），现已去掉。
        # 现在画面上**只保留两个框**：
        #   红框 "Attack Range"        = 攻击范围（技能能打到的矩形）
        #   蓝框 "Mob Detection Box"   = 怪物检测搜索区（引擎只在这个矩形内找怪）
        # 其余标注（Route Action / Route Index / Cmd / minimap 红框 / 怪物小框）
        # 也一并去掉，保持画面干净。
        # ⚠️ 这些运行信息并没有丢：日志里主循环每 5 秒一条 [运行状态]
        #    （帧号/小地图/全局坐标/段号/指令/怪数/名字命中），排查照旧。
        # 注：`self.fps` 仍在这里更新，其它地方/日志可能会读。
        self.fps = round(1.0 / (time.time() - self.t_last_frame))

        # Draw attack box on debug window
        if self.cfg["bot"]["attack"] == "aoe_skill":
            x0, y0, x1, y1 = self.get_attack_range()
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击范围"
            )
        elif self.cfg["bot"]["attack"] == "directional":
            x0, y0, x1, y1 = self.get_attack_range(is_left=True)
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击范围(左)"
            )
            x0, y0, x1, y1 = self.get_attack_range(is_left=False)
            draw_rectangle(
                self.img_frame_debug, (x0, y0),
                (y1-y0, x1-x0),
                (0, 0, 255), "攻击范围(右)"
            )

        # 2026-09-12：原来这里会在游戏画面上画一个 "minimap" 红框标出小地图位置，
        # 按用户要求去掉（画面上只留 Attack Range + Mob Detection Box 两个框）。
        # self.loc_minimap / self.img_minimap 本身仍在用（坐标换算需要），只是不画框了。

        # ⚠️ 没有路线图时 img_route_debug 是 None（这张图还没录路线就会这样），
        #    下面直接 .shape 会 AttributeError，而 viz 一崩主循环就跟着崩 ——
        #    打开「游戏画面」页签反而把挂机搞停。这里提前收手。
        if self.img_route_debug is None:
            return

        # Compute crop region with boundary check
        crop_w, crop_h = 80, 80
        x0 = max(0, self.loc_player_global[0] - crop_w // 2)
        y0 = max(0, self.loc_player_global[1] - crop_h // 2)
        x1 = min(self.img_route_debug.shape[1], x0 + crop_w)
        y1 = min(self.img_route_debug.shape[0], y0 + crop_h)

        # Check if valid crop region
        if x1 <= x0 or y1 <= y0:
            return

        # Crop region
        mini_map_crop = self.img_route_debug[y0:y1, x0:x1]
        mini_map_crop = cv2.resize(mini_map_crop,
                                (int(mini_map_crop.shape[1] * 3),
                                 int(mini_map_crop.shape[0] * 3)),
                                interpolation=cv2.INTER_NEAREST)
        # Paste into top-right corner of self.img_frame_debug
        h_crop, w_crop = mini_map_crop.shape[:2]
        h_frame, w_frame = self.img_frame_debug.shape[:2]
        x_paste = w_frame - w_crop - 10  # 10px margin from right
        y_paste = 10
        self.img_frame_debug[y_paste:y_paste + h_crop, x_paste:x_paste + w_crop] = mini_map_crop

        # Draw border around minimap
        cv2.rectangle(
            self.img_frame_debug,
            (x_paste, y_paste),
            (x_paste + w_crop, y_paste + h_crop),
            color=(255, 255, 255),   # White border
            thickness=2
        )

        # 2026-09-12：原来这里会画一行 `Cmd: <指令>`，按用户要求去掉。
        # 想看当前指令时看日志的 [运行状态]（每 5 秒一条）即可。

    def update_img_frame_debug(self):
        '''
        update_img_frame_debug
        '''
        cv2.imshow("Game Window Debug",
            self.img_frame_debug[:self.cfg["ui_coords"]["ui_y_start"], :])
        # Update FPS timer
        self.t_last_frame = time.time()

    def terminate_threads(self):
        '''
        terminate all threads
        '''
        # Terminate keyboard controller
        if self.kb is not None:
            self.kb.is_terminated = True
        # Terminate game window capturor
        if self.capture is not None:
            self.capture.stop()
        self.is_terminated = True
        logger.info(f"[terminate_threads] Terminated all threads")

    def get_attack_direction(self, monster_left, monster_right):
        '''
        get_attack_direction
        '''
        # Compute distance for left
        distance_left = float('inf')
        if monster_left is not None:
            mx, my = monster_left["position"]
            mw, mh = monster_left["size"]
            center_left = (mx + mw // 2, my + mh // 2)
            distance_left = abs(center_left[0] - self.loc_player[0]) + \
                            abs(center_left[1] - self.loc_player[1])
        # Compute distance for right
        distance_right = float('inf')
        if monster_right is not None:
            mx, my = monster_right["position"]
            mw, mh = monster_right["size"]
            center_right = (mx + mw // 2, my + mh // 2)
            distance_right = abs(center_right[0] - self.loc_player[0]) + \
                            abs(center_right[1] - self.loc_player[1])
        # Choose attack direction and nearest monster
        attack_direction = None
        # nearest_monster = None

        # Additional validation: check if monster is actually on the correct side
        def is_monster_on_correct_side(monster, direction):
            if monster is None:
                return False
            mx, my = monster["position"]
            mw, mh = monster["size"]
            monster_center_x = mx + mw // 2
            player_x = self.loc_player[0]

            if direction == "left":
                return monster_center_x < player_x  # Monster should be left of player
            else:  # direction == "right"
                return monster_center_x > player_x  # Monster should be right of player

        # Only choose direction if there's a clear winner and monster is on correct side
        if monster_left is not None and monster_right is None and \
            is_monster_on_correct_side(monster_left, "left"):
            attack_direction = "left"
            # nearest_monster = monster_left
        elif monster_right is not None and monster_left is None and \
            is_monster_on_correct_side(monster_right, "right"):
            attack_direction = "right"
            # nearest_monster = monster_right
        elif monster_left is not None and monster_right is not None:
            # Both sides have monsters, check distance and side validation
            left_valid = is_monster_on_correct_side(monster_left, "left")
            right_valid = is_monster_on_correct_side(monster_right, "right")

            if left_valid and not right_valid:
                attack_direction = "left"
                # nearest_monster = monster_left
            elif right_valid and not left_valid:
                attack_direction = "right"
                # nearest_monster = monster_right
            elif left_valid and right_valid and distance_left < distance_right - 50:
                attack_direction = "left"
                # nearest_monster = monster_left
            elif left_valid and right_valid and distance_right < distance_left - 50:
                attack_direction = "right"
                # nearest_monster = monster_right
            # If both valid but distances too close, don't attack to avoid confusion

        # 2026-09-12：原来这里会在画面上写一行
        #   "L:<dist>(ok) R:<dist>(ok) Dir:<方向>" 的调试文字，按用户要求去掉
        # （画面上只留 Attack Range 红框 + Mob Detection Box 蓝框两个框）。
        # 攻击方向的选择结果会体现在日志 [运行状态] 的指令里。
        return attack_direction

    def get_face_direction(self):
        '''
        离玩家最近的怪在玩家的哪一侧（"left" / "right" / None）。

        只用于让角色**面向**目标（AOE 模式）—— 与 get_attack_direction 不同：
        后者在两边怪距离接近时会返回 None（避免乱打），但**朝向**总得选一个，
        不能因为取舍不定就让角色一直保持初始朝向。
        '''
        if not self.monsters:
            return None
        best_x, best_d = None, float('inf')
        for monster in self.monsters:
            mx, my = monster["position"]
            mw, mh = monster["size"]
            cx, cy = mx + mw // 2, my + mh // 2
            d = abs(cx - self.loc_player[0]) + abs(cy - self.loc_player[1])
            if d < best_d:
                best_x, best_d = cx, d
        if best_x is None:
            return None
        if abs(best_x - self.loc_player[0]) <= 3:
            return None     # 正上方/正下方，不强行转身
        return "left" if best_x < self.loc_player[0] else "right"

    def get_login_button_location(self):
        '''
        get_login_button_location

        返回登录按钮中心点在**客户区坐标系**里的位置（找不到返回 None）。

        【注意】 这里不再加 title_bar_height（2026-09-10 修正）：
          img_frame 已被 get_img_frame() 裁成客户区，所以算出来的 loc 本来就是
          客户区坐标；而调用方是把它交给 click_in_game_window() 的，后者会自己
          加上客户区左上角的屏幕原点。原先再补一个标题栏高度属于重复补偿，
          会让点击整体下移一个标题栏的高度（本机实测 31px）。
        '''
        # 登录按钮模板未提供时（国服手动登录，见 load_config 的降级说明），直接放弃
        if self.img_login_button is None:
            return None

        # Extract the region where the login button should appear
        x0, y0 = self.cfg["ui_coords"]["login_button_top_left"]
        x1, y1 = self.cfg["ui_coords"]["login_button_bottom_right"]
        img_roi = self.img_frame[y0:y1, x0:x1]

        # 2026-09-12：原来这里会在画面上画一个 "login_button box" 绿框，
        # 按用户要求去掉（只有登录画面才会走到这里，平时不出现）。

        # Find the 'login' button
        loc, score, _ = find_pattern_sqdiff(
                        img_roi, self.img_login_button)
        if score < self.cfg["ui_coords"]["login_button_thres"]:
            h, w = self.img_login_button.shape[:2]
            logger.info(f"[get_login_button_location] Found login button with score({score})")
            # 客户区坐标，交给 click_in_game_window() 时会再加上客户区屏幕原点
            return (int(x0 + loc[0] + w // 2),
                    int(y0 + loc[1] + h // 2))
        else:
            return None

    def try_return_to_mainline(self):
        '''
        回正期间的退出判定：**主线优先**（2026-09-12 用户要求 + v0.9 第二版新判据）。

        第二版内核（设计 4.2）：
            `d_home >= d_main - home_dist_tol` → 角色离主线更近（或一样近）→
            已经回到主线上了 → 立刻切回主线，停止走回正线。

        ⚠️ 与**进入判据是同一个表达式**（home_should_return），口径唯一：
           第一版"警告口径和退出口径不一致"的 bug 就是这么来的。

        ⚠️ 不再用任何"半径"找主线（第一版的 home_return_range / D-3 全部删除）：
           落点离主线只有 6px 时，任何固定半径在落点处就成立 → 一进回正就被掐掉
           （第一版坑 3「抢跑」）。改成比两条线的距离后天然免疫。

        Returns:
            bool: 是否成功切回主线
        '''
        if not self.img_routes:
            return False
        # 最小驻留门（保险丝，默认关）未开 → 屏蔽一切"已回主线"判定（防抢跑）
        if not self._home_dwell_ok():
            return False
        # ⚠️ d_main 量不出（这张图没有主线）时**绝不**判"已回主线" ——
        #    否则角色会被扔回一条不存在的路线，等于站着不动。
        if self.home_d_main is None:
            return False

        # ── 硬闸：必须**真的站到主线上**才准退出回正（2026-09-13 实测修）────────
        # 为什么必须加（这段是踩坑实录，别删）：
        #   主线的 goal 圆点半径 2，会朝上下各铺 2 行 → 本项目的主线像素覆盖
        #   y=120~124。角色掉到坑底 y=126 时，到 goal 下沿 y=124 只有 **2px**，
        #   而 home_dist_tol=2 → `d_home(0) >= d_main(2) - 2 = 0` **恒成立**
        #   → 角色**刚进回正，下一帧就被踢出来**，回正线一步都走不完。
        #   实测日志：22:45:19 进 / 22:45:20 出 / 22:45:21 进 / 22:45:22 出 ……
        #   每秒进出 3~4 次，表现为「掉下去就再也回不来」。
        #
        #   更致命的是**跳跃途中**：人跳到 y=124（还在半空）就满足上面的退出式，
        #   主线立刻接管、给 left/right → 空中被横向带偏 → 摔回坑底。
        #   实测日志 22:45:21 帧10 在 (98,126) 正确拿到 `none none jump`（起跳点 98
        #   确实落在 97~101 的跳跃区里），但 22:45:22 人还在 y=124 就被踢出回正
        #   → 起跳修好了也白搭，这就是「跳了但永远上不去」。
        #
        # 所以退出回正必须**额外**要求：角色的 y 落在主线站立行 ±1 内。
        #   坑底 y=126 → |126-122|=4 > 1 → 不退出 ✓
        #   跳跃中 y=124 → |124-122|=2 > 1 → 不退出 ✓
        #   真站上平台 y=122 → 0 ≤ 1 → 退出 ✓
        _sy = getattr(self, "_main_stand_y", None)
        if _sy is not None and self.loc_player_global:
            _py = int(self.loc_player_global[1])
            if abs(_py - int(_sy)) > 1:
                self._home_stand_last_py = None
                return False      # 还在坑底 / 还在空中 → 继续沿回正线走
            # ★ 光"y 落在站立行 ±1"还不够 —— 必须**站定了**才算数。
            #   起跳过程中 y 会从 126 一路经过 124 / 123 / 122（甚至冲到 121），
            #   其中 123、122、121 都满足 ±1 —— 只判这一条的话，**人还在半空**
            #   就会被判「已回主线」踢出回正，主线立刻接管给 left/right →
            #   空中被横向带偏 → 摔回坑底。这正是「起跳了却永远上不去」的成因。
            #   所以再要求：**连续两帧 y 相同**（落地后 y 才会连续不变）。
            if getattr(self, "_home_stand_last_py", None) != _py:
                self._home_stand_last_py = _py
                return False      # y 还在变 = 还在空中 → 不许退出

        if self._home_line_keep(self.home_d_home, self.home_d_main):
            return False     # 还是回正线更近 → 保持回正，继续沿回正线走
        p = home_params(self.cfg)
        tol = int(p["home_dist_tol"])
        # 切回**最近的那一段**主线（多段巡逻时不能切回上一段，会往回走）
        seg = self._main_seg_i
        if seg is not None and 0 <= int(seg) < len(self.img_routes):
            self.idx_routes = int(seg)
            self.img_route = self.img_routes[int(seg)]
        # 走统一出口（它会清掉 home_* 那一组跨帧状态 + 重新武装这条线）
        self._exit_home_route(
            f"离回正线 {self.home_d_home}px ≥ 离主线 {self.home_d_main}px - {tol} "
            f"→ 主线优先（第 {self.idx_routes + 1}/{len(self.img_routes)} 段）")
        logger.info("[回正] 已回到主线，恢复主线巡逻")
        return True

    # ══════════════════════════════════════════════════════════════════
    # v0.9 第二版回正状态机（设计 3.6 签名）
    # ══════════════════════════════════════════════════════════════════

    def _build_mainline_cache(self) -> None:
        '''把全部主线（route{N}.png）的色码像素抽成 numpy 数组，供每帧算 d_main。

        ⚠️ 为什么必须预抽：d_main 要**每帧**算一次，而 268×187 全图 Python 双循环
           约 5 万次/帧，30fps 下吃不消。主线实际只有几十个像素，向量化后 < 20µs。

        ⚠️ 白色中转点 (255,255,255) 不在 color_code 里 → 天然不算主线像素
           （沿用第一版用例【10】：角色站在中转点上不会被误判成"已回主线"）。

        ⚠️ img_routes 运行期不变 → 只在 load_config 里建一次。
        '''
        codes = set(self.color_code) | set(self.color_code_up_down)
        xs, ys, segs = [], [], []
        for i, img in enumerate(self.img_routes or []):
            if img is None:
                continue
            for (x, y) in collect_pixels(img, codes):
                xs.append(int(x))
                ys.append(int(y))
                segs.append(int(i))
        if xs:
            self._main_pts_xy = (np.asarray(xs, dtype=np.int32),
                                 np.asarray(ys, dtype=np.int32))
            self._main_pts_seg = np.asarray(segs, dtype=np.int32)
            # ── 主线的「站立行」= 主线像素里 y 出现次数最多的那一排 ──────────
            # ⚠️ 为什么不能直接用「最近主线像素的 y」：goal 圆点半径 2，会朝上下
            #    各铺 2 行（本项目 route1 的像素覆盖 y=120~124，但真正走的只有
            #    y=122 那 13 个像素）。取众数才是角色真正站的那一排。
            #    它用来给「退出回正」加硬闸 —— 见 try_return_to_mainline。
            self._main_stand_y = int(np.argmax(np.bincount(
                np.asarray(ys, dtype=np.int32))))
        else:
            self._main_pts_xy = None
            self._main_pts_seg = None
            self._main_stand_y = None

    def _scan_home_lines(self, map_name, img_home_rgb, img_routes_rgb):
        '''加载期：把 route_home.png 解析成一组「回正线」。

        流程：split_components（一个 8-连通域 = 一条线）
              → 每条 calc_line_geom + far_segment_stats
              → validate_home_line 逐条判废 → **就地抹掉废线像素**（只涂黑那一个组件）
              → 存活的线填 xs/ys（numpy，运行期算 d_home 用）

        ⚠️ 判废粒度是**组件**，不是整张图：一张图可以有好几条回正线（左边一个坑、
           右边一个坑），一条画废了不能连坐其它几条。

        Args:
            map_name: 只用于日志
            img_home_rgb: 回正线 RGB 图（**会被就地修改**：废线像素被涂黑）
            img_routes_rgb: 主线 RGB 图列表（用来量 d_main / 覆盖跨度 / 终点贴主线）

        Returns:
            list[dict]: 可用回正线（已按"最上、最左"排序），元素结构见设计 3.3
        '''
        route_codes = set(self.color_code) | set(self.color_code_up_down)
        comps = split_components(img_home_rgb, route_codes)
        if not comps:
            logger.error(
                f"[回正线] minimaps/{map_name}/route_home.png 上一个路线像素都没有"
                f"（等于空图）—— 本次不启用回正。")
            return []

        lines = []
        n_bad = 0
        for comp in comps:
            geom = calc_line_geom(comp, img_routes_rgb, route_codes, self.cfg,
                                  img_rgb=img_home_rgb)
            far = cover_stats(comp, img_routes_rgb, route_codes, self.cfg)
            ok, why = validate_home_line(comp, img_home_rgb, img_routes_rgb,
                                         route_codes, self.cfg)
            if ok:
                item = {
                    "id": len(lines) + 1,
                    "start": geom["start"],
                    "goal": geom["goal"],
                    "span": geom["span"],
                    "has_jump": geom["has_jump"],
                    "n_pts": geom["n_pts"],
                    "far_pts": far["far_pts"],
                    "cover_xs": far["cover_xs"],
                    "cover_span": far["cover_span"],
                    "bbox": geom["bbox"],
                    # 运行期算 d_home 用的坐标数组（向量化）
                    "xs": np.asarray([p[0] for p in comp], dtype=np.int32),
                    "ys": np.asarray([p[1] for p in comp], dtype=np.int32),
                    "armed": True,       # 运行期：这条线还能不能被选中
                    "disarm_t": 0.0,     # 运行期：去武装时刻（超时兜底用）
                }
                lines.append(item)
                continue

            # ── 废线：只抹掉这一个组件（其它线照常生效）─────────────────────
            n_bad += 1
            for x, y in comp:
                img_home_rgb[y, x] = (0, 0, 0)
            where = (f"起点 {geom['start']}" if geom["start"] else
                     f"这一坨像素（{len(comp)} 个，左上角 {min(comp, key=lambda q: (q[1], q[0]))}）")
            logger.error(
                f"[回正线] minimaps/{map_name}/route_home.png 第 {len(lines) + n_bad} 条"
                f"（{where}）判为废线，已**只禁用这一条**：{why}\n"
                f"        同图其它回正线不受影响；修好这条之前，从这儿掉下去只能站桩打怪。")

        # P2-1：两条线的落点区间（起始段 x 范围）重叠 → 只是警告，不阻断
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                ci, cj = lines[i]["cover_xs"], lines[j]["cover_xs"]
                if not ci or not cj:
                    continue
                if ci[0] <= cj[1] and cj[0] <= ci[1]:
                    logger.warning(
                        f"[回正线] 第 {lines[i]['id']} 条与第 {lines[j]['id']} 条的落点区间"
                        f"（x {ci[0]}~{ci[1]} / x {cj[0]}~{cj[1]}）**重叠了** —— "
                        f"角色掉在重叠区可能走错线。不阻断，但建议两条线画在不同的坑上。")
        return lines

    def _log_home_health(self):
        '''P1-2：加载期逐条打一行中文体检（非程序员只看日志就该知道画得对不对）。

        一行包含：起点 / 终点 / 点数 / 是否含跳跃 / 落点覆盖 x 范围。
        （PRD 6.4 的文案样式：`#1 起点(94,128) → 终点(99,122)，21 px，含 1 次跳跃，
          落点覆盖 x94~108 ✓`）
        '''
        if not self.home_lines:
            return
        for a in self.home_lines:
            cov = a.get("cover_xs")
            cov_txt = f"x{cov[0]}~{cov[1]} ✓" if cov else "未知"
            logger.info(
                f"[回正线] #{a['id']} 起点 {a['start']} → 终点 {a['goal']}，"
                f"{a['n_pts']} px，{'含跳跃 ✓' if a['has_jump'] else '无跳跃'}，"
                f"落点覆盖 {cov_txt}")
        logger.info(f"[回正线] 共 {len(self.home_lines)} 条可用；"
                    f"角色离回正线比离主线近 "
                    f"{int(home_params(self.cfg)['home_dist_tol'])}px 以上就自动回正"
                    f"（不需要等到「看不见主线」）")

    def _home_line_armed(self, line) -> bool:
        '''这条线现在**能不能被选中**（去武装后的重新武装逻辑）。

        去武装后两种方式恢复（取或）：
          · 角色离开这条线的 **bbox** ≥ home_rearm_margin（默认 4px）
          · 或去武装满 home_rearm_timeout（默认 30s）无条件恢复

        ⚠️ 兜底那条必须有：否则角色一直在坑底晃悠时这条线会**永远**武装不回来
           （等于这条线被静默删除）。

        ⚠️ 本函数有副作用（恢复时会写回 line["armed"]）—— 它是**唯一的**重新武装点，
           由 `_home_dist()` 在过滤前逐条调用，保证每帧都刷新。
        '''
        if not line:
            return False
        if line.get("armed", True):
            return True
        p = home_params(self.cfg)
        disarm_t = line.get("disarm_t", 0.0)
        if disarm_t and time.time() - disarm_t > float(p["home_rearm_timeout"]):
            line["armed"] = True
            line["disarm_t"] = 0.0
            return True
        if self.loc_player_global and line.get("bbox"):
            x0, y0, x1, y1 = line["bbox"]
            px, py = int(self.loc_player_global[0]), int(self.loc_player_global[1])
            # 点到包围盒的曼哈顿距离（在盒内 = 0）
            d = max(int(x0) - px, 0, px - int(x1)) + max(int(y0) - py, 0, py - int(y1))
            if d >= int(p["home_rearm_margin"]):
                line["armed"] = True
                line["disarm_t"] = 0.0
                return True
        return False

    def _home_dist(self):
        '''本帧：角色到**已武装**回正线的最近距离 → (d, line)。

        ★ 防御 D2：必须**跳过 armed=False 的线**（在本函数内部过滤，不是在外面）。
          否则去武装形同虚设 → 「8s 超时 → 下一帧立刻重进」的死锁必然复活：
          新判定天然不带 armed 概念，超时退出后角色仍在坑底、d_home 还是 0。

        ⚠️ 为什么"内部过滤"而不是"外层再判一次 armed"：
          只要有一处漏判 armed，死锁就复活。口径统一为 ——
          `_home_dist()` 的返回值语义 = 「**当前可用的**回正线里最近的距离」，
          `None` = 没有可用回正线（进不了回正）。

        ⚠️ 本函数会顺带调 `_home_line_armed()` 刷新每条线的武装状态：
          没有这个刷新，去武装的线永远武装不回来（等于被静默删除）。

        Returns:
            (int | None, dict | None): (最近距离, 那条线)；没有可用线 → (None, None)
        '''
        if not self.loc_player_global:
            return None, None
        best_d, best_line = None, None
        for ln in self.home_lines or []:
            self._home_line_armed(ln)          # 先刷新武装状态（内部只在需要时改）
            if not ln.get("armed", True):
                continue                       # ★ D2：未武装的线**不算** d_home
            d = dist_to_pixels(ln.get("xs"), ln.get("ys"), self.loc_player_global)
            if d is None:
                continue
            if best_d is None or d < best_d:   # 并列时取靠前那条（id 小者）
                best_d, best_line = int(d), ln
        return best_d, best_line

    def _main_dist(self):
        '''本帧：角色到最近主线像素的曼哈顿距离 + 那个像素属于第几段 → (d, seg_i)。

        ⚠️ 走 `_main_pts_xy` 缓存（O(像素数) 向量化），不是每帧全图扫描。
        ⚠️ **全图**最近像素，不受 search_range / rescue_range 限制 ——
           套搜索框会复活第一版坑 1（坑底离主线 6px < 10 → 判定在主线上 → 永不回正）。

        Returns:
            (int | None, int | None): 没有主线 / 还没定位 → (None, None)
        '''
        if self._main_pts_xy is None or not self.loc_player_global:
            return None, None
        xs, ys = self._main_pts_xy
        px, py = int(self.loc_player_global[0]), int(self.loc_player_global[1])
        d_arr = np.abs(xs - px) + np.abs(ys - py)
        i = int(np.argmin(d_arr))
        return int(d_arr[i]), int(self._main_pts_seg[i])

    def _home_line_keep(self, d_home, d_main) -> bool:
        '''= home_should_return(d_home, d_main, tol)（tol 从配置读，<0 回落默认 2）。

        ⚠️ 进入 / 保持 / 退出**共用这一处**，禁止在别处重写这个比较 ——
           第一版"警告口径和退出口径不一致"就是这么来的。

        ★ 坑底例外（2026-09-15 真机实测加）：角色在**主线站立行下方 3px 以上**
          （= 真掉在平台下面，见 `_home_below_mainline`）时，**容差收成 0**。

          为什么必须收：`d_main` 在坑底是"**隔着地板看见的**"，会虚小 ——
          主线在平台两端 x=94 / x=104 有斜坡像素落在 **y=124**，离坑底 y=126
          只有 **2px**，而 `tol=2` 的预算正好被这 2px 吃光：
              `d_home(0) < d_main(2) - 2` 即 `0 < 0` → 判"你在主线上" → **不进回正**
          → 角色**最可能掉下去的两个边缘位置**，恰好是**唯一进不了回正**的两个位置
          → 掉下去就永远回不来（再叠加窄平台降级吞掉左右移动 = 站着不动、不报卡死）。

          收成 0 之后判据**仍然生效**（依旧要求"离回正线比离主线更近"），只是不再
          要求"近 2px 以上" —— 所以"接不住的回正线"照样进不去
          （见 verify_home_route【15】：只画竖线时 d_home=8 / d_main=8 → `8 < 8` 不成立）。

          实测（落点 y=126 逐像素，调 `_home_line_pick`）：修前 **x≤94 与 x=104
          共 12 个落点进不了回正**；修后 **x=84~116 全部能进**，而主线上 y=122~124
          的 69 个点**一个都不会被误拉进回正**（无"抢跑"）。
        '''
        return home_should_return(d_home, d_main, self._home_tol_now())

    def _home_tol_now(self) -> int:
        '''本帧**实际生效**的容差 —— 判定与日志**共用这一处**（2026-09-15 加）。

        ⚠️ 为什么必须有它：坑底会把容差收成 0（见 `_home_line_keep`），
           而进入回正那条日志原来是**自己**读 `home_dist_tol` 拼字符串的 →
           坑底明明按 0 算，日志却打「离回正线 2px < 离主线 4px - **2**」，
           看起来像 `2 < 2` 不成立却进了回正 —— **日志说谎比没日志更坏**。
           现在判定和日志都从这里取，不可能再对不上。
        '''
        tol = int(home_params(self.cfg)["home_dist_tol"])
        if self._home_below_mainline():
            tol = 0
        return tol

    def _home_below_mainline(self) -> bool:
        '''角色是否在主线**站立行下方**（= 掉在平台下面 / 坑里）。

        ⚠️ 为什么要它（2026-09-15 真机实测踩出来，用户原话
           「中间和右边边缘掉下去都能立马回来，**左边**不知道是不是超出当初录制的
             回正线了，所以没回来也没反应，**原地不动**」）：

        进入回正的判据是 `d_home < d_main - tol`（比两条线谁更近）。它有个**前提**：
        `d_main` 必须真的代表"角色离主线的距离"。而在坑底这个前提**不成立** ——
        主线在平台两端（x=94 / x=104）有斜坡像素落在 **y=124**，而坑底是 **y=126**
        → `d_main` 只有 **2px**（隔着地板"看见"的），而 `d_home` 也是 0~2px
        → `d_home(0) < d_main(2) - 2` 即 `0 < 0` **不成立**
        → 判"你在主线上" → **不进回正**。

        结果极其讽刺：**角色最可能掉下去的两个边缘位置，恰好是唯一进不了回正的位置**。
        而掉下去之后又因为窄平台降级吞掉左右移动 → 站着不动、也不报卡死
        → 永远出不来（用户看到的"没反应，原地不动"）。

        实测（落点 y=126，逐像素）：**x≤94 与 x=104 共 12 个落点进不了回正**，
        x=95~103 / 105~116 都能进。

        ⇒ 判据本身没写错，是**坑底离主线比设计假设的近**：设计按"坑底离主线 6px"
          验算（config 注释），实测只有 4px、两端更只有 2px —— tol=2 的预算被吃光。

        这道闸与**退出侧**那道硬闸（`try_return_to_mainline` 里"必须站在站立行 ±1
        才算回到主线"）正好对称：一个防"在坑底被误判成**已回**主线"，
        一个防"在坑底被误判成**在主线上**"。

        Returns:
            bool: True = 人在站立行**下方 3px 以上**（`py > stand_y + 2`）

        ⚠️ 为什么门槛是 `+2`（下方 3px）而不是退出侧那个 `±1`：
           主线像素**不止站立行一行** —— 本项目 route1/route2 的 y 覆盖 120~124
           （goal 圆点上下各铺 2 行 + 两端斜坡）。所以 y=124 是**主线自己的斜坡行**，
           角色正常巡逻踩上去时 y 也会到 124 —— 那是"在主线上"，不是"掉下去了"。
           拿 ±1 当门槛会把"踩斜坡"误判成"掉坑" → 一到平台边缘就抢跑进回正。
           实测：坑底 y=126、斜坡 y=124 ⇒ `+2`（=125 起算）正好把两者分开，
           且对 ±1px 的坐标抖动留了 1px 余量（斜坡读到 125 也不会误触发）。
        '''
        _sy = getattr(self, "_main_stand_y", None)
        if _sy is None or not self.loc_player_global:
            return False
        try:
            return int(self.loc_player_global[1]) > int(_sy) + 2
        except (TypeError, ValueError):
            return False

    def _home_line_pick(self):
        '''运行期：这一帧该不该进回正、进的是哪条线。

        三条闸（**缺一不可**，设计 11-⑪）：
          ① `_home_dist()` 拿到最近的**已武装**回正线（内部已过滤未武装的线 = D2）
          ② `_home_line_keep()` 成立（d_home < d_main - tol）
          ③ `_home_reenter_allowed()`（冷静期必须卡在**新入口**上 = D1）

        ⚠️ D1 为什么必须在这：第一版只在「主线一帧像素都找不到」的老路径上调冷静期，
           新入口根本没调 —— 于是超时退出后下一帧从新入口直接重进，
           `home_enter_t` 每帧归零，8s 超时退化成"每帧重置"（实测 120/120 帧卡死）。

        ★ 缓存失效时 **d_home 和 d_main 一并作废、一并重算**（不各自为政）：
          调用方（插入点①）本来就会先算好 `home_d_main` / `home_d_home`，
          正常路径**一次都不用重算**（每帧计算量不变）。
          但"缓存里的线已被去武装 / 本方法被直接调用（没走插入点①）"时，
          如果只重算 d_home 而复用 `home_d_main`，就会拿**上一帧、甚至上一个位置**
          的主线距离去比 —— 本函数不再自洽，悄悄依赖"调用方一定先算过 d_main"这个
          没有写下来的约定（换个调用点就静默出错）。一并重算才自洽。

        Returns:
            dict | None: 该进的那条回正线；不该进返回 None
        '''
        if not self.home_lines or not self.loc_player_global:
            return None
        d_home, line = (self.home_d_home, self._home_line_near)
        d_main = self.home_d_main
        # ⚠️ 缓存可能过期（这一帧内刚被去武装 / 直接调用本方法没走插入点①）：
        #    只要缓存里的线已经不是"已武装"、或 d_main 还没算出来，就**两个距离一起重算**。
        #    宁可多算一次，也不能拿着一条刚被去武装的线把人拉回回正（那正是 D2 要防的死锁），
        #    也不能拿一个过期的 d_main 去比（那会让进入判定反向）。
        if line is None or not line.get("armed", True) or d_main is None:
            d_home, line = self._home_dist()
            d_main, _seg = self._main_dist()
        if line is None:
            return None
        # 坑底把容差收成 0 的处理在 `_home_line_keep` 里（口径唯一，别在这重写比较）
        if not self._home_line_keep(d_home, d_main):
            return None
        if not self._home_reenter_allowed():      # ★ D1：冷静期卡新入口
            return None
        return line

    def _enter_home_route(self, reason: str, line=None):
        '''**进入回正状态的唯一入口**（禁止在别处直接写 is_using_home_route）。

        ⚠️ 没有回正线（img_route_home 为 None）时**什么都不做** —— 这是最重要的保护：
           进去了又没有线可走 = 指令真空 + 不打怪 = 死局。
        '''
        if self.is_using_home_route:
            return
        if self.img_route_home is None:
            return
        self.is_using_home_route = True
        self.img_route = self.img_route_home
        self.home_line = line
        self.home_enter_pos = (tuple(self.loc_player_global)
                               if self.loc_player_global else None)
        self.home_enter_t = time.time()
        self.home_frame = 0
        # 重置「站稳判定」的上一帧 y：不清掉的话，进入回正那一刻可能碰巧
        # 和上一帧 y 相同，被误判成「已经站稳」，回正线一步都走不出去。
        self._home_stand_last_py = None
        # ── 回正起跳埋点：计数**必须在这里清零** ──────────────────────────
        # 不清零就会跨轮累计 —— 第二轮看到的「走到起跳点 5 次」其实是好几轮
        # 加起来的，数字是假的，比没有还糟（会直接把排查带沟里）。
        # 起点不用新建变量：上面 `home_enter_pos` 已经记好了。
        self.home_jump_n = 0
        self._home_jump_on = False
        self.home_jump_ts = []
        self.home_y_min = None
        self.home_y_max = None
        self.home_d_home0 = getattr(self, "home_d_home", None)
        self.home_d_main0 = getattr(self, "home_d_main", None)
        # 一进回正就取消归位：归位是"回到主线之后"才做的事，
        # 人又掉坑了还在归位，两套指令会打架。
        self._recenter_active = False
        self._recenter_n = 0
        logger.info(f"[回正] {reason} → 进入回正状态，沿回正线走回主线"
                    f"（起点 "
                    f"{tuple(self.loc_player_global) if self.loc_player_global else '?'}，"
                    f"主线站立行 y={getattr(self, '_main_stand_y', '?')}，"
                    f"离回正线 {getattr(self, 'home_d_home', '?')}px"
                    f" / 离主线 {getattr(self, 'home_d_main', '?')}px）")

    def _exit_home_route(self, reason: str, disarm: bool = False):
        '''**退出回正状态的唯一出口**（三条路：回主线 / 接不上 / 8s 超时）。

        Args:
            reason: 中文原因（进日志）
            disarm: True = 这次回正**以失败告终**（超时 / 接不上）→ **这条线**去武装，
                    并且给**所有**入口上一段冷静期（home_reenter_lock）；
                    False = 成功回到主线 → 立刻重新武装 + 解除冷静期
                    （下次真的掉下去要能马上接住）

        ⚠️ 为什么失败退出必须去武装 + 上冷静期（QA 实测必修的 Bug，第二版更严重）：
            超时退出这一帧，角色**仍站在坑底**（d_home 还是 0）→ 新判定
            `d_home < d_main - tol` **下一帧仍然成立** → 立刻重进 →
            home_enter_t / home_frame 全部归零 → 8s 超时变成"每帧重置"，
            而回正期间不打怪 = 「既不走也不打」的死循环。
            去武装（D2）+ 冷静期（D1）一起把这个环断开。
        '''
        if not self.is_using_home_route:
            return
        self.is_using_home_route = False
        # ── 埋点：抓快照（**必须在下面清空 home_* 之前**，否则抓到的全是 None）──
        # 只读、不写任何状态，纯观测。所有字段一律 getattr 兜底：
        # tools/verify_navigation.py 的 Stub 不走 __init__，直接 self.xxx 会崩。
        _ent = float(getattr(self, "home_enter_t", 0) or 0)
        _snap = {
            "result": "超时/接不上退出" if disarm else "成功回主线",
            "reason": reason,
            "dur_s": (time.time() - _ent) if _ent else 0.0,
            "frames": int(getattr(self, "home_frame", 0) or 0),
            "start": getattr(self, "home_enter_pos", None),
            "end": (tuple(self.loc_player_global)
                    if getattr(self, "loc_player_global", None) else None),
            "jump_n": int(getattr(self, "home_jump_n", 0) or 0),
            "y_min": getattr(self, "home_y_min", None),
            "y_max": getattr(self, "home_y_max", None),
            "d_home": getattr(self, "home_d_home", None),
            "d_main": getattr(self, "home_d_main", None),
            "mm_score": getattr(self, "minimap_score", None),
        }
        try:
            _ts = list(getattr(self, "home_jump_ts", None) or [])
            _snap["jump_gap"] = (_ts[-1] - _ts[-2]) if len(_ts) >= 2 else None
        except (TypeError, IndexError):
            _snap["jump_gap"] = None
        ln = self.home_line
        p = home_params(self.cfg)
        if ln is None:
            tail = "（这次回正不是某条回正线触发的，无需改动线的状态）"
        elif disarm:
            ln["armed"] = False
            ln["disarm_t"] = time.time()
            tail = (f"回正线 #{ln.get('id')} **暂时去武装** —— 需离开它的范围 "
                    f"{int(p['home_rearm_margin'])}px 或 {int(p['home_rearm_timeout'])}s 后"
                    f"才会再次接人（防「8s 回正 → 立刻重进」的死循环）")
        else:
            ln["armed"] = True
            ln["disarm_t"] = 0.0
            tail = f"回正线 #{ln.get('id')} 已重新武装（下次掉下去还会接住）"
        # 冷静期：失败退出 → 上锁；成功回主线 → 解锁（不然下次真掉下去要白等 10s）
        if disarm:
            self.home_reenter_lock_t = time.time()
        else:
            self.home_reenter_lock_t = 0.0
        self.home_line = None
        self.home_enter_pos = None
        self.home_enter_t = 0.0
        self.home_frame = 0
        # 把导航图交回主线（没主线就保持现状，由打怪逻辑接管移动）
        if self.img_routes:
            i = max(0, min(self.idx_routes, len(self.img_routes) - 1))
            self.idx_routes = i
            self.img_route = self.img_routes[i]
        logger.info(f"[回正] {reason} → 退出回正状态，恢复主线巡逻；{tail}")
        # ── 回正**成功** → 先走回安全区中心再正常巡逻（见 _apply_recenter）──
        # 失败（超时 / 接不上）时**不**归位：人还在坑底，归位只会跟回正打架。
        self._recenter_active = (not disarm)
        self._recenter_n = 0
        # ★ 归位新开一轮 → 清掉上一轮的"上一帧 x"（2026-09-15 加，见 _apply_recenter）
        self._recenter_last_px = None
        # ── 埋点：一次回正压成一行小结（区分 A / B / C 全靠它）────────────
        #   A 没走到起跳点 → 小结里「**一次都没走到起跳点**」
        #   B 被冷却吞了   → 「走到起跳点 N 次」但键盘侧只有 1 条「真的按下了跳跃键」
        #   C 跳了没上去   → 按了 ≥2 次，但「期间 y」仍然贴着坑底
        # ⚠️ 打日志失败绝不能影响退出回正 —— 这儿抛异常 = 回正退不出去 = 卡死。
        if jump_trace_on(self.cfg):
            try:
                logger.info(home_summary_line(_snap))
            except Exception:
                pass

    def _home_dwell_ok(self) -> bool:
        '''最小驻留门（**保险丝，默认关闭**）：进入回正后，得先"真的动起来了 /
        过了一会儿"才允许判定退出。

        条件（取**或**）：
          ① 已离开进入点 ≥ home_min_leave（默认 0 = 关闭）
          ② 已处于回正态 ≥ home_min_frames（默认 0 = 关闭）

        ⚠️ 为什么默认关：第二版靠「走到终点时 d_home = d_main = 0 → 相等时主线优先」
           自动处理抢跑，理论上不需要。实测若发现"一进就出"的抖动，把这两个值
           调回 6 / 15 即可（这就是保留保险丝的意义，不写死删）。

        ⚠️ home_enter_pos 为 None（没走进入口 / 还没定位）时返回 True：
           量不出位移就不屏蔽，交给距离判据，避免"门永远打不开"。
        '''
        if not self.is_using_home_route:
            return True
        p = home_params(self.cfg)
        if self.home_frame >= int(p["home_min_frames"]):
            return True
        if self.home_enter_pos is None or not self.loc_player_global:
            return True
        return manhattan(self.loc_player_global, self.home_enter_pos) >= int(p["home_min_leave"])

    def _home_reenter_allowed(self) -> bool:
        '''现在**能不能进回正**（冷静期）。

        ⚠️ 第二版改：这条**卡所有入口**（新判定 `_home_line_pick` + 老兜底
           「主线一帧像素都找不到」），不再只卡老路径 —— 见 D1。

        回正失败（超时 / 接不上）退出后的 home_reenter_lock 秒内一律不准再进 ——
        理由见 _exit_home_route 的注释：否则下一帧被拉回来，超时和驻留门全部失效。
        '''
        # ⚠️ home_params 已经把 0 / 负数回落成默认 10（D4），这里只管读
        lock = float(home_params(self.cfg)["home_reenter_lock"])
        if lock <= 0:
            lock = float(DEFAULT_HOME_PARAMS["home_reenter_lock"])
        if not self.home_reenter_lock_t:
            return True
        if time.time() - self.home_reenter_lock_t >= lock:
            self.home_reenter_lock_t = 0.0   # 冷静期自然到期 → 解锁
            return True
        if time.time() - self.t_last_home_lock_log > 10:
            self.t_last_home_lock_log = time.time()
            logger.info(
                f"[回正] 角色 {self.loc_player_global} 现在应该回正，但上一次回正刚失败、"
                f"还在 {lock:.0f}s 冷静期内 —— 这一轮先打怪，不重复往回正线里钻。\n"
                f"        想让它早点再试：重画这条回正线（终点画到主线上），"
                f"或把 route.home_reenter_lock 调小（秒）。")
        return False

    def _calc_seg_goals(self):
        '''对外入口：按 endpoint_margin 内缩终点，并保证两端终点**不至于靠太近**。

        ⚠️★ 2026-09-13 用户实测修：内缩到"两端终点只差 2px"时，`_near_seg_goal`
        的判定容差 reach 会让两段的"已到达"区间**完全重叠** —— 切到其中一段后
        条件恒为真、永远等不到"离开"，就再也切不回来，角色一路走出主线掉下平台
        （日志表现：段1 的判定是 x ≤ 104，而角色一路往左走根本到不了 104）。
        所以这里**从配置值往下试**，直到内缩后两端至少隔 4px。
        单段路线不存在"切段"的问题，直接用配置值。
        '''
        try:
            _m = int(self.cfg.get("route", {}).get("endpoint_margin", 3) or 0)
        except (TypeError, ValueError):
            _m = 3
        _m = max(0, _m)
        for m in range(_m, -1, -1):
            self._calc_seg_goals_with(m)
            if len(self._seg_goals) < 2 or self._seg_goal_gap >= 4:
                break

    def _calc_seg_goals_with(self, _margin):
        '''每一段的终点（goal 圆点质心）+ 主方向（这段主要是往左还是往右走）。

        平台和路线可能很短（用户实测只有 14px），光靠"踩到 goal 像素"判定到段尾
        太脆弱 —— 见 _near_seg_goal 的说明。

        Args:
            _margin: 终点朝段内收缩多少像素（0 = 不缩）
        '''
        self._seg_goals = []
        self._seg_dir = []
        self._seg_span = []
        # ── 端点安全余量（2026-09-13 加）────────────────────────────────
        # 主线是"录到哪走到哪"，很多人会一路录到平台边缘，于是终点就贴在悬崖上。
        # 走到终点才掉头 + 判定有一帧延迟 + 移动惯性 = 踩空掉下平台
        # （用户实测：录制时没事，挂机久了却会走出主线掉下去）。
        # 所以把终点朝**段内**收缩 margin 像素，让角色提前掉头。
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        for img in self.img_routes:
            pts = self._route_pixels(img)
            if pts:
                _xs = [q[0] for q in pts]
                _ys = [q[1] for q in pts]
                self._seg_span.append(max(max(_xs) - min(_xs),
                                          max(_ys) - min(_ys)))
            else:
                self._seg_span.append(0)
            goals = [(x, y) for x, y in pts if tuple(img[y, x][:3]) in goal_codes]
            if goals:
                gx = sum(p[0] for p in goals) // len(goals)
                gy = sum(p[1] for p in goals) // len(goals)
                if _margin > 0 and pts:
                    # 只沿**主导轴**（跨度大的那根轴）收，免得斜着把终点推离路线
                    _sx = max(q[0] for q in pts) - min(q[0] for q in pts)
                    _sy = max(q[1] for q in pts) - min(q[1] for q in pts)
                    # ⚠️ 上限保护：**原来**是「不得超过跨度的 20%」，实测证明是错的。
                    #    2026-09-13 用户实测：本图跨度 10px → 20% 只让收 2px，
                    #    而角色冲过终点的**过冲量正好也是 2px** —— 内缩被过冲完全吃掉，
                    #    等于白缩，照样踩到 x=94/104 的斜坡掉进坑。
                    #    教训：**内缩量必须明显大于过冲量才有效**，按"跨度百分比"封顶
                    #    在短路线上必然把内缩压到和过冲同一量级。
                    #    改成"至少保留 3px 巡逻宽度"的硬下限：(_span - 4) // 2。
                    #    （留 3px 而不是 1px：两端塌到同一个点时角色会原地抖动，
                    #      而且彻底失去巡逻意义 —— 用户虽说不介意范围小，但定点模式
                    #      他早就因为识别问题放弃了。）
                    #    用户已明确：巡逻范围无所谓，一切以不掉落为优先。
                    _span = max(_sx, _sy)
                    _margin_eff = min(_margin, max(1, (_span - 4) // 2))
                    if _sx >= _sy:
                        _cx = sum(q[0] for q in pts) / len(pts)
                        _lo, _hi = min(q[0] for q in pts), max(q[0] for q in pts)
                        if gx > _cx:
                            gx = max(_lo, gx - _margin_eff)   # 终点在右端 → 往左收
                        elif gx < _cx:
                            gx = min(_hi, gx + _margin_eff)   # 终点在左端 → 往右收
                    else:
                        _cy = sum(q[1] for q in pts) / len(pts)
                        _lo, _hi = min(q[1] for q in pts), max(q[1] for q in pts)
                        if gy > _cy:
                            gy = max(_lo, gy - _margin_eff)
                        elif gy < _cy:
                            gy = min(_hi, gy + _margin_eff)
                    logger.debug(f"[巡逻] 终点内缩 {_margin_eff}px"
                                 f"（上限 {_margin}，防走出平台）→ ({gx},{gy})")
                self._seg_goals.append((gx, gy))
            else:
                self._seg_goals.append(None)
            # 主方向：数一下这段里 left / right 像素哪个多
            nl = nr = 0
            for x, y in pts:
                cmd = (self.color_code.get(tuple(img[y, x][:3]))
                       or self.color_code_up_down.get(tuple(img[y, x][:3])))
                if not cmd:
                    continue
                parts = cmd.split()
                if parts and parts[0] == "left":
                    nl += 1
                elif parts and parts[0] == "right":
                    nr += 1
            self._seg_dir.append("left" if nl > nr else ("right" if nr > nl else None))

        # ── 内缩后相邻终点的实际间距（_near_seg_goal 用它给 reach 封顶）──
        self._seg_goal_gap = 0
        _fg = [g for g in self._seg_goals if g]
        if len(_fg) >= 2:
            self._seg_goal_gap = min(
                max(abs(_fg[i][0] - _fg[i - 1][0]),
                    abs(_fg[i][1] - _fg[i - 1][1]))
                for i in range(len(_fg)))

    def _near_seg_goal(self):
        '''角色是否**走过了**当前段的终点（不要求踩到 goal 像素）。

        为什么需要它：goal 圆点半径只有 2px，而玩家坐标是**质心**、踩不中就判不到 ——
        平台本来就不长（用户实测路线跨度只有 14px），角色走过去却没触发切段，
        冲出去后再也找不到路线像素。表现就是「到了终点停不下来一直往前走」。
        ⚠️★ 2026-09-16 订正：这里原来写的是"角色一帧能移动十几像素"，**那是推断、
           从没实测**；实测只有 ~0.3 px/帧（≈4~5 px/秒，依据见下面"上限三"那段），
           差了 20~50 倍。症状是真的，数字是猜的，别再引用它。

        ⚠️ 必须**方向感知 + 边沿触发**（2026-09-12 模拟发现的坑）：
          光用"离终点够近"会出大事 —— 用户这两段终点只相距 10px（104 vs 94），
          而 goal_reach_range 默认 15 > 10，两段终点的判定范围**重叠**，
          角色走到中间时两段都算"已到达" → 每帧来回切段（模拟实测 60 帧切了 60 次）。
          所以：① 按本段主方向判断"是否越过终点"；② 切过一次后要先**离开**终点
          才重新武装（边沿触发），避免重复触发。

        Returns:
            bool: 是否应该切到下一段（每进入一次终点范围只返回一次 True）
        '''
        if not self.img_routes or self.is_using_home_route:
            return False
        i = self.idx_routes
        if i >= len(self._seg_goals):
            return False
        g = self._seg_goals[i]
        if g is None or not self.loc_player_global:
            return False
        reach = self.cfg.get("route", {}).get("goal_reach_range", 15)
        # ⚠️ 上限一：不能超过本段跨度的一半：平台本来就不长时（用户路线跨度 14px），
        #    15px 的判定范围会导致"一起步就算到达终点"，这一段根本走不完。
        span = self._seg_span[i] if i < len(self._seg_span) else 0
        if span:
            reach = min(reach, max(3, int(span * 0.5)))
        # ⚠️★ 上限二（2026-09-13 实测修，**比上限一关键**）：
        #    reach 还不能超过「相邻两个终点间距」的一半减 1。
        #    为什么：两段是**交替**的 —— 段0(向右) 在 x ≥ g0-reach 触发，
        #    段1(向左) 在 x ≤ g1+reach 触发。若 reach 大到让 g0-reach ≤ g1+reach，
        #    两个判定区就重叠了：切到段1 后条件**恒为真**，而"离开"要求
        #    x > g1+reach 才重新武装 —— 角色正往左走，永远到不了 →
        #    **再也切不回段0，一路走出主线掉下平台**。
        #    实测就是这么坏的：g0=100 / g1=98 / reach=6 → 阈值变成
        #    "x ≥ 94" 和 "x ≤ 104"，把整个巡逻区都罩住了，段1 永远出不来。
        gap = getattr(self, "_seg_goal_gap", 0) or 0
        if gap:
            reach = min(reach, max(1, (gap - 2) // 2))
        # ⚠️★ 上限三（2026-09-16 真机实测修，**这条才是关键**）：
        #    上限二只保证"两个判定区不重叠"，但允许它们**贴在一起** ——
        #    gap=24 时 reach=11：段0 在 x ≥ 76-11 = 65 触发、段1 在 x ≤ 52+11 = 63 触发
        #    ⇒ 角色只在 **63~65（2px）** 里来回蹭。
        #    真机实测（2026-09-16 23:22 那次）：录了 24px 的路，日志里全局 x 只在
        #    62~66 之间动，看起来就像"卡在一个很小的范围里"。
        #
        #    ⚠️ 为什么上限一/二都没拦住：它们都建立在同一个**从没实测过的前提**上 ——
        #       "角色一帧能走十几像素"（见 __init__ 里那段注释，2026-09-12 写的）。
        #       2026-09-16 用真机日志实测：**~0.3 px/帧（≈4~5 px/秒）**，差了 20~50 倍。
        #       前提错 → 容差被设成 15 → 短路线被整条吞掉。
        #       实测依据（三处互相印证）：录制 7 秒走 ~32px；挂机 5 秒走 18px；
        #       用户自述"一个来回约 10 秒"（来回 48px）⇒ 都是 4~5 px/秒。
        #
        #    ⇒ 按 gap 的比例封顶，取 1/16、下限 1px：
        #      · gap=24（本图）→ reach=1 ⇒ 可用行程 22px（原来只有 2px）
        #      · gap=240 以上时 1/16 已 ≥ 配置值 15 ⇒ **长路线完全不受影响**
        #    用户 2026-09-16 定："不太需要担心角色掉坑的问题了，容差甚至可以缩到 1px"。
        #    ⚠️ 这条推翻了 2026-09-13 记下的旧前提（"巡逻范围无所谓、一切以不掉落为优先"），
        #       见下方 `_goal_armed` 那段注释里的说明。
        if gap:
            reach = min(reach, max(1, gap // 16))
        px, py = self.loc_player_global
        direction = self._seg_dir[i] if i < len(self._seg_dir) else None
        # ⚠️ 没有主方向时（这段 left/right 像素一样多，`_seg_dir` 会是 None）**不能**给 1px：
        #    那条分支只能靠"离终点够近"判定（没有方向感知的"越过"兜底），
        #    1px 等于要求踩点，角色一帧 0.3px 也可能判不到 → 永远不切段。
        #    那种情况至少留 3px。
        if direction is None:
            reach = max(reach, 3)

        if direction == "right":
            passed = px >= g[0] - reach
        elif direction == "left":
            passed = px <= g[0] + reach
        else:
            passed = (abs(px - g[0]) + abs(py - g[1])) <= reach

        # ★★ 换段 → **无条件重新武装**（2026-09-13 逐帧实测修，别改回 `not passed`）
        #
        #   ❌ 老写法 `self._goal_armed = not passed` 的致命问题：
        #      角色一帧能走 1.5px（实测值），**切到新段的那一帧**角色往往已经站在
        #      新段的终点区里了（本图段0 终点 101、段1 终点 97，gap 只有 4）。
        #      于是 `not passed` = False = "这一段的终点已经消费过了" → 此后
        #      `passed` 恒为真、`if not passed: _goal_armed = True` 这条**永远进不去**
        #      → 再也切不回段0 → 一路走出平台掉下去。
        #
        #   逐帧实测（废都南方工地，两段，段0 向右 终点 (101,122) / 段1 向左 终点
        #   (97,122)，gap=4 → reach=1，平台 92~106）：
        #        f0  seg=0 x= 98.0 px= 98 dir=right passed=False armed=True  → FIRE=False
        #        f1  seg=0 x= 99.5 px=100 dir=right passed=True  armed=True  → FIRE=True（切到段1）
        #        f2  seg=1 x= 98.0 px= 98 dir=left  passed=True  armed=False → FIRE=False ← 死锁开始
        #        f3  seg=1 x= 96.5 px= 96 dir=left  passed=True  armed=False → FIRE=False
        #        …… f13 已经跑到 x=81.5，平台在 92~106，早就掉下去了
        #
        #   ✅ 正确语义：**进入新段后的第一个 passed=True 的帧就触发**，
        #      而不是"进入新段时若已在终点区内就当已经触发过"。
        #      改完后 f2 立刻 FIRE 切回段0，角色在 98~100 之间来回蹭 ——
        #      巡逻范围变小了，但用户明确：「一切以不掉落为优先」，宁可蹭 3px。
        #      ⚠️★ 2026-09-16 更新：这条前提**已被用户推翻** —— 他当天说
        #         "目前不太需要担心角色掉坑的问题了，容差甚至可以缩到 1px"。
        #         所以 `_near_seg_goal` 新增了上限三（reach ≤ gap/16，下限 1px），
        #         本图（gap=24）reach 从 11 降到 1 ⇒ 可用行程从 2px 变成 22px。
        #         「宁可蹭 3px」不再成立，改成"在保证不掉落的前提下尽量走满"。
        #         掉落的防线仍在：`endpoint_margin`（终点朝内缩，见 _calc_seg_goals_with）。
        _left = not passed
        if _left or getattr(self, "_goal_armed_seg", -1) != i:
            self._goal_armed_seg = i
            self._goal_armed = True
        if _left:
            return False                # 还没走到终点区 → 不切段（顺带重新武装）
        if not self._goal_armed:
            return False                # 刚切过、还没离开过 → 不重复触发
        self._goal_armed = False
        return True

    def _calc_pingpong_start(self):
        '''单段路线的"另一端"—— 往返走时用它判断"该掉头了"。

        路线是**单向**编码的（从 A 走到 B），只有终点 B 有 goal 标记，起点 A 什么都没有。
        所以 A 只能反推：取**离终点最远**的那个路线像素（对一条不折返的线成立）。
        '''
        self._pingpong_start = None
        if len(self.img_routes) != 1:
            return   # 多段靠 goal 切段循环，不需要这个
        img = self.img_routes[0]
        goal_codes = {c for c, v in self.color_code.items()
                      if v.strip().split()[-1] == "goal"}
        pts = self._route_pixels(img)
        goals = [(x, y) for x, y in pts if tuple(img[y, x][:3]) in goal_codes]
        if not pts or not goals:
            return
        gx = sum(p[0] for p in goals) // len(goals)
        gy = sum(p[1] for p in goals) // len(goals)
        self._pingpong_start = max(
            pts, key=lambda p: abs(p[0] - gx) + abs(p[1] - gy))
        logger.info(f"[巡逻] 只录了 1 段主线 → 启用自动往返"
                    f"（终点 ({gx},{gy}) ↔ 起点 {self._pingpong_start}）")

    def _pingpong_enabled(self):
        return (self.cfg.get("route", {}).get("pingpong_single_route", True)
                and len(self.img_routes) == 1
                and not self.is_using_home_route
                and self._pingpong_start is not None)

    def _apply_pingpong(self):
        '''单段路线的自动往返（2026-09-12 加）。

        为什么要它：只录了**一段**（A→B）时，`check_reach_goal` 的取模
        `idx_routes = (idx+1) % 1` 恒等于 0 —— 角色走到 B 之后仍然跟着同一段走，
        而 B 附近像素的指令还是"继续往 B"，于是**一路冲出平台**。
        （用户实测：启动后直奔右边不停。这比"卡在终点"更糟。）

        与其要求每个人都录"去 + 回"两段，不如单段时自动往返：
          · 正向走到 goal（终点 B）→ 掉头
          · 反向时把**左右指令取反**（路线是单向编码的，往回走必须反着读）
          · 走回起点 A 附近 → 再掉头

        ⚠️ 只反转 left/right：跳跃/上下/传送这类动作反向后语义不变，
           动它们会在台阶和梯子处出错。
        '''
        if not self.route_reverse:
            # 正向：走到终点 → 掉头
            if self.cmd_action == "goal":
                self.route_reverse = True
                self.cmd_action = "none"
                logger.info("[巡逻] 单段路线已到终点 → 掉头往回走")
            return

        # 反向：goal 对回程没意义（那意味着"又到终点了"），清掉免得站着不动
        if self.cmd_action == "goal":
            self.cmd_action = "none"
        if self.cmd_move_x == "left":
            self.cmd_move_x = "right"
        elif self.cmd_move_x == "right":
            self.cmd_move_x = "left"

        # 走回起点附近 → 掉头
        turn_range = self.cfg.get("route", {}).get("pingpong_turn_range", 15)
        d = (abs(self.loc_player_global[0] - self._pingpong_start[0])
             + abs(self.loc_player_global[1] - self._pingpong_start[1]))
        if d <= turn_range:
            self.route_reverse = False
            logger.info(f"[巡逻] 单段路线已回到起点（{d}px）→ 掉头继续正走")

    # ── 平台边缘**硬**保护（2026-09-13 用户点名要的功能）────────────────────
    # 用户原话：「没有平台边缘保护，挂机却会走出主线，掉到平台下面」。
    # 前面那些「终点内缩 / reach 封顶 / 切段重新武装」都是**软**保护 —— 它们靠
    # 状态机算得对不对。掉帧、坐标识别抖动、打怪位移都能让角色一帧冲出好几像素，
    # 软保护算得再准也追不上。所以再加一道**绝对地理围栏**：不管状态机怎么想，
    # 只要这一步会把角色带出「主线路线像素的 x 范围」，就把方向掰回来。

    def _edge_guard_rows(self):
        '''把全部主线路线图的**非 goal** 像素按行汇总成 {y: (xmin, xmax)}。

        为什么要按行存：围栏只看角色**当前这一行**附近的像素（见 _edge_guard_range）。
        地图常有上下两层平台，全局 x 范围会把下层平台的 x 也算进来 —— 那样角色
        走到上层边缘时围栏根本不生效，等于没保护。

        goal 圆点（半径 2 的黄色色块）**不参与**：它是画在终点上的，可能画到平台
        外面去，把它算进来等于把围栏放宽到平台外。

        结果按 img_routes 的 id 签名缓存 —— 每帧全图扫一遍太浪费（小地图 200×150
        ×2 张 ×10fps）。路线图换了才重算。
        '''
        # ⚠️ 一律用 getattr 兜底：本方法在**每帧**的指令计算链路上，
        #    离线自检的 Stub 未必有 img_routes / color_code 这些字段 ——
        #    少一个就抛 AttributeError 会把整帧指令搞崩（比不保护更糟）。
        imgs = [im for im in (getattr(self, "img_routes", None) or [])
                if im is not None]
        sig = tuple(id(im) for im in imgs)
        cached = getattr(self, "_edge_guard_rows_cache", None)
        if cached is not None and cached[0] == sig:
            return cached[1]

        goal_codes = {c for c, v in (getattr(self, "color_code", None) or {}).items()
                      if v.strip().split()[-1] == "goal"}
        rows = {}
        for img in imgs:
            for x, y in self._route_pixels(img):
                if tuple(img[y, x][:3]) in goal_codes:
                    continue            # goal 圆点不算，见上
                lo, hi, n = rows.get(y, (x, x, 0))
                rows[y] = (min(lo, x), max(hi, x), n + 1)
        self._edge_guard_rows_cache = (sig, rows)
        return rows

    def _edge_guard_range(self, py):
        '''角色所在这一行的围栏左右边界 (xmin, xmax)；该行像素太少 → None（不启用）。

        Args:
            py (int): 角色当前 y（小地图全局坐标）。

        Returns:
            tuple | None: (xmin, xmax)，或 None 表示这一帧不启用围栏。
        '''
        try:
            tol = int(self.cfg.get("route", {}).get("edge_guard_row_tol", 2))
        except (TypeError, ValueError):
            tol = 2
        tol = max(0, tol)
        lo = hi = None
        n = 0
        for y, (xl, xr, cnt) in self._edge_guard_rows().items():
            if abs(y - py) > tol:
                continue                # 只认角色**同一层**的像素（上下层平台不算）
            lo = xl if lo is None else min(lo, xl)
            hi = xr if hi is None else max(hi, xr)
            n += cnt
        # 这一行非 goal 像素 < 2 个：多半是角色在梯子上 / 空中 / 竖线楼梯上，
        # 此时"横向围栏"没有意义，开了只会误伤（把角色按在原地）。
        if lo is None or n < 2:
            return None

        # ── 安全余量：把围栏往**里**收 N 像素（2026-09-14 加）──────────────
        # 为什么需要：角色跳回主线时落点会偏 ±4~6px（跳跃保留水平速度，实测），
        #   窄平台上这点偏移足以把它送到安全区外、再走一步就掉下去。
        #   往里收一圈后，落点偏了也还在安全区内，且会被强制走回中间。
        #
        # ⚠️ 这是**围栏边界**的内缩，跟 `endpoint_margin`（**段终点**内缩、
        #   见 _calc_seg_goals_with）**不是一回事**，两者可以叠加、不冲突。
        #
        # ⚠️ 值必须按地形调：窄平台调大（更安全、巡逻范围变小），
        #   宽平台调 0（= 不收，老行为）—— 所以界面上要能直接改。
        try:
            inset = int(self.cfg.get("route", {}).get("edge_guard_inset", 0))
        except (TypeError, ValueError):
            inset = 0
        if inset > 0:
            lo2, hi2 = lo + inset, hi - inset
            if lo2 < hi2:          # 收完还站得住才收，别把角色锁死在一个点上
                lo, hi = lo2, hi2
        return lo, hi

    def _edge_guard_step(self, px):
        '''围栏要**往前看多少像素**才准（= 单帧步长 × 反应延迟帧数，至少 1）。

        为什么需要它：只看"当前 px 有没有出界"是不够的 —— 角色一帧能跨好几像素，
        等 px 真的贴到边界再掰方向，这一步已经跨出去了（离线模拟实测：速度 5.0
        时角色能一帧冲到 x=93，而平台边界是 94）。所以要用**实测步长往前看**。

        取近 5 帧最大值而不是"上一帧"：坐标识别本身有 ±1px 抖动，单帧位移会忽大
        忽小（真值 5 时可能量到 4 或 7），只看上一帧会低估、等于没往前看。
        只取 5 帧而不是永久最大值：被撞飞 / 传送那一下的巨型位移不能被永久记住，
        否则围栏会一直把角色按在平台正中间（巡逻范围被白白吃光）。
        下限 1：哪怕角色站着没动，也要留 1px 余量（站在 xmin 上不许再往左踩）。

        ── 反应延迟补偿（2026-09-13 加，这条很关键）────────────────────────
        原来只往前看**一帧**，但指令从「看到位置」到「按键生效」要穿过
        截图 → 主循环 → 键盘 三个线程，每个各占一帧。也就是说，围栏发出
        "掉头"指令时，角色还会继续沿**旧方向**走好几帧才真的转向。
        只看一帧 = 严重低估过冲 → 围栏形同虚设（10 FPS 时实测过冲 ~8.8px，
        而平台只有 14px 宽，必然冲出边缘）。
        所以再乘 `route.edge_guard_react_frames`（默认 3 = 三个线程各 1 帧）。
        '''
        last = getattr(self, "_edge_guard_last_px", None)
        hist = list(getattr(self, "_edge_guard_step_hist", []))
        self._edge_guard_last_px = px
        if last is not None:
            hist.append(abs(int(px) - int(last)))
        hist = hist[-5:]
        self._edge_guard_step_hist = hist
        step = max([1] + hist)
        # 反应延迟补偿：读不到配置就按 3 帧算（宁可多留余量，也不能漏保护）
        try:
            react = int(self.cfg.get("route", {}).get(
                "edge_guard_react_frames", 3))
        except (TypeError, ValueError):
            react = 3
        react = max(1, min(react, 10))
        return step * react

    def _apply_jump_brake(self):
        '''回正起跳前先**停稳**，让跳跃真的变垂直（2026-09-14 实测修）。

        治的是什么（用户观察 + 实测数据完全吻合）：
            回正线的设计假设是"**垂直起跳**" —— 角色走到 x=97~101（主线的正下方）
            原地一跳，正好落到 goal(98,122)。
            但实测角色是**跑着进来**的，跳跃会保留水平速度：
                从右边(110)过来 → 一路往左 → 在 x=100 起跳 → 带着向左速度 → 落到 96/92
                从左边(96)过来  → 一路往右 → 在 x=100 起跳 → 带着向右速度 → 落到 106/104
            **落点 = 起跳点 ± 4~6px**，一偏就贴到平台边缘，再走两步又掉下去
            （用户原话：「边移动边跳，所以落点本身就很靠近平台的边缘了」）。

        所以：拿到起跳指令时，如果角色**还在移动**，本帧先松开方向键让它停稳；
        等它停住了（或最多等 `home_jump_brake_frames` 帧，防卡死）再真正起跳。

        ⚠️ 为什么自己记位移，而不用围栏那套 `_edge_guard_step_hist`：
            回正期间 `_apply_edge_guard` 是**直接 return** 的（回正线本来就要往
            平台外走），那套历史根本不更新，拿来用永远是空的。
        '''
        if not getattr(self, "is_using_home_route", False):
            return                                  # 只在回正期间生效
        pg = getattr(self, "loc_player_global", None)
        if not pg:
            return
        try:
            px = int(pg[0])
        except (TypeError, ValueError, IndexError):
            return

        last_px = getattr(self, "_jump_brake_last_px", None)
        self._jump_brake_last_px = px

        if self.cmd_action != "jump":
            self._jump_brake_n = 0                  # 不是起跳帧 → 重置计数
            return
        if last_px is None:
            return                                  # 第一帧没有参照，先放行
        if abs(px - last_px) <= 0:
            return                                  # 已经停稳 → 正常起跳

        try:
            on = bool(self.cfg.get("route", {}).get("home_jump_brake", True))
        except Exception:                           # noqa: BLE001
            on = True
        if not on:
            return
        try:
            limit = int(self.cfg.get("route", {}).get("home_jump_brake_frames", 3))
        except (TypeError, ValueError):
            limit = 3
        n = int(getattr(self, "_jump_brake_n", 0) or 0) + 1
        self._jump_brake_n = n
        if n > max(1, limit):
            return                                  # 等太久了，别卡住，照跳
        # ★ 本帧不起跳：先松开方向键让角色停下，下一帧速度归零再跳
        self.cmd_move_x = "none"
        self.cmd_move_y = "none"
        self.cmd_action = "none"
        logger.info(f"[回正·起跳] 还在移动（上一帧 x={last_px} → 现在 {px}）"
                    f"→ 本帧先松开方向键停稳（第 {n} 次等待）")

    def _apply_recenter(self):
        '''回正成功后，先走回安全区**中心**再恢复正常巡逻（2026-09-14 实测加）。

        治的是什么（真机实测，用户原话「这一走就又掉下去了」）：
            角色跳回主线时会**横移约 4px**（`(100,126)→(96,122)`），
            而回正期间边缘保护是**故意关掉**的（回正线本来就要往平台外走）
            → 落点完全没人管。落到 106（安全区 94~104 之外）那次，
            **同一秒就又掉下去了**（日志 18:48:19 完整复现）。
            先走回中心，落地位置永远安全，也给后续巡逻留出到边界的距离。

        ⚠️ 为什么必须有**三条**退出条件（别把角色锁死）：
            ① 到了中心（±1px）→ 立刻交还正常巡逻；
            ② 量不出安全区（在梯子 / 空中 / 该行像素太少）→ 放弃归位；
            ③ 超过 `home_recenter_frames` 帧还没到 → 放弃归位。
            缺任何一条，角色都可能被按在原地 —— 那就是之前「围栏死区」
            把角色按死 10 秒、又被脱困指令踹下平台的同一个教训。

        Returns:
            bool: True = 本帧的左右指令已被归位接管。
        '''
        try:
            on = bool(self.cfg.get("route", {}).get("home_recenter", True))
        except Exception:                                    # noqa: BLE001
            on = True
        if not on or not getattr(self, "_recenter_active", False):
            return False
        pg = getattr(self, "loc_player_global", None)
        if not pg:
            return False
        try:
            px, py = int(pg[0]), int(pg[1])
        except (TypeError, ValueError, IndexError):
            return False

        # ③ 帧数上限（防被怪挡住 / 一直走不到 → 把角色锁死）
        try:
            limit = int(self.cfg.get("route", {}).get("home_recenter_frames", 30))
        except (TypeError, ValueError):
            limit = 30
        n = int(getattr(self, "_recenter_n", 0) or 0) + 1
        self._recenter_n = n
        if limit > 0 and n > limit:
            self._recenter_active = False
            self._recenter_n = 0
            logger.info(f"[回正·归位] 走了 {n} 帧还没到中心 → 放弃归位，交还正常巡逻")
            return False

        rng = self._edge_guard_range(py)
        if rng is None:
            # ② 量不出安全区：多半在梯子 / 空中，此时硬归位只会误伤
            self._recenter_active = False
            self._recenter_n = 0
            return False
        xmin, xmax = rng
        center = (int(xmin) + int(xmax)) // 2
        if abs(px - center) <= 1:
            # ★ 光"到中心"**不够**，还必须**真的停住**（连续两帧 x 相同）才交还控制权。
            #   （2026-09-15 真机实测加；与 try_return_to_mainline 的"连续两帧 y 相同"对称）
            #
            #   为什么必须加（真机实测 19:15 那次，27 秒里循环了 5 轮）：
            #     回正是**跑着跳**上来的，跳跃保留水平速度 → 落回平台后还会**滑 4~6px**。
            #     旧逻辑一到 ±1px 就交还控制权，可人还在滑 →
            #       从左边回正（往右跑着跳）→ 落点 100 → 滑到 **104** → 从右边缘掉下去
            #       从右边回正（往左跑着跳）→ 落点 98  → 滑到 **94**  → 从左边缘掉下去
            #     → 再从那边掉进坑、反向回正 → **来回死循环**。
            #     平台安全区只有 94~104（10px），而滑行量有 4~6px —— 交还得太早必然滑出去。
            #
            #   ⚠️ 加这条之后归位会一直生效到角色停下；一旦越过中心，下面那行
            #      `right if px < center else left` 会自动反向 → **正好起刹车作用**，
            #      把滑行吃掉。所以"停稳"是可达的，不会锁死（还有 30 帧上限兜底）。
            if getattr(self, "_recenter_last_px", None) == px:
                self._recenter_active = False
                self._recenter_n = 0
                self._recenter_last_px = None
                logger.info(f"[回正·归位] 已回到安全区中心 x≈{center}（当前 x={px}）"
                            f"且已停稳，恢复正常巡逻")
                return False
            self._recenter_last_px = px
            # ⚠️ 这里**故意不 return**：人还在动 → 保持归位生效、继续朝中心给方向
        self.cmd_move_x = "right" if px < center else "left"
        return True

    def _apply_edge_guard(self):
        '''最后一道防线：不许角色走出主线路线像素的 x 范围（2026-09-13 加）。

        在 update_cmd_by_route 里、`_apply_pingpong()` 之后调用（要等前面的
        往返 / 左右取反都改完，围栏才盖得住）。

        判定不是"贴到边界就掰"，而是"**再走一步会不会出界**"：
            can_left  = px - step >= xmin   （还能往左）
            can_right = px + step <= xmax   （还能往右）
        不能往左就往右，反之亦然；两边都不能（一步比平台还宽）时朝**中心**走 ——
        这时没有"安全方向"，只能选离边界更远的那侧，先把角色拉回平台中段。

        ⚠️ 必须跳过回正状态：回正线是**故意**要往坑外 / 平台外走的，围栏一插手
           就会和回正线的方向打架，角色被按在坑底上不来。
        '''
        # 开关一律 try/except 兜底：配坏了宁可**按默认开启**，
        # 也不能因为读配置抛异常把整帧指令搞崩（用户要的是"绝不掉落"）。
        try:
            enabled = bool(self.cfg.get("route", {}).get("edge_guard", True))
        except Exception:
            enabled = True
        if not enabled:
            return
        if getattr(self, "is_using_home_route", False):
            return                      # 走回正线时不干预，见上
        if not getattr(self, "loc_player_global", None):
            return
        if not (getattr(self, "img_routes", None) or []):
            return                      # 没有主线路线图 → 无从算围栏
        px, py = self.loc_player_global
        rng = self._edge_guard_range(py)
        if rng is None:
            return
        xmin, xmax = rng
        step = self._edge_guard_step(px)
        # ── ★ 封顶（2026-09-14 实测修，见 edge_guard_step_cap）────────────
        # 平台只有 10px 宽时，6px 的补偿量会让角色在中间**两边都不许走**：
        #   往左会到 93（出界）、往右会到 105（出界）→ 被按死在原地
        #   → 卡住 10s → 脱困随机指令（right down jump）把角色踹下平台。
        #   实测日志 18:28:34~53 完整复现了这条链。
        # 所以上限必须 **小于半宽**，保证任意位置至少有一个方向能走。
        # 顺带压住异常值（被怪撞飞量到 14px、×3 变 42px，接下来几帧会彻底锁死）。
        step = edge_guard_step_cap(step, xmax - xmin)
        # ⚠️ 用**严格**不等式（`>` / `<`，不是 `>=` / `<=`）：
        #    坐标识别有 ±1px 抖动，px 本身是"约数"。用 >= 等于默认 px 绝对准确，
        #    实测速度 5.0 + 抖动时角色正好从 px=99 一路左移到真实 x=93（边界 94）
        #    —— 踩空的正是这一像素的乐观。宁可把围栏收严 1px（用户：范围无所谓）。
        can_left = (px - step) > xmin
        can_right = (px + step) < xmax
        if self.cmd_move_x == "left" and not can_left:
            # 往左会出界：能往右就往右；两边都出界 → 朝中心走（px ≤ 中心就往右）。
            #
            # ⚠️ 正中线上的平局**必须有个确定选择**（`<=` 而不是 `<`）：
            #    实测速度 5.0 + 抖动时，角色真实 x=98、识别成 px=99（平台正中），
            #    一步 5px 时左右两边"看起来"一样危险 → 若走 `<` 分支会选中往左，
            #    角色一帧冲到 x=93（边界 94）掉坑；往右则是 x=103（在界内）。
            #    噪声下平局必须**钉死一个方向**，让它随机踩空等于没保护。
            if can_right or px * 2 <= xmin + xmax:
                self.cmd_move_x = "right"
                self._log_edge_guard(f"[边缘保护] 角色 x={px} 已到路线左边界({xmin})"
                                     f"（一帧约走 {step}px）→ 强制改向右")
        elif self.cmd_move_x == "right" and not can_right:
            if can_left or px * 2 >= xmin + xmax:
                self.cmd_move_x = "left"
                self._log_edge_guard(f"[边缘保护] 角色 x={px} 已到路线右边界({xmax})"
                                     f"（一帧约走 {step}px）→ 强制改向左")

    def _log_edge_guard(self, msg):
        '''围栏命中日志（1 条/秒节流）。

        为什么要节流：角色被按在边界上时会**每帧**命中 —— 主循环 10fps 就是
        每秒 10 条，真出问题时日志会被它刷屏，反而看不见别的信息（用户之前抱怨过
        日志看不清）。1 条/秒足够自证「围栏真的生效了」，又不刷屏。
        '''
        now = time.time()
        if now - getattr(self, "_t_last_edge_log", 0.0) < 1.0:
            return
        self._t_last_edge_log = now
        logger.info(msg)

    # ── 窄平台降级（2026-09-15，判据见 common.route_too_narrow_for_patrol）────
    def _patrol_degraded_now(self):
        '''本图是否"窄到不值得来回巡逻"。每帧算一次，存成 self._patrol_degraded。

        两处用它：
          · `update_cmd_by_route` —— 降级时不发左右移动（不来回蹭）
          · `is_player_stuck`     —— 降级且**画面里没有怪**时不判卡死
        '''
        try:
            min_gap = self.cfg.get("route", {}).get("patrol_min_gap", 24)
        except Exception:                                    # noqa: BLE001
            min_gap = 24
        return route_too_narrow_for_patrol(
            getattr(self, "_seg_goal_gap", 0) or 0, min_gap)

    def _log_patrol_degraded(self):
        '''降级生效 / 解除时各打一行（**只在状态变化时**打，不刷屏）。'''
        want = bool(getattr(self, "_patrol_degraded", False))
        if getattr(self, "_degraded_logged", None) == want:
            return
        self._degraded_logged = want
        if want:
            try:
                _th = self.cfg.get("route", {}).get("patrol_min_gap", 24)
            except Exception:                                # noqa: BLE001
                _th = 24
            logger.info(
                f"[窄平台降级] 本图相邻两段终点只差 "
                f"{getattr(self, '_seg_goal_gap', 0)}px（阈值 {_th}px）"
                f"→ 判定「这条路不值得来回巡逻」：不再左右蹭，站着面向怪打。\n"
                f"        附近有怪却不打时，仍然按「卡死」处理（会触发脱困指令）。\n"
                f"        想改判据：route.patrol_min_gap（填 0 = 关掉降级）。")
        else:
            logger.info("[窄平台降级] 已解除，恢复正常巡逻")

    def update_cmd_by_route(self):
        # ── 插入点①：**两线距离比较**（v0.9 第二版，必须放在函数最前面）─────────
        # 新判定是**坐标比较**，与"看不看得见主线"无关，所以必须放在最前面：
        # 哪怕这一帧主线像素找得到，只要人离回正线明显更近就要进回正。
        #
        # ★ 铁律：d_home / d_main **每帧只在这里算一次**，存实例字段；
        #   插入点②（超时 / 驻留 / try_return）**只读不算** ——
        #   一帧内出现"①说保持、②说退出"的口径分裂是最难查的 bug。
        _perf_t0 = time.perf_counter()
        self.home_d_main, self._main_seg_i = self._main_dist()
        self.home_d_home, self._home_line_near = self._home_dist()
        self._perf("回正距离", _perf_t0)

        # ── 窄平台降级：每帧先算一次（判据见 common.route_too_narrow_for_patrol）──
        # 放在这里 = 在任何 return 之前，保证同帧后面跑的 is_player_stuck
        # 读到的一定是本帧的值（它靠这个标志决定豁不豁免卡死判定）。
        self._patrol_degraded = self._patrol_degraded_now()
        self._log_patrol_degraded()

        if not self.is_using_home_route and self.img_route_home is not None:
            line = self._home_line_pick()
            if line is not None:
                # ⚠️ 用 _home_tol_now()（本帧实际生效的容差），**不要**自己读
                #    home_dist_tol —— 坑底会把它收成 0，自己读就会打出对不上的日志
                #    （2026-09-15 踩过：日志写"- 2"，实际按 0 算，看起来像 bug）。
                tol = self._home_tol_now()
                self._enter_home_route(
                    f"离回正线 {self.home_d_home}px < 离主线 {self.home_d_main}px - {tol}"
                    f"（回正线 #{line.get('id')}）", line)

        # ── 插入点②：回正状态下的帧计数 + 8s 超时出口 ─────────────────────────
        if self.is_using_home_route:
            self.home_frame += 1
            timeout = float(home_params(self.cfg)["home_timeout"])
            # ⚠️ 0 / 负数 → 视为"没配/配错"，**回落到默认 8s**（home_params 已兜住，
            #    这里再兜一层），而不是关掉超时出口。
            #    关掉出口的代价是：回正线接不上主线时，角色会**无限期**停在回正状态
            #    —— 回正期间不打怪，踩到终点后 goal 在键盘层又是 pass，等于
            #    「既不走也不打」且没有任何日志（QA-14）。这个"逃生口"弊大于利，砍掉。
            if timeout <= 0:
                timeout = float(DEFAULT_HOME_PARAMS["home_timeout"])
            if self.home_enter_t > 0 \
                    and time.time() - self.home_enter_t > timeout:
                # ⚠️ 加开关必须加出口：进了回正又不打怪，接不上时就是"既不走也不打"。
                #    超时后**该锚点去武装**，否则会变成「8s 回正 → 立刻重进」的死循环。
                # ── ★ 2026-09-14 实测修：超时那一刻，先补判「是不是其实已经回去了」──
                # 现象（真机日志 18:02:08 / 18:02:33 各一次）：小结写着
                #   「(96,126)→(92,122) | 离回正线 5px 离主线 **0px**」
                #   —— 角色**就站在主线上**（d_main=0，y=122 正是站立行），
                #   却照样走 8s 超时出口 → 判失败 → 回正线被去武装 30s，
                #   等于"明明回去了还要罚站 30 秒"，这期间再掉下去没人接。
                #
                # 成因：正常退出要求「连续两帧 y 相同」（防空中误判的 E3 硬闸，
                #   见 try_return_to_mainline），而角色**跳上去又掉下来**，
                #   y 在 122/126 之间反复（小结「期间 y 122~126」），
                #   2fps 下根本凑不齐连续两帧相同 → 8s 超时抢先触发。
                #
                # ⚠️ 判据为什么用 `d_main <= 1`：坑底 d_main=4（实测值）、
                #   站上主线 d_main=0，两者差得远，1px 足以区分，
                #   不会把"还在坑底"误判成"已经回去了"。
                _back_on_main = False
                try:
                    _dm = getattr(self, "home_d_main", None)
                    _back_on_main = (_dm is not None and float(_dm) <= 1)
                except (TypeError, ValueError):
                    _back_on_main = False

                if _back_on_main:
                    # 已经回去了 → **按成功处理**：不去武装、不上冷静期
                    self._exit_home_route(
                        f"回正超过 {timeout}s，但角色已经站在主线上"
                        f"（离主线 {self.home_d_main}px）→ 按成功处理，"
                        f"回正线保持可用")
                else:
                    self._exit_home_route(f"回正超过 {timeout}s 还没回到主线，"
                                          f"先交还给打怪逻辑", disarm=True)
                # ── 超时退出后**本帧直接交还给打怪逻辑**（QA-20 的另一半）────────
                # 光靠 _home_reenter_allowed 的冷静期只能保证"不会再被拉回回正"；
                # 但函数还会继续往下走到 get_nearest_color_code()，而此刻 img_route
                # 已被交回主线、角色离主线 > rescue_range → 取不到像素 → 命中
                # 「完全找不到路线像素」那条分支，把这一帧的指令清成 none。
                # 那跟这里 return 的结果一样，但**依赖了外面的分支**——脆弱。
                # 显式清成 none 再 return，语义才是"这一帧我就是不打不走了，交给打怪"：
                #   · 必须清成 none，不能留 jump / goal —— jump 在键盘层是真按键，
                #     goal 是 pass（空转），两者都会让角色原地做多余动作；
                #   · 必须 return，否则本帧后面的路线/往返逻辑会用**回正线的**
                #     旧 img_route 再算一次指令，把刚退出的状态又搅一遍。
                self.cmd_move_x = "none"
                self.cmd_move_y = "none"
                self.cmd_action = "none"
                # ⚠️ 只有**真没回去**才打这条告警 —— 人已经站在主线上就别再吓唬了
                #   （原来无条件打，日志里满屏"回正失败 + 建议重画"，其实人早回去了，
                #    差点误导到"重画回正线"上去）。
                if not _back_on_main and time.time() - self.t_last_home_fail_log > 10:
                    self.t_last_home_fail_log = time.time()
                    logger.warning(
                        f"[回正] 角色 {self.loc_player_global} 回正超过 {timeout}s 仍没接上主线 "
                        f"（离回正线 {self.home_d_home}px / 离主线 {self.home_d_main}px）"
                        f"—— 多半是这条回正线画短了/画偏了（终点没画到主线上，"
                        f"角色在下面够不着主线）。\n"
                            f"        本次先回去打怪；{home_params(self.cfg)['home_reenter_lock']:.0f}s 内"
                            f"不会再尝试回正、{home_params(self.cfg)['home_rearm_timeout']:.0f}s 内"
                            f"不会再被这条线接住。**建议重画这条回正线**：【🖊 手绘回正线】"
                            f"先沿坑底横着画一段，再竖着跳回主线上。")
                # 本帧到此为止（理由见上面那段注释）：打怪逻辑立刻接管这一帧。
                return
            elif not self._home_dwell_ok():
                # 最小驻留门没开：屏蔽一切"已回主线/已到终点"判定，防一进就出
                pass
            else:
                # 回正期间：优先尝试接回主线（理由见 try_return_to_mainline 注释）
                self.try_return_to_mainline()

        # get color code from img_route
        _perf_t0 = time.perf_counter()
        color_code, color_code_up_down = self.get_nearest_color_code()
        self._perf("路线搜索", _perf_t0)

        # ── 回正线：主线附近找不到路线像素 = 走丢了（掉落平台 / 被撞飞）──────────
        # 这时改用 route_home.png 走回主线；走到回正线末端的 goal 后，
        # 由 check_reach_goal() 把 is_using_home_route 置回 False，恢复主线巡逻。
        if color_code is None and color_code_up_down is None:
            # ⚠️ 冷静期判据（QA 实测必修）：上一次回正刚失败（超时 / 接不上）时，
            #    这里**必须**挡住 —— 超时退出后角色离主线还很远，本帧必定再次
            #    "找不到主线像素"，于是同一帧退出又进入，home_enter_t 归零，
            #    8s 超时与 15 帧驻留门全部失效 → 卡在回正里既不走也不打。
            if self.img_route_home is not None and not self.is_using_home_route \
                    and self._home_reenter_allowed():
                # 老兜底（P1-3，保留）：主线附近一个像素都找不到 = 走丢了 → 试回正线
                #
                # ★ 第二版新增的这道闸（QA-20 死锁的**新伪装**，必须加）：
                #   必须**先拿到一条已武装的回正线**才准进。
                #   为什么：这一版把「去武装」绑到了"线"上（D2）。角色卡在坑底、
                #   那条线刚被超时去武装时，`d_home` = None（没有可用线）→ 新判定
                #   `_home_line_pick()` 会正确地**不进**；但老兜底看的是"主线像素
                #   够不着"，跟 armed 完全无关 → 于是每帧都从这儿钻回回正状态：
                #     · `_enter_home_route` 每帧把 `home_enter_t` 重置成本帧 →
                #       8s 超时**永远**走不到（实测：最大连续回正 748/1290 帧）；
                #     · 回正期间不打怪 → 「既不走也不打」的 QA-20 死局原样复活。
                #   加了这道闸后：没有可用回正线 = 站着不动、由打怪逻辑接管（有出口）。
                _d_far, _ln = self._home_dist()
                if _ln is not None:
                    self._enter_home_route(
                        f"主线附近找不到路线（走丢了），离回正线 {_d_far}px", _ln)
                    if self.is_using_home_route:
                        _perf_t0 = time.perf_counter()
                        color_code, color_code_up_down = self.get_nearest_color_code()
                        self._perf("路线搜索", _perf_t0)
            if color_code is None and color_code_up_down is None:
                # ── 出口：回正线接不上 → 立刻退出回正状态（2026-09-12 加）────────────
                # 「加开关必须加出口」。「回正期间不打怪」这个开关的前提是回正线能接上；
                # 接不上时它就从"保护回正"变成"锁死角色"：
                #     找不到像素 → 指令真空 → 又因为还在回正状态而不打怪
                #     → 既不走也不打，日志一切正常 = 极难查的死局（用户实测）。
                # 接不上说明**拿着这条线也走不回去**（掉进了没录过的坑 / 落点录歪了），
                # 那就不如退出回正状态，让 update_cmd_by_mob_detection 接管 —— 至少还能
                # 打怪。下一帧如果还在安全点外，会再试一次回正；一旦角色飘回回正线
                # 附近（能找到像素了），就正常走回去。
                # ⚠️ 这里**故意不加计时器/冷却**：找得到像素 = 能走 = 保持回正优先
                #    （用户要的"掉下去先回来再打怪"）；找不到 = 走不了 = 让位给打怪。
                #    一个 if 就说清了，不需要超时阈值那堆状态。
                if self.is_using_home_route:
                    # disarm=True：接不上时也让**这条线**去武装，避免"退出 → 下一帧又立刻进"
                    # 的反复横跳（回正期间是不打怪的，横跳 = 既不走也不打）。
                    self._exit_home_route("回正线接不上（附近一个回正线像素都没有）",
                                          disarm=True)
                    if time.time() - self.t_last_home_fail_log > 10:
                        self.t_last_home_fail_log = time.time()
                        logger.warning(
                            f"[回正] 角色位置 {self.loc_player_global} 附近找不到回正线像素"
                            f"（搜索 {self.cfg['route']['search_range']}px / "
                            f"救援 {self.cfg['route'].get('rescue_range', 40)}px 都够不着）"
                            f"—— 这条回正线覆盖不到这里，暂时放弃回正、先打怪。\n"
                            f"        多半是掉进了一个**没录过回正线**的坑。补录：站到这个落点上"
                            f"按 F6 起头，再走回主线按 F6 保存（可以叠加多条）；\n"
                            f"        或者用【🖊 手绘回正线】直接画一条（不用真的掉下去）。")
                # ⚠️ 完全找不到路线像素时**不能保持上一帧的方向键**（2026-09-12 实测）：
                #    那等于让角色一直朝最后一次的方向走 —— 短路线（平台就那么长）
                #    一冲出去就再也回不来，表现是「到终点停不下来一直往前走」。
                #    停下比冲出去安全：站着还能打怪，看门狗也会来救。
                self.cmd_move_x = "none"
                self.cmd_move_y = "none"
                if self.cmd_action not in ("goal", "attack"):
                    self.cmd_action = "none"
                return

        # Use color_code and color_code_up_down to complement each other
        # To prevent character stuck at the end of ladder, we use two color color pixels
        # and let them complement with each other, to ensure smoothy ladder climbing
        if color_code and color_code_up_down:
            if color_code["distance"] < color_code_up_down["distance"]:
                self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code["command"].split()
                _, cmd, _ = color_code_up_down["command"].split()
                if self.cmd_move_y == "none" and self.is_on_ladder:
                    self.cmd_move_y = cmd # only complement cmd_move_y when player is on ladder
            else:
                # ★ 2026-09-17 防"地面伪灰"（用户报「路过管道一直往右走」的根因）。
                # up 像素和角色**同 y**（站在地面层）且**同列往上 3px 内没有第二个 up 像素**
                # → 它不是真管柱的起点（真管柱一定有连续 up 像素）。角色按 up 也爬不上去，
                # 改成走 color_code（多半是"right"，让角色先走到管柱下方再爬）。
                _up_x = color_code_up_down["pixel"][0]
                _up_y = color_code_up_down["pixel"][1]
                _fake = (_up_y == self.loc_player_global[1]
                         and (_up_y - 3 < 0
                              or not any(tuple(int(v) for v in self.img_route_home[_up_y-d, _up_x][:3])
                                          in {(127, 127, 127), (255, 255, 127)}
                                          for d in (-3,-2,-1,1,2,3))))
                if _fake and color_code is not None:
                    self.cmd_move_x, self.cmd_move_y, self.cmd_action = (
                        color_code["command"].split())
                else:
                    self.cmd_move_x, self.cmd_move_y, self.cmd_action = (
                        color_code_up_down["command"].split())
                    cmd, _, _ = color_code["command"].split()
                    if self.cmd_move_x == "none" and self.is_on_ladder:
                        self.cmd_move_x = cmd  # only complement cmd_move_x when player is on ladder
        elif color_code:
            self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code["command"].split()
        elif color_code_up_down:
            self.cmd_move_x, self.cmd_move_y, self.cmd_action = color_code_up_down["command"].split()

        # ── 窄平台降级：把路线给的**左右移动**吞掉（见 _patrol_degraded_now）──
        # 只吞 cmd_move_x，保留 cmd_move_y / cmd_action —— 改动面最小。
        # ⚠️ 放在这里是安全的：update_cmd_by_mob_detection 在后面跑，它还会按
        #    「面向最近的怪」重新设 cmd_move_x，所以降级**不影响转身打怪**。
        # ⚠️★ 回正期间**绝不降级**（2026-09-15 补，跟围栏同一个道理）：
        #    回正线本来就要左右走，吞掉它角色就**走不回主线**。
        #    `_apply_edge_guard` 里也有同样的 `is_using_home_route` 短路。
        #    漏了这条会被 verify_home_route_qa / qa2 直接抓到
        #    （"起点 (94,128)：走回主线并退出" 失败）—— 本次就是这么发现的。
        if (getattr(self, "_patrol_degraded", False)
                and not self.is_using_home_route):
            self.cmd_move_x = "none"

        # replace teleport to jump if user doesn't set teleport key
        # （路线颜色编码里仍可能有 teleport 动作，所以这个降级判断保留）
        if self.cfg["key"]["teleport"] == "" and self.cmd_action == "teleport":
            self.cmd_action = "jump"

        # ── 回正：起跳前先**停稳**，让跳跃真的变垂直（见 _apply_jump_brake）────
        self._apply_jump_brake()

        # 单段路线走到头自动掉头（否则会一路冲出平台，见 _apply_pingpong）
        if self._pingpong_enabled():
            self._apply_pingpong()

        # ── 回正成功后：先走回安全区中心，再正常巡逻（见 _apply_recenter）────
        # 放在 pingpong 之后、边缘围栏之前：归位给的是"朝中心走"，
        # 本身就在安全区里，围栏不会拦它，也不会被 pingpong 再翻回去。
        self._apply_recenter()

        # ── 最后一道防线：平台边缘硬围栏（见 _apply_edge_guard 的说明）────────
        # 必须放在**所有**方向改动之后（含 _apply_pingpong 的左右取反），
        # 否则 pingpong 会在围栏之后再把方向翻回去，围栏白干。
        self._apply_edge_guard()

        # ── 埋点：回正期间记"走到起跳点几次"（放最后 = 拿到的是最终指令）────
        if getattr(self, "is_using_home_route", False):
            self._trace_home_cmd()

    def _trace_home_cmd(self):
        '''回正期间每帧记一次状态（2026-09-14 排查「掉坑回不来」的埋点，纯观测）。

        做两件事，**不判定、不改任何指令**：
          ① `cmd_action` 变成 jump 的**上升沿** → 记一次「走到起跳点」（带坐标、距上次间隔）
          ② 累积回正期间角色 y 的区间 → 小结里用来看「跳没跳上去」

        ⚠️ 为什么只记**上升沿**而不是每帧：角色站在一片 jump 像素上时，
           每帧的 cmd_action 都是 jump —— 30fps 下站 1 秒就是 30 条一模一样的
           日志，会把「进入 / 退出回正」这些真正有用的行全淹掉。
           而"走到起跳点**几次**"这个计数才是区分病因的关键，上升沿天然就等于次数。

        ⚠️ 所有状态一律 `getattr` 兜底：tools/verify_navigation.py 的 Stub
           **不走 __init__**，直接 self.xxx 会让现有 19 项自检全崩（踩过的坑）。
        '''
        if not jump_trace_on(self.cfg):
            return
        # ── ② 期间 y 的区间 ────────────────────────────────────────────
        try:
            py = int(self.loc_player_global[1])
        except (TypeError, ValueError, IndexError):
            py = None
        if py is not None:
            y0 = getattr(self, "home_y_min", None)
            y1 = getattr(self, "home_y_max", None)
            self.home_y_min = py if y0 is None else min(y0, py)
            self.home_y_max = py if y1 is None else max(y1, py)

        # ── ① 走到起跳点的上升沿 ───────────────────────────────────────
        on = (self.cmd_action == "jump")
        was_on = bool(getattr(self, "_home_jump_on", False))
        self._home_jump_on = on
        if not (on and not was_on):
            return
        n = int(getattr(self, "home_jump_n", 0) or 0) + 1
        self.home_jump_n = n
        ts = list(getattr(self, "home_jump_ts", None) or [])
        now = time.time()
        gap = (now - ts[-1]) if ts else None
        ts.append(now)
        self.home_jump_ts = ts
        logger.info(
            f"[回正·起跳] 第 {n} 次走到起跳点 位置 "
            f"{tuple(self.loc_player_global) if self.loc_player_global else '?'} "
            f"指令({self.cmd_move_x} {self.cmd_move_y} {self.cmd_action})"
            + (f"，距上次 {gap:.2f}s" if gap is not None else ""))

    def update_cmd_by_mob_detection(self):
        # Get monster search box
        margin = self.cfg["monster_detect"]["search_box_margin"]
        if self.cfg["bot"]["attack"] == "aoe_skill":
            dx = self.cfg["aoe_skill"]["range_x"] // 2 + margin
            dy = self.cfg["aoe_skill"]["range_y"] // 2 + margin
            cooldown = self.cfg["aoe_skill"]["cooldown"]
        elif self.cfg["bot"]["attack"] == "directional":
            dx = self.cfg["directional_attack"]["range_x"] + margin
            dy = self.cfg["directional_attack"]["range_y"] + margin
            cooldown = self.cfg["directional_attack"]["cooldown"]
        else:
            raise RuntimeError(f"Unsupported attack mode: {self.cfg['bot']['attack']}")
        x0 = max(0                      , self.loc_player[0] - dx)
        x1 = min(self.img_frame.shape[1], self.loc_player[0] + dx)
        y0 = max(0                      , self.loc_player[1] - dy)
        y1 = min(self.img_frame.shape[0], self.loc_player[1] + dy)

        # Get monsters in the search box
        _perf_t0 = time.perf_counter()
        # ── 怪检测**降频**（2026-09-14 实测加，见 monster_detect_interval）────
        # 为什么：一帧 552ms 里怪检测占 **502ms（91%）**，是帧率卡在 1.8fps 的
        #   唯一元凶。怪不需要每帧重新找 —— 降频省下的时间会让帧率涨回去，
        #   实测"两次检测之间"只慢 50~100ms，却换来整体响应快一倍。
        # 中间帧沿用上一次的 self.monsters（下游只读它的大小和位置）。
        #
        # ⚠️ 跳过的帧**不调 _perf**：否则耗时被摊到更多帧上，日志里的
        #    "怪检测 XXXms" 会假性变小（502 → 251），让人误以为优化生效，
        #    把后续排查彻底带偏。它应该始终显示"跑一次要多久"。
        _iv = monster_detect_interval(self.cfg)
        _skip = int(getattr(self, "_mob_skip", 0)) + 1
        if _iv <= 1 or _skip >= _iv:
            self.monsters = self.get_monsters_in_range((x0, y0), (x1, y1))
            self._perf("怪检测", _perf_t0)
            self._mob_skip = 0
        else:
            self._mob_skip = _skip

        # ── 回正优先（最高优先级，2026-09-12）────────────────────────────────
        # 正在走回正线时，**完全不覆盖移动指令**，
        # 只更新 self.monsters（画面上的索敌框、状态日志的怪数还要用它）。
        #
        # 用户实测：「从平台上掉下去后，角色会主动跑去打怪而不是回到安全点」——
        # 就是这里把回正的方向覆盖成了怪的方向。回正期间先回来，回来再打。
        # （上一版只保护了"起跳那一帧"，回正线给 left/right 时照样被拽走，不够）
        #
        # ⚠️ 配套的出口在 update_cmd_by_route：回正线**一帧像素都找不到**时立刻
        #    退出回正状态（is_using_home_route=False），否则会「既不走也不打」。
        if self.is_using_home_route:
            return

        # Check if no mob to attack
        if len(self.monsters) == 0:
            # ⚠️ 必须清掉上一帧残留的攻击指令（2026-09-12 修）
            # cmd_move_x / cmd_move_y / cmd_action 是**跨帧保持**的实例属性，原实现
            # （上游逐字如此）在这里直接 return，于是只要某一帧设过 "attack"，怪没了
            # 它也一直是 "attack"，键盘层每帧按一次技能键（30fps ≈ 每秒 30 次）
            # → 用户实测「索敌范围内没有怪还在疯狂打空气」。
            # 上游不发作是因为 update_cmd_by_route 每帧拿路线色码覆盖 cmd_action；
            # 但回正线接不上、或路线走到 goal 时它不会覆盖，这颗雷就会响 —— 所以必须清。
            if self.cmd_action == "attack":
                self.cmd_action = "none"
            return

        # Update attack command
        if self.cfg["bot"]["attack"] == "aoe_skill":
            if time.time() - self.t_last_attack > cooldown:
                # AOE 技能本身不分方向，但仍然要**面向**最近的怪：
                # 上游在这里完全不设 cmd_move_x，角色永远保持初始朝向，
                # 用户实测「后方的怪打不到」。AOE 不需要等转身，同帧出招即可。
                # ⚠️ 梯子上不抢方向键（见下方 directional 分支的说明）。
                if not self.is_on_ladder:
                    face = self.get_face_direction()
                    if face is not None and self.cmd_move_x_last != face:
                        self.cmd_move_x = face
                self.cmd_action = "attack"
                self.t_last_attack = time.time()

        elif self.cfg["bot"]["attack"] == "directional":
            # Get nearest monster to player
            monster_left  = self.get_nearest_monster(is_left = True)
            monster_right = self.get_nearest_monster(is_left = False)
            # Determine attack direction
            attack_direction = self.get_attack_direction(monster_left, monster_right)
            if attack_direction is None:
                # 两侧都没明确目标（两边怪距离太接近、或怪在正上/正下方）
                # —— 同样要清残留，否则会继续打空气
                if self.cmd_action == "attack":
                    self.cmd_action = "none"
                return
            # Attack Command
            if time.time() - self.t_last_attack > cooldown:
                # ⚠️ 先转身、再出招（2026-09-12 修）
                # 技能是朝**当前朝向**打出去的；同一帧按方向键+技能键，技能会朝旧朝向
                # 飞走 —— 后方的怪永远打不到。上游 config 里躺着 character_turn_delay,
                # 但整个 src/ 一处引用都没有（没做完的半成品），这里补上：朝向不对时
                # 本帧只发转向键、不出招，下一帧再打（延迟一帧 ≈ 33ms）。
                #
                # 判据用「上一帧实际发出的方向」而不是一个持久的 facing 字段：
                # 巡逻模式下路线每帧都会把 cmd_move_x 改回路线方向，用持久 facing
                # 会导致永远卡在"等转身"、一次都不出招。
                # ⚠️ 梯子上不抢方向键：update_cmd_by_route 刚把上下方向配好，
                #    这里一旦覆盖成 left/right，角色横向一动就会被判成「离开梯子」
                #    （run_once 里 dx > 3 → is_on_ladder = False）→ 直接从梯子上掉下去。
                #    梯子上照常打怪，只是不改朝向。
                if self.is_on_ladder:
                    self.cmd_action = "attack"
                    self.t_last_attack = time.time()
                    return
                # ── 转身状态机（2026-09-17 重写）────────────────────────────
                # 旧实现只有两步：① 朝向不对 → 本帧只发方向键；② 下一帧就出招。
                # 它有两个致命问题（用户实测「后面有怪却一直在往前面砍空气」）：
                #   (a) 转向只维持 1 帧（≈33ms），游戏里朝向可能根本没切过来；
                #   (b) 出招那帧**仍然按着方向键** —— 技能按"当前朝向"飞出去，
                #       同帧按方向+技能，技能会朝**转身前**的朝向飞走（见上面
                #       2026-09-12 的注释），等于每次都朝路线前进方向甩。
                # 现在改成三段：
                #   ① 朝向不对 / 怪换边 → 起一个"按住方向键 N 帧"的转身过程
                #   ② 转身期间只发方向键、不出招
                #   ③ 转完**松开方向键**（cmd_move_x="none"，朝向保持不变）再出招
                #      ⇒ 技能朝当前朝向飞，后方的怪打得着。
                _turn_frames = int(self.cfg["directional_attack"].get("turn_frames", 3))
                # getattr 兜底：离线用例里的 Stub 不走 __init__，没有这两个属性
                _turn_left = int(getattr(self, "_turn_frames_left", 0) or 0)
                if ((_turn_left <= 0
                     and self.cmd_move_x_last != attack_direction)
                        or getattr(self, "_turn_dir", None) != attack_direction):
                    _turn_left = max(1, _turn_frames)
                    self._turn_dir = attack_direction
                if _turn_left > 0:
                    self.cmd_move_x = attack_direction
                    self._turn_frames_left = _turn_left - 1
                    logger.debug(
                        f"[转向] 目标在{attack_direction}侧，按住方向键转身"
                        f"（还剩 {self._turn_frames_left} 帧，本帧不出招）")
                    return
                self._turn_frames_left = 0
                # 朝向已就位 → 松开方向键再出招（用户要的「回头打一下就继续走」：
                # 出招后本函数不再改 cmd_move_x，下一帧由路线接管，角色继续巡逻）
                self.cmd_action = "attack"
                self.cmd_move_x = "none"
                self.t_last_attack = time.time()

    def update_cmd_by_random(self):
        '''
        update_cmd_by_random - pick a random action except 'up' and teleport command
        '''
        self.cmd_move_x = random.choice(["left", "right", "none"])
        self.cmd_move_y = random.choice(["down", "none"])
        self.cmd_action = random.choice(["jump", "none"])
        logger.warning("[update_cmd_by_random]"\
                    f"{self.cmd_move_x} {self.cmd_move_y} {self.cmd_action}")

    def check_reach_goal(self):
        # ── 回正状态下：goal **不再是**退出条件（设计决策⑤）───────────────────
        # 为什么去掉：goal 圆点（半径 2 = 13px）比一整条短回正线的一半还大，
        # 而 search_range=10 > 整条线长 → 角色**站在落点时**最近的回正线像素
        # 可能就是 goal（黄） → 一进回正就被判"走完了"，原地退出（坑4）。
        # 退出改成「两条线谁更近」（见 try_return_to_mainline / _home_line_keep）。
        if self.is_using_home_route:
            if self.cmd_action == "goal" and \
                    time.time() - self.home_last_goal_log > 10:
                self.home_last_goal_log = time.time()
                tol = int(home_params(self.cfg)["home_dist_tol"])
                logger.info(
                    "[回正] 已踩到回正线的终点标记；是否真的回到主线由**两线距离**判定"
                    f"（当前 离回正线 {self.home_d_home}px / 离主线 {self.home_d_main}px，"
                    f"要满足「离回正线 < 离主线 - {tol}」才继续走回正线）"
                    f"—— 这样短回正线才不会一进就出。")
            return
        # ⚠️ 不能只认 cmd_action == "goal"：goal 圆点半径只有 2px，而玩家坐标是质心、
        #    踩不中就判不到 —— 平台本来就不长时会**走过去却不触发切段**，
        #    冲出去后再也找不到像素 → 一路往前（用户实测："到终点停不下来"）。
        #    所以再用「离本段终点够近」补一次判定（见 _near_seg_goal）。
        #    ⚠️ 原注释写的是"一帧能移动十几像素"，那是**推断、从没实测**（2026-09-16 订正：
        #       实测 ~0.3 px/帧，差 20~50 倍）。别再引用那个数字。
        reached = (self.cmd_action == "goal") or self._near_seg_goal()
        if not reached:
            return
        # 没有主线路线时下面的取模会 ZeroDivisionError 把主循环带崩 —— 原地不动即可
        if not self.img_routes:
            return
        # Switch to next route map
        self.idx_routes = (self.idx_routes+1)%len(self.img_routes)
        self.route_reverse = False     # 换段了，往返方向要复位
        logger.debug(f"Change to new route:{self.idx_routes}")

    def run_once(self):
        '''
        Process one game window frame
        '''
        # Check if need viz window
        self.is_show_debug_window = self.is_need_show_debug_window
        if not self.is_show_debug_window:
            self.img_frame_debug = None
            self.img_route_debug = None

        ###########################
        ### Image Preprocessing ###
        ###########################
        # Get game window frame
        _perf_t0 = time.perf_counter()
        img_frame = self.get_img_frame()
        self._perf("抓帧", _perf_t0)
        if img_frame is None:
            activate_game_window(self.capture.window_title)
            return -1 # Wait for game window to be ready
        else:
            self.img_frame = img_frame

        # Grayscale game window
        self.img_frame_gray = cv2.cvtColor(self.img_frame, cv2.COLOR_BGR2GRAY)

        # Image for debug viz - use the cropped (client area) frame
        if self.is_show_debug_window:
            self.img_frame_debug = self.img_frame.copy()

        # Get current route image
        if self.cfg["bot"]["mode"] == "normal" and self.img_routes:
            # 回正线优先：走丢 / 掉出安全点时临时改用 route_home.png，
            # 走完回正线（goal）或回到安全点后自动切回主线（见 update_cmd_by_*）。
            if self.is_using_home_route and self.img_route_home is not None:
                self.img_route = self.img_route_home
            else:
                self.img_route = self.img_routes[self.idx_routes]
            if self.is_show_debug_window:
                self.img_route_debug = cv2.cvtColor(self.img_route, cv2.COLOR_RGB2BGR)


        ###################
        ### Get Minimap ###
        ###################
        # Get minimap coordinate and size on game window
        _perf_t0 = time.perf_counter()
        minimap_result = get_minimap_loc_size(self.img_frame)
        self._perf("小地图定位", _perf_t0)
        if minimap_result is None:
            if time.time() - self.t_last_minimap_update > 30:
                # Unable to get minimap for 30 seconds -> assume it's login screen
                loc_login_button = self.get_login_button_location()
                if loc_login_button:
                    logger.info("Found login button on screen. Proceed to login.")
                    click_in_game_window(self.capture.window_title,
                                         loc_login_button)
                    time.sleep(3)
                    click_in_game_window(self.capture.window_title,
                                         self.cfg["ui_coords"]["select_character"])
                    time.sleep(2)
        else:
            x, y, w, h = minimap_result
            # Shrink minimap boardary by one pixel to avoid pixel leaking to minimap
            x += 1
            y += 1
            w -= 2
            h -= 2
            # update minimap image
            self.loc_minimap = (x, y)
            self.img_minimap = self.img_frame[y:y+h, x:x+w]
            self.t_last_minimap_update = time.time()


        #################################
        ### Player Location Detection ###
        #################################
        # Get player location in game window
        # 国服无法用组队血条定位，角色名标签（nametag）是画面内的唯一定位方式
        _perf_t0 = time.perf_counter()
        loc_player = self.get_player_location_by_nametag()
        self._perf("名字定位", _perf_t0)

        # 定位健康度告警（2026-09-10 加）
        # 匹配失败时 get_player_location_by_nametag() 仍会返回坐标，只是那个坐标由
        # **上一帧的** self.loc_nametag 算出来 → 位置被「冻住」。分两种情况，严重程度完全不同：
        #   ① 命中过、最近失败 —— 基本无害。同一张地图里角色的**屏幕位置几乎是死的**
        #      （实测：地图A 连续 5 帧角色中心恒为 (711,382)，跨图也只有 y 差 39px），
        #      所以沿用上次命中的位置照样能用。最常见原因：地图拥挤、名牌被别的玩家盖住。
        #   ② **开局至今一次都没命中** —— 严重。位置停在初始值，攻击框落到画面左上角，
        #      症状是「只走路、不打怪」而且**不报错**。必须让用户看见。
        # 每 30 帧只在跨过阈值那一帧告警一次，不刷屏。
        _streak = self.nametag_miss_streak
        if _streak >= 30 and _streak % 30 == 0:
            if not self.nametag_ever_hit:
                logger.error(
                    f"[名字定位] 开局至今 {_streak} 帧一次都没定位到角色名名牌"
                    f"（阈值 {self.cfg['nametag']['diff_thres']}）—— 位置仍是初始值，"
                    f"攻击框会落在画面左上角，等于打不到怪。\n"
                    f"        排查：① 当前地图玩家是不是太多、自己的名牌被盖住了？"
                    f"先走到人少处或换个位置\n"
                    f"              ② 角色名/称号改过了吗？改过要重新标定模板\n"
                    f"              ③ 点工具箱「保存诊断包」发给 AI，让他看模板本身还认不认得")
            else:
                logger.warning(
                    f"[名字定位] 已连续 {_streak} 帧没匹配到角色名名牌"
                    f"（阈值 {self.cfg['nametag']['diff_thres']}），"
                    f"位置沿用上次命中的结果 —— 同图内角色屏幕位置基本不动，影响有限。"
                    f"最常见原因：地图拥挤、自己的名牌被别的玩家盖住。")

        # Update player location
        if loc_player is not None:
            # Check if character is on ladder
            dx = abs(loc_player[0] - self.loc_player[0])
            dy = abs(loc_player[1] - self.loc_player[1])
            if self.is_on_ladder:
                if dx > 3: # Leave ladder if there is horizontal move
                    self.is_on_ladder = False
            else:
                if dx < 3 and dy != 0:
                    self.is_on_ladder = True
            # logger.info((self.is_on_ladder, dx, dy))
            # Update player location
            self.loc_player = loc_player

        # Draw player center for debugging
        cv2.circle(self.img_frame_debug,
                self.loc_player, radius=3,
                color=(0, 0, 255), thickness=-1)

        # Get player location on minimap
        loc_player_minimap = get_player_location_on_minimap(
                                self.img_minimap,
                                minimap_player_color=self.cfg["minimap"]["player_color"])
        if loc_player_minimap:
            self.loc_player_minimap = loc_player_minimap

        # Get player location on global map
        self.loc_player_global = self.get_player_location_on_global_map()


        #######################
        ### Attack WatchDog ###
        ####################### Check if last attack is timeout
        dt = time.time() - self.t_last_attack
        if self.cfg['bot']['mode'] == 'normal' and \
            dt > self.cfg["watchdog"]["last_attack_timeout"]:
            logger.info(f"[Attack Timeout] Last attack timeout for {round(dt, 2)} seconds")
            # 换频道已移除（国服到处是人，换频道没意义）→ 超时后统一「回城并停止脚本」
            logger.info("[Attack Timeout] Return home!")
            press_key(self.cfg["key"]["return_home"])
            self.is_terminated = True
            self.kb.is_terminated = True


        ######################
        ### State Behavior ###
        ######################
        self.fsm.do_state_stuff()

        self.is_first_frame = False

        # 运行状态日志（每 5 秒一条，见 __init__ 里的说明）。
        # 位置有讲究：放在这里 = 指令刚由 hunting.on_frame 算完、且**还没**走到
        # 「关掉可视化就提前 return」那一步，所以关掉可视化时也照样打。
        self.n_frame += 1
        if time.time() - self.t_last_status_log >= 5:
            self.t_last_status_log = time.time()
            logger.info(
                f"[运行状态] 帧{self.n_frame}"
                f" 小地图{self.loc_player_minimap}"
                f" 全局{self.loc_player_global}"
                f" 段{self.idx_routes}/{len(self.img_routes)}"
                f"{' 回正' if self.is_using_home_route else ''}"
                f" 指令({self.cmd_move_x} {self.cmd_move_y} {self.cmd_action})"
                f" 怪{len(self.monsters)}"
                f" 名字命中{self.nametag_hit}(miss{self.nametag_miss_streak})")

        # ── 性能埋点：紧跟 [运行状态]，同样每 5 秒一条 ─────────────────
        # 位置有讲究：必须在「关掉可视化就提前 return」**之前** —— 这次排查
        # 正是要对比"开 / 关画面页签"两种场景，关掉时它也必须打得出来。
        if getattr(self, "perf_on", False) and \
                time.time() - getattr(self, "t_last_perf_log", 0) >= 5:
            try:
                _n = max(1, int(getattr(self, "perf_n", 0)))
                _win = max(1e-6, time.time() - self.t_last_perf_log)
                _acc = getattr(self, "perf_acc", None) or {}
                _cnt = getattr(self, "perf_cnt", None) or {}
                _mx = getattr(self, "perf_max", None) or {}
                logger.info(perf_summary_line({
                    "fps": _n / _win,
                    "n": _n,
                    "viz": bool(getattr(self, "is_show_debug_window", False)),
                    "gap_ms": float(getattr(self, "perf_gap", 0.0)) * 1000.0 / _n,
                    # ★ 分母用**实际执行次数**而不是帧数：降频的阶段（怪检测）
                    #   不能按帧数摊薄，否则日志显示的是假数字（502→251）。
                    "items": [(k, v * 1000.0 / max(1, _cnt.get(k, _n)),
                               _mx.get(k, 0.0) * 1000.0)
                              for k, v in _acc.items()],
                }))
            except Exception:                                # noqa: BLE001
                pass            # 打日志失败绝不能影响主循环
            finally:
                # 清零必须在 finally：就算抛异常也要滚窗口，否则窗口越滚越大
                self.perf_acc = {}
                self.perf_cnt = {}
                self.perf_max = {}
                self.perf_n = 0
                self.perf_gap = 0.0
                self.t_last_perf_log = time.time()


        #####################
        ### Debug Windows ###
        #####################
        # Don't show debug window to save system resource
        if not self.is_show_debug_window:
            return 0 # frame done

        # Print text on debug image
        _perf_t0 = time.perf_counter()
        self.update_info_on_img_frame_debug()
        self._perf("viz画图", _perf_t0)

        # Save debug window to video
        if self.video_writer:
            self.video_writer.write(self.img_frame_debug)

        # Resize img_route_debug for better visualization
        # ⚠️ 必须判空：这张图还没录路线时 img_route_debug 是 None，
        #    cv2.resize(None, ...) 会抛异常 → 主循环崩（2026-09-12 实测抓到）。
        if self.cfg["bot"]["mode"] == "normal" and \
                self.img_route_debug is not None:
            self.img_route_debug = cv2.resize(
                        self.img_route_debug, (0, 0),
                        fx=self.cfg["minimap"]["debug_window_upscale"],
                        fy=self.cfg["minimap"]["debug_window_upscale"],
                        interpolation=cv2.INTER_NEAREST)


        # Update FPS timer
        self.t_last_frame = time.time()

        return 0 # frame done

    def _perf(self, key, t0):
        '''主循环性能埋点：把一个阶段的耗时累加进当前 5 秒窗口（**纯观测**）。

        用法：在要测的代码段前后各加一行 ——
            _t = time.perf_counter()
            ... 要测的代码 ...
            self._perf("阶段名", _t)

        治的是什么（2026-09-14 真机实测）：配置 `fps_limit_main: 30`，实测主循环
        只有 **1.8~2 fps**，而全项目原来**没有任何地方记录主循环实际帧率**
        → "一帧 500ms 花在哪"完全靠猜。这套累加器就是量它用的。

        ⚠️ 为什么全程 `try/except` + `getattr` 兜底（三重保险，别删）：
            ① 它跑在**主循环**里 —— 抛异常 = 整个挂机停摆，
               这比"埋点没数据"严重一万倍；
            ② `tools/verify_navigation.py` 的 Stub **不走 `__init__`**，
               直接 `self.xxx` 会 AttributeError（这已经是第三次踩这个坑）。

        Args:
            key: 阶段名（中文，会直接进日志，如 "怪检测"）
            t0:  该阶段开始时的 `time.perf_counter()` 值
        '''
        try:
            if not getattr(self, "perf_on", False):
                return
            acc = getattr(self, "perf_acc", None)
            if acc is None:
                return
            d = time.perf_counter() - t0
            acc[key] = acc.get(key, 0.0) + d
            # 记**实际执行次数**（降频的阶段会小于帧数，见 perf_cnt 的说明）
            cnt = getattr(self, "perf_cnt", None)
            if cnt is not None:
                cnt[key] = cnt.get(key, 0) + 1
            mx = getattr(self, "perf_max", None)
            if mx is not None and d > mx.get(key, 0.0):
                mx[key] = d
        except Exception:                                   # noqa: BLE001
            pass        # 埋点自身绝不允许把主循环带崩

    def loop(self):
        '''
        Auto Bot main loop
        Only run when call autobot from UI framework and AutoBotController
        '''
        # Activate game window before starting
        activate_game_window(self.capture.window_title)
        time.sleep(0.3)

        while not self.kb.is_terminated:

            t_start = time.time()
            # ── 性能埋点：帧间隔（**含限帧 sleep**）与本窗口帧数 ──────────
            # 必须放这里而不是 run_once 里：run_once 可能提前 return -1（抓帧失败等），
            # 那些帧也得算进来，否则 fps 会虚高、真实卡顿看不出来。
            if getattr(self, "perf_on", False):
                try:
                    _now = time.time()
                    self.perf_gap += max(0.0, _now - self.t_perf_prev)
                    self.t_perf_prev = _now
                    self.perf_n += 1
                except Exception:                            # noqa: BLE001
                    pass

            # Process one game window frame
            self.is_frame_done = False
            # ⚠️ 必须包住（2026-09-12 加）：原来这里没有任何异常处理，run_once 一抛异常
            #    这个线程就**静默死亡** —— 键盘线程还在跑，但指令永远停在 "none none none"，
            #    于是「点开始、角色不动」，而日志里**一个字都没有**（异常堆栈只进 stderr，
            #    不进日志文件）。用户实际就撞上了这个：配置缺 nametag.mode → 第一帧
            #    KeyError → 挂机彻底不动且无从查起。现在异常必须落进日志。
            try:
                _perf_t0 = time.perf_counter()
                ret = self.run_once()
                self._perf("整帧", _perf_t0)
                if self.n_loop_err:
                    logger.info(f"[主循环] 已从异常中恢复（连续失败 {self.n_loop_err} 帧）")
                    self.n_loop_err = 0
            except Exception as e:
                self.n_loop_err += 1
                now = time.time()
                if self.n_loop_err == 1:
                    # 第一次必须打完整堆栈，否则等于没信息
                    logger.error(
                        "[主循环] run_once 抛异常，挂机已停止推进。完整堆栈如下：\n"
                        + traceback.format_exc())
                elif now - self.t_last_loop_err > 5:
                    logger.error(
                        f"[主循环] run_once 已连续失败 {self.n_loop_err} 帧，"
                        f"最近一次：{type(e).__name__}: {e}")
                self.t_last_loop_err = now
                ret = -1

            # Only proceed if the frame is valid
            if ret == 0:
                _perf_t0 = time.perf_counter()
                # Draw image on debug window
                if self.is_show_debug_window and self.is_ui:
                    # ⚠️ 必须判空（2026-09-12）：没有路线图时（这张图还没录路线）
                    #    img_route_debug 是 None，原来直接 .copy() 会 AttributeError，
                    #    而 viz 一崩主循环就跟着崩 —— 「打开游戏画面页签反而把挂机搞停」。
                    if self.img_frame_debug is not None:
                        self.image_debug_signal.emit(self.img_frame_debug.copy())
                    if self.img_route_debug is not None:
                        self.route_map_viz_signal.emit(self.img_route_debug.copy())
                self._perf("调试图emit", _perf_t0)
            else:
                pass
                # logger.warning("Skipped debug window update due to invalid frame.")

            self.is_frame_done = True

            # Cap FPS to save system resource
            frame_duration = time.time() - t_start
            target_duration = 1.0 / self.cfg["system"]["fps_limit_main"]
            if frame_duration < target_duration:
                time.sleep(target_duration - frame_duration)

def main(args):
    '''
    This main function works as a fake autoBotController
    This function will only be called when the using terminal to
    run this script
    '''
    #####################
    ### Init Auto Bot ###
    #####################
    try:
        mapleStoryAutoBot = MapleStoryAutoBot(args)
    except Exception as e:
        logger.error(f"MapleStoryAutoBot Init failed: {e}")
        sys.exit(1)
    else:
        logger.info("MapleStoryAutoBot Init Successfully")

    ####################
    ### Apply Config ###
    ####################
    # Load defautl yaml config
    cfg = load_yaml("config/config_default.yaml")
    # Override with user customized config
    cfg = override_cfg(cfg, load_yaml(f"config/config_{args.cfg}.yaml"))
    # Dump config to log for debugging
    logger.debug(yaml.dump(cfg, sort_keys=False,
                 indent=2, default_flow_style=False))
    # autoBot load config
    mapleStoryAutoBot.load_config(cfg)

    #####################
    ### Start AutoBot ###
    #####################
    try:
        mapleStoryAutoBot.start() # Start all threads in autoBot
    except Exception as e:
        logger.error(f"MapleStoryAutoBot start failed: {e}")
        mapleStoryAutoBot.terminate_threads() # Terminate all threads
        sys.exit(1)
    else:
        logger.info("MapleStoryAutoBot Start Successfully")

    # Start record game window for debugging
    if args.record:
        mapleStoryAutoBot.start_record()

    kb_listener = KeyBoardListener(is_autobot=True)
    kb_listener.register_func_key_handler('f1', mapleStoryAutoBot.kb.toggle_enable)
    kb_listener.register_func_key_handler('f2', mapleStoryAutoBot.screenshot_img_frame)
    kb_listener.register_func_key_handler('f12', mapleStoryAutoBot.terminate_threads)

    # While loop
    while not mapleStoryAutoBot.is_terminated:
        # Show debug image on window
        if mapleStoryAutoBot.is_frame_done:
            if mapleStoryAutoBot.img_frame_debug is not None:
                cv2.imshow("Game Window Debug",
                    mapleStoryAutoBot.img_frame_debug[:
                        mapleStoryAutoBot.cfg["ui_coords"]["ui_y_start"], :])

            if mapleStoryAutoBot.img_route_debug is not None:
                cv2.imshow("Route Map Debug", mapleStoryAutoBot.img_route_debug)

        cv2.waitKey(1)

        time.sleep(0.01)

    #########################
    ### Terminate AutoBot ###
    #########################
    mapleStoryAutoBot.terminate_threads() # Terminate all threads

    cv2.destroyAllWindows()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--disable_control',
        action='store_true',
        help='Disable simulated keyboard input'
    )

    parser.add_argument(
        '--cfg',
        type=str,
        default='custom',
        help='Choose customized config yaml file in config/'
    )

    parser.add_argument(
        '--debug',
        action="store_true",
        help="Enable debug logging"
    )

    parser.add_argument(
        '--record',
        action="store_true",
        help="Record debug window"
    )

    parser.add_argument(
        '--disable_viz',
        action="store_true",
        help="Disable viz debug window"
    )

    parser.add_argument(
        '--test_image',
        default="",
        help="Pass in image in test/XXX.png"
    )

    parser.add_argument(
        '--init_state',
        default="",
        help="choose the init_state"
    )

    args = parser.parse_args()
    args.is_ui = False # Always set False for command line

    # Set logger level
    if args.debug:
        logger.set_level(logging.DEBUG)

    main(args)
