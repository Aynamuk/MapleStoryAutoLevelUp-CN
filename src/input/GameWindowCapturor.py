'''
Execute this script:
python mapleStoryAutoLevelUp.py --map cloud_balcony --monster brown_windup_bear,pink_windup_bear
'''
# Standard import
import time
import threading

# Libarary Import
from windows_capture import WindowsCapture, Frame, InternalCaptureControl
import cv2

# local import
from src.utils.logger import logger
import win32gui

from src.utils.common import (get_game_window_title_by_token, load_image,
                              find_game_window_hwnd)

class GameWindowCapturor:
    '''
    GameWindowCapturor
    '''
    def __init__(self, cfg, test_image_name = None, window_title = None):
        self.cfg = cfg
        self.frame = None
        self.lock = threading.Lock()
        self.fps = 0
        self.fps_limit = cfg["system"]["fps_limit_window_capturor"]
        self.t_last_run = 0.0
        self.capture_control = None
        self.window_title = ""

        # If use test image as input, disable the whole capture thread
        if test_image_name is not None:
            self.frame = load_image(f"test/{test_image_name}.png")
            return

        # 允许调用方直接指定窗口标题（跳过按关键词查找）。
        # 用于：诊断工具量指定窗口、多窗口歧义时人工指定。
        if window_title is not None:
            self.window_title = window_title
            self.window_hwnd = win32gui.FindWindow(None, window_title)
            logger.info(f"[GameWindowCapturor] Use assigned window title: {self.window_title}")
        else:
            # 按配置关键词解析出**唯一的窗口句柄**（内部会排除资源管理器等外壳窗口）
            self.window_hwnd = find_game_window_hwnd(cfg["game_window"]["title"])
            self.window_title = (win32gui.GetWindowText(self.window_hwnd)
                                 if self.window_hwnd else None)

        if not self.window_hwnd:
            raise RuntimeError(
                f"[GameWindowCapturor] 找不到游戏窗口（关键词：{cfg['game_window']['title']}）。"
                f"请确认：游戏已打开、处于窗口模式、且没有被最小化。"
            )
        logger.info(f"[GameWindowCapturor] Found game window: hwnd={self.window_hwnd} "
                    f"title={self.window_title!r}")

        # Create capture handler
        # 【注意】 必须按 **hwnd** 捕获，不能用 window_name —— 2026-09-10 实测踩坑：
        #    抓帧库对 window_name 做的是**子串匹配**（其文档原文 "Name Of The Window To Capture
        #    (substring match)"），而游戏标题「冒险岛怀旧服」正好是资源管理器窗口标题
        #    「冒险岛怀旧服国服自动练级 - 文件资源管理器」的**前缀** → 会随机抓到资源管理器，
        #    而且全程不报错。实测连抓 5 帧有 2 帧是资源管理器（2546x1433）。
        #    库的 window_hwnd 参数文档写明"比 window_name 更可靠"，这里改用它。
        self.capture = WindowsCapture(window_hwnd=self.window_hwnd)
        self.capture.event(self.on_frame_arrived)
        self.capture.event(self.on_closed)

        # Start capturing thread
        self.capture_control = self.capture.start_free_threaded()

        logger.info("[GameWindowCapturor] Init done")

    def on_frame_arrived(self, frame: Frame,
                         capture_control: InternalCaptureControl):
        '''
        Frame arrived callback: store frame into buffer with lock.
        '''
        with self.lock:
            self.frame = frame.frame_buffer
        self.limit_fps()

    def on_closed(self):
        '''
        Capture closed callback.
        '''
        logger.warning("[GameWindowCapturor] closed.")
        cv2.destroyAllWindows()

    def get_frame(self):
        '''
        Safely get latest game window frame.
        '''
        with self.lock:
            if self.frame is None:
                return None
            return cv2.cvtColor(self.frame, cv2.COLOR_BGRA2BGR)

    def stop(self):
        '''
        Stop capturing thread
        '''
        if self.capture_control is not None:
            self.capture_control.stop()
        logger.info("[GameWindowCapturor] Terminated")

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
