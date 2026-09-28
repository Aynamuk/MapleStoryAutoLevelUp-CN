# -*- coding: utf-8 -*-
'''
离线自检：**终点够不着 / 跳跃被静默挡掉** 的告警（2026-09-28，issue #6）

背景（issue #6 真机现场，废都南方工地）：
    用户报「不跳、不打怪，只在一条线上来回走」。逐帧核对他的 route1.png 后确认：
      段0 的 goal 画在 **y=90~94 的高台**上，而角色一直在地面 y=116 往返，
      3 分钟里 y 从没到过 95 以内 ⇒ **一次都没爬上去**。
    但现有的看门狗抓不到它 —— `is_player_stuck` 看的是"位置有没有变"，
    而角色**一直在动**（每几秒往返一趟），`dx+dy > range` 频繁成立 ⇒ 永不判卡死
    ⇒ 静默空转，日志里一个字都没有。
    另外 3894 行那层「跳跃还没和梯子对齐 → 先只走路」会把跳跃**静默吞掉**
    （cmd_action 直接变 none，连键盘层"跳跃冷却挡住"那条日志都不会产生）。

本修复补两条**纯观测**告警（不改任何指令、不脱困）：
  ① `_warn_unreachable_goal`：本段终点在高度上够不着且持续 N 帧 → error 日志；
  ② 跳跃被 align 层挡住累计到 90 帧 → warning 日志。

本自检验什么（不开游戏、不要窗口、不依赖任何私有素材，可在 CI 跑）：
  1. 高度差**始终超容差**且连续 180 帧 → 恰好打出一次 `[路线够不着]`；
  2. 高度差**收敛**（角色上去了）→ 不告警，且计数被清零；
  3. 告警**不碰任何指令**（cmd_move_* / idx_routes 前后一致）；
  4. 回正状态下不告警（回正有自己的日志）；
  5. 缺 `_seg_goals`（自检 Stub 不走 __init__）时不崩；
  6. 跳跃埋点：`_jump_align_suppress` 在真发跳后被清零（源码级校验清零语句存在）。

⚠️ 本自检**不读** minimaps/ 、config_data.yaml、nametag/*.png —— 它们在 CI 检出后
   都不存在（仓库只有 .gitkeep / .blank）。所有判据用桩对象构造。

用法：
    python -m tools.verify_goal_unreachable_warn
'''
import sys

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


class Stub:
    '''只带本自检用得到的属性的假引擎（**不跑 __init__**，与既有自检一致）。'''

    def __init__(self, y, goal_y, seg=0, n_goals=2, home=False, cfg=None):
        self.loc_player_global = (180, y)
        self._seg_goals = [(199, goal_y)] + [(x, 116) for x in range(201, 201 + n_goals - 1)]
        self.idx_routes = seg
        self.is_using_home_route = home
        self.cfg = cfg if cfg is not None else {"route": {"goal_reach_y_tol": 3}}


def call(stub):
    '''调用被测方法（从引擎模块拿，保证测的就是跑的那份）。'''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
    return MapleStoryAutoBot._warn_unreachable_goal(stub)


def main():
    import src.utils.logger as L

    print("===== 1. 终点够不着 → 连续 180 帧时报一次 =====")
    stub = Stub(y=116, goal_y=93)          # 高度差 23 > 容差 3
    msgs = []
    orig = L.logger.error
    L.logger.error = lambda m, *a, **k: msgs.append(m)
    try:
        for _ in range(179):
            call(stub)
        check("179 帧内不报", len(msgs), 0)
        call(stub)                          # 第 180 帧
        check("第 180 帧报一次", len(msgs), 1)
        check("日志带 [路线够不着] 标记", "[路线够不着]" in msgs[0], True)
        check("日志给出终点 y", "y=93" in msgs[0], True)
        check("日志说明不是卡死", "不是卡死" in msgs[0], True)
        for _ in range(60):
            call(stub)                      # 阈值是 ==180，不应再刷
        check("之后不重复刷屏", len(msgs), 1)
    finally:
        L.logger.error = orig

    print("===== 2. 角色上去了（高度收敛）→ 不告警且清零 =====")
    stub = Stub(y=116, goal_y=93)
    for _ in range(50):
        call(stub)
    check("尚未收敛时计数在涨", stub._unreach_frames, 50)
    stub.loc_player_global = (180, 94)     # 爬上去了：|94-93| = 1 <= 3
    call(stub)
    check("收敛后计数清零", stub._unreach_frames, 0)
    msgs = []
    L.logger.error = lambda m, *a, **k: msgs.append(m)
    try:
        for _ in range(200):
            call(stub)
        check("收敛状态下永不告警", len(msgs), 0)
    finally:
        L.logger.error = orig

    print("===== 3. 告警**不改任何指令**（纯观测） =====")
    stub = Stub(y=116, goal_y=93)
    stub.cmd_move_x, stub.cmd_move_y, stub.cmd_action = "left", "none", "none"
    before = (stub.cmd_move_x, stub.cmd_move_y, stub.cmd_action, stub.idx_routes)
    L.logger.error = lambda m, *a, **k: None
    try:
        for _ in range(200):
            call(stub)
    finally:
        L.logger.error = orig
    after = (stub.cmd_move_x, stub.cmd_move_y, stub.cmd_action, stub.idx_routes)
    check("指令与段号未被改动", after, before)

    print("===== 4. 回正状态下不告警 =====")
    stub = Stub(y=116, goal_y=93, home=True)
    msgs = []
    L.logger.error = lambda m, *a, **k: msgs.append(m)
    try:
        for _ in range(300):
            call(stub)
        check("回正中不报", len(msgs), 0)
    finally:
        L.logger.error = orig

    print("===== 5. 健壮性：缺属性不崩（Stub 不走 __init__） =====")
    class Bare:
        pass
    try:
        call(Bare())
        check("裸对象不抛异常", True, True)
    except Exception as e:                   # noqa: BLE001
        check(f"裸对象不抛异常（实际抛了 {type(e).__name__}: {e}）", False, True)

    class NoGoals(Stub):
        pass
    ng = NoGoals(y=116, goal_y=93)
    del ng._seg_goals
    try:
        call(ng)
        check("缺 _seg_goals 不抛异常", True, True)
    except Exception as e:                   # noqa: BLE001
        check(f"缺 _seg_goals 不抛异常（实际抛了 {type(e).__name__}: {e}）", False, True)

    class ZeroTol(Stub):
        pass
    zt = ZeroTol(y=116, goal_y=93, cfg={"route": {"goal_reach_y_tol": 0}})
    msgs = []
    L.logger.error = lambda m, *a, **k: msgs.append(m)
    try:
        for _ in range(300):
            call(zt)
        check("容差填 0（关掉本层）→ 不告警", len(msgs), 0)
    finally:
        L.logger.error = orig

    print("===== 6. 跳跃被挡的埋点在真发跳后清零（源码级校验） =====")
    import inspect
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
    src = inspect.getsource(MapleStoryAutoBot)
    check("存在 _jump_align_suppress 计数", "_jump_align_suppress" in src, True)
    check("真发跳时清零该计数",
          'if self.cmd_action == "jump":\n            self._jump_align_suppress = 0' in src,
          True)
    check("跳跃被挡会打 [跳跃被挡] 日志", "[跳跃被挡]" in src, True)

    print()
    if FAIL:
        print(f"失败的用例（{len(FAIL)}）：{', '.join(FAIL)}")
        return 1
    print("全部通过：终点够不着的告警行为符合预期（6 组 / 共 20 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
