# -*- coding: utf-8 -*-
"""重建 monster_lib/：按 monster/ 里已筛定的帧清单，从素材源重抓**透明底原图**。

## 为什么必须重抓、不能从 monster/ 复制

`monster/` 里的图是**已转绿底的成品**（3 通道）。`monster_lib` 必须存
**原始透明底 PNG**（4 通道），理由是一条实测出来的硬约束：

    _to_template() 对 3 通道图有个兜底分支 —— 把「纯白像素」当透明刷成绿。
    而怪的眼睛/牙齿本来就有纯白像素（三眼章鱼有 83 个）。
    ⇒ 拿已刷绿的成品再过一遍 _to_template，眼白会被误刷成绿色（毁图）。

    但对 4 通道原图它走的是正路：只按 alpha 刷绿，纯白原样保留。
    实测：源图转换后眼白仍是 83 个白像素 ✓；绿底成品转换后眼白 0 个 ✗

所以本工具 = 「按 monster/ 的清单，从源重抓原图铺进 monster_lib/」。

## 做什么

  1. 备份旧 monster_lib
  2. 对每只怪：读 monster/<怪名>/ 里的**文件名**（只取动作+帧号），
     据此从素材源重抓对应的**透明底原图**，写进 monster_lib/<怪名>/
  3. 重写 index.json（file=首帧兼容 / files=全部帧）
  4. 校验：4 通道透明底 + 纯黑描边

用法：python -m tools.pack_mob_library
"""
import datetime
import glob
import json
import os
import re
import shutil
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
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LIB = os.path.join(REPO_ROOT, 'monster_lib')
SRC = os.path.join(REPO_ROOT, 'monster')
IDX = os.path.join(LIB, 'index.json')

API = ('http://api.dreamms.gg/api/GMS/latest/mob/{mid}'
       '/render/{act}/{idx}?format=png')
UA = {'User-Agent': 'Mozilla/5.0 (compatible; MapleStoryAutoLevelUp-CN lib builder)'}

#: 文件名里的动作缩写 → 素材源的动作名（与 fetch_mob_frames.ACT_TAG 互为反查）
TAG2ACT = {'mv': 'move', 'st': 'stand', 'fl': 'fly', 'jp': 'jump',
           'atk1': 'attack1', 'atk2': 'attack2', 'atk3': 'attack3',
           'sk1': 'skill1', 'hit': 'hit1', 'di': 'die1'}

FAILS = []


def check(cond, msg):
    print(('  [OK]   ' if cond else '  [FAIL] ') + msg)
    if not cond:
        FAILS.append(msg)


def parse_filename(name, fn):
    """<怪名>_<tag><帧号>.png → (动作名, 帧号)；解析不出返回 None。"""
    m = re.match(re.escape(name) + r'_([a-z]+\d?)0*(\d+)\.png$', fn)
    if not m:
        # 再试不带数字结尾的（如 _hit0）
        m = re.match(re.escape(name) + r'_([a-z]+)(\d+)\.png$', fn)
        if not m:
            return None
    tag, num = m.group(1), int(m.group(2))
    act = TAG2ACT.get(tag)
    if not act:
        return None
    return act, num


