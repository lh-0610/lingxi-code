"""主题弹窗：跟随主题明暗的窗内卡片，取代原生 QMessageBox / QInputDialog。

设计稿是画布「灵犀弹窗设计」：无边框窗，背后一层变暗的遮罩，中间一张宽 440、圆角 14 的卡片。
颜色全部来自 theme 的 ``dlg_*`` token（打开弹窗那一刻按当前主题取），图标底色表示类型：
普通靛蓝、完成绿、警告琥珀、错误 / 危险红。路径和报错放在灰色等宽框里，带「复制」。

六种用法，对外 API 尽量对齐 QMessageBox / QInputDialog，调用处可以机械替换：

- ``question``：确认（返回 ``Yes`` / ``No`` 等标准按钮值；``danger=True`` 变危险样式）
- ``success``：完成提示，可附动作按钮
- ``critical``：报错，``detail`` 放进可复制的等宽框
- ``information`` / ``warning``：普通提示
- ``choose``：多个出口（返回所选动作的 key，Esc / 关闭返回 None）
- ``get_text``：输入（返回 ``(文本, 是否确定)``，同 QInputDialog.getText）
- ``confirm_danger``：危险操作（返回 bool）

键盘：Esc 等于取消（关闭按钮同）；Enter 触发默认按钮——普通对话框默认是主按钮（调用方
给了 ``default`` 就按它），焦点停在别的按钮上时 Enter 按那个按钮；**危险对话框默认聚焦
「取消」，Enter 永远等于取消**，危险按钮只能用鼠标点或 Tab 过去按空格。

所有弹窗都是模态的 ``exec()``，必须在主线程调用（跨线程照旧走 Signal，见 CLAUDE.md）。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import IntFlag

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from . import theme as _theme
from .fonts import MONO_CSS, UI_CSS, mono_font

CARD_WIDTH = 440
CARD_RADIUS = 14
CARD_PADDING = 24
_RING = 3            # 焦点环宽度：按钮四周各留 3px 画环，布局间距相应扣掉
_INDENT = 48         # 详情框与图标列对齐：图标 36 + 间距 12
_DETAIL_MAX_H = 180  # 详情框最高这么多，再长就滚动
_CLOSE_DX, _CLOSE_DY = 8, 6   # 关闭按钮往右、往上探出卡片内边距的量


class StandardButton(IntFlag):
    """与 QMessageBox.StandardButton 数值一致，方便对照迁移。"""
    NoButton = 0x0
    Ok = 0x400
    Yes = 0x4000
    No = 0x10000
    Close = 0x200000
    Cancel = 0x400000


NoButton = StandardButton.NoButton
Ok = StandardButton.Ok
Yes = StandardButton.Yes
No = StandardButton.No
Close = StandardButton.Close
Cancel = StandardButton.Cancel

# 按钮角色：primary 实心靛蓝 / danger 实心红 / secondary 描边 / text 只有字（多出口里的「关闭」）
PRIMARY, SECONDARY, DANGER, TEXT = "primary", "secondary", "danger", "text"
# 图标底色（类型）
TONES = ("info", "success", "warning", "error")

_STD_LABELS = {Ok: "确定", Yes: "确定", No: "取消", Cancel: "取消", Close: "关闭"}
_STD_ORDER = (No, Cancel, Close, Ok, Yes)          # 次按钮在左、主按钮在右
_ACCEPT_STD = (Yes, Ok)
_ESCAPE_STD = (No, Cancel, Close, Ok, Yes)         # Esc 落在哪个按钮上：取消类优先

# 图标（Lucide 风格描边，24 视框；和设计稿同一组路径）
_GLYPHS = {
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5h.01"/>',
    "question": ('<circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4'
                 ' 2.3-2.4 3.6"/><path d="M12 17h.01"/>'),
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "warning": ('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0'
                ' 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>',
    "trash": '<path d="M4 7h16"/><path d="M9 7V4.5h6V7"/><path d="M6.5 7l1 13h9l1-13"/>',
    "rewind": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
    "folder": ('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
}
_DEFAULT_GLYPH = {"info": "info", "success": "check", "warning": "warning", "error": "error"}
_CLOSE_GLYPH = '<path d="M4 4l8 8M12 4l-8 8"/>'

_open_dialogs: list = []


# ── 小工具 ───────────────────────────────────────────────────────────

def tok(key: str) -> str:
    """当前主题下弹窗用的一个颜色（打开弹窗时取，切主题后新开的弹窗跟着变）。"""
    return _theme.color(key)


_RGBA_RE = re.compile(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)")


def qcolor(value: str) -> QColor:
    """'#rrggbb' 或 'rgba(r, g, b, a)'（a 取 0~1）→ QColor。QColor 本身不认 css 的 rgba()。"""
    m = _RGBA_RE.fullmatch(value.strip())
    if m:
        r, g, b = (int(float(x)) for x in m.group(1, 2, 3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return QColor(r, g, b, round(a * 255))
    return QColor(value)


def _svg_pixmap(body: str, color: str, size: int, *, view: int = 24, stroke: float = 2.0,
                dpr: float = 1.0) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
           f'viewBox="0 0 {view} {view}" fill="none" stroke="{color}" stroke-width="{stroke}" '
           f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(svg.encode("utf-8"))
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _anchor_window(parent):
    """弹窗盖住的那个窗口：parent 所在的顶层窗口，且它正显示着（最小化 / 藏进托盘就不盖）。"""
    if parent is None:
        return None
    try:
        win = parent.window()
    except RuntimeError:
        return None
    if win is None or not win.isVisible() or win.isMinimized():
        return None
    return win


def _dpr(parent) -> float:
    try:
        if parent is not None:
            return float(parent.devicePixelRatioF())
    except RuntimeError:
        pass
    screen = QGuiApplication.primaryScreen()
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


_PATHISH = re.compile(r"([A-Za-z]:[\\/]|[\\/]{2}|~?/)\S")


def _bullet_html(item: str) -> str:
    """列表项：「说明：路径」里的路径部分套灰色等宽底（设计稿的内联 code 样式）。"""
    head, sep, tail = item.partition("：")
    if sep and _PATHISH.match(tail.strip()):
        code = (f"<span style=\"font-family:{MONO_CSS}; font-size:13px; color:{tok('dlg_code_text')};"
                f" background-color:{tok('dlg_code_bg')};\">&nbsp;{html.escape(tail.strip())}&nbsp;</span>")
        return html.escape(head + sep) + code
    return html.escape(item)


# ── 按钮 ───────────────────────────────────────────────────────────

class DialogButton(QPushButton):
    """自绘按钮：实心 / 描边 / 纯文字三种，有焦点时外圈画 3px 焦点环（设计稿的 box-shadow）。

    不用 QSS 画：主窗口的全局样式会层层继承下来，而焦点环要画在按钮外面，QSS 的盒模型做不到。
    autoDefault 关掉，Enter 统一交给 ThemedDialog 处理（危险对话框要把 Enter 钉在取消上）。"""

    def __init__(self, text, role=SECONDARY, key=None, parent=None):
        super().__init__(text, parent)
        self.role = role
        self.key = key
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setObjectName(f"lxDlgBtn_{role}")
        self.setAccessibleName(text)
        self._hover = False
        self.setAttribute(Qt.WA_Hover, True)

    def body_height(self):
        return 32 if self.role == TEXT else 36

    def sizeHint(self):
        fm = QFontMetrics(self.font())
        pad = 16 if self.role == TEXT else 18
        return QSize(fm.horizontalAdvance(self.text()) + pad * 2 + _RING * 2,
                     self.body_height() + _RING * 2)

    def minimumSizeHint(self):
        return self.sizeHint()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def colors(self) -> dict:
        """当前该用的三种颜色（填充 / 描边 / 文字），测试也用它核对明暗主题取色。"""
        hover = self._hover and self.isEnabled()
        if self.role == PRIMARY:
            bg = tok("dlg_primary_hover" if hover else "dlg_primary_bg")
            return {"bg": bg, "border": bg, "text": tok("dlg_primary_text")}
        if self.role == DANGER:
            bg = tok("dlg_danger_hover" if hover else "dlg_danger_bg")
            return {"bg": bg, "border": bg, "text": tok("dlg_danger_text")}
        if self.role == TEXT:
            return {"bg": tok("dlg_hover_bg") if hover else "transparent",
                    "border": "transparent", "text": tok("dlg_body")}
        return {"bg": tok("dlg_secondary_hover" if hover else "dlg_secondary_bg"),
                "border": tok("dlg_secondary_border"), "text": tok("dlg_secondary_text")}

    def paintEvent(self, event):
        c = self.colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)              # 不可用：整颗按钮变淡（如限制超字数时的「保存」）
        body = QRectF(self.rect()).adjusted(_RING, _RING, -_RING, -_RING)
        radius = 8 if self.role == TEXT else 10
        if self.hasFocus():
            ring = QPen(qcolor(tok("dlg_focus_ring")), _RING)
            p.setPen(ring)
            p.setBrush(Qt.NoBrush)
            r = body.adjusted(-_RING / 2, -_RING / 2, _RING / 2, _RING / 2)
            p.drawRoundedRect(r, radius + _RING / 2, radius + _RING / 2)
        path = QPainterPath()
        path.addRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        if c["bg"] != "transparent":
            p.fillPath(path, qcolor(c["bg"]))
        if c["border"] != "transparent":
            p.setPen(QPen(qcolor(c["border"]), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        text_color = qcolor(c["text"])
        p.setPen(text_color)
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        text = fm.elidedText(self.text(), Qt.ElideRight, int(body.width()) - 16)
        p.drawText(body, Qt.AlignCenter, text)
        p.end()


class _CopyButton(QPushButton):
    """详情框右上的「复制」：点了把原文放进剪贴板，字变「已复制」1.5 秒。"""

    def __init__(self, get_text, parent=None):
        super().__init__("复制", parent)
        self._get_text = get_text
        self.setObjectName("lxDlgCopy")
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoDefault(False)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(28)
        self.clicked.connect(self._copy)

    def _copy(self):
        QApplication.clipboard().setText(self._get_text())
        self.setText("已复制")
        QTimer.singleShot(1500, self, lambda: self.setText("复制"))


# ── 弹窗本体 ─────────────────────────────────────────────────────────

@dataclass
class Action:
    key: object
    label: str
    role: str = SECONDARY


class _OverlayMixin:
    """窗内覆盖层：有可见的父窗口时，弹窗是**父窗口里的子控件**，不是另一个顶层窗口。

    早先是盖在父窗口上的无边框顶层窗：应用级模态，Windows 下主窗口标题栏的最小化 / 最大化 /
    关闭都点不动（用户实测），窗口放大后遮罩也不跟着变。改成子控件后：
    - 标题栏按钮照常能点；主窗口缩放时遮罩和卡片跟着重排（父窗口装事件过滤器）
    - 仍然 ``exec()`` 同步返回（QDialog 自己的局部事件循环），调用处写法不变
    - 遮罩盖满客户区，鼠标点不到下面；Tab 焦点只在卡片里转；主窗口的快捷键在弹窗打开时不触发
      （ShortcutOverride 在这里接住，按键照常交给弹窗里的控件）；没处理的按键不往主窗口冒
    - 关掉后焦点还给打开前的那个控件
    没有可见父窗口（最小化、藏进托盘、没给 parent）时退回无边框顶层窗，屏幕居中、不画遮罩。
    """

    def _overlay_parent(self, parent):
        self._anchor = _anchor_window(parent)
        return self._anchor if self._anchor is not None else parent

    def _setup_overlay(self):
        self._inline = self._anchor is not None
        self._scrim = self._inline
        self._return_focus = None
        if self._inline:
            self.setWindowFlags(Qt.Widget)
            self.setFocusPolicy(Qt.StrongFocus)
            self._anchor.installEventFilter(self)
        else:
            self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
            self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)

    def _place_overlay(self):
        """内联时盖满父窗口；返回父窗口当前大小（卡片按它限宽高）。"""
        win = self._anchor
        self.setGeometry(win.rect())
        self.raise_()
        return win.size()

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._anchor and event.type() == QEvent.Resize and self.isVisible():
            self._place()
        return super().eventFilter(obj, event)

    def event(self, ev):
        from PySide6.QtCore import QEvent
        if getattr(self, "_inline", False) and ev.type() == QEvent.ShortcutOverride:
            ev.accept()               # 主窗口的快捷键别越过弹窗触发；按键照常送给焦点控件
            return True
        return super().event(ev)

    def _overlay_shown(self):
        if self not in _open_dialogs:
            _open_dialogs.append(self)
        if self._inline:
            fw = QApplication.focusWidget()
            if fw is not None and not self.isAncestorOf(fw):
                self._return_focus = fw

    def _overlay_done(self):
        if self in _open_dialogs:
            _open_dialogs.remove(self)
        if self._inline:
            try:
                self._anchor.removeEventFilter(self)
            except RuntimeError:
                pass
            fw = self._return_focus
            self._return_focus = None
            if fw is not None:
                import shiboken6
                if shiboken6.isValid(fw) and fw.isVisible():
                    fw.setFocus(Qt.OtherFocusReason)

    def focusNextPrevChild(self, nxt):
        if not getattr(self, "_inline", False):
            return super().focusNextPrevChild(nxt)
        # Tab 只在弹窗里转：沿焦点链找下一个属于本弹窗、能接 Tab 焦点的控件
        cur = self.focusWidget() or self
        w = cur
        for _ in range(1000):
            w = w.nextInFocusChain() if nxt else w.previousInFocusChain()
            if w is cur:
                break
            if (self.isAncestorOf(w) and w.isVisibleTo(self) and w.isEnabled()
                    and (w.focusPolicy() & Qt.TabFocus) == Qt.TabFocus):
                w.setFocus(Qt.TabFocusReason if nxt else Qt.BacktabFocusReason)
                return True
        return True

    def _focus_first_child(self):
        fw = QApplication.focusWidget()
        if fw is not None and self.isAncestorOf(fw):
            return
        self.setFocus(Qt.OtherFocusReason)
        self.focusNextPrevChild(True)

    def mousePressEvent(self, event):
        event.accept()                # 点在遮罩上：吞掉，不传给下面的主窗口

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        if not self._scrim:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(tok("dlg_scrim")))
        p.end()


class ThemedDialog(_OverlayMixin, QDialog):
    """一张卡片 + 遮罩。所有便捷函数都落到这里；``exec()`` 后读 ``result_key``。

    参数里只有 title / actions 是必须的，其余按需出现：
    text（14px 正文）· informative（13px 补充）· bullets（列表，路径会套等宽底）·
    detail + detail_label（灰色等宽框，带复制）· footnote（框下面的小字）·
    banner（琥珀色提醒条，输入框上方）· input（输入框）。
    """

    def __init__(self, parent=None, *, title: str, actions: list, text: str = "",
                 informative: str = "", bullets=None, detail: str = "", detail_label: str = "",
                 footnote: str = "", banner: str = "", tone: str = "info", glyph: str | None = None,
                 default=None, escape=None, danger: bool = False, stacked: bool = False,
                 rich_text: bool = False, input_label: str | None = None, input_text: str = "",
                 input_placeholder: str = "", multiline: bool = False, show_icon: bool = True,
                 copyable: bool = True, input_rows: int = 4):
        super().__init__(self._overlay_parent(parent))
        self.setObjectName("lxDialog")
        self.setWindowTitle(title)
        self._setup_overlay()
        self.danger = danger
        self.tone = tone if tone in TONES else "info"
        self.result_key = escape
        self._escape_key = escape
        self._actions = [a if isinstance(a, Action) else Action(*a) for a in actions]
        self._detail_text = detail
        self._multiline = multiline
        self._input_rows = max(2, int(input_rows))
        self.buttons: dict = {}
        self.input = None
        self.copy_button = None

        self._build(title=title, text=text, informative=informative, bullets=bullets or [],
                    detail_label=detail_label, footnote=footnote, banner=banner,
                    glyph=glyph or _DEFAULT_GLYPH[self.tone], stacked=stacked, rich_text=rich_text,
                    input_label=input_label, input_text=input_text,
                    input_placeholder=input_placeholder, show_icon=show_icon,
                    copyable=copyable, dpr=_dpr(parent))

        # 默认按钮：危险对话框永远是取消（Esc 那个）；其余按调用方给的，没给就是主按钮
        keys = [a.key for a in self._actions]
        if danger and escape in keys:
            default = escape
        elif default not in keys:
            primary = [a.key for a in self._actions if a.role == PRIMARY]
            default = (primary[0] if primary
                       else escape if escape in keys else keys[-1] if keys else None)
        self.default_key = default
        if default in self.buttons:
            # 标成 default：findChildren 一眼能看出默认是谁；Enter 仍由 keyPressEvent 统一处理
            self.buttons[default].setDefault(True)
        self._place()

    # ── 构造 ──
    def _build(self, *, title, text, informative, bullets, detail_label, footnote, banner, glyph,
               stacked, rich_text, input_label, input_text, input_placeholder, show_icon,
               copyable, dpr):
        self.setStyleSheet(self._qss())
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("lxDlgCard")
        self.card.setFixedWidth(CARD_WIDTH)
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 18)
        shadow.setColor(qcolor(tok("dlg_shadow")))
        self.card.setGraphicsEffect(shadow)

        lay = QVBoxLayout(self.card)
        # 设计稿：关闭按钮往右上探出 (-6, -8)，所以卡片右 / 上边距按它算，其余行再补回来；
        # 按钮四周为焦点环各留了 3px，下边距和按钮行也相应扣掉，视觉上仍是 24 的内边距
        lay.setContentsMargins(CARD_PADDING, CARD_PADDING - _CLOSE_DY, CARD_PADDING - _CLOSE_DX,
                               CARD_PADDING - _RING)
        lay.setSpacing(0)

        def add_row(widget_or_layout, gap, inset=_CLOSE_DX):
            if lay.count():
                lay.addSpacing(gap)
            holder = QWidget(self.card)
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, inset, 0)
            hl.setSpacing(0)
            if isinstance(widget_or_layout, QWidget):
                hl.addWidget(widget_or_layout)
            else:
                hl.addLayout(widget_or_layout)
            lay.addWidget(holder)

        # 标题行：图标块 + （标题 / 正文 / 补充 / 列表）+ 关闭按钮
        head = QHBoxLayout()
        head.setSpacing(12)
        head.setContentsMargins(0, 0, 0, 0)
        if show_icon:
            tile = QFrame(self.card)
            tile.setObjectName("lxDlgIcon")
            tile.setFixedSize(36, 36)
            tl = QVBoxLayout(tile)
            tl.setContentsMargins(0, 0, 0, 0)
            icon = QLabel(tile)
            icon.setObjectName("lxDlgIconGlyph")
            icon.setAlignment(Qt.AlignCenter)
            stroke = 2.2 if glyph == "check" else 2.0
            icon.setPixmap(_svg_pixmap(_GLYPHS.get(glyph, _GLYPHS["info"]),
                                       tok(f"dlg_{self._tone_key()}_fg"), 18, stroke=stroke, dpr=dpr))
            tl.addWidget(icon)
            tile_box = QVBoxLayout()
            tile_box.setContentsMargins(0, _CLOSE_DY, 0, 0)
            tile_box.addWidget(tile)
            tile_box.addStretch(1)
            head.addLayout(tile_box)
        col = QVBoxLayout()
        col.setContentsMargins(0, _CLOSE_DY + (2 if show_icon else 0), 0, 0)
        col.setSpacing(6)
        self.title_label = QLabel(title, self.card)
        self.title_label.setObjectName("lxDlgTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.PlainText)
        col.addWidget(self.title_label)
        self.text_label = None
        if text:
            self.text_label = self._label(text, "lxDlgText", rich_text)
            col.addWidget(self.text_label)
        if informative:
            col.addWidget(self._label(informative, "lxDlgInfo", rich_text))
        if bullets:
            # 不用 <ul>：Qt 富文本的列表缩进固定 40px，比设计稿的 18px 宽太多，改用两列表格
            rows = "".join(
                f"<tr><td width='18' valign='top' style='padding-bottom:4px'>•</td>"
                f"<td style='padding-bottom:4px'>{_bullet_html(b)}</td></tr>" for b in bullets)
            bl = QLabel(f"<table cellspacing='0' cellpadding='0'>{rows}</table>", self.card)
            bl.setObjectName("lxDlgBullets")
            bl.setTextFormat(Qt.RichText)
            bl.setWordWrap(True)
            bl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            col.addSpacing(2)
            col.addWidget(bl)
        head.addLayout(col, 1)
        self.close_button = QPushButton(self.card)
        self.close_button.setObjectName("lxDlgClose")
        self.close_button.setFixedSize(32, 32)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setAutoDefault(False)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setAccessibleName("关闭")
        self.close_button.setToolTip("关闭（Esc）")
        self.close_button.setIcon(_svg_pixmap(_CLOSE_GLYPH, tok("dlg_body"), 14, view=16,
                                              stroke=1.6, dpr=dpr))
        self.close_button.setIconSize(QSize(14, 14))
        self.close_button.clicked.connect(lambda: self.finish(self._escape_key))
        head.addWidget(self.close_button, 0, Qt.AlignTop)
        add_row(head, 0, inset=0)

        # 琥珀提醒条（输入框上方）
        if banner:
            bar = QFrame(self.card)
            bar.setObjectName("lxDlgBanner")
            bh = QHBoxLayout(bar)
            bh.setContentsMargins(12, 10, 12, 10)
            bh.setSpacing(8)
            bi = QLabel(bar)
            bi.setPixmap(_svg_pixmap(_GLYPHS["info"], tok("dlg_warn_fg"), 16, dpr=dpr))
            bh.addWidget(bi, 0, Qt.AlignTop)
            bt = self._label(banner, "lxDlgBannerText", False)
            bh.addWidget(bt, 1)
            add_row(bar, 14)

        # 详情框（路径 / 报错），左边和正文对齐
        if self._detail_text:
            box = QVBoxLayout()
            box.setContentsMargins(_INDENT if show_icon else 0, 0, 0, 0)
            box.setSpacing(8)
            if detail_label or copyable:
                top = QHBoxLayout()
                top.setContentsMargins(0, 0, 0, 0)
                cap = QLabel(detail_label or "详情", self.card)
                cap.setObjectName("lxDlgCaption")
                top.addWidget(cap, 1)
                if copyable:
                    self.copy_button = _CopyButton(lambda: self._detail_text, self.card)
                    top.addWidget(self.copy_button, 0)
                box.addLayout(top)
            self.detail_box = self._detail_widget(self._detail_text,
                                                  CARD_WIDTH - 2 * CARD_PADDING
                                                  - (_INDENT if show_icon else 0))
            box.addWidget(self.detail_box)
            if footnote:
                fn = self._label(footnote, "lxDlgInfo", False)
                box.addSpacing(2)
                box.addWidget(fn)
            add_row(box, 16)
        elif footnote:
            fn = self._label(footnote, "lxDlgInfo", False)
            fn.setContentsMargins(_INDENT if show_icon else 0, 0, 0, 0)
            add_row(fn, 16)

        # 输入框
        if input_label is not None:
            box = QVBoxLayout()
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            if input_label:
                cap = QLabel(input_label, self.card)
                cap.setObjectName("lxDlgCaption")
                box.addWidget(cap)
            if self._multiline:
                self.input = QPlainTextEdit(self.card)
                self.input.setPlainText(input_text)
                self.input.setFixedHeight(self._input_rows * 22 + 22)
                self.input.setTabChangesFocus(True)
                self.input.installEventFilter(self)      # Ctrl+Enter 确定
            else:
                self.input = QLineEdit(self.card)
                self.input.setText(input_text)
                self.input.setFixedHeight(40)
                # 单行回车不在这里接：QLineEdit 会把回车再交给对话框，统一走 keyPressEvent
            self.input.setObjectName("lxDlgInput")
            if input_placeholder:
                self.input.setPlaceholderText(input_placeholder)
            if input_label:
                cap.setBuddy(self.input)
            box.addWidget(self.input)
            add_row(box, 14)

        # 按钮
        btn_gap = 16 - _RING
        if stacked:
            self._build_stacked_buttons(lay, btn_gap)
        else:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10 - 2 * _RING)
            row.addStretch(1)
            for a in self._actions:
                row.addWidget(self._make_button(a))
            lay.addSpacing(btn_gap + (4 if self._detail_text or footnote else 0))
            row.setContentsMargins(0, 0, _CLOSE_DX - _RING, 0)
            lay.addLayout(row)

        outer.addWidget(self.card, 0, Qt.AlignHCenter | Qt.AlignVCenter)

    def _build_stacked_buttons(self, lay, gap):
        """多出口（设计稿 4）：主按钮占满一行，次按钮并排平分，纯文字的「关闭」垫底。"""
        lay.addSpacing(gap)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8 - 2 * _RING)
        mids = []
        for a in self._actions:
            btn = self._make_button(a)
            if a.role in (PRIMARY, DANGER, TEXT):
                if mids:
                    col.addLayout(self._even_row(mids))
                    mids = []
                col.addWidget(btn)
            else:
                mids.append(btn)
        if mids:
            col.addLayout(self._even_row(mids))
        col.setContentsMargins(0, 0, _CLOSE_DX - _RING, 0)
        lay.addLayout(col)

    def _even_row(self, buttons):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8 - 2 * _RING)
        for b in buttons:
            row.addWidget(b, 1)
        return row

    def _make_button(self, action: Action) -> DialogButton:
        btn = DialogButton(action.label, action.role, action.key, self.card)
        btn.clicked.connect(lambda _=False, k=action.key: self.finish(k))
        self.buttons[action.key] = btn
        return btn

    def _label(self, text, name, rich):
        lb = QLabel(self.card)
        lb.setObjectName(name)
        lb.setWordWrap(True)
        lb.setTextFormat(Qt.RichText if rich else Qt.PlainText)
        lb.setText(text)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse | (Qt.LinksAccessibleByMouse if rich
                                                               else Qt.NoTextInteraction))
        if rich:
            lb.setOpenExternalLinks(True)
        return lb

    def _detail_widget(self, text, width):
        """灰色等宽框。用 QTextEdit 不用 QLabel：QLabel 只在空格处折行，长路径会撑破卡片。"""
        from PySide6.QtGui import QTextDocument, QTextOption
        box = QTextEdit(self.card)
        box.setObjectName("lxDlgDetail")
        box.setReadOnly(True)
        box.setPlainText(text)
        box.setFrameShape(QFrame.NoFrame)
        box.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        box.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        box.setFocusPolicy(Qt.ClickFocus)
        font = mono_font(self._detail_px())
        box.setFont(font)
        box.document().setDocumentMargin(0)
        # 高度另拿一份文档量：QTextEdit 自己的文档宽度跟着控件当前大小走，构造时还没排版
        probe = QTextDocument()
        probe.setDocumentMargin(0)
        probe.setDefaultFont(font)
        opt = probe.defaultTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        probe.setDefaultTextOption(opt)
        probe.setPlainText(text)
        probe.setTextWidth(width - 2 * 12 - 2)
        h = int(probe.size().height() + 0.999) + 2 * 9 + 2
        box.setFixedHeight(min(max(h, 38), _DETAIL_MAX_H))
        if h <= _DETAIL_MAX_H:
            box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return box

    def _detail_px(self):
        return 12 if self.tone == "error" else 13

    def _tone_key(self):
        return {"info": "info", "success": "ok", "warning": "warn", "error": "err"}[self.tone]

    def _qss(self) -> str:
        t = tok
        tk = self._tone_key()
        return f"""
