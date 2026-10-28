"""Ctrl+F 搜索浮层（mixin for ChatUI）。

聊天区右上角的一条搜索框：输入关键词，回车 / 上下箭头在命中之间跳，显示"第几处 / 共几处"。
查找本身在 MessageView.find_text（选中命中、把收起的过程 / 卡片打开、滚过去）。

依赖宿主提供：self._t / self.chat_area / self._search_widget（实例属性）
"""
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from .message_view import _icon, _svg

_SVG_UP = _svg('<path d="M4 10l4-4 4 4"/>')
_SVG_DOWN = _svg('<path d="M4 6l4 4 4-4"/>')
_SVG_CLOSE = _svg('<path d="M4 4l8 8M12 4l-8 8"/>')


class SearchOverlayMixin:
    """Ctrl+F 浮窗搜索的全部 UI + 跳转逻辑。"""

    def _toggle_search(self):
        """打开 / 关闭对话内搜索。"""
        if self._search_widget is not None and self._search_widget.isVisible():
            self._close_search()
            return

        from PySide6.QtWidgets import QLineEdit, QHBoxLayout as _HLA

        if self._search_widget is None:
            container = QWidget(self)
            container.setObjectName("searchContainer")
            layout = _HLA(container)
            layout.setContentsMargins(8, 6, 6, 6)
            layout.setSpacing(4)

            search_input = QLineEdit()
            search_input.setPlaceholderText("在对话中搜索")
            search_input.setAccessibleName("在对话中搜索")
            search_input.setMinimumWidth(220)
            search_input.returnPressed.connect(lambda: self._search_next(search_input.text()))
            search_input.textChanged.connect(lambda _t: self._search_reset())
            layout.addWidget(search_input)

            count = QLabel("")
            count.setMinimumWidth(48)
            count.setAlignment(Qt.AlignCenter)
            layout.addWidget(count)

            buttons = []
            for svg, tip, slot in ((_SVG_UP, "上一个", lambda: self._search_prev(search_input.text())),
                                   (_SVG_DOWN, "下一个（Enter）", lambda: self._search_next(search_input.text())),
                                   (_SVG_CLOSE, "关闭搜索（Esc）", self._close_search)):
                btn = QPushButton()
                btn.setFixedSize(28, 28)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setToolTip(tip)
                btn.setAccessibleName(tip.split("（")[0])
                btn.setIconSize(QSize(14, 14))
                btn.clicked.connect(lambda _=False, s=slot: s())
                btn._svg = svg
                layout.addWidget(btn)
                buttons.append(btn)

            container._input = search_input
            container._count = count
            container._buttons = buttons
            self._search_widget = container

        self._style_search_widget()
        # 贴着聊天区右上角
        self._search_widget.adjustSize()
        x = self.chat_area.x() + self.chat_area.width() - self._search_widget.width() - 20
        y = self.chat_area.y() + 10
        self._search_widget.move(x, y)
        self._search_widget.show()
        self._search_widget.raise_()
        self._search_widget._input.setFocus()
        self._search_widget._input.selectAll()

    def _style_search_widget(self):
        """按当前主题上色（每次打开时重设：中途可能切过深浅色）。"""
        w = self._search_widget
        w.setStyleSheet(
            f"#searchContainer {{ background: {self._t('mv_card_bg')}; "
            f"border: 1px solid {self._t('mv_chip_border')}; border-radius: 10px; }}"
            f"QLineEdit {{ border: 1px solid {self._t('mv_card_border')}; border-radius: 6px; "
            f"padding: 4px 8px; font-size: 13px; background: {self._t('mv_card_bg')}; "
            f"color: {self._t('mv_text')}; }}"
            f"QLineEdit:focus {{ border-color: {self._t('mv_indigo')}; }}"
            f"QLabel {{ color: {self._t('mv_text_sec')}; font-size: 12px; background: transparent; }}"
            f"QPushButton {{ border: none; background: transparent; border-radius: 6px; }}"
            f"QPushButton:hover {{ background: {self._t('mv_header_bg')}; }}"
        )
        for btn in w._buttons:
            btn.setIcon(QIcon(_icon(btn._svg, self._t("mv_text_sec"), 14)))

    def _search_next(self, text):
        self._search_go(text, backward=False)

    def _search_prev(self, text):
        self._search_go(text, backward=True)

    def _search_go(self, text, backward):
        if not hasattr(self.chat_area, "find_text"):
            return
        n, total = self.chat_area.find_text(text, backward=backward)
        if self._search_widget is not None:
            if not (text or "").strip():
                self._search_widget._count.setText("")
            else:
                self._search_widget._count.setText(f"{n} / {total}" if total else "没有")

    def _search_reset(self):
        """改了关键词：清掉上一次的选中和计数，回车从头找。"""
        if hasattr(self.chat_area, "clear_find"):
            self.chat_area.clear_find()
        if self._search_widget is not None:
            self._search_widget._count.setText("")

    def _close_search(self):
        if self._search_widget is not None:
            self._search_widget.hide()
        if hasattr(self.chat_area, "clear_find"):
            self.chat_area.clear_find()
