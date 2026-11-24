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
from collections import deque
from contextlib import contextmanager
import re
import time

from PySide6.QtCore import QByteArray, QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QIcon, QPainter, QPen, QPixmap, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .helpers import strip_leading_emoji
from .fonts import BODY, MONO_CSS, UI_CSS, mono_font, ui_font
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
_ANSWER_DY = 12      # 回答节点对正文第一行的中线（14px 字、1.7 倍行高）
_CX = 11             # 竖线和节点的中心 x（节点最宽 22px，留足不被裁）
COLUMN_MAX = 820     # 消息栏最大宽度（含线槽）；窗口再宽，两侧留白
COLUMN_SIDE = 24     # 消息栏两侧至少留这么宽（窄窗口时就是这么宽）


def mv_qss() -> str:
    """消息流全部控件的样式：一张表，挂在 MessageView 上（apply_theme），按当前主题现生成。

    控件只打标记（objectName 或 mv 属性），不各自 setStyleSheet。早先每个控件构造时
    单独设一份：每次都要解析 CSS、重算这一枝的样式，一张工具卡十几次，重画 92 张工具卡的
    长会话光这一项就几百毫秒；内容区还挂了一条不带选择器的 background，会级联到每个
    子控件，于是每个子控件又得写 background:transparent 盖回去。
    规则都限定在 mvRoot 属性下（不影响别处）；悬停提示不受影响——带选择器的规则不会
    让提示框变黑（那是不带选择器的声明才有的问题，见 _ElidedLabel.setStyleSheet）。"""
    P = _P
    # 范围用属性限定，不用 objectName：ChatUI 把消息区的 objectName 改成了 chatArea，
    # 按 #messageView 写的规则一条都匹配不上（实测：气泡没底色、按钮成了默认样式）
    mv = 'QScrollArea[mvRoot="true"]'
    return f"""
{mv} {{ background:{P['view_bg']}; border:none; }}
{mv} #mvContent {{ background:{P['view_bg']}; }}
{mv} QLabel[mv="body"] {{ color:{P['text2']}; font-family:{_SANS}; font-size:14px; }}
{mv} QLabel[mv="meta"] {{ color:{P['text_sec']}; font-size:12px; }}
{mv} QLabel[mv="secondary12"] {{ color:{P['text_sec']}; font-size:12px; }}
{mv} QLabel[mv="secondary13"] {{ color:{P['text_sec']}; font-size:13px; }}
{mv} QLabel[mv="muted12"] {{ color:{P['muted']}; font-size:12px; }}
{mv} QPushButton[mv="flat"] {{ border:none; background:transparent; color:{P['indigo']};
    font-size:12px; padding:2px 6px; border-radius:6px; }}
{mv} QPushButton[mv="flat"]:hover {{ background:{P['indigo_soft']}; }}
{mv} #thinkChip {{ border:1px solid {P['chip_border']}; background:{P['header_bg']};
    border-radius:8px; padding:3px 10px; color:{P['text_sec']}; font-size:12px; }}
{mv} #thinkChip:hover {{ border-color:{P['chip_hover']}; }}
{mv} #thinkPanel {{ background:{P['header_bg']}; border-left:2px solid {P['panel_accent']};
    border-top-right-radius:8px; border-bottom-right-radius:8px;
    color:{P['muted']}; font-size:13px; padding:12px 14px; }}
{mv} #toolRowHead {{ background:transparent; border-radius:7px; }}
{mv} #toolRowHead:hover {{ background:{P['header_bg']}; }}
{mv} #toolRow[open="true"] #toolRowHead {{ background:{P['header_bg']}; }}
{mv} QLabel[mv="toolVerb"] {{ color:{P['text_sec']}; font-size:13px; }}
{mv} QLabel[mv="toolTarget"] {{ color:{P['text3']}; font-family:{_MONO}; font-size:12px; }}
{mv} QLabel[mv="toolStats"] {{ font-family:{_MONO}; font-size:12px; }}
{mv} QLabel[mv="toolStatus"] {{ color:{P['muted']}; font-size:12px; }}
{mv} QLabel[mv="toolStatus"][tone="bad"] {{ color:{P['bad']}; }}
{mv} QLabel[mv="toolStatus"][tone="warn"] {{ color:{P['orange']}; }}
{mv} QLabel[mv="toolClock"] {{ color:{P['indigo']}; font-size:12px; }}
{mv} #toolArgs {{ color:{P['text_sec']}; padding-left:11px; }}  /* 和输出框里的字对齐：边框 1 + 内边距 6 + 文档边距 4 */
{mv} #toolTail {{ color:{P['muted']}; font-family:{_MONO}; font-size:12px; padding:0px 0px 4px 31px; }}
{mv} #toolFiles {{ color:{P['text_sec']}; font-size:12px; padding:0px 0px 4px 31px; }}
{mv} #toolGroupHead {{ background:transparent; border-radius:7px; }}
{mv} #toolGroupHead:hover {{ background:{P['header_bg']}; }}
{mv} QLabel[mv="groupSummary"] {{ color:{P['text_sec']}; font-size:13px; }}
{mv} QLabel[mv="groupFail"] {{ color:{P['bad']}; font-size:13px; }}
{mv} QLabel[mv="groupDur"] {{ color:{P['muted2']}; font-size:13px; }}
{mv} #toolGroupRows[framed="true"] {{ border:none; border-left:2px solid {P['card_border']}; margin-left:10px; }}
{mv} QPlainTextEdit#toolOut, {mv} QPlainTextEdit#toolScript {{ background:{P['result_bg']}; color:{P['text_sec']};
    border:1px solid {P['result_border']}; border-radius:8px; padding:4px 6px; }}
{mv} QPlainTextEdit#toolOut QScrollBar:vertical, {mv} QPlainTextEdit#toolScript QScrollBar:vertical {{
    background:transparent; width:6px; margin:2px 0; }}
{mv} QPlainTextEdit#toolOut QScrollBar::handle:vertical, {mv} QPlainTextEdit#toolScript QScrollBar::handle:vertical {{
    background:{P['chip_border']}; border-radius:3px; min-height:20px; }}
{mv} QPlainTextEdit#toolOut QScrollBar::add-line:vertical, {mv} QPlainTextEdit#toolScript QScrollBar::add-line:vertical,
{mv} QPlainTextEdit#toolOut QScrollBar::sub-line:vertical, {mv} QPlainTextEdit#toolScript QScrollBar::sub-line:vertical {{ height:0; }}
{mv} QPlainTextEdit#toolOut QScrollBar::add-page:vertical, {mv} QPlainTextEdit#toolScript QScrollBar::add-page:vertical,
{mv} QPlainTextEdit#toolOut QScrollBar::sub-page:vertical, {mv} QPlainTextEdit#toolScript QScrollBar::sub-page:vertical {{ background:transparent; }}
{mv} QLabel[mv="toolBoxLabel"] {{ color:{P['muted']}; font-size:12px; padding-left:2px; }}
{mv} QLabel[mv="toolScriptInfo"] {{ color:{P['muted2']}; font-size:12px; }}
{mv} #notice {{ color:{P['text_sec']}; font-size:12px; background:{P['header_bg']};
    border-radius:8px; padding:6px 10px; }}
{mv} QLabel[mv="stepTitle"] {{ color:{P['text']}; font-size:13px; font-weight:600; }}
{mv} #userBubble {{ background:{P['indigo_soft']}; border-radius:10px; }}
{mv} QLabel[mv="userBody"] {{ color:{P['text']}; font-family:{_SANS}; font-size:14px; }}
{mv} #msgAction {{ border:none; background:transparent; border-radius:6px; }}
{mv} #msgAction:hover {{ background:{P['header_bg']}; }}
{mv} #procSummary {{ border:none; background:transparent; padding:2px 6px 2px 0; text-align:left; }}
{mv} #procSummary QLabel {{ color:{P['text_sec']}; font-size:13px; }}
{mv} #procSummary:hover QLabel {{ color:{P['text']}; }}
{mv} QLabel[mv="recTitle"] {{ color:{P['muted2']}; font-size:12px; }}
{mv} QLabel[mv="recDetail"] {{ color:{P['muted']}; font-size:12px; }}
{mv} QPushButton[mv="recLink"] {{ border:none; background:transparent; color:{P['indigo']};
    font-size:12px; padding:0 2px; }}
{mv} QPushButton[mv="recLink"]:hover {{ text-decoration:underline; }}
{mv} #roundSep {{ background:{P['result_border']}; border:none; }}
{mv} #retryCard {{ background:{P['err_bg']}; border:1px solid {P['err_border']}; border-radius:10px; }}
{mv} QLabel[mv="retryMsg"] {{ color:{P['err_text']}; font-size:13px; }}
{mv} QPushButton[mv="retryBtn"] {{ background:{P['card_bg']}; border:1px solid {P['err_border']};
    border-radius:8px; padding:5px 14px; color:{P['err_text']}; font-size:13px; }}
{mv} QPushButton[mv="retryBtn"]:hover {{ background:{P['err_bg']}; }}
"""


_ICON_CACHE: dict = {}
_ICON_CACHE_MAX = 512


def _icon(svg: str, color: str, size: int = 16) -> QPixmap:
    """把内联 SVG（用 currentColor 占位描边）渲染成单色 QPixmap。

    按 (svg, 颜色, 尺寸) 缓存：重画长会话时每张工具卡、每个箭头都要一张，同样几种图标
    反复解析 SVG、渲染（两个长会话 500 多次）。返回副本——QPixmap 隐式共享，复制不拷像素，
    调用方在副本上画东西也不会改到缓存里那张。"""
    key = (svg, color, size)
    pm = _ICON_CACHE.get(key)
    if pm is None:
        pm = _render_icon(svg, color, size)
        if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
            _ICON_CACHE.clear()
        _ICON_CACHE[key] = pm
    return QPixmap(pm)


def _render_icon(svg: str, color: str, size: int) -> QPixmap:
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


def _set_prop(w, name: str, value) -> bool:
    """设动态属性；值真的变了才返回 True（调用方据此决定要不要重新套样式）。"""
    if w.property(name) == value:
        return False
    w.setProperty(name, value)
    return True


def _set_style_prop(w, name: str, value):
    """设选择器里用到的动态属性，变了才 unpolish / polish 让样式表重新匹配。"""
    if _set_prop(w, name, value):
        w.style().unpolish(w)
        w.style().polish(w)


def _icon_label(svg: str, color: str, size: int = 16) -> QLabel:
    lbl = QLabel()
    lbl.setFixedSize(size, size)
    lbl.setPixmap(_icon(svg, color, size))
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
SVG_UNDO = _svg('<path d="M2.5 3v3.2h3.2"/><path d="M2.7 6.2A5.5 5.5 0 1113.4 9.5"/>')
SVG_FORK = _svg('<circle cx="4" cy="3.3" r="1.7"/><circle cx="4" cy="12.7" r="1.7"/><circle cx="12" cy="5.3" r="1.7"/><path d="M4 5v6M12 7v.5a2 2 0 01-2 2H4"/>')
SVG_TERMINAL = _svg('<rect x="1.8" y="2.5" width="12.4" height="11" rx="1.5"/><path d="M4.5 6l2 2-2 2M8.5 10h3"/>')
SVG_FILE = _svg('<path d="M9 1.8H4.5A1.5 1.5 0 003 3.3v9.4a1.5 1.5 0 001.5 1.5h7a1.5 1.5 0 001.5-1.5V5.8z"/><path d="M9 1.8v4h4"/>')
SVG_PEN = _svg('<path d="M10.5 2.5l3 3L6 13H3v-3z"/>')
SVG_SEARCH = _svg('<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5l3.5 3.5"/>')
SVG_TOOL = _svg('<path d="M10 2.2a3.3 3.3 0 00-3.9 4.3L2.3 10.3a1.4 1.4 0 002 2l3.8-3.8A3.3 3.3 0 0012.4 4.6l-2 2-1.7-.4-.4-1.7z"/>')
SVG_CHEVRON_RIGHT = _svg('<path d="M6 4l4 4-4 4"/>')
SVG_CHEVRON_DOWN = _svg('<path d="M4 6l4 4 4-4"/>')
SVG_CHEVRON_UP = _svg('<path d="M4 10l4-4 4 4"/>')
SVG_GLOBE = _svg('<circle cx="8" cy="8" r="6"/><path d="M2 8h12M8 2c1.8 1.7 2.6 3.7 2.6 6S9.8 12.3 8 14c-1.8-1.7-2.6-3.7-2.6-6S6.2 3.7 8 2z"/>')

