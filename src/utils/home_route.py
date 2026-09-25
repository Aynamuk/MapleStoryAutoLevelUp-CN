# -*- coding: utf-8 -*-
'''
回正线（minimaps/<图>/route_home.png）领域层 —— **纯函数**，不依赖引擎实例 / Qt
（v0.9 第二版：删掉「落点锚」，改成「谁近走谁」）

为什么单独开这个模块：
    「动作→颜色 / 连通域划分 / 几何现算 / 覆盖检查 / 逐条判废 / 重绘 / 距离」
    这几件事，**引擎、手绘器、录制器、自检脚本都要用同一份**。
    塞进引擎里的话，离线自检就只能另写一份假逻辑 —— 那就变成
    「测的和跑的不是同一份代码」，自检全绿也保证不了线上不出事。
    放公共模块，才能做到"测的就是跑的"。

★ 本次（第二版）唯一新增的运行期概念：
    d_home = 角色 → 最近**已武装**回正线像素 的曼哈顿距离
    d_main = 角色 → 最近主线像素 的曼哈顿距离（全图，不受 search_range 限制）
    home_should_return(d_home, d_main, tol) = (d_home < d_main - tol)
    进入 / 保持 / 退出**共用这一个表达式**，口径唯一。

数据模型（唯一真源）：
    一条回正线 = route_home.png 上的一个 **8-连通域**，由两类像素构成
        · 段   : 走/跳/上/下 的色码像素（沿用 route.color_code / color_code_up_down）
        · 终点 : 255,255,0（goal）
    落点锚（127,0,127）已在第二版**彻底删除**；旧图上残留的由 wipe_legacy_anchor 涂黑。
    ⚠️ route_home.json 只是**给人看的备注**，本模块的判废/现算**一律不读它**。

颜色空间约定（跨文件，别搞反）：
    · PNG 落盘是 **BGR**（cv2）；config 里 color_code 的键是 **RGB**
    · 本模块所有 `img_rgb` 参数一律 **RGB**（引擎已 cvtColor 过）
    · 只有 `paint_converging_hseg` / `stamp_goal` / `render_home_image` 收 **BGR**
      （画完直接落盘），调用方给 RGB 色值，函数内部负责 [::-1]

中文路径：本模块自己不读盘（图像由调用方 load_image 传进来）；
          备注 json 的读写走 os 接口，路径由 resource_path 解析。
'''
from __future__ import annotations

import json
import os
import tempfile

import cv2
import numpy as np

from src.utils.logger import logger

try:  # 备注文件需要按"应用根目录"解析路径（打包后也能找到）
    from src.utils.paths import resource_path
except Exception:  # pragma: no cover - 极端环境下退回原样路径，不影响主流程
    def resource_path(p):
        return p


# ══════════════════════════════════════════════════════════════════════
# 常量与默认值
# ══════════════════════════════════════════════════════════════════════

#: 【v0.9 第一版已废弃】落点锚的旧色码。**只**用于旧图迁移（wipe_legacy_anchor 涂黑）。
#: ⚠️ 它不是配置项、不进任何色码表、不参与任何判定 —— 第二版没有第三种色码。
LEGACY_ANCHOR_RGB = (127, 0, 127)

#: 终点（goal）色码
DEFAULT_GOAL_RGB = (255, 255, 0)

#: 跳跃类色码（用于判断"这条线含不含跳跃"）
DEFAULT_JUMP_RGBS = {
    (255, 127, 0),   # 🟠 左跳
    (0, 255, 255),   # 🟦 右跳
    (127, 255, 0),   # 💚 下跳
    (255, 0, 255),   # 💜 原地跳
}

#: 回正专用参数默认值（与 config_default.yaml 的 route: 段一一对应）
#: ⚠️ 所有读取一律走 home_params(cfg)，禁止 cfg["route"]["xxx"]（缺键会崩）
DEFAULT_HOME_PARAMS = {
    # ── 判定 ──────────────────────────────────────────────────────────
    "home_dist_tol": 2,               # 比较容差：d_home < d_main - tol 才进/保持回正
    # ── 判废 ──────────────────────────────────────────────────────────
    "home_min_return_span": 5,        # 判废③：起点→终点跨度 < 5 判废
    "home_min_cover_span": 5,         # 判废④：起始段 x 跨度 < 5 判废（只画竖线 = 1）
    "home_cover_far_tol": 2,          # 「离主线最远那一批」的容差：d_main >= dmax - 本值
    "home_goal_mainline_tol": 1,      # 判废⑤：goal 像素到主线的最小距离 > 本值 判废
    # ── 运行期 ────────────────────────────────────────────────────────
    "home_min_leave": 0,              # 保险丝：离开进入点 ≥ N px 才允许判"已回主线"（0=关）
    "home_min_frames": 0,             # 保险丝：已回正 ≥ N 帧也算门开（0=关）
    "home_timeout": 8,                # 回正超时（秒），必须 < watchdog.timeout
    "home_reenter_lock": 10,          # 回正**失败**后的冷静期（秒），卡**所有**入口
    "home_rearm_margin": 4,           # 去武装后重新武装：角色离开该线 bbox ≥ 本值
    "home_rearm_timeout": 30,         # 去武装兜底：30s 后无条件重新武装
}

#: 参数合法区间（D4：这些旋钮"关不掉"—— 配成 0 / 负数时回落默认，而不是禁用）
#:   值 = (回落默认值, 是否必须 > 0)
#: ⚠️ 为什么必须硬拦：home_timeout / home_reenter_lock / home_rearm_timeout / 两个 span
#:    任意一个被配成 0，都会让第一版修掉的「8s 超时 → 立刻重进」死锁原样复活
#:    （新判定天然不带 armed 概念，超时退出后角色仍在坑底，下一帧 d_home 仍是 0）。
_HOME_PARAM_FLOORS = {
    "home_timeout": (8, True),
    "home_reenter_lock": (10, True),
    "home_rearm_timeout": (30, True),
    "home_min_return_span": (5, True),
    "home_min_cover_span": (5, True),
    "home_dist_tol": (2, False),
    "home_cover_far_tol": (2, False),
    "home_goal_mainline_tol": (1, False),
    "home_rearm_margin": (4, False),
    "home_min_leave": (0, False),
    "home_min_frames": (0, False),
}

#: 手绘器专用默认值（config 的 route_recoder: 段）
DEFAULT_DRAW_PARAMS = {
    "home_draw_scale": 4,
    "home_draw_scale_min": 3,
    "home_draw_scale_max": 6,
    "home_draw_goal_radius": 1,
}

#: ★「跳跃区间」（jump band）专用默认值（config 的 route: 段，2026-09-14 加）
#:
#: 背景（用户实测痛点）：上下两层平台的地形里，下层（坑底）的回正线通常画得比上层
#: 主线**长**。比如主线横跨 x 20~40，坑底回正线横跨 x 15~45 —— 角色在 x=15~25
#: 附近往左跳，大概率**跳不到平台上**；右侧同理。光靠「汇聚到单拐角」只在拐角那一
#: 列能起跳，角色一帧走好几像素根本落不上去，于是在横段两端来回震荡；偶尔蹭到那一
#: 列时人还在横移，起跳带着横向惯性 → 斜着跳出去 / 跳回坑里。
#:
#: 所以横段从「二态汇聚」升级成「**三态分区**」：
#:     角色 x <  band[0]        → 往**右**走（走向平台正下方）
#:     band[0] <= x <= band[1]  → **原地起跳**（已经在主线正下方了）
#:     角色 x >  band[1]        → 往**左**走
#: band 就是「主线正下方」这个 x 区间，由 calc_jump_band() 现算。
#:
#: ⚠️ 为什么单独一个字典、不塞进 DEFAULT_HOME_PARAMS：
#:    DEFAULT_HOME_PARAMS 是**运行期判定/判废**参数，条数被 verify_home_route_qa
#:    钉死（"有 11 个键"），塞进去会让那条断言红；而且 jump band 只在**手绘着色**
#:    这一刻用一次，引擎运行期一个字节都不读它，混在一起反而误导。
DEFAULT_JUMP_BAND_PARAMS = {
    "home_jump_band_max_drop": 8,   # 见 config_default.yaml 的注释（落差上限）
    # ⚠️ 5，不是 1（2026-09-14 改，与 config_default.yaml **必须保持一致**）：
    #    inset=1 时跳跃区几乎铺满落点横段 → 角色在坑底第 1 帧原地起跳、
    #    起跳 x = 落点 x → 落在平台外/贴边缘（用户实测「起跳很晚、跳不上平台」）。
    #    inset=5 才把它收成中间一小段（废都南方工地实测：x 97~101，距边缘各 5px）。
    "home_jump_band_inset": 5,      # 见 config_default.yaml 的注释（距平台边缘的安全余量）
}

