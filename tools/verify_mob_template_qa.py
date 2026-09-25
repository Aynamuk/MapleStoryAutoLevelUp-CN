# -*- coding: utf-8 -*-
"""离线自检 —— 怪物模板查重模块（src/utils/mob_template_qa.py）

全部用**合成图**跑，不依赖任何真实游戏素材，所以随时可跑、结果稳定。

跑法（项目根目录）：
    python -m tools.verify_mob_template_qa
"""
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.mob_template_qa import (  # noqa: E402
    DUP_THRES_DEFAULT, contour_mask, content_crop, find_duplicate_groups,
    group_duplicates, load_masks, mask_pixels, old_hits_frame, template_paths,
    template_score,
)

N_OK = 0
N_FAIL = 0
FAILS = []


def ck(name, got, want):
    global N_OK, N_FAIL
    ok = got == want
    if ok:
        N_OK += 1
    else:
        N_FAIL += 1
        FAILS.append(name)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")


def ck_true(name, cond, detail=""):
    global N_OK, N_FAIL
    if cond:
        N_OK += 1
        print(f"  [OK ] {name}{(' — ' + detail) if detail else ''}")
    else:
        N_FAIL += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------- 合成素材

def make_monster(kind="A", size=40):
    """造一个"怪"：白底 + 黑色轮廓（引擎只用黑像素当特征）。"""
    img = np.full((size, size, 3), 255, np.uint8)
    if kind == "A":
        cv2.rectangle(img, (7, 7), (size - 8, size - 8), (0, 0, 0), 3)
        cv2.circle(img, (size // 2, size // 2), 5, (0, 0, 0), -1)
    elif kind == "B":
        cv2.line(img, (6, 6), (size - 7, size - 7), (0, 0, 0), 4)
        cv2.line(img, (size - 7, 6), (6, size - 7), (0, 0, 0), 4)
    elif kind == "C":                       # A 的"轻微变体"：框粗一点
        cv2.rectangle(img, (7, 7), (size - 8, size - 8), (0, 0, 0), 4)
        cv2.circle(img, (size // 2, size // 2), 5, (0, 0, 0), -1)
    return img


def make_template(kind="A", size=40):
    """把白底刷成项目约定的纯绿，做成"模板"。"""
    img = make_monster(kind, size).copy()
    img[np.all(img == 255, axis=2)] = (0, 255, 0)
    return img


def write_png(path, img):
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("encode failed")
    buf.tofile(path)


# ---------------------------------------------------------------- 用例

def t1_contour_mask():
    print("\n【1】contour_mask —— 与引擎同款的「取黑像素 + 高斯模糊」")
    black = np.zeros((20, 20, 3), np.uint8)
    ck("纯黑图 → 全 255", int(contour_mask(black).min()), 255)
    green = np.zeros((20, 20, 3), np.uint8)
    green[:, :] = (0, 255, 0)
    ck("纯绿图 → 全 0", int(contour_mask(green).max()), 0)
    ck_true("blur 传非法值不抛", contour_mask(black, "abc").shape == (20, 20),
            "回落默认核")
    ck_true("blur 传 0 不抛", contour_mask(black, 0).shape == (20, 20), "回落默认核")


def t2_content_crop():
    print("\n【2】content_crop —— 裁到内容，去掉边缘空白")
    m = np.zeros((30, 40), np.uint8)
    m[10:20, 5:15] = 255
    ck("裁完尺寸", content_crop(m).shape, (10, 10))
    ck("空掩码原样返回（不崩）", content_crop(np.zeros((7, 9), np.uint8)).shape, (7, 9))


def t3_template_score():
    print("\n【3】template_score —— 0 = 一模一样，越大越不像")
    a = contour_mask(make_template("A"))
    a2 = contour_mask(make_template("A"))
    b = contour_mask(make_template("B"))
    s_same = template_score(a, a2)
    s_diff = template_score(a, b)
    ck_true("同一张 → 几乎 0", s_same < 0.01, f"score={s_same:.4f}")
    ck_true("完全不同的两张 → 明显大", s_diff > 0.4, f"score={s_diff:.4f}")
    ck_true("默认判据落在两者之间", s_same <= DUP_THRES_DEFAULT < s_diff,
            f"{s_same:.4f} <= {DUP_THRES_DEFAULT} < {s_diff:.4f}")

    # 尺寸不同但内容一样（多了一圈空白）→ 仍应判为很像
    pad = np.zeros((60, 60, 3), np.uint8)
    pad[:, :] = (0, 255, 0)
    t = make_template("A")
    pad[10:10 + t.shape[0], 12:12 + t.shape[1]] = t
    s_pad = template_score(a, contour_mask(pad))
    ck_true("尺寸不同、内容相同 → 仍很小", s_pad < 0.05, f"score={s_pad:.4f}")


def t4_group_duplicates():
    print("\n【4】group_duplicates —— 互为重复的并成一组")
    A = ("a.png", contour_mask(make_template("A")))
    A2 = ("a2.png", contour_mask(make_template("A")))
    B = ("b.png", contour_mask(make_template("B")))
    g = group_duplicates([A, A2, B])
    ck("1 组（A 与 A2）", len(g), 1)
    ck("组内 2 张", len(g[0]) if g else 0, 2)
    ck("全不同 → 0 组", len(group_duplicates([A, B])), 0)
    ck("只有 1 张 → 0 组", len(group_duplicates([A])), 0)
    ck("空 → 0 组", len(group_duplicates([])), 0)
    # 三张互为重复（A / A2 / C，C 是 A 的粗线变体）
    C = ("c.png", contour_mask(make_template("C")))
    g3 = group_duplicates([A, A2, C])
    ck("三张串成一组", len(g3), 1)
    ck("组内 3 张", len(g3[0]) if g3 else 0, 3)


def t5_old_hits_frame():
    print("\n【5】old_hits_frame —— 旧模板能不能在「刚框的那只怪」上认出来")
    frame = np.full((300, 400, 3), 255, np.uint8)
    frame[100:140, 150:190] = make_monster("A", 40)
    box = (150, 100, 40, 40)

    old_a = [("oldA.png", contour_mask(make_template("A")))]
    hit, score, which = old_hits_frame(frame, box, old_a)
    ck("旧模板同款 → 命中", hit, True)
    ck_true("得分很低", score is not None and score <= DUP_THRES_DEFAULT,
            f"score={score:.4f}")
    ck("命中来源是它", os.path.basename(which) if which else None, "oldA.png")

    old_b = [("oldB.png", contour_mask(make_template("B")))]
    hit2, score2, _ = old_hits_frame(frame, box, old_b)
    ck("旧模板是另一种形状 → 不命中", hit2, False)
    ck_true("得分明显高", score2 is not None and score2 > DUP_THRES_DEFAULT,
            f"score={score2:.4f}")

    ck("没有旧模板 → (False, None, None)",
       old_hits_frame(frame, box, []), (False, None, None))

    # 框在画面外 / 越界不能崩
    ck_true("框贴到画面右边界不崩",
            old_hits_frame(frame, (395, 295, 40, 40), old_a)[0] in (True, False),
            "已裁剪到画面内")

    # 多张旧模板：应报"最像的那张"
    both = [("oldB.png", contour_mask(make_template("B"))),
            ("oldA.png", contour_mask(make_template("A")))]
    hit3, _, which3 = old_hits_frame(frame, box, both)
    ck("多张时挑最像的", os.path.basename(which3) if which3 else None, "oldA.png")
    ck("多张时仍判命中", hit3, True)


def t6_end_to_end_tmpdir():
    print("\n【6】端到端（临时目录）—— 写盘 → 扫描 → 找出重复组")
    tmp = tempfile.mkdtemp(prefix="mobqa_")
    try:
        root = os.path.join(tmp, "root")
        d = os.path.join(root, "monster", "testmob")
        os.makedirs(d)
        write_png(os.path.join(d, "testmob_1.png"), make_template("A"))
        write_png(os.path.join(d, "testmob_2.png"), make_template("A"))   # 重复
        write_png(os.path.join(d, "testmob_3.png"), make_template("B"))   # 不同
        ck("template_paths 找到 3 张", len(template_paths(root, "testmob")), 3)
        ck("load_masks 读到 3 张", len(load_masks(template_paths(root, "testmob"))), 3)
        g = find_duplicate_groups(root, "testmob")
        ck("找到 1 个重复组", len(g), 1)
        names = sorted(os.path.basename(p) for p, _ in g[0]) if g else []
        ck("组内是 1 和 2", names, ["testmob_1.png", "testmob_2.png"])
        ck("不存在的怪 → 0 张", len(template_paths(root, "nope")), 0)
        ck("不存在的怪 → 0 组", len(find_duplicate_groups(root, "nope")), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t7_mask_pixels():
    print("\n【7】mask_pixels —— 报告用的「带了多少信息」")
    ck("全零 → 0", mask_pixels(np.zeros((10, 10), np.uint8)), 0)
    m = np.zeros((10, 10), np.uint8)
    m[0, :] = 255
    ck("一行 10 个 → 10", mask_pixels(m), 10)


def t8_real_library_info():
    """真实素材只做信息性打印，**不判失败** —— 用户以后加模板不该让自检变红。"""
    print("\n【8】真实库现状（信息性，不参与判定）")
    mob_root = os.path.join(REPO_ROOT, "monster")
    if not os.path.isdir(mob_root):
        print("    （没有 monster/ 目录，跳过）")
        return
    total = 0
    for name in sorted(os.listdir(mob_root)):
        if not os.path.isdir(os.path.join(mob_root, name)):
            continue
        paths = template_paths(REPO_ROOT, name)
        total += len(paths)
        groups = find_duplicate_groups(REPO_ROOT, name)
        n_dup = sum(len(g) - 1 for g in groups)
        flag = f"  ⚠️ 有 {n_dup} 张重复" if n_dup else ""
        print(f"    {name}: {len(paths)} 张 → 匹配 {len(paths) * 2} 次{flag}")
    print(f"    合计 {total} 张 → 每次检测匹配 {total * 2} 次")


def main():
    print("=" * 62)
    print("怪物模板查重模块 离线自检")
    print("=" * 62)
    t1_contour_mask()
    t2_content_crop()
    t3_template_score()
    t4_group_duplicates()
    t5_old_hits_frame()
    t6_end_to_end_tmpdir()
    t7_mask_pixels()
    t8_real_library_info()

    print("\n" + "=" * 62)
    if N_FAIL == 0:
        print(f"✅ 全部通过（{N_OK} 项）")
    else:
        print(f"❌ {N_FAIL} 项失败 / 共 {N_OK + N_FAIL} 项")
        for n in FAILS:
            print(f"   - {n}")
    print("=" * 62)
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
