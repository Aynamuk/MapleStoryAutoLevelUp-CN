# Standard Import
import sys

# Pyside
from PySide6.QtWidgets import QApplication

# Load Import
from src.ui.ui import MainWindow
from src.ui.AutoBotController import AutoBotController

def _run_tool(argv):
    """打包后用**自身 exe** 跑 tools/ 下的子工具。

    为什么需要它：界面上的 F2 标定名字 / F3 截怪 / F4 录路线 / 工具箱的
    诊断包与模板查重，都是**另起一个进程**跑 `python -m tools.xxx`。
    打包成 exe 之后 `sys.executable` 就是这个 exe，而 exe **不认 `-m`**，
    照原样启动会立刻失败 —— 症状是「点按钮没反应 / 一闪而过」。

    所以改成自调用：`MSBot.exe --tool tools.routeRecorder --new_map 废都南方工地`
    由本函数在**同一个 exe 里** import 并运行那个模块（源码运行仍走 `-m`，不受影响）。
    """
    import runpy
    if not argv:
        sys.stderr.write("用法: --tool <模块名> [参数...]\n")
        return 2
    module = argv[0]
    # 被跑的模块里通常是 `if __name__ == '__main__':`，所以 argv 要摆成
    # 「模块名 + 它自己的参数」的样子，runpy 才会把它当脚本入口。
    sys.argv = [module] + list(argv[1:])
    runpy.run_module(module, run_name="__main__")
    return 0


def main():
    '''
    Main Function
    Run: python -m ui.main
    '''
    # 打包后跑子工具的入口（见 _run_tool）：有 --tool 就不起界面
    if len(sys.argv) > 1 and sys.argv[1] == "--tool":
        sys.exit(_run_tool(sys.argv[2:]))

    app = QApplication(sys.argv)

    autoBotController = AutoBotController()
    ui = MainWindow(autoBotController)

    autoBotController.update_signal(ui)

    ui.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
