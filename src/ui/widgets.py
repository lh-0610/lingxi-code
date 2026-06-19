"""自定义 Qt 控件 + 线程通信桥。

- SignalBridge：worker 线程通过它把渲染请求 emit 到 UI 线程
- DragDropTextBrowser / DragDropTextEdit：把拖拽事件转发给主窗口（避免子控件吞掉）
- HistoryRow：侧栏会话条容器（删除按钮通过布局排在标题右侧）
- CloseConfirmDialog：关闭软件时的"最小化到托盘 / 退出"二选一对话框
"""
import os

from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSize, QRectF
from PySide6.QtGui import QIcon, QPainter, QFont, QFontMetrics, QColor, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
    QTextEdit, QVBoxLayout, QWidget, QStyle, QStyledItemDelegate,
)

from ._base import BASE_DIR


class SignalBridge(QObject):
    append_signal = Signal(str, str)       # (text, tag)
    remove_thinking = Signal()
    update_thinking = Signal(str)          # 更新等待指示器文
    render_md = Signal(str)                # 渲染 Markdown 替换最后的纯文
    show_retry = Signal(str)               # 显示重试按钮 + 错误信息
    finished = Signal(object)              # (finished_session) 完成生成的会话对象
    token_usage = Signal(dict, dict)   # (session_usage, round_usage)
    sessions_refresh = Signal()        # 异步标题生成完后刷新侧栏会话列表
    # 让 worker 线程能阻塞式请求 UI 弹确认框：发 (命令文本, 用于回传结果的 dict,
    # threading.Event)。槽运行在 UI 主线程，调完 QMessageBox 后写 dict + 唤醒 Event
    confirm_request = Signal(str, object, object)
    # edit_file 之前弹 diff 预览：发 (path, diff_text, result_dict, event)
    edit_confirm_request = Signal(str, str, object, object)
    remote_submit = Signal(str)        # Telegram 遥控消息注入（跨线程 → 主线程）
    # 手机端点完确认后，让主线程隐藏可能还挂着的 PC 确认卡（仅 UI，result/done 已由远程写好）
    dismiss_confirm = Signal()
    show_plan = Signal(object)         # 传 list[{text,status}]：update_plan → 主线程


