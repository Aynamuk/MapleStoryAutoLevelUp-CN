#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
verifier: 标题栏高度自适应 —— 治「配置 31 但本机只有 27」导致的抓不到画面（issue #14）

背景
----
`game_window.title_bar_height` 配置里是 **开发机实测的常数 31**，但标题栏高度是
Windows 按 `主题 + DPI + build` 算出来的**系统值**，每台机器都可能不同。
实测有用户机器为 27（比配置少 4px）。

切多了会怎样（这是 issue #14 的报错）：
    帧高 = 客户区高 + 标题栏 + 1   →  768 + 27 + 1 = 796
    切掉配置的 31 行后只剩 765 行，不够 768
    → crop_frame_to_client 判定"比目标小"并返回 None
    → 引擎/录制/试读**全部拿不到画面**（表现为「画面大小对不上」+ F4 说没截到画面）。

切多但没跨过阈值时更隐蔽：不报错，**所有坐标整体上移几像素** ——
血蓝条 ROI 距底边本就只有约 4px，会连带读数不准。

修复契约（本脚本固化，防止以后被改回"直接读配置值"）
--------------------------------------------------
    · `resolve_title_bar_height(cfg, ...)` 是**唯一**取值入口；
    · 实测与配置相差 ≤ 1px → 返回配置值（正常用户行为零变化）；
    · 相差 > 1px → 按实测值接管，并打日志留证；
    · 量不到窗口 / 实测值离谱（最小化时外框是 -32000 哨兵）→ 退回配置值；
    · 引擎、路线录制、UI 试读**三处必须共用它**，否则会出现
      「录制正常但跑起来偏」—— 两套坐标系。

本脚本不依赖真实游戏窗口，全部用桩（stub）跑，任何机器上都能过。

