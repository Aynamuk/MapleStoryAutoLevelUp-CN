# Standard Import
from argparse import Namespace
import sys
import traceback

# Pyside
from PySide6.QtCore import Signal, QObject

#  Local Import
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.logger import logger
from src.utils.common import load_yaml, override_cfg
from src.input.KeyBoardListener import KeyBoardListener

class AutoBotController(QObject):
    '''
    AutoBot Controller server as a middleman between engine and UI
    '''
    debug_image_signal = Signal(object)
    route_map_viz_signal = Signal(object)

    #: 按键驱动不可用时（start_bot 返回 -2）的原因说明，供界面弹窗展示
    driver_problem = ""

    def __init__(self):
        """
        Init
        """
        super().__init__()

        # Init Auto Bot
        try:
            # Fake args to pass to AutoBot
            args = Namespace(
                disable_control=False,
                cfg="default",
                debug=False,
                record=False,
                is_ui=True,
                disable_viz=True,
                test_image='',
                init_state='',
            )
            self.auto_bot = MapleStoryAutoBot(args)
        except Exception as e:
            logger.error(f"MapleStoryAutoBot Init Failed: {e}")
            sys.exit(1)
        else:
            logger.info("MapleStoryAutoBot Init Successfully")

        # Update signal for debug window viz
        self.auto_bot.update_signals(self.debug_image_signal,
                                     self.route_map_viz_signal)

        # Monitor function keys
        self.kb_listener = KeyBoardListener(is_autobot=True)

    def toggle_enable(self):
        '''
        toggle_enable
        '''
        self.is_enable = not self.is_enable
        logger.info(f"Player pressed F1, is_enable:{self.is_enable}")

        # Make sure all key are released
        self.release_all_key()

    def update_signal(self, ui):
        '''
        Only called after UI init
        '''
        self.debug_image_signal.connect(ui.update_debug_canvas)
        self.route_map_viz_signal.connect(ui.update_route_map_canvas)
        # Register Function Key handler
        # F1~F4 全部走 Qt 信号回主线程（游戏窗口聚焦时也能触发主界面功能；
        # F4 录路线要弹 Qt 输入框，绝不能在 pynput 线程直接操作 Qt 控件）
        self.kb_listener.register_func_key_handler('f1', ui.hotkey_f1.emit)
        self.kb_listener.register_func_key_handler('f2', ui.hotkey_f2.emit)
        self.kb_listener.register_func_key_handler('f3', ui.hotkey_f3.emit)
        self.kb_listener.register_func_key_handler('f4', ui.hotkey_f4.emit)
        # F2/F3/F4 现在是「标定名字 / 截怪物模板 / 录路线」—— 点主界面按钮触发，
        # 不注册游戏内热键：录路线要弹地图ID输入框，必须走 Qt 主线程，
        # 从 pynput 线程弹 Qt 对话框会崩。
        self.kb_listener.register_func_key_handler('f12', lambda: ui.request_close.emit())

    def start_bot(self, cfg_path):
        '''
        Start the bot engine threads
        '''
        # ⚠️ 必须先把 config_default 铺底，再用用户配置覆盖（2026-09-12 实测踩坑）。
        #    界面保存的用户配置是「局部配置」——只写用户改过的键。单独喂给引擎会缺
        #    引擎必需的键。本次实例：界面保存时把 nametag 段整段换成了用户方案的
        #    的局部版本（只剩 name/offset），引擎第一帧读 cfg["nametag"]["mode"]
        #    → KeyError → 主循环线程**静默死亡** → 症状「点开始、角色不动、
        #    日志里啥都没有」，查了一整天。铺底之后任何局部配置都能安全跑
        #    （终端入口 main() 本来就是这么做的，只有界面这条路径漏了）。
        cfg = override_cfg(load_yaml("config/config_default.yaml"),
                           load_yaml(cfg_path))

        # Auto bot load config
        # ⚠️ 必须包住（2026-09-12）：load_config 内部**没有** try/except，它一抛异常
        #    （字段写错、资源路径不对、误用还没赋值的属性……）异常就只进 stderr，
        #    **不进日志** → 界面上表现为「点开始没反应」，而日志区一片空白，
        #    完全无从查起。
        #    本次实例：load_config 里误用 self.cfg（它要到函数末尾才赋值，那会儿
        #    还是 None）→ AttributeError → 静默失败，排查花了一轮。
        #    现在必须把完整堆栈写进日志。
        try:
            ret = self.auto_bot.load_config(cfg)
        except Exception:
            logger.error("[start_bot] load_config 抛异常，启动失败。完整堆栈：\n"
                         + traceback.format_exc())
            return -1 # Load fail
        if ret != 0:
            return -1 # Load fail

        # ── 按键驱动前置检查（2026-09-27 加）────────────────────────────────
        # 为什么必须在 start() **之前**：
        #   Interception 是**内核驱动**，pip 装包不等于装好驱动 —— 还要单独
        #   下载安装并重启电脑。缺它时按键一个都送不出去，而症状是
        #   「点开始、角色一动不动」，用户在日志里只能看到一句英文
        #   （interception driver was not found or is not installed）。
        #   实测（issue #2）：用户下载的是免安装包，既不知道要装驱动，
        #   也没有 Python 去 pip install，就这么卡住了。
        # ⇒ 在这里拦下，把「装什么、怎么装、装完要重启」直接讲清楚。
        try:
            from src.input.InterceptionController import (
                driver_status, how_to_install_driver)
            ok, detail = driver_status()
        except Exception:                                        # noqa: BLE001
            ok, detail = True, ""      # 检查本身出错不拦路（宁可让引擎去报错）
        if not ok:
            self.driver_problem = detail
            logger.error(
                "[start_bot] 按键驱动不可用，已阻止启动 —— 否则会表现为"
                "「角色一动不动」而看不出原因。\n"
                f"        原因：{detail}\n"
                + "\n".join("        " + ln for ln in how_to_install_driver().splitlines()))
            return -2   # 与"配置加载失败(-1)"区分开，界面据此弹安装指引

        # ── 有效路线前置检查（2026-09-27 加）────────────────────────────────
        # 为什么必须在这里拦：路线全部判废时 `img_routes` 是空列表，而引擎**没有**
        # 自我保护 —— `get_nearest_color_code()` 第一行就是
        # `h, w = self.img_route.shape[:2]`（`img_route` 为 None）⇒ 每帧抛
        # `AttributeError: 'NoneType' object has no attribute 'shape'`，
        # 主循环停摆但**不退出**。
        #
        # 症状（用户实测）：点 F1 后「角色一动不动、日志也不刷屏」，而**前台守卫
        # 线程还在正常把游戏窗口抢回前台** ⇒ 表现得像"程序卡住了"，
        # 用户只能去任务管理器强杀进程，完全不知道真因是路线判废。
        #
        # ⇒ 在这里拦下，把「哪张图、为什么废、怎么修」直接讲清楚，而不是启动后装死。
        #   （判据用 img_routes 是否为空：所有判废分支都走 `continue`，
        #     不会进这个列表，见 MapleStoryAutoLevelUp 加载路线那段。）
        if not getattr(self.auto_bot, "img_routes", None):
            self.no_route_problem = True
            _map = (cfg.get("bot", {}) or {}).get("map", "") or "<未选择>"
            logger.error(
                f"[start_bot] 地图「{_map}」**没有一条可用的路线**，已阻止启动 —— "
                f"否则会表现为「点开始后角色一动不动」，且日志不会继续刷新。\n"
                f"        常见原因：这条路线判废了（没有终点 goal 标记 / 没录到路线 / "
                f"去程回程叠在一段）。\n"
                f"        上面的日志里有 [路线校验] 打出的**具体原因**，照着修即可。\n"
                f"        怎么补：主界面按 F4 重录这张图 —— 一路走到终点按 F3 保存"
                f"（收尾会自动盖上 goal 标记）。")
            return -3   # 与"驱动不可用(-2)"区分开，界面据此弹"没有可用路线"

        # Start the bot engine
        try:
            self.auto_bot.start()
        except Exception:
            # 同样要完整堆栈：只记 {e} 的话像「'NoneType' object has no attribute
            # 'shape'」这种根本看不出是哪一行（2026-09-12 踩过）
            logger.error("[start_bot] 启动引擎时抛异常。完整堆栈：\n"
                         + traceback.format_exc())
            return -1 # Start fail

        return 0 # start bot success

    def pause_bot(self):
        '''
        Gracefully pause in the engine
        '''
        self.auto_bot.pause()

    def take_screenshot(self):
        '''
        Called when user press screenshot button
        '''
        self.auto_bot.screenshot_img_frame()

    def start_recording(self):
        '''
        Called when user press start record button
        '''
        self.auto_bot.start_record()

    def stop_recording(self):
        '''
        Called when user press stop record button
        '''
        self.auto_bot.stop_record()

    def terminate_bot(self):
        '''
        Called when user stop bot or close UI
        '''
        # Terminate all bot threads
        self.auto_bot.terminate_threads()

    def enable_bot_viz(self):
        '''
        Called when user switch to viz tab
        '''
        self.auto_bot.enable_viz()

    def disable_bot_viz(self):
        '''
        Called when user switch from viz tab
        '''
        self.auto_bot.disable_viz()
