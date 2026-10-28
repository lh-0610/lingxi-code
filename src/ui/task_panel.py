"""任务面板（TaskPanel）与固定限制编辑对话框（B05）。

三段式任务面板：
1. 要求与限制（Requirements & Constraints）：
   - 展示用户原始要求、补充要求，保留消息来源 ID
   - 展示用户设定的固定限制（pinned_constraints）
   - [编辑限制] 按钮呼出编辑框，带 1000 字符硬预算校验与生效提示
2. 计划与进展（Plan & Progress）：
   - 计划步骤圆标（完成 / 进行中 / 待办）与进度条；进行中的圆环只在本轮运行时转动
   - 最近计划变动原因与摘要
   - 新任务切换可撤销横幅：已开始新任务（原因：……）[撤销任务切换]
3. 执行与核对（Execution & Verification）：
   - 尚未解除的验证义务（verification_status 的缺口判定，与运行态注入同一结论）
4. 验证证据：每项检查最后一次的结论（与结果卡共用 result_view.effective_rows 的归并），
   过期的照实标出；再列本会话还在跑的后台命令。

面板像 Claude / Codex 的侧栏一样由用户开关：顶栏按钮打开 / 收起，没有任务时显示空状态说明，
不会因为普通问答自己消失。左边缘可以拖宽。窗口放不下时 ChatUI 把它改成盖在对话区上的浮层。
"""
from __future__ import annotations

import html
from typing import Any, Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
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
from .fonts import MONO_CSS
from .theme import color as _theme_color

_REQUEST_SOURCE_NOTES = {   # 画的时候按当前主题上色
    "legacy_unknown": "；来源未确认",
    "missing": "；原文已不在历史中",
}

_EVIDENCE_MAX = 6   # 验证证据最多列几行，其余折成"另有 N 项"
_EVIDENCE_COLORS = {"passed": "mv_ok", "failed": "mv_bad", "error": "mv_bad", "timeout": "mv_orange"}


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
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        self._apply_style()

        title = QLabel("编辑固定限制", self)
        title.setObjectName("dlgTitle")
        layout.addWidget(title)
        # 提示说明：行首的 -、*、编号也按原文保存，写在悬停提示里，不占地方
        hint = QLabel("每行一条，按原文保存，从下一轮起对这个任务一直生效。只有你能在这里改，模型改不了。", self)
        hint.setObjectName("dlgHint")
        hint.setWordWrap(True)
        hint.setToolTip("行首的 -、*、编号等字符都算内容；已保存的条目再次保存不会被改动。")
        layout.addWidget(hint)

        # 文本框
        self.editor = QPlainTextEdit(self)
        self.editor.setAccessibleName("固定限制")
        self.editor.setPlaceholderText("每行一条，例如：\n必须兼容 Windows\n不改对外行为，测试要全部通过\n导出格式必须支持 CSV")
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
        self.char_count_label.setObjectName("dlgCount")
        self.char_count_label.setWordWrap(True)      # 拒绝保存的说明较长，不换行会撑宽定宽对话框被裁掉
        bottom_row.addWidget(self.char_count_label, 1)

        self.cancel_btn = QPushButton("取消", self)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)
        bottom_row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存限制", self)
        self.save_btn.setObjectName("dlgPrimary")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        bottom_row.addWidget(self.save_btn)

        layout.addLayout(bottom_row)
        self._on_text_changed()

    def _apply_style(self):
        """配色取当前主题（深色下不能是白底），样式集中在对话框自己的样式表里。"""
        c = _theme_color
        self.setStyleSheet(
            f"QDialog {{ background: {c('mv_view_bg')}; }}"
            f"QLabel {{ background: transparent; color: {c('mv_text')}; font-size: 13px; }}"
            f"QLabel#dlgTitle {{ font-size: 16px; font-weight: 600; }}"
            f"QLabel#dlgHint {{ color: {c('mv_text_sec')}; }}"
            f"QLabel#dlgCount {{ color: {c('mv_text_sec')}; font-size: 12px; }}"
            f"QLabel#dlgCount[error=\"true\"] {{ color: {c('mv_err_text')}; font-weight: 600; }}"
            f"QPlainTextEdit {{ border: 1px solid {c('mv_chip_border')}; border-radius: 8px; padding: 8px 10px;"
            f" font-size: 13px; background: {c('mv_card_bg')}; color: {c('mv_text')}; }}"
            f"QPlainTextEdit:focus {{ border-color: {c('mv_indigo')}; }}"
            f"QPushButton {{ border: 1px solid {c('mv_chip_border')}; border-radius: 8px; padding: 6px 14px;"
            f" background: {c('mv_card_bg')}; color: {c('mv_text3')}; font-size: 13px; }}"
            f"QPushButton:hover {{ border-color: {c('mv_chip_hover')}; }}"
            f"QPushButton#dlgPrimary {{ border: none; background: {c('mv_indigo')}; color: {c('mv_view_bg')};"
            f" font-weight: 600; }}"
            f"QPushButton#dlgPrimary:disabled {{ background: {c('mv_muted3')}; }}")

    def _set_count_error(self, error: bool):
        if self.char_count_label.property("error") != ("true" if error else "false"):
            self.char_count_label.setProperty("error", "true" if error else "false")
            self.char_count_label.style().unpolish(self.char_count_label)
            self.char_count_label.style().polish(self.char_count_label)

    def _on_text_changed(self):
        # 计数与保存校验走同一个函数：各算各的话，界面说"没超"、保存却被拒（或反过来）。
        _, _, cleaned = _ts.validate_pinned_constraints(self.editor.toPlainText())
        total_chars = sum(len(c) for c in cleaned)
        limit = _ts.PINNED_CONSTRAINTS_MAX_CHARS
        self.char_count_label.setText(f"{total_chars} / {limit} 字")
        over = total_chars > limit
        self._set_count_error(over)
        self.save_btn.setEnabled(not over)

    def _on_save(self):
        text = self.editor.toPlainText()
        valid, error_msg, cleaned = _ts.validate_pinned_constraints(text)
        if not valid:
            self._show_error(error_msg)
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
        self._set_count_error(True)


