"""主聊天窗口 ChatUI。

UI/Agent 解耦：agent 线程通过 SignalBridge.emit 把渲染请求 queue 到主线程，
ChatUI 暴露给 agent 的全部公开方法（show_message / render_final_markdown /
remove_thinking_indicator / show_token_usage / show_retry）都是线程安全 wrapper。
"""
import os
import base64

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame,
    QSizePolicy, QFileDialog, QMenu, QDialog,
)
from PySide6.QtCore import Qt, QSize, QTimer, QPoint, QUrl
from PySide6.QtGui import (
    QFont, QIcon, QTextCursor, QColor, QTextCharFormat, QPixmap, QImage,
    QPainter,
)
from langchain_core.messages import HumanMessage

from .. import agent
from .fonts import BODY, ui_font
from .. import state
from ..content_blocks import block_text
from ._base import BASE_DIR
from .theme import THEMES, build_stylesheet, load_saved_theme, save_theme_choice, set_active_theme
from .widgets import (
    DragDropTextEdit, FileCompleter, QuitConfirmDialog,
    SignalBridge,
)
from .helpers import (
    _make_button_icon, _make_upload_icon, strip_emoji,
)
from .prefs import _load_ui_prefs, _save_ui_prefs
from .settings_dialog import SettingsDialog
from .confirm_bars import ConfirmBarsMixin
from .markdown_render import MarkdownRenderMixin
from .search_overlay import SearchOverlayMixin
from .sidebar import SidebarMixin
from .header import HeaderMixin
from .rag_sidebar import RagSidebarMixin
from .queue_panel import QueuePanel
from .run_admission import RunAdmissionMixin
from .queue_dispatch import QueueDispatchMixin
from .send import SendMixin
from .run_results import RunResultMixin
from .timeline import TimelineMixin


