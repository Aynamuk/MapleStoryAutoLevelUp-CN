# -*- coding: utf-8 -*-
'''
离线自检：v0.9 手绘回正线面板 + 界面体检口径（2026-09-13 **第二版**）

为什么**另开一个文件**而不是塞进 verify_home_route.py：
    verify_home_route 是引擎/数据层的核心自检，正被并行验收；
    手绘面板（tools/homeRouteDrawer.py）与界面体检（src/utils/route_audit.py）
    是 T03/T04 的收尾件，混进去会互相干扰。

⚠️ v0.9 **第二版**（方案重构）：**落点锚（127,0,127）整个删除**。本自检同步改写：
    · 不再有 anchor / D / R / away 这些第一版概念；
    · 落点改成「起始段覆盖跨度」（cover_span），接住落点的判定改成
      「谁近走谁」（d_home < d_main - tol）；
    · 画法改成 **L 形**（横段逐像素汇聚着色 + 竖段跳回主线）；
    · 撞色自检从"数锚点像素"改成"数回正线像素总数"。

重点覆盖（team-lead 指派 + 我自己加的回归）：
    1. 4 倍画布坐标反算 (400,300)→(100,75) / (403,300)→(101,75)
       —— **专门防 `//` 截断**（用整除会把 403 算成 100，短线上这 1px
          就是"接得上/接不上"的差别）
    2. 落点区间蓝带 → 矩形展开（坑底两端都要算得到）
    3. 覆盖率模拟：坑底 15 个落点全部接住；主线上的点**不**进回正
    4. 落盘回放校验能拦住被 mask_route_colors 吃掉的像素（换图撞色场景）
    5. 一张图多条回正线：逐条列出、逐条判废、废的不连坐
    6. L 形横段**逐像素汇聚着色**（左半 right、右半 left，都指向拐角）
    7. 判废：只画竖线（cover_span=1）判废；终点不贴主线判废
    8. 界面体检（route_audit）与引擎加载（_scan_home_lines）**口径一致**
    9. 终点必须**显式**点出来（隐式「最后一下即终点」已废止）
   10. 落点可以**填坐标**（需求方不开游戏也能画：坐标从引擎日志里抄）
   11. 文案（列表行 / 覆盖率）
   12. 纯函数契约（参数回落 / 撤销裁剪 / 旧锚迁移）

不开游戏、不需要显示器（只导入面板模块拿纯函数，不创建 QApplication）。

用法：
    python -m tools.verify_home_drawer
'''
import inspect
import logging
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

from src.utils.common import load_image, imwrite_unicode
from src.utils.logger import logger as PROJ_LOGGER
from src.utils.home_route import (
    color_code_maps, render_home_image, screen_to_map, manhattan, draw_params,
    home_params, pick_color, paint_converging_hseg, corner_index,
    split_components, goal_codes, calc_line_geom, far_segment_stats,
    validate_home_line, home_pixel_clash, wipe_legacy_anchor, simulate_coverage,
    collect_pixels, calc_jump_band, jump_band_params, hseg_pixel_color,
    DEFAULT_JUMP_BAND_PARAMS,
)
from tools.homeRouteDrawer import (
    audit_components, verify_saved_image, line_summary, coverage_text,
    mask_route_all, load_merged_cfg, HomeRouteDrawer, open_drawer,
    make_blob, save_gate_error, trim_actions, band_to_pts,
    GOAL_SEGMENT_ACTION, ACTIONS, GRID_STEP,
    parse_coord_text, coord_error, jump_off_platform,
)

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def check_true(name, got):
    check(name, bool(got), True)


class capture_log:
    '''临时挂一个 handler 抓 logger 输出（断言"这条提示到底有没有打出来"）。

    ⚠️ 本文件【17】用到它：`_jump_band` 内部有 `except → 返回 None` 的兜底。
       没有这个抓日志，裸实例少塞一个字段时会**静默返回 None**，
       用例就变成"自己骗自己绿了"。
    '''

    def __init__(self, logger=None, level=logging.WARNING):
        raw = logger or PROJ_LOGGER
        self.logger = getattr(raw, "_logger", raw)
        self.level = level
        self.msgs = []

    def __enter__(self):
        self.h = logging.Handler()
        self.h.emit = lambda rec: self.msgs.append(rec.getMessage())
        self.h.setLevel(self.level)
        self.logger.addHandler(self.h)
        return self.msgs

    def __exit__(self, *exc):
        self.logger.removeHandler(self.h)
        return False


# ══════════════════════════════════════════════════════════════════════
# 配置与色码（与 config_default.yaml 第二版保持一致）
# ══════════════════════════════════════════════════════════════════════
COLOR_CODE = {
    "255,0,0": "left none none",
    "0,0,255": "right none none",
    "255,127,0": "left none jump",
    "0,255,255": "right none jump",
    "127,255,0": "none down jump",
    "255,0,255": "none none jump",
    "255,255,0": "none none goal",
}
COLOR_CODE_UP_DOWN = {"127,127,127": "none up none", "255,255,127": "none down none"}

#: 第一版落点锚色 —— 第二版**已删除**，只在旧图迁移里用到（这里专门验它不进色码表）
LEGACY_ANCHOR_RGB = (127, 0, 127)

CFG = {
    "bot": {"attack": "directional", "mode": "normal"},
    "route": dict(
        {"search_range": 10, "rescue_range": 40,
         "color_code": COLOR_CODE,
         "color_code_up_down": COLOR_CODE_UP_DOWN},
        **{
            # ── 第二版回正专用参数（没有 home_anchor_* / home_return_* 了）──────
            "home_dist_tol": 2, "home_min_return_span": 5,
            "home_min_cover_span": 5, "home_cover_far_tol": 2,
            "home_goal_mainline_tol": 1, "home_min_leave": 0, "home_min_frames": 0,
            "home_timeout": 8, "home_reenter_lock": 10, "home_rearm_margin": 4,
            "home_rearm_timeout": 30,
        }),
    "route_recoder": {
        "home_draw_scale": 4, "home_draw_scale_min": 3, "home_draw_scale_max": 6,
        "home_draw_goal_radius": 1,
    },
    "watchdog": {"timeout": 10, "range": 10, "attack_grace": 5},
}

CC, CC_UD = color_code_maps(CFG)
#: pick_color 要查 up/down 那两个色 → 传**合并后**的表（与手绘面板一致）
MERGED_CC = dict(CC)
MERGED_CC.update(CC_UD)
ROUTE_CODES = set(CC) | set(CC_UD)
GOAL_RGB = (255, 255, 0)

W, H = 268, 187
SHAPE = (H, W, 3)

#: 真实地形（设计 3.1）：主线（平台）y=122, x∈[92,106]；坑底 y=128，比主线低 6px
MAIN_Y, MAIN_X0, MAIN_X1 = 122, 92, 106
FLOOR_Y = 128


def _bgr(rgb):
    """RGB → BGR 元组（cv2 画图和落盘都用 BGR）。"""
    return (int(rgb[2]), int(rgb[1]), int(rgb[0]))


def _make_mainline(y=MAIN_Y, x0=MAIN_X0, x1=MAIN_X1):
    """造一条主线（BGR）：蓝色向右走 + 终点黄点。"""
    img = np.zeros(SHAPE, dtype=np.uint8)
    cv2.line(img, (x0, y), (x1, y), _bgr((0, 0, 255)), thickness=1)   # 蓝 = 向右走
    cv2.circle(img, (x1, y), 2, _bgr(GOAL_RGB), thickness=-1)         # 终点黄点
    return img


