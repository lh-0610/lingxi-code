"""B10b 后台任务面板：只消费管理器快照，不持有 Popen 或会话执行权。

这是用户主动打开的应用级管理入口；模型工具仍受各自会话令牌约束。
停止在 Python 工作线程执行，完成信号按 bg_id 回到主线程；隐藏窗口只停刷新，
不停止命令。窗口销毁后的回执可以丢弃，实际进程状态仍由 background 管理。
"""
import os
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot, QSize, QRectF
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QStyledItemDelegate, QStyleOptionViewItem, QStyle, QLayout,
)

from .. import background, session
from ._base import BASE_DIR


def process_status(snap, *, busy=False):
    """只报告主进程事实；正常退出不等于代码验证或任务完成。"""
    running = snap.get("running")
    if running is None:
        return "状态未知", "badge_warn_text"
    if running is False:
        if snap.get("end_kind") == background.END_STOPPED:
            return "主进程已退出（停止请求后）", "text_dim"
        if snap.get("exit_code") == 0:
            return "主进程正常退出", "badge_done_text"
        if snap.get("exit_code") is None:
            return "主进程已退出（退出码未知）", "badge_warn_text"
        return "主进程异常退出", "badge_warn_text"
    if busy or snap.get("stop_in_progress"):
        return "停止中，尚未确认退出", "badge_warn_text"
    if snap.get("stop_error"):
        return "停止未确认，仍在运行", "badge_warn_text"
    if snap.get("stop_dispatched"):
        return "已请求停止，仍在运行", "badge_warn_text"
    return "运行中", "badge_run_text"


def output_notes(snap):
    notes = []
    if snap.get("output_truncated"):
        notes.append(f"仅保留最新输出；较早的 {snap.get('output_dropped_chars', 0):,} "
                     "字符已从缓冲区丢弃，无法补读。")
    if snap.get("read_error"):
        notes.append("输出读取失败（已读内容仍保留）：" + snap["read_error"])
    elif not snap.get("output_complete"):
        notes.append("输出仍在读取，当前内容可能不完整。")
    elif not snap.get("output_tail"):
        notes.append("未捕获到输出。")
    for field, label in (("process_error", "无法核对主进程状态"),
                         ("stop_error", "停止问题"), ("tree_error", "进程树请求问题")):
        if snap.get(field):
            notes.append(f"{label}：{snap[field]}")
    return "\n".join(notes)


def _source(snap):
    return ("子 Agent · " if snap.get("is_subagent") else "会话 · ") + snap["session_id"]


class _StopEvents(QObject):
    completed = Signal(str, object)


def _stop_in_thread(bg_id, events):
    # 不捕获 panel/active Session；即使用户换了会话、选择或关闭面板也只停止此 id。
    try:
        result = background.stop(bg_id, wait_timeout=5.0)
    except Exception as exc:
        result = {"found": True, "confirmed": False,
                  "stop_error": f"{type(exc).__name__}: {exc}"}
    try:
        events.completed.emit(bg_id, result)
    except RuntimeError:
        # QObject 随窗口销毁后已经断开连接；停止本身已完成，不再访问控件。
        pass


class _TaskTree(QTreeWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 窄窗时保留可读的命令列，多余宽度走水平滚动，不把命令压成两三个字。
        other = sum(self.header().sectionSize(i) for i in range(1, 5))
        self.header().resizeSection(0, max(220, self.viewport().width() - other))


class _TaskDelegate(QStyledItemDelegate):
    """状态用带文字的轻量徽章，选中背景仍交给 Qt；颜色不是唯一线索。"""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(46, size.height()))
        return size

    def paint(self, painter, option, index):
        colors = index.data(Qt.UserRole + 1) if index.column() == 1 else None
        if not colors:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        opt.widget.style().drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(option.font)
        font.setPointSize(9)
        painter.setFont(font)
        available = max(20, option.rect.width() - 24)
        text = painter.fontMetrics().elidedText(text, Qt.ElideRight, available - 20)
        width = min(available, painter.fontMetrics().horizontalAdvance(text) + 20)
        rect = QRectF(option.rect.left() + 8, option.rect.center().y() - 12, width, 24)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(colors[1]))
        painter.drawRoundedRect(rect, 6, 6)
        painter.setPen(QColor(colors[0]))
        painter.drawText(rect, Qt.AlignCenter, text)
        painter.restore()


