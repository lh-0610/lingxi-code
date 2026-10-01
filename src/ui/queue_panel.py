"""运行中输入队列面板（B09b）：本会话待发列表的可视化。

只做视图：数据源是 Session.input_queue（经 input_queue 模块归一的结构），
由 ChatUI 在主线程调用 render_queue() 重绘；所有按钮只发 Signal，
处置逻辑（持久化、派发、与 B09a 屏障的协调）都在 ChatUI 侧。
"""
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy,
)

from .. import input_queue
from ..limits import QUEUE_EXCERPT_CHARS

_STATE_LABELS = {
    input_queue.QUEUED: "待处理",
    input_queue.HELD: "已暂停",
    input_queue.DISPATCHING: "等待上一轮结束",
    input_queue.ADMITTED: "运行中",
    input_queue.DONE: "已完成",
    input_queue.NEEDS_CHECK: "待核对",
}

_STATE_COLORS = {   # (bg, border, text) 的主题键后缀由 ChatUI 的取色函数决定
    input_queue.QUEUED: "#5b6cf0",
    input_queue.HELD: "#c07f00",
    input_queue.DISPATCHING: "#888888",
    input_queue.ADMITTED: "#2f9e44",
    input_queue.DONE: "#888888",
    input_queue.NEEDS_CHECK: "#d9480f",
}


class QueuePanel(QWidget):
    """本会话的待发输入列表。数据与处置都在 ChatUI，这里只画。"""

    edit_requested = Signal(str)
    delete_requested = Signal(str)
    process_now_requested = Signal(str)
    retry_requested = Signal(str)
    discard_requested = Signal(str)
    resume_requested = Signal()

    def __init__(self, color_getter):
        super().__init__()
        self._color = color_getter
        self.setVisible(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 0, 48, 0)
        outer.setSpacing(4)

        self._header = QWidget()
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(2, 4, 2, 2)
        header_layout.setSpacing(8)
        self._title = QLabel("输入队列")
        self._title.setStyleSheet("font-weight: bold; font-size: 12px;")
        header_layout.addWidget(self._title)
        self._state_label = QLabel("")
        self._state_label.setWordWrap(True)
        header_layout.addWidget(self._state_label, 1)
        self._resume_btn = QPushButton("继续处理队列")
        self._resume_btn.setCursor(Qt.PointingHandCursor)
        self._resume_btn.setFixedHeight(22)
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        header_layout.addWidget(self._resume_btn, 0, Qt.AlignTop)
        outer.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFixedHeight(0)     # 行数决定高度（见 _relayout）
        self._scroll.setStyleSheet("QScrollArea { border: none; }")
        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._scroll.setWidget(self._rows_host)
        outer.addWidget(self._scroll)

    # ── 渲染 ──

    def render_queue(self, queue):
        """用会话的队列结构整幅重绘（条目量小，整建比差量简单可靠）。"""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        items = (queue or {}).get("items") or []
        paused = bool((queue or {}).get("paused"))
        reason = (queue or {}).get("pause_reason") or ""

        if not items:
            self.setVisible(False)
            return
        self.setVisible(True)

        if paused:
            self._state_label.setText(f"⏸ 已暂停：{reason}" if reason else "⏸ 已暂停")
            self._resume_btn.setVisible(True)
        else:
            pending = sum(1 for it in items if it.get("state") in
                          (input_queue.QUEUED, input_queue.HELD))
            self._state_label.setText(f"按入队顺序自动处理（待发 {pending} 条）"
                                      if pending else "按入队顺序自动处理")
            self._resume_btn.setVisible(False)

        for it in items:
            self._rows_layout.addWidget(self._build_row(it))
        self._relayout(len(items))

    def _relayout(self, n_rows):
        row_h = 30
        self._scroll.setFixedHeight(min(n_rows, 5) * (row_h + 4) + 6)

    def _build_row(self, it):
        row = QWidget()
        row.setObjectName("queue-row-" + (it.get("queue_item_id") or ""))
        lay = QHBoxLayout(row)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(6)


        state = it.get("state") or input_queue.QUEUED
        chip = QLabel(_STATE_LABELS.get(state, state))
        color = _STATE_COLORS.get(state, "#888888")
        chip.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 6px;"
            "padding: 0 6px; font-size: 11px;")
        chip.setFixedHeight(20)
        lay.addWidget(chip)

        text = input_queue.excerpt(it.get("text"), QUEUE_EXCERPT_CHARS) or "（仅附件）"
        n_img = len(it.get("images") or [])
        label = QLabel(text + (f"　📎{n_img}" if n_img else ""))
        label.setToolTip(it.get("text") or "")
        label.setStyleSheet("font-size: 12px;")
        lay.addWidget(label, 1)

        note = it.get("hold_reason") or it.get("error") or ""
        if note:
            note_lbl = QLabel(f"⚠ {input_queue.excerpt(note, 40)}")
            note_lbl.setToolTip(note)
            note_lbl.setStyleSheet("color: #c07f00; font-size: 11px;")
            lay.addWidget(note_lbl)

        if state == input_queue.QUEUED:
            lay.addWidget(self._btn("编辑", lambda _=False, i=it["queue_item_id"]:
                                    self.edit_requested.emit(i)))
            lay.addWidget(self._btn("删除", lambda _=False, i=it["queue_item_id"]:
                                    self.delete_requested.emit(i)))
            lay.addWidget(self._btn("立即处理", lambda _=False, i=it["queue_item_id"]:
                                    self.process_now_requested.emit(i)))
        elif state == input_queue.HELD:
            lay.addWidget(self._btn("重试", lambda _=False, i=it["queue_item_id"]:
                                    self.retry_requested.emit(i)))
            lay.addWidget(self._btn("删除", lambda _=False, i=it["queue_item_id"]:
                                    self.delete_requested.emit(i)))
        elif state == input_queue.NEEDS_CHECK:
            lay.addWidget(self._btn("重新入队", lambda _=False, i=it["queue_item_id"]:
                                    self.retry_requested.emit(i)))
            lay.addWidget(self._btn("丢弃", lambda _=False, i=it["queue_item_id"]:
                                    self.discard_requested.emit(i)))
        elif state == input_queue.DONE:
            status = it.get("outcome_status") or ""
            mark = "✓" if status == "completed" else f"✗ {status}"
            done = QLabel(mark)
            done.setStyleSheet("color: #888888; font-size: 11px;")
            lay.addWidget(done)
        return row

    def _btn(self, text, on_click):
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(20)
        btn.setStyleSheet("QPushButton { font-size: 11px; padding: 0 8px; }")
        btn.clicked.connect(on_click)
        return btn


def format_created_at(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return ""
