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
