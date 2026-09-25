'''
路线录制器的离线验证器（不开游戏、不用手点键盘）。

为什么要它
----------
`tools/routeRecorder.py` 是**交互式**的：要开游戏窗口、用 cv2 开两个调试窗口、
还要人按 F1~F4、按方向键走位。这种工具"手动点一遍看看"根本验不出问题 ——
点位对不对、地图拼接有没有错位、路线画在不在正确坐标上，全靠眼睛扫一眼，
而且下次改动又得重点一遍。

所以这里把它的**真实 run_once()** 拿来离线跑：
  · 帧从 debug/ 里已有的抓帧喂进去（不连游戏窗口）
  · cv2.imshow / waitKey / destroyAllWindows 换成空操作（不弹窗）
  · KeyBoardListener 换成假的（由命令行指定"一直按着什么键"）
**不重写任何录制逻辑** —— 测的就是线上那段代码。

用法
----
    python -m tools.verify_route                 # 用最近 20 张抓帧
    python -m tools.verify_route --frames 5
    python -m tools.verify_route --glob "debug/capture_raw_2026-09-10_18-38-*"
    python -m tools.verify_route --keys right    # 一直按右键（会画路线）
    python -m tools.verify_route --keys ""       # 不按键（不画线）
'''
# Standard import
import os
import sys
import glob
import time
import argparse

import numpy as np
import cv2

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.common import (                       # noqa: E402
    load_yaml, override_cfg, imread_unicode, imwrite_unicode,
    crop_frame_to_client, get_minimap_loc_size,
    get_player_location_on_minimap, load_image,
)
from src.utils.paths import resource_path            # noqa: E402

# 离线复用的临时地图目录名（跑完会清掉）
TMP_MAP = "_verify_tmp"
OUT_DIR = os.path.join(REPO_ROOT, "debug", "analysis")


# ----------------------------------------------------------------------------
# 假替身：把交互部分换成可控的
# ----------------------------------------------------------------------------
class FakeKb:
    """顶替 KeyBoardListener。只需要 run_once 会读的那几个属性。"""

    def __init__(self, keys):
        self.key_pressing = list(keys)
        self.is_pressed_func_key = [False] * 12
        self.t_func_key = [time.time()] * 12


class FakeCapture:
    """顶替 GameWindowCapturor。get_frame() 返回当前这一帧的原始帧。"""

    def __init__(self):
        self.raw = None
        self.n = 0

    def get_frame(self):
        self.n += 1
        return self.raw


class _NullGui:
    """把 cv2 的窗口调用变成空操作（不弹窗）。"""

    def __enter__(self):
        self._imshow = cv2.imshow
        self._waitKey = cv2.waitKey
        self._destroy = cv2.destroyAllWindows
        self._rect = cv2.rectangle
        cv2.imshow = lambda *a, **k: None
        cv2.waitKey = lambda *a, **k: 255      # 不是 'q'
        cv2.destroyAllWindows = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        cv2.imshow = self._imshow
        cv2.waitKey = self._waitKey
        cv2.destroyAllWindows = self._destroy
        return False