def _mainline_rgb(main_bgr, img_map_bgr=None):
    """主线 → 已 mask 的 RGB（与引擎实际看到的一致）。"""
    base = img_map_bgr if img_map_bgr is not None else np.zeros(SHAPE, dtype=np.uint8)
    return mask_route_all(base, cv2.cvtColor(main_bgr, cv2.COLOR_BGR2RGB), CFG)


#: 真实地形上的合法 L 形回正线（横段沿坑底 x94..108，拐角 x=99，竖段跳回 (99,122)）
L_PTS = [(94, FLOOR_Y), (108, FLOOR_Y), (99, FLOOR_Y), (99, MAIN_Y)]
L_ACTS = ["walk", "walk", "jump"]
L_GOAL = (99, MAIN_Y)
#: 只画一条竖线（PRD 2.4 的反例）：cover_span = 1 → 判废
V_PTS = [(100, FLOOR_Y), (100, MAIN_Y)]
V_ACTS = ["jump"]


def _render(blobs, base=None, goal_radius=1):
    """按手绘面板同一条链路渲一条（或几条）回正线，返回 BGR。"""
    if base is None:
        base = np.zeros(SHAPE, dtype=np.uint8)
    return render_home_image(base, list(blobs), MERGED_CC, goal_radius=goal_radius)


def _render_rgb(blobs, base=None, goal_radius=1):
    return cv2.cvtColor(_render(blobs, base, goal_radius), cv2.COLOR_BGR2RGB)


# ══════════════════════════════════════════════════════════════════════
print("【1】4 倍画布坐标反算（**专门防 `//` 截断**）")
check("f=4 (400,300) → (100,75)", screen_to_map(400, 300, 4, W, H), (100, 75))
check("f=4 (403,300) → (101,75)（用 // 会算成 (100,75)）",
      screen_to_map(403, 300, 4, W, H), (101, 75))
check("f=4 (401,299) → (100,75)（0.25 像素四舍五入）",
      screen_to_map(401, 299, 4, W, H), (100, 75))
# ⚠️ 402/4 = 100.5 → Python 是**银行家取整**（取偶数）→ 100，不是 101。
#    所以面板画点必须用 x*f + (f-1)//2 当中心（偏移量恒 <0.5），见下面的用例。
check("f=4 (402,302) → (100,76)（100.5 取偶数、75.5 取偶数）",
      screen_to_map(402, 302, 4, W, H), (100, 76))
