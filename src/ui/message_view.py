"""消息流：真 Qt 控件拼成的对话时间线（设计稿 A5「一线加任务面板」）。

每一轮（用户的一句话，加上这一轮里 AI 的回复、思考、工具调用、计划步骤和结果卡）
装在一个 _Round 里。_Round 左边留出 38px 的线槽，一条主题色竖线把这一轮串起来，
线上的节点标出：用户消息（空心圆）、思考与程序提示（小圆点）、工具调用（带点的圆环）、
计划步骤（带编号的圆）和结果卡（实心圆加勾 / 叉 / 感叹号）。

节点由各块登记，绘制时按块的当前位置现算：展开 / 收起、流式追加都不用另外维护坐标。
开新一轮时，上一轮的思考、工具和步骤收成一行"调用了 N 次工具　展开过程"，正文和结果卡保留。

配色全部取当前主题的 mv_* token（_P）；已经画出来的块不会自己变色，切主题后由 ChatUI 重绘。
"""
import math
import re
import time

from PySide6.QtCore import QByteArray, QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .helpers import strip_leading_emoji
from .fonts import MONO_CSS, UI_CSS, mono_font
from .theme import color as theme_color

try:
    from PySide6.QtSvg import QSvgRenderer
except Exception:  # QtSvg 理论上随 PySide6 自带；缺了降级成无图标
    QSvgRenderer = None


class _ThemePalette:
    """消息流配色：_P["indigo"] 取的是当前主题的 mv_indigo（定义在 theme.THEMES）。

    每次取都现查当前主题，所以控件要在构造 / 绘制时取色；切主题后由 ChatUI 重绘消息流。
    """
    def __getitem__(self, key):
        return theme_color("mv_" + key)


_P = _ThemePalette()

_SANS = UI_CSS
_MONO = MONO_CSS

GUTTER = 38          # 每轮左侧线槽的宽度
_CX = 11             # 竖线和节点的中心 x（节点最宽 22px，留足不被裁）
COLUMN_MAX = 820     # 消息栏最大宽度（含线槽）；窗口再宽，两侧留白


def _icon(svg: str, color: str, size: int = 16) -> QPixmap:
    """把内联 SVG（用 currentColor 占位描边）渲染成单色 QPixmap。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if QSvgRenderer is None:
        return pm
    data = svg.replace("currentColor", color)
    r = QSvgRenderer(QByteArray(data.encode("utf-8")))
    p = QPainter(pm)
    r.render(p)
    p.end()
    return pm


def _icon_label(svg: str, color: str, size: int = 16) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setPixmap(_icon(svg, color, size))
    lbl.setStyleSheet("background:transparent;")
    return lbl


def _svg(body: str) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
            'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


# 内联 SVG（currentColor 占位）
SVG_CLIPBOARD = _svg('<rect x="3" y="2.5" width="10" height="11.5" rx="1.5"/><path d="M5.5 6h5M5.5 8.5h5M5.5 11h3"/>')
SVG_STAR = _svg('<path d="M8 1.8l1.7 3.9 4.2.4-3.2 2.8 1 4.1L8 10.8l-3.7 2.2 1-4.1-3.2-2.8 4.2-.4z"/>')
SVG_COPY = _svg('<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3 10.5V3.5A1 1 0 014 2.5h6.5"/>')
SVG_REFRESH = _svg('<path d="M13.5 3v3.2h-3.2"/><path d="M13.3 6.2A5.5 5.5 0 102.6 9.5"/>')
SVG_TERMINAL = _svg('<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.5"/><path d="M4.5 6l2 2-2 2M8.5 10h3"/>')
SVG_FILE = _svg('<path d="M9 1.8H4.5A1.5 1.5 0 003 3.3v9.4a1.5 1.5 0 001.5 1.5h7a1.5 1.5 0 001.5-1.5V5.8z"/><path d="M9 1.8v4h4"/>')
SVG_PEN = _svg('<path d="M10.5 2.5l3 3L6 13H3v-3z"/>')
SVG_SEARCH = _svg('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5l3.5 3.5"/>')
SVG_TOOL = _svg('<path d="M10 2.2a3.3 3.3 0 00-3.9 4.3L2.3 10.3a1.4 1.4 0 002 2l3.8-3.8A3.3 3.3 0 0012.4 4.6l-2 2-1.7-.4-.4-1.7z"/>')
SVG_CHEVRON_RIGHT = _svg('<path d="M6 4l4 4-4 4"/>')
SVG_CHEVRON_DOWN = _svg('<path d="M4 6l4 4 4-4"/>')

# 工具名（显示名）里出现这些词，就用对应图标
_TOOL_ICONS = (
    (("执行命令", "测试", "代码检查", "后台"), SVG_TERMINAL),
    (("写入", "编辑", "追加", "补丁"), SVG_PEN),
    (("搜索", "查找", "跳转", "检索"), SVG_SEARCH),
    (("读取", "目录", "文件", "地图", "改动", "历史", "状态"), SVG_FILE),
    (("计划", "步骤"), SVG_CLIPBOARD),
)


def _tool_icon(name: str) -> str:
    for words, svg in _TOOL_ICONS:
        if any(w in name for w in words):
            return svg
    return SVG_TOOL


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_secs(secs) -> str:
    s = max(0, int(secs))
    if s < 60:
        return f"{s} 秒"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} 分 {s:02d} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m:02d} 分"


def _one_line(detail: str) -> str:
    """工具参数压成一行摘要：路径 / 命令原样；多行 JSON 依次取各字段的值（多行的值只取第一行）。"""
    s = (detail or "").strip()
    if not s:
        return ""
    if not s.startswith("{"):
        return s.splitlines()[0]
    values = []
    for line in s.splitlines()[1:]:
        m = re.match(r'\s*"[^"]*"\s*:\s*(.*)', line)
        if not m:
            continue        # 多行字符串值的后续行 / 结构括号
        value = m.group(1).strip().rstrip(",").strip('"')
        if value and value not in ("{", "[", "true", "false", "null"):
            values.append(value)
    return "  ".join(values)


def highlight_json(text: str) -> str:
    """极简 JSON 着色 → rich HTML（key/字符串/数字/标点分色），用于工具卡展开后的参数。
    非严格解析,够用就行;失败退化为转义纯文本。"""
    out = []
    s = text or ""
    token = re.compile(r'"(?:[^"\\]|\\.)*"|-?\d+\.?\d*|[{}\[\],:]|\s+|[^\s{}\[\],:"]+')
    for m in token.finditer(s):
        t = m.group(0)
        if t.isspace():
            out.append(_esc(t))
        elif t.startswith('"'):
            # 字符串：后面紧跟冒号的算 key
            rest = s[m.end():].lstrip()
            color = _P["json_key"] if rest.startswith(":") else _P["json_str"]
            out.append(f'<span style="color:{color}">{_esc(t)}</span>')
        elif re.fullmatch(r'-?\d+\.?\d*', t):
            out.append(f'<span style="color:{_P["json_num"]}">{_esc(t)}</span>')
        elif t in "{}[],:":
            out.append(f'<span style="color:{_P["json_punct"]}">{_esc(t)}</span>')
        else:
            out.append(_esc(t))
    body = "".join(out)
    return (f'<pre style="margin:0; font-family:{_MONO}; font-size:12px; '
            f'line-height:1.7; white-space:pre-wrap; word-break:break-word;">{body}</pre>')


class _ElidedLabel(QLabel):
    """单行文字，放不下时末尾省略；完整内容放在 tooltip 里。"""
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(0)
        self.set_full(text)

    def set_full(self, text: str):
        self._full = " ".join((text or "").split())
        self.setToolTip(self._full if len(self._full) > 40 else "")
        self._apply()

    def full(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()

    def _apply(self):
        width = max(0, self.width() - 2)
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class _FlatButton(QPushButton):
    """消息流里的文字按钮（展开 / 收起 / 展开过程）：无边框，主题色字。"""
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QPushButton {{ border:none; background:transparent; color:{_P['indigo']}; "
            f"font-size:12px; padding:2px 6px; border-radius:6px; }}"
            f"QPushButton:hover {{ background:{_P['indigo_soft']}; }}")


# ── 一轮：线槽 + 节点 ─────────────────────────────────────────

_RESULT_TONE = {"ok": "indigo", "warn": "orange", "interrupted": "orange", "bad": "bad",
                "muted": "muted3", "running": "indigo"}


class _Round(QWidget):
    """一轮对话：左边线槽里画竖线和节点，右边纵向排这一轮的各块。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(GUTTER, 0, 0, 0)
        self._v.setSpacing(0)
        self._nodes = []              # [(widget, kind, info)]
        self.assistant_turns = []
        self.current_step = None      # (步号, 步骤名)：流里最近插过的计划步骤分段

    def add(self, w, top_gap=0):
        if top_gap and self._v.count():
            self._v.addSpacing(top_gap)
        self._v.addWidget(w)
        return w

    def add_node(self, widget, kind, **info):
        self._nodes.append((widget, kind, info))
        self.update()

    def collapse_process(self):
        for turn in self.assistant_turns:
            turn.collapse_process()

    def event(self, e):
        handled = super().event(e)
        if e.type() == QEvent.LayoutRequest:
            self.update()    # 子块高度变了（展开、流式追加）：节点位置跟着重画
        return handled

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()

    def paintEvent(self, e):
        pts = []
        for w, kind, info in self._nodes:
            try:
                if not w.isVisibleTo(self):
                    continue
                dy = info.get("dy", 10)
                y = w.mapTo(self, QPoint(0, 0)).y() + (dy(w) if callable(dy) else dy)
            except RuntimeError:   # 块已被删除
                continue
            pts.append((y, w, kind, info))
        if not pts:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        thread = QColor(_P["indigo"])
        if len(pts) > 1:
            ys = [pt[0] for pt in pts]
            p.setPen(QPen(thread, 2))
            p.drawLine(QPointF(_CX, min(ys)), QPointF(_CX, max(ys)))
        for y, w, kind, info in pts:
            _draw_node(p, kind, QPointF(_CX, y), w, info)
        p.end()


