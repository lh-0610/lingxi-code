"""输入框里的引用卡（设计稿「消息区：引用」②）。

引用不再往输入框里塞「> 」文字：每段一张卡（``quotes.QuoteCard``），在输入框上方，× 去掉，
最多 ``quotes.MAX_QUOTES`` 张。发送时 ``_compose_input`` 把卡片和打的字拼成 Markdown 引用
（``quotes.compose``）——发给模型、进历史、进运行中队列的都是这一份文字。
"""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import quotes as _q


class QuoteInputMixin:
    def _build_quote_area(self):
        """输入框外框里、文字上方的一列引用卡（没有引用时藏起来）。"""
        self._pending_quotes = []
        self.quote_area = QWidget()
        self.quote_area.setObjectName("quoteArea")
        self.quote_area_layout = QVBoxLayout(self.quote_area)
        self.quote_area_layout.setContentsMargins(0, 0, 0, 0)
        self.quote_area_layout.setSpacing(6)
        self.quote_area.setVisible(False)
        return self.quote_area

    # ── 右键 / 选中浮条：引用到输入框 ──
    def context_menu_quote(self, text):
        """「引用到输入框」：加一张引用卡，光标留在输入框里接着打字。"""
        text = _q._clean(text)
        if not text:
            return
        if text in self._pending_quotes:
            self.entry.setFocus()
            return
        if len(self._pending_quotes) >= _q.MAX_QUOTES:
            self._show_toast(f"最多引用 {_q.MAX_QUOTES} 段，先去掉几段", 2500)
            return
        self._pending_quotes.append(text)
        self._refresh_quote_cards()
        self._check_input_state()
        self.entry.setFocus()

    def _refresh_quote_cards(self):
        lay = self.quote_area_layout
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for text in self._pending_quotes:
            card = _q.QuoteCard(text, self._t)
            card.removed.connect(self._remove_quote_card)
            lay.addWidget(card)
        self.quote_area.setVisible(bool(self._pending_quotes))

    def _remove_quote_card(self, card):
        text = card.text()
        if text in self._pending_quotes:
            self._pending_quotes.remove(text)
        self._refresh_quote_cards()
        self._check_input_state()
        self.entry.setFocus()

    def _compose_input(self) -> str:
        """引用卡 + 输入框里的字 → 要发送的文字（没有引用就是输入框里的字）。"""
        return _q.compose(self._pending_quotes, self.entry.toPlainText().strip())

    def _clear_quotes(self):
        if self._pending_quotes:
            self._pending_quotes = []
            self._refresh_quote_cards()
