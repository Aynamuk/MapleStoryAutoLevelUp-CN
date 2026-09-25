# -*- coding: utf-8 -*-
"""
模板截取工具 —— 从国服游戏画面里做出项目要的模板（绿底 PNG）

为什么需要它：
  上游那套模板是从 maplestory.io 拉的 GMS 贴图（自带透明通道，直接把透明像素
  刷成绿就行）。国服是 Unity/IL2CPP 重制客户端，贴图、字体、UI 全变了，
  上游模板实测不可用，只能用国服真机画面重做。
  而项目对模板有个硬约定：**背景必须是纯绿 RGB(0,255,0)**，
  代码用 get_mask(img, (0,255,0)) 把绿像素当"透明"排除掉。
  于是这个工具干的事就是：抓图 → 框选 → 抠出目标 → 背景刷绿 → 存到对应目录。

用法（在项目根目录执行）：
  # 从游戏窗口现抓一帧来截（游戏要先开好）
  python -m tools.template_capture --kind monster --name green_snail

  # 用已有的截图（推荐：先用系统截图工具 Win+Shift+S 截好，再反复用这张图做）
  python -m tools.template_capture --image debug/capture_xxx.png --kind monster --name green_snail

  # 角色名标签（nametag 只能真机截图，没有别的来源）
  python -m tools.template_capture --kind nametag --name 我的角色名

  # 无人值守自检（跳过鼠标操作，直接给矩形），用来验证工具本身没问题
  python -m tools.template_capture --image xx.png --kind misc --name t --auto-rect 10,10,100,100

窗口里的操作：
  鼠标左键拖拽 = 框住目标（松手自动抠图，秒出）
  回车 或 s = 保存当前模板
             ⚠️ 保存前会先跟 monster/<怪名>/ 里**已有**的模板比一遍：
                如果旧模板在「你刚框的这只怪」上已经能认出来，就**不保存**，
                只提示「本地已有可用模板」。
                理由：引擎对每张 PNG 都要跑两次匹配（原图 + 左右镜像），
                重复模板每帧都白花两次全 ROI 的 matchTemplate。
  r = 撤回重框（抠得不满意就拖一个更紧的框，比涂抹直观）
  c = 切换 / 新建怪物名（连着截多种怪）
  q 或 ESC = 退出
"""
import argparse
import datetime
import os
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 仓库根：本文件在 <root>/tools/ 下
try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.utils.common import load_yaml, imread_unicode, imwrite_unicode  # noqa: E402
from src.utils.mob_template_qa import (  # noqa: E402
    DUP_THRES_DEFAULT, load_masks, old_hits_frame, template_paths,
)

GREEN = (0, 255, 0)          # 项目约定的模板背景色（BGR）
WIN = "template_capture"

# 中文字体（微软雅黑优先）
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]