# 工具名（显示名）里出现这些词，就用对应图标
_TOOL_ICONS = (
    (("执行命令", "测试", "代码检查", "后台"), SVG_TERMINAL),
    (("写入", "编辑", "追加", "补丁"), SVG_PEN),
    (("搜索", "查找", "跳转", "检索", "探索"), SVG_SEARCH),
    (("读取", "目录", "文件", "地图", "改动", "历史", "状态", "查看"), SVG_FILE),
    (("计划", "步骤"), SVG_CLIPBOARD),
)


def _tool_icon(name: str) -> str:
    if "浏览器" in name or name in ("抓取网页", "网络搜索"):
        return SVG_GLOBE
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
        self.setProperty("elided", True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(0)
        self.set_full(text)

    def setStyleSheet(self, css: str):
        # 悬停提示按"所属控件"的样式表级联：不带选择器的 background:transparent 会让提示框
        # 在 Windows 上变成黑底，字体也跟着成了等宽。套上只匹配自己的选择器，提示框回到主题样式。
        if css and "{" not in css:
            css = f'QLabel[elided="true"] {{ {css} }}'
        super().setStyleSheet(css)

    def set_full(self, text: str):
        self._full = " ".join((text or "").split())
        # 包成富文本：Qt 只给富文本提示自动换行，长命令不会拉成一整行盖住下面的输出
        self.setToolTip(f"<p>{_esc(self._full)}</p>" if len(self._full) > 40 else "")
        self._apply()

    def full(self) -> str:
        return self._full

    def minimumSizeHint(self):
        # 放不下就省略，所以能压到任意窄：最小宽度如实报 0。不报的话 QLabel 按当前文字宽度报，
        # 一条长摘要就把整张卡 / 整行顶出可视区
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        # 想要的宽度按【完整文字】报：QLabel 默认按当前（已经省略过的）文字算，按想要的宽度排的
        # 地方（工具卡头上交了脚本的命令）会一直拿到 0 宽、只剩空白。尺寸策略是 Ignored 时不看这个。
        # 文字宽度记着：布局一轮要问很多遍，现量的话拖窗口时这里就占好几毫秒（实测）
        key = (self._full, self.font().key())
        if getattr(self, "_hint_key", None) != key:
            self._hint_key = key
            self._hint = QSize(self.fontMetrics().horizontalAdvance(self._full) + 4,
                               super().sizeHint().height())
        return self._hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()

    def _apply(self):
        width = max(0, self.width() - 2)
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, width))


class _FitLabel(QLabel):
    """自动换行的富文本，但宽度先按"不换行要多宽"来要：短话不被提前折行，长话撑到可用宽度再折。
    QLabel 自带的换行宽度是它自己估的"好看"宽度，气泡里一长句会被折成二十来字一行。"""
    def sizeHint(self):
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setDocumentMargin(0)
        doc.setHtml(self.text())
        width = math.ceil(doc.idealWidth()) + 2
        return QSize(width, self.heightForWidth(width))


class _FlatButton(QPushButton):
    """消息流里的文字按钮（展开 / 收起 / 展开过程）：无边框，主题色字。"""
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("mv", "flat")


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

    def collapse_process(self, *, need_answer=False):
        for turn in self.assistant_turns:
            turn.collapse_process(need_answer=need_answer)

    def event(self, e):
        handled = super().event(e)
        if e.type() == QEvent.LayoutRequest:
            self.update()    # 子块高度变了（展开、流式追加）：节点位置跟着重画
        return handled

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update()

    def answer_body(self):
        """这一轮的最终回答（最后一段回复的最后一个正文块），线上给它一个实心圆点；
        这一轮不是以回答收尾（停在工具上、报了错、后面还有提示）时返回 None——
        那时最后一段正文只是过渡回复，标成回答会误导。"""
        turn = self.assistant_turns[-1] if self.assistant_turns else None
        if turn is None or _deleted(turn) or not turn.isVisibleTo(self):
            return None
        for j in range(turn._v.count() - 1, -1, -1):
            b = turn._v.itemAt(j).widget()
            if b is None or not b.isVisibleTo(self):
                continue
            if isinstance(b, _Block) and b.inner.objectName() == "msgActions":
                continue
            return b.inner if isinstance(b, _Block) and isinstance(b.inner, BodyText) else None
        return None

    def _content_bottom(self) -> int:
        """这一轮里用户消息之后的内容画到哪（本控件坐标）：回答末尾那排复制 / 重新生成不算
        （藏着也占位，算进去线会伸进空白里）；只有用户消息时返回 0（不往下拖一截）。"""
        bottom = 0
        for i in range(self._v.count()):
            w = self._v.itemAt(i).widget()
            if w is None or isinstance(w, UserTurn) or not w.isVisibleTo(self):
                continue
            target = w
            if isinstance(w, AssistantTurn):
                blocks = [w._v.itemAt(j).widget() for j in range(w._v.count())]
                blocks = [b for b in blocks if b is not None and b.isVisibleTo(self)
                          and not (isinstance(b, _Block) and b.inner.objectName() == "msgActions")]
                if not blocks:
                    continue
                target = blocks[-1]
            try:
                y = target.mapTo(self, QPoint(0, 0)).y() + target.height()
            except RuntimeError:
                continue
            bottom = max(bottom, y)
        return bottom - 4 if bottom else 0

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
        answer = self.answer_body()
        if answer is not None and pts:
            try:
                y = answer.mapTo(self, QPoint(0, 0)).y() + _ANSWER_DY
                pts.append((y, answer, "answer", {}))
            except RuntimeError:
                pass
        if not pts:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        thread = QColor(_P["indigo"])
        ys = [pt[0] for pt in pts]
        # 竖线从第一个节点一直画到这一轮内容的底（回答最后一行），不停在最后一个节点上——
        # 收起过程后最后一个节点是那行摘要，下面整段回答旁边是空的；只有一个节点的轮也画
        end = max(max(ys), self._content_bottom())
        if end > min(ys):
            pen = QPen(thread, 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(_CX, min(ys)), QPointF(_CX, end))
            if end - max(ys) >= 24:
                # 线的底端收一个小空心圆：这一轮到这里结束（离最后一个节点太近就不画，免得挤在一起）
                p.setBrush(QColor(_P["view_bg"]))
                p.drawEllipse(QPointF(_CX, end), 4, 4)
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
    elif kind == "answer":                   # 最终回答：实心圆（和用户消息的空心圆一对）
        p.setPen(Qt.NoPen)
        p.setBrush(thread)
        p.drawEllipse(c, 5, 5)
    elif kind == "dot":                      # 思考 / 提示 / 收起的过程：小圆点
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(_P[info.get("color", "muted2")]))
        p.drawEllipse(c, 3, 3)
    elif kind == "tool":                     # 工具调用（一组 / 改文件的一行）：带点的圆环；有失败变红
        ring = QColor(_P["bad"]) if getattr(w, "node_tone", lambda: "")() == "bad" else thread
        p.setPen(QPen(ring, 2))
        p.setBrush(bg)
        p.drawEllipse(c, 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(ring)
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
    """正文段落。富文本，流式时是转义后的纯文本，结束后换成渲染好的 markdown。

    回复里的项目文件路径渲染成链接（见 markdown_render），点了发 link_clicked。
    文字照常能选中复制——链接和选择两种交互 Qt 里可以并存。
    """
    link_clicked = Signal(str)     # 点了正文里的链接（原样转发 href）

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.setProperty("mv", "body")
        self.linkActivated.connect(self.link_clicked)
        # 右键菜单要知道点在哪个链接上：QLabel 不给按位置查链接，跟着悬停记下来
        self.linkHovered.connect(lambda href: self.setProperty("lx_hover_link", href or ""))
        self.linkHovered.connect(self._show_link_tip)
        self._hfw_cache = {}
        self.setText(text)

    # 「这个宽度要多高」自己记着：QLabel 不缓存，每问一次就把整段富文本重新排一遍版。布局一轮
    # 要问好几次、还按不同宽度问（实测流式时每来一段，前面写完的每一段都被问了 782 / 266 两个
    # 宽度），回复一长每段就要几十毫秒。文字 / 字体 / 样式一变就清
    def setText(self, text):
        self._hfw_cache = {}
        super().setText(text)

    def heightForWidth(self, w):
        h = self._hfw_cache.get(w)
        if h is None:
            h = super().heightForWidth(w)
            if len(self._hfw_cache) >= 6:
                self._hfw_cache.clear()
            self._hfw_cache[w] = h
        return h

    def changeEvent(self, event):
        if event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.ContentsRectChange):
            self._hfw_cache = {}
        super().changeEvent(event)

    def _show_link_tip(self, href):
        """悬停链接：完整网址 + 点了会怎样（设计稿「回复里的链接」）。链接上只显示标题或
        缩短的网址，真实地址要在这里看得到——点之前知道会去哪。"""
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QToolTip
        tip = link_tip(href)
        if tip:
            QToolTip.showText(QCursor.pos(), tip, self)
        else:
            QToolTip.hideText()


def link_tip(href: str) -> str:
    """正文链接的悬停提示文字；不是链接（移开了）返回 ""。"""
    href = href or ""
    if href.startswith(("http://", "https://")):
        from .. import config as _cfg
        from ..browser import security
        if security.is_localhost(href) or _cfg.LINK_OPEN_MODE == "panel":
            how = "点击在侧栏浏览器打开"
        else:
            how = "点击用系统浏览器打开"
        return f"{href}\n{how} · 右键更多"
    if href.startswith("lingxi-file:"):
        return "在右侧文件面板里打开"
    return ""


class MetaLine(QWidget):
    """回复上方的一行小字：模型、模式、用时。进行中每秒走一次，这一轮结束后定格。"""
    def __init__(self, model_name: str = "", mode: str = "", live: bool = False, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(14)
        for text in (model_name, mode):
            if text:
                lbl = QLabel(text)
                lbl.setProperty("mv", "meta")
                h.addWidget(lbl)
        self._t0 = time.monotonic()
        self._clock = None
        self._timer = None
        if live:
            self._clock = QLabel()
            self._clock.setProperty("mv", "meta")
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
        _settle_layout(self)


# ── 工具调用：一行一条，连续的收成一组（设计稿「消息区：工具调用改成一行一条」）──

# 改文件的工具单独一行、不进组：改了什么要一眼看得到
_EDIT_TOOLS = ("写入文件", "追加文件", "精确编辑", "批量补丁", "删除文件", "移动/重命名",
               "复制文件", "新建目录")
_TOOL_VERBS = {
    "执行命令": "运行", "读取文件": "读取", "读取多个文件": "读取", "读取图片": "读取",
    "列出目录": "列出", "单文件搜索": "搜索", "跨文件搜索": "搜索", "精确编辑": "编辑",
    "写入文件": "写入", "追加文件": "追加", "批量补丁": "补丁", "删除文件": "删除",
    "移动/重命名": "移动", "复制文件": "复制", "网络搜索": "搜索网页",
    # 不叫「打开网页」：fetch_url 是直接抓网页正文、不经过侧栏浏览器，叫「打开」看着像
    # 浏览器该打开了却没开（用户实测）
    "抓取网页": "抓取网页",
}
_KIND_OF = (
    ("read", ("读取文件", "读取多个文件", "读取图片")),
    ("list", ("列出目录",)),
    ("search", ("单文件搜索", "跨文件搜索", "跳转定义", "查找引用", "查找测试", "关联文件",
                "探索代码", "知识库检索", "代码地图")),
    ("cmd", ("执行命令", "跑测试", "代码检查", "批量查看（程序）")),
    ("web", ("网络搜索", "抓取网页")),
)


def _tool_kind(name: str) -> str:
    if name in _EDIT_TOOLS:
        return "edit"
    if "浏览器" in name:
        return "browser"
    for kind, names in _KIND_OF:
        if name in names:
            return kind
    return "other"


_SCRIPT_LANGS = {
    "python": "python", "python3": "python", "py": "python", "pythonw": "python",
    "node": "javascript", "nodejs": "javascript",
}


def _script_language(command: str) -> str:
    """交了脚本的命令按解释器认语言（python / python3 / py → python，node → javascript），
    认不出返回空串（不着色）。路径和 .exe 后缀去掉再认：C:\\Python314\\python.exe -。"""
    first = (command or "").strip().split()
    if not first:
        return ""
    exe = re.split(r"[\\/]", first[0].strip('"'))[-1].lower()
    exe = exe[:-4] if exe.endswith(".exe") else exe
    exe = re.sub(r"\d+(\.\d+)*$", "", exe) if exe.startswith("python") else exe
    return _SCRIPT_LANGS.get(exe, "")


class _ScriptHighlighter(QSyntaxHighlighter):
    """脚本框按语言着色：范围由文件面板那套 syntax_highlight 算（tree-sitter），这里只按行上色。
    颜色按当前主题取；认不出语言 / 没装语法库时什么都不做（等宽纯文本）。"""

    _PATHS = {"python": "script.py", "javascript": "script.js"}

    def __init__(self, document, text, lang):
        super().__init__(document)
        self._spans = {}
        path = self._PATHS.get(lang)
        if path:
            from . import syntax_highlight as _sh
            rows = [(i + 1, "ctx", line) for i, line in enumerate(text.split("\n"))]
            self._spans = _sh.highlight_rows(rows, path)
        if self._spans:
            self.rehighlight()

    def highlightBlock(self, text):
        spans = self._spans.get(self.currentBlock().blockNumber())
        if not spans:
            return
        from . import syntax_highlight as _sh
        for start, end, category in spans:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(theme_color(_sh.token_for(category))))
            self.setFormat(start, end - start, fmt)


