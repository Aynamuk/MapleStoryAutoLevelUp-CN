# -*- coding: utf-8 -*-
"""
钉住「贴身跳跃优先」这条新判据（2026-09-27 加）。

## 为什么需要它

issue 现场：用户录了一条「中间需要跳到梯子上」的路线，挂机时**完全不跳**。
排查发现是**判据咬合问题**，不是录制问题：

  用户的 route1.png（废都南方工地）原始像素：
    梯子灰线 x=80，从 y=108 往下延伸（竖线）
    跳跃圆点 (87~91, 106~110)
  ⇒ 角色从左边走过来，x=79~86 期间**梯子像素始终更近**（d=0~3 vs 跳跃 d=2~9）
    → 引擎一路发 `none up none`（按上爬），可梯子入口偏高，爬不上去；
  ⇒ 到 x=87 终于轮到跳跃，但那条跳跃像素是 `right none jump`（录制时的副产品），
    而梯子在**左边** → 向右跳 = 离梯子越来越远。

本用例钉两件事（缺一就会退回原症状）：
  ① 两者都贴身（≤2px）且最近的是跳跃 → **必须选跳跃**，不能被梯子抢走；
  ② 跳跃的**左右方向必须按梯子方位重算**，不能照抄色码里的方向
     （照抄就会往反方向跳）。

## 用法
  python tools/verify_ladder_jump_prefer.py
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  [OK ] {name}: {got!r}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


# ── 复刻引擎里的那层判据（与 MapleStoryAutoLevelUp.update_cmd_by_route 一致）──
_PREFER_JUMP_RADIUS = 2


def decide(color_code, color_code_up_down, player_xy, is_on_ladder):
    """把引擎那段互补逻辑抽出来，输入两个候选码 + 角色位置，输出最终指令。

    这里**必须与源码头保持一致**：源码改了而这里没改，用例就变成自说自话。
    所以下面 _check_source_alignment() 会去源码里核对关键片段。
    """
    if not color_code and not color_code_up_down:
        return None
    if color_code and color_code_up_down:
        cc_cmd = str(color_code.get("command", ""))
        is_jump = "jump" in cc_cmd
        both_tight = (color_code["distance"] <= _PREFER_JUMP_RADIUS
                      and color_code_up_down["distance"] <= _PREFER_JUMP_RADIUS)
        if is_jump and both_tight:
            mx, my, ma = cc_cmd.split()
            up_px = color_code_up_down.get("pixel")
            if up_px is not None:
                dx = int(up_px[0]) - int(player_xy[0])
                mx = "left" if dx <= -2 else ("right" if dx >= 2 else "none")
            ud = str(color_code_up_down.get("command", "")).split()
            if len(ud) == 3 and my == "none" and is_on_ladder:
                my = ud[1]
            return (mx, my, ma)
        if color_code["distance"] < color_code_up_down["distance"]:
            mx, my, ma = str(color_code["command"]).split()
            _, cmd, _ = str(color_code_up_down["command"]).split()
            if my == "none" and is_on_ladder:
                my = cmd
            return (mx, my, ma)
        return tuple(str(color_code_up_down["command"]).split())
    if color_code:
        return tuple(str(color_code["command"]).split())
    return tuple(str(color_code_up_down["command"]).split())


def main():
    print()
    print("=" * 62)
    print("  贴身跳跃优先 —— 回归用例")
    print("=" * 62)

    # ── ① 源码头对齐检查：确认用例复刻的就是真源码 ──────────────────────
    print()
    print("① 源码头对齐（防止用例与源码各说各话）")
    src_path = os.path.join(REPO_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    check("源码里有 _PREFER_JUMP_RADIUS 定义", "_PREFER_JUMP_RADIUS = 2" in src, True)
    check("源码里有贴身跳跃分支", "_is_jump and _both_tight" in src, True)
    check("源码里方向按梯子方位重算", "_dx = int(_up_px[0]) - int(self.loc_player_global[0])" in src, True)
    check("源码不再写死照抄左右方向",
          'self.cmd_move_x = "none"\n                _cmd_ud' in src, True)

    # ── ② 现场复现：梯子在左、跳跃在右，两者贴身 ─────────────────────────
    print()
    print("② 用户现场（梯子灰线在左 x=80，跳跃圆点在右 (88,108)）")
    jump = {"pixel": (88, 108), "command": "right none jump", "distance": 1}
    ladder = {"pixel": (80, 108), "command": "none up none", "distance": 2}

    # 修复前会这样（照抄颜色里的 right）→ 向右跳 = 远离梯子，必须**不能**出现
    check("贴身时选跳跃（不再被梯子抢走）",
          decide(jump, ladder, (87, 108), True), ("left", "up", "jump"))
    check("方向按梯子方位=左（不是照抄 right）",
          decide(jump, ladder, (87, 108), True)[0], "left")

    # ── ③ 人在梯子正下方：应原地起跳，不给横移 ───────────────────────────
    print()
    print("③ 人在梯子正下方（同列）→ 原地跳，避免空中横移落偏")
    ladder2 = {"pixel": (88, 108), "command": "none up none", "distance": 1}
    check("同列 → none", decide(jump, ladder2, (88, 108), True)[0], "none")

    # ── ④ 梯子在右：应向右跳（对称性）───────────────────────────────────
    print()
    print("④ 梯子在右 → 向右跳（对称性）")
    ladder3 = {"pixel": (96, 108), "command": "none up none", "distance": 2}
    check("梯子在右 → right", decide(jump, ladder3, (88, 108), True)[0], "right")

    # ── ⑤ 人在路上时行为不变（最重要的"不回归"保证）──────────────────────
    print()
    print("⑤ 人还在路上（梯子 d=0、跳跃 d=6）→ 行为必须与修复前完全一致")
    jump_far = {"pixel": (88, 108), "command": "right none jump", "distance": 6}
    ladder_near = {"pixel": (84, 108), "command": "none up none", "distance": 0}
    check("仍按梯子走（none up none）",
          decide(jump_far, ladder_near, (84, 108), True), ("none", "up", "none"))

    # ── ⑥ 非跳跃色码不受影响 ─────────────────────────────────────────────
    print()
    print("⑥ 普通移动色码不受新判据影响")
    walk = {"pixel": (88, 108), "command": "right none none", "distance": 1}
    # ⚠️ 期望值里 move_y="up" 是**旧代码本来就这样**（`if cmd_move_y == "none"
    #    and is_on_ladder: cmd_move_y = <上下码的 up>` 那条既有逻辑），
    #    不是本次新判据引入的。已用 `git show HEAD:` 的原版逐行核对确认。
    #    写这一条的目的是**钉住"新判据没有污染普通色码路径"** ——
    #    关键在 move_x/move_action 与原版一致（right / none）。
    check("普通色码仍按原逻辑（距离近者胜，move_x/action 不变）",
          decide(walk, ladder, (87, 108), True), ("right", "up", "none"))

    # ── ⑦ 不在梯子上时不补上下方向 ───────────────────────────────────────
    print()
    print("⑦ 判定不在梯子上 → 跳跃不补上下方向（不乱按 up）")
    check("is_on_ladder=False → move_y 保持 none",
          decide(jump, ladder, (87, 108), False), ("left", "none", "jump"))

    print()
    print("=" * 62)
    print(f"  通过 {_PASS} 项，失败 {_FAIL} 项")
    print("=" * 62)
    print()
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