def get_font(size=18):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_cn_text(img, lines, origin=(8, 8), size=18, color=(255, 255, 255),
                 bg=(0, 0, 0), pad=6):
    """在 OpenCV 图像上画中文（cv2.putText 不支持中文，所以走 PIL）。"""
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = get_font(size)
    lh = size + 6
    w_max = 0
    for i, t in enumerate(lines):
        try:
            bb = draw.textbbox((0, 0), t, font=font)
        except Exception:
            bb = (0, 0, len(t) * size // 2, size)
        w_max = max(w_max, bb[2] - bb[0])
    x, y = origin
    draw.rectangle([x - pad, y - pad, x + w_max + pad, y + len(lines) * lh + pad], fill=bg)
    for i, t in enumerate(lines):
        draw.text((x, y + i * lh), t, font=font, fill=tuple(color))
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


# ----------------------------------------------------------------- 取图

def grab_game_frame(window_title_token, timeout=8.0):
    """用 windows_capture 抓游戏窗口的一帧。返回 BGR 图或 None。"""
    from windows_capture import WindowsCapture
    from src.utils.common import find_game_window_hwnd

    # 【注意】 按 **hwnd** 定位窗口，不能用标题 —— 抓帧库对 window_name 做的是**子串匹配**：
    #    游戏标题「冒险岛怀旧服」正好是资源管理器标题「冒险岛怀旧服国服自动练级 - 文件资源管理器」
    #    的**前缀** → 会随机抓到资源管理器，且全程不报错（2026-09-10 实测：连抓 5 帧错 2 帧）。
    #    find_game_window_hwnd 内部已排除系统外壳窗口，并取唯一候选。
    hwnd = find_game_window_hwnd(window_title_token)
    if not hwnd:
        print(f"[错误] 没找到标题里含「{window_title_token}」的游戏窗口。")
        print("       请先把游戏开起来（窗口模式、不要最小化）；或用 --image 指定已有截图。")
        return None
    print(f"[信息] 正在抓取游戏窗口：hwnd={hwnd}（关键词「{window_title_token}」）")

    holder = {}

    cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, draw_border=False)

    @cap.event
    def on_frame_arrived(frame, capture_control):
        holder["frame"] = frame.frame_buffer.copy()
        capture_control.stop()

    @cap.event
    def on_closed():
        pass

    control = cap.start_free_threaded()
    t0 = time.time()
    while "frame" not in holder and time.time() - t0 < timeout:
        time.sleep(0.05)
    try:
        control.stop()
    except Exception:
        pass

    if "frame" not in holder:
        print(f"[错误] {timeout:.0f} 秒内没抓到画面（窗口最小化了？或被反作弊挡住了？）")
        return None

    img = holder["frame"]
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    print(f"[信息] 抓到画面 {img.shape[1]}x{img.shape[0]}")

    # ★ 尺寸体检：确认抓到的**确实是游戏**，而不是标题里碰巧含关键词的别的窗口。
    #   实测踩过（2026-09-10）：抓到过 2546x1433 的资源管理器搜索窗口 ——
    #   它的标题里含「冒险岛怀旧服」，又比游戏窗口大，于是按「取面积最大」把真游戏窗口挤掉了。
    #   最坑的是**全程不报错**：后面所有模板/坐标分析都建立在一张错的图上。
    try:
        cfg = load_yaml(os.path.join(REPO_ROOT, "config", "config_default.yaml"))
        h_c, w_c = cfg["game_window"]["size"]
        tb = cfg["game_window"]["title_bar_height"]
        exp_w, exp_h = w_c + 2, h_c + tb + 1
        if abs(img.shape[1] - exp_w) > 2 or abs(img.shape[0] - exp_h) > 2:
            print(f"[错误] 抓到 {img.shape[1]}x{img.shape[0]}，与游戏窗口应有的 {exp_w}x{exp_h} "
                  f"（客户区 {w_c}x{h_c} + 标题栏 {tb} + 1px 边框）差太多。")
            print(f"       这大概率不是游戏窗口，而是标题里碰巧含「{window_title_token}」的"
                  f"其它窗口（例如资源管理器/搜索结果窗口）。**已拒绝保存这一帧。**")
            print("       排查：1) 游戏要处于窗口模式，且没有被最小化")
            print("             2) 若改过游戏分辨率，先跑一次 python -m tools.measure_window --write-config 更新配置")
            return None
    except Exception as e:
        print(f"[警告] 尺寸体检被跳过（读配置失败：{e}）")

    return img


# ----------------------------------------------------------------- 抠图

def grabcut(img, rect, mask_hint=None, scribble_bg=None, scribble_fg=None, iters=5):
    """GrabCut 分割，返回前景二值图（255=目标）。"""
    h, w = img.shape[:2]
    gc = np.zeros((h, w), np.uint8)
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)

    if mask_hint is None:
        x, y, rw, rh = rect
        # 框稍微往里收一点，避免把矩形边线本身当成前景
        inset = max(1, min(rw, rh) // 40)
        r = (x + inset, y + inset, max(2, rw - 2 * inset), max(2, rh - 2 * inset))
        cv2.grabCut(img, gc, r, bgd, fgd, iters, cv2.GC_INIT_WITH_RECT)
    else:
        gc = mask_hint.copy()
        if scribble_bg is not None:
            gc[scribble_bg > 0] = cv2.GC_BGD
        if scribble_fg is not None:
            gc[scribble_fg > 0] = cv2.GC_FGD
        cv2.grabCut(img, gc, None, bgd, fgd, iters, cv2.GC_INIT_WITH_MASK)

    fg = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    return fg, gc


def clean_mask(fg, keep_largest=True):
    """去噪 + 只留最大连通块（避免抠出零碎小点）。"""
    k = np.ones((3, 3), np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, k, iterations=1)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=2)
    if keep_largest:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
        if n > 2:
            idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            fg = np.where(labels == idx, 255, 0).astype(np.uint8)
    return fg


