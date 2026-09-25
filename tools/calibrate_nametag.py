# -*- coding: utf-8 -*-
"""
角色名标签标定工具 —— 一趟搞定「名字模板 + 位置偏移 offset」

为什么需要它
-----------
国服怀旧服**唯一**能判断"我角色在哪"的办法，就是在画面上找到你角色的**名字标签**
（上游那套组队血条定位对国服不可用，已整条删除）。
而引擎的换算写死在 src/engine/MapleStoryAutoLevelUp.py 第 433-435 行：

    loc_player.x = 名字模板左上角x + 模板宽 // 2      ← offset[0] 当前**不参与运算**
    loc_player.y = 名字模板左上角y - offset[1]      ← 全靠 offset[1]

也就是说：光找到名字还不够，还要靠 offset[1] 才能推出"你人站的位置"。
这个数**只能量、不能猜**：config 里的默认值 [-50, 30] 是上游遗留、从未被标定验证过
（上游把 nametag 标为 deprecated，实际用的组队血条），而且上游注释写的方向与代码相反。
手填极易填反 —— 填反了引擎会以为你人在名字的另一侧，走位和打怪判定全跟着错。

所以做成工具：**框住名字 → 点一下角色中心 → 自动反解 offset 并写进配置**。

国服怀旧服的排布（用户实测，2026-09-10）
---------------------------------------
        ┌──────────┐
        │  人物    │   ← 角色本体
        ├──────────┤
        │  名字    │   ← 要框的就是这一行（做成模板）
        ├──────────┤
        │  称号    │   ← 更醒目，但**不要框进来**
        └──────────┘

注意：**名字在人物【下方】**，所以角色中心在名字框的**上方** → offset[1] 是**正数**。

称号为什么不框进来（2026-09-10 跨地图实测）：
  ① 名字底板和称号徽章**都是半透明**叠加在地图上的 —— 平均亮度随背后地形变：
     名字白字 240.5 → 238.2（几乎不变）；底板 129.5 → 89.3；称号条 169.4 → 136.7。
     把称号框进来 = 把"随背景变"的像素放进模板 → 换地图必失配。
  ② 称号徽章图形**人人相同**（别的玩家挂的是同一个蓝金边徽章）→ 等于引入干扰物。
  所以模板只取「名字的白字」，其余一律刷成纯绿当掩码。
  在真实搜索区（79 万个候选位）上实测：真位置 0.0003、全图仅 1 处 <0.001、余量 9.09 倍。

坐标系说明
---------
本工具和 截取模板工具 一样，直接在**原始帧**（含标题栏，1368x800）上框选。
这对模板无害（模板是局部像素块，整体偏移不改变其内容）；
对 offset 也无害（offset 是两个 y 的**差值**，同一坐标系里差值相抵）。
但**绝对坐标**（如 ui_coords）必须以客户区坐标系为准，不能拿这里的读数当坐标用。

用法（在项目根目录）
------------------
  python -m tools.calibrate_nametag                        # 现抓一帧游戏画面
  python -m tools.calibrate_nametag --image debug/xxx.png   # 用已有截图（推荐，可反复用）
或直接用界面上的「F2 标定名字」（同一个工具，只是帮你把参数填好）。

窗口里的操作
-----------
  第 1 步  左键拖拽 = 框住**名字那一行**（别把下面的称号框进来）
  第 2 步  左键单击 = 点一下**角色中心**（人物身体中间，在名字框上方）
          回车 / s = 确认，出模板 + 写配置
          r = 重新框选     q / ESC = 退出不保存

无人值守自检（跳过鼠标，直接给坐标，用来验证工具本身）
----------------------------------------------------
  python -m tools.calibrate_nametag --image xx.png --name 测试 \
      --auto-box 100,200,120,22 --auto-click 160,150
  --no-save    只算不写：不出模板、不改配置
"""
import argparse
import os
import sys

import cv2
import numpy as np
import yaml

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.common import (load_yaml, imread_unicode,  # noqa: E402
                              imwrite_unicode)