def _draw_node(p, kind, c, w, info):
    bg = QColor(_P["view_bg"])
    thread = QColor(_P["indigo"])
    if kind == "user":                       # 用户消息：空心圆
        p.setPen(QPen(thread, 2))
        p.setBrush(bg)
        p.drawEllipse(c, 6, 6)
    elif kind == "dot":                      # 思考 / 提示 / 收起的过程：小圆点
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_P[info.get("color", "muted2")]))
        p.drawEllipse(c, 3, 3)
    elif kind == "tool":                     # 工具调用：带点的圆环
        p.setPen(QPen(thread, 2))
        p.setBrush(bg)
        p.drawEllipse(c, 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(thread)
        p.drawEllipse(c, 2, 2)
    elif kind == "step":                     # 计划步骤：带编号的圆
        status = getattr(w, "status", "pending")
        r = 8
        if status == "done":
            p.setPen(Qt.NoPen)
            p.setBrush(thread)
            ink = bg
        elif status == "in_progress":
            p.setPen(QPen(thread, 2))
            p.setBrush(bg)
            ink = thread
        else:
            ink = QColor(_P["muted2"])
            p.setPen(QPen(ink, 1.5))
            p.setBrush(bg)
        p.drawEllipse(c, r, r)
        f = QFont(p.font())
        f.setPixelSize(10)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(ink)
        p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, str(getattr(w, "n", "")))
    elif kind == "result":                   # 结果卡：实心圆 + 勾 / 叉 / 感叹号
        tone = info.get("tone") or "muted"
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_P[_RESULT_TONE.get(tone, "muted3")]))
        p.drawEllipse(c, 10, 10)
        pen = QPen(bg, 2.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        x, y = c.x(), c.y()
        if tone in ("ok", "running"):
            p.drawPolyline([QPointF(x - 4.4, y + 0.2), QPointF(x - 1.2, y + 3.4), QPointF(x + 4.6, y - 3.4)])
        elif tone == "bad":
            p.drawLine(QPointF(x - 3.5, y - 3.5), QPointF(x + 3.5, y + 3.5))
            p.drawLine(QPointF(x + 3.5, y - 3.5), QPointF(x - 3.5, y + 3.5))
        else:
            p.drawLine(QPointF(x, y - 4.8), QPointF(x, y + 1.0))
            p.drawPoint(QPointF(x, y + 4.4))


# ── 块 ────────────────────────────────────────────────────────

class BodyText(QLabel):
    """正文段落。富文本，流式时是转义后的纯文本，结束后换成渲染好的 markdown。"""
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setStyleSheet(
            f"color:{_P['text2']}; font-family:{_SANS}; font-size:14px; background:transparent;"
        )
        self.setText(text)


class MetaLine(QWidget):
    """回复上方的一行小字：模型、模式、用时。进行中每秒走一次，这一轮结束后定格。"""
    def __init__(self, model_name: str = "", mode: str = "", live: bool = False, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(14)
        style = f"color:{_P['text_sec']}; font-size:12px; background:transparent;"
        for text in (model_name, mode):
            if text:
                lbl = QLabel(text)
                lbl.setStyleSheet(style)
                h.addWidget(lbl)
        self._t0 = time.monotonic()
        self._clock = None
        self._timer = None
        if live:
            self._clock = QLabel()
            self._clock.setStyleSheet(style)
            h.addWidget(self._clock)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(1000)
            self._tick()
        h.addStretch(1)

    @property
    def live(self) -> bool:
        return self._timer is not None

    def _tick(self):
        self._clock.setText(f"进行中 {_fmt_secs(time.monotonic() - self._t0)}")

    def finish(self):
        if self._timer is None:
            return
        self._timer.stop()
        self._timer = None
        self._clock.setText(f"用时 {_fmt_secs(time.monotonic() - self._t0)}")


class ThinkingChip(QWidget):
    """思考块：一个小按钮（思考中… / 思考了 N 秒），点开看思考内容。"""
    def __init__(self, char_count: int = 0, content: str = "", parent=None):
        super().__init__(parent)
        self._open = False
        self._live = False
        self._content = content
        self._char_count = char_count
        self._t0 = time.monotonic()
        self._secs = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 用 QPushButton 原生 icon+text（不要往按钮里塞 QLabel 布局——真机上子 label 常不绘制）；
        # 从右往左排，箭头就在文字右边
        self.btn = QPushButton()
        self.btn.setObjectName("thinkChip")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setLayoutDirection(Qt.RightToLeft)
        self.btn.setIconSize(QSize(12, 12))
        self.btn.setStyleSheet(
            f"#thinkChip {{ border:1px solid {_P['chip_border']}; background:{_P['header_bg']}; "
            f"border-radius:8px; padding:3px 10px; color:{_P['text_sec']}; font-size:12px; }} "
            f"#thinkChip:hover {{ border-color:{_P['chip_hover']}; }}"
        )
        chip_wrap = QHBoxLayout()
        chip_wrap.setContentsMargins(0, 0, 0, 0)
        chip_wrap.addWidget(self.btn, 0, Qt.AlignLeft)
        chip_wrap.addStretch(1)
        v.addLayout(chip_wrap)

        self.panel = QLabel()
        self.panel.setObjectName("thinkPanel")
        self.panel.setWordWrap(True)
        self.panel.setTextFormat(Qt.RichText)
        self.panel.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        self.panel.setStyleSheet(
            f"#thinkPanel {{ background:{_P['header_bg']}; border-left:2px solid {_P['panel_accent']}; "
            f"border-top-right-radius:8px; border-bottom-right-radius:8px; "
            f"color:{_P['muted']}; font-size:13px; padding:12px 14px; }}"
        )
        self.panel.setVisible(False)
        v.addWidget(self.panel)

        self.update_content(content)
        self.btn.clicked.connect(self._toggle)

    def _refresh_label(self):
        if self._live:
            text = "思考中…"
        elif self._secs is not None:
            text = f"思考了 {_fmt_secs(self._secs)}"
        else:
            n = self._char_count if self._content == "" else len(self._content)
            text = f"思考过程 {n} 字"
        self.btn.setText(text)
        chevron = SVG_CHEVRON_DOWN if self._open else SVG_CHEVRON_RIGHT
        self.btn.setIcon(QIcon(_icon(chevron, _P["text_sec"], 12)))

    def set_live(self, live: bool):
        if self._live and not live and self._secs is None:
            self._secs = max(1, round(time.monotonic() - self._t0))
        self._live = live
        self._refresh_label()

    def update_content(self, content: str):
        self._content = content
        self.panel.setText(_esc(content).replace("\n", "<br>"))
        self._refresh_label()

    def _toggle(self):
        _hold_scroll(self)
        self._open = not self._open
        self.panel.setVisible(self._open)
        self._refresh_label()


class ToolCallCard(QFrame):
    """工具调用卡：一行头部（图标、名称、参数摘要、输出行数、展开），收起时下面再露一行
    最新输出；展开看完整参数和输出。默认收起，消息流里只留"做了什么、结果怎样"。"""

    _OUT_MAX_LINES = 16      # 展开后输出框最多这么高，更多的在框里滚动
    _OUT_MAX_BLOCKS = 4000   # 输出框最多保留的行数（长命令的流式输出），更早的丢弃

    def __init__(self, name: str, code_text: str = "", icon_svg: str | None = None,
                 icon_color: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._open = False
        self._detail = ""
        self._args_needed = False
        self._out_lines = 0
        self._chunks = 0
        self._first_line = ""
        self._last_line = ""
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.header = QWidget()
        self.header.setObjectName("toolCardHeader")
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(12, 6, 8, 6)
        h.setSpacing(8)
        h.addWidget(_icon_label(icon_svg or _tool_icon(name), icon_color or _P["indigo"], 15),
                    0, Qt.AlignVCenter)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color:{_P['text3']}; font-size:13px; font-weight:600; background:transparent;")
        h.addWidget(name_lbl, 0, Qt.AlignVCenter)
        self._summary = _ElidedLabel()
        self._summary.setStyleSheet(
            f"color:{_P['text_sec']}; font-family:{_MONO}; font-size:12px; background:transparent;")
        h.addWidget(self._summary, 1, Qt.AlignVCenter)
        self._count = QLabel()
        self._count.setStyleSheet(f"color:{_P['muted']}; font-size:12px; background:transparent;")
        self._count.setVisible(False)
        h.addWidget(self._count, 0, Qt.AlignVCenter)
        self._toggle_btn = _FlatButton("展开")
        self._toggle_btn.clicked.connect(self.toggle)
        self._toggle_btn.setVisible(False)
        h.addWidget(self._toggle_btn, 0, Qt.AlignVCenter)
        v.addWidget(self.header)

        self._tail = _ElidedLabel()
        self._tail.setObjectName("toolTail")
        self._tail.setStyleSheet(
            f"#toolTail {{ color:{_P['muted']}; font-family:{_MONO}; font-size:12px; "
            f"background:transparent; padding:5px 14px 6px 14px; }}")
        self._tail.setVisible(False)
        v.addWidget(self._tail)

        self._body = QWidget()
        self._body.setStyleSheet("background:transparent;")
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(14, 10, 14, 12)
        bl.setSpacing(8)
        self._args = QLabel()
        self._args.setWordWrap(True)
        self._args.setTextFormat(Qt.RichText)
        self._args.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._args.setStyleSheet(f"color:{_P['text_sec']}; background:transparent;")
        self._args.setVisible(False)
        bl.addWidget(self._args)
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setFrameShape(QFrame.NoFrame)
        self._out.setFont(mono_font(12))
        self._out.setMaximumBlockCount(self._OUT_MAX_BLOCKS)
        self._out.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._out.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # 放得下时不出滚动条，见 _fit_output
        self._out.setStyleSheet(
            f"QPlainTextEdit {{ background:{_P['result_bg']}; color:{_P['text_sec']}; "
            f"border:1px solid {_P['result_border']}; border-radius:8px; padding:4px 6px; }}"
            # 超过 _OUT_MAX_LINES 才有滚动条：细条、没有上下箭头，和消息流的滚动条一个样子
            "QScrollBar:vertical { background:transparent; width:6px; margin:2px 0; }"
            f"QScrollBar::handle:vertical {{ background:{_P['chip_border']}; border-radius:3px; min-height:20px; }}"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }")
        self._out.setVisible(False)
        # 自动换行后的行数变了就重新定高：内容追加时文档会通知；展开后有了真实宽度、
        # 窗口变宽变窄时它不一定通知，所以输出框显示出来和宽度变化时也重算一次
        self._out.document().documentLayout().documentSizeChanged.connect(lambda _size: self._fit_output())
        self._out.installEventFilter(self)
        self._fitted_width = -1
        bl.addWidget(self._out)
        self._body.setVisible(False)
        v.addWidget(self._body)

        self.set_body(code_text)

    # 线上节点对齐头部中线
    def header_center(self, _w=None) -> int:
        return max(14, self.header.height() // 2)

    def set_body(self, code_text: str):
        """工具参数（tool_detail）到位：头部放一行摘要，多行参数留到展开后看。"""
        self._detail = (code_text or "").strip()
        self._summary.set_full(_one_line(self._detail))
        self._args_needed = "\n" in self._detail
        if self._args_needed:
            self._args.setText(highlight_json(self._detail))
        self._sync()

    def append_output(self, text: str):
        """工具输出（流式命令逐行来，其余工具一次来）：累进输出框，收起时露最后一行。"""
        chunk = strip_leading_emoji(text or "")
        if not chunk.strip():
            return
        cur = self._out.textCursor()
        cur.movePosition(QTextCursor.End)
        if self._out_lines:        # 已有输出：新的一段另起一行（不读整段文本，长输出逐行来也不变慢）
            cur.insertText("\n")
        cur.insertText(chunk.strip("\n"))
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        self._out_lines += len(lines)
        self._chunks += 1
        if lines:
            self._first_line = self._first_line or lines[0]
            self._last_line = lines[-1]
        # 一次给完的结果（读文件、改计划）第一行是结论；逐行流式来的命令输出看最后一行（退出码）
        self._tail.set_full(self._first_line if self._chunks == 1 else self._last_line)
        self._count.setText(f"输出 {self._out_lines} 行")
        self._fit_output()
        self._sync()

    def output_text(self) -> str:
        return self._out.toPlainText()

    def is_open(self) -> bool:
        return self._open

    def toggle(self):
        _hold_scroll(self)
        self._open = not self._open
        self._sync()

    def eventFilter(self, obj, event):
        if obj is self._out and (event.type() == QEvent.Show or (
                event.type() == QEvent.Resize and self._out.width() != self._fitted_width)):
            # 等这一轮布局排完（换行按新宽度算好）再量；收起时算的行数不作数，显示出来总要重量
            QTimer.singleShot(0, self, self._fit_output)
        return super().eventFilter(obj, event)

    def _fit_output(self):
        """输出框按【自动换行后】的行数定高，最多 _OUT_MAX_LINES 行；超过才出滚动条。

        QPlainTextEdit 的文档高度单位就是行（含换行折出来的行）。旧算法按段落数 × 行高 + 18：
        18 正好等于边框、内边距和文档边距之和，一点余量都没有，只有一行时也会溢出一两个像素，
        冒出一根矮到只剩两个箭头的滚动条；长行折行后更是直接被截。"""
        self._fitted_width = self._out.width()
        doc = self._out.document()
        lines = max(1, int(round(doc.size().height())))
        shown = min(lines, self._OUT_MAX_LINES)
        # 行高按小数算：高 DPI 下实际行高常是 14.3 这样的值，按整数乘十几行会差出几个像素，
        # 最后一行露不全
        line_h = QFontMetricsF(self._out.font()).lineSpacing()
        chrome = doc.documentMargin() * 2 + 8 + 2 + 4         # 文档边距 + 上下内边距 + 边框 + 余量
        self._out.setFixedHeight(math.ceil(shown * line_h + chrome))
        self._out.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if lines > self._OUT_MAX_LINES else Qt.ScrollBarAlwaysOff)
        if lines <= self._OUT_MAX_LINES:
            QTimer.singleShot(0, self, self._grow_if_cut)

    def _grow_if_cut(self):
        """核对一次：本该全部露出来却还有行在框外（滚动范围不为 0），就把差的几行补上。"""
        hidden = self._out.verticalScrollBar().maximum()
        if hidden > 0 and self._out.isVisible():
            line_h = QFontMetricsF(self._out.font()).lineSpacing()
            self._out.setFixedHeight(self._out.height() + math.ceil(hidden * line_h))

    def _sync(self):
        has_out = self._out_lines > 0
        expandable = has_out or self._args_needed
        self._toggle_btn.setVisible(expandable)
        self._toggle_btn.setText("收起" if self._open else "展开")
        self._count.setVisible(has_out)
        self._tail.setVisible(has_out and not self._open)
        self._body.setVisible(self._open and expandable)
        self._args.setVisible(self._open and self._args_needed)
        self._out.setVisible(self._open and has_out)
        below = self._tail.isVisibleTo(self) or self._body.isVisibleTo(self)
        self._restyle(below)

    def _restyle(self, below: bool):
        if below == getattr(self, "_styled_below", None):
            return             # 流式输出每来一行都会走到这里：样式没变就不重设
        self._styled_below = below
        self.setStyleSheet(
            f"#toolCard {{ background:{_P['card_bg']}; border:1px solid {_P['card_border']}; "
            f"border-radius:10px; }}")
        bottom = "0px" if below else "9px"
        rule = f"border-bottom:1px solid {_P['card_border']};" if below else "border:none;"
        self.header.setStyleSheet(
            f"#toolCardHeader {{ background:{_P['header_bg']}; {rule} "
            f"border-top-left-radius:9px; border-top-right-radius:9px; "
            f"border-bottom-left-radius:{bottom}; border-bottom-right-radius:{bottom}; }}")


class Notice(QLabel):
    """程序提示（自动重试、拒绝调用、恢复说明……）：不是工具输出，也不是模型说的话。"""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("notice")
        self.setWordWrap(True)
        self.setTextFormat(Qt.PlainText)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#notice {{ color:{_P['text_sec']}; font-size:12px; background:{_P['header_bg']}; "
            f"border-radius:8px; padding:6px 10px; }}")
        self.setText(text)


def _notice_tone(text: str) -> str:
    s = (text or "").lstrip()
    if s.startswith(("❌", "⛔")):
        return "bad"
    if s.startswith(("⚠", "🔒", "⏱", "⏹")):
        return "orange"
    return "muted2"


class StepHeader(QWidget):
    """计划步骤分段：线上一个带编号的圆（完成 / 进行中 / 待办），右边是步骤名。

    步骤来自任务计划本身（模型调用计划工具后程序记下的状态），不是从回复文字里猜的。"""
    _NOTES = {"in_progress": "进行中", "done": "已完成"}

    def __init__(self, n: int, title: str, status: str = "in_progress", parent=None):
        super().__init__(parent)
        self.n = n
        self.status = status
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 4, 0, 4)
        h.setSpacing(12)
        self._title = QLabel(title)
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color:{_P['text']}; font-size:13px; font-weight:600; background:transparent;")
        h.addWidget(self._title, 1)
        self._note = QLabel()
        self._note.setStyleSheet(f"color:{_P['text_sec']}; font-size:12px; background:transparent;")
        h.addWidget(self._note, 0, Qt.AlignTop)
        self.set_status(status)

    def set_status(self, status: str):
        self.status = status
        self._note.setText(self._NOTES.get(status, ""))
        self.update()
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, _Round):
            parent = parent.parentWidget()
        if parent is not None:
            parent.update()

    @property
    def title(self) -> str:
        return self._title.text()

    def flash(self, ms: int = 1400):
        """从任务面板点过来时短暂高亮，看得出落在了哪儿。"""
        self.setObjectName("stepHeader")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"#stepHeader {{ background:{_P['indigo_soft']}; border-radius:6px; }}")
        QTimer.singleShot(ms, self, lambda: self.setStyleSheet(""))


