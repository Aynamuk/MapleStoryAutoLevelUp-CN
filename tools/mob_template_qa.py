# -*- coding: utf-8 -*-
"""怪物模板重复体检 —— 列出 monster/ 下互相重复的模板。

**只报告，不删。** 要不要删、删哪张由人决定（自动删风险太高：
删掉一张其实是孤本的模板 = 以后遇到那个姿势就漏怪）。

## 为什么值得跑

引擎对每张 PNG 都会生成「原图 + 左右镜像」两个模板，所以
    **每次怪检测的匹配次数 = PNG 张数 × 2**
多留一张模板，就是每次检测多跑两次全 ROI 的 matchTemplate（每次十几毫秒）。
而重复的模板删掉是**零损失**的 —— 它本来就只是同一个姿势重截了一遍。

## 用法（在项目根目录执行）

    python -m tools.mob_template_qa               # 扫全部怪
    python -m tools.mob_template_qa --name 木妖    # 只扫一种怪
    python -m tools.mob_template_qa --thres 0.5   # 换判据（默认 0.4）

判据见 src/utils/mob_template_qa.py 的模块说明（与引擎同源：互相匹配
TM_SQDIFF_NORMED <= 阈值 即互为重复）。
"""
import argparse
import os
import sys

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.mob_template_qa import (  # noqa: E402
    DUP_THRES_DEFAULT, find_duplicate_groups, load_masks, mask_pixels,
    template_paths,
)


def list_mobs(root):
    d = os.path.join(root, "monster")
    if not os.path.isdir(d):
        return []
    return sorted(x for x in os.listdir(d)
                  if os.path.isdir(os.path.join(d, x)))


def report_one(root, name, thres):
    """扫一种怪。返回 (总张数, 重复组列表)。"""
    paths = template_paths(root, name)
    print(f"\n=== {name} ===  {len(paths)} 张 → 引擎每次检测匹配 {len(paths) * 2} 次")
    if not paths:
        print("    （目录里没有模板）")
        return 0, []
    if len(paths) < 2:
        print("    只有一张，无从重复。")
        return len(paths), []

    groups = find_duplicate_groups(root, name, thres)
    if not groups:
        print(f"    ✅ 没有互相重复的模板（判据 {thres}）")
        return len(paths), []

    # 把每组的掩码读回来算像素数，好让报告能给出"建议留哪张"
    masks = dict(load_masks(paths))
    drop_total = 0
    for gi, g in enumerate(groups, 1):
        rows = []
        for p, m in g:
            rows.append((mask_pixels(m), os.path.basename(p), m.shape))
        rows.sort(reverse=True)                 # 轮廓像素多的排前面
        drop_total += len(rows) - 1
        print(f"\n    ⚠️ 重复组 {gi}：{len(rows)} 张互相重复")
        for i, (px, base, shape) in enumerate(rows):
            tag = "  ← 建议保留（轮廓最完整）" if i == 0 else "  ← 可删"
            print(f"        {base:20s} 轮廓 {px:6d} px  尺寸 {shape[1]}x{shape[0]}{tag}")
        print(f"        判据说明：组内两两相似度都 <= {thres}，"
              f"删到只剩 1 张不会丢识别能力")
    print(f"\n    → 这一种怪可删 {drop_total} 张，"
          f"每次检测少跑 {drop_total * 2} 次匹配")
    return len(paths), groups


def main():
    ap = argparse.ArgumentParser(description="怪物模板重复体检（只报告，不删）")
    ap.add_argument("--name", default=None, help="只检查这一种怪（默认全部）")
    ap.add_argument("--thres", type=float, default=DUP_THRES_DEFAULT,
                    help=f"查重阈值，默认 {DUP_THRES_DEFAULT}（越小越保守）")
    ap.add_argument("--root", default=REPO_ROOT, help="项目根目录（默认自动推断）")
    a = ap.parse_args()

    root = a.root
    print("=" * 62)
    print("怪物模板重复体检（只报告，不删）")
    print(f"项目根：{root}")
    print(f"判据：两张模板互相匹配 TM_SQDIFF_NORMED <= {a.thres} 即互为重复")
    print("=" * 62)

    names = [a.name] if a.name else list_mobs(root)
    if not names:
        print("\n[提示] monster/ 目录下没有找到任何怪。")
    elif a.name and not os.path.isdir(os.path.join(root, "monster", a.name)):
        print(f"\n[错误] monster/{a.name}/ 不存在。"
              f"现有怪：{', '.join(list_mobs(root)) or '（无）'}")
    else:
        total_tpl, total_drop = 0, 0
        for n in names:
            cnt, groups = report_one(root, n, a.thres)
            total_tpl += cnt
            for g in groups:
                total_drop += len(g) - 1

        print("\n" + "=" * 62)
        print(f"合计：{len(names)} 种怪 / {total_tpl} 张模板 "
              f"→ 每次检测匹配 {total_tpl * 2} 次")
        if total_drop:
            print(f"     其中 {total_drop} 张是重复的 → "
                  f"删掉可省 {total_drop * 2} 次匹配"
                  f"（{total_tpl}→{total_tpl - total_drop} 张）")
            print("\n⚠️ 本工具**不会**替你删。确认后再手动删，或让 AI 删（会先备份）。")
            print("   删之前想一想：这一张是不是某个姿势的**孤本**？"
                  "是就留着。")
        else:
            print("     ✅ 没有发现重复模板。")
        print("=" * 62)
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    except BaseException:
        # 出错必须停住让用户看到（控制台跟着进程关闭时一行都看不到）
        import traceback
        traceback.print_exc()
        code = 1
    try:
        input("\n[结束] 按回车关闭窗口 ")
    except Exception:
        pass
    sys.exit(code)
