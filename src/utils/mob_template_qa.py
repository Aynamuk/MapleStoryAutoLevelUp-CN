# -*- coding: utf-8 -*-
"""怪物模板查重 / 覆盖度分析（离线可测，纯函数为主）。

## 为什么需要它

引擎对**每张** PNG 都会生成「原图 + 左右镜像」两个模板（见 engine 里加载怪物
模板那段的 `cv2.flip`），所以每次怪检测的**实际匹配次数 = PNG 张数 × 2**。
多留一张模板 = 每次检测多跑两次全 ROI 的 `matchTemplate`，而"多出来的"往往
只是同一个姿势重截了一遍 —— 纯属白花钱。

实测（2026-09-15，废都南方工地 / 木妖）：9 张 → 18 次匹配，其中 3 张是重复的；
删掉后 6 张 → 12 次匹配，**识别能力不变**。

## 判据（与引擎同源）

两张模板互相跑 `TM_SQDIFF_NORMED`，取更小的那个分；`<= 阈值` 即互为重复。

阈值默认 `DUP_THRES_DEFAULT = 0.4`，**故意不直接读 `monster_detect.diff_thres`**：
`diff_thres` 是"引擎认为认出了这只怪"的宽松门槛（项目里被配到过 0.8），
拿它当查重门槛会把"其实不一样"的模板也判成重复，删了会**漏怪**。
查重宁可保守：只删真的几乎一样的。0.4 正好落在实测的天然空档里
（重复对 ≤0.28 / 非重复对 ≥0.50）。

## ⚠️ 预处理必须与引擎逐字一致

引擎 `contour_only` 分支做的是：
    mask = np.all(img == [0, 0, 0], axis=2).astype(np.uint8) * 255
    mask = cv2.GaussianBlur(mask, (contour_blur, contour_blur), 0)
这里必须照抄，否则算出来的分数跟引擎不是一个尺度、阈值也就没意义。
"""
import glob
import os

import cv2
import numpy as np

#: 查重阈值（见模块 docstring：故意不跟 diff_thres 走）
DUP_THRES_DEFAULT = 0.4

#: 轮廓模糊核大小，与 config 的 monster_detect.contour_blur 默认值一致
DEFAULT_CONTOUR_BLUR = 5


def contour_mask(img, blur=DEFAULT_CONTOUR_BLUR):
    """取"纯黑像素"掩码并高斯模糊 —— 与引擎 contour_only 分支一致。

    Args:
        img: BGR 图（模板绿底图 / 画面 ROI 都可以）
        blur: 高斯核大小；非法值回落默认

    Returns:
        np.uint8 单通道掩码（255 = 黑像素）
    """
    m = np.all(img == [0, 0, 0], axis=2).astype(np.uint8) * 255
    try:
        k = int(blur)
    except (TypeError, ValueError):
        k = DEFAULT_CONTOUR_BLUR
    if k < 1:
        k = DEFAULT_CONTOUR_BLUR
    return cv2.GaussianBlur(m, (k, k), 0)


def content_crop(mask):
    """裁到非零内容的紧致外接矩形（模板边缘的空白对匹配没意义）。"""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return mask
    return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _pad_center(m, H, W):
    """把 m 居中放进 H×W 的零画布。"""
    h, w = m.shape[:2]
    h, w = min(h, H), min(w, W)
    out = np.zeros((H, W), np.uint8)
    t, l = (H - h) // 2, (W - w) // 2
    out[t:t + h, l:l + w] = m[:h, :w]
    return out