class WaitingIndicator(QWidget):
    """等待模型响应：转圈 + 「等待响应 N 秒」（自走秒表）。"""
    def __init__(self, start_elapsed: int = 0, parent=None):
        super().__init__(parent)
        self._elapsed = start_elapsed
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        from .widgets import LoadingSpinner
        h.addWidget(LoadingSpinner(size=14, color=_P["indigo"]), 0, Qt.AlignVCenter)
        self._lbl = QLabel()
        self._lbl.setStyleSheet(f"color:{_P['text_sec']}; font-size:13px; background:transparent;")
        h.addWidget(self._lbl, 0, Qt.AlignVCenter)
        h.addStretch(1)
        self._tick()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._bump)
        self._timer.start(1000)

    def _tick(self):
        self._lbl.setText(f"等待响应 {self._elapsed} 秒")

    def _bump(self):
        self._elapsed += 1
        self._tick()


class _ProcessSummary(QWidget):
    """上一轮收起后的一行：调用了 N 次工具，思考 M 次　展开过程。"""
    def __init__(self, text: str, on_toggle, parent=None):
        super().__init__(parent)
        self._open = False
        self._on_toggle = on_toggle
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{_P['text_sec']}; font-size:13px; background:transparent;")
        h.addWidget(lbl, 0, Qt.AlignVCenter)
        self.btn = _FlatButton("展开过程")
        self.btn.clicked.connect(self._toggle)
        h.addWidget(self.btn, 0, Qt.AlignVCenter)
        h.addStretch(1)

    def _toggle(self):
        _hold_scroll(self)
        self._open = not self._open
        self.btn.setText("收起过程" if self._open else "展开过程")
        self._on_toggle(self._open)


