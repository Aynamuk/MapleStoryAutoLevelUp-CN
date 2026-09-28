# -*- coding: utf-8 -*-
"""内置怪物素材库 —— 从官方素材转成的现成模板，用户选完地图直接勾选登记。

## 为什么需要它

原来的流程是「每种怪都要自己截一次模板」：用户得开游戏、找到那种怪、
框住它、调好框，才能用。而国服怀旧服的怪种类很多（资料库收录 80+ 种），
用户根本不可能每种都截，导致「选定地图后没怪可用」——这是「不打怪」
类反馈最常见的根因之一（见 issue #10）。

现在改为：仓库随包附带一批已经转好的官方素材模板，用户选完地图后
点一下就能把这张图会出现的怪登记进去，不用自己截。

## 素材来源与转换

源：怀旧冒险岛资料库（国服数据，static.mxdzlk.com/CMSCV001/images/mob/<ID>/preview.png）。
这些 PNG 是**透明底**的官方贴图，且自带领口纯黑描边（实测 11/11 张都是
严格 RGB(0,0,0)），因此**天然适配引擎的 contour_only 模式**——
该模式只取纯黑像素当轮廓（见 engine 里 np.all(img == [0,0,0])）。

转换规则（与 tools/template_capture.py 的项目约定一致）：
    透明像素 → 纯绿 RGB(0,255,0)
    其余原样保留（含自带的黑描边）
存成 monster/<中文怪名>/<中文怪名>_1.png，与引擎的加载通配完全一致。

## 用法

    python -m src.utils.mob_library --list          # 看库里有什么
    python -m src.utils.mob_library --install 木妖   # 装某一种到 monster/
    python -m src.utils.mob_library --install-all   # 全装
"""
import argparse
import glob
import json
import os
import sys

import cv2
import numpy as np

# 仓库根：本文件在 <root>/src/utils/ 下
try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

#: 素材库目录（随包分发）。里面是透明底官方贴图 + 一张索引表。
LIB_DIR = os.path.join(REPO_ROOT, 'monster_lib')

#: 项目约定的模板背景色（BGR）—— 与 tools/template_capture.py 完全一致
GREEN = (0, 255, 0)

#: 索引文件名
INDEX_NAME = 'index.json'


def lib_index_path(root=None):
    return os.path.join(root or LIB_DIR, INDEX_NAME)


def load_index(root=None):
    """读素材库索引。返回 dict：{中文名: {"file":..., "id":..., "level":...}}。

    索引不存在时回退到「直接扫描目录」——这样即使索引丢了，只要贴图还在
    就仍然能用（不让一个 json 卡住整个功能）。
    """
    p = lib_index_path(root)
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    # 回退：扫描 <root>/*/*.png
    base = root or LIB_DIR
    out = {}
    for f in sorted(glob.glob(os.path.join(base, '*', '*.png'))):
        name = os.path.basename(os.path.dirname(f))
        out[name] = {'file': os.path.relpath(f, base).replace('\\', '/'),
                     'id': '', 'level': ''}
    return out


def _to_template(img):
    """透明底贴图 → 项目约定的绿底模板。

    透明（alpha < 128）像素刷成纯绿；其余原样。**不动黑描边**——
    那条描边正是 contour_only 模式赖以工作的东西。
    """
    if img is None:
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        bgr = img[:, :, :3].copy()
        bgr[img[:, :, 3] < 128] = GREEN
        return bgr
    if img.ndim == 3 and img.shape[2] == 3:
        bgr = img.copy()
        # 无 alpha 的源：把纯白当透明刷绿（少见，兜底）
        bgr[np.all(bgr == [255, 255, 255], axis=2)] = GREEN
        return bgr
    return None


def installed_mobs(root=None):
    """monster/ 下已装好的怪名集合。"""
    base = os.path.join(root or REPO_ROOT, 'monster')
    if not os.path.isdir(base):
        return set()
    return {d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and not d.startswith('_')}


def install(name, root=None, overwrite=False):
    """把素材库里的 name 装成 monster/<name>/<name>_1.png。

    返回 'installed' / 'exists' / 'missing'。
    已存在且 overwrite=False 时不动（用户自己截的模板优先级更高，不能被覆盖）。
    """
    root = root or REPO_ROOT
    idx = load_index()
    ent = idx.get(name)
    if not ent:
        return 'missing'
    src = os.path.join(LIB_DIR, ent.get('file', ''))
    if not os.path.isfile(src):
        return 'missing'

    dst_dir = os.path.join(root, 'monster', name)
    dst = os.path.join(dst_dir, f'{name}_1.png')
    if os.path.isfile(dst) and not overwrite:
        return 'exists'

    img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    tpl = _to_template(img)
    if tpl is None:
        return 'missing'

    os.makedirs(dst_dir, exist_ok=True)
    ok, buf = cv2.imencode('.png', tpl)
    if not ok:
        return 'missing'
    buf.tofile(dst)          # 走 tofile 以支持中文路径（cv2.imwrite 在 Windows 中文路径会失败）
    return 'installed'


def install_all(root=None, overwrite=False):
    """全装。返回 (已装列表, 跳过列表, 缺失列表)。"""
    done, skip, miss = [], [], []
    for name in sorted(load_index(), key=str.lower):
        r = install(name, root=root, overwrite=overwrite)
        {'installed': done, 'exists': skip, 'missing': miss}.get(r, miss).append(name)
    return done, skip, miss


def _safe_print(*args, **kwargs):
    """Windows 控制台默认 GBK，直接 print 记号字符会 UnicodeEncodeError。

    这里统一兜底：编码不可表示时退化成 '?'，**不让打印本身把功能搞挂**
    （--list 是纯查询，崩在这里最冤）。
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or 'utf-8'
        safe = [str(a).encode(enc, 'replace').decode(enc, 'replace') for a in args]
        print(*safe, **kwargs)


def main():
    ap = argparse.ArgumentParser(
        description='内置怪物素材库：把官方素材装成现成模板（免手抠）')
    ap.add_argument('--list', action='store_true', help='列出库里有哪些怪')
    ap.add_argument('--install', metavar='NAME', help='装某一种怪到 monster/')
    ap.add_argument('--install-all', action='store_true', help='全部装上')
    ap.add_argument('--overwrite', action='store_true',
                    help='覆盖已存在的模板（默认不覆盖，保护用户自己截的）')
    args = ap.parse_args()

    idx = load_index()
    if args.list or not (args.install or args.install_all):
        _safe_print(f'素材库：{LIB_DIR}')
        _safe_print(f'共 {len(idx)} 种：')
        have = installed_mobs()
        for name in sorted(idx, key=str.lower):
            # 用 ASCII 记号，避免 GBK 控制台编码坑
            mark = '[已装]' if name in have else '[未装]'
            lv = idx[name].get('level', '')
            _safe_print(f'  {mark} {name}' + (f'  (Lv{lv})' if lv else ''))
        return 0

    if args.install_all:
        done, skip, miss = install_all(overwrite=args.overwrite)
        _safe_print(f'新装 {len(done)} 种，已存在跳过 {len(skip)} 种，素材缺失 {len(miss)} 种')
        if miss:
            _safe_print('缺失：' + '、'.join(miss))
        return 0

    r = install(args.install, overwrite=args.overwrite)
    _safe_print({'installed': f'已装：{args.install}',
                 'exists': f'已存在，未覆盖：{args.install}（加 --overwrite 强制）',
                 'missing': f'素材库里没有：{args.install}'}[r])
    return 0 if r != 'missing' else 1


if __name__ == '__main__':
    sys.exit(main())
