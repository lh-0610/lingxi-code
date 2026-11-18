"""主题系统：light（白天）+ dark（夜间）。两套配色与设计稿 A5 一致，强调色都是靛蓝。

THEMES 是单一颜色字典，build_stylesheet 把它编译成全局 QSS。
load/save_theme_choice 负责把选中的主题持久化到 chat_memory/theme_config.json。
"""
import json
import os

from ..paths import theme_config_path
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
        # 左侧栏（设计稿「左侧栏：编码与知识库两个工作区」）
        "sb_card_bg":          "#ffffff",
        "sb_card_border":      "#e3e4ea",
        "sb_seg_bg":           "#eceef3",
        "sb_seg_active_bg":    "#ffffff",
        "sb_seg_active_text":  "#4a59e0",
        "sb_seg_idle_text":    "#5a6172",
        "sb_accent":           "#4a59e0",
        "sb_row_text":         "#2b3142",
        "sb_row_hover_bg":     "#eceef3",
        "sb_row_current_bg":   "#eef0fe",
        "sb_row_current_text": "#3b48c9",
        "sb_group_text":       "#3a4152",
        "sb_group_active_text": "#222838",
        "sb_meta":             "#5a6172",
        "sb_chip_bg":          "#eef0fe",
        "sb_chip_text":        "#4a59e0",
        "sb_dot_done":         "#1a7f37",
        "sb_placeholder":      "#7a8193",
        "sb_badge_bg":         "#4a59e0",
        "sb_badge_text":       "#ffffff",
        "sb_danger":           "#b42318",
        # 知识库卡片五态
        "kb_ok_text":          "#1a7f37",
        "kb_busy_border":      "#c8cbf3",
        "kb_busy_track":       "#eef0fe",
        "kb_stale_bg":         "#fffaf0",
        "kb_stale_border":     "#f0d28a",
        "kb_stale_text":       "#9a6700",
        "kb_stale_detail":     "#6b4e00",
        "kb_error_bg":         "#fff5f5",
        "kb_error_border":     "#f2c6c6",
        "kb_error_text":       "#b42318",
        "kb_error_detail":     "#7a1d14",
        "kb_primary_bg":       "#4a59e0",
        "kb_primary_text":     "#ffffff",
        "kb_btn_bg":           "#f7f8fa",
        "kb_btn_disabled":     "#9aa0ad",
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
        "md_code_bg":        "#f1f2f5",
        "md_code_text":      "#2b3142",
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
        # 文件面板的语法高亮（syntax_highlight.TOKEN_BY_CATEGORY）：和上面这套同一族色相，
        # 白底和新增 / 删除行的淡底上都压得住
        "syn_keyword": "#7a52cc",
        "syn_string": "#1c7a72",
        "syn_comment": "#6e7781",
        "syn_number": "#c2783f",
        "syn_function": "#4a59e0",
        "syn_type": "#b25634",
        "syn_constant": "#9a6700",
        "syn_operator": "#5a6172",
        "syn_punct": "#9aa0b0",
        # 需要你处理的东西（确认条、暂停横幅、设置里的重启提示）：琥珀色一套
        "mv_gate_bg": "#fff8ec",
        "mv_gate_border": "#eed9b5",
        "mv_gate_text": "#8a4b00",
        # 主题弹窗（ui/dialogs.py，设计稿「灵犀弹窗设计」）：遮罩 + 卡片 + 四种图标底色 + 三种按钮
        "dlg_scrim": "rgba(22, 26, 38, 0.42)",
        "dlg_shadow": "rgba(22, 26, 38, 0.22)",
        "dlg_bg": "#ffffff",
        "dlg_border": "#ececf0",
        "dlg_title": "#222838",
        "dlg_body": "#5a6172",
        "dlg_code_bg": "#eef1f6",
        "dlg_code_border": "#eef1f6",
        "dlg_code_text": "#232b7a",
        "dlg_link": "#4a59e0",
        "dlg_hover_bg": "#f1f2f6",
        "dlg_focus_ring": "#c8cbf3",
        "dlg_info_bg": "#eef0fe",
        "dlg_info_border": "#c8cbf3",
        "dlg_info_fg": "#4a59e0",
        "dlg_ok_bg": "#e6f4ea",
        "dlg_ok_border": "#c3e6cd",
        "dlg_ok_fg": "#1a7f37",
        "dlg_warn_bg": "#fdf0d5",
        "dlg_warn_border": "#f0d28a",
        "dlg_warn_fg": "#9a6700",
        "dlg_warn_text": "#6b4800",
        "dlg_err_bg": "#fdebe7",
        "dlg_err_border": "#f2c4ba",
        "dlg_err_fg": "#c43d27",
        # 主按钮实心靛蓝；危险按钮 #c43d27 比 warn 色深，白字对比度过 4.5:1
        "dlg_primary_bg": "#4a59e0",
        "dlg_primary_hover": "#3d4bc4",
        "dlg_primary_text": "#ffffff",
        "dlg_danger_bg": "#c43d27",
        "dlg_danger_hover": "#a8321f",
        "dlg_danger_text": "#ffffff",
        "dlg_secondary_bg": "#ffffff",
        "dlg_secondary_hover": "#f4f5f8",
        "dlg_secondary_border": "#dadde6",
        "dlg_secondary_text": "#222838",
        "dlg_input_bg": "#ffffff",
        "dlg_input_border": "#dadde6",
        "dlg_input_focus": "#5b6cf0",
        # 卡片对话框左侧导航（设置）
        "dlg_nav_bg": "#f7f8fa",
        "dlg_nav_active_bg": "#eef0fe",
        "dlg_nav_active_text": "#4a59e0",
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
        # 左侧栏
        "sb_card_bg":          "#20242b",
        "sb_card_border":      "#3a3f4b",
        "sb_seg_bg":           "#16181d",
        "sb_seg_active_bg":    "#2a2f4d",
        "sb_seg_active_text":  "#c3caff",
        "sb_seg_idle_text":    "#c9ceda",
        "sb_accent":           "#95a0fa",
        "sb_row_text":         "#d5d9e2",
        "sb_row_hover_bg":     "#20242b",
        "sb_row_current_bg":   "#2a2f4d",
        "sb_row_current_text": "#c3caff",
        "sb_group_text":       "#d5d9e2",
        "sb_group_active_text": "#e7e9ee",
        "sb_meta":             "#a9afbd",
        "sb_chip_bg":          "#2a2f4d",
        "sb_chip_text":        "#c3caff",
        "sb_dot_done":         "#5cc77f",
        "sb_placeholder":      "#9aa1b2",
        "sb_badge_bg":         "#95a0fa",
        "sb_badge_text":       "#16181d",
        "sb_danger":           "#f09a8d",
        # 知识库卡片五态
        "kb_ok_text":          "#7cc79a",
        "kb_busy_border":      "#3f4573",
        "kb_busy_track":       "#262b45",
        "kb_stale_bg":         "#2a2318",
        "kb_stale_border":     "#6e5530",
        "kb_stale_text":       "#f0c178",
        "kb_stale_detail":     "#e8c98f",
        "kb_error_bg":         "#2a1d1c",
        "kb_error_border":     "#6e3530",
        "kb_error_text":       "#f09a8d",
        "kb_error_detail":     "#f0b8ae",
        "kb_primary_bg":       "#95a0fa",
        "kb_primary_text":     "#16181d",
        "kb_btn_bg":           "#1b1e24",
        "kb_btn_disabled":     "#7a8190",
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
        "md_code_bg":        "#23262d",
        "md_code_text":      "#d5d9e0",
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
        # 文件面板的语法高亮：深底上提亮一档，避免和 #16181d 的正文糊在一起
        "syn_keyword": "#c3a2f0",
        "syn_string": "#6fc2b4",
        "syn_comment": "#7a8190",
        "syn_number": "#e2a26e",
        "syn_function": "#a3adff",
        "syn_type": "#e0b070",
        "syn_constant": "#f0c178",
        "syn_operator": "#a3aab8",
        "syn_punct": "#6b7280",
        "mv_gate_bg": "#2a2318",
        "mv_gate_border": "#5a4626",
        "mv_gate_text": "#f0c178",
        # 主题弹窗：深底上实心按钮改用提亮的填充 + 深色字（设计稿深色板的做法）
        "dlg_scrim": "rgba(0, 0, 0, 0.55)",
        "dlg_shadow": "rgba(0, 0, 0, 0.5)",
        "dlg_bg": "#20242b",
        "dlg_border": "#3a3f4b",
        "dlg_title": "#e7e9ee",
        "dlg_body": "#c9ceda",
        "dlg_code_bg": "#262a32",
        "dlg_code_border": "#2b2f38",
        "dlg_code_text": "#c3caff",
        "dlg_link": "#95a0fa",
        "dlg_hover_bg": "#2b2f38",
        "dlg_focus_ring": "#33396b",
        "dlg_info_bg": "#262b45",
        "dlg_info_border": "#3e4680",
        "dlg_info_fg": "#95a0fa",
        "dlg_ok_bg": "#1c2a21",
        "dlg_ok_border": "#2f5a3d",
        "dlg_ok_fg": "#7cc79a",
        "dlg_warn_bg": "#2a2318",
        "dlg_warn_border": "#6e5530",
        "dlg_warn_fg": "#f0c178",
        "dlg_warn_text": "#f0c178",
        "dlg_err_bg": "#2a1d1c",
        "dlg_err_border": "#5a2e2a",
        "dlg_err_fg": "#f09a8d",
        "dlg_primary_bg": "#95a0fa",
        "dlg_primary_hover": "#aab3fb",
        "dlg_primary_text": "#16181d",
        "dlg_danger_bg": "#f09a8d",
        "dlg_danger_hover": "#f4b0a6",
        "dlg_danger_text": "#16181d",
        "dlg_secondary_bg": "#20242b",
        "dlg_secondary_hover": "#2b2f38",
        "dlg_secondary_border": "#3a3f4b",
        "dlg_secondary_text": "#e7e9ee",
        "dlg_input_bg": "#1b1e24",
        "dlg_input_border": "#3a3f4b",
        "dlg_input_focus": "#7b89f7",
        "dlg_nav_bg": "#1b1e24",
        "dlg_nav_active_bg": "#262b45",
        "dlg_nav_active_text": "#95a0fa",
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
        with open(theme_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("theme")
        if name in THEMES:
            return name
    except Exception:
        pass
    return DEFAULT_THEME


def save_theme_choice(name):
    try:
        path = theme_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
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


def build_app_qss(theme):
    """设到 QApplication 上的 QSS：tooltip + 全局滚动条。

    滚动条放在 app 级，是为了让所有地方（主窗口、各对话框、下拉列表和补全这类弹出层）
    都是同一种细的圆角条、没有上下箭头；没单独设过样式的控件以前用的是系统默认的箭头滚动条。
    个别控件自己设了更窄的版本，控件级样式优先，不受影响。"""
    p = THEMES[theme]
    return build_tooltip_qss(theme) + f"""
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px 1px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 1px 2px;
}}
QScrollBar::handle:vertical {{
    background: {p['scrollbar_handle']};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:horizontal {{
    background: {p['scrollbar_handle']};
    border-radius: 3px;
    min-width: 24px;
}}
QScrollBar::handle:hover {{
    background: {p['scrollbar_handle_hover']};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
}}
"""


def build_stylesheet(theme):
    p = THEMES[theme]
    return f"""
QMainWindow, QDialog {{
    background-color: {p['win_bg']};
    font-family: {UI_CSS};
}}

/* 侧边栏（设计稿「左侧栏：编码与知识库两个工作区」） */
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
    font-size: 17px;
    font-weight: 600;
    letter-spacing: 0px;
}}
#sidebarFooter {{
    background: transparent;
    border: none;
    border-top: 1px solid {p['sb_card_border']};
}}
#sidebarSettingsBtn, #sidebar QPushButton#sidebarBgBtn {{
    background: {p['sb_card_bg']};
    border: 1px solid {p['sb_card_border']};
    border-radius: 10px;
    color: {p['sb_row_text']};
    font-size: 13px;
    padding: 0;
}}
#sidebar QPushButton#sidebarBgBtn {{
    text-align: left;
    padding: 0 10px 0 10px;
}}
#sidebarSettingsBtn:hover, #sidebar QPushButton#sidebarBgBtn:hover {{
    border-color: {p['kb_busy_border']};
}}
#sidebarSettingsBtn:pressed, #sidebar QPushButton#sidebarBgBtn:pressed {{
    background: {p['sb_row_hover_bg']};
}}
#sidebar QLabel#sbBadge {{
    background: {p['sb_badge_bg']};
    color: {p['sb_badge_text']};
    border-radius: 9px;
    font-size: 11px;
    font-weight: 600;
    padding: 0 5px;
    min-width: 8px;
}}
#sidebar QPushButton#newChatBtn {{
    background: {p['sb_card_bg']};
    border: 1px solid {p['sb_card_border']};
    color: {p['sb_row_text']};
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 0 12px;
    border-radius: 10px;
}}
#sidebar QPushButton#newChatBtn:hover {{
    border-color: {p['kb_busy_border']};
}}
#sidebar QLineEdit#sidebarSearch {{
    background: {p['sb_card_bg']};
    color: {p['sb_row_text']};
    border: 1px solid {p['sb_card_border']};
    border-radius: 9px;
    padding: 0 6px 0 2px;
    font-size: 13px;
}}
#sidebar QLineEdit#sidebarSearch:focus {{
    border-color: {p['sb_accent']};
}}
/* 编码 | 知识库 工作区切换 */
#ragModeSeg {{
    background: {p['sb_seg_bg']};
    border: none;
    border-radius: 10px;
}}
#ragModeSeg QPushButton[class="segBtn"] {{
    background: transparent;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    color: {p['sb_seg_idle_text']};
}}
#ragModeSeg QPushButton[class="segBtn"]:hover {{
    color: {p['sb_seg_active_text']};
}}
#ragModeSeg QPushButton[class="segBtn"]:checked {{
    background: {p['sb_seg_active_bg']};
    color: {p['sb_seg_active_text']};
    font-weight: 600;
}}
/* 知识库卡片：五态（kbState 属性驱动，见 rag_sidebar 模块说明） */
#kbCard {{
    background: {p['sb_card_bg']};
    border: 1px solid {p['sb_card_border']};
    border-radius: 12px;
}}
#kbCard[kbState="indexing"] {{ border-color: {p['kb_busy_border']}; }}
#kbCard[kbState="none"] {{ border: 1px dashed {p['kb_busy_border']}; }}
#kbCard[kbState="stale"] {{ background: {p['kb_stale_bg']}; border-color: {p['kb_stale_border']}; }}
#kbCard[kbState="error"] {{ background: {p['kb_error_bg']}; border-color: {p['kb_error_border']}; }}
#kbCard QLabel {{ background: transparent; }}
#kbCard QLabel#kbCardName {{
    font-size: 14px;
    font-weight: 600;
    color: {p['sb_group_active_text']};
}}
#kbCard QLabel#kbCardState {{ font-size: 12px; color: {p['sb_meta']}; }}
#kbCard[kbState="ok"] QLabel#kbCardState {{ color: {p['kb_ok_text']}; }}
#kbCard[kbState="reading"] QLabel#kbCardState, #kbCard[kbState="indexing"] QLabel#kbCardState {{ color: {p['sb_accent']}; }}
#kbCard[kbState="stale"] QLabel#kbCardState {{ color: {p['kb_stale_text']}; }}
#kbCard[kbState="error"] QLabel#kbCardState {{ color: {p['kb_error_text']}; }}
#kbCard QLabel#kbCardStatus {{ font-size: 12px; color: {p['sb_meta']}; }}
#kbCard[kbState="stale"] QLabel#kbCardStatus {{ color: {p['kb_stale_detail']}; }}
#kbCard[kbState="error"] QLabel#kbCardStatus {{ color: {p['kb_error_detail']}; }}
#kbCard QPushButton[class="kbCardBtn"] {{
    background: {p['kb_btn_bg']};
    border: 1px solid {p['sb_card_border']};
    border-radius: 8px;
    font-size: 12px;
    color: {p['sb_row_text']};
}}
#kbCard[kbState="stale"] QPushButton[class="kbCardBtn"],
#kbCard[kbState="error"] QPushButton[class="kbCardBtn"] {{ background: {p['sb_card_bg']}; }}
#kbCard QPushButton[class="kbCardBtn"]:hover {{ border-color: {p['kb_busy_border']}; }}
#kbCard QPushButton[class="kbCardBtn"][primary="true"] {{
    background: {p['kb_primary_bg']};
    border-color: {p['kb_primary_bg']};
    color: {p['kb_primary_text']};
    font-weight: 600;
}}
#kbCard QPushButton[class="kbCardBtn"]:disabled {{
    color: {p['kb_btn_disabled']};
    background: {p['kb_btn_bg']};
    border-color: {p['sb_card_border']};
}}
#kbCard QProgressBar#kbCardBusy {{
    background: {p['kb_busy_track']};
    border: none;
    border-radius: 2px;
}}
#kbCard QProgressBar#kbCardBusy::chunk {{
    background: {p['sb_accent']};
    border-radius: 2px;
}}
/* 知识库列表为空时的说明卡 */
#sidebar #kbEmptyCard {{
    background: {p['sb_card_bg']};
    border: 1px solid {p['sb_card_border']};
    border-radius: 10px;
}}
#sidebar QLabel#kbEmptyTitle {{ font-size: 13px; font-weight: 600; color: {p['sb_row_text']}; background: transparent; }}
#sidebar QLabel#kbEmptyText {{ font-size: 12px; color: {p['sb_meta']}; background: transparent; }}
/* 分组标题：折叠箭头 + 名字；知识库的「知识库对话」是不折叠的小标题（kind=section） */
#sidebar QPushButton#projectHeader, #sidebar QPushButton#projectHeaderActive {{
    background: transparent;
    border: none;
    border-radius: 8px;
    color: {p['sb_group_text']};
    font-size: 13px;
    font-weight: 600;
    text-align: left;
    padding: 0 8px;
}}
#sidebar QPushButton#projectHeaderActive {{ color: {p['sb_group_active_text']}; }}
#sidebar QPushButton#projectHeader:hover, #sidebar QPushButton#projectHeaderActive:hover {{
    background: {p['sb_row_hover_bg']};
}}
#sidebar QPushButton#projectHeader[kind="section"], #sidebar QPushButton#projectHeaderActive[kind="section"] {{
    color: {p['sb_meta']};
    font-size: 12px;
    font-weight: 400;
    padding: 0 10px;
}}
#sidebar QPushButton#projectHeader[kind="section"]:hover, #sidebar QPushButton#projectHeaderActive[kind="section"]:hover {{
    background: transparent;
}}
#sidebar QLabel#sbChip {{
    background: {p['sb_chip_bg']};
    color: {p['sb_chip_text']};
    border-radius: 6px;
    font-size: 11px;
    padding: 1px 6px;
}}
#sidebar QLabel#sbCount, #sidebar QLabel#sbTime {{
    color: {p['sb_meta']};
    font-size: 12px;
    background: transparent;
}}
#sidebar QLabel#sbDoneDot {{
    background: {p['sb_dot_done']};
    border-radius: 3px;
}}
#sidebar QPushButton#sbMoreBtn {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0;
}}
#sidebar QPushButton#sbMoreBtn:hover {{
    background: {p['sb_card_border']};
}}
#sidebar #historyRow {{
    background: transparent;
    border-radius: 8px;
}}
/* 整行 hover 底色（HistoryRow 开了 WA_Hover）；放在 current 之前：选中底色优先 */
#sidebar #historyRow:hover {{
    background: {p['sb_row_hover_bg']};
}}
#sidebar #historyRow[current="true"] {{
    background: {p['sb_row_current_bg']};
}}
#sidebar #historyEmptyHint {{
    color: {p['sb_meta']};
    font-size: 12px;
    padding: 2px 0 6px 26px;
}}
#sidebar QPushButton#loadMoreBtn {{
    background: transparent;
    border: none;
    color: {p['sb_meta']};
    font-size: 12px;
    text-align: left;
    padding: 0 0 0 26px;
}}
#sidebar QPushButton#loadMoreBtn:hover {{
    color: {p['sb_accent']};
}}

/* 历史项（会话标题按钮）。不在 sidebar.py 里给按钮单独 setStyleSheet：自带样式表会切断
 * app 级 QToolTip 规则继承——tooltip 颜色异常；走类选择器就没这个坑。 */
QPushButton[class="historyItem"] {{
    background: transparent;
    border: none;
    color: {p['sb_row_text']};
    font-size: 13px;
    text-align: left;
    padding: 0;
}}
QPushButton[class="historyItemActive"] {{
    background: transparent;
    border: none;
    color: {p['sb_row_current_text']};
    font-size: 13px;
    font-weight: 600;
    text-align: left;
    padding: 0;
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