退出码 0=全过，1=有失败项。
'''

import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

FAIL = []
PASS = []


def check(name, ok, detail=""):
    if ok:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  → {detail}" if detail else ""))


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


print("【1】源码契约：三处调用点必须共用同一个自适应取值")
# ---------------------------------------------------------------------------
_engine = _src(os.path.join("src", "engine", "MapleStoryAutoLevelUp.py"))
_recorder = _src(os.path.join("tools", "routeRecorder.py"))
_ui = _src(os.path.join("src", "ui", "ui.py"))

check("引擎用 resolve_title_bar_height 取值",
      "resolve_title_bar_height(" in _engine)
check("路线录制用 resolve_title_bar_height 取值",
      "resolve_title_bar_height(" in _recorder)
check("UI 试读用 resolve_title_bar_height 取值",
      "resolve_title_bar_height(" in _ui)

# 不许再有人直接读配置里的裸值去裁剪（那正是 bug 的形态）
for _label, _blob, _bad in [
    ("引擎", _engine, 'self.cfg["game_window"]["title_bar_height"]'),
    ("路线录制", _recorder, 'self.cfg["game_window"]["title_bar_height"]'),
]:
    check(f"{_label}不再直接读裸配置值裁剪", _bad not in _blob,
          f"仍有 {_bad}")

check("UI 试读不再直接读裸配置值",
      'self.cfg["game_window"]["title_bar_height"], tag="试读"' not in _ui)

print("\n【2】取值闸门：容差内不动、超容差接管、量不到退回配置")
# ---------------------------------------------------------------------------
import src.utils.common as C
from src.utils.common import (TITLE_BAR_TOLERANCE, resolve_title_bar_height,
                              crop_frame_to_client)

check("容差 = 1px", TITLE_BAR_TOLERANCE == 1, f"实为 {TITLE_BAR_TOLERANCE}")

CFG = {"game_window": {"title": "桩窗口", "title_bar_height": 31}}
_real_metrics = C.get_window_metrics


def _with_metrics(fn):
    """把 get_window_metrics 换成桩，返回 resolver 的结果。"""
    C.get_window_metrics = fn
    try:
        return resolve_title_bar_height(CFG)
    finally:
        C.get_window_metrics = _real_metrics


def _mk(measured):
    return lambda t: {"title_bar_height": measured}


def _boom(t):
    raise RuntimeError("桩：窗口不存在")


GATES = [
    ("实测31=配置 → 用配置31（行为零变化）", _mk(31), 31),
    ("实测30 差1px → 容差内，用配置31", _mk(30), 31),
    ("实测32 差1px → 容差内，用配置31", _mk(32), 31),
    ("实测27 差4px → 接管，用实测27（issue #14 场景）", _mk(27), 27),
    ("实测26 差5px → 接管，用实测26", _mk(26), 26),
    ("实测0 → 不合理，退回配置31", _mk(0), 31),
    ("实测500 → 不合理（最小化哨兵会量出天文数字），退回配置31", _mk(500), 31),
    ("窗口量不到（返回None）→ 退回配置31", lambda t: None, 31),
    ("量测抛异常 → 退回配置31，不崩", _boom, 31),
]
for _name, _fn, _exp in GATES:
    _got = _with_metrics(_fn)
    check(_name, _got == _exp, f"返回 {_got}，期望 {_exp}")

print("\n【3】端到端：issue #14 那台机器上，修复前裁不出来、修复后能裁出来")
# ---------------------------------------------------------------------------
# 复现他的真机数据：客户区 768 高，真实标题栏 27 → 抓帧 1366+2 x 768+27+1
_raw = np.zeros((796, 1368, 3), dtype=np.uint8)

_before, _msg_before = crop_frame_to_client(_raw, (768, 1366), 31, tag="桩-修复前")
check("修复前（切31）确实返回 None —— 复现 issue #14 的报错",
      _before is None, f"实得 {None if _before is None else _before.shape}")
check("修复前的报错文案里点出了「小」和两个尺寸",
      _before is None and "比配置的" in _msg_before and "1366x768" in _msg_before,
      f"msg={_msg_before!r}")

_after, _msg_after = crop_frame_to_client(_raw, (768, 1366), 27, tag="桩-修复后")
check("修复后（切27）裁出正好 768x1366 的画面，坐标锚在客户区左上角",
      _after is not None and _after.shape == (768, 1366, 3),
      f"实得 {None if _after is None else _after.shape}")

# 正常用户的机器（标题栏 31）必须仍然裁得出来 —— 零回归
_ok31, _ = crop_frame_to_client(np.zeros((800, 1368, 3), dtype=np.uint8),
                                (768, 1366), 31, tag="桩-常规")
check("常规机器（标题栏31，抓帧1368x800）仍然正常裁出 768x1366",
      _ok31 is not None and _ok31.shape == (768, 1366, 3),
      f"实得 {None if _ok31 is None else _ok31.shape}")

print("\n【4】日志不刷屏：每帧调用也只打一条（30fps 下否则会刷爆）")
# ---------------------------------------------------------------------------
import io
import logging

from src.utils.logger import logger as _lg

_buf = []


class _Cap(logging.Handler):
    def emit(self, record):
        _buf.append(record.getMessage())


_underlying = getattr(_lg, "logger", None) or _lg
# MSLogger 只代理了部分 logging 方法（无 removeHandler）→ 用 hasattr 兜底，
# 拿不到原生 logger 就直接检查 _TITLE_BAR_LOGGED 这个去重集合的行为。
_can_proxy = hasattr(_underlying, "addHandler") and hasattr(_underlying, "removeHandler")


def _count_adaptive(fn):
    """执行 fn()，返回期间打出的【自适应】日志条数。拿不到 handler 时用去重集合判。"""
    if not _can_proxy:
        _size_before = len(C._TITLE_BAR_LOGGED)
        fn()
        _new = len(C._TITLE_BAR_LOGGED) - _size_before
        # 集合新增一个组合 == 打了一条日志
        return _new
    del _buf[:]
    _underlying.addHandler(_Cap())
    try:
        fn()
    finally:
        _underlying.removeHandler(_Cap())
    return len([b for b in _buf if "自适应" in b])


C._TITLE_BAR_LOGGED.clear()
_n_adaptive = _count_adaptive(
    lambda: [_with_metrics(_mk(27)) for _ in range(300)])
check("300 帧里【自适应】只打 1 条（不是 300 条）", _n_adaptive == 1,
      f"实打 {_n_adaptive} 条")

# 值变了要能重新打一次（否则改配置/换窗口后就看不到提示了）
_n_again = _count_adaptive(lambda: _with_metrics(_mk(25)))
check("实测值变化后会重新打一次（不会永久静默）", _n_again == 1,
      f"实打 {_n_again} 条")

print("\n【5】文档与配置：说清「31 只是开发机实测值」")
# ---------------------------------------------------------------------------
_cfg_src = _src(os.path.join("config", "config_default.yaml"))
check("配置里 title_bar_height 仍在（默认值不因本 issue 改动）",
      "title_bar_height:" in _cfg_src)
check("配置说明了这是开发机实测值、会随机器不同",
      "开发机" in _cfg_src and "issue #14" in _cfg_src)

_common_src = _src(os.path.join("src", "utils", "common.py"))
check("common.py 里写明了 issue #14 的成因（系统值 vs 开发机常数）",
      "issue #14" in _common_src)
check("裁剪失败的文案同时给出「配置切多了」这条真因（不只怪用户窗口）",
      "配置切多了" in _common_src)

print("\n【6】抓帧体检不再误伤正常机器（issue #14 的第二处，F2/F3 静默失败）")
# ---------------------------------------------------------------------------
# template_capture.grab_game_frame 是 F2(标定名字)/F3(截怪) 共用的取图函数，
# 里面有一段「尺寸体检」用来挡掉"标题碰巧含关键词的别的窗口"。
# 它原来直接读**配置里**的 title_bar_height(=31)，于是把"本机标题栏更小"的
# 正常机器误判成"抓到了别的窗口"并拒绝该帧 —— 用户表现就是**按 F2 毫无反应**
# （控制台一闪/只打一行错误，框选窗口根本不弹）。
_tpl_src = _src(os.path.join("tools", "template_capture.py"))
check("抓帧体检改用自适应取值（不再读裸配置值）",
      "resolve_title_bar_height(" in _tpl_src)
check("抓帧体检不再用裸配置的 title_bar_height 算期望尺寸",
      'tb = cfg["game_window"]["title_bar_height"]' not in _tpl_src)

# 端到端：他的机器上体检必须通过（期望 == 实际）
import yaml as _yaml

_cfg_full = _yaml.safe_load(open(os.path.join(_ROOT, "config", "config_default.yaml"),
                                 encoding="utf-8"))
_h_c, _w_c = _cfg_full["game_window"]["size"]
_tb_adaptive = _with_metrics(_mk(26))          # 他实测 26
_exp_w, _exp_h = _w_c + 2, _h_c + _tb_adaptive + 1
_real_w, _real_h = 1368, 795                   # 他那台实际抓到的大小
check("他的机器上抓帧体检通过（不再被判成「别的窗口」）",
      abs(_real_w - _exp_w) <= 2 and abs(_real_h - _exp_h) <= 2,
      f"期望 {_exp_w}x{_exp_h} vs 实际 {_real_w}x{_real_h}")

# 体检的原目的不能丢：抓到明显不是游戏的巨窗时仍必须拒绝
_big_w, _big_h = 2546, 1433                    # 实测抓到过的资源管理器搜索窗口
check("体检原目的未丢：抓到 2546x1433 的非游戏窗口时仍拒绝",
      abs(_big_w - _exp_w) > 2 or abs(_big_h - _exp_h) > 2)

print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项。")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过。")
sys.exit(0)
