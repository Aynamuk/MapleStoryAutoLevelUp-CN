# -*- coding: utf-8 -*-
'''
离线自检：名字定位长时间失效时，攻击框用**过期坐标**（issue #6 排查，2026-09-28）

现象（用户日志实证）：
    [运行状态] 21 条里「名字命中」**全是 False**，miss 一路涨到 650；
    同期「怪N」显示 1~2（怪检测正常工作），但指令字段**从头到尾没有一个 attack**
    —— 这 100 秒里角色一次都没出招。

根因（代码路径，已核对）：
    `get_player_location_by_nametag()` 匹配失败时**不更新** `self.loc_nametag`
    （见 MapleStoryAutoLevelUp.py:1240-1244 的 else 分支），
    而 `loc_player` 每帧都用那个**陈旧**的 `loc_nametag` 重算 ⇒ 位置被「冻住」。
    攻击判定链（`update_cmd_by_mob_detection`）与攻击框
    （`get_attack_range` / `get_nearest_monster`）**全部以 `self.loc_player` 为锚**：
        搜索框 : x0 = px - dx,  x1 = px + dx   (dx = 350 + 50 = 400)
                 y0 = py - dy,  y1 = py + dy   (dy = 70 + 50 = 120)
        攻击框 : y0 = py - 35,  y1 = py + 35   ← 高度只有 70px
    角色每帧在动（实测 ~0.3px/帧，20fps ⇒ 约 6px/秒），而名字可能连续 650 帧
    （≈32 秒）不命中 ⇒ 攻击框与角色的真实位置**越差越远**，
    检测到的怪落不进框里 ⇒ 永远不满足出招条件 ⇒ 静默地"不打怪"。

⚠️ 本自检**只验证事实与不变量**，不预设该怎么修：
    · 它在 CI 可跑（不依赖 minimaps/、config_data.yaml、nametag/*.png 等私有素材）
    · 所有引擎调用都走「绕过 __init__ 的 Stub」，与既有自检一致

用法：
    python -m tools.verify_attack_box_stale_pos
'''
import sys

import numpy as np

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def make_stub(cfg, loc_player):
    '''构造一个能走通攻击链的假引擎（**不跑 __init__**）。

    ⚠️ `get_nearest_monster` 内部会调 `self.get_attack_range(...)`，
       所以必须把真实方法**绑到实例上**（否则 AttributeError）。
       绑的是引擎原方法，保证「测的就是跑的那份」。
    '''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass

    s = S()
    s.get_attack_range = lambda is_left=True: MapleStoryAutoBot.get_attack_range(
        s, is_left=is_left)
    # 同理：`update_cmd_by_mob_detection` 开头会调 `self._attack_pos_is_fresh()`，
    # 也必须把真实方法绑上（绑的是引擎原方法，保证"测的就是跑的那份"）。
    s._attack_pos_is_fresh = lambda: MapleStoryAutoBot._attack_pos_is_fresh(s)
    s.cfg = cfg
    s.loc_player = loc_player
    s.monsters = []
    # ⚠️ 必须给一个**真实尺寸**的模板：`get_nearest_monster` 的阈值是
    #    `min(min_mob_area, max_mob_area_trigger)`，而 min_mob_area 取模板面积。
    #    留空 ⇒ min_mob_area=0 ⇒ 阈值 0 ⇒ **任何**怪都算命中（把 bug 掩盖掉）。
    #    这里塞一个 40x40 的模板，阈值 = min(1600, 1500) = 1500，
    #    要求怪与攻击框的重叠面积 ≥ 1500px² —— 与真机一致。
    s.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    s.is_on_ladder = False
    s.is_using_home_route = False
    s.t_last_attack = 0.0
    s.cmd_action = "none"
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_move_x_last = "none"
    s._turn_frames_left = 0
    s._turn_dir = None
    s.img_frame = None
    return s


def base_cfg():
    return {
        "bot": {"attack": "directional"},
        "monster_detect": {"search_box_margin": 50, "max_mob_area_trigger": 1500},
        "character": {"width": 100, "height": 150},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
    }


