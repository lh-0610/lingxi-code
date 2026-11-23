"""引用（设计稿「消息区：引用」）。

- 选中消息流里的文字 → 选区旁浮出「引用 · 复制」（``SelectionBar``，由 context_menus 的全局
  过滤器在松开鼠标时叫出来）；右键「引用到输入框」走同一条路。
- 引用不往输入框里塞「> 」文字，而是输入框上方一张卡（``QuoteCard``），最多 ``MAX_QUOTES`` 张。
- 发给模型的仍是 Markdown 引用（``compose``）：每段每行「> 」，段与段、引用与正文之间空一行。
  用户气泡按开头连续的「> 」行认出引用（``split_leading_quotes``），画成小一号的引用块——
  以前发过的消息重画时也一样。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QIcon, QPainter, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QWidget,
)

MAX_QUOTES = 5


# ── 文本：拼装 / 拆开 ─────────────────────────────────────────

def _clean(text: str) -> str:
    # QLabel 选中的富文本里段落之间是 U+2029 / U+2028
    return (text or "").replace(" ", "\n").replace(" ", "\n").strip()


def compose(quotes, text: str) -> str:
    """引用 + 正文 → 发给模型的文字（Markdown 引用）。没有引用时原样返回正文。"""
    blocks = []
    for q in quotes or ():
        lines = [ln.rstrip() for ln in _clean(q).splitlines()]
        if lines:
            blocks.append("\n".join(("> " + ln) if ln.strip() else ">" for ln in lines))
    text = (text or "").strip()
    if not blocks:
        return text
    head = "\n\n".join(blocks)
    return head + "\n\n" + text if text else head


def split_leading_quotes(text: str):
    """开头连续的「> 」行拆成引用（空行分段），返回 (引用列表, 剩下的正文)。

    只认开头：正文中间的「> 」是用户自己写的 Markdown，照原样留在正文里。"""
    lines = (text or "").split("\n")
    quotes, cur, i = [], None, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">"):
            body = line[1:]
            if body.startswith(" "):
                body = body[1:]
            if cur is None:
                cur = []
            cur.append(body)
        elif not line.strip():
            # 空行：结束当前这段引用；开头的空行、段与段 / 引用与正文之间的空行都跳过
            if cur is not None:
                quotes.append(cur)
                cur = None
        else:
            break
        i += 1
    if cur is not None:
        quotes.append(cur)
    out = ["\n".join(q).strip() for q in quotes]
    out = [q for q in out if q]
    if not out:
        return [], text or ""
    return out, "\n".join(lines[i:]).strip()


# ── 控件 ─────────────────────────────────────────────────────

def _svg24(body: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


class ClampText(QWidget):
    """纯文本，自动换行，最多 max_lines 行，多了最后一行末尾省略号；悬停看全文。
    QLabel 做不到按行数截（只能截高度，最后一行被切半截、没有省略号）。"""

    def __init__(self, text: str, max_lines: int, color: str, font=None, line_px: float = 19,
                 parent=None):
        super().__init__(parent)
        self._text = _clean(text)
        self._max = max(1, max_lines)
        self._color = color
        self._line_px = line_px
        if font is not None:
            self.setFont(font)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setToolTip(self._text if len(self._text) <= 2000 else self._text[:2000] + "…")
        self._cache = None        # (width, [(行文字, 是否最后一行且被截)], 实际行数)

    def text(self) -> str:
        return self._text

    def _lines(self, width: int):
        width = max(20, int(width))
        if self._cache and self._cache[0] == width:
            return self._cache[1]
        fm = QFontMetricsF(self.font())
        out = []
        paras = self._text.split("\n")
        truncated = False
        for pi, para in enumerate(paras):
            layout = QTextLayout(para, self.font())
            opt = QTextOption()
            opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            layout.setTextOption(opt)
            layout.beginLayout()
            spans = []
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                spans.append((line.textStart(), line.textLength()))
            layout.endLayout()
            if not spans:
                spans = [(0, 0)]
            for si, (s, n) in enumerate(spans):
                if len(out) == self._max:
                    truncated = True
                    break
                out.append(para[s:s + n].rstrip("\n"))
            if truncated:
                break
            if len(out) == self._max and (pi < len(paras) - 1):
                truncated = True
                break
        if truncated and out:
            # 最后一行往往正好排满：从末尾去字，直到「…」放得下
            last = out[-1].rstrip()
            while last and fm.horizontalAdvance(last + "…") > width:
                last = last[:-1]
            out[-1] = last.rstrip() + "…"
        self._cache = (width, out)
        return out

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return round(len(self._lines(w)) * self._line_px)

    def sizeHint(self):
        fm = QFontMetricsF(self.font())
        widest = max((fm.horizontalAdvance(p) for p in self._text.split("\n")), default=0)
        w = min(int(widest) + 2, 640)
        return QSize(max(w, 20), self.heightForWidth(max(w, 20)))

    def minimumSizeHint(self):
        return QSize(40, round(self._line_px))

    def resizeEvent(self, e):
        self._cache = None
        super().resizeEvent(e)

    def set_color(self, color: str):
        self._color = color
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(QColor(self._color))
        p.setFont(self.font())
        fm = QFontMetricsF(self.font())
        pad = (self._line_px - fm.height()) / 2
        for i, line in enumerate(self._lines(self.width())):
            r = QRectF(0, i * self._line_px + pad, self.width(), fm.height())
            p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, line)
        p.end()


class _Bar(QFrame):
    def __init__(self, width: int, color: str, parent=None):
        super().__init__(parent)
        self.setFixedWidth(width)
        self._w = width
        self.set_color(color)

    def set_color(self, color: str):
        self.setStyleSheet(f"background:{color}; border:none; border-radius:{max(1, self._w // 2)}px;")


class QuoteBlock(QWidget):
    """气泡里的一段引用：左边一条细线 + 小一号次要色的字，最多三行。"""

    def __init__(self, text: str, *, bar: str, color: str, font, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.bar = _Bar(2, bar)
        self.label = ClampText(text, 3, color, font)
        h.addWidget(self.bar)
        h.addWidget(self.label, 1)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def text(self) -> str:
        return self.label.text()


class QuoteCard(QFrame):
    """输入框里的一张引用卡：浅底 + 左边一条靛蓝线 + 最多两行 + ×。"""
    removed = Signal(object)

    def __init__(self, text: str, theme, parent=None):
        super().__init__(parent)
        self.setObjectName("quoteCard")
        self._text = _clean(text)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 6, 6, 6)
        h.setSpacing(8)
        self.bar = _Bar(3, theme("mv_quote_card_bar"))
        from .fonts import ui_font
        f = ui_font(12)
        f.setPixelSize(12)
        self.label = ClampText(self._text, 2, theme("mv_quote_card_text"), f, line_px=18)
        self.close_btn = QPushButton()
        self.close_btn.setObjectName("quoteCardClose")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("去掉这段引用")
        self.close_btn.setAccessibleName("去掉这段引用")
        self.close_btn.clicked.connect(lambda: self.removed.emit(self))
        h.addWidget(self.bar)
        h.addWidget(self.label, 1)
        h.addWidget(self.close_btn, 0, Qt.AlignTop)
        self.apply_theme(theme)

    def text(self) -> str:
        return self._text

    def apply_theme(self, theme):
        from .message_view import _icon, _svg
        self.setStyleSheet(
            f"QFrame#quoteCard {{ background:{theme('mv_quote_card_bg')}; border:none; border-radius:8px; }}"
            "QPushButton#quoteCardClose { background:transparent; border:none; border-radius:4px; }"
            f"QPushButton#quoteCardClose:hover {{ background:{theme('mv_chip_border')}; }}")
        self.bar.set_color(theme("mv_quote_card_bar"))
        self.label.set_color(theme("mv_quote_card_text"))
        self.close_btn.setIcon(QIcon(_icon(_svg('<path d="M4 4l8 8M12 4l-8 8"/>'),
                                           theme("mv_text_sec"), 10)))
        self.close_btn.setIconSize(QSize(10, 10))


# 引号图标（lucide quote）
_QUOTE_SVG = ('<path d="M3 21c3 0 7-1 7-8V5c0-1.25-.756-2.017-2-2H4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2 1 0 1 0 1 1v1c0 1-1 2-2 2s-1 .008-1 1.031V20c0 1 0 1 1 1z"/>'
              '<path d="M15 21c3 0 7-1 7-8V5c0-1.25-.757-2.017-2-2h-4c-1.25 0-2 .75-2 1.972V11c0 1.25.75 2 2 2h.75c0 2.25.25 4-2.75 4v3c0 1 0 1 1 1z"/>')
_COPY_SVG = ('<rect x="8" y="8" width="13" height="13" rx="2"/>'
             '<path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>')


class SelectionBar(QFrame):
    """选中消息流里的文字后浮出的「引用 · 复制」。挂在 MessageView 上（不是独立窗口）：
    跟着消息区一起被裁剪；滚动、点别处、按 Esc 就收（见 MessageView / context_menus）。"""
    quote_requested = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("selectionBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self)
        h.setContentsMargins(3, 3, 3, 3)
        h.setSpacing(2)
        self.quote_btn = QPushButton("引用")
        self.quote_btn.setObjectName("selQuote")
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setObjectName("selCopy")
        self.sep = QFrame()
        self.sep.setObjectName("selSep")
        self.sep.setFixedSize(1, 16)
        for b in (self.quote_btn, self.copy_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(28)
            b.setFocusPolicy(Qt.NoFocus)
        h.addWidget(self.quote_btn)
        h.addWidget(self.sep, 0, Qt.AlignVCenter)
        h.addWidget(self.copy_btn)
        self.quote_btn.clicked.connect(self._quote)
        self.copy_btn.clicked.connect(self._copy)
        self._text = ""
        self.hide()

    def apply_theme(self, theme):
        from .message_view import _icon
        self.setStyleSheet(
            f"QFrame#selectionBar {{ background:{theme('menu_bg')}; border:1px solid {theme('menu_border')};"
            " border-radius:9px; }"
            f"QFrame#selSep {{ background:{theme('menu_separator')}; border:none; }}"
            "QPushButton { border:none; border-radius:6px; padding:0 10px; font-size:12.5px;"
            f" color:{theme('menu_text')}; background:transparent; }}"
            f"QPushButton:hover {{ background:{theme('menu_sel_bg')}; color:{theme('menu_sel_text')}; }}")
        self.quote_btn.setIcon(QIcon(_icon(_svg24(_QUOTE_SVG), theme("mv_indigo"), 14)))
        self.copy_btn.setIcon(QIcon(_icon(_svg24(_COPY_SVG), theme("menu_text"), 14)))
        for b in (self.quote_btn, self.copy_btn):
            b.setIconSize(QSize(14, 14))

    def text(self) -> str:
        return self._text

    def show_for(self, text: str, global_pos: QPoint):
        """在 global_pos（松开鼠标的位置）上方浮出；放不下就放下方。"""
        self._text = _clean(text)
        if not self._text:
            self.hide()
            return
        parent = self.parentWidget()
        self.adjustSize()
        p = parent.mapFromGlobal(global_pos)
        x = min(max(4, p.x() - self.width() // 2), max(4, parent.width() - self.width() - 4))
        y = p.y() - self.height() - 14
        if y < 4:
            y = p.y() + 18
        self.move(x, y)
        self.show()
        self.raise_()

    def dismiss(self):
        if self.isVisible():
            self.hide()

    def _quote(self):
        text = self._text
        self.hide()
        if text:
            self.quote_requested.emit(text)

    def _copy(self):
        QApplication.clipboard().setText(self._text)
        self.hide()