#: jump band 两个旋钮的**合法区间**（非法值回落默认，口径同 _HOME_PARAM_FLOORS）
_JUMP_BAND_FLOORS = {
    "home_jump_band_max_drop": (8, 0),     # (回落默认, 下界)；0 = 只看同 y 那一层
    # ⚠️ 回落默认同样是 5：填负数 / 老配置缺键时回落到 1 = 把"跳跃区铺满横段"
    #    这个已修的坏值原样放回来（静默失效，最难查）。fallback 必须回落到**安全默认**。
    "home_jump_band_inset": (5, 0),        # 0 = 不内缩
}


# ══════════════════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════════════════

def manhattan(a, b) -> int:
    '''曼哈顿距离 |dx| + |dy|（本项目一切"多少 px"都是它，别用欧氏）。'''
    return abs(int(a[0]) - int(b[0])) + abs(int(a[1]) - int(b[1]))


def _as_code_set(codes) -> set:
    '''把 {RGB: cmd} 字典或 RGB 集合/列表统一成 set(tuple)。空 → 空集。'''
    if not codes:
        return set()
    if isinstance(codes, dict):
        return {tuple(c) for c in codes.keys()}
    return {tuple(c) for c in codes}


def _as_code_map(codes) -> dict:
    '''把 {RGB: cmd} 字典或 RGB 集合统一成 {RGB: cmd}（集合时 cmd 为空串）。'''
    if not codes:
        return {}
    if isinstance(codes, dict):
        return {tuple(c): str(v) for c, v in codes.items()}
    return {tuple(c): "" for c in codes}


def code_dict_to_str(code_dict: dict) -> dict:
    '''{(255,0,0): "..."} → {"255,0,0": "..."}（mask_route_colors 要字符串键）。'''
    return {f"{int(c[0])},{int(c[1])},{int(c[2])}": v for c, v in (code_dict or {}).items()}


def _to_int(v, default):
    '''安全转 int（配置里可能是字符串 / None / 小数）。失败回落 default。'''
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def home_params(cfg) -> dict:
    '''取回正专用参数（缺键**或非法值**一律回落默认值）。

    ⚠️ 纪律 1：禁止 `cfg["route"]["home_xxx"]` —— 老用户配置没有这些键，
        直接下标会在 load_config 里抛 KeyError，而那里没有 try/except，
        表现是「点开始没反应」，极难查。

    ⚠️ 纪律 2（D4）：`home_timeout` / `home_reenter_lock` / `home_rearm_timeout`
        这三个旋钮**关不掉**。用户配成 0 或负数时这里直接回落默认值 ——
        不是"尊重用户"，而是"填 0 会把已修的回正死锁原样放回来"：
          · home_timeout=0 → 超时出口消失 → 卡在回正里既不走也不打；
          · home_reenter_lock=0 → 超时那一帧被老路径同帧拉回 → home_enter_t 每帧归零；
          · home_rearm_timeout=0 → 坏线立刻重新武装 → 等于没去武装。
        同理两个 span 判废阈值配 0 会让"只画竖线"这种废线被放行。
    '''
    route_cfg = ((cfg or {}).get("route") or {})
    out = dict(DEFAULT_HOME_PARAMS)
    for k in DEFAULT_HOME_PARAMS:
        v = route_cfg.get(k)
        if v is not None:
            out[k] = v
    for k, (floor, must_pos) in _HOME_PARAM_FLOORS.items():
        val = _to_int(out.get(k), None)
        if val is None or (must_pos and val <= 0) or (not must_pos and val < 0):
            out[k] = floor
        else:
            out[k] = val
    return out


def draw_params(cfg) -> dict:
    '''取手绘器参数（route_recoder: 段，缺键回落默认）。'''
    rec_cfg = ((cfg or {}).get("route_recoder") or {})
    out = dict(DEFAULT_DRAW_PARAMS)
    for k in DEFAULT_DRAW_PARAMS:
        v = rec_cfg.get(k)
        if v is not None:
            out[k] = v
    return out


def jump_band_params(cfg) -> dict:
    '''取「跳跃区间」参数（route: 段的 home_jump_band_max_drop / _inset）。

    ⚠️ 缺键 / 非法值（负数、非数字）一律回落默认值 —— 与 home_params 同一条纪律：
        老用户的 config.yaml 里没有这两个键，直接下标会 KeyError，而手绘器保存
        路径上没有 try/except，表现是「点保存没反应」。

    Returns:
        {"home_jump_band_max_drop": int, "home_jump_band_inset": int}
    '''
    route_cfg = ((cfg or {}).get("route") or {})
    out = dict(DEFAULT_JUMP_BAND_PARAMS)
    for k in DEFAULT_JUMP_BAND_PARAMS:
        v = route_cfg.get(k)
        if v is not None:
            out[k] = _to_int(v, out[k])
    for k, (floor, lo) in _JUMP_BAND_FLOORS.items():
        val = _to_int(out.get(k), None)
        out[k] = floor if (val is None or val < lo) else val
    return out


def color_code_maps(cfg):
    '''从 cfg 反查**两张**色码表（RGB 元组 → 命令串）。

    Returns:
        (color_code, color_code_up_down)
        ⚠️ 第二版**只有两张表** —— 落点锚的第三张表（color_code_home_anchor）已删除。
    '''
    route_cfg = ((cfg or {}).get("route") or {})

    def _parse(raw):
        out = {}
        for k, v in (raw or {}).items():
            try:
                parts = tuple(int(p) for p in str(k).split(','))
            except (TypeError, ValueError):
                continue
            if len(parts) >= 3:
                out[(parts[0], parts[1], parts[2])] = v
        return out

    return _parse(route_cfg.get("color_code")), _parse(route_cfg.get("color_code_up_down"))


def _rgb_at(img_rgb, pt):
    '''取 img_rgb 上 pt 处的 RGB 元组（越界返回 None）。'''
    if img_rgb is None or pt is None:
        return None
    y, x = int(pt[1]), int(pt[0])
    if y < 0 or x < 0 or y >= img_rgb.shape[0] or x >= img_rgb.shape[1]:
        return None
    return tuple(int(v) for v in img_rgb[y, x][:3])


def goal_codes(codes) -> set:
    '''从色码表（字典或集合）里挑出 goal 色。'''
    cmap = _as_code_map(codes)
    if cmap and any(str(v).strip() for v in cmap.values()):
        return {c for c, v in cmap.items() if str(v).strip().split()[-1] == "goal"}
    return {DEFAULT_GOAL_RGB}


def is_jump_color(rgb, code_map=None) -> bool:
    '''这个色是不是"跳跃类"（决定这条线要不要跳一下）。'''
    rgb = tuple(int(v) for v in rgb)
    if code_map:
        cmd = code_map.get(rgb)
        if cmd:
            return str(cmd).strip().split()[-1] == "jump"
    return rgb in DEFAULT_JUMP_RGBS


# ══════════════════════════════════════════════════════════════════════
# 像素扫描 / 连通域
# ══════════════════════════════════════════════════════════════════════

def _match_mask(img_rgb, codes) -> np.ndarray:
    '''img_rgb 上命中 codes 的布尔掩膜（H×W）。codes 为空 → 全 False。'''
    codes = _as_code_set(codes)
    if img_rgb is None or not codes:
        return np.zeros((0, 0), dtype=bool)
    arr = img_rgb[:, :, :3]
    mask = np.zeros(arr.shape[:2], dtype=bool)
    for c in codes:
        # ⚠️ 逐通道比较再 &，比 `np.all(arr == c, axis=-1)` 少一次全图广播临时数组
        #    （2026-09-17 实测：这条路径被调 900+ 次时总耗时能差 3 秒）
        ci = (int(c[0]), int(c[1]), int(c[2]))     # 与旧写法一致：小数截断
        mask |= ((arr[:, :, 0] == ci[0]) & (arr[:, :, 1] == ci[1])
                 & (arr[:, :, 2] == ci[2]))
    return mask


#: `route_pixels_cached()` 的缓存：(id(img), codes_key) → (weakref(img), xs, ys)
_ROUTE_PIXEL_CACHE: dict = {}


