'''
KeyBoardController
Simulate user keyboard input to control character in the game 
使用 Interception 驱动做内核级键盘模拟
'''
# Standard Import
import os
import threading
import time

# Library import - 使用 Interception 驱动替代 pyautogui
from src.input.InterceptionController import key_down, key_up, press_key, init_interception

# Local import
from src.utils.logger import logger, jump_trace_on
from src.utils.common import activate_game_window
import win32gui
import win32process


#: 跳跃冷却的**内置兜底值**（配置缺键 / 填了非数字时用它，见 jump_ready）
DEFAULT_JUMP_COOLDOWN = 0.8


def jump_ready(t_last_jump, now=None, cooldown=DEFAULT_JUMP_COOLDOWN) -> bool:
    '''跳跃键**现在**该不该发（纯函数，不碰 self、不读配置 → 可离线单测）。

    治的是「连跳」（2026-09-13 用户实测必修）：角色只要站在一片 jump 像素上，
    键盘线程就是**每帧**走到 run() 的 jump 分支按一次跳 —— 30fps 下每秒按 30 次，
    表现是「跳上平台了还在跳，结果又跳出去了」。所以按过一次必须挡一段时间。

    ⚠️ 为什么抽成模块级纯函数（跟 is_game_hwnd 一个道理）：
       这段判断原来埋在 run() 的循环里，要验它就得把整个键盘线程跑起来（要驱动、
       要游戏窗口、要抢焦点）—— 结果就是「只有实测验证、没有用例」，改坏了没人拦。
       抽出来之后 tools/verify_jump_cooldown.py 能离线把每条分支都钉死。

    Args:
        t_last_jump: 上次**真正发出**跳跃键的时间（time.time() 时间戳）。
                     ⚠️ 初值是 0.0（1970 年）→ now - 0.0 是个巨大的数 → **第一次必发**。
                     这条不能破：破了就是"加了冷却后角色压根不跳了"。
        now: 当前时间，None → 取 time.time()（run() 里就走这条）
        cooldown: 冷却秒数。**<= 0 表示关冷却**（每次都发）；
                  None / 非数字 → 回落 DEFAULT_JUMP_COOLDOWN(0.8)，不抛异常。

    Returns:
        bool: True = 该发（冷却已过 / 关着 / 从没按过）

    判定式（与抽取前**逐字等价**，别改成 `>`）：
        cooldown <= 0  or  (now - t_last_jump) >= cooldown
    '''
    if now is None:
        now = time.time()
    try:
        cd = float(cooldown)
    except (TypeError, ValueError):
        # ⚠️ 为什么吞掉而不是抛：配置里填 "abc" / 留空时，抛异常会让键盘线程
        #    整个挂掉（角色再也不动了），而"冷却失效"只是回到老行为。
        #    跟 run() 里读配置那段的 try/except 是同一条纪律。
        cd = DEFAULT_JUMP_COOLDOWN
    if cd <= 0:
        return True                      # 0 / 负数 = 关掉冷却（退回老行为）
    try:
        last = float(t_last_jump)
    except (TypeError, ValueError):
        return True                      # 认不出上次时间 → 宁可发，也别把跳跃卡死
    return (float(now) - last) >= cd


#: 「被冷却挡住」这条日志的两个旋钮（见 jump_blocked_msg）
JUMP_BLOCKED_REPORT_MIN = 5      # 连挡几次才开始报（前几次是正常现象，不值得刷屏）
JUMP_BLOCKED_REPORT_EVERY = 1.0  # 开始报之后，每秒最多报一条


