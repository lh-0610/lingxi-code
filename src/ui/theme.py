"""主题系统：light（白天）+ dark（夜间）。两套配色与设计稿 A5 一致，强调色都是靛蓝。

THEMES 是单一颜色字典，build_stylesheet 把它编译成全局 QSS。
load/save_theme_choice 负责把选中的主题持久化到 chat_memory/theme_config.json。
"""
import json
import os

from ._base import THEME_CONFIG_PATH
from .fonts import UI_CSS


THEMES = {
    "light": {
        # 主表面（design_handoff 靛蓝浅色：纯白页面 + #f7f8fa 侧栏）
        "win_bg":            "#ffffff",
        "sidebar_bg":        "#f7f8fa",
        "sidebar_border":    "#ececf0",
        "header_bg":         "#ffffff",
        "header_border":     "#f0f0f3",
        "chat_bg":           "#ffffff",
        "input_bg":          "#ffffff",
        "input_border":      "#dadde6",
        "input_text":        "#222838",
        "input_sel_bg":      "#eef0fe",
        "input_sel_text":    "#222838",
        # 文本
        "text":              "#222838",
        "text_dim":          "#5a6172",
        "text_subtle":       "#9aa0b0",
        "text_muted":        "#cdd1dc",
        # 历史项
        "history_label":           "#9aa0b0",
        "history_label_spacing":   "0.06em",
        "history_item":            "#5a6172",
        "history_hover_bg":        "#eef0f4",
        "history_hover_text":      "#222838",
        "history_active_bg":       "#eef0fe",
        "history_active_text":     "#4a59e0",
        "history_active_border":   "#5b6cf0",
        "history_active_pad_left": "11px",
        # 滚动条
        "scrollbar_track":   "transparent",
        "scrollbar_handle":  "#d3d7e0",
        "scrollbar_handle_hover": "#b8bcc8",
        "scrollbar_thin":    "#d3d7e0",
        # 删除按钮
        "del_btn":           "#b8bcc8",
        "del_btn_hover":     "#6a7080",
        "del_btn_hover_bg":  "#dfe1e8",
        # New chat 按钮
        "new_chat_bg":           "#ffffff",
        "new_chat_text":         "#2b3142",
        "new_chat_border":       "#e3e4ea",
        "new_chat_hover_bg":     "#ffffff",
        "new_chat_hover_border": "#c8cbf3",
        "new_chat_hover_text":   "#2b3142",
        # Toggle btn
        "toggle":            "#5a6172",
        "toggle_hover":      "#5a6172",
        "toggle_hover_bg":   "#f2f3f6",
        # 品牌字符
        "brand_visible":     "false",
        "brand_color":       "#5b6cf0",
        "brand_dot":         "#f0824a",
        "brand_letter_sp":   "2px",
        "header_title_letter_sp": "0px",
        # Footer
        "footer":            "#aeb3c0",
        "footer_letter_sp":  "0px",
        # 发送按钮
        "send_disabled_bg":  "#d2d8e2",
        "send_active_bg":    "#5b6cf0",
        "send_active_hover": "#4a59e0",
        "send_stop_bg":      "#ef6a3d",
        "send_stop_hover":   "#e25a2d",
        "send_text":         "#ffffff",
        # Combobox（模型选择器）
        "combo_bg":            "#ffffff",
        "combo_border":        "#e3e4ea",
        "combo_text":          "#2b3142",
        "combo_hover_border":  "#c8cbf3",
        "combo_hover_text":    "#2b3142",
        "combo_arrow":         "#9aa0b0",
        "combo_view_bg":       "#ffffff",
        "combo_view_border":   "#e3e4ea",
        "combo_view_text":     "#2b3142",
        "combo_view_sel_bg":   "#eef0fe",
        "combo_view_sel_text": "#4a59e0",
        # Think btn（思考开关：开=靛蓝软底）
        "think_on_bg":     "#eef0fe",
        "think_on_border": "#eef0fe",
        "think_on_text":   "#5b6cf0",
        "think_on_hover":  "#e3e7fd",
        "think_on_hover_border": "#c8cbf3",
        "think_off_bg":    "#ffffff",
        "think_off_border":"#e8e8ed",
        "think_off_text":  "#9aa0b0",
        "think_off_hover_border":"#d7d9e0",
        "think_off_hover_text":  "#6a7080",
        # Segmented 计划|执行（灰底 pill + 选中态白底 + 阴影）
        "seg_bg":            "#f1f2f6",
        "seg_border":        "#f1f2f6",
        "seg_active_bg":     "#ffffff",
        "seg_active_border": "#ffffff",
        "seg_active_text":   "#5b6cf0",
        "seg_idle_text":     "#9aa0b0",
        "seg_hover_text":    "#5b6cf0",
        # 侧栏会话运行态徽章（待确认=琥珀 / 已完成=绿 / 生成中文字=蓝）
        "badge_warn_bg":     "#fdf0d5",
        "badge_warn_text":   "#9a6700",
        "badge_warn_border": "#f0d28a",
        "badge_done_bg":     "#e6f4ea",
        "badge_done_text":   "#1a7f37",
        "badge_done_border": "#c3e6cd",
        "badge_run_text":    "#5b6cf0",
        "badge_run_bg":      "#ffffff",
        "badge_run_border":  "transparent",
        # 会话行运行态底色（生成中=靛蓝软底,对齐 design active 行 / 待确认浅琥珀）
        "row_gen_bg":        "#eef0fe",
        "row_pending_bg":    "#fbf2e0",
        # Role btn idle
        "role_bg":            "#ffffff",
        "role_border":        "#e8e8ed",
        "role_text":          "#5a6172",
        "role_hover_bg":      "#fafbfc",
        "role_hover_border":  "#d7d9e0",
        "role_hover_text":    "#5a6172",
        # Role btn active（已加载角色）
        "role_active_bg":            "#fff1e9",
        "role_active_border":        "#e7ae92",
        "role_active_text":          "#b25634",
        "role_active_hover_bg":      "#ffe7dc",
        "role_active_hover_border":  "#d58d6b",
        "role_active_hover_text":    "#984323",
        "role_active_weight":        "600",
        # 图片附件
        "img_btn":           "#7f8795",
        "img_btn_hover":     "#5b66d6",
        "img_thumb_border":  "#dfe4ee",
        "img_thumb_bg":      "#ffffff",
        "img_del_bg":        "#e74c3c",
        "img_del_text":      "#ffffff",
        "img_del_hover_bg":  "#c0392b",
        # 聊天区滚动条/选区
        "chat_text":         "#1f2430",
        "chat_sel_bg":       "#dfe6ff",
        "chat_sel_text":     "#1f2430",
        "chat_scroll_handle":       "#c8cfda",
        "chat_scroll_handle_hover": "#a8b2c1",
        # 浮动回到底部按钮
        "scroll_btn_bg":       "#ffffff",
        "scroll_btn_border":   "#dfe4ee",
        "scroll_btn_icon":     "#5b66d6",
        "scroll_btn_hover_bg": "#f4f6fb",
        # 全局菜单
        "menu_bg":           "#ffffff",
        "menu_border":       "#dfe4ee",
        "menu_text":         "#1f2430",
        "menu_sel_bg":       "#e8ecff",
        "menu_sel_text":     "#232b7a",
        "menu_disabled":     "#9aa3b2",
        "menu_separator":    "#e6e9f0",
        # Tooltip
        "tooltip_bg":        "#ffffff",
        "tooltip_text":      "#1f2430",
        "tooltip_border":    "#dfe4ee",
        # 聊天文本（QTextCharFormat）
        "user_label":        "#5b6cf0",
        "ai_label":          "#ef7a45",
        "user_msg":          "#2b3142",
        "ai_msg":            "#2b3142",
        "thinking":          "#5b6cf0",
        "thinking_bg":       "#f7f8fa",
        "thinking_msg":      "#7a8092",
        "thinking_msg_bg":   "#f7f8fa",
        "tool":              "#5b6cf0",
        "tool_bg":           "#f7f8fa",
        "tool_result":       "#5a6172",
        "tool_result_bg":    "#fafbfc",
        "warn":              "#e2553d",
        "retry_link":        "#5b6cf0",
        "retry_link_bg":     "#eef0fe",
        "retry_link_border": "#e6e7ec",
        "copy_link":         "#8b94a3",
        "copy_link_bg":      "transparent",
        "copy_link_border":  "transparent",
        # Markdown
        "md_text":           "#1f2430",
        "md_code_bg":        "#eef1f6",
        "md_code_text":      "#232b7a",
        "md_pre_bg":         "#f3f5f8",
        "md_pre_text":       "#1f2430",
        "md_pre_border_left":"#c8cdf7",
        "md_table_border":   "#d3d9e4",
        "md_th_bg":          "#eef1f6",
        "md_th_text":        "#232b7a",
        "md_td_text":        "#1f2430",
        "md_blockquote_border":"#c8cdf7",
        "md_blockquote_text":  "#596273",
        "md_blockquote_bg":    "transparent",
        "md_h_color":          "#1f2430",
        "md_copy_btn_bg":      "#e8ecff",
        "md_copy_btn_text":    "#3842b8",
        "md_copy_btn_border":  "#d7dcfb",
        # Drag overlay
        "drag_bg":           "rgba(255, 255, 255, 0.85)",
        "drag_border":       "#5b66d6",
        "drag_border_style": "3px dashed",
        "drag_text":         "#5b66d6",
        "drag_subtext":      "#8b94a3",
        # Toast
        "toast_bg":          "#333333",
        "toast_text":        "#ffffff",
        "toast_border":      "transparent",
        # Search bar
        "search_bg":         "#f5f7fb",
        "search_border":     "#dfe4ee",
        "search_input_bg":   "#ffffff",
        "search_input_border":"#dfe4ee",
        "search_input_text": "#1f2430",
        "search_input_focus":"#aeb6ef",
        "search_btn_text":   "#343b4c",
        "search_btn_bg":     "transparent",
        "search_btn_hover_bg":   "#e8ecff",
        "search_btn_hover_color":"#3842b8",
        "search_close":          "#8b94a3",
        "search_close_hover":    "#343b4c",
        "search_close_hover_bg": "transparent",
        # Think dialog
        "think_dlg_label":   "#5b66d6",
        "think_dlg_bg":      "#f8f9fb",
        "think_dlg_text":    "#343b4c",
        "think_dlg_border":  "#dfe4ee",
        "think_dlg_letter_sp":"0px",
        # 消息流与结果卡（message_view / result_card 经 color("mv_*") 按需取色）
        "mv_indigo": "#5b6cf0",
        "mv_indigo_soft": "#eef0fe",
        "mv_orange": "#ef7a45",
        "mv_text": "#222838",
        "mv_text2": "#2b3142",
        "mv_text3": "#3a4150",
        "mv_text_sec": "#5a6172",
        "mv_muted": "#6b7183",
        "mv_muted2": "#9aa0b0",
        "mv_muted3": "#aeb3c0",
        "mv_card_bg": "#ffffff",
        "mv_card_border": "#ebecf1",
        "mv_header_bg": "#f7f8fa",
        "mv_result_bg": "#fafbfc",
        "mv_result_border": "#f0f0f3",
        "mv_chip_border": "#e3e4ea",
        "mv_chip_hover": "#c8cbf3",
        "mv_box_border": "#cdd1dc",
        "mv_panel_accent": "#c8cbf3",
        "mv_spin_track": "#dfe2f0",
        "mv_json_key": "#9a6bd6",
        "mv_json_str": "#3a8a5f",
        "mv_json_num": "#c2783f",
        "mv_json_punct": "#5a6172",
        "mv_view_bg": "#ffffff",
        "mv_err_bg": "#fdf3f1",
        "mv_err_border": "#f1d2cd",
        "mv_err_text": "#a93226",
        "mv_bad": "#c0392b",
        "mv_ok": "#2e7d4a",
        # 需要你处理的东西（确认条、暂停横幅、设置里的重启提示）：琥珀色一套
        "mv_gate_bg": "#fff8ec",
        "mv_gate_border": "#eed9b5",
        "mv_gate_text": "#8a4b00",
    },
    "dark": {
        # 主表面
        "win_bg":            "#16181d",
        "sidebar_bg":        "#1b1e24",
        "sidebar_border":    "#2b2f38",
        "header_bg":         "#16181d",
        "header_border":     "#2b2f38",
        "chat_bg":           "#16181d",
        "input_bg":          "#20242b",
        "input_border":      "#3a3f4b",
        "input_text":        "#e7e9ee",
        "input_sel_bg":      "#33396b",
        "input_sel_text":    "#e7e9ee",
        # 文本
        "text":              "#e7e9ee",
        "text_dim":          "#c9ceda",
        "text_subtle":       "#a3aab8",
        "text_muted":        "#7a8190",
        # 历史项
        "history_label":           "#8c93a1",
        "history_label_spacing":   "0.06em",
        "history_item":            "#c9ceda",
        "history_hover_bg":        "#20242b",
        "history_hover_text":      "#e7e9ee",
        "history_active_bg":       "#262b45",
        "history_active_text":     "#e7e9ee",
        "history_active_border":   "#7b89f7",
        "history_active_pad_left": "11px",
        # 滚动条
        "scrollbar_track":   "transparent",
        "scrollbar_handle":  "#3a3f4b",
        "scrollbar_handle_hover":"#7b89f7",
        "scrollbar_thin":    "#3a3f4b",
        # 删除按钮
        "del_btn":           "#8c93a1",
        "del_btn_hover":     "#f09a8d",
        "del_btn_hover_bg":  "#2a1d1c",
        # New chat 按钮
        "new_chat_bg":           "#1c1f26",
        "new_chat_text":         "#e7e9ee",
        "new_chat_border":       "#3a3f4b",
        "new_chat_hover_bg":     "#20242b",
        "new_chat_hover_border": "#7b89f7",
        "new_chat_hover_text":   "#c3caff",
        # Toggle btn
        "toggle":            "#a3aab8",
        "toggle_hover":      "#e7e9ee",
        "toggle_hover_bg":   "#20242b",
        # 品牌字符
        "brand_visible":     "false",
        "brand_color":       "#7b89f7",
        "brand_dot":         "#e0785f",
        "brand_letter_sp":   "2px",
        "header_title_letter_sp": "0px",
        # Footer
        "footer":            "#7a8190",
        "footer_letter_sp":  "0px",
        # 发送按钮
        "send_disabled_bg":  "#3a3f4b",
        "send_active_bg":    "#7b89f7",
        "send_active_hover": "#95a0fa",
        "send_stop_bg":      "#e0785f",
        "send_stop_hover":   "#e88e78",
        "send_text":         "#ffffff",
        # Combobox
        "combo_bg":            "#20242b",
        "combo_border":        "#3a3f4b",
        "combo_text":          "#e7e9ee",
        "combo_hover_border":  "#7b89f7",
        "combo_hover_text":    "#c3caff",
        "combo_arrow":         "#7b89f7",
        "combo_view_bg":       "#20242b",
        "combo_view_border":   "#3a3f4b",
        "combo_view_text":     "#e7e9ee",
        "combo_view_sel_bg":   "#262b45",
        "combo_view_sel_text": "#c3caff",
        # Think btn
        "think_on_bg":     "#262b45",
        "think_on_border": "#7b89f7",
        "think_on_text":   "#c3caff",
        "think_on_hover":  "#2e3456",
        "think_on_hover_border": "#95a0fa",
        "think_off_bg":    "transparent",
        "think_off_border":"#3a3f4b",
        "think_off_text":  "#a3aab8",
        "think_off_hover_border":"#8c93a1",
        "think_off_hover_text":  "#c9ceda",
        # Segmented 计划|执行
        "seg_bg":            "#21252d",
        "seg_border":        "#3a3f4b",
        "seg_active_bg":     "#262b45",
        "seg_active_border": "#33396b",
        "seg_active_text":   "#c3caff",
        "seg_idle_text":     "#a3aab8",
        "seg_hover_text":    "#c3caff",
        # 侧栏会话运行态徽章
        "badge_warn_bg":     "#2a2318",
        "badge_warn_text":   "#f0c178",
        "badge_warn_border": "#6e5530",
        "badge_done_bg":     "#1e3326",
        "badge_done_text":   "#7cc79a",
        "badge_done_border": "#2f5a3e",
        "badge_run_text":    "#a3adff",
        "badge_run_bg":      "#1d2133",
        "badge_run_border":  "#3e4680",
        # 会话行运行态底色
        "row_gen_bg":        "#1d2133",
        "row_pending_bg":    "#2a2318",
        # Role btn idle
        "role_bg":            "transparent",
        "role_border":        "#3a3f4b",
        "role_text":          "#c9ceda",
        "role_hover_bg":      "#20242b",
        "role_hover_border":  "#7b89f7",
        "role_hover_text":    "#c3caff",
        # Role btn active
        "role_active_bg":            "#2a2318",
        "role_active_border":        "#8e6e3a",
        "role_active_text":          "#e0b070",
        "role_active_hover_bg":      "#3a2f1e",
        "role_active_hover_border":  "#e0b070",
        "role_active_hover_text":    "#f0c178",
        "role_active_weight":        "600",
        # 图片附件
        "img_btn":           "#a3aab8",
        "img_btn_hover":     "#7b89f7",
        "img_thumb_border":  "#3a3f4b",
        "img_thumb_bg":      "#20242b",
        "img_del_bg":        "#e06c5c",
        "img_del_text":      "#16181d",
        "img_del_hover_bg":  "#f09a8d",
        # 聊天区滚动条/选区
        "chat_text":         "#e7e9ee",
        "chat_sel_bg":       "#33396b",
        "chat_sel_text":     "#e7e9ee",
        "chat_scroll_handle":       "#3a3f4b",
        "chat_scroll_handle_hover": "#5a6172",
        # 全局菜单
        "menu_bg":           "#20242b",
        "menu_border":       "#3a3f4b",
        "menu_text":         "#e7e9ee",
        # 浮动回到底部按钮
        "scroll_btn_bg":       "#3a3f4b",
        "scroll_btn_border":   "#3a3f4b",
        "scroll_btn_icon":     "#c9ceda",
        "scroll_btn_hover_bg": "#3a3f4b",
        "menu_sel_bg":       "#262b45",
        "menu_sel_text":     "#c3caff",
        "menu_disabled":     "#a3aab8",
        "menu_separator":    "#3a3f4b",
        # Tooltip
        "tooltip_bg":        "#262a32",
        "tooltip_text":      "#c9ceda",
        "tooltip_border":    "#3a3f4b",
        # 聊天文本
        "user_label":        "#a3adff",
        "ai_label":          "#7b89f7",
        "user_msg":          "#e7e9ee",
        "ai_msg":            "#e7e9ee",
        "thinking":          "#b9a3f0",
        "thinking_bg":       "#21252d",
        "thinking_msg":      "#a3aab8",
        "thinking_msg_bg":   "#191c22",
        "tool":              "#a3aab8",
        "tool_bg":           "#191c22",
        "tool_result":       "#a3aab8",
        "tool_result_bg":    "#191c22",
        "warn":              "#f09a8d",
        "retry_link":        "#7b89f7",
        "retry_link_bg":     "#262b45",
        "retry_link_border": "#33396b",
        "copy_link":         "#7b89f7",
        "copy_link_bg":      "#191c22",
        "copy_link_border":  "transparent",
        # Markdown
        "md_text":           "#e7e9ee",
        "md_code_bg":        "#262a32",
        "md_code_text":      "#c3caff",
        "md_pre_bg":         "#191c22",
        "md_pre_text":       "#e7e9ee",
        "md_pre_border_left":"#7b89f7",
        "md_table_border":   "#3a3f4b",
        "md_th_bg":          "#20242b",
        "md_th_text":        "#7b89f7",
        "md_td_text":        "#e7e9ee",
        "md_blockquote_border":"#7b89f7",
        "md_blockquote_text":  "#a3aab8",
        "md_blockquote_bg":    "#191c22",
        "md_h_color":          "#e7e9ee",
        "md_copy_btn_bg":      "#262b45",
        "md_copy_btn_text":    "#7b89f7",
        "md_copy_btn_border":  "#33396b",
        # Drag overlay
        "drag_bg":           "rgba(22, 24, 29, 0.92)",
        "drag_border":       "#7b89f7",
        "drag_border_style": "2px dashed",
        "drag_text":         "#7b89f7",
        "drag_subtext":      "#a3aab8",
        # Toast
        "toast_bg":          "#2b2f38",
        "toast_text":        "#e7e9ee",
        "toast_border":      "#33396b",
        # Search bar
        "search_bg":         "#20242b",
        "search_border":     "#3a3f4b",
        "search_input_bg":   "#16181d",
        "search_input_border":"#3a3f4b",
        "search_input_text": "#e7e9ee",
        "search_input_focus":"#7b89f7",
        "search_btn_text":   "#c9ceda",
        "search_btn_bg":     "transparent",
        "search_btn_hover_bg":   "#3a3f4b",
        "search_btn_hover_color":"#7b89f7",
        "search_close":          "#a3aab8",
        "search_close_hover":    "#f09a8d",
        "search_close_hover_bg": "#3a3f4b",
        # Think dialog
        "think_dlg_label":   "#b9a3f0",
        "think_dlg_bg":      "#16181d",
        "think_dlg_text":    "#c9ceda",
        "think_dlg_border":  "#3a3f4b",
        "think_dlg_letter_sp":"2px",
        # 消息流与结果卡（message_view / result_card 经 color("mv_*") 按需取色）
        "mv_indigo": "#7b89f7",
        "mv_indigo_soft": "#262b45",
        "mv_orange": "#e0a84e",
        "mv_text": "#e7e9ee",
        "mv_text2": "#d5d9e0",
        "mv_text3": "#c9ceda",
        "mv_text_sec": "#a3aab8",
        "mv_muted": "#8c93a1",
        "mv_muted2": "#7a8190",
        "mv_muted3": "#6b7280",
        "mv_card_bg": "#1c1f26",
        "mv_card_border": "#2b2f38",
        "mv_header_bg": "#21252d",
        "mv_result_bg": "#191c22",
        "mv_result_border": "#262a32",
        "mv_chip_border": "#3a3f4b",
        "mv_chip_hover": "#4a5170",
        "mv_box_border": "#4a505c",
        "mv_panel_accent": "#3e4680",
        "mv_spin_track": "#2e3346",
        "mv_json_key": "#c3a2f0",
        "mv_json_str": "#7cc79a",
        "mv_json_num": "#e2a26e",
        "mv_json_punct": "#a3aab8",
        "mv_view_bg": "#16181d",
        "mv_err_bg": "#2a1d1c",
        "mv_err_border": "#5a2e2a",
        "mv_err_text": "#f09a8d",
        "mv_bad": "#e06c5c",
        "mv_ok": "#7cc79a",
        "mv_gate_bg": "#2a2318",
        "mv_gate_border": "#5a4626",
        "mv_gate_text": "#f0c178",
    },
}