def fetch_raw(mid, act, i, timeout=25):
    req = urllib.request.Request(API.format(mid=mid, act=act, idx=i), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def main():
    print('=' * 64)
    print('重建 monster_lib/（从源重抓透明底原图）')
    print('  帧清单来自：', SRC)
    print('  输出到    ：', LIB)
    print('=' * 64)

    old = json.load(open(IDX, encoding='utf-8')) if os.path.isfile(IDX) else {}
    print('\n[1] 备份旧素材库')
    stamp = datetime.datetime.now().strftime('%Y%m%d')
    bak = os.path.join(REPO_ROOT, 'monster_lib_backup_%s' % stamp)
    if os.path.isdir(bak):
        shutil.rmtree(bak)
    shutil.copytree(LIB, bak)
    print('  已备份到 %s（%d 张）'
          % (os.path.relpath(bak, REPO_ROOT),
             len(glob.glob(os.path.join(bak, '*', '*.png')))))

    names = sorted(d for d in os.listdir(SRC)
                   if os.path.isdir(os.path.join(SRC, d)) and not d.startswith('_'))
    print('\n[2] 按帧清单重抓原图（%d 只怪）' % len(names))

    new_index = {}
    total = failed = 0
    for name in names:
        meta = old.get(name, {})
        mid = str(meta.get('id', ''))
        if not mid:
            check(False, '%s：index.json 里没有 id，跳过' % name)
            continue

        # 从 monster/ 的文件名解析出「要哪几帧」
        wants = []
        for p in sorted(glob.glob(os.path.join(SRC, name, name + '*.png'))):
            got = parse_filename(name, os.path.basename(p))
            if got:
                wants.append(got)
        if not wants:
            check(False, '%s：无法从文件名解析出帧清单' % name)
            continue

        dst_dir = os.path.join(LIB, name)
        if os.path.isdir(dst_dir):
            for f in glob.glob(os.path.join(dst_dir, '*.png')):
                os.remove(f)
        else:
            os.makedirs(dst_dir, exist_ok=True)

        rel_files = []
        for act, i in wants:
            data = fetch_raw(mid, act, i)
            if not data:
                failed += 1
                continue
            fn = '%s_%s%d.png' % (name, act, i)      # 库里用完整动作名，可读
            with open(os.path.join(dst_dir, fn), 'wb') as f:
                f.write(data)
            rel_files.append('%s/%s' % (name, fn))
            total += 1
            time.sleep(0.03)

        if rel_files:
            new_index[name] = {
                'file': rel_files[0],
                'files': rel_files,
                'id': mid,
                'level': meta.get('level', ''),
                'frames': len(rel_files),
            }
        else:
            check(False, '%s：一帧都没抓到' % name)

    print('  重抓 %d 张，失败 %d 帧' % (total, failed))

    print('\n[3] 校验（透明底 + 纯黑描边）')
    bad_alpha, bad_black, bad_read = [], [], []
    for name, ent in new_index.items():
        for rel in ent['files']:
            p = os.path.join(LIB, rel.replace('/', os.sep))
            img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if img is None:
                bad_read.append(rel)
                continue
            if img.ndim != 3 or img.shape[2] != 4:
                bad_alpha.append('%s(通道=%s)' % (rel, getattr(img, 'shape', '?')))
                continue
            op = img[:, :, 3] >= 128
            n_black = int((np.all(img[:, :, :3] == 0, axis=2) & op).sum())
            if n_black <= 0:
                bad_black.append(rel)

    check(not bad_read, '全部可解码' + ('；坏图 %s' % bad_read[:5] if bad_read else ''))
    check(not bad_alpha, '全部为 4 通道透明底 PNG'
          + ('；异常 %s' % bad_alpha[:5] if bad_alpha else ''))
    check(not bad_black, '全部自带纯黑描边（contour_only 前提）'
          + ('；无描边 %s' % bad_black[:5] if bad_black else ''))

    print('\n[4] 写 index.json')
    with open(IDX, 'w', encoding='utf-8') as f:
        json.dump(new_index, f, ensure_ascii=False, indent=2, sort_keys=True)
    print('  %d 条（file/files/id/level/frames）' % len(new_index))

    print('\n[5] 总览')
    dist = {}
    for ent in new_index.values():
        dist[ent['frames']] = dist.get(ent['frames'], 0) + 1
    print('  每只怪帧数分布：', dict(sorted(dist.items())))
    print('  素材库总张数：%d' % total)

    print('\n' + '=' * 64)
    if FAILS:
        print('校验失败 %d 项：' % len(FAILS))
        for m in FAILS:
            print('  -', m)
        return 1
    print('完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