def jump_blocked_msg(n_blocked, dt_since_last, cooldown,
                     last_report_t=0.0, now=None):
    '''「想跳但被冷却挡住」这条日志的**文案 + 节流**（纯函数 → 可离线单测）。

    治的是什么（2026-09-14 掉坑回不来排查）：
        最可疑的病因是「jump_cooldown=1.0s 吞掉二次起跳」——角色第一次没跳上去、
        落回坑底，1s 内的第二次起跳被挡掉 → 原地站 1s → 被主线指令横向带偏 →
        死循环。但**旧日志完全没有这条记录**（下面 run() 的注释原本写明
        "故意不打日志"），所以这个猜想既没法证实也没法否证。这里补上，但**必须节流**。

    ⚠️ 为什么必须节流（这正是当初"故意不打日志"的原因，不能无视）：
        角色站在一片 jump 像素上时，键盘线程**每帧**走到 jump 分支 —— 30fps 下
        连挡 1 秒 = 30 条一模一样的日志，会把「进入/退出回正」和「真的按下了跳跃键」
        这些真正有用的行全淹掉。所以定两条规矩：
          ① 连挡不够多不报（刚跳完的那几次是正常现象，不算病）
          ② 开始报之后每秒最多一条（但**计数照常累加**，文案里带总数）

    Args:
        n_blocked:     当前连挡了几次（真发键后清零）
        dt_since_last: 距上次**真正发出**跳跃键过了多少秒
        cooldown:      冷却秒数（进文案，让人看懂"还差多久才能再跳"）
        last_report_t: 上次报这条日志的时间戳（0.0 = 从没报过 → 立刻可报）
        now:           当前时间，None → 取 time.time()

    Returns:
        (str | None, float): (该报的文案 / None=这次别报, 新的 last_report_t)
            ⚠️ 调用方**必须**把第二个返回值写回自己的状态，否则节流失效。
    '''
    if now is None:
        now = time.time()
    try:
        n = int(n_blocked)
    except (TypeError, ValueError):
        n = 0
    if n < JUMP_BLOCKED_REPORT_MIN:
        return None, last_report_t          # 连挡次数不够 → 不报（计数照常累加）
    try:
        last_t = float(last_report_t)
    except (TypeError, ValueError):
        last_t = 0.0
    if (float(now) - last_t) < JUMP_BLOCKED_REPORT_EVERY:
        return None, last_t                 # 距上次报还不满 1 秒
    try:
        dt = float(dt_since_last)
        cd = float(cooldown)
    except (TypeError, ValueError):
        dt, cd = 0.0, DEFAULT_JUMP_COOLDOWN
    tip = ""
    if n >= 20:
        tip = (" —— 连挡这么多次说明**起跳一直没成功**，"
               "重点看上面有没有「真的按下了跳跃键」")
    else:
        tip = " —— 这就是「二次起跳被吞」的现场"
    return (f"[按键·跳跃] 想跳但被冷却挡住（已连挡 {n} 次，"
            f"距上次发跳 {dt:.2f}s / 冷却 {cd:.1f}s）{tip}"), float(now)