check("f=4 (0,0) → (0,0)", screen_to_map(0, 0, 4, W, H), (0, 0))
check("f=3 (300,300) → (100,100)", screen_to_map(300, 300, 3, W, H), (100, 100))
check("f=6 (600,300) → (100,50)", screen_to_map(600, 300, 6, W, H), (100, 50))
check("越界钳制：超大坐标 → 右下角", screen_to_map(99999, 99999, 4, W, H), (W - 1, H - 1))
check("负坐标钳制 → (0,0)", screen_to_map(-5, -5, 4, W, H), (0, 0))
# ── 面板画点用的中心口径：x*f + (f-1)//2（像素块 [x*f, x*f+f-1] 的中心索引）──
for f in (3, 4, 5, 6):
    check(f"f={f}：点在自画点中心上反算回自己",
          screen_to_map(100 * f + (f - 1) // 2, 75 * f + (f - 1) // 2, f, W, H),
          (100, 75))
# 用 f//2 当中心是**错的**（f=4 时偏到隔壁像素）—— 这条就是它的回归防线
check("反例：f=4 若用 f//2 当中心会偏到隔壁 (100,76)（所以面板必须用 (f-1)//2）",
      screen_to_map(100 * 4 + 4 // 2, 75 * 4 + 4 // 2, 4, W, H), (100, 76))


# ══════════════════════════════════════════════════════════════════════
print("\n【2】落点区间蓝带 → 矩形展开（坑底两端都要算得到）")
band = band_to_pts((94, FLOOR_Y), (108, FLOOR_Y + 1))     # 15 宽 × 2 高
check("15×2 的蓝带 → 30 个落点", len(band), 30)
check("蓝带左上角在", (min(p[0] for p in band), min(p[1] for p in band)), (94, FLOOR_Y))
check("蓝带右下角在", (max(p[0] for p in band), max(p[1] for p in band)),
      (108, FLOOR_Y + 1))
check("端点反着拖也能得到同一片", sorted(band_to_pts((108, FLOOR_Y + 1), (94, FLOOR_Y))),
      sorted(band))
check("只拖一个像素 → 只有它自己", band_to_pts((99, 128), (99, 128)), [(99, 128)])
check("没拖（None）→ 空", band_to_pts(None, None), [])


# ══════════════════════════════════════════════════════════════════════
tmp_root = os.path.join(tempfile.gettempdir(), "手绘面板自检_" + str(os.getpid()))
os.makedirs(tmp_root, exist_ok=True)
try:
    img_map_black = np.zeros(SHAPE, dtype=np.uint8)        # 全黑底图 = 不撞色
    main_bgr = _make_mainline()
    route_rgb = _mainline_rgb(main_bgr)
    main_pts = [(x, MAIN_Y) for x in range(MAIN_X0, MAIN_X1 + 1)]

    print("\n【3】覆盖率模拟（谁近走谁）：坑底 15 个落点全部接住")
    home_rgb = _render_rgb([make_blob(L_PTS, L_ACTS, True)])
    home_pts = split_components(home_rgb, ROUTE_CODES)[0]
    landing_floor = [(x, FLOOR_Y) for x in range(94, 109)]     # 15 个坑底落点
    cov = simulate_coverage(home_pts, main_pts, landing_floor, tol=2)
    check("坑底 15 个落点", cov["total"], 15)
    check("全部接住 15/15", cov["covered"], 15)
    check("没有漏掉的", cov["missed"], [])
    # 主线上的点：d_home=0 且 d_main=0 → 0 < -2 不成立 → **不进回正**（相等主线优先）
    cov_main = simulate_coverage(home_pts, main_pts, [(99, MAIN_Y)], tol=2)
    check("主线上的点：不接（相等主线优先）", cov_main["covered"], 0)
    cov_none = simulate_coverage([], main_pts, landing_floor, tol=2)
    check("没有回正线 → 一个都接不住", (cov_none["covered"], cov_none["total"]), (0, 15))

    print("\n【4】落盘回放校验：正常情况（像素都活着）")
    path_home = os.path.join(tmp_root, "route_home.png")
    home_bgr = _render([make_blob(L_PTS, L_ACTS, True)])
    check_true("imwrite_unicode 写中文路径成功", imwrite_unicode(path_home, home_bgr))
    v = verify_saved_image(path_home, img_map_black, CFG, L_GOAL, goal_radius=1)
    check_true("回读校验通过（段像素没少 + 终点还活着）", v["ok"])
    check("终点像素活着", v["goal_alive"], True)
    check("没有被吃掉的像素", v["eaten"], [])
    check("失败原因为空", v["reason"], "")
    check_true("回读的是 mask 后的图（口径与引擎一致）", v["img_rgb"] is not None)

    print("\n【5】落盘回放校验：**必须**能拦住被 mask 吃掉的像素（撞色场景）")
    # 在地图底图的坑底位置放一个同色像素 → mask_route_colors 会把该处回正线像素涂黑
    img_map_clash = np.zeros(SHAPE, dtype=np.uint8)
    img_map_clash[FLOOR_Y, 95] = _bgr((0, 0, 255))        # 底图里恰好有"蓝"（=向右走）
    v2 = verify_saved_image(path_home, img_map_clash, CFG, L_GOAL, goal_radius=1)
    check("撞色 → 校验不通过", v2["ok"], False)
    check("被吃掉的像素坐标被列出来", v2["eaten"], [(95, FLOOR_Y)])
    check_true("给了人话原因（提示挪 1px）", "吃掉" in v2["reason"])
    # 终点被吃掉同样要拦住（终点在别处，这里把终点色放进底图）
    img_map_clash2 = np.zeros(SHAPE, dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            img_map_clash2[MAIN_Y + dy, 99 + dx] = _bgr(GOAL_RGB)
    v3 = verify_saved_image(path_home, img_map_clash2, CFG, L_GOAL, goal_radius=1)
    check("终点被吃 → 校验不通过", v3["ok"], False)
    check("终点像素被判定为死亡", v3["goal_alive"], False)

    print("\n【6】L 形横段**逐像素汇聚着色**（左半 right、右半 left，都指向拐角）")
    rgb_l = _render_rgb([make_blob(L_PTS, L_ACTS, True)])
    # 拐角在 x=99；左半必须"向右走"(蓝 0,0,255)、右半必须"向左走"(红 255,0,0)
    check("横段左端 (94,128) = 蓝（right，往右走向拐角）",
          tuple(int(x) for x in rgb_l[FLOOR_Y, 94]), (0, 0, 255))
    check("横段右端 (108,128) = 红（left，往左走向拐角）",
          tuple(int(x) for x in rgb_l[FLOOR_Y, 108]), (255, 0, 0))
    check("拐角左侧 (98,128) = 蓝", tuple(int(x) for x in rgb_l[FLOOR_Y, 98]), (0, 0, 255))
    check("拐角右侧 (100,128) = 红", tuple(int(x) for x in rgb_l[FLOOR_Y, 100]), (255, 0, 0))
    # 拐角列 (99,128) 被竖段着成"原地跳"洋红（横段跳过它，不覆盖竖段）
    check("拐角列 (99,128) = 洋红（竖段 jump，横段跳过）",
          tuple(int(x) for x in rgb_l[FLOOR_Y, 99]), (255, 0, 255))
    check("竖段 (99,125) = 洋红（原地跳）",
          tuple(int(x) for x in rgb_l[125, 99]), (255, 0, 255))
    check("终点 (99,122) = 黄（goal）",
          tuple(int(x) for x in rgb_l[MAIN_Y, 99]), GOAL_RGB)
    # ⚠️ 反例：如果用 cv2.line 把整条横段画成一个颜色，右半就会跟左半同色（都蓝）
    check("反例：右半**不能**和左半同色（否则就是「整段单色」的 bug）",
          tuple(int(x) for x in rgb_l[FLOOR_Y, 94]) == tuple(int(x) for x in rgb_l[FLOOR_Y, 108]),
          False)
    # 直接验 paint_converging_hseg 的取舍
    buf = np.zeros((20, 20, 3), dtype=np.uint8)            # BGR
    paint_converging_hseg(buf, [(x, 5) for x in range(2, 12)], (7, 5), MERGED_CC, "walk")
    check("paint_converging_hseg：拐角左侧 = 蓝(BGR 255,0,0)",
          tuple(int(x) for x in buf[5, 3]), (255, 0, 0))
    check("paint_converging_hseg：拐角右侧 = 红(BGR 0,0,255)",
          tuple(int(x) for x in buf[5, 10]), (0, 0, 255))
    check("paint_converging_hseg：拐角列跳过（仍黑）",
          tuple(int(x) for x in buf[5, 7]), (0, 0, 0))

    print("\n【7】判废（第二版五条判据）")
    # ⑦-a 只画竖线：cover_span = 1 → 判废
    rgb_v = _render_rgb([make_blob(V_PTS, V_ACTS, True)])
    ok_v, why_v = validate_home_line(split_components(rgb_v, ROUTE_CODES)[0],
                                     rgb_v, [route_rgb], ROUTE_CODES, CFG)
    check("只画竖线 → 判废", ok_v, False)
    check_true("原因说清了「只接得住一个点」", "接得住" in why_v)
    far_v = far_segment_stats(split_components(rgb_v, ROUTE_CODES)[0],
                              [route_rgb], ROUTE_CODES, CFG)
    check("只画竖线：起始段覆盖跨度 = 1", far_v["cover_span"], 1)
    # ⑦-b 终点不贴主线 → 判废（用一条贴着坑底的横线，终点离主线 6px）
    far_goal_pts = [(100, FLOOR_Y), (106, FLOOR_Y)]
    rgb_g = _render_rgb([make_blob(far_goal_pts, ["walk"], True)])
    ok_g, why_g = validate_home_line(split_components(rgb_g, ROUTE_CODES)[0],
                                     rgb_g, [route_rgb], ROUTE_CODES, CFG)
    check("终点离主线 6px → 判废", ok_g, False)
    check_true("原因说清了「终点没画在主线上」", "终点" in why_g and "主线" in why_g)
    # ⑦-c 合法 L 形 → 通过（对照组，证明上面的判废不是因为判据太严）
    ok_l, why_l = validate_home_line(split_components(rgb_l, ROUTE_CODES)[0],
                                     rgb_l, [route_rgb], ROUTE_CODES, CFG)
    check("合法 L 形 → 通过（对照组）", (ok_l, why_l), (True, ""))

    print("\n【8】一张图多条回正线：逐条列出 / 逐条判废 / 废的不连坐")
    # 三条线各有自己的主线（判据④/⑤ 要相对主线量），x 方向拉开
    main2_bgr = _make_mainline(y=MAIN_Y, x0=118, x1=124)     # 给"只画竖线"那条
    main3_bgr = _make_mainline(y=MAIN_Y, x0=126, x1=140)     # 给右边那条合法 L
    route_multi = [_mainline_rgb(main_bgr, img_map_black),
                   _mainline_rgb(main2_bgr, img_map_black),
                   _mainline_rgb(main3_bgr, img_map_black)]
    # 第 1 条：合法 L（落点区间 x94..108）
    blob_a = make_blob(L_PTS, L_ACTS, True)
    # 第 2 条：废（只画竖线，落点区间 x=120 一个点）
    blob_b = make_blob([(120, FLOOR_Y), (120, MAIN_Y)], ["jump"], True)
    # 第 3 条：合法 L（落点区间 x126..138）
    blob_c = make_blob([(126, FLOOR_Y), (138, FLOOR_Y), (132, FLOOR_Y), (132, MAIN_Y)],
                       ["walk", "walk", "jump"], True)
    multi_rgb = _render_rgb([blob_a, blob_b, blob_c])
    lines = audit_components(multi_rgb, route_multi, CFG)
    check("解析出 3 条", len(lines), 3)
    # ⚠️ split_components 按"最上、最左"排序（界面列表才不会跳），**不是**插入顺序
    #    → 断言一律按落点区间定位，别按下标
    by_x = {l["cover_xs"][0]: l for l in lines if l["cover_xs"]}
    check("3 条的落点区间都在", sorted(by_x), [94, 120, 126])
    l_a, l_b, l_c = by_x[94], by_x[120], by_x[126]
    check("落点 x94 那条合格", l_a["ok"], True)
    check("落点 x120 那条判废（只画竖线）", l_b["ok"], False)
    check("落点 x126 那条合格（废线**没连坐**）", l_c["ok"], True)
    check("合法的两条含跳跃", (l_a["has_jump"], l_c["has_jump"]), (True, True))
    check("废的那条覆盖跨度 1", l_b["cover_span"], 1)
    check("合法那条覆盖跨度 15", l_a["cover_span"], 15)
    check_true("废的那条给了人话原因", "接得住" in l_b["why"])
    check("废的那条在列表里标了「用不了」", "用不了" in line_summary(l_b), True)
    check("合格的那条列表里没有「用不了」", "用不了" in line_summary(l_a), False)
    check_true("列表文案看得懂（含起点/终点/跨度/落点覆盖）",
               all(all(k in line_summary(l) for k in ("起点", "终点", "落点覆盖"))
                   for l in lines))
    # 落点区间重叠 → 只警告不阻断
    overlap_lines = [dict(l_a, id=1, cover_xs=(94, 108)),
                     dict(l_c, id=2, cover_xs=(100, 120))]
    warns = HomeRouteDrawer._overlap_warnings(None, overlap_lines)
    check("两条落点区间重叠 → 1 条警告", len(warns), 1)
    check_true("警告里点明了「重叠」", "重叠" in warns[0])
    check("区间不重叠 → 无警告",
          HomeRouteDrawer._overlap_warnings(None, [dict(l_a, id=1),
                                                   dict(l_c, id=2)]), [])

    print("\n【9】界面体检（route_audit）与引擎加载（_scan_home_lines）**口径一致**")
    audit_dir = os.path.join(tmp_root, "自检用图_中文")
    os.makedirs(audit_dir, exist_ok=True)
    imwrite_unicode(os.path.join(audit_dir, "map.png"), img_map_black)
    imwrite_unicode(os.path.join(audit_dir, "route1.png"), main_bgr)
    imwrite_unicode(os.path.join(audit_dir, "route_home.png"), home_bgr)

    from src.utils import route_audit
    orig_resource_path = route_audit.resource_path
    route_audit.resource_path = lambda p, _d=audit_dir: _d     # 只审计这一处
    try:
        rep = route_audit.audit_map(audit_dir, CFG)
    finally:
        route_audit.resource_path = orig_resource_path
    check("界面体检：主线正常加载", rep["n_main"], 1)
    check("界面体检：L 形回正线**判为可用**", rep["home_ok"], True)
    bad_home = [l.strip()[:40] for l in rep["lines"]
                if l.strip().startswith("✗") and "回正线" in l]
    check("界面体检：没有任何一条回正线被判废", bad_home, [])
    check_true("界面体检：给的是「可用」那条的人话报告",
               any(l.strip().startswith("✓") and "回正线" in l for l in rep["lines"]))

    # 引擎侧（挂 _scan_home_lines 真方法）：同一份数据也必须给出 1 条可用
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
    bot = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    bot.cfg = CFG
    bot.color_code = CC
    bot.color_code_up_down = CC_UD
    home_rgb_engine = mask_route_all(
        img_map_black,
        cv2.cvtColor(load_image(os.path.join(audit_dir, "route_home.png")),
                     cv2.COLOR_BGR2RGB), CFG)
    engine_lines = bot._scan_home_lines(audit_dir, home_rgb_engine, [route_rgb])
    check("引擎加载：解析出 1 条可用回正线", len(engine_lines), 1)
    check("引擎加载：落点覆盖跨度 15（与界面一致）", engine_lines[0]["cover_span"], 15)
    # ⚠️ 主线的 goal 圆点（r=2）会把主线延伸到 x=108 → 坑底最左/最右到主线**一样远**（都 6px）
    #    → start 落在"最上、最左"的并列者 (94,128) → span = |94-99| + |128-122| = 11
    check("引擎加载：跨度 11（与界面一致）", engine_lines[0]["span"], 11)
    check("引擎加载：含跳跃", engine_lines[0]["has_jump"], True)
    check("界面与引擎口径一致（都不判废）",
          (rep["home_ok"], len(engine_lines)), (True, 1))

    print("\n【10】对照：只画竖线 → 两边**都**判废（不能只改宽松）")
    bad_home_bgr = _render([make_blob(V_PTS, V_ACTS, True)])
    imwrite_unicode(os.path.join(audit_dir, "route_home.png"), bad_home_bgr)
    route_audit.resource_path = lambda p, _d=audit_dir: _d
    try:
        rep2 = route_audit.audit_map(audit_dir, CFG)
    finally:
        route_audit.resource_path = orig_resource_path
    check("界面体检：只画竖线 → 回正线不可用", rep2["home_ok"], False)
    home_rgb_engine2 = mask_route_all(
        img_map_black,
        cv2.cvtColor(load_image(os.path.join(audit_dir, "route_home.png")),
                     cv2.COLOR_BGR2RGB), CFG)
    engine_lines2 = bot._scan_home_lines(audit_dir, home_rgb_engine2, [route_rgb])
    check("引擎加载：同样 0 条可用", len(engine_lines2), 0)
    check("两边口径仍然一致", (rep2["home_ok"], len(engine_lines2)), (False, 0))
    # 旧图迁移：第一版落点锚（127,0,127）必须被就地涂黑
    legacy_rgb = home_rgb.copy()
    legacy_rgb[FLOOR_Y, 95] = LEGACY_ANCHOR_RGB
    wiped = wipe_legacy_anchor(legacy_rgb)
    check("旧版落点标记被清掉", wiped, [(95, FLOOR_Y)])
    check("清掉后该像素变黑", tuple(int(x) for x in legacy_rgb[FLOOR_Y, 95]), (0, 0, 0))

    print("\n【11】终点必须**显式**点出来（隐式「最后一下即终点」已废止）")
    check("单选里有 5 个：4 个走法 + 1 个「回到主线（终点）」", len(ACTIONS), 5)
    check("第 5 个就是 goal", ACTIONS[-1][0], "goal")
    check_true("第 5 个的中文label 含「回到主线」", "回到主线" in ACTIONS[-1][1])
    check("到终点那一段按「走」画（跳/爬上来后还要走两步）", GOAL_SEGMENT_ACTION, "walk")

    # ── 用例 1：4 点流程（落点 → 沿坑底走 → 拐角 → 回到主线）────────────────
    blob_l = make_blob(L_PTS, L_ACTS, True)
    check("make_blob 的终点落在最后一点上", blob_l["goal"], L_GOAL)
    check("make_blob 带上了拐角（拐角列 x=99，横段就汇聚到这里）",
          blob_l["corner"][0], 99)
    out_l = _render_rgb([blob_l])
    check("4 点流程：终点像素 = goal 黄 255,255,0",
          tuple(int(x) for x in out_l[MAIN_Y, 99]), GOAL_RGB)
    line_l = audit_components(out_l, [route_rgb], CFG)
    check("4 点流程：解析出 1 条", len(line_l), 1)
    check("4 点流程：体检合格", line_l[0]["ok"], True)
    check("4 点流程：has_jump = True", line_l[0]["has_jump"], True)
    check("4 点流程：跨度 11px（见【9】说明）", line_l[0]["span"], 11)

    # ── 用例 2：只点 2 点（落点 + 一个跳）就保存 → 必须拒绝 ─────────────────
    err2 = save_gate_error(V_PTS, False, True)
    check_true("没点终点 → 拒绝保存（给出中文原因）", bool(err2))
    check_true("原因里写明了「回到主线」", "回到主线" in err2)
    check_true("原因里说明了后果（不知道该停在哪）", "停在哪" in err2)
    # 反证：没点终点时**连黄点都不会盖**（否则又变回隐式规则）
    blob_nog = make_blob(L_PTS, L_ACTS, False)
    out_nog = _render_rgb([blob_nog])
    check("没点终点 → 图上**没有** goal 黄点",
          any(tuple(int(x) for x in out_nog[y, x]) == GOAL_RGB
              for y in range(H) for x in range(W)), False)

    # ── 用例 3：选了「回到主线」再点 → 能存，goal 在正确坐标 ─────────────────
    err3 = save_gate_error(L_PTS, True, True)
    check("点了终点 → 放行（原因为空）", err3, "")
    # 其它两个门槛
    check_true("只点 1 个点 → 拒绝保存", bool(save_gate_error([(100, 128)], False, True)))
    check_true("没有主线 → 拒绝保存", bool(save_gate_error(L_PTS, True, False)))
    check_true("没有主线的原因说清楚要先录主线",
               "主线" in save_gate_error(L_PTS, True, False))

    # ── 撤销的段动作裁剪（回归：只点 1 点就撤销曾 pop 空列表崩溃）─────────────
    check("撤销到 0 个点 → 段动作清空（不抛 IndexError）", trim_actions([], ["walk"]), [])
    check("撤销到 1 个点 → 段动作清空", trim_actions([(100, 128)], ["walk"]), [])
    check("撤销到 2 个点 → 只留 1 条段动作",
          trim_actions([(100, 128), (100, 124)], ["jump", "walk"]), ["jump"])
    check("没多裁：3 个点留 2 条",
          trim_actions([(1, 1), (2, 2), (3, 3)], ["jump", "walk"]), ["jump", "walk"])
    check("空动作列表也不炸", trim_actions([], []), [])

    print("\n【12】落点可以**填坐标**（需求方不开游戏也能画：坐标从引擎日志里抄）")
    LOG_LINE = "[回正] 角色位置 (98, 128) 附近找不到回正线像素"
    check("① 粘贴整行日志 → 解析出 (98, 128)", parse_coord_text(LOG_LINE), (98, 128))
    check("带日志时间戳的整行 → 仍是 (98, 128)（时间戳不能抢戏）",
          parse_coord_text("[2026-09-13 02:16:54] INFO: " + LOG_LINE), (98, 128))
    check("中文括号/逗号 → (98, 128)", parse_coord_text("角色位置（98，128）"), (98, 128))
    check("方括号 → (98, 128)", parse_coord_text("[98,128]"), (98, 128))
    check("手敲 '98,128' → (98, 128)", parse_coord_text("98,128"), (98, 128))
    check("手敲 '98 128' → (98, 128)", parse_coord_text("98 128"), (98, 128))
    check("X/Y 两框拼起来 '98' + '128' → (98, 128)",
          parse_coord_text("98, 128"), (98, 128))
    check("0 也能认（别把 0 当空）", parse_coord_text("(0, 0)"), (0, 0))
    check("纯文字 → None（不瞎猜）", parse_coord_text("不知道落点在哪"), None)
    check("空串 / None → None", (parse_coord_text(""), parse_coord_text(None)),
          (None, None))
    check("只有时间戳 → None（'13 02' 不能被当成坐标）",
          parse_coord_text("[2026-09-13 02:16:54]"), None)

    # ── 用例 2：填坐标 与 点画布 走**同一条路径** ────────────────────────────
    #    两条路都只有 make_blob 一个出口 → 同一组坐标渲出来的 PNG 必须**逐像素相同**
    from_input = (98, 128)                       # 日志里抄来的
    from_click = (98, 128)                       # 在画布上点到的同一个像素
    acts = ["jump", GOAL_SEGMENT_ACTION]
    pts_i = [from_input, (98, 124), (98, 122)]
    pts_c = [from_click, (98, 124), (98, 122)]
    img_i = _render([make_blob(pts_i, acts, True)])
    img_c = _render([make_blob(pts_c, acts, True)])
    check("填坐标 vs 点画布：渲出来的图**逐像素相同**",
          bool(np.array_equal(img_i, img_c)), True)
    rgb_i = cv2.cvtColor(img_i, cv2.COLOR_BGR2RGB)
    check("两条路的落点像素坐标一致（起点 x=98）",
          split_components(rgb_i, ROUTE_CODES)[0] == split_components(
              cv2.cvtColor(img_c, cv2.COLOR_BGR2RGB), ROUTE_CODES)[0], True)
    check("落点 (98,128) 有路线像素", (98, 128) in split_components(rgb_i, ROUTE_CODES)[0], True)

    # ── 用例 3：越界 / 负数 / 非数字 → 中文提示且**不崩** ────────────────────
    check("合法坐标 → 无错误", coord_error(98, 128, W, H), "")
    check("右下角边界内 (267,186) → 无错误", coord_error(W - 1, H - 1, W, H), "")
    check_true("X 越界 → 中文提示", bool(coord_error(268, 128, W, H)))
    check_true("Y 越界 → 中文提示", bool(coord_error(98, 187, W, H)))
    check_true("负数 → 中文提示", bool(coord_error(-1, 128, W, H)))
    check_true("提示里写明了这张图的取值范围", "0~267" in coord_error(300, 128, W, H))
    check_true("提示里提示可能抄错行了", "抄错行" in coord_error(300, 128, W, H))
    check_true("None → 中文提示（不崩）", bool(coord_error(None, None, W, H)))
    check_true("字符串数字能过（coord_error 自己转 int）",
               coord_error("98", "128", W, H) == "")
    check_true("非数字 → 中文提示（不崩）", bool(coord_error("abc", "128", W, H)))
    check("网格间隔 20px", GRID_STEP, 20)

    print("\n【13】文案（列表行 / 覆盖率）")
    check_true("列表行含起点/终点/跨度/落点覆盖",
               all(k in line_summary(dict(l_a)) for k in ("起点", "终点", "跨度", "落点覆盖")))
    cov_full = {"total": 15, "covered": 15, "missed": []}
    check_true("覆盖 15/15 的文案含「15/15」", "15/15" in coverage_text(cov_full))
    check_true("覆盖 15/15 的文案有「✓」", "✓" in coverage_text(cov_full))
    cov_part = {"total": 15, "covered": 10, "missed": [(100, FLOOR_Y), (108, FLOOR_Y)]}
    check_true("覆盖 10/15 的文案含「10/15」", "10/15" in coverage_text(cov_part))
    check_true("覆盖不满的文案教了「把横段画长」", "横段" in coverage_text(cov_part))
    check_true("还没框区间 → 提示先框蓝带", "蓝带" in coverage_text(None))

    print("\n【14】纯函数契约（不创建窗口也能验的部分）")
    dp = draw_params(CFG)
    check("手绘默认放大 4 倍", dp["home_draw_scale"], 4)
    check("终点盖章半径 1", dp["home_draw_goal_radius"], 1)
    check("第二版**没有** home_draw_anchor_radius 了",
          "home_draw_anchor_radius" in dp, False)
    hp = home_params(CFG)
    check("比较容差 home_dist_tol = 2", hp["home_dist_tol"], 2)
    check("起始段覆盖门槛 home_min_cover_span = 5", hp["home_min_cover_span"], 5)
    check("第二版**没有** home_anchor_up_tol 了", "home_anchor_up_tol" in hp, False)
    # D4：这些旋钮"关不掉"—— 配成 0 / 负数时回落默认（而不是禁用）
    cfg_bad = dict(CFG)
    cfg_bad["route"] = dict(CFG["route"], home_timeout=0, home_reenter_lock=-1,
                            home_rearm_timeout=0, home_dist_tol=-9)
    hp_bad = home_params(cfg_bad)
    check("D4：home_timeout 配 0 → 回落 8", hp_bad["home_timeout"], 8)
    check("D4：home_reenter_lock 配 -1 → 回落 10", hp_bad["home_reenter_lock"], 10)
    check("D4：home_rearm_timeout 配 0 → 回落 30", hp_bad["home_rearm_timeout"], 30)
    check("D4：home_dist_tol 配 -9 → 回落 2", hp_bad["home_dist_tol"], 2)
    check_true("面板类存在且可实例化签名齐全",
               hasattr(HomeRouteDrawer, "_save_line")
               and hasattr(HomeRouteDrawer, "_delete_selected")
               and hasattr(HomeRouteDrawer, "open_for"))
    check_true("对外入口 open_drawer 存在", callable(open_drawer))
    merged = load_merged_cfg(None)
    check_true("load_merged_cfg 能吃下默认配置",
               "route" in merged and "color_code" in merged["route"])
    check("曼哈顿距离仍是可用的公共量", manhattan((100, 128), (104, 128)), 4)
    # corner_index：第一个非 walk 段的**终点**下标
    check("corner_index：全 walk → 最后一个点", corner_index([(1, 1), (2, 1), (3, 1)],
                                                             ["walk", "walk"]), 2)
    check("corner_index：第 2 段是 jump → 3（拐角下标）",
          corner_index([(1, 1), (2, 1), (3, 1), (3, 0)], ["walk", "walk", "jump"]), 3)
    check("corner_index：空点 → 0", corner_index([], []), 0)
    # pick_color：走/跳 的方向与色码
    check("pick_color 向右走 = 蓝", pick_color((0, 0), (5, 0), "walk", MERGED_CC), (0, 0, 255))
    check("pick_color 向左走 = 红", pick_color((5, 0), (0, 0), "walk", MERGED_CC), (255, 0, 0))
    check("pick_color 原地跳 = 洋红", pick_color((0, 0), (0, -5), "jump", MERGED_CC), (255, 0, 255))

    print("\n【15】竖段画在平台外 → 手绘器给**警告**（不阻断保存）")
    # ⚠️ QA 发现的盲区：判据⑤ 只看「goal 像素集到主线 min ≤ home_goal_mainline_tol」，
    #    而 goal 是半径 1~2 的圆 → 圆必然碰到平台边缘 → 竖段画在平台外照样放行。
    #    所以这里用**平台 x 范围**判"竖段在不在平台正上方"（与手绘器
    #    self.main_move_pts / self.main_pts 同一条口径）。
    _gs = goal_codes(MERGED_CC)
    move_codes = ROUTE_CODES - _gs
    main_all = collect_pixels(route_rgb, ROUTE_CODES)
    main_move = collect_pixels(route_rgb, move_codes)
    check("移动像素只到 x=103（goal 圆吃掉了平台边缘 104~106）",
          (min(p[0] for p in main_move), max(p[0] for p in main_move)), (92, 103))
    # ① 平台正上方（x=99 / 平台最右 x=106）→ **不能**误报
    check("竖段 x=99（平台内）→ 不警告",
          jump_off_platform(L_PTS, L_ACTS, main_move, main_all), None)
    check("竖段 x=106（平台最右，**不能**误报）→ 不警告",
          jump_off_platform([(94, FLOOR_Y), (106, FLOOR_Y), (106, MAIN_Y)],
                            ["walk", "jump"], main_move, main_all), None)
    # ② 平台**外面**（x=107）→ 警告（**不阻断**，只返回提示）
    w = jump_off_platform([(94, FLOOR_Y), (106, FLOOR_Y), (107, MAIN_Y)],
                          ["walk", "jump"], main_move, main_all)
    check_true("★ 竖段 x=107（平台外）→ 给警告", w is not None)
    check("平台范围判成 92~106（goal 圆的中心补回来了，没被撑到 108）",
          (w["x0"], w["x1"]), (92, 106))
    check("警告里点明了「平台外面」", "平台外面" in w["msg"], True)
    check("警告里写了平台 x 范围", "x92~106" in w["msg"], True)
    check("警告里说了后果「落空」", "落空" in w["msg"], True)
    check("落在右边 → side=右", w["side"], "右")
    # ③ 左边也一样（x=91）
    wl = jump_off_platform([(94, FLOOR_Y), (106, FLOOR_Y), (91, MAIN_Y)],
                           ["walk", "jump"], main_move, main_all)
    check("竖段 x=91（平台左边外）→ 也警告", (wl is not None, wl["side"]), (True, "左"))
    # ④ 还没画竖直段 / 点太少 → 不提示（别拿"走"误报）
    check("全是「走」→ 不提示",
          jump_off_platform([(94, FLOOR_Y), (108, FLOOR_Y)], ["walk"], main_move, main_all),
          None)
    check("只有一个点 → 不提示",
          jump_off_platform([(107, MAIN_Y)], [], main_move, main_all), None)
    check("不传 all_pts（只有移动像素）也不崩、仍能判出平台外",
          bool(jump_off_platform([(94, FLOOR_Y), (106, FLOOR_Y), (107, MAIN_Y)],
                                 ["walk", "jump"], main_move)), True)
    # ⑤ 判据⑤ **确实拦不住** x=107（这就是为什么要加提示条）—— 用真实主线验一次
    rgb_107 = _render_rgb([make_blob([(94, FLOOR_Y), (106, FLOOR_Y), (107, MAIN_Y)],
                                     ["walk", "jump"], True)])
    ok_107, why_107 = validate_home_line(split_components(rgb_107, ROUTE_CODES)[0],
                                         rgb_107, [route_rgb], ROUTE_CODES, CFG)
    check("★ 判据⑤ 拦不住 x=107（goal 圆碰到平台边缘 → 放行；所以提示条才是主要手段）",
          ok_107, True)
finally:
    shutil.rmtree(tmp_root, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════
# 落点横段的「跳跃区间」jump band（2026-09-14 加）—— 手绘器接线
# ══════════════════════════════════════════════════════════════════════
#: ⚠️ 为什么主角是**橙色** (255,127,0) 而不是蓝色：这条链路要转两次色彩空间
#:    （route_imgs_rgb 是 RGB → calc_jump_band 收 BGR → 内部再转回 RGB）。
#:    蓝 (0,0,255) 的 BGR 反转是红 (255,0,0)，而红**也在**色码表里 ——
#:    用蓝色的话"忘记转 BGR"这个 bug 照样能匹配上，用例根本发现不了。
#:    橙色的反转 (0,127,255) **不在**任何一张色码表里 → 忘了转就是 None。
#:    （drawer 里那次转换在 1149 行附近，注释也写了"别省"。）
CANARY_RGB = (255, 127, 0)                 # 🟠 左跳；BGR 反转后不是合法色码


def _fake_main_rgb(rows):
    '''造假主线（**RGB**，与 self.route_imgs_rgb 同口径）。

    Args:
        rows: [(y, x0, x1, rgb), ...] —— rgb 是 **RGB** 元组
    '''
    img = np.zeros(SHAPE, dtype=np.uint8)
    for y, x0, x1, rgb in rows:
        img[int(y), int(x0):int(x1) + 1] = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    return img


class _BareDrawer(HomeRouteDrawer):
    '''只跑 `_jump_band` 的**裸实例**：跳过 __init__（它会建 Qt 界面 + 读盘）。

    ⚠️ 为什么不建真的 QApplication：本脚本的定位是"不开游戏、不需要显示器"。
       而 `_jump_band` 只读 4 个字段（cfg / route_imgs_rgb / route_codes /
       _band_cache）+ cur_points / cur_actions，手工塞齐就能跑**真实方法本体**
       —— 不是只验源码里有没有这行字。
    '''

    def __init__(self):      # noqa: D107 - 故意留空，别碰 Qt
        pass


def _new_drawer(cfg=None, routes=None, pts=None, acts=None):
    d = _BareDrawer()
    d.cfg = CFG if cfg is None else cfg
    d.route_imgs_rgb = routes
    d.route_codes = ROUTE_CODES
    d._band_cache = {}
    d.cur_points = list(pts) if pts else []
    d.cur_actions = list(acts) if acts else []
    return d


print("\n【16】make_blob 透传 jump_band（给了 → 带进 blob；不给 → 老行为）")
BAND_94 = (93, 105)
blob_band = make_blob(L_PTS, L_ACTS, True, BAND_94)
check("给了 band → blob 里就是这个 (x0, x1)", blob_band["jump_band"], BAND_94)
check("给了 band → corner 没被带偏", blob_band["corner"], (99, MAIN_Y))
check("给了 band → walk_segs 没被带偏", blob_band["walk_segs"], 3)
blob_def = make_blob(L_PTS, L_ACTS, True)
check("不传 band（老调用方式）→ 键**存在**、值是 None", blob_def["jump_band"], None)
check("显式传 None → 值也是 None", make_blob(L_PTS, L_ACTS, True, None)["jump_band"],
      None)
check("★ 不传 band 与传 None，两个 blob 完全等价",
      (blob_def["jump_band"], blob_def["corner"], blob_def["walk_segs"]),
      (None, (99, MAIN_Y), 3))
# 下游 render_home_image 的行为：给了 band → 区间内是「原地跳」
rgb_band = _render_rgb([blob_band])
check("给了 band → 区间内 (99,128) = 洋红「原地跳」",
      tuple(int(v) for v in rgb_band[FLOOR_Y, 99]), (255, 0, 255))
check("给了 band → 区间内右端 (105,128) 也是洋红",
      tuple(int(v) for v in rgb_band[FLOOR_Y, 105]), (255, 0, 255))
check("给了 band → 区间外 (107,128) 仍是红（往左走回区间）",
      tuple(int(v) for v in rgb_band[FLOOR_Y, 107]), (255, 0, 0))
# 没给 band → **逐像素等价于改造前**（横段上只有拐角那 1 个 jump 像素）
rgb_none = _render_rgb([make_blob(L_PTS, L_ACTS, True)])
_jumps = [x for x in range(94, 109)
          if tuple(int(v) for v in rgb_none[FLOOR_Y, x]) == (255, 0, 255)]
check("★ 没给 band → 横段上只有拐角 1 个 jump 像素（老行为逐像素不变）",
      _jumps, [99])
check("没给 band → 拐角左边 (94,128) 仍是蓝（老行为）",
      tuple(int(v) for v in rgb_none[FLOOR_Y, 94]), (0, 0, 255))
check("没给 band → 拐角右边 (108,128) 仍是红（老行为）",
      tuple(int(v) for v in rgb_none[FLOOR_Y, 108]), (255, 0, 0))

print("\n【17】★ _jump_band 本体（真跑方法，不是只看源码）")
# ── 17-a 多张主线取**并集**（最关键的一条）────────────────────────────────
# 这条在防什么：主线可能是 route1.png / route2.png 两张（去 + 回）。
# 只算了第一张的话，另一半"主线正下方"就会被画成"往回走"，角色在区间里
# 明明站得住却往回跑 —— 跟没修之前一样在震荡。
MAIN_A = _fake_main_rgb([(20, 20, 40, CANARY_RGB)])          # x 20~40
MAIN_B = _fake_main_rgb([(20, 30, 50, CANARY_RGB)])          # x 30~50
#: 横段在坑底 y=26，拐角（竖段顶端）在 y=20 —— 与真实地形同构（122 / 128）
PIT26_PTS = [(20, 26), (40, 26), (30, 26), (30, 20)]
PIT26_ACTS = ["walk", "walk", "jump"]
check("（口径自证）calc_jump_band 单张 A → (21,39)（inset=1）",
      calc_jump_band(cv2.cvtColor(MAIN_A, cv2.COLOR_RGB2BGR), 26, 8, 1,
                     ROUTE_CODES), (21, 39))
check("（口径自证）calc_jump_band 单张 B → (31,49)",
      calc_jump_band(cv2.cvtColor(MAIN_B, cv2.COLOR_RGB2BGR), 26, 8, 1,
                     ROUTE_CODES), (31, 49))
with capture_log() as _msgs:
    _d2 = _new_drawer(routes=[MAIN_A, MAIN_B], pts=PIT26_PTS, acts=PIT26_ACTS)
    _band2 = _d2._jump_band()
check("★ 两张主线 → 取**并集** (25,45)（只算第一张会是 (25,35)）",
      _band2, (25, 45))
check("★ 算 band 时没走 except 兜底分支（裸实例字段是齐的）",
      [m for m in _msgs if "跳跃区间算不出来" in m], [])
check("★ 能算出结果 ⇒ RGB→BGR 那次转换是对的（橙色在 BGR 反转后不是合法色码）",
      _band2 is not None, True)

# ── 17-b 拿不到主线 → None，不抛异常 ──────────────────────────────────────
# 这条在防什么：新地图还没录主线时也要能画回正线（画完再录的路子很常见）。
# 崩在手绘器里 = 保存按钮点了没反应，最难查。
check("route_imgs_rgb = [] → None",
      _new_drawer(routes=[], pts=PIT26_PTS, acts=PIT26_ACTS)._jump_band(), None)
check("route_imgs_rgb = None → None",
      _new_drawer(routes=None, pts=PIT26_PTS, acts=PIT26_ACTS)._jump_band(), None)
check("route_imgs_rgb = [None]（读失败的图）→ None",
      _new_drawer(routes=[None], pts=PIT26_PTS, acts=PIT26_ACTS)._jump_band(), None)
check("还没有点（pts 为空）→ None",
      _new_drawer(routes=[MAIN_A], pts=[], acts=[])._jump_band(), None)
check("主线在坑底**下方**（够不着）→ None",
      _new_drawer(routes=[_fake_main_rgb([(40, 20, 40, CANARY_RGB)])],
                  pts=PIT26_PTS, acts=PIT26_ACTS)._jump_band(), None)

# ── 17-c ★ pit_y 用错会算歪（把上次踩的坑钉死）────────────────────────────
# 背景：`corner_index(pts, acts)` 返回的是竖段**顶端**（本例 (30,**20**)，在主线上），
#       不是坑底 (y=26)。拿它当 pit_y 会去数"比 20 还高 8px"的主线 → 算歪。
# 两种失败模式都要能区分，所以造两个场景：
#   c1 歪成另一个区间（主线有两层）  c2 直接返回 None（主线只有一层）
MAIN_TWO = _fake_main_rgb([(20, 20, 40, CANARY_RGB),      # 坑底正上方那层
                           (14, 60, 70, CANARY_RGB)])     # 更上面的一层
with capture_log() as _msgs2:
    _band_c1 = _new_drawer(routes=[MAIN_TWO], pts=PIT26_PTS,
                           acts=PIT26_ACTS)._jump_band()
# 正确（pit_y=26，窗口 y∈[18,26) 只看到 y=20 那层）→ (25,35)
# 错误（pit_y=20，窗口 y∈[12,20) 只看到 y=14 那层）→ (65,65)
check("★ c1 拐角在 y=20 / 横段在 y=26 → 按 **y=26** 算 → (25,35)",
      _band_c1, (25, 35))
check("★ c1 不是按拐角 y=20 算的（那种错法会得到 (65,65)）",
      _band_c1 == (65, 65), False)
check("c1 没走 except 兜底", [m for m in _msgs2 if "跳跃区间算不出来" in m], [])
with capture_log() as _msgs3:
    _band_c2 = _new_drawer(routes=[MAIN_A], pts=PIT26_PTS,
                           acts=PIT26_ACTS)._jump_band()
# 错误（pit_y=20，窗口 y∈[12,20) 不含 y=20）→ 一个像素都找不到 → None
check("★ c2 只有一层主线时，用错 pit_y 会得到 None —— 正确实现必须**不是** None",
      _band_c2, (25, 35))
check("c2 没走 except 兜底", [m for m in _msgs3 if "跳跃区间算不出来" in m], [])

# ── 17-d 缓存：同 pit_y 一致，换 pit_y 会重算 ─────────────────────────────
_d4 = _new_drawer(routes=[MAIN_A, MAIN_B], pts=PIT26_PTS, acts=PIT26_ACTS)
_b_first = _d4._jump_band()
_d4.route_imgs_rgb = []          # 把主线抽走：若没缓存会变成 None
check("缓存命中：同一个 pit_y 第二次调用结果一致", _d4._jump_band(), _b_first)
check("★ 缓存真的生效（抽走主线后仍返回同一个 band，不是重新扫图）",
      _d4._jump_band(), (25, 45))
check("换一个 pit_y（头顶没主线）→ 重算得到 None（没被旧缓存污染）",
      _d4._jump_band([(20, 60), (40, 60), (30, 60)], ["walk", "walk", "jump"]),
      None)

# ── 17-e 配置里的两个旋钮真的读到 ─────────────────────────────────────────
_cfg_i2 = {"route": dict(CFG["route"], home_jump_band_inset=2)}
check("cfg 里 inset=2 → 两端各缩 2 → (22,38)",
      _new_drawer(cfg=_cfg_i2, routes=[MAIN_A], pts=PIT26_PTS,
                  acts=PIT26_ACTS)._jump_band(), (22, 38))
_cfg_d3 = {"route": dict(CFG["route"], home_jump_band_max_drop=3)}
check("cfg 里 max_drop=3（落差 6 > 3）→ 够不着 → None",
      _new_drawer(cfg=_cfg_d3, routes=[MAIN_A], pts=PIT26_PTS,
                  acts=PIT26_ACTS)._jump_band(), None)
check("（对照）max_drop=8 就够得着 → (25,35)",
      _new_drawer(routes=[MAIN_A], pts=PIT26_PTS, acts=PIT26_ACTS)._jump_band(),
      (25, 35))
check("★ 本文件的 CFG 没配这两个键 → 用默认 (8,5)（缺键回落，不崩）",
      (jump_band_params(CFG)["home_jump_band_max_drop"],
       jump_band_params(CFG)["home_jump_band_inset"]),
      (DEFAULT_JUMP_BAND_PARAMS["home_jump_band_max_drop"],
       DEFAULT_JUMP_BAND_PARAMS["home_jump_band_inset"]))

# ── 17-f 端到端：_jump_band → make_blob → render → 落盘着色的真的是三态 ─────
# ⚠️ band（主线给的，x 25~45）和**横段画到哪儿**（用户点出来的）是两码事：
#    paint 只给"画到的像素"上色。所以下面分两种线形验，别混为一谈。
_d6 = _new_drawer(routes=[MAIN_A, MAIN_B], pts=PIT26_PTS, acts=PIT26_ACTS)
_band6 = _d6._jump_band()
blob6 = make_blob(PIT26_PTS, PIT26_ACTS, True, _band6)
rgb6 = _render_rgb([blob6])
check("端到端：band = (25,45)", _band6, (25, 45))
check("端到端：区间内 (30,26) = 洋红「原地跳」",
      tuple(int(v) for v in rgb6[26, 30]), (255, 0, 255))
check("端到端：区间内左端 (25,26) = 洋红",
      tuple(int(v) for v in rgb6[26, 25]), (255, 0, 255))
check("端到端：区间左边 (20,26) = 蓝（往右走回区间）",
      tuple(int(v) for v in rgb6[26, 20]), (0, 0, 255))
# 线形①：横段只画到 x=40（比区间窄）→ 20 蓝 + 21~40 全洋红，x>40 压根没画线
check("线形①（横段 x20~40，比区间窄）：x20 蓝 / x21~40 全洋红",
      sorted({tuple(int(v) for v in rgb6[26, x]) for x in range(20, 41)}),
      [(0, 0, 255), (255, 0, 255)])
check("线形①：横段外面 (41,26) 没画线（不是被误着色）",
      tuple(int(v) for v in rgb6[26, 41]), (0, 0, 0))
# 线形②：横段画到 x=55（比区间宽）→ 右边多出来的那段必须是"往左走回区间"
WIDE_PTS = [(20, 26), (55, 26), (30, 26), (30, 20)]
rgb7 = _render_rgb([make_blob(WIDE_PTS, ["walk", "walk", "jump"], True, _band6)])
check("线形②（横段 x20~55）：区间右边 (50,26) = 红（往左走回区间）",
      tuple(int(v) for v in rgb7[26, 50]), (255, 0, 0))
check("线形②：区间右边末端 (55,26) = 红",
      tuple(int(v) for v in rgb7[26, 55]), (255, 0, 0))
check("线形②：区间内右端 (45,26) 仍是洋红",
      tuple(int(v) for v in rgb7[26, 45]), (255, 0, 255))
check("★ 线形②：整条横段**只有**蓝/洋红/红三色（左走 → 跳 → 右走的三态分区）",
      sorted({tuple(int(v) for v in rgb7[26, x]) for x in range(20, 56)}),
      [(0, 0, 255), (255, 0, 0), (255, 0, 255)])
# 单像素口径与落盘一致（画布预览走 hseg_pixel_color，必须与落盘同色）
check("★ 画布预览用的 hseg_pixel_color 与落盘同色（(30,26)）",
      hseg_pixel_color(30, 26, (30, 20), MERGED_CC, "walk", _band6), (255, 0, 255))
check("★ 画布预览与落盘同色（区间右边的 (50,26)）",
      hseg_pixel_color(50, 26, (30, 20), MERGED_CC, "walk", _band6), (255, 0, 0))

print("\n【18】★ 三处调用点**都**传了 band（防『新加调用点忘了传』）")
# 这条在防什么：`_jump_band` 算得再对，只要有一个调用点忘了传，那条路径画出来
# 就还是老着色 —— 而用例全绿（因为别的调用点是对的），线上没效果，最难查。
_src_pp = inspect.getsource(HomeRouteDrawer._preview_pts)
_src_pl = inspect.getsource(HomeRouteDrawer._preview_line)
_src_sv = inspect.getsource(HomeRouteDrawer._save_line)
_src_rv = inspect.getsource(HomeRouteDrawer._rebuild_view)
check("★ _preview_pts（实时体检）调了 _jump_band", "self._jump_band(" in _src_pp, True)
check("★ _preview_line（体检详情）调了 _jump_band", "self._jump_band(" in _src_pl, True)
check("★ _save_line（**落盘**）调了 _jump_band", "self._jump_band(" in _src_sv, True)
check("★ _rebuild_view（画布预览）也调了 _jump_band（所见即所得）",
      "self._jump_band(" in _src_rv, True)
check("make_blob 有 jump_band 形参",
      "jump_band" in inspect.signature(make_blob).parameters, True)
check("★ _save_line 里 make_blob 的第 4 个实参就是 _jump_band",
      "make_blob(pts, self.cur_actions, self.goal_set, self._jump_band(" in _src_sv,
      True)
check("★ _rebuild_view 用 hseg_pixel_color（不是自己另写一份取色）",
      "hseg_pixel_color(" in _src_rv, True)
check("_rebuild_view 里已**没有**旧的手动拐角取色（pick_color 那条）",
      "rgb = pick_color((x, p0[1]), (pts[ci][0], p0[1])," in _src_rv, False)


# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
if FAIL:
    print(f"❌ 自检失败 {len(FAIL)} 项：")
    for n in FAIL:
        print(f"   - {n}")
    sys.exit(1)
print("✅ 全部通过")
