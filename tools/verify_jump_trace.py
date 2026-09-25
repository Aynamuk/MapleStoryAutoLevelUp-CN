# -*- coding: utf-8 -*-
'''
离线自检：回正起跳埋点（2026-09-14 加）

治的是什么：
    角色掉坑回不来时，日志里只有「进入回正 / 退出回正」两条，中间发生了什么全靠猜
    —— 到底是

        (A) 压根没走到起跳点   → 引擎从来没产出 jump 指令
        (B) 走到了，但被吞了   → 产出了 jump，键盘线程的 jump_cooldown=1.0s 把它挡了
        (C) 键按下去了但没上去 → 真发了键，角色仍然摔回坑底

    三种完全不同的病，在旧日志里长得一模一样。这次补的三处埋点 + 一行小结，
    就是为了把这三者分开：看小结里的「走到起跳点 N 次」和键盘侧的
    「真的按下了跳跃键」两条，就能定位。

为什么必须离线测（不能只靠"让用户去掉一次坑"）：
    埋点跑在两个要命的位置 ——
      ① 引擎 `_exit_home_route`：回正的**唯一出口**，在那儿抛异常 = 回正退不出去
         = 机器人卡死；
      ② 键盘线程的 jump 分支：30fps 每帧都走，不节流就把日志刷废，
         真正有用的「进入 / 退出回正」反而看不见。
    这两种坏法都没法靠真机试出来（试一次就得掉一次坑）。所以文案生成与节流判断
    都抽成了**纯函数**，在这里离线钉死。

⚠️ 纪律：本脚本测的是 **src/ 里真的 home_summary_line / jump_blocked_msg /
   jump_trace_on / jump_ready** —— 不另写一份假逻辑，否则"测的和跑的不是同一份
   代码"，全绿也保证不了线上不出事。

不开游戏、不抢焦点、不初始化驱动。

用法：
    PYTHONPATH=<项目根> python tools/verify_jump_trace.py
'''
import inspect
import sys
import time

from src.utils.common import load_yaml
from src.utils.home_route import home_summary_line, home_params
from src.utils.logger import jump_trace_on
from src.input.KeyBoardController import (
    KeyBoardController, jump_blocked_msg, jump_ready,
    JUMP_BLOCKED_REPORT_MIN, JUMP_BLOCKED_REPORT_EVERY,
)

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


# ══════════════════════════════════════════════════════════════════════
print("\n【1】home_summary_line：字段齐全时，关键信息一个都不能少")
_s = {
    "result": "超时退出", "reason": "回正超过 8s 还没回到主线",
    "dur_s": 3.2, "frames": 96,
    "start": (98, 126), "end": (98, 126),
    "jump_n": 2, "jump_gap": 1.05,
    "y_min": 124, "y_max": 126,
    "d_home": 0, "d_main": 4, "mm_score": 0.91,
}
_line = home_summary_line(_s)
print(f"       {_line}")
check("以 [回正·小结] 开头", _line.startswith("[回正·小结]"), True)
check("带结果", "超时退出" in _line, True)
check("带耗时 / 帧数", "3.2s/96帧" in _line, True)
check("带起点→终点", "(98,126)→(98,126)" in _line, True)
check("带走到起跳点次数", "走到起跳点 2 次" in _line, True)
check("带相邻两次的间隔", "间隔 1.05s" in _line, True)
check("带期间 y 的区间", "期间 y 124~126" in _line, True)
check("带退出时两个距离", "离回正线 0px 离主线 4px" in _line, True)
check("带小地图差异（★ 必须是「差异」不是「匹配度」：TM_SQDIFF_NORMED 越小越好）",
      "小地图差异 0.9100" in _line, True)
check("★ 文案里明说了「越小越好」（写反会把排查带去错误方向）",
      "越小越好" in _line, True)
