# -*- coding: utf-8 -*-
"""
convert_simplified — 把 .py 源码里的繁体中文转成简体中文

只处理「字符串」和「注释」两种词法单元，代码部分（变量名、key、引号）一律不动。
用法：
    python -m tools.convert_simplified            # 只预览
    python -m tools.convert_simplified --apply    # 实际写入
"""
import io
import os
import sys
import tokenize

import zhconv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "media", "build", "dist", "tools"}


def convert_file(path, apply_change=False):
    src = io.open(path, "r", encoding="utf-8", newline="").read()
    if zhconv.convert(src, "zh-cn") == src:
        return 0, []

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception as e:
        print(f"  [跳过] tokenize 失败: {path} ({e})")
        return 0, []

    # 从后往前替换，避免偏移量错位
    edits = []
    for tok in toks:
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            new = zhconv.convert(tok.string, "zh-cn")
            if new != tok.string:
                (srow, scol), (erow, ecol) = tok.start, tok.end
                edits.append(((srow, scol), (erow, ecol), tok.string, new))

    if not edits:
        return 0, []

    lines = src.splitlines(keepends=True)
    for (srow, scol), (erow, ecol), old_text, new_text in reversed(edits):
        start = sum(len(l) for l in lines[: srow - 1]) + scol
        end = sum(len(l) for l in lines[: erow - 1]) + ecol
        src = src[:start] + new_text + src[end:]

    if apply_change:
        io.open(path, "w", encoding="utf-8", newline="").write(src)

    return len(edits), [(e[2][:40], e[3][:40]) for e in edits]


def main():
    apply_change = "--apply" in sys.argv
    total_files = 0
    total_edits = 0

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            n, samples = convert_file(full, apply_change)
            if n:
                total_files += 1
                total_edits += n
                print(f"{n:4d} 处  {rel}")
                if not apply_change:
                    for old, new in samples[:3]:
                        print(f"         {old!r} -> {new!r}")

    flag = "已写入" if apply_change else "预览（加 --apply 才会真的改文件）"
    print(f"\n文件数 {total_files}，改动处数 {total_edits} —— {flag}")


if __name__ == "__main__":
    main()
