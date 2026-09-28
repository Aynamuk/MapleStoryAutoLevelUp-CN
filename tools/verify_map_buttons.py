'''
verify_map_buttons.py
=====================

自检：地图列表选中后，三个地图级按钮（打哪几种怪 / 删除这张地图 / 手绘回正线）
必须**亮起来**（2026-09-29 用户实测报的 bug）。

用户原话：选中地图后「这三个按钮都是灰的」，鼠标点了也没用。

背景（真机上为什么点不亮）：
    `_update_delete_btn()` 的判据是 `has = bool(self.selected_map)`，
    而它**只挂在 itemSelectionChanged 上**。关键在于 Qt 的信号顺序：

      itemSelectionChanged  →  先发出（此刻 selected_map 还是旧值 ""）
      itemClicked           →  后发出（on_map_selected 这时才赋 selected_map）

    ⇒ 第一次点击时，_update_delete_btn 跑在 on_map_selected **之前**，
      读到的 selected_map 仍是 ""，于是 `has=False` → 三个按钮保持禁用。
      而**不会有第二次机会**：再点同一行不产生 selectionChanged
      （选区没变），信号不再发 ⇒ 按钮永远灰着。

    ⚠️ 我第一轮只看了「selected_map 只在 on_map_selected 里赋值」，
    就断言"鼠标点一下就会亮" —— **错了**，漏了信号顺序这一层。
    本自检钉的就是这个顺序 bug。

跑法：
    python -m tools.verify_map_buttons

⚠️ 需要 PySide6（不进 CI 的离线自检列表也没关系，本机跑）。
   不依赖游戏窗口 / 不抓帧 / 不发按键。无显示环境时 Qt 用 offscreen 平台。
'''

import os
import sys

# 无显示环境（CI/headless）也能跑：offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, '.')

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望={want!r} 实际={got!r}")


def check_true(name, cond, detail=""):
    check(name + (f"  ({detail})" if detail else ""), bool(cond), True)


print("=" * 68)
print("自检：地图列表三个按钮的启用逻辑")
print("=" * 68)

try:
    from PySide6.QtWidgets import (QApplication, QListWidget, QListWidgetItem,
                                   QPushButton, QWidget, QVBoxLayout)
    from PySide6.QtCore import Qt
except ImportError as e:                                       # noqa: BLE001
    print(f"跳过：没装 PySide6（{e}）")
    sys.exit(0)

app = QApplication.instance() or QApplication([])


# ─────────────────────────────────────────────────────────────────────────
# 复刻 Ui_MainWindow 里那块地图列表的**真实接线**（不是简化版）
# ─────────────────────────────────────────────────────────────────────────
class _MapPanel(QWidget):
    '''复刻 setup_main_tab 里地图那一段的接线：

        list_widget_maps.itemClicked           -> on_map_selected
        list_widget_maps.itemSelectionChanged  -> _update_delete_btn

    以及 _update_delete_btn 的判据 has = bool(selected_map)。
    '''

    def __init__(self):
        super().__init__()
        self.selected_map = ""
        lay = QVBoxLayout(self)

        self.list_widget_maps = QListWidget()
        self.list_widget_maps.itemClicked.connect(self.on_map_selected)
        lay.addWidget(self.list_widget_maps)

        self.btn_map_mobs = QPushButton("打哪几种怪")
        self.btn_map_mobs.setEnabled(False)
        self.btn_delete_map = QPushButton("删除这张地图")
        self.btn_delete_map.setEnabled(False)
        self.btn_draw_home = QPushButton("手绘回正线")
        self.btn_draw_home.setEnabled(False)
        lay.addWidget(self.btn_map_mobs)
        lay.addWidget(self.btn_delete_map)
        lay.addWidget(self.btn_draw_home)

        # ★ 真机就是这一行接线（ui.py L1473），保持原样
        self.list_widget_maps.itemSelectionChanged.connect(self._update_delete_btn)

    # ── 以下两个方法**逐字复刻** ui.py 的实现 ──────────────────────────
    def on_map_selected(self, item):
        map_name = item.text().split(" (")[0]
        self.selected_map = map_name

    def _update_delete_btn(self):
        '''复刻 ui.py 2026-09-29 修复后的实现。

        ⚠️ 本自检的价值全在"复刻是否忠实"。所以这里**不复制粘贴就完事** ——
        下面 [7] 会直接读 ui.py 的源码，断言它真的长这样（防止改了产品代码
        而没改自检，自检还一路绿灯的假通过）。
        '''
        item = self.list_widget_maps.currentItem()
        if item is not None and item.isSelected():
            self.selected_map = item.text().split(" (")[0]
        else:
            self.selected_map = ""

        has = bool(self.selected_map)
        self.btn_delete_map.setEnabled(has)
        self.btn_map_mobs.setEnabled(has)
        if getattr(self, 'btn_draw_home', None) is not None:
            self.btn_draw_home.setEnabled(has)

    def fill(self, names):
        for n in names:
            it = QListWidgetItem(n)
            it.setData(Qt.UserRole, n)
            self.list_widget_maps.addItem(it)


