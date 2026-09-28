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


# ── 驱动可用性检查（2026-09-27 加）──────────────────────────────────────────
# 为什么需要它：Interception **内核驱动不是 pip 装包就有的**，要单独下载安装
#   并重启电脑。而项目原先**没有任何前置检查** —— 用户只有在引擎跑起来、
#   发现角色一动不动之后，才在日志里看到一句英文报错
#   （interception driver was not found or is not installed）。
#   实测（2026-09-27，issue #2）用户就是这样卡住的：他下载的是免安装包，
#   既不知道要装驱动，也没有 Python 去 pip install。
#
# ⇒ 提供一个**可以在启动前调用**的检查，让界面能明确告诉用户「缺什么、怎么装」，
#   而不是让他对着"角色不动"干猜。

DRIVER_DOWNLOAD_URL = "https://github.com/oblitum/Interception/releases/latest"


def driver_status():
    """检查 Interception 是否可用（不抛异常）。

    Returns:
        (ok: bool, detail: str)
            ok=True  —— 驱动可用，detail 是设备信息
            ok=False —— 不可用，detail 是**给人看的原因**
    """
    if interception is None:
        return False, ("interception-python 这个 Python 库没装上"
                       "（源码运行请 pip install -r requirements.txt）")
    try:
        init_interception()
        return True, (f"键盘设备={interception.get_keyboard()}，"
                      f"鼠标设备={interception.get_mouse()}")
    except Exception as e:                                   # noqa: BLE001
        msg = str(e)
        if "not found or is not installed" in msg or "DriverNotFound" in msg:
            return False, ("**Interception 内核驱动没有安装**（这是两回事："
                           "程序装了，驱动还要单独装一次，装完要重启电脑）")
        return False, f"Interception 初始化失败：{msg}"


def how_to_install_driver():
    """返回一段**可以直接展示给用户**的驱动安装指引（纯文本，供弹窗/日志用）。"""
    return (
        "这个工具用内核级驱动给游戏发按键，需要单独装一次驱动。\n"
        "\n"
        "装法（只做一次）：\n"
        f"　1. 打开 {DRIVER_DOWNLOAD_URL}\n"
        "　　 下载 Interception.zip 并解压；\n"
        "　2. 在解压出来的文件夹里，右键『以管理员身份运行』命令提示符；\n"
        "　3. 在里面执行：\n"
        "　　 install-interception.exe /install\n"
        "　4. 装完**重启电脑**，再打开本程序。\n"
        "\n"
        "确认装好没有：重新打开本程序，日志里不应再出现\n"
        "「Interception 初始化失败」。\n"
        "\n"
        "⚠️ 两点提醒：\n"
        "　· 必须重启电脑，不重启不生效；\n"
        "　· 极少数机器上装完可能出现键鼠异常。真出问题：开机时进\n"
        "　　 安全模式，把 C:\\Windows\\System32\\drivers\\ 下的\n"
        "　　 keyboard.sys / mouse.sys 删掉即可恢复（装之前先记下这条）。")


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


# ── 按键失败的可观测性（2026-09-27 加）─────────────────────────────────────
# 为什么需要：下面几个函数原来把异常一律记 `logger.debug`，而日志默认 INFO 级
#   ⇒ **按键送不出去时，日志里一条线索都没有**。
#   用户侧症状正是「引擎在发指令、角色纹丝不动」（issue #2 里 2026-09-27 那条：
#   截图显示 `指令=left none none`、位置 10 秒没变、`附近怪=0`），
#   而他能拿到的日志只有"卡住了"的提示，完全无法判断是权限、驱动还是别的问题。
#
# ⇒ 改成：**每类失败只大声报一次**（WARNING，带可能原因与怎么修），
#   之后降级为 debug —— 既留下排查线索，又不会每帧刷屏。
_KEY_FAIL_LOGGED: set = set()


def _report_key_failure(api, key, err):
    """按键调用失败时报告一次（同类只报一次，避免按 fps 刷屏）。"""
    tag = f"{api}:{type(err).__name__}"
    if tag in _KEY_FAIL_LOGGED:
        logger.debug(f"[Interception {api}] 失败: {key} - {err}")
        return
    _KEY_FAIL_LOGGED.add(tag)
    logger.warning(
        f"[Interception {api}] 按键失败（本类错误只报这一次）: key={key} - {err}\n"
        f"        ⚠️ 按键没能送进游戏 —— 角色会表现为「原地不动 / 一次都不打怪」。\n"
        f"        最常见原因（按顺序查）：\n"
        f"          ① 程序**没用管理员身份运行**（右键 exe → 以管理员身份运行）；\n"
        f"          ② **Interception 内核驱动没装**（光装本程序不够，装完要重启电脑）；\n"
        f"          ③ 游戏窗口不在前台（按键只会送到最前面的窗口）。")


#: ── 键名归一化（2026-09-28 加，issue #6 的真凶）─────────────────────────
#: 驱动只认它自己那套键名（见 interception._keycodes）。而**界面上的按键捕获**
#: 走的是 Qt 的 `QKeySequence.toString(NativeText)`，两者叫法不总是一致：
#:     界面（Qt）      驱动
#:     control    ->   ctrl      ← **issue #6 就是踩在这个上**
#:     pgdown     ->   pgdn
#:     ins        ->   insert
#: 后果极隐蔽：引擎照常算出 attack 指令、日志里也能看到 `指令(none none attack)`，
#: 但键**一次都没送进游戏** —— 用户看到的是「走位正常，完全不攻击」。
#: （实测：用户把基础攻击设成 Ctrl 键，界面上他看到的确实是 "Control"。）
#:
#: ⚠️ 表要**小而准**：这里只放"用户真会按、界面真会产出、而驱动不认"的键。
#:    实测（枚举 Qt.Key_* 全量 × 驱动键表）：常规键里只有下面这几种对不上，
#:    其余全是多媒体键/特殊符号（放大缩小、音量、货币符号…），不必管。
_KEY_ALIAS = {
    "control": "ctrl",
    "pgdown": "pgdn",
    "ins": "insert",
}


def normalize_key(key):
    """把界面上的键名转成驱动认识的名字（纯函数，可离线单测）。

    ⚠️ 为什么必须有这一层：界面的按键捕获产出 Qt 的叫法，驱动的键表是另一套。
       不转换的话，`control` 这种键会在**每一帧**都抛 UnknownKeyError，
       而失败上报是"每类只报一次"的 warning —— 用户基本看不到，
       症状就变成「角色走得好好的，但一次都不打怪」。
    """
    if not key:
        return key
    k = str(key).strip().lower()
    return _KEY_ALIAS.get(k, k)


def key_down(key):
    """按下按键不释放"""
    try:
        interception.key_down(normalize_key(key))
    except Exception as e:
        _report_key_failure("key_down", key, e)


def key_up(key):
    """释放按键"""
    try:
        interception.key_up(normalize_key(key))
    except Exception as e:
        _report_key_failure("key_up", key, e)


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
        _k = normalize_key(key)
        interception.key_down(_k)
        time.sleep(duration)
        interception.key_up(_k)
    except Exception as e:
        _report_key_failure("press_key", key, e)


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
