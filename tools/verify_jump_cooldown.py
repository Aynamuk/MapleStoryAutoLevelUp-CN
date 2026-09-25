# -*- coding: utf-8 -*-
'''
离线自检：跳跃冷却 `key.jump_cooldown`（2026-09-14 补用例）

治的是什么（2026-09-13 用户实测）：
    角色只要站在一片 jump 像素上，键盘线程就是**每帧**走到 run() 的 jump 分支
    按一次跳 —— 30fps 下每秒按 30 次 = **连跳**，表现是「跳上平台了还在跳，
    结果又跳出去了」。所以按过一次必须挡一段时间（默认 1.0s ≈ 一次完整的
    起跳+滞空+落地）。

为什么现在才有用例：
    这段判断原来埋在 `KeyBoardController.run()` 的循环里，要验它就得把整个键盘
    线程跑起来（要 Interception 驱动、要游戏窗口、要抢前台焦点）—— 结果就是
    「只有实测验证、没有用例」，改坏了没人拦。现在判断已抽成模块级纯函数
    `jump_ready()`（不碰 self、不读配置），这条链路终于能离线钉死。

⚠️ 纪律：本脚本测的是 **src/input/KeyBoardController.py 里那个真的 jump_ready**
   —— 不另写一份假逻辑，否则"测的和跑的不是同一份代码"，全绿也保证不了线上不出事。

不开游戏、不抢焦点、不初始化驱动（jump_ready 是纯函数；import 模块只做函数定义）。
唯一的真实耗时是【5】那一节（真等 1 秒，就是为了复现"1 秒按 30 次"这个场景）。

用法：
    PYTHONPATH=<项目根> python tools/verify_jump_cooldown.py
'''
import inspect
import sys
import time

import numpy as np

from src.utils.common import load_yaml
from src.input.KeyBoardController import (
    KeyBoardController, jump_ready, DEFAULT_JUMP_COOLDOWN,
)

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


print("【1】★ 第一次**必发**（t_last_jump 初值 0.0 不能把第一次吞掉）")
# 这条在防什么：`t_last_jump` 初值是 0.0（1970 年）。要是写成
#   `if self.t_last_jump and (now - self.t_last_jump) >= cd`
# 这类"先判有没有值"的写法，第一次跳就被吞掉 —— 表现是「加了冷却之后角色
# 压根不跳了」，比连跳还难查（连跳至少看得见在跳）。
check("t_last_jump=0.0（初值）→ 发", jump_ready(0.0, now=1000.0, cooldown=1.0), True)
check("t_last_jump=0.0 + 真实时钟 → 发", jump_ready(0.0, cooldown=1.0), True)
check("t_last_jump=None（认不出）→ 宁可发，也不把跳跃卡死",
      jump_ready(None, now=1000.0, cooldown=1.0), True)
check("t_last_jump 是字符串（脏数据）→ 不抛异常，仍然发",
      jump_ready("0.0", now=1000.0, cooldown=1.0), True)

print("\n【2】冷却窗口内挡住、冷却到了放行（cd=1.0）")
# 这条在防什么：连跳本体。刚按过就再按 = 一秒三十次。
check("刚发过 0.0s → 不发", jump_ready(1000.0, now=1000.0, cooldown=1.0), False)
check("刚发过 0.5s → 不发", jump_ready(1000.0, now=1000.5, cooldown=1.0), False)
check("刚发过 0.99s → 不发", jump_ready(1000.0, now=1000.99, cooldown=1.0), False)
check("刚好 1.0s → 发", jump_ready(1000.0, now=1001.0, cooldown=1.0), True)
check("超过 1.0s（1.01s）→ 发", jump_ready(1000.0, now=1001.01, cooldown=1.0), True)
check("now 省略 → 取 time.time()（与显式传值同口径）",
      jump_ready(time.time(), cooldown=1.0), False)

print("\n【3】★ 边界：now - t_last **等于** cd → 发（是 >= 不是 >）")
# 这条在防什么：有人"顺手"把 >= 改成 >。差别只有一帧，但冷却窗口边界上的那帧
# 会被多挡一次 —— 单次看不出来，长跑时表现成"偶尔漏一次跳"，极难复现。
check("差值 == cd → 发（>=）", jump_ready(1000.0, now=1001.0, cooldown=1.0), True)
check("差值 == cd - 1e-9 → 不发（差一点点也不行）",
      jump_ready(1000.0, now=1001.0 - 1e-9, cooldown=1.0), False)
