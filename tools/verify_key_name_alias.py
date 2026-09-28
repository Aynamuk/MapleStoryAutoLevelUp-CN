# -*- coding: utf-8 -*-
'''
离线自检：界面键名 → 驱动的键名转换 + 启动时校验（issue #6 真凶，2026-09-28）

背景（issue #6 用户的「走位正常但完全不攻击」）
------------------------------------------------
界面上的按键是**捕获式**的（按哪个键填哪个），产出的是 **Qt 的叫法**；
而 Interception 驱动有**自己一套键名表**。两者不总是一致 ——
最关键的就是 **Ctrl 键：界面写 "Control"，驱动只认 "ctrl"**。

后果极隐蔽：
  · 引擎照常算出 attack 指令，日志里能看到 `指令(none none attack)`
  · 但 `key_down("control")` **每一帧**都抛 UnknownKeyError
  · 而失败上报是"每类只报一次"的 warning，很容易淹没
  ⇒ 用户看到「走位完全正常，但一次都不打怪」
    （方向键名 left/right/up/down 驱动都认，只有技能键死了）

本自检钉住两件事
----------------
1. **normalize_key 的映射**：已知的同义词必须转对，**且不能改坏本来就对的键**
   （这条最重要 —— 映射写错会导致按错键，比不转换更糟）。
2. **转换后的键名，驱动真的接受**（直接问驱动的键表，不是猜）。

⚠️ 驱动没装的机器上，第 2 部分会优雅跳过（CI 上装的是 requirements 里的包，
   正常都在；万一不在也不该让整份自检红掉）。

用法：
    python -m tools.verify_key_name_alias
'''
import sys

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


def main():
    from src.input.InterceptionController import normalize_key, _KEY_ALIAS

    print("===== 1. 已知同义词必须转对（issue #6 的真凶） =====")
    check("control -> ctrl", normalize_key("control"), "ctrl")
    check("Control -> ctrl（**界面给的就是这个大小写**）",
          normalize_key("Control"), "ctrl")
    check("CONTROL -> ctrl", normalize_key("CONTROL"), "ctrl")
    check("pgdown -> pgdn（这位用户 buff 里也填了）",
          normalize_key("pgdown"), "pgdn")
    check("ins -> insert", normalize_key("ins"), "insert")

    print()
    print("===== 2. 本来就对的键**一个都不能改坏** =====")
    # 这条是安全底线：映射表写错会让角色按错键，比"不转换"更糟。
    unchanged = [
        "ctrl", "alt", "shift", "space", "esc", "escape", "enter", "return",
        "backspace", "tab", "capslock", "delete", "insert", "home", "end",
        "pageup", "pagedown", "pgup", "pgdn", "up", "down", "left", "right",
        "f1", "f5", "f12", "a", "z", "q", "w", "e", "0", "9", "1",
    ]
    bad = [(k, normalize_key(k)) for k in unchanged if normalize_key(k) != k]
    check("这些键原样返回", bad, [])

    print()
    print("===== 3. 边界值不抛异常 =====")
    check("空字符串 -> 空字符串", normalize_key(""), "")
    check("None -> None", normalize_key(None), None)
    check("带空格 -> 去空格后转换", normalize_key("  control  "), "ctrl")
    check("数字（非字符串）也不崩", normalize_key(1), "1")

    print()
    print("===== 4. 转换后的键名，驱动真的接受（问驱动的键表） =====")
    try:
        from interception._keycodes import get_key_information, UnknownKeyError
        _has_driver = True
    except Exception as e:                                       # noqa: BLE001
        print(f"  [跳过] 驱动包不可用（{e}）—— 本项在没装驱动的机器上跳过")
        _has_driver = False

    if _has_driver:
        for raw in ["control", "Control", "CONTROL", "pgdown", "ins"]:
            k = normalize_key(raw)
            try:
                get_key_information(k)
                ok = True
            except UnknownKeyError:
                ok = False
            check(f"{raw!r} -> {k!r} 驱动接受", ok, True)

        print()
        print("===== 5. 回归对照：**不转换**时驱动确实会拒（证明这层不可省） =====")
        for raw in ["control", "pgdown", "ins"]:
            try:
                get_key_information(raw)
                rejected = False
            except UnknownKeyError:
                rejected = True
            check(f"{raw!r} 原样交给驱动会被拒", rejected, True)

    print()
    print("===== 6. 引擎启动时会做按键校验（不静默失效） =====")
    import inspect
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot as B
    src = inspect.getsource(B)
    _code = "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#"))
    check("存在 _check_key_names", "_check_key_names" in _code, True)
    check("load_config 里会调用它", "self._check_key_names(cfg)" in _code, True)
    check("  ⤷ 且被 try 包住（校验出错不能堵住挂机）",
          "self._check_key_names(cfg)" in _code
          and "校验本身出错（不影响挂机）" in _code, True)
    check("校验发现不认识的键时打 error（不是静默）",
          "[按键校验]" in _code and "驱动不认识" in _code, True)
    check("校验结论也报「都正常」（让用户知道它跑过了）",
          "个按键都正常" in _code, True)

    print()
    if FAIL:
        print(f"失败的用例（{len(FAIL)}）：{', '.join(FAIL)}")
        return 1
    print("全部通过：键名转换 + 启动校验已钉住（issue #6「不打怪」的根因）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
