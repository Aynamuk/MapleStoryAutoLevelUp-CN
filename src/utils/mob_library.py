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

源：怀旧冒险岛资料库（国服数据，static.mxdzlk.com/CMSCV001/images/mob/<ID>/preview.png）
    的**逐帧版**（api.dreamms.gg，已实测与国服贴图同源：82/82 逐像素核对通过）。
这些 PNG 是**透明底**的官方贴图，且自带领口纯黑描边，因此**天然适配引擎的
contour_only 模式**——该模式只取纯黑像素当轮廓（见 engine 里 np.all(img == [0,0,0])）。

## 每只怪是「多帧」不是「一张图」（2026-09-29 升级）

原来每只怪只有 1 张静态图（stand 首帧），怪一走动就认不出（漏检）。
现在每只怪有 1~9 个**动作帧**（move/stand/fly/jump），由 tools.fetch_mob_frames
按「覆盖度」筛掉冗余帧后得到 —— 姿态单调的怪留 1~2 帧，复杂的留 6~9 帧。

⚠️ 只收常态动作：出招(attack/skill)、受击(hit)、死亡(die) 都不收 ——
   本项目挂机只打**一击即死的小怪**，这些动作在场上根本来不及出现。

转换规则（与 tools/template_capture.py 的项目约定一致）：
    透明像素 → 纯绿 RGB(0,255,0)
    其余原样保留（含自带的黑描边）
⚠️ 库里存的是**原始透明底图**，不是绿底成品 —— 必须如此，因为 _to_template()
   对 3 通道图会把纯白像素当透明刷绿，而怪的眼睛/牙齿就是纯白（实测三眼章鱼
   眼白 83 像素会被误刷成绿）。绿底图要过 _to_template 只在 4 通道输入下才安全。
存成 monster/<中文怪名>/<中文怪名>_<动作><帧号>.png，与引擎的加载通配完全一致。

## 用法

    python -m src.utils.mob_library --list          # 看库里有什么（含帧数）
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


def mob_files(ent):
    """索引条目 → 该怪的全部素材相对路径。

    兼容两种索引格式：
      新版：{"files": [...], ...}
      旧版：{"file": "..."}（只有一张）
    """
    files = ent.get('files')
    if isinstance(files, list) and files:
        return [f for f in files if isinstance(f, str) and f]
    one = ent.get('file')
    return [one] if one else []


def install(name, root=None, overwrite=False):
    """把素材库里的 name 装成 monster/<name>/<name>_<动作><帧号>.png。

    ## 多帧（2026-09-29）

    库里每只怪已从「1 张静态图」升级为「多个动作帧」（move/stand/fly/jump，
    由 tools.fetch_mob_frames 筛掉冗余帧后得到）。本函数把**全部帧**一次装完，
    因为引擎对每张 PNG 都要跑两次匹配（原图+镜像），只装一帧等于白搭素材。

    ## 旧的单图模板会被清掉

    装载前先删掉该怪目录里已有的全部 PNG（旧版装出来的 <名>_1.png 等）。
    理由（实测）：monster/ 里的旧模板就是素材库旧素材的副本，且被新帧
    完全覆盖（82/82，中位分数 0.000）—— 留着只是每张白花 2 次 matchTemplate。

    返回 'installed' / 'exists' / 'missing'。
    已存在且 overwrite=False 时不动（用户自己截的模板优先级更高，不能被覆盖）。
    """
    root = root or REPO_ROOT
    idx = load_index()
    ent = idx.get(name)
    if not ent:
        return 'missing'
    rels = mob_files(ent)
    if not rels:
        return 'missing'

    dst_dir = os.path.join(root, 'monster', name)
    if os.path.isdir(dst_dir) and not overwrite:
        # 目录里已经有该怪的模板 → 视为已装（保持旧行为，不覆盖）
        if glob.glob(os.path.join(dst_dir, f'{name}*.png')):
            return 'exists'

    # 备好全部帧再落盘：中途缺图就别把旧模板删了
    templates = []
    for rel in rels:
        src = os.path.join(LIB_DIR, rel.replace('/', os.sep))
        if not os.path.isfile(src):
            continue
        img = cv2.imdecode(np.fromfile(src, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        tpl = _to_template(img)
        if tpl is not None:
            templates.append((os.path.basename(rel), tpl))
    if not templates:
        return 'missing'

    os.makedirs(dst_dir, exist_ok=True)
    # 清掉旧模板（见 docstring：旧的就是素材库旧素材的副本，已被新帧覆盖）
    for f in glob.glob(os.path.join(dst_dir, '*.png')):
        try:
            os.remove(f)
        except OSError:
            pass

    for fn, tpl in templates:
        ok, buf = cv2.imencode('.png', tpl)
        if not ok:
            continue
        # 走 tofile 以支持中文路径（cv2.imwrite 在 Windows 中文路径会失败）
        buf.tofile(os.path.join(dst_dir, fn))
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
            nf = len(mob_files(idx[name]))
            _safe_print(f'  {mark} {name}  {nf}帧'
                        + (f'  (Lv{lv})' if lv else ''))
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
