# -*- coding: utf-8 -*-
'''
cfg_names — 配置项的中文显示名

高级设置页的分组标题和字段标签原本直接用配置字段名（英文 key）显示，
这里做一层“key -> 中文名”的映射，只影响显示，不影响任何取值/写回逻辑。

设计原则：
1. 查不到就原样显示英文 key —— 以后新增配置项不会炸，也不会显示成 undefined。
2. 支持 "分组.字段" 的精确覆盖，同名 key 在不同分组可以显示各自的中文含义。
3. 删功能时直接删配置段即可；本表的条目也一并清理，免得留成"查不到对应配置"的死条目。

（2026-09-10 已随功能删除清理：符文、创角掷骰、邮件、Mac、巡逻模式、
  遇人换频道、定时换频道、瞬移赶路、性能分析、其他玩家检测）
'''

# 分组标题
SECTION_CN = {
    "buff_skill": "增益技能",
    "directional_attack": "单体攻击",
    "aoe_skill": "群体技能",
    "nametag": "角色名标签",
    "character": "角色尺寸",
    "monster_detect": "怪物检测",
    "ui_coords": "界面坐标",
    "route": "路线跟随",
    "watchdog": "看门狗（卡死自救）",
    "minimap": "小地图",
    "route_recoder": "路线录制器",
    "game_window": "游戏窗口",
    "system": "系统设置",
}

# 通用字段名（所有分组共用）
KEY_CN = {
    "enable": "启用",
    "mode": "模式",
    "cooldown": "冷却时间（秒）",
    "offset": "坐标偏移",
    "debug": "绘制调试框",
    "keys": "按键列表",
    "action_cooldown": "施放后间隔（秒）",
    "range_x": "水平范围 X",
    "range_y": "垂直范围 Y",
    "turn_frames": "转身保持帧数（出招前按住方向键几帧）",
    "color_code": "颜色编码（RGB）",
    "name": "名字",
    "diff_thres": "匹配阈值",
    "global_diff_thres": "全局重搜阈值",
    "split_width": "标签切分宽度（像素）",
    "width": "宽度",
    "height": "高度",
    "search_box_margin": "搜索框外扩像素",
    "contour_blur": "轮廓模糊核大小",
    "with_enemy_hp_bar": "启用怪物血条检测",
    "hp_bar_color": "怪物血条颜色（BGR）",
    "max_mob_area_trigger": "判定为目标的最小重叠面积",
    "ui_y_start": "界面区域起始 Y 坐标",
    "select_character": "选角色按钮坐标",
    "login_button_thres": "登录按钮匹配阈值",
    "login_button_top_left": "登录按钮搜索区左上角",
    "login_button_bottom_right": "登录按钮搜索区右下角",
    "search_range": "路线搜索半径",
    "color_code_up_down": "上下方向颜色编码（RGB）",
    "timeout": "超时时间（秒）",
    "last_attack_timeout": "最长未攻击时间（秒）",
    "player_color": "小地图玩家点颜色（BGR）",
    "debug_window_upscale": "调试窗口放大倍数",
    "blob_cooldown": "动作标记冷却（秒）",
    "map_padding": "地图边缘留白（像素）",
    "title": "窗口标题",
    "size": "客户区尺寸 [高, 宽]",
    "coord_base_size": "坐标录制尺寸 [高, 宽]",
    "title_bar_height": "标题栏高度（像素）",
    "fps_limit_main": "主循环帧率上限",
    "fps_limit_keyboard_controller": "键盘线程帧率上限",
    "fps_limit_window_capturor": "截图线程帧率上限",
    "fps_limit_route_recorder": "路线录制帧率上限",
    "language": "界面语言",
}

# 同名 key 在不同分组含义不同时，用 "分组.字段" 精确覆盖
SECTION_KEY_CN = {
    "watchdog.range": "移动判定阈值（像素）",
    "nametag.offset": "标签左上角到角色中心的偏移",
    "minimap.offset": "小地图整体偏移",
    "monster_detect.mode": "检测模式",
    "nametag.mode": "比对模式",
    "character.width": "角色宽度（像素）",
    "character.height": "角色高度（像素）",

    # ── route：v0.9「回正线 / 落点锚」专用参数 ────────────────────────────
    #    这批键全是 v0.9 新增的，不补的话高级设置页（route 段被放出来时）
    #    会直接显示英文 key，非程序员看不懂。
    #    v0.9 第二版已**删除「落点锚」概念**，改为「谁近走谁」：
    #    d_home < d_main - home_dist_tol 才进回正，否则主线优先（相等时主线优先）。
    "route.home_dist_tol": "两条线比距离的容差（像素）：明显更靠近回正线才进回正",
    "route.home_min_return_span": "判废③：起点到终点的最小距离（像素），太小视为原地起头",
    "route.home_min_cover_span": "判废④：起始段的最小横向跨度（像素），太小接不住落点区间",
    "route.home_cover_far_tol": "判定「起始段」的容差：离主线最远那批像素的允许误差",
    "route.home_goal_mainline_tol": "判废⑤：终点到主线的最大距离（像素），超了就接不上",
    "route.home_min_leave": "保险丝：回正最小位移（像素，0=关闭）",
    "route.home_min_frames": "保险丝：回正最小持续帧数（0=关闭）",
    "route.home_timeout": "回正超时（秒）",
    "route.home_rearm_margin": "去武装后重新武装：角色离开这条线多远才生效（像素）",
    "route.home_rearm_timeout": "去武装兜底时间（秒）：这么久后无条件重新武装",
    "route.home_reenter_lock": "回正失败后的冷静期（秒，期间不再重试回正、先打怪）",

    # ── route_recoder：v0.9 手绘回正线面板参数 ────────────────────────────
    "route_recoder.home_draw_scale": "手绘画布放大倍数",
    "route_recoder.home_draw_scale_min": "手绘画布最小放大倍数",
    "route_recoder.home_draw_scale_max": "手绘画布最大放大倍数",
    "route_recoder.home_draw_goal_radius": "手绘终点标记半径（像素）",
}


def cn_section(section):
    """分组名 -> 中文标题（查不到返回原名）"""
    return SECTION_CN.get(section, section)


def cn_key(section, key):
    """字段名 -> 中文标签：优先 "分组.字段"，再通用表，都没有则原样返回 key"""
    return SECTION_KEY_CN.get(f"{section}.{key}", KEY_CN.get(key, key))
