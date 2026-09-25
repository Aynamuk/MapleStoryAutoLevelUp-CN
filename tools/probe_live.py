# -*- coding: utf-8 -*-
'''
只读实测探针：抓**一帧真实游戏画面**，量出 d_home / d_main 和判定结果（2026-09-13）

用途：把 v0.9 第二版「谁近走谁」的新判据，拿到**真实游戏画面**上验一遍 ——
     离线自检（verify_navigation / verify_home_route / verify_home_route_qa）用的都是
     合成图和假时钟，只有这个文件跑的是真窗口、真小地图、真定位。

★★★ 只读保证（三条铁律，改这个文件的人不许破）★★★
  1. **绝不发任何按键** —— 不 import 任何输入驱动（kb / input_driver / click_in_game_window），
     不 import hunting，不实例化 MapleStoryAutoBot 的 __init__（它会建抓帧器和键鼠驱动）。
  2. **绝不启动挂机循环** —— 只调 load_config + 三个定位函数 + 两个距离函数，
     **不调** update_cmd_by_route / update_cmd_by_mob_detection / 任何 on_frame。
     理由：一跑循环角色就会真的动起来，那不是"探针"是"代练"。
  3. **不写任何游戏/地图数据** —— 不落盘 route_home.png，不改配置。

★ 两个必须这么写的环境坑（都是实测踩过的）：
  · 抓帧只能用 `PIL.ImageGrab.grab(bbox=..., all_screens=True)` —— **本环境没装 mss**，
    import mss 直接 ModuleNotFoundError。bbox 是**屏幕绝对坐标**（不是客户区坐标）。
  · 读图只能用 `cv2.imdecode(np.fromfile(path, np.uint8), ...)` —— 项目路径带中文，
    `cv2.imread` 在中文路径上**实测静默返回 None**（不报错，最难查那类）。

用法：
    python -m tools.probe_live                       # 抓 1 帧，出报告
    python -m tools.probe_live --frames 5            # 连抓 5 帧（每帧间隔 1s，看抖动）
    python -m tools.probe_live --bbox 589,316,1971,1123
    python -m tools.probe_live --save frame.png      # 顺便把抓到的帧存下来（存到系统临时目录之外的英文路径）

输出示例：
    角色全局坐标 (99, 128)   离回正线 d_home = 0px   离主线 d_main = 6px
    判定式 d_home < d_main - 2  →  0 < 4  →  True  →  ★ 进回正
'''
import argparse
import os
import sys
import time
import types

import numpy as np

# ⚠️ 只读第一条：**不 import 任何输入驱动**。下面这一行是"探针不按键"的机器可验证证据。
_FORBIDDEN_MODULES = ("src.states.hunting", "src.utils.input_driver", "src.input_driver",
                      "pynput", "pyautogui", "mss")


def _assert_read_only():
    '''启动前先自证清白：任何输入驱动被 import 进来就**立刻停**。'''
    bad = [m for m in _FORBIDDEN_MODULES if m in sys.modules]
    if bad:
        raise RuntimeError(
            f"probe_live 是**只读**探针，但检测到这些模块已被导入：{bad}\n"
            f"        它们能发按键 / 抓别的窗口 —— 请从入口里去掉，别改这个检查。")


def activate_game_window(bbox):
    '''把游戏窗口切到前台 —— 抓帧前**必须**先做这一步。

    ⚠️ 为什么必须切（2026-09-13 实测）：
       `PIL.ImageGrab.grab(bbox=...)` 抓的是**屏幕该区域当前显示的内容**，
       不是"某个窗口的内容"。游戏窗口的 Z 序一旦被别的窗口（浏览器等）压住，
       抓到的就是那个遮挡物 —— 表现为"画面里找不到小地图"，把人往"没进游戏"
       的方向带（实测：抓到的是 3DM 网页，而游戏其实好端端开在地图里）。

    ⚠️ 为什么不用 PrintWindow（这条路试过，不行）：
       Unity/D3D 渲染的窗口用 PrintWindow（含 PW_RENDERFULLCONTENT=3）
       返回**全黑**，抓不到内容。所以只能走"切前台 + 屏幕抓取"。

    ⚠️ 副作用：会抢焦点。探针是诊断工具，可接受；但**别在挂机运行时跑**。

    Args:
        bbox: (left, top, right, bottom) 屏幕绝对坐标，用来反查匹配的窗口

    Returns:
        int | None: 切成功的 hwnd；找不到返回 None（仍继续抓帧，只是可能被遮挡）
    '''
    try:
        import win32gui
    except Exception:
        return None
    target = tuple(bbox)
    found = []

    def _cb(h, _):
        try:
            if not win32gui.IsWindowVisible(h):
                return
            r = win32gui.GetWindowRect(h)
            # 允许 2px 误差（窗口边框 / DPI 取整）
            if all(abs(r[i] - target[i]) <= 2 for i in range(4)):
                found.append(h)
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None
    if not found:
        return None
    hwnd = found[0]
    try:
        win32gui.SetForegroundWindow(hwnd)
        import time
        time.sleep(0.8)   # 等窗口真正提到最上层并完成一次渲染
    except Exception:
        pass
    return hwnd


