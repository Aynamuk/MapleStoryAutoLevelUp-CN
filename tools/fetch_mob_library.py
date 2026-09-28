# -*- coding: utf-8 -*-
"""从国服资料库抓全量怪物素材，建 monster_lib/（一次性建库脚本）。

源：怀旧冒险岛资料库（国服 CMSCV001 数据集）
    https://static.mxdzlk.com/CMSCV001/images/mob/<怪物ID>/preview.png

这些是**透明底**的官方贴图，自带纯黑描边 —— 直接适配引擎的 contour_only 模式。

## 用法（在项目根目录）

    python -m tools.fetch_mob_library                 # 抓全部（清单见 IDS）
    python -m tools.fetch_mob_library --refresh-list  # 先从资料库刷新 ID/等级清单
    python -m tools.fetch_mob_library --ids 100100,1120100
    python -m tools.fetch_mob_library --force         # 已存在的也重下

抓到 monster_lib/<中文名>/<中文名>.png，并写 monster_lib/index.json。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# 仓库根
try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LIB_DIR = os.path.join(REPO_ROOT, 'monster_lib')
URL = 'https://static.mxdzlk.com/CMSCV001/images/mob/{id}/preview.png'
LIST_URL = 'https://mxdzlk.com/monster/'

UA = {'User-Agent': 'Mozilla/5.0 (compatible; MapleStoryAutoLevelUp-CN lib builder)',
      'Referer': 'https://mxdzlk.com/'}

#: 清单缓存（--refresh-list 会更新它）
IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mob_ids.json')

#: 内置全量清单：国服怀旧服资料库收录的 82 只怪（ID -> [中文名, 等级]）。
#: 等级来自资料库；少数标 '?' 的是资料库未给等级的（多为活动/特殊怪）。
DEFAULT_IDS = {
    # ── Lv 1~19 新手区 ─────────────────────────────────────
    '100100': ['蜗牛', 1], '9300018': ['特殊小石球', 1],
    '100101': ['蓝蜗牛', 2], '120100': ['蘑菇仔', 2],
    '130100': ['木妖', 4], '130101': ['红蜗牛', 4],
    '210100': ['绿水灵', 6], '1210100': ['猪猪', 7],
    '1210102': ['花蘑菇', 8], '1110101': ['黑木妖', 10],
    '1210101': ['漂漂猪', 10], '1120100': ['三眼章鱼', 12],
    '1110100': ['绿蘑菇', 15], '1210103': ['蓝水灵', 15],
    '1130100': ['斧木妖', 17], '1140100': ['古木妖', 19],
    # ── Lv 20~39 中期 ──────────────────────────────────────
    '2220000': ['红蜗牛王', 20], '2220100': ['蓝蘑菇', 20],
    '2300100': ['蝙蝠', 20], '2130103': ['青蛇', 21],
    '2110200': ['刺蘑菇', 22], '2130100': ['黑斧木妖', 22],
    '2230110': ['木面怪人', 23], '2230101': ['无魂蘑菇', 24],
    '2230111': ['石面怪人', 24], '2230102': ['野猪', 25],
    '2230100': ['火独眼兽', 27], '3000002': ['蝴蝶精', 30],
    '3000003': ['蝴蝶精', 30], '3000004': ['蝴蝶精', 30],
    '3110100': ['鳄鱼', 32], '3210100': ['火野猪', 32],
    '3220000': ['树妖王', 35], '3230100': ['风独眼兽', 35],
    '3230101': ['小幽灵', 35], '3230300': ['幼魔精灵', 35],
    '3230301': ['幼魔精灵', 35], '9000001': ['风独眼兽2', 35],
    '9000002': ['刺蘑菇2', 35], '9000100': ['火野猪2', 35],
    '9000101': ['猴子2', 35], '9000200': ['火独眼兽2', 35],
    '9000201': ['无魂蘑菇2', 35], '9000300': ['冰独眼兽2', 35],
    '9000301': ['蓝蘑菇2', 35], '3210800': ['猴子', 37],
    '3230102': ['红螃蟹', 37],
    # ── Lv 40~59 后期 ──────────────────────────────────────
    '4230100': ['冰独眼兽', 40], '4090000': ['铁甲猪', 42],
    '4230103': ['铁甲猪', 42], '4230125': ['石膏犬', 44],
    '4130100': ['土龙', 45], '4230400': ['钢甲猪', 45],
    '4130101': ['乌龟', 46], '4230126': ['木乃伊犬', 47],
    '4230102': ['大幽灵', 48], '4230104': ['青螃蟹', 48],
    '5130100': ['青龙', 50], '5220002': ['浮士德', 50],
    '5130103': ['黑鳄鱼', 52], '5130101': ['石头人', 55],
    '5220000': ['巨居蟹', 55], '5220001': ['巨居蟹', 55],
    '5090000': ['谢尔德', 56], '5150001': ['石膏士兵', 57],
    '5130102': ['黑石头人', 58], '5150000': ['混种石头人', 59],
    # ── Lv 60+ 高阶 ────────────────────────────────────────
    '6130100': ['赤龙', 60], '6230600': ['冰龙', 64],
    '6220000': ['多尔', 65], '6300005': ['无魂蘑菇王', 65],
    '7130101': ['长枪牛魔王', 75], '8220008': ['未知的小吃店', 85],
    # ── 资料库未标等级（活动/特殊怪）───────────────────────
    '4230101': ['无魂猴', '?'], '5300100': ['巫婆', '?'],
    '6130101': ['蘑菇王', '?'], '6230100': ['怪猫', '?'],
    '6230601': ['黑恐龙', '?'], '6230602': ['石膏士官', '?'],
    '7130100': ['月牙牛魔王', '?'], '7130103': ['石膏指挥官', '?'],
    '8130100': ['蝙蝠怪', '?'],
}


def _fetch_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def refresh_list():
    """从资料库抓「ID -> 中文名」全量清单（分页 ?page_num=N）。

    等级要逐只开详情页，太慢，所以这里只取名字；等级用内置清单里的。
    """
    out = {}
    for pg in range(1, 10):
        u = (LIST_URL if pg == 1 else f'{LIST_URL}?page_num={pg}')
        h = _fetch_bytes(u)
        if not h:
            break
        h = h.decode('utf-8', 'replace')
        found = dict(re.findall(r'/monster/(\d+)/"[^>]*title="([^"]*)"', h))
        if not found:
            break
        before = len(out)
        out.update(found)
        if len(out) == before:
            break          # 没有新增 = 到底了
        time.sleep(0.2)
    return out


def main():
    ap = argparse.ArgumentParser(description='抓国服怪物素材建 monster_lib/')
    ap.add_argument('--ids', help='只抓这些 ID（逗号分隔）')
    ap.add_argument('--force', action='store_true', help='已存在也重下')
    ap.add_argument('--refresh-list', action='store_true',
                    help='先从资料库刷新 ID/名字清单再抓')
    args = ap.parse_args()

    todo = {k: tuple(v) for k, v in DEFAULT_IDS.items()}
    if args.refresh_list:
        live = refresh_list()
        print(f'资料库当前收录 {len(live)} 只')
        merged = {}
        for mid, name in live.items():
            lv = todo.get(mid, ['', ''])[1]
            merged[mid] = (name, lv)
        for mid, (name, lv) in todo.items():
            if mid not in merged:
                merged[mid] = (name, lv)      # 保留内置但资料库没列出的
        todo = merged
        with open(IDS_FILE, 'w', encoding='utf-8') as f:
            json.dump({k: list(v) for k, v in sorted(todo.items())},
                      f, ensure_ascii=False, indent=1)

    if args.ids:
        want = {s.strip() for s in args.ids.split(',') if s.strip()}
        todo = {k: v for k, v in todo.items() if k in want}

    os.makedirs(LIB_DIR, exist_ok=True)
    idx_path = os.path.join(LIB_DIR, 'index.json')
    index = {}
    if os.path.isfile(idx_path) and not args.force:
        try:
            with open(idx_path, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except Exception:
            index = {}

    ok = fail = skip = 0
    # 同名怪（如「蝴蝶精」有 3 个 ID）：加 ID 后缀区分，避免互相覆盖
    name_count = {}
    for mid, (name, _lv) in todo.items():
        name_count[name] = name_count.get(name, 0) + 1

    seen = {}
    for mid, (name, lv) in sorted(todo.items()):
        display = name
        if name_count[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            display = f'{name}_{seen[name]}'      # 同为「蝴蝶精」→ 蝴蝶精_1/_2/_3
        rel = f'{display}/{display}.png'
        dst = os.path.join(LIB_DIR, display, f'{display}.png')
        if os.path.isfile(dst) and not args.force:
            index[display] = {'file': rel, 'id': mid, 'level': lv}
            skip += 1
            continue
        data = _fetch_bytes(URL.format(id=mid))
        if not data or len(data) < 100:
            fail += 1
            print(f'  失败 {display} ({mid})')
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as f:
            f.write(data)
        index[display] = {'file': rel, 'id': mid, 'level': lv}
        ok += 1
        print(f'  完成 {display} (Lv{lv}, {mid}) {len(data)}B')
        time.sleep(0.15)          # 对第三方站客气一点

    with open(idx_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f'\n新抓 {ok}，跳过 {skip}，失败 {fail}；索引共 {len(index)} 种')
    print(f'库目录：{LIB_DIR}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