def _tool_verb(name: str) -> str:
    return _TOOL_VERBS.get(name, name)


_EXIT_RE = re.compile(r"^[✓\s]*退出码[:：]?\s*(-?\d+)")
_FAIL_RE = re.compile(r"^(\S{0,8}?失败)[:：]")


def _tool_outcome(text: str):
    """从工具输出判断这次调用是不是失败了：(tone, 标签)；看不出来返回 ("", "")。

    宁可不标也不错标：只认确定的信号——非零退出码、❌ / ⛔ 开头、「失败：」「读取失败:」这类
    开头，以及用户中断。输出里出现 error / 失败字样不算（测试名、正常日志里常有）。"""
    for line in (text or "").splitlines():
        m = _EXIT_RE.match(line)
        if m:
            code = int(m.group(1))
            return ("bad", f"退出码 {code}") if code else ("ok", "")
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if not first:
        return "", ""
    if first.startswith(("❌", "⛔")):
        return "bad", "失败"
    if first.startswith("⏹") or first.startswith(("用户中断", "已停止")):
        return "warn", "已停止"
    m = _FAIL_RE.match(strip_leading_emoji(first))
    if m:
        return "bad", m.group(1)
    return "", ""


class _RowHead(QWidget):
    """工具行的那一行：整行可点（展开 / 收起，改文件的点文件名看差异），悬停有底色。"""
    clicked = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolRowHead")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedHeight(30)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(e.position().toPoint())
        super().mouseReleaseEvent(e)

    def enterEvent(self, e):
        super().enterEvent(e)
        p = self.parentWidget()
        if isinstance(p, ToolCallCard):
            p._set_hover(True)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        p = self.parentWidget()
        if isinstance(p, ToolCallCard):
            p._set_hover(False)