def grab_frame(bbox, save_path=None):
    '''抓一帧游戏画面（屏幕绝对坐标）。

    ⚠️ 为什么用 PIL 而不是 mss：本环境**没装 mss**（import mss → ModuleNotFoundError）。
       项目里 capture 那套 window_capture 也要起驱动，只读探针碰不得。
    ⚠️ all_screens=True：多显示器时 PIL 默认只抓主屏，bbox 是**虚拟桌面**坐标。

    Args:
        bbox: (left, top, right, bottom) 屏幕绝对坐标
        save_path: 可选，把帧写到这个路径（**英文路径**，中文会让 cv2.imread 返回 None）

    Returns:
        np.ndarray: BGR 帧（与引擎 self.img_frame 同格式）
    '''
    from PIL import ImageGrab

    shot = ImageGrab.grab(bbox=bbox, all_screens=True)
    tmp = save_path
    if tmp is None:
        # ⚠️ 落系统临时目录（英文路径）—— 项目根目录是中文，cv2.imread 会静默失败
        tmp = os.path.join(os.environ.get("TEMP") or ".", "_probe_live_frame.png")
    shot.save(tmp)

    # ⚠️ 中文路径 / 非 ASCII 路径下 cv2.imread 返回 None 且不报错 → 一律走 imdecode
    import cv2
    buf = np.fromfile(tmp, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(
            f"抓到的帧读不出来（{tmp}）—— 检查 PIL 是否真的截到了内容。"
            f"注意：cv2.imread 在中文路径上会静默返回 None，所以这里走的是 imdecode。")
    return img


def merge(base, custom):
    '''把 custom 深度合并进 base（和 controller 的配置合并一致）。'''
    for k, v in (custom or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def new_probe_bot(cfg):
    '''造一个**只能做定位和量距离**的引擎实例。

    ⚠️ 故意**不跑** MapleStoryAutoBot.__init__：它会创建抓帧器 / 键鼠驱动 / 起线程，
       那就不叫"只读"了。这里手工补齐 load_config + 定位 + 距离需要的字段，
       全部照抄 tools/verify_load_config.py 里那份（真实跑通过）。
    '''
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
    from src.utils.common import load_yaml

    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                   debug=False, disable_viz=True)
    b.cfg = None
    b.data = load_yaml("config/config_data.yaml")
    b.monsters_info = {}
    b.img_routes = []
    b.img_route_home = None
    b.img_map = None
    b.img_route = None
    b.color_code = {}
    b.color_code_up_down = {}
    b.is_show_debug_window = False
    b.img_route_debug = None
    b.idx_routes = 0
    b.is_using_home_route = False
    b.is_terminated = False
    b.kb = None
    b.capture = None
    # ── 定位相关（只补这几个，够用且不碰驱动）──────────────────────────────
    b.img_frame = None
    b.loc_minimap = (0, 0)
    b.img_minimap = np.zeros((10, 10, 3), dtype=np.uint8)
    b.loc_player_minimap = (0, 0)
    b.loc_minimap_global = (0, 0)
    b.loc_player_global = (0, 0)
    b.t_last_minimap_update = time.time()
    _ = cfg
    return b


def locate(b, img_frame):
    '''在真实帧上跑**引擎真实的**定位三步（只读，不按键）。

       小地图位置 → 小地图上的玩家点 → 全局地图上的玩家坐标

    ⚠️ 为什么不用 get_player_location_by_nametag：名牌定位只影响**画面内**的
       loc_player（攻击框用），回正判定只吃 loc_player_global（小地图全局坐标），
       用不上那套模板，也少一处"开局没命中"的不确定性。

    Returns:
        (bool, str): (是否定位成功, 中文说明)
    '''
    from src.utils.common import (get_minimap_loc_size,
                                  get_player_location_on_minimap)
    b.img_frame = img_frame
    res = get_minimap_loc_size(img_frame)
    if res is None:
        return False, "画面里找不到小地图（多半没进游戏 / 在登录界面 / bbox 没对准窗口）"
    x, y, w, h = res
    x += 1              # 缩 1px，避免白边像素漏进小地图（引擎里同款处理）
    y += 1
    w -= 2
    h -= 2
    if w <= 0 or h <= 0:
        return False, "小地图尺寸异常（缩边后 ≤ 0）"
    b.loc_minimap = (x, y)
    b.img_minimap = img_frame[y:y + h, x:x + w]

    p = get_player_location_on_minimap(
        b.img_minimap,
        minimap_player_color=b.cfg["minimap"]["player_color"])
    if not p:
        return False, "小地图上找不到玩家黄点（检查 minimap.player_color 配置）"
    b.loc_player_minimap = p
    b.loc_player_global = b.get_player_location_on_global_map()
    return True, "定位成功"


def build_synthetic_frame(b, pos, frame_wh=(1382, 807), minimap_wh=(120, 80),
                          dot_xy=(40, 40)):
    '''（**--selftest 专用**）拼一张"像真实画面"的帧：左上角白边小地图 + 玩家黄点。

    ⚠️ 这不是真实画面，只用来证明「抓帧 → 定位 → 量 d_home/d_main」这条链路是通的 ——
       游戏没开 / 窗口对不上时没法实测，但探针本身不能是"没验过的代码"。
       真实测量一律走 grab_frame()，两条路**共用后面同一个** locate()。

    构造（照 get_minimap_loc_size 的四条结构判据拼，不是瞎糊）：
      · 外白框 (6,71) 尺寸 (mw+2, mh+2) → 左上角落点、四边纯白、内部紧贴外框（差 1px）
      · 内部贴 map.png 的一块**精确裁剪** → find_pattern_sqdiff 能原样匹配回去
      · 玩家点画在内部 (dot_xy) 处的一个 3×3 块（≥4 像素才不会被判噪声）

    ⚠️ 期望坐标为什么是 (mx + px + offx, my + py + offy)：
       引擎会先把白框缩 1px 再裁（+1），模板匹配也因此在 (mx+1, my+1) 命中（+1），
       而玩家点在缩过的图里落在 (px-1, py-1)（-1）—— 三个偏移正好抵消。

    Args:
        b: 已 load_config 的探针对象（要用它的 img_map / cfg）
        pos: 期望的角色**全局**坐标 (gx, gy)
        frame_wh: 画面尺寸（默认与游戏客户区一致 1382×807）
        minimap_wh: 小地图内部尺寸
        dot_xy: 玩家点在小地图内部的位置

    Returns:
        (np.ndarray, tuple): (合成帧 BGR, 期望的全局坐标)
    '''
    import cv2
    gx, gy = int(pos[0]), int(pos[1])
    px, py = int(dot_xy[0]), int(dot_xy[1])
    offx, offy = b.cfg["minimap"]["offset"]
    mw, mh = int(minimap_wh[0]), int(minimap_wh[1])
    img_map = b.img_map
    mh_map, mw_map = img_map.shape[:2]

    # 反推裁剪原点，让"模板匹配原点 + 玩家点 + offset"正好等于期望坐标
    mx = min(max(0, gx - px - int(offx)), max(0, mw_map - mw - 1))
    my = min(max(0, gy - py - int(offy)), max(0, mh_map - mh - 1))

    frame = np.zeros((int(frame_wh[1]), int(frame_wh[0]), 3), dtype=np.uint8)
    x0, y0 = 6, 71                                   # 外白框左上角（真实游戏就在这附近）
    frame[y0:y0 + mh + 2, x0:x0 + mw + 2] = (255, 255, 255)
    frame[y0 + 1:y0 + 1 + mh, x0 + 1:x0 + 1 + mw] = \
        img_map[my:my + mh, mx:mx + mw]              # 内部 = 全局图的一块精确裁剪

    pcol = tuple(int(c) for c in b.cfg["minimap"]["player_color"])
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            frame[y0 + 1 + py + dy, x0 + 1 + px + dx] = pcol
    return frame, (mx + px + int(offx), my + py + int(offy))


def inject_synthetic_home_line(b, pos):
    '''（**--selftest-home 专用**）在**内存里**造一条合法 L 形回正线。

    ⚠️ 只在内存里塞进 b.home_lines，**不写盘**（不碰 minimaps/ 下的 route_home.png）。
       用途：现网那条回正线是废线（0 条可用 → d_home 恒为 None → 永远"巡逻"），
       不注入就演示不出「进回正」这一半。注入后可以在同一张帧上对比两种判定。

    画法完全照规范：横段沿"比主线低 6px"那一行（= 坑底）逐像素汇聚着色，
    竖段从坑底跳回主线，终点画在主线上。
    '''
    import cv2
    from src.utils.home_route import calc_line_geom, far_segment_stats
    if b._main_pts_xy is None:
        return None
    xs, ys = b._main_pts_xy
    gx, gy = int(pos[0]), int(pos[1])
    d = np.abs(xs - gx) + np.abs(ys - gy)
    i = int(np.argmin(d))
    main_y = int(ys[i])                 # 离角色最近的那个主线像素所在行 = 平台高度
    pit_y = main_y + 6                  # 坑底（真实地形就是低 6~7px）
    corner_x = gx
    x0, x1 = gx - 7, gx + 7

    img = np.zeros((*b.img_map.shape[:2], 3), dtype=np.uint8)
    from src.utils.home_route import paint_converging_hseg
    paint_converging_hseg(img, [(x, pit_y) for x in range(x0, x1 + 1)],
                          (corner_x, pit_y), b.color_code, "walk")
    jr, jg, jb = None, None, None
    for rgb, cmd in b.color_code.items():
        if cmd.split()[2] == "jump":
            jr, jg, jb = rgb
            break
    if jr is not None:
        for y in range(main_y + 1, pit_y + 1):
            img[y, corner_x] = (jb, jg, jr)     # img 是 BGR
    gr, gg, gb = (255, 255, 0)
    img[main_y, corner_x] = (gb, gg, gr)        # goal 黄点贴在主线上

    comp = [(x, y) for y in range(img.shape[0]) for x in range(img.shape[1])
            if tuple(int(v) for v in img[y, x]) != (0, 0, 0)]
    if not comp:
        return None
    line = {
        "id": 999, "start": (x1, pit_y), "goal": (corner_x, main_y),
        "span": abs(x1 - corner_x) + abs(pit_y - main_y), "has_jump": True,
        "n_pts": len(comp),
        "far_pts": [], "cover_xs": (x0, x1), "cover_span": x1 - x0 + 1,
        "bbox": (x0, main_y, x1, pit_y),
        "xs": np.asarray([p[0] for p in comp], dtype=np.int32),
        "ys": np.asarray([p[1] for p in comp], dtype=np.int32),
        "armed": True, "disarm_t": 0.0,
    }
    b.home_lines = [line]
    b.img_route_home = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    print(f"       （内存注入）合成回正线 #{line['id']}：坑底 y={pit_y} x{x0}~{x1}"
          f" → 竖段跳回主线 y={main_y}，终点 {line['goal']}")
    return line


def main():
    _assert_read_only()
    ap = argparse.ArgumentParser(description="只读实测探针：量 d_home / d_main 和判定结果")
    ap.add_argument("--bbox", default="589,316,1971,1123",
                    help="抓帧区域 left,top,right,bottom（屏幕绝对坐标）")
    ap.add_argument("--frames", type=int, default=1, help="连抓几帧（默认 1）")
    ap.add_argument("--interval", type=float, default=1.0, help="每帧间隔秒数（默认 1.0）")
    ap.add_argument("--save", default=None, help="把第一帧存到这个英文路径（可选）")
    ap.add_argument("--selftest", action="store_true",
                    help="不抓屏，用**合成帧**跑通同一条定位链路（游戏没开时用它验探针本身）")
    ap.add_argument("--at", default="99,128",
                    help="--selftest 时把角色摆在哪个全局坐标（默认 99,128 = 坑底）")
    ap.add_argument("--selftest-home", action="store_true",
                    help="--selftest 时再**在内存里**注入一条合法 L 形回正线（不写盘）")
    args = ap.parse_args()

    bbox = tuple(int(v) for v in args.bbox.replace("，", ",").split(","))
    if len(bbox) != 4:
        raise SystemExit("--bbox 需要 4 个数：left,top,right,bottom")
    print(f"抓帧区域 bbox={bbox}（宽 {bbox[2]-bbox[0]} 高 {bbox[3]-bbox[1]}）")
    print("★ 只读探针：不发按键、不启动挂机循环。\n")

    from src.utils.common import load_yaml
    from src.utils.home_route import home_params, home_should_return

    # 活动配置方案：默认用 config_custom.yaml（出厂回退值）。
    # 若你在主界面另存过方案，改成你自己的 config/<方案名>.yaml 即可。
    cfg = merge(load_yaml("config/config_default.yaml"),
                load_yaml("config/config_custom.yaml"))
    map_name = cfg["bot"]["map"]
    print(f"配置：地图={map_name!r} 模式={cfg['bot']['mode']!r}")

    b = new_probe_bot(cfg)
    ret = b.load_config(cfg)
    if ret != 0:
        raise SystemExit(f"load_config 返回 {ret}（配置/资源有问题，见上方日志）")
    b.cfg = cfg
    print(f"主线段数={len(b.img_routes)}  可用回正线={len(b.home_lines)} 条")
    for ln in b.home_lines:
        cov = ln.get("cover_xs")
        print(f"    #{ln['id']} 起点 {ln['start']} → 终点 {ln['goal']}，"
              f"{ln['n_pts']} px，{'含跳跃' if ln['has_jump'] else '无跳跃'}，"
              f"落点覆盖 x{cov[0]}~{cov[1]}" if cov else
              f"    #{ln['id']} 起点 {ln['start']} → 终点 {ln['goal']}")

    p = home_params(cfg)
    tol = int(p["home_dist_tol"])
    print(f"\n判定式：d_home < d_main - {tol}（相等时主线优先）\n")

    if args.selftest:
        pos = tuple(int(v) for v in args.at.replace("，", ",").split(","))
        if args.selftest_home:
            inject_synthetic_home_line(b, pos)
        print(f"★ --selftest：不抓屏，用合成帧（角色摆到全局坐标 {pos}）跑同一条链路\n")

    # ── 抓帧前先把游戏切到前台（否则 ImageGrab 抓到的是遮挡物）─────────────
    # 实测（2026-09-13）：游戏窗口的 Z 序被浏览器压住时，ImageGrab 抓到 3DM 网页，
    # 表现成"画面里找不到小地图"，把人往"没进游戏"的方向带 —— 实际游戏好端端
    # 开在地图里。selftest 走合成帧，不需要切前台。
    if not args.selftest:
        _hwnd = activate_game_window(bbox)
        print(f"已把游戏窗口切到前台：hwnd={_hwnd}" if _hwnd
              else "⚠️ 没找到与 bbox 匹配的窗口，直接抓帧（可能被遮挡）")
        print()

    rows = []
    for i in range(max(1, args.frames)):
        if i:
            time.sleep(max(0.0, args.interval))
        if args.selftest:
            img, _expect = build_synthetic_frame(b, pos)
            if i == 0 and args.save:
                import cv2
                cv2.imwrite(args.save, img)
        else:
            img = grab_frame(bbox, save_path=(args.save if i == 0 else None))
        ok, why = locate(b, img)
        if not ok:
            print(f"  第 {i+1} 帧：{why}")
            continue
        d_main, seg_i = b._main_dist()
        d_home, line = b._home_dist()
        verdict = home_should_return(d_home, d_main, tol)
        rows.append((b.loc_player_global, d_home, d_main, verdict, line, seg_i))
        got = b.loc_player_global
        ln_txt = f"回正线 #{line['id']}" if line else "（没有可用回正线）"
        if args.selftest:
            drift = abs(got[0] - _expect[0]) + abs(got[1] - _expect[1])
            print(f"          定位自检：期望 {_expect}，实测 {got}，偏差 {drift}px"
                  f"  →  {'✅ 链路通' if drift <= 2 else '❌ 定位链路有问题'}")
        seg_txt = f"第 {seg_i + 1}/{len(b.img_routes)} 段" if seg_i is not None else "—"
        print(f"  第 {i+1} 帧：角色全局坐标 {got}")
        print(f"          离回正线 d_home = {d_home}  [{ln_txt}]")
        print(f"          离主线   d_main = {d_main}  [最近在 {seg_txt}]")
        if d_home is None or d_main is None:
            print(f"          判定：量不出距离 → **按主线优先处理**（不进回正）")
        else:
            print(f"          判定：{d_home} < {d_main} - {tol} = {d_main - tol} "
                  f"→ {verdict}  →  {'★ 进回正' if verdict else '主线巡逻'}")
        print()

    if not rows:
        raise SystemExit("一帧都没定位成功 —— 先看上面每一帧的原因。")

    if len(rows) > 1:
        xs = [r[0][0] for r in rows]
        ys = [r[0][1] for r in rows]
        vs = [r[3] for r in rows]
        print(f"汇总 {len(rows)} 帧：坐标 x {min(xs)}~{max(xs)} / y {min(ys)}~{max(ys)}；"
              f"判定一致 = {len(set(vs)) == 1}")
    print("\n提示：想看角色在坑里/平台上的差别，手动走到坑底再跑一次本脚本对比。")


if __name__ == "__main__":
    main()
