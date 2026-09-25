# -*- coding: utf-8 -*-
"""
GUI 启动入口（供根目录的 bat 双击调用）

为什么要单独有这么一个文件：
  src/main.py 内部用的是包内绝对导入（`from src.ui.ui import MainWindow`）。
  如果直接 `python src\\main.py`，Python 会把 sys.path[0] 设成 src\\ 目录，
  于是找不到顶层包 src，直接 ModuleNotFoundError: No module named 'src'。
  本文件放在项目根目录，先把项目根塞进 sys.path 再调用真正的 main()，
  这样双击 bat（工作目录=项目根）就能正常起界面。

  另外把启动期的异常落盘到 log/，因为 bat 用的是无窗口的 pythonw，
  出错时用户看不到任何提示，只能靠这个日志。

用法：
  python run_gui.py          （源码模式，bat 里就是这么调的）
"""
import datetime
import os
import sys
import traceback

# 项目根 = 本文件所在目录
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _write_crash_log(text):
    """把异常写到 log/ 下，返回文件路径（失败则返回 None）。"""
    try:
        log_dir = os.path.join(ROOT, "log")
        os.makedirs(log_dir, exist_ok=True)
        name = "启动失败_%s.log" % datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(log_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None


def _preflight():
    """启动前的轻量自检：缺什么就明确说清楚，别让用户对着黑窗猜。"""
    problems = []

    try:
        import PySide6  # noqa: F401
    except Exception as e:
        problems.append("缺少界面库 PySide6（%s）" % e)

    # 模板目录：现在是空的，跑挂机还需要重做模板，但界面本身应该能开
    empty_dirs = []
    for d in ("nametag", "misc", "monster", "minimaps"):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            empty_dirs.append(d + "（目录不存在）")
        elif not [x for x in os.listdir(p) if not x.startswith(".")]:
            empty_dirs.append(d + "（空）")
    if empty_dirs:
        print("[提示] 以下模板目录还没内容，界面能开但挂机跑不了：")
        for e in empty_dirs:
            print("       - " + e)
        print("       原因：模板需从国服真机截图重做（上游台服模板不通用）。")

    return problems


def main():
    print("=" * 60)
    print(" 冒险岛怀旧服国服自动练级 - 界面启动")
    print("=" * 60)
    print("项目目录:", ROOT)
    print("Python  :", sys.version.split()[0], "|", sys.executable)
    print()

    problems = _preflight()
    if problems:
        print("[错误] 启动自检未通过：")
        for p in problems:
            print("       - " + p)
        print()
        print("请先按提示补齐环境，再重新启动。")
        return 2

    print("正在打开界面 ...")
    print("（关掉界面窗口，或双击『停止界面.bat』即可退出）")
    print()

    try:
        from src.main import main as bot_main
        bot_main()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        path = _write_crash_log(tb)
        print()
        print("=" * 60)
        print("[错误] 界面启动失败，详细信息如下：")
        print("=" * 60)
        print(tb)
        if path:
            print("以上信息已保存到:", path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