QDialog#lxDialog {{ background: transparent; }}
QFrame#lxDlgCard {{ background: {t('dlg_bg')}; border: 1px solid {t('dlg_border')};
    border-radius: {CARD_RADIUS}px; }}
QFrame#lxDlgCard QLabel {{ background: transparent; border: none; padding: 0; }}
QFrame#lxDlgCard > QWidget {{ background: transparent; }}
QFrame#lxDlgIcon {{ background: {t(f'dlg_{tk}_bg')}; border: 1px solid {t(f'dlg_{tk}_border')};
    border-radius: 10px; }}
QLabel#lxDlgTitle {{ font-family: {UI_CSS}; color: {t('dlg_title')}; font-size: 16px; font-weight: 600; }}
QLabel#lxDlgText {{ font-family: {UI_CSS}; color: {t('dlg_body')}; font-size: 14px; }}
QLabel#lxDlgInfo {{ font-family: {UI_CSS}; color: {t('dlg_body')}; font-size: 13px; }}
QLabel#lxDlgBullets {{ font-family: {UI_CSS}; color: {t('dlg_title')}; font-size: 14px; }}
QLabel#lxDlgCaption {{ font-family: {UI_CSS}; color: {t('dlg_body')}; font-size: 12px; }}
QFrame#lxDlgBanner {{ background: {t('dlg_warn_bg')}; border: 1px solid {t('dlg_warn_border')};
    border-radius: 10px; }}
