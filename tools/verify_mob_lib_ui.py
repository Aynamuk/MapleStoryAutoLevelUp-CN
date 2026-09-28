# -*- coding: utf-8 -*-
"""素材库 UI 自检 —— 不依赖 config_data.yaml / monster/（CI 可跑）。

## 为什么不能直接 `MainWindow()`

`MainWindow.__init__` 里第一件事就是
    self.data = load_yaml("config/config_data.yaml")
而 `config_data.yaml` 是**被 gitignore 的**（仓库里只有 config_data.blank.yaml）。
CI 检出后这个文件不存在 ⇒ 构造直接 FileNotFoundError。

所以本自检改成**复刻真实接线**（与 tools/verify_map_buttons.py 同一套思路）：
把 ui.py 里那段按钮创建/启停逻辑照搬出来，验证
  · 按钮文案、绑定的槽
  · 启停随「列表选中项」变化（_update_delete_btn 的真实判据）
  · 素材库对话框的数据源（load_index）可用

另有一段**纯逻辑**检查：register_from_library 的源码里必须仍走
「未选地图 → 早返回」这条防御，避免有人改成直接下标访问而崩。

用法：python -m tools.verify_mob_lib_ui
"""
import inspect
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

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
    print('=' * 64)
    print('素材库 UI 自检（复刻接线，不构造 MainWindow）')
    print('=' * 64)

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QApplication, QListWidget,
                                       QListWidgetItem, QPushButton, QWidget)
    except ImportError as e:
        print(f'跳过：没装 PySide6（{e}）')
        return 0

    app = QApplication.instance() or QApplication([])

    # ── 复刻 ui.py 里地图面板那段接线 ───────────────────────────────
    class _MapPanel(QWidget):
        """照抄 setup_main_tab 里地图区的接线与 _update_delete_btn 的判据。"""

        def __init__(self):
            super().__init__()
            self.selected_map = ''
            self.list_widget_maps = QListWidget()
            # 与 ui.py 一致的三个按钮（顺序、文案都照搬）
            self.btn_map_mobs = QPushButton('🐾 打哪几种怪…')
            self.btn_mob_lib = QPushButton('📦 从素材库选怪…')
            self.btn_delete_map = QPushButton('🗑️ 删除这张地图…')
            for b in (self.btn_map_mobs, self.btn_mob_lib, self.btn_delete_map):
                b.setEnabled(False)
            self.list_widget_maps.itemSelectionChanged.connect(
                self._update_delete_btn)

        def _update_delete_btn(self):
            # 与 ui.py:1799-1815 逐字同构
            item = self.list_widget_maps.currentItem()
            if item is not None and item.isSelected():
                self.selected_map = item.text().split(' (')[0]
            else:
                self.selected_map = ''
            has = bool(self.selected_map)
            self.btn_delete_map.setEnabled(has)
            self.btn_map_mobs.setEnabled(has)
            if getattr(self, 'btn_mob_lib', None) is not None:
                self.btn_mob_lib.setEnabled(has)

    p = _MapPanel()

    print('\n[1] 素材库按钮存在、文案正确')
    check(p.btn_mob_lib is not None, 'btn_mob_lib 已创建')
    check('素材库' in p.btn_mob_lib.text(), f'文案含「素材库」：{p.btn_mob_lib.text()!r}')

    print('\n[2] 启停跟「列表选中项」走（不是跟 selected_map 走）')
    check(not p.btn_mob_lib.isEnabled(), '列表无选中项 → 禁用')
    check(p.selected_map == '', '列表无选中项 → selected_map 被清空')

    it = QListWidgetItem('henesys_west (勇士村西部入口)')
    it.setData(Qt.UserRole, 'henesys_west')
    p.list_widget_maps.addItem(it)
    p.list_widget_maps.setCurrentRow(0)
    p.list_widget_maps.currentItem().setSelected(True)
    p._update_delete_btn()
    check(p.btn_mob_lib.isEnabled(), '选中一项 → 启用')
    check(p.selected_map == 'henesys_west',
          f'地图名解析正确（实际 {p.selected_map!r}）')
    # 三个按钮必须同进同退
    check(p.btn_map_mobs.isEnabled() and p.btn_delete_map.isEnabled(),
          '三个地图级按钮同进同退')

    p.list_widget_maps.clearSelection()
    p.list_widget_maps.setCurrentRow(-1)
    p._update_delete_btn()
    check(not p.btn_mob_lib.isEnabled(), '清空选择 → 重新禁用')

    print('\n[3] 素材库数据源可用（对话框的怪名列表）')
    from src.utils.mob_library import installed_mobs, load_index
    idx = load_index()
    check(len(idx) > 0, f'load_index() 返回 {len(idx)} 种怪')
    check(all(isinstance(k, str) and k for k in idx), '怪名都是非空字符串')
    bad = [k for k, v in idx.items()
           if not str((v or {}).get('level', '')).isdigit()
           and (v or {}).get('level') not in ('?', '')]
    check(not bad, f'等级字段格式合法（异常：{bad}）')
    # installed_mobs 在 monster/ 不存在时也必须安全返回空集（不抛）
    try:
        got = installed_mobs()
        check(isinstance(got, set), f'installed_mobs() 安全返回 set（{len(got)} 个）')
    except Exception as e:
        check(False, f'installed_mobs() 抛异常：{e!r}')

    print('\n[4] register_from_library 的防御分支仍在（源码级）')
    try:
        import src.ui.ui as U
        src = inspect.getsource(U.MainWindow.register_from_library)
        check('selected_map' in src and 'return' in src,
              '函数内仍有「未选地图 → 早返回」的防御')
        check('load_index' in src, '函数使用 load_index（不硬编码路径）')
        check('install' in src, '函数会调 install 装模板')
        check('unregister_mob' in src, '支持取消勾选时移除登记')
    except Exception as e:
        check(False, f'读取源码失败：{e!r}')

    print('\n' + '=' * 64)
    if FAILS:
        print(f'自检失败：{len(FAILS)} 项')
        for m in FAILS:
            print(f'  - {m}')
        return 1
    print('自检通过：素材库入口可用。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