class ToolCallCard(QFrame):
    """一次工具调用 = 一行：图标 · 动作 · 对象 · 右边的状态（失败 / 已停止 / 运行中 / 增删行数）。

    做完了不加标记；失败时图标和结果变红（判断见 _tool_outcome，宁可不标不错标）。运行超过
    CLOCK_AFTER_S 秒图标换成转圈、右边走秒，下面露最新一行输出。
    **输出只存成文字，点开这一行才建输出框**：长会话几十上百次调用，收着的输出不建控件——
    早先每张卡都建好一个藏着的 QPlainTextEdit，重画长会话时这一项就占了三分之一。
    类名沿用 ToolCallCard（对外接口不变：set_body / append_output / set_changed_files /
    start_clock / stop_clock / toggle / output_text ……）。"""

    _OUT_MAX_LINES = 16      # 展开后输出框最多这么高，更多的在框里滚动
    _OUT_MAX_BLOCKS = 4000   # 最多保留的行数（长命令的流式输出），更早的丢弃
    _FILES_MAX_LINKS = 6     # "改动了 N 个文件"那一行最多列几个文件名，全部的在悬停提示里
    CLOCK_AFTER_S = 2        # 跑了这么久还没结束才出「运行中 N 秒」：快的工具不闪一下
    # 展开区（参数 + 输出框）和这一行的底色左边对齐：展开时行头整条有底色，框再缩进去一截
    # 看着像错位（用户实测）。最后一行输出 / 改动文件那两行小字没有框，仍和动作文字对齐（QSS 里的 31）
    _BODY_INDENT = 0

    file_clicked = Signal(str)   # 点了改动的文件名（项目相对路径）/ 改文件那一行的文件名
    state_changed = Signal()     # 失败、结束、输出到了：所在的组刷新组头

    def __init__(self, name: str, code_text: str = "", icon_svg: str | None = None,
                 icon_color: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("toolRow")
        self.name = name
        self.kind = _tool_kind(name)
        self._icon_svg = icon_svg or _tool_icon(name)
        self._icon_color = icon_color
        self._open = False
        self._hover = False
        self._detail = ""
        self._args_needed = False
        self._script = ""            # run_command 交进去的脚本（stdin）；没有就是空串
        self._script_lang = ""       # 按命令认出的语言：python / javascript / ""（不着色）
        self._script_box = None
        self._script_label = None
        self._out_label = None
        self._lines = deque(maxlen=self._OUT_MAX_BLOCKS)
        self._out_lines = 0
        self._chunks = 0
        self._first_line = ""
        self._last_line = ""
        self.tone = ""               # "" 做完了 / bad 失败 / warn 已停止
        self.status_text = ""
        self._t0 = None
        self._clock_timer = None
        self._spinner = None
        self.duration = None         # 实时运行才有（秒）
        self._body = None            # 展开区：第一次展开才建
        self._args = None
        self._out = None
        self._fitted_width = -1
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.header = _RowHead(self)
        self.header.clicked.connect(self._on_head_clicked)
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 0, 8, 0)
        h.setSpacing(8)
        self._hlay = h
        self._icon = QLabel()
        self._icon.setFixedSize(15, 15)
        h.addWidget(self._icon, 0, Qt.AlignVCenter)
        self._verb = QLabel(_tool_verb(name))
        self._verb.setProperty("mv", "toolVerb")
        h.addWidget(self._verb, 0, Qt.AlignVCenter)
        self._summary = _ElidedLabel()
        self._summary.setProperty("mv", "toolTarget")
        if self.kind == "edit":
            self._summary.setCursor(Qt.PointingHandCursor)
        self._summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        h.addWidget(self._summary, 1, Qt.AlignVCenter)
        # 下面几样大多数卡用不上，要用时才建（_ensure_*）：重画长会话时每张卡少建五个藏着的
        # 控件。头上的顺序：图标 · 动作 · 对象 ·「脚本 N 行」· 增删行数 · 状态 · 走秒 · 箭头；
        # 头下面：展开区 · 最新一行输出（运行中）· 改动的文件
        self._script_info = None     # 交了脚本的命令：命令后面一句浅色的「脚本 N 行」
        self._stats = None           # 改文件的增删行数
        self._clock = None           # 运行中 N 秒（实时运行超过 CLOCK_AFTER_S 才有）
        self._tail = None            # 运行中露最新一行输出
        self._files = None           # 命令运行期间项目里变了的文件：点文件名在文件面板里看差异
        self._status = QLabel()
        self._status.setProperty("mv", "toolStatus")
        self._status.setVisible(False)
        h.addWidget(self._status, 0, Qt.AlignVCenter)
        self._chev = QLabel()
        self._chev.setFixedSize(12, 12)
        self._chev.setVisible(False)
        h.addWidget(self._chev, 0, Qt.AlignVCenter)
        v.addWidget(self.header)

        self._paint_icon()
        self.set_body(code_text)

    # 线上节点对齐这一行的中线
    def header_center(self, _w=None) -> int:
        return max(14, self.header.height() // 2)

    def node_tone(self) -> str:
        return "bad" if self.tone == "bad" else ""

    def units(self) -> int:
        """组头计数用：一次读多个文件按文件数算。"""
        if self.name == "读取多个文件":
            return max(1, len([p for p in self._detail.split("、") if p.strip()]))
        return 1

    # ── 内容 ──
    def set_body(self, code_text: str):
        """工具参数（tool_detail）到位：行里放一行摘要，多行参数留到展开后看。

        交了脚本的命令（streaming.SCRIPT_MARK 隔开命令和脚本）：头上写命令 +「脚本 N 行」，
        展开后脚本进框、按语言着色，不再把命令和脚本拼成一段按 JSON 规则上色。"""
        from ..streaming import SCRIPT_MARK
        detail = (code_text or "").strip()
        cmd, mark, script = detail.partition(SCRIPT_MARK)
        self._script = script.strip("\n") if mark else ""
        self._detail = cmd.strip() if mark else detail
        self._script_lang = _script_language(self._detail) if self._script else ""
        self._summary.set_full(_one_line(self._detail))
        self._args_needed = "\n" in self._detail
        self._sync_script_info()
        if self._args is not None and self._args_needed:
            self._args.setText(highlight_json(self._detail))
        if self._body is not None and self._script:
            self._ensure_script_widgets()
            self._fill_script()
        self._sync()

    # ── 用到才建的几样 ──
    def _header_label(self, mv: str, before: QWidget) -> QLabel:
        lbl = QLabel()
        lbl.setProperty("mv", mv)
        self._hlay.insertWidget(self._hlay.indexOf(before), lbl, 0, Qt.AlignVCenter)
        return lbl

    def _ensure_script_info(self) -> QLabel:
        if self._script_info is None:
            self._script_info = self._header_label("toolScriptInfo", self._stats or self._status)
        return self._script_info

    def _ensure_stats(self) -> QLabel:
        if self._stats is None:
            self._stats = self._header_label("toolStats", self._status)
            self._stats.setTextFormat(Qt.RichText)
        return self._stats

    def _ensure_clock(self) -> QLabel:
        if self._clock is None:
            self._clock = self._header_label("toolClock", self._chev)
        return self._clock

    def _ensure_tail(self):
        if self._tail is None:
            self._tail = _ElidedLabel()
            self._tail.setObjectName("toolTail")
            lay = self.layout()
            # 展开区（有的话）在头和它之间，改动的文件在它下面
            lay.insertWidget(lay.indexOf(self._files) if self._files is not None else lay.count(),
                             self._tail)
        return self._tail

    def _ensure_files(self) -> QLabel:
        if self._files is None:
            self._files = QLabel()
            self._files.setObjectName("toolFiles")
            self._files.setTextFormat(Qt.RichText)
            self._files.setWordWrap(True)
            self._files.linkActivated.connect(self.file_clicked)
            self.layout().addWidget(self._files)
        return self._files

    def _sync_script_info(self):
        has = bool(self._script)
        if not has and self._script_info is None:
            return                   # 没交脚本：对象占满剩下的宽度（建卡时就是这样摆的）
        n = len(self._script.splitlines()) if has else 0
        info = self._ensure_script_info()
        info.setText(f"脚本 {n} 行" if has else "")
        info.setVisible(has)
        # 有脚本时命令按自己的宽度占位（放不下才省略），「脚本 N 行」紧跟在后面，剩下的空着
        self._summary.setSizePolicy(QSizePolicy.Preferred if has else QSizePolicy.Ignored,
                                    QSizePolicy.Fixed)
        self._hlay.setStretchFactor(self._summary, 0 if has else 1)
        self._hlay.setStretchFactor(info, 1 if has else 0)

    def script_text(self) -> str:
        return self._script

    def set_stats(self, text: str):
        """改文件的增删行数（tool_stat）：+N 绿、−M 红，其余（「48 行」）灰。"""
        parts = []
        for tok in (text or "").split():
            color = _P["ok"] if tok.startswith("+") else _P["bad"] if tok.startswith("−") else _P["muted"]
            parts.append(f'<span style="color:{color}">{_esc(tok)}</span>')
        if not parts and self._stats is None:
            return
        stats = self._ensure_stats()
        stats.setText("&nbsp;".join(parts))
        stats.setVisible(bool(parts))

    def append_output(self, text: str):
        """工具输出（流式命令逐行来，其余工具一次来）：存成文字；展开着就同时进输出框。"""
        raw = text or ""
        if not raw.strip():
            return
        tone, label = _tool_outcome(raw)
        if tone and (self.tone != "bad" or tone == "bad"):
            self.tone, self.status_text = ("" if tone == "ok" else tone), label
        chunk = strip_leading_emoji(raw).strip("\n")
        new_lines = chunk.split("\n")
        self._lines.extend(new_lines)
        if self._out is not None:
            cur = self._out.textCursor()
            cur.movePosition(QTextCursor.End)
            if self._out_lines:
                cur.insertText("\n")
            cur.insertText(chunk)
        lines = [ln.strip() for ln in new_lines if ln.strip()]
        self._out_lines += len(lines)
        self._chunks += 1
        if lines:
            self._first_line = self._first_line or lines[0]
            self._last_line = lines[-1]
        self._sync()
        self.state_changed.emit()

    def set_changed_files(self, paths: list):
        """列出这条命令运行期间变了的文件：前几个是可点的文件名，全部的在悬停提示里。"""
        paths = [p for p in paths if p]
        if not paths:
            return
        links = "、".join(
            f'<a href="{_esc(p)}" style="color:{_P["indigo"]}; text-decoration:none;">'
            f'{_esc(p.rsplit("/", 1)[-1])}</a>'
            for p in paths[:self._FILES_MAX_LINKS])
        more = f" 等 {len(paths)} 个" if len(paths) > self._FILES_MAX_LINKS else ""
        files = self._ensure_files()
        files.setText(f"改动了 {len(paths)} 个文件：{links}{more}")
        files.setToolTip("<p>这条命令运行期间项目里变了的文件（点文件名看差异）：<br>"
                         + "<br>".join(_esc(p) for p in paths) + "</p>")
        files.setVisible(True)

    def changed_files_text(self) -> str:
        files = self._files
        return files.text() if files is not None and files.isVisibleTo(self) else ""

    def output_text(self) -> str:
        return "\n".join(self._lines)

    # ── 走秒 ──
    def start_clock(self):
        """工具开始执行（只有实时运行才走秒，重绘历史不走）。"""
        self._t0 = time.monotonic()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)

    def stop_clock(self):
        """工具结束或用户停止：去掉转圈和走秒（总用时算进组头）。"""
        if self._clock_timer is None:
            return
        self._clock_timer.stop()
        self._clock_timer = None
        self.duration = time.monotonic() - self._t0
        if self._spinner is not None:
            self._spinner.setParent(None)
            self._spinner.deleteLater()
            self._spinner = None
            self._icon.setVisible(True)
        if self._clock is not None:
            self._clock.setVisible(False)
        self._sync()
        self.state_changed.emit()

    def is_running(self) -> bool:
        return self._clock_timer is not None

    def clock_text(self) -> str:
        clock = self._clock
        return "" if clock is None or clock.isHidden() else clock.text()

    def _tick_clock(self):
        secs = time.monotonic() - self._t0
        if secs < self.CLOCK_AFTER_S:
            return
        if self._spinner is None:
            from .widgets import LoadingSpinner
            self._spinner = LoadingSpinner(size=14, color=_P["indigo"])
            self._hlay.insertWidget(self._hlay.indexOf(self._icon), self._spinner, 0, Qt.AlignVCenter)
            self._icon.setVisible(False)
            self._ensure_clock().setVisible(True)
            self._sync()
        self._clock.setText(f"运行中 {_fmt_secs(secs)}")

    # ── 展开 ──
    def is_open(self) -> bool:
        return self._open

    def _expandable(self) -> bool:
        return self._out_lines > 0 or self._args_needed or bool(self._script)

    def toggle(self):
        if not self._expandable():
            return
        _hold_scroll(self)
        self._open = not self._open
        if self._open and self._body is None:
            self._build_body()
        self._sync()
        _settle_layout(self)

    def _on_head_clicked(self, pos):
        # 改文件那一行：点文件名看差异（只有一个路径时；「a → b」这种不是一个文件）
        target = self._summary.full()
        if (self.kind == "edit" and target and "→" not in target
                and self._summary.geometry().contains(pos)):
            self.file_clicked.emit(target)
            return
        self.toggle()

    def _set_hover(self, on):
        self._hover = on
        self._sync_chevron()

    def ensure_body(self):
        """展开区还没建就建好（保持当前的展开 / 收起状态）。搜索要搜到参数时用。"""
        if self._body is None:
            self._build_body()
            self._sync()

    def _build_body(self):
        """第一次展开才建：参数（多行时）+ 输出框。"""
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(self._BODY_INDENT, 2, 0, 6)
        bl.setSpacing(6)
        self._args = QLabel()
        self._args.setWordWrap(True)
        self._args.setTextFormat(Qt.RichText)
        self._args.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._args.setObjectName("toolArgs")
        if self._args_needed:
            self._args.setText(highlight_json(self._detail))
        bl.addWidget(self._args)
        if self._script:
            self._ensure_script_widgets()
            self._fill_script()
        self._out = QPlainTextEdit()
        self._out.setObjectName("toolOut")
        self._out.setReadOnly(True)
        self._out.setFrameShape(QFrame.NoFrame)
        self._out.setFont(mono_font(12))
        self._out.setMaximumBlockCount(self._OUT_MAX_BLOCKS)
        self._out.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._out.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # 放得下时不出滚动条，见 _fit_output
        self._out.setPlainText("\n".join(self._lines))
        # 自动换行后的行数变了就重新定高：内容追加时文档会通知；展开后有了真实宽度、
        # 窗口变宽变窄时它不一定通知，所以输出框显示出来和宽度变化时也重算一次
        self._out.document().documentLayout().documentSizeChanged.connect(lambda _size: self._fit_output())
        self._out.installEventFilter(self)
        bl.addWidget(self._out)
        self.layout().insertWidget(1, self._body)

    def _make_box(self, name):
        box = QPlainTextEdit()
        box.setObjectName(name)
        box.setReadOnly(True)
        box.setFrameShape(QFrame.NoFrame)
        box.setFont(mono_font(12))
        box.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        box.document().documentLayout().documentSizeChanged.connect(
            lambda _size, b=box: self._fit_box(b))
        box.installEventFilter(self)
        return box

    def _ensure_script_widgets(self):
        """交了脚本才建：「脚本 · Python」+ 脚本框（和输出框同一个样式、行距，最多露 16 行），
        输出框上面相应标「输出」——两个框叠在一起时分得开。没交脚本的命令一个都不建
        （重画长会话时每张卡多建几个藏着的控件，正是早先「点开才建输出框」要省掉的）。"""
        if self._script_box is not None:
            return
        bl = self._body.layout()
        at = bl.indexOf(self._args) + 1
        self._script_label = QLabel()
        self._script_label.setProperty("mv", "toolBoxLabel")
        self._script_box = self._make_box("toolScript")
        self._out_label = QLabel("输出")
        self._out_label.setProperty("mv", "toolBoxLabel")
        for i, w in enumerate((self._script_label, self._script_box, self._out_label)):
            bl.insertWidget(at + i, w)

    def _fill_script(self):
        lang = {"python": "Python", "javascript": "JavaScript"}.get(self._script_lang, "")
        self._script_label.setText(f"脚本 · {lang}" if lang else "脚本")
        box = self._script_box
        box.setPlainText(self._script)
        box._hl = _ScriptHighlighter(box.document(), self._script, self._script_lang)

    def eventFilter(self, obj, event):
        if obj in (self._out, self._script_box) and obj is not None and (
                event.type() == QEvent.Show or (
                event.type() == QEvent.Resize and obj.width() != getattr(obj, "_fitted_w", -1))):
            # 等这一轮布局排完（换行按新宽度算好）再量；显示出来总要重量
            QTimer.singleShot(0, self, lambda b=obj: self._fit_box(b))
        return super().eventFilter(obj, event)

    def _fit_box(self, box):
        if box is self._out:
            self._fit_output()
        elif box is not None and box is self._script_box:
            self._fit_plain(box)

    def _fit_plain(self, box):
        """脚本框定高：同输出框（_fit_output），按换行后的行数，最多 _OUT_MAX_LINES 行，多了在框里滚。"""
        box._fitted_w = box.width()
        doc = box.document()
        lines = max(1, int(round(doc.size().height())))
        shown = min(lines, self._OUT_MAX_LINES)
        line_h = QFontMetricsF(box.font()).lineSpacing()
        # 余量 4 只给「全放得下」时防最后一行被切；要滚动时不留，不然框底露出下一行的上半截
        chrome = doc.documentMargin() * 2 + 8 + 2 + (0 if lines > self._OUT_MAX_LINES else 4)
        box.setFixedHeight((math.floor if lines > self._OUT_MAX_LINES else math.ceil)(shown * line_h + chrome))
        box.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if lines > self._OUT_MAX_LINES else Qt.ScrollBarAlwaysOff)

    def _fit_output(self):
        """输出框按【自动换行后】的行数定高，最多 _OUT_MAX_LINES 行；超过才出滚动条。

        QPlainTextEdit 的文档高度单位就是行（含换行折出来的行）。旧算法按段落数 × 行高 + 18：
        18 正好等于边框、内边距和文档边距之和，一点余量都没有，只有一行时也会溢出一两个像素，
        冒出一根矮到只剩两个箭头的滚动条；长行折行后更是直接被截。"""
        if self._out is None:
            return
        self._fitted_width = self._out._fitted_w = self._out.width()
        doc = self._out.document()
        lines = max(1, int(round(doc.size().height())))
        shown = min(lines, self._OUT_MAX_LINES)
        # 行高按小数算：高 DPI 下实际行高常是 14.3 这样的值，按整数乘十几行会差出几个像素
        line_h = QFontMetricsF(self._out.font()).lineSpacing()
        # 文档边距 + 上下内边距 + 边框 + 余量；余量只给「全放得下」时防最后一行被切，要滚动时
        # 不留——不然框底露出第 17 行的上半截
        scrolls = lines > self._OUT_MAX_LINES
        chrome = doc.documentMargin() * 2 + 8 + 2 + (0 if scrolls else 4)
        self._out.setFixedHeight((math.floor if scrolls else math.ceil)(shown * line_h + chrome))
        self._out.setVerticalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if lines > self._OUT_MAX_LINES else Qt.ScrollBarAlwaysOff)
        if lines <= self._OUT_MAX_LINES:
            QTimer.singleShot(0, self, self._grow_if_cut)

    def _grow_if_cut(self):
        """核对一次：本该全部露出来却还有行在框外（滚动范围不为 0），就把差的几行补上。"""
        if self._out is None:
            return
        hidden = self._out.verticalScrollBar().maximum()
        if hidden > 0 and self._out.isVisible():
            line_h = QFontMetricsF(self._out.font()).lineSpacing()
            self._out.setFixedHeight(self._out.height() + math.ceil(hidden * line_h))

    # ── 外观 ──
    def _paint_icon(self):
        color = (self._icon_color if self._icon_color and not self.tone else
                 _P["bad"] if self.tone == "bad" else
                 _P["orange"] if self.tone == "warn" else
                 _P["indigo"] if self.kind == "edit" else _P["muted"])
        if color != getattr(self, "_icon_painted", None):
            self._icon_painted = color
            self._icon.setPixmap(_icon(self._icon_svg, color, 15))

    def _sync_chevron(self):
        show = self._expandable() and (self._open or self._hover)
        if show:
            svg = SVG_CHEVRON_UP if self._open else SVG_CHEVRON_DOWN
            if svg != getattr(self, "_chev_svg", None):
                self._chev_svg = svg
                self._chev.setPixmap(_icon(svg, _P["muted"], 12))
        self._chev.setVisible(show)

    def _sync(self):
        expandable = self._expandable()
        running = self._spinner is not None
        self._paint_icon()
        has_status = bool(self.tone and self.status_text) and not running
        self._status.setText(self.status_text if has_status else "")
        # 样式属性变了才重新套样式（unpolish / polish 不便宜：重画长会话时每张卡要 _sync 三次）
        _set_style_prop(self._status, "tone", self.tone)
        self._status.setVisible(has_status)
        if self._body is not None:
            self._body.setVisible(self._open and expandable)
            self._args.setVisible(self._open and self._args_needed)
            self._out.setVisible(self._open and self._out_lines > 0)
            if self._script_box is not None:
                has_script = bool(self._script)
                self._script_label.setVisible(self._open and has_script)
                self._script_box.setVisible(self._open and has_script)
                self._out_label.setVisible(self._open and has_script and self._out_lines > 0)
        # 运行中露最新一行输出；跑完收掉（结果看右边的状态，要看全文就展开）
        show_tail = running and self._out_lines > 0 and not self._open
        if show_tail:
            self._ensure_tail().set_full(self._last_line)
        if self._tail is not None:
            self._tail.setVisible(show_tail)
        if _set_prop(self, "open", "true" if self._open else "false"):
            self.header.style().unpolish(self.header)
            self.header.style().polish(self.header)
        self._sync_chevron()


