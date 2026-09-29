# -*- coding: utf-8 -*-
"""从官方素材源抓怪物的**常态动作帧**（move/stand/fly/jump），筛掉冗余帧。

## 为什么需要它

原来的 monster_lib/ 是「一种怪 = 一张图」（preview.png，只取了 stand 首帧）。
但怪在场上会来回走动，单一姿态的模板认不出移动中的怪 —— 这是
「怪贴脸了却没框」（漏检）的常见根因。

本工具从同一素材源按帧取图，把一只怪的**常态动作帧**抓下来，
再用**项目自带的查重判据**筛掉冗余帧，只留真正有区分度的姿态。

⚠️ 只抓常态动作，不抓出招/受击/死亡动画 —— 原因见下面「三层筛选」第 1 条。

## 素材源

    https://static.mxdzlk.com/CMSCV001/images/mob/<ID>/preview.png
        ↑ 国服怀旧冒险岛资料库（项目现有源），只有一张静态图
    http://api.dreamms.gg/api/GMS/latest/mob/<ID>/...
        ↑ 同一批贴图的逐帧版（已实测：国服 preview 与它的 stand/0 帧
          转绿底后逐像素完全相同，82/82 全库核对通过）

所以本工具**不换源**，只是把同一个源的「单帧」换成「全帧」。

## 三层筛选（前两层是硬规则，第三层用阈值）

  1. 动作白名单：**只留 move / stand / fly / jump**。
     前提是本项目挂机只打**一击即死的小怪** —— 要识别它的窗口就是
     「活着、还没挨打」那段时间，只有常态姿态会出现。
     die1（死亡）、hit1（受击）、attack1/2/3、skill1（出招）全都看不到，
     留着纯属白花匹配开销。详见 KEEP_ACTS 的说明。
     ⚠️ 以后要打打不死的精英/BOSS，必须把这些动作加回白名单。
  2. 信息量闸：掩码非零像素 < MIN_PIXELS 的帧丢掉（挡残渣帧）。
     ⚠️ 必须有这层：template_score 对空掩码返回 1.0（"完全不像"），
        会让「挑差异最大的帧」专门挑中空帧，反而选出一堆垃圾。
  3. 覆盖度：贪心挑「与已选帧最不像」的，直到新帧的相似度 <= 阈值就停。
     ⇒ **帧数不固定**：姿态单调的怪（如绿蘑菇）自然只剩 1~2 帧，
       姿态复杂的怪（如树妖王）会留到 8 帧以上。

## 用法（在项目根目录）

    python -m tools.fetch_mob_frames --list              # 只看有哪些怪、各多少帧
    python -m tools.fetch_mob_frames --dry-run 三眼章鱼   # 只算筛选结果，不落地
    python -m tools.fetch_mob_frames --install 三眼章鱼   # 抓取+筛选+装进 monster/
    python -m tools.fetch_mob_frames --install-all       # 全库 82 只
    python -m tools.fetch_mob_frames --install 三眼章鱼 --thres 0.3

产物：monster/<怪名>/<怪名>_<动作><帧号>.png（绿底，与引擎加载通配一致）
"""
import argparse
import glob
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

import cv2
import numpy as np

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, REPO_ROOT)

from src.utils.mob_library import GREEN, lib_index_path, load_index  # noqa: E402
from src.utils.mob_template_qa import (  # noqa: E402
    DUP_THRES_DEFAULT, content_crop, contour_mask, mask_pixels, template_score,
)

#: 素材 API（逐帧版）。region/version 写死 GMS/latest —— 已实测国服贴图与它同源。
API_FRAME = ('http://api.dreamms.gg/api/GMS/latest/mob/{mid}'
             '/render/{act}/{idx}?format=png')
API_META = 'http://api.dreamms.gg/api/GMS/latest/mob/{mid}'

UA = {'User-Agent': 'Mozilla/5.0 (compatible; MapleStoryAutoLevelUp-CN frame fetcher)'}

#: 覆盖度阈值。保守档 = 项目现有查重值，只砍几乎一模一样的帧。
COVER_THRES_DEFAULT = DUP_THRES_DEFAULT

#: 信息量闸：掩码非零像素少于这个数 = 帧里几乎没内容（残渣帧）
MIN_PIXELS = 120

