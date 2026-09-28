# -*- coding: utf-8 -*-
"""素材库自检 —— 不依赖 config_data.yaml / monster/ 等被 gitignore 的东西（CI 可跑）。

检查：
  1. monster_lib/index.json 存在且能读
  2. 索引里每个条目的文件真实存在，且能解码
  3. 每张素材都带**纯黑描边**（contour_only 模式赖以工作的前提，缺了就是废图）
  4. 每张素材有透明通道（转换绿底的依据）
  5. install() 能把素材装成符合项目约定的模板（绿底 + 黑描边保留），
     且不覆盖用户已有的模板

为什么必须查第 3 条：引擎在 contour_only 模式下只取纯黑像素当轮廓
（np.all(img == [0,0,0])）。素材若没有纯黑描边，装进去也认不出怪 ——
这是静默失效，必须在这里拦住。

用法：python -m tools.verify_mob_library
"""
import os
import sys

import cv2
import numpy as np

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.mob_library import (LIB_DIR, GREEN, load_index,  # noqa: E402
                                   install, _to_template)

FAILS = []


def check(cond, msg):
    if cond:
        print(f'  [OK]   {msg}')
    else:
        print(f'  [FAIL] {msg}')
        FAILS.append(msg)


def main():
    print('=' * 60)
    print('素材库自检')
    print(f'库目录：{LIB_DIR}')
    print('=' * 60)

    print('\n[1] 索引可读')
    idx = load_index()
    check(bool(idx), f'index.json 读到 {len(idx)} 种怪')
    if not idx:
        print('\n索引空，后续检查跳过。')
        return 1

    print('\n[2] 素材文件存在且可解码')
    bad_missing, decoded = [], {}
    for name, ent in sorted(idx.items()):
        p = os.path.join(LIB_DIR, ent.get('file', ''))
        if not os.path.isfile(p):
            bad_missing.append(name)
            continue
        img = cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            bad_missing.append(name)
            continue
        decoded[name] = img
    check(not bad_missing,
          f'{len(decoded)}/{len(idx)} 张素材可解码'
          + (f'；缺失/坏图：{bad_missing}' if bad_missing else ''))

    print('\n[3] 每张都带纯黑描边（contour_only 的前提）')
    no_black = []
    for name, img in decoded.items():
        bgr = img[:, :, :3].astype(int)
        op = img[:, :, 3] > 0 if img.shape[2] == 4 else np.ones(img.shape[:2], bool)
        n = int((np.all(bgr == [0, 0, 0], axis=2) & op).sum())
        if n <= 0:
            no_black.append(name)
    check(not no_black,
          '全部素材自带纯黑描边'
          + (f'；无描边：{no_black}' if no_black else ''))

    print('\n[4] 素材带透明通道')
    no_alpha = [n for n, im in decoded.items() if im.shape[2] != 4]
    check(not no_alpha,
          '全部素材为透明底 PNG'
          + (f'；无 alpha：{no_alpha}' if no_alpha else ''))

    print('\n[5] 转模板后仍是绿底 + 保留黑描边')
    import tempfile
    probe = sorted(decoded)[0]
    tpl = _to_template(decoded[probe])
    ok_green = tpl is not None and bool(np.all(tpl == GREEN, axis=2).any())
    ok_black = tpl is not None and bool(np.all(tpl == [0, 0, 0], axis=2).any())
    check(ok_green, f'{probe}：转换后含纯绿背景 RGB(0,255,0)')
    check(ok_black, f'{probe}：转换后纯黑描边被保留（未被刷成绿）')

    print('\n[6] install() 端到端（临时目录，不碰真实 monster/）')
    with tempfile.TemporaryDirectory() as td:
        r1 = install(probe, root=td)
        dst = os.path.join(td, 'monster', probe, f'{probe}_1.png')
        check(r1 == 'installed', f'首次安装返回 installed（实际 {r1}）')
        check(os.path.isfile(dst), f'模板落在 monster/{probe}/{probe}_1.png')
        if os.path.isfile(dst):
            out = cv2.imdecode(np.fromfile(dst, dtype=np.uint8), cv2.IMREAD_COLOR)
            check(out is not None and out.shape[2] == 3, '落盘模板是 3 通道 BGR')
            check(out is not None and bool(np.all(out == GREEN, axis=2).any()),
                  '落盘模板背景是纯绿')
            check(out is not None and bool(np.all(out == [0, 0, 0], axis=2).any()),
                  '落盘模板保留了纯黑描边')
        # 二次安装不得覆盖（保护用户自己截的）
        r2 = install(probe, root=td)
        check(r2 == 'exists', f'重复安装返回 exists、不覆盖（实际 {r2}）')
        r3 = install('__不存在的怪__', root=td)
        check(r3 == 'missing', f'库里没有的名字返回 missing（实际 {r3}）')

    print('\n' + '=' * 60)
    if FAILS:
        print(f'自检失败：{len(FAILS)} 项')
        for m in FAILS:
            print(f'  - {m}')
        return 1
    print('自检通过：素材库可用。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