def build_recorder(cfg, args, keys):
    """
    照 RouteRecorder.__init__ 把状态铺好，但**不做**那三件有副作用的事：
    建目录（要 input 确认）、起键盘线程、起抓帧线程。
    """
    from src.input.KeyBoardListener import KeyBoardListener
    from tools.routeRecorder import RouteRecorder

    rec = object.__new__(RouteRecorder)
    rec.args = args
    rec.cfg = cfg
    rec.idx_routes = 0
    rec.fps = 0
    rec.is_first_frame = True
    rec.is_enable = True
    # ⚠️ 必须自己 arm（2026-09-16 补）：09-15 加了【开始录】闸门 ——
    #    主循环画线的前提是 `self.is_enable and self._armed and action != ""`
    #    （见 routeRecorder.arm_recording）。本脚本是闸门之前写的、从不 arm，
    #    于是**一个像素都不会画**，而结尾那句提示还把它解释成
    #    "没有模拟按键时属正常" → 画线这段实际上**从没被验证过**（恒过）。
    #    这里直接置位 = 模拟"用户已经点过【开始录】"的状态。
    rec._armed = True
    rec.loc_minimap = (0, 0)
    rec.loc_player = (0, 0)
    rec.loc_player_minimap = (0, 0)
    rec.loc_minimap_global = (0, 0)
    rec.loc_player_global = (0, 0)
    rec.loc_player_global_last = None
    rec.frame = None
    rec.img_frame = None
    rec.img_frame_debug = None
    rec.img_route = None
    rec.img_route_debug = None
    rec.img_minimap = None
    rec.img_map = None
    # 与 __init__ 一致：给了 --map 就用已有地图（此时不再往地图里写新内容）
    if getattr(args, "map", ""):
        rec.img_map = load_image(args.map)
    rec.t_last_frame = time.time()
    rec.t_last_draw_blob = time.time() - 999      # 允许立刻画 blob
    rec.fps_limit = cfg["system"]["fps_limit_route_recorder"]

    # 颜色码表（与 __init__ 一致）
    rec.color_code = {
        tuple(map(int, k.split(','))): v
        for k, v in cfg["route"]["color_code"].items()
    }
    rec.color_code.update({
        tuple(map(int, k.split(','))): v
        for k, v in cfg["route"]["color_code_up_down"].items()
    })

    rec.kb = FakeKb(keys)
    rec.capture = FakeCapture()

    filled = fill_missing_state(rec, RouteRecorder,
                                {"self": rec, "time": time, "cfg": cfg, "args": args, "os": os},
                                label="RouteRecorder")
    if filled:
        print("  ⚠️ build_recorder 手写的属性清单已过期，已按 __init__ 自动补上 %d 个：" % len(filled))
        print("     " + ", ".join("%s=%r" % (n, v) for n, v in filled))
        print("     （请顺手把上面这些同步进 build_recorder 的清单，别让脚本靠自动兜底活着）")

    # ⚠️ FakeKb 是**手写**的桩，同样会过期：2026-09-16 引擎加了
    #    KeyBoardListener.quit_requested（实例属性，不是方法），桩里没有 →
    #    run_once 里的 `self.kb.quit_requested` 直接 AttributeError。
    #    同一个病根（手写清单），同一套修法：从真类 __init__ 自动补。
    kb_filled = fill_missing_state(rec.kb, KeyBoardListener,
                                   {"self": rec.kb, "time": time, "cfg": cfg},
                                   label="KeyBoardListener")
    if kb_filled:
        print("  ⚠️ FakeKb 手写的属性清单已过期，已按 KeyBoardListener.__init__ 补上 %d 个："
              % len(kb_filled))
        print("     " + ", ".join("%s=%r" % (n, v) for n, v in kb_filled))
    return rec