def main():
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    print("===== 1. 攻击框锚定在 loc_player（这是「位置过期 = 不打怪」的机制） =====")
    st = make_stub(base_cfg(), (700, 400))
    r = MapleStoryAutoBot.get_attack_range(st, is_left=True)
    check("directional/left 的攻击框", r, (700 - 350, 400 - 35, 700, 400 + 35))
    check("  ⤷ 横向跨度 350px", r[2] - r[0], 350)
    check("  ⤷ 纵向只有 70px（很窄）", r[3] - r[1], 70)

    st = make_stub(base_cfg(), (700, 400))
    r = MapleStoryAutoBot.get_attack_range(st, is_left=False)
    check("directional/right 的攻击框", r, (700, 400 - 35, 700 + 350, 400 + 35))

    print()
    print("===== 2. 位置一过期，怪就落不进攻击框（直接复现「不打怪」） =====")
    # 假设角色真实在屏幕 (700, 400)，但名字失效、loc_player 冻在 (200, 100)
    # 一只 40x40 的怪贴在角色脸上：放在 (660, 380)
    #   => 完整落在攻击框 x[350,700] y[365,435] 内，重叠 1600px² ≥ 阈值 1500
    mon = {"name": "m", "position": (660, 380), "size": (40, 40), "score": 0.1}

    st = make_stub(base_cfg(), (700, 400))          # 位置新鲜
    st.monsters = [mon]
    got = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    check("位置新鲜 → 找得到怪（会出招）", got is not None, True)

    st = make_stub(base_cfg(), (200, 100))          # 名字失效，位置冻在别处
    st.monsters = [mon]
    got = MapleStoryAutoBot.get_nearest_monster(st, is_left=True)
    check("位置过期 → 找不到怪（静默不出招）", got is None, True)

    print()
    print("===== 3. 位置过期**十几帧**就足以让攻击彻底失效 =====")
    # 角色真实位置随帧推进（20fps、每帧约 0.3px 时位移很小；
    # 但名字失效期间**掉帧/重定位**会让偏差累积到几十像素）。
    st = make_stub(base_cfg(), (700, 400))
    st.monsters = [mon]
    found_at = None
    for lag in range(0, 130, 10):                   # 冻结位置离真实位置 lag px
        st.loc_player = (700 - lag, 400)
        if MapleStoryAutoBot.get_nearest_monster(st, is_left=True) is not None:
            found_at = lag
    # 攻击框左边界 = px - 350；怪在 x=690 ⇒ px - 350 <= 730 恒成立（横向够宽）
    # 纵向 ±35 才是瓶颈：怪 y=385 需要 |py - 400| <= 35
    check("横向 350px 很宽，纵向才是瓶颈（本用例横向仍能命中）", found_at is not None, True)

    # 换成纵向偏差 —— 这才是真正的瓶颈
    losses = []
    for lag in range(0, 130, 10):
        st.loc_player = (700, 400 - lag)
        if MapleStoryAutoBot.get_nearest_monster(st, is_left=True) is None:
            losses.append(lag)
    check("纵向偏差 ≥40px 起就再也找不到怪", bool(losses) and min(losses) <= 40, True)
    print(f"       （实际最先失效的纵向偏差 = {min(losses) if losses else '未失效'}px）")

    print()
    print("===== 5. 位置过期时的**行为**：暂停出招 + 分级告警 =====")
    import inspect
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as B
    src_gate = inspect.getsource(B._attack_pos_is_fresh)

    st = make_stub(base_cfg(), (700, 400))
    st.nametag_miss_streak = 0
    check("刚命中（miss=0）→ 位置可信", B._attack_pos_is_fresh(st), True)

    st.nametag_miss_streak = 29
    check("miss=29（<30）→ 仍可信（不误伤短暂遮挡）", B._attack_pos_is_fresh(st), True)

    st.nametag_miss_streak = 30
    check("miss=30（=阈值）→ 判为过期", B._attack_pos_is_fresh(st), False)

    st.nametag_miss_streak = 650
    check("miss=650（issue #6 实测最长）→ 判为过期", B._attack_pos_is_fresh(st), False)

    st.nametag_miss_streak = 5
    st.cfg = dict(base_cfg())
    st.cfg["nametag"] = {"stale_after_frames": 0}
    check("configured 0 → 关掉本层（退回老行为）", B._attack_pos_is_fresh(st), True)

    st.cfg = {"nametag": {"stale_after_frames": "abc"}}
    check("填非法值 → 回落默认 30（不抛异常）", B._attack_pos_is_fresh(st), True)

    class Bare:
        pass
    try:
        r = B._attack_pos_is_fresh(Bare())
        check("裸对象不抛异常（Stub 不走 __init__）", r, True)
    except Exception as e:                       # noqa: BLE001
        check(f"裸对象不抛异常（实际抛了 {type(e).__name__}）", False, True)

    check("闸门在 update_cmd_by_mob_detection 最前面（算搜索框之前）",
          "if not self._attack_pos_is_fresh():" in
          inspect.getsource(B.update_cmd_by_mob_detection), True)
    check("   ⤷ 暂停时会清掉残留 attack（不打空气）",
          'if self.cmd_action == "attack":\n                self.cmd_action = "none"' in
          inspect.getsource(B.update_cmd_by_mob_detection), True)
    check("   ⤷ **不碰移动指令**（巡逻靠小地图全局坐标，与名字定位无关）",
          "cmd_move_x = " not in
          inspect.getsource(B.update_cmd_by_mob_detection).split("_attack_pos_is_fresh()")[1]
          .split("Get monster search box")[0], True)

    print()
    print("===== 5b. 位置过期对「选方向」的影响（issue #6 第 3 位用户） =====")
    # `get_attack_direction` 用 self.loc_player[0] 判断"怪在角色左边还是右边"
    # （见引擎 is_monster_on_correct_side）。位置一过期，这个左右判断的**基准**就错了。
    #
    # ⚠️ 实测结论（本用例两次实证，纠正一个想当然的判断）：
    #    位置过期**不会**让引擎"朝反方向出招" —— 因为 is_monster_on_correct_side
    #    会发现"怪不在我这一侧"从而返回 None，只是**不出招**。
    #    所以「左右不分」不是这条路径直接造成的；这条只解释「不打怪」。
    mon_left = {"name": "m", "position": (500, 380), "size": (40, 40), "score": 0.1}
    st = make_stub(base_cfg(), (700, 400))
    st.monsters = [mon_left]
    st.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    d = B.get_attack_direction(st, mon_left, None)
    check("位置新鲜：怪在左边 → 判 left（正确）", d, "left")

    st.loc_player = (100, 400)          # 位置过期到怪的右边
    d = B.get_attack_direction(st, mon_left, None)
    check("位置过期：同一只怪 → 返回 None（**不是**反向出招）", d, None)

    # 真正会发生的是：闸门让引擎**在选方向之前就 return** ⇒ 下面这条路径走不到。
    st = make_stub(base_cfg(), (100, 400))
    st.monsters = [mon_left]
    st.monsters_info = {"m": [(np.zeros((40, 40, 3), np.uint8), None)]}
    st.cmd_action = "attack"          # 预置残留，验证被清掉
    st.nametag_miss_streak = 650
    st.t_last_attack = 0.0
    st.cfg = dict(base_cfg())
    st.cfg["nametag"] = {"stale_after_frames": 30}
    st.img_frame = np.zeros((680, 768, 3), np.uint8)
    B.update_cmd_by_mob_detection(st)
    check("位置过期 → 不出招（残留 attack 被清）", st.cmd_action, "none")
    check("   ⤷ 也没给任何方向指令（不会左右乱窜）", st.cmd_move_x, "none")

    src_all = inspect.getsource(B)
    # ⚠️ 断言只能查**真的会打出去的日志串**，不能扫全文 ——
    #    源码里为说明"改了什么、为什么改"，在注释和 docstring 里**引用**了旧文案，
    #    按整篇匹配会把它们也算进去、造成假失败（本用例前两版都这么挂的：
    #    第一版漏了注释、第二版只过滤 `#` 行，漏了跨行 docstring —— 见 :4038）。
    #    做法：只看**日志调用真正传进去的 f-string 字面量**。
    import re as _re
    _logged = " ".join(_re.findall(r'logger\.\w+\(\s*((?:f?)"[^"]*"(?:\s*(?:f?)"[^"]*")*)',
                                   src_all, _re.S))
    check("打出去的日志里不再有「屏幕位置基本不动，影响有限」",
          "影响有限" not in _logged, True)
    check("打出去的日志里有分级后的「打怪判定暂停」",
          "打怪判定暂停" in _logged, True)
    check("   ⤷ 且保留未超阈值时的「暂未影响打怪」分支",
          "暂未影响打怪" in _logged, True)

    print()
    if FAIL:
        print(f"失败的用例（{len(FAIL)}）：{', '.join(FAIL)}")
        return 1
    print("全部通过：已钉死「位置过期 → 攻击框偏移 → 静默不打怪」的事实链")
    return 0


if __name__ == "__main__":
    sys.exit(main())