check("cd=0.5 / 差值 0.5 → 发", jump_ready(1000.0, now=1000.5, cooldown=0.5), True)
check("cd=2.0 / 差值 1.99 → 不发", jump_ready(1000.0, now=1001.99, cooldown=2.0), False)

print("\n【4】开关能关掉：cd <= 0 → 每次都发（退回老行为）")
# 这条在防什么：配置项要真的**可关**。老版本的同一位置是没有冷却的，
# 用户觉得冷却碍事（比如要连续跳台阶）时必须能关回去，否则就是"改了回不去"。
check("cd=0 → 发", jump_ready(1000.0, now=1000.0, cooldown=0), True)
check("cd=0.0（浮点零）→ 发", jump_ready(1000.0, now=1000.0, cooldown=0.0), True)
check("cd=-1（负数）→ 发（等价于关）", jump_ready(1000.0, now=1000.0, cooldown=-1), True)
check("cd=-0.001 → 发", jump_ready(1000.0, now=1000.0, cooldown=-0.001), True)
check("cd 是字符串 '0'（配置里可能这么写）→ 也能关掉",
      jump_ready(1000.0, now=1000.0, cooldown="0"), True)

print("\n【5】cd 传非法值 → 不抛异常，回落默认 0.8 正常判")
# 这条在防什么（跟 run() 里读配置那段 try/except 是同一条纪律）：
#   配置里填 "abc" / 留空时，抛异常会让**键盘线程整个挂掉**（角色再也不动了，
#   而且只崩一个后台线程，界面上一点提示都没有）—— 而"冷却失效"只是回到老行为。
#   宁可冷却失效，也不能整条输入链死掉。
check("内置兜底值 DEFAULT_JUMP_COOLDOWN = 0.8", DEFAULT_JUMP_COOLDOWN, 0.8)
check("cd=None → 不抛异常", jump_ready(1000.0, now=1000.5, cooldown=None), False)
check("cd=None → 按 0.8 判：0.9s 后放行",
      jump_ready(1000.0, now=1000.9, cooldown=None), True)
check("cd='abc' → 不抛异常，按 0.8 判：0.5s 挡住",
      jump_ready(1000.0, now=1000.5, cooldown="abc"), False)
# ⚠️ 为什么写 0.85 而不是 0.8：1000.8 - 1000.0 在二进制浮点里是
#    0.7999999999999545 < 0.8 → 不放行。这是**十进制小数的表示误差**，
#    不是 jump_ready 的 bug（边界本身在【3】里用能精确表示的 1000.0/1001.0/1.0 测）。
#    照着"差值正好等于 cd"写十进制边界，只会得到一个随机红的用例。
check("cd='abc' → 按 0.8 判：0.85s 放行（>0.8）",
      jump_ready(1000.0, now=1000.85, cooldown="abc"), True)
check("（浮点边界自证）1000.8 - 1000.0 < 0.8 —— 所以别拿十进制小数测边界",
      (1000.8 - 1000.0) < 0.8, True)
check("cd=''（空串）→ 不抛异常，按 0.8 判",
      jump_ready(1000.0, now=1000.5, cooldown=""), False)
check("cd 缺省（用默认 0.8）→ 0.5s 挡住", jump_ready(1000.0, now=1000.5), False)
check("cd='1.5'（合法字符串数字）→ 真的按 1.5 判，不是抛掉",
      (jump_ready(1000.0, now=1000.9, cooldown="1.5"),
       jump_ready(1000.0, now=1001.5, cooldown="1.5")), (False, True))
check("now=None + cd=None 同时非法 → 仍不抛异常（返回 bool）",
      isinstance(jump_ready(0.0, now=None, cooldown=None), bool), True)

print("\n【6】★ 连跑 30 帧、真等 1 秒：cd=1.0 时**只发 1 次**")
# 这条在防什么：**用户实测那个 bug 本身** —— 30fps 下 1 秒按 30 次跳。
# 用真实 time.sleep 走完整 1 秒，不用假时钟：假时钟测的是"我的算术"，
# 真时钟测的是"这条链路在真实帧率下到底发几次"。
# ⚠️ 用**绝对时刻**排帧（t0 + i/30），不累积 sleep 误差 ——
#    否则 Windows 上 sleep 每次多睡 1~2ms，30 帧累积下来最后一帧会越过 1.0s
#    边界，用例变成随机红（这类 flaky 用例比没有用例更糟）。
FPS, CD, FRAMES = 30, 1.0, 30
_t0 = time.time()
_t_last = 0.0
_sent = 0
for _i in range(FRAMES):
    _now = time.time()
    if jump_ready(_t_last, now=_now, cooldown=CD):
        _sent += 1
        _t_last = _now
    _target = _t0 + (_i + 1) / float(FPS)
    _rest = _target - time.time()
    if _rest > 0:
        time.sleep(_rest)