def _buttons_on(p):
    return (p.btn_map_mobs.isEnabled(),
            p.btn_delete_map.isEnabled(),
            p.btn_draw_home.isEnabled())


# ── 1. 初始状态：没选地图 → 三个按钮都该禁用 ────────────────────────────
print("\n[1] 初始状态（未选地图）")
p = _MapPanel()
p.fill(["废都南方工地"])
check("三个按钮初始禁用", _buttons_on(p), (False, False, False))

# ── 2. **核心回归**：模拟"鼠标点中一行"后按钮必须亮 ─────────────────────
print("\n[2] 点击一行后按钮必须亮（用户报的 bug 就在这）")
# ⚠️ 用 QTest 模拟真实点击，让 Qt 按**真实顺序**发信号 ——
#    直接调 on_map_selected 会绕过信号顺序，测不出这个 bug（第一版就这么错过的）。
from PySide6.QtTest import QTest                       # noqa: E402

rect = p.list_widget_maps.visualItemRect(p.list_widget_maps.item(0))
QTest.mouseClick(p.list_widget_maps.viewport(), Qt.LeftButton,
                 pos=rect.center())
app.processEvents()

check_true("点击后 selected_map 已记录",
           p.selected_map == "废都南方工地", f"got={p.selected_map!r}")
check("点击后三个按钮**全部亮起**", _buttons_on(p), (True, True, True))

# ── 3. 解释 bug 成因：Qt 信号顺序 ───────────────────────────────────────
print("\n[3] bug 成因：itemSelectionChanged 早于 itemClicked")
order = []


class _OrderPanel(_MapPanel):
    def on_map_selected(self, item):
        order.append("clicked")
        super().on_map_selected(item)

    def _update_delete_btn(self):
        order.append("selectionChanged")
        super()._update_delete_btn()


p2 = _OrderPanel()
p2.fill(["废都南方工地"])
order.clear()
rect2 = p2.list_widget_maps.visualItemRect(p2.list_widget_maps.item(0))
QTest.mouseClick(p2.list_widget_maps.viewport(), Qt.LeftButton,
                 pos=rect2.center())
app.processEvents()
check_true("selectionChanged 先到、clicked 后到（正是 bug 根因）",
           order[:2] == ["selectionChanged", "clicked"], f"实际顺序={order}")

# ── 4. 同一次点击里，_update_delete_btn 看到的 selected_map 还是旧值 ────
print("\n[4] 关键证据：selectionChanged 时读到的 selected_map 仍是空")
_seen = []


class _SeenPanel(_MapPanel):
    def _update_delete_btn(self):
        _seen.append(self.selected_map)     # 记录此刻读到的值
        super()._update_delete_btn()


p3 = _SeenPanel()
p3.fill(["废都南方工地"])
_seen.clear()
rect3 = p3.list_widget_maps.visualItemRect(p3.list_widget_maps.item(0))
QTest.mouseClick(p3.list_widget_maps.viewport(), Qt.LeftButton,
                 pos=rect3.center())
app.processEvents()
check_true("第一次触发时读到的是空串（= 判定 has=False → 按钮灰着）",
           _seen and _seen[0] == "", f"读到={_seen}")

# ── 5. 再点同一行不会给第二次机会（选区没变 → 无信号）──────────────────
print("\n[5] 再点同一行：selectionChanged 不再发出（没有第二次机会）")
p4 = _MapPanel()
p4.fill(["废都南方工地"])
rect4 = p4.list_widget_maps.visualItemRect(p4.list_widget_maps.item(0))
QTest.mouseClick(p4.list_widget_maps.viewport(), Qt.LeftButton, pos=rect4.center())
app.processEvents()
_state_after_first = _buttons_on(p4)
# 再点一次同一行
QTest.mouseClick(p4.list_widget_maps.viewport(), Qt.LeftButton, pos=rect4.center())
app.processEvents()
check("连点两次，结果不变（不会「再点一次就好」）",
      _buttons_on(p4), _state_after_first)