check("带原因", "回正超过 8s" in _line, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【2】★ 字段残缺 / 类型不对 → 绝不抛异常（它跑在回正的**唯一出口**上）")
# 这条在防什么：_exit_home_route 是回正唯一出口，埋点在那儿抛异常 = 回正退不出去
# = 机器人卡在坑底。所以宁可少打几个字段，也不能抛。
for _bad in [{}, None, "乱七八糟", {"jump_n": "abc"},
             {"start": None, "end": 123},
             {"y_min": "x", "y_max": None, "mm_score": "y"},
             {"dur_s": None, "frames": "zzz"}]:
    try:
        _l = home_summary_line(_bad)
        _ok = isinstance(_l, str) and _l.startswith("[回正·小结]")
    except Exception as e:                                  # noqa: BLE001
        _ok = False
        print(f"       !! 抛异常了：{e!r}")
    check(f"残缺输入 {str(_bad)[:26]} 也能出一行", _ok, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【3】★ 三种病因在小结里长得不一样（这就是这次埋点的全部意义）")
_a = home_summary_line({"result": "超时退出", "jump_n": 0})
print(f"       A：{_a}")
check("A 没走到起跳点 → 有明确提示（一眼看出是 A）",
      "一次都没走到起跳点" in _a, True)
_b = home_summary_line({"result": "超时退出", "jump_n": 3, "jump_gap": 1.02})
print(f"       B：{_b}")
check("B 走到起跳点 3 次 → 计数正确（配合键盘侧日志判定 B）",
      "走到起跳点 3 次" in _b, True)
_c = home_summary_line({"result": "超时退出", "jump_n": 2,
                        "y_min": 126, "y_max": 126})
print(f"       C：{_c}")
check("C 键按下去了却没上去 → y 区间仍然贴着坑底 126",
      "期间 y 126~126" in _c, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【4】jump_blocked_msg 节流（30fps 下连挡 1 秒 = 30 帧，不能全打）")
_t0 = 1000.0
_m0, _nt0 = jump_blocked_msg(1, 0.03, 1.0, 0.0, now=_t0)
check("才挡 1 次 → 不报（刚跳完那几次是正常现象）", _m0, None)
_m1, _nt1 = jump_blocked_msg(JUMP_BLOCKED_REPORT_MIN, 0.17, 1.0, 0.0, now=_t0)
check(f"连挡 {JUMP_BLOCKED_REPORT_MIN} 次 → 开始报", _m1 is not None, True)
check("文案里带连挡次数", f"已连挡 {JUMP_BLOCKED_REPORT_MIN} 次" in _m1, True)
check("文案里带冷却秒数（让人看懂还差多久）", "冷却 1.0s" in _m1, True)
check("返回的 last_report_t 已更新成 now", _nt1, _t0)
_m2, _nt2 = jump_blocked_msg(20, 0.5, 1.0, _nt1, now=_t0 + 0.5)
check("同一秒内不重复报", _m2, None)
check("不报时 last_report_t 保持不变（否则节流失效）", _nt2, _nt1)
_m3, _nt3 = jump_blocked_msg(23, 0.8, 1.0, _nt1, now=_t0 + 1.2)
check(f"过了 {JUMP_BLOCKED_REPORT_EVERY}s 可以再报", _m3 is not None, True)
check("★ 再报时带的是**最新累计值**（不是上次那个旧数字）",
      "已连挡 23 次" in _m3, True)
_m4, _ = jump_blocked_msg(30, 2.0, 1.0, 0.0, now=_t0)
check("连挡 ≥20 次 → 提示升级成「起跳一直没成功」",
      "起跳一直没成功" in _m4, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【5】jump_trace_on 开关（只影响打不打日志，不影响任何判定）")
check("log.jump_trace: true → 开",
      jump_trace_on({"log": {"jump_trace": True}}), True)
check("★ 显式 false 能关掉（没被 FLOORS 锁死 → 随时可关）",
      jump_trace_on({"log": {"jump_trace": False}}), False)
check("缺 log 段 → 默认开", jump_trace_on({"route": {}}), True)
check("缺 jump_trace 键 → 默认开", jump_trace_on({"log": {}}), True)
check("cfg 是 None → 默认开（宁可多打，别漏记）", jump_trace_on(None), True)
check("cfg 传错类型（字符串）→ 默认开", jump_trace_on("坏了"), True)


# ══════════════════════════════════════════════════════════════════════
print("\n【6】★ 真实配置：log.jump_trace 确实是 true（排查期必须开着）")
_cfg = load_yaml("config/config_default.yaml")
_jt = (_cfg.get("log") or {}).get("jump_trace")
print(f"       config_default.yaml: log.jump_trace = {_jt!r}")
check("默认值是 true", _jt, True)
check("★ 它**没有**被塞进 home_params / FLOORS（进了就关不掉）",
      "jump_trace" in home_params(_cfg or {}), False)


# ══════════════════════════════════════════════════════════════════════
print("\n【7】★ 埋点真的接上了（防「抽了函数却没接线」——全绿而线上没日志）")
_src_run = inspect.getsource(KeyBoardController.run)
check("键盘：真按下跳跃键会打日志",
      "[按键·跳跃] 真的按下了跳跃键" in _src_run, True)
check("键盘：被冷却挡住会累加计数", "self.n_jump_blocked += 1" in _src_run, True)
check("键盘：被挡的日志走节流函数（不是每帧硬打）",
      "jump_blocked_msg(" in _src_run, True)
check("★ 冷却判定仍然走 jump_ready（没被埋点改坏）",
      "jump_ready(" in _src_run, True)
check("★ t_last_jump 仍然在发键后更新（冷却不能失效）",
      "self.t_last_jump = time.time()" in _src_run, True)

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot   # noqa: E402
_src_enter = inspect.getsource(MapleStoryAutoBot._enter_home_route)
check("引擎：进入回正会清掉上一轮计数（否则数字是好几轮加起来的假数）",
      "self.home_jump_n = 0" in _src_enter, True)
_src_exit = inspect.getsource(MapleStoryAutoBot._exit_home_route)
check("引擎：退出回正会打小结", "home_summary_line(" in _src_exit, True)
check("★ 快照在清空 home_* **之前**抓（否则抓到的全是 None）",
      _src_exit.index("_snap = {") < _src_exit.index("self.home_line = None"),
      True)
_src_trace = inspect.getsource(MapleStoryAutoBot._trace_home_cmd)
check("引擎：起跳埋点只记**上升沿**（不是每帧，否则刷屏）",
      "on and not was_on" in _src_trace, True)
check("引擎：埋点受开关控制", "jump_trace_on(self.cfg)" in _src_trace, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【8】★ 埋点没有改变冷却行为（回归：「纯观测」的底线）")
check("冷却 1.0s：0.5s 时仍然挡住",
      jump_ready(1000.0, now=1000.5, cooldown=1.0), False)
check("冷却 1.0s：1.0s 时仍然放行",
      jump_ready(1000.0, now=1001.0, cooldown=1.0), True)
check("第一次必发（t_last_jump 初值 0.0 → 巨大的时间差）",
      jump_ready(0.0, now=time.time(), cooldown=1.0), True)


# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