#: 只保留这些动作。**白名单**，不在表里的一律丢弃。
#:
#: 前提：本项目挂机**只打一击即死的小怪**（用户 2026-09-29 明确）。
#: 于是要识别的窗口 = 怪「活着、还没挨打」的那段时间，只有常态姿态：
#:     move / stand / fly / jump
#: 其余动作全部看不到，留着只是白花匹配开销：
#:     die1   —— 死亡消失；倒地即已死，死都死了还认它做什么
#:     hit1   —— 受击；一击即死，没有"挨打但没死"这个状态
#:     attack1/2/3、skill1 —— 出招；怪还没出招就被打死了，这些帧永不出现
#:
#: ⚠️ 如果以后要打**精英/BOSS**（打不死、会还手），必须把 attack/skill/hit
#:    加回白名单 —— 那时这些姿态才是怪在场上的常态。
#:     `--all-acts` 可临时解除本白名单。
KEEP_ACTS = {'move', 'stand', 'fly', 'jump'}

#: 动作优先级：数字越小越优先留。同优先级按帧序。
#: move/stand/fly 是主要形态；jump 补充空中姿态。
ACT_RANK = {'move': 0, 'stand': 0, 'fly': 0, 'jump': 1}

#: 硬上限，防某只怪动作极多时留太多（匹配开销是张数的两倍）
MAX_KEEP = 12

#: 动作名缩写 → 文件名片段（文件名要短且是 ASCII，避免中文路径问题）
ACT_TAG = {'move': 'mv', 'stand': 'st', 'fly': 'fl', 'jump': 'jp',
           'attack1': 'atk1', 'attack2': 'atk2', 'attack3': 'atk3',
           'skill1': 'sk1', 'hit1': 'hit'}


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def fetch_meta(mid):
    """取怪物元数据；返回 framebooks（{动作: 帧数}）或 None。"""
    data = _fetch(API_META.format(mid=mid), timeout=20)
    if not data:
        return None
    try:
        return json.loads(data.decode('utf-8', 'replace')).get('framebooks') or {}
    except ValueError:
        return None


def fetch_frame_green(mid, act, idx):
    """取一帧，按项目约定转绿底。返回 BGR 图或 None。"""
    data = _fetch(API_FRAME.format(mid=mid, act=act, idx=idx))
    if not data:
        return None
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        return None
    if arr.ndim == 3 and arr.shape[2] == 4:
        bgr = arr[:, :, :3].copy()
        bgr[arr[:, :, 3] < 128] = GREEN      # 透明 → 纯绿，与 mob_library 同规则
        return bgr
    if arr.ndim == 3 and arr.shape[2] == 3:
        bgr = arr.copy()
        bgr[np.all(bgr == [255, 255, 255], axis=2)] = GREEN
        return bgr
    return None


def select_frames(frames, thres=COVER_THRES_DEFAULT):
    """三层筛选。frames: [(act, idx, bgr)] → (选中, 全部有效帧, 日志)

    返回的「选中」是 (act, idx, bgr) 列表，按动作优先级排序。
    """
    log = []

    # 1. 动作白名单
    kept_act = [f for f in frames if f[0] in KEEP_ACTS]
    dropped_act = [f for f in frames if f[0] not in KEEP_ACTS]
    if dropped_act:
        by_act = {}
        for f in dropped_act:
            by_act[f[0]] = by_act.get(f[0], 0) + 1
        log.append('动作白名单：丢掉 %d 帧（%s）'
                   % (len(dropped_act),
                      '、'.join('%s×%d' % (k, v) for k, v in sorted(by_act.items()))))

    # 2. 信息量闸
    cand = []
    for f in kept_act:
        m = contour_mask(f[2])
        px = mask_pixels(m)
        if px < MIN_PIXELS:
            log.append('信息量不足：%s#%d（像素 %d）' % (f[0], f[1], px))
        else:
            cand.append((f[0], f[1], f[2], m))
    if not cand:
        return [], [], log

    # 2b. 相对主姿态的过小帧（同动作里的残渣，如 die1 之外的收缩帧）
    main_areas = [content_crop(c[3]).size for c in cand if ACT_RANK.get(c[0], 5) == 0]
    if main_areas:
        floor = max(main_areas) * 0.25
        keep = []
        for c in cand:
            if content_crop(c[3]).size < floor:
                log.append('轮廓过小：%s#%d（%s）'
                           % (c[0], c[1], content_crop(c[3])))
            else:
                keep.append(c)
        cand = keep or cand

    # 3. 覆盖度贪心
    cand.sort(key=lambda c: (ACT_RANK.get(c[0], 5), c[1]))
    sel = [cand[0]]
    for c in cand[1:]:
        if len(sel) >= MAX_KEEP:
            log.append('达到上限 %d 帧，其余丢弃' % MAX_KEEP)
            break
        gap = min(template_score(c[3], s[3]) for s in sel)
        if gap > thres:
            sel.append(c)
        else:
            log.append('与已选重复：%s#%d（相似度 %.3f <= %.2f）'
                       % (c[0], c[1], gap, thres))
    return [(c[0], c[1], c[2]) for c in sel], cand, log