QLabel#lxDlgBannerText {{ font-family: {UI_CSS}; color: {t('dlg_warn_text')}; font-size: 13px; }}
QTextEdit#lxDlgDetail {{ background: {t('dlg_code_bg')}; border: 1px solid {t('dlg_code_border')};
    border-radius: 8px; padding: 9px 12px; color: {t('dlg_code_text')};
    font-family: {MONO_CSS}; font-size: {self._detail_px()}px;
    selection-background-color: {t('dlg_focus_ring')}; selection-color: {t('dlg_title')}; }}
QPushButton#lxDlgCopy {{ background: transparent; border: none; border-radius: 8px;
    padding: 0 10px; color: {t('dlg_link')}; font-family: {UI_CSS}; font-size: 12px; }}
QPushButton#lxDlgCopy:hover {{ background: {t('dlg_hover_bg')}; }}
QPushButton#lxDlgCopy:focus {{ border: 1px solid {t('dlg_focus_ring')}; }}
QPushButton#lxDlgClose {{ background: transparent; border: none; border-radius: 8px; }}
QPushButton#lxDlgClose:hover {{ background: {t('dlg_hover_bg')}; }}
QPushButton#lxDlgBtn_primary, QPushButton#lxDlgBtn_secondary,
QPushButton#lxDlgBtn_danger {{ font-family: {UI_CSS}; font-size: 14px; }}
QPushButton#lxDlgBtn_primary, QPushButton#lxDlgBtn_danger {{ font-weight: 600; }}
QPushButton#lxDlgBtn_text {{ font-family: {UI_CSS}; font-size: 13px; }}
QLineEdit#lxDlgInput, QPlainTextEdit#lxDlgInput {{ background: {t('dlg_input_bg')};
    border: 1px solid {t('dlg_input_border')}; border-radius: 10px; padding: 0 12px;
    color: {t('dlg_title')}; font-family: {UI_CSS}; font-size: 14px;
    selection-background-color: {t('dlg_focus_ring')}; selection-color: {t('dlg_title')}; }}