class ChatUI(ConfirmBarsMixin, MarkdownRenderMixin, SearchOverlayMixin,
             SidebarMixin, HeaderMixin, RagSidebarMixin,
             SendMixin, RunAdmissionMixin, QueueDispatchMixin, RunResultMixin,
             TimelineMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("灵犀")
        self.resize(1000, 700)
        self.setMinimumSize(600, 400)
        self.theme = load_saved_theme()
        set_active_theme(self.theme)   # 消息流按需取色，构造控件之前就要知道当前主题
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
        self.bridge.session_search_done.connect(self._on_session_search_done)
        self.bridge.confirm_request.connect(self._on_confirm_request)
        self.bridge.edit_confirm_request.connect(self._on_edit_confirm_request)
        self.bridge.remote_submit.connect(self._on_remote_submit)
        self.bridge.dismiss_confirm.connect(self._on_dismiss_confirm)
        self.bridge.show_plan.connect(self._on_plan_update)
        self.bridge.run_result.connect(self._on_run_result)
        self.bridge.kb_status.connect(self._on_kb_status)

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
        # 当前消息流里已经画过结果卡的 run_id（同一轮只画一张，重复信号不重复追加）
        self._rendered_result_run_id = ""
        # 命令 / 编辑白名单已迁移到 session.Session（会话级，见 confirm_bars）：用户"允许
        # 并记住"只影响发起确认的那个会话，不再作为 ChatUI 全局属性跨会话共享/泄漏。

        self._build_ui()
        # 启动时的会话用上次选的模式（跳过权限确认从不记成默认，见 _choose_mode）
        from .. import permission_modes as _pm
        from .. import session as _session
        _pm.apply(_session.get_active(), _pm.default_for_new_session(
            _load_ui_prefs().get("permission_mode"), "manual"))
        self._refresh_mode_btn()
        self._refresh_session_list()
        self._restore_role_card_ui()
        self._show_empty_state()
        QTimer.singleShot(300, self, self._show_current_model_config_warning)

    # ── 主题工具 ──
    def _t(self, key):
        """读取当前主题的 token 颜色"""
        return THEMES[self.theme][key]

    def _toggle_theme(self):
        """白天 ↔ 夜间。刷新全局样式和各处 chrome，再重绘消息流——
        消息块是构造时取的色，不重绘的话已经显示的内容会停在旧主题。"""
        self.theme = "light" if self.theme == "dark" else "dark"
        save_theme_choice(self.theme)
        self._apply_theme()
        self._redraw_chat()
        self._refresh_queue_panel()
        self._render_plan_panel()   # 面板里的状态行和证据也是按旧主题取的色

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
        from .theme import build_app_qss

        app = QApplication.instance()
        if app is None:
            return

        bg = QColor(self._t("tooltip_bg"))
        fg = QColor(self._t("tooltip_text"))

        def _apply():
            # 1. QSS（tooltip + 全局滚动条：细圆角条、没有上下箭头）
            app.setStyleSheet(build_app_qss(self.theme))
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
        QTimer.singleShot(0, self, _apply)

    def _apply_theme(self):
        """重新生成全局 QSS，并刷新所有用 setStyleSheet 直接设置的 chrome。"""
        set_active_theme(self.theme)
        self.setStyleSheet(build_stylesheet(self.theme))
        self._apply_tooltip_style()  # 切主题时 tooltip 也跟着刷
        # 顶栏图标是按主题色现画的单色图（深浅色按钮还要换成月亮 / 太阳）
        if hasattr(self, "theme_btn"):
            self._refresh_header_icons()
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
        if getattr(self, "_background_panel", None) is not None:
            self._background_panel.apply_theme()
        if hasattr(self, "chat_area"):
            self._style_chat_area()
        if hasattr(self, "queue_panel"):
            self.queue_panel.apply_theme()
        if hasattr(self, "empty_state"):
            self._refresh_empty_state_layout()
            self._position_empty_state()
        if hasattr(self, "model_btn"):
            self._refresh_model_btn()      # 底栏模型按钮的文字色 / 模型名跟着主题走
        if hasattr(self, "task_panel_btn"):
            self._style_task_panel_btn()
        # 段控（计划|执行）样式走全局 QSS，setStyleSheet 重建即生效，无需单独刷
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        if hasattr(self, "role_btn"):
            self._restore_role_card_ui()
        if hasattr(self, "img_btn"):
            self._style_img_btn()
        if hasattr(self, "mode_btn"):
            self._refresh_mode_btn()
        if hasattr(self, "scroll_bottom_btn"):
            self._style_scroll_bottom_btn()
        if hasattr(self, "plan_panel"):
            self._style_plan_panel()
        if hasattr(self, "file_panel"):
            self.file_panel.apply_theme()
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
        text = "\n" + "\n".join(issues) + "\n"
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
        self._build_input_area(right_layout)
        self._build_footer(right_layout)

        main_layout.addWidget(right, 1)

        # 任务面板在最右侧，像 Claude / Codex 的侧栏一样由顶栏按钮开关；
        # 窗口放不下时改成盖在对话区上的浮层（见 _apply_task_panel_collapse）
        self._main_layout = main_layout
        self._build_plan_panel()
        self._task_panel_docked = True
        # 文件面板紧挨着对话区（在任务面板左边），默认收起；点"改动的文件"才出来
        self._build_file_panel()
        main_layout.addWidget(self.file_panel)
        main_layout.addWidget(self.plan_panel)
        self._apply_task_panel_collapse()

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
        self._rendered_result_run_id = ""   # 换会话 = 换消息流，结果卡要重新画
        if hasattr(self, "chat_area"):
            self.chat_area.clear()
        if hasattr(self, 'token_usage_label'):
            self.token_usage_label.setVisible(False)
        # 计划面板跟着当前会话刷新。_reset_render_state 是新建/切换/切项目三条路径的
        # 共同漏斗，且都在 set_active(新会话) 之后才调——current_plan 已是新会话的计划。
        # （hasattr 守卫：本方法可能在 build_ui 建好 plan_panel 之前被调到）
        if hasattr(self, "plan_panel"):
            # 临时打开只属于刚才那份计划：换到别的上下文就作废，回到用户记住的开关。
            # 步骤标题快照保留——下面这次渲染会按新会话的计划对齐，切回后同一份计划
            # 继续推进仍算"同一份"，不会重新弹出来。
            self._task_panel_plan_open = False
            self._render_plan_panel(getattr(state, "current_plan", None) or [])

    def _is_hidden_bridge_message(self, msg):
        """程序注入、只给模型看的消息，历史界面不当作用户聊天展示。

        判据以 `lingxi_internal` 标记为准：早先只认视觉桥接的文字前缀，自动修复 / 完成闸门
        的内部提示在重开会话后会被画成"你"说的话——实时界面从来不这么显示它们。
        恢复说明（B04）不在这里隐藏，它另有画法（`_recovery_note_text`）。
        """
        if not isinstance(msg, HumanMessage):
            return False
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        if kwargs.get("lingxi_internal") is True:
            from ..recovery import RECOVERY_KINDS
            return kwargs.get("lingxi_kind") not in RECOVERY_KINDS
        if isinstance(msg.content, list):
            return False
        content = str(msg.content or "").lstrip()
        return (
            content.startswith("[[LINGXI_INTERNAL_VISION_BRIDGE]]")
            or content.startswith("[图片识别结果，由 ")
        )

    @staticmethod
    def _recovery_note_text(msg):
        """恢复说明（继续任务 / 中断后的恢复提示）在历史里的显示文字；不是恢复说明返回 None。

        优先用当时实时显示的那段（随消息落盘在 `lingxi_recovery.note`），
        重开后看到的与当时看到的是同一段话；没有就退回给模型的全文。
        """
        if not isinstance(msg, HumanMessage):
            return None
        kwargs = getattr(msg, "additional_kwargs", None) or {}
        from ..recovery import RECOVERY_KINDS
        if kwargs.get("lingxi_internal") is not True or kwargs.get("lingxi_kind") not in RECOVERY_KINDS:
            return None
        record = kwargs.get("lingxi_recovery")
        if isinstance(record, dict) and isinstance(record.get("note"), str) and record["note"]:
            return record["note"]
        return str(msg.content or "")

    def _next_img_name(self) -> str:
        """历史图片资源用单调递增名，不能用 id(img)——QImage 临时对象的地址会被复用，
        导致两张不同的历史图片拿到同一资源名、渲染成同一张。"""
        self._img_seq = getattr(self, "_img_seq", 0) + 1
        return f"hist_img_{self._img_seq}"

    def _redraw_chat(self):
        self.chat_area.clear()
        # 重绘的是历史：回复上方不起秒表、不标当前模式
        self.chat_area.replaying = True
        try:
            rendered_any = self._replay_history(list(agent.chat_history))
        finally:
            self.chat_area.replaying = False
        if not rendered_any:
            self._show_empty_state()
        else:
            from .. import session as _session
            if not _session.get_active().is_generating:
                # 和实时一致：已经以回答收尾的最后一轮也收起过程（还在生成的接着重放，不收）
                self.chat_area.collapse_answered()
        self._redraw_result_card()
        # 计划面板的刷新已统一到 _reset_render_state（新建/切换/切项目三路共用漏斗），
        # 且 _load_session 调 _redraw_chat 前必先调 _reset_render_state，这里无需重复渲染。

    def _replay_history(self, history_snapshot):
        """把历史消息按实时那一套画回消息流：工具调用画成同样的工具卡（结果取对应的
        ToolMessage），被接受的计划变化推演成步骤分段。返回是否画出了任何内容。"""
        from langchain_core.messages import ToolMessage
        from ..limits import TOOL_RESULT_PREVIEW_CHARS
        from ..streaming import describe_tool_call
        from .message_view import replay_plan_change
        results = {m.tool_call_id: str(m.content or "") for m in history_snapshot
                   if isinstance(m, ToolMessage)}
        role_name = agent.get_current_role_name()
        rendered_any = False
        labeled = False      # 这一轮的回复已经起过头（实时也是一轮只标一次模型名）
        plan = []
        for idx, msg in enumerate(history_snapshot):
            if self._is_hidden_bridge_message(msg):
                continue
            note = self._recovery_note_text(msg)
            if note is not None:
                # 恢复说明是程序写的，画成程序说明，不画成"你"的气泡
                rendered_any = True
                labeled = False
                self._append_html(note + "\n\n", "tool_result")
                continue
            if isinstance(msg, HumanMessage):
                rendered_any = True
                labeled = False
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
                # 用户气泡下挂时间轴操作（编辑/回退/分叉），idx 是绝对历史下标
                self.chat_area.add_user_actions(idx)
            elif hasattr(msg, 'content') and msg.__class__.__name__ == "AIMessage":
                # content 可能是 str 或 list（含 thinking + text blocks）
                _ai_content = msg.content
                if isinstance(_ai_content, list):
                    # 从 content blocks 提取正文(兼容 Anthropic text / Responses output_text)
                    _ai_content = "\n".join(
                        t for t in (block_text(_blk) for _blk in _ai_content) if t
                    )
                _has_text = bool(_ai_content and _ai_content.strip())
                _calls = [tc for tc in (getattr(msg, 'tool_calls', None) or []) if isinstance(tc, dict)]
                if not (_has_text or _calls):
                    continue
                rendered_any = True
                if not labeled:
                    # 角色卡优先（和实时一致）；否则是写这条回复时记下的模型，旧会话没有就不标
                    model = (role_name or (getattr(msg, "additional_kwargs", None) or {})
                             .get("lingxi_model") or "")
                    self._append_html(f"{model}\n", "ai_label")
                    labeled = True

                # 有正文就渲染正文（定格 markdown 富文本）
                if _has_text:
                    self._msg_buffers[str(len(self._msg_buffers))] = _ai_content
                    self.chat_area.finalize_markdown(self._md_to_html(_ai_content))
                    from PySide6.QtWidgets import QApplication
                    self.chat_area.add_message_actions(
                        on_copy=lambda t=_ai_content: (QApplication.clipboard().setText(t),
                                                       self._show_toast("已复制")))

                # 工具调用画成和实时一样的工具卡——不管这条 AIMessage 有没有正文
                # （MiMo 常把"短文字 + 工具调用"放在同一条里）
                for tc in _calls:
                    name = tc.get("name") or "?"
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    display_name, detail = describe_tool_call(name, args)
                    self._append_html(f"\n{display_name}", "tool_tag")
                    self._append_html(f"  {detail}\n", "tool_detail")
                    result = results.get(tc.get("id"))
                    if result:
                        if len(result) > TOOL_RESULT_PREVIEW_CHARS:
                            result = result[:TOOL_RESULT_PREVIEW_CHARS] + "\n... [结果已截断]"
                        self._append_html(result + "\n", "tool_result")
                    self._append_html("", "reset_ai_reply")
                    new_plan = replay_plan_change(plan, name, args, result)
                    if new_plan is not None:
                        plan = new_plan
                        self.chat_area.on_plan(plan)
        return rendered_any

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
        self._refresh_empty_suggestion_set()
        self._position_empty_state()
        self.empty_state.show()
        self.empty_state.raise_()
        # 初次打开时 viewport 还没完成最终布局，立刻算出来的 width/height
        # 会偏小导致欢迎态居中错位；延迟几次再 reposition，覆盖到布局稳定后的尺寸。
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self, self._position_empty_state)

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

    def _build_chat_area(self, parent_layout):
        # 消息流改成真控件渲染（MessageView：QScrollArea + 每轮控件树），
        # 圆角卡/思考块/工具卡走 message_view.py 的组件（QTextBrowser 画不了圆角/阴影）。
        from .message_view import MessageView
        self.chat_area = MessageView()
        self.chat_area.setObjectName("chatArea")
        self.chat_area.image_clicked.connect(self._show_image_dialog)   # 点击图片 → 放大遮罩
        # 命令卡上"改动了 N 个文件"里的文件名：和任务面板的改动文件一样，在文件面板里开
        self.chat_area.file_clicked.connect(self._on_task_panel_file_clicked)
        # 回复正文里用行内代码写的项目文件路径（可带行号）：同样在文件面板里打开
        self.chat_area.file_link_clicked.connect(self._on_file_link_clicked)
        # 用户气泡上的时间轴操作（参数是该消息在 chat_history 里的绝对下标）
        self.chat_area.user_edit_requested.connect(self._on_user_edit)
        self.chat_area.user_rewind_requested.connect(self._on_user_rewind)
        self.chat_area.user_fork_requested.connect(self._on_user_fork)
        self.chat_area.meta_provider = self._stream_meta               # 回复上方那行小字里的模式
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

    def _stream_meta(self):
        from .. import permission_modes as _pm
        from .. import session as _session
        return {"mode": _pm.label(_pm.current(_session.get_active()))}

    # 面板停靠后，对话栏（含顶栏）至少要留这么宽，否则改成浮层——顶栏最窄一档约 590
    _TASK_PANEL_MIN_CHAT = 592
    # 文件面板停靠时对话区也至少留这么宽（再窄顶栏按钮会叠在一起）；放不下时任务面板先让成浮层，
    # 还放不下就把文件面板藏起来（标签页留着）
    _FILE_PANEL_MIN_CHAT = _TASK_PANEL_MIN_CHAT

    def _build_plan_panel(self):
        # 最右侧的一栏（B05 三段式 + 验证证据），由 _build_ui 放进主布局
        from .task_panel import TaskPanel
        self.plan_panel = TaskPanel(None, theme_lookup=self._t)
        prefs = _load_ui_prefs()
        # 用户的开关：默认打开，选择记在界面偏好里（沿用 task_panel_collapsed 这个键）
        self._task_panel_open = not bool(prefs.get("task_panel_collapsed", False))
        self._task_panel_overlay_open = False     # 窗口放不下时临时打开的浮层，不改偏好
        # 收到一份新计划（步骤标题变了）时临时打开停靠的面板，同样不改偏好：用户收起面板后
        # 新计划要能自己出来，但同一份计划的推进（勾步骤、换下一步）不再弹。用户亲手关掉
        # 这份计划即作废，直到下一份新计划。
        self._task_panel_plan_open = False
        self._plan_step_titles = ()               # 上一份计划（渲染过的）的步骤标题快照
        try:
            width = int(prefs.get("task_panel_width", TaskPanel.DEFAULT_WIDTH))
        except (TypeError, ValueError):
            width = TaskPanel.DEFAULT_WIDTH
        self.plan_panel.setFixedWidth(max(TaskPanel.MIN_WIDTH, min(TaskPanel.MAX_WIDTH, width)))
        self.plan_panel.max_width_fn = self._task_panel_max_width
        self.plan_panel.width_changed.connect(self._on_task_panel_width_changed)
        self.plan_panel.step_clicked.connect(self._on_task_panel_step_clicked)
        self.plan_panel.file_clicked.connect(self._on_task_panel_file_clicked)
        self.plan_panel.files_provider = self._task_panel_files
        # 面板出现 / 消失 / 变宽会改变对话栏宽度：输入框宽度和顶栏折叠档位要跟着重算
        self.plan_panel.installEventFilter(self)
        self.plan_panel.on_undo_request = self._on_undo_task_switch
        # 兼容旧属性别名
        self.plan_title_icon = self.plan_panel.plan_title_icon
        self.plan_title = self.plan_panel.plan_title
        self.plan_count = self.plan_panel.plan_count
        self.plan_progress = self.plan_panel.plan_progress
        self.plan_scroll = self.plan_panel.plan_scroll
        self.plan_body = self.plan_panel.plan_body
        self._plan_items = self.plan_panel._plan_items
        self._plan_spinner_timer = self.plan_panel._plan_spinner_timer

    def _style_plan_panel(self):
        if hasattr(self, "plan_panel"):
            self.plan_panel.apply_theme()

    def _sidebar_shown_width(self):
        side = getattr(self, "sidebar", None)
        return side.width() if side is not None and getattr(self, "_sidebar_visible", True) else 0

    def _task_panel_narrow(self):
        """停靠进来的话，对话栏剩下的宽度不够：这时面板只能以浮层打开。

        文件面板开着标签页时先给它留出最窄一档——用户正在看文件，任务面板让成浮层，
        关掉文件面板后自己停靠回来。"""
        rest = (self.width() - self._sidebar_shown_width() - self.plan_panel.width()
                - self._file_panel_reserved_width())
        return rest < self._TASK_PANEL_MIN_CHAT

    def _task_panel_max_width(self):
        """拖宽的上限：停靠时不能把对话栏挤到放不下顶栏；浮层时只要不超出窗口。"""
        if getattr(self, "_task_panel_docked", True):
            return (self.width() - self._sidebar_shown_width() - self._file_panel_reserved_width()
                    - self._TASK_PANEL_MIN_CHAT)
        return self.width() - 120

    def _task_panel_wanted_open(self):
        """放得下时该不该显示：用户的开关，或这一份新计划临时打开（窗口窄时只认浮层）。"""
        return self._task_panel_open or self._task_panel_plan_open

    def _task_panel_collapsed(self):
        narrow = self._task_panel_narrow()
        return not (self._task_panel_overlay_open if narrow else self._task_panel_wanted_open())

    def _open_task_panel_for_new_plan(self, titles):
        """收到一份步骤标题变了的新计划：临时打开停靠的面板（不改偏好）。

        同一份计划的推进（勾步骤、换下一步）标题没变，不重复弹；窗口太窄、面板只能当
        浮层盖住对话时不自动弹；用户亲手收起过就不再自己出来，直到下一份新计划。
        """
        # 新的一份计划：之前的"手动关掉"作废，之后的推进不再弹
        self._task_panel_plan_open = False
        if not titles or not hasattr(self, "plan_panel"):
            return
        if self._task_panel_narrow():
            return          # 放不下：这时面板只能当浮层盖住对话，不自动弹
        if self._task_panel_open:
            return          # 面板本来就开着（用户没收起过）
        self._task_panel_plan_open = True
        self._apply_task_panel_collapse()

    def _apply_task_panel_collapse(self):
        """按用户的开关和窗口宽度摆放面板：放得下就停靠在右边，放不下就收起，
        这时点按钮以浮层打开（盖在对话区上，不挤压它）。"""
        if not hasattr(self, "plan_panel"):
            return
        narrow = self._task_panel_narrow()
        self._dock_task_panel(not narrow)
        collapsed = self._task_panel_collapsed()
        self.plan_panel.set_collapsed(collapsed)
        if narrow and not collapsed:
            self._position_task_panel_overlay()
        btn = getattr(self, "task_panel_btn", None)
        if btn is not None:
            btn.setChecked(not collapsed)

    def _dock_task_panel(self, dock):
        if dock == getattr(self, "_task_panel_docked", True):
            return
        panel = self.plan_panel
        if dock:
            self._task_panel_overlay_open = False      # 回到停靠：浮层那次临时打开作废，按用户偏好来
            panel.set_overlay(False)
            self._main_layout.addWidget(panel)
        else:
            self._main_layout.removeWidget(panel)      # 仍是中心控件的子控件，按坐标摆成浮层
            panel.set_overlay(True)
        self._task_panel_docked = dock

    def _position_task_panel_overlay(self):
        """浮层贴在窗口最右边、从顶栏下面开始，盖在对话区上面——顶栏（包括开关它的按钮）一直能点。"""
        host = self.plan_panel.parentWidget()
        if host is None:
            return
        header = getattr(self, "_header_bar", None)
        top = header.mapTo(host, QPoint(0, header.height())).y() if header is not None else 0
        width = min(self.plan_panel.width(), max(self.plan_panel.MIN_WIDTH, host.width() - 120))
        self.plan_panel.setFixedWidth(width)
        self.plan_panel.setGeometry(host.width() - width, top, width, host.height() - top)
        self.plan_panel.raise_()

    def _toggle_task_panel(self):
        """顶栏开关。放得下时切换停靠的面板并记住选择；放不下时只开关这一次的浮层。"""
        if self._task_panel_narrow():
            self._task_panel_overlay_open = not self._task_panel_overlay_open
        elif self._task_panel_wanted_open():
            # 当前正显示：关掉。这一份计划临时打开的那次一并作废——用户手动关掉后
            # 同一份计划再推进也不再自己出来，直到下一份新计划。
            self._task_panel_plan_open = False
            if self._task_panel_open:
                self._task_panel_open = False
                prefs = _load_ui_prefs()
                prefs["task_panel_collapsed"] = True
                _save_ui_prefs(prefs)
        else:
            self._task_panel_open = True
            prefs = _load_ui_prefs()
            prefs["task_panel_collapsed"] = False
            _save_ui_prefs(prefs)
        self._apply_task_panel_collapse()

    def _on_task_panel_step_clicked(self, n, title):
        """点面板里的第 n 步：消息流滚到那一步的分段（收起的那一轮会展开）。"""
        if self.chat_area.scroll_to_step(n, title):
            if not getattr(self, "_task_panel_docked", True) and self._task_panel_overlay_open:
                self._task_panel_overlay_open = False      # 浮层会挡住消息流：跳过去就收起
                self._apply_task_panel_collapse()
            return
        from .. import session as _session
        plan = list(getattr(_session.get_active(), "current_plan", None) or [])
        status = plan[n - 1].get("status") if 0 < n <= len(plan) else None
        if status == "pending":
            self._show_toast(f"第 {n} 步还没开始", 2500)
        else:
            self._show_toast("对话里没有这一步的分段（可能是在之前的会话里做的）", 3500)

    def _task_panel_files(self, sess):
        """面板"改动的文件"：这一轮写过的文件，加上此刻相对上次提交的增删行数。
        行数要跑 git，按（工作区, 文件列表, 改动次数）缓存——只在又写了文件时重算，
        面板每 2 秒的刷新不会每次都起 git 进程。"""
        v = getattr(sess, "verification", None) or {}
        files = list(v.get("dirty_files") or [])
        if not files:
            return []
        work = self._undo_workspace_for(sess)
        key = (work, tuple(files), int(v.get("change_revision", 0) or 0))
        cache = getattr(self, "_panel_files_cache", None)
        if cache is None or cache[0] != key:
            from ..tools_git import diff_numstat
            stats = diff_numstat(work, files) or {}
            rows = [(f, *stats.get(f, (None, None, False))) for f in files]
            self._panel_files_cache = (key, rows)
        return self._panel_files_cache[1]

    def _on_task_panel_file_clicked(self, rel_path):
        """点改动的文件：在右边停靠的文件面板里开一个标签页（只读，不替模型记"已审阅"）。

        不开模态窗口——看文件的时候对话照常能读、能输入，几个文件也能并排切着看。
        """
        self._open_file_in_panel(rel_path)
        self._settle_file_panel()

    def _on_file_link_clicked(self, href):
        """点回复正文里的文件链接（`lingxi-file:src/ui/task_panel.py:835`）。

        入口和任务面板「改动的文件」一样走文件面板；带行号的打开后滚到那一行。
        """
        from .markdown_render import parse_file_link
        target = parse_file_link(href)
        if target is None:
            return
        rel_path, line = target
        self._open_file_in_panel(rel_path, line=line)
        self._settle_file_panel()

    def _open_file_in_panel(self, rel_path, work_dir=None, line: int = 0):
        """按指定目录取这个文件此刻的差异，开一个标签页（已经开着就切过去）。

        work_dir 不给才用当前会话的工作区。结果卡要看**那一轮自己的**目录：
        worktree 里跑的那轮改动在隔离区，拿主会话的项目目录取 diff 只会说"没有差异"。
        line 是正文链接带的行号，打开后滚到那一行（0 表示不带行号）。
        """
        from .. import session as _session
        from ..tools_git import file_diff_lines
        work = work_dir or self._undo_workspace_for(_session.get_active())
        rows = file_diff_lines(work, rel_path)
        self.file_panel.open_file(rel_path, rows, line=line)

    def _settle_file_panel(self):
        """标签页开完之后结算面板位置：收起挡路的浮层任务面板，按空间决定显示与否。"""
        if not getattr(self, "_task_panel_docked", True) and self._task_panel_overlay_open:
            self._task_panel_overlay_open = False      # 从浮层点的：收起浮层，别挡住刚开的文件
        self._apply_task_panel_collapse()          # 有了文件面板，任务面板可能要让成浮层
        self._apply_file_panel_collapse()
        if not self.file_panel.isVisible():
            # 窗口太小，面板放不下：标签页已经开好，窗口宽回来就会显示，这里说清楚
            self._show_toast("窗口太窄，文件面板暂时放不下；拉宽窗口就能看到", 3500)

    # ── 文件面板（对话区右边的停靠面板）──────────────────────────

    def _build_file_panel(self):
        from .file_panel import FilePanel
        prefs = _load_ui_prefs()
        self.file_panel = FilePanel(None, theme_lookup=self._t)
        try:
            width = int(prefs.get("file_panel_width", FilePanel.DEFAULT_WIDTH))
        except (TypeError, ValueError):
            width = FilePanel.DEFAULT_WIDTH
        # 用户想要的宽度（拖宽时更新）；实际宽度还要受窗口空间约束，见 _apply_file_panel_collapse
        self._file_panel_wanted_width = max(FilePanel.MIN_WIDTH, min(FilePanel.MAX_WIDTH, width))
        self.file_panel.setFixedWidth(self._file_panel_wanted_width)
        self.file_panel.max_width_fn = self._file_panel_max_width
        self.file_panel.width_changed.connect(self._on_file_panel_width_changed)
        self.file_panel.closed.connect(self._on_file_panel_closed)
        # 出现 / 消失 / 变宽都会改变对话栏宽度：输入框和顶栏折叠档位要跟着重算
        self.file_panel.installEventFilter(self)

    def _file_panel_max_width(self):
        """拖宽上限：对话栏至少还要留 _FILE_PANEL_MIN_CHAT，别把对话挤没了。"""
        from .file_panel import FilePanel
        return max(FilePanel.MIN_WIDTH, self._file_panel_room())

    def _plan_panel_shown_width(self):
        """停靠的任务面板占的宽度；浮层式打开是盖在对话上的，不占位置。"""
        panel = getattr(self, "plan_panel", None)
        if panel is None or not panel.isVisible() or not getattr(self, "_task_panel_docked", True):
            return 0
        return panel.width()

    def _file_panel_reserved_width(self):
        """任务面板决定停靠还是浮层时，要给开着标签页的文件面板留的宽度（最窄一档）。"""
        panel = getattr(self, "file_panel", None)
        return panel.MIN_WIDTH if panel is not None and panel.paths() else 0

    def _file_panel_room(self):
        """文件面板最多能占多宽：窗口去掉侧栏、停靠的任务面板，再给对话栏留一条底线。"""
        return (self.width() - self._sidebar_shown_width() - self._plan_panel_shown_width()
                - self._FILE_PANEL_MIN_CHAT)

    def _apply_file_panel_collapse(self):
        """文件面板只在有标签页、且窗口放得下时显示；收起时对话区恢复原来的宽度。

        窗口临时变窄只把面板压窄（有下限），压不下就先藏起来——标签页留着，
        窗口宽回来再显示，不替用户丢掉正在看的文件。
        """
        panel = getattr(self, "file_panel", None)
        if panel is None:
            return
        if not panel.paths():
            if panel.isVisible():
                panel.setVisible(False)
            return
        if panel.is_dragging():
            return          # 用户正拖着改宽度，别在这时候按可用空间改回去
        room = self._file_panel_room()
        if room < panel.MIN_WIDTH:
            panel.setVisible(False)
            return
        want = max(panel.MIN_WIDTH, min(self._file_panel_wanted_width, room))
        if panel.width() != want:
            panel.setFixedWidth(want)
        panel.setVisible(True)

    def _on_file_panel_width_changed(self, width):
        self._file_panel_wanted_width = max(self.file_panel.MIN_WIDTH,
                                            min(self.file_panel.MAX_WIDTH, int(width)))
        prefs = _load_ui_prefs()
        prefs["file_panel_width"] = self._file_panel_wanted_width
        _save_ui_prefs(prefs)
        self._refresh_responsive_layout()

    def _on_file_panel_closed(self):
        """面板收起（标签都关完了）：对话区恢复原来的宽度，输入框和顶栏跟着重算。"""
        self._refresh_responsive_layout()

    def _on_task_panel_width_changed(self, width):
        prefs = _load_ui_prefs()
        prefs["task_panel_width"] = int(width)
        _save_ui_prefs(prefs)
        self._refresh_responsive_layout()

    def _build_empty_state(self):
        self.empty_state = QWidget(self.chat_area.viewport())
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents, False)

        layout = QVBoxLayout(self.empty_state)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        logo = QLabel("灵")          # 和侧栏一样的圆形字标
        logo.setObjectName("emptyLogo")
        logo.setFixedSize(48, 48)
        logo.setAlignment(Qt.AlignCenter)

        title = QLabel("今天想聊点什么？")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        self._empty_title = title

        subtitle = QLabel("灵犀时刻准备为你提供帮助")
        subtitle.setObjectName("emptySubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        self._empty_subtitle = subtitle

        layout.addWidget(logo, 0, Qt.AlignHCenter)
        layout.addSpacing(6)
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
        ob_note = QLabel("配置一个 API Key 后重启应用即可开始。\n只填你手头有的那一个就够用。")
        ob_note.setObjectName("emptySubtitle")
        ob_note.setAlignment(Qt.AlignCenter)
        ob_layout.addWidget(ob_hint)
        ob_layout.addWidget(ob_note)
        ob_layout.addWidget(ob_btn, 0, Qt.AlignHCenter)
        layout.addWidget(self._empty_onboarding, 0, Qt.AlignHCenter)
        self._empty_onboarding.hide()
        self._empty_onboarding_btn = ob_btn

        # 建议（两列小卡片）：打开了项目就给和这个项目相关的，没有项目给通用的。点一下填进输入框
        from PySide6.QtWidgets import QGridLayout
        suggestions = QWidget()
        suggestions.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        sug_v = QVBoxLayout(suggestions)
        sug_v.setContentsMargins(0, 20, 0, 0)
        sug_v.setSpacing(0)
        self._empty_suggestion_buttons = []
        self._empty_suggestion_sets = {}
        self._empty_suggestion_grids = []
        for kind, items in (("project", self._PROJECT_SUGGESTIONS), ("general", self._GENERAL_SUGGESTIONS)):
            box = QWidget()
            grid = QGridLayout(box)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(10)
            for i, (icon_file, text, compact_text) in enumerate(items):
                btn = QPushButton(text)
                btn.setIcon(self._svg_icon(icon_file, self._t("mv_text_sec")))
                btn.setIconSize(QSize(16, 16))
                btn.setObjectName("emptySuggestion")
                btn.setCursor(Qt.PointingHandCursor)
                btn.setMinimumWidth(0)
                btn.clicked.connect(lambda checked=False, t=text: self._use_suggestion(t))
                grid.addWidget(btn, i // 2, i % 2)
                self._empty_suggestion_buttons.append((btn, icon_file, text, compact_text))
            sug_v.addWidget(box, 0, Qt.AlignHCenter)
            self._empty_suggestion_sets[kind] = box
            self._empty_suggestion_grids.append(grid)
        layout.addWidget(suggestions, 0, Qt.AlignHCenter)
        self._empty_suggestions = suggestions

        self.empty_state.hide()

    # 空状态的建议：(图标, 完整文字, 窄窗口时的短文字)
    _PROJECT_SUGGESTIONS = (
        ("folder_open_lucide.svg", "讲讲这个项目的结构", "项目结构"),
        ("scan-search.svg", "找出最近改动里可能的问题", "检查改动"),
        ("flask-conical.svg", "跑一遍测试，看看哪里失败", "跑测试"),
        ("file-pen.svg", "给 README 补一段安装说明", "补 README"),
    )
    _GENERAL_SUGGESTIONS = (
        ("code_lucide.svg", "解释这段代码的逻辑", "解释代码"),
        ("file_text_lucide.svg", "总结一下这篇文档", "总结文档"),
        ("sparkles_lucide.svg", "帮我生成一张插画", "生成插画"),
        ("wand-sparkles.svg", "帮我写一个小脚本", "写脚本"),
    )

    def _refresh_empty_suggestion_set(self):
        """打开了项目就显示和项目相关的建议，没有项目显示通用的。"""
        sets = getattr(self, "_empty_suggestion_sets", None)
        if not sets:
            return
        from .. import session as _session
        has_project = bool(_session.current_project())
        sets["project"].setVisible(has_project)
        sets["general"].setVisible(not has_project)

    def _refresh_empty_state_layout(self, viewport_width=None):
        """刷新空状态的图标色与窄屏文案，避免首屏 chip 在小窗口里挤出视口。"""
        if viewport_width is None and hasattr(self, "chat_area"):
            viewport_width = self.chat_area.viewport().width()
        compact = bool(viewport_width and viewport_width < 620)
        for grid in getattr(self, "_empty_suggestion_grids", []):
            grid.setHorizontalSpacing(8 if compact else 10)

        btn = getattr(self, "_empty_onboarding_btn", None)
        if btn is not None:
            btn.setIcon(self._svg_icon("settings_lucide.svg", self._t("brand_color")))
            btn.setIconSize(QSize(16, 16))

        for item in getattr(self, "_empty_suggestion_buttons", []):
            btn, icon_file, full_text, compact_text = item
            btn.setText(compact_text if compact else full_text)
            btn.setIcon(self._svg_icon(icon_file, self._t("mv_text_sec")))
            btn.setIconSize(QSize(15 if compact else 16, 15 if compact else 16))
            btn.setMinimumHeight(36 if compact else 40)
            btn.setFixedWidth(140 if compact else 240)

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
        # 消息区是 MessageView（QScrollArea）。背景和滚动条要一次设好：setStyleSheet 会整体替换，
        # 以前这里单独设一份 QTextBrowser 样式，把 MessageView 自己的背景冲掉了。
        self.chat_area.apply_theme(
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
            f"  font-weight: 600;"
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
        """滚动到聊天区底部并恢复贴底跟随：之后内容还在排版、变高，消息流自己跟到底
        （MessageView 跟着滚动范围走），不用再隔几百毫秒补滚几次。"""
        self.chat_area.scroll_to_end()
        self._on_scroll_changed()


    # ── 输入区 ──

    def _build_input_area(self, parent_layout):
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(48, 8, 48, 12)
        self.input_wrapper = wrapper
        self.input_wrapper_layout = wrapper_layout

        # B09b：输入队列面板（本会话待发列表；空队列隐藏）。只画不判，处置在 ChatUI。
        self.queue_panel = QueuePanel(self._t)
        self.queue_panel.edit_requested.connect(self._on_queue_edit_requested)
        self.queue_panel.delete_requested.connect(self._on_queue_delete_requested)
        self.queue_panel.process_now_requested.connect(self._on_queue_process_now_requested)
        self.queue_panel.retry_requested.connect(self._on_queue_retry_requested)
        self.queue_panel.discard_requested.connect(self._on_queue_discard_requested)
        self.queue_panel.resume_requested.connect(self._on_queue_resume_requested)
        wrapper_layout.addWidget(self.queue_panel)

        # 命令确认条（默认隐藏；AI 想 run_command 时由 _on_confirm_request 显示）
        self._build_command_confirm_bar()
        wrapper_layout.addWidget(self.command_confirm_bar, 0, Qt.AlignHCenter)

        # edit_file diff 预览卡（默认隐藏；AI 想改文件时由 _on_edit_confirm_request 显示）
        self._build_edit_confirm_bar()
        wrapper_layout.addWidget(self.edit_confirm_bar, 0, Qt.AlignHCenter)

        # 输入框外框（设计稿 A5）：上面一排待发送的图片 chip，中间输入，底下一行 + 和发送
        container = QWidget()
        container.setObjectName("inputContainer")
        self.input_container = container
        container.setFixedWidth(920)
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(12, 10, 10, 8)
        container_layout.setSpacing(6)

        # 待发送的图片：一排小 chip（缩略图 + 名字 + 移除），悬停看大图
        self.image_preview_area = QWidget()
        self.image_preview_area.setVisible(False)
        self.image_preview_layout = QHBoxLayout(self.image_preview_area)
        self.image_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.image_preview_layout.setSpacing(6)
        self.image_preview_layout.addStretch()
        container_layout.addWidget(self.image_preview_area)

        self.entry = DragDropTextEdit()
        self.entry.setObjectName("inputEdit")
        self.entry.setPlaceholderText("向灵犀提问…  Enter 发送，Shift+Enter 换行，@ 引用文件，可拖入或粘贴图片")
        entry_font = ui_font(BODY)
        entry_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        entry_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.entry.setFont(entry_font)
        # 高度由 _on_input_change 按内容定（setFixedHeight）：QTextEdit 默认的 sizeHint 很高，
        # 交给布局的话空输入框也会撑到最大
        self.entry.setFixedHeight(self._ENTRY_MIN_H)
        self.entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.entry.textChanged.connect(self._on_input_change)
        container_layout.addWidget(self.entry)

        # 底下一行：左边 +（上传图片 / 导入项目），右边发送 / 停止
        self._composer_bottom = QHBoxLayout()
        self._composer_bottom.setContentsMargins(0, 0, 0, 0)
        self._composer_bottom.setSpacing(4)
        # 变量名仍叫 img_btn：eventFilter 里的悬停换图标按这个名字找
        self.img_btn = QPushButton()
        self.img_btn.setToolTip("上传图片 / 导入项目")
        self.img_btn.setAccessibleName("上传图片或导入项目")
        self.img_btn.setCursor(Qt.PointingHandCursor)
        self.img_btn.setFixedSize(32, 32)
        self._style_img_btn()
        self.img_btn.clicked.connect(self._show_plus_menu)
        self._composer_bottom.addWidget(self.img_btn)
        self._composer_bottom.addStretch(1)
        container_layout.addLayout(self._composer_bottom)

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
        self.send_btn.setAccessibleName("发送")
        # 不接键盘焦点：点过它以后焦点会一直停在上面，生成中它是停止键，
        # 之后在窗口里按一下空格（比如想翻页）就把正在跑的任务停了
        self.send_btn.setFocusPolicy(Qt.NoFocus)
        self.send_btn.clicked.connect(self._on_send_click)
        # 模型按钮（模型名 · 思考强度）：原顶栏的模型下拉 + 「思考」开关合并到这里，
        # 在模式按钮左边，点开菜单向上弹（见 _show_model_menu）
        self.model_btn = QPushButton()
        self.model_btn.setObjectName("modelBtn")
        self.model_btn.setCursor(Qt.PointingHandCursor)
        self.model_btn.setFocusPolicy(Qt.NoFocus)    # 和发送键 / 模式键一样，不留键盘焦点
        self.model_btn.setFixedHeight(32)
        self.model_btn.clicked.connect(self._show_model_menu)
        self._composer_bottom.addWidget(self.model_btn)
        self._refresh_model_btn()
        # 模式按钮（五种权限模式，原来顶栏的「计划 / 执行」）：在发送键左边，点开菜单向上弹
        self.mode_btn = QPushButton()
        self.mode_btn.setObjectName("modeBtn")
        self.mode_btn.setCursor(Qt.PointingHandCursor)
        self.mode_btn.setFocusPolicy(Qt.NoFocus)     # 和发送键一样，不留键盘焦点
        self.mode_btn.setFixedHeight(32)
        self.mode_btn.clicked.connect(self._show_mode_menu)
        self._composer_bottom.addWidget(self.mode_btn)
        self._refresh_mode_btn()
        self._composer_bottom.addWidget(self.send_btn)
        # 初始化样式
        self._update_btn_state("disabled")

        wrapper_layout.addWidget(container, 0, Qt.AlignHCenter)
        parent_layout.addWidget(wrapper)
        QTimer.singleShot(0, self, self._resize_input_container)

    # 输入框高度：至少两行，最多约七行，再多就在框里滚动
    _ENTRY_MIN_H = 48
    _ENTRY_MAX_H = 160

    def _resize_input_container(self):
        """输入框（连同上面的队列、确认条和下面的底栏）和消息栏对齐：同一条居中的栏，
        左边让出线槽，跟消息正文的左边齐。"""
        if not hasattr(self, "input_container") or not hasattr(self, "chat_area"):
            return
        from .message_view import COLUMN_MAX, GUTTER
        vp = self.chat_area.viewport()
        viewport_width = vp.width()
        side = max(24, (viewport_width - COLUMN_MAX) // 2)        # 与 MessageView.resizeEvent 同一算法
        col_left = side + (GUTTER if viewport_width >= 520 else 0)
        # 消息正文的左右边换算到输入区自己的坐标：视口比输入区窄（滚动条等），按视口宽度
        # 直接当边距会让队列和输入框对不齐
        wrapper = getattr(self, "input_wrapper", None)
        if wrapper is not None:
            # mapTo 只能映射到祖先控件：两边都换算到主窗口坐标再相减
            origin = wrapper.mapTo(self, QPoint(0, 0)).x()
            left = vp.mapTo(self, QPoint(col_left, 0)).x() - origin
            right = wrapper.width() - (vp.mapTo(self, QPoint(viewport_width - side, 0)).x() - origin)
            width = max(260, wrapper.width() - left - right)
        else:
            left, right, width = col_left, side, max(260, viewport_width - col_left - side)
        if hasattr(self, "input_wrapper_layout"):
            self.input_wrapper_layout.setContentsMargins(left, 8, right, 6)
        if hasattr(self, "footer_layout"):
            self.footer_layout.setContentsMargins(left, 0, right, 6)
        self.input_container.setFixedWidth(width)
        if hasattr(self, "command_confirm_bar"):
            self.command_confirm_bar.setFixedWidth(width)
        if hasattr(self, "edit_confirm_bar"):
            self.edit_confirm_bar.setFixedWidth(width)
        # 输入框宽度变了：补全浮窗若开着，等布局刷完（singleShot 0）再按 input_container
        # 的新尺寸/位置重对齐——立即调会拿到布局未稳定的旧几何，导致位置偏
        if hasattr(self, "_file_completer") and self._file_completer.isVisible():
            from PySide6.QtCore import QTimer as _QTimer
            _QTimer.singleShot(0, self, self._position_completer)


    def _refresh_project_indicator(self):
        """根据当前项目刷新底栏项目按钮的文本、图标、tooltip。
        在以下时机调用：__init__、_switch_project、_remove_current_project、_apply_theme、
        切换隔离模式。project_btn 现在位于底栏状态条左侧（见 _build_footer）。
        """
        if not hasattr(self, "project_btn"):
            return
        from .. import projects as _projects
        from .. import session as _sess
        from .. import config
        current = _projects.get_current()
        # 会话级隔离状态
        active = _sess.get_active()
        is_isolated = bool(getattr(active, "worktree", None))
        # 知识库工作区：底栏改显"当前知识库：目录名"，与代码项目彻底解耦（隔离按钮隐藏）
        if getattr(active, "session_kind", "code") == "rag":
            kb = config.RAG_KB_DIR
            if kb:
                import os as _os
                name = _os.path.basename(kb.rstrip("/\\")) or kb
                self.project_btn.setText(f"当前知识库：{name}")
                self.project_btn.setToolTip(f"知识库目录：{kb}")
            else:
                self.project_btn.setText("当前知识库：未配置")
                self.project_btn.setToolTip("知识库模式：请在左侧「选择目录」并重建索引")
            self._style_project_indicator()
            if hasattr(self, "isolation_btn"):
                self.isolation_btn.setVisible(False)
            return
        if current:
            display = self._abbreviate_path(current, max_chars=60)
            if is_isolated:
                self.project_btn.setText(f"当前项目 {display}")
                self.project_btn.setToolTip(
                    f"隔离模式 · worktree: {active.worktree}\n"
                    f"项目：{current}\n（点击切换 / 添加 / 移除项目）"
                )
            else:
                self.project_btn.setText(f"当前项目 {display}")
                self.project_btn.setToolTip(f"当前项目：{current}\n（点击切换 / 添加 / 移除项目）")
        else:
            self.project_btn.setText("无项目 · 全局工作区")
            self.project_btn.setToolTip("当前不在任何项目中\n（点击选择 / 添加项目）")
        self._style_project_indicator()
        # 隔离按钮可见性跟随"有无项目"(隔离=git worktree,必须有项目)。本方法在 __init__、
        # 切项目、切隔离时都会调,所以启动恢复了默认项目时这里就让隔离按钮出现——修"启动有
        # 默认项目却没有隔离按钮,要切一次会话才冒出来"。
        if hasattr(self, "isolation_btn"):
            self.isolation_btn.setVisible(bool(current))
            if current:
                self._style_isolation_btn(active=is_isolated)

    def _style_project_indicator(self):
        """底栏项目/知识库指示按钮的内联样式（用 footer 颜色 token，跟随主题）。"""
        self.project_btn.setStyleSheet(
            f"QPushButton#projectIndicatorBtn {{"
            f"  background: transparent; border: 1px solid transparent; border-radius: 8px;"
            f"  color: {self._t('text_dim')}; font-size: 12px; padding: 3px 10px;"
            f"  text-align: left;"
            f"}}"
            f"QPushButton#projectIndicatorBtn:hover {{"
            f"  background: {self._t('history_hover_bg')};"
            f"  color: {self._t('text')};"
            f"  border-color: {self._t('sidebar_border')};"
            f"}}"
        )

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
        footer_widget.setFixedHeight(28)
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(18, 0, 18, 6)   # 左右由 _resize_input_container 对齐消息栏
        footer_layout.setSpacing(0)
        self.footer_layout = footer_layout

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

        if (hasattr(self, 'plan_panel') and obj is self.plan_panel
                and event.type() in (event.Type.Show, event.Type.Hide, event.Type.Resize)):
            for delay in (0, 30):
                QTimer.singleShot(delay, self, self._refresh_responsive_layout)
            return False

        if (hasattr(self, 'file_panel') and obj is self.file_panel
                and event.type() in (event.Type.Show, event.Type.Hide, event.Type.Resize)):
            for delay in (0, 30):
                QTimer.singleShot(delay, self, self._refresh_responsive_layout)
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

        # 自动调整高度：两行起，最多 _ENTRY_MAX_H，再多在框里滚动
        doc_height = self.entry.document().size().height() + 4
        self.entry.setFixedHeight(int(min(max(doc_height, self._ENTRY_MIN_H), self._ENTRY_MAX_H)))

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

    _COMPLETER_WIDTH = 320

    def _position_completer(self):
        """补全浮窗跟着 @ 走（设计稿状态 3b）：横向左边对齐 @，靠近右边时往左收、右边和
        输入框对齐；纵向始终贴在输入框外框的上沿——@ 在第二行时也不盖住第一行。
        浮窗是主窗口子控件，用相对主窗口的本地坐标 move + raise_。"""
        frame = getattr(self, "input_container", self.entry)
        width = min(self._COMPLETER_WIDTH, frame.width())
        self._file_completer.setFixedWidth(width)
        top_left = frame.mapTo(self, QPoint(0, 0))
        x = top_left.x()
        mention = self._get_active_mention()
        if mention is not None:
            cur = QTextCursor(self.entry.document())
            cur.setPosition(mention[0])
            rect = self.entry.cursorRect(cur)               # @ 在输入框视口里的位置
            x = self.entry.viewport().mapTo(self, rect.topLeft()).x() - 12
        x = max(top_left.x(), min(x, top_left.x() + frame.width() - width))
        y = top_left.y() - self._file_completer.height() - 6
        self._file_completer.move(x, max(0, y))
        self._file_completer.raise_()

    def _apply_completer_theme(self):
        """按当前主题给文件补全器涂色：外框 + hover 走 QSS，文字/选中色交给 delegate
        （delegate 自绘文字，所以颜色不能只靠 QSS 的 item color）。"""
        frame_bg = self._t("mv_card_bg")
        frame_border = self._t("mv_chip_border")
        hover_bg = self._t("mv_header_bg")
        text_color = self._t("mv_text")
        sel_bg = self._t("mv_indigo_soft")
        icon_color = self._t("mv_text_sec")
        self._file_completer.setStyleSheet(
            f"FileCompleter {{ background: {frame_bg}; border: 1px solid {frame_border}; "
            f"border-radius: 10px; padding: 4px; }}"
            f"QLabel#completerHint {{ color: {self._t('mv_text_sec')}; font-size: 12px; background: transparent; "
            f"border-top: 1px solid {self._t('mv_card_border')}; padding: 6px 10px 2px 10px; }}")
        self._file_completer.list_widget.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border-radius: 6px; }"
            f"QListWidget::item:hover {{ background: {hover_bg}; }}")
        dlg = self._file_completer.list_widget.itemDelegate()
        if dlg is not None and hasattr(dlg, "text_color"):
            dlg.text_color = text_color
            dlg.sel_bg = sel_bg
            dlg.highlight_color = self._t("mv_indigo")
            dlg.folder_icon = self._svg_icon("folder_lucide.svg", icon_color).pixmap(16, 16)
            dlg.file_icon = self._svg_icon("file_text_lucide.svg", icon_color).pixmap(16, 16)

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
        ref_fmt.setForeground(QColor(self._t("mv_indigo")))
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
        # 发送键跟着当前会话的运行状态走；任务面板里进行中的圆环同步转 / 停
        if hasattr(self, "plan_panel"):
            self.plan_panel.set_running(state == "stop")

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)
        for delay in (0, 30, 120):
            QTimer.singleShot(delay, self, self._refresh_responsive_layout)

    def _refresh_responsive_layout(self):
        self._resize_input_container()
        self._position_empty_state()
        if hasattr(self, 'scroll_bottom_btn'):
            self._position_scroll_btn()
        self._apply_task_panel_collapse()
        self._apply_file_panel_collapse()
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
        from .widgets import DescribedMenuItem
        menu = QMenu(self)
        menu.setObjectName("plusMenu")
        items = (
            ("image_lucide.svg", "上传图片", "PNG、JPG 都行，也可以直接 Ctrl+V 粘贴", self._pick_image),
            ("folder_open_lucide.svg", "导入项目", "把一个文件夹加为项目，会话按项目分组", self._add_project),
        )
        for icon, title, desc, slot in items:
            DescribedMenuItem.add_to(menu, self._svg_icon(icon, self._t("mv_text_sec")), title, desc, slot,
                                     text=self._t("mv_text"), sub=self._t("mv_text_sec"),
                                     hover=self._t("mv_header_bg"))
        menu.setStyleSheet(
            f"QMenu#plusMenu {{ background: {self._t('mv_card_bg')}; border: 1px solid {self._t('mv_chip_border')}; "
            f"border-radius: 10px; padding: 4px; }}")
        # 菜单从按钮正上方弹出，左边和按钮对齐，不挡输入框
        anchor = self.img_btn.mapToGlobal(QPoint(0, 0))
        menu.exec(anchor - QPoint(0, menu.sizeHint().height() + 6))
        menu.deleteLater()   # 每次点 + 都新建一个菜单，用完删掉，不留在窗口上

    # ── 模型（输入框底栏的模型按钮：模型 + 思考强度合在一个菜单里）──

    # 按钮文字的宽度上限（像素）：再长就省略，别把模式键和发送键挤出底栏
    _MODEL_BTN_MAX_TEXT = 320

    def _refresh_model_btn(self):
        """按钮上写「模型名 · 思考强度」（不支持思考的模型只写模型名）。

        档位取【该模型实际能表达的动作】而不是会话里存的原始值：只能开关的本地模型
        存的是 low/high 之类的通用档位，界面上应当显示「开」。
        """
        from .. import models as _models
        from .. import reasoning as _reasoning
        from .. import session as _session
        sess = _session.get_active()
        count = len(_models.MODEL_LIST)
        idx = int(getattr(sess, "current_model_index", 0) or 0)
        if idx < 0 or idx >= count:
            idx = 0
        name = _models.MODEL_LIST[idx][0]
        supported = _models.supported_efforts_for(idx)
        action = _reasoning.effective(sess.reasoning_effort, supported) if supported else None
        text = f"{name} · {_reasoning.LABELS.get(action, '')}" if action else name
        # 模型名可能很长：底栏的宽度要让给模式键和发送键，超出的用省略号
        metrics = self.model_btn.fontMetrics()
        text = metrics.elidedText(text, Qt.ElideRight, self._MODEL_BTN_MAX_TEXT)
        self.model_btn.setText(text)
        tip = f"模型：{name}（点击切换）"
        if supported:
            tip += f"\n思考强度：{_reasoning.LABELS.get(action, '')}"
        self.model_btn.setToolTip(tip)
        self.model_btn.setAccessibleName(f"模型：{name}")
        self.model_btn.setStyleSheet(
            f"QPushButton#modelBtn {{ background: transparent; border: none; border-radius: 6px;"
            f" padding: 0 10px; font-size: 13px; color: {self._t('mv_text_sec')}; }}"
            f"QPushButton#modelBtn:hover {{ background: {self._t('mv_header_bg')};"
            f" color: {self._t('mv_text')}; }}")

    _EFFORT_DESCS = {
        "off": "不输出推理过程",
        "on": "开启思考（这个模型只能开关）",
        "low": "少量推理，更快",
        "high": "默认档位",
        "max": "尽量多推理，更慢",
    }

    def _show_model_menu(self):
        """点模型按钮：上面是现在能用的模型（当前打勾、带数字键），
        中间是「思考强度 ›」，最下面是「更多模型 ›」（还没配 Key 的）。
        样式与模式菜单一致；菜单从按钮正上方弹出。"""
        self._open_model_menu(anchor=self.model_btn, numbered=True)

    def _open_model_menu(self, anchor, numbered=True):
        """在 anchor 按钮正上方弹出模型菜单。"""
        from PySide6.QtWidgets import QLabel, QWidgetAction
        from .. import models as _models
        from .. import session as _session
        from .widgets import DescribedMenuItem, NumberedMenu
        sess = _session.get_active()
        count = len(_models.MODEL_LIST)
        try:
            current = int(getattr(sess, "current_model_index", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        if current < 0 or current >= count:
            current = 0
        ready, needs_config = _models.partition_models()

        menu = NumberedMenu(self)
        menu.setObjectName("modelMenu")
        head = QWidgetAction(menu)
        title = QLabel("模型")
        title.setStyleSheet(f"color: {self._t('mv_text_sec')}; font-size: 12px;"
                            " padding: 6px 10px 2px 10px; background: transparent;")
        head.setDefaultWidget(title)
        head.setEnabled(False)
        menu.addAction(head)

        if not ready:
            empty = QWidgetAction(menu)
            hint = QLabel("还没有配置好的模型，去设置里填 API Key")
            hint.setStyleSheet(f"color: {self._t('mv_text_sec')}; font-size: 12px;"
                               " padding: 6px 10px 8px 10px; background: transparent;")
            empty.setDefaultWidget(hint)
            empty.setEnabled(False)
            menu.addAction(empty)
        for pos, idx in enumerate(ready):
            name, mtype, _model_id, _think = _models.MODEL_LIST[idx]
            desc = _models.model_type_label(mtype)
            act = DescribedMenuItem.add_to(
                menu, None, name, desc, lambda i=idx: self._choose_model(i),
                text=self._t("mv_text"), sub=self._t("mv_text_sec"),
                hover=self._t("mv_header_bg"),
                trailing=str(pos + 1) if numbered and pos < 9 else "",
                checked=(idx == current), accent=self._t("mv_indigo"))
            if numbered and pos < 9:
                menu.numbered.append(act)

        if ready or needs_config:
            menu.addSeparator()
        # 模型不支持思考时干脆没有这一项（不是灰掉——灰掉会让人以为是配置问题）
        if _models.supported_efforts_for(current):
            self._add_submenu_item(
                menu, "思考强度", self._effort_summary(current),
                lambda: self._open_effort_menu(anchor))
        if needs_config:
            self._add_submenu_item(
                menu, "更多模型", f"{len(needs_config)} 个还没配置好",
                lambda: self._open_more_models_menu(anchor))

        menu.setStyleSheet(
            f"QMenu#modelMenu {{ background: {self._t('mv_card_bg')};"
            f" border: 1px solid {self._t('mv_chip_border')}; border-radius: 10px; padding: 4px; }}")
        size = menu.sizeHint()
        point = anchor.mapToGlobal(QPoint(anchor.width(), 0))
        menu.exec(point - QPoint(size.width(), size.height() + 6))
        menu.deleteLater()

    def _add_submenu_item(self, menu, name, desc, slot):
        """菜单底部那种「标题 + 说明 + ›」的项：点它打开对应的子菜单。

        子菜单延到下一个事件循环再弹：这一下点击还在主菜单的 exec() 里，
        直接嵌套 exec 会叠出第二层事件循环，主菜单收起时才不好收场。
        """
        from .widgets import DescribedMenuItem

        def _later():
            QTimer.singleShot(0, self, slot)

        return DescribedMenuItem.add_to(
            menu, None, name, desc, _later,
            text=self._t("mv_text"), sub=self._t("mv_text_sec"),
            hover=self._t("mv_header_bg"), trailing="›", accent=self._t("mv_indigo"))

    def _effort_summary(self, model_index) -> str:
        """「思考强度」那一行的说明：模型不支持思考时说清原因，不显示假档位。"""
        from .. import models as _models
        from .. import reasoning as _reasoning
        from .. import session as _session
        supported = _models.supported_efforts_for(model_index)
        if not supported:
            return "这个模型不支持思考"
        action = _reasoning.effective(_session.get_active().reasoning_effort, supported)
        return f"当前：{_reasoning.LABELS.get(action, '')}"

    def _open_effort_menu(self, anchor=None):
        """「思考强度」子菜单：档位按 models.supported_efforts_for(当前模型下标) 显示。

        当前档位打勾；选了调 agent.set_reasoning_effort。模型不支持思考时没有这一项。
        """
        from PySide6.QtWidgets import QLabel, QWidgetAction
        from .. import models as _models
        from .. import reasoning as _reasoning
        from .. import session as _session
        from .widgets import DescribedMenuItem, NumberedMenu
        sess = _session.get_active()
        idx = int(getattr(sess, "current_model_index", 0) or 0)
        supported = _models.supported_efforts_for(idx)
        if not supported:
            return
        current = _reasoning.effective(sess.reasoning_effort, supported)
        if current is None:
            current = "off"
        menu = NumberedMenu(self)
        menu.setObjectName("effortMenu")
        head = QWidgetAction(menu)
        title = QLabel("思考强度")
        title.setStyleSheet(f"color: {self._t('mv_text_sec')}; font-size: 12px;"
                            " padding: 6px 10px 2px 10px; background: transparent;")
        head.setDefaultWidget(title)
        head.setEnabled(False)
        menu.addAction(head)
        for pos, effort in enumerate(supported):
            act = DescribedMenuItem.add_to(
                menu, None, _reasoning.LABELS.get(effort, effort),
                self._EFFORT_DESCS.get(effort, ""),
                lambda e=effort: self._choose_effort(e),
                text=self._t("mv_text"), sub=self._t("mv_text_sec"),
                hover=self._t("mv_header_bg"),
                trailing=str(pos + 1) if pos < 9 else "",
                checked=(effort == current), accent=self._t("mv_indigo"))
            if pos < 9:
                menu.numbered.append(act)
        menu.setStyleSheet(
            f"QMenu#effortMenu {{ background: {self._t('mv_card_bg')};"
            f" border: 1px solid {self._t('mv_chip_border')}; border-radius: 10px; padding: 4px; }}")
        self._exec_above(menu, anchor or self.model_btn)

    def _open_more_models_menu(self, anchor=None):
        """「更多模型」子菜单：还没配 Key 的模型，选了提示去设置里补上。"""
        from PySide6.QtWidgets import QLabel, QWidgetAction
        from .. import models as _models
        from .widgets import DescribedMenuItem, NumberedMenu
        _ready, needs_config = _models.partition_models()
        menu = NumberedMenu(self)
        menu.setObjectName("moreModelsMenu")
        head = QWidgetAction(menu)
        title = QLabel("更多模型")
        title.setStyleSheet(f"color: {self._t('mv_text_sec')}; font-size: 12px;"
                            " padding: 6px 10px 2px 10px; background: transparent;")
        head.setDefaultWidget(title)
        head.setEnabled(False)
        menu.addAction(head)
        for idx in needs_config:
            name, mtype, _model_id, _think = _models.MODEL_LIST[idx]
            issues = _models.get_model_config_issues(idx)
            desc = issues[0].rstrip("。") if issues else "还需要在设置里补上配置"
            DescribedMenuItem.add_to(
                menu, None, name, desc, lambda i=idx: self._explain_model_config(i),
                text=self._t("mv_text"), sub=self._t("mv_text_sec"),
                hover=self._t("mv_header_bg"))
        menu.setStyleSheet(
            f"QMenu#moreModelsMenu {{ background: {self._t('mv_card_bg')};"
            f" border: 1px solid {self._t('mv_chip_border')}; border-radius: 10px; padding: 4px; }}")
        self._exec_above(menu, anchor or self.model_btn)

    @staticmethod
    def _exec_above(menu, anchor):
        """子菜单也向上弹：右边缘对齐 anchor 的右边缘，底边留 6px 间隙。"""
        size = menu.sizeHint()
        point = anchor.mapToGlobal(QPoint(anchor.width(), 0))
        menu.exec(point - QPoint(size.width(), size.height() + 6))
        menu.deleteLater()

    def _choose_model(self, index):
        """菜单里选了一个能用的模型：走原下拉框同一条路（停止 / 切换 / 标记 / 提示）。"""
        self._on_model_changed(index)

    def _choose_effort(self, effort):
        """选思考档位：只改当前会话的档位并重建 llm，不动模型。"""
        agent.set_reasoning_effort(effort)
        self._refresh_model_btn()

    def _explain_model_config(self, index):
        """选了还没配好的模型：不切换，只说清缺什么、去哪里补。"""
        from .. import models as _models
        issues = _models.get_model_config_issues(index)
        name = _models.MODEL_LIST[index][0]
        text = issues[0] if issues else f"{name} 还需要在设置里补上配置。"
        self._show_toast(f"{text.rstrip('。')}。在 设置 → 模型 里填好后就能选它。", 6000)

    # ── 权限模式（输入框底栏的"模式"按钮；规则见 permission_modes.py）──

    def _refresh_mode_btn(self):
        """按钮上写当前会话的模式；跳过权限确认用警示色，一眼看得出"现在什么都不问"。"""
        from .. import permission_modes as _pm
        from .. import session as _session
        mode = _pm.current(_session.get_active())
        self.mode_btn.setText(_pm.name(mode))
        self.mode_btn.setToolTip(f"模式：{_pm.name(mode)}（点击切换）")
        self.mode_btn.setAccessibleName(f"模式：{_pm.name(mode)}")
        color = self._t("mv_orange") if mode == "bypass" else self._t("mv_text_sec")
        self.mode_btn.setStyleSheet(
            f"QPushButton#modeBtn {{ background: transparent; border: none; border-radius: 6px;"
            f" padding: 0 10px; font-size: 13px; color: {color}; }}"
            f"QPushButton#modeBtn:hover {{ background: {self._t('mv_header_bg')}; color: {self._t('mv_text')}; }}")

    def _show_mode_menu(self):
        """点模式按钮：五种模式，当前的打勾，右边是数字键，菜单打开时按数字直接选。"""
        from PySide6.QtWidgets import QWidgetAction
        from .. import permission_modes as _pm
        from .. import session as _session
        from .widgets import DescribedMenuItem, NumberedMenu
        current = _pm.current(_session.get_active())
        menu = NumberedMenu(self)
        menu.setObjectName("modeMenu")
        title = QLabel("模式")
        title.setStyleSheet(f"color: {self._t('mv_text_sec')}; font-size: 12px; padding: 6px 10px 2px 10px;"
                            " background: transparent;")
        head = QWidgetAction(menu)
        head.setDefaultWidget(title)
        head.setEnabled(False)
        menu.addAction(head)
        for i, (mode, name, desc) in enumerate(_pm.MODES, 1):
            act = DescribedMenuItem.add_to(
                menu, None, name, desc, lambda m=mode: self._choose_mode(m),
                text=self._t("mv_text"), sub=self._t("mv_text_sec"), hover=self._t("mv_header_bg"),
                trailing=str(i), checked=(mode == current), accent=self._t("mv_indigo"))
            menu.numbered.append(act)
        menu.setStyleSheet(
            f"QMenu#modeMenu {{ background: {self._t('mv_card_bg')}; border: 1px solid {self._t('mv_chip_border')}; "
            f"border-radius: 10px; padding: 4px; }}")
        # 输入框在窗口底部：菜单从按钮正上方弹出，右边和按钮对齐
        size = menu.sizeHint()
        anchor = self.mode_btn.mapToGlobal(QPoint(self.mode_btn.width(), 0))
        menu.exec(anchor - QPoint(size.width(), size.height() + 6))
        menu.deleteLater()

    def _choose_mode(self, mode):
        from .. import permission_modes as _pm
        from .. import session as _session
        sess = _session.get_active()
        if mode == "bypass" and _pm.current(sess) != "bypass" and not self._confirm_bypass_mode():
            return
        _pm.apply(sess, mode)
        if mode in _pm.EXEC_MODES and mode != "bypass":
            # 记成新会话的默认放行规则；跳过权限确认不记（新会话退回上一次的非跳过模式），
            # 计划也不记（它是 agent_mode，新会话照旧沿用上一个会话的）
            prefs = _load_ui_prefs()
            prefs["permission_mode"] = mode
            _save_ui_prefs(prefs)
        self._refresh_mode_btn()
        self._show_toast(f"已切到「{_pm.name(mode)}」：{_pm.describe(mode)}", 3000)

    def _confirm_bypass_mode(self) -> bool:
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("跳过权限确认")
        box.setIcon(QMessageBox.Warning)
        box.setText("切换后，这个会话里 AI 改文件、执行任何命令都不再问你，包括删除文件、"
                    "Git 提交和推送、调用 MCP 工具。\n\n只建议在你信得过、能随时撤回的项目里用。"
                    "这个模式不会被记住：重开程序或新建会话会回到原来的模式。")
        ok = box.addButton("仍要切换", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is ok

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

        from .message_view import _icon, _svg
        close_svg = _svg('<path d="M4 4l8 8M12 4l-8 8"/>')
        for i, (path, _b64) in enumerate(self._pending_images):
            chip = QFrame()
            chip.setObjectName("attachChip")
            chip.setStyleSheet(
                f"#attachChip {{ background: {self._t('mv_header_bg')}; border: 1px solid {self._t('mv_card_border')}; "
                f"border-radius: 6px; }}"
                f"QLabel {{ background: transparent; color: {self._t('mv_text3')}; font-size: 12px; }}"
                f"QPushButton {{ background: transparent; border: none; border-radius: 4px; }}"
                f"QPushButton:hover {{ background: {self._t('mv_chip_border')}; }}")
            row = QHBoxLayout(chip)
            row.setContentsMargins(4, 2, 2, 2)
            row.setSpacing(6)
            thumb = QLabel()
            thumb.setFixedSize(18, 18)
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(18, 18, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                                .copy(0, 0, 18, 18))
            row.addWidget(thumb)
            name = os.path.basename(path) or f"图片 {i + 1}"
            label = QLabel(name if len(name) <= 24 else name[:21] + "…")
            row.addWidget(label)
            del_btn = QPushButton()
            del_btn.setFixedSize(18, 18)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("移除这张图片")
            del_btn.setAccessibleName("移除这张图片")
            del_btn.setIcon(QIcon(_icon(close_svg, self._t("mv_text_sec"), 10)))
            del_btn.setIconSize(QSize(10, 10))
            del_btn.clicked.connect(lambda checked=False, ii=i: self._remove_pending_image(ii))
            row.addWidget(del_btn)
            # 悬停看大图（tooltip 支持富文本里的本地图片）
            chip.setToolTip(f"<img src='{QUrl.fromLocalFile(path).toString()}' width='240'><br>{path}")
            self.image_preview_layout.insertWidget(self.image_preview_layout.count() - 1, chip)

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
            f"font-size: 20px; font-weight: 600; color: {self._t('drag_text')}; "
            f"background: transparent; border: none; padding: 0;"
        )
        sub_label = QLabel("支持图片和文本文件", overlay)
        sub_label.setAlignment(Qt.AlignCenter)
        sub_label.setStyleSheet(
            f"font-size: 13px; color: {self._t('drag_subtext')}; background: transparent; "
            f"border: none; padding: 0;"
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

    def _open_background_panel(self):
        """用户主动打开的应用级入口，不改变当前会话或模型的工具权限。"""
        from .background_panel import BackgroundPanel
        if getattr(self, "_background_panel", None) is None:
            self._background_panel = BackgroundPanel(self._t, self)
        self._background_panel.show()
        self._background_panel.raise_()
        self._background_panel.activateWindow()

    def hideEvent(self, event):
        # 主窗隐藏到托盘时同时停面板刷新；后台命令沿用应用退出清理规则。
        if getattr(self, "_background_panel", None) is not None:
            self._background_panel.hide()
        super().hideEvent(event)

    def closeEvent(self, event):
        # main.py 挂上系统托盘后会把 _hide_on_close 置 True；没挂（测试里直接建的窗口）就是普通关窗。
        # _quitting：request_quit 已确认退出，QApplication.quit() 关窗时走到这里，要真关
        if not getattr(self, "_hide_on_close", False) or getattr(self, "_quitting", False):
            # 关窗前先唤醒任何还挂着的命令 / edit diff 确认请求——否则 worker 线程
            # 会卡满 5 分钟才超时，期间 agent 线程整段挂死
            self._release_pending_confirm()
            self._release_pending_edit()
            super().closeEvent(event)
            return

        # 和 Codex 一样：点 × / Alt+F4 只收进托盘，对话、命令和等你确认的请求都原样留着，
        # 真正退出走托盘右键「退出」（request_quit）。托盘不可用时没处可收，关窗就是退出。
        event.ignore()
        tray = getattr(self, "_tray", None)
        if tray is not None and tray.isVisible():
            self.hide()
            self._hint_still_running(tray)
        else:
            self.request_quit()

    def request_quit(self):
        """真正退出灵犀（托盘右键「退出」）。还有对话或后台命令在跑时先问一句，
        默认按钮是「取消」，误点不会把任务杀掉。"""
        if getattr(self, "_quitting", False):
            return
        busy = self._running_work_summary()
        if busy and QuitConfirmDialog(busy, self).exec() != QDialog.Accepted:
            return
        self._quitting = True
        # 窗口在托盘里时 QApplication.quit() 不会再走 closeEvent，这里先唤醒挂着的确认请求
        self._release_pending_confirm()
        self._release_pending_edit()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def _running_work_summary(self) -> str:
        """还在跑的对话和后台命令，拼成一句话（如"2 个对话、1 个后台命令还在运行"）；都没有返回空串。"""
        from .. import background as _bg
        from .. import session as _session
        chats = sum(1 for s in _session.live_sessions() if self._worker_alive(s))
        try:
            commands = sum(1 for t in _bg.list_snapshots() if t.get("running"))
        except Exception:
            commands = 0
        parts = [f"{chats} 个对话" if chats else "", f"{commands} 个后台命令" if commands else ""]
        parts = [p for p in parts if p]
        return f"{'、'.join(parts)}还在运行" if parts else ""

    def _hint_still_running(self, tray):
        """第一次收进托盘时冒个气泡说清楚：任务没停、怎么找回窗口、怎么真正退出。只提示这一次。"""
        prefs = _load_ui_prefs()
        if prefs.get("tray_hint_shown"):
            return
        prefs["tray_hint_shown"] = True
        _save_ui_prefs(prefs)
        tray.showMessage("灵犀还在后台运行",
                         "关掉窗口不会停止对话和命令。点托盘图标打开窗口，右键 → 退出才会真正关闭。")

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
            if getattr(self, "_task_panel_overlay_open", False) and not getattr(self, "_task_panel_docked", True):
                self._task_panel_overlay_open = False     # 浮层打开时，Esc 收起它
                self._apply_task_panel_collapse()
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
        # 统一启动闸：正常生成中 / 已有等待请求 / 旧 worker 还在收尾，都不能重试
        # （重试要动 chat_history，等旧 worker 退出前碰不得）。拒绝时明确反馈。
        if self._gate_new_run_request(sess, "retry") != "start":
            return
        # 移除上次失败的 AI 消息（如果最后一条是 AI 的空消息）
        from langchain_core.messages import AIMessage
        if agent.chat_history and isinstance(agent.chat_history[-1], AIMessage):
            _c = agent.chat_history[-1].content
            # content 可能是 str 或 list（含 thinking blocks）
            if isinstance(_c, list):
                # 兼容 Responses 的 output_text——否则有正文的回复会被误判为空而弹掉
                _text = "".join(block_text(b) for b in _c)
                if not _text.strip():
                    agent.chat_history.pop()
            elif isinstance(_c, str) and not _c.strip():
                agent.chat_history.pop()

        _session.register(sess)
        sess.is_generating = True
        self._update_btn_state("stop")
        state.stop_flag = False
        if not self._spawn_worker_thread(sess, self._run_agent, args=(sess,)):
            self._show_toast("重试失败：无法创建工作线程，请稍后再试", 4000)


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
        # 界面不显示 emoji：无论提示来自界面还是后端，一律去掉后按纯文本显示
        toast.setTextFormat(Qt.PlainText)
        toast.setText(strip_emoji(text))
        toast.setStyleSheet(
            f"QLabel {{ background: {self._t('toast_bg')}; color: {self._t('toast_text')}; "
            f"padding: 8px 18px; border: 1px solid {self._t('toast_border')}; border-radius: 10px; "
            f"font-size: 12px; }}"
        )
        toast.setAlignment(Qt.AlignCenter)
        toast.adjustSize()
        # 浮在输入框上方、对着对话栏居中；不压在输入框和发送按钮上
        frame = getattr(self, "input_container", None)
        if frame is not None and frame.isVisible():
            top_left = frame.mapTo(self, QPoint(0, 0))
            x = top_left.x() + (frame.width() - toast.width()) // 2
            y = top_left.y() - toast.height() - 12
        else:
            x = (self.width() - toast.width()) // 2
            y = self.height() - 80
        toast.move(max(8, x), max(8, y))
        toast.show()
        toast.raise_()
        self._toast_label = toast
        QTimer.singleShot(duration, self, lambda: self._dismiss_toast(toast))

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
        from .. import session as _session
        sess = _session.current_session()
        if sess is not _session.get_active():
            sess.needs_redraw = True
            return
        self.bridge.show_plan.emit(sess, [dict(it) for it in (items or [])])

    # ── 继续任务（B04）──
    #
    # 点击只在主线程做三件便宜的事：核对归属、等旧 worker 真正退出、预检（目录/模型/模式）。
    # 现场核对（git、文件指纹、结果未知的操作）要 IO，放进新 worker 里、在 begin_run 之前做
    # （recovery.before_run）。整个过程不跑任何项目命令；点了继续才调模型。

    _RESUME_WAIT_MS = 100
    _RESUME_WAIT_LIMIT = 150      # × 100ms = 15s，旧 worker 还没退就放弃并明说

    def _show_text_dialog(self, title, text):
        from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(760, 520)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        lay.addWidget(box)
        dlg.exec()

    def _on_plan_update(self, sess, items):
        """信号排队期间可能切会话或更新计划；只显示当前会话的最新快照。"""
        from .. import session as _session
        if sess is not _session.get_active() or items != sess.current_plan:
            return
        # 步骤标题变了 = 收到一份新计划：收起的面板临时打开（不改偏好）
        if self._render_plan_panel(items):
            self._open_task_panel_for_new_plan(
                tuple((it or {}).get("text", "") for it in (items or [])))
        self.chat_area.on_plan(items)   # 当前步骤换了：消息流里插一个步骤分段

    def _tick_plan_spinner(self):
        if hasattr(self, "plan_panel"):
            self.plan_panel._tick_plan_spinner()

    def _render_plan_rows(self, items):
        if hasattr(self, "plan_panel"):
            self.plan_panel._render_plan_rows(items)

    def _render_plan_panel(self, items=None):
        """主线程 slot：渲染任务目标与计划面板（B05 三段式：要求限制、计划、执行验证）。

        返回这份计划的步骤标题是否与上次渲染的不同（判"是不是新的一份计划"）。
        只有真正收到计划更新（_on_plan_update）才据此临时打开面板；切会话 / 换主题 /
        结果卡刷新这些重渲染只是把快照对齐，不会让用户收起的面板自己冒出来。
        """
        from .. import session as _session
        sess = _session.get_active()
        if not hasattr(self, "plan_panel"):
            return False
        if not sess:
            return False
        plan = items if items is not None else (getattr(sess, "current_plan", None) or [])
        titles = tuple((it or {}).get("text", "") for it in plan)
        changed = titles != self._plan_step_titles
        self._plan_step_titles = titles
        self.plan_panel.render_session(sess)
        self._apply_task_panel_collapse()
        return changed

    def _on_undo_task_switch(self):
        """用户点击【撤销任务切换】。"""
        from .. import session as _session
        sess = _session.get_active()
        if not sess:
            return

        last_switch = getattr(sess, "last_task_switch", None)
        if not isinstance(last_switch, dict):
            return

        # 屏障与「继续任务」共用 resume_pending：等待期间发消息、点继续都会被挡住，
        # 否则撤销还没落地，新一轮 worker 就已经在按切换后的任务干活了。
        if sess.resume_pending:
            self.show_message("上一个操作还在等待当前运行结束，请稍后再撤销。", "system")
            return
        if self._pending_run_for(sess) is not None:
            # B09a：有一条待发送的消息也在等旧 worker 退出。先处理消息再撤销，
            # 否则消息会在撤销落地后突然发出、落进刚恢复的任务里。
            self.show_message("有消息正在等待当前运行结束，请先等它发出或撤回后再撤销任务切换。",
                              "system")
            return
        sess.resume_pending = True
        if sess.is_generating:
            # 走强制停止而不是只打 stop_flag：worker 可能正停在确认卡上等用户，
            # 不放掉确认它永远不会去看 stop_flag，屏障就只能等到超时。
            self._force_stop_generation(wait=False)
            self.show_message("正在停止当前生成，结束后撤销任务切换…", "system")
        self._wait_worker_and_undo_switch(
            sess, last_switch.get("new_task_id", ""), last_switch.get("switch_version", 0))

    def _wait_worker_and_undo_switch(self, sess, new_task_id, switch_version, retries=0):
        """屏障：旧 worker **真正退出**之后才撤销；超时或切走就放弃，不硬改。

        worker 还活着时撤销，它随后的写入（计划、任务、存盘）会覆盖刚恢复的任务。
        忙碌判据统一走 _worker_alive（B09a）。
        """
        from .. import session as _session
        if sess is not _session.get_active():
            # 等待期间切到了别的会话。撤销结果、提示都属于那个看不见的会话，不在后台悄悄改。
            self._undo_switch_abort(sess, "已切换到其它会话，撤销任务切换已取消")
            return
        if self._worker_alive(sess):
            if retries >= self._RESUME_WAIT_LIMIT:
                self._undo_switch_abort(sess, "当前运行仍未结束，撤销任务切换没有执行，请稍后再试")
                return
            QTimer.singleShot(self._RESUME_WAIT_MS, self, lambda: self._wait_worker_and_undo_switch(
                sess, new_task_id, switch_version, retries + 1))
            return
        try:
            self._execute_undo_task_switch(sess, new_task_id, switch_version)
        finally:
            sess.resume_pending = False

    def _undo_switch_abort(self, sess, message):
        sess.resume_pending = False
        self._show_toast(message, 2500)

    def _execute_undo_task_switch(self, sess, new_task_id, switch_version):
        from .. import task_state as _ts
        from .. import memory
        from ..paths import logger
        success, msg = _ts.undo_task_switch(sess, new_task_id, switch_version)
        if success:
            outcome = memory.save_session_report(session=sess)
            save_error = outcome.error
            if save_error is not None:
                logger.error(f"撤销任务切换后保存失败（正文已写入={outcome.body_written}）: {save_error}")
            if hasattr(self, "plan_panel"):
                self._render_plan_panel()   # 步骤标题快照跟着对齐（撤销不改计划不算"新计划"）
            if save_error is None:
                self.show_message(msg, "system")
            elif outcome.body_written:
                # 正文已落盘、只是索引没更新：撤销结果重开也在，不能说成"尚未写入磁盘"。
                self.show_message(
                    f"{msg}\n撤销结果已保存，但会话列表索引更新失败（{str(save_error)[:120]}），"
                    "下次读取会话列表时会自动修复。", "error")
            else:
                # 内存里已经撤销，但磁盘上还是切换后的状态。不说出来的话，重开会话会
                # "莫名其妙"回到新任务，用户还以为撤销成功了。
                self.show_message(
                    f"{msg}\n但保存失败（{str(save_error)[:120]}），撤销结果尚未写入磁盘；"
                    "下次保存成功前重开会话，会回到切换后的状态。", "error")
        else:
            self.show_message(f"撤销任务切换失败：{msg}", "error")
        # 撤销被拒（版本不符、记录已失效）时横幅也要跟着刷新，不留一个点不动的按钮
        if not success and hasattr(self, "plan_panel"):
            self._render_plan_panel()

    def show_retry(self, error_msg):
        """线程安全：显示错误信息和重试按钮"""
        from .. import session as _session
        _sess = _session.current_session()
        # 记进 render_log：后台会话报错时，切回去才看得到（否则报错静默消失、像没回复就停了）
        with _sess.render_lock:
            _sess.render_log.append(("msg", f"\n{error_msg}\n", "ai_msg"))
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