def install_mob(name, mid, root=None, overwrite=False,
                thres=COVER_THRES_DEFAULT):
    """抓一只怪的全部帧 → 筛选 → 装进 monster/<name>/。

    返回 dict：{status, kept, cand, files, log}
    status: 'installed' / 'exists' / 'empty'
    """
    root = root or REPO_ROOT
    dst_dir = os.path.join(root, 'monster', name)

    if os.path.isdir(dst_dir) and os.listdir(dst_dir) and not overwrite:
        return {'status': 'exists', 'kept': 0, 'cand': 0, 'files': [], 'log': []}

    fb = fetch_meta(mid)
    if not fb:
        return {'status': 'empty', 'kept': 0, 'cand': 0, 'files': [],
                'log': ['取元数据失败']}

    frames = []
    for act, n in fb.items():
        if act not in KEEP_ACTS:
            frames.append((act, 0, None))          # 占位，只为让日志统计到
            continue
        for i in range(n):
            img = fetch_frame_green(mid, act, i)
            if img is None:
                continue
            frames.append((act, i, img))
            time.sleep(0.03)
    if not frames:
        return {'status': 'empty', 'kept': 0, 'cand': 0, 'files': [],
                'log': ['一帧都没取到']}

    sel, cand, log = select_frames(frames, thres)
    if not sel:
        return {'status': 'empty', 'kept': 0, 'cand': 0, 'files': [], 'log': log}

    os.makedirs(dst_dir, exist_ok=True)

    # ⚠️ overwrite 必须**先清空目录里的旧 PNG**，不能只覆盖同名文件。
    #    2026-09-29 踩过：用户说「旧模板全废掉」，但旧模板叫 <怪名>_1.png、
    #    新模板叫 <怪名>_mv0.png，文件名根本不同 —— 只覆盖同名文件的结果是
    #    新旧混装，旧的低质量截图还留在目录里继续参与匹配（白花开销）。
    removed = 0
    if overwrite:
        for f in glob.glob(os.path.join(dst_dir, '*.png')):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        if removed:
            log.append('清掉旧模板 %d 张' % removed)

    files = []
    for n, (act, idx, img) in enumerate(sel, 1):
        tag = ACT_TAG.get(act, act)
        # ⚠️ 文件名必须以怪名开头：引擎用 monster/<名>/<名>*.png 通配
        fn = '%s_%s%d.png' % (name, tag, idx)
        path = os.path.join(dst_dir, fn)
        ok, buf = cv2.imencode('.png', img)
        if not ok:
            continue
        buf.tofile(path)          # 走 tofile 支持中文路径
        files.append(fn)
    return {'status': 'installed', 'kept': len(sel), 'cand': len(cand),
            'files': files, 'log': log, 'removed': removed}


def _safe_print(*a, **kw):
    try:
        print(*a, **kw)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or 'utf-8'
        print(*[str(x).encode(enc, 'replace').decode(enc, 'replace') for x in a], **kw)


