# -*- coding: utf-8 -*-
'''回正线体检器**自己的**自检 —— 防止它的判据被改坏 / 静默恒过。

用法：
    python -m tools.verify_home_route_health

## 为什么要有它

`tools/home_route_health.py` 的判据我第一版**写错过两次**，都是靠"和真机实测对照"
才发现的（不是靠自检）：
  ① 关 3 只看"离主线最近距离" → 把**终点贴主线**（判废规则⑤要求的设计意图）误报成不通过；
  ② 改完还是错：**漏了"必须站在主线站立行 ±1"** —— 坑底 `(94,126)` 离主线 2px
     但 y 差 4px，其实是**安全**的，却被报成不通过；
  ③ 关 2 自己比"横段跨度 vs 平台宽度" → 也是错的：`d_home` **没有半径上限**
     （本图实测 x=86 离横段左端 8px 照样进得去）⇒ 必须直接问引擎。

⇒ 这三条都得**钉成用例**，否则下次再改判据还会犯。

⚠️ 本自检**只读**：不弹窗、不连游戏、不改任何文件。
'''
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

import tools.home_route_health as H

FAIL: list = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def verdicts(bot, line, tol=2, timeout=8.0, sr=10):
    return {lbl: v for lbl, v, _ in H.check_line(bot, line, tol, timeout, sr)}


