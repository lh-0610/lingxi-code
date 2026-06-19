"""主聊天窗口 ChatUI。

UI/Agent 解耦：agent 线程通过 SignalBridge.emit 把渲染请求 queue 到主线程，
ChatUI 暴露给 agent 的全部公开方法（show_message / render_final_markdown /
remove_thinking_indicator / show_token_usage / show_retry）都是线程安全 wrapper。
"""
import os
import base64
import threading

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel,
    QSizePolicy, QFileDialog, QMenu, QDialog, QFrame, QProgressBar, QScrollArea,
)
from PySide6.QtCore import Qt, QSize, QTimer, QPoint
from PySide6.QtGui import (
    QFont, QIcon, QTextCursor, QColor, QTextCharFormat, QPixmap, QImage,
    QPainter, QAction, QTextDocument,
)
from langchain_core.messages import HumanMessage

from .. import agent
from .. import state
from ._base import BASE_DIR
from .theme import THEMES, build_stylesheet, load_saved_theme, save_theme_choice
from .widgets import (
    CloseConfirmDialog, DragDropTextEdit, FileCompleter,
    SignalBridge,
)
from .helpers import (
    _build_image_content_block, _make_button_icon, _make_upload_icon,
)
from .prefs import _load_ui_prefs, _save_ui_prefs
from .settings_dialog import SettingsDialog
from .confirm_bars import ConfirmBarsMixin
from .markdown_render import MarkdownRenderMixin
from .search_overlay import SearchOverlayMixin
from .sidebar import SidebarMixin
from .header import HeaderMixin


# 聊天区 HTML 文本里的彩色 emoji → icons/ 下的 SVG 文件（见 docs/emoji_inventory.md）。
# 8 个概念复用现有 *_lucide.svg，其余用合并进来的 lucide 图标。✓/✗/⚙ 等单色字符符号
# 按 README 决定保留为字体字形、不在此映射。SVG 走 currentColor，由 _inline_svg_img 按主题着色。
_EMOJI_ICON = {
    # 工具显示名（tools.py TOOL_DISPLAY_NAMES）
    "📖": "book-open.svg", "✏️": "file-pen.svg", "📝": "file-plus.svg",
    "🪄": "wand-sparkles.svg", "📂": "folder_open_lucide.svg", "⚡": "zap.svg",
    "🔍": "search.svg", "🌐": "globe.svg", "🎨": "palette.svg",
    "🧠": "brain_lucide.svg", "🗑️": "trash_lucide.svg", "📋": "clipboard-list.svg",
    "⏹": "square-stop.svg", "🗺": "map.svg", "🔀": "git-compare.svg",
    "📜": "scroll-text.svg", "🧪": "flask-conical.svg", "🔧": "wrench.svg",
    "🔌": "plug.svg",
    # 工具区还会出现的拦截/安全提示
    "⚠️": "triangle-alert.svg", "⛔": "octagon-x.svg", "🔒": "lock.svg",
    # 状态/过程（tool_result 等宽输出 + 错误/重试/图片识别）
    "📁": "folder_lucide.svg", "📄": "file_text_lucide.svg", "⏱️": "timer.svg",
    "✅": "circle-check.svg", "❌": "circle-x.svg", "🔎": "scan-search.svg",
    "🔄": "refresh_cw_lucide.svg",
}