def main():
    ap = argparse.ArgumentParser(
        description='抓「每只怪不同动作」的模板帧并筛选（绿底 PNG）')
    ap.add_argument('--list', action='store_true', help='列出素材库里有哪些怪')
    ap.add_argument('--install', metavar='NAME', help='抓某一只怪')
    ap.add_argument('--install-all', action='store_true', help='全库都抓')
    ap.add_argument('--dry-run', metavar='NAME',
                    help='只算筛选结果、打印明细，不写文件')
    ap.add_argument('--thres', type=float, default=COVER_THRES_DEFAULT,
                    help='覆盖度阈值（默认 %.2f，项目现有查重值；调大=留更多帧）'
                         % COVER_THRES_DEFAULT)
    ap.add_argument('--overwrite', action='store_true',
                    help='覆盖已存在的模板（默认跳过，保护用户自己截的）')
    ap.add_argument('--all-acts', action='store_true',
                    help='解除动作白名单（默认只留 move/stand/fly/jump）；'
                         '打精英/BOSS 时才需要')
    a = ap.parse_args()

    if a.all_acts:
        # 解除白名单 = 接受所有动作（打精英/BOSS 时用）
        KEEP_ACTS.update(('die1', 'hit1', 'attack1', 'attack2', 'attack3',
                          'skill1', 'skill2'))
        ACT_RANK.update({'attack1': 2, 'attack2': 2, 'attack3': 2,
                         'skill1': 2, 'skill2': 2, 'hit1': 8, 'die1': 9})

    idx = load_index()

    if a.list or not (a.install or a.install_all or a.dry_run):
        _safe_print('素材库：%s' % lib_index_path())
        _safe_print('共 %d 种' % len(idx))
        return 0

    if a.dry_run:
        name = a.dry_run
        ent = idx.get(name)
        if not ent:
            _safe_print('素材库里没有：%s' % name)
            return 1
        mid = str(ent.get('id', ''))
        fb = fetch_meta(mid)
        _safe_print('=== %s (id=%s) 帧表：%s' % (name, mid, fb))
        frames = []
        for act, n2 in (fb or {}).items():
            for i in range(n2):
                img = fetch_frame_green(mid, act, i)
                if img is not None:
                    frames.append((act, i, img))
        sel, cand, log = select_frames(frames, a.thres)
        _safe_print('候选 %d 帧 → 选中 %d 帧（阈值 %.2f）' % (len(frames), len(sel), a.thres))
        for act, i, img in sel:
            m = contour_mask(img)
            _safe_print('  [留] %-9s #%-2d  %dx%d  轮廓像素 %d'
                        % (act, i, img.shape[1], img.shape[0], mask_pixels(m)))
        _safe_print('--- 筛除明细 ---')
        for line in log:
            _safe_print('  ' + line)
        return 0

    names = [a.install] if a.install else sorted(idx, key=str.lower)
    if a.install and a.install not in idx:
        _safe_print('素材库里没有：%s' % a.install)
        return 1

    done = skip = fail = 0
    tot_kept = tot_cand = 0
    for name in names:
        ent = idx[name]
        r = install_mob(name, str(ent.get('id', '')), overwrite=a.overwrite,
                        thres=a.thres)
        if r['status'] == 'installed':
            done += 1
            tot_kept += r['kept']
            tot_cand += r['cand']
            rm = r.get('removed') or 0
            _safe_print('  [装] %-14s %d 帧 → 留 %d 帧%s：%s'
                        % (name, r['cand'], r['kept'],
                           ('（清旧模板 %d 张）' % rm) if rm else '',
                           ' '.join(r['files'])))
        elif r['status'] == 'exists':
            skip += 1
            _safe_print('  [跳] %-14s 已有模板，未覆盖（加 --overwrite 强制）' % name)
        else:
            fail += 1
            _safe_print('  [败] %-14s %s' % (name, '；'.join(r['log'][:2])))
        time.sleep(0.1)

    _safe_print('')
    _safe_print('完成：新装 %d 只，跳过 %d 只，失败 %d 只' % (done, skip, fail))
    if tot_cand:
        _safe_print('共取 %d 帧，筛选后留 %d 帧（压掉 %.0f%%）'
                    % (tot_cand, tot_kept, 100.0 * (1 - tot_kept / tot_cand)))
        _safe_print('引擎每帧对每张模板跑 2 次匹配 → 每只怪约 %d 次'
                    % (2 * tot_kept // max(done, 1)))
    _safe_print('产物：monster/<怪名>/<怪名>_<动作><帧号>.png')
    return 0


if __name__ == '__main__':
    sys.exit(main())