from tools.template_capture import (grab_game_frame, save_raw_snapshot,  # noqa: E402
                                    draw_cn_text, CANVAS_MAX)

GREEN = (0, 255, 0)
WIN = "nametag_calibrate"
WIN_PREVIEW = "nametag_template_preview"
# 抠图废模板判据（与 skill 里一致）：前景像素太少 = 没抠到东西，别拿去用
MIN_FG_PIXELS = 120


# ------------------------------------------------------------------ 纯计算（可单测）

def compute_offset(box, click):
    """
    由「名字模板矩形 + 你点的角色中心」反解 nametag.offset。

    box   = (x0, y0, x1, y1)  名字模板在（原始）帧里的外接矩形
    click = (cx, cy)          你点的角色中心

    推导（照 engine 第 433-435 行反推，保证引擎算出来的点正好落回你点的位置）：
        offset[1] = 模板顶边y - 角色中心y      → 引擎 y = 顶边y - offset[1] = 角色中心y √
        offset[0] = 角色中心x - (模板左边x + 模板宽//2)   （当前被引擎忽略，但记成自洽值）

    【注意】 不假设方向：结果完全由你标的两个点决定。
       名字在人物下方时，角色中心在框上方 → offset[1] 为正。
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    cx, cy = [int(v) for v in click]
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)

    off_x = int(round(cx - (x0 + w // 2)))
    off_y = int(round(y0 - cy))

    notes = []
    if cy >= y0:
        notes.append("【注意】 你点的位置落在名字框顶边的**下方**。")
        notes.append("   按「名字在人物下面」的排布，角色应该在框的**上方**才对 ——")
        notes.append("   请确认：是不是点到名字/称号上了？还是框把称号也框进来了？")
    if abs(off_y) < 5:
        notes.append(f"【注意】 垂直偏移只有 {off_y} 像素，偏小，可能是框或点得不准。")
    if h > 0 and abs(off_y) > 12 * h:
        notes.append(f"【注意】 垂直偏移 {off_y} 相对名字高度 {h} 太大（>12 倍），"
                     f"像是点错了地方。")
    if abs(off_x) > w:
        notes.append(f"【注意】 水平偏差 {off_x} 超过名字宽度 {w}，"
                     f"说明名字明显没水平居中在角色上（引擎当前忽略该值，仅记录）。")

    notes.append(f"名字模板 {w}x{h}（帧内 x{x0}..{x1}, y{y0}..{y1}）")
    notes.append(f"你点的角色中心 ({cx}, {cy})")
    notes.append(f"→ 垂直 offset[1] = 模板顶边y {y0} - 角色中心y {cy} = {off_y}")
    notes.append(f"   引擎换算：角色y = 名字顶边y - ({off_y}) = {y0 - off_y}"
                 f"  ← 应等于你点的 y={cy}")
    notes.append(f"   水平 offset[0] = {off_x}（引擎当前不读它，仅记录备查）")
    return (off_x, off_y), notes


def write_config(name, offset, path=None):
    """把参数标签写进用户覆盖层配置（默认 config/config_custom.yaml，不动 config_default）。
    path 可指定其它配置文件（2026-09-12：配合主界面「新建配置」的命名配置）。"""
    if path is None:
        path = os.path.join(REPO_ROOT, "config", "config_custom.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    except Exception as e:
        print(f"[错误] 读 {path} 失败：{e}")
        return None

    nt = data.get("nametag")
    if not isinstance(nt, dict):
        nt = {}
        data["nametag"] = nt
    nt["name"] = name
    nt["offset"] = [int(offset[0]), int(offset[1])]

    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
    except Exception as e:
        print(f"[错误] 写 {path} 失败：{e}")
        return None

    print(f"[完成] 已写入 {os.path.relpath(path, REPO_ROOT)}："
          f"nametag.name = {name}   nametag.offset = {list(offset)}")
    return path


def save_nametag_template(name, tpl):
    d = os.path.join(REPO_ROOT, "nametag")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{name}.png")
    if not imwrite_unicode(p, tpl):
        print(f"[错误] 模板写盘失败：{p}")
        return None
    print(f"[完成] 名字模板已保存：{os.path.relpath(p, REPO_ROOT)}"
          f"  ({tpl.shape[1]}x{tpl.shape[0]})")
    return p


COMBO_MODE = False  # True = 「名字+称号」原样合成模板；False = 只留名字白字（**默认**）
                    # 由 main() 按 --combo 设置。取值理由见 extract_template 的说明。


def extract_template(img, drag_rect):
    """
    从框里做出名字标签模板。

    **默认（COMBO_MODE=False）：只留「名字的白字」，其余全部涂绿当掩码。**

      为什么（2026-09-10 跨地图实测；结论当天翻转过一次，以下为最终结论）：
        名字底板和称号徽章都是**半透明**叠加在地图上的，像素随背后地形变：
            名字白字   平均亮度 240.5 → 238.2   （几乎不变）← 不透明
            名字底板   平均亮度 129.5 →  89.3   （大变）
            称号徽章条 平均亮度 169.4 → 136.7   （大变）
        → 只有**文字字形**是背景无关的；拿它当模板才可能跨背景站得住。

        在真实搜索区（1366x680 = 79 万个候选位）上比三种模板 —— 模板取自旧地图，
        真位置在新地图 (657,447)：
            A 原样框（含半透明底板） 真位置 0.1578，全图最低落在 (761,192) 【失败】，余量 0.68 倍
            B 白字（名字+称号）      真位置 0.0020，全图最低=真位置 【OK】，余量 1.44 倍
            C 白字（仅名字）         真位置 0.0003，全图最低=真位置 【OK】，余量 **9.09 倍**
                                    全图仅 **1** 处 <0.001
        → 选 C。跨地图直接落对。

      【注意】 曾一度改成「名字+称号原样框」（同一天），理由是"只认名字时全图 21.6% 的位置分数 <0.05"。
        **那个判断是错的**：0.05 本身太松（真位置 0.0003，差三个数量级），用 0.001 当线只剩 1 处。
        已改回 C。

      【注意】 称号不放进模板的第二个理由：**称号徽章图形人人相同**
        （别的玩家挂的是同一个蓝金边徽章），放进来等于引入"和你长得一样的干扰物"。
        名字字形才是你独有的特征 —— 这也是"画面中心区域可能有别人"这一点的解法。

    COMBO_MODE=True —— 老行为：名字+称号一起框、原样裁剪、不抠图（留作退路，用 --combo）。

    【注意】 两种模式都**保持你拖的框几何不变**：引擎拿「模板左上角」配合 nametag.offset 定位，
      裁掉边距会让 offset 失效。
    返回 (模板图, 模板在帧内的矩形, 错误信息)
    """
    x, y, w, h = [int(v) for v in drag_rect]
    sub = img[y:y + h, x:x + w]
    if sub.size == 0:
        return None, None, "框超出了画面范围，请重框。"
    if COMBO_MODE:
        # 原样框：会把半透明底板也带进来 → 跨背景必失配（实测 0.1578 且落点错）。仅临时排查用。
        return sub.copy(), (x, y, x + w, y + h), None

    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, fg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    n_fg = int((fg > 0).sum())

    # 名字是白字 → 应只占框里的一小条。若 Otsu 判出「亮部」占了大半，
    # 说明框没框准（把背景/称号一起框进来了），或名字跟背景亮度太接近。
    ratio = float((fg > 0).mean())
    if ratio > 0.60:
        return None, None, (f"框里 {ratio*100:.0f}% 的像素都偏亮，不像「白字只占一小条」。"
                            f"请重新框准：只框名字那一行，别带称号和背景。")
    if n_fg < MIN_FG_PIXELS:
        return None, None, (f"框里只取到 {n_fg} 个亮像素（少于 {MIN_FG_PIXELS}），"
                            f"基本等于没取到字。请重框。")
    tpl = sub.copy()
    tpl[fg == 0] = GREEN
    return tpl, (x, y, x + w, y + h), None


def report_template_quality(tpl):
    """
    体检：这个模板拿去做匹配靠不靠谱。

    最要命的坏法不是"没抠到"（那种一眼可见），而是**把地图背景也留下来了**：
    名字背后的地图是会变的，模板里只要混进地图像素，人一走位就失配，
    而且症状很隐蔽（能定位，但时好时坏）。
    所以关键判据是**背景排除了多少**（绿底占比）。

    注意：紧贴文字的框里，前景本来就该占大头 —— 「前景占比高」不是问题，
    这里不再告警（原先那条是误报，2026-09-10 改掉）。
    """
    h, w = tpl.shape[:2]
    total = max(1, h * w)
    green = int(np.all(tpl == GREEN, axis=2).sum())
    fg = total - green
    if green == 0:
        # 只在 --combo（名字+称号原样框）时出现
        print(f"[体检] 模板 {w}x{h}（名字+称号原样框）：整块 {total} px 参与比对")
        print("       【注意】 原样框把**半透明底板**也带进来了 —— 跨地图实测真位置分 0.1578、")
        print("          全图最低还落在别的亮区上，等于不可用。仅临时排查时用。")
        print("       【注意】 换称号也会失配。默认请用「只框名字」模式。")
        return
    print(f"[体检] 模板 {w}x{h}：名字白字 {fg} px（{100.0*fg/total:.0f}%），"
          f"绿底已排除 {green} px（{100.0*green/total:.0f}%）")
    if fg < MIN_FG_PIXELS:
        print("       【注意】 白字太少，基本等于没抠到东西，别用这个模板。")
    if green < 0.10 * total:
        print("       【注意】 绿底不足 10% —— 模板里很可能混进了随背景变的像素（底板/地图）。")
        print("          跨地图实测：这类像素平均亮度会从 129.5 掉到 89.3，人一走位就失配。")
        print("          建议把框重新框准一点再来。")
    print("       【说明】 深色描边会被一并刷成绿底 —— 这是实测后**故意**的：")
    print("          把暗像素也留进模板会连岩石一起带进来，反而更差（实测 0.32 vs 0.04）。")
    print("       【OK】 参照：名字白字掩码模板跨地图实测真位置 0.0003、全图仅 1 处 <0.001。")


# ------------------------------------------------------------------ 交互

class CalibSession:
    def __init__(self, img):
        self.img = img
        self.h, self.w = img.shape[:2]
        self.disp_scale = 1.0
        self.step = "box"            # box | click | done
        self.rect = None             # 你拖的框（帧坐标）
        self.drag_start = None
        self.drag_now = None
        self.click = None            # 你点的角色中心（帧坐标）
        self.tpl = None              # 抠好的绿底模板
        self.tpl_box = None          # 模板紧密外接矩形
        self.err = None
        self.on_box_confirmed = None   # 拖框松手后由 main 注入的自动确认回调

    def to_img(self, x, y):
        s = self.disp_scale or 1.0
        return (min(max(int(x / s), 0), self.w - 1),
                min(max(int(y / s), 0), self.h - 1))

    def on_mouse(self, event, x, y, flags, param):
        ix, iy = self.to_img(x, y)
        if self.step == "box":
            if event == cv2.EVENT_LBUTTONDOWN:
                self.drag_start = (ix, iy)
                self.drag_now = (ix, iy)
            elif event == cv2.EVENT_MOUSEMOVE and self.drag_start is not None:
                self.drag_now = (ix, iy)
            elif event == cv2.EVENT_LBUTTONUP and self.drag_start is not None:
                self.drag_now = (ix, iy)
                x0, y0 = self.drag_start
                x1, y1 = self.drag_now
                x0, x1 = sorted((max(0, x0), min(self.w - 1, x1)))
                y0, y1 = sorted((max(0, y0), min(self.h - 1, y1)))
                if x1 - x0 > 4 and y1 - y0 > 4:
                    self.rect = (x0, y0, x1 - x0, y1 - y0)
                    self.err = None
                    # 拖框松手自动确认（原需按回车，2026-09-12 截怪风格优化）
                    if self.on_box_confirmed:
                        self.on_box_confirmed()
            return

        if self.step == "click":
            if event in (cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONUP):
                self.click = (ix, iy)
            return

    def run_extract(self):
        """从你拖的框里抠出名字模板。"""
        if self.rect is None:
            return
        self.tpl, self.tpl_box, self.err = extract_template(self.img, self.rect)
        if self.tpl is None:
            print(f"[警告] {self.err}")
        else:
            print(f"[信息] 已抠出名字模板，紧密外接矩形 = {self.tpl_box}")
            report_template_quality(self.tpl)

    def hints(self):
        if self.step == "box":
            if COMBO_MODE:
                return ["【第 1 步】左键拖拽 = 把**名字**和下面那个**称号**一起框住（--combo 模式）",
                        "          【注意】 原样框会把半透明底板带进来 → 跨地图会失配，仅临时排查时用",
                        "          松开鼠标自动进入下一步",
                        "          r 清除重框    q 退出"]
            return ["【第 1 步】左键拖拽 = 框住**名字那一行**（默认模式）",
                    "          【注意】 只框名字，下面的称号**别框进来** ——",
                    "             称号徽章图形人人相同，且它的底色是半透明的（换地图就变）",
                    "          松开鼠标自动进入下一步",
                    "          r 清除重框    q 退出"]
        if self.step == "click":
            lines = ["【第 2 步】左键点一下**角色中心**（人物身体中间）",
                     "          【注意】 名字在人物下方 → 你应该点在**黄框的上方**"]
            if self.click:
                lines.append(f"          当前点：{self.click}（点错了就再点一次）")
            if self.tpl_box:
                lines.append(f"          模板紧密框：{self.tpl_box}")
            lines.append("          回车/s 确认并保存    r 重新框选    q 退出")
            return lines
        return ["回车/ESC 关闭"]

    def render(self):
        base = self.img.copy()
        if self.rect is not None:
            x, y, w, h = self.rect
            cv2.rectangle(base, (x, y), (x + w, y + h), (0, 255, 255), 2)
        if self.drag_start and self.drag_now:
            cv2.rectangle(base, self.drag_start, self.drag_now, (0, 255, 255), 2)
        if self.tpl_box is not None:
            bx0, by0, bx1, by1 = self.tpl_box
            cv2.rectangle(base, (bx0, by0), (bx1, by1), (0, 255, 0), 2)
        if self.click is not None:
            cv2.drawMarker(base, self.click, (0, 0, 255), cv2.MARKER_CROSS, 26, 2)
            cv2.circle(base, self.click, 4, (0, 0, 255), -1)

        ch, cw = base.shape[:2]
        sc = min(0.95 * CANVAS_MAX[0] / cw, 0.85 * CANVAS_MAX[1] / ch, 1.0)
        self.disp_scale = sc
        disp = cv2.resize(base, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        return draw_cn_text(disp, self.hints())

    def render_preview(self):
        if self.tpl is None:
            return None
        z = max(1, min(6, 300 // max(1, self.tpl.shape[1])))
        p = cv2.resize(self.tpl, None, fx=z, fy=z, interpolation=cv2.INTER_NEAREST)
        return draw_cn_text(p, [f"名字模板预览 x{z}  {self.tpl.shape[1]}x{self.tpl.shape[0]}"])


# ------------------------------------------------------------------ 无人值守自检

def run_noninteractive(img, name, box_rect, click, no_save):
    print("[自检] 跳过鼠标操作，直接用给定坐标跑一遍")
    sess = CalibSession(img)
    sess.rect = tuple(box_rect)
    sess.click = tuple(click)
    sess.run_extract()
    if sess.tpl is None:
        print("[失败] 抠图没出结果，请检查 --auto-box。")
        return 1
    off, notes = compute_offset(sess.tpl_box, click)
    print()
    for n in notes:
        print("  " + n)
    if no_save:
        print("[自检] --no-save：不写模板、不改配置。")
        return 0
    save_nametag_template(name, sess.tpl)
    write_config(name, off, a.config)
    return 0


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="角色名标签标定（名字模板 + offset）")
    ap.add_argument("--name", default=None,
                    help="模板名，通常就是你的角色名（不给就在控制台里问）")
    ap.add_argument("--image", default=None, help="用已有截图，不抓游戏窗口")
    ap.add_argument("--window", default=None, help="游戏窗口标题关键字（默认读配置）")
    ap.add_argument("--auto-box", default=None,
                    help="无人值守自检：名字框 x,y,w,h（跳过鼠标框选）")
    ap.add_argument("--auto-click", default=None,
                    help="无人值守自检：角色中心 x,y（跳过鼠标点选）")
    ap.add_argument("--no-save", action="store_true",
                    help="只算不写：不出模板、不改配置")
    ap.add_argument("--no-template", action="store_true",
                    help="不抠模板，直接把你拖的框当模板区域（模板已单独做好、只补标定时用）")
    ap.add_argument("--combo", action="store_true",
                    help="改用「名字+称号」原样框（会把半透明底板带进来，跨地图会失配；仅排复用）")
    ap.add_argument("--name-only", action="store_true",
                    help="[已废弃] 只框名字、按亮度取白字 —— 现在这是默认行为，可省略")
    ap.add_argument("--config", default="config/config_custom.yaml",
                    help="标定结果写进哪个用户配置（默认 config/config_custom.yaml；"
                         "主界面「新建配置」的命名配置由按钮启动时传入）")
    a = ap.parse_args()

    global COMBO_MODE
    COMBO_MODE = bool(a.combo)
    if a.name_only and a.combo:
        print("[错误] --name-only 与 --combo 互斥，只能选一个。")
        return 2
    if a.name_only:
        print("[信息] --name-only 已废弃：只框名字现在就是默认行为，该参数可省略。")

    # ---- 模板名
    if not a.name:
        if a.auto_box and a.auto_click:
            # 自检模式不该停下来等输入（之前会卡死在 input() 上，2026-09-10 修）
            if not a.no_save:
                print("[错误] 自检模式要写盘时必须给出 --name（模板名，通常就是角色名）。")
                print("       例：--auto-box 100,200,120,22 --auto-click 160,150 --name 角色名")
                return 2
            a.name = "自检"
            print("[信息] 自检模式（--no-save）未给 --name，临时用「自检」，不会落盘。")
        else:
            print()
            print("  提示：名称请填你角色的完整名字，要和游戏里显示的**一模一样**")
            print("        （这个名字既是模板文件名，也会写进 config 的 nametag.name）")
            while not a.name:
                a.name = input("  请输入名称后回车: ").strip()
                if not a.name:
                    print("  名称不能为空，请重新输入。")
    print(f"[信息] 模板名 = {a.name}")

    # ---- 取图
    if a.image:
        if a.image.lower() == "latest":
            # 自动取 debug 里最近抓的那张整帧（配合 bat 的「用最近那张图」选项）
            import glob
            cands = glob.glob(os.path.join(REPO_ROOT, "debug", "capture_raw_*.png"))
            if not cands:
                print("[错误] debug 里没有 capture_raw_*.png。")
                print("       请先跑 python -m tools.grab_frame 抓一张，或用 --image 指定图片。")
                return 2
            p = max(cands, key=os.path.getmtime)
            print(f"[信息] --image latest → 自动选用最近的一张："
                  f"{os.path.relpath(p, REPO_ROOT)}")
        else:
            p = a.image if os.path.isabs(a.image) else os.path.join(REPO_ROOT, a.image)
        img = imread_unicode(p, cv2.IMREAD_COLOR)
        if img is None:
            print(f"[错误] 读不到图片：{p}")
            return 2
        print(f"[信息] 已载入 {os.path.relpath(p, REPO_ROOT)}  ({img.shape[1]}x{img.shape[0]})")
    else:
        token = a.window
        if token is None:
            try:
                token = load_yaml(os.path.join(REPO_ROOT, "config",
                                               "config_default.yaml"))["game_window"]["title"]
            except Exception as e:
                print(f"[错误] 读配置失败（{e}），请用 --window 指定窗口标题关键字")
                return 2
        img = grab_game_frame(token)
        if img is None:
            return 1
        save_raw_snapshot(img)

    # ---- DPI：让画布尺寸按真实屏幕算，鼠标坐标才准
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    CANVAS_MAX[0] = ctypes.windll.user32.GetSystemMetrics(0)
    CANVAS_MAX[1] = ctypes.windll.user32.GetSystemMetrics(1)

    # ---- 无人值守
    if a.auto_box and a.auto_click:
        box_rect = tuple(int(v) for v in a.auto_box.split(","))
        click = tuple(int(v) for v in a.auto_click.split(","))
        return run_noninteractive(img, a.name, box_rect, click, a.no_save)

    # ---- 交互
    sess = CalibSession(img)
    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WIN, sess.on_mouse)

    while True:
        cv2.imshow(WIN, sess.render())
        if sess.tpl is not None:
            prev = sess.render_preview()
            if prev is not None:
                cv2.imshow(WIN_PREVIEW, prev)
        k = cv2.waitKey(20) & 0xFF

        if k in (27, ord("q")):
            print("[信息] 退出，未保存。")
            break

        def confirm_box():
            """第 1 步框选的确认（原需按回车；现拖框松手即自动调用，2026-09-12 截怪风格优化）"""
            if sess.rect is None:
                print("[提示] 先用鼠标拖一个框把名字框住。")
                return False
            if a.no_template:
                x, y, w, h = sess.rect
                sess.tpl_box = (x, y, x + w, y + h)
                print(f"[信息] --no-template：直接采用拖框 {sess.tpl_box} 作为模板框")
            else:
                sess.run_extract()
                if sess.tpl is None:
                    print("[提示] 这个框里没抠出名字模板，按 R 重新框（框紧一点，只框名字）。")
                    return False
            sess.step = "click"
            return True

        sess.on_box_confirmed = confirm_box

        if k == 13 and sess.step == "box":     # 兼容保留（正常流程松手已自动确认）
            confirm_box()
            continue

        if k in (13, ord("s")) and sess.step == "click":
            if sess.click is None:
                print("[提示] 先点一下角色中心（人物身体中间）。")
                continue
            box = sess.tpl_box if sess.tpl_box else (
                sess.rect[0], sess.rect[1], sess.rect[0] + sess.rect[2], sess.rect[1] + sess.rect[3])
            off, notes = compute_offset(box, sess.click)
            print()
            print("  ---------------- 标定结果 ----------------")
            for n in notes:
                print("  " + n)
            print("  -----------------------------------------")
            if a.no_save:
                print("[信息] --no-save：不写模板、不改配置。")
            else:
                if sess.tpl is not None:
                    save_nametag_template(a.name, sess.tpl)
                write_config(a.name, off, a.config)
            break

        if k == ord("r"):
            sess.step = "box"
            sess.rect = None
            sess.drag_start = sess.drag_now = None
            sess.click = None
            sess.tpl = None
            sess.tpl_box = None
            sess.err = None
            try:
                cv2.destroyWindow(WIN_PREVIEW)
            except Exception:
                pass

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        # 出错必须停住让用户看到（窗口跟进程关闭 = 报错不可见 = 「点了没反应」）
        import traceback
        traceback.print_exc()
        code = 1
    try:
        input("\n[结束] 按回车关闭窗口 ")
    except Exception:
        pass
    sys.exit(code)