def route_pixels_cached(img_rgb, codes):
    '''`collect_pixels` 的**带缓存**版本：同一张图 + 同一套色码只扫一次。

    ⚠️ 为什么必须缓存（2026-09-17 真机踩到，UI 卡死 8~11 秒）：
        `nearest_mainline_point()` 每调一次就 `_match_mask()` **重建一次全图掩膜**，
        而回正线体检要对**每个像素**问一次 d_main ⇒ 473 个点 × 2 张主线图
        = 946 次全图比较。cProfile 实测 `audit_map` 一次 **11.3s**，其中 10.5s
        全在这里。表现：录制器点【已回到主线】后卡 8 秒、界面【结束录制】卡 10 秒。
        缓存后同一张图只扫一次（实测 audit 掉到 0.2s 以内）。

    Returns:
        (xs, ys): np.int32 数组（可能为空）；img 为 None → (None, None)
    '''
    import weakref
    if img_rgb is None:
        return None, None
    key = (id(img_rgb), tuple(sorted(_as_code_set(codes))))
    hit = _ROUTE_PIXEL_CACHE.get(key)
    if hit is not None and hit[0]() is img_rgb:
        return hit[1], hit[2]
    pts = collect_pixels(img_rgb, codes)
    if len(_ROUTE_PIXEL_CACHE) > 64:        # 兜底：别让缓存无限长（正常只会用到个位数）
        _ROUTE_PIXEL_CACHE.clear()
    xs = np.asarray([p[0] for p in pts], dtype=np.int32)
    ys = np.asarray([p[1] for p in pts], dtype=np.int32)
    _ROUTE_PIXEL_CACHE[key] = (weakref.ref(img_rgb), xs, ys)
    return xs, ys


def collect_pixels(img_rgb, codes) -> list:
    '''扫出图上所有路线色码像素，返回 [(x, y), ...]（行优先，顺序稳定）。'''
    mask = _match_mask(img_rgb, codes)
    if mask.size == 0 or not mask.any():
        return []
    ys, xs = np.nonzero(mask)
    return [(int(x), int(y)) for y, x in zip(ys, xs)]


def split_components(img_rgb, codes) -> list:
    '''8-连通域划分：**一个组件 = 一条回正线**。

    Returns:
        list[list[(x,y)]]，已按"最上、最左"排序（顺序稳定，界面列表才不会跳）
    '''
    mask = _match_mask(img_rgb, codes)
    if mask.size == 0 or not mask.any():
        return []
    n, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    comps = []
    for i in range(1, int(n)):
        ys, xs = np.nonzero(labels == i)
        comps.append([(int(x), int(y)) for y, x in zip(ys, xs)])
    # np.nonzero 是行优先 → 每个组件的首元素天然是"最上、最左"
    comps.sort(key=lambda c: (c[0][1], c[0][0]))
    return comps


def component_has_jump(img_rgb, comp_pts, codes) -> bool:
    '''这个组件里有没有跳跃类像素（决定"跳一下就上来"能不能成立）。'''
    cmap = _as_code_map(codes)
    for p in comp_pts or []:
        rgb = _rgb_at(img_rgb, p)
        if rgb is not None and is_jump_color(rgb, cmap):
            return True
    return False


# ══════════════════════════════════════════════════════════════════════
# 距离（全图最近像素，曼哈顿，numpy 向量化）
# ══════════════════════════════════════════════════════════════════════

def dist_to_pixels(xs, ys, pt):
    '''点到一批像素的**最近曼哈顿距离**（numpy 向量化）。

    ⚠️ 为什么必须向量化：每帧都要算 d_home / d_main，268×187 全图 Python 双循环
       约 5 万次/帧，30fps 下吃不消；而主线只有 ~31 个点、回正线 ~21 个点，
       numpy 一次 < 20µs。坐标数组在 load_config 里建一次。

    Args:
        xs / ys: np.int32 坐标数组（等长）
        pt: (x, y)

    Returns:
        int | None: 最近距离；数组为空 / pt 为 None → None
    '''
    if pt is None or xs is None or ys is None:
        return None
    try:
        n = len(xs)
    except TypeError:
        return None
    if n == 0 or len(ys) == 0:
        return None
    px, py = int(pt[0]), int(pt[1])
    return int(np.min(np.abs(xs - px) + np.abs(ys - py)))


def nearest_mainline_point(pt, route_imgs, codes, max_radius=None):
    '''点到**主线**（route{N}.png）最近路线像素的坐标；一条主线都没有时返回 None。

    Args:
        pt: (x, y) 小地图全局坐标
        route_imgs: 主线 RGB 图列表（已 mask）
        codes: 路线色码集合/字典
        max_radius: 超过这个距离就当没有（None = 不限）
    '''
    if pt is None:
        return None
    px, py = int(pt[0]), int(pt[1])
    best, best_d = None, None
    for img in route_imgs or []:
        if img is None:
            continue
        # ⚠️ 必须用**带缓存**的版本（2026-09-17）：这里每个像素调一次，
        #    重建全图掩膜会让回正线体检变成 11 秒（详见 route_pixels_cached）
        xs, ys = route_pixels_cached(img, codes)
        if xs is None or len(xs) == 0:
            continue
        d_arr = np.abs(xs - px) + np.abs(ys - py)
        d = int(d_arr.min())
        if max_radius is not None and d > max_radius:
            continue
        if best_d is None or d < best_d:
            i = int(d_arr.argmin())
            best_d, best = d, (int(xs[i]), int(ys[i]))
    return best


def nearest_mainline_dist(pt, route_imgs, codes, max_radius=None):
    '''点到最近主线像素的**曼哈顿距离**；够不着/没有主线时返回 None。'''
    if pt is None:
        return None
    p = nearest_mainline_point(pt, route_imgs, codes, max_radius)
    if p is None:
        return None
    return manhattan(pt, p)


def mainline_dist(pt, route_imgs, codes):
    '''= nearest_mainline_dist(pt, route_imgs, codes)（**不限半径**）。

    ⚠️ d_main 必须**全图**最近像素。套 search_range / rescue_range 会复活第一版的
       坑 1（坑底离主线只有 6px < search_range 10 → 引擎"透过地板看得见主线"
       → 判定在主线上 → 永远不进回正）。
    '''
    return nearest_mainline_dist(pt, route_imgs, codes)


def home_should_return(d_home, d_main, tol) -> bool:
    '''★ 新判定的**唯一实现**（进入 / 保持 / 退出共用，口径唯一）。

        d_home < d_main - tol  →  进 / 保持回正状态，沿回正线走回主线
        否则                   →  主线优先（继续巡逻，或退出回正）

    ⚠️ 严格小于号天然实现「相等时主线优先」（需求方定的规则）：
       角色走到回正线终点（终点画在主线上）时 d_home = d_main = 0 → 0 < -2 不成立 → 自动退出。

    ⚠️ 任何一个距离量不出来（None）→ 不进回正，交给老兜底「主线一帧像素都找不到」。
       （d_main 为 None 通常是这张图压根没有主线，此时"回主线"无从谈起。）

    Args:
        d_home: 角色到最近**已武装**回正线的距离；None = 没有可用回正线
        d_main: 角色到最近主线像素的距离；None = 量不出
        tol: 比较容差（< 0 时回落 2）
    '''
    if d_home is None or d_main is None:
        return False
    try:
        t = int(tol)
    except (TypeError, ValueError):
        t = 2
    if t < 0:
        t = 2
    return int(d_home) < int(d_main) - t


# ══════════════════════════════════════════════════════════════════════
# 现算（加载期算一次，不落盘 —— 不存就不漂移）
# ══════════════════════════════════════════════════════════════════════

