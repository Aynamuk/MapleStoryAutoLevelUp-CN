# -*- coding: utf-8 -*-
'''
离线自检：主循环性能埋点（2026-09-14 加）

治的是什么：
    配置 `fps_limit_main: 30`，而**真机实测主循环只有 1.8~2 fps** —— 两条独立证据：
      · `[运行状态]` 5 秒只走 10 帧（帧1@17:11:30 → 帧69@17:12:07 ≈ 1.84fps）
      · `[回正·小结] 8.2s/16帧` ≈ 1.95fps
    而全项目原来**没有任何地方记录主循环的实际帧率**（键盘线程会打印 `fps=`，
    主循环不会）→ "一帧 500ms 到底花在哪"完全靠猜。这次补的埋点就是量它用的。

为什么必须离线测（不能只靠真机跑）：
    埋点跑在**主循环**里 —— 抛异常 = 整个挂机停摆，比"没数据"严重一万倍。
    而且 `tools/verify_navigation.py` 会把引擎的 `time` **整个**换成 FakeClock，
    埋点一调用它不支持的方法（`perf_counter`）就全线崩。这两种坏法都没法靠
    真机试出来。所以文案生成做成**纯函数**，在这里离线钉死。

⚠️ 纪律：本脚本测的是 **src/ 里真的 perf_trace_on / perf_summary_line /
   minimap_diff_text** —— 不另写一份假逻辑，否则"测的和跑的不是同一份代码"，
   全绿也保证不了线上不出事。

不开游戏、不抢焦点、不初始化驱动。

用法：
    PYTHONPATH=<项目根> python tools/verify_perf_trace.py
'''
import inspect
import os
import sys

from src.utils.common import load_yaml, monster_detect_interval
from src.utils.logger import perf_trace_on, perf_summary_line
from src.utils.home_route import minimap_diff_text
from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


# ══════════════════════════════════════════════════════════════════════
print("\n【1】perf_trace_on 开关（只影响打不打日志，不影响任何判定）")
check("log.perf_trace: true → 开",
      perf_trace_on({"log": {"perf_trace": True}}), True)
check("★ 显式 false 能关掉（没被 FLOORS 锁死 → 随时可关）",
      perf_trace_on({"log": {"perf_trace": False}}), False)
check("缺 log 段 → 默认开", perf_trace_on({"route": {}}), True)
check("缺 perf_trace 键 → 默认开", perf_trace_on({"log": {}}), True)
check("cfg 是 None → 默认开（宁可多打，别漏记）", perf_trace_on(None), True)
check("cfg 传错类型（字符串）→ 默认开", perf_trace_on("坏了"), True)


# ══════════════════════════════════════════════════════════════════════
print("\n【2】perf_summary_line：降序 + 关键信息齐全")
_line = perf_summary_line({
    "fps": 2.1, "n": 11, "viz": True, "gap_ms": 480.0,
    "items": [("抓帧", 62.0, 70.0), ("名字定位", 210.0, 520.0),
              ("怪检测", 95.0, 110.0), ("回正距离", 0.3, 0.4)],
})
print(f"       {_line}")
check("以 [性能] 开头", _line.startswith("[性能]"), True)
check("带实际帧率", "fps=2.1" in _line, True)
check("带本窗口帧数", "（11帧）" in _line, True)
check("★ 带可视化开关状态（排查「GUI 是不是拖累」全靠它）",
      "可视化=开" in _line, True)
check("带单帧耗时", "一帧 480ms" in _line, True)
check("★ 按耗时**降序**（最慢的排最前 → 一眼看瓶颈）",
      _line.index("名字定位") < _line.index("怪检测") < _line.index("抓帧"),
      True)
check("★ <1ms 的阶段被省略（防噪声刷屏）", "回正距离" in _line, False)
check("带单帧峰值", "名字定位210(峰520)" in _line, True)
check("viz=False → 显示「可视化=关」",
      "可视化=关" in perf_summary_line({"viz": False}), True)


# ══════════════════════════════════════════════════════════════════════
print("\n【3】★ 脏数据绝不抛异常（它跑在**主循环**里，抛了就是停摆）")
for _bad in [{}, None, "乱七八糟", {"items": None},
             {"items": [("a", "x", "y")]}, {"items": [("a", 5.0)]},
             {"n": 0, "fps": None}, {"items": [("a", -5.0, -6.0)]},
             {"gap_ms": "坏"}]:
    try:
        _l = perf_summary_line(_bad)
        _ok = isinstance(_l, str) and _l.startswith("[性能]")
    except Exception as e:                                  # noqa: BLE001
        _ok = False
        print(f"       !! 抛异常了：{e!r}")
    check(f"残缺输入 {str(_bad)[:24]} 也能出一行", _ok, True)
check("★ n=0 不除零（fps 显示 0.0 而不是崩）",
      "fps=0.0" in perf_summary_line({"n": 0}), True)


# ══════════════════════════════════════════════════════════════════════
print("\n【4】★ minimap_diff_text —— 方向绝不能写反（SQDIFF 越小越好）")
_s = minimap_diff_text(0.0087, 0.01)
print(f"       {_s}")
check("4 位小数（不再把 0.005~0.014 全显示成 0.01）", "0.0087" in _s, True)
check("★ 明说「越小越好」（写反会把排查带去完全错误的方向）",
      "越小越好" in _s, True)
check("带阈值对照", "≤0.01" in _s, True)
check("小于阈值 → 算准", "算准" in _s, True)
check("大于阈值 → 偏大", "偏大" in minimap_diff_text(0.5, 0.01), True)
check("不给阈值 → 只说方向",
      minimap_diff_text(0.0087).endswith("（越小越好）"), True)
