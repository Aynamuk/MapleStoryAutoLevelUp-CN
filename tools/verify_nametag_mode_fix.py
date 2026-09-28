# -*- coding: utf-8 -*-
'''
离线自检：white_mask 存量配置自动纠正（2026-09-28 回归修复）

背景：v1.0.11 把 nametag.mode 默认值（及标定工具写入值）改成 white_mask，
是一次回归（真机帧 14/14 复现：匹配锁死填充边、分数完美无告警）。
本修复在引擎 load_config 里自动把 white_mask 纠正为 grayscale。

本自检验三件事（不开游戏、不要窗口、不依赖任何私有素材）：
  1. 存量 white_mask 配置 → load_config 后被改为 grayscale；
  2. 显式 grayscale 配置 → 不动（幂等，不误伤）；
  3. 显式 histogram_eq 配置 → 不动（保留用户显式选择的权利）。

⚠️★ 为什么这里用「未选地图」的配置（bot.map = ""）：
   自动纠正块在 load_config 的**地图分支之前**（这正是本修复的位置要求，
   见 MapleStoryAutoLevelUp.py 的注释：地图未选时会提前 return，块放它
   后面就执行不到 —— 这曾让本自检用例 1 挂掉）。
   因此用空地图，load_config 在 "地图未选择，跳过地图加载" 处 return，
   而纠正块**已经跑完** ⇒ 既能验到纠正结果，又完全不依赖：
     · minimaps/<图>/  —— 仓库只跟踪 .gitkeep（挂机点位属个人数据）
     · config_data.yaml —— .gitignore 忽略（仓库只有 .blank 空表）
     · nametag/*.png    —— .gitignore 忽略（文件名即角色名）
   这三样在 CI 检出后都不存在，所以本自检**必须**不碰它们。

用法：
    python -m tools.verify_nametag_mode_fix
'''
import copy
import os
import shutil
import sys

from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot
from src.utils.common import load_yaml

FAIL = []

# CI 上没有 config/config_data.yaml（.gitignore 忽略，仓库只有 .blank 空表），
# 而 load_config 在 L667 会无条件 load_yaml 它（在地图名判空**之前**）。
# 所以本自检在文件缺失时用 .blank 临时顶一份，跑完删除。
# ⚠️ 本地已存在时**绝不覆盖** —— 那是用户真实的地图登记表。
DATA_YAML = "config/config_data.yaml"
DATA_BLANK = "config/config_data.blank.yaml"
_tmp_data_created = False


def ensure_data_yaml():
    '''保证 config_data.yaml 存在；不存在就从 .blank 复制一份（记下以便清理）。'''
    global _tmp_data_created
    if os.path.exists(DATA_YAML):
        return False
    if not os.path.exists(DATA_BLANK):
        raise RuntimeError(f"{DATA_YAML} 与 {DATA_BLANK} 都不存在，无法构造自检环境")
    shutil.copyfile(DATA_BLANK, DATA_YAML)
    _tmp_data_created = True
    return True


def cleanup_data_yaml():
    '''只删掉本自检自己复制出来的那份（不碰用户原有的）。'''
    if _tmp_data_created and os.path.exists(DATA_YAML):
        os.remove(DATA_YAML)


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def new_bot():
    import types
    args = types.SimpleNamespace(test_image="", is_ui=False, init_state="",
                                 debug=False, disable_viz=True)
    b = MapleStoryAutoBot.__new__(MapleStoryAutoBot)
    b.args = args
    b.cfg = None
    b.data = None
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
    '''构造 nametag.mode=mode_in 的配置跑 load_config，返回纠正后的 mode。

    bot.map 留空（见模块 docstring）⇒ 只跑到地图分支的 return，
    恰好覆盖到纠正块、且不触碰 CI 上不存在的资源。
    '''
    cfg = copy.deepcopy(load_yaml("config/config_default.yaml"))
    cfg["nametag"]["mode"] = mode_in
    cfg["bot"]["map"] = ""
    b = new_bot()
    b.load_config(cfg)
    return cfg["nametag"]["mode"]


def main():
    print("=" * 60)
    print(" white_mask 存量配置自动纠正 自检")
    print("=" * 60)

    made = ensure_data_yaml()
    if made:
        print(f" [准备] {DATA_YAML} 不存在（CI 环境），已用 {DATA_BLANK} 临时顶替")
    try:
        run_checks()
    finally:
        cleanup_data_yaml()

    print("\n" + "=" * 60)
    if FAIL:
        print(f"[FAIL] 自检失败 {len(FAIL)} 项：")
        for n in FAIL:
            print(f"   - {n}")
        return 1
    print("[OK] 全部通过")
    return 0


def run_checks():
    print("\n【1】存量 white_mask（v1.0.11 期间标定的用户配置）→ 自动纠正")
    check("white_mask 被纠正为 grayscale", run_load("white_mask"), "grayscale")

    print("\n【2】显式 grayscale（正确值）→ 不动")
    check("grayscale 保持不变", run_load("grayscale"), "grayscale")

    print("\n【3】显式 histogram_eq（用户显式选择）→ 不动")
    check("histogram_eq 保持不变", run_load("histogram_eq"), "histogram_eq")

    # ── 【4】防重犯：出厂默认值本身必须是 grayscale ────────────────────────
    #   为什么单独加这条：上面三条走的都是"显式指定 mode"，
    #   而纠正块会把 white_mask 一律改成 grayscale ⇒ 就算有人把
    #   config_default.yaml 的默认值改回 white_mask，上面三条**照样全绿**
    #   —— 而那正是 v1.0.11 犯的错。所以必须直接盯住出厂默认值本身。
    print("\n【4】出厂默认值必须是 grayscale（防 v1.0.11 式回归重犯）")
    default_mode = load_yaml("config/config_default.yaml")["nametag"]["mode"]
    check("config_default.yaml 的 nametag.mode", default_mode, "grayscale")

    # ── 【5】防漏改：标定工具写入的 mode 也必须与默认一致 ─────────────────
    #   v1.0.11 是"默认值 + 标定工具写入值"两处一起改坏的；
    #   只把默认值改回来、漏改工具，用户重新标定一次就又被写坏。
    print("\n【5】标定工具写入的 mode 必须与默认一致（防只改一处）")
    import re
    with open("tools/calibrate_nametag.py", "r", encoding="utf-8") as f:
        cal_src = f.read()
    m = re.search(r'nt\["mode"\]\s*=\s*["\'](\w+)["\']', cal_src)
    cal_mode = m.group(1) if m else None
    check("calibrate_nametag.py 写入的 mode", cal_mode, "grayscale")


if __name__ == "__main__":
    sys.exit(main())