def fill_missing_state(obj, cls, namespace, label="对象"):
    """把 `cls.__init__` 会设、但本脚本的**手写桩**没写到的实例属性补上，并打印补了什么。

    ⚠️ 为什么必须有这个（2026-09-15 / 09-16 各踩一次，两个不同的桩）：
    本脚本的桩（`build_recorder` 的属性清单、`FakeKb`）都是**手写**的，
    引擎每加一个实例属性就漏一个：
      · 2026-09-15 引擎加了 `RouteRecorder._first_frame_logged` → 脚本崩在第 1 帧；
      · 2026-09-16 引擎加了 `KeyBoardListener.quit_requested` → `self.kb.quit_requested`
        直接 AttributeError。
    两次都是**静默截断**：只打十来行、没有总结行、`[OK]`/`[FAIL]` 都是 0，看着像"没跑"。
    手写清单在"方法"上已经踩过 8 次（见 skill maple-bot-offline-verify），属性同理。

    ⇒ 从 `__init__` 源码里**自动**取属性名，再**在受限环境里把它的初值真求值出来**。

    ⚠️ 为什么不按名字猜初值：猜必错，而且两个反例方向相反 ——
      · `_t_record_start` 初值是 **None**（代码里有 `if ... is None:` 判断），
        猜成 0.0 → 等待时长算成 17 亿秒 → 宽限期瞬间过期 → 直接 `sys.exit(2)`；
      · `_t_frame_wait_start` 初值是 **time.time()**，猜成 None →
        `float - None` 直接 TypeError。
    两次症状都跟真 bug 长得完全不一样，白查半天。

    求值用 `eval` + **只放开几个纯函数**的 builtins：表达式里只有字面量、
    `time.time()`、对 `self.cfg`/`self.args` 的读取、`bool()/int()` 这类转换和推导式。
    求不出来才退回字面量，最后才给 None 并标成"(猜的)"。

    返回值是 [(名字, 补进去的值)]，调用方**必须打印**出来 ——
    自动兜底如果静默，就成了"恒过的测试"，比没测试更危险。
    """
    import ast
    import inspect
    import textwrap

    # ⚠️ 必须用 dedent 而不是 inspect.cleandoc：cleandoc **不动第一行**（def 那行），
    #    却会按"后续行的最小缩进"去剥，于是 docstring 的 `'''` 被剥到第 0 列 →
    #    IndentationError: expected an indented block。dedent 对所有行一视同仁。
    src = textwrap.dedent(inspect.getsource(cls.__init__))
    assigned = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == 'self'):
                    assigned.setdefault(t.attr, node.value)

    safe_builtins = {"bool": bool, "int": int, "float": float, "str": str,
                     "tuple": tuple, "list": list, "dict": dict,
                     "len": len, "getattr": getattr, "abs": abs}

    filled = []
    for name, value_node in sorted(assigned.items()):
        if hasattr(obj, name):
            continue
        try:
            code = compile(ast.Expression(value_node), "<%s.__init__>" % cls.__name__, "eval")
            val = eval(code, {"__builtins__": safe_builtins}, dict(namespace))
        except Exception:
            try:
                val = ast.literal_eval(value_node)
            except Exception:
                setattr(obj, name, None)
                filled.append((name + "(猜的)", None))
                continue
        setattr(obj, name, val)
        filled.append((name, val))
    return filled


def collect_frames(limit, pattern):
    if pattern:
        paths = sorted(glob.glob(pattern))
    else:
        paths = sorted(glob.glob(os.path.join(REPO_ROOT, "debug", "capture_raw_*.png")))
        paths = paths[-limit:] if limit else paths
    return paths