class ChatUI(ConfirmBarsMixin, MarkdownRenderMixin, SearchOverlayMixin,
             SidebarMixin, HeaderMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("灵犀")
        self.resize(1000, 700)
        self.setMinimumSize(600, 400)
        self.theme = load_saved_theme()
        self.setStyleSheet(build_stylesheet(self.theme))
        self._apply_tooltip_style()  # QToolTip 要设到 app 级才生效

        # 设置图标
        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # 注意：self.is_generating 已迁移至 session.is_generating
        # 构造时仍初始化一份作为 fallback（不应在正常流程中使用）
        self.is_generating = False
        self._has_input = False
        self._sidebar_visible = True
        self._pending_images = []  # [(file_path, base64_data), ...]
        # 发送停止按钮图标
        self._icon_arrow = _make_button_icon(arrow=True)
        self._icon_pause = _make_button_icon(arrow=False)
        self._icon_upload = _make_upload_icon(color="#888888")
        self._settings_btn_icon = None
        self._settings_btn_icon_hover = None

        # 信号
        self.bridge = SignalBridge()
        self.bridge.append_signal.connect(self._append_html)
        self.bridge.remove_thinking.connect(self._remove_thinking)
        self.bridge.update_thinking.connect(self._update_thinking)
        self.bridge.render_md.connect(self._render_markdown)
        self.bridge.show_retry.connect(self._show_retry)
        self.bridge.finished.connect(self._on_finished_sess)
        self.bridge.token_usage.connect(self._update_token_usage)
        self.bridge.sessions_refresh.connect(self._refresh_session_list)
        self.bridge.confirm_request.connect(self._on_confirm_request)
        self.bridge.edit_confirm_request.connect(self._on_edit_confirm_request)
        self.bridge.remote_submit.connect(self._on_remote_submit)
        self.bridge.dismiss_confirm.connect(self._on_dismiss_confirm)
        self.bridge.show_plan.connect(self._render_plan_panel)

        # 让 tools.py 在 worker 线程里能找到主窗口（弹确认框用）
        state.ui_ref = self

        # 跟踪位置
        self._ai_reply_start = None
        self._thinking_start = None
        self._thinking_end = None
        self._think_block_start = None
        self._think_block_chars = 0
        self._think_block_text = ""        # 累积思考原文，用于折叠后查看
        self._code_blocks = {}             # code_idx -> raw code text
        self.setAcceptDrops(True)
        self._search_widget = None         # Ctrl+F search floating window
        self._msg_buffers = {}             # msg_idx -> AI message plain text
        # 命令 / 编辑白名单已迁移到 session.Session（会话级，见 confirm_bars）：用户"允许
        # 并记住"只影响发起确认的那个会话，不再作为 ChatUI 全局属性跨会话共享/泄漏。

        self._build_ui()
        self._refresh_session_list()
        self._restore_role_card_ui()
        self._show_empty_state()
        QTimer.singleShot(300, self._show_current_model_config_warning)

    # ── 主题工具 ──
    def _t(self, key):
        """读取当前主题的 token 颜色"""
        return THEMES[self.theme][key]

    def _toggle_theme(self):
        """白天 ↔ 夜间。立即应用到主样式表与所有内联样式 chrome；
        已渲染聊天历史保留旧色，下次重新载入会话时刷新。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        save_theme_choice(self.theme)
        self._apply_theme()

    # ── 设置弹窗 ──

    def _open_settings_menu(self):
        """齿轮按钮：弹出 VSCode 风格的设置对话框。"""
        dlg = SettingsDialog(self)
        dlg.exec()

    def _apply_tooltip_style(self):
        """把 QToolTip 颜色强行刷成跟主题一致。

        QToolTip 是顶层弹窗，**不继承主窗口 setStyleSheet**。在 Windows 上 Qt 还会
        在多种情况下绕过 app.setStyleSheet 的 QToolTip 规则，用系统默认（黑底白字）。

        所以这里**三管齐下**全设上：
          1. app.setStyleSheet(QToolTip QSS) —— 标准路径
          2. app.setPalette(ToolTipBase/Text) —— Qt 优先级最高的色板系统
          3. QToolTip.setPalette(同) —— 类级 palette 兜底
        三条都设上，无论 Qt 走哪条路解析颜色，结果都跟我们主题一致。
        """
        from PySide6.QtWidgets import QApplication, QToolTip
        from PySide6.QtGui import QColor, QPalette
        from PySide6.QtCore import QTimer
        from .theme import build_tooltip_qss

        app = QApplication.instance()
        if app is None:
            return

        bg = QColor(self._t("tooltip_bg"))
        fg = QColor(self._t("tooltip_text"))

        def _apply():
            # 1. QSS
            app.setStyleSheet(build_tooltip_qss(self.theme))
            # 2. App palette（覆盖整个 app 的 ToolTipBase/Text 角色色板）
            app_palette = app.palette()
            app_palette.setColor(QPalette.ToolTipBase, bg)
            app_palette.setColor(QPalette.ToolTipText, fg)
            app_palette.setColor(QPalette.Inactive, QPalette.ToolTipBase, bg)
            app_palette.setColor(QPalette.Inactive, QPalette.ToolTipText, fg)
            app.setPalette(app_palette)
            # 3. QToolTip 类级 palette
            tooltip_palette = QToolTip.palette()
            tooltip_palette.setColor(QPalette.ToolTipBase, bg)
            tooltip_palette.setColor(QPalette.ToolTipText, fg)
            tooltip_palette.setColor(QPalette.Inactive, QPalette.ToolTipBase, bg)
            tooltip_palette.setColor(QPalette.Inactive, QPalette.ToolTipText, fg)
            QToolTip.setPalette(tooltip_palette)

        _apply()
        # 再延迟一次：有些场景 Qt 在 init 过程中会重置 palette，延一个 tick 再覆盖
        QTimer.singleShot(0, _apply)

    def _apply_theme(self):
        """重新生成全局 QSS，并刷新所有用 setStyleSheet 直接设置的 chrome。"""
        self.setStyleSheet(build_stylesheet(self.theme))
        self._apply_tooltip_style()  # 切主题时 tooltip 也跟着刷
        # 主题按钮：文字显示当前主题（浅色 / 深色）
        if hasattr(self, "theme_btn"):
            self.theme_btn.setText("浅色" if self.theme == "light" else "深色")
            self.theme_btn.setToolTip("切到夜间模式" if self.theme == "light" else "切到白天模式")
        # 品牌字符在白天主题里隐藏，夜间显示
        if hasattr(self, "header_brand"):
            visible = self._t("brand_visible") == "true"
            self.header_brand.setVisible(visible)
            self.header_brand_dot.setVisible(visible)
        # 各内联样式区域重新涂色
        if hasattr(self, "history_widget"):
            self._style_sidebar_scroll()
        if hasattr(self, "settings_btn"):
            self._style_settings_btn()
        if hasattr(self, "chat_area"):
            self._style_chat_area()
        if hasattr(self, "empty_state"):
            self._refresh_empty_state_layout()
            self._position_empty_state()
        if hasattr(self, "model_combo"):
            self._style_model_combo()
        if hasattr(self, "think_btn"):
            self._style_think_btn()
        # 段控（计划|执行）样式走全局 QSS，setStyleSheet 重建即生效，无需单独刷
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        if hasattr(self, "role_btn"):
            self._restore_role_card_ui()
        if hasattr(self, "img_btn"):
            self._style_img_btn()
        if hasattr(self, "scroll_bottom_btn"):
            self._style_scroll_bottom_btn()
        if hasattr(self, "plan_panel"):
            self._style_plan_panel()
        # 新一轮历史项（删除按钮）会用新色
        if hasattr(self, "history_layout"):
            self._refresh_session_list()
        if hasattr(self, "project_btn"):
            self._refresh_project_indicator()
        if hasattr(self, "command_confirm_bar"):
            self._style_command_confirm_bar()
        if hasattr(self, "edit_confirm_bar"):
            self._style_edit_confirm_bar()
        if hasattr(self, "_file_completer"):
            self._apply_completer_theme()
        # 已存在的搜索浮层销毁，下次再显示用新主题重建
        if getattr(self, "_search_widget", None) is not None:
            try:
                self._search_widget.deleteLater()
            except Exception:
                pass
            self._search_widget = None

    def _show_current_model_config_warning(self):
        issues = agent.get_model_config_issues()
        if not issues:
            return
        warning_key = (agent.current_model_index, tuple(issues))
        if getattr(self, "_last_config_warning_key", None) == warning_key:
            return
        self._last_config_warning_key = warning_key
        text = "\n⚠️ " + "\n".join(issues) + "\n"
        self.show_message(text, "tool_result")
        self._show_toast(issues[0], duration=5000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 侧边栏
        self._build_sidebar()
        main_layout.addWidget(self.sidebar)

        # 主区域
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self._build_header(right_layout)
        self._build_chat_area(right_layout)
        self._build_plan_panel(right_layout)
        self._build_input_area(right_layout)
        self._build_footer(right_layout)

        main_layout.addWidget(right, 1)

    # ── 侧边栏 ──


    def _reset_render_state(self):
        """切换/新建会话前，清掉只对当前会话有意义的渲染状态"""
        self._code_blocks.clear()
        self._msg_buffers.clear()
        # 渲染游标归位（只服务实时渲染，切会话时必须清）
        self._ai_reply_start = None
        self._thinking_start = None
        self._thinking_end = None
        self._think_block_start = None
        self._think_block_chars = 0
        self._think_block_text = ""
        if hasattr(self, "chat_area"):
            self.chat_area.clear()
        if hasattr(self, 'token_usage_label'):
            self.token_usage_label.setVisible(False)
        # 计划面板跟着当前会话刷新。_reset_render_state 是新建/切换/切项目三条路径的
        # 共同漏斗，且都在 set_active(新会话) 之后才调——current_plan 已是新会话的计划。
        # （hasattr 守卫：本方法可能在 build_ui 建好 plan_panel 之前被调到）
        if hasattr(self, "plan_panel"):
            self._render_plan_panel(getattr(state, "current_plan", None) or [])

    def _is_hidden_bridge_message(self, msg):
        """内部图片识别桥接消息只给模型看，历史界面不当作用户聊天展示。"""
        if not isinstance(msg, HumanMessage) or isinstance(msg.content, list):
            return False
        content = str(msg.content or "").lstrip()
        return (
            content.startswith("[[LINGXI_INTERNAL_VISION_BRIDGE]]")
            or content.startswith("[图片识别结果，由 ")
        )

    def _next_img_name(self) -> str:
        """历史图片资源用单调递增名，不能用 id(img)——QImage 临时对象的地址会被复用，
        导致两张不同的历史图片拿到同一资源名、渲染成同一张。"""
        self._img_seq = getattr(self, "_img_seq", 0) + 1
        return f"hist_img_{self._img_seq}"

    def _redraw_chat(self):
        self.chat_area.clear()
        rendered_any = False
        history_snapshot = list(agent.chat_history)
        for msg in history_snapshot:
            if self._is_hidden_bridge_message(msg):
                continue
            if isinstance(msg, HumanMessage):
                rendered_any = True
                self._append_html("你\n", "user_label")
                # 多模态消息
                if isinstance(msg.content, list):
                    for part in msg.content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            self._append_html(part["text"] + "\n", "user_msg")
                        elif isinstance(part, dict) and part.get("type") == "image_url":
                            url = part["image_url"]["url"]
                            if url.startswith("data:image"):
                                img = QImage()
                                img.loadFromData(base64.b64decode(url.split(",", 1)[1]))
                                self.chat_area.add_image(img)
                        elif isinstance(part, dict) and part.get("type") == "image":
                            source = part.get("source", {})
                            if source.get("type") == "base64" and source.get("data"):
                                img = QImage()
                                img.loadFromData(base64.b64decode(source["data"]))
                                self.chat_area.add_image(img)
                    self._append_html("\n", "spacer")
                else:
                    self._append_html(msg.content + "\n\n", "user_msg")
            elif hasattr(msg, 'content') and msg.__class__.__name__ == "AIMessage":
                # content 可能是 str 或 list（含 thinking + text blocks）
                _ai_content = msg.content
                if isinstance(_ai_content, list):
                    # 从 content blocks 中提取纯文本部分用于显示
                    _text_parts = []
                    for _blk in _ai_content:
                        if isinstance(_blk, dict) and _blk.get('type') == 'text' and _blk.get('text'):
                            _text_parts.append(_blk['text'])
                    _ai_content = "\n".join(_text_parts)
                _has_text = bool(_ai_content and _ai_content.strip())
                _tool_names = [tc.get('name', '?') for tc in (getattr(msg, 'tool_calls', None) or [])
                               if isinstance(tc, dict)]
                ai_name = agent.get_current_role_name() or "AI"

                # 有正文就渲染正文（MessageView：起一轮 + 定格 markdown 富文本）
                if _has_text:
                    rendered_any = True
                    self._append_html(f"{ai_name}\n", "ai_label")
                    self._msg_buffers[str(len(self._msg_buffers))] = _ai_content
                    self.chat_area.finalize_markdown(self._md_to_html(_ai_content))
                    from PySide6.QtWidgets import QApplication
                    self.chat_area.add_message_actions(
                        on_copy=lambda t=_ai_content: (QApplication.clipboard().setText(t),
                                                       self._show_toast("已复制")))

                # 有工具调用就显示摘要——不管这条 AIMessage 有没有正文。
                # （之前只在"无正文"时显示，导致 MiMo "短文字 + 工具调用"同条时工具被吞，
                #   恢复的历史看不出调了哪些工具，整段很怪）
                if _tool_names:
                    rendered_any = True
                    if not _has_text:
                        self._append_html(f"{ai_name}\n", "ai_label")
                    self._append_html(f"🔧 调用了工具: {', '.join(_tool_names)}\n\n", "tool_result")
        if not rendered_any:
            self._show_empty_state()
        # 计划面板的刷新已统一到 _reset_render_state（新建/切换/切项目三路共用漏斗），
        # 且 _load_session 调 _redraw_chat 前必先调 _reset_render_state，这里无需重复渲染。

    def _show_empty_state(self):
        """聊天为空时显示欢迎态。"""
        if not hasattr(self, "chat_area") or not hasattr(self, "empty_state"):
            return
        self._empty_state_visible = True
        self.chat_area.setProperty("empty", "true")
        self.chat_area.style().unpolish(self.chat_area)
        self.chat_area.style().polish(self.chat_area)
        self.chat_area.clear()
        # 首次上手：一个可用模型都没有 → 显示"填 key"引导，藏掉点了会报错的建议 chips
        try:
            no_key = not agent.has_usable_model()
        except Exception:
            no_key = False
        if hasattr(self, "_empty_onboarding"):
            self._empty_onboarding.setVisible(no_key)
        for attr in ("_empty_title", "_empty_subtitle", "_empty_suggestions"):
            w = getattr(self, attr, None)
            if w is not None:
                w.setVisible(not no_key)
        self._position_empty_state()
        self.empty_state.show()
        self.empty_state.raise_()
        # 初次打开时 viewport 还没完成最终布局，立刻算出来的 width/height
        # 会偏小导致欢迎态居中错位；延迟几次再 reposition，覆盖到布局稳定后的尺寸。
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self._position_empty_state)

    def _clear_empty_state(self):
        if getattr(self, "_empty_state_visible", False):
            self._empty_state_visible = False
            if hasattr(self, "empty_state"):
                self.empty_state.hide()
            self.chat_area.clear()
            self.chat_area.setProperty("empty", "false")
            self.chat_area.style().unpolish(self.chat_area)
            self.chat_area.style().polish(self.chat_area)

    # ── 顶栏 ──


    def _svg_icon(self, filename, color):
        """共用 helper：把 icons/*.svg 渲染成 QIcon（支持高 DPI）。"""
        svg_path = os.path.join(BASE_DIR, "icons", filename)
        if not os.path.exists(svg_path):
            return QIcon()
        from PySide6.QtSvg import QSvgRenderer
        with open(svg_path, 'r', encoding='utf-8') as f:
            svg_tpl = f.read()
        svg_filled = svg_tpl.replace('currentColor', color)
        renderer = QSvgRenderer(svg_filled.encode('utf-8'))
        dpr = self.devicePixelRatioF() if hasattr(self, 'devicePixelRatioF') else 1.0
        px = QPixmap(int(24 * dpr), int(24 * dpr))
        px.fill(Qt.transparent)
        painter = QPainter(px)
        renderer.render(painter)
        painter.end()
        px.setDevicePixelRatio(dpr)
        return QIcon(px)

    def _inline_svg_img(self, filename, color, size=15, alt=""):
        """给 QTextBrowser HTML 链接用的内联 SVG 图标。"""
        svg_path = os.path.join(BASE_DIR, "icons", filename)
        if not os.path.exists(svg_path):
            return alt
        with open(svg_path, "r", encoding="utf-8") as f:
            svg = f.read().replace("currentColor", color)
        # 归一化 SVG 自身宽高到目标尺寸：QTextBrowser 渲染内联 SVG 时按 SVG 自带的
        # width/height（这批图标都是 24）来画，<img> 的 width/height 未必生效——不归一
        # 化图标会按 24px 渲染、比 14/15px 文字大，撑破行高看着"不在一行"。
        svg = svg.replace('width="24" height="24"', f'width="{size}" height="{size}"', 1)
        data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return (
            f'<img src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" '
            f'alt="{alt}" style="vertical-align:middle;" />'
        )

    def _emoji_to_svg_html(self, text, color, size=14):
        """把 text 里 _EMOJI_ICON 已知的 emoji 替换成内联彩色 SVG <img>，
        其余字符做 HTML 转义。返回可 insertHtml 的片段。最长 emoji 优先匹配
        （含 FE0F 变体选择符的多码点 emoji 排前），避免被前缀截断。"""
        import html as _html
        keys = sorted(_EMOJI_ICON.keys(), key=len, reverse=True)
        out = []
        i = 0
        while i < len(text):
            for emo in keys:
                if text.startswith(emo, i):
                    out.append(self._inline_svg_img(_EMOJI_ICON[emo], color, size, alt=emo))
                    i += len(emo)
                    break
            else:
                out.append(_html.escape(text[i]))
                i += 1
        return "".join(out)

    def _build_chat_area(self, parent_layout):
        # 消息流改成真控件渲染（MessageView：QScrollArea + 每轮控件树），
        # 圆角卡/思考块/工具卡走 message_view.py 的组件（QTextBrowser 画不了圆角/阴影）。
        from .message_view import MessageView
        self.chat_area = MessageView()
        self.chat_area.setObjectName("chatArea")
        self.chat_area.image_clicked.connect(self._show_image_dialog)   # 点击图片 → 放大遮罩
        self.chat_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        parent_layout.addWidget(self.chat_area, 1)
        self._build_empty_state()

        # 浮动 "回到底部" 按钮（作为主窗口浮层，避免 QTextBrowser 抢焦点/锚点）
        self.scroll_bottom_btn = QPushButton("▼", self)
        self.scroll_bottom_btn.setObjectName("scrollBottomBtn")
        self.scroll_bottom_btn.setFixedSize(36, 36)
        self.scroll_bottom_btn.setCursor(Qt.PointingHandCursor)
        self.scroll_bottom_btn.setToolTip("回到底部")
        self.scroll_bottom_btn.clicked.connect(lambda checked=False: self._scroll_to_bottom())
        self.scroll_bottom_btn.hide()
        self._style_scroll_bottom_btn()

        # 监听滚动条变化，决定是否显示浮动按钮
        sb = self.chat_area.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_changed)
        sb.rangeChanged.connect(self._on_scroll_changed)

    def _build_plan_panel(self, parent_layout=None):
        # 浮层：挂主窗口、定位到 chat_area 右上角（不进垂直布局流，避免占输入框上方整行）。
        # parent_layout 保留参数兼容旧调用，但不再 addWidget——跟 scroll_bottom_btn 同套路。
        self.plan_panel = QFrame(self)
        self.plan_panel.setObjectName("planPanel")
        self.plan_panel.setVisible(False)            # 无计划不占位
        self.plan_panel.setFixedWidth(344)           # 浮层（design_handoff 任务计划卡），少遮挡正文
        self.plan_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        _psh = QGraphicsDropShadowEffect(self)
        _psh.setBlurRadius(30)
        _psh.setXOffset(0)
        _psh.setYOffset(8)
        _psh.setColor(QColor(40, 50, 90, 18))
        self.plan_panel.setGraphicsEffect(_psh)
        lay = QVBoxLayout(self.plan_panel)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(0)

        title_row = QWidget(self.plan_panel)
        title_row.setStyleSheet("background:transparent;")
        title_row.setFixedHeight(26)
        title_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(6)
        self.plan_title_icon = QLabel(title_row)
        self.plan_title_icon.setFixedSize(17, 17)
        self.plan_title = QLabel("任务计划", title_row)
        self.plan_title.setTextFormat(Qt.RichText)
        self.plan_count = QLabel(title_row)
        self.plan_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        title_layout.addWidget(self.plan_title_icon, 0, Qt.AlignVCenter)
        title_layout.addWidget(self.plan_title, 0, Qt.AlignVCenter)
        title_layout.addStretch(1)
        title_layout.addWidget(self.plan_count, 0, Qt.AlignVCenter)
        lay.addWidget(title_row)
        lay.addSpacing(7)

        self.plan_progress = QProgressBar(self.plan_panel)
        self.plan_progress.setRange(0, 100)
        self.plan_progress.setTextVisible(False)
        self.plan_progress.setFixedHeight(6)
        lay.addWidget(self.plan_progress)
        lay.addSpacing(9)

        self._plan_items = []
        self._plan_spinner_angle = 0
        self._plan_spinner_timer = QTimer(self)
        self._plan_spinner_timer.timeout.connect(self._tick_plan_spinner)

        self.plan_scroll = QScrollArea(self.plan_panel)
        self.plan_scroll.setFrameShape(QFrame.NoFrame)
        self.plan_scroll.setWidgetResizable(False)
        self.plan_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plan_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.plan_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollBar:vertical {"
            "  background:transparent; width:6px; margin:2px 0 2px 0;"
            "}"
            "QScrollBar::handle:vertical {"
            f"  background:{self._t('scroll_btn_border')};"
            "  border-radius:3px; min-height:28px;"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
            "  height:0; border:none; background:transparent;"
            "}"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            "  background:transparent;"
            "}"
        )
        self.plan_scroll.viewport().setStyleSheet("background:transparent;")

        self.plan_body = QLabel()
        self.plan_body.setStyleSheet("background:transparent;")
        self.plan_body.setTextFormat(Qt.RichText)
        self.plan_body.setWordWrap(True)
        self.plan_body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.plan_body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.plan_scroll.setWidget(self.plan_body)
        lay.addWidget(self.plan_scroll)
        self._style_plan_panel()                     # 卡片底色/边框（浮层压在正文上必须有背景）

    def _style_plan_panel(self):
        # 复用 scroll 按钮的主题 token（明暗都有），objectName 选择器不波及子 QLabel。
        self.plan_panel.setStyleSheet(
            f"QFrame#planPanel {{"
            f"  background: {self._t('scroll_btn_bg')};"
            f"  border: 1px solid {self._t('sidebar_border')};"
            f"  border-radius: 16px;"
            f"}}"
        )
        title_color = self._t("thinking")
        muted_color = self._t("thinking_msg")
        self.plan_title.setStyleSheet(
            "background:transparent; font-size:16px; font-weight:700;"
        )
        self.plan_count.setStyleSheet(
            f"background:{self._t('thinking_msg_bg')};"
            f"color:{muted_color};"
            f"border:1px solid {self._t('scroll_btn_border')};"
            "border-radius:9px;"
            "padding:2px 7px;"
            "font-size:12px;"
        )
        self.plan_title_icon.setPixmap(
            self._svg_icon("clipboard-list.svg", title_color).pixmap(15, 15)
        )
        self.plan_progress.setStyleSheet(
            "QProgressBar {"
            f"  background: {self._t('thinking_msg_bg')};"
            "  border: none;"
            "  border-radius: 3px;"
            "  padding: 0;"
            "}"
            "QProgressBar::chunk {"
            f"  background: {title_color};"
            "  border-radius: 3px;"
            "}"
        )
        if hasattr(self, "plan_scroll"):
            self.plan_scroll.setStyleSheet(
                "QScrollArea { background:transparent; border:none; }"
                "QScrollBar:vertical {"
                "  background:transparent; width:6px; margin:2px 0 2px 0;"
                "}"
                "QScrollBar::handle:vertical {"
                f"  background:{self._t('scroll_btn_border')};"
                "  border-radius:3px; min-height:28px;"
                "}"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {"
                "  height:0; border:none; background:transparent;"
                "}"
                "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
                "  background:transparent;"
                "}"
            )

    def _position_plan_panel(self):
        """将计划浮层定位到 chat_area 右上角（先按内容 adjustSize 再贴角）。"""
        if not hasattr(self, "plan_panel"):
            return
        panel = self.plan_panel
        if panel.isVisible() and getattr(self, "_plan_items", None):
            self._fit_plan_body_height(self.plan_body.text())
            panel.setFixedHeight(self._plan_panel_target_height())
        else:
            panel.adjustSize()
        pos = self.chat_area.mapTo(self, self.chat_area.rect().topRight())
        scrollbar_width = self.chat_area.verticalScrollBar().width()
        right_clearance = scrollbar_width + 28
        panel.move(pos.x() - panel.width() - right_clearance, pos.y() + 16)

    def _schedule_plan_panel_reflow(self):
        """父布局高度变化后分阶段重算；Qt 的布局恢复不是同步完成的。"""
        if not hasattr(self, "plan_panel") or not self.plan_panel.isVisible():
            return
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self._position_plan_panel)

    def _plan_panel_target_height(self):
        """按当前正文精确计算浮层高度，避免父布局变化后保留旧高度。"""
        margins = self.plan_panel.layout().contentsMargins()
        chrome = (
            margins.top() + margins.bottom()
            + 26  # 标题行
            + 7   # 标题与进度条
            + self.plan_progress.height()
            + 9   # 进度条与正文
        )
        return chrome + self.plan_scroll.height()

    def _build_empty_state(self):
        self.empty_state = QWidget(self.chat_area.viewport())
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        layout = QVBoxLayout(self.empty_state)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        logo = QLabel("灵犀<span style='color:#f0824a;'>.</span>")
        logo.setObjectName("emptyLogo")
        logo.setTextFormat(Qt.RichText)
        logo.setAlignment(Qt.AlignCenter)

        title = QLabel("今天想聊点什么？")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        self._empty_title = title

        subtitle = QLabel("灵犀时刻准备为你提供帮助")
        subtitle.setObjectName("emptySubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        self._empty_subtitle = subtitle

        layout.addWidget(logo)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        # 首次上手引导：一个可用模型都没有时显示（默认隐藏，_show_empty_state 按 has_usable_model 切换）。
        # 复用 emptyTitle/emptySubtitle/emptySuggestion 的 objectName → 直接套用主题样式，无需改 theme。
        self._empty_onboarding = QWidget()
        ob_layout = QVBoxLayout(self._empty_onboarding)
        ob_layout.setContentsMargins(0, 4, 0, 0)
        ob_layout.setSpacing(12)
        ob_hint = QLabel("还没有可用模型")
        ob_hint.setObjectName("emptyTitle")
        ob_hint.setAlignment(Qt.AlignCenter)
        ob_btn = QPushButton("打开设置")
        ob_btn.setObjectName("emptySuggestion")
        ob_btn.setIcon(self._svg_icon("settings_lucide.svg", self._t("brand_color")))
        ob_btn.setIconSize(QSize(16, 16))
        ob_btn.setCursor(Qt.PointingHandCursor)
        ob_btn.clicked.connect(self._open_settings_menu)
        ob_note = QLabel("配置一个 API Key 后重启应用即可开始")
        ob_note.setObjectName("emptySubtitle")
        ob_note.setAlignment(Qt.AlignCenter)
        ob_layout.addWidget(ob_hint)
        ob_layout.addWidget(ob_btn, 0, Qt.AlignHCenter)
        ob_layout.addWidget(ob_note)
        layout.addWidget(self._empty_onboarding, 0, Qt.AlignHCenter)
        self._empty_onboarding.hide()
        self._empty_onboarding_btn = ob_btn

        # 建议按钮（chips）：副标题下方水平排一行，引导用户点选
        suggestions = QWidget()
        suggestions.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        sug_layout = QHBoxLayout(suggestions)
        sug_layout.setContentsMargins(0, 20, 0, 0)
        sug_layout.setSpacing(12)
        sug_layout.setAlignment(Qt.AlignHCenter)
        self._empty_suggestions_layout = sug_layout
        self._empty_suggestion_buttons = []
        for icon_file, text, compact_text in [
            ("sparkles_lucide.svg", "帮我生成一张插画", "生成插画"),
            ("file_text_lucide.svg", "总结一下这篇文档", "总结文档"),
            ("code_lucide.svg", "解释这段代码的逻辑", "解释代码"),
        ]:
            btn = QPushButton(text)
            btn.setIcon(self._svg_icon(icon_file, self._t("text_dim")))
            btn.setIconSize(QSize(16, 16))
            btn.setObjectName("emptySuggestion")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumWidth(0)
            btn.clicked.connect(lambda checked=False, t=text: self._use_suggestion(t))
            sug_layout.addWidget(btn)
            self._empty_suggestion_buttons.append((btn, icon_file, text, compact_text))
        layout.addWidget(suggestions, 0, Qt.AlignHCenter)
        self._empty_suggestions = suggestions

        self.empty_state.hide()

    def _refresh_empty_state_layout(self, viewport_width=None):
        """刷新空状态的图标色与窄屏文案，避免首屏 chip 在小窗口里挤出视口。"""
        if viewport_width is None and hasattr(self, "chat_area"):
            viewport_width = self.chat_area.viewport().width()
        compact = bool(viewport_width and viewport_width < 620)
        if hasattr(self, "_empty_suggestions_layout"):
            self._empty_suggestions_layout.setSpacing(8 if compact else 12)

        btn = getattr(self, "_empty_onboarding_btn", None)
        if btn is not None:
            btn.setIcon(self._svg_icon("settings_lucide.svg", self._t("brand_color")))
            btn.setIconSize(QSize(16, 16))

        for item in getattr(self, "_empty_suggestion_buttons", []):
            btn, icon_file, full_text, compact_text = item
            btn.setText(compact_text if compact else full_text)
            btn.setIcon(self._svg_icon(icon_file, self._t("text_dim")))
            btn.setIconSize(QSize(15 if compact else 16, 15 if compact else 16))
            btn.setMinimumHeight(36 if compact else 40)
            btn.setMaximumWidth(116 if compact else 240)

    def _position_empty_state(self):
        if not hasattr(self, "empty_state"):
            return
        vp = self.chat_area.viewport()
        self._refresh_empty_state_layout(vp.width())
        # 让 widget 自然 sizeToContent
        self.empty_state.adjustSize()
        sh = self.empty_state.sizeHint()
        w, h = sh.width(), sh.height()

        # chat_area 的 CSS padding 是 28/28/18/52（左右不对称），
        # 直接用 viewport 中心居中会偏右。
        # 改用 chat_area 的几何中心做视觉中心，再换算成 viewport 内的坐标。
        chat_w = self.chat_area.width()
        chat_h = self.chat_area.height()
        vp_offset = vp.mapTo(self.chat_area, vp.rect().topLeft())

        target_x = chat_w // 2 - vp_offset.x() - w // 2
        # 垂直方向：在 chat_area 几何中心略偏下（+40 把内容压向下方视觉重心）
        target_y = chat_h // 2 - vp_offset.y() - h // 2

        x = max(0, target_x)
        y = max(34, target_y)
        self.empty_state.setGeometry(x, y, w, h)

    def _use_suggestion(self, text):
        self.entry.setPlainText(text)
        self.entry.setFocus()
        self._check_input_state()

    def _style_chat_area(self):
        self.chat_area.setStyleSheet(
            f"QTextBrowser {{"
            f"  background: {self._t('chat_bg')}; border: none; color: {self._t('chat_text')};"
            f"  padding: 28px 28px 18px 52px;"
            f"  selection-background-color: {self._t('chat_sel_bg')};"
            f"  selection-color: {self._t('chat_sel_text')};"
            f"}}"
            f"QScrollBar:vertical {{ width: 6px; background: transparent; margin: 4px 2px 4px 0px; }}"
            f"QScrollBar::handle:vertical {{ background: {self._t('chat_scroll_handle')}; border-radius: 3px; min-height: 30px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {self._t('chat_scroll_handle_hover')}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}"
        )

    def _style_scroll_bottom_btn(self):
        self.scroll_bottom_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {self._t('scroll_btn_bg')};"
            f"  border: 1px solid {self._t('scroll_btn_border')};"
            f"  border-radius: 18px;"
            f"  color: {self._t('scroll_btn_icon')};"
            f"  font-size: 16px;"
            f"  font-weight: bold;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: {self._t('scroll_btn_hover_bg')};"
            f"}}"
        )

    def _position_scroll_btn(self):
        """将浮动按钮定位到 chat_area 的右下角"""
        if not hasattr(self, 'scroll_bottom_btn'):
            return
        btn = self.scroll_bottom_btn
        pos = self.chat_area.mapTo(self, self.chat_area.rect().bottomRight())
        btn.move(pos.x() - btn.width() - 20, pos.y() - btn.height() - 20)

    def _on_scroll_changed(self):
        """滚动位置变化时，决定是否显示浮动按钮"""
        sb = self.chat_area.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 30
        if at_bottom:
            self.scroll_bottom_btn.hide()
        else:
            self.scroll_bottom_btn.show()
            self.scroll_bottom_btn.raise_()
            self._position_scroll_btn()

    def _scroll_to_bottom(self):
        """滚动到聊天区底部。

        只操作滚动条，不移动 QTextBrowser 文本光标。移动光标会让正文里
        获得焦点的 Copy 链接被框选，并可能把视口拉回该链接位置。
        """
        def force_bottom(final=False):
            sb = self.chat_area.verticalScrollBar()
            self.chat_area.clearFocus()
            self.scroll_bottom_btn.setFocus()
            sb.setValue(sb.maximum())
            sb.setSliderPosition(sb.maximum())
            sb.triggerAction(sb.SliderAction.SliderToMaximum)
            if final and sb.value() >= sb.maximum() - 30:
                self.scroll_bottom_btn.hide()
            elif hasattr(self, "scroll_bottom_btn"):
                self._position_scroll_btn()
                self.scroll_bottom_btn.raise_()

        for delay in (0, 16, 50, 120, 250, 400, 700):
            QTimer.singleShot(delay, lambda final=(delay == 700): force_bottom(final))


    # ── 输入区 ──

    def _build_input_area(self, parent_layout):
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(48, 8, 48, 12)
        self.input_wrapper = wrapper
        self.input_wrapper_layout = wrapper_layout

        # 图片预览区
        self.image_preview_area = QWidget()
        self.image_preview_area.setVisible(False)
        self.image_preview_layout = QHBoxLayout(self.image_preview_area)
        self.image_preview_layout.setContentsMargins(8, 4, 8, 0)
        self.image_preview_layout.setSpacing(6)
        self.image_preview_layout.addStretch()
        wrapper_layout.addWidget(self.image_preview_area)

        # 命令确认条（默认隐藏；AI 想 run_command 时由 _on_confirm_request 显示）
        self._build_command_confirm_bar()
        wrapper_layout.addWidget(self.command_confirm_bar, 0, Qt.AlignHCenter)

        # edit_file diff 预览卡（默认隐藏；AI 想改文件时由 _on_edit_confirm_request 显示）
        self._build_edit_confirm_bar()
        wrapper_layout.addWidget(self.edit_confirm_bar, 0, Qt.AlignHCenter)

        # 圆角容器（+ 投影,对齐 design_handoff composer 卡）
        container = QWidget()
        container.setObjectName("inputContainer")
        self.input_container = container
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        _sh = QGraphicsDropShadowEffect(self)
        _sh.setBlurRadius(20)
        _sh.setXOffset(0)
        _sh.setYOffset(4)
        _sh.setColor(QColor(40, 50, 90, 16))
        container.setGraphicsEffect(_sh)
        container.setFixedWidth(920)
        container.setMinimumHeight(104)
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(10, 6, 10, 6)
        container_layout.setSpacing(6)

        # 输入框（左侧留 padding 给加号按钮）
        self.entry = DragDropTextEdit()
        self.entry.setObjectName("inputEdit")
        self.entry.setPlaceholderText("向灵犀提问…")
        entry_font = QFont("Microsoft YaHei")
        entry_font.setPixelSize(16)
        entry_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        entry_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.entry.setFont(entry_font)
        self.entry.setMaximumHeight(132)
        self.entry.setMinimumHeight(82)
        self.entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.entry.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entry.textChanged.connect(self._on_input_change)
        container_layout.addWidget(self.entry, 1)

        # "+" 按钮（悬浮在输入框左下角）——点击弹菜单：上传图片 / 导入项目
        # 之所以变量名仍叫 img_btn 是为了不动 eventFilter 里的几处兼容代码
        self.img_btn = QPushButton(self.entry)
        self.img_btn.setToolTip("上传图片 / 导入项目")
        self.img_btn.setCursor(Qt.PointingHandCursor)
        self.img_btn.setFixedSize(28, 28)
        self._style_img_btn()
        self.img_btn.clicked.connect(self._show_plus_menu)
        self.img_btn.move(4, 40)

        # img_btn 创建完成后再装事件过滤器，避免 eventFilter 提前触发时引用未定义属性
        self.entry.installEventFilter(self)
        self.img_btn.installEventFilter(self)

        # @文件名补全器
        self._file_completer = FileCompleter(self)
        self._file_completer.item_selected.connect(self._on_file_completer_selected)
        self._file_completer.lister = self._list_project_dir  # 逐层浏览：列单层目录的回调
        self._file_completer.reposition = self._position_completer  # 高度变后重定位（底部贴输入框）
        self._file_completer_files = None  # 缓存的文件列表
        self._file_completer_cache_key = None  # 缓存对应的项目路径
        self._apply_completer_theme()  # 初始就给 delegate 设好 text_color/sel_bg，否则选中项白字看不清
        self._file_completer.hide()    # 必须显式隐藏：它是主窗口子控件，不 hide 的话窗口一 show
                                       # 就会空着冒出来（停在 0,0，缩放窗口时被 _resize_input_container 重定位到输入框上方）

        # 发送按钮
        self.send_btn = QPushButton()
        self.send_btn.setIcon(self._icon_arrow)
        self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send_click)
        # 底部留 8px 间距
        btn_wrapper = QWidget()
        btn_wrapper_layout = QVBoxLayout(btn_wrapper)
        btn_wrapper_layout.setContentsMargins(0, 0, 8, 6)
        btn_wrapper_layout.setSpacing(0)
        btn_wrapper_layout.addStretch()
        btn_wrapper_layout.addWidget(self.send_btn)
        container_layout.addWidget(btn_wrapper, 0, Qt.AlignBottom)
        # 初始化样式
        self._update_btn_state("disabled")

        wrapper_layout.addWidget(container, 0, Qt.AlignHCenter)
        parent_layout.addWidget(wrapper)
        QTimer.singleShot(0, self._resize_input_container)

    def _resize_input_container(self):
        if not hasattr(self, "input_container") or not hasattr(self, "chat_area"):
            return
        viewport_width = self.chat_area.viewport().width()
        if viewport_width < 520:
            side_margin = 16
        elif viewport_width < 760:
            side_margin = 28
        else:
            side_margin = 48
        if hasattr(self, "input_wrapper_layout"):
            self.input_wrapper_layout.setContentsMargins(side_margin, 8, side_margin, 12)
        available = max(260, viewport_width - side_margin * 2)
        width = max(260, min(980, available))
        self.input_container.setFixedWidth(width)
        if hasattr(self, "command_confirm_bar"):
            self.command_confirm_bar.setFixedWidth(width)
        if hasattr(self, "edit_confirm_bar"):
            self.edit_confirm_bar.setFixedWidth(width)
        # 输入框宽度变了：补全浮窗若开着，等布局刷完（singleShot 0）再按 input_container
        # 的新尺寸/位置重对齐——立即调会拿到布局未稳定的旧几何，导致位置偏
        if hasattr(self, "_file_completer") and self._file_completer.isVisible():
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(0, self._position_completer)


    def _refresh_project_indicator(self):
        """根据当前项目刷新底栏项目按钮的文本、图标、tooltip。
        在以下时机调用：__init__、_switch_project、_remove_current_project、_apply_theme、
        切换隔离模式。project_btn 现在位于底栏状态条左侧（见 _build_footer）。
        """
        if not hasattr(self, "project_btn"):
            return
        from .. import projects as _projects
        from .. import session as _sess
        current = _projects.get_current()
        # 会话级隔离状态
        active = _sess.get_active()
        is_isolated = bool(getattr(active, "worktree", None))
        if current:
            display = self._abbreviate_path(current, max_chars=60)
            if is_isolated:
                self.project_btn.setText(f"🔒 当前项目 {display}")
                self.project_btn.setToolTip(
                    f"🔒 隔离模式 · worktree: {active.worktree}\n"
                    f"项目：{current}\n（点击切换 / 添加 / 移除项目）"
                )
            else:
                self.project_btn.setText(f"当前项目 {display}")
                self.project_btn.setToolTip(f"当前项目：{current}\n（点击切换 / 添加 / 移除项目）")
        else:
            self.project_btn.setText("无项目 · 全局工作区")
            self.project_btn.setToolTip("当前不在任何项目中\n（点击选择 / 添加项目）")
        # 内联样式（用 footer 的颜色 token，跟随主题）
        self.project_btn.setStyleSheet(
            f"QPushButton#projectIndicatorBtn {{"
            f"  background: transparent; border: 1px solid transparent; border-radius: 8px;"
            f"  color: {self._t('text_dim')}; font-size: 11px; padding: 3px 10px;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton#projectIndicatorBtn:hover {{"
            f"  background: {self._t('history_hover_bg')};"
            f"  color: {self._t('text')};"
            f"  border-color: {self._t('sidebar_border')};"
            f"}}"
        )
        # 隔离按钮可见性跟随"有无项目"(隔离=git worktree,必须有项目)。本方法在 __init__、
        # 切项目、切隔离时都会调,所以启动恢复了默认项目时这里就让隔离按钮出现——修"启动有
        # 默认项目却没有隔离按钮,要切一次会话才冒出来"。
        if hasattr(self, "isolation_btn"):
            self.isolation_btn.setVisible(bool(current))
            if current:
                self._style_isolation_btn(active=is_isolated)

    @staticmethod
    def _abbreviate_path(path, max_chars=60):
        """路径太长时做中部省略，让"盘符 + 项目名"两头都看得见。"""
        if not path or len(path) <= max_chars:
            return path
        keep_head = max_chars // 2 - 2
        keep_tail = max_chars - keep_head - 3
        return f"{path[:keep_head]}...{path[-keep_tail:]}"

    def _build_footer(self, parent_layout):
        """底栏状态条：左=当前项目（可点切换），右=Token 占用。"""
        from PySide6.QtWidgets import QHBoxLayout, QWidget as _W
        footer_widget = _W()
        footer_widget.setFixedHeight(30)
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(18, 0, 18, 2)
        footer_layout.setSpacing(0)

        # 左：当前项目按钮（点击弹项目切换菜单，与侧栏菜单一致）。
        # 用 QPushButton 是因为它天然支持 hover / cursor / clicked，比 QLabel 干净。
        self.project_btn = QPushButton()
        self.project_btn.setObjectName("projectIndicatorBtn")
        self.project_btn.setCursor(Qt.PointingHandCursor)
        self.project_btn.clicked.connect(self._show_project_menu)
        footer_layout.addWidget(self.project_btn, 0, Qt.AlignVCenter | Qt.AlignLeft)

        footer_layout.addStretch(1)

        # 右：Token 占用
        self.token_usage_label = QLabel("")
        self.token_usage_label.setObjectName("tokenUsageLabel")
        self.token_usage_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        footer_layout.addWidget(self.token_usage_label, 0, Qt.AlignVCenter | Qt.AlignRight)

        parent_layout.addWidget(footer_widget)
        self._refresh_project_indicator()

    # ── 事件处理 ──

    def eventFilter(self, obj, event):
        # 确认条按键（1/2/3/Esc）派到 mixin 处理
        if self._handle_confirm_bar_keys(obj, event):
            return True

        if hasattr(self, 'settings_btn') and obj == self.settings_btn and self._settings_btn_icon:
            if event.type() == event.Type.Enter:
                self.settings_btn.setIcon(self._settings_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.settings_btn.setIcon(self._settings_btn_icon)
                return False

        # img_btn hover 图标切换
        if hasattr(self, 'img_btn') and obj == self.img_btn and hasattr(self, '_img_btn_icon'):
            if event.type() == event.Type.Enter:
                self.img_btn.setIcon(self._img_btn_icon_hover)
                return False
            elif event.type() == event.Type.Leave:
                self.img_btn.setIcon(self._img_btn_icon)
                return False
        """Enter 发送，Shift+Enter 换行，Ctrl+V 粘贴图片"""
        if not hasattr(self, 'entry'):
            return super().eventFilter(obj, event)

        if obj == self.entry and event.type() == event.Type.Resize:
            y = self.entry.height() - 40
            self.img_btn.move(4, y)
        if obj == self.entry and event.type() == event.Type.KeyPress:
            # @文件补全浮窗激活时，拦截导航/确认/取消键，不触发发送或换行
            if (hasattr(self, '_file_completer')
                    and self._file_completer.isVisible()):
                key = event.key()
                if key == Qt.Key_Up:
                    self._file_completer.navigate_up()
                    return True
                if key == Qt.Key_Down:
                    self._file_completer.navigate_down()
                    return True
                if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                    self._file_completer.confirm_selection()
                    return True
                if key == Qt.Key_Escape:
                    self._file_completer.hide()
                    return True
            if event.key() == Qt.Key_Return and not event.modifiers() & Qt.ShiftModifier:
                if self._has_input or self._pending_images:
                    self._send_message()
                return True
            # Ctrl+V 粘贴图片
            if event.key() == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
                from PySide6.QtWidgets import QApplication
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime.hasImage():
                    img = clipboard.image()
                    if not img.isNull():
                        self._add_image_from_qimage(img)
                        return True
                # 粘贴的是文件路径（如从资源管理器复制的文件）
                if mime.hasUrls():
                    handled = False
                    for url in mime.urls():
                        path = url.toLocalFile()
                        if path and path.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')):
                            self._add_pending_image(path)
                            handled = True
                    if handled:
                        return True
                # 普通文本粘贴，走默认处理
        return super().eventFilter(obj, event)

    def _add_image_from_qimage(self, qimage):
        """从 QImage（剪贴板截图等）添加待发送图片"""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=os.environ.get("TEMP", "."))
        tmp.close()
        qimage.save(tmp.name, "PNG")
        self._add_pending_image(tmp.name)

    def _on_input_change(self):
        text = self.entry.toPlainText().strip()
        has_input = bool(text) or bool(self._pending_images)
        if has_input != self._has_input:
            self._has_input = has_input
            from .. import session as _session
            if not _session.get_active().is_generating:
                self._update_btn_state("enabled" if has_input else "disabled")

        # 自动调整高度（不低于 80）
        doc_height = self.entry.document().size().height() + 16
        self.entry.setMinimumHeight(int(min(max(doc_height, 80), 150)))

        # @文件补全触发
        self._check_at_mention()

    # ── @文件名补全 ──

    def _check_at_mention(self):
        """检测输入框光标前是否有 @文件名 上下文，有则弹出补全浮窗。"""
        if not hasattr(self, '_file_completer'):
            return
        mention = self._get_active_mention()
        if mention is not None:
            _pos, partial = mention
            if not self._file_completer.isVisible():
                self._file_completer.open_root()   # 首次打 @：从项目根开始逐层浏览
            self._file_completer.filter_and_show(partial)
            self._position_completer()
        else:
            self._file_completer.hide()

    def _get_active_mention(self):
        """检测光标前是否有未完成的 @文件名 提及。

        规则：@ 前是行首/空白/非字母数字（排除 email 和装饰器），
        @ 后到光标间是路径字符（不含空白和 @），且后面要么是光标末尾，
        要么是非路径字符（已完成的 @path 后面跟空格则不算）。

        返回 (at_pos, partial_path) 或 None。
        """
        cursor = self.entry.textCursor()
        pos = cursor.position()
        if pos == 0:
            return None
        text_before = self.entry.toPlainText()[:pos]
        # 从光标往前找最近的 @
        idx = text_before.rfind('@')
        if idx < 0:
            return None
        # @ 前必须是行首 / 空白 / 非字母数字（排除 user@domain 和 @decorator）
        if idx > 0 and text_before[idx - 1].isalnum():
            return None
        partial = text_before[idx + 1:]
        # @ 后不能包含空白或 @
        if ' ' in partial or '\t' in partial or '\n' in partial or '@' in partial:
            return None
        return (idx, partial)

    def _list_project_dir(self, rel_dir):
        """列出 项目根/rel_dir 的直接子项 [(name, is_dir)]，跳噪声目录，文件夹优先。
        逐层浏览：@ 浮窗每次只列一层，选文件夹进入下一层。"""
        from .. import session as _session
        project_root = _session.current_project() or os.getcwd()  # 会话级，与工具 cwd 同源
        target = os.path.join(project_root, rel_dir) if rel_dir else project_root
        ignore = {
            ".git", ".hg", ".svn", "node_modules", "bower_components",
            "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            ".venv", "venv", "env", ".env",
            "build", "dist", "target", "out",
            ".next", ".nuxt", ".idea", ".vscode",
        }
        try:
            names = os.listdir(target)
        except Exception:
            return []
        out = []
        for n in names:
            if n in ignore:
                continue
            is_dir = os.path.isdir(os.path.join(target, n))
            out.append((n, is_dir))
        out.sort(key=lambda t: (not t[1], t[0].lower()))  # 文件夹优先，再按名
        return out

    def _position_completer(self):
        """把补全浮窗定位到输入框【上方】，宽度/左右与输入框外框对齐。
        基准用 input_container（带圆角的可见外框），不能用 entry——entry 是框内的
        文本控件、比外框窄。浮窗是主窗口子控件，用相对主窗口的本地坐标 move + raise_。"""
        anchor = getattr(self, "input_container", self.entry)
        self._file_completer.setFixedWidth(anchor.width())  # 与输入框外框等宽
        ph = self._file_completer.height()
        top_left = anchor.mapTo(self, anchor.rect().topLeft())
        self._file_completer.move(top_left.x(), top_left.y() - ph - 4)
        self._file_completer.raise_()

    def _apply_completer_theme(self):
        """按当前主题给文件补全器涂色：外框 + hover 走 QSS，文字/选中色交给 delegate
        （delegate 自绘文字，所以颜色不能只靠 QSS 的 item color）。"""
        frame_bg = self._t("input_bg")
        frame_border = self._t("input_border")
        hover_bg = self._t("history_hover_bg")
        text_color = self._t("input_text")
        sel_bg = self._t("history_active_bg")
        icon_color = self._t("brand_color")
        self._file_completer.setStyleSheet(
            f"FileCompleter {{ background: {frame_bg}; border: 1px solid {frame_border}; "
            f"border-radius: 12px; padding: 4px; }}")
        self._file_completer.list_widget.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border-radius: 6px; }"
            f"QListWidget::item:hover {{ background: {hover_bg}; }}")
        dlg = self._file_completer.list_widget.itemDelegate()
        if dlg is not None and hasattr(dlg, "text_color"):
            dlg.text_color = text_color
            dlg.sel_bg = sel_bg
            dlg.highlight_color = self._t("brand_color")
            dlg.folder_icon = self._svg_icon("folder_lucide.svg", icon_color).pixmap(16, 16)
            dlg.file_icon = self._svg_icon("file_text_lucide.svg", text_color).pixmap(16, 16)

    def _on_file_completer_selected(self, relative_path: str):
        """补全浮窗选中文件 → 替换输入框中的 @partial 为 @相对路径。"""
        mention = self._get_active_mention()
        if mention is None:
            return
        at_pos, _partial = mention
        cursor = self.entry.textCursor()
        # 选中从 @ 位置到当前光标之间的文本
        cursor.setPosition(at_pos)
        cursor.setPosition(self.entry.textCursor().position(), QTextCursor.KeepAnchor)
        # 替换为 "@相对路径 "，并给引用上强调色 + 加粗（视觉标识这是文件引用）；
        # 随后的空格用默认格式插入，避免用户接着打字时文字继续带色
        ref_fmt = QTextCharFormat()
        ref_fmt.setForeground(QColor("#3b82f6"))
        ref_fmt.setFontWeight(700)
        self.entry.blockSignals(True)
        cursor.insertText(f"@{relative_path}", ref_fmt)
        cursor.insertText(" ", QTextCharFormat())
        self.entry.setTextCursor(cursor)                     # 同步光标到空格后
        self.entry.setCurrentCharFormat(QTextCharFormat())   # 重置输入框当前格式，后续打字恢复默认色
        self.entry.blockSignals(False)
        self.entry.setFocus()

    def _expand_file_mentions(self, text: str) -> str:
        """扫描 @相对路径，【不注入文件内容】，而是末尾追加强提示，让 AI 自己用
        read_file / list_directory 工具读取（历史干净 + 与工具体系一致）。"""
        import re as _re
        from .. import session as _session
        project_root = _session.current_project() or os.getcwd()  # 会话级，与工具 cwd 同源
        pattern = _re.compile(r'(?<!\S)@([^\s@]+)')
        refs = []
        for m in pattern.finditer(text):
            rel_path = m.group(1)
            abs_path = os.path.join(project_root, rel_path)
            if os.path.isdir(abs_path):
                refs.append((rel_path, "目录", "list_directory"))
            elif os.path.isfile(abs_path):
                refs.append((rel_path, "文件", "read_file"))
        if not refs:
            return text
        lines = [f"  - {rel}（{kind}）→ 用 {tool} 读取" for rel, kind, tool in refs]
        hint = (
            "\n\n[用户用 @ 引用了以下文件/目录，请【务必先调用对应工具读取其内容】"
            "再据此回答，不要凭空作答]：\n" + "\n".join(lines)
        )
        return text + hint

    def _force_stop_generation(self, wait: bool = False, timeout: float = 3.0):
        """强制停止当前生成，立即更新 UI 状态。

        与 _on_send_click 中的 stop 不同：这里会同步把 is_generating 置 False
        并立即刷新按钮/输入框，这样调用方（切会话 / 新对话等）可以继续往下执行，
        而不用等 worker 线程退出。
        """
        from .. import session as _session
        sess = _session.get_active()
        if not sess.is_generating:
            return True
        thread = getattr(sess, "thread", None)
        state.stop_flag = True
        self._release_pending_confirm()
        self._release_pending_edit()
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                return False
        # 立即标记生成结束——_on_finished 再被触发时会跳过重复处理
        sess.is_generating = False
        if sess.thread is thread:
            sess.thread = None
        self._ai_reply_start = None
        # 对齐 _on_finished 的按钮恢复（输入框有内容则 enabled）。
        self._update_btn_state("enabled" if self._has_input else "disabled")
        return True

    def _on_send_click(self):
        from .. import session as _session
        sess = _session.get_active()
        if sess.is_generating:
            # wait=True：join 旧 worker。若超时未退则保持 is_generating=True、按钮停"停止"态，
            # 用户无法立刻再发 → 杜绝"旧 worker 还在跑就起新 worker"双开乱写同一会话。
            self._force_stop_generation(wait=True)
        elif self._has_input or self._pending_images:
            self._send_message()

    def _insert_image_path(self, path):
        """从本地路径加载图片并插入聊天区（MessageView 图片块）。"""
        if not path or not os.path.exists(path):
            return
        img = QImage(path)
        if img.isNull():
            return
        self.chat_area.add_image(img, max_w=480)

    def _insert_images_in_chat(self, images):
        """在聊天区插入用户发送的图片缩略图（MessageView 图片块）。"""
        for path, _b64 in images:
            img = QImage(path)
            if not img.isNull():
                self.chat_area.add_image(img, max_w=480)

    def _update_btn_state(self, state):
        self.send_btn.setProperty("state", state)
        if state == "stop":
            self.send_btn.setIcon(self._icon_pause)
        else:
            self.send_btn.setIcon(self._icon_arrow)
        self.send_btn.setIconSize(QSize(16, 16))
        # 强制刷新样式
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        self._resize_input_container()
        self._position_empty_state()
        if hasattr(self, 'scroll_bottom_btn'):
            self._position_scroll_btn()
        if hasattr(self, 'plan_panel') and self.plan_panel.isVisible():
            self._position_plan_panel()
        self._refresh_header_compactness()


    def _pick_image(self):
        from .. import session as _session
        if _session.get_active().is_generating:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)"
        )
        for path in paths:
            self._add_pending_image(path)

    def _show_plus_menu(self):
        """点 + 按钮：弹菜单选「上传图片 / 导入项目」。"""
        from .. import session as _session
        if _session.get_active().is_generating:
            return
        menu = QMenu(self)

        a_img = QAction("上传图片", menu)
        a_img.setIcon(self._svg_icon("image_lucide.svg", self._t("menu_text")))
        a_img.triggered.connect(self._pick_image)
        menu.addAction(a_img)

        a_proj = QAction("导入项目", menu)
        a_proj.setIcon(self._svg_icon("folder_open_lucide.svg", self._t("menu_text")))
        a_proj.triggered.connect(self._add_project)
        menu.addAction(a_proj)

        # 菜单弹在按钮上方（避免遮挡输入框）
        anchor = self.img_btn.mapToGlobal(self.img_btn.rect().topLeft())
        size_hint = menu.sizeHint()
        menu.exec(anchor - QPoint(0, size_hint.height()))

    def _add_pending_image(self, path):
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            return

        self._pending_images.append((path, b64))
        self._refresh_image_preview()
        self._check_input_state()

    def _refresh_image_preview(self):
        # 清空旧预览
        while self.image_preview_layout.count() > 1:
            item = self.image_preview_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for i, (path, _b64) in enumerate(self._pending_images):
            thumb = QWidget()
            thumb_layout = QVBoxLayout(thumb)
            thumb_layout.setContentsMargins(0, 0, 0, 0)
            thumb_layout.setSpacing(2)

            # 缩略图
            lbl = QLabel()
            pix = QPixmap(path).scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            lbl.setPixmap(pix)
            lbl.setFixedSize(64, 64)
            lbl.setStyleSheet(
                f"border: 1px solid {self._t('img_thumb_border')}; border-radius: 8px; padding: 2px; "
                f"background: {self._t('img_thumb_bg')};"
            )
            lbl.setAlignment(Qt.AlignCenter)
            thumb_layout.addWidget(lbl)

            # 删除按钮
            del_btn = QPushButton("×")
            del_btn.setFixedSize(20, 20)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setStyleSheet(
                f"QPushButton {{ background: {self._t('img_del_bg')}; color: {self._t('img_del_text')}; border: none; "
                f"border-radius: 10px; font-size: 12px; font-weight: bold; }}"
                f"QPushButton:hover {{ background: {self._t('img_del_hover_bg')}; }}"
            )
            idx = i
            del_btn.clicked.connect(lambda checked=False, ii=idx: self._remove_pending_image(ii))
            thumb_layout.addWidget(del_btn, 0, Qt.AlignCenter)

            self.image_preview_layout.insertWidget(self.image_preview_layout.count() - 1, thumb)

        self.image_preview_area.setVisible(len(self._pending_images) > 0)

    def _remove_pending_image(self, index):
        if 0 <= index < len(self._pending_images):
            self._pending_images.pop(index)
            self._refresh_image_preview()
            self._check_input_state()

    def _check_input_state(self):
        """文本或图片有任一存在即可发送"""
        text = self.entry.toPlainText().strip()
        has_input = bool(text) or bool(self._pending_images)
        if has_input != self._has_input:
            self._has_input = has_input
            from .. import session as _session
            if not _session.get_active().is_generating:
                self._update_btn_state("enabled" if has_input else "disabled")

    # ── 发送消息 ──

    def submit_from_remote(self, text: str):
        """遥控消息注入入口（可从任意线程调用，通过 Signal 跨线程）。"""
        self.bridge.remote_submit.emit(text)

    def _on_remote_submit(self, text: str):
        """远程消息注入槽（主线程）。"""
        from .. import session as _session
        if not text or _session.get_active().is_generating:
            return
        state.remote_session = True
        self._do_send(text)

    def _send_message(self):
        text = self.entry.toPlainText().strip()
        images = self._pending_images[:]
        from .. import session as _session
        if (not text and not images) or _session.get_active().is_generating:
            return
        # GUI 专属清理
        self.entry.clear()
        self._pending_images.clear()
        self._refresh_image_preview()
        self._has_input = False
        self._do_send(text, images)

    def _append_user_text(self, text):
        """显示用户消息：@文件引用渲染成靛蓝加粗，其余普通（MessageView 用户气泡）。"""
        import re as _re
        import html as _html
        parts = []
        pat = _re.compile(r'(?<!\S)@[^\s@]+')
        pos = 0
        for m in pat.finditer(text):
            if m.start() > pos:
                parts.append(_html.escape(text[pos:m.start()]))
            parts.append(f'<b style="color:#5b6cf0;">{_html.escape(m.group(0))}</b>')
            pos = m.end()
        if pos < len(text):
            parts.append(_html.escape(text[pos:]))
        self.chat_area.append_user_html("".join(parts).replace("\n", "<br>"))

    def _do_send(self, text: str, images=None):
        """核心发送逻辑，GUI 和远程共用。"""
        images = images or []
        self._clear_empty_state()

        # @文件引用：聊天区只显示原文 display_text，完整文件内容只注入发给 AI 的 send_text
        # （避免把整个文件内容也刷在聊天界面上）
        display_text = text
        send_text = self._expand_file_mentions(text)

        # 上传/拖拽/粘贴的图片都带本地路径——把路径注入给模型（像 @文件一样），需要把图片
        # 当文件处理时它能拿去用。视觉看图仍走 base64 block，这里只多给个"文件句柄"。
        if images:
            _paths = "、".join(p for p, _ in images)
            _hint = (f"[本次随消息上传了 {len(images)} 张图片，本地路径"
                     f"（需要把图片当文件处理时可直接把路径传给工具）：{_paths}]")
            send_text = (send_text + "\n\n" + _hint) if send_text else _hint

        # 带图片但当前模型不支持视觉时，不再把整轮任务切给弱视觉模型。
        # 改为先用视觉模型做识别/OCR，再把识别结果作为文本交回当前强模型。
        use_vision_bridge = bool(images) and not agent.current_model_supports_vision()
        original_model_name = agent.MODEL_LIST[agent.current_model_index][0]
        vision_model_name = ""
        if use_vision_bridge:
            vision_idx = agent.get_vision_model_index()
            if vision_idx < 0:
                self._append_html(
                    "\n⚠️ 当前模型不支持图片。请先在「设置 → 图片识别模型」里选一个能看图的模型。\n",
                    "tool_result",
                )
                return
            vision_model_name = agent.MODEL_LIST[vision_idx][0]

        self._update_btn_state("stop")
        # 捕获当前会话——worker 线程绑到它，不受后续切换 active 影响
        from .. import session as _session
        sess = _session.get_active()
        _session.register(sess)   # 确保在注册表里（启动初始会话首次发消息时落册）
        sess.is_generating = True

        # 显示用户消息
        self._append_html("\n", "spacer")
        self._append_html("你\n", "user_label")
        if images:
            self._insert_images_in_chat(images)
        if display_text:
            self._append_user_text(display_text + "\n\n")
        # 发消息后强制滚到底：看到自己刚发的 + 贴底后 AI 回复会自动跟随
        self._scroll_to_bottom()

        if use_vision_bridge:
            state.stop_flag = False
            threading.Thread(
                target=self._run_vision_bridge_agent,
                args=(send_text, images, vision_model_name, original_model_name, sess),
                daemon=True,
            ).start()
            return

        # 构造消息
        if images:
            # Anthropic / MiMo 官方建议：图片在前、文字在后，模型才能正确关联问题与图片
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if send_text:
                content.append({"type": "text", "text": send_text})
            agent.chat_history.append(HumanMessage(content=content))
        else:
            agent.chat_history.append(HumanMessage(content=send_text))

        # 立即存盘 + 刷侧栏：让会话马上出现在侧栏，长任务时开新对话也能切回它
        # （否则要等 worker 跑完才 save 进 index → 正在跑的会话在侧栏找不到）。
        try:
            agent.save_session()
        except Exception:
            pass
        self._refresh_session_list()

        state.stop_flag = False
        threading.Thread(target=self._run_agent, args=(sess,), daemon=True).start()

    def _run_agent(self, sess=None):
        # 把这个 worker 线程绑定到它要跑的会话：之后 agent_loop / tools 里所有
        # state.X（会话级）都落到这个 session，不受主线程切换 active 影响。
        # P1 单会话时绑的就是 active，行为等价；多会话时这是隔离的关键。
        from .. import session as _session
        if sess is None:
            sess = _session.get_active()
        sess.is_generating = True
        with sess.render_lock:
            sess.render_log.clear()    # 新一轮提问开始：清掉上一轮的渲染事件缓冲
        sess.thread = threading.current_thread()
        _session.bind_thread(sess)
        from .. import roles as _roles
        sess.role_snapshot = _roles.capture_active_role()  # 冻结本轮人格（防生成途中被换卡）
        try:
            agent.agent_loop(self)
        finally:
            sess.is_generating = False
            sess.thread = None
            sess.role_snapshot = None    # 解冻：空闲会话回退读全局当前角色
            _session.unbind_thread()
            self.bridge.finished.emit(sess)

    def _run_vision_bridge_agent(self, text, images, vision_model_name, original_model_name, sess=None):
        """非视觉模型收到图片时：视觉模型只负责识别，原模型负责最终回答。"""
        from .. import session as _session
        if sess is None:
            sess = _session.get_active()
        sess.is_generating = True
        with sess.render_lock:
            sess.render_log.clear()    # 新一轮提问开始：清掉上一轮的渲染事件缓冲
        sess.thread = threading.current_thread()
        _session.bind_thread(sess)
        from .. import roles as _roles
        sess.role_snapshot = _roles.capture_active_role()  # 冻结本轮人格（防生成途中被换卡）
        try:
            self.show_message(
                f"\n🔎 使用「{vision_model_name}」识别图片，随后交给「{original_model_name}」继续处理\n",
                "tool_result",
            )
            detected_name, description = agent.describe_images_with_vision(text, images)
            if agent.stop_flag:
                return
            if not description:
                description = "图片识别未返回有效内容。"

            preview = description
            if len(preview) > 1200:
                preview = preview[:1200] + "\n... [识别结果较长，已折叠显示；完整内容会交给当前模型]"
            self.show_message(f"✅ 图片识别完成（{detected_name}）\n{preview}\n", "tool_result")

            bridge_text = (
                "[[LINGXI_INTERNAL_VISION_BRIDGE]]\n"
                f"[图片识别结果，由 {detected_name} 提供，供 {original_model_name} 继续处理]\n"
                f"{description}"
            )
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if text:
                content.append({"type": "text", "text": text})
            agent.chat_history.append(HumanMessage(content=content))
            agent.chat_history.append(HumanMessage(content=bridge_text))
            # 立即存盘 + 刷侧栏（worker 线程→主线程信号），让该会话马上进侧栏可切回
            try:
                agent.save_session()
            except Exception:
                pass
            self.bridge.sessions_refresh.emit()
            agent.agent_loop(self)
        except Exception as e:
            self.show_retry(f"图片识别失败: {str(e)[:100]}")
        finally:
            sess.is_generating = False
            sess.thread = None
            sess.role_snapshot = None    # 解冻：空闲会话回退读全局当前角色
            _session.unbind_thread()
            self.bridge.finished.emit(sess)

    def _on_finished_sess(self, finished_sess):
        """finished 信号槽：区分活跃会话 vs 后台会话。

        - finished_sess == active → 活跃会话结束，走完整收尾（刷 UI/按钮/撤销等）
        - finished_sess != active → 后台会话结束，只刷侧栏，不碰前台 UI
        """
        from .. import session as _session
        active_sess = _session.get_active()
        if finished_sess is not active_sess:
            # 后台会话完成：只刷侧栏列表（显示新标题/状态），不碰前台 UI
            self._refresh_session_list()
            return
        # 活跃会话完成：完整收尾（若中途切走过，切回时已 _redraw_chat + 重放 render_log
        # 补齐，worker 这轮的后续也是实时渲染的，这里无需再整体重绘）
        self._ai_reply_start = None
        self._update_btn_state("enabled" if self._has_input else "disabled")
        self._refresh_session_list()
        # AI 这一轮可能新建了 checkpoint，刷新撤销按钮状态
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        state.remote_session = False

    def _show_retry(self, error_msg):
        """在聊天区显示错误信息和重试按钮（MessageView）。"""
        self.chat_area.show_retry(error_msg, self._on_retry)

    def _show_image_dialog(self, pixmap):
        """点击聊天区图片：在应用内显示半透明遮罩 + 居中大图（传 QPixmap 全分辨率原图）。"""
        if pixmap is None or pixmap.isNull():
            return

        # 已有遮罩则先关闭
        if getattr(self, "_image_overlay", None) is not None:
            self._image_overlay.deleteLater()
            self._image_overlay = None

        from PySide6.QtWidgets import QLabel as _QLabel

        overlay = QWidget(self)
        overlay.setObjectName("imageOverlay")
        overlay.setStyleSheet(
            "#imageOverlay { background-color: rgba(0, 0, 0, 160); }"
        )
        overlay.setGeometry(0, 0, self.width(), self.height())

        # 居中放图片
        label = _QLabel(overlay)
        max_w = int(self.width() * 0.85)
        max_h = int(self.height() * 0.85)
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignCenter)
        # 居中定位
        lw, lh = pixmap.width(), pixmap.height()
        label.setGeometry(
            (overlay.width() - lw) // 2,
            (overlay.height() - lh) // 2,
            lw, lh
        )

        # 点击遮罩任意位置关闭
        def _close(event):
            overlay.deleteLater()
            self._image_overlay = None
        overlay.mousePressEvent = _close

        overlay.show()
        overlay.raise_()
        self._image_overlay = overlay


    # ---- #8 Drag & Drop ----
    def _show_drag_overlay(self):
        """Show fullscreen semi-transparent drag overlay"""
        if hasattr(self, '_drag_overlay') and self._drag_overlay is not None:
            return
        overlay = QWidget(self)
        overlay.setObjectName("dragOverlay")
        overlay.setGeometry(0, 0, self.width(), self.height())
        overlay.setStyleSheet(
            f"QWidget#dragOverlay {{"
            f"  background-color: {self._t('drag_bg')};"
            f"  border: {self._t('drag_border_style')} {self._t('drag_border')};"
            f"  border-radius: 18px;"
            f"}}"
        )
        layout = QVBoxLayout(overlay)
        layout.setAlignment(Qt.AlignCenter)
        icon_label = QLabel(overlay)
        icon_label.setPixmap(self._svg_icon("folder_open_lucide.svg", self._t("drag_text")).pixmap(QSize(64, 64)))
        icon_label.setFixedSize(72, 72)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("background: transparent; border: none; padding: 0;")
        text_label = QLabel("拖拽文件到这里", overlay)
        text_label.setAlignment(Qt.AlignCenter)
        text_label.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {self._t('drag_text')}; "
            f"background: transparent; border: none; padding: 0; letter-spacing: 4px;"
        )
        sub_label = QLabel("支持图片和文本文件", overlay)
        sub_label.setAlignment(Qt.AlignCenter)
        sub_label.setStyleSheet(
            f"font-size: 12px; color: {self._t('drag_subtext')}; background: transparent; "
            f"border: none; padding: 0; letter-spacing: 1px;"
        )
        layout.addWidget(icon_label)
        layout.addWidget(text_label)
        layout.addWidget(sub_label)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.show()
        overlay.raise_()
        self._drag_overlay = overlay

    def _hide_drag_overlay(self):
        """Hide drag overlay"""
        ov = getattr(self, '_drag_overlay', None)
        if ov is not None:
            ov.deleteLater()
            self._drag_overlay = None

    def dragEnterEvent(self, event):
        """Accept drag if it contains files or images, show overlay"""
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._show_drag_overlay()
        else:
            super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        """Hide drag overlay when leaving"""
        self._hide_drag_overlay()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """Handle dropped files and images"""
        self._hide_drag_overlay()

        if event.mimeData().hasImage():
            img = event.mimeData().imageData()
            if img and not img.isNull():
                self._add_image_from_qimage(img)
                event.acceptProposedAction()
                return

        if event.mimeData().hasUrls():
            handled = False
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if not path:
                    continue
                lower = path.lower()
                if lower.endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')):
                    self._add_pending_image(path)
                    handled = True
                elif lower.endswith(('.txt', '.md', '.py', '.js', '.ts', '.html', '.css',
                                     '.json', '.xml', '.yaml', '.yml', '.toml', '.ini',
                                     '.cfg', '.sh', '.bat', '.ps1', '.c', '.cpp', '.h',
                                     '.java', '.go', '.rs', '.rb', '.php', '.sql', '.csv',
                                     '.log')):
                    try:
                        limit = 200000  # 字符上限（不是字节——中文一个字符 3 字节）
                        with open(path, "r", encoding="utf-8", errors="replace") as f:
                            # 多读 1 个字符探测是否真被截断，避免拿字节数跟字符上限比
                            content = f.read(limit + 1)
                        truncated = len(content) > limit
                        content = content[:limit]
                        fname = os.path.basename(path)
                        if truncated:
                            limit_k = limit // 1000
                            insert_text = (
                                f"[File: {fname}]\n"
                                f"[文件过长，仅插入前 {limit_k}K 字符]\n"
                                f"{content}"
                            )
                        else:
                            insert_text = f"[File: {fname}]\n{content}"
                        cursor = self.entry.textCursor()
                        cursor.movePosition(QTextCursor.End)
                        cursor.insertText(insert_text)
                        handled = True
                    except Exception:
                        pass
            if handled:
                event.acceptProposedAction()
                return
        super().dropEvent(event)


    def resizeEvent(self, event):
        """窗口尺寸变化时，让图片遮罩跟随"""
        super().resizeEvent(event)
        self._refresh_responsive_layout()
        ov = getattr(self, "_image_overlay", None)
        if ov is not None:
            ov.setGeometry(0, 0, self.width(), self.height())
            # 重新居中图片
            for child in ov.findChildren(QLabel):
                pm = child.pixmap()
                if pm and not pm.isNull():
                    child.setGeometry(
                        (ov.width() - pm.width()) // 2,
                        (ov.height() - pm.height()) // 2,
                        pm.width(), pm.height()
                    )

    def closeEvent(self, event):
        # 关窗前先唤醒任何还挂着的命令 / edit diff 确认请求——否则 worker 线程
        # 会卡满 5 分钟才超时，期间 agent 线程整段挂死
        self._release_pending_confirm()
        self._release_pending_edit()

        # main.py 在挂上系统托盘后会把 _hide_on_close 置 True
        if not getattr(self, "_hide_on_close", False):
            super().closeEvent(event)
            return

        # 读已保存的选择（"hide" / "quit"），若有则跳过弹窗
        prefs = _load_ui_prefs()
        saved = prefs.get("close_action")
        if saved == "hide":
            event.ignore()
            self.hide()
            return
        if saved == "quit":
            super().closeEvent(event)
            from PySide6.QtWidgets import QApplication
            QApplication.quit()
            return

        dialog = CloseConfirmDialog(self)
        if dialog.exec() != QDialog.Accepted or dialog.action is None:
            event.ignore()
            return

        action = dialog.action
        if dialog.remember_check.isChecked():
            prefs["close_action"] = action
            _save_ui_prefs(prefs)

        if action == CloseConfirmDialog.ACTION_HIDE:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)
            from PySide6.QtWidgets import QApplication
            QApplication.quit()

    def keyPressEvent(self, event):
        """Esc / Ctrl+F / F3 / F12 key handler"""
        if event.key() == Qt.Key_Escape:
            if getattr(self, "_image_overlay", None) is not None:
                self._image_overlay.deleteLater()
                self._image_overlay = None
                return
            if self._search_widget is not None and self._search_widget.isVisible():
                self._close_search()
                return
        # ---- #7 Ctrl+F ----
        if event.key() == Qt.Key_F and event.modifiers() & Qt.ControlModifier:
            self._toggle_search()
            return
        if event.key() == Qt.Key_F3:
            if self._search_widget and self._search_widget.isVisible():
                text = self._search_widget._input.text()
                if event.modifiers() & Qt.ShiftModifier:
                    self._search_prev(text)
                else:
                    self._search_next(text)
                return
        # ---- F12: Debug Inspector ----
        if event.key() == Qt.Key_F12:
            self._toggle_debug_inspector()
            return
        super().keyPressEvent(event)

    def _toggle_debug_inspector(self):
        """F12：唤出 / 关闭 Debug Inspector（非模态，懒构造）。"""
        insp = getattr(self, "_debug_inspector", None)
        if insp is None:
            from .debug_inspector import DebugInspector
            insp = DebugInspector(self)
            self._debug_inspector = insp
        if insp.isVisible():
            insp.hide()
        else:
            insp.show()
            insp.raise_()
            insp.activateWindow()


    # ---- #7 Ctrl+F in-chat search ----


    def _on_retry(self):
        """点击重试：重新执行 agent_loop"""
        from .. import session as _session
        sess = _session.get_active()
        if sess.is_generating:
            return
        # 移除上次失败的 AI 消息（如果最后一条是 AI 的空消息）
        from langchain_core.messages import AIMessage
        if agent.chat_history and isinstance(agent.chat_history[-1], AIMessage):
            _c = agent.chat_history[-1].content
            # content 可能是 str 或 list（含 thinking blocks）
            if isinstance(_c, list):
                _text = "".join(
                    b.get("text", "") for b in _c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if not _text.strip():
                    agent.chat_history.pop()
            elif isinstance(_c, str) and not _c.strip():
                agent.chat_history.pop()

        _session.register(sess)
        sess.is_generating = True
        self._update_btn_state("stop")
        state.stop_flag = False
        threading.Thread(target=self._run_agent, args=(sess,), daemon=True).start()


    def _show_toast(self, text, duration=1500):
        """Brief toast notification that auto-disappears"""
        from PySide6.QtWidgets import QLabel
        from PySide6.QtCore import QTimer
        if hasattr(self, '_toast_label') and self._toast_label is not None:
            try:
                self._toast_label.deleteLater()
            except Exception:
                pass

        toast = QLabel(self)
        # 已映射的 emoji（🧠/⚡ 等）换成内联 SVG；未映射的（🟡🟢🔴⚫ 状态点靠颜色表意）原样保留
        toast.setTextFormat(Qt.RichText)
        toast.setText(self._emoji_to_svg_html(text, self._t("toast_text"), size=13))
        toast.setStyleSheet(
            f"QLabel {{ background: {self._t('toast_bg')}; color: {self._t('toast_text')}; "
            f"padding: 8px 18px; border: 1px solid {self._t('toast_border')}; border-radius: 10px; "
            f"font-size: 11px; letter-spacing: 1px; }}"
        )
        toast.setAlignment(Qt.AlignCenter)
        toast.adjustSize()
        x = (self.width() - toast.width()) // 2
        y = self.height() - 80
        toast.move(x, y)
        toast.show()
        toast.raise_()
        self._toast_label = toast
        QTimer.singleShot(duration, lambda: self._dismiss_toast(toast))

    def _dismiss_toast(self, toast):
        try:
            if toast:
                toast.deleteLater()
        except Exception:
            pass


    def show_message(self, text, tag):
        """线程安全：从 agent 线程发送信号到 UI 线程"""
        from .. import session as _session
        _sess = _session.current_session()
        # 记本轮渲染事件供"切走→切回"重放（thinking_indicator 是临时计时器、不留痕，不记）
        if tag != "thinking_indicator":
            with _sess.render_lock:
                _sess.render_log.append(("msg", text, tag))
        # 后台会话：不实时渲染（切回时统一 _redraw_chat + 重放 render_log）
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.append_signal.emit(text, tag)

    def show_plan(self, items):
        """线程安全：从 agent 线程推送任务计划到 UI 主线程"""
        self.bridge.show_plan.emit(list(items or []))

    def _tick_plan_spinner(self):
        self._plan_spinner_angle = (self._plan_spinner_angle + 30) % 360
        if getattr(self, "_plan_items", None):
            self._render_plan_rows(self._plan_items)

    def _plan_spinner_svg(self, color, size=16):
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="3" '
            f'stroke-linecap="round" stroke-linejoin="round">'
            f'<g transform="rotate({self._plan_spinner_angle} 12 12)">'
            f'<path d="M21 12a9 9 0 1 1-3.2-6.9"/></g></svg>'
        )
        data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return (
            f'<img src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" '
            f'style="vertical-align:middle;" />'
        )

    def _render_plan_rows(self, items):
        title_color = self._t("thinking")
        muted_color = self._t("thinking_msg")
        hl_bg = self._t("thinking_bg")     # 进行中那一行的高亮底色
        rows = []
        for it in items:
            txt = (it.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            status = it.get("status")
            # 三态：待办 ○ / 进行中 ⟳（高亮整行）/ 完成 ✓（弱化）。状态词前缀对齐设计图。
            if status == "done":
                icon = self._inline_svg_img("circle-check.svg", muted_color, 16)
                label, label_color = "完成", muted_color
                text_style = f"color:{muted_color};"
                cell_bg = ""
            elif status == "in_progress":
                icon = self._plan_spinner_svg(title_color, 16)
                label, label_color = "进行中", title_color
                text_style = f"color:{title_color};font-weight:600;"
                cell_bg = f"background:{hl_bg};"
            else:
                icon = self._inline_svg_img("circle_lucide.svg", muted_color, 16)
                label, label_color = "待办", muted_color
                text_style = ""
                cell_bg = ""
            rows.append(
                '<tr>'
                f'<td width="24" style="{cell_bg}padding:3px 0 5px 6px;vertical-align:top;">'
                f'{icon}</td>'
                f'<td style="{cell_bg}{text_style}padding:3px 8px 5px 4px;line-height:1.4;">'
                f'<span style="color:{label_color};font-size:11px;">{label}</span>&nbsp;{txt}</td>'
                '</tr>'
            )
        html = (
            '<table cellspacing="0" cellpadding="0" width="100%">'
            + "".join(rows)
            + "</table>"
        )
        self.plan_body.setText(html)
        self._fit_plan_body_height(html)

    def _fit_plan_body_height(self, html):
        margins = self.plan_panel.layout().contentsMargins()
        body_width = self.plan_panel.width() - margins.left() - margins.right()
        self.plan_body.setFixedWidth(body_width)
        self.plan_scroll.setFixedWidth(body_width)

        doc = QTextDocument()
        doc.setDefaultFont(self.plan_body.font())
        doc.setTextWidth(body_width)
        doc.setHtml(html)
        # QLabel 的 RichText sizeHint 对 table + word wrap 会低估高度；QTextDocument
        # 按实际文本宽度计算，再多给 6px 防止最后一行 descender 被裁。
        body_height = int(doc.size().height()) + 6
        self.plan_body.setFixedHeight(body_height)
        self.plan_scroll.setFixedHeight(min(body_height, self._plan_body_max_height()))

    def _plan_body_max_height(self):
        """计划正文最大高度：限制浮层不把聊天区撑满，内容超出时滚动。"""
        if hasattr(self, "chat_area"):
            # 留出上下空隙和标题/进度条高度。确认卡出现时 chat_area 会变矮，
            # 这里必须跟着缩，不能用旧的 160px 下限把浮层挤到确认卡上。
            available = max(80, self.chat_area.height() - 105)
        else:
            available = 360
        return min(420, available)

    def _render_plan_panel(self, items):
        """主线程 slot：渲染任务计划面板"""
        self._plan_items = list(items or [])
        if not items:
            self._plan_spinner_timer.stop()
            self.plan_body.clear()
            self.plan_scroll.setFixedHeight(0)
            self.plan_panel.setVisible(False)
            return
        done = sum(1 for it in items if it.get("status") == "done")
        self.plan_count.setText(f"{done}/{len(items)} 完成")
        self.plan_progress.setValue(round(done / len(items) * 100))
        self._render_plan_rows(items)
        if any(it.get("status") == "in_progress" for it in items):
            if not self._plan_spinner_timer.isActive():
                self._plan_spinner_timer.start(80)
        else:
            self._plan_spinner_timer.stop()
        self.plan_panel.layout().invalidate()
        self.plan_panel.layout().activate()
        self.plan_panel.setVisible(True)
        self.plan_panel.raise_()          # 浮在聊天区之上
        self.plan_panel.setFixedHeight(self._plan_panel_target_height())
        self._position_plan_panel()       # 贴右上角（adjustSize 后定位）

    def show_retry(self, error_msg):
        """线程安全：显示错误信息和重试按钮"""
        from .. import session as _session
        _sess = _session.current_session()
        # 记进 render_log：后台会话报错时，切回去才看得到（否则报错静默消失、像没回复就停了）
        with _sess.render_lock:
            _sess.render_log.append(("msg", f"\n⚠️ {error_msg}\n", "ai_msg"))
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.show_retry.emit(error_msg)

    def remove_thinking_indicator(self):
        from .. import session as _session
        _sess = _session.current_session()
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.remove_thinking.emit()

    def update_thinking_indicator(self, text):
        """线程安全：更新等待指示器文本"""
        from .. import session as _session
        _sess = _session.current_session()
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.update_thinking.emit(text)

    def _append_html(self, text, tag):
        if tag not in ("thinking_indicator",) and getattr(self, "_empty_state_visible", False):
            self._clear_empty_state()
        # 消息流已改 MessageView 真控件渲染 —— 直接把 tag 分发给它（镜像旧分发器协议）。
        self.chat_area.handle(text, tag)


    def show_token_usage(self, session_usage, round_usage):
        """线程安全：从 agent 线程通知 UI 更新 token 用量"""
        from .. import session as _session
        _sess = _session.current_session()
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.token_usage.emit(session_usage, round_usage)


    def _ctx_window(self):
        """当前会话所选模型的上下文窗口大小（token）。取不到返回 0。"""
        from .. import models as _models, session as _session
        try:
            idx = _session.get_active().current_model_index
            _, mtype, model_id, _ = _models.MODEL_LIST[idx]
            return _models.context_window_for(mtype, model_id)
        except Exception:
            return 0

    def _update_token_usage(self, session_usage, round_usage):
        """UI 线程：更新底栏 Token 显示，格式「已用 {累计} · 上下文 {占用}/{窗口}·{级别}」。

        - 已用 = 本会话累计 token（input+输出的总和，session_usage['total']）——"这会话共花了多少"。
        - 上下文 = 本轮输入 token（≈ 当前上下文占用，整段发出去的历史）/ 模型窗口 + 充裕/适中/紧张。
          切会话/刚启动时拿不到本轮 input，只显示「已用」，不瞎报占用。"""
        if not hasattr(self, 'token_usage_label'):
            return
        inp = session_usage.get('input', 0)
        out = session_usage.get('output', 0)
        total = session_usage.get('total', 0) or (inp + out)
        occ = round_usage.get('input', 0)           # 当前上下文占用（仅本轮可得）
        window = self._ctx_window()

        def _fmt(n):
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1000:
                return f"{n / 1000:.1f}K"
            return str(int(n))

        def _fmt_win(n):
            if n >= 1_000_000:
                return f"{n / 1_000_000:.0f}M"
            if n >= 1000:
                return f"{n / 1000:.0f}K"
            return str(int(n))

        parts = [f"Token 已用 {_fmt(total)}"]
        if occ > 0 and window > 0:
            ratio = occ / window
            level = "充裕" if ratio < 0.5 else ("适中" if ratio < 0.8 else "紧张")
            parts.append(f"上下文 {_fmt(occ)}/{_fmt_win(window)}·{level}")
        self.token_usage_label.setText("  ·  ".join(parts))
        self.token_usage_label.setToolTip(
            f"本会话累计：输入 {_fmt(inp)} · 输出 {_fmt(out)} · 总计 {_fmt(total)}"
            + (f"\n当前上下文占用：约 {_fmt(occ)} / 窗口 {_fmt_win(window)}" if occ > 0 and window > 0 else "")
        )
        self.token_usage_label.setVisible(True)

    def _refresh_token_label_from_session(self):
        """切会话后把底部 token 显示刷成【该会话】的累计用量（token 已会话级）。
        新会话还没用量 → 隐藏 label。"""
        if not hasattr(self, "token_usage_label"):
            return
        from .. import session as _session
        su = _session.get_active().session_token_usage
        if su.get("total", 0) > 0:
            self._update_token_usage(su, {})
        else:
            self.token_usage_label.setVisible(False)