def to_green_bg(img, fg_mask):
    """把前景以外全部刷成项目约定的纯绿背景，返回绿底图 + 目标的外接矩形。"""
    out = img.copy()
    out[fg_mask == 0] = GREEN
    ys, xs = np.where(fg_mask > 0)
    if len(xs) == 0:
        return out, None
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return out, box


# ----------------------------------------------------------------- 保存

def out_path(kind, name):
    if kind == "nametag":
        return os.path.join(REPO_ROOT, "nametag", f"{name}.png")
    if kind == "misc":
        return os.path.join(REPO_ROOT, "misc", f"{name}.png")
    if kind == "monster":
        d = os.path.join(REPO_ROOT, "monster", name)
        return os.path.join(d, f"{name}_1.png")
    if kind == "minimap":
        d = os.path.join(REPO_ROOT, "minimaps", name)
        return os.path.join(d, "map.png")
    raise ValueError(kind)


from src.utils.registry import list_maps, register_mob


def ask_mob_registration(mob_name, map_hint=None):
    """monster 模板保存成功后自动登记到指定地图（2026-09-12 改版）。
    【为什么不再控制台问询】框选循环内的 input 会阻塞 cv2 窗口消息泵
    → 窗口「未响应/卡死」（2026-09-12 用户实测）。地图由主界面选中后
    经 --map 传入，保存时静默登记，零打断。"""
    if not map_hint:
        print("[登记] 未指定地图（--map），跳过登记。可在 config_data.yaml 手动补。")
        return
    changed = register_mob(map_hint, mob_name)
    print(f"[登记] 「{mob_name}」已加入「{map_hint}」的打怪列表" if changed
          else f"[登记] 「{mob_name}」已在「{map_hint}」的列表里，跳过")


def switch_mob_name(current):
    """c 键：切换/新建怪物名。列出怪物库（monster/ 目录）选编号，或输新名字。
    返回新名字。中文名输入走 input，频率低（一次会话一次），编码风险可控。"""
    mobs = sorted(d for d in os.listdir(os.path.join(REPO_ROOT, "monster"))
                  if os.path.isdir(os.path.join(REPO_ROOT, "monster", d)))
    print()
    print("  切换怪物：")
    for i, m in enumerate(mobs, 1):
        mark = "（当前）" if m == current else ""
        print(f"    {i} = {m} {mark}")
    print("    n = 新怪物（输入新名字）")
    while True:
        k = input("  输入编号或 n 后回车（直接回车 = 不变）: ").strip()
        if not k:
            return current
        if k.lower() == "n":
            new = input("  新怪物的名字（中文名就可以）: ").strip()
            if new:
                return new
            print("  名字不能为空。")
            continue
        if k.isdigit() and 1 <= int(k) <= len(mobs):
            return mobs[int(k) - 1]
        print("  输入无效，请重新输入。")


_current_map = None   # 主界面选中的地图（--map 传入），怪物登记目标


def existing_template_hit(frame, box, name):
    """保存前自查：本地**已有**的模板能不能在「刚框的这只怪」上认出它？

    ★ 为什么这样就够（不用去比"新旧哪个更好"）：
        不问"哪个更好"，只问**"旧模板还够不够用"** ——
          旧的能认出来 → 它已经覆盖了这个姿势 → 新的多余；
          旧的认不出   → 这是旧模板没见过的新姿势 → 新的该留。
        两种情况都不会留下多余模板，而"判优劣"这个难题被整个绕开了。

    ⚠️ **不能拿新模板去测**：新模板就是从这一帧抠下来的，必然满分，
       等于自己考自己。只有旧模板的得分有信息量。

    为什么要查：引擎对每张 PNG 都要跑两次匹配（原图 + 左右镜像），
        多留一张 = 每次怪检测多跑两次全 ROI 的 matchTemplate。

    ⚠️ 任何异常一律当作"没命中" —— 自查出问题绝不能把保存卡住。

    Returns:
        (hit, score, path)；没有旧模板 / 出异常时是 (False, None, None)
    """
    try:
        paths = template_paths(REPO_ROOT, name)
        if not paths:
            return False, None, None
        return old_hits_frame(frame, box, load_masks(paths),
                              thres=DUP_THRES_DEFAULT)
    except Exception as e:                                   # noqa: BLE001
        print(f"[警告] 模板自查被跳过（{e}）—— 按「没有旧模板」处理，照常保存")
        return False, None, None