DEFAULT_THEME = "light"


_active_theme = "light"


def set_active_theme(name):
    """记下当前主题，供按需取色的组件（消息流、结果卡）使用。"""
    global _active_theme
    _active_theme = name if name in THEMES else "light"


def active_theme():
    return _active_theme


def color(key):
    """当前主题下的一个颜色。要在绘制时取：取好存成模块级常量的话，切主题不会跟着变。"""
    return THEMES[_active_theme][key]


def load_saved_theme():
    """读取持久化的主题选择，失败回到默认"""
    try:
        with open(THEME_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("theme")
        if name in THEMES:
            return name
    except Exception:
        pass
    return DEFAULT_THEME


def save_theme_choice(name):
    try:
        os.makedirs(os.path.dirname(THEME_CONFIG_PATH), exist_ok=True)
        with open(THEME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"theme": name}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def build_tooltip_qss(theme):
    """只含 QToolTip 的 QSS。

    QToolTip 是独立顶层弹窗，**不继承主窗口的 setStyleSheet**——必须设到
    QApplication 级别才生效。否则 tooltip 会用 Qt/系统默认（Windows 上是黑底），
    跟主题对不上。所以单独抽出来，由 ChatUI 设到 app 上。
    """
    p = THEMES[theme]
    return f"""
QToolTip {{
    background-color: {p['tooltip_bg']};
    color: {p['tooltip_text']};
    border: 1px solid {p['tooltip_border']};
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
}}
"""


def build_stylesheet(theme):
    p = THEMES[theme]
    return f"""
QMainWindow, QDialog {{
    background-color: {p['win_bg']};
    font-family: {UI_CSS};
}}

/* 侧边栏 */
#sidebar {{
    background-color: {p['sidebar_bg']};
    border-right: 1px solid {p['sidebar_border']};
}}
#sidebarBrand {{
    background: transparent;
}}
#sidebarBrandTitle {{
    color: {p['text']};
    font-family: {UI_CSS};
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0px;
}}
#sidebarBrandSub {{
    color: {p['text_subtle']};
    font-size: 12px;
}}
#sidebarFooter {{
    background: transparent;
    border: none;
}}
#sidebarSettingsBtn {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: {p['text_dim']};
    font-size: 16px;
    padding: 0;
}}
#sidebarSettingsBtn:hover {{
    background: {p['history_hover_bg']};
    color: {p['text']};
    border-color: {p['sidebar_border']};
}}
#sidebarSettingsBtn:pressed {{
    background: {p['history_active_bg']};
}}
#sidebar QPushButton#newChatBtn {{
    background: {p['new_chat_bg']};
    border: 1px solid {p['new_chat_border']};
    color: {p['new_chat_text']};
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 10px 16px;
    border-radius: 8px;
}}
#sidebar QPushButton#newChatBtn:hover {{
    background-color: {p['new_chat_hover_bg']};
    border-color: {p['new_chat_hover_border']};
    color: {p['new_chat_hover_text']};
}}
/* 侧栏 编码|知识库 模式段控（撑满侧栏宽度，复用顶栏段控配色） */
#ragModeSeg {{
    background: {p['seg_bg']};
    border: 1px solid {p['seg_border']};
    border-radius: 9px;
}}
#ragModeSeg QPushButton[class="segBtn"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 9px 0;
    font-size: 13px;
    color: {p['seg_idle_text']};
}}
#ragModeSeg QPushButton[class="segBtn"]:hover {{
    color: {p['seg_hover_text']};
}}
#ragModeSeg QPushButton[class="segBtn"]:checked {{
    background: {p['seg_active_bg']};
    border: 1px solid {p['seg_active_border']};
    color: {p['seg_active_text']};
    font-weight: 600;
}}
/* 知识库管理卡片：轻背景 + 圆角，不堆边框 */
#kbCard {{
    background: {p['seg_bg']};
    border-radius: 10px;
}}
#kbCard QLabel#kbCardStatus {{
    font-size: 12px;
    color: {p['history_label']};
    padding: 1px 2px;
}}
/* 状态圆点颜色（kbState 属性驱动；中间调在浅/深主题都可读） */
#kbCard QLabel#kbCardStatus[kbState="ok"] {{ color: #21a366; }}
#kbCard QLabel#kbCardStatus[kbState="indexing"] {{ color: {p['seg_active_text']}; }}
#kbCard QLabel#kbCardStatus[kbState="stale"] {{ color: #d97706; }}
#kbCard QLabel#kbCardStatus[kbState="error"] {{ color: #dc2626; }}
#kbCard QPushButton[class="kbCardBtn"] {{
    background: transparent;
    border: 1px solid {p['seg_border']};
    border-radius: 6px;
    padding: 5px 0;
    font-size: 12px;
    color: {p['seg_idle_text']};
}}
#kbCard QPushButton[class="kbCardBtn"]:hover {{
    color: {p['seg_hover_text']};
    background: {p['history_hover_bg']};
}}
#kbCard QPushButton[class="kbCardBtn"]:disabled {{
    color: {p['seg_border']};
}}
#sidebar QPushButton#projectHeader {{
    background: transparent;
    border: none;
    color: {p['history_label']};
    font-size: 12px;
    font-weight: 600;
    text-align: left;
    padding: 8px 10px 4px 10px;
    letter-spacing: 0.5px;
}}
#sidebar QPushButton#projectHeader:hover {{
    color: {p['text']};
}}
#sidebar QPushButton#projectHeaderActive {{
    background: transparent;
    border: none;
    color: {p['new_chat_hover_text']};
    font-size: 12px;
    font-weight: 600;
    text-align: left;
    padding: 8px 10px 4px 10px;
    letter-spacing: 0.5px;
}}
#sidebar #historyRow {{
    background: transparent;
}}
/* 整行 hover 底色——配合 HistoryRow 的 WA_Hover,覆盖标题/× 全宽,和选中态一致。
   放在 rowstate/current 之前:后定义者胜,故运行态/选中底色优先级更高(hover 时不被盖) */
#sidebar #historyRow:hover {{
    background: {p['history_hover_bg']};
    border-radius: 9px;
}}
/* 运行态会话行底色（生成中 / 待确认）——属性选择器,不污染 tooltip 继承 */
#sidebar #historyRow[rowstate="generating"] {{
    background: {p['row_gen_bg']};
    border-radius: 9px;
}}
#sidebar #historyRow[rowstate="pending"] {{
    background: {p['row_pending_bg']};
    border-radius: 9px;
}}
/* 选中(当前)会话整行底色——放在 rowstate 之后,选中态优先于运行态底色 */
#sidebar #historyRow[current="true"] {{
    background: {p['history_active_bg']};
    border-radius: 9px;
}}
#sidebar #historyEmptyHint {{
    color: {p['history_label']};
    font-size: 12px;
    padding: 2px 0 6px 22px;
}}
#historyLabel {{
    color: {p['history_label']};
    font-size: 12px;
    font-weight: 600;
    letter-spacing: {p['history_label_spacing']};
    padding: 18px 14px 8px 14px;
}}

/* 历史项 */
/* padding-right=0 让右侧的删除按钮(×)能贴近文字、不被 padding 推开。
 * 之前在 sidebar.py 里用 btn.setStyleSheet 注入这一句，会让 btn 自带 stylesheet
 * 从而切断 app 级 QToolTip 规则继承——tooltip 颜色异常。移到这里走类选择器就没这个坑。
 */
QPushButton[class="historyItem"] {{
    background: transparent;
    border: none;
    color: {p['history_item']};
    font-size: 14px;
    text-align: left;
    padding: 9px 0 9px 12px;
    border-radius: 9px;
}}
QPushButton[class="historyItem"]:hover {{
    color: {p['history_hover_text']};
}}
/* 选中态：按钮本身透明,整行底色由 #historyRow[current="true"] 提供(整行高亮)；
 * 这里只保留加粗 + 选中文字色,避免按钮底色和整行底色叠成"嵌套块"。*/
QPushButton[class="historyItemActive"] {{
    background-color: transparent;
    border: none;
    color: {p['history_active_text']};
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 9px 0 9px 12px;
    border-radius: 9px;
}}
/* 后台会话完成、尚未查看：绿点 + 绿字（切回该会话查看后恢复普通样式）。
 * 绿色两主题通用，故硬编码不走 palette。*/
QPushButton[class="historyItemDone"] {{
    background: transparent;
    border: none;
    color: #22c55e;
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 9px 0 9px 12px;
    border-radius: 9px;
}}
QPushButton[class="historyItemDone"]:hover {{
    color: #16a34a;
}}

/* 聊天区 */
#chatArea {{
    background-color: {p['chat_bg']};
    border: none;
    padding: 20px;
    font-family: {UI_CSS};
    font-size: 14px;
    color: {p['chat_text']};
}}
#chatArea[empty="true"] {{
    color: {p['text']};
}}
#emptyState {{
    background: transparent;
}}
#emptyLogo {{
    background: {p['mv_indigo_soft']};
    color: {p['mv_indigo']};
    border-radius: 24px;
    font-family: {UI_CSS};
    font-size: 20px;
    font-weight: 600;
}}
#emptyTitle {{
    color: {p['mv_text']};
    font-size: 20px;
    font-weight: 600;
}}
#emptySubtitle {{
    color: {p['mv_text_sec']};
    font-size: 14px;
}}
#emptySuggestion {{
    background: {p['mv_card_bg']};
    border: 1px solid {p['mv_chip_border']};
    border-radius: 10px;
    padding: 10px 14px;
    color: {p['mv_text3']};
    font-size: 13px;
    font-family: {UI_CSS};
    text-align: left;
}}
#emptySuggestion:hover {{
    background: {p['mv_card_bg']};
    border-color: {p['mv_chip_hover']};
    color: {p['mv_text']};
}}

/* 输入区容器 */
#inputContainer {{
    background-color: {p['input_bg']};
    border: 1px solid {p['input_border']};
    border-radius: 14px;
}}
#inputEdit {{
    background-color: transparent;
    border: none;
    font-family: {UI_CSS};
    font-size: 14px;
    padding: 0 2px;
    color: {p['input_text']};
    selection-background-color: {p['input_sel_bg']};
    selection-color: {p['input_sel_text']};
}}

/* 发送按钮 */
#sendBtn {{
    border: none;
    border-radius: 9px;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    font-size: 16px;
    padding: 0px;
}}
#sendBtn[state="disabled"] {{
    background-color: {p['send_disabled_bg']};
    color: {p['send_text']};
}}
#sendBtn[state="enabled"] {{
    background-color: {p['send_active_bg']};
    color: {p['send_text']};
}}
#sendBtn[state="enabled"]:hover {{
    background-color: {p['send_active_hover']};
}}
#sendBtn[state="stop"] {{
    background-color: {p['send_active_bg']};
    color: {p['send_text']};
    font-size: 16px;
}}
#sendBtn[state="stop"]:hover {{
    background-color: {p['send_active_hover']};
}}

/* 顶栏 */
#header {{
    background-color: {p['header_bg']};
    border-bottom: 1px solid {p['header_border']};
}}
/* 顶栏按钮（设计稿 A5）：不带边框的"幽灵"按钮，图标 + 短词；开着的（思考、任务面板、
   隔离中）用浅主题色底。图标是按状态现画的单色 pixmap，见 header.py _refresh_header_icons */
#header QPushButton[class="ghost"] {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0 8px;
    min-height: 30px;
    max-height: 30px;
    font-size: 13px;
    color: {p['mv_text_sec']};
}}
#header QPushButton[class="ghost"]:hover {{
    background: {p['mv_header_bg']};
    color: {p['mv_text']};
}}
#header QPushButton[class="ghost"]:checked,
#header QPushButton[class="ghost"][on="true"] {{
    background: {p['mv_indigo_soft']};
    color: {p['mv_indigo']};
}}
#header QPushButton[class="ghost"]:disabled {{
    color: {p['mv_muted3']};
}}
#header QPushButton[class="icon"] {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0;
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
}}
#header QPushButton[class="icon"]:hover {{
    background: {p['mv_header_bg']};
}}
#header QPushButton[class="icon"]:checked {{
    background: {p['mv_indigo_soft']};
}}

/* 计划|执行 段控：浅灰底的一条，选中的那段是白底主题色字 */
#modeSeg {{
    background: {p['mv_header_bg']};
    border: none;
    border-radius: 8px;
}}
#modeSeg QPushButton[class="segBtn"] {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0 10px;
    min-height: 26px;
    max-height: 26px;
    font-size: 13px;
    color: {p['mv_text_sec']};
}}
#modeSeg QPushButton[class="segBtn"]:hover {{
    color: {p['mv_text']};
}}
#modeSeg QPushButton[class="segBtn"]:checked {{
    background: {p['mv_card_bg']};
    color: {p['mv_indigo']};
    font-weight: 600;
}}
#headerTitle {{
    font-family: {UI_CSS};
    font-size: 16px;
    font-weight: 600;
    color: {p['text']};
    letter-spacing: {p['header_title_letter_sp']};
    padding: 0 8px;
}}
#headerBrand {{
    font-family: {UI_CSS};
    font-size: 16px;
    color: {p['brand_color']};
    letter-spacing: {p['brand_letter_sp']};
    padding: 0 4px 0 12px;
}}
#headerBrandDot {{
    color: {p['brand_dot']};
    font-size: 12px;
    padding: 0 8px 0 0;
}}
#footerLabel {{
    color: {p['footer']};
    font-size: 12px;
    letter-spacing: {p['footer_letter_sp']};
}}
#tokenUsageLabel {{
    color: {p['footer']};
    font-size: 12px;
    letter-spacing: {p['footer_letter_sp']};
}}

/* 全局菜单 */
QMenu {{
    background-color: {p['menu_bg']};
    border: 1px solid {p['menu_border']};
    border-radius: 10px;
    padding: 6px;
    color: {p['menu_text']};
}}
QMenu::item {{
    padding: 7px 28px 7px 14px;
    border-radius: 6px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background-color: {p['menu_sel_bg']};
    color: {p['menu_sel_text']};
}}
QMenu::item:disabled {{
    color: {p['menu_disabled']};
}}
QMenu::separator {{
    height: 1px;
    background: {p['menu_separator']};
    margin: 6px 10px;
}}

/* 工具提示 */
QToolTip {{
    background-color: {p['tooltip_bg']};
    color: {p['tooltip_text']};
    border: 1px solid {p['tooltip_border']};
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 12px;
}}
"""
