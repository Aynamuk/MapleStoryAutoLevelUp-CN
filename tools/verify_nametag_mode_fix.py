# -*- coding: utf-8 -*-
'''
离线自检：white_mask 存量配置自动纠正（2026-09-28 回归修复）

背景：v1.0.11 把 nametag.mode 默认值（及标定工具写入值）改成 white_mask，
是一次回归（真机帧 14/14 复现：匹配锁死填充边、分数完美无告警）。
本修复在引擎 load_config 里自动把 white_mask 纠正为 grayscale。

本自检验三件事（不开游戏、不要窗口）：
  1. 存量 white_mask 配置 → load_config 后被改为 grayscale；
  2. 显式 grayscale 配置 → 不动（幂等，不误伤）；
  3. 显式 histogram_eq 配置 → 不动（保留用户显式选择的权利）。

用法：
    python -m tools.verify_nametag_mode_fix
'''
import sys

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.common import load_yaml

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def merge(base, custom):
    for k, v in custom.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def new_bot():
    import types
    args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                 debug=False, disable_viz=True)
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.args = args
    b.cfg = None
    b.data = load_yaml("config/config_data.yaml")
    b.monsters_info = {}
    b.img_routes = []
    b.img_route_home = None
    b.img_map = None
    b.img_route = None
    b.color_code = {}
    b.color_code_up_down = {}
    b.is_show_debug_window = False
    b.img_route_debug = None
    b.idx_routes = 0
    b.is_using_home_route = False
    b.is_terminated = False
    b.kb = None
    b.capture = None
    return b


def run_load(mode_in):
    '''构造一个 nametag.mode=mode_in 的配置走完整 load_config，返回纠正后的 mode。'''
    import copy
    base = load_yaml("config/config_default.yaml")
    cfg = copy.deepcopy(base)
    cfg["nametag"]["mode"] = mode_in
    # load_config 用的 nametag 模板必须存在，用 Kumanya（仓库自带）
    cfg["nametag"]["name"] = "Kumanya"
    b = new_bot()
    ret = b.load_config(cfg)
    if ret not in (None, 0):
        raise RuntimeError(f"load_config 返回 {ret}")
    return cfg["nametag"]["mode"]


def main():
    print("=" * 60)
    print(" white_mask 存量配置自动纠正 自检")
    print("=" * 60)

    print("\n【1】存量 white_mask（v1.0.11 期间标定的用户配置）→ 自动纠正")
    check("white_mask 被纠正为 grayscale", run_load("white_mask"), "grayscale")

    print("\n【2】显式 grayscale（正确值）→ 不动")
    check("grayscale 保持不变", run_load("grayscale"), "grayscale")

    print("\n【3】显式 histogram_eq（用户显式选择）→ 不动")
    check("histogram_eq 保持不变", run_load("histogram_eq"), "histogram_eq")

    print("\n" + "=" * 60)
    if FAIL:
        print(f"❌ 自检失败 {len(FAIL)} 项：")
        for n in FAIL:
            print(f"   - {n}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
