'''
Global Logger
'''
# Standard Import
import logging
import datetime
import os

# Local import
from src.utils.paths import describe, ensure_dir
from src.utils.console import make_console_safe

# 【注意】 必须在 MSLogger() 实例化之前调用：日志挂了 StreamHandler 往 stderr 写，
#    GBK 控制台遇到编不出的字符（emoji）会往 stderr 刷一堆 "--- Logging error ---" 回溯，
#    把日志刷废。详见 src/utils/console.py 的说明（2026-09-10 实测踩坑）。
make_console_safe()

class MSLogger:
    '''
    MapleStory AutoBot Logger
    '''
    def __init__(self, name="MSBot"):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.INFO)

        formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')

        # 日志目录同样走统一路径解析（打包后落在 exe 旁边，而非不确定的 CWD）
        log_dir = ensure_dir("log")
        file_handler = logging.FileHandler(
            os.path.join(log_dir, f"{name}_{now_str}.log"), mode='w', encoding="utf-8")
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        self._logger.addHandler(file_handler)
        self._logger.addHandler(console_handler)

        self._file_handler = file_handler
        self._console_handler = console_handler

        # 启动首行记录运行位置 —— 出问题时第一眼就能看出资源该放哪
        self._logger.info(f"[启动] {describe()}")
        self._logger.info(f"[启动] 日志文件: {file_handler.baseFilename}")

    def set_level(self, level):
        '''
        Set logger level, e.g. DEBUG, INFO, WARNING
        '''
        self._logger.setLevel(level)
        self._file_handler.setLevel(level)
        self._console_handler.setLevel(level)

    def info(self, msg):
        self._logger.info(msg)

    def warning(self, msg):
        self._logger.warning(msg)

    def error(self, msg):
        self._logger.error(msg)

    def debug(self, msg):
        self._logger.debug(msg)

    def addHandler(self, handle):
        self._logger.addHandler(handle)

# Initialize shared logger instance for global import
logger = MSLogger()


def jump_trace_on(cfg) -> bool:
    '''回正起跳埋点开关 `log.jump_trace`（纯函数 → 可离线单测）。

    为什么放在 logger.py：引擎线程和键盘线程都要读它，而两侧**都已经 import 了
    logger** —— 放这儿谁都不用再加一条 import，也不会把 numpy/cv2 拖进输入线程。

    为什么**默认开**：这是个排查用的临时埋点，漏记一次就得让用户再掉一次坑。
    配错（缺 `log` 段 / 缺 `jump_trace` 键 / 填了非布尔值）时宁可多打，别静默不记。

    ⚠️ 只影响"打不打日志"，**不影响任何判定逻辑** —— 关掉不会改变机器人行为。
    ⚠️ 不进 `home_params` / `_HOME_PARAM_FLOORS`（进了 FLOORS 就等于"想关关不掉"）。

    Args:
        cfg: 配置 dict。None / 不是 dict / 没有 log 段 → 一律返回 True。

    Returns:
        bool: True = 打埋点日志。
    '''
    try:
        return bool(cfg.get("log", {}).get("jump_trace", True))
    except (AttributeError, TypeError):
        return True


def perf_trace_on(cfg) -> bool:
    '''主循环性能埋点开关 `log.perf_trace`（纯函数 → 可离线单测）。

    套路与 `jump_trace_on` **完全一致**，理由也一致：
    - 放 logger.py 是因为各模块都已 import logger，不用再加 import；
    - **默认开**：排查期漏记一次就得让用户再跑一次；
    - 只影响"打不打日志"，**不影响任何判定逻辑**；
    - 不进 `home_params` / `_HOME_PARAM_FLOORS`（进了就"想关关不掉"）。

    Args:
        cfg: 配置 dict。None / 不是 dict / 没有 log 段 → 一律返回 True。

    Returns:
        bool: True = 打性能埋点日志。
    '''
    try:
        return bool(cfg.get("log", {}).get("perf_trace", True))
    except (AttributeError, TypeError):
        return True


def perf_summary_line(stats) -> str:
    '''把 5 秒窗口内主循环各阶段的耗时压成**一行**日志（纯函数，绝不抛异常）。

    治的是什么（2026-09-14 真机实测）：
        配置 `fps_limit_main: 30`，实测主循环只有 **1.8~2 fps** —— 而全项目
        **没有任何地方记录主循环的实际帧率**（键盘线程会打印 `fps=`，主循环不会），
        于是"一帧 500ms 到底花在哪"完全靠猜。这一行把各阶段耗时**按降序**摊开，
        最慢的排最前，一眼就能看出瓶颈。

    ⚠️ 为什么做成纯函数（跟 `jump_ready` / `home_summary_line` 一个道理）：
        它跑在**主循环**里 —— 在那儿抛异常 = 整个挂机停摆。做成纯函数后
        `tools/verify_perf_trace.py` 能离线把"字段残缺 / 除零 / 类型不对"全钉死，
        不必靠真机去试。

    Args:
        stats: dict，**允许缺任何键**（缺了用占位符或省略该段，绝不抛）
            fps     实际帧率（帧/秒）
            n       本窗口帧数
            viz     bool，是否开着可视化（GUI 的「游戏画面」页签）
            gap_ms  平均帧间隔（毫秒，含限帧 sleep）
            items   [(阶段名, 平均耗时ms, 单帧峰值ms), ...]（顺序无所谓，内部会排序）
                    ⚠️ 平均耗时 <1ms 的阶段会被省略（防噪声刷屏）

    Returns:
        str: 一行日志文本，恒定以 "[性能] " 开头。
    '''
    try:
        d = dict(stats or {})
    except (TypeError, ValueError):
        d = {}

    def _f(k, default=0.0):
        try:
            v = d.get(k, default)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    parts = [f"[性能] 近5秒 fps={_f('fps'):.1f}（{int(_f('n'))}帧）"
             f" 可视化={'开' if d.get('viz') else '关'}",
             f"一帧 {_f('gap_ms'):.0f}ms"]

    rows = []
    try:
        items = list(d.get("items") or [])
    except (TypeError, ValueError):
        items = []
    for it in items:
        try:
            name, avg, peak = str(it[0]), float(it[1]), float(it[2])
        except (TypeError, ValueError, IndexError):
            continue
        if avg < 1.0:
            continue                      # <1ms 省略，防噪声
        rows.append((name, avg, peak))
    rows.sort(key=lambda r: -r[1])        # ★ 最慢的排最前 → 一眼看瓶颈

    if rows:
        parts.append(" ".join(f"{n}{a:.0f}(峰{p:.0f})" for n, a, p in rows))
    else:
        parts.append("各阶段都 <1ms")
    return " | ".join(parts)