QPlainTextEdit#lxDlgInput {{ padding: 8px 10px; }}
QLineEdit#lxDlgInput:focus, QPlainTextEdit#lxDlgInput:focus {{
    border: 1px solid {t('dlg_input_focus')}; }}
"""

    # ── 位置 ──
    def _place(self):
        """有可见的父窗口：整个盖住它的客户区，卡片在中间偏上；没有就只是卡片大小、屏幕居中。"""
        if self._inline:
            size = self._place_overlay()
            lay = self.layout()
            # 中间偏上：上下留白 4:6
            card_h = self.card.heightForWidth(CARD_WIDTH)
            if card_h < 0:
                card_h = self.card.sizeHint().height()
            spare = max(0, size.height() - card_h)
            lay.setContentsMargins(0, int(spare * 0.4), 0, spare - int(spare * 0.4))
        else:
            margin = 48
            card_h = self.card.heightForWidth(CARD_WIDTH)
            if card_h < 0:
                card_h = self.card.sizeHint().height()
            self.layout().setContentsMargins(margin, margin // 2, margin, margin + 12)
            w, h = CARD_WIDTH + 2 * margin, card_h + margin // 2 + margin + 12
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen is not None else QRect(0, 0, w, h)
            self.setGeometry(QRect(area.center().x() - w // 2, area.center().y() - h // 2, w, h))

    # ── 行为 ──
    def finish(self, key):
        self.result_key = key
        if key is not None and key == self._escape_key:
            self.reject()
        else:
            self.accept()

    def _input_return(self):
        accept = [a.key for a in self._actions if a.role in (PRIMARY, DANGER)]
        if accept:
            self.finish(accept[0])

    def showEvent(self, event):
        self._place()
        super().showEvent(event)
        self._overlay_shown()
        if self.input is not None:
            self.input.setFocus(Qt.OtherFocusReason)
            if isinstance(self.input, QLineEdit):
                self.input.selectAll()
        elif self.default_key in self.buttons:
            self.buttons[self.default_key].setFocus(Qt.OtherFocusReason)

    def done(self, r):
        self._overlay_done()
        super().done(r)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if (obj is self.input and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier):
            self._input_return()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.finish(self._escape_key)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self._multiline and self.input is not None:
                if event.modifiers() & Qt.ControlModifier:
                    self._input_return()
                return              # 多行输入里的普通回车由输入框自己换行，到不了这里
            if self.danger:
                self.finish(self.default_key)
                return
            focused = self.focusWidget()
            if isinstance(focused, DialogButton) and focused.isEnabled():
                self.finish(focused.key)
                return
            if self.default_key is not None:
                self.finish(self.default_key)
            return
        super().keyPressEvent(event)
        event.accept()                    # 没处理的键也不往主窗口冒（内联时主窗口是父控件）

    def reject(self):
        # 系统关闭（Alt+F4 等）也算取消
        self.result_key = self._escape_key
        super().reject()


# ── 窗内卡片外框（设置、编辑器这类表单对话框）──────────────────────────

class CardDialog(_OverlayMixin, QDialog):
    """自带窗框的 QDialog 换成的窗内卡片：遮罩 + 圆角 16 的卡片，顶部标题栏（标题 + 关闭），
    中间 ``self.body`` 由子类自己排，底部可选的操作栏（左边一行说明，右边按钮）。

    设计稿是画布里的「设置对话框 · 应用内外框」板。和 ThemedDialog 一样是父窗口里的覆盖层
    （见 _OverlayMixin）；卡片按请求的宽高放在中间，窗口太小就缩到窗口里留 32 的边。
    键盘沿用 QDialog：Esc 取消，Enter 触发 ``default=True`` 的那个按钮。

    子类的内容样式设到 ``self.body`` 上（别设到对话框本身：那会盖掉遮罩和卡片的样式）。
    """

    MARGIN = 32

    def __init__(self, parent=None, *, title: str, width: int = 520, height: int | None = None,
                 footer: bool = True, hint: str = ""):
        super().__init__(self._overlay_parent(parent))
        self.setObjectName("lxSheet")
        self.setWindowTitle(title)
        self._setup_overlay()
        self._req_size = (width, height)
        dpr = _dpr(parent)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self)
        self.card.setObjectName("lxSheetCard")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(64)
        shadow.setOffset(0, 24)
        shadow.setColor(qcolor(tok("dlg_shadow")))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card, 0, Qt.AlignCenter)

        col = QVBoxLayout(self.card)
        col.setContentsMargins(1, 1, 1, 1)          # 让出 1px 描边，内容不压在圆角边线上
        col.setSpacing(0)

        header = QWidget(self.card)
        header.setObjectName("lxSheetHeader")
        header.setFixedHeight(56)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(24, 0, 12, 0)
        self.title_label = QLabel(title, header)
        self.title_label.setObjectName("lxSheetTitle")
        hl.addWidget(self.title_label, 1)
        self.close_button = QPushButton(header)
        self.close_button.setObjectName("lxSheetClose")
        self.close_button.setFixedSize(36, 36)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setAutoDefault(False)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setAccessibleName("关闭")
        self.close_button.setToolTip("关闭（Esc）")
        self.close_button.setIcon(_svg_pixmap(_CLOSE_GLYPH, tok("dlg_body"), 14, view=16,
                                              stroke=1.6, dpr=dpr))
        self.close_button.setIconSize(QSize(14, 14))
        self.close_button.clicked.connect(self.reject)
        hl.addWidget(self.close_button)
        col.addWidget(header)

        self.body = QWidget(self.card)
        self.body.setObjectName("lxSheetBody")
        col.addWidget(self.body, 1)

        self.footer = None
        self.footer_layout = None
        self.hint_label = None
        if footer:
            self.footer = QWidget(self.card)
            self.footer.setObjectName("lxSheetFooter")
            self.footer.setFixedHeight(64)
            self.footer_layout = QHBoxLayout(self.footer)
            self.footer_layout.setContentsMargins(24, 0, 24 - _RING, 0)
            self.footer_layout.setSpacing(10 - 2 * _RING)
            self.hint_label = QLabel(hint, self.footer)
            self.hint_label.setObjectName("lxSheetHint")
            self.hint_label.setWordWrap(True)
            self.footer_layout.addWidget(self.hint_label, 1)
            col.addWidget(self.footer)
        self.setStyleSheet(self._frame_qss())

    def add_button(self, text, role=SECONDARY, slot=None, *, default=False) -> DialogButton:
        """往底部操作栏加一个按钮（从左往右排在说明文字后面）。"""
        btn = DialogButton(text, role, None, self.footer)
        if slot is not None:
            btn.clicked.connect(slot)
        if default:
            btn.setDefault(True)          # QDialog 的 Enter 认它
        self.footer_layout.addWidget(btn)
        return btn

    def set_hint(self, text: str):
        if self.hint_label is not None:
            self.hint_label.setText(text)

    def _frame_qss(self) -> str:
        t = tok
        return f"""
