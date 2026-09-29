"""任务面板（TaskPanel）与固定限制编辑对话框（B05）。

三段式任务面板：
1. 要求与限制（Requirements & Constraints）：
   - 展示用户原始要求、补充要求，保留消息来源 ID
   - 展示用户设定的固定限制（pinned_constraints）
   - [编辑限制] 按钮呼出编辑框，带 1000 字符硬预算校验与生效提示
2. 计划与进展（Plan & Progress）：
   - 计划步骤状态图标、进度条与动态 spinner
   - 最近计划变动原因与摘要
   - 新任务切换可撤销横幅：已开始新任务（原因：……）[撤销任务切换]
3. 执行与核对（Execution & Verification）：
   - 尚未解除的验证义务（verification_status 的缺口判定，与运行态注入同一结论）

普通问答保持简单：无任务且无计划时面板隐藏。
"""
from __future__ import annotations

import base64
import html
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import memory
from .. import task_state as _ts

_REQUEST_SOURCE_NOTES = {
    "legacy_unknown": "<span style='color:#b45309;'>；来源未确认</span>",
    "missing": "<span style='color:#b45309;'>；原文已不在历史中</span>",
}


def _one_line(text: str, limit: int) -> str:
    """压成单行并截断（义务说明可能多行，面板里每条只占一行）。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


class EditConstraintsDialog(QDialog):
    """编辑固定限制对话框。

    - 仅用户可修改或删除
    - 实时字符计数，上限 1000 字符，超限拒绝保存并提示
    - 保存后即刻写入会话并落盘
    """

    def __init__(self, parent: QWidget | None, sess: Any, on_saved: Callable[[], None] | None = None):
        super().__init__(parent)
        self.sess = sess
        self.on_saved = on_saved
        self.setWindowTitle("编辑固定限制")
        self.setFixedWidth(460)
        self.setMinimumHeight(340)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # 提示说明
        hint = QLabel(
            "设置本任务必须严格遵守的固定限制（如必须兼容的环境、禁止修改的接口等）。\n"
            "• 仅用户可在此修改或删除，模型计划工具不得改写。\n"
            "• 每行一条，按输入原文保存：行首的 -、*、编号等字符都算内容，已保存的条目再次保存不会被改动。\n"
            "• 保存后在下次模型调用即刻生效。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b; font-size: 12px; line-height: 1.4;")
        layout.addWidget(hint)

        # 文本框
        self.editor = QPlainTextEdit(self)
        self.editor.setPlaceholderText("每行输入一条限制，按原文保存，例如：\n必须兼容 Windows 系统\n保留现有界面，不引入第三方重型框架\n导出格式必须支持 CSV")
        self.editor.setStyleSheet(
            "QPlainTextEdit {"
            "  border: 1px solid #cbd5e1;"
            "  border-radius: 8px;"
            "  padding: 10px;"
            "  font-size: 13px;"
            "  background: #ffffff;"
            "  color: #1e293b;"
            "}"
            "QPlainTextEdit:focus {"
            "  border-color: #3b82f6;"
            "}"
        )
        task = getattr(sess, "current_task", None)
        # 对话框编辑的是**打开时**的那个任务。对话框是模态的，但 worker 不受它影响，
        # 编辑期间模型完全可能切换任务；保存时要核对，不能把 A 的限制写到 B 上。
        self._task_id = task.get("id") if isinstance(task, dict) else None
        existing = (task.get("pinned_constraints") or []) if isinstance(task, dict) else []
        if existing:
            self.editor.setPlainText("\n".join(existing))
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, 1)

        # 字符计数与底部按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(10)

        self.char_count_label = QLabel(self)
        self.char_count_label.setWordWrap(True)      # 拒绝保存的说明较长，不换行会撑宽定宽对话框被裁掉
        self.char_count_label.setStyleSheet("font-size: 12px; color: #64748b;")
        bottom_row.addWidget(self.char_count_label)
        bottom_row.addStretch(1)

        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            "  border: 1px solid #cbd5e1;"
            "  border-radius: 6px;"
            "  padding: 6px 14px;"
            "  background: #f8fafc;"
            "  color: #475569;"
            "  font-size: 13px;"
            "}"
            "QPushButton:hover { background: #f1f5f9; }"
        )
        self.cancel_btn.clicked.connect(self.reject)
        bottom_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存限制", self)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setStyleSheet(
            "QPushButton {"
            "  border: none;"
            "  border-radius: 6px;"
            "  padding: 6px 16px;"
            "  background: #2563eb;"
            "  color: #ffffff;"
            "  font-size: 13px;"
            "  font-weight: 600;"
            "}"
            "QPushButton:hover { background: #1d4ed8; }"
            "QPushButton:disabled { background: #94a3b8; }"
        )
        self.save_btn.clicked.connect(self._on_save)
        bottom_row.addWidget(self.save_btn)

        layout.addLayout(bottom_row)
        self._on_text_changed()

    def _on_text_changed(self):
        # 计数与保存校验走同一个函数：各算各的话，界面说"没超"、保存却被拒（或反过来）。
        _, _, cleaned = _ts.validate_pinned_constraints(self.editor.toPlainText())
        total_chars = sum(len(c) for c in cleaned)
        limit = _ts.PINNED_CONSTRAINTS_MAX_CHARS
        self.char_count_label.setText(f"{total_chars} / {limit} 字符")
        if total_chars > limit:
            self.char_count_label.setStyleSheet("font-size: 12px; color: #ef4444; font-weight: 600;")
            self.save_btn.setEnabled(False)
        else:
            self.char_count_label.setStyleSheet("font-size: 12px; color: #64748b;")
            self.save_btn.setEnabled(True)

    def _on_save(self):
        text = self.editor.toPlainText()
        valid, error_msg, cleaned = _ts.validate_pinned_constraints(text)
        if not valid:
            self.char_count_label.setText(error_msg)
            self.char_count_label.setStyleSheet("font-size: 12px; color: #ef4444; font-weight: 600;")
            return

        with self.sess.snapshot_lock:
            task = getattr(self.sess, "current_task", None)
            current_id = task.get("id") if isinstance(task, dict) else None
            if current_id != self._task_id:
                # 打开之后任务变了（切换、撤销，或模型建立了首个任务）。不替用户决定这些
                # 限制属于哪个任务：拒绝这一次，改为指向现在的任务，再点一次才保存到它上面。
                self._task_id = current_id
                self._show_error(
                    "打开编辑后当前任务已变化，本次未保存。编辑框里的内容保留；"
                    "确认这些限制适用于现在的任务后再点一次保存。")
                return
            created = current_id is None
            if created:
                task = _ts.create_or_attach_task(self.sess)
                self._task_id = task["id"]
            before = list(task.get("pinned_constraints") or [])
            task["pinned_constraints"] = cleaned

        outcome = memory.save_session_report(session=self.sess)
        if outcome.error is not None and not outcome.body_written:
            # 正文没写进磁盘才算没保存：把内存改回去，免得界面显示"已生效"、重开却不见了，
            # 或者被之后某次无关的保存悄悄写进去。对话框保持打开，用户可以重试。
            with self.sess.snapshot_lock:
                if created and self.sess.current_task is task:
                    self.sess.current_task = None
                    self._task_id = None
                elif self.sess.current_task is task:
                    task["pinned_constraints"] = before
            self._show_error(f"保存失败，限制未生效：{str(outcome.error)[:80]}")
            return

        # 正文已落盘而索引没更新（body_written=True 仍带 error）：限制确实已经存下、重开也在，
        # 不能回滚内存——那样内存说"没保存"、磁盘上却有，下次保存前退出就是反的结论。
        self.save_warning = (
            f"限制已保存并生效，但会话列表索引更新失败（{str(outcome.error)[:80]}），"
            "下次读取会话列表时会自动修复。" if outcome.error is not None else "")
        if self.on_saved:
            self.on_saved()
        self.accept()

    save_warning = ""

    def _show_error(self, text: str):
        self.char_count_label.setText(text)
        self.char_count_label.setStyleSheet("font-size: 12px; color: #ef4444; font-weight: 600;")


class TaskPanel(QFrame):
    """三段式任务面板浮层控件。

    与现有 `chat_window.py` 接口保持完全兼容（self.plan_panel / show_plan / adjustSize 等）。
    """

    def __init__(self, parent: QWidget, theme_lookup: Callable[[str], str]):
        super().__init__(parent)
        self.setObjectName("planPanel")
        self.theme_lookup = theme_lookup
        self.setVisible(False)
        self.setFixedWidth(360)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)

        # 阴影效果
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(8)
        shadow.setColor(QColor(40, 50, 90, 20))
        self.setGraphicsEffect(shadow)

        self._plan_items: list[dict] = []
        self._plan_spinner_angle = 0
        self._plan_spinner_timer = QTimer(self)
        self._plan_spinner_timer.timeout.connect(self._tick_plan_spinner)
        self.on_undo_request: Callable[[], None] | None = None

        self._build_ui()
        self.apply_theme()

    def _t(self, key: str) -> str:
        try:
            return self.theme_lookup(key)
        except Exception:
            return "#64748b"

    def _build_ui(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(16, 14, 16, 14)
        root_lay.setSpacing(8)

        # ── 顶栏：标题 + 任务标识 + 总体进度 ──
        self.top_row = QWidget(self)
        self.top_row.setStyleSheet("background:transparent;")
        self.top_row.setFixedHeight(24)
        top_layout = QHBoxLayout(self.top_row)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        self.plan_title_icon = QLabel(self.top_row)
        self.plan_title_icon.setFixedSize(16, 16)
        self.plan_title = QLabel("任务目标与计划", self.top_row)
        self.plan_title.setTextFormat(Qt.RichText)
        self.plan_count = QLabel(self.top_row)
        self.plan_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        top_layout.addWidget(self.plan_title_icon, 0, Qt.AlignVCenter)
        top_layout.addWidget(self.plan_title, 0, Qt.AlignVCenter)
        top_layout.addStretch(1)
        top_layout.addWidget(self.plan_count, 0, Qt.AlignVCenter)
        root_lay.addWidget(self.top_row)

        # 进度条
        self.plan_progress = QProgressBar(self)
        self.plan_progress.setRange(0, 100)
        self.plan_progress.setTextVisible(False)
        self.plan_progress.setFixedHeight(5)
        root_lay.addWidget(self.plan_progress)

        # ── 滚动主容器 ──
        self.plan_scroll = QScrollArea(self)
        self.plan_scroll.setFrameShape(QFrame.NoFrame)
        self.plan_scroll.setWidgetResizable(True)
        self.plan_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plan_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.plan_scroll.viewport().setStyleSheet("background:transparent;")

        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background:transparent;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 4, 0, 4)
        self.content_layout.setSpacing(10)

        # 1. 任务切换可撤销横幅（新任务切换时展示）
        self.switch_banner = QFrame(self.content_widget)
        self.switch_banner.setObjectName("switchBanner")
        self.switch_banner.setVisible(False)
        banner_lay = QHBoxLayout(self.switch_banner)
        banner_lay.setContentsMargins(10, 6, 10, 6)
        banner_lay.setSpacing(6)
        self.banner_text = QLabel(self.switch_banner)
        self.banner_text.setWordWrap(True)
        self.banner_text.setStyleSheet("font-size: 11px; color: #854d0e; font-weight: 600;")
        self.undo_btn = QPushButton("撤销任务切换", self.switch_banner)
        self.undo_btn.setCursor(Qt.PointingHandCursor)
        self.undo_btn.setStyleSheet(
            "QPushButton {"
            "  border: 1px solid #ca8a04;"
            "  border-radius: 4px;"
            "  padding: 2px 8px;"
            "  background: #fef08a;"
            "  color: #713f12;"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "}"
            "QPushButton:hover { background: #fde047; }"
        )
        self.undo_btn.clicked.connect(self._on_undo_click)
        banner_lay.addWidget(self.banner_text, 1)
        banner_lay.addWidget(self.undo_btn, 0)
        self.content_layout.addWidget(self.switch_banner)

        # 2. Section 1：要求与固定限制
        self.sec1_frame = QFrame(self.content_widget)
        self.sec1_frame.setObjectName("secFrame")
        sec1_lay = QVBoxLayout(self.sec1_frame)
        sec1_lay.setContentsMargins(8, 8, 8, 8)
        sec1_lay.setSpacing(6)

        sec1_head = QHBoxLayout()
        sec1_head.setContentsMargins(0, 0, 0, 0)
        self.sec1_title = QLabel("要求与固定限制", self.sec1_frame)
        self.sec1_title.setStyleSheet("font-size: 12px; font-weight: 700;")
        self.edit_constraints_btn = QPushButton("编辑限制", self.sec1_frame)
        self.edit_constraints_btn.setCursor(Qt.PointingHandCursor)
        self.edit_constraints_btn.setStyleSheet(
            "QPushButton {"
            "  border: 1px solid #94a3b8;"
            "  border-radius: 4px;"
            "  padding: 1px 7px;"
            "  background: transparent;"
            "  color: #475569;"
            "  font-size: 11px;"
            "}"
            "QPushButton:hover { background: #f1f5f9; color: #1e293b; }"
        )
        self.edit_constraints_btn.clicked.connect(self._on_edit_constraints)
        sec1_head.addWidget(self.sec1_title)
        sec1_head.addStretch(1)
        sec1_head.addWidget(self.edit_constraints_btn)
        sec1_lay.addLayout(sec1_head)

        self.requests_label = QLabel(self.sec1_frame)
        self.requests_label.setWordWrap(True)
        self.requests_label.setTextFormat(Qt.RichText)
        self.requests_label.setStyleSheet("font-size: 11px; line-height: 1.35;")
        sec1_lay.addWidget(self.requests_label)

        self.constraints_label = QLabel(self.sec1_frame)
        self.constraints_label.setWordWrap(True)
        self.constraints_label.setTextFormat(Qt.RichText)
        self.constraints_label.setStyleSheet("font-size: 11px; line-height: 1.35;")
        sec1_lay.addWidget(self.constraints_label)
        self.content_layout.addWidget(self.sec1_frame)

        # 3. Section 2：计划步骤
        self.sec2_frame = QFrame(self.content_widget)
        self.sec2_frame.setObjectName("secFrame")
        sec2_lay = QVBoxLayout(self.sec2_frame)
        sec2_lay.setContentsMargins(8, 8, 8, 8)
        sec2_lay.setSpacing(6)

        self.sec2_title = QLabel("执行计划步骤", self.sec2_frame)
        self.sec2_title.setStyleSheet("font-size: 12px; font-weight: 700;")
        sec2_lay.addWidget(self.sec2_title)

        self.plan_change_note = QLabel(self.sec2_frame)
        self.plan_change_note.setWordWrap(True)
        self.plan_change_note.setTextFormat(Qt.RichText)
        self.plan_change_note.setVisible(False)
        self.plan_change_note.setStyleSheet("font-size: 11px; color: #0284c7; background: #e0f2fe; border-radius: 4px; padding: 4px 6px;")
        sec2_lay.addWidget(self.plan_change_note)

        self.plan_body = QLabel(self.sec2_frame)
        self.plan_body.setTextFormat(Qt.RichText)
        self.plan_body.setWordWrap(True)
        self.plan_body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        sec2_lay.addWidget(self.plan_body)
        self.content_layout.addWidget(self.sec2_frame)

        # 4. Section 3：执行与验证
        self.sec3_frame = QFrame(self.content_widget)
        self.sec3_frame.setObjectName("secFrame")
        sec3_lay = QVBoxLayout(self.sec3_frame)
        sec3_lay.setContentsMargins(8, 8, 8, 8)
        sec3_lay.setSpacing(4)

        self.sec3_title = QLabel("执行与验证状态", self.sec3_frame)
        self.sec3_title.setStyleSheet("font-size: 12px; font-weight: 700;")
        sec3_lay.addWidget(self.sec3_title)

        self.verification_status_label = QLabel(self.sec3_frame)
        self.verification_status_label.setWordWrap(True)
        self.verification_status_label.setTextFormat(Qt.RichText)
        self.verification_status_label.setStyleSheet("font-size: 11px; line-height: 1.35;")
        sec3_lay.addWidget(self.verification_status_label)
        self.content_layout.addWidget(self.sec3_frame)

        self.plan_scroll.setWidget(self.content_widget)
        root_lay.addWidget(self.plan_scroll)

    def apply_theme(self):
        """应用主题样式。"""
        bg_col = self._t("scroll_btn_bg")
        border_col = self._t("sidebar_border")
        title_col = self._t("thinking")
        muted_col = self._t("thinking_msg")

        self.setStyleSheet(
            f"QFrame#planPanel {{"
            f"  background: {bg_col};"
            f"  border: 1px solid {border_col};"
            f"  border-radius: 14px;"
            f"}}"
            f"QFrame#secFrame {{"
            f"  background: transparent;"
            f"  border: 1px solid {border_col};"
            f"  border-radius: 8px;"
            f"}}"
            f"QFrame#switchBanner {{"
            f"  background: #fef9c3;"
            f"  border: 1px solid #fde047;"
            f"  border-radius: 6px;"
            f"}}"
        )
        self.plan_title.setStyleSheet(f"background:transparent; font-size:14px; font-weight:700; color:{title_col};")
        self.plan_count.setStyleSheet(f"font-size:12px; color:{muted_col};")
        self.sec1_title.setStyleSheet(f"font-size:12px; font-weight:700; color:{title_col};")
        self.sec2_title.setStyleSheet(f"font-size:12px; font-weight:700; color:{title_col};")
        self.sec3_title.setStyleSheet(f"font-size:12px; font-weight:700; color:{title_col};")

        self.plan_progress.setStyleSheet(
            "QProgressBar {"
            f"  background: {border_col};"
            "  border: none;"
            "  border-radius: 2px;"
            "}"
            "QProgressBar::chunk {"
            f"  background: {title_col};"
            "  border-radius: 2px;"
            "}"
        )
        self.plan_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollBar:vertical {"
            "  background:transparent; width:5px; margin:2px 0 2px 0;"
            "}"
            "QScrollBar::handle:vertical {"
            f"  background:{self._t('scroll_btn_border')};"
            "  border-radius:2px; min-height:24px;"
            "}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )

    def _on_undo_click(self):
        if self.on_undo_request:
            self.on_undo_request()

    def _on_edit_constraints(self):
        from .. import session as _session_mod
        sess = _session_mod.get_active()
        dlg = EditConstraintsDialog(self.window(), sess, on_saved=lambda: self.render_session(sess))
        dlg.exec()
        if dlg.save_warning:
            show = getattr(self.window(), "show_message", None)
            if callable(show):
                show(dlg.save_warning, "error")

    def _tick_plan_spinner(self):
        self._plan_spinner_angle = (self._plan_spinner_angle + 30) % 360
        if self._plan_items:
            self._render_plan_rows(self._plan_items)

    def _plan_spinner_svg(self, color: str, size: int = 15) -> str:
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

    def _render_plan_rows(self, items: list[dict]):
        title_color = self._t("thinking")
        muted_color = self._t("thinking_msg")
        hl_bg = self._t("thinking_bg")
        rows = []
        for it in items:
            txt = (it.get("text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            status = it.get("status")
            if status == "done":
                icon = '<span style="color:#10b981; font-weight:bold; font-size:13px;">✓</span>'
                label, label_color = "完成", "#10b981"
                text_style = f"color:{muted_color};"
                cell_bg = ""
            elif status == "in_progress":
                icon = self._plan_spinner_svg(title_color, 14)
                label, label_color = "进行中", title_color
                text_style = f"color:{title_color};font-weight:600;"
                cell_bg = f"background:{hl_bg};"
            else:
                icon = '<span style="color:#94a3b8; font-size:12px;">○</span>'
                label, label_color = "待办", muted_color
                text_style = ""
                cell_bg = ""
            rows.append(
                '<tr>'
                f'<td width="20" style="{cell_bg}padding:2px 0 3px 4px;vertical-align:top;">{icon}</td>'
                f'<td style="{cell_bg}{text_style}padding:2px 6px 3px 2px;line-height:1.35;">'
                f'<span style="color:{label_color};font-size:10px;">{label}</span>&nbsp;{txt}</td>'
                '</tr>'
            )
        html_text = '<table cellspacing="0" cellpadding="0" width="100%">' + "".join(rows) + "</table>"
        self.plan_body.setText(html_text)

    def render_session(self, sess: Any):
        """核心渲染入口：由主线程调度渲染当前会话的任务面板。"""
        self.apply_theme()
        task = getattr(sess, "current_task", None)
        items = list(getattr(sess, "current_plan", None) or [])
        self._plan_items = items

        # 普通问答无任务且无计划：隐藏面板，不打扰用户
        if not task and not items:
            self._plan_spinner_timer.stop()
            self.setVisible(False)
            return

        # ── 1. 顶栏总体进度 ──
        done = sum(1 for it in items if it.get("status") == "done")
        total = len(items)
        if total > 0:
            self.plan_count.setText(f"{done}/{total} 完成")
            self.plan_progress.setValue(round(done / total * 100))
            self.plan_progress.setVisible(True)
        else:
            self.plan_count.setText("准备中")
            self.plan_progress.setValue(0)
            self.plan_progress.setVisible(False)

        # ── 2. 新任务切换横幅 ──
        last_switch = getattr(sess, "last_task_switch", None)
        if isinstance(last_switch, dict) and last_switch.get("new_task_id") == (task.get("id") if task else None):
            reason = last_switch.get("reason") or "未说明"
            self.banner_text.setText(f"已开始新任务（原因：{reason}）")
            self.switch_banner.setVisible(True)
        else:
            self.switch_banner.setVisible(False)

        # ── 3. Section 1：要求与固定限制 ──
        req_items = _ts.task_requests(sess, task) if task else []

        req_lines = []
        for it in req_items:
            prefix = "原始要求" if it.get("is_initial") else "补充要求"
            txt = html.escape(it.get("text", "")[:120])
            mid = html.escape(it.get("id", ""))
            # 来源如实标注：旧会话里说不清是谁写的，原文不在历史里的，都不能画得和用户原话一样
            source_note = _REQUEST_SOURCE_NOTES.get(it.get("kind"), "")
            req_lines.append(
                f"<b>{prefix}</b>（<span style='color:#64748b;'>{mid[:10]}</span>{source_note}）：{txt}")

        if not req_lines:
            req_lines.append("<span style='color:#94a3b8;'>暂无关联的用户需求来源</span>")
        self.requests_label.setText("<br>".join(req_lines))

        constraints = (task.get("pinned_constraints") or []) if task else []
        if constraints:
            c_html = ["<b>固定限制</b>（严格遵守）："]
            for c in constraints:
                c_html.append(f"• <span style='color:#2563eb;'>{html.escape(c)}</span>")
            self.constraints_label.setText("<br>".join(c_html))
        else:
            self.constraints_label.setText("<span style='color:#94a3b8;'>暂无固定限制（点击上方编辑添加）</span>")

        # ── 4. Section 2：计划步骤与变动 ──
        if items:
            self.sec2_frame.setVisible(True)
            self._render_plan_rows(items)
            if any(it.get("status") == "in_progress" for it in items):
                if not self._plan_spinner_timer.isActive():
                    self._plan_spinner_timer.start(80)
            else:
                self._plan_spinner_timer.stop()
        else:
            self.sec2_frame.setVisible(False)
            self._plan_spinner_timer.stop()

        reason = (task.get("last_plan_change_reason") or "") if task else ""
        summary = (task.get("last_plan_change_summary") or "") if task else ""
        if reason or summary:
            parts = []
            if summary:
                parts.append(f"<b>变化</b>：{html.escape(summary)}")
            if reason:
                parts.append(f"<b>原因</b>：{html.escape(reason)}")
            self.plan_change_note.setText("；".join(parts))
            self.plan_change_note.setVisible(True)
        else:
            self.plan_change_note.setVisible(False)

        # ── 5. Section 3：执行与验证状态 ──
        # 只陈述有证据的事实。义务是否仍未解除以 verification_status 的 gaps 为准
        # （复用现有验证体系的缺口判定，与模型上下文同一结论）；dirty / 盲区只是
        # "本轮涉及过"的记录，记录还在不等于义务没解除，不据此画"待验证"。
        st = _ts.verification_status(sess)
        gaps = st.get("gaps") or []
        v_status_lines = []
        if gaps:
            v_status_lines.append(
                f"<span style='color:#ea580c; font-weight:600;'>待完成的验证（{len(gaps)} 项）：</span>")
            for g in gaps[:4]:
                v_status_lines.append(
                    f"• <span style='color:#c2410c;'>{html.escape(_one_line(g, 90))}</span>")
            if len(gaps) > 4:
                v_status_lines.append(f"（另有 {len(gaps) - 4} 项）")
        elif st["state"] == "failed":
            v_status_lines.append(f"<span style='color:#dc2626; font-weight:600;'>最近的检查有 {st['failed']} 项未通过</span>")
        elif st["state"] == "checked":
            v_status_lines.append(
                f"<span style='color:#16a34a;'>已执行的检查通过（{st['passed']} 项）</span>"
                "<span style='color:#94a3b8;'>；不代表所有要求都已满足</span>")
        elif st["inconclusive"]:
            # 跑过但结论是未执行/已取消/结果未知：说成"尚未执行检查"就抹掉了跑过这件事
            v_status_lines.append(
                f"<span style='color:#b45309;'>已尝试 {st['inconclusive']} 项检查，但没有得到通过结论"
                "（未执行、已取消或结果未知）</span>")
        else:
            v_status_lines.append("<span style='color:#94a3b8;'>尚未执行检查，没有验证证据</span>")
        self.verification_status_label.setText("<br>".join(v_status_lines))

        self.layout().invalidate()
        self.layout().activate()
        self.setVisible(True)
        self.raise_()
        self._fit_height()

    def _fit_height(self):
        """自适应高度，最大不超过聊天区高度的 70% 或 460px。"""
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setTextWidth(self.width() - 36)

        # 估算内容高度并给滚动区设置合适的高度
        parent_h = self.parentWidget().height() if self.parentWidget() else 600
        max_h = min(480, max(120, parent_h - 120))

        content_h = self.content_widget.sizeHint().height()
        scroll_target = min(content_h + 10, max_h - 60)
        self.plan_scroll.setFixedHeight(scroll_target)
        self.setFixedHeight(scroll_target + 60)