def _goal_centroid(pts, img_rgb, codes):
    '''组件里 goal 像素的质心（没有 goal → None）。'''
    gset = goal_codes(codes)
    g_pts = [q for q in pts if _rgb_at(img_rgb, q) in gset]
    if not g_pts:
        return None, []
    return (sum(q[0] for q in g_pts) // len(g_pts),
            sum(q[1] for q in g_pts) // len(g_pts)), g_pts


def calc_line_geom(comp_pts, route_imgs, codes, cfg, img_rgb=None) -> dict:
    '''一条回正线的全部几何量 —— **加载时现算，不落盘**。

    第二版只算 start / goal / span / has_jump / n_pts / bbox；
    第一版的 D（落点到主线）/ R（触发半径）/ away（方向侧）**全部删除**：
    判定改成比两条线的距离，不再需要这些中间量。

    start = 组件里**离主线最远**的那个像素（≈ 坑底 / 落点区）
    span  = start → goal 的曼哈顿距离（判废③）

    Args:
        comp_pts: 该连通域的全部像素
        route_imgs: 主线 RGB 图列表
        codes: 路线色码集合/字典
        cfg: 完整配置（本函数暂不用，留作签名统一）
        img_rgb: **可选**，回正线 RGB 图。传了才能算 has_jump（要看像素颜色）。

    Returns:
        dict: {'start', 'goal', 'span', 'has_jump', 'n_pts', 'bbox'}
              start / goal 可能为 None（那时这条线会被判废）
    '''
    pts = list(comp_pts or [])
    if not pts:
        return {"start": None, "goal": None, "span": 0, "has_jump": False,
                "n_pts": 0, "bbox": None}

    goal, _g_pts = _goal_centroid(pts, img_rgb, codes) if img_rgb is not None else (None, [])
    # start = 离主线最远的像素（并列时取"最上、最左"那个，保证顺序稳定）
    best, best_d = None, None
    for q in sorted(pts, key=lambda p: (p[1], p[0])):
        d = mainline_dist(q, route_imgs, codes)
        if d is None:
            continue
        if best_d is None or d > best_d:
            best_d, best = d, q
    if best is None:                      # 量不出（没有主线）→ 退化成"最上、最左"
        best = min(pts, key=lambda p: (p[1], p[0]))

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = manhattan(best, goal) if goal is not None else 0
    return {
        "start": best,
        "goal": goal,
        "span": span,
        "has_jump": component_has_jump(img_rgb, pts, codes) if img_rgb is not None else False,
        "n_pts": len(pts),
        "bbox": (min(xs), min(ys), max(xs), max(ys)),
    }


def far_segment_stats(comp_pts, route_imgs, codes, cfg) -> dict:
    '''**起始段**（离主线最远的那一批像素）的水平覆盖范围 —— 判废④ 的地基。

        dmax = max(d_main(p))
        far_pts = { p | d_main(p) >= dmax - home_cover_far_tol }
        cover_span = max(x) - min(x) + 1

    🔬 已验算（设计 4.3，实现时照着对）：
      · L 形（横段 x=94..108,y=128 + 竖段 x=99,y=123..127；主线 y=122,x∈[92,106]）
        → dmax = 8（x=108）；far_pts = 整条横段（d ≥ 6）→ **cover_span = 15** ✅ 通过
      · 只画竖线（x=100,y=123..128）→ dmax = 6（y=128）；far_pts = {126,127,128}
        → **cover_span = 1** ❌ 判废 ✅（精确命中 PRD 2.4 的反例）

    Returns:
        dict: {'far_pts', 'cover_xs', 'cover_span'}
    '''
    p = home_params(cfg)
    tol = int(p["home_cover_far_tol"])
    scored = []
    for q in comp_pts or []:
        d = mainline_dist(q, route_imgs, codes)
        if d is not None:
            scored.append((q, d))
    if not scored:
        return {"far_pts": [], "cover_xs": None, "cover_span": 0}
    dmax = max(d for _, d in scored)
    far = [q for q, d in scored if d >= dmax - tol]
    xs = [q[0] for q in far]
    return {
        "far_pts": far,
        "cover_xs": (min(xs), max(xs)),
        "cover_span": int(max(xs) - min(xs) + 1),
    }


def floor_row_span(comp_pts, route_imgs=None, codes=None, cfg=None) -> dict:
    '''**落点行**（这条回正线像素 y 的众数那一行）的 x 跨度 —— 判废④ 的第二判据。

    为什么要加它（2026-09-17 真机反例，`废都南方工地` 实测）：
        用户从落点 **(34,126)** 沿坑底横着走到 x=84、再爬上台子回到主线 ——
        坑底**整条盖住了**（y=126 上 x 34~84，共 51px）。
        但 `far_segment_stats` 的口径是"离主线**最远**的那一批像素"，
        而坑底离主线的距离是**平滑变化**的（x 越大越近：30/29/28…到 x=48 之后恒为 18）
        ⇒ "最远那批"只挑出最左的 3 个点 (34/35/36) ⇒ cover_span=3 < 5 ⇒ **判废**。
        **可这条线其实完全能用**：按引擎自己的落点判定扫
        y=126~128 × x=38~92 共 165 个落点，**165/165 全部接得住**
        （对照：只画竖线只有 27/165）。
    ⇒ 所以再加一条"落点行有多宽"。**两条满足一条即过（OR）**，取更宽的那个当报告值。

    ⚠️ 为什么不直接替换 `far_segment_stats`：它的语义被自检钉死了
       （L 形=15、只画竖线=1，见 verify_home_route / verify_home_drawer），
       而且它对"只画竖线"的命中是对的。**两边都要留住。**

    Returns:
        dict: {'row_y', 'xs', 'span'}；无像素 → {'row_y': None, 'xs': None, 'span': 0}
    '''
    pts = list(comp_pts or [])
    if not pts:
        return {"row_y": None, "xs": None, "span": 0}
    ys = np.asarray([q[1] for q in pts], dtype=np.int32)
    vals, cnts = np.unique(ys, return_counts=True)
    # 并列时取 **y 最大**的那行 = 最靠坑底（回正线是从坑底往上画的）
    row_y = int(np.max(vals[cnts == cnts.max()]))
    row_xs = sorted(int(q[0]) for q in pts if int(q[1]) == row_y)
    return {"row_y": row_y, "xs": (row_xs[0], row_xs[-1]),
            "span": int(row_xs[-1] - row_xs[0] + 1)}


def cover_stats(comp_pts, route_imgs, codes, cfg) -> dict:
    '''判废④ 的**完整**覆盖口径 = 「离主线最远那批像素」与「落点行」取更宽的那个。

    两个口径各管一半：前者抓"只画一条斜线/竖线"，后者抓"坑底到底盖没盖住"。
    报告里带上 `by` 说明是哪一边给出的值，省得以后看日志的人再猜。
    '''
    far = far_segment_stats(comp_pts, route_imgs, codes, cfg)
    row = floor_row_span(comp_pts, route_imgs, codes, cfg)
    if row["span"] > far["cover_span"]:
        return {**far, "cover_xs": row["xs"], "cover_span": row["span"],
                "row_y": row["row_y"], "by": "落点行"}
    return {**far, "row_y": row["row_y"], "by": "起始段"}


def simulate_coverage(home_pts, route_pts, landing_pts, tol=2) -> dict:
    '''P1-1 覆盖率模拟（**手绘器与自检共用**）：落点区间里每个像素跑一遍新判定。

    对落点区间（用户拖的蓝带）里的每个像素算 d_home / d_main，
    看 `home_should_return` 成不成立 —— 成立 = 这个落点接得住。

    Returns:
        dict: {'total', 'covered', 'missed': [(x, y), ...]}
    '''
    home_pts = list(home_pts or [])
    route_pts = list(route_pts or [])
    total, covered, missed = 0, 0, []
    if not home_pts or not route_pts:
        for lp in landing_pts or []:
            total += 1
            missed.append((int(lp[0]), int(lp[1])))
        return {"total": total, "covered": covered, "missed": missed}

    hx = np.asarray([int(p[0]) for p in home_pts], dtype=np.int32)
    hy = np.asarray([int(p[1]) for p in home_pts], dtype=np.int32)
    rx = np.asarray([int(p[0]) for p in route_pts], dtype=np.int32)
    ry = np.asarray([int(p[1]) for p in route_pts], dtype=np.int32)
    for lp in landing_pts or []:
        total += 1
        d_home = dist_to_pixels(hx, hy, lp)
        d_main = dist_to_pixels(rx, ry, lp)
        if home_should_return(d_home, d_main, tol):
            covered += 1
        else:
            missed.append((int(lp[0]), int(lp[1])))
    return {"total": total, "covered": covered, "missed": missed}


# ══════════════════════════════════════════════════════════════════════
# 逐条判废（第二版判据，见设计 4.3）
# ══════════════════════════════════════════════════════════════════════

#: 人话文案（每条 = 「症状 + 怎么改」，逐字照抄设计 4.3 判据表）
MSG_NO_GOAL = "这条回正线没有终点标记（黄点）—— 画到最后一点后点【保存】才会自动盖上"
MSG_NO_MOVE = "这条回正线一个动作像素都没有，等于没画"
MSG_SPAN = ("这条回正线的起点和终点只差 {span}px —— 原地起头原地收尾。"
            "起点画在台子**下面**，终点画在**主线上**")
MSG_NO_MAINLINE = ("这张图没有可用的主线路线，量不出回正线离主线多远 "
                   "—— 请先录一段主线，再画回正线")
MSG_COVER = ("这条回正线只接得住 x={x} 这一个点 —— 角色掉在坑的左右两边会接不住。"
             "请先沿着坑底横着画一段，把整个坑底都盖上")
MSG_GOAL_FAR = ("这条回正线的终点没画在主线上（离主线还有 {d}px）—— "
                "角色走到终点也不会被判定「已回到主线」，会原地干等 {timeout}s。"
                "请把最后一点画在主线的彩色像素上")


def validate_home_line(comp_pts, img_rgb, route_imgs, codes, cfg):
    '''**逐条**判废（一条废只抹这一条，不连坐同图其它线）。

    第二版判据（全部满足才启用）：
      1. 含 goal 像素（255,255,0）
      2. 至少含 1 个位移/跳跃像素（只有 goal 等于没画）
      3. span = 起点→终点 曼哈顿 ≥ home_min_return_span（默认 5）
      4. 【新增】起始段覆盖跨度 cover_span ≥ home_min_cover_span（默认 5）
         —— 接住 PRD 2.4 的反例：只画一条竖线时 cover_span = 1，坑两端的落点接不住
      5. 【新增·判废不是警告】终点贴主线：goal **像素集合**到主线的最小距离
         ≤ home_goal_mainline_tol（默认 1）
         —— 终点离主线 5px 时，角色站上去仍是 d_home=0 < 5-2 → **永远出不来**，
            只能等 8s 超时（第一版 QA-18 那个洞在新方案里的对应物）
         ⚠️ 用 goal **像素集合的 min**（不是质心）：goal 是半径 1~2 的圆，
            只要有一个像素贴上主线就过，不会误杀"终点画得稍微偏一点"的线。
            ⚠️ 这个容差**拦不住**"竖段画在平台外"（圆必然碰到平台边缘）——
               那个盲区由手绘器的 jump_off_platform 黄字提示兜住。

    ⚠️ **删除**的第一版判据：
      · ~~组件必须含锚点像素~~（第二版没有锚点）
      · ~~落点到主线 D ≥ home_anchor_min_clearance(6)~~
        —— 坑底离主线本来只有 6px，这条会把所有合法的短回正线全杀掉

    ⚠️ 判据 5 用 goal **像素集合的 min**（不是质心）：5px 的黄圆点只要有一个像素
       贴上主线就过，不会误杀"终点画得稍微偏一点"的线。

    Returns:
        (bool, str): (是否有效, 无效原因；有效时原因为 "")
    '''
    p = home_params(cfg)
    pts = list(comp_pts or [])
    if not pts:
        return False, MSG_NO_MOVE

    gset = goal_codes(codes)
    cset = _as_code_set(codes)

    # ── 判据 1：有 goal ──────────────────────────────────────────────
    g_pts = [q for q in pts if _rgb_at(img_rgb, q) in gset]
    if not g_pts:
        return False, MSG_NO_GOAL

    # ── 判据 2：至少 1 个位移/跳跃像素 ────────────────────────────────
    move_pts = [q for q in pts if _rgb_at(img_rgb, q) in cset and _rgb_at(img_rgb, q) not in gset]
    if not move_pts:
        return False, MSG_NO_MOVE

    # ── 判据 3：跨度够 ───────────────────────────────────────────────
    geom = calc_line_geom(pts, route_imgs, codes, cfg, img_rgb=img_rgb)
    start, goal = geom["start"], geom["goal"]
    if goal is None:
        return False, MSG_NO_GOAL
    span = geom["span"]
    if span < int(p["home_min_return_span"]):
        return False, MSG_SPAN.format(span=span)

    # ── 有没有主线：量不出 d_main 时判据 4/5 无从谈起 ──────────────────
    if mainline_dist(start, route_imgs, codes) is None:
        return False, MSG_NO_MAINLINE

    # ── 判据 4【新增】：起始段覆盖跨度 ─────────────────────────────────
    # 2026-09-17 订正：口径从"只看起始段"扩成 cover_stats() 的**两者取宽**
    #   —— 否则"坑底横着盖满、但离主线距离平滑变化"的地形会被误判废
    #   （真机反例见 floor_row_span 的说明：165/165 落点接得住却判废）。
    cov = cover_stats(pts, route_imgs, codes, cfg)
    if cov["cover_span"] < int(p["home_min_cover_span"]):
        x0, x1 = cov["cover_xs"] if cov["cover_xs"] else (start[0], start[0])
        return False, MSG_COVER.format(x=x0 if x0 == x1 else f"{x0}~{x1}")

    # ── 判据 5【新增·判废】：终点必须贴主线 ────────────────────────────
    # 用 goal **像素集合的 min**，不是质心（5px 黄圆点有一个像素贴上就算过）
    d_goal = None
    for q in g_pts:
        d = mainline_dist(q, route_imgs, codes)
        if d is not None and (d_goal is None or d < d_goal):
            d_goal = d
    if d_goal is None:
        return False, MSG_NO_MAINLINE
    if d_goal > int(p["home_goal_mainline_tol"]):
        return False, MSG_GOAL_FAR.format(d=d_goal, timeout=int(p["home_timeout"]))

    return True, ""


# ══════════════════════════════════════════════════════════════════════
# 落盘 / 重绘
# ══════════════════════════════════════════════════════════════════════

def pick_color(p_from, p_to, action, color_code):
    '''段动作 → RGB 色码（与录制器按键着色规则一致）。

       走  : |dx|>=|dy| → 红(left)/蓝(right)；否则 灰(up)/浅黄(down)
       跳  : |dx|>=|dy| → 橙(left+jump)/青(right+jump)；否则 dy<0 → 洋红(原地跳)，
             dy>0 → 青绿(down+jump)
       上  : 灰 127,127,127        下 : 浅黄 255,255,127

    Args:
        p_from / p_to: (x, y)；y 向下为正
        action: "walk" / "jump" / "up" / "down"
        color_code: {RGB: "cmd_x cmd_y cmd_action"}（建议传**合并后**的表，
                    这样 up/down 这类只在 color_code_up_down 里的色也能查到）

    Returns:
        (R, G, B)；画到 BGR 图时调用方负责 [::-1]（本模块内部已处理）
    '''
    rev = {}
    for rgb, cmd in (color_code or {}).items():
        rev[str(cmd).strip()] = tuple(int(v) for v in rgb)

    dx = int(p_to[0]) - int(p_from[0])
    dy = int(p_to[1]) - int(p_from[1])

    if str(action) == "jump":
        if abs(dx) >= abs(dy):
            key = "left none jump" if dx < 0 else "right none jump"
        elif dy < 0:
            key = "none none jump"
        else:
            key = "none down jump"
    elif str(action) == "up":
        key = "none up none"
    elif str(action) == "down":
        key = "none down none"
    else:  # "walk" 及任何未知动作一律按"走"处理（宁可走，也不要画出非法色）
        if abs(dx) >= abs(dy):
            key = "left none none" if dx < 0 else "right none none"
        else:
            key = "none up none" if dy < 0 else "none down none"

    if key in rev:
        return rev[key]

    # 配置表里查不到（用户改过色码表）→ 回落到内置常量，保证画出来的色**一定合法**
    fallback = {
        "left none none": (255, 0, 0),
        "right none none": (0, 0, 255),
        "left none jump": (255, 127, 0),
        "right none jump": (0, 255, 255),
        "none none jump": (255, 0, 255),
        "none down jump": (127, 255, 0),
        "none up none": (127, 127, 127),
        "none down none": (255, 255, 127),
        "none none goal": DEFAULT_GOAL_RGB,
    }
    return fallback.get(key, (255, 0, 0))


def calc_jump_band(main_bgr, pit_y, max_drop=8, inset=1, color_code=None):
    '''★ 算「主线正下方」这个 x 区间 —— 角色站在这段里**原地起跳**就能上平台。

    为什么需要它（用户实测痛点，2026-09-14）：
        上下两层平台的地形里，下层（坑底）的回正线通常画得比上层主线**长**。
        主线横跨 x 92~106，坑底回正线横跨 x 94~108 —— 角色在 x=94 附近往左跳、
        在 x=108 附近往右跳，都**跳不到平台上**。旧口径「汇聚到单拐角」更糟：
        只有拐角那一列能起跳，角色一帧走好几像素根本落不上去 → 横段两端来回
        震荡；偶尔蹭到那一列时人还在横移 → 斜着跳出去 / 跳回坑里。
        正确规则是**三态分区**：区间左边往右走、区间内**原地跳**、区间右边往左走。

    口径（与引擎 `_build_mainline_cache` 完全一致，不自己发明）：
        主线像素 = 主线图上命中 `color_code` 的像素（引擎用的是
        `set(color_code) | set(color_code_up_down)`，调用方把合并后的表传进来即可）。
        ⚠️ **包含 goal**：引擎的主线像素缓存就是把 goal 算在内的，照抄这个口径，
           否则"终点到底算不算平台"这件事又会和 d_main 打架。

    只认**角色头顶上方、落差不超过 max_drop** 的那截主线：
        `pit_y - max_drop <= y < pit_y`
        即坑底横段所在行往上数 max_drop 行（不含横段自己那一行 —— 那一行是坑底，
        主线不可能长在坑底）。比坑底还高的主线（落差 > max_drop）是**上一层平台**，
        跳上去也够不着，不算。

    然后两端各向内缩 `inset`，防贴着平台边缘起跳踩空；缩完 `x0 > x1` → None。

    ⚠️ inset 会被**钳制**：先压到 `(x_max - x_min - 2) // 2`（保证缩完至少 3px 宽），
       压完仍然退化才返回 None。平台只有 1~2px 时上限是负数 → **不压**，
       直接走 x0 > x1 → None 的老分支（与升级前逐像素一致）。
       为什么必须钳：inset 默认提到 5 之后，15px 的短平台（x 92~106）
       会被缩成 5px（x 97~101，正好）；再窄的平台不钳就没区间了。

    Args:
        main_bgr: 主线（route{N}.png）的 **BGR** 图（cv2 落盘口径）；None → None
        pit_y: 回正线**横段**所在的 y（坑底），int
        max_drop: 认主线的最大落差（行），默认 8
        inset: 两端各内缩多少 px（= 跳跃区距**平台边缘**的安全余量）。
               ⚠️ 形参默认 1 只是"没传时的兜底"；**真实口径一律走
               `jump_band_params(cfg)["home_jump_band_inset"]`**（config 默认 5，
               缺键 / 填负数也回落 5），调用方（手绘器 _jump_band / 重生成脚本）必须显式传。
        color_code: {RGB: cmd} 或 RGB 集合（**合并后**的路线色码表）。
                    None / 空 → 认不出主线像素 → 返回 None（调用方回落老行为）

    Returns:
        (x0, x1) | None：闭区间；算不出来（没图 / 没像素 / 退化 / 没色码表）→ None
    '''
    if main_bgr is None:
        return None
    try:
        pit_y = int(pit_y)
        max_drop = int(max_drop)
        inset = int(inset)
    except (TypeError, ValueError):
        return None
    codes = _as_code_set(color_code)
    # 灰度图 / 空图 / 非法 inset 一律"算不出来" → 调用方回落老行为，不抛异常
    if not codes or inset < 0 or getattr(main_bgr, "ndim", 0) < 3 \
            or getattr(main_bgr, "size", 0) == 0:
        return None

    img_rgb = cv2.cvtColor(main_bgr, cv2.COLOR_BGR2RGB)
    mask = _match_mask(img_rgb, codes)
    if mask.size == 0:
        return None
    h = mask.shape[0]
    y_lo = max(0, pit_y - max_drop)
    y_hi = min(h, pit_y)                 # 右开：只看 y ∈ [pit_y - max_drop, pit_y)
    if y_lo >= y_hi:
        return None                      # 落差窗口整个在图外 / 是空的

    cols = np.nonzero(mask[y_lo:y_hi, :].any(axis=0))[0]
    if len(cols) == 0:
        return None                      # 头顶上方根本没有主线 → 回落老行为

    x0_raw = int(cols[0])
    x1_raw = int(cols[-1])

    # ⚠️ 钳制（2026-09-14 实测必修）：inset 的默认从 1 提到 4 之后，**短平台会被缩没**。
    #    平台跨度 = x1_raw - x0_raw + 1，缩完的宽度 = 跨度 - 2*inset；
    #    要"至少 3px 宽"（角色一帧能走 2px 也落得进去）就得
    #        inset <= (x1_raw - x0_raw - 2) // 2
    #    所以这里把 inset 压到这个上限；压完仍然退化（x0 > x1）才返回 None。
    #    ⚠️ 上限 < 0 时**不压**（平台只有 1~2px，再压也救不回来），
    #       交给下面 x0 > x1 → None 的老分支，行为与升级前**逐像素一致**。
    max_inset = (x1_raw - x0_raw - 2) // 2
    if max_inset >= 0:
        inset = min(inset, max_inset)

    x0 = x0_raw + inset
    x1 = x1_raw - inset
    if x0 > x1:
        return None                      # 内缩后退化（平台比 2*inset 还窄）→ 不画区间
    return (x0, x1)


def _norm_jump_band(jump_band):
    '''把 jump_band 归一化成 (x0, x1)（非法 / 退化 → None）。

    ⚠️ 为什么单独一个函数：`jump_band` 会同时进 paint_converging_hseg 和
       hseg_pixel_color（还有手绘器画布预览），三处必须**同一个口径**，
       否则"落盘的色"和"画布上看到的色"会不一致（所见非所得，最难查）。
    '''
    if jump_band is None:
        return None
    try:
        x0, x1 = int(jump_band[0]), int(jump_band[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if x0 > x1:
        return None
    return (x0, x1)


def hseg_pixel_color(x, y, corner, color_code, action="walk", jump_band=None):
    '''★ 横段上**单个像素**该着什么色（**RGB**）—— 唯一口径（落盘 + 预览共用）。

    ★ 三态分区（jump_band 给了时，2026-09-14 升级）：
        · x <  band[0]        → 往**右**走（走向主线正下方）
        · band[0] <= x <= band[1] → **原地起跳**（已在主线正下方）
        · x >  band[1]        → 往**左**走
      ⚠️ 取色**一律走 pick_color**（从 color_code 反查），禁止硬编码 (255,0,255)
         —— 用户改过色码表时硬编码会画出非法色（引擎取不到指令 → 站着不动）。

    老行为（jump_band 是 None，一个字节都不变）：
        · x < corner_x → dx > 0 → 蓝（right，往右走向拐角）
        · x > corner_x → dx < 0 → 红（left，往左走向拐角）
        · x == corner_x → **None（跳过）**，留给竖段着 jump 色

    Args:
        x / y: 像素坐标
        corner: 拐角 (cx, cy)
        color_code: {RGB: cmd}（pick_color 用）
        action: 横段动作（默认 "walk"）
        jump_band: (x0, x1) | None

    Returns:
        (R, G, B) 或 None（None = 这个像素不着色，保持原样）
    '''
    if corner is None:
        return None
    x, y = int(x), int(y)
    band = _norm_jump_band(jump_band)
    if band is not None:
        bx0, bx1 = band
        if x < bx0:                                   # 在区间左边 → 往右走过去
            return pick_color((x, y), (bx0, y), action, color_code)
        if x > bx1:                                   # 在区间右边 → 往左走过去
            return pick_color((x, y), (bx1, y), action, color_code)
        # 区间内：dx=0 / dy=-1 → pick_color 走 "none none jump"（**原地跳**）
        return pick_color((x, y), (x, y - 1), "jump", color_code)
    cx = int(corner[0])
    if x == cx:
        return None                                   # 拐角列：留给竖段着 jump 色
    return pick_color((x, y), (cx, y), action, color_code)


def paint_converging_hseg(img_bgr, hseg_pts, corner, color_code, action="walk",
                          jump_band=None) -> None:
    '''★ 横段**逐像素分区着色**（L 形回正线的关键，设计 3.2 写死）。

    默认（jump_band=None）＝**汇聚到拐角**，行为与升级前一模一样：
        · x < corner_x → 蓝（right，往右走向拐角）
        · x > corner_x → 红（left，往左走向拐角）
        · x == corner_x → **跳过**，留给竖段着 jump 色

    ★ 给了 jump_band=(x0, x1) → 升级成**三态分区**（2026-09-14）：
        · x < x0        → 蓝（right，往右走向主线正下方）
        · x0 <= x <= x1 → **洋红「原地跳」**（已经在主线正下方了，光按跳就能上去）
        · x > x1        → 红（left，往左走向主线正下方）
      为什么必须改成三态（用户实测）：能起跳的只有拐角那一列时，角色在坑底一帧
      能走好几像素，根本落不到那唯一一列 → 在横段两端之间来回震荡；极偶尔蹭到
      那一列时人还在横向移动 → 起跳带着横向惯性 → 斜着跳、跳上平台又跳出去。

    ⚠️ 禁止用 `cv2.line(整段, 单一颜色)` 画横段 —— 那样整条横段方向一致，
       角色落在拐角另一侧就会被推着走反、永远到不了拐角（Q9 描述的 bug）。

    ⚠️ 引擎侧**零改动**：`get_nearest_color_code` 照旧取最近回正线像素 →
       拿到 right / left / jump，自然就"汇聚 / 分区"了。

    Args:
        img_bgr: **BGR** 图（就地修改）
        hseg_pts: 横段上的全部像素 [(x, y), ...]
        corner: 拐角坐标 (corner_x, corner_y)
        color_code: {RGB: cmd}（pick_color 用）
        action: 横段的动作（默认 "walk"）
        jump_band: (x0, x1) | None —— None 时**逐像素等价于老行为**
    '''
    if img_bgr is None or not hseg_pts or corner is None:
        return
    band = _norm_jump_band(jump_band)
    for pt in hseg_pts:
        x, y = int(pt[0]), int(pt[1])
        if y < 0 or x < 0 or y >= img_bgr.shape[0] or x >= img_bgr.shape[1]:
            continue
        rgb = hseg_pixel_color(x, y, corner, color_code, action, band)
        if rgb is None:
            continue                       # 拐角列：留给竖段（会被着成 jump 色）
        img_bgr[y, x] = (int(rgb[2]), int(rgb[1]), int(rgb[0]))


def stamp_goal(img_bgr, pt, rgb=DEFAULT_GOAL_RGB, radius=1) -> None:
    '''在 **BGR** 图上盖终点标记（黄点）。手绘用 r=1（5px），录制器老流程用 r=2（13px）。'''
    if img_bgr is None or pt is None:
        return
    x, y = int(pt[0]), int(pt[1])
    if x < 0 or y < 0 or y >= img_bgr.shape[0] or x >= img_bgr.shape[1]:
        return
    bgr = (int(rgb[2]), int(rgb[1]), int(rgb[0]))
    if int(radius) <= 0:
        img_bgr[y, x] = bgr
        return
    cv2.circle(img_bgr, (x, y), int(radius), bgr, thickness=-1)


def render_home_image(base_bgr, blobs, color_code, goal_radius=1) -> np.ndarray:
    '''由「段 + goal」重绘整张 route_home.png（删除某条时用来重画其余全部）。

    ⚠️ 盖章顺序固定为 **段 → goal**（第二版没有锚点，顺序比第一版简单）。

    ★ **落点横段必须逐像素分区着色**（设计 3.2 写死，禁止 `cv2.line` 整段单色）：
        blob 里给 `corner`（拐角坐标）+ `walk_segs`（拐角之前有几段"走"）时，
        这前 `walk_segs` 段里凡是**横的**，都改用 paint_converging_hseg 逐像素着色
        —— 左半得 right、右半得 left，都指向拐角。
        为什么必须在这里做（而不是让调用方自己后处理）：删除某条线要重画**其余全部**，
        渲染只有这一处，口径才唯一。

        ★ blob 里再给 `jump_band`（(x0, x1)）时，横段从「二态汇聚」升级成
          **三态分区**：区间内着「原地跳」色（角色已在主线正下方，光按跳就能上去），
          区间外才走。**不给 / 给 None → 逐像素等价于老行为**（见 DEFAULT_JUMP_BAND_PARAMS）。

    Args:
        base_bgr: 底图（黑图或叠加了其它内容的 BGR 图；会被**拷贝**，不就地改）
        blobs: list[dict]，每个元素一条线：
            {
              "points":  [(x, y), ...],           # 依次点击的点
              "actions": ["walk"/"jump"/...],     # len = len(points)-1，第 i 段动作
              "goal":    (x, y) | None,           # 终点（None = 用 points[-1]）
              "corner":  (x, y) | None,           # 可选：拐角（L 形的竖直段起点）
              "walk_segs": int,                   # 可选：拐角之前有几段"走"（默认 0）
              "jump_band": (x0, x1) | None,       # 可选：落点横段的「原地跳」区间
            }
        color_code: {RGB: cmd}（pick_color 用，建议传合并表）
        goal_radius: goal 盖章半径（默认手绘口径 1）

    Returns:
        BGR ndarray（与 base_bgr 同尺寸）
    '''
    out = base_bgr.copy() if base_bgr is not None else None
    if out is None:
        raise ValueError("render_home_image: base_bgr 不能为 None（需要一个底图尺寸）")

    for blob in blobs or []:
        pts = [tuple(int(v) for v in p) for p in (blob.get("points") or [])]
        if not pts:
            continue
        acts = list(blob.get("actions") or [])
        corner = blob.get("corner")
        n_walk = int(blob.get("walk_segs") or 0)
        jump_band = _norm_jump_band(blob.get("jump_band"))
        # ① 段
        for i in range(len(pts) - 1):
            action = acts[i] if i < len(acts) else "walk"
            p0, p1 = pts[i], pts[i + 1]
            horizontal = abs(p1[0] - p0[0]) >= abs(p1[1] - p0[1])
            if (i < n_walk and corner is not None and str(action) == "walk"
                    and horizontal):
                # ★ 落点横段：逐像素分区着色
                #   · 没给 jump_band：左半 right / 右半 left，都朝拐角（老行为）
                #   · 给了 jump_band：区间内「原地跳」，区间外才走（2026-09-14 三态）
                xs = range(min(p0[0], p1[0]), max(p0[0], p1[0]) + 1)
                paint_converging_hseg(out, [(x, p0[1]) for x in xs], corner,
                                      color_code, "walk", jump_band)
                continue
            rgb = pick_color(p0, p1, action, color_code)
            cv2.line(out, p0, p1,
                     (int(rgb[2]), int(rgb[1]), int(rgb[0])), thickness=1)
        # ② goal（最后盖，压在段像素之上）
        goal_pt = blob.get("goal")
        if goal_pt is None and len(pts) >= 2:
            goal_pt = pts[-1]
        if goal_pt is not None:
            stamp_goal(out, goal_pt, DEFAULT_GOAL_RGB, goal_radius)
    return out


def corner_index(points, actions) -> int:
    '''L 形**拐角**在 points 里的下标 = 第一个「非 walk 段」的起点（设计 6.2）。

    例：点 (94,128) → (108,128) → (99,128) → 跳 (99,122)
        actions = ["walk", "walk", "jump"] → 第一个非 walk 是第 2 段 → 拐角 = points[2]
    全是"走"（没点跳）→ 回落到最后一个点（等于"整条都是落点段"）。

    Args:
        points: [(x, y), ...]
        actions: 段动作，len = len(points) - 1

    Returns:
        int: 拐角下标（points 为空时返回 0）
    '''
    pts = list(points or [])
    if not pts:
        return 0
    acts = list(actions or [])
    for i, a in enumerate(acts):
        if str(a) != "walk":
            return i + 1          # 第 i 段（points[i] → points[i+1]）不是"走" → 拐角是它的起点
    return len(pts) - 1


# ══════════════════════════════════════════════════════════════════════
# 坐标换算（手绘器专用）
# ══════════════════════════════════════════════════════════════════════

def screen_to_map(sx, sy, scale, w, h):
    '''放大画布的屏幕坐标 → 原图坐标。

    ⚠️ 禁止用整除 `//`：那会系统性偏 0.5 像素（4 倍图上点 (403,300) 会算成 (100,75)
       而不是 (101,75)），短回正线上这 1px 就是"接得上/接不上"的差别。

    Returns:
        (x, y)，已 clamp 到 [0, w-1] / [0, h-1]
    '''
    try:
        f = int(scale)
    except (TypeError, ValueError):
        f = 1
    if f < 1:
        f = 1
    x = int(round(float(sx) / f))
    y = int(round(float(sy) / f))
    return max(0, min(int(w) - 1, x)), max(0, min(int(h) - 1, y))


# ══════════════════════════════════════════════════════════════════════
# 撞色自检（把"路线色不撞底图色"这个假设变成每次开跑都验一次）
# ══════════════════════════════════════════════════════════════════════

def home_pixel_clash(raw_rgb, masked_rgb, codes) -> list:
    '''比对 mask 前后的**回正线像素总数**，返回**被地图底色吃掉**的坐标。

    mask_route_colors 会把"底图里出现同色"的位置在路线图上涂黑。
    一旦某个路线色在这张地图的底图里出现，回正线像素就会凭空消失 ——
    而"消失"这件事在运行时是静默的（只是这条线再也接不住人）。
    所以加载期必须数两次，少了就报 ERROR 并列出坐标。

    ⚠️ 第二版改成数**回正线像素总数**（第一版只数锚点像素）：
       锚点没了，被吃的可能是横段/竖段上任何一个像素，只数锚点等于没自检。
    '''
    before = set(collect_pixels(raw_rgb, codes))
    after = set(collect_pixels(masked_rgb, codes))
    return sorted(before - after)


def wipe_legacy_anchor(img_rgb) -> list:
    '''【旧图迁移】把 route_home.png 上残留的 `127,0,127`（第一版落点锚）涂黑。

    ⚠️ 为什么必须涂黑（Q6 裁决）而不是留着不管：
       127,0,127 **不在** color_code 表里 → 角色站上去时 get_nearest_color_code
       取不到它 → 那一帧没有指令 → 角色站着不动（且不报错，极难查）。

    ⚠️ 它**不是**配置项、不进任何色码表 —— 只在这一个迁移点用一次。

    Args:
        img_rgb: 回正线 RGB 图（**就地修改**）

    Returns:
        list[(x, y)]: 被清掉的坐标（>0 时调用方打中文提示"发现旧版落点标记，已自动忽略"）
    '''
    if img_rgb is None:
        return []
    arr = img_rgb[:, :, :3].astype(np.int32)
    mask = np.all(arr == np.array(LEGACY_ANCHOR_RGB, dtype=np.int32), axis=-1)
    if not mask.any():
        return []
    ys, xs = np.nonzero(mask)
    pts = [(int(x), int(y)) for y, x in zip(ys, xs)]
    for (x, y) in pts:
        img_rgb[y, x] = (0, 0, 0)
    return pts


# ══════════════════════════════════════════════════════════════════════
# 可选备注文件 route_home.json（**引擎运行时不读**，只有手绘器用）
# ══════════════════════════════════════════════════════════════════════

def notes_path(map_name) -> str:
    '''minimaps/<图>/route_home.json 的绝对路径（中文路径安全）。'''
    return resource_path(os.path.join("minimaps", str(map_name), "route_home.json"))


def load_home_notes(map_name) -> dict:
    '''读可选备注；**任何失败都静默返回 {}**（它不是真源，绝不能因为它拦住开跑）。

    Returns:
        dict: {"version": 1, "notes": [{"start": [x, y], "name": ..., "note": ...}, ...]}
    '''
    path = notes_path(map_name)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    notes = data.get("notes")
    if not isinstance(notes, list):
        data["notes"] = []
    return data


def save_home_notes(map_name, data) -> bool:
    '''写可选备注（原子写：先写临时文件再 os.replace）。失败返回 False，不抛。'''
    path = notes_path(map_name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".route_home_", suffix=".json",
                                   dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data or {}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[回正线] 备注文件写失败（不影响挂机，只是列表里没名字）：{e}")
        return False


# ══════════════════════════════════════════════════════════════════════
# 回正小结（日志文案 · 纯字符串拼装，不碰任何判定）
# ══════════════════════════════════════════════════════════════════════

def _fmt_pt(p) -> str:
    '''坐标 → "(98,126)"。认不出来就给 "(?,?)"，绝不抛（它要进日志行）。'''
    try:
        return f"({int(p[0])},{int(p[1])})"
    except (TypeError, ValueError, IndexError, KeyError):
        return "(?,?)"


def minimap_diff_text(score, thres=None) -> str:
    '''小地图模板匹配的**差异度**文案（纯函数，绝不抛异常）。

    ⚠️ 为什么"越小越好"必须显式写出来（2026-09-14 真机实测踩的坑）：
        `find_pattern_sqdiff` 返回的是 cv2 **TM_SQDIFF_NORMED** 的 min_val ——
        **0 = 完全一致，值越小越准**。而旧文案写成「小地图匹配度 0.01」，
        配上 `.2f` 格式，会被理解成"只匹配上 1% = 定位失败"，
        **实际 0.0087 是定位很准**（nametag 的判定阈值 `diff_thres` 就是 0.01）。
        方向搞反会直接把排查带去完全错误的方向 —— 这次差点就去查"定位是不是错了"。

    Args:
        score: 差异值（越小越好）。None / 非数字 → 返回 "未知"。
        thres: 判定阈值（如 `nametag.diff_thres` = 0.01）。None → 不带阈值说明。

    Returns:
        str: 形如 "小地图差异 0.0087（越小越好，≤0.01 算准）"
    '''
    try:
        v = float(score)
    except (TypeError, ValueError):
        return "小地图差异 未知"
    if thres is None:
        return f"小地图差异 {v:.4f}（越小越好）"
    try:
        t = float(thres)
    except (TypeError, ValueError):
        return f"小地图差异 {v:.4f}（越小越好）"
    tip = "算准" if v <= t else "偏大"
    return f"小地图差异 {v:.4f}（越小越好，≤{t:g} {tip}）"


def home_summary_line(stats) -> str:
    '''把一次回正的整个过程压成**一行**日志文本。

    治的是什么（2026-09-14 掉坑回不来排查）：
        原来日志里只有「进入回正 / 退出回正」两条，中间发生了什么全靠猜 ——
        到底是 **A：压根没走到起跳点**（引擎没产出 jump 指令），
        还是 **B：走到了、但跳跃键被 jump_cooldown=1.0s 吞了**，
        还是 **C：键按下去了、但游戏里没跳上去** —— 三种完全不同的病，
        在旧日志里长得一模一样。这一行把关键计数全摊开，一眼能分：

            A → 「**一次都没走到起跳点**」
            B → 「走到起跳点 2 次」但键盘侧只有 1 条「真的按下了跳跃键」
            C → 「真的按下了跳跃键」≥2 次，但「期间 y」仍然贴着坑底

    ⚠️ 为什么做成纯函数（跟 jump_ready 同一个道理）：
        它跑在 `_exit_home_route`（回正的**唯一出口**）里 —— 在那儿抛异常
        = 回正退不出去 = 机器人卡死。做成纯函数后 `tools/verify_jump_trace.py`
        能离线把「字段残缺 / 类型不对」的情况全钉死，不用真掉一次坑。

    Args:
        stats: dict，**允许缺任何键**（缺了用占位符或省略该段，绝不抛）。
            result    结果文本（"成功回主线" / "超时退出" / "接不上"）
            reason    退出原因原文（拼在末尾）
            dur_s     回正耗时（秒）
            frames    回正帧数
            start     进入时坐标 (x, y)
            end       退出时坐标 (x, y)
            jump_n    走到起跳点的次数（cmd_action 变 jump 的**上升沿**次数）
            jump_gap  相邻两次走到起跳点的间隔（秒）
            y_min / y_max   回正期间角色 y 的区间（看"跳没跳上去"）
            d_home / d_main 退出时离回正线 / 主线的距离
            mm_score  小地图模板匹配**差异度**（TM_SQDIFF_NORMED，越小越好；None = 不显示）
            mm_thres  判定阈值（如 nametag.diff_thres=0.01）；None = 不带阈值说明

    Returns:
        str: 一行日志文本，恒定以 "[回正·小结] " 开头。
    '''
    try:
        d = dict(stats or {})
    except (TypeError, ValueError):
        d = {}

    def _num(k, default=0):
        try:
            v = d.get(k, default)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    parts = [f"[回正·小结] {d.get('result') or '退出回正'}"]

    # ── 耗时 / 帧数 ────────────────────────────────────────────────
    parts.append(f"{_num('dur_s'):.1f}s/{int(_num('frames'))}帧")

    # ── 起点 → 终点 ────────────────────────────────────────────────
    parts.append(f"{_fmt_pt(d.get('start'))}→{_fmt_pt(d.get('end'))}")

    # ── ★ 区分 A 的关键：走到起跳点几次 ───────────────────────────
    jn = int(_num('jump_n'))
    if jn <= 0:
        parts.append("**一次都没走到起跳点**")
    else:
        gap_s = ""
        try:
            gap = d.get('jump_gap')
            if gap is not None:
                gap_s = f"(间隔 {float(gap):.2f}s)"
        except (TypeError, ValueError):
            gap_s = ""
        parts.append(f"走到起跳点 {jn} 次{gap_s}")

    # ── ★ 区分 C 的关键：期间 y 的区间（跳了没上去 → 仍贴坑底）────
    y0, y1 = d.get('y_min'), d.get('y_max')
    try:
        if y0 is not None and y1 is not None:
            parts.append(f"期间 y {int(y0)}~{int(y1)}")
    except (TypeError, ValueError):
        pass

    # ── 退出时的两个距离 ──────────────────────────────────────────
    dh, dm = d.get('d_home'), d.get('d_main')
    try:
        if dh is not None and dm is not None:
            parts.append(f"离回正线 {int(dh)}px 离主线 {int(dm)}px")
    except (TypeError, ValueError):
        pass

    # ── 小地图定位准不准（顺带验证"是不是定位错了"）───────────────
    # ⚠️ 必须走 minimap_diff_text：它是**差异度、越小越好**，自己拼容易写反方向。
    ms = d.get('mm_score')
    if ms is not None:
        parts.append(minimap_diff_text(ms, d.get('mm_thres')))

    line = " | ".join(parts)
    reason = d.get('reason')
    if reason:
        line = f"{line} | 原因：{reason}"
    return line
