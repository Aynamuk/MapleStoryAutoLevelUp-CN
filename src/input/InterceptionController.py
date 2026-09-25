'''
InterceptionController
Interception 驱动输入适配器（内核级键盘/鼠标模拟）
基于 interception-python（https://pypi.org/project/interception-python/）

前置条件：
1. 必须先安装 Interception 驱动（https://github.com/oblitum/Interception），
   运行驱动包里的 command line installer/install.bat，装完需要重启电脑。
2. 未安装驱动时调用任何输入函数会抛 DriverNotFoundError。
'''
import time
import random

from src.utils.logger import logger

try:
    import interception
except ImportError:
    interception = None
    logger.error("[Interception] interception-python 未安装，请运行: pip install interception-python")

# 驱动是否已成功初始化
_interception_initialized = False

# 按键保持时长范围（毫秒）- 模拟人类行为
KEY_HOLD_MIN = 40
KEY_HOLD_MAX = 90


def init_interception():
    """初始化 Interception 驱动并自动识别键盘设备"""
    global _interception_initialized

    if _interception_initialized:
        return True

    if interception is None:
        raise RuntimeError("interception-python 未安装")

    # 自动探测键盘/鼠标设备编号（读取设备 HWID，无需用户配合按键）
    # 注意：mouse 必须设为 True，否则库的探测循环在找到第一个键盘设备后
    #       会立刻 break，可能选中虚拟键盘设备而非真实物理键盘。
    interception.auto_capture_devices(keyboard=True, mouse=True)
    _interception_initialized = True
    logger.info("[Interception] 驱动已初始化，"
                f"键盘设备={interception.get_keyboard()}, 鼠标设备={interception.get_mouse()}")
    return True


def key_down(key):
    """按下按键不释放"""
    try:
        interception.key_down(key.lower())
    except Exception as e:
        logger.debug(f"[Interception key_down] 失败: {key} - {e}")


def key_up(key):
    """释放按键"""
    try:
        interception.key_up(key.lower())
    except Exception as e:
        logger.debug(f"[Interception key_up] 失败: {key} - {e}")


def press_key(key, duration=None):
    """
    模拟按键按下并释放
    duration: 保持时长（秒），None 则随机（模拟人类行为）
    """
    if not key:
        return

    if duration is None:
        duration = random.randint(KEY_HOLD_MIN, KEY_HOLD_MAX) / 1000.0

    try:
        interception.key_down(key.lower())
        time.sleep(duration)
        interception.key_up(key.lower())
    except Exception as e:
        logger.debug(f"[Interception press_key] 失败: {key} - {e}")


# ---------------------------------------------------------------------------
# 鼠标（同样走 Interception 内核驱动）
#
# 为什么不用 pyautogui：pyautogui 走用户态 SendInput，注入痕迹与内核驱动不同。
# 本项目的输入链路统一定位在 Interception，鼠标也必须一致，否则是一个明显短板。
# ---------------------------------------------------------------------------

def _ensure_mouse_ready():
    """鼠标事件前确保驱动已初始化（幂等）"""
    if not _interception_initialized:
        init_interception()


def mouse_move_to(x, y, duration=None):
    """
    把鼠标移动到屏幕绝对坐标 (x, y)。

    duration: 保留参数，用于将来接入人类化曲线移动；当前由库内部控制速度。
    """
    _ensure_mouse_ready()
    if duration is not None:
        logger.debug("[Interception mouse_move_to] duration 参数当前未使用，忽略")
    interception.move_to(int(x), int(y))


def move_to_and_settle(x, y, timeout=1.0, tolerance=2, poll=0.02):
    """
    把鼠标移到目标点，并**等待光标真正到位**后再返回。

    为什么需要这一步（2026-09-10 本机实测，2560x1440 单显示器）：
        Interception 的绝对移动是有效的，但光标到位存在延迟——距离越大越明显。
        实测连续移动到 (400,400)：第 1 次读回 (197,413)，第 3 次才精确到 (400,399)。
        若移动后立刻按下鼠标，就会点偏。本函数轮询确认位置，避免盲点。

    返回 True 表示已在容差内到位；False 表示超时（调用方应放弃点击，不要盲点）。
    """
    _ensure_mouse_ready()
    target_x, target_y = int(x), int(y)
    t0 = time.time()

    while True:
        interception.move_to(target_x, target_y)
        time.sleep(poll)
        cur_x, cur_y = interception.mouse_position()
        if abs(cur_x - target_x) <= tolerance and abs(cur_y - target_y) <= tolerance:
            return True
        if time.time() - t0 >= timeout:
            logger.warning(f"[move_to_and_settle] {timeout}s 内未到位: "
                           f"目标 {(target_x, target_y)}，实际 {(cur_x, cur_y)}")
            return False


def click_at(x, y, button="left", clicks=1, delay=0.05, settle_timeout=1.0):
    """
    在屏幕绝对坐标 (x, y) 点击。

    - button: "left" / "right" / "middle" / "mouse4" / "mouse5"
    - clicks: 点击次数
    - delay : 到位后、按下前的短暂停顿（秒），给游戏 UI 响应时间
    - settle_timeout: 等待光标到位的最长时间（秒）

    安全策略：光标未在超时内到位时**不点击**，只记错误并返回 False。
    在游戏里点错位置（比如误点「换频道」「退出」）比不点更糟。
    """
    if x is None or y is None:
        logger.warning("[Interception click_at] 坐标为 None，跳过点击")
        return False
    if button not in ("left", "right", "middle", "mouse4", "mouse5"):
        logger.warning(f"[Interception click_at] 不支持的按键: {button}，回退为 left")
        button = "left"

    if not move_to_and_settle(x, y, timeout=settle_timeout):
        logger.error(f"[Interception click_at] 光标未到位，放弃点击 {(x, y)}")
        return False

    time.sleep(delay)
    for i in range(int(clicks)):
        interception.mouse_down(button)
        interception.mouse_up(button)
        if i < clicks - 1:
            time.sleep(0.1)
    return True


def mouse_position():
    """返回当前鼠标的屏幕绝对坐标 (x, y)"""
    _ensure_mouse_ready()
    return interception.mouse_position()
