"""运行中输入队列面板（B09b）：本会话待发列表的可视化。

只做视图：数据源是 Session.input_queue（经 input_queue 模块归一的结构），
由 ChatUI 在主线程调用 render_queue() 重绘；所有按钮只发 Signal，
处置逻辑（持久化、派发、与 B09a 屏障的协调）都在 ChatUI 侧。

样子按设计稿 A5：一张卡，头上一行"排队 N 条 / 这一轮结束后发送"（暂停时是琥珀色的
横条和"继续发送"）；下面是带编号的待发列表，每行右边是文字按钮；已经发出去的收成
最后一行"已发送 N 条"，点开看。
"""
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
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

_STATE_COLORS = {   # 主题色键名；画行时经 ChatUI 的取色函数按当前主题取值
    input_queue.QUEUED: "mv_indigo",
    input_queue.HELD: "mv_orange",
    input_queue.DISPATCHING: "mv_muted",
    input_queue.ADMITTED: "mv_ok",
    input_queue.DONE: "mv_muted",
    input_queue.NEEDS_CHECK: "mv_bad",
}

_ROW_H = 38          # 一行的高度（有说明的行会高一些）
_MAX_VISIBLE = 5     # 最多露出几行，再多在面板里滚动


class QueuePanel(QFrame):
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
        self._show_done = False
        self._last_queue = None
        self.setObjectName("queuePanel")
        self.setVisible(False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 头：排队 N 条 + 说明；暂停时整条变琥珀色，右边"继续发送"
        self._header = QFrame()
        self._header.setObjectName("queueHeader")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(14, 7, 10, 7)
        header_layout.setSpacing(10)
        self._title = QLabel("输入队列")
        self._title.setObjectName("queueTitle")
        header_layout.addWidget(self._title)
        self._state_label = QLabel("")
        self._state_label.setObjectName("queueState")
        self._state_label.setWordWrap(True)
        header_layout.addWidget(self._state_label, 1)
        self._resume_btn = QPushButton("继续发送")
        self._resume_btn.setObjectName("queueResume")
        self._resume_btn.setCursor(Qt.PointingHandCursor)
        self._resume_btn.setToolTip("从暂停处继续，按顺序发送排队的内容")
        self._resume_btn.clicked.connect(self.resume_requested.emit)
        header_layout.addWidget(self._resume_btn, 0, Qt.AlignVCenter)
        outer.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setFixedHeight(0)     # 行数决定高度（见 _relayout）
        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._scroll.setWidget(self._rows_host)
        outer.addWidget(self._scroll)

        # 尾：已发送的条目收成一行
        self._done_btn = QPushButton("")
        self._done_btn.setObjectName("queueDone")
        self._done_btn.setCursor(Qt.PointingHandCursor)
        self._done_btn.clicked.connect(self._toggle_done)
        self._done_btn.setVisible(False)
        outer.addWidget(self._done_btn, 0, Qt.AlignLeft)
        self.apply_theme()

    def apply_theme(self, paused=False):
        """整张卡跟主题走（深色下不能是系统的白底）。行内的状态色在 _build_row 里按当前主题取，
        切主题后由 ChatUI 重画一次队列。"""
        c = self._color
        head_bg = c("mv_header_bg")
        state_color = c("mv_text_sec")
        if paused:
            head_bg = c("mv_gate_bg")
            state_color = c("mv_gate_text")
        self.setStyleSheet(
            f"#queuePanel {{ background: {c('mv_card_bg')}; border: 1px solid {c('mv_card_border')}; "
            f"border-radius: 12px; }}"
            f"#queueHeader {{ background: {head_bg}; border: none; border-top-left-radius: 11px; "
            f"border-top-right-radius: 11px; }}"
            f"QLabel {{ color: {c('mv_text')}; background: transparent; font-size: 13px; }}"
            f"QLabel#queueTitle {{ font-weight: 600; }}"
            f"QLabel#queueState {{ color: {state_color}; }}"
            f"QLabel#queueNum {{ color: {c('mv_muted')}; }}"
            f"QLabel#queueNote {{ color: {c('mv_err_text')}; font-size: 12px; }}"
            f"QFrame[class=\"queueRow\"] {{ background: transparent; border: none; "
            f"border-top: 1px solid {c('mv_result_border')}; }}"
            f"QPushButton#queueResume {{ background: {c('mv_indigo')}; color: {c('mv_view_bg')}; border: none; "
            f"border-radius: 7px; padding: 4px 12px; font-size: 12px; }}"
            f"QPushButton#queueAction {{ background: transparent; border: none; border-radius: 6px; "
            f"padding: 3px 8px; font-size: 12px; color: {c('mv_text3')}; }}"
            f"QPushButton#queueAction:hover {{ background: {c('mv_header_bg')}; }}"
            f"QPushButton#queueAction[primary=\"true\"] {{ color: {c('mv_indigo')}; }}"
            f"QPushButton#queueDone {{ background: transparent; border: none; padding: 6px 14px; "
            f"font-size: 12px; color: {c('mv_text_sec')}; text-align: left; }}"
            f"QPushButton#queueDone:hover {{ color: {c('mv_indigo')}; }}")
        for w in (self._scroll.viewport(), self._rows_host):
            w.setStyleSheet("background: transparent;")

    # ── 渲染 ──

    def render_queue(self, queue):
        """用会话的队列结构整幅重绘（条目量小，整建比差量简单可靠）。"""
        self._last_queue = queue
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
        self.apply_theme(paused=paused)

        open_items = [it for it in items if it.get("state") != input_queue.DONE]
        done_items = [it for it in items if it.get("state") == input_queue.DONE]
        pending = sum(1 for it in open_items
                      if it.get("state") in (input_queue.QUEUED, input_queue.HELD))
        if paused:
            self._title.setText("已暂停")
            self._state_label.setText(reason or "排队的内容不会自动发出")
            self._resume_btn.setVisible(True)
        else:
            self._title.setText(f"排队 {pending} 条" if pending else "输入队列")
            self._state_label.setText("这一轮结束后按顺序发送" if pending else "")
            self._resume_btn.setVisible(False)

        shown = list(open_items) + (done_items if self._show_done else [])
        for n, it in enumerate(shown, start=1):
            self._rows_layout.addWidget(self._build_row(it, n))
        if done_items:
            arrow = "收起" if self._show_done else "展开"
            self._done_btn.setText(f"已发送 {len(done_items)} 条　{arrow}")
            self._done_btn.setVisible(True)
        else:
            self._done_btn.setVisible(False)
        self._relayout()

    def _toggle_done(self):
        self._show_done = not self._show_done
        self.render_queue(self._last_queue)

    def _relayout(self):
        heights = []
        for i in range(self._rows_layout.count()):
            w = self._rows_layout.itemAt(i).widget()
            if w is not None:
                heights.append(max(_ROW_H, w.sizeHint().height()))
        self._scroll.setFixedHeight(sum(heights[:_MAX_VISIBLE]))

    def _build_row(self, it, n):
        row = QFrame()
        row.setObjectName("queue-row-" + (it.get("queue_item_id") or ""))   # 处置和测试都按它找行
        row.setProperty("class", "queueRow")
        row.setAccessibleName(f"排队第 {n} 条")
        holder = QVBoxLayout(row)
        holder.setContentsMargins(14, 0, 8, 0)
        holder.setSpacing(0)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(10)
        holder.addLayout(line)

        num = QLabel(str(n))
        num.setObjectName("queueNum")
        num.setFixedWidth(16)
        line.addWidget(num, 0, Qt.AlignVCenter)

        text = input_queue.excerpt(it.get("text"), QUEUE_EXCERPT_CHARS) or "（仅附件）"
        n_img = len(it.get("images") or [])
        label = QLabel(text + (f"　图片 ×{n_img}" if n_img else ""))
        label.setToolTip(it.get("text") or "")
        label.setMinimumHeight(_ROW_H)
        line.addWidget(label, 1)

        state = it.get("state") or input_queue.QUEUED
        if state != input_queue.QUEUED:
            # 待发的不标状态（编号就是顺序）；其余状态如实标出
            chip = QLabel(_STATE_LABELS.get(state, state))
            chip.setStyleSheet(f"color: {self._color(_STATE_COLORS.get(state, 'mv_muted'))}; font-size: 12px;")
            line.addWidget(chip)

        item_id = it["queue_item_id"]
        if state == input_queue.QUEUED:
            line.addWidget(self._btn("立即发送", lambda _=False, i=item_id: self.process_now_requested.emit(i),
                                     primary=True, tip="停下当前这一轮，马上发这一条"))
            line.addWidget(self._btn("编辑", lambda _=False, i=item_id: self.edit_requested.emit(i)))
            line.addWidget(self._btn("删除", lambda _=False, i=item_id: self.delete_requested.emit(i)))
        elif state == input_queue.HELD:
            line.addWidget(self._btn("重试", lambda _=False, i=item_id: self.retry_requested.emit(i),
                                     primary=True))
            line.addWidget(self._btn("删除", lambda _=False, i=item_id: self.delete_requested.emit(i)))
        elif state == input_queue.NEEDS_CHECK:
            line.addWidget(self._btn("重新入队", lambda _=False, i=item_id: self.retry_requested.emit(i),
                                     primary=True))
            line.addWidget(self._btn("丢弃", lambda _=False, i=item_id: self.discard_requested.emit(i)))
        elif state == input_queue.DONE:
            status = it.get("outcome_status") or ""
            done = QLabel("已发送" if status == "completed" else f"已发送，结果：{status}")
            done.setStyleSheet(f"color: {self._color('mv_muted')}; font-size: 12px;")
            line.addWidget(done)

        note = it.get("hold_reason") or it.get("error") or ""
        if note:
            # 为什么停在这里：放在文字下面一行，原因写全（悬停看完整）
            note_lbl = QLabel(input_queue.excerpt(note, 80))
            note_lbl.setObjectName("queueNote")
            note_lbl.setToolTip(note)
            note_lbl.setWordWrap(True)
            note_lbl.setContentsMargins(26, 0, 0, 8)
            holder.addWidget(note_lbl)
        return row

    def _btn(self, text, on_click, primary=False, tip=""):
        btn = QPushButton(text)
        btn.setObjectName("queueAction")
        btn.setProperty("primary", "true" if primary else "false")
        btn.setCursor(Qt.PointingHandCursor)
        if tip:
            btn.setToolTip(tip)
        btn.clicked.connect(on_click)
        return btn


def format_created_at(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except Exception:
        return ""
