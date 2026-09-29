# -*- coding: utf-8 -*-
"""
钉住「跳跃对齐抑制层必须有兜底出口」（2026-09-29 加，issue #12）。

## 为什么需要它

`update_cmd_by_route` 里有一层「方向性跳跃先等角色走到梯子正下方再发」
（`jump_align_tol`，2026-09-27 加）。它的放行条件是横向对齐：

    abs(梯子x - 角色x) <= jump_align_tol

**这个条件有可能永远不成立** —— 因为角色是被**路线指令**带着走的，
路线接引的落点每次和梯子差 2px 以上时，对齐窗口就闪不过去。于是：

    跳跃 → 被这层吞掉（只走不跳）→ 角色被接引指令拉走 → 更对不准 → …

真机现场（issue #12 评论，用户 chenzhi822 的日志）：
  · `[梯子] 全局 x 变化 2px（82 → 80）⇒ 判定已离开梯子`
  · `[路线失联] 角色已偏离路线（小地图位置 (112, 82)）… 重新接引：left none jump`
  · 全局 x 在 82~96 之间反复，跳跃一路被吞
  用户原话：「死活对不准绳子跳」「第二次尝试能跳对一次，之后又乱走了」

⇒ 本用例钉的是**兜底出口**：连续被吞满 `route.jump_align_giveup` 帧后
必须**放弃对齐、照跳**。宁可跳偏一次（角色自己有接引机制会纠正），
也不要**永远不跳**。

## 用法
  python tools/verify_jump_align_giveup.py
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


# ── 复刻引擎里那层判据（与 MapleStoryAutoLevelUp.update_cmd_by_route 一致）──
_PREFER_JUMP_RADIUS = 2
DEFAULT_JTOL = 2
DEFAULT_JGIVEUP = 60


def decide_jump(cc_cmd, ladder_px, player_x, n_suppress,
                jtol=DEFAULT_JTOL, jgiveup=DEFAULT_JGIVEUP):
    """把「对齐抑制 + 兜底出口」那段抽出来，返回 (是否放行跳跃, 新的连续被吞计数)。

    这里**必须与源码头保持一致**：源码改了而这里没改，用例就变成自说自话。
    所以下面 ① 会去源码里核对关键片段。

    语义（与源码逐条对应）：
      · 只有 方向性跳跃(left/right) + 附近有梯子 + **尚未对齐** 才抑制；
      · 已经连续被吞满 jgiveup 帧 ⇒ 放弃对齐、放行（返回 True）；
      · 放行时计数**清零**（回到正常状态，下次重新开始数）。
    """
    is_jump = "jump" in str(cc_cmd)
    jmx = str(cc_cmd).split()[0] if str(cc_cmd).split() else "none"
    if not is_jump or jtol <= 0 or jmx not in ("left", "right") or ladder_px is None:
        return True, 0
    aligned = abs(int(ladder_px[0]) - int(player_x)) <= jtol
    if aligned:
        return True, 0
    give_up = jgiveup > 0 and n_suppress >= jgiveup
    if give_up:
        return True, 0          # ★ 兜底：放弃对齐、照跳
    return False, n_suppress + 1   # 仍在对齐中 → 只走不跳，计数 +1


def main():
    print()
    print("=" * 62)
    print("  跳跃对齐兜底出口 —— 回归用例（issue #12）")
    print("=" * 62)

    # ── ① 源码头对齐检查：确认用例复刻的就是真源码 ──────────────────────
    print()
    print("① 源码头对齐（防止用例与源码各说各话）")
    src_path = os.path.join(REPO_ROOT, "src", "engine", "MapleStoryAutoLevelUp.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    check("源码里读了 jump_align_giveup",
          'get("jump_align_giveup", 60)' in src, True)
    check("源码里算了 _give_up",
          "_give_up = _jgiveup > 0 and _n_before >= _jgiveup" in src, True)
    check("抑制分支带 not _give_up 出口",
          "and not _give_up:" in src, True)
    check("抑制分支仍保留原对齐判据",
          'abs(int(_lad_px[0]) - int(self.loc_player_global[0])) > _jtol' in src, True)
    check("有『放弃对齐、照跳』的用户提示",
          "放弃对齐、**照跳**" in src, True)

    # ── ② 对齐时正常放行 ─────────────────────────────────────────────────
    print()
    print("② 已对齐（|dx| ≤ 2）→ 立刻放行，不受兜底影响")
    check("dx=0 放行", decide_jump("right none jump", (80, 108), 80, 0)[0], True)
    check("dx=2 放行（边界内）", decide_jump("right none jump", (80, 108), 82, 0)[0], True)
    check("对齐放行后计数清零",
          decide_jump("right none jump", (80, 108), 82, 55)[1], 0)

    # ── ③ 对不准时先抑制（这是原有行为，不能丢）──────────────────────────
    print()
    print("③ 没对齐且没到放弃阈值 → 仍然抑制（保留原设计的'先走过去'）")
    check("dx=4、首次 → 抑制", decide_jump("right none jump", (80, 108), 84, 0)[0], False)
    check("dx=4、被吞 59 次 → 仍抑制（未到 60）",
          decide_jump("right none jump", (80, 108), 84, 59)[0], False)
    check("抑制时计数递增",
          decide_jump("right none jump", (80, 108), 84, 10)[1], 11)

    # ── ④ ★ 核心：到阈值必须放行（这就是本次修的 bug）───────────────────
    print()
    print("④ ★ 兜底出口：连续被吞满 60 帧 → 放弃对齐、照跳")
    check("dx=4、被吞 60 次 → **放行**（旧代码这里永远 False）",
          decide_jump("right none jump", (80, 108), 84, 60)[0], True)
    check("被吞 100 次 → 仍放行（不会倒回去抑制）",
          decide_jump("right none jump", (80, 108), 84, 100)[0], True)
    check("放行后计数清零（下次重新数）",
          decide_jump("right none jump", (80, 108), 84, 60)[1], 0)

    # ── ⑤ 用户现场逐帧复现：全局 x 在 80~96 反复，跳跃必须最终发出去 ──────
    print()
    print("⑤ 用户现场（issue #12）：全局 x 在 80~96 反复、始终对不准")
    n = 0
    sent_at = None
    # 模拟 200 帧：角色被接引指令带着在梯子两侧来回（永远 |dx| > 2）
    for i in range(200):
        px = 84 if (i // 10) % 2 == 0 else 96     # 在 84 ↔ 96 之间摆动，全程对不准
        ok, n = decide_jump("right none jump", (80, 108), px, n)
        if ok:
            sent_at = i
            break
    check("200 帧内**一定**跳出一次（旧代码：永远跳不出）",
          sent_at is not None, True)
    check("首次跳出的帧号 == 阈值（60）", sent_at, DEFAULT_JGIVEUP)

    # ── ⑥ 原地跳不受本层影响（none 方向不是"方向性跳跃"）────────────────
    print()
    print("⑥ 原地跳（move_x=none）不受对齐抑制影响")
    check("none up jump + 对不准 → 仍放行",
          decide_jump("none up jump", (80, 108), 96, 0)[0], True)

    # ── ⑦ 两个旋钮都能关 ─────────────────────────────────────────────────
    print()
    print("⑦ 配置旋钮：填 0 各自关掉对应那一层")
    check("jump_align_tol=0 → 关掉抑制（永远放行）",
          decide_jump("right none jump", (80, 108), 96, 0, jtol=0)[0], True)
    check("jump_align_giveup=0 → 关掉兜底（退回旧行为：一直只走不跳）",
          decide_jump("right none jump", (80, 108), 96, 999, jgiveup=0)[0], False)

    # ── ⑧ 无梯子像素时不介入（不影响普通跳跃）───────────────────────────
    print()
    print("⑧ 附近没有梯子像素 → 本层完全不介入")
    check("ladder_px=None → 放行",
          decide_jump("right none jump", None, 96, 0)[0], True)

    # ── ⑨ config 里确实有这两个键（防止只改代码不改出厂表）────────────
    print()
    print("⑨ 出厂配置表里必须有这两个键（否则用户看不懂怎么调）")
    cfg_path = os.path.join(REPO_ROOT, "config", "config_default.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        cfg_txt = f.read()
    check("config_default.yaml 有 jump_align_giveup", "jump_align_giveup:" in cfg_txt, True)
    check("config_default.yaml 有 jump_align_tol", "jump_align_tol:" in cfg_txt, True)

    print()
    print("=" * 62)
    print(f"  通过 {_PASS} 项，失败 {_FAIL} 项")
    print("=" * 62)
    print()
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