def main():
    b, ret = H._load_bot()
    print("=" * 68)
    print("回正线体检器 · 自检")
    print("=" * 68)
    check("真实资源能加载（load_config 返回 0）", ret, 0)

    # ── 换成**冻结夹具**再断言 ────────────────────────────────────────────
    # ⚠️★ 2026-09-17 修（原样是"数据依赖的坏味道"，见 HANDOFF §七）：
    #    原来直接拿 `b.home_lines[0]`（**当前活动地图的活文件**）当"真实线"，
    #    用户一重录 / 那条线一判废，本节就打印「跳过后续用例」→ 只剩 1 条断言，
    #    **等于自检静默失效**（2026-09-17 用户补线之前，它长期只有 1 条）。
    #    现在改用 `tools/test_fixtures/废都南方工地_真机/`（冻结副本）：
    #    断言对象固定，跟用户今天有没有重录完全无关。
    #    ⚠️ 位置：必须在上面那条 load_config 断言**之后** —— 那条要验真实加载链。
    import cv2
    from src.utils.common import load_image, mask_route_colors, load_yaml
    _fx = "tools/test_fixtures/废都南方工地_真机"
    _cfg_raw = load_yaml("config/config_default.yaml")
    _cc_raw = _cfg_raw["route"]["color_code"]
    _cc_ud = _cfg_raw["route"]["color_code_up_down"]
    b.img_map = load_image(f"{_fx}/map.png")
    _fx_routes = []
    for _n in ("route1.png", "route2.png"):
        _rr = cv2.cvtColor(load_image(f"{_fx}/{_n}"), cv2.COLOR_BGR2RGB)
        _rr = mask_route_colors(b.img_map, _rr, _cc_raw)
        _rr = mask_route_colors(b.img_map, _rr, _cc_ud)
        _fx_routes.append(_rr)
    b.img_routes = _fx_routes
    b.img_route_home = cv2.cvtColor(load_image(f"{_fx}/route_home.png"),
                                    cv2.COLOR_BGR2RGB)
    b.home_lines = b._scan_home_lines("夹具", b.img_route_home, b.img_routes)
    b._build_mainline_cache()
    print(f"       断言改用冻结夹具 {_fx}/：主线 {len(b.img_routes)} 段 / "
          f"回正线 {len(b.home_lines or [])} 条")

    check("★ 冻结夹具里必须能扫出 1 条可用回正线（扫不出=夹具坏了，不是跳过）",
          len(b.home_lines or []), 1)
    if not (b.home_lines or []):
        print("\n❌ 夹具坏了，后续用例无法进行")
        return 1

    real = b.home_lines[0]
    main = b._main_pts_xy
    sy = int(b._main_stand_y)
    mx0, mx1 = int(main[0].min()), int(main[0].max())

    # ── 【1】对照：真实那条线必须全绿 ──────────────────────────────────
    print("\n【1】对照：真实回正线（关 2/3/5 都应通过）")
    v = verdicts(b, real)
    check("真实线 → 关 3 提前踢出 = 通过", v["关 3 提前踢出"], H.OK)
    check("真实线 → 关 5 超时 = 通过", v["关 5 超时"], H.OK)
    check("真实线 → 关 2 落点覆盖 = 通过", v["关 2 落点覆盖"], H.OK)

    # ── 【2】关 3 必须能抓"贴着主线走"（同高度 + 离主线 ≤ tol）──────────
    print("\n【2】变异：贴着主线走（y = 站立行+1，离主线 1px）→ 关 3 必须报红")
    hug = {
        "id": 99, "start": (mx0, sy + 1), "goal": (mx1, sy + 1),
        "n_pts": mx1 - mx0 + 1, "has_jump": False,
        "xs": np.arange(mx0, mx1 + 1), "ys": np.full(mx1 - mx0 + 1, sy + 1),
        "cover_xs": (mx0, mx1), "cover_span": mx1 - mx0 + 1,
    }
    v = verdicts(b, hug)
    check("贴主线的假线 → 关 3 = 不通过", v["关 3 提前踢出"], H.BAD)

    # ── 【3】★ 关 3 不许把"高度差够大"误报（我犯过的第二个错）──────────
    # 坑底 (94,126)：离主线只有 2px，但 y 差 4px → 引擎**不会**退出回正
    # （退出要同时满足「y 在站立行±1」+「d_main ≤ tol」）。
    print("\n【3】★ 离主线很近但**高度差够大** → 关 3 必须通过（防误报）")
    low = dict(hug)
    low["id"] = 97
    low["start"] = (mx0, sy + 4)
    low["goal"] = (mx1, sy + 4)
    low["ys"] = np.full(mx1 - mx0 + 1, sy + 4)
    v = verdicts(b, low)
    check("y 差 4px 的假线 → 关 3 = 通过（只看距离会误报）",
          v["关 3 提前踢出"], H.OK)

    # ── 【4】关 3 不许把"终点贴主线"误报（我犯过的第一个错）────────────
    # 判废规则⑤**要求**终点贴主线（home_goal_mainline_tol=1），那儿退出是设计意图。
    print("\n【4】★ 终点区贴主线不算问题（真实线的终点就压在主线上）")
    goal = real.get("goal")
    _d_goal = (abs(int(goal[0]) - mx0) + abs(int(goal[1]) - sy)) if goal else None
    print(f"       真实线终点 {goal}，到主线最近像素的曼哈顿距离 = "
          f"{min(abs(int(goal[0]) - int(x)) + abs(int(goal[1]) - int(y)) for x, y in zip(main[0], main[1])) if goal else '?'}")
    check("真实线终点贴主线 → 关 3 仍判通过（终点区已排除）",
          v["关 3 提前踢出"], H.OK)

    # ── 【5】关 5 必须能抓"超长回正线" ────────────────────────────────
    print("\n【5】变异：长度 400px（远超 8s 上限）→ 关 5 必须报红")
    n = 400
    long_line = dict(hug)
    long_line["id"] = 98
    long_line["xs"] = np.arange(mx0, mx0 + n)
    long_line["ys"] = np.full(n, sy + 1)
    long_line["n_pts"] = n
    v = verdicts(b, long_line)
    check("400px 假线 → 关 5 = 不通过", v["关 5 超时"], H.BAD)

    # ── 【6】关 5 的判据必须**随长度变化**（防"永远通过"）────────────
    print("\n【6】关 5 随长度变化：短 → 通过 / 长 → 不通过")
    short_line = dict(hug)
    short_line["xs"] = np.arange(mx0, mx0 + 20)
    short_line["ys"] = np.full(20, sy + 1)
    check("20px 假线 → 关 5 = 通过", verdicts(b, short_line)["关 5 超时"], H.OK)

    # ── 【7】关 2 必须**调引擎**（自己算跨度会误报）──────────────────
    # 真实验证：本图横段只铺到 x=94~108，而平台是 x=92~106 ——
    # 若按"跨度 vs 平台宽度"会报不通过，但引擎实际让 93 个落点全进得去。
    print("\n【7】★ 关 2 走引擎（横段没盖满平台 ≠ 接不住）")
    v = verdicts(b, real)
    check("真实线横段没盖住平台左端，但关 2 仍通过（d_home 无半径上限）",
          v["关 2 落点覆盖"], H.OK)

    # ── 【8】★ 关 4 必须能抓「有梯子/管道却**一个跳跃像素都没有**」────────
    # 2026-09-17 真机：用户跳跃键 = c，录制器旧版判定跳跃写死 `if "space" in key_press`
    # ⇒ 录制时"边跳边按上"爬管子，线里**一个 jump 都没有**（只有蓝/灰/黄）
    # ⇒ 机器人走到管柱底下只会按 up，管柱入口偏高就上不去（「路过管道一直往右走」）。
    print("\n【8】★ 关 4 抓「爬管却没画跳」（录制器漏记跳跃的静默失效）")
    v_real = verdicts(b, real)
    check("真机线（44px 管道、0 个跳跃）→ 关 4 = 不通过",
          v_real["关 4 跳跃梯子"], H.BAD)
    # 对照：给同一条线**补一个跳跃像素** → 应该从 BAD 回到 WARN（不是误伤）
    import cv2 as _cv2
    _img_with_jump = b.img_route_home.copy()
    _jump_rgb = None
    for _rgb, _cmd in b.color_code.items():
        if "jump" in str(_cmd):
            _jump_rgb = _rgb
            break
    if _jump_rgb is not None:
        # 在管道底部 (84,126) 涂 1 个跳跃色像素（录制器/手绘器补跳后的样子）
        # ⚠️ b.img_route_home 是 **RGB**（load_config 做过 BGR2RGB），直接写 rgb，
        #    不要像录制器那样反着存。
        _x, _y = 84, 126
        _img_with_jump[_y, _x] = _jump_rgb
        _b2 = b
        _saved = _b2.img_route_home
        _b2.img_route_home = _img_with_jump
        v_jump = verdicts(_b2, real)
        _b2.img_route_home = _saved
        check("对照：补上 1 个跳跃像素 → 关 4 回到「注意」（不是误伤）",
              v_jump["关 4 跳跃梯子"], H.WARN)

    print("\n" + "=" * 68)
    if FAIL:
        print(f"❌ 自检失败 {len(FAIL)} 项：")
        for n_ in FAIL:
            print(f"   - {n_}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
