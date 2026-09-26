# -*- coding: utf-8 -*-
"""把 --onedir 打包产物压成 zip，**确保中文文件名带 UTF-8 标志位**。

【为什么需要专门的脚本】
Windows 上的压缩工具很容易把中文名写成「UTF-8 字节但不设标志位」，
按 zip 规范，没标志位时解压方会按本地编码（GBK）解 ⇒ 用户看到乱码文件名。
实测（2026-09-26）：
  · tar -a -c -f          → 乱码
  · PowerShell Compress-Archive → 乱码（条目 flag=0x0000，未设 UTF-8 位）
  · Python zipfile        → 正确（文件名含非 ASCII 时自动置 UTF-8 位）

用法：
    python tools/make_release_zip.py <源目录> <输出zip>
例：
    python tools/make_release_zip.py "dist/冒险岛自动练级" "冒险岛自动练级-v1.0.1.zip"

自检：压完后本脚本会**重新打开 zip**、逐条检查含中文的条目有没有 UTF-8 标志位，
      没有就直接报错退出（非 0），不让你把一个会乱码的包发出去。
"""
import os
import sys
import zipfile

# zip 本地文件头的「通用位标记」里，bit 11 = 文件名是 UTF-8
UTF8_FLAG = 0x0800


def has_non_ascii(s):
    return any(ord(c) > 127 for c in s)


def make_zip(src_dir, out_zip):
    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        raise SystemExit(f"[错误] 源目录不存在: {src_dir}")

    base_name = os.path.basename(src_dir)      # 顶层目录名（解压后就是这个文件夹）
    parent = os.path.dirname(src_dir)

    n_files = 0
    if os.path.exists(out_zip):
        os.remove(out_zip)

    # ZIP_DEFLATED 压缩；allowZip64 支持 >4GB（本包不需要，但无妨）
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, dirs, files in os.walk(src_dir):
            # 相对路径（含顶层目录名），统一用正斜杠
            rel_root = os.path.relpath(root, parent).replace(os.sep, "/")
            # 空目录也要留条目（程序靠这些目录找资源）
            if not files and not dirs:
                zf.writestr(zipfile.ZipInfo(rel_root + "/"), b"")
            for fn in files:
                full = os.path.join(root, fn)
                arc = (rel_root + "/" + fn).replace(os.sep, "/")
                zf.write(full, arc)
                n_files += 1

    return n_files, base_name


def verify_utf8_flags(zip_path):
    """重新打开 zip，检查含中文的条目是否都带了 UTF-8 标志位。"""
    bad = []
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            total += 1
            if not has_non_ascii(info.filename):
                continue
            # zipfile 的 ZipInfo.flag_bits 就是读出来的通用位标记
            if not (info.flag_bits & UTF8_FLAG):
                bad.append(info.filename)
    return total, bad


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    src, out = sys.argv[1], sys.argv[2]

    n, base = make_zip(src, out)
    size_mb = os.path.getsize(out) / 1024 / 1024
    print(f"[打包] 顶层目录: {base}/")
    print(f"[打包] 文件数: {n}")
    print(f"[打包] 输出: {out}")
    print(f"[打包] 大小: {size_mb:.1f} MB")

    total, bad = verify_utf8_flags(out)
    print(f"[自检] 条目总数: {total}")
    if bad:
        print(f"[自检][失败] 以下 {len(bad)} 个中文名条目没有 UTF-8 标志位，解压会乱码：")
        for b in bad[:10]:
            print(f"        {b}")
        return 1
    print("[自检] 所有含中文的条目都带 UTF-8 标志位 —— 解压不会乱码 [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