class BackgroundPanel(QDialog):
    """可重复打开的非模态窗口；主线程定时快照、工作线程停止。"""

    def __init__(self, color_getter, parent=None):
        super().__init__(parent)
        self._color = color_getter
        self._snapshots = {}
        self._selected_id = None
        self._busy = set()
        self._feedback = {}
        self._refresh_error = ""
        self._output_id = None
        self.setObjectName("backgroundPanel")
        self.setWindowTitle("后台任务")
        self.resize(1040, 760)
        self.setMinimumSize(600, 560)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        # 固定的 560px 下限小于部分字体下的内容最小高度。让布局按实际控件
        # 更新窗口下限，而不是把最后的输出区挤成一条线或画到父控件外。
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        top = QHBoxLayout()
        title_group = QVBoxLayout()
        title_group.setSpacing(4)
        title = QLabel("后台任务")
        title.setObjectName("backgroundTitle")
        title_group.addWidget(title)
        subtitle = QLabel("查看后台命令的运行状态与输出")
        subtitle.setObjectName("backgroundSubtitle")
        title_group.addWidget(subtitle)
        top.addLayout(title_group, 1)
        self.scope_combo = QComboBox()
        self.scope_combo.setMinimumWidth(122)
        self.scope_combo.setFixedHeight(36)
        self.scope_combo.addItems(["全部会话", "当前会话"])
        self.scope_combo.setToolTip("全部会话允许你手动管理本次启动中的所有后台命令")
        self.scope_combo.currentIndexChanged.connect(self.refresh)
        top.addWidget(self.scope_combo)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setFixedHeight(36)
        self.refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)

        summary = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setObjectName("backgroundCount")
        summary.addWidget(self.count_label)
        summary.addStretch()
        self.live_label = QLabel("● 每 0.5 秒刷新")
        self.live_label.setObjectName("backgroundLive")
        summary.addWidget(self.live_label)
        layout.addLayout(summary)

        self.list_note = QLabel()
        self.list_note.setWordWrap(True)
        self.list_note.setTextFormat(Qt.PlainText)
        self.list_note.setObjectName("backgroundListNote")
        self.list_note.hide()
        layout.addWidget(self.list_note)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        margins = layout.contentsMargins()
        self.splitter.setMinimumWidth(
            self.minimumWidth() - margins.left() - margins.right())
        layout.addWidget(self.splitter, 1)
        self.tree = _TaskTree()
        self.tree.setHeaderLabels(["命令", "状态", "来源 / 项目", "耗时", "退出码"])
        self.tree.setRootIsDecorated(False)
        self.tree.setObjectName("backgroundTree")
        self.tree.setMinimumHeight(120)
        self.tree.setAlternatingRowColors(False)
        self.tree.setItemDelegate(_TaskDelegate(self.tree))
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setTextElideMode(Qt.ElideRight)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        for index, width in ((1, 200), (2, 190), (3, 72), (4, 66)):
            header.setSectionResizeMode(index, QHeaderView.Interactive)
            header.resizeSection(index, width)
        header.setMinimumSectionSize(50)
        self.tree.itemSelectionChanged.connect(self._select)
        self.splitter.addWidget(self.tree)

        detail = QFrame()
        detail.setObjectName("backgroundDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(16, 14, 16, 14)
        detail_layout.setSpacing(10)
        # 展开信息或显示停止回执后，将新的最小高度传给 splitter 和主布局。
        detail_layout.setSizeConstraint(QLayout.SetMinimumSize)
        detail_top = QHBoxLayout()
        self.detail_title = QLabel("选择任务以查看输出")
        self.detail_title.setTextFormat(Qt.PlainText)
        self.detail_title.setWordWrap(True)
        self.detail_title.setObjectName("backgroundStatus")
        detail_top.addWidget(self.detail_title, 1)
        self.info_btn = QPushButton("任务信息")
        self.info_btn.setCheckable(True)
        self.info_btn.setEnabled(False)
        self.info_btn.toggled.connect(self._toggle_info)
        detail_top.addWidget(self.info_btn)
        self.stop_btn = QPushButton("停止此任务")
        self.stop_btn.setObjectName("backgroundStop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_selected)
        detail_top.addWidget(self.stop_btn)
        detail_layout.addLayout(detail_top)
        self.command_view = QPlainTextEdit()
        self.command_view.setObjectName("backgroundCommand")
        self.command_view.setReadOnly(True)
        self.command_view.setFixedHeight(46)
        self.command_view.setPlaceholderText("选中上方任务后，这里显示完整命令")
        self.command_view.setFont(QFont("Consolas", 10))
        detail_layout.addWidget(self.command_view)
        self.location_label = QLabel()
        self.location_label.setObjectName("backgroundLocation")
        self.location_label.setTextFormat(Qt.PlainText)
        detail_layout.addWidget(self.location_label)
        self.metadata = QPlainTextEdit()
        self.metadata.setObjectName("backgroundMetadata")
        self.metadata.setReadOnly(True)
        self.metadata.setMaximumHeight(120)
        self.metadata.setPlaceholderText("这里显示命令和创建时的归属，切换会话不会改变它们。")
        detail_layout.addWidget(self.metadata)
        self.metadata.hide()
        self.stop_note = QLabel()
        self.stop_note.setTextFormat(Qt.PlainText)
        self.stop_note.setWordWrap(True)
        detail_layout.addWidget(self.stop_note)
        self.stop_note.hide()

        log_frame = QFrame()
        log_frame.setObjectName("backgroundLog")
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(14, 10, 14, 12)
        log_layout.setSpacing(6)
        log_layout.setSizeConstraint(QLayout.SetMinimumSize)
        log_header = QHBoxLayout()
        log_title = QLabel("运行输出")
        log_title.setObjectName("backgroundLogTitle")
        log_header.addWidget(log_title, 1)
        self.follow_box = QCheckBox("跟随输出")
        self.follow_box.setChecked(True)
        self.follow_box.toggled.connect(self._follow_changed)
        log_header.addWidget(self.follow_box)
        log_layout.addLayout(log_header)
        self.output_note = QPlainTextEdit()
        self.output_note.setObjectName("backgroundOutputNote")
        self.output_note.setReadOnly(True)
        self.output_note.setMaximumHeight(85)
        self.output_note.setMinimumHeight(35)
        log_layout.addWidget(self.output_note)
        self.output = QPlainTextEdit()
        self.output.setObjectName("backgroundOutput")
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(90)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setPointSize(10)
        self.output.setFont(font)
        self.output.setPlaceholderText("任务的输出会显示在这里")
        self.output.verticalScrollBar().valueChanged.connect(self._output_scrolled)
        log_layout.addWidget(self.output, 1)
        detail_layout.addWidget(log_frame, 1)
        self.splitter.addWidget(detail)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([220, 430])

        bottom = QHBoxLayout()
        hint = QLabel("关闭面板不会停止命令。退出状态仅代表主进程，任务验证请查看结果卡。")
        hint.setObjectName("backgroundFootnote")
        hint.setToolTip("仅显示本次应用启动的记录，重启后不恢复进程控制。\n"
                        "主进程退出不证明所有子进程已退出，也不代表任务已验证。")
        hint.setWordWrap(True)
        bottom.addWidget(hint, 1)
        close_btn = QPushButton("完成")
        close_btn.clicked.connect(self.close)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)
        self._events = _StopEvents(self)
        self._events.completed.connect(self._stop_finished, Qt.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self.apply_theme()

    def apply_theme(self):
        c = self._color
        arrow = os.path.join(BASE_DIR, "icons", "chevron_down.svg").replace("\\", "/")
        self.setStyleSheet(
            f"QDialog#backgroundPanel {{ background: {c('sidebar_bg')}; }}"
            f"QWidget {{ color: {c('text')}; font-size: 13px; }}"
            f"QLabel {{ background: transparent; }}"
            f"QLabel#backgroundTitle {{ font-size: 24px; font-weight: 600; }}"
            f"QLabel#backgroundSubtitle, QLabel#backgroundLocation {{ color: {c('text_dim')}; font-size: 12px; }}"
            f"QLabel#backgroundCount {{ color: {c('text_dim')}; font-size: 12px; }}"
            f"QLabel#backgroundLive {{ color: {c('badge_run_text')}; font-size: 11px; }}"
            f"QLabel#backgroundFootnote {{ color: {c('text_dim')}; font-size: 11px; }}"
            f"QLabel#backgroundListNote {{ color: {c('badge_warn_text')}; padding: 8px; }}"
            f"QLabel#backgroundStatus {{ font-weight: 600; }}"
            f"QTreeWidget#backgroundTree {{ border: 1px solid {c('input_border')}; border-radius: 10px; "
            f"background: {c('win_bg')}; color: {c('text')}; outline: none; "
            f"selection-background-color: {c('history_active_bg')}; selection-color: {c('text')}; }}"
            f"QTreeWidget::item {{ padding: 7px 8px; border-bottom: 1px solid {c('header_border')}; }}"
            f"QTreeWidget::item:hover {{ background: {c('history_hover_bg')}; }}"
            f"QTreeWidget::item:focus {{ border-bottom: 1px solid {c('history_active_border')}; }}"
            f"QHeaderView::section {{ background: {c('win_bg')}; color: {c('text_dim')}; "
            f"border: none; border-bottom: 1px solid {c('input_border')}; padding: 10px 8px; font-size: 11px; }}"
            f"QFrame#backgroundDetail {{ background: {c('win_bg')}; border: 1px solid {c('input_border')}; border-radius: 10px; }}"
            f"QFrame#backgroundLog {{ background: {c('sidebar_bg')}; border: 1px solid {c('input_border')}; border-radius: 8px; }}"
            f"QLabel#backgroundLogTitle {{ font-size: 12px; font-weight: 600; color: {c('text_dim')}; }}"
            f"QPlainTextEdit {{ background: {c('win_bg')}; color: {c('text')}; "
            f"border: 1px solid {c('input_border')}; border-radius: 6px; padding: 6px; "
            f"selection-background-color: {c('input_sel_bg')}; selection-color: {c('input_sel_text')}; }}"
            f"QPlainTextEdit#backgroundCommand {{ background: {c('win_bg')}; border: none; padding: 0; }}"
            f"QPlainTextEdit#backgroundOutput {{ background: {c('sidebar_bg')}; border: none; padding: 0; }}"
            f"QPushButton {{ background: {c('new_chat_bg')}; color: {c('text_dim')}; "
            f"border: 1px solid {c('input_border')}; border-radius: 7px; padding: 8px 12px; font-size: 12px; }}"
            f"QPushButton:hover, QComboBox:hover {{ background: {c('history_hover_bg')}; border-color: {c('history_active_border')}; }}"
            f"QPushButton:focus, QComboBox:focus {{ border: 1px solid {c('history_active_border')}; }}"
            f"QPushButton:checked {{ background: {c('history_active_bg')}; color: {c('history_active_text')}; }}"
            f"QPushButton:disabled {{ color: {c('text_subtle')}; border-color: {c('header_border')}; }}"
            f"QPushButton#backgroundStop {{ background: {c('badge_warn_bg')}; color: {c('badge_warn_text')}; border-color: {c('badge_warn_border')}; }}"
            f"QPushButton#backgroundStop:disabled {{ background: {c('sidebar_bg')}; color: {c('text_subtle')}; border-color: {c('input_border')}; }}"
            f"QComboBox {{ background: {c('win_bg')}; color: {c('text_dim')}; border: 1px solid {c('input_border')}; "
            f"border-radius: 7px; padding: 4px 30px 4px 12px; font-size: 12px; }}"
            f"QComboBox::drop-down {{ border: none; width: 26px; }}"
            f"QComboBox::down-arrow {{ image: url({arrow}); width: 12px; height: 12px; }}"
            f"QComboBox QAbstractItemView {{ background: {c('win_bg')}; color: {c('text')}; "
            f"selection-background-color: {c('history_active_bg')}; }}"
            f"QCheckBox {{ color: {c('text_dim')}; font-size: 11px; spacing: 6px; }}"
            f"QSplitter::handle {{ background: transparent; height: 12px; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 7px; margin: 2px; }}"
            f"QScrollBar:horizontal {{ background: transparent; height: 7px; margin: 2px; }}"
            f"QScrollBar::handle {{ background: {c('scrollbar_handle')}; border-radius: 2px; min-width: 24px; min-height: 24px; }}"
            f"QScrollBar::handle:hover {{ background: {c('scrollbar_handle_hover')}; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}"
        )
        self.stop_note.setStyleSheet(f"color: {c('text_dim')}; font-size: 12px;")
        self.refresh_btn.setIcon(self._icon("refresh_cw_lucide.svg", c("text_dim")))
        self.stop_btn.setIcon(self._icon("square-stop.svg", c("badge_warn_text")))
        for button in (self.refresh_btn, self.info_btn, self.stop_btn):
            button.setIconSize(QSize(14, 14))
            button.setCursor(Qt.PointingHandCursor)
        self.refresh()

    def _icon(self, name, color):
        from PySide6.QtSvg import QSvgRenderer
        try:
            with open(os.path.join(BASE_DIR, "icons", name), encoding="utf-8") as handle:
                svg = handle.read().replace("currentColor", color)
        except OSError:
            return QIcon()
        renderer = QSvgRenderer(svg.encode("utf-8"))
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap(round(16 * dpr), round(16 * dpr))
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(dpr)
        return QIcon(pixmap)

    @Slot(bool)
    def _toggle_info(self, checked):
        self.metadata.setVisible(checked and self._selected_id in self._snapshots)
        self.info_btn.setText("收起信息" if checked else "任务信息")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    @Slot()
    def refresh(self):
        owner = (session.get_active().background_owner_id
                 if self.scope_combo.currentIndex() == 1 else None)
        try:
            snapshots = background.list_snapshots(owner_id=owner)
        except Exception as exc:
            self._refresh_error = f"刷新失败，以下为旧数据：{type(exc).__name__}: {exc}"
            self.list_note.setText(self._refresh_error[:250])
            self.list_note.setToolTip(self._refresh_error)
            self.list_note.show()
            self.live_label.setText("暂未取得最新状态")
            self.stop_btn.setEnabled(False)
            return
        self._refresh_error = ""
        self.list_note.setToolTip("")
        self._snapshots = {snap["bg_id"]: snap for snap in snapshots}
        # 仅保留仍可见或刚被清理的选中项回执；面板不再攒另一份无上限历史。
        self._feedback = {key: text for key, text in self._feedback.items()
                          if key in self._snapshots or key == self._selected_id}
        running = sum(s["running"] is True for s in snapshots)
        unknown = sum(s["running"] is None for s in snapshots)
        ended = sum(s["running"] is False for s in snapshots)
        counts = f"{len(snapshots)} 个任务    {running} 运行中    {ended} 已退出"
        self.count_label.setText(counts + (f"    {unknown} 状态未知" if unknown else ""))
        self.live_label.setText("● 每 0.5 秒刷新")
        empty_note = ("当前会话没有后台任务，可切换到“全部会话”查看其他来源。"
                      if owner else "还没有后台任务。让灵犀在后台运行命令后，即可在这里查看。")
        self.list_note.setText("" if snapshots else empty_note)
        self.list_note.setVisible(not snapshots)
        items = {self.tree.topLevelItem(i).data(0, Qt.UserRole): self.tree.topLevelItem(i)
                 for i in range(self.tree.topLevelItemCount())}
        self.tree.blockSignals(True)
        for bg_id, item in items.items():
            if bg_id not in self._snapshots:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        for index, snap in enumerate(sorted(snapshots, key=lambda s: s["started_at"], reverse=True)):
            bg_id = snap["bg_id"]
            item = items.get(bg_id)
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.UserRole, bg_id)
                self.tree.insertTopLevelItem(index, item)
            status, color = process_status(snap, busy=bg_id in self._busy)
            project = snap.get("project") or "无项目"
            source = _source(snap)
            values = [" ".join(snap["command"].split())[:250], status,
                      source + " / " + project, f"{snap['elapsed_s']} s",
                      "—" if snap["exit_code"] is None else str(snap["exit_code"])]
            for column, value in enumerate(values):
                item.setText(column, value)
            item.setToolTip(0, snap["command"][:2000])
            item.setToolTip(1, status)
            item.setToolTip(2, source + "\n项目：" + project)
            item.setForeground(1, QColor(self._color(color)))
            badge_bg = {"badge_warn_text": "badge_warn_bg", "badge_done_text": "badge_done_bg",
                        "badge_run_text": "think_on_bg", "text_dim": "sidebar_bg"}[color]
            item.setData(1, Qt.UserRole + 1, (self._color(color), self._color(badge_bg)))
            item.setFont(0, QFont("Consolas", 10))
            if bg_id == self._selected_id and not item.isSelected():
                item.setSelected(True)
        self.tree.blockSignals(False)
        self._render_selected()

    @Slot()
    def _select(self):
        items = self.tree.selectedItems()
        self._selected_id = items[0].data(0, Qt.UserRole) if items else None
        self._render_selected()

    def _render_selected(self):
        bg_id = self._selected_id
        snap = self._snapshots.get(bg_id)
        feedback = self._feedback.get(bg_id, "")
        self.stop_note.setText(feedback if len(feedback) <= 160 else
                               feedback[:160] + "…（悬停查看完整回执）")
        self.stop_note.setToolTip(feedback)
        self.stop_note.setVisible(bool(feedback))
        if snap is None:
            self.detail_title.setText("该任务不在当前列表中" if bg_id else "选择任务以查看输出")
            self.metadata.clear()
            self.metadata.hide()
            self.command_view.clear()
            self.location_label.clear()
            self.info_btn.setEnabled(False)
            self.output.clear()
            self._output_id = None
            self._set_output_note("记录可能已被清理或不属于当前筛选范围；不能据此判断进程是否退出。" if bg_id else "", warning=True)
            self.stop_btn.setEnabled(False)
            return
        status, _ = process_status(snap, busy=bg_id in self._busy)
        self.detail_title.setText(status)
        self.info_btn.setEnabled(True)
        self.metadata.setVisible(self.info_btn.isChecked())
        if self.command_view.toPlainText() != snap["command"]:
            self.command_view.setPlainText(snap["command"])
        location = "工作目录：" + (snap.get("cwd") or "未知")
        self.location_label.setToolTip(location)
        self.location_label.setText(self.location_label.fontMetrics().elidedText(
            location, Qt.ElideMiddle, max(100, self.width() - 84)))
        meta = (f"命令：{snap['command']}\n来源：{_source(snap)}\n"
                f"项目：{snap.get('project') or '无项目'}\n工作目录：{snap.get('cwd') or '未知'}\n"
                f"开始：{snap['started_iso']}   后台 id：{bg_id}\n"
                f"运行 id：{snap.get('run_id') or '未记录'}   任务 id：{snap.get('task_id') or '未建立'}")
        if self.metadata.toPlainText() != meta:
            self.metadata.setPlainText(meta)
        busy = bg_id in self._busy or snap.get("stop_in_progress")
        self.stop_btn.setText("停止中…" if busy else
                              ("重试停止" if snap.get("stop_requested") else "停止此任务"))
        self.stop_btn.setEnabled(snap["running"] is not False and not busy and not self._refresh_error)
        warning = any(snap.get(key) for key in
                      ("output_truncated", "read_error", "process_error", "stop_error", "tree_error"))
        self._set_output_note(output_notes(snap), warning=warning)
        text = "".join(snap["output_tail"])
        changed_id = self._output_id != bg_id
        if changed_id or self.output.toPlainText() != text:
            bar = self.output.verticalScrollBar()
            old_value = bar.value()
            bar.blockSignals(True)
            self.output.setPlainText(text)  # 原样纯文本，不把工具输出当作 HTML。
            bar.setValue(bar.maximum() if self.follow_box.isChecked() else
                         (0 if changed_id else old_value))
            bar.blockSignals(False)
        self._output_id = bg_id

    def _set_output_note(self, text, *, warning=False):
        if self.output_note.toPlainText() != text:
            self.output_note.setPlainText(text)
        self.output_note.setVisible(bool(text))
        c = self._color
        self.output_note.setStyleSheet(
            f"QPlainTextEdit {{ background: {c('badge_warn_bg') if warning else c('sidebar_bg')}; "
            f"color: {c('badge_warn_text') if warning else c('text_dim')}; "
            f"border: none; border-radius: 5px; padding: 3px 6px; font-size: 11px; }}"
        )
        self.output_note.setFixedHeight(min(85, max(35, 16 +
            (text.count("\n") + 1) * self.output_note.fontMetrics().lineSpacing())))

    @Slot(bool)
    def _follow_changed(self, checked):
        if checked:
            bar = self.output.verticalScrollBar()
            bar.setValue(bar.maximum())

    @Slot(int)
    def _output_scrolled(self, value):
        if value < self.output.verticalScrollBar().maximum() - 4:
            self.follow_box.setChecked(False)

    @Slot()
    def _stop_selected(self):
        bg_id = self._selected_id
        if not bg_id or bg_id in self._busy or self._refresh_error:
            return
        # 点击时重新核对，不使用上次轮询的运行态；后台管理器再做一次最终核对。
        try:
            snap = background.get_snapshot(bg_id)
        except Exception as exc:
            self._feedback[bg_id] = f"无法核对任务，未请求停止：{type(exc).__name__}: {exc}"
            self._render_selected()
            return
        if snap is None or snap["running"] is False or snap.get("stop_in_progress"):
            self.refresh()
            return
        self._busy.add(bg_id)
        self._feedback[bg_id] = "正在请求停止并核对主进程退出…"
        self._render_selected()
        try:
            threading.Thread(target=_stop_in_thread, args=(bg_id, self._events),
                             name=f"bg-stop-{bg_id}", daemon=True).start()
        except Exception as exc:
            self._stop_finished(bg_id, {"found": True, "confirmed": False,
                                       "stop_error": f"无法启动停止线程：{exc}"})

    @Slot(str, object)
    def _stop_finished(self, bg_id, result):
        self._busy.discard(bg_id)
        if not result.get("found"):
            note = "任务记录已不在管理器中，无法核对停止结果。"
        elif result.get("confirmed"):
            note = "已确认主进程退出。"
        else:
            note = "尚未确认主进程退出，可刷新或重试。"
        for key in ("stop_error", "tree_error"):
            if result.get(key):
                note += " " + result[key]
        self._feedback[bg_id] = note
        # 只更新这个 id 的回执，展示以当前快照为准，迟到回执不会覆盖新选择。
        self.refresh()
