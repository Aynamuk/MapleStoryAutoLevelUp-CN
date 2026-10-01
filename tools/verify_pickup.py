# -*- coding: utf-8 -*-
"""
verify_pickup — 自动拾取（2026-10-01 加）的离线自检，进 CI

覆盖三件事，全部不依赖游戏/驱动/窗口，可在 CI 裸跑：
  1. 配置项存在且默认值正确（pickup 默认空 = 关闭，pickup_cooldown 默认 2.0）
  2. 冷却抖动**真的在抖**：多次采样不是常数，且全部落在 [0.7×cd, 1.3×cd] 内
  3. pickup 留空 = 不发任何按键；设置后到点才发，且计数正确
  4. UI 源码里确实有「自动拾取」组、下限 0.5、以及拾取键的读写接线

为什么要有第 2 条：抖动的**目的**是让间隔统计上不像机器（见
KeyBoardController.PICKUP_JITTER 注释）。如果哪天被人「优化」成精确值，
功能表面照常工作、但设计意图就没了 —— 这种回归只有靠断言能拦住。
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    if not ok:
        FAILS.append(name)


def load_default_cfg():
    with open(os.path.join(ROOT, "config", "config_default.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_config_defaults():
    cfg = load_default_cfg()
    key = cfg.get("key", {})
    check("config: key.pickup 存在且默认为空（功能默认关闭）",
          "pickup" in key and (key["pickup"] or "") == "",
          f"pickup={key.get('pickup')!r}")
    check("config: key.pickup_cooldown 存在且默认为 2.0",
          float(key.get("pickup_cooldown", 0)) == 2.0,
          f"pickup_cooldown={key.get('pickup_cooldown')!r}")


def test_jitter():
    """抖动：不是常数，且落在 [0.7, 1.3] × cd 内。"""
    from src.input.KeyBoardController import KeyBoardController

    kb = KeyBoardController.__new__(KeyBoardController)   # 不跑 __init__（会起线程）
    kb.pickup_cooldown = 2.0

    samples = [kb._pickup_interval() for _ in range(200)]
    lo, hi = min(samples), max(samples)

    check("抖动: 200 次采样不是常数（不是精确 2.0）",
          len(set(round(s, 6) for s in samples)) > 1,
          f"去重后 {len(set(round(s, 6) for s in samples))} 个不同值")
    check("抖动: 全部落在 2.0×[0.7,1.3] = [1.4, 2.6] 内",
          all(1.4 - 1e-9 <= s <= 2.6 + 1e-9 for s in samples),
          f"实测区间 [{lo:.4f}, {hi:.4f}]")
    # 抖动应当真正覆盖整个区间（不是只抖一丁点）
    check("抖动: 实际覆盖区间够宽（> 0.8 秒）",
          (hi - lo) > 0.8, f"跨度 {hi - lo:.4f}s")

    # 配置非法时必须退回下限，不能变成「每帧都按」
    for bad in (0, -5, None, "abc"):
        kb.pickup_cooldown = bad
        s = [kb._pickup_interval() for _ in range(20)]
        ok = all(0.5 * 0.7 - 1e-9 <= v <= 0.5 * 1.3 + 1e-9 for v in s)
        check(f"抖动: 非法配置 {bad!r} 退回下限 0.5（不会变成每帧狂按）", ok,
              f"[{min(s):.3f}, {max(s):.3f}]")


def test_fire_logic():
    """空键不发；设置后按间隔发，计数递增。"""
    from src.input.KeyBoardController import KeyBoardController

    sent = []

    def make_kb(pickup):
        kb = KeyBoardController.__new__(KeyBoardController)
        kb.pickup_key = pickup
        kb.pickup_cooldown = 2.0
        kb.t_last_pickup = 0.0
        kb._t_next_pickup = 0.0
        kb.n_pickup_sent = 0
        kb._press = lambda k: sent.append(k)   # 桩：不发真键
        return kb

    # ① 留空 = 关闭
    kb = make_kb("")
    for t in range(0, 100):
        kb._try_pickup(float(t))
    check("行为: pickup 留空 = 一次都不发（功能关闭）", sent == [], f"发出 {sent}")

    # ② 设了键：先排期、不立刻发
    del sent[:]
    kb = make_kb("z")
    kb._try_pickup(0.0)
    check("行为: 首次调用只排期、不立刻按（避免开局抢键）", sent == [],
          f"发出 {sent}")
    check("行为: 排期时间已写入 _t_next_pickup",
          kb._t_next_pickup > 0, f"next={kb._t_next_pickup:.2f}")

    # ③ 跑到时间点才发
    kb._try_pickup(kb._t_next_pickup + 0.001)
    check("行为: 到点后发出一次，计数=1", sent == ["z"] and kb.n_pickup_sent == 1,
          f"sent={sent}, n={kb.n_pickup_sent}")

    # ④ 长时间循环：频率应接近 1/2s 量级，且绝非每帧狂按
    del sent[:]
    kb = make_kb("z")
    kb._try_pickup(0.0)
    t = 0.0
    while t < 120.0:
        kb._try_pickup(t)
        t += 0.01          # 模拟 100fps 的键盘线程
    # 120 秒 / 平均 2 秒 ≈ 60 次；抖动下合法范围放宽到 [40, 85]
    check("行为: 120 秒内发送次数在合理区间 [40, 85]（不是每帧狂按）",
          40 <= kb.n_pickup_sent <= 85,
          f"实发 {kb.n_pickup_sent} 次（12000 帧调用）")

    # ⑤ 实际间隔必须有抖动（不能每次都是精确 2.0）
    del sent[:]
    kb = make_kb("z")
    times = []

    def fake_press(k):
        times.append(kb._now)

    kb._press = fake_press
    t = 0.0
    while t < 200.0:
        kb._now = t
        kb._try_pickup(t)
        t += 0.01
    gaps = [round(times[i + 1] - times[i], 2) for i in range(len(times) - 1)]
    check("行为: 实际发送间隔不是常数（带抖动）",
          len(set(gaps)) > 1, f"出现 {len(set(gaps))} 种不同间隔")
    # 用 0.01s 量化会引入误差，放宽到 [1.35, 2.65]
    check("行为: 实际间隔全部落在 [1.35, 2.65]（2.0 ± 30% + 量化误差）",
          all(1.35 <= g <= 2.65 for g in gaps),
          f"实测 [{min(gaps):.2f}, {max(gaps):.2f}]")


def test_ui_wiring():
    """UI 源码接线检查：不依赖 Qt，纯文本校验（CI 无显示环境也能跑）。"""
    src = open(os.path.join(ROOT, "src", "ui", "ui.py"), encoding="utf-8").read()

    check("UI: 有「自动拾取」分组", "自动拾取" in src)
    check("UI: 冷却下限是 0.5（不是 0.1）",
          "setRange(0.5, 10.0)" in src)
    check("UI: 布局里挂了 pickup_gbox",
          "scroll_layout.addWidget(self.pickup_gbox)" in src)
    check("UI: 按键绑定组里有「拾取：」行",
          'form_right.addRow("拾取：", self.pickup_key)' in src)
    check("UI: apply_config_to_ui 读了 pickup",
          'key_cfg.get("pickup"' in src)
    check("UI: update_cfg_from_main_ui 写了 pickup",
          'self.cfg["key"]["pickup"] = self.pickup_key.get_key().strip()' in src)
    check("UI: update_cfg_from_main_ui 写了 pickup_cooldown",
          'self.cfg["key"]["pickup_cooldown"]' in src)
    # 用户 2026-10-01 明确指出：提示里必须说明「冷却调小会影响攻击」。
    # 这条曾只存在于预览脚本、落地时漏掉，故在此**钉死**，防止再丢。
    check("UI: 提示里说明了「冷却调小会挤占攻击节奏」（用户明确要求）",
          "挤占攻击节奏" in src)
    check("UI: 冷却偏小时有醒目告警（⚠️，阈值 < 1.0 秒）",
          "冷却偏小" in src and "if cd < 1.0" in src)
    check("config: 注释里也提示了冷却与攻击的取舍",
          "挤占攻击节奏" in open(
              os.path.join(ROOT, "config", "config_default.yaml"),
              encoding="utf-8").read())

    kbsrc = open(os.path.join(ROOT, "src", "input", "KeyBoardController.py"),
                 encoding="utf-8").read()
    check("KB: run() 主循环里调用了 _try_pickup",
          "self._try_pickup(time.time())" in kbsrc)
    check("KB: 抖动幅度是 0.3",
          "PICKUP_JITTER = 0.3" in kbsrc)
    check("KB: 冷却下限是 0.5",
          "PICKUP_COOLDOWN_MIN = 0.5" in kbsrc)


def main():
    test_config_defaults()
    test_jitter()
    test_fire_logic()
    test_ui_wiring()

    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