class UserTurn(QWidget):
    """用户的一句话：线上一个空心圆，右边是原文（不再另加"你"标签）。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setStyleSheet(f"color:{_P['text']}; font-family:{_SANS}; font-size:14px; background:transparent;")
        v.addWidget(self.body)
        self._text = ""

    def append(self, text):
        self._text += text
        self.set_html(_esc(self._text).replace("\n", "<br>"))

    def set_html(self, html):
        # 发送时文本末尾带着换行（旧分隔约定）：去掉，不然用户消息下面空出两行
        html = re.sub(r"^(?:\s*<br\s*/?>)+|(?:<br\s*/?>\s*)+$", "", (html or "").strip())
        self.body.setText(f'<p style="margin:0; line-height:160%;">{html}</p>')


class _Block(QWidget):
    """块外面的一层：带上方间距。收起过程时整块藏起来，间距一起走，不留空洞。"""
    def __init__(self, inner: QWidget, top_gap: int):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, top_gap, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(inner)
        self.inner = inner


class AssistantTurn(QWidget):
    """AI 的一段回复：顶上一行模型 / 模式 / 用时，下面依次是思考、正文、工具卡、步骤分段……
    流式期间各 show_message tag 往这里追加 / 更新块。"""
    def __init__(self, model_name="", parent=None, *, mode="", live=False, round_=None):
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(0)
        self._round = round_
        self.meta = None
        if model_name or mode or live:
            self.meta = MetaLine(model_name, mode, live)
            self._v.addWidget(self.meta)
        # 当前流式状态
        self._cur_body = None          # 正在流式的正文 BodyText（累加纯文本）
        self._cur_body_text = ""
        self._cur_think = None         # 当前 ThinkingChip（流式累加）
        self._cur_think_text = ""
        self._cur_tool = None          # 当前 ToolCallCard（还在接收输出）
        self._waiting = None           # WaitingIndicator
        self._process = []             # 可收起的过程块（思考、工具、步骤分段）
        self._steps = {}               # 步号 → StepHeader
        self._summary = None

    def _add(self, w, top_gap=14, node=None, process=False, **info):
        if self._v.count() == 0:
            top_gap = 0
        blk = _Block(w, top_gap)
        self._v.addWidget(blk)
        if node and self._round is not None:
            self._round.add_node(w, node, **info)
        if process:
            self._process.append(blk)
        return w

    # —— 块级操作 ——
    def end_body(self):
        self._cur_body = None
        self._cur_body_text = ""

    def end_tool(self):
        self._cur_tool = None

    def append_body(self, text):
        self.clear_waiting()
        self._close_think()
        self.end_tool()
        if self._cur_body is None:
            self._cur_body = self._add(BodyText(""), top_gap=12)
        self._cur_body_text += text
        self._cur_body.setText(_esc(self._cur_body_text).replace("\n", "<br>"))

    def finalize_markdown(self, html):
        """流式结束:把当前正文替换成渲染好的 markdown 富文本。"""
        self._close_think()
        if self._cur_body is None:
            self._cur_body = self._add(BodyText(""), top_gap=12)
        self._cur_body.setText(html)
        self.end_body()

    def think_start(self):
        self.clear_waiting()
        self.end_body()
        self._close_think()
        self._cur_think_text = ""
        self._cur_think = self._add(ThinkingChip(0, ""), top_gap=12, node="dot", process=True, dy=13)
        self._cur_think.set_live(True)

    def think_append(self, text):
        if self._cur_think is None:
            self.think_start()
        self._cur_think_text += text
        self._cur_think.update_content(self._cur_think_text)

    def think_collapse(self):
        self._close_think()

    def _close_think(self):
        if self._cur_think is not None:
            self._cur_think.set_live(False)
            self._cur_think.update_content(self._cur_think_text)
        self._cur_think = None

    def tool_call(self, name, code_text="", icon_svg=None, icon_color=None):
        self.clear_waiting()
        self.end_body()
        self._close_think()
        # 步骤类工具用橙星，其余按工具种类取图标
        if icon_svg is None and any(k in name for k in ("步骤", "step")):
            icon_svg, icon_color = SVG_STAR, _P["orange"]
        card = ToolCallCard(name, code_text, icon_svg, icon_color)
        self._cur_tool = self._add(card, top_gap=10, node="tool", process=True, dy=card.header_center)
        return self._cur_tool

    def tool_detail(self, text):
        """工具参数到位：填进当前工具卡；没有当前卡则退化成一条提示。"""
        if self._cur_tool is not None:
            self._cur_tool.set_body(text)
        else:
            self.notice(text)

    def tool_output(self, text):
        """工具输出：进当前工具卡；工具已结束（或根本没有）时是程序提示。"""
        if self._cur_tool is not None:
            self._cur_tool.append_output(text)
        elif (text or "").strip():
            self.notice(text.strip())

    def notice(self, text):
        self.clear_waiting()
        self.end_body()
        tone = _notice_tone(text)
        self._add(Notice(strip_leading_emoji(text.strip())), top_gap=10, node="dot", color=tone, dy=15)

    # 兼容旧名：一段独立的说明文字
    result_block = notice

    def step(self, n, title, status="in_progress"):
        """插入（或更新）一个计划步骤分段。"""
        self.end_body()
        self._close_think()
        hdr = self._steps.get(n)
        if hdr is not None and hdr.isVisibleTo(self):
            hdr.set_status(status)
            return hdr
        hdr = self._add(StepHeader(n, title, status), top_gap=14, node="step", process=True, dy=13)
        self._steps[n] = hdr
        return hdr

    # 兼容旧名
    def step_header(self, n, title):
        return self.step(n, title)

    def update_steps(self, statuses: dict):
        for n, hdr in self._steps.items():
            st = statuses.get(n)
            if st and st != hdr.status:
                hdr.set_status(st)

    def show_waiting(self):
        self.clear_waiting()
        self._waiting = self._add(WaitingIndicator(0), top_gap=14)

    def clear_waiting(self):
        if self._waiting is not None:
            blk = self._waiting.parentWidget()
            target = blk if isinstance(blk, _Block) else self._waiting
            target.setParent(None)
            target.deleteLater()
            self._waiting = None

    def finish(self):
        """这一轮结束：用时定格，还开着的思考 / 工具 / 等待都收尾。"""
        if self.meta is not None:
            self.meta.finish()
        self._close_think()
        self.clear_waiting()
        self.end_tool()

    def collapse_process(self):
        """把思考、工具和步骤分段收成一行摘要（正文、提示和结果卡照常显示）。"""
        if self._summary is not None:
            return
        blocks = [b for b in self._process if not _deleted(b)]
        tools = sum(isinstance(b.inner, ToolCallCard) for b in blocks)
        thinks = sum(isinstance(b.inner, ThinkingChip) for b in blocks)
        if not tools and not thinks:
            return
        parts = []
        if tools:
            parts.append(f"调用了 {tools} 次工具")
        if thinks:
            parts.append(f"思考 {thinks} 次")

        def show(open_):
            for b in blocks:
                b.setVisible(open_)

        summary = _ProcessSummary("，".join(parts), show)
        idx = self._v.indexOf(blocks[0])
        blk = _Block(summary, 0 if idx == 0 else 10)
        self._v.insertWidget(idx, blk)
        if self._round is not None:
            self._round.add_node(summary, "dot", color="muted2", dy=12)
        self._summary = summary
        show(False)


def _hold_scroll(widget):
    """用户点了展开 / 收起：所在消息流停在原处（见 MessageView.hold_position）。"""
    w = widget.parentWidget()
    while w is not None and not isinstance(w, MessageView):
        w = w.parentWidget()
    if w is not None:
        w.hold_position()


def _label_plain_text(lbl) -> str:
    """标签显示出来的纯文字（富文本标签按 Qt 自己的排版取，位置和 setSelection 对得上）。"""
    if lbl.textFormat() == Qt.PlainText:
        return lbl.text()
    from PySide6.QtGui import QTextDocument
    doc = QTextDocument()
    doc.setHtml(lbl.text())
    return doc.toPlainText()


def _reveal(widget):
    """搜索命中在收起的地方时，把挡着它的那几层依次打开。"""
    w = widget.parentWidget()
    while w is not None and not isinstance(w, MessageView):
        if isinstance(w, ThinkingChip) and not w._open:
            w._toggle()
        elif isinstance(w, ToolCallCard) and not w.is_open():
            w.toggle()
        elif isinstance(w, AssistantTurn) and w._summary is not None and not w._summary._open:
            w._summary._toggle()
        w = w.parentWidget()


def _deleted(w) -> bool:
    try:
        w.objectName()
        return False
    except RuntimeError:
        return True


class MessageView(QScrollArea):
    """消息流容器:QScrollArea + 纵向的轮次列表。对外 handle(text, tag) 镜像 show_message 协议,
    finalize_markdown(html) 对应 render_final_markdown,clear()/redraw 复位。"""

    image_clicked = Signal(QPixmap)   # 点击聊天区图片 → 宿主弹放大遮罩(传全分辨率原图)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content = QWidget()
        self.apply_theme()
        self._col = QVBoxLayout(self._content)
        self._col.setContentsMargins(32, 28, 32, 28)
        self._col.setSpacing(0)
        self._col.addStretch(1)
        self.setWidget(self._content)
        self._cur_user = None
        self._cur_assistant = None
        self._cur_round = None
        # 重绘历史时为 True：回复不是"正在进行"的，不起秒表、不标模式
        self.replaying = False
        # ChatUI 提供：当前会话的模式等（{"mode": "执行模式"}），新回复的那行小字用
        self.meta_provider = None
        # 贴底跟随：停在底部时，内容变高（流式追加、换行排版、结果卡）就跟着滚到新的底部。
        # 跟着滚动范围走，不靠追加后定时滚一次——标签和卡片的高度要再排几轮版才定下来，
        # 定时滚动会停在半路，之后"在不在底部"的判断也就一直是否，再也跟不上。
        self._follow = True
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll_value)
        sb.rangeChanged.connect(self._on_scroll_range)

    def _on_scroll_value(self, value):
        """滚动位置变了：停在底部附近就继续跟随，往上翻了就不跟。"""
        self._follow = value >= self.verticalScrollBar().maximum() - 40

    def _on_scroll_range(self, _lo, hi):
        if self._follow:
            self.verticalScrollBar().setValue(hi)

    def scroll_to_end(self):
        """滚到底并恢复贴底跟随（发消息、打开会话、点"回到底部"）。"""
        self._follow = True
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())

    def hold_position(self):
        """用户点了展开 / 收起：停在原处，不因为内容变高被拽到底部。"""
        self._follow = False

    # —— 从任务面板定位到某一步 ——
    def scroll_to_step(self, n: int, title: str = "") -> bool:
        """滚到第 n 步的步骤分段（同一步出现过多次取最近的那次；步骤名对得上的优先），
        所在的那一轮收起了就先展开，落点短暂高亮。消息流里没有这一步的分段时返回 False。"""
        headers = [h for h in self._content.findChildren(StepHeader) if h.n == n]
        if title:
            headers = [h for h in headers if h.title == title] or headers
        if not headers:
            return False
        header = headers[-1]
        _reveal(header)
        self.hold_position()
        header.flash()
        # 展开后的高度要等布局排完才准，下一轮事件循环再滚
        QTimer.singleShot(0, self, lambda: self._scroll_to_widget(header))
        return True

    def _scroll_to_widget(self, w, margin: int = 24):
        if _deleted(w):
            return
        y = w.mapTo(self._content, QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, y - margin))

    # —— 对话内搜索（Ctrl+F）——
    def find_text(self, text, backward=False):
        """在消息流里找文字（不分大小写）：选中下一处（或上一处）并滚过去，到头绕回。

        搜的是能选中的文字：用户消息、回复正文、程序提示、思考内容、工具参数。
        命中在收起的过程 / 卡片 / 思考块里时先把它展开。返回 (第几处, 共几处)，没找到是 (0, 0)。"""
        needle = (text or "").strip().lower()
        hits = []
        if needle:
            for lbl in self._content.findChildren(QLabel):
                if not lbl.textInteractionFlags() & Qt.TextSelectableByMouse:
                    continue
                plain = _label_plain_text(lbl).lower()
                start = plain.find(needle)
                while start >= 0:
                    hits.append((lbl, start, len(plain)))
                    start = plain.find(needle, start + len(needle))
        self._clear_find_selection()
        if not hits:
            self._find_state = (needle, -1)
            return 0, 0
        last_needle, idx = getattr(self, "_find_state", ("", -1))
        if last_needle != needle:
            idx = len(hits) if backward else -1
        idx = (idx + (-1 if backward else 1)) % len(hits)
        self._find_state = (needle, idx)
        lbl, pos, total = hits[idx]
        _reveal(lbl)
        lbl.setSelection(pos, len(needle))
        self._find_label = lbl
        self.hold_position()
        # 展开后的新高度要等布局排完才准，下一轮事件循环再滚
        QTimer.singleShot(0, self, lambda: self._scroll_to_hit(lbl, pos, total))
        return idx + 1, len(hits)

    def clear_find(self):
        self._clear_find_selection()
        self._find_state = ("", -1)

    def _clear_find_selection(self):
        lbl = getattr(self, "_find_label", None)
        self._find_label = None
        if lbl is not None and not _deleted(lbl):
            lbl.setSelection(0, 0)

    def _scroll_to_hit(self, lbl, pos, total):
        """命中大致在标签里的哪一段就滚到哪：长回复里，滚到标签顶上会看不见命中。"""
        if _deleted(lbl):
            return
        y = lbl.mapTo(self._content, QPoint(0, 0)).y()
        y += int(lbl.height() * (pos / max(1, total)))
        sb = self.verticalScrollBar()
        sb.setValue(max(0, y - self.viewport().height() // 3))

    def apply_theme(self, extra_qss=""):
        """背景跟当前主题走。extra_qss 是宿主补充的滚动条等样式——setStyleSheet 会整体替换，
        所以必须和背景一起设，分开设的话后设的会把背景冲掉（深色下聊天区一直是白底就是这个原因）。
        已经画出来的消息块不会自己变色，切主题后由 ChatUI 重绘消息流。"""
        bg = _P["view_bg"]
        self.setStyleSheet(f"#messageView {{ background:{bg}; border:none; }}" + extra_qss)
        self._content.setStyleSheet(f"background:{bg};")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 消息栏最多 COLUMN_MAX 宽，居中；窄窗口时两侧至少留 24px
        side = max(24, (self.viewport().width() - COLUMN_MAX) // 2)
        self._col.setContentsMargins(side, 28, side, 28)

    def clear(self):
        while self._col.count() > 1:   # 留最后的 stretch
            it = self._col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)        # 立即从显示移除（deleteLater 是异步的,不设会残留旧消息）
                w.deleteLater()
        self._cur_user = None
        self._cur_assistant = None
        self._cur_round = None

    def _add_turn(self, w, top_gap=28):
        if self._col.count() > 1 and top_gap:
            # 在 stretch 前插入,轮次之间留间距
            self._col.insertSpacing(self._col.count() - 1, top_gap)
        self._col.insertWidget(self._col.count() - 1, w)

    def _round(self):
        if self._cur_round is None:
            self._cur_round = _Round()
            self._add_turn(self._cur_round)
        return self._cur_round

    def _assistant(self):
        """当前这段回复；还没有（比如程序提示先到）就起一段不带模型名的。"""
        if self._cur_assistant is None:
            r = self._round()
            self._cur_assistant = AssistantTurn(round_=r)
            r.add(self._cur_assistant, top_gap=12)
            r.assistant_turns.append(self._cur_assistant)
        return self._cur_assistant

    def add_user_turn(self, text=""):
        if self._cur_round is not None:
            self._cur_round.collapse_process()   # 新的一轮开始：上一轮的过程收起来
        self._cur_round = _Round()
        self._add_turn(self._cur_round)
        self._cur_user = UserTurn()
        self._cur_round.add(self._cur_user)
        self._cur_round.add_node(self._cur_user.body, "user", dy=11)
        if text:
            self._cur_user.append(text)
        self._cur_assistant = None
        return self._cur_user

    def append_user_html(self, html):
        if self._cur_user is None:
            self.add_user_turn()
        self._cur_user.set_html(html)

    def start_assistant(self, model_name=""):
        r = self._round()
        live = not self.replaying
        mode = ""
        if live and self.meta_provider is not None:
            try:
                mode = (self.meta_provider() or {}).get("mode", "")
            except Exception:
                mode = ""
        self._cur_assistant = AssistantTurn(model_name, mode=mode, live=live, round_=r)
        r.add(self._cur_assistant, top_gap=12)
        r.assistant_turns.append(self._cur_assistant)
        return self._cur_assistant

    def end_run(self):
        """这一轮运行结束（停止、失败也算）：回复的用时定格，开着的块收尾。"""
        if self._cur_round is not None:
            for turn in self._cur_round.assistant_turns:
                turn.finish()

    def on_plan(self, items):
        """任务计划变了：当前步骤换了就在这一轮里插一个步骤分段，已有分段更新状态。"""
        r = self._cur_round
        if r is None or not items:
            return
        statuses = {i + 1: it.get("status") for i, it in enumerate(items)}
        for turn in r.assistant_turns:
            turn.update_steps(statuses)
        cur = next((i + 1 for i, it in enumerate(items) if it.get("status") == "in_progress"), None)
        if cur is not None:
            key = (cur, items[cur - 1].get("text") or "")
            if key != r.current_step:
                r.current_step = key
                self._assistant().step(cur, key[1], "in_progress")

    def finalize_markdown(self, html):
        self._assistant().finalize_markdown(html)

    def add_message_actions(self, on_copy=None, on_regen=None):
        """回复末尾的操作按钮:复制 / 重新生成(幽灵图标按钮)。"""
        a = self._assistant()
        a.end_body()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        specs = [(SVG_COPY, on_copy, "复制")]
        if on_regen is not None:
            specs.append((SVG_REFRESH, on_regen, "重新生成"))
        for svg, cb, tip in specs:
            b = QPushButton()
            b.setObjectName("msgAction")
            b.setFixedSize(28, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setIcon(QIcon(_icon(svg, _P["muted"], 14)))
            b.setIconSize(QSize(14, 14))
            b.setStyleSheet(
                "#msgAction { border:none; background:transparent; border-radius:6px; } "
                f"#msgAction:hover {{ background:{_P['header_bg']}; }}")
            if cb is not None:
                b.clicked.connect(lambda _checked=False, c=cb: c())
            h.addWidget(b, 0, Qt.AlignVCenter)
        h.addStretch(1)
        a._add(row, top_gap=6)

    def handle(self, text, tag):
        """tag→控件分发。镜像 chat_window._append_html 的 tag 协议。"""
        # 滚动由贴底跟随负责（_on_scroll_range）：用户停在底部就跟到底，往上翻了就不动。
        # 用户刚发的消息例外——总是滚到底并恢复跟随。
        if tag == "user_label":
            self.add_user_turn()
        elif tag == "user_msg":
            if self._cur_user is None:
                self.add_user_turn()
            self._cur_user.append(text)
        elif tag in ("ai_label", "reply_header"):
            self.start_assistant(text.strip())
        elif tag == "ai_msg":
            self._assistant().append_body(text)
        elif tag == "think_header":
            self._assistant().think_start()
        elif tag == "think_msg":
            self._assistant().think_append(text)
        elif tag == "think_collapse":
            self._assistant().think_collapse()
        elif tag == "thinking_indicator":
            self._assistant().show_waiting()
        elif tag == "tool_tag":
            # 拒绝/错误类(⚠️/⛔/🔒)是独立提示,不建工具卡;其余是真工具调用 → 建卡
            if any(w in text for w in ("⚠️", "⛔", "🔒", "未知工具")):
                self._assistant().notice(text.strip())
            else:
                name, detail = _parse_tool_tag(text)
                self._assistant().tool_call(name, detail)
        elif tag == "tool_detail":
            self._assistant().tool_detail(text.strip())   # 工具参数 → 当前工具卡的摘要 / 展开内容
        elif tag == "tool_result":
            self._assistant().tool_output(text)
        elif tag == "ai_image":
            self.add_image(QPixmap(text.strip()))   # text = 图片本地路径
        elif tag == "reset_ai_reply":
            if self._cur_assistant is not None:
                self._cur_assistant.end_body()   # 工具调用结束,下一轮 ai_msg 另起正文块
                self._cur_assistant.end_tool()   # 之后到的输出不再算进这张卡
        elif tag == "spacer":
            pass   # 间距由布局给
        if tag in ("user_label", "user_msg") and not self.replaying:
            self.scroll_to_end()

    def remove_waiting(self):
        if self._cur_assistant is not None:
            self._cur_assistant.clear_waiting()

    def show_retry(self, error_msg, on_retry=None):
        """错误 + 重试按钮块。"""
        a = self._assistant()
        a.clear_waiting()
        a.end_body()
        card = QFrame()
        card.setAttribute(Qt.WA_StyledBackground, True)
        card.setObjectName("retryCard")
        card.setStyleSheet(
            f"#retryCard {{ background:{_P['err_bg']}; border:1px solid {_P['err_border']}; "
            "border-radius:10px; }")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(8)
        msg = QLabel(_esc(error_msg))
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{_P['err_text']}; font-size:13px; background:transparent;")
        cv.addWidget(msg)
        btn = QPushButton("重试")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setIcon(QIcon(_icon(SVG_REFRESH, _P["err_text"], 14)))
        btn.setIconSize(QSize(14, 14))
        btn.setStyleSheet(
            f"QPushButton {{ background:{_P['card_bg']}; border:1px solid {_P['err_border']}; "
            f"border-radius:8px; padding:5px 14px; color:{_P['err_text']}; font-size:13px; }} "
            f"QPushButton:hover {{ background:{_P['err_bg']}; }}")
        if on_retry is not None:
            btn.clicked.connect(lambda: on_retry())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(btn, 0, Qt.AlignLeft)
        row.addStretch(1)
        cv.addLayout(row)
        a._add(card, top_gap=12, node="dot", color="bad", dy=20)

    def add_image(self, pixmap, max_w=320):
        """插入一张图片块（用户多模态消息 / 生成图）。接受 QPixmap 或 QImage。
        点击缩略图 → emit image_clicked(全分辨率原图),宿主弹放大遮罩。"""
        from PySide6.QtGui import QImage
        if pixmap is None:
            return
        if isinstance(pixmap, QImage):
            pixmap = QPixmap.fromImage(pixmap)
        if pixmap.isNull():
            return
        orig = pixmap                                   # 全分辨率原图,放大遮罩用
        shown = pixmap
        if shown.width() > max_w:
            shown = shown.scaledToWidth(max_w, Qt.SmoothTransformation)
        lbl = QLabel()
        lbl.setPixmap(shown)
        lbl.setStyleSheet("background:transparent;")
        lbl.setCursor(Qt.PointingHandCursor)
        lbl.setToolTip("点击放大")
        lbl.mousePressEvent = lambda _e, p=orig: self.image_clicked.emit(p)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(wrap)
        wrap.addWidget(lbl, 0, Qt.AlignLeft)
        wrap.addStretch(1)
        if self._cur_round is not None:
            self._cur_round.add(holder, top_gap=8)
        else:
            self._add_turn(holder, top_gap=8)

    def add_result_card(self, card):
        """把一张运行结果卡插到这一轮末尾（线上画结果节点），并返回它。

        结果卡是**应用状态**，不是模型说的话——所以它走独立控件插入，
        绝不为了显示它往 chat_history 里塞一条伪造的 AIMessage。
        """
        if card is None:
            return None
        # **直接加卡，不再套一层只为留白的 QWidget**：那种壳不向上传递 heightForWidth，
        # 卡片被算矮、底部按钮被整条切掉（实测差 300px 以上）。轮次 _Round 本身是带
        # 纵向布局的容器，和 AssistantTurn 装正文一样，高度能正确传上去。
        if self._cur_round is not None:
            self._cur_round.add(card, top_gap=14)
            tone = (getattr(card, "view", None) or {}).get("tone")
            self._cur_round.add_node(card, "result", tone=tone, dy=23)
        else:
            self._add_turn(card, top_gap=16)
        # 结果卡后面的内容（继续任务的说明等）另起一段，不追加到卡片**之前**的那段回复里。
        self._cur_assistant = None
        return card


def replay_plan_change(plan, name, args, result):
    """重绘历史时推演计划：只认被工具接受了的变化（结果里写着已更新）。

    推演出的计划只用来画消息流里的步骤分段；任务面板仍以会话保存的计划为准。
    返回新计划，这次调用没改计划时返回 None。"""
    if name not in ("update_plan", "set_step_status") or not result:
        return None
    if not any(k in result for k in ("计划已更新", "已开启新任务", "已把第", "计划已清空")):
        return None
    if name == "update_plan":
        from .. import state
        text = re.split(r"\s*\[(?:历史摘要|之前已有摘要|新增对话)\]\s*:?",
                        str(args.get("plan") or ""), maxsplit=1)[0]
        return state.parse_plan(text)
    from ..tools import _auto_advance_plan, _normalize_step_status
    try:
        idx = int(args.get("step"))
    except (TypeError, ValueError):
        return None
    status = _normalize_step_status(args.get("status", ""))
    if status is None or not 1 <= idx <= len(plan):
        return None
    new_plan = [dict(it) for it in plan]
    new_plan[idx - 1]["status"] = status
    _auto_advance_plan(new_plan)
    return new_plan


def _parse_tool_tag(text):
    """从 tool_tag 文本里拆出工具名和（同一行带来的）参数摘要：去掉开头的 emoji 图标。

    普通工具的参数随后由 tool_detail 送来；Claude Code 模式把参数和名字放在同一行，
    中间隔两个空格。"""
    s = (text or "").strip()
    s = re.sub(r'^[\U0001F000-\U0001FAFF☀-➿️←-⇿\s]+', '', s)
    name, _, detail = s.partition("  ")
    return name.strip(), detail.strip()