# ── 6. 健壮性：键盘切换 / 程序化 setCurrentRow 也要能点亮 ───────────────
print("\n[6] 键盘切换与程序化选中（setCurrentRow）也要能点亮")
p5 = _MapPanel()
p5.fill(["废都南方工地", "第二张图"])
# 程序化选中（= refresh_map_list 里 L848 那种恢复选中的做法）
p5.list_widget_maps.setCurrentRow(0)
app.processEvents()
check_true("setCurrentRow 后按钮也亮（程序化恢复选中场景）",
           _buttons_on(p5) == (True, True, True), f"实际={_buttons_on(p5)}")

# 键盘上下键切换
from PySide6.QtCore import Qt as _Qt                        # noqa: E402
p5.list_widget_maps.setCurrentRow(0)
p5.list_widget_maps.setFocus()
QTest.keyClick(p5.list_widget_maps, _Qt.Key_Down)
app.processEvents()
check_true("键盘↓切换后按钮仍亮", _buttons_on(p5) == (True, True, True),
           f"实际={_buttons_on(p5)}")

# clearSelection 之后必须重新变灰（current 与 selected 是两回事）
p5.list_widget_maps.clearSelection()
app.processEvents()
check("清空选择后按钮重新变灰", _buttons_on(p5), (False, False, False))

# ── 7. 防假通过：自检复刻的判据必须与 ui.py 真实源码一致 ────────────────
print("\n[7] 自检与产品代码一致性（防「改了产品没改自检」的假绿灯）")
import inspect                                              # noqa: E402
import re                                                   # noqa: E402

_src_ui = open("src/ui/ui.py", encoding="utf-8").read()
_m = re.search(r"def _update_delete_btn\(self\):(.*?)\n    def ",
               _src_ui, re.S)
check_true("能在 ui.py 里定位到 _update_delete_btn", _m is not None)
if _m:
    _body = _m.group(1)
    # ⚠️ 判据不能直接在**含文档字符串**的 body 上做子串匹配：本函数的 docstring 里
    #    特意写了「不要写成 self.list_widget_maps.isItemSelected(...)」这句警告，
    #    直接搜会命中警告本身，报假失败（我第一次就踩了）。
    #    ⇒ 先剥掉三引号块与 # 注释，只看**可执行代码**。
    #    ⚠️ 三引号要**两种都剥**：本项目的 docstring 惯用单引号 '''（不是 """），
    #       只剥 """ 会漏掉整个文档串，警告句仍然残留、继续报假失败（第二次踩）。
    def _strip_doc_and_comments(s):
        s = re.sub(r"'''.*?'''", "", s, flags=re.S)
        s = re.sub(r'""".*?"""', "", s, flags=re.S)
        return "\n".join(l.split("#")[0] for l in s.splitlines())

    _code_only = _strip_doc_and_comments(_body)

    check_true("ui.py 的判据改为读 currentItem（修掉了信号顺序 bug）",
               "currentItem()" in _code_only)
    check_true("ui.py 用 item.isSelected()（QListWidget 没有 isItemSelected）",
               "item.isSelected()" in _code_only and
               ".isItemSelected(" not in _code_only)
    check_true("ui.py 在未选中时**清掉** selected_map（防误亮）",
               'self.selected_map = ""' in _code_only)
    check_true("ui.py 里仍以 selected_map 决定三个按钮的启用",
               "has = bool(self.selected_map)" in _code_only)
    # 自检里的复刻片段必须与产品同源（比对关键语句）
    _self_src = inspect.getsource(_MapPanel._update_delete_btn)
    _self_code = _strip_doc_and_comments(_self_src)
    check_true("自检复刻体与 ui.py 用的是同一套关键语句",
               ("currentItem()" in _self_code) == ("currentItem()" in _code_only)
               and ("item.isSelected()" in _self_code) ==
                   ("item.isSelected()" in _code_only)
               and ('self.selected_map = ""' in _self_code) ==
                   ('self.selected_map = ""' in _code_only))

# 接线一致性：itemSelectionChanged 必须挂着 _update_delete_btn
check_true("ui.py 仍把 itemSelectionChanged 接到 _update_delete_btn",
           "itemSelectionChanged.connect(self._update_delete_btn)" in _src_ui)

print("\n" + "=" * 68)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
