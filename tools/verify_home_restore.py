# -*- coding: utf-8 -*-
'''「重录主线时抢救回正线」的自检 —— 专盯 2026-09-17 新加的那道**几何校验**。

用法：
    python -m tools.verify_home_restore

## 为什么要有它

2026-09-16 加的抢救逻辑**只比尺寸**，实测 `废都南方工地` 撞上反例：
底图前后两次录制**完全相同**（相位相关 dx=dy=0、模板匹配 0.995、74% 像素逐位相等），
可主线从 (92~106,120~124) 换到了 (46~84,106~110)（用户重录时换了巡逻位置）——
那份"尺寸刚好一致"的旧回正线离新主线 27~44px ⇒ 判废。
⇒ 只比尺寸就会**救回一条废线**，用户以为有回正线，掉出去照样站桩。

所以新增的判据是「**几何也得对得上**」（走引擎自己的判废口径）。这个自检把它钉死：
  【1】自洽的一套（夹具主线 + 夹具回正线）→ 必须判**可用**
  【2】回正线整体平移 +40px（合成"给别的位置画的"）→ 必须判**不可用**，且原因可读
  【3】压根没有回正线 → 判不可用，且**不许抛异常**
  【4】`_restore_home_route_from_backup`：自洽 → 还原并返回 True
  【5】同上传错位的 → **不还原**（目标文件必须不存在）+ 返回 False
  【6】尺寸与底图不一致 → 不还原 + 返回 False
  【7】新目录里已经有回正线 → 不覆盖 + 返回 False

⚠️ 本自检**只读真实地图**；它自己建一个临时目录 `minimaps/_自检_回正线抢救`
   跑完就删（用真夹具 tools/test_fixtures/废都南方工地/，不碰用户录制数据）。
'''
from __future__ import annotations

import os
import shutil
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from src.utils.common import load_image, resource_path, load_yaml, active_config_path

FAIL: list = []
TMP_MAP = "_自检_回正线抢救"
FIXTURE = os.path.join("tools", "test_fixtures", "废都南方工地")
FILES = ("map.png", "route1.png", "route2.png", "route_home.png")


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def _merge(base, custom):
    for k, v in custom.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _cfg():
    cfg = load_yaml("config/config_default.yaml")
    if os.path.exists("config/config_custom.yaml"):
        cfg = _merge(cfg, load_yaml("config/config_custom.yaml"))
    ap = active_config_path()
    if ap and os.path.exists(ap):
        cfg = _merge(cfg, load_yaml(ap))
    return cfg


def _make_recorder(map_name, cfg):
    from tools.routeRecorder import RouteRecorder
    r = RouteRecorder.__new__(RouteRecorder)     # 绕过 __init__：不建抓帧、不连游戏
    r.args = types.SimpleNamespace(new_map=map_name)
    r.cfg = cfg
    return r


def _shift_x(src, dst, dx):
    """把路线图整体沿 x 平移 dx（模拟"这条线是给别的位置画的"）。"""
    img = load_image(src)
    out = np.zeros_like(img)
    h, w = img.shape[:2]
    if dx >= 0:
        out[:, dx:] = img[:, :w - dx]
    else:
        out[:, :w + dx] = img[:, -dx:]
    import cv2
    cv2.imencode(".png", out)[1].tofile(dst)


def main():
    print("=" * 68)
    print("回正线抢救 · 自检（几何校验）")
    print("=" * 68)

    cfg = _cfg()
    tmp = resource_path(os.path.join("minimaps", TMP_MAP))
    try:
        # ── 建临时地图目录（真夹具）────────────────────────────────────
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        for f in FILES:
            shutil.copy(resource_path(os.path.join(FIXTURE, f)),
                        os.path.join(tmp, f))
        rec = _make_recorder(TMP_MAP, cfg)

        # ── 【1】自洽的一套 → 可用 ────────────────────────────────────
        ok, why = rec._home_route_still_usable()
        check("【1】自洽（夹具主线 + 夹具回正线）→ 可用", ok, True)
        check("【1】可用时原因应为空", why, "")

        # ── 【2】回正线平移 +40px（给别的位置画的）→ 不可用 ─────────────
        _shift_x(os.path.join(tmp, "route_home.png"),
                 os.path.join(tmp, "route_home.png"), 40)
        ok2, why2 = rec._home_route_still_usable()
        check("【2】错位 +40px 的回正线 → 不可用", ok2, False)
        check("【2】原因里要说清「判为废线」", "废线" in (why2 or ""), True)

        # ── 【3】没有回正线 → 不可用且不抛异常 ────────────────────────
        os.remove(os.path.join(tmp, "route_home.png"))
        ok3, why3 = rec._home_route_still_usable()
        check("【3】没有回正线 → 不可用", ok3, False)
        check("【3】不抛异常且给出原因", bool(why3), True)

        # ── 【4】抢救：自洽的回正线 → 还原成功 ────────────────────────
        shutil.copy(resource_path(os.path.join(FIXTURE, "route_home.png")),
                    os.path.join(tmp, "route_home.png"))
        bak = resource_path(os.path.join("minimaps", TMP_MAP + ".bak"))
        shutil.rmtree(bak, ignore_errors=True)
        os.makedirs(bak, exist_ok=True)
        shutil.copy(resource_path(os.path.join(FIXTURE, "route_home.png")),
                    os.path.join(bak, "route_home.png"))
        os.remove(os.path.join(tmp, "route_home.png"))     # 新目录空着才谈得上"还原"
        check("【4】自洽 → 还原成功", rec._restore_home_route_from_backup(bak, tmp), True)
        check("【4】还原后文件真的在", os.path.exists(os.path.join(tmp, "route_home.png")), True)

        # ── 【5】抢救：错位的回正线 → **不还原** ──────────────────────
        _shift_x(os.path.join(bak, "route_home.png"),
                 os.path.join(bak, "route_home.png"), 40)
        os.remove(os.path.join(tmp, "route_home.png"))
        check("【5】错位 → 返回 False（不救）",
              rec._restore_home_route_from_backup(bak, tmp), False)
        check("【5】错位 → **目标文件不许存在**（不塞废线进去）",
              os.path.exists(os.path.join(tmp, "route_home.png")), False)

        # ── 【6】尺寸不一致 → 不还原 ──────────────────────────────────
        big = np.zeros((10, 10, 3), dtype=np.uint8)
        import cv2
        cv2.imencode(".png", big)[1].tofile(os.path.join(bak, "route_home.png"))
        check("【6】尺寸不一致 → 返回 False",
              rec._restore_home_route_from_backup(bak, tmp), False)
        check("【6】尺寸不一致 → 目标文件不存在",
              os.path.exists(os.path.join(tmp, "route_home.png")), False)

        # ── 【7】新目录已有回正线 → 不覆盖 ────────────────────────────
        shutil.copy(resource_path(os.path.join(FIXTURE, "route_home.png")),
                    os.path.join(tmp, "route_home.png"))
        before = os.path.getsize(os.path.join(tmp, "route_home.png"))
        check("【7】已有回正线 → 返回 False",
              rec._restore_home_route_from_backup(bak, tmp), False)
        check("【7】已有回正线 → 原文件没被改",
              os.path.getsize(os.path.join(tmp, "route_home.png")), before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(resource_path(os.path.join("minimaps", TMP_MAP + ".bak")),
                      ignore_errors=True)
        print(f"\n临时目录已清理：{not os.path.exists(tmp)}")

    print("=" * 68)
    if FAIL:
        print(f"❌ 自检失败 {len(FAIL)} 项：")
        for n in FAIL:
            print(f"   - {n}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
