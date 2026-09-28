# -*- coding: utf-8 -*-
"""生成「增量补丁包」—— 只含变化文件，供老用户覆盖升级，不用重下 100MB 完整包。

【为什么需要它】
  完整免安装包约 106MB，但其中 **257MB 是 Python 运行时 + 第三方库**
  （打包后 _internal/），**自己的代码只有约 6MB**（内嵌在 exe 的 PKG 里）。
  实测一次 issue 修复（2026-09-27，issue #2）只动了 3 个文件、依赖零变化 ——
  让用户为这点改动重下 106MB 是浪费（用户 2026-09-27 提出）。

【补丁包含什么】
  以 git 的两个版本标签为单位，把**变化过的文件**打进 zip，并附带：
    · patch.json   —— 记录 from/to 版本、需要删除的文件、校验和
    · 应用补丁.bat —— 老用户双击即可完成覆盖（自动备份被覆盖的文件）

【前提（必须成立，脚本会自检）】
  1. 两个版本之间 **依赖没变**（requirements.txt 未改动）——
     否则 _internal/ 里的依赖对不上，补丁不能替代完整包；
  2. 变化文件都在 **打包产物里真实存在**的位置（源码 .py 会被打进 exe，
     所以补丁要含**重新打包的 exe**，而不是 .py 源文件）。

  ⚠️ 第 2 条是本项目的关键约束：业务代码**内嵌在 exe 里**（PyInstaller onedir），
     不是散落的 .py 文件。所以「补丁」= 新 exe +（若有）变化的数据文件，
     而不是几个 .py。这仍然远小于完整包（exe 约 6MB vs 106MB）。

用法：
    # 1. 先按常规流程打好新版完整包（dist/冒险岛自动练级）
    python -m tools.make_patch --from v1.0.2 --to v1.0.3 ^
        --dist "dist/冒险岛自动练级" --out "patch-v1.0.2-to-v1.0.3.zip"

自检：本脚本会拒绝在「依赖有变化」时生成补丁（那种情况必须走完整包）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile

# 补丁包里**始终要替换**的文件（相对打包产物根目录）。
# exe 内嵌全部业务代码，所以它是每次都要换的那个。
ALWAYS_REPLACE = ["冒险岛自动练级.exe"]

# 补丁包里**永不包含**的东西：用户自己的数据，覆盖会毁掉他的配置和路线。
#
# ⚠️ monster_lib/ **不在此列**（2026-09-29）：它是随包分发的**资源**，
#    不是用户数据 —— 里面是官方素材转好的现成怪物模板，新版加了怪就得跟着更新。
#    用户的**自有**模板在 monster/ 下（已列在这里），两者是不同目录，不会互相影响。
NEVER_INCLUDE = ("log/", "minimaps/", "monster/", "nametag/",
                 "config/config_custom.yaml", "config/config_data.yaml")

# 随包分发的**资源目录**：源码里变了就整体带进补丁（不是用户数据，可以覆盖）。
# 与 NEVER_INCLUDE 互斥 —— 别把同一个目录两边都写。
SHIPPED_RESOURCE_DIRS = ("monster_lib/",)

UTF8_FLAG = 0x0800


def _git(*args, cwd=None):
    """跑一条 git 命令并返回 stdout（失败抛异常，不静默）。"""
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{r.stderr.decode('utf-8', 'replace')}")
    return r.stdout.decode("utf-8", "replace")


def changed_files(from_tag, to_tag, cwd):
    """两个标签间变化的文件清单（相对仓库根，POSIX 分隔）。"""
    out = _git("diff", "--name-only", f"{from_tag}..{to_tag}", cwd=cwd)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def deps_changed(from_tag, to_tag, cwd):
    """依赖是否变过 —— 变了就不能发补丁。"""
    files = changed_files(from_tag, to_tag, cwd)
    keys = ("requirements.txt", "Pipfile", "pyproject.toml", "setup.py")
    return [f for f in files if f in keys]


def _all_release_tags(cwd):
    """列出仓库里全部 v* 版本标签（按版本号排序，旧的在前）。"""
    out = _git("tag", "--list", "v*", cwd=cwd)
    tags = [t.strip() for t in out.splitlines() if t.strip()]

    def key(t):
        parts = t.lstrip("v").split(".")
        nums = []
        for p in parts:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        return nums

    return sorted(tags, key=key)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_patch(from_tag, to_tag, dist_dir, out_zip, cwd, universal=False):
    """生成补丁。

    universal=True 时（用户 2026-09-27 提出「1.0.1 / 1.0.0 的用户装不了」）：
      不再按 from_tag..to_tag 求差集，而是把**所有比 to_tag 旧的版本**都算一遍，
      并入它们的差集 —— 得到一份「从任意旧版本都能升级」的通用补丁。

      为什么这样是对的：业务代码（603 个模块）**全部内嵌在 exe 里**，
      而补丁每次都整包替换 exe ⇒ 不论用户从 v1.0 / v1.0.1 / v1.0.2 出发，
      装完都是同一个新 exe、同一份新配置。所以**一份补丁就够**，
      不必为每个起始版本各发一份（那会徒增用户困惑）。

      前提：所有起始版本到 to_tag 之间**依赖都没变**（否则 _internal/ 对不上）。
      脚本会逐个检查，任何一个不满足就拒绝生成。
    """
    if universal:
        all_tags = _all_release_tags(cwd)
        older = [t for t in all_tags if t != to_tag]
        print(f"[通用补丁] 将对以下旧版本各求一次差集：{older}")
        if not older:
            raise SystemExit("[错误] 找不到任何旧版本标签，无法生成通用补丁。")
        # 依赖必须对所有旧版本都没变
        for t in older:
            bad = deps_changed(t, to_tag, cwd)
            if bad:
                raise SystemExit(
                    f"[拒绝] {t} → {to_tag} 之间依赖有变化：{bad}\n"
                    f"       通用补丁要求所有旧版本都能安全升级，请走完整包。")
        files = []
        seen = set()
        for t in older:
            for f in changed_files(t, to_tag, cwd):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        print(f"[通用补丁] 合并后共 {len(files)} 个文件有变化")

    # ---- 门槛 1：依赖不许变 ----
    # 通用模式已在上面逐个版本查过；单版本模式在这里查当前这一对。
    if not universal:
        bad = deps_changed(from_tag, to_tag, cwd)
        if bad:
            raise SystemExit(
                f"[拒绝] {from_tag} → {to_tag} 之间依赖文件有变化：{bad}\n"
                f"       依赖变了就不能发增量补丁（用户 _internal/ 里的库对不上），"
                f"请走完整包。")
        files = changed_files(from_tag, to_tag, cwd)
        print(f"[补丁] {from_tag} → {to_tag} 共 {len(files)} 个文件有变化：")
        for f in files:
            print(f"        {f}")

    # ---- 门槛 2：打包产物目录必须存在且是新的 ----
    if not os.path.isdir(dist_dir):
        raise SystemExit(f"[错误] 打包产物目录不存在：{dist_dir}\n"
                         f"       先按常规流程打好新版完整包再生成补丁。")

    # ---- 组装补丁内容 ----
    # 业务代码在 exe 里，所以 exe 必换；配置类数据文件若变化也带上。
    entries = []          # (产物内相对路径, 来源文件绝对路径)
    for rel in ALWAYS_REPLACE:
        p = os.path.join(dist_dir, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            raise SystemExit(f"[错误] 补丁必需文件在产物里找不到：{p}")
        entries.append((rel, p))

    # 源码里变化的、且属于「出厂数据」的文件（配置文件等）一并带上
    for f in files:
        if f.startswith("config/") and f.endswith(".yaml"):
            base = os.path.basename(f)
            if base in ("config_custom.yaml", "config_data.yaml"):
                continue                      # 用户自己的配置，绝不覆盖
            p = os.path.join(dist_dir, "config", base)
            if os.path.isfile(p):
                entries.append((f"config/{base}", p))

    # ── 随包分发的**资源目录**（2026-09-29 加）────────────────────────
    # 背景：v1.1.0 起内置了 monster_lib/（现成怪物素材库）。老用户打增量补丁时
    # 必须**也拿到这个目录**，否则补丁装完 exe 是新的、素材库却不存在，
    # 点【从素材库选怪】会报"素材库是空的"——功能静默缺失。
    #
    # ⚠️ 与 NEVER_INCLUDE 的区别：那些是**用户自己产出的数据**（他的模板/路线/配置），
    #    覆盖会毁掉他的心血；这里是**随包发的资源**，新版本就该覆盖成新的。
    #    两者目录不同（monster/ vs monster_lib/），不会互相影响。
    for res_dir in SHIPPED_RESOURCE_DIRS:
        prefix = res_dir.rstrip("/") + "/"
        touched = [f for f in files if f.startswith(prefix)]
        if not touched:
            continue
        src_root = os.path.join(dist_dir, res_dir.rstrip("/"))
        if not os.path.isdir(src_root):
            raise SystemExit(
                f"[错误] 源码里 {res_dir} 有变化，但打包产物里找不到它：{src_root}\n"
                f"       先重新打包（打包_用户版.bat）再生成补丁。")
        for root, _dirs, fnames in os.walk(src_root):
            for fn in fnames:
                abs_p = os.path.join(root, fn)
                rel = os.path.relpath(abs_p, dist_dir).replace(os.sep, "/")
                entries.append((rel, abs_p))

    # 去重
    seen, uniq = set(), []
    for rel, p in entries:
        if rel not in seen:
            seen.add(rel)
            uniq.append((rel, p))
    entries = uniq

    # ---- 门禁：SHIPPED_RESOURCE_DIRS 与 NEVER_INCLUDE 不得重叠 ----
    # 两者语义相反（前者必须覆盖、后者绝不覆盖），同时命中会悄悄毁掉用户数据。
    for rd in SHIPPED_RESOURCE_DIRS:
        for never in NEVER_INCLUDE:
            if rd.rstrip("/") == never.rstrip("/"):
                raise SystemExit(
                    f"[错误] {rd} 同时出现在 SHIPPED_RESOURCE_DIRS 和 "
                    f"NEVER_INCLUDE 里 —— 语义冲突，请二选一。")

    # ---- 写 zip ----
    if os.path.exists(out_zip):
        os.remove(out_zip)
    total = 0
    manifest = {"from": from_tag, "to": to_tag, "files": [],
                "always_replace": ALWAYS_REPLACE,
                "universal": bool(universal),
                "note": "解压后双击『应用补丁.bat』；它会先备份被覆盖的文件。"}
    if universal:
        manifest["applies_to"] = [t for t in _all_release_tags(cwd) if t != to_tag]
        manifest["note"] = ("解压到任意位置后双击『应用补丁.bat』；"
                            "适用于上面 applies_to 列出的所有旧版本。")

    # ⚠️★ 关键设计（2026-09-27 实测发现并修正）：
    #   补丁内的文件**必须放在子目录 `_补丁文件/` 下**，绝不能与用户程序同级！
    #   原因：补丁里有「冒险岛自动练级.exe」，若解压到程序目录，**解压动作本身
    #   就把用户的 exe 覆盖了** —— 于是：
    #     ① 备份功能形同虚设（bat 备份到的是刚被覆盖的新文件，不是原文件）；
    #     ② 解压中途失败/中断会留下半新半旧的损坏安装，且无法回退。
    #   实测复现：解压后 exe 的 sha256 立刻变成新版，用户还没双击 bat。
    #   ⇒ 放进子目录，让「覆盖」这件事**只由 bat 在备份之后执行**。
    PAYLOAD = "_补丁文件"

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for rel, src in entries:
            for never in NEVER_INCLUDE:
                if rel.startswith(never):
                    break
            else:
                arcname = f"{PAYLOAD}/{rel}"
                zf.write(src, arcname)
                size = os.path.getsize(src)
                total += size
                manifest["files"].append({
                    "path": rel, "size": size, "sha256": sha256_of(src)})
                print(f"[补丁] 收录 {rel}  ({size/1024/1024:.1f} MB)")

        # patch.json
        blob = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        zi = zipfile.ZipInfo("patch.json")
        zi.external_attr = (0o644 & 0xFFFF) << 16
        zi.flag_bits |= UTF8_FLAG
        zf.writestr(zi, blob)

        # 应用脚本
        bat = build_apply_bat(from_tag, to_tag, manifest["files"],
                              applies_to=manifest.get("applies_to"),
                              payload_dir=PAYLOAD)
        zi = zipfile.ZipInfo("应用补丁.bat")
        zi.external_attr = (0o644 & 0xFFFF) << 16
        zi.flag_bits |= UTF8_FLAG
        zf.writestr(zi, bat.encode("utf-8-sig"))     # cmd 认 BOM

    size_mb = os.path.getsize(out_zip) / 1024 / 1024
    print(f"\n[补丁] 输出：{out_zip}")
    print(f"[补丁] 大小：{size_mb:.1f} MB（完整包约 106 MB）")

    # ---- 自检：中文条目必须带 UTF-8 标志位，否则解压乱码 ----
    bad_enc = []
    with zipfile.ZipFile(out_zip) as zf:
        for info in zf.infolist():
            if any(ord(c) > 127 for c in info.filename) \
                    and not (info.flag_bits & UTF8_FLAG):
                bad_enc.append(info.filename)
    if bad_enc:
        raise SystemExit(f"[自检失败] 这些中文条目没有 UTF-8 标志位：{bad_enc}")
    print("[自检] 中文条目均带 UTF-8 标志位 —— 解压不会乱码 [OK]")
    return 0


def build_apply_bat(from_tag, to_tag, files, applies_to=None, payload_dir="_补丁文件"):
    """生成给老用户双击的覆盖脚本。

    必须做对的三件事：
      ① 先备份被覆盖的文件（用户升级失败还能退回）；
      ② 从**子目录 payload_dir** 取新文件，而不是与本脚本同级 ——
         同级会导致"解压即覆盖"，备份失去意义（见 make_patch 里的详细说明）；
      ③ 提示用户「先关掉程序」—— 程序在跑时 exe 被占用，复制会失败。

    applies_to 非空时是「通用补丁」，脚本会列出它适用的所有旧版本。
    """
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        "cd /d \"%~dp0\"",
        "",
        "echo ============================================================",
        f"echo  冒险岛自动练级  增量补丁  -^>  {to_tag}",
        "echo ============================================================",
        "echo.",
        "echo 这个补丁只替换变化的文件，不用重下完整包。",
        "echo.",
    ]
    if applies_to:
        lines += [
            "echo 适用于这些旧版本（装完都变成最新版）：",
            "echo   " + "、".join(applies_to),
            "echo.",
        ]
    lines += [
        "echo [!] 安装前请先【关闭 冒险岛自动练级 程序】，否则文件被占用会失败。",
        "echo.",
        "pause",
        "",
        "rem 让用户确认游戏程序所在目录（默认就是本补丁所在目录）",
        "set TARGET=%CD%",
        "if exist \"%CD%\\冒险岛自动练级.exe\" goto :found",
        "echo.",
        "echo 没在当前目录找到 冒险岛自动练级.exe。",
        "echo 请把本补丁解压到【程序所在的那个文件夹】再运行；",
        "echo 或者现在手动输入程序目录的完整路径：",
        "set /p TARGET=路径: ",
        ":found",
        "if not exist \"%TARGET%\\冒险岛自动练级.exe\" (",
        "  echo [错误] 该目录下没有 冒险岛自动练级.exe，已中止。",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        "rem 新文件放在子目录里，先确认它在（不在就是没解压完整）",
        f"if not exist \"%~dp0{payload_dir}\" (",
        f"  echo [错误] 找不到 {payload_dir} 文件夹 —— 补丁没有解压完整。",
        "  echo 请重新解压本补丁（解压时要保留文件夹结构）。",
        "  pause",
        "  exit /b 1",
        ")",
        "",
        "rem ---- 备份 ----",
        "set BAK=%TARGET%\\_backup_补丁前",
        "if not exist \"%BAK%\" mkdir \"%BAK%\"",
        "echo.",
        "echo [1/2] 备份将被覆盖的文件到 %BAK% ...",
    ]
    for f in files:
        p = f["path"].replace("/", "\\")
        lines.append(f'if exist "%TARGET%\\{p}" copy /Y "%TARGET%\\{p}" "%BAK%\\" >nul')
    lines += [
        "",
        "echo [2/2] 正在覆盖...",
    ]
    for f in files:
        p = f["path"].replace("/", "\\")
        # ⚠️ 目标子目录可能不存在 —— 旧版打包产物里出厂包**只有 exe 和
        #    _internal/**（config/ 等目录是程序首次运行时才自建的）。
        #    实测（2026-09-27）：从一个没有 config/ 的旧目录打补丁时，
        #    直接 copy 会因为"目录名无效"失败。所以先确保父目录存在。
        parent = os.path.dirname(p)
        if parent:
            lines.append(f'if not exist "%TARGET%\\{parent}" mkdir "%TARGET%\\{parent}"')
        lines.append(f'copy /Y "%~dp0{payload_dir}\\{p}" "%TARGET%\\{p}" >nul')
    lines += [
        "",
        "echo.",
        "echo ============================================================",
        "echo  补丁安装完成！",
        "echo ============================================================",
        "echo.",
        "echo 现在可以打开 冒险岛自动练级.exe 了。",
        f"echo 版本：{to_tag}",
        "echo.",
        "echo 如果出现问题，你的原文件已备份在：",
        "echo   %BAK%",
        "echo （把里面的文件复制回程序目录即可还原）",
        "echo.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成增量补丁包（老用户覆盖升级用）")
    ap.add_argument("--from", dest="from_tag", default=None,
                    help="起始版本标签，如 v1.0.2。用 --universal 时可不填。")
    ap.add_argument("--to", dest="to_tag", required=True, help="目标版本标签，如 v1.0.3")
    ap.add_argument("--dist", dest="dist_dir", default="dist/冒险岛自动练级",
                    help="新版打包产物目录（默认 dist/冒险岛自动练级）")
    ap.add_argument("--out", dest="out_zip", default=None, help="输出 zip 名")
    ap.add_argument("--repo", dest="cwd", default=".", help="仓库根目录")
    ap.add_argument("--universal", action="store_true",
                    help="生成通用补丁：一份适用于**所有**比 --to 旧的版本"
                         "（业务代码全内嵌在 exe 里，整包替换 exe 即可，"
                         "不必按起始版本各发一份）。默认关闭。")
    args = ap.parse_args(argv)

    if args.universal:
        out = args.out_zip or f"patch-to-{args.to_tag}-universal.zip"
        return make_patch(args.from_tag or "v0.0", args.to_tag,
                          args.dist_dir, out, args.cwd, universal=True)

    if not args.from_tag:
        raise SystemExit("[错误] 不指定 --universal 时必须给 --from（起始版本标签）。")
    out = args.out_zip or f"patch-{args.from_tag}-to-{args.to_tag}.zip"
    return make_patch(args.from_tag, args.to_tag, args.dist_dir, out, args.cwd)


if __name__ == "__main__":
    sys.exit(main())