_STEP_NAMES = {"done": "完成", "in_progress": "进行中"}


class _StepMarker(QWidget):
    """计划步骤前的 14px 圆标：完成是实心圆加勾，进行中是带缺口的圆环，待办是空心圆。

    自己画而不是塞进富文本：转动时只重绘这一个小控件，不用整段 HTML 每帧重排。
    颜色在绘制时按当前主题取，切换深浅色后重绘即可。
    """

    SIZE = 14

    def __init__(self, status: str, theme_lookup: Callable[[str], str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.status = status
        self._t = theme_lookup
        self.angle = 0
        name = _STEP_NAMES.get(status, "待办")
        self.setAccessibleName(name)
        self.setToolTip(name)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.status == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self._t("mv_ok")))
            p.drawEllipse(QRectF(0, 0, self.SIZE, self.SIZE))
            pen = QPen(QColor(self._t("sidebar_bg")), 1.8)   # 勾用面板底色，深浅色下都看得清
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(4.4, 7.3), QPointF(6.2, 9.0), QPointF(9.6, 5.0)])
        elif self.status == "in_progress":
            pen = QPen(QColor(self._t("mv_indigo")), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            # 缺口朝右（和设计稿一致），转动时整段弧顺时针走
            p.drawArc(QRectF(1, 1, self.SIZE - 2, self.SIZE - 2), (45 - self.angle) * 16, 270 * 16)
        else:
            p.setPen(QPen(QColor(self._t("mv_muted")), 1.5))
            p.drawEllipse(QRectF(1.25, 1.25, self.SIZE - 2.5, self.SIZE - 2.5))
        p.end()


class TaskPanel(QFrame):
    """任务面板：主窗口最右侧的一栏，和窗口一样高，内容多了在面板里滚动。
    显示与否由 ChatUI 决定（set_collapsed）；宽度可拖左边缘调整（width_changed 通知保存）。

    与现有 `chat_window.py` 接口保持兼容（self.plan_panel / render_session 等）。
    """

    MIN_WIDTH = 300
    MAX_WIDTH = 560
    DEFAULT_WIDTH = 360
    _GRIP = 6                        # 左边缘这么宽的一条可以拖动改宽度

    width_changed = Signal(int)      # 拖完松手时发，ChatUI 记进界面偏好
    step_clicked = Signal(int, str)  # 点了计划里的第 n 步（步号, 步骤名）：ChatUI 在消息流里定位
    file_clicked = Signal(str)       # 点了改动文件（相对路径）：ChatUI 打开它的差异

    _FILES_MAX = 8                   # 改动文件最多列几个，其余折成"另有 N 个"

    def __init__(self, parent: QWidget, theme_lookup: Callable[[str], str]):
        super().__init__(parent)
        self.setObjectName("planPanel")
        self.theme_lookup = theme_lookup
        self.setVisible(False)
        self.setFixedWidth(self.DEFAULT_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setMouseTracking(True)      # 鼠标移到左边缘时换成左右拖动的光标
        self._drag_from = None           # 拖宽时：(按下时的全局 x, 按下时的宽度)
        self.max_width_fn: Callable[[], int] | None = None   # ChatUI 给：当前窗口下最多能拖多宽
        self._overlay = False
        self._has_content = False   # 有任务或计划
        self._collapsed = False     # 用户收起 / 窗口放不下
        # 面板显示时每 2 秒刷新一次验证证据：工具跑完、后台命令的已运行时间都能跟上，
        # 不必等下一次计划更新。只读内存里的记录，很便宜。
        self._evidence_sess = None
        self._evidence_timer = QTimer(self)
        self._evidence_timer.setInterval(2000)
        self._evidence_timer.timeout.connect(self._refresh_evidence)

        self._plan_items: list[dict] = []
        self._plan_markers: list[_StepMarker] = []   # 进行中步骤的圆标，转动时逐个重绘
        self._plan_spinner_angle = 0
        self._plan_spinner_timer = QTimer(self)
        self._plan_spinner_timer.setInterval(33)
        self._plan_spinner_timer.timeout.connect(self._tick_plan_spinner)
        self.on_undo_request: Callable[[], None] | None = None
        # ChatUI 给：取会话改动的文件和增删行数 [(相对路径, 增, 删, 是否新文件)]（面板只画不查 git）
        self.files_provider: Callable[[Any], list] | None = None

        self._build_ui()
        self.apply_theme()
        self._show_empty(True)        # 还没渲染过会话：先是空状态，不是一排空白标签

    def _t(self, key: str) -> str:
        try:
            return self.theme_lookup(key)
        except Exception:
            return "#64748b"

    def _build_ui(self):
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(18, 16, 18, 16)
        root_lay.setSpacing(8)

        # ── 顶栏：标题 + 任务标识 + 总体进度 ──
        self.top_row = QWidget(self)
        # 样式表都带选择器：不带选择器的规则会盖住面板给子控件写的规则（比如行的悬停底色）
        self.top_row.setObjectName("panelTopRow")
        self.top_row.setStyleSheet("#panelTopRow { background: transparent; }")
        self.top_row.setFixedHeight(24)
        top_layout = QHBoxLayout(self.top_row)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        self.plan_title_icon = QLabel(self.top_row)   # 旧属性，ChatUI 仍有别名；标题前不放图标
        self.plan_title_icon.setFixedSize(16, 16)
        self.plan_title_icon.setVisible(False)
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
        self.plan_scroll.viewport().setStyleSheet("#qt_scrollarea_viewport { background: transparent; }")

        self.content_widget = QWidget()
        self.content_widget.setObjectName("panelContent")
        self.content_widget.setStyleSheet("#panelContent { background: transparent; }")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 4, 0, 4)
        self.content_layout.setSpacing(12)

        # 0. 空状态：还没有任务时面板也在，说明这里会出现什么；固定限制可以先加上
        self.empty_box = QWidget(self.content_widget)
        empty_lay = QVBoxLayout(self.empty_box)
        empty_lay.setContentsMargins(0, 18, 0, 0)
        empty_lay.setSpacing(8)
        self.empty_title = QLabel("还没有任务", self.empty_box)
        self.empty_title.setObjectName("emptyTitle")
        self.empty_text = QLabel(
            "开始一件需要几步才能做完的事，这里会列出你的要求、执行计划、验证状态和证据。"
            "普通问答不会出现在这里。", self.empty_box)
        self.empty_text.setObjectName("emptyText")
        self.empty_text.setWordWrap(True)
        self.empty_btn = QPushButton("添加固定限制", self.empty_box)
        self.empty_btn.setCursor(Qt.PointingHandCursor)
        self.empty_btn.setToolTip("先写下必须遵守的限制（如必须兼容 Windows），之后的任务都会照着做")
        self.empty_btn.clicked.connect(self._on_edit_constraints)
        empty_lay.addWidget(self.empty_title)
        empty_lay.addWidget(self.empty_text)
        empty_lay.addWidget(self.empty_btn, 0, Qt.AlignLeft)
        self.empty_box.setVisible(False)
        self.content_layout.addWidget(self.empty_box)

        # 1. 任务切换可撤销横幅（新任务切换时展示）
        self.switch_banner = QFrame(self.content_widget)
        self.switch_banner.setObjectName("switchBanner")
        self.switch_banner.setVisible(False)
        banner_lay = QHBoxLayout(self.switch_banner)
        banner_lay.setContentsMargins(10, 6, 10, 6)
        banner_lay.setSpacing(6)
        self.banner_text = QLabel(self.switch_banner)
        self.banner_text.setWordWrap(True)
        self.undo_btn = QPushButton("撤销任务切换", self.switch_banner)
        self.undo_btn.setCursor(Qt.PointingHandCursor)
        self.undo_btn.clicked.connect(self._on_undo_click)
        banner_lay.addWidget(self.banner_text, 1)
        banner_lay.addWidget(self.undo_btn, 0)
        self.content_layout.addWidget(self.switch_banner)

        # 2. Section 1：要求与固定限制
        self.sec1_frame = QFrame(self.content_widget)
        self.sec1_frame.setObjectName("secFrame")
        sec1_lay = QVBoxLayout(self.sec1_frame)
        sec1_lay.setContentsMargins(0, 12, 0, 4)
        sec1_lay.setSpacing(6)

        sec1_head = QHBoxLayout()
        sec1_head.setContentsMargins(0, 0, 0, 0)
        self.sec1_title = QLabel("要求与固定限制", self.sec1_frame)
        self.sec1_title.setStyleSheet("font-size: 12px; font-weight: 600;")
        self.edit_constraints_btn = QPushButton("编辑限制", self.sec1_frame)
        self.edit_constraints_btn.setCursor(Qt.PointingHandCursor)
        self.edit_constraints_btn.clicked.connect(self._on_edit_constraints)
        sec1_head.addWidget(self.sec1_title)
        sec1_head.addStretch(1)
        sec1_head.addWidget(self.edit_constraints_btn)
        sec1_lay.addLayout(sec1_head)

        self.requests_label = QLabel(self.sec1_frame)
        self.requests_label.setWordWrap(True)
        self.requests_label.setTextFormat(Qt.RichText)
        self.requests_label.setStyleSheet("font-size: 12px; line-height: 1.35;")
        sec1_lay.addWidget(self.requests_label)

        self.constraints_label = QLabel(self.sec1_frame)
        self.constraints_label.setWordWrap(True)
        self.constraints_label.setTextFormat(Qt.RichText)
        self.constraints_label.setStyleSheet("font-size: 12px; line-height: 1.35;")
        sec1_lay.addWidget(self.constraints_label)
        self.content_layout.addWidget(self.sec1_frame)

        # 3. Section 2：计划步骤
        self.sec2_frame = QFrame(self.content_widget)
        self.sec2_frame.setObjectName("secFrame")
        sec2_lay = QVBoxLayout(self.sec2_frame)
        sec2_lay.setContentsMargins(0, 12, 0, 4)
        sec2_lay.setSpacing(6)

        self.sec2_title = QLabel("执行计划步骤", self.sec2_frame)
        self.sec2_title.setStyleSheet("font-size: 12px; font-weight: 600;")
        sec2_lay.addWidget(self.sec2_title)

        # 步骤一行一个：左边圆标，右边文字（_render_plan_rows 重建）
        self.plan_body = QWidget(self.sec2_frame)
        self.plan_rows_lay = QVBoxLayout(self.plan_body)
        self.plan_rows_lay.setContentsMargins(0, 2, 0, 2)
        self.plan_rows_lay.setSpacing(8)
        sec2_lay.addWidget(self.plan_body)

        # 最近一次计划变动，排在步骤下面
        self.plan_change_note = QLabel(self.sec2_frame)
        self.plan_change_note.setWordWrap(True)
        self.plan_change_note.setTextFormat(Qt.RichText)
        self.plan_change_note.setVisible(False)
        sec2_lay.addWidget(self.plan_change_note)
        self.content_layout.addWidget(self.sec2_frame)

        # 3.5 改动的文件：这一轮写过的文件和增删行数，点开看差异
        self.files_frame = QFrame(self.content_widget)
        self.files_frame.setObjectName("secFrame")
        files_lay = QVBoxLayout(self.files_frame)
        files_lay.setContentsMargins(0, 12, 0, 4)
        files_lay.setSpacing(4)
        files_head = QHBoxLayout()
        files_head.setContentsMargins(0, 0, 0, 0)
        self.files_title = QLabel("改动的文件", self.files_frame)
        self.files_title.setToolTip("这一轮写过的文件；上一轮还没验证完的也会带过来。行数是此刻相对上次提交的差异")
        self.files_count = QLabel(self.files_frame)
        self.files_count.setObjectName("filesCount")
        files_head.addWidget(self.files_title)
        files_head.addStretch(1)
        files_head.addWidget(self.files_count)
        files_lay.addLayout(files_head)
        self.files_body = QWidget(self.files_frame)
        self.files_rows_lay = QVBoxLayout(self.files_body)
        self.files_rows_lay.setContentsMargins(0, 2, 0, 0)
        self.files_rows_lay.setSpacing(0)
        files_lay.addWidget(self.files_body)
        self.files_more = QLabel(self.files_frame)
        self.files_more.setObjectName("filesMore")
        files_lay.addWidget(self.files_more)
        self.files_frame.setVisible(False)
        self.content_layout.addWidget(self.files_frame)

        # 4. Section 3：执行与验证
        self.sec3_frame = QFrame(self.content_widget)
        self.sec3_frame.setObjectName("secFrame")
        sec3_lay = QVBoxLayout(self.sec3_frame)
        sec3_lay.setContentsMargins(0, 12, 0, 4)
        sec3_lay.setSpacing(4)

        self.sec3_title = QLabel("执行与验证状态", self.sec3_frame)
        self.sec3_title.setStyleSheet("font-size: 12px; font-weight: 600;")
        sec3_lay.addWidget(self.sec3_title)

        self.verification_status_label = QLabel(self.sec3_frame)
        self.verification_status_label.setWordWrap(True)
        self.verification_status_label.setTextFormat(Qt.RichText)
        self.verification_status_label.setStyleSheet("font-size: 12px; line-height: 1.35;")
        sec3_lay.addWidget(self.verification_status_label)
        self.content_layout.addWidget(self.sec3_frame)

        # 5. Section 4：验证证据
        self.sec4_frame = QFrame(self.content_widget)
        self.sec4_frame.setObjectName("secFrame")
        sec4_lay = QVBoxLayout(self.sec4_frame)
        sec4_lay.setContentsMargins(0, 12, 0, 4)
        sec4_lay.setSpacing(6)
        self.sec4_title = QLabel("验证证据", self.sec4_frame)
        sec4_lay.addWidget(self.sec4_title)
        self.evidence_label = QLabel(self.sec4_frame)
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setTextFormat(Qt.RichText)
        self.evidence_label.setStyleSheet("font-size: 12px;")
        sec4_lay.addWidget(self.evidence_label)
        self.content_layout.addWidget(self.sec4_frame)
        self.content_layout.addStretch(1)

        self.plan_scroll.setWidget(self.content_widget)
        root_lay.addWidget(self.plan_scroll, 1)

    def apply_theme(self):
        """停靠样式：和侧栏同底色、左边一条分隔线；分段之间只用一条细线隔开。"""
        bg = self._t("sidebar_bg")
        line = self._t("sidebar_border")
        text = self._t("text")
        dim = self._t("text_dim")
        accent = self._t("mv_indigo")
        chip = self._t("mv_chip_border")
        # 浮层（窗口放不下时盖在对话区上）：左边一条更明显的分隔线，和对话区分开
        edge = f"2px solid {self._t('mv_chip_border')}" if self._overlay else f"1px solid {line}"
        self.setStyleSheet(
            f"QFrame#planPanel {{ background: {bg}; border: none; border-left: {edge}; }}"
            f"QLabel#emptyTitle {{ font-size: 14px; font-weight: 600; color: {text}; }}"
            f"QLabel#emptyText {{ font-size: 13px; color: {self._t('mv_text_sec')}; }}"
            f"QWidget#planRow, QWidget#fileRow {{ background: transparent; border-radius: 6px; }}"
            # 悬停底色用浅主题色：面板底色和 mv_header_bg 是同一个灰，用它悬停看不出变化
            f"QWidget#planRow:hover, QWidget#fileRow:hover {{ background: {self._t('mv_indigo_soft')}; }}"
            f"QLabel#fileName {{ font-family: {MONO_CSS}; font-size: 12px; color: {text}; }}"
            f"QLabel#filesCount, QLabel#filesMore {{ font-size: 12px; color: {self._t('mv_text_sec')}; }}"
            f"QFrame#secFrame {{ background: transparent; border: none; border-top: 1px solid {line}; }}"
            f"QFrame#switchBanner {{ background: {self._t('mv_indigo_soft')}; border: none; border-radius: 8px; }}"
            f"QLabel {{ color: {text}; background: transparent; }}"
            f"QLabel#planStep {{ font-size: 13px; color: {text}; }}"
            f"QLabel#planStepDone {{ font-size: 13px; color: {self._t('mv_text_sec')}; }}"
        )
        for marker in self.findChildren(_StepMarker):
            marker.update()
        self.plan_title.setStyleSheet(f"font-size:14px; font-weight:600; color:{text};")
        self.plan_count.setStyleSheet(f"font-size:12px; color:{dim};")
        for title in (self.sec1_title, self.sec2_title, self.files_title, self.sec3_title, self.sec4_title):
            title.setStyleSheet(f"font-size:13px; font-weight:600; color:{text};")
        btn_qss = (
            f"QPushButton {{ border: 1px solid {chip}; border-radius: 8px; padding: 2px 10px;"
            f" background: {self._t('mv_card_bg')}; color: {text}; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {self._t('mv_chip_hover')}; }}"
        )
        self.edit_constraints_btn.setStyleSheet(btn_qss)
        self.undo_btn.setStyleSheet(btn_qss)
        self.empty_btn.setStyleSheet(btn_qss + "QPushButton { padding: 4px 12px; }")
        self.banner_text.setStyleSheet(f"font-size: 12px; color: {text};")
        self.plan_change_note.setStyleSheet(
            f"font-size: 12px; color: {dim}; background: {self._t('mv_header_bg')};"
            " border-radius: 6px; padding: 4px 8px;")
        self.plan_progress.setStyleSheet(
            f"QProgressBar {{ background: {chip}; border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}"
        )
        self.plan_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollBar:vertical { background:transparent; width:5px; margin:2px 0 2px 0; }"
            f"QScrollBar::handle:vertical {{ background:{chip}; border-radius:2px; min-height:24px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        )

    def set_collapsed(self, collapsed: bool):
        """显示 / 收起由 ChatUI 决定（用户的开关、窗口放不放得下）；有没有任务都显示，
        没任务时是空状态。"""
        self._collapsed = bool(collapsed)
        self.setVisible(not self._collapsed)

    def set_overlay(self, overlay: bool):
        """窗口放不下时 ChatUI 把面板改成盖在对话区上的浮层，样式上和停靠时略有区分。"""
        if overlay != self._overlay:
            self._overlay = overlay
            self.apply_theme()

    # —— 拖左边缘改宽度 ——
    def _max_width(self) -> int:
        limit = self.MAX_WIDTH
        if self.max_width_fn is not None:
            try:
                limit = min(limit, int(self.max_width_fn()))
            except Exception:
                pass
        return max(self.MIN_WIDTH, limit)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().x() <= self._GRIP:
            self._drag_from = (event.globalPosition().x(), self.width())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            start_x, start_w = self._drag_from
            width = int(start_w + (start_x - event.globalPosition().x()))   # 往左拖变宽
            self.setFixedWidth(max(self.MIN_WIDTH, min(self._max_width(), width)))
            event.accept()
            return
        self.setCursor(Qt.SizeHorCursor if event.position().x() <= self._GRIP else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self.width_changed.emit(self.width())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if self._drag_from is None:
            self.unsetCursor()
        super().leaveEvent(event)

    def has_content(self) -> bool:
        return self._has_content

    def showEvent(self, event):
        super().showEvent(event)
        self._evidence_timer.start()
        self._refresh_evidence()   # 收起期间可能跑完了检查、圆环也该接着转 / 停

    def hideEvent(self, event):
        self._evidence_timer.stop()
        self.set_running(False)   # 看不见就不转，圆环回到缺口朝右
        super().hideEvent(event)

    def _refresh_evidence(self):
        if self._evidence_sess is not None:
            if self._has_content:
                self._render_evidence(self._evidence_sess)
            self._render_files(self._evidence_sess)
            self._sync_spinner(self._evidence_sess)

    def _render_files(self, sess: Any):
        """改动的文件：名字（太长从中间省略）+ 增删行数；点了在 ChatUI 里看差异。
        行数由 files_provider 给（它按改动次数缓存 git 结果），说不出就不写。"""
        rows = []
        if self.files_provider is not None and sess is not None:
            try:
                rows = list(self.files_provider(sess) or [])
            except Exception:
                rows = []
        self.files_frame.setVisible(bool(rows))
        theme = self._t("mv_ok")
        if (rows, theme) == getattr(self, "_files_drawn", None):
            return          # 每 2 秒刷新一次：内容没变就不重建（重建会让悬停的那一行闪一下）
        self._files_drawn = (rows, theme)
        while self.files_rows_lay.count():
            w = self.files_rows_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)      # 立即摘下：deleteLater 要等下一轮事件循环，期间旧行还挂着
                w.deleteLater()
        if not rows:
            return
        self.files_count.setText(f"{len(rows)} 个")
        ok, bad, muted = self._t("mv_ok"), self._t("mv_bad"), self._t("mv_muted")
        for path, added, removed, is_new in rows[:self._FILES_MAX]:
            row = QWidget(self.files_body)
            row.setObjectName("fileRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setAttribute(Qt.WA_Hover, True)
            row.setCursor(Qt.PointingHandCursor)
            row.setToolTip(f"{path}\n点开看此刻相对上次提交的差异")
            row.mousePressEvent = lambda _e, p=path: self.file_clicked.emit(p)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(4, 3, 4, 3)
            lay.setSpacing(10)
            name = QLabel(row)
            name.setObjectName("fileName")
            name.setText(name.fontMetrics().elidedText(path, Qt.ElideMiddle, 230))
            lay.addWidget(name, 1)
            parts = []
            if is_new:
                parts.append(f"<span style='color:{muted};'>新文件</span>")
            if added is not None:
                parts.append(f"<span style='color:{ok};'>+{added}</span>")
            if removed is not None and not is_new:
                parts.append(f"<span style='color:{bad};'>−{removed}</span>")
            stat = QLabel("&nbsp;".join(parts), row)
            stat.setTextFormat(Qt.RichText)
            stat.setStyleSheet(f"font-family: {MONO_CSS}; font-size: 12px; background: transparent;")
            lay.addWidget(stat, 0)
            self.files_rows_lay.addWidget(row)
        extra = len(rows) - self._FILES_MAX
        self.files_more.setText(f"另有 {extra} 个文件，见结果卡的「查看改动」" if extra > 0 else "")
        self.files_more.setVisible(extra > 0)

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
        self._plan_spinner_angle = (self._plan_spinner_angle + 12) % 360
        for marker in self._plan_markers:
            marker.angle = self._plan_spinner_angle
            marker.update()

    def _sync_spinner(self, sess: Any):
        self.set_running(bool(getattr(sess, "is_generating", False)))

    def set_running(self, running: bool):
        """有进行中的步骤、且这一轮确实在跑时才转；停下来就是一个静止的缺口圆环。"""
        spinning = bool(self._plan_markers) and running
        if spinning and not self._plan_spinner_timer.isActive():
            self._plan_spinner_timer.start()
        elif not spinning and self._plan_spinner_timer.isActive():
            self._plan_spinner_timer.stop()
            self._plan_spinner_angle = 0
            for marker in self._plan_markers:
                marker.angle = 0
                marker.update()

    def _render_plan_rows(self, items: list[dict]):
        while self.plan_rows_lay.count():
            w = self.plan_rows_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)      # 立即摘下：deleteLater 要等下一轮事件循环，期间旧行还挂着
                w.deleteLater()
        self._plan_markers = []
        for n, it in enumerate(items, start=1):
            status = it.get("status")
            row = QWidget(self.plan_body)
            # 点一下在对话里找到这一步（消息流里的步骤分段）
            row.setObjectName("planRow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            row.setAttribute(Qt.WA_Hover, True)
            row.setCursor(Qt.PointingHandCursor)
            row.setToolTip("在对话里找到这一步" if status != "pending" else "这一步还没开始")
            text = it.get("text") or ""
            row.mousePressEvent = lambda _e, n=n, t=text: self.step_clicked.emit(n, t)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(2, 2, 4, 2)
            lay.setSpacing(10)
            # 第一行文字约 19px 高、圆标 14px：圆标下移 3px，对齐第一行的中线
            marker_box = QVBoxLayout()
            marker_box.setContentsMargins(0, 3, 0, 0)
            marker = _StepMarker(status, self._t, row)
            marker.angle = self._plan_spinner_angle
            marker_box.addWidget(marker)
            marker_box.addStretch(1)
            lay.addLayout(marker_box)
            label = QLabel(it.get("text") or "", row)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setObjectName("planStepDone" if status == "done" else "planStep")   # 颜色见 apply_theme
            lay.addWidget(label, 1)
            self.plan_rows_lay.addWidget(row)
            if status == "in_progress":
                self._plan_markers.append(marker)

    def render_session(self, sess: Any):
        """核心渲染入口：由主线程调度渲染当前会话的任务面板。"""
        self.apply_theme()
        task = getattr(sess, "current_task", None)
        items = list(getattr(sess, "current_plan", None) or [])
        self._plan_items = items

        # 普通问答无任务且无计划：面板照常在（由用户开关），显示空状态说明
        if not task and not items:
            self._plan_spinner_timer.stop()
            self._has_content = False
            self._evidence_sess = sess
            self._show_empty(True)
            self._render_files(sess)
            return
        self._show_empty(False)
        muted = self._t("mv_muted")
        warn = self._t("mv_orange")

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
            note = _REQUEST_SOURCE_NOTES.get(it.get("kind"), "")
            source_note = f"<span style='color:{warn};'>{note}</span>" if note else ""
            req_lines.append(
                f"<b>{prefix}</b>（<span style='color:{muted};'>{mid[:10]}</span>{source_note}）：{txt}")

        if not req_lines:
            req_lines.append(f"<span style='color:{muted};'>暂无关联的用户需求来源</span>")
        self.requests_label.setText("<br>".join(req_lines))

        constraints = (task.get("pinned_constraints") or []) if task else []
        if constraints:
            c_html = ["<b>固定限制</b>（严格遵守）："]
            for c in constraints:
                c_html.append(f"• {html.escape(c)}")
            self.constraints_label.setText("<br>".join(c_html))
        else:
            self.constraints_label.setText(f"<span style='color:{muted};'>暂无固定限制（点击上方编辑添加）</span>")

        # ── 4. Section 2：计划步骤与变动 ──
        if items:
            self.sec2_frame.setVisible(True)
            self._render_plan_rows(items)
        else:
            self.sec2_frame.setVisible(False)
            self._render_plan_rows([])
        self._sync_spinner(sess)

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
                f"<span style='color:{warn}; font-weight:600;'>待完成的验证（{len(gaps)} 项）：</span>")
            for g in gaps[:4]:
                v_status_lines.append(
                    f"• <span style='color:{warn};'>{html.escape(_one_line(g, 90))}</span>")
            if len(gaps) > 4:
                v_status_lines.append(f"（另有 {len(gaps) - 4} 项）")
        elif st["state"] == "failed":
            v_status_lines.append(
                f"<span style='color:{self._t('mv_bad')}; font-weight:600;'>最近的检查有 {st['failed']} 项未通过</span>")
        elif st["state"] == "checked":
            v_status_lines.append(
                f"<span style='color:{self._t('mv_ok')};'>已执行的检查通过（{st['passed']} 项）</span>"
                f"<span style='color:{muted};'>；不代表所有要求都已满足</span>")
        elif st["inconclusive"]:
            # 跑过但结论是未执行/已取消/结果未知：说成"尚未执行检查"就抹掉了跑过这件事
            v_status_lines.append(
                f"<span style='color:{warn};'>已尝试 {st['inconclusive']} 项检查，但没有得到通过结论"
                "（未执行、已取消或结果未知）</span>")
        else:
            v_status_lines.append(f"<span style='color:{muted};'>尚未执行检查，没有验证证据</span>")
        self.verification_status_label.setText("<br>".join(v_status_lines))

        # ── 6. Section 4：验证证据 ──
        self._evidence_sess = sess
        self._render_evidence(sess)
        self._render_files(sess)

        self._has_content = True

    def _show_empty(self, empty: bool):
        """空状态和三段内容二选一；计划段是否显示由 render_session 按有没有计划再定。"""
        self.empty_box.setVisible(empty)
        for w in (self.sec1_frame, self.sec3_frame, self.sec4_frame):
            w.setVisible(not empty)
        if empty:
            self.sec2_frame.setVisible(False)
            self.files_frame.setVisible(False)   # 有改动时 _render_files 再打开
            self.switch_banner.setVisible(False)
            self.plan_count.setText("")
            self.plan_progress.setVisible(False)

    def _render_evidence(self, sess: Any):
        """每项检查只列最后一次的结论——和结果卡共用 result_view.effective_rows 的归并，
        两边对同一项检查不会说出不同的话；过期的照实标出。再列本会话还在跑的后台命令。
        没有记录就说没有，不编一条"通过"。"""
        from .. import background, result_view, run_records
        try:
            _rows, latest = result_view.effective_rows(
                {"evidence": run_records.collect_evidence(getattr(sess, "verification", None))})
        except Exception:
            latest = []
        try:
            sid = getattr(sess, "current_session_id", None)
            running = [s for s in background.list_snapshots()
                       if sid and s.get("running") and s.get("session_id") == sid]
        except Exception:
            running = []

        rows = []
        for snap in running:
            mins, secs = divmod(int(snap.get("elapsed_s") or 0), 60)
            rows.append((snap.get("command") or "", f"运行中 {mins} 分 {secs:02d} 秒", self._t("mv_indigo")))
        for row in reversed(latest):   # 最近的在上
            name = row.get("command") or " ".join(
                x for x in (row.get("checker"), row.get("path")) if x) or "检查"
            if row.get("stale"):
                rows.append((name, f"{row.get('label') or ''}（已过期）", self._t("mv_muted")))
            else:
                key = _EVIDENCE_COLORS.get(row.get("status"), "mv_muted")
                rows.append((name, row.get("label") or "", self._t(key)))

        if not rows:
            self.evidence_label.setText(
                f"<span style='color:{self._t('mv_muted')};'>还没有检查记录</span>")
            return
        dim = self._t("text_dim")
        cells = []
        for name, label, color in rows[:_EVIDENCE_MAX]:
            cells.append(
                "<tr>"
                f"<td style=\"font-family:{MONO_CSS}; color:{dim}; padding:2px 10px 2px 0;\">"
                f"{html.escape(_one_line(name, 40))}</td>"
                f"<td align=\"right\" style=\"color:{color}; padding:2px 0;\">{html.escape(label)}</td>"
                "</tr>")
        more = ""
        if len(rows) > _EVIDENCE_MAX:
            more = f"<div style='color:{self._t('mv_muted')};'>另有 {len(rows) - _EVIDENCE_MAX} 项</div>"
        self.evidence_label.setText(
            '<table cellspacing="0" cellpadding="0" width="100%">' + "".join(cells) + "</table>" + more)