QDialog#lxSheet {{ background: transparent; }}
QFrame#lxSheetCard {{ background: {t('dlg_bg')}; border: 1px solid {t('dlg_border')};
    border-radius: 16px; }}
QWidget#lxSheetHeader {{ background: transparent; border: none;
    border-bottom: 1px solid {t('dlg_border')}; }}
QWidget#lxSheetFooter {{ background: transparent; border: none;
    border-top: 1px solid {t('dlg_border')}; }}
QWidget#lxSheetBody {{ background: transparent; }}
QLabel#lxSheetTitle {{ font-family: {UI_CSS}; font-size: 16px; font-weight: 600;
    color: {t('dlg_title')}; background: transparent; }}
QLabel#lxSheetHint {{ font-family: {UI_CSS}; font-size: 12px; color: {t('dlg_body')};
    background: transparent; }}
QPushButton#lxSheetClose {{ background: transparent; border: none; border-radius: 8px; }}
QPushButton#lxSheetClose:hover {{ background: {t('dlg_hover_bg')}; }}
QPushButton#lxDlgBtn_primary, QPushButton#lxDlgBtn_secondary,
QPushButton#lxDlgBtn_danger {{ font-family: {UI_CSS}; font-size: 14px; }}
QPushButton#lxDlgBtn_primary, QPushButton#lxDlgBtn_danger {{ font-weight: 600; }}
QPushButton#lxDlgBtn_text {{ font-family: {UI_CSS}; font-size: 13px; }}
"""

    def _place(self):
        w, h = self._req_size
        if h is None:
            self.card.setFixedWidth(w)
            h = self.card.sizeHint().height()
        if self._inline:
            size = self._place_overlay()
            w = min(w, max(320, size.width() - 2 * self.MARGIN))
            h = min(h, max(240, size.height() - 2 * self.MARGIN))
            self.card.setFixedSize(w, h)
            self.layout().setContentsMargins(0, 0, 0, 0)
        else:
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
            w = min(w, area.width() - 2 * self.MARGIN)
            h = min(h, area.height() - 2 * self.MARGIN)
            self.card.setFixedSize(w, h)
            m = 48
            self.layout().setContentsMargins(m, m // 2, m, m + 12)
            ww, wh = w + 2 * m, h + m // 2 + m + 12
            self.setGeometry(QRect(area.center().x() - ww // 2, area.center().y() - wh // 2, ww, wh))

    def showEvent(self, event):
        self._place()
        super().showEvent(event)
        self._overlay_shown()
        if self._inline:
            self._focus_first_child()

    def done(self, r):
        self._overlay_done()
        super().done(r)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)      # Esc 取消、Enter 触发默认按钮（QDialog 自带）
        event.accept()                    # 没处理的键也不往主窗口冒


class TextViewerDialog(CardDialog):
    """只读长文本（验证输出、完整日志）：等宽、可选中，底部「复制」+「关闭」。"""

    def __init__(self, parent, title: str, text: str):
        super().__init__(parent, title=title, width=760, height=520)
        lay = QVBoxLayout(self.body)
        lay.setContentsMargins(20, 16, 20, 16)
        self.text_box = QPlainTextEdit(self.body)
        self.text_box.setObjectName("lxSheetText")
        self.text_box.setReadOnly(True)
        self.text_box.setPlainText(text)
        self.text_box.setFont(mono_font(13))
        lay.addWidget(self.text_box)
        self.body.setStyleSheet(
            f"QPlainTextEdit#lxSheetText {{ background: {tok('dlg_code_bg')};"
            f" border: 1px solid {tok('dlg_code_border')}; border-radius: 10px; padding: 8px 10px;"
            f" color: {tok('dlg_title')}; font-family: {MONO_CSS}; font-size: 13px;"
            f" selection-background-color: {tok('dlg_focus_ring')}; selection-color: {tok('dlg_title')}; }}")
        self.copy_button = self.add_button("复制全部", SECONDARY, self._copy)
        self.add_button("关闭", PRIMARY, self.accept, default=True)

    def _copy(self):
        QApplication.clipboard().setText(self.text_box.toPlainText())
        self.copy_button.setText("已复制")
        QTimer.singleShot(1500, self, lambda: self.copy_button.setText("复制全部"))


def active_dialog():
    """最上面那个正开着的主题弹窗或卡片对话框（测试和截图脚本用）。"""
    return _open_dialogs[-1] if _open_dialogs else None


def _run(dlg: ThemedDialog):
    dlg.exec()
    key = dlg.result_key
    dlg.deleteLater()
    return key


# ── 对齐 QMessageBox 的便捷入口 ─────────────────────────────────────

def _std_actions(buttons, labels, danger, *, single_ok_label="确定"):
    acts = []
    std = [b for b in _STD_ORDER if buttons & b]
    for b in std:
        label = (labels or {}).get(b) or (single_ok_label if len(std) == 1 and b == Ok
                                          else _STD_LABELS[b])
        if b in _ACCEPT_STD:
            role = DANGER if danger else PRIMARY
        else:
            role = SECONDARY
        acts.append(Action(b, label, role))
    if len(std) == 1:
        acts[0].role = PRIMARY if not danger else acts[0].role
    return acts, std


def _escape_of(std):
    for b in _ESCAPE_STD:
        if b in std:
            return b
    return NoButton


def message(parent, title, text, *, tone="info", buttons=Ok, default=NoButton, labels=None,
            danger=False, single_ok_label="确定", **extra) -> StandardButton:
    """通用标准按钮弹窗；information / warning / critical / success / question 都落到这里。"""
    acts, std = _std_actions(buttons, labels, danger, single_ok_label=single_ok_label)
    esc = _escape_of(std)
    if len(std) == 1:
        esc = std[0]           # 只有一个按钮：Esc 也是它（「知道了」）
    dlg = ThemedDialog(parent, title=title, text=text, actions=acts, tone=tone,
                       default=default or None, escape=esc, danger=danger, **extra)
    key = _run(dlg)
    return StandardButton(key) if key is not None else esc


def information(parent, title, text, buttons=Ok, default=NoButton, **extra):
    return message(parent, title, text, tone="info", buttons=buttons, default=default, **extra)


def success(parent, title, text, buttons=Ok, default=NoButton, **extra):
    return message(parent, title, text, tone="success", buttons=buttons, default=default, **extra)


def warning(parent, title, text, buttons=Ok, default=NoButton, **extra):
    return message(parent, title, text, tone="warning", buttons=buttons, default=default, **extra)


def critical(parent, title, text, buttons=Ok, default=NoButton, *, detail="",
             detail_label="错误详情", **extra):
    """报错：``detail`` 放进可复制的等宽框（原始异常串、路径都放这里）。"""
    extra.setdefault("single_ok_label", "知道了")
    return message(parent, title, text, tone="error", buttons=buttons, default=default,
                   detail=detail, detail_label=detail_label if detail else "", **extra)


def question(parent, title, text, buttons=Yes | No, default=NoButton, *, labels=None,
             danger=False, tone=None, **extra):
    """确认。返回所点的标准按钮；Esc / 关闭返回 No（或 Cancel）。
    ``danger=True``：确认按钮变红、默认聚焦取消、Enter 等于取消。"""
    tone = tone or ("error" if danger else "info")
    extra.setdefault("glyph", "trash" if danger and tone == "error" else
                     ("question" if tone == "info" else None))
    return message(parent, title, text, tone=tone, buttons=buttons, default=default,
                   labels=labels, danger=danger, **extra)


def confirm_danger(parent, title, text, confirm_text, *, cancel_text="取消", tone="error",
                   glyph=None, **extra) -> bool:
    """危险操作确认（删除 / 跳过权限确认之类）：只有鼠标点了确认按钮才返回 True。"""
    r = question(parent, title, text, Yes | No, No, labels={Yes: confirm_text, No: cancel_text},
                 danger=True, tone=tone, glyph=glyph, **extra)
    return r == Yes


def choose(parent, title, text="", actions=(), *, tone="warning", glyph=None, stacked=True,
           **extra):
    """多个出口。actions 是 ``(key, 文案, 角色)`` 列表，角色取 PRIMARY / SECONDARY / DANGER / TEXT；
    返回所点按钮的 key，Esc / 关闭返回 None。"""
    acts = [a if isinstance(a, Action) else Action(*a) for a in actions]
    esc_keys = [a.key for a in acts if a.role == TEXT]
    escape = esc_keys[-1] if esc_keys else None
    dlg = ThemedDialog(parent, title=title, text=text, actions=acts, tone=tone, glyph=glyph,
                       escape=escape, stacked=stacked, **extra)
    key = _run(dlg)
    return None if key == escape else key


def get_text(parent, title, label, text="", *, placeholder="", ok_text="确定",
             cancel_text="取消", multiline=False, banner="", rows=4) -> tuple[str, bool]:
    """输入框，用法同 ``QInputDialog.getText``：返回 ``(文本, 是否确定)``。
    单行时 Enter 确定；多行时 Ctrl+Enter 确定。Esc 取消。"""
    acts = [Action(Cancel, cancel_text, SECONDARY), Action(Ok, ok_text, PRIMARY)]
    dlg = ThemedDialog(parent, title=title, actions=acts, tone="info", show_icon=False,
                       escape=Cancel, default=Ok, input_label=label, input_text=text,
                       input_placeholder=placeholder, multiline=multiline, banner=banner,
                       input_rows=rows)
    dlg.exec()
    if isinstance(dlg.input, QPlainTextEdit):
        value = dlg.input.toPlainText()
    else:
        value = dlg.input.text()
    ok = dlg.result_key == Ok
    dlg.deleteLater()
    return value, ok