check("None → 未知，不抛", minimap_diff_text(None), "小地图差异 未知")
check("非数字 → 未知，不抛", minimap_diff_text("x"), "小地图差异 未知")


# ══════════════════════════════════════════════════════════════════════
print("\n【5】★ 真实配置：log.perf_trace 确实是 true")
_cfg = load_yaml("config/config_default.yaml")
_pt = (_cfg.get("log") or {}).get("perf_trace")
print(f"       config_default.yaml: log.perf_trace = {_pt!r}")
check("默认值是 true（排查期别关）", _pt, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【6】★ 埋点接上了，且**行为没被改坏**（本轮纯观测的底线）")
_src_loop = inspect.getsource(MapleStoryAutoBot.loop)
check("loop 里会累加帧间隔", "self.perf_gap +=" in _src_loop, True)
check("loop 里本窗口帧数 +1", "self.perf_n += 1" in _src_loop, True)
# ⚠️ 汇总放在 **run_once** 里而不是 loop 里，理由（别挪）：
#    它紧跟 `[运行状态]`，而那行在「关掉可视化就提前 return」**之前** ——
#    这次排查要对比"开/关画面页签"，关掉时它也必须打得出来。
check("run_once 里会打 [性能] 汇总（紧跟 [运行状态]）",
      "perf_summary_line(" in inspect.getsource(MapleStoryAutoBot.run_once),
      True)
check("★ 限帧判定原文未动",
      "if frame_duration < target_duration:" in _src_loop, True)
check("★ 限帧 sleep 原文未动",
      "time.sleep(target_duration - frame_duration)" in _src_loop, True)
_src_perf = inspect.getsource(MapleStoryAutoBot._perf)
check("_perf 全程 try/except（埋点绝不能把主循环带崩）",
      "except Exception" in _src_perf, True)
check("_perf 用 getattr 兜底（Stub 不走 __init__）",
      "getattr(self," in _src_perf, True)
check("★ 关掉开关时 _perf 直接返回（零开销）",
      'if not getattr(self, "perf_on", False)' in _src_perf, True)


# ══════════════════════════════════════════════════════════════════════
print("\n【7】★ 假时钟必须有 perf_counter（否则自检全线崩）")
# 这条在防什么：verify_navigation.py 把引擎的 `time` **整个**换成 FakeClock。
# 埋点用 time.perf_counter()，FakeClock 少了这个方法 → 【24】之后全崩。
_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "tools", "verify_navigation.py")
try:
    with open(_p, encoding="utf-8") as f:
        _src_nav = f.read()
except OSError:
    _src_nav = ""
check("verify_navigation.py 的 FakeClock 提供了 perf_counter",
      "def perf_counter" in _src_nav, True)


# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
print("\n【8】monster_detect_interval：怪检测隔几帧跑一次（1 = 关掉降频）")
check("every_n_frames: 2 → 2",
      monster_detect_interval({"monster_detect": {"every_n_frames": 2}}), 2)
check("every_n_frames: 1 → 1（关掉降频，回到老行为）",
      monster_detect_interval({"monster_detect": {"every_n_frames": 1}}), 1)
check("★ 缺 monster_detect 段 → 1（最保守：每帧都跑）",
      monster_detect_interval({}), 1)
check("★ 缺 every_n_frames 键 → 1",
      monster_detect_interval({"monster_detect": {}}), 1)
check("cfg 是 None → 1", monster_detect_interval(None), 1)
check("填 0 → 1（0 会让检测永远不跑）",
      monster_detect_interval({"monster_detect": {"every_n_frames": 0}}), 1)
check("填负数 → 1",
      monster_detect_interval({"monster_detect": {"every_n_frames": -3}}), 1)
check("填非数字 → 1",
      monster_detect_interval({"monster_detect": {"every_n_frames": "x"}}), 1)
_iv_cfg = (load_yaml("config/config_default.yaml").get("monster_detect") or {}).get(
    "every_n_frames")
print(f"       config_default.yaml: monster_detect.every_n_frames = {_iv_cfg!r}")
check("★ 真实配置默认是 2（降频确实生效）", _iv_cfg, 2)


# ══════════════════════════════════════════════════════════════════════
print("\n【9】★ 怪检测：ROI 预处理已提到模板循环外（2026-09-14 零风险优化）")
# 原来 `mask_roi` / `img_roi_blur` 放在模板循环里 → 每个模板重算一遍
# （本图 20 张模板 = 重复 20 次）。它只跟「画面 + 角色位置」有关、跟模板无关，
# 提出来**结果一模一样、只是少算** —— 典型的零风险优化。
# 这里用源码顺序钉住它确实在循环外（防止以后改回去还不知道）。
_src_gmr = inspect.getsource(MapleStoryAutoBot.get_monsters_in_range)
check("★ ROI 的 GaussianBlur 在模板循环**之外**（整帧只算一次）",
      _src_gmr.index("_roi_blur = cv2.GaussianBlur")
      < _src_gmr.index("for monster_name, monster_imgs"), True)
check("★ 循环内改为复用 _roi_blur（不再每个模板重算）",
      "img_roi_blur = _roi_blur" in _src_gmr, True)
# ⚠️ 不要用 "mask_roi = np.all(img_roi" 做子串判断 —— 循环外的 `_mask_roi = ...`
#    也含这个子串，会误判（本次就踩了）。改用变量名 + 位置比较才准。
check("★ ROI 的 mask 计算同样在循环**之外**",
      _src_gmr.index("_mask_roi = np.all") < _src_gmr.index("for monster_name, monster_imgs"),
      True)


print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