def save_template(kind, name, img_green):
    """按 kind 落到正确位置。monster 多帧时自动往后编号，不覆盖已有文件。"""
    if kind == "monster":
        d = os.path.join(REPO_ROOT, "monster", name)
        os.makedirs(d, exist_ok=True)
        n = 1
        while os.path.exists(os.path.join(d, f"{name}_{n}.png")):
            n += 1
        path = os.path.join(d, f"{name}_{n}.png")
    else:
        path = out_path(kind, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)

    ok = imwrite_unicode(path, img_green)
    if not ok:
        print(f"[错误] 写文件失败：{path}")
        return None
    print(f"[完成] 已保存：{os.path.relpath(path, REPO_ROOT)}")
    if kind == "monster":
        ask_mob_registration(name, _current_map)
    return path


def save_raw_snapshot(img):
    """原始整帧留一份，方便之后反复取用（不用重复开游戏）。"""
    d = os.path.join(REPO_ROOT, "debug")
    os.makedirs(d, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    p = os.path.join(d, f"capture_raw_{ts}.png")
    imwrite_unicode(p, img)
    print(f"[信息] 原始截图已留档：{os.path.relpath(p, REPO_ROOT)}")
    print(f"       下次可以加 --image \"{os.path.relpath(p, REPO_ROOT)}\" 直接复用这张图")
    return p


# ----------------------------------------------------------------- 交互

class Session:
    """极简交互（2026-09-12 按用户要求重做）：
    拖框 → 松手自动抠图（局部 GrabCut，秒出）→ 回车保存 / R 撤回重框 / q 退出。
    原版的「两态状态机 + 涂抹修边 + 视图切换」整体移除（要求里只要必备功能）；
    抠得不满意就按 R 再拖一个更紧的框，比涂抹直观。"""

    def __init__(self, img):
        self.img = img
        self.h, self.w = img.shape[:2]
        self.disp_scale = 1.0
        self.rect = None          # 当前框 (x, y, w, h)，原图坐标
        self.drag_start = None
        self.drag_now = None
        self.fg = None            # 抠图前景掩码
        self.result = None        # 绿底抠图结果
        self.box = None
        self.saved_hint = None    # 保存成功后要显示的提示

    # ---- 坐标换算（画布即原图等比缩放，坐标一一对应）
    def to_img(self, x, y):
        sc = self.disp_scale or 1.0
        ix = min(max(int(x / sc), 0), self.w - 1)
        iy = min(max(int(y / sc), 0), self.h - 1)
        return ix, iy

    # ---- 鼠标：拖框；松手自动抠图；已有结果时再拖 = 自动重开新框
    def on_mouse(self, event, x, y, flags, param):
        ix, iy = self.to_img(x, y)
        if event == cv2.EVENT_LBUTTONDOWN:
            self.rect = None
            self.fg = self.result = None
            self.box = None
            self.saved_hint = None
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
                self.run_grabcut()          # 松手即抠，无需回车
            self.drag_start = self.drag_now = None

    def reset(self):
        """撤回重框：清掉当前框/结果，回到待框选（同一帧，可继续框下一只）。"""
        self.rect = None
        self.drag_start = self.drag_now = None
        self.fg = self.result = None
        self.box = None
        self.saved_hint = None

    # ---- 抠图（局部跑，秒出；详见 run_grabcut 注释）
    def run_grabcut(self):
        if self.rect is None:
            return
        margin = 60
        H, W = self.img.shape[:2]
        x, y, w, h = self.rect
        x0, y0 = max(0, x - margin), max(0, y - margin)
        x1, y1 = min(W, x + w + margin), min(H, y + h + margin)
        sub = self.img[y0:y1, x0:x1]
        fg_sub, _ = grabcut(sub, (x - x0, y - y0, w, h))
        fg = np.zeros((H, W), np.uint8)
        fg[y0:y1, x0:x1] = fg_sub
        self.fg = clean_mask(fg)
        self.result, self.box = to_green_bg(self.img, self.fg)

    def crop_result(self):
        """把绿底结果按目标外接矩形裁出来（模板要贴紧目标，不留多余留白）。"""
        if self.result is None or self.box is None:
            return None
        x0, y0, x1, y1 = self.box
        return self.result[y0:y1, x0:x1].copy()

    # ---- 画面
    def hints(self):
        tips = []
        if self.saved_hint:
            tips.append(self.saved_hint)
        if self.result is None:
            tips += ["【拖一个框把怪框住】松开鼠标就自动抠图",
                     "回车=保存（同一帧可连续框多只）   R=撤回重框   q=退出"]
        else:
            tips += ["【这就是截到的模板】满意按 回车 保存（同一帧可继续框下一只）",
                     "R=撤回重框   q=退出"]
        return tips

    def render(self):
        if self.result is not None:
            # 结果态：把抠好的模板放大居中显示，直观看到「截到了什么」
            rh, rw = self.result.shape[:2]
            scale = max(1, min(4, 420 // max(rh, 1)))
            big = cv2.resize(self.result, (rw * scale, rh * scale),
                             interpolation=cv2.INTER_NEAREST)
            ch, cw = big.shape[:2]
            canvas = np.full((ch + 70, max(cw, 560), 3), 30, np.uint8)
            canvas[35:35 + ch, (canvas.shape[1] - cw) // 2:(canvas.shape[1] - cw) // 2 + cw] = big
        else:
            base = self.img.copy()
            if self.drag_start and self.drag_now:
                cv2.rectangle(base, self.drag_start, self.drag_now, (0, 255, 255), 2)
            elif self.rect is not None:
                x, y, w, h = self.rect
                cv2.rectangle(base, (x, y), (x + w, y + h), (0, 255, 0), 2)
            canvas = base

        ch, cw = canvas.shape[:2]
        sc = min(0.95 * CANVAS_MAX[0] / cw, 0.85 * CANVAS_MAX[1] / ch, 1.0)
        self.disp_scale = sc
        disp = cv2.resize(canvas, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        return draw_cn_text(disp, self.hints())


CANVAS_MAX = [1920, 1080]


def ask_interactively(kind, name):
    """没给 --kind/--name 时，在控制台里问。

    为什么不放在 bat 里用 set /p 问：cmd 从管道/重定向读取时，多个 set /p
    会互相抢输入（第二个常常读到空值），实测踩过。放在 Python 里问最稳，
    中文输入输出也不受 bat 编码影响。
    """
    # 2026-09-12：只保留怪物一类（nametag 用主界面 F2、minimap 用 F4、misc 无用途，
    # 均已收编）—— 只剩一个选项就不必再问类别，直接问名字（用户反馈）。
    kind = "monster"
    print("  提示：名称请填这种怪的名字（中文名就可以，如：绿蘑菇）。")
    while not name:
        name = input("  请输入怪物的名字后回车: ").strip()
        if not name:
            print("  名称不能为空，请重新输入。")
    return kind, name


def main():
    ap = argparse.ArgumentParser(description="国服模板截取工具（输出绿底 PNG）")
    ap.add_argument("--kind", default="monster",
                    help="模板类别，目前只有 monster（nametag 用主界面 F2、minimap 用 F4）")
    ap.add_argument("--name", default=None,
                    help="模板名（nametag 用角色名；monster 用怪物英文名）（不给就在控制台里问）")
    ap.add_argument("--map", default=None,
                    help="登记目标地图名（主界面选中后传入；怪物模板保存时自动登记到该图）")
    ap.add_argument("--image", default=None, help="用已有截图，不抓游戏窗口")
    ap.add_argument("--window", default=None, help="游戏窗口标题关键字（默认读配置）")
    ap.add_argument("--auto-rect", default=None,
                    help="无人值守自检：直接给 x,y,w,h，跳过鼠标框选")
    a = ap.parse_args()

    global _current_map
    _current_map = a.map

    # 没给参数就在控制台里问（双击 bat 走的就是这条路）
    a.kind, a.name = ask_interactively(a.kind, a.name)
    if a.kind is None:
        sys.exit(0)   # 选了已停用的类别（提示已打印）

    if a.auto_rect:
        x, y, w, h = [int(v) for v in a.auto_rect.split(",")]
        auto_rect = (x, y, w, h)
    else:
        auto_rect = None

    # ---- 取图
    if a.image:
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
                cfg = load_yaml(os.path.join(REPO_ROOT, "config", "config_default.yaml"))
                token = cfg["game_window"]["title"]
            except Exception as e:
                print(f"[错误] 读配置失败（{e}），请用 --window 指定窗口标题关键字")
                return 2
        img = grab_game_frame(token)
        if img is None:
            return 1
        save_raw_snapshot(img)

    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    u = ctypes.windll.user32
    CANVAS_MAX[0] = u.GetSystemMetrics(0)
    CANVAS_MAX[1] = u.GetSystemMetrics(1)

    # ---- 无人值守：直接出图
    if auto_rect is not None:
        fg, _ = grabcut(img, auto_rect)
        fg = clean_mask(fg)
        green, box = to_green_bg(img, fg)
        if box is None:
            print("[错误] 这个矩形里没抠出任何目标")
            return 1
        crop = green[box[1]:box[3], box[0]:box[2]]
        p = save_template(a.kind, a.name, crop)
        print(f"[信息] 目标尺寸 {crop.shape[1]}x{crop.shape[0]}")
        return 0 if p else 1

    # ---- 交互
    sess = Session(img)
    cv2.namedWindow(WIN, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WIN, sess.on_mouse)

    saved_any = False
    while True:
        try:
            cv2.imshow(WIN, sess.render())
        except cv2.error as e:
            print(f"[错误] 显示失败：{e}")
            break
        k = cv2.waitKey(20) & 0xFF

        if k in (27, ord("q")):
            break
        elif k in (13, ord("s")):                 # 回车 / s = 保存
            crop = sess.crop_result()
            if crop is not None:
                # ── 保存前自查（2026-09-15 加）：本地已有可用模板就不再存 ──
                # 引擎对每张 PNG 都要跑两次匹配（原图 + 左右镜像），重复模板纯浪费；
                # 而「旧模板认不认得出这个姿势」正好等价于「新的有没有必要留」。
                if a.kind == "monster" and sess.box is not None:
                    hit, score, which = existing_template_hit(img, sess.box, a.name)
                    if hit:
                        # ⚠️ 顺序不能反：reset() 会把 saved_hint 清成 None
                        sess.reset()
                        sess.saved_hint = (
                            f"本地已有可用模板（{os.path.basename(which)}，"
                            f"相似度 {score:.3f}）—— 本次未保存")
                        print(f"[自查] 本地已有可用模板：{os.path.basename(which)}"
                              f"（相似度 {score:.3f} <= {DUP_THRES_DEFAULT}）"
                              f" → 本次不保存，少一张重复模板")
                        continue
                if save_template(a.kind, a.name, crop):
                    saved_any = True
                    # ⚠️ 顺序不能反：reset() 会把 saved_hint 清成 None。
                    #    原实现是先设 hint 再 reset，等于这条提示从没显示过。
                    sess.reset()                   # 回到待框选，接着截下一帧
                    sess.saved_hint = "已保存！继续拖框截下一帧，或 q 退出"
        elif k in (ord("r"), ord("R")):           # 撤回重框（清掉当前框/结果，重新拖）
            sess.reset()
        elif k in (ord("c"), ord("C")):           # 切换/新建怪物名（连续截多种怪）
            a.name = switch_mob_name(a.name)
            print(f"[信息] 当前怪物 = {a.name}，继续拖框截取")

    cv2.destroyAllWindows()
    if not saved_any:
        print("[信息] 没有保存任何模板。")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main() or 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        # 出错必须停住让用户看到 —— 控制台窗口跟着进程关闭时，
        # 报错一行都看不到，用户只会觉得「点了没反应」（2026-09-12 用户实测教训）
        import traceback
        traceback.print_exc()
        code = 1
    try:
        input("\n[结束] 按回车关闭窗口 ")
    except Exception:
        pass
    sys.exit(code)