class DragDropTextBrowser(QTextBrowser):
    """QTextBrowser: forward file drag/drop to parent window"""

    def dragEnterEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dragEnterEvent(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dropEvent(event)
        else:
            super().dropEvent(event)


class DragDropTextEdit(QTextEdit):
    """QTextEdit: forward file drag/drop to parent window。

    同时强制粘贴为纯文本——否则粘进带样式的富文本（如带红底的字）后，
    光标会继承那段格式，后续打字全是那个样式。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptRichText(False)

    def insertFromMimeData(self, source):
        # 只取纯文本，丢弃所有富文本格式（颜色 / 背景 / 字体等）
        if source.hasText():
            self.insertPlainText(source.text())
        else:
            super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dragEnterEvent(event)
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            self.window().dropEvent(event)
        else:
            super().dropEvent(event)


# delegate 数据角色：QListWidgetItem 存储相对路径 & 匹配位置
_ROLE_PATH = Qt.UserRole
_ROLE_POS = Qt.UserRole + 1
_ROLE_ISDIR = Qt.UserRole + 2


class _FileItemDelegate(QStyledItemDelegate):
    """自绘文件补全项：basename 高亮匹配 + dirname 灰字。"""

    # 外部通过 _apply_completer_theme 设置
    highlight_color = "#4fc3f7"
    sel_bg = None       # 选中背景；None → 用 Qt 默认 highlight
    text_color = None   # 非选中文字色（主题色）；None → 回退 palette.Text
    folder_icon = None  # 文件夹图标 QPixmap（_apply_completer_theme 设）
    file_icon = None    # 文件图标 QPixmap

    def initStyleOption(self, option, index):
        # 清空默认文本：item 的 DisplayRole 是 basename，全部交给 paint 自绘。
        # 必须在这里清，不能在 paint 里——QStyledItemDelegate.paint 内部会重新调
        # self.initStyleOption(option, index) 填回 text，paint 里改 opt.text 会被覆盖、依旧重影。
        super().initStyleOption(option, index)
        option.text = ""

    def paint(self, painter, option, index):
        # ① 背景（selected/hover）：text 已被 initStyleOption 清空，super 只画背景不画文字
        painter.save()
        super().paint(painter, option, index)
        painter.restore()

        # ② 选中覆盖（QSS 未定义 selected 色，手动绘制）
        if option.state & QStyle.State_Selected and self.sel_bg:
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self.sel_bg))
            painter.drawRoundedRect(option.rect.adjusted(4, 2, -4, -2), 6, 6)
            painter.restore()

        # ③ 提取数据
        path_str = index.data(_ROLE_PATH) or ""
        positions = set(index.data(_ROLE_POS) or [])
        basename = os.path.basename(path_str)
        dirname = os.path.dirname(path_str)
        path_len = len(os.path.dirname(path_str)) + (1 if dirname else 0)

        painter.save()
        font = option.font
        font.setPixelSize(13)
        painter.setFont(font)
        fm = QFontMetrics(font)

        mx, my = 12, 8                           # 与 QSS padding 协调
        x0 = option.rect.x() + mx
        y_base = option.rect.y() + my + fm.ascent()

        is_sel = bool(option.state & QStyle.State_Selected)
        if self.text_color:
            # 选中/非选中都用主题文字色，只靠 sel_bg 背景区分——避免选中时用系统
            # HighlightedText（白）落在浅色 sel_bg 上看不清
            norm_color = QColor(self.text_color)
        else:
            norm_color = option.palette.color(
                QPalette.HighlightedText if is_sel else QPalette.Text)
        muted_color = QColor("#aaa") if is_sel else QColor("#888")
        hl_color = QColor(self.highlight_color)

        # ── 文件夹 / 文件图标（Lucide SVG，由 _apply_completer_theme 设置） ──
        icon = self.folder_icon if index.data(_ROLE_ISDIR) else self.file_icon
        if icon is not None and not icon.isNull():
            iy = option.rect.y() + my + (fm.height() - 16) // 2
            painter.drawPixmap(int(x0), int(iy), 16, 16, icon)
        x0 += 24

        # ── 第一行：basename ──
        for i, ch in enumerate(basename):
            global_i = path_len + i
            if global_i in positions:
                painter.setPen(hl_color)
                f = painter.font()
                f.setBold(True)
                painter.setFont(f)
            else:
                painter.setPen(norm_color)
                f = painter.font()
                f.setBold(False)
                painter.setFont(f)
            painter.drawText(QRectF(x0, y_base - fm.ascent(),
                                    fm.horizontalAdvance(ch), fm.height()),
                             Qt.AlignLeft | Qt.AlignTop, ch)
            x0 += fm.horizontalAdvance(ch)

        # ── 第二行：dirname ──
        if dirname:
            dn_font = QFont(font)
            dn_font.setPixelSize(11)
            painter.setFont(dn_font)
            dn_fm = QFontMetrics(dn_font)
            y_dn = (option.rect.y() + my + fm.height() + 2
                    + dn_fm.ascent())
            painter.setPen(muted_color)
            painter.drawText(QRectF(option.rect.x() + mx, y_dn - dn_fm.ascent(),
                                    option.rect.width() - mx * 2, dn_fm.height()),
                             Qt.AlignLeft | Qt.AlignTop | Qt.TextSingleLine,
                             dirname)

        painter.restore()

    def sizeHint(self, option, index):
        path_str = index.data(_ROLE_PATH) or ""
        has_dirname = bool(os.path.dirname(path_str))
        fm = QFontMetrics(option.font)
        h = 8 + fm.height() + (fm.height() - 2 if has_dirname else 0) + 8
        return QSize(-1, max(h, 52))


class FileCompleter(QWidget):
    """@文件名补全浮窗。

    在输入框中键入 "@" 后弹出，列出项目根下的文件。
    支持键盘上下移动、Enter/Tab 选中、Esc 关闭。
    """

    item_selected = Signal(str)  # 选中时发射完整相对路径

    def __init__(self, parent=None):
        # 主窗口子控件（不用 Qt.ToolTip 顶层窗口——否则切到别的应用时浮窗还悬在桌面最上层）。
        # 定位用相对主窗口的本地坐标 + raise_ 盖住聊天区。
        super().__init__(parent)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 自定义 QWidget 子类必须设这个，否则 QSS 的 background/border 不绘制 → 透明底
        self.setAttribute(Qt.WA_StyledBackground, True)

        from PySide6.QtWidgets import QVBoxLayout as _VL, QListWidget as _LW
        layout = _VL(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.list_widget = _LW()
        self.list_widget.setFocusPolicy(Qt.NoFocus)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.setItemDelegate(_FileItemDelegate(self.list_widget))
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list_widget)

        self._all_items = []   # 兼容旧字段（逐层浏览已不用）
        self._colorized = False
        self._current_dir = ""  # 逐层浏览的当前目录（相对项目根，"" = 根）
        self.lister = None      # 回调 lister(rel_dir) -> [(name, is_dir)]，列单层子项
        self.reposition = None  # 回调：项数/高度变化后让宿主重新定位（底部对齐输入框）

    # ── 样式 ──
    def apply_theme(self, bg, border, text, sel_bg=None, sel_text=None,
                    hover_bg=None, hover_text=None):
        """按当前主题调色。由 ChatUI 在初始化和切主题时调用。"""
        self._colorized = True
        # 存储颜色供 delegate 使用
        self._sel_bg = sel_bg
        self._sel_text = sel_text
        self._text = text
        dlg = self.list_widget.itemDelegate()
        if isinstance(dlg, _FileItemDelegate):
            dlg.sel_bg = sel_bg
        sel_qss = (f"QListWidget::item:selected {{ background: {sel_bg}; "
                   f"color: {sel_text}; }}" if sel_bg and sel_text else "")
        hover_qss = (f"QListWidget::item:hover {{ background: {hover_bg}; "
                     f"color: {hover_text}; }}" if hover_bg and hover_text else "")
        self.setStyleSheet(
            f"FileCompleter {{ background: {bg}; border: 1px solid {border}; "
            f"border-radius: 8px; }}"
            f"QListWidget {{ background: transparent; border: none; "
            f"outline: none; color: {text}; font-size: 13px; "
            f"padding: 4px 0; }}"
            f"QListWidget::item {{ padding: 6px 14px; border-radius: 4px; }}"
            f"{sel_qss}{hover_qss}"
        )

    # ── 显示 / 过滤 ──
    def set_files(self, files: list):
        """设置全量候选列表（相对路径列表）。"""
        self._all_items = files
        self.filter_and_show("")

    def open_root(self):
        """打 @ 时调用：回到项目根（实际列表由随后的 filter_and_show 刷新）。"""
        self._current_dir = ""

    def filter_and_show(self, query: str):
        """列【当前目录】(_current_dir) 的直接子项，query 做子序列过滤 + 高亮。
        逐层浏览：选文件夹进入下一层、选文件插入完整路径、.. 返回上级。"""
        from PySide6.QtWidgets import QListWidgetItem as _LI
        self.list_widget.clear()
        rows = []
        if self._current_dir:
            rows.append(("..", True, []))          # 返回上一级
        entries = self.lister(self._current_dir) if self.lister else []
        q = (query or "").lower()
        for name, is_dir in entries:
            if q:
                score, positions = self.fuzzy_match_positions(q, name)
                if score < 0:
                    continue
            else:
                positions = []
            rows.append((name, is_dir, positions))

        if not rows:
            self.hide()
            return

        for name, is_dir, positions in rows:
            item = _LI(name)
            item.setData(_ROLE_PATH, name)         # 单层名（delegate 显示用）
            item.setData(_ROLE_POS, positions)
            item.setData(_ROLE_ISDIR, is_dir)
            self.list_widget.addItem(item)

        self.list_widget.setCurrentRow(0)
        self._adjust_size()
        self.show()
        if self.reposition:        # 项数变→高度变，重新定位让底部继续贴着输入框
            self.reposition()

    def _activate(self, item):
        """.. 返回上级 / 文件夹进入下一层 / 文件 emit 完整相对路径。"""
        if item is None:
            return
        name = item.data(_ROLE_PATH) or item.text()
        is_dir = bool(item.data(_ROLE_ISDIR))
        if name == "..":
            self._current_dir = os.path.dirname(self._current_dir)
            self.filter_and_show("")
        elif is_dir:
            self._current_dir = (self._current_dir + "/" + name).strip("/")
            self.filter_and_show("")
        else:
            full = (self._current_dir + "/" + name).strip("/")
            self.item_selected.emit(full)
            self.hide()

    def _adjust_size(self):
        """根据当前项目数动态调整浮窗高度。"""
        count = self.list_widget.count()
        row_h = 52  # 与 _FileItemDelegate.sizeHint 一致
        h = min(count * row_h, 350) + 8
        self.setFixedHeight(h)
        self.setFixedWidth(max(380, self.width()))

    # ── 键盘导航 ──
    def navigate_up(self):
        row = self.list_widget.currentRow()
        if row > 0:
            self.list_widget.setCurrentRow(row - 1)

    def navigate_down(self):
        row = self.list_widget.currentRow()
        if row < self.list_widget.count() - 1:
            self.list_widget.setCurrentRow(row + 1)

    def confirm_selection(self):
        """确认当前选中项：.. 返回 / 文件夹进入 / 文件选定。"""
        self._activate(self.list_widget.currentItem())

    # ── 信号槽 ──
    def _on_item_clicked(self, item):
        self._activate(item)

    # ── 失焦关闭 ──
    def focusOutEvent(self, event):
        # 延迟关闭，让 click 事件有时间触发
        QTimer.singleShot(150, self._close_if_no_focus)
        super().focusOutEvent(event)

    def _close_if_no_focus(self):
        from PySide6.QtWidgets import QApplication
        focused = QApplication.focusWidget()
        # 如果焦点还在 entry 或 list_widget 上，不关
        if focused == self.list_widget or (self.parent() and
                hasattr(self.parent(), 'entry') and focused == self.parent().entry):
            return
        self.hide()

    # ── 排序支持 ──
    @staticmethod
    def fuzzy_score(query: str, text: str) -> int:
        """简单的子序列模糊匹配得分，用于后续排序（阶段2预置接口）。"""
        qi = 0
        score = 0
        prev_match = False
        for ch in text.lower():
            if qi < len(query) and ch == query[qi]:
                score += 10 if prev_match else 5  # 连续匹配加分
                # 路径分隔符后匹配额外加分（更像文件名匹配）
                if not prev_match and score > 5:
                    score += 2
                prev_match = True
                qi += 1
            else:
                prev_match = False
        return score if qi == len(query) else -1

    @staticmethod
    def fuzzy_match_positions(query: str, text: str):
        """子序列匹配，返回 (score, positions)。

        positions: query 各字符在 text.lower() 中命中的下标列表；
        未完全匹配时返回 (-1, [])。
        """
        q = query.lower()
        t = text.lower()
        qi = 0
        score = 0
        prev_match = False
        positions = []
        for i, ch in enumerate(t):
            if qi < len(q) and ch == q[qi]:
                score += 10 if prev_match else 5
                if not prev_match and score > 5:
                    score += 2
                prev_match = True
                positions.append(i)
                qi += 1
            else:
                prev_match = False
        return (score, positions) if qi == len(q) else (-1, [])


class HistoryRow(QWidget):
    """Sidebar history row（删除按钮通过布局排在标题右侧，永远可见）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 纯 QWidget 默认不画样式表背景；要让 #historyRow[rowstate]/[current] 的整行底色
        # 真正绘制出来，必须开 WA_StyledBackground（否则属性选择器只生效在能自绘背景的控件上）。
        self.setAttribute(Qt.WA_StyledBackground, True)
        # WA_Hover：让 #historyRow:hover 整行底色在鼠标移入(含移到内部标题按钮/× 上)时也生效。
        # 只靠子按钮 :hover 的话高亮只覆盖标题按钮那一截、左边被 marker 推开、右边到不了 ×，
        # 看着是个左缩右缺的半截色块，很难看；整行 hover 才和选中态(整行底色)一致。
        self.setAttribute(Qt.WA_Hover, True)

    def watch_hover(self, widget):
        # 兼容旧调用，无操作
        pass


class LoadingSpinner(QWidget):
    """旋转的缺口圆环 loading 指示（侧栏会话"生成中"用）。

    QPainter 自绘一段 270° 圆弧 + QTimer 转角度——比字符 spinner（◐◓◑◒）更像
    网页那种转圈，且不依赖字体里有没有那些几何字符的 glyph。
    """

    def __init__(self, size=16, color="#3b82f6", parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._color = QColor(color)
        self._timer = QTimer(self)   # parent=self → 控件被 deleteLater 时一并回收
        self._timer.timeout.connect(self._rotate)
        self._timer.start(50)

    def _rotate(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPen
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        m = 2.0
        rect = QRectF(m, m, self.width() - 2 * m, self.height() - 2 * m)
        pen = QPen(self._color)
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        # Qt drawArc 角度单位 = 1/16 度；负号让它顺时针转，270° 留个缺口
        painter.drawArc(rect, int(-self._angle * 16), int(270 * 16))


class GeneratingBadge(QWidget):
    """侧栏会话"生成中"徽章：转圈 + 自走秒表「生成中 MM:SS」。

    自带 QTimer 每秒刷新文字；控件随会话行重建被 deleteLater 时，timer 一并回收。
    start_ts 用 time.monotonic()（不受系统时钟回拨影响）。"""

    def __init__(self, start_ts, spin_color="#3b82f6", text_color="#3b82f6",
                 show_spinner=True, bg="transparent", border="transparent", parent=None):
        super().__init__(parent)
        self._start_ts = start_ts
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        if show_spinner:
            self._spin = LoadingSpinner(size=14, color=spin_color)
            lay.addWidget(self._spin, 0, Qt.AlignVCenter)
        self._label = QLabel()
        # 有 bg 时做成药丸徽章（设计稿:无边框白色药丸 radius6/padding3·7/11px·500;
        # border 传 transparent/none 即省掉描边——dark 主题仍传色保留描边）,否则纯文字
        if bg != "transparent":
            _bd = (f"border:1px solid {border};"
                   if border not in ("transparent", "none", "") else "border:none;")
            self._label.setStyleSheet(
                f"color:{text_color}; font-size:11px; font-weight:500; background:{bg}; "
                f"{_bd} border-radius:6px; padding:3px 7px;"
            )
        else:
            self._label.setStyleSheet(
                f"color:{text_color}; font-size:11px; background:transparent; border:none;"
            )
        lay.addWidget(self._label, 0, Qt.AlignVCenter)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    def _tick(self):
        import time
        elapsed = max(0, int(time.monotonic() - self._start_ts))
        mm, ss = divmod(elapsed, 60)
        self._label.setText(f"生成中 {mm:02d}:{ss:02d}")


class BlinkingCursor(QWidget):
    """会话行"生成中"标题后的闪烁竖条（step-end blink），呼应设计稿里的文本光标。

    自带 QTimer 定时翻转可见性；随会话行重建被 deleteLater 时 timer 一并回收。"""

    def __init__(self, color="#4a59e0", width=2, height=15, parent=None):
        super().__init__(parent)
        self.setFixedSize(width, height)
        self._color = QColor(color)
        self._on = True
        self._timer = QTimer(self)   # parent=self → 控件回收时 timer 一并回收
        self._timer.timeout.connect(self._blink)
        self._timer.start(530)

    def _blink(self):
        self._on = not self._on
        self.update()

    def paintEvent(self, event):
        if not self._on:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._color)


class CloseConfirmDialog(QDialog):
    ACTION_HIDE = "hide"
    ACTION_QUIT = "quit"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关闭灵犀")
        self.setModal(True)
        self.setFixedSize(420, 200)
        self.action = None

        icon_path = os.path.join(BASE_DIR, "icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 20, 26, 18)
        root.setSpacing(12)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(8)

        title = QLabel("关闭灵犀？")
        title.setObjectName("closeTitle")
        title.setWordWrap(True)
        text_col.addWidget(title)

        desc = QLabel(
            "最小化后，灵犀会继续在系统托盘后台运行（双击托盘图标可再唤起）。"
            "退出软件将完全关闭灵犀。"
        )
        desc.setObjectName("closeDescription")
        desc.setWordWrap(True)
        text_col.addWidget(desc)

        self.remember_check = QCheckBox("记住我的选择，下次不再询问")
        self.remember_check.setObjectName("closeRemember")
        text_col.addWidget(self.remember_check)
        root.addLayout(text_col)
        root.addStretch()

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(10)
        buttons.addStretch()

        hide_btn = QPushButton("最小化到托盘")
        hide_btn.setObjectName("closePrimaryButton")
        hide_btn.setDefault(True)
        hide_btn.clicked.connect(self._choose_hide)

        quit_btn = QPushButton("退出软件")
        quit_btn.setObjectName("closeSecondaryButton")
        quit_btn.clicked.connect(self._choose_quit)

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("closeSecondaryButton")
        cancel_btn.clicked.connect(self.reject)

        for btn in (hide_btn, quit_btn, cancel_btn):
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumSize(92, 32)
            buttons.addWidget(btn)

        root.addLayout(buttons)
        self._apply_style()

    def _choose_hide(self):
        self.action = self.ACTION_HIDE
        self.accept()

    def _choose_quit(self):
        self.action = self.ACTION_QUIT
        self.accept()

    def _apply_style(self):
        is_dark = bool(self.parent() and getattr(self.parent(), "theme", "light") == "dark")
        bg = "#11151b" if is_dark else "#f7f9fc"
        fg = "#eef2f7" if is_dark else "#111827"
        muted = "#b9c2cf" if is_dark else "#262b33"
        border = "#2b3440" if is_dark else "#d8e0ec"
        button_bg = "#171c23" if is_dark else "#ffffff"
        button_hover = "#202733" if is_dark else "#f4f7fb"
        accent = "#1687d9"
        accent_hover = "#0d74c2"
        check_icon = os.path.join(BASE_DIR, "icons", "check_white.svg").replace("\\", "/")

        self.setStyleSheet(
            f"CloseConfirmDialog {{ background: {bg}; color: {fg}; }}\n"
            f"#closeTitle {{ color: {fg}; font-size: 16px; font-weight: 600;"
            f" line-height: 1.35; }}\n"
            f"#closeDescription {{ color: {muted}; font-size: 13px;"
            f" line-height: 1.35; }}\n"
            f"#closeRemember {{ color: {fg}; font-size: 13px; spacing: 8px; }}\n"
            f"#closeRemember::indicator {{ width: 16px; height: 16px;"
            f" border-radius: 4px; border: 1px solid {border}; background: {button_bg}; }}\n"
            f"#closeRemember::indicator:hover {{ border-color: {accent}; }}\n"
            f"#closeRemember::indicator:checked {{ background: {accent}; border-color: {accent};"
            f" image: url(\"{check_icon}\"); }}\n"
            f"#closeRemember::indicator:checked:hover {{ background: {accent_hover};"
            f" border-color: {accent_hover}; }}\n"
            f"#closePrimaryButton, #closeSecondaryButton {{ border-radius: 6px;"
            f" padding: 5px 10px; font-size: 13px; background: {button_bg};"
            f" color: {fg}; border: 1px solid {border}; }}\n"
            f"#closePrimaryButton {{ color: {accent}; border-color: {accent}; }}\n"
            f"#closePrimaryButton:hover {{ background: rgba(22, 135, 217, 0.10);"
            f" border-color: {accent_hover}; color: {accent_hover}; }}\n"
            f"#closeSecondaryButton:hover {{ background: {button_hover};"
            f" border-color: {accent}; }}\n"
        )