class KeyBoardController():
    '''
    KeyBoardController - Interception 驱动输入版本
    '''
    def __init__(self, cfg):
        self.cfg = cfg
        self.cmd_action = "none"
        self.cmd_up_down = "none"
        self.cmd_left_right = "none"
        self.cmd_up_down_last = ""
        self.cmd_left_right_last = ""
        self.window_title = cfg["game_window"]["title"]
        self.fps = 0 # Frame per seconds
        # Timer
        self.t_last_run = time.time()
        self.t_last_skill = 0.0 # Last time character perform action(attack, cast spell, ...)
        # 上次真正发出跳跃键的时间（跳跃冷却用，见 run() 的 jump 分支）
        self.t_last_jump = 0.0
        self.t_last_buff_cast = [0] * len(self.cfg["buff_skill"]["keys"]) # Last time cast buff skill
        # Flags
        self.is_enable = True
        self.is_terminated = False
        # 游戏窗口句柄：由 AutoBot.start() 在抓到窗口后回填。判"游戏是否在前台"用。
        # 为 None 时退回「标题匹配 + 排除本进程」的弱判据（见 is_game_window_active）。
        self.game_hwnd = None
        # 前台守卫状态（见 run() / _handle_game_window_not_active）
        self._fg_blocked = False
        self._fg_blocked_since = 0.0
        self.t_last_fg_retry = 0.0
        self.t_last_fg_warn = 0.0
        self._logged_first_key = False
        # 连续判否计数：前台切换瞬间 GetForegroundWindow() 会短暂返回 0（实测），
        # 单次采样会把「其实在前台」误判成「不在前台」→ 松键 + 刷假告警。
        # 要求连续 N 次都判否才动手（见 run()）。
        self._fg_miss_count = 0
        # 按键心跳：近 5 秒真正发出去的按键次数 + 上次打心跳的时间。
        # 用途：把「指令是 none（压根没键可发）」和「键发了但游戏没反应」区分开 ——
        # 没有它，这两种完全不同的病因在日志里长得一模一样。
        self.n_key_sent = 0
        self.t_last_kb_status = 0.0
        # 回正起跳埋点（2026-09-14 排查「掉坑回不来」用，见 log.jump_trace）：
        #   n_jump_sent    = 本轮**真的**按下跳跃键几次
        #   n_jump_blocked = **连续**被 jump_cooldown 挡掉几次（真发键后清零）
        # ⚠️ 这两个数是区分病因的关键：走到起跳点 N 次、却只按下 1 次
        #    → 就是「二次起跳被冷却吞了」。
        self.n_jump_sent = 0
        self.n_jump_blocked = 0
        self._t_last_block_log = 0.0
        # Parameters
        self.fps_limit = self.cfg["system"]["fps_limit_keyboard_controller"]

        # 初始化 Interception 驱动输入
        try:
            init_interception()
            logger.info("[KeyBoardController] Interception 驱动输入已初始化")
        except Exception as e:
            logger.error(f"[KeyBoardController] Interception 初始化失败: {e}")
            logger.warning("[KeyBoardController] 请确认已安装 Interception 驱动并重启过电脑")

        # set up attack key
        self.attack_key = ""
        if cfg["bot"]["attack"] == "aoe_skill":
            self.attack_key = cfg["key"]["aoe_skill"]
        elif cfg["bot"]["attack"] == "directional":
            self.attack_key = cfg["key"]["directional_attack"]
        else:
            raise ValueError(f"Unexpected attack type: {cfg['bot']['attack']}")

        # Start keyboard control thread
        threading.Thread(target=self.run, daemon=True).start()

        logger.info("[KeyBoardController] Init done")

    def toggle_enable(self):
        '''
        toggle_enable
        '''
        self.is_enable = not self.is_enable
        logger.info(f"Player pressed F1, is_enable:{self.is_enable}")

        # Make sure all key are released
        self.release_all_key()

    def disable(self):
        '''
        disable keyboard controlller
        '''
        self.is_enable = False

    def enable(self):
        '''
        enable keyboard controlller
        '''
        self.is_enable = True

    def set_command(self, new_command):
        '''
        Set keyboard command
        '''
        self.cmd_left_right, self.cmd_up_down, self.cmd_action = new_command.split()

    def is_game_window_active(self):
        '''
        Check if the game window is currently the active (foreground) window.

        ⚠️ 判据必须按 **hwnd**，不能用「标题包含关键词」（2026-09-12 实测踩坑）：
           游戏窗口标题 = '冒险岛怀旧服'
           本项目主界面标题 = '冒险岛怀旧服 - 自动练级'  ← 含游戏标题这个子串
           原实现 `self.window_title in active_window.title` 于是有一个致命漏洞：
           **界面自己在前台时也返回 True**。控制器以为游戏在前台，照常发方向键，
           而按键落在界面上、游戏一个都收不到 —— 症状「点开始角色不动」，
           而且全程不报错、不打日志（本 bug 就是这么埋了一整天）。
           现在优先与抓帧器认定的游戏窗口句柄比对（唯一可靠）；拿不到句柄时才退回
           「标题包含 + 不属于本进程」的弱判据。

        Returns:
        - True
        - False
        '''
        try:
            return self.is_game_hwnd(win32gui.GetForegroundWindow())
        except Exception:
            return False

    def is_game_hwnd(self, hwnd):
        '''
        给定窗口句柄，判断它是不是**游戏**窗口。抽出来单独放，便于离线单测
        （不需要真的去抢前台焦点）。

        判据优先级：
          1. 已知游戏窗口句柄（抓帧器认定的那个）→ 必须完全相等。唯一可靠的判据。
          2. 未拿到句柄（test_image 模式等）→ 退回「标题包含关键词 且 不属于本进程」。
        '''
        if not hwnd:
            return False
        if self.game_hwnd:
            return hwnd == self.game_hwnd
        try:
            title = win32gui.GetWindowText(hwnd)
            if not title or self.window_title not in title:
                return False
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid != os.getpid()
        except Exception:
            return False

    def foreground_window_title(self):
        '''当前前台窗口标题（仅用于给用户看的提示信息）。'''
        try:
            return win32gui.GetWindowText(win32gui.GetForegroundWindow())
        except Exception:
            return "<未知>"

    def _handle_game_window_not_active(self):
        '''
        游戏窗口不在前台时的处置：松键 + 提示 + 自动切回前台。

        ⚠️ 必须先松键：原实现是直接 continue，如果角色正走着时前台被切走，
           方向键会**一直保持按下**（Interception 的按下/抬起是配对的、没有超时），
           等前台切回来角色已经在往一个方向暴走。
        '''
        now = time.time()
        if not self._fg_blocked:
            self._fg_blocked = True
            self._fg_blocked_since = now
            self.t_last_fg_warn = now
            self.release_all_key()
            logger.warning(
                "[前台检查] 游戏窗口不在前台，暂停发送按键。"
                f"当前前台窗口 = {self.foreground_window_title()!r}。"
                "挂机期间游戏窗口必须保持在前台（脚本会每 2 秒自动切回一次）。")

        # 自动切回前台（节流 2 秒，避免和用户抢焦点太频繁）
        if now - self.t_last_fg_retry > 2.0:
            self.t_last_fg_retry = now
            try:
                if activate_game_window(self.window_title):
                    logger.info("[前台检查] 已把游戏窗口切回前台，继续发送按键")
                    self._fg_blocked = False
            except Exception as e:
                logger.warning(f"[前台检查] 切回前台失败: {e}")

        # 仍被挡着：每 5 秒提示一次，别刷屏
        if now - self.t_last_fg_warn > 5.0:
            self.t_last_fg_warn = now
            logger.warning(
                f"[前台检查] 游戏窗口仍不在前台（已 {round(now - self._fg_blocked_since)} 秒），"
                f"当前前台窗口 = {self.foreground_window_title()!r}")

    def release_all_key(self):
        '''
        Release all key
        '''
        key_up("left")
        key_up("right")
        key_up("up")
        key_up("down")
        # Also release attack keys to stop any ongoing attacks
        key_up(self.attack_key)

    def _kd(self, key):
        '''按下某键并计数（计数供 [按键状态] 心跳用，见 n_key_sent）。'''
        key_down(key)
        self.n_key_sent += 1

    def _press(self, key):
        '''点按某键并计数。空键名直接跳过（配置里允许留空，如 teleport）。'''
        if not key:
            return
        press_key(key)
        self.n_key_sent += 1

    def limit_fps(self):
        '''
        Limit FPS
        '''
        # If the loop finished early, sleep to maintain target FPS
        target_duration = 1.0 / self.fps_limit  # seconds per frame
        frame_duration = time.time() - self.t_last_run
        if frame_duration < target_duration:
            time.sleep(target_duration - frame_duration)

        # Update FPS
        self.fps = round(1.0 / (time.time() - self.t_last_run))
        self.t_last_run = time.time()
        # logger.info(f"FPS = {self.fps}")

    def run(self):
        '''
        run
        '''
        while not self.is_terminated:
            if not self.is_enable:
                self.limit_fps()
                continue

            # 游戏窗口必须在前台：Interception 的按键只会落到**前台窗口**，
            # 游戏不在前台时发键 = 全部打给别人。原实现是静默 continue（连日志都没有），
            # 于是「点开始角色不动」这件事在日志里完全查不到 —— 见 is_game_window_active。
            #
            # ⚠️ 必须连续判否 3 次才动手（2026-09-12 实测）：窗口切换的**瞬间**
            #    GetForegroundWindow() 会短暂返回 0，单次采样会把「其实在前台」
            #    误判成「不在前台」→ 白松一次键 + 刷一条假告警（实测日志里那几条
            #    「当前前台窗口 = '冒险岛怀旧服'」就是这么来的）。
            if self.is_game_window_active():
                self._fg_miss_count = 0
            else:
                self._fg_miss_count += 1
            if self._fg_miss_count >= 3:
                self._handle_game_window_not_active()
                self.limit_fps()
                continue

            if self._fg_blocked:
                self._fg_blocked = False

            # 第一次真正把按键发出去时记一条：这是"角色到底有没有收到指令"
            # 最直接的一条证据（此前完全没有这类日志，排查只能靠猜）。
            if not self._logged_first_key:
                self._logged_first_key = True
                logger.info(
                    f"[按键] 已开始向游戏窗口发送按键（第一条指令："
                    f"{self.cmd_left_right} {self.cmd_up_down} {self.cmd_action}）")

            # Buff skill
            for i, buff_skill_key in enumerate(self.cfg["buff_skill"]["keys"]):
                cooldown = self.cfg["buff_skill"]["cooldown"][i]
                if time.time() - self.t_last_buff_cast[i] >= cooldown and \
                    time.time() - self.t_last_skill > self.cfg["buff_skill"]["action_cooldown"]:
                    self._press(buff_skill_key)
                    logger.info(f"[Buff] Press buff skill key: '{buff_skill_key}' (cooldown: {cooldown}s)")
                    # Reset timers
                    self.t_last_buff_cast[i] = time.time()
                    self.t_last_skill = time.time()
                    break

            ##########################
            ### Left-Right Command ###
            ##########################
            if self.cmd_left_right == "left":
                key_up("right")
                self._kd("left")
            elif self.cmd_left_right == "right":
                key_up("left")
                self._kd("right")
            elif self.cmd_left_right == "stop":
                key_up("left")
                key_up("right")
            elif self.cmd_left_right == "none":
                if self.cmd_left_right_last != "none":
                    key_up("left")
                    key_up("right")
            else:
                logger.error("[KeyBoardController] Unsupported left-right command: "
                             f"{self.cmd_left_right}")
            self.cmd_left_right_last = self.cmd_left_right

            #######################
            ### Up-Down Command ###
            #######################
            if self.cmd_up_down == "up":
                key_up("down")
                self._kd("up")
            elif self.cmd_up_down == "down":
                key_up("up")
                self._kd("down")
            elif self.cmd_up_down == "stop":
                key_up("up")
                key_up("down")
            elif self.cmd_up_down == "none":
                if self.cmd_up_down_last != "none":
                    key_up("up")
                    key_up("down")
            else:
                logger.error("[KeyBoardController] Unsupported up-down command: "
                             f"{self.cmd_up_down}")
            self.cmd_up_down_last = self.cmd_up_down

            ######################
            ### Action Command ###
            ######################
            if self.cmd_action == "jump":
                # ── 跳跃冷却（2026-09-13 用户实测必修）────────────────────────
                # 角色只要站在一片 jump 像素上，本线程就是**每帧**走到这里按一次跳。
                # 30fps 下 = 每秒按 30 次 = 连跳，表现是「跳上平台了还在跳，
                # 结果又跳出去了」。所以按一次跳之后必须挡一段时间。
                # ⚠️ 这里**曾经故意不打日志**：冷却命中的帧每帧都会走，打了就是刷屏。
                #    2026-09-14 排查「掉坑回不来」时发现——不打就永远分不清
                #    「压根没走到起跳点」和「走到了、但被冷却吞了」这两种病。
                #    所以现在改成**节流**打（连挡 ≥5 次才开报、之后每秒最多一条），
                #    节流与文案都抽进了纯函数 jump_blocked_msg（离线可测）。
                try:
                    _jump_cd = float(self.cfg["key"].get("jump_cooldown",
                                                         DEFAULT_JUMP_COOLDOWN))
                except (TypeError, ValueError):
                    _jump_cd = DEFAULT_JUMP_COOLDOWN
                # "该不该发"这个判断已抽成模块级纯函数 jump_ready（离线可测，
                # 见 tools/verify_jump_cooldown.py）—— 这里只负责读配置 + 发键。
                if jump_ready(self.t_last_jump, cooldown=_jump_cd):
                    # ⚠️ `self.t_last_jump = time.time()` 这行**必须保持原样写法**：
                    #    tools/verify_jump_cooldown.py 用 inspect.getsource 反向钉住了它
                    #    （防有人把更新删掉 → 冷却永远不生效 → 连跳复发）。
                    #    所以"距上次多久"要在更新**之前**先存下旧值，更新完再算。
                    _prev_jump = self.t_last_jump
                    self._press(self.cfg["key"]["jump"])
                    self.t_last_jump = time.time()
                    _dt = self.t_last_jump - _prev_jump
                    # ── 埋点①：真的按下去了（区分 B / C 的关键一行）───────
                    self.n_jump_sent += 1
                    self.n_jump_blocked = 0
                    if jump_trace_on(self.cfg):
                        logger.info(f"[按键·跳跃] 真的按下了跳跃键"
                                    f"（本轮第 {self.n_jump_sent} 次，"
                                    f"距上次 {_dt:.2f}s）")
                else:
                    # ── 埋点②：想跳但被冷却挡住（节流，见 jump_blocked_msg）──
                    # 计数**每帧照常累加**（不节流），只有"打不打日志"才节流 ——
                    # 这样文案里那个"已连挡 N 次"才是真实的累计值。
                    self.n_jump_blocked += 1
                    if jump_trace_on(self.cfg):
                        _msg, _t = jump_blocked_msg(
                            self.n_jump_blocked,
                            time.time() - self.t_last_jump,
                            _jump_cd,
                            getattr(self, "_t_last_block_log", 0.0))
                        self._t_last_block_log = _t
                        if _msg:
                            logger.info(_msg)
            elif self.cmd_action == "teleport":
                self._press(self.cfg["key"]["teleport"])
            elif self.cmd_action == "attack":
                self._press(self.attack_key)
                self.t_last_skill = time.time()
            elif self.cmd_action == "goal":
                pass
            elif self.cmd_action == "none":
                pass
            else:
                logger.error("[KeyBoardController] Unsupported action command: "
                             f"{self.cmd_action}")

            # 按键心跳（每 5 秒一条）：把「指令是 none，压根没键可发」和
            # 「键确实发了，但游戏没反应」这两种病因区分开 —— 没有它，两者在
            # 日志里长得一模一样（都是啥都没有）。发键次数为 0 而指令非 none，
            # 就说明卡在守卫/使能开关上。
            if time.time() - self.t_last_kb_status >= 5:
                self.t_last_kb_status = time.time()
                logger.info(
                    f"[按键状态] 指令({self.cmd_left_right} {self.cmd_up_down} "
                    f"{self.cmd_action}) 近5秒实际发键 {self.n_key_sent} 次 "
                    f"fps={self.fps}")
                self.n_key_sent = 0

            self.limit_fps()

        self.release_all_key() # Prevent key keep press down after termination

        logger.info("[KeyBoardController] terminated")
