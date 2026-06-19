"""Ctrl+F 搜索浮层（mixin for ChatUI）。

聊天区右上角的搜索框 —— 输入关键词 + 上下箭头跳转 + 关闭。
QTextBrowser 内置 `find()`，找不到时绕到首/尾再找一次实现循环搜索。

依赖宿主提供：self._t / self.chat_area / self._search_widget（实例属性）
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPushButton, QWidget


class SearchOverlayMixin:
    """Ctrl+F 浮窗搜索的全部 UI + 跳转逻辑。"""

    def _toggle_search(self):
        """Show/hide floating search bar"""
        if self._search_widget is not None and self._search_widget.isVisible():
            self._close_search()
            return

        from PySide6.QtWidgets import QLineEdit, QHBoxLayout as _HLA

        if self._search_widget is None:
            container = QWidget(self)
            container.setObjectName("searchContainer")
            container.setStyleSheet(
                f"#searchContainer {{ background: {self._t('search_bg')}; "
                f"border: 1px solid {self._t('search_border')}; border-radius: 10px; }}"
            )
            layout = _HLA(container)
            layout.setContentsMargins(8, 4, 8, 4)
            layout.setSpacing(4)

            search_input = QLineEdit()
            search_input.setPlaceholderText("Search in chat...")
            search_input.setStyleSheet(
                f"QLineEdit {{ border: 1px solid {self._t('search_input_border')}; border-radius: 6px; "
                f"padding: 5px 10px; font-size: 12px; background: {self._t('search_input_bg')}; "
                f"color: {self._t('search_input_text')}; }}"
                f"QLineEdit:focus {{ border-color: {self._t('search_input_focus')}; }}"
            )
            search_input.setMinimumWidth(220)
            search_input.returnPressed.connect(lambda: self._search_next(search_input.text()))
            layout.addWidget(search_input)

            nav_btn_css = (
                f"QPushButton {{ border: none; font-size: 14px; color: {self._t('search_btn_text')}; "
                f"background: {self._t('search_btn_bg')}; }}"
                f"QPushButton:hover {{ background: {self._t('search_btn_hover_bg')}; "
                f"color: {self._t('search_btn_hover_color')}; border-radius: 4px; }}"
            )

            prev_btn = QPushButton("▲")
            prev_btn.setFixedSize(28, 28)
            prev_btn.setCursor(Qt.PointingHandCursor)
            prev_btn.setStyleSheet(nav_btn_css)
            prev_btn.clicked.connect(lambda: self._search_prev(search_input.text()))
            layout.addWidget(prev_btn)

            next_btn = QPushButton("▼")
            next_btn.setFixedSize(28, 28)
            next_btn.setCursor(Qt.PointingHandCursor)
            next_btn.setStyleSheet(nav_btn_css)
            next_btn.clicked.connect(lambda: self._search_next(search_input.text()))
            layout.addWidget(next_btn)

            close_btn = QPushButton("✕")
            close_btn.setFixedSize(28, 28)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setStyleSheet(
                f"QPushButton {{ border: none; font-size: 14px; color: {self._t('search_close')}; background: transparent; }}"
                f"QPushButton:hover {{ color: {self._t('search_close_hover')}; "
                f"background: {self._t('search_close_hover_bg')}; border-radius: 4px; }}"
            )
            close_btn.clicked.connect(self._close_search)
            layout.addWidget(close_btn)

            container._input = search_input
            self._search_widget = container

        # Position at top-right of chat area
        self._search_widget.adjustSize()
        x = self.chat_area.x() + self.chat_area.width() - self._search_widget.width() - 20
        y = self.chat_area.y() + 10
        self._search_widget.move(x, y)
        self._search_widget.show()
        self._search_widget.raise_()
        self._search_widget._input.setFocus()
        self._search_widget._input.selectAll()

    def _search_next(self, text):
        # 消息流已改 MessageView(QScrollArea,无 find/textCursor)——跨控件搜索待重做,先优雅禁用
        if not text or not hasattr(self.chat_area, "find"):
            return
        found = self.chat_area.find(text)
        if not found:
            cursor = self.chat_area.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.chat_area.setTextCursor(cursor)
            self.chat_area.find(text)

    def _search_prev(self, text):
        if not text or not hasattr(self.chat_area, "find"):
            return
        from PySide6.QtGui import QTextDocument as _QTD
        found = self.chat_area.find(text, _QTD.FindBackward)
        if not found:
            cursor = self.chat_area.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.chat_area.setTextCursor(cursor)
            self.chat_area.find(text, _QTD.FindBackward)

    def _close_search(self):
        if self._search_widget is not None:
            self._search_widget.hide()