class _GroupHead(QWidget):
    """组头：箭头 + 「读取了 3 个文件，运行了 2 条命令」+「· 1 条失败」+「· 41 秒」。"""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolGroupHead")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 0, 8, 0)
        h.setSpacing(6)
        self.chev = QLabel()
        self.chev.setFixedSize(12, 12)
        h.addWidget(self.chev, 0, Qt.AlignVCenter)
        self.summary = QLabel()
        self.summary.setProperty("mv", "groupSummary")
        h.addWidget(self.summary, 0, Qt.AlignVCenter)
        self.fail = QLabel()
        self.fail.setProperty("mv", "groupFail")
        h.addWidget(self.fail, 0, Qt.AlignVCenter)
        self.dur = QLabel()
        self.dur.setProperty("mv", "groupDur")
        h.addWidget(self.dur, 0, Qt.AlignVCenter)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class _ToolGroup(QWidget):
    """连续的工具调用（改文件的除外）收成一组。一两条直接平铺；第三条起出组头、可折叠。

    实时运行时组是展开的，看得到一条条在跑；这一组结束（后面来了正文 / 思考 / 改文件……）
    就收起。有失败的组不自动收，失败的那一行得看得到。重画历史时组直接按结束了处理。"""
    FLAT_MAX = 2
    _PHRASES = (("list", "列了 {} 个目录"), ("read", "读取了 {} 个文件"), ("search", "搜索了 {} 次"),
                ("cmd", "运行了 {} 条命令"), ("web", "查了 {} 次网页"),
                ("browser", "在浏览器里操作了 {} 次"))

    def __init__(self, live=False, parent=None):
        super().__init__(parent)
        self.rows = []
        self._live = live
        self._closed = not live
        self._user_open = None
        self._t0 = time.monotonic() if live else None
        self._t1 = None
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        self.head = _GroupHead(self)
        self.head.clicked.connect(self.toggle)
        self.head.setVisible(False)
        v.addWidget(self.head)
        self.rows_box = QWidget()
        self.rows_box.setObjectName("toolGroupRows")
        self.rows_box.setAttribute(Qt.WA_StyledBackground, True)
        self._rl = QVBoxLayout(self.rows_box)
        self._rl.setContentsMargins(0, 0, 0, 0)
        self._rl.setSpacing(2)
        v.addWidget(self.rows_box)

    def add_row(self, row):
        self.rows.append(row)
        self._rl.addWidget(row)
        row.state_changed.connect(self.refresh)
        self.refresh()

    def framed(self) -> bool:
        return len(self.rows) > self.FLAT_MAX

    def failed_count(self) -> int:
        return sum(r.tone == "bad" for r in self.rows)

    def node_tone(self) -> str:
        return "bad" if self.failed_count() else ""

    def header_center(self, _w=None) -> int:
        if self.framed():
            return self.head.height() // 2
        return self.rows[0].header_center() if self.rows else 14

    def is_open(self) -> bool:
        if self._user_open is not None:
            return self._user_open
        return (not self._closed) or self.failed_count() > 0

    def finish(self):
        """这一组结束了：后面来了别的块（正文、思考、改文件……）或这一轮跑完。
        （不叫 close：那是 QWidget 自己的方法，Qt 内部也会调。）"""
        if self._closed:
            return
        self._closed = True
        self._t1 = time.monotonic()
        self.refresh()

    def toggle(self):
        _hold_scroll(self)
        self._user_open = not self.is_open()
        self.refresh()
        _settle_layout(self)

    def summary_text(self) -> str:
        counts = {}
        for r in self.rows:
            counts[r.kind] = counts.get(r.kind, 0) + r.units()
        parts = [tpl.format(counts.pop(k)) for k, tpl in self._PHRASES if k in counts]
        other = sum(counts.values())
        if other:
            parts.append(f"调用了 {other} 次{'其他' if parts else ''}工具")
        return "，".join(parts)

    def refresh(self):
        framed = self.framed()
        self.head.setVisible(framed)
        open_ = self.is_open()
        if framed:
            self.head.chev.setPixmap(_icon(SVG_CHEVRON_DOWN if open_ else SVG_CHEVRON_RIGHT,
                                           _P["muted"], 12))
            self.head.summary.setText(self.summary_text())
            fails = self.failed_count()
            self.head.fail.setText(f"· {fails} 条失败" if fails else "")
            self.head.fail.setVisible(bool(fails))
            secs = None
            if self._closed and self._t0 is not None:
                secs = (self._t1 or time.monotonic()) - self._t0
            self.head.dur.setText(f"· {_fmt_secs(secs)}" if secs is not None and secs >= 1 else "")
            self.head.dur.setVisible(bool(self.head.dur.text()))
        self._rl.setContentsMargins(12 if framed else 0, 0, 0, 0)
        _set_style_prop(self.rows_box, "framed", "true" if framed else "false")
        self.rows_box.setVisible((not framed) or open_)


class Notice(QLabel):
    """程序提示（自动重试、拒绝调用、恢复说明……）：不是工具输出，也不是模型说的话。"""
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("notice")
        self.setWordWrap(True)
        self.setTextFormat(Qt.PlainText)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setAttribute(Qt.WA_StyledBackground, True)
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
        self._title.setProperty("mv", "stepTitle")
        h.addWidget(self._title, 1)
        self._note = QLabel()
        self._note.setProperty("mv", "secondary12")
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


_WAITING_LABEL = "等待响应"
# 一个 chunk 都没收到超过这么久，多半是服务商在排队（DeepSeek 高峰期会让请求排到 10 分钟）
QUEUE_HINT_AFTER_S = 30
QUEUE_HINT = "服务商可能在排队，高峰期常见。可以先停止，过会儿再继续。"


def waiting_phase(text: str) -> tuple[str, bool]:
    """streaming 发来的指示器整句 → (标签, 是不是还没收到任何东西)。

    整句形如「等待响应... (12s)」「模型思考中... (12s)」「🔧 正在生成工具调用 write_file... (3s)」，
    秒数由指示器自己走，这里只取前面的标签。只有「等待响应」才可能是服务商在排队：
    后两种说明 chunk 已经在来了，只是还没有正文。
    """
    label = strip_leading_emoji((text or "").strip())
    label = re.sub(r"(\.\.\.|…)?\s*(\(\d+s\))?$", "", label).strip() or _WAITING_LABEL
    return label, label == _WAITING_LABEL


class WaitingIndicator(QWidget):
    """等待模型响应：转圈 + 「等待响应 N 秒」（自走秒表）。

    一直没收到 chunk 超过 QUEUE_HINT_AFTER_S 秒，下面多一行说服务商可能在排队——
    不然看着像灵犀卡了。思考中 / 生成工具调用阶段 chunk 一直在来，不提示。
    """
    def __init__(self, start_elapsed: int = 0, parent=None, text: str = ""):
        super().__init__(parent)
        self._elapsed = start_elapsed
        self._label, self._may_queue = waiting_phase(text)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        from .widgets import LoadingSpinner
        row.addWidget(LoadingSpinner(size=14, color=_P["indigo"]), 0, Qt.AlignVCenter)
        self._lbl = QLabel()
        self._lbl.setProperty("mv", "secondary13")
        row.addWidget(self._lbl, 0, Qt.AlignVCenter)
        row.addStretch(1)
        v.addLayout(row)
        self._hint = QLabel(QUEUE_HINT)
        self._hint.setWordWrap(True)
        self._hint.setContentsMargins(22, 0, 0, 0)   # 和上面的文字对齐（转圈 14 + 间距 8）
        self._hint.setProperty("mv", "secondary12")
        self._hint.hide()
        v.addWidget(self._hint)
        self._tick()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._bump)
        self._timer.start(1000)

    def set_phase(self, text: str):
        """后台心跳每秒报一次当前阶段；从「等待响应」进到「模型思考中」就收起排队提示。"""
        label, may_queue = waiting_phase(text)
        if (label, may_queue) != (self._label, self._may_queue):
            self._label, self._may_queue = label, may_queue
            self._tick()

    def hint_visible(self) -> bool:
        return not self._hint.isHidden()

    def _tick(self):
        self._lbl.setText(f"{self._label} {self._elapsed} 秒")
        self._hint.setVisible(self._may_queue and self._elapsed >= QUEUE_HINT_AFTER_S)

    def _bump(self):
        self._elapsed += 1
        self._tick()


