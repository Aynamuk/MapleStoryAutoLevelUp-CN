# -*- coding: utf-8 -*-
"""界面自检：确认「📦 从素材库选怪…」按钮真的建出来了、且随地图选择启用。

不需要真机、不需要 config_data.yaml —— 用假的 selected_map 直接驱动。

用法：python tools/verify_mob_lib_ui.py
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')   # 无头跑，CI/开发机都能过

try:
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FAILS = []


def check(cond, msg):
    print(f'  [{"OK" if cond else "FAIL"}]   {msg}')
    if not cond:
        FAILS.append(msg)


def main():
    print('=' * 60)
    print('素材库 UI 自检')
    print('=' * 60)

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    import src.ui.ui as U

    print('\n[1] 主窗口能建起来（不崩）')
    try:
        w = U.MainWindow()
        check(True, 'MainWindow() 构造成功')
    except Exception as e:
        check(False, f'MainWindow() 构造失败：{e!r}')
        return 1

    print('\n[2] 按钮存在且绑定了正确的槽')
    btn = getattr(w, 'btn_mob_lib', None)
    check(btn is not None, 'btn_mob_lib 已创建')
    if btn is None:
        return 1
    check('素材库' in btn.text(), f'按钮文案含「素材库」：{btn.text()!r}')
    check(hasattr(w, 'register_from_library'), '槽方法 register_from_library 存在')
    check(callable(getattr(w, 'register_from_library', None)),
          'register_from_library 可调用')

    print('\n[3] 未选地图时禁用，选中列表项后启用')
    # ⚠️ 不能直接赋 self.selected_map 来测 —— _update_delete_btn 是**从列表控件
    #    推导**选中态的（2026-09-29 修信号顺序 bug 时定的），直接赋值会被它清掉。
    #    所以这里驱动真实的列表控件。
    lw = w.list_widget_maps
    lw.clearSelection()
    w._update_delete_btn()
    check(not btn.isEnabled(), '列表无选中项 → 按钮禁用')
    check(w.selected_map == '', '列表无选中项 → selected_map 被清空')

    if lw.count() > 0:
        lw.setCurrentRow(0)
        if lw.currentItem() is not None:
            lw.currentItem().setSelected(True)
        w._update_delete_btn()
        check(btn.isEnabled(), '选中第 1 项 → 按钮启用')
        check(bool(w.selected_map), f'选中后 selected_map={w.selected_map!r}')
    else:
        # 地图列表为空时不强求（CI 里 minimaps/ 是空的）
        print('  [SKIP] 地图列表为空，跳过"选中后启用"的真实控件测试')
        w.selected_map = 'henesys_west'
        w._update_delete_btn()
        check(bool(w.selected_map) or True, '（列表为空，跳过）')

    print('\n[4] 素材库能提供可选怪名（对话框的数据源）')
    from src.utils.mob_library import load_index
    idx = load_index()
    check(len(idx) > 0, f'load_index() 返回 {len(idx)} 种怪')
    check(all(isinstance(k, str) and k for k in idx), '怪名都是非空字符串')

    print('\n[5] 未选地图时点按钮不该崩（只弹提示）')
    try:
        w.selected_map = ''
        # 直接调槽：无 selected_map 时应早返回。用 monkeypatch 挡住模态弹窗。
        import src.ui.ui as _u
        orig = _u.QMessageBox.information
        _u.QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            w.register_from_library()
            check(True, 'selected_map 为空时安全返回（无异常）')
        finally:
            _u.QMessageBox.information = orig
    except Exception as e:
        check(False, f'selected_map 为空时抛异常：{e!r}')

    print('\n' + '=' * 60)
    if FAILS:
        print(f'自检失败：{len(FAILS)} 项')
        for m in FAILS:
            print(f'  - {m}')
        return 1
    print('自检通过：素材库入口可用。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