def template_score(a, b, margin=16):
    """两张掩码"像不像"：互相匹配取更小的分。0 = 一模一样，1 = 完全不像。

    为什么互相都要跑一遍：模板尺寸略有差异时，小的放进大的里找得到最佳对位，
    反过来却放不下。取两个方向的最小值才对称。

    Args:
        a, b: contour_mask 出来的掩码
        margin: 给底图留的搜索余量（像素），让模板能上下左右滑动对位
    """
    a, b = content_crop(a), content_crop(b)
    if a.size == 0 or b.size == 0:
        return 1.0
    H = max(a.shape[0], b.shape[0])
    W = max(a.shape[1], b.shape[1])
    scores = []
    for base, tmpl in ((a, b), (b, a)):
        I = _pad_center(base, H + margin, W + margin)
        T = _pad_center(tmpl, H, W)
        if T.shape[0] > I.shape[0] or T.shape[1] > I.shape[1]:
            continue
        scores.append(float(cv2.matchTemplate(I, T, cv2.TM_SQDIFF_NORMED).min()))
    return min(scores) if scores else 1.0


def template_paths(root, name):
    """monster/<name>/<name>*.png —— 与引擎加载时用的通配完全一致。"""
    d = os.path.join(root, "monster", name)
    return sorted(glob.glob(os.path.join(d, f"{name}*.png")))


def load_masks(paths, blur=DEFAULT_CONTOUR_BLUR):
    """读一组模板文件 → [(path, mask)]；读不到的跳过（不抛）。"""
    from src.utils.common import imread_unicode
    out = []
    for p in paths:
        try:
            img = imread_unicode(p, cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None:
            continue
        out.append((p, contour_mask(img, blur)))
    return out


def group_duplicates(items, thres=DUP_THRES_DEFAULT):
    """把互为重复的模板并成组（连通分量）。返回 [[(path, mask), ...], ...]。

    只返回张数 >= 2 的组。连通分量而不是"贪心挑一张"：这样报告里能一眼看到
    「这几张是一伙的」，删哪张由人决定。
    """
    n = len(items)
    if n < 2:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if template_score(items[i][1], items[j][1]) <= thres:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])
    return [g for g in groups.values() if len(g) >= 2]


def find_duplicate_groups(root, name, thres=DUP_THRES_DEFAULT,
                          blur=DEFAULT_CONTOUR_BLUR):
    """读 monster/<name>/ 下全部模板，返回重复组。"""
    return group_duplicates(load_masks(template_paths(root, name), blur), thres)


def mask_pixels(mask):
    """掩码里非零像素数 —— 报告里当"这张模板带了多少信息"的参考量。"""
    return int(np.count_nonzero(mask))


def old_hits_frame(frame, box, items, thres=DUP_THRES_DEFAULT, margin=40,
                   blur=DEFAULT_CONTOUR_BLUR):
    """旧模板能不能在「用户刚框的那只怪」附近认出它？

    ★ 这是「截模板时自查」的核心判据，也是绕开"判定优劣"的关键：
        不问"新旧哪个更好"，只问"旧模板还够不够用"。
        旧模板能认出来 → 它已经覆盖了这个姿势 → 新模板是多余的。
        旧模板认不出   → 这是旧模板没见过的新姿势 → 新模板该留。

    ⚠️ **不能拿新模板去测**：新模板就是从这一帧抠下来的，必然满分，
       等于自己考自己。只有旧模板的得分有信息量。

    Args:
        frame: 整帧 BGR（坐标系与 box 一致）
        box:   (x, y, w, h)，用户刚框住那只怪的矩形
        items: [(path, mask)]，该怪已有的模板
        thres: 命中阈值（与引擎判定"认出"同口径）
        margin: 在框外多搜这么多像素（容忍手框位置的小偏差）

    Returns:
        (hit, best_score, best_path)；没有可比模板时是 (False, None, None)
    """
    if not items:
        return False, None, None
    H, W = frame.shape[:2]
    x, y, w, h = [int(v) for v in box]
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return False, None, None
    roi = contour_mask(frame[y0:y1, x0:x1], blur)
    best, best_path = None, None
    for p, m in items:
        if m.shape[0] > roi.shape[0] or m.shape[1] > roi.shape[1]:
            continue                      # 模板比搜索窗还大，引擎也不会匹配到
        s = float(cv2.matchTemplate(roi, m, cv2.TM_SQDIFF_NORMED).min())
        if best is None or s < best:
            best, best_path = s, p
    if best is None:
        return False, None, None
    return best <= thres, best, best_path