class _ProcessSummary(QPushButton):
    """收起后的一行：调用了 N 次工具，思考 M 次 ›。整行可点，行尾箭头朝右 = 收着、朝下 = 开着
    （设计稿「消息流整理」③：原来的「展开过程」文字按钮换成箭头）。"""
    def __init__(self, text: str, on_toggle, parent=None):
        super().__init__(parent)
        self.setObjectName("procSummary")
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self._open = False
        self._on_toggle = on_toggle
        self.btn = self              # 兼容旧引用（summary.btn.click()）
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 6, 2)
        h.setSpacing(6)
        self.label = QLabel(text)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        h.addWidget(self.label, 0, Qt.AlignVCenter)
        self._chev = QLabel()
        self._chev.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._chev.setFixedSize(12, 12)
        h.addWidget(self._chev, 0, Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.clicked.connect(self._toggle)
        self._sync()

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()

    def is_open(self) -> bool:
        return self._open

    def _sync(self):
        self._chev.setPixmap(_icon(SVG_CHEVRON_DOWN if self._open else SVG_CHEVRON_RIGHT,
                                   _P["muted2"], 12))
        tip = "收起过程" if self._open else "展开过程"
        self.setToolTip(tip)
        self.setAccessibleName(f"{self.label.text()}，{tip}")

    def _toggle(self):
        _hold_scroll(self)
        self._open = not self._open
        self._sync()
        self._on_toggle(self._open)
        _settle_layout(self)


class RecoveryNote(QWidget):
    """继续任务 / 中断恢复的说明（设计稿「消息流整理」②）：线上一个小圆点 + 一行灰字；
    现场变化、结果未知的操作这些细节收在「详情」里，点了才显示。

    以前整段画成通栏灰底的程序提示，比用户自己的消息还显眼。"""
    def __init__(self, title: str, details=(), parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        icon = QLabel()
        icon.setPixmap(_icon(SVG_REFRESH, _P["muted2"], 13))
        icon.setFixedSize(13, 13)
        head.addWidget(icon, 0, Qt.AlignVCenter)
        self.title = QLabel(title)
        self.title.setProperty("mv", "recTitle")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.title, 0, Qt.AlignVCenter)
        self.link = QPushButton("详情")
        self.link.setProperty("mv", "recLink")
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.clicked.connect(self.toggle)
        head.addWidget(self.link, 0, Qt.AlignVCenter)
        head.addStretch(1)
        v.addLayout(head)
        self.detail = QLabel()
        self.detail.setProperty("mv", "recDetail")
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setContentsMargins(19, 0, 0, 0)
        v.addWidget(self.detail)
        self._details = []
        self._open = False
        self.add_details(details)

    def add_details(self, lines):
        self._details.extend(ln for ln in lines if ln)
        self.detail.setText("\n".join(self._details))
        self.link.setVisible(bool(self._details))
        self.detail.setVisible(self._open and bool(self._details))

    def is_open(self) -> bool:
        return self._open

    def toggle(self):
        _hold_scroll(self)
        self._open = not self._open
        self.link.setText("收起" if self._open else "详情")
        self.detail.setVisible(self._open and bool(self._details))
        _settle_layout(self)


def _split_recovery_text(text: str):
    """恢复说明 → (标题, 细节行)：第一行去掉开头的表情是标题，其余是细节。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "", []
    return strip_leading_emoji(lines[0]).strip(), lines[1:]


class _HoverButtons:
    """一排幽灵按钮的显隐（设计稿「消息流整理」①⑤）：pinned 时常显，否则鼠标移到宿主
    （用户消息 / 那段回复）上才出现。藏起来也占着位置——显隐时旁边的文字不重排、
    下面的内容不跳。"""
    def __init__(self, buttons, pinned=False):
        self.buttons = list(buttons)
        self.pinned = pinned
        self.hovered = False
        for b in self.buttons:
            sp = b.sizePolicy()
            sp.setRetainSizeWhenHidden(True)
            b.setSizePolicy(sp)
        self.apply()

    def apply(self):
        on = self.pinned or self.hovered
        for b in self.buttons:
            if not _deleted(b):
                b.setVisible(on)

    def set_hovered(self, hovered):
        if hovered != self.hovered:
            self.hovered = hovered
            self.apply()

    def set_pinned(self, pinned):
        if pinned != self.pinned:
            self.pinned = pinned
            self.apply()


class UserTurn(QWidget):
    """用户的一句话：线上一个空心圆，右边是浅靛蓝底的气泡（宽度贴合文字，不再另加"你"标签）。
    只放原文时和 AI 的正文、过程说明长得一样，一轮里分不出哪句是用户说的。"""
    _LINE_HEIGHT = 1.6

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.bubble = QFrame()
        self.bubble.setObjectName("userBubble")
        self.bubble.setAttribute(Qt.WA_StyledBackground, True)
        v = QVBoxLayout(self.bubble)
        # 行高放大多出来的那截，Qt 加在每一行下面，最后一行也一样：照常上下各留 8 的话，
        # 底下比顶上多白一截，文字看着偏上。把这截从底部挪到顶部，文字上下就对称了。
        # （线上的空心圆对的是正文第一行，add_node 锚在 body 上，不受影响。）
        extra = round((self._LINE_HEIGHT - 1) * QFontMetricsF(ui_font(BODY)).height())
        bottom = max(0, 8 - extra)
        v.setContentsMargins(14, bottom + extra, 14, bottom)
        v.setSpacing(0)
        # 引用（设计稿「消息区：引用」）：开头连续的「> 」行画成小一号的引用块，在正文上面
        self._quotes_box = QVBoxLayout()
        self._quotes_box.setContentsMargins(0, 0, 0, 0)
        self._quotes_box.setSpacing(6)
        v.addLayout(self._quotes_box)
        self._quotes = []
        self.body = _FitLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setProperty("mv", "userBody")
        v.addWidget(self.body)
        row.addWidget(self.bubble, 0)
        row.addStretch(1)
        self._row = row
        self._text = ""
        self.actions = None          # _HoverButtons：编辑 / 回退 / 分叉，悬停才出现

    def set_actions(self, widget, buttons):
        """操作按钮放在气泡右边同一行、贴底（不另占一行），鼠标移到这条消息上才出现。"""
        self._row.insertWidget(1, widget, 0, Qt.AlignBottom)
        self.actions = _HoverButtons(buttons)
        self.actions.set_hovered(self.underMouse())

    def enterEvent(self, event):
        if self.actions is not None:
            self.actions.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.actions is not None:
            self.actions.set_hovered(False)
        super().leaveEvent(event)

    def append(self, text):
        self._text += text
        self.set_text(self._text)

    def set_text(self, text):
        """用户原文：开头的引用拆成引用块，其余是正文（@文件 靛蓝加粗）。"""
        from .quotes import split_leading_quotes
        self._text = text or ""
        quotes, rest = split_leading_quotes(self._text)
        self._set_quotes(quotes)
        self.body.setVisible(bool(rest.strip()) or not quotes)
        self.set_html(_user_html(rest))

    def quotes(self) -> list:
        return list(self._quotes)

    def _set_quotes(self, quotes):
        if quotes == self._quotes:
            return
        from .quotes import QuoteBlock
        while self._quotes_box.count():
            w = self._quotes_box.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._quotes = list(quotes)
        font = ui_font(13)
        for q in quotes:
            self._quotes_box.addWidget(QuoteBlock(q, bar=_P["quote_bar"], color=_P["quote_text"],
                                                  font=font))
        # 引用和下面的正文之间空一点（只有引用时不留）
        self._quotes_box.setContentsMargins(0, 0, 0, 6 if quotes else 0)

    def set_html(self, html):
        # 发送时文本末尾带着换行（旧分隔约定）：去掉，不然用户消息下面空出两行
        html = re.sub(r"^(?:\s*<br\s*/?>)+|(?:<br\s*/?>\s*)+$", "", (html or "").strip())
        self.body.setText(
            f'<p style="margin:0; line-height:{round(self._LINE_HEIGHT * 100)}%;">{html}</p>')


def _user_html(text: str) -> str:
    """用户正文 → 气泡里的富文本：@文件引用靛蓝加粗，其余转义，换行变 <br>。"""
    parts, pos = [], 0
    for m in re.finditer(r'(?<!\S)@[^\s@]+', text or ""):
        if m.start() > pos:
            parts.append(_esc(text[pos:m.start()]))
        parts.append(f'<b style="color:{_P["indigo"]};">{_esc(m.group(0))}</b>')
        pos = m.end()
    parts.append(_esc((text or "")[pos:]))
    return "".join(parts).replace("\n", "<br>")


# 流式时按空行拆段落（见 AssistantTurn.append_body）；拆开的段落之间留的空白约等于原来的一个空行
_PARA_SPLIT = re.compile(r"\n[ \t]*\n+")
_STREAM_PARA_GAP = 14


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

    file_link_clicked = Signal(str)   # 点了正文里的文件链接（href 原样转给宿主）

    def __init__(self, model_name="", parent=None, *, mode="", live=False, round_=None):
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(0)
        self._round = round_
        self._live = live
        self.meta = None
        if model_name or mode or live:
            self.meta = MetaLine(model_name, mode, live)
            self._v.addWidget(self.meta)
        # 当前流式状态
        self._cur_body = None          # 正在流式的正文 BodyText（累加纯文本）
        self._cur_body_text = ""
        self._stream_parts = []        # 流式时第二段起的段落各占一个 BodyText（见 append_body）
        self._cur_think = None         # 当前 ThinkingChip（流式累加）
        self._cur_think_text = ""
        self._cur_tool = None          # 当前工具行（还在接收输出）
        self._cur_group = None         # 正在收工具行的组（来了别的块就结束）
        self._waiting = None           # WaitingIndicator
        self._process = []             # 可收起的过程块（思考、工具、步骤分段）
        self._steps = {}               # 步号 → StepHeader
        self._summary = None
        self.actions = None            # _HoverButtons：复制 / 重新生成（不是最新一条时悬停才出现）

    def enterEvent(self, event):
        if self.actions is not None:
            self.actions.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.actions is not None:
            self.actions.set_hovered(False)
        super().leaveEvent(event)

    def _add(self, w, top_gap=14, node=None, process=False, **info):
        if not isinstance(w, _ToolGroup):
            self._close_group()          # 来了别的块：这一组工具调用结束
        if w.objectName() != "msgActions":
            self._drop_dangling_actions()
        if self._v.count() == 0:
            top_gap = 0
        blk = _Block(w, top_gap)
        self._v.addWidget(blk)
        if node and self._round is not None:
            self._round.add_node(w, node, **info)
        if process:
            self._process.append(blk)
        return w

    def _drop_dangling_actions(self):
        """上一段回复后面又来了别的块（工具调用、思考、提示……）：它只是过渡回复，不是最终
        回答，底下那排复制 / 重新生成拿掉。

        这排按钮不是最新一条时平时藏着、但照样占着位置（免得悬停显示时内容往下跳），
        挂在过渡回复底下就成了一块看不见的空白——中间回复和工具行之间空出一大截（用户实测）。
        实时（中间轮也走 render_final_markdown）和重绘历史都会给每段文字挂按钮，在这里统一收。"""
        if self._v.count() == 0:
            return
        last = self._v.itemAt(self._v.count() - 1).widget()
        if not (isinstance(last, _Block) and last.inner.objectName() == "msgActions"):
            return
        self._v.removeWidget(last)
        last.setParent(None)
        last.deleteLater()
        self.actions = None

    # —— 块级操作 ——
    def _close_group(self):
        if self._cur_group is not None:
            self._cur_group.finish()
            self._cur_group = None

    def end_body(self):
        self._cur_body = None
        self._cur_body_text = ""
        self._stream_parts = []        # 没走 finalize 就结束的（停止 / 报错）：分段留着照样能看

    def _drop_stream_parts(self):
        """流式时拆出来的段落块拿掉：正文合回第一块，换成渲染好的 markdown。"""
        for part in self._stream_parts:
            blk = part.parentWidget()
            if blk is not None and not _deleted(blk):
                self._v.removeWidget(blk)
                blk.setParent(None)
                blk.deleteLater()
        self._stream_parts = []

    def end_tool(self):
        self.stop_tool_clock()
        self._cur_tool = None

    def stop_tool_clock(self):
        if self._cur_tool is not None:
            self._cur_tool.stop_clock()

    def _new_body(self):
        """新起一个正文块，并把里面的文件链接转发给宿主。"""
        body = self._add(BodyText(""), top_gap=12)
        body.link_clicked.connect(self.file_link_clicked)
        return body

    def append_body(self, text):
        self.clear_waiting()
        self._close_think()
        self.end_tool()
        if self._cur_body is None:
            self._cur_body = self._new_body()
        self._cur_body_text += text
        # 首尾的换行不画：模型常写「我来查一下：\n\n」再调工具，两个换行变成两行 <br>，
        # 这段过渡回复和下面那行工具之间平白空出两行（中间的换行照常保留）
        shown = self._cur_body_text.strip("\r\n")
        # 按段落拆开，每段一个 BodyText，只改最后一段（和刚写完的那段）。以前整段回复塞进一个
        # QLabel，每来几个字就把全文重新排版好几遍：九千字时每段要 140ms，越写越卡（实测）。
        # 回复结束（finalize_markdown）再合回一个渲染好的 markdown 块
        paras = _PARA_SPLIT.split(shown)
        labels = [self._cur_body] + self._stream_parts
        while len(labels) < len(paras):
            part = self._add(BodyText(""), top_gap=_STREAM_PARA_GAP)
            part.link_clicked.connect(self.file_link_clicked)
            self._stream_parts.append(part)
            labels.append(part)
        for label, para in zip(labels[max(0, len(paras) - 2):len(paras)],
                               paras[max(0, len(paras) - 2):]):
            html = _esc(para).replace("\n", "<br>")
            if label.text() != html:
                label.setText(html)

    def finalize_markdown(self, html, md=None):
        """流式结束:把当前正文替换成渲染好的 markdown 富文本（md 是源文，右键「复制为 Markdown」用）。"""
        self._close_think()
        if self._cur_body is None:
            self._cur_body = self._new_body()
        self._drop_stream_parts()
        self._cur_body.setText(html)
        if md:
            self._cur_body.setProperty("lx_markdown", md)
        self.end_body()

    def think_start(self):
        self.clear_waiting()
        self.end_body()
        self._close_think()
        self.stop_tool_clock()   # Claude Code 模式工具结果之后不发 reset_ai_reply：又开始思考，工具就跑完了
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
        self.end_tool()        # 上一张卡（Claude Code 模式不发 reset_ai_reply）不再走秒
        # 步骤类工具用橙星，其余按工具种类取图标
        if icon_svg is None and any(k in name for k in ("步骤", "step")):
            icon_svg, icon_color = SVG_STAR, _P["orange"]
        row = ToolCallCard(name, code_text, icon_svg, icon_color)
        if self._live:
            row.start_clock()
        if row.kind == "edit":
            # 改文件单独一行，不进组：改了什么要一眼看得到
            self._cur_tool = self._add(row, top_gap=6, node="tool", process=True, dy=row.header_center)
            return self._cur_tool
        if self._cur_group is None:
            group = _ToolGroup(live=self._live)
            self._add(group, top_gap=6, node="tool", process=True, dy=group.header_center)
            self._cur_group = group
        self._cur_group.add_row(row)
        self._cur_tool = row
        return row

    def tool_stat(self, text):
        """改文件的增删行数（tool_stat）：进当前工具行。"""
        if self._cur_tool is not None:
            self._cur_tool.set_stats(text)

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

    def tool_files(self, text):
        """命令改动的文件（每行一个路径）：进当前工具卡；返回那张卡，没有当前卡返回 None。"""
        if self._cur_tool is None:
            return None
        self._cur_tool.set_changed_files([ln.strip() for ln in (text or "").splitlines()])
        return self._cur_tool

    def notice(self, text):
        self.clear_waiting()
        self.end_body()
        tone = _notice_tone(text)
        self._add(Notice(strip_leading_emoji(text.strip())), top_gap=10, node="dot", color=tone, dy=15)

    # 兼容旧名：一段独立的说明文字
    result_block = notice

    def recovery_note(self, text):
        """继续任务 / 中断恢复的说明：一行小字，细节收进「详情」。

        实时继续时先到一行「继续任务 · 使用模型…」，紧接着是恢复检查的说明（第一行又是
        「继续任务」）：并进上一条，标题不重复，细节接在后面。"""
        self.clear_waiting()
        self.end_body()
        title, details = _split_recovery_text(text)
        if not title:
            return None
        last = self._v.itemAt(self._v.count() - 1).widget() if self._v.count() else None
        prev = last.inner if isinstance(last, _Block) and isinstance(last.inner, RecoveryNote) else None
        if prev is not None:
            if not prev.title.text().startswith(title):
                details = [title] + details
            prev.add_details(details)
            return prev
        return self._add(RecoveryNote(title, details), top_gap=10, node="dot", color="muted3", dy=8)

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

    def show_waiting(self, text: str = ""):
        self.clear_waiting()
        self._waiting = self._add(WaitingIndicator(0, text=text), top_gap=14)

    def update_waiting(self, text: str):
        if self._waiting is not None:
            self._waiting.set_phase(text)

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

    def _answer_blocks(self, blocks):
        """最后一段回复（最终回答）和它下面那排复制 / 重新生成按钮。"""
        bodies = [i for i, b in enumerate(blocks) if isinstance(b.inner, BodyText)]
        if not bodies:
            return []
        i = bodies[-1]
        answer = [blocks[i]]
        if i + 1 < len(blocks) and blocks[i + 1].inner.objectName() == "msgActions":
            answer.append(blocks[i + 1])
        return answer

    def collapse_process(self, *, need_answer=False):
        """把思考、工具、步骤分段和中间的过渡回复收成一行摘要，只露最后一段回复（最终回答）；
        提示和结果卡照常显示。

        need_answer：最后一块是回答才收——运行结束时用；停在工具上或报了错的那一轮先不收，
        要看得到停在了哪。"""
        if self._summary is not None:
            return
        layout = [self._v.itemAt(i).widget() for i in range(self._v.count())]
        layout = [b for b in layout if isinstance(b, _Block) and not _deleted(b)]
        answer = self._answer_blocks(layout)
        if need_answer and (not layout or layout[-1] not in answer):
            return
        process = [b for b in self._process if not _deleted(b)]
        tools = sum(len(b.inner.rows) if isinstance(b.inner, _ToolGroup) else
                    int(isinstance(b.inner, ToolCallCard)) for b in process)
        thinks = sum(isinstance(b.inner, ThinkingChip) for b in process)
        if not tools and not thinks:
            return
        between = [b for b in layout if b not in answer and b not in process
                   and (isinstance(b.inner, BodyText) or b.inner.objectName() == "msgActions")]
        blocks = sorted(process + between, key=self._v.indexOf)
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
            self._round.add_node(summary, "dot", color="muted2",
                                 dy=lambda w: w.height() // 2)
        self._summary = summary
        show(False)


def _hold_scroll(widget):
    """用户点了展开 / 收起：所在消息流停在原处（见 MessageView.hold_position）。"""
    w = widget.parentWidget()
    while w is not None and not isinstance(w, MessageView):
        w = w.parentWidget()
    if w is not None:
        w.hold_position()


def _settle_layout(widget):
    """展开 / 收起之后，从消息流最外层往里把这一串布局同步排一遍。

    不排的话 Qt 是从里往外各排各的：里层先按外层还没缩的旧高度排，多出来的那截平均分给
    兄弟块（用户气泡、正文、按钮行都被拉高），外层下一刻才缩回去——收起时整片闪一下。
    外层先排，里层拿到的就是新高度；消息列表底下有弹性空白，吃掉列表本身还没缩的那截。"""
    chain = []
    w = widget
    while w is not None and not isinstance(w, MessageView):
        chain.append(w)
        w = w.parentWidget()
    if w is None:
        return
    # 外层这时还没被标成要重排（Qt 要等里层排完才往外传），父布局里也还缓存着子块的旧尺寸，
    # 直接 activate 会原样返回：先由里往外 updateGeometry（清缓存、把父布局标成要重排）
    for x in chain:
        x.updateGeometry()
        # updateGeometry 只让父控件的顶层布局失效；嵌在里面的子布局（气泡里的引用盒 _quotes_box）
        # 还缓存着旧高度，第一遍就按旧的排（Qt 要等这一遍激活时才顺手清掉，第二遍才对）
        lay = x.layout()
        if lay is not None:
            _invalidate_sublayouts(lay)
    # 列表本身也立刻改到新高度（滚动区要等下一轮事件才改）：展开时不先把下面的回答挤扁一下
    if w.widget() in chain:
        w.fit_content()
    for x in reversed(chain):
        lay = x.layout()
        if lay is not None:
            lay.activate()


def _invalidate_sublayouts(lay):
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if sub is not None:
            sub.invalidate()
            _invalidate_sublayouts(sub)


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
        elif isinstance(w, _ToolGroup) and not w.is_open():
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


class _Content(QWidget):
    """消息列表。尺寸由 MessageView.fit_content 定，sizeHint 直接报当前尺寸：默认实现会按
    「建议宽度」把整列排版高度再算一遍，滚动区每次缩放都要问它——布局只缓存一个宽度的高度，
    两个宽度来回算互相冲掉，长对话每次缩放白算几毫秒到几十毫秒。"""
    def sizeHint(self):
        return self.size()


class MessageView(QScrollArea):
    """消息流容器:QScrollArea + 纵向的轮次列表。对外 handle(text, tag) 镜像 show_message 协议,
    finalize_markdown(html) 对应 render_final_markdown,clear()/redraw 复位。"""

    image_clicked = Signal(QPixmap)   # 点击聊天区图片 → 宿主弹放大遮罩(传全分辨率原图)
    file_clicked = Signal(str)        # 点了命令卡上改动的文件 → 宿主在文件面板里打开
    file_link_clicked = Signal(str)   # 点了回复正文里的文件链接（href 原样转给宿主解析）
    # 用户气泡上的时间轴操作：参数是该用户消息在 chat_history 里的绝对下标
    user_edit_requested = Signal(int)     # 编辑这条消息并重发
    user_rewind_requested = Signal(int)   # 回退到这条之前（丢弃它及之后）
    user_fork_requested = Signal(int)     # 从这条之前分叉出新对话
    quote_requested = Signal(str)         # 选中文字后点了浮条上的「引用」

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageView")
        self.setProperty("mvRoot", "true")      # mv_qss 的作用范围（objectName 会被宿主改掉）
        # 列表的宽高自己定（_fit_content），不用 widgetResizable：滚动区自己定高时按【视口宽度】
        # 算排版高度，可列表限了宽、比视口窄——算出来的高度差十几像素（下一轮再补，贴底时跳一下），
        # 而且每次都按两个宽度各排一遍，布局只缓存一个宽度的结果，互相冲掉，长对话一次十几毫秒
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._content = _Content()
        self._content.setObjectName("mvContent")
        self.apply_theme()
        self._col = QVBoxLayout(self._content)
        # 消息栏最多 COLUMN_MAX 宽、居中，窄时两侧留 COLUMN_SIDE：列表本身限宽，由滚动区居中摆放，
        # 不再按视口宽度改左右边距。侧栏开关、拖窗口时只要栏宽没变（窗口比栏宽就不变），
        # 列表只是平移、不用重排——原来改边距每次都把整列重排一遍，长对话一次几十毫秒
        self._col.setContentsMargins(COLUMN_SIDE, 28, COLUMN_SIDE, 28)
        self._col.setSpacing(0)
        self._col.addStretch(1)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWidget(self._content)
        self._content.installEventFilter(self)      # 内容变了（LayoutRequest）→ 重定高度
        self.viewport().installEventFilter(self)    # 滚动条出现 / 消失，视口变窄 → 重定宽度
        self._cur_user = None
        self._cur_assistant = None
        self._cur_round = None
        self._latest_actions = None     # 最新一条回答的复制 / 重新生成（常显）
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
        # 选中文字浮出「引用 · 复制」（设计稿「消息区：引用」①；由 context_menus 的过滤器叫出来）
        from . import context_menus
        from .quotes import SelectionBar
        self.selection_bar = SelectionBar(self)
        self.selection_bar.apply_theme(theme_color)
        self.selection_bar.quote_requested.connect(self.quote_requested)
        sb.valueChanged.connect(lambda _v: self.selection_bar.dismiss())
        context_menus.register_selection_bar(self.selection_bar)

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
        headers = [h for h in self._content.findChildren(StepHeader) if h.n == n and self._alive(h)]
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

        搜的是能选中的文字：用户消息、回复正文、程序提示、思考内容、工具参数，加上用户气泡里的
        引用块（ClampText 自己画字，不是 QLabel）和命令交进去的脚本（脚本框，QPlainTextEdit）。
        按在消息流里出现的先后排。
        命中在收起的过程 / 卡片 / 思考块里时先把它展开；在引用块收起的那几行里时，那一段临时
        展开到全文（跳到别处或关掉搜索收回）。返回 (第几处, 共几处)，没找到是 (0, 0)。"""
        from .quotes import ClampText
        needle = (text or "").strip().lower()
        hits = []
        if needle:
            # 工具参数要展开才生成（见 ToolCallCard）：参数里有要找的字、还没生成的先补建出来
            for row in self._content.findChildren(ToolCallCard):
                if not self._alive(row):
                    continue
                if row._body is None and (
                        (row._args_needed and needle in row._detail.lower())
                        or needle in row.script_text().lower()):
                    row.ensure_body()
            # 一次遍历同时收 QLabel 和引用块：findChildren 是先序深度优先，顺序就是显示的先后
            alive_check = bool([g for g in getattr(self, "_gone", []) if not _deleted(g)])
            for lbl in self._content.findChildren(QWidget):
                if alive_check and not self._alive(lbl):
                    continue
                if isinstance(lbl, ClampText):
                    plain = lbl.text().lower()
                elif isinstance(lbl, QPlainTextEdit) and lbl.objectName() == "toolScript":
                    plain = lbl.toPlainText().lower()
                elif isinstance(lbl, QLabel) and lbl.textInteractionFlags() & Qt.TextSelectableByMouse:
                    plain = _label_plain_text(lbl).lower()
                else:
                    continue
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
        if isinstance(lbl, ClampText):
            was = lbl.is_expanded()
            lbl.show_hit(pos, len(needle))
            if lbl.is_expanded() != was:
                _settle_layout(lbl)   # 变高了：当场排版，别让下面的消息先叠一帧
        elif isinstance(lbl, QPlainTextEdit):
            # 脚本框自己会滚（长脚本只露 16 行）：选中后让框把命中滚进来
            cur = lbl.textCursor()
            cur.setPosition(pos)
            cur.setPosition(pos + len(needle), QTextCursor.KeepAnchor)
            lbl.setTextCursor(cur)
            lbl.ensureCursorVisible()
        else:
            lbl.setSelection(pos, len(needle))
        self._find_label = lbl
        self.hold_position()
        # 展开后的新高度要等布局排完才准，下一轮事件循环再滚
        QTimer.singleShot(0, self, lambda: self._scroll_to_hit(lbl, pos, total))
        return idx + 1, len(hits)

    def _alive(self, w) -> bool:
        """不在刚清掉、还没删完的旧会话里（见 clear）。"""
        gone = [g for g in getattr(self, "_gone", []) if not _deleted(g)]
        self._gone = gone
        return not any(g is w or g.isAncestorOf(w) for g in gone)

    def clear_find(self):
        self._clear_find_selection()
        self._find_state = ("", -1)

    def _clear_find_selection(self):
        from .quotes import ClampText
        lbl = getattr(self, "_find_label", None)
        self._find_label = None
        if lbl is not None and not _deleted(lbl):
            if isinstance(lbl, ClampText):
                was = lbl.is_expanded()
                lbl.clear_hit()
                if was:
                    _settle_layout(lbl)   # 收回三行
            elif isinstance(lbl, QPlainTextEdit):
                cur = lbl.textCursor()
                cur.clearSelection()
                lbl.setTextCursor(cur)
            else:
                lbl.setSelection(0, 0)

    def _scroll_to_hit(self, lbl, pos, total):
        """命中大致在标签里的哪一段就滚到哪：长回复里，滚到标签顶上会看不见命中。"""
        if _deleted(lbl):
            return
        y = lbl.mapTo(self._content, QPoint(0, 0)).y()
        if isinstance(lbl, QPlainTextEdit):
            # 框里已经滚到命中处：按命中在框里的位置算，不按全文比例
            y += lbl.cursorRect().center().y()
        else:
            y += int(lbl.height() * (pos / max(1, total)))
        sb = self.verticalScrollBar()
        sb.setValue(max(0, y - self.viewport().height() // 3))

    def apply_theme(self, extra_qss=""):
        """背景跟当前主题走。extra_qss 是宿主补充的滚动条等样式——setStyleSheet 会整体替换，
        所以必须和背景一起设，分开设的话后设的会把背景冲掉（深色下聊天区一直是白底就是这个原因）。
        已经画出来的消息块不会自己变色，切主题后由 ChatUI 重绘消息流。"""
        self.setStyleSheet(mv_qss() + extra_qss)
        bar = getattr(self, "selection_bar", None)
        if bar is not None:
            bar.apply_theme(theme_color)

    # 宽度变化：视口一变就当场 fit_content（下面的过滤器，滚动区缩放时视口的 Resize 同步送到），
    # 不改边距——原来先排版后改边距，中间那一帧内容按旧边距铺开：侧栏收起时整列铺到右边、
    # 打开时被挤成窄条（用户实测「闪一下」）
    def eventFilter(self, obj, event):
        t = event.type()
        if (obj is self._content and t == QEvent.LayoutRequest) or (
                obj is self.viewport() and t == QEvent.Resize):
            self.fit_content()
        return super().eventFilter(obj, event)

    def fit_content(self):
        """消息列表定宽定高：宽 = 视口宽，最多 COLUMN_MAX + 两侧留白（居中靠滚动区摆）；
        高 = 按这个宽度排版要的高度，至少铺满视口。当场改，不等下一轮事件——中间那帧
        尺寸不对就会闪：下面的回答被挤扁，或贴底时整片跳一下。"""
        vp = self.viewport()
        if vp.width() <= 0:
            return
        lay = self._col
        width = max(min(vp.width(), COLUMN_MAX + 2 * COLUMN_SIDE), lay.totalMinimumSize().width())
        h = (lay.totalHeightForWidth(width) if lay.hasHeightForWidth()
             else lay.totalSizeHint().height())
        h = max(h, vp.height())
        content = self._content
        if content.width() != width or content.height() != h:
            content.resize(width, h)

    def clear(self):
        # 藏起来再延后删：立即从显示上消失，删本身挪到下一轮事件循环（那时新会话已经画出来了）。
        # 以前先 setParent(None) 摘下来——Qt 把每一轮临时变成顶层窗口，长会话切过去光这一步就
        # 140ms（实测，挪到另一个藏着的父控件下也一样慢）。删之前它们还挂在 _content 下面，
        # 按类型找子控件的地方（搜索、定位步骤）用 _alive 跳过
        self._gone = [g for g in getattr(self, "_gone", []) if not _deleted(g)]
        while self._col.count() > 1:   # 留最后的 stretch
            it = self._col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
                self._gone.append(w)
        self._cur_user = None
        self._cur_assistant = None
        self._cur_round = None
        self._latest_actions = None

    @contextmanager
    def batch(self):
        """一口气画很多轮（重画历史）：期间新加的每一轮先藏着，结束时一起显示。

        直接往显示着的列表里加，每个子控件的「显示」都排进事件队列、各自触发一遍排版，
        长会话里几千个控件逐个走一趟（还要过全局事件过滤器）。藏着建、最后一次显示出来，
        子控件随父控件同步显示、整列只排一次版。期间只用 isVisibleTo 判断显隐（相对本轮），
        收起过程等逻辑不受影响。可嵌套，最外层结束时才显示。"""
        self._batch_depth = getattr(self, "_batch_depth", 0) + 1
        if self._batch_depth == 1:
            self._batched = []
        try:
            yield
        finally:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                pending, self._batched = self._batched, []
                for w in pending:
                    if not _deleted(w):
                        w.show()

    def _add_turn(self, w, top_gap=26):
        if getattr(self, "_batch_depth", 0):
            w.hide()
            self._batched.append(w)
        if self._col.count() > 1 and top_gap and not isinstance(w, _Round):
            self._col.insertSpacing(self._col.count() - 1, top_gap)
        elif self._col.count() > 1 and top_gap:
            # 轮与轮之间：留白 + 一条很淡的分隔线（设计稿「消息流整理」⑥），线从线槽右边起，
            # 一眼看出是新的一问；第二轮的空心圆不再像断开的半截线
            at = self._col.count() - 1          # 在 stretch 前插入
            self._col.insertSpacing(at, top_gap - 10)
            sep = QFrame()
            sep.setObjectName("roundSep")
            sep.setFixedHeight(1)
            sep.setAttribute(Qt.WA_StyledBackground, True)
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(GUTTER, 0, 0, 0)
            hl.addWidget(sep)
            self._col.insertWidget(at + 1, holder)
            self._col.insertSpacing(at + 2, top_gap)
            # 线上方少空一点：上一段回复末尾那排按钮（只在悬停时出现）照样占着位置
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
            self._cur_assistant.file_link_clicked.connect(self.file_link_clicked)
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
        # 节点对着气泡里第一行（有引用时是引用的第一行；只有引用时正文是藏着的，锚在正文上就没了）
        self._cur_round.add_node(self._cur_user.bubble, "user",
                                 dy=lambda b: b.layout().contentsMargins().top() + 11)
        if text:
            self._cur_user.append(text)
        self._cur_assistant = None
        return self._cur_user

    def append_user_html(self, html):
        if self._cur_user is None:
            self.add_user_turn()
        self._cur_user.set_html(html)

    def append_user_text(self, text):
        """用户原文（实时发送时用；重画历史走 handle("user_msg")，同一套渲染）：
        开头的引用画成引用块，@文件 靛蓝加粗。"""
        if self._cur_user is None:
            self.add_user_turn()
        self._cur_user.set_text(text)

    def add_user_actions(self, index):
        """用户气泡右边的时间轴操作按钮：编辑 / 回退 / 分叉（幽灵图标按钮，与回复末尾同款），
        鼠标移到这条消息上才出现。
        index 是该用户消息在 chat_history 里的绝对下标，点击时原样发信号给宿主解析。"""
        if self._cur_user is None or self._cur_round is None:
            return
        row = QWidget()
        row.setObjectName("userActions")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        specs = [
            (SVG_PEN, "编辑并重发", self.user_edit_requested),
            (SVG_UNDO, "回退到这里（丢弃这一轮及之后）", self.user_rewind_requested),
            (SVG_FORK, "从这里分叉新对话", self.user_fork_requested),
        ]
        for svg, tip, sig in specs:
            b = QPushButton()
            b.setObjectName("msgAction")
            b.setFixedSize(28, 28)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setAccessibleName(tip)
            b.setIcon(QIcon(_icon(svg, _P["muted"], 14)))
            b.setIconSize(QSize(14, 14))
            b.clicked.connect(lambda _checked=False, s=sig, i=index: s.emit(i))
            h.addWidget(b, 0, Qt.AlignVCenter)
        self._cur_user.set_actions(row, row.findChildren(QPushButton))

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
        self._cur_assistant.file_link_clicked.connect(self.file_link_clicked)
        r.add(self._cur_assistant, top_gap=12)
        r.assistant_turns.append(self._cur_assistant)
        return self._cur_assistant

    def end_run(self):
        """这一轮运行结束（停止、失败也算）：回复的用时定格，开着的块收尾；
        有了最终回答就把过程连同中间回复收起来。"""
        if self._cur_round is not None:
            for turn in self._cur_round.assistant_turns:
                turn.finish()
        self.collapse_answered()

    def collapse_answered(self):
        """最后一轮已经以回答收尾：过程和中间的过渡回复收进「展开过程」，只留最终回答。
        运行结束、重绘历史后各调一次（前面的轮次在下一轮开始时就收了）。"""
        if self._cur_round is not None:
            self._cur_round.collapse_process(need_answer=True)

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

    def finalize_markdown(self, html, md=None):
        self._assistant().finalize_markdown(html, md)

    def retire_message_actions(self):
        """该挂一排复制按钮、但它马上会被后面的块拿掉（重画历史时过渡回复后面紧跟工具卡）：
        不建，只做建了再拿掉会留下的效果——上一条回答的按钮不再常显、当前回复不留按钮。"""
        a = self._assistant()
        a.end_body()
        prev = self._latest_actions
        if prev is not None:
            prev.set_pinned(False)
        self._latest_actions = None
        a.actions = None

    def add_message_actions(self, on_copy=None, on_regen=None):
        """回复末尾的操作按钮:复制 / 重新生成(幽灵图标按钮)。"""
        a = self._assistant()
        a.end_body()
        row = QWidget()
        row.setObjectName("msgActions")   # 收起过程时跟着它上面那段回复一起收
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
            if cb is not None:
                b.clicked.connect(lambda _checked=False, c=cb: c())
            h.addWidget(b, 0, Qt.AlignVCenter)
        h.addStretch(1)
        a._add(row, top_gap=6)
        # 只有最新一条回答的按钮常显；之前那条改成鼠标移到它那段回复上才出现
        prev = self._latest_actions
        if prev is not None:
            prev.set_pinned(False)
        a.actions = self._latest_actions = _HoverButtons(row.findChildren(QPushButton), pinned=True)

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
            self._assistant().show_waiting(text)
        elif tag == "tool_tag":
            # 拒绝/错误类(⚠️/⛔/🔒)是独立提示,不建工具卡;其余是真工具调用 → 建卡
            if any(w in text for w in ("⚠️", "⛔", "🔒", "未知工具")):
                self._assistant().notice(text.strip())
            else:
                name, detail = _parse_tool_tag(text)
                row = self._assistant().tool_call(name, detail)
                row.file_clicked.connect(self.file_clicked)   # 改动的文件 / 改文件那一行的文件名
        elif tag == "recovery_note":
            self._assistant().recovery_note(text)
        elif tag == "tool_detail":
            self._assistant().tool_detail(text.strip())   # 工具参数 → 当前工具卡的摘要 / 展开内容
        elif tag == "tool_result":
            self._assistant().tool_output(text)
        elif tag == "tool_files":
            self._assistant().tool_files(text)
        elif tag == "tool_stat":
            self._assistant().tool_stat(text.strip())
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

    def stop_tool_clock(self):
        """用户点了停止：当前工具卡不再显示「运行中」（不等工具真的返回）。"""
        if self._cur_assistant is not None:
            self._cur_assistant.stop_tool_clock()

    def update_waiting(self, text: str):
        """心跳报的阶段（等待响应 / 模型思考中 / 正在生成工具调用）；没有指示器就不管。"""
        if self._cur_assistant is not None:
            self._cur_assistant.update_waiting(text)

    def show_retry(self, error_msg, on_retry=None):
        """错误 + 重试按钮块。"""
        a = self._assistant()
        a.clear_waiting()
        a.end_body()
        card = QFrame()
        card.setAttribute(Qt.WA_StyledBackground, True)
        card.setObjectName("retryCard")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(8)
        msg = QLabel(_esc(error_msg))
        msg.setWordWrap(True)
        msg.setProperty("mv", "retryMsg")
        cv.addWidget(msg)
        btn = QPushButton("重试")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setIcon(QIcon(_icon(SVG_REFRESH, _P["err_text"], 14)))
        btn.setIconSize(QSize(14, 14))
        btn.setProperty("mv", "retryBtn")
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
            # 节点对准那一行的中线（卡片收成一行之后，行高 40）
            self._cur_round.add_node(card, "result", tone=tone,
                                     dy=getattr(card, "header_center", 20))
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