def test_expand(cfg, rec_args, keys, frame_path):
    """
    专门验「地图扩容」这条分支 —— 它用真实数据摸不到，只能人为制造。

    为什么摸不到：实测国服小地图是**整图视图**（不随人物滚动），
    所以地图画布永远不需要扩容，`ensure_img_map_capacity` 一直是死代码。
    但大地图上小地图可能是滚动的，那条路上有两个"平时看不见"的坐标 bug，
    不验就等于埋着。

    做法：拿真实帧的小地图内容，人为塞进一张只留 8px 边距的画布
    （而不是正常的 30px）。第一帧匹配必然落在 (8,8)，
    于是四个方向都触发扩容（各 22px）—— 真实的坐标平移就出现了。
    然后检查三件事：
      ① 路线画布是否跟着地图一起扩（不然路线和 map.png 会错位）
      ② 路线画布上的既有内容是否跟着平移（用一枚"追踪像素"验）
      ③ 扩容后有没有把内容写到错误位置（旧坐标没更新的经典后果）
    """
    print()
    print("=" * 96)
    print(" 【专项】地图扩容分支验证 —— 人为把画布做小，逼出扩容")
    print("=" * 96)

    pad_normal = cfg["route_recoder"]["map_padding"]
    pad_small = 8
    TRACER = (7, 7, 7)

    raw = imread_unicode(frame_path, cv2.IMREAD_COLOR)
    if raw is None:
        print(f" 读不到帧：{frame_path}")
        return 1
    client, _ = crop_frame_to_client(raw, tuple(cfg["game_window"]["size"]),
                                     cfg["game_window"]["title_bar_height"])
    found = get_minimap_loc_size(client)
    if found is None:
        print(" 这帧里没找到小地图，换一帧再试。")
        return 1
    mx, my, mw, mh = [int(v) for v in found]
    loc = (mx + 2, my + 2)                     # 与录制器一样丢弃 1px 边框
    w, h = mw - 4, mh - 4
    mm = client[loc[1]:loc[1] + h, loc[0]:loc[0] + w].copy()
    print(f" 小地图（录制器视角）：loc={loc} 尺寸 {w}x{h}")

    rec = build_recorder(cfg, rec_args, keys)
    canvas = np.zeros((h + 2 * pad_small, w + 2 * pad_small, 3), np.uint8)
    canvas[pad_small:pad_small + h, pad_small:pad_small + w] = mm
    rec.img_map = canvas
    rec.img_route = canvas.copy()
    rec.img_route[2:5, 2:5] = TRACER            # 只在路线画布上留一枚标记
    rec.img_route_debug = rec.img_route.copy()
    rec.loc_minimap = loc
    rec.img_minimap = mm
    rec.is_first_frame = False                  # 跳过首帧初始化，直接用造好的画布

    before_map = rec.img_map.shape[:2]
    before_route = rec.img_route.shape[:2]
    print(f" 扩容前：map={before_map}  route={before_route}  "
          f"（边距只有 {pad_small}px，而正常要求 {pad_normal}px）")

    rec.capture.raw = raw
    with _NullGui():
        try:
            rec.run_once()
        except Exception as e:
            print(f" 【异常】扩容时崩了：{type(e).__name__}: {e}")
            return 1

    after_map = rec.img_map.shape[:2]
    after_route = rec.img_route.shape[:2]
    el = pad_normal - pad_small
    print(f" 扩容后：map={after_map}  route={after_route}  （预期各边 +{el}）")
    print()

    ok = True

    # ① 画布同步
    if after_map == after_route and after_map == (before_map[0] + 2 * el, before_map[1] + 2 * el):
        print(f" ① 路线画布与地图同步扩容：OK（两边都是 {after_map}）")
    else:
        ok = False
        print(f" ① 失败：map={after_map}  route={after_route}（预期两边都变 "
              f"{(before_map[0]+2*el, before_map[1]+2*el)}）")

    # ② 既有内容跟着平移（追踪像素应从 (2,2) 移到 (2+el, 2+el)）
    tr = np.all(rec.img_route == TRACER, axis=2)
    ys_t, xs_t = np.where(tr)
    exp = (2 + el, 2 + el)
    if len(xs_t) and (int(xs_t.min()), int(ys_t.min())) == exp:
        print(f" ② 既有路线内容跟着平移：OK（追踪像素 {len(xs_t)} 个，落在 {exp}）")
    else:
        ok = False
        got = None if len(xs_t) == 0 else (int(xs_t.min()), int(ys_t.min()))
        print(f" ② 失败：追踪像素在 {got}，预期 {exp}（内容没跟着平移 → 路线会与地图错位）")

    # ③ 没有把内容写到错误位置（旧坐标未更新的话，刚扩出来的黑边里会出现错位副本）
    region = np.zeros(after_map, bool)
    region[pad_normal:pad_normal + h, pad_normal:pad_normal + w] = True
    outside = np.any(rec.img_map != 0, axis=2) & (~region)
    n_out = int(outside.sum())
    if n_out == 0:
        print(" ③ 内容落点：OK（内容严格落在坐标系平移后的正确区域，黑边里没有错位副本）")
    else:
        ok = False
        ys_o, xs_o = np.where(outside)
        print(f" ③ 失败：正确区域外有 {n_out} 个像素有内容 "
              f"（x {xs_o.min()}..{xs_o.max()} y {ys_o.min()}..{ys_o.max()}）"
              f" → 说明扩容后仍用旧坐标写图，贴出了错位副本")

    print()
    print(f" 【{'OK' if ok else '失败'}】地图扩容分支{'通过' if ok else '有问题'}")
    print()
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="路线录制器离线验证（跑真实 run_once，不连游戏）")
    ap.add_argument("--frames", type=int, default=20, help="用最近 N 张抓帧（默认 20）")
    ap.add_argument("--glob", default=None, help="直接用 glob 指定帧（优先于 --frames）")
    ap.add_argument("--keys", default="right",
                    help="模拟一直按住什么键：right/left/up/down/space/e/空串（默认 right）")
    ap.add_argument("--map", default="", help="传入已有地图路径（默认不传=新建地图模式）")
    ap.add_argument("--save", action="store_true",
                    help="顺带模拟按 F3（存路线）与 F4（存地图），测写盘路径")
    ap.add_argument("--test-expand", action="store_true",
                    help="专项：人为逼出地图扩容分支，验证扩容时坐标系是否一致")
    args = ap.parse_args()

    if not args.keys:
        args.keys = []
    elif args.keys in ("up", "down", "left", "right", "space", "e"):
        args.keys = [args.keys]
    else:
        args.keys = args.keys.split(",")

    paths = collect_frames(args.frames, args.glob)
    if not paths:
        print("没找到抓帧。先跑 python -m tools.grab_frame 抓几张。")
        return 1

    cfg = override_cfg(load_yaml("config/config_default.yaml"),
                       load_yaml("config/config_custom.yaml"))

    if args.test_expand:
        return test_expand(cfg,
                           argparse.Namespace(cfg="custom", map="", new_map=TMP_MAP),
                           args.keys, paths[-1])

    # args 要给 run_once 用：.map（空=新建地图）与 .new_map（落盘目录）
    rec_args = argparse.Namespace(cfg="custom", map=args.map, new_map=TMP_MAP)
    map_dir = resource_path(os.path.join("minimaps", TMP_MAP))
    os.makedirs(map_dir, exist_ok=True)

    rec = build_recorder(cfg, rec_args, args.keys)

    gw = cfg["game_window"]
    cw, ch = gw["size"][1], gw["size"][0]
    thres = cfg["route"]["search_range"]
    print()
    print("=" * 96)
    print(f" 路线录制器离线验证：{len(paths)} 帧，模拟按键 = {args.keys or '(不按键)'}")
    print(f" 客户区尺寸 = {cw}x{ch}    写入目录 = minimaps/{TMP_MAP}（跑完清理）")
    print("=" * 96)
    print(f"{'帧':<24}{'小地图':>10}{'玩家点':>11}{'玩家全局':>11}"
          f"{'map':>11}{'画线':>8}{'画线范围':>16}{'扩容':>8}")
    print("-" * 96)

    rows = []
    n_crop_fail = 0
    route_mismatch = []
    reset_events = []
    reset_stray = []

    with _NullGui():
        for i, p in enumerate(paths, 1):
            raw = imread_unicode(p, cv2.IMREAD_COLOR)
            name = os.path.basename(p).replace("capture_raw_", "").replace(".png", "")
            if raw is None:
                print(f"{name:<26}{'读不到':>18}")
                continue
            client, msg = crop_frame_to_client(raw, tuple(gw["size"]), gw["title_bar_height"])
            if client is None:
                n_crop_fail += 1
                print(f"{name:<26}  尺寸不符，跳过：{msg}")
                continue

            rec.capture.raw = raw
            rec.is_first_frame = (i == 1)      # 第一帧走初始化分支
            idx_before = rec.idx_routes
            try:
                rec.run_once()
            except Exception as e:
                print(f"{name:<26}  【异常】{type(e).__name__}: {e}")
                return 1
            if rec.idx_routes > idx_before:
                reset_events.append(name)      # 这一帧存了路线并重置了画布
                pending_reset = True
            else:
                pending_reset = False

            # ---- 采集诊断 ----
            mm = rec.loc_minimap
            mm_size = None if rec.img_minimap is None else rec.img_minimap.shape[:2]
            dot = rec.loc_player_minimap
            pg = rec.loc_player_global
            map_shape = None if rec.img_map is None else rec.img_map.shape[:2]
            route_shape = None if rec.img_route is None else rec.img_route.shape[:2]

            # ---- 画的线：统计颜色码像素。第一帧时 img_route 已被 remove_color_code_pixels
            #      清空过，所以之后出现的颜色码像素 = 录制过程中画上去的路线。
            drawn_px, drawn_box = 0, None
            if rec.img_route is not None:
                m = np.zeros(rec.img_route.shape[:2], np.uint8)
                for rgb in rec.color_code.keys():
                    bgr = (rgb[2], rgb[1], rgb[0])
                    m |= np.all(rec.img_route == bgr, axis=2).astype(np.uint8)
                drawn_px = int(m.sum())
                if drawn_px:
                    ys_, xs_ = np.where(m > 0)
                    drawn_box = (int(xs_.min()), int(ys_.min()),
                                 int(xs_.max()), int(ys_.max()))

            # ★ 关键一致性检查：地图扩容后，路线画布必须跟着扩容，
            #   否则后续 cv2.line 会按全局坐标画进一张小画布 → 线画错位置或被裁掉。
            if map_shape and route_shape and map_shape != route_shape:
                route_mismatch.append((name, map_shape, route_shape))

            grew = ""
            if rows and map_shape and rows[-1]["map_shape"] and map_shape != rows[-1]["map_shape"]:
                grew = "★扩容"

            rows.append(dict(name=name, dot=dot, pg=pg, drawn_px=drawn_px,
                             drawn_box=drawn_box,
                             map_shape=map_shape, route_shape=route_shape))
            if pending_reset:
                reset_stray.append((name, drawn_px))

            box_s = "-" if drawn_box is None else \
                f"{drawn_box[0]},{drawn_box[1]}~{drawn_box[2]},{drawn_box[3]}"
            mms = "-" if mm_size is None else f"{mm_size[1]}x{mm_size[0]}"
            print(f"{name:<24}{mms:>10}{str(dot):>11}{str(pg):>11}"
                  f"{str(map_shape):>11}{drawn_px:>8}{box_s:>16}{grew:>8}")

            # 顺带测写盘路径：F3 → route{N}.png，F4 → map.png
            if args.save:
                if i == max(1, len(paths) // 2):
                    rec.kb.is_pressed_func_key[2] = True       # F3
                elif i == len(paths):
                    rec.kb.is_pressed_func_key[3] = True       # F4
                    rec.run_once()

    # ------------------------------------------------------------------
    print("-" * 96)
    print(f" 抓到帧 {len(paths)}，尺寸不符跳过 {n_crop_fail}，实际跑过 {len(rows)} 帧")
    if route_mismatch:
        print()
        print(" 【失败】路线画布与地图尺寸不一致（地图扩容时路线没跟着扩）：")
        for name, m, r in route_mismatch[:6]:
            print(f"     {name}  map={m}  route={r}   差 {m[0]-r[0]}x{m[1]-r[1]}")
    else:
        print(" 【OK】路线画布与地图尺寸始终一致")

    if rows:
        xs = [r["pg"][0] for r in rows if r["pg"]]
        ys = [r["pg"][1] for r in rows if r["pg"]]
        if xs:
            print(f" 玩家全局坐标 x {min(xs)}..{max(xs)}（跨 {max(xs)-min(xs)}px）"
                  f"　y {min(ys)}..{max(ys)}（跨 {max(ys)-min(ys)}px）")
        dots = [r["dot"] for r in rows if r["dot"]]
        print(f" 小地图玩家点：{len(dots)}/{len(rows)} 帧取到"
              + (f"　点 x {min(d[0] for d in dots)}..{max(d[0] for d in dots)}" if dots else ""))

        # 画的线：数量应逐帧增长，且范围应覆盖玩家轨迹
        last = rows[-1]
        if last["drawn_px"]:
            bx = last["drawn_box"]
            print(f" 已画路线像素：{last['drawn_px']} 个，范围 x {bx[0]}..{bx[2]}"
                  f"　y {bx[1]}..{bx[3]}")
            if xs and ys:
                pgx = (min(xs), max(xs))
                pgy = (min(ys), max(ys))
                ok_x = bx[0] >= pgx[0] - 3 and bx[2] <= pgx[1] + 3
                ok_y = bx[1] >= pgy[0] - 3 and bx[3] <= pgy[1] + 3
                print(f"   与玩家轨迹范围对照：x 轨迹 {pgx} / 线 {bx[0]}..{bx[2]}"
                      f"　y 轨迹 {pgy} / 线 {bx[1]}..{bx[3]}"
                      f"　→ {'【OK】线落在轨迹上' if (ok_x and ok_y) else '【注意】线超出轨迹范围'}")
                # 逐帧递增检查（存盘重置会让计数归零，那是预期行为）
                seq = [r["drawn_px"] for r in rows]
                bad = [rows[i]["name"] for i in range(1, len(seq)) if seq[i] < seq[i - 1]]
                if not bad:
                    print("   画线像素逐帧单调不减：OK")
                else:
                    print(f"   画线像素有回退（应为 F3 存盘后重置画布）：{bad}")
        else:
            print(" 已画路线像素：0 —— 没有模拟按键（--keys）时属正常")

        # 存盘后重置的画布必须是"干净的"：
        # 实测客户区里本来就有 277~522 个像素恰好等于某种路线颜色（地图上的蓝色水系、
        # 黄色标记等）。若重置时直接 copy img_map 而不清这些像素，引擎按路线走时
        # 会把它们当成指令。
        if args.save:
            print()
            if not reset_events:
                print(" 【注意】模拟了 F3 但没有任何路线被保存 —— 存盘路径没走到，检查判据")
            elif all(px == 0 for _, px in reset_stray):
                print(f" 【OK】存盘重置后的画布是干净的"
                      f"（{len(reset_events)} 次重置，杂散颜色像素均为 0）")
            else:
                print(f" 【失败】存盘重置后画布残留颜色码像素：{reset_stray}"
                      f" → 引擎会把它们当成路线指令")


    # ------------------------------------------------------------------
    # 落盘留证
    os.makedirs(OUT_DIR, exist_ok=True)
    if rec.img_map is not None:
        p1 = os.path.join(OUT_DIR, "route_verify_map.png")
        imwrite_unicode(p1, rec.img_map)
        print(f" 累计地图已存：{os.path.relpath(p1, REPO_ROOT)}  ({rec.img_map.shape[1]}x{rec.img_map.shape[0]})")
    if rec.img_route is not None:
        p2 = os.path.join(OUT_DIR, "route_verify_route.png")
        big = cv2.resize(rec.img_route, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
        imwrite_unicode(p2, big)
        print(f" 路线图已存（放大3倍）：{os.path.relpath(p2, REPO_ROOT)}")

    # 检查录制器实际写出的文件
    saved = sorted(glob.glob(os.path.join(map_dir, "*.png")))
    print(f" 录制器实际写盘的文件：{[os.path.basename(s) for s in saved] or '（无）'}")

    print()
    print(f" 提示：验证用的临时目录 minimaps/{TMP_MAP} 需要手动删（脚本不自动删，避免误伤）")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