_elapsed = time.time() - _t0
print(f"       真等了 {_elapsed:.3f}s，{FRAMES} 帧里发了 {_sent} 次跳"
      f"（没有冷却时应该是 {FRAMES} 次）")
check("★ 1 秒 30 帧只发 1 次（连跳已挡住）", _sent, 1)
check("确实等够了约 1 秒（不是秒过，测了个寂寞）", _elapsed >= 0.9, True)
# 冷却到期后必须**重新放行**（不能一挡到底，否则角色这辈子只跳一次）
time.sleep(max(0.0, _t0 + 1.08 - time.time()))
check("★ 过了 1.0s → 冷却解除，可以再跳（不是一挡到底）",
      jump_ready(_t_last, cooldown=CD), True)
# 对照组：把冷却关掉，同样 30 帧会发多少次（证明"只发 1 次"是冷却的功劳）
_t_last, _sent_off = 0.0, 0
for _i in range(FRAMES):
    _now = time.time()
    if jump_ready(_t_last, now=_now, cooldown=0):
        _sent_off += 1
        _t_last = _now
    time.sleep(1.0 / (FPS * 3))
check("（对照）cd=0 时同样 30 帧**每次都发** = 30 次（这正是连跳）",
      _sent_off, FRAMES)

print("\n【7】★ 抽出来的函数**真的被 run() 接上了**（防『抽了没接线』）")
# 这条在防什么：把判断抽成纯函数之后，如果 run() 里忘了改成调用它
# （或者改了但参数传错），用例全绿而线上**根本没有冷却** —— 属于最难查的一类。
_src_run = inspect.getsource(KeyBoardController.run)
check("run() 里确实在调 jump_ready（不是留着旧的内联判断）",
      "jump_ready(" in _src_run, True)
check("run() 里已**没有**旧的内联冷却判断",
      "if _jump_cd <= 0 or" in _src_run, False)
check("run() 发完跳仍然会更新 t_last_jump（否则冷却永远不生效）",
      "self.t_last_jump = time.time()" in _src_run, True)
check("jump_ready 是模块级函数（可离线 import，不依赖实例）",
      inspect.isfunction(jump_ready), True)
_sig = inspect.signature(jump_ready)
check("签名 = (t_last_jump, now=None, cooldown=0.8)",
      (list(_sig.parameters),
       _sig.parameters["now"].default,
       _sig.parameters["cooldown"].default),
      (["t_last_jump", "now", "cooldown"], None, 0.8))

print("\n【8】★ 真实配置：key.jump_cooldown 确实是 1.0（防以后有人手抖改掉）")
# 这条在防什么：默认值是**实测调出来的**（一次普通跳 + 滞空 + 落地约 0.7~0.9s，
# 1.0 才盖得住整个滞空）。有人顺手改成 0.1 / 删掉这个键，连跳就原样回来，
# 而线上表现是"偶发跳出去"，没人会想到是配置被改了。
_cfg = load_yaml("config/config_default.yaml")
_cd_cfg = (_cfg.get("key") or {}).get("jump_cooldown")
print(f"       config_default.yaml: key.jump_cooldown = {_cd_cfg!r}")
check("默认值是 1.0（不是 0.8 —— 0.8 只是**非法值兜底**）", _cd_cfg, 1.0)
check("1.0 真的能挡住连跳（0.5s 时挡住）",
      jump_ready(1000.0, now=1000.5, cooldown=_cd_cfg), False)
check("1.0 不会把跳跃卡死（1.0s 时放行）",
      jump_ready(1000.0, now=1001.0, cooldown=_cd_cfg), True)
# 一次普通跳的滞空（实测 0.7~0.9s）必须被完整盖住，否则会在空中补第二跳
check("★ 冷却 1.0s ≥ 一次完整起跳+滞空（0.9s）→ 空中不会补第二跳",
      _cd_cfg >= 0.9, True)

print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
