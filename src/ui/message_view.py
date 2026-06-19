"""消息流的真控件渲染（替代 QTextBrowser）。

设计 handoff（design_handoff_lingxi_chat）要求消息流是圆角卡 + 阴影 + 可展开思考块——
QTextBrowser 画不了文本块的圆角/阴影,故消息流改成真 Qt 控件:每"块"一个 QWidget,
拼进 MessageView 的纵向布局里。本文件先放【块级组件】(Phase A),MessageView 容器 +
tag→控件分发(Phase B)、接进 ChatUI(Phase C)随后。

配色/字号/圆角全部对齐 design_handoff_lingxi_chat 的浅色规格。颜色先内联在 _P,
接进主题系统时再抽 token。
"""
from PySide6.QtCore import Qt, QByteArray, QSize, QTimer, Signal
from PySide6.QtGui import QPixmap, QPainter, QIcon
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtSvg import QSvgRenderer
except Exception:  # QtSvg 理论上随 PySide6 自带；缺了降级成无图标
    QSvgRenderer = None


# ── 设计 token（浅色） ──
_P = {
    "indigo":        "#5b6cf0",
    "indigo_soft":   "#eef0fe",
    "orange":        "#ef7a45",
    "text":          "#222838",
    "text2":         "#2b3142",
    "text3":         "#3a4150",
    "text_sec":      "#5a6172",
    "muted":         "#7a8092",
    "muted2":        "#9aa0b0",
    "muted3":        "#aeb3c0",
    "card_bg":       "#ffffff",
    "card_border":   "#ebecf1",
    "header_bg":     "#f7f8fa",
    "result_bg":     "#fafbfc",
    "result_border": "#f0f0f3",
    "chip_border":   "#e3e4ea",
    "chip_hover":    "#c8cbf3",
    "box_border":    "#cdd1dc",
    "panel_accent":  "#c8cbf3",
    "spin_track":    "#dfe2f0",
    # JSON 高亮
    "json_key":      "#9a6bd6",
    "json_str":      "#3a8a5f",
    "json_num":      "#c2783f",
    "json_punct":    "#5a6172",
}

_SERIF = "'Noto Serif SC', 'Source Han Serif SC', 'STSong', serif"
_SANS = "'Noto Sans SC', 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif"
_MONO = "'JetBrains Mono', 'Cascadia Code', Consolas, monospace"


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


# 内联 SVG（来自 design handoff；currentColor 占位）
SVG_CLIPBOARD = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
                 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
                 '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/></svg>')
SVG_STAR = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M12 2l2.4 7.4H22l-6 4.4 2.3 7.2L12 16.6 5.7 21l2.3-7.2-6-4.4h7.6z"/></svg>')
SVG_BULB = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M9.5 16a5 5 0 1 1 5 0v2h-5z"/><path d="M10 21h4"/></svg>')
SVG_COPY = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>')
SVG_REFRESH = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
               'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>')


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def highlight_json(text: str) -> str:
    """极简 JSON 着色 → rich HTML（key/字符串/数字/标点分色），用于工具卡 body。
    非严格解析,够用就行;失败退化为转义纯文本。"""
    import re
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
    return (f'<pre style="margin:0; font-family:{_MONO}; font-size:13px; '
            f'line-height:1.7; white-space:pre-wrap; word-break:break-word;">{body}</pre>')


class ModelTag(QLabel):
    """模型名 tag（橙色衬线 700 19px）。"""
    def __init__(self, name: str, parent=None):
        super().__init__(name, parent)
        self.setStyleSheet(
            f"color:{_P['orange']}; font-family:{_SERIF}; font-weight:700; "
            f"font-size:19px; background:transparent;"
        )


class BodyText(QLabel):
    """正文段落（15.5px/1.75）。富文本,支持后续 markdown HTML。"""
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.RichText)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setStyleSheet(
            f"color:{_P['text2']}; font-family:{_SANS}; font-size:15.5px; background:transparent;"
        )
        self.setText(text)


class ToolCallCard(QFrame):
    """工具调用卡:圆角边框 + 灰底头部(图标+名+tool call) + 代码 body(JSON 高亮)。"""
    def __init__(self, name: str, code_text: str, icon_svg: str = SVG_CLIPBOARD,
                 icon_color: str = _P["indigo"], parent=None):
        super().__init__(parent)
        self.setObjectName("toolCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#toolCard {{ background:{_P['card_bg']}; border:1px solid {_P['card_border']}; "
            f"border-radius:12px; }}"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("toolCardHeader")
        header.setAttribute(Qt.WA_StyledBackground, True)
        header.setStyleSheet(
            f"#toolCardHeader {{ background:{_P['header_bg']}; "
            f"border-top-left-radius:11px; border-top-right-radius:11px; "
            f"border-bottom:1px solid {_P['card_border']}; }}"
        )
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 11, 14, 11)
        h.setSpacing(8)
        h.addWidget(_icon_label(icon_svg, icon_color, 15), 0, Qt.AlignVCenter)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color:{_P['text3']}; font-size:13.5px; font-weight:600; background:transparent;")
        h.addWidget(name_lbl, 0, Qt.AlignVCenter)
        tc = QLabel("tool call")
        tc.setStyleSheet(f"color:{_P['muted3']}; font-size:12px; background:transparent;")
        h.addWidget(tc, 0, Qt.AlignVCenter)
        h.addStretch(1)
        v.addWidget(header)

        self._body = QLabel()
        self._body.setObjectName("toolCardBody")
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        self._body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._body.setStyleSheet(f"color:{_P['text_sec']}; background:transparent; padding:14px 16px;")
        self._body.setText(highlight_json(code_text))
        self._body.setVisible(bool(code_text.strip()))   # 参数还没来时不留空白行
        self._v = v
        v.addWidget(self._body)

    def set_body(self, code_text: str):
        """工具参数（tool_detail）到位后填进卡 body。"""
        self._body.setText(highlight_json(code_text))
        self._body.setVisible(bool(code_text.strip()))


class ChecklistCard(QFrame):
    """结果清单卡:浅底圆角 + 说明 + 若干「□ N. 描述」行(可勾选高亮)。"""
    def __init__(self, caption_html: str, rows: list, parent=None):
        # rows: list of dict(n, txt, checked)
        super().__init__(parent)
        self.setObjectName("checkCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#checkCard {{ background:{_P['result_bg']}; border:1px solid {_P['result_border']}; "
            f"border-radius:12px; }}"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(0)
        cap = QLabel(caption_html)
        cap.setTextFormat(Qt.RichText)
        cap.setStyleSheet(f"color:{_P['muted']}; font-size:13.5px; background:transparent;")
        v.addWidget(cap)
        v.addSpacing(10)
        for r in rows:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 5, 0, 5)
            rl.setSpacing(10)
            box = QFrame()
            box.setFixedSize(16, 16)
            box.setAttribute(Qt.WA_StyledBackground, True)
            if r.get("checked"):
                box.setStyleSheet(f"background:{_P['indigo_soft']}; border:1.6px solid {_P['indigo']}; border-radius:5px;")
            else:
                box.setStyleSheet(f"background:transparent; border:1.6px solid {_P['box_border']}; border-radius:5px;")
            rl.addWidget(box, 0, Qt.AlignVCenter)
            color = _P["indigo"] if r.get("checked") else _P["text_sec"]
            lbl = QLabel(f"<b>{r['n']}.</b> {_esc(r['txt'])}")
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet(f"color:{color}; font-size:14px; background:transparent;")
            rl.addWidget(lbl, 1)
            v.addWidget(row)


class ThinkingChip(QWidget):
    """可展开思考块:chip(灯泡 + 思考·N字/思考中 + ▸)→ 点开下方面板(左竖线 + 灰底)。
    流式期 set_live(True) 显示「思考中」+ update_content 累加;think_collapse 后定格「思考·N字」。"""
    def __init__(self, char_count: int = 0, content: str = "", parent=None):
        super().__init__(parent)
        self._open = False
        self._live = False
        self._content = content
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # 用 QPushButton 原生 icon+text（不要往按钮里塞 QLabel 布局——真机上子 label 常不绘制）
        self.btn = QPushButton()
        self.btn.setObjectName("thinkChip")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setIcon(QIcon(_icon(SVG_BULB, _P["indigo"], 14)))
        self.btn.setIconSize(QSize(14, 14))
        self.btn.setStyleSheet(
            f"#thinkChip {{ border:1px solid {_P['chip_border']}; background:{_P['header_bg']}; "
            f"border-radius:8px; padding:5px 12px; color:{_P['indigo']}; "
            f"font-size:13.5px; font-weight:600; text-align:left; }} "
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
            f"color:{_P['muted']}; font-size:14px; padding:14px 16px; }}"
        )
        self.panel.setVisible(False)
        v.addWidget(self.panel)

        self._char_count = char_count
        self.update_content(content)
        self.btn.clicked.connect(self._toggle)

    def _refresh_label(self):
        chev = "   ▾" if self._open else "   ▸"
        if self._live:
            self.btn.setText("思考中" + chev)
        else:
            n = self._char_count if self._content == "" else len(self._content)
            self.btn.setText(f"思考 · {n} 字" + chev)

    def set_live(self, live: bool):
        self._live = live
        self._refresh_label()

    def update_content(self, content: str):
        self._content = content
        self.panel.setText(_esc(content).replace("\n", "<br>"))
        self._refresh_label()

    def _toggle(self):
        self._open = not self._open
        self.panel.setVisible(self._open)
        self._refresh_label()


class StepHeader(QWidget):
    """步骤标题:方角徽章「N」 + 「第 N 步:…」。"""
    def __init__(self, n: int, title: str, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)
        badge = QLabel(str(n))
        badge.setFixedSize(24, 24)
        badge.setAlignment(Qt.AlignCenter)
        badge.setAttribute(Qt.WA_StyledBackground, True)
        badge.setStyleSheet(
            f"background:{_P['indigo_soft']}; color:{_P['indigo']}; border-radius:7px; "
            f"font-size:13px; font-weight:700;"
        )
        h.addWidget(badge, 0, Qt.AlignVCenter)
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color:{_P['text']}; font-size:16.5px; font-weight:700; background:transparent;")
        h.addWidget(lbl, 0, Qt.AlignVCenter)
        h.addStretch(1)


class WaitingIndicator(QWidget):
    """等待响应:转圈 + 「等待响应 (Ns)」(自走秒表)。"""
    def __init__(self, start_elapsed: int = 0, parent=None):
        super().__init__(parent)
        self._elapsed = start_elapsed
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)
        from .widgets import LoadingSpinner
        h.addWidget(LoadingSpinner(size=16, color=_P["indigo"]), 0, Qt.AlignVCenter)
        self._lbl = QLabel()
        self._lbl.setStyleSheet(f"color:{_P['indigo']}; font-size:14px; font-weight:500; background:transparent;")
        h.addWidget(self._lbl, 0, Qt.AlignVCenter)
        h.addStretch(1)
        self._tick()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._bump)
        self._timer.start(1000)

    def _tick(self):
        self._lbl.setText(f"等待响应 ({self._elapsed}s)")

    def _bump(self):
        self._elapsed += 1
        self._tick()


class UserTurn(QWidget):
    """用户一轮：「你」标签 + 文本。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        lab = QLabel("你")
        lab.setStyleSheet(f"color:{_P['indigo']}; font-family:{_SANS}; font-size:16px; font-weight:700; background:transparent;")
        v.addWidget(lab)
        self._body = QLabel()
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        self._body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._body.setStyleSheet(f"color:{_P['text2']}; font-family:{_SANS}; font-size:15px; background:transparent;")
        v.addWidget(self._body)
        self._text = ""

    def append(self, text):
        self._text += text
        self._body.setText(_esc(self._text).replace("\n", "<br>"))

    def set_html(self, html):
        self._body.setText(html)


class AssistantTurn(QWidget):
    """助手一轮:模型名 tag + 纵向块容器(思考块/正文/工具卡/结果/等待/步骤标题…)。
    流式期间各 show_message tag 往这里追加/更新块。"""
    def __init__(self, model_name="", parent=None):
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(0)
        if model_name:
            self._v.addWidget(ModelTag(model_name))
        # 当前流式状态
        self._cur_body = None          # 正在流式的正文 BodyText（累加纯文本）
        self._cur_body_text = ""
        self._cur_think = None         # 当前 ThinkingChip（流式累加）
        self._cur_think_text = ""
        self._cur_tool = None          # 当前 ToolCallCard
        self._waiting = None           # WaitingIndicator

    def _add(self, w, top_gap=14):
        if top_gap:
            self._v.addSpacing(top_gap)
        self._v.addWidget(w)
        return w

    # —— 块级操作 ——
    def end_body(self):
        self._cur_body = None
        self._cur_body_text = ""

    def append_body(self, text):
        self.clear_waiting()
        if self._cur_body is None:
            self._cur_body = self._add(BodyText(""), top_gap=16)
        self._cur_body_text += text
        self._cur_body.setText(_esc(self._cur_body_text).replace("\n", "<br>"))

    def finalize_markdown(self, html):
        """流式结束:把当前正文替换成渲染好的 markdown 富文本。"""
        if self._cur_body is None:
            self._cur_body = self._add(BodyText(""), top_gap=16)
        self._cur_body.setText(html)
        self.end_body()

    def think_start(self):
        self.clear_waiting()
        self.end_body()
        self._cur_think_text = ""
        self._cur_think = self._add(ThinkingChip(0, ""), top_gap=14)
        self._cur_think.set_live(True)

    def think_append(self, text):
        if self._cur_think is None:
            self.think_start()
        self._cur_think_text += text
        self._cur_think.update_content(self._cur_think_text)

    def think_collapse(self):
        if self._cur_think is not None:
            self._cur_think.set_live(False)
            self._cur_think.update_content(self._cur_think_text)
        self._cur_think = None

    def tool_call(self, name, code_text="", icon_svg=None, icon_color=None):
        self.clear_waiting()
        self.end_body()
        # 步骤类工具用橙星,其余用蓝剪贴板（对齐设计：更新步骤=star,更新计划=clipboard）
        if icon_svg is None:
            if any(k in name for k in ("步骤", "step")):
                icon_svg, icon_color = SVG_STAR, _P["orange"]
            else:
                icon_svg, icon_color = SVG_CLIPBOARD, _P["indigo"]
        self._cur_tool = self._add(
            ToolCallCard(name, code_text, icon_svg, icon_color or _P["indigo"]), top_gap=16)
        return self._cur_tool

    def tool_detail(self, text):
        """工具参数到位：填进当前工具卡 body；没有当前卡则退化成普通块。"""
        if self._cur_tool is not None:
            self._cur_tool.set_body(text)
        else:
            self.result_block(text)

    def result_block(self, text):
        self.clear_waiting()
        self.end_body()
        lbl = QLabel(_esc(text).replace("\n", "<br>"))
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lbl.setStyleSheet(
            f"color:{_P['muted']}; font-family:{_MONO}; font-size:13px; background:{_P['result_bg']}; "
            f"border:1px solid {_P['result_border']}; border-radius:10px; padding:10px 14px;"
        )
        lbl.setAttribute(Qt.WA_StyledBackground, True)
        self._add(lbl, top_gap=12)

    def checklist(self, caption_html, rows):
        self.clear_waiting()
        self.end_body()
        self._add(ChecklistCard(caption_html, rows), top_gap=14)

    def step_header(self, n, title):
        self.end_body()
        self._add(StepHeader(n, title), top_gap=22)

    def show_waiting(self):
        self.clear_waiting()
        self._waiting = self._add(WaitingIndicator(0), top_gap=18)

    def clear_waiting(self):
        if self._waiting is not None:
            self._waiting.setParent(None)
            self._waiting.deleteLater()
            self._waiting = None


class MessageView(QScrollArea):
    """消息流容器:QScrollArea + 纵向 turn 列表。对外 handle(text, tag) 镜像 show_message 协议,
    finalize_markdown(html) 对应 render_final_markdown,clear()/redraw 复位。"""

    image_clicked = Signal(QPixmap)   # 点击聊天区图片 → 宿主弹放大遮罩(传全分辨率原图)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("messageView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("#messageView { background:#ffffff; border:none; }")
        self._content = QWidget()
        self._content.setStyleSheet("background:#ffffff;")
        self._col = QVBoxLayout(self._content)
        self._col.setContentsMargins(32, 32, 32, 28)
        self._col.setSpacing(0)
        # 内容靠中间一栏(max 780)。简化:外层留白由 margins 给,后续可加居中 wrap。
        self._col.addStretch(1)
        self.setWidget(self._content)
        self._cur_user = None
        self._cur_assistant = None

    def clear(self):
        while self._col.count() > 1:   # 留最后的 stretch
            it = self._col.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)        # 立即从显示移除（deleteLater 是异步的,不设会残留旧消息）
                w.deleteLater()
        self._cur_user = None
        self._cur_assistant = None

    def _add_turn(self, w, top_gap=28):
        if self._col.count() > 1 and top_gap:
            # 在 stretch 前插入,turn 之间留间距
            self._col.insertSpacing(self._col.count() - 1, top_gap)
        self._col.insertWidget(self._col.count() - 1, w)

    def _assistant(self):
        if self._cur_assistant is None:
            self._cur_assistant = AssistantTurn()
            self._add_turn(self._cur_assistant)
        return self._cur_assistant

    def add_user_turn(self, text=""):
        self._cur_user = UserTurn()
        if text:
            self._cur_user.append(text)
        self._add_turn(self._cur_user)
        self._cur_assistant = None
        return self._cur_user

    def append_user_html(self, html):
        if self._cur_user is None:
            self.add_user_turn()
        self._cur_user.set_html(html)

    def start_assistant(self, model_name=""):
        self._cur_assistant = AssistantTurn(model_name)
        self._add_turn(self._cur_assistant)
        return self._cur_assistant

    def finalize_markdown(self, html):
        stick = self._at_bottom()
        self._assistant().finalize_markdown(html)
        if stick:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def add_message_actions(self, on_copy=None, on_regen=None):
        """回复末尾的操作按钮:复制 / 重新生成(幽灵图标按钮)。"""
        stick = self._at_bottom()
        a = self._assistant()
        a.end_body()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        specs = [(SVG_COPY, on_copy, "复制")]
        if on_regen is not None:
            specs.append((SVG_REFRESH, on_regen, "重新生成"))
        for svg, cb, tip in specs:
            b = QPushButton()
            b.setObjectName("msgAction")
            b.setFixedSize(32, 32)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setIcon(QIcon(_icon(svg, _P["muted3"], 15)))
            b.setIconSize(QSize(15, 15))
            b.setStyleSheet(
                "#msgAction { border:none; background:transparent; border-radius:8px; } "
                "#msgAction:hover { background:#f2f3f6; }")
            if cb is not None:
                b.clicked.connect(lambda _checked=False, c=cb: c())
            h.addWidget(b, 0, Qt.AlignVCenter)
        h.addStretch(1)
        a._add(row, top_gap=14)
        if stick:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def handle(self, text, tag):
        """tag→控件分发。镜像 chat_window._append_html 的 tag 协议。"""
        # 贴底跟随:仅当用户当前就在底部附近(或这是用户刚发的消息)才滚到底,否则不动——
        # 生成过程中允许用户向上翻历史而不被流式追加拽回底部(复刻旧 _scroll_guard 行为)。
        stick = self._at_bottom() or tag in ("user_label", "user_msg")
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
                self._assistant().result_block(text.strip())
            else:
                self._assistant().tool_call(_parse_tool_tag(text))
        elif tag == "tool_detail":
            self._assistant().tool_detail(text.strip())   # 工具参数 → 填进当前工具卡 body
        elif tag == "tool_result":
            self._assistant().result_block(text.strip())
        elif tag == "ai_image":
            self.add_image(QPixmap(text.strip()))   # text = 图片本地路径
        elif tag == "reset_ai_reply":
            if self._cur_assistant is not None:
                self._cur_assistant.end_body()   # 工具调用结束,下一轮 ai_msg 另起正文块
        elif tag == "spacer":
            pass   # 间距由布局给
        if stick:
            QTimer.singleShot(0, self._scroll_to_bottom)

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
            "#retryCard { background:#fde7e3; border:1px solid #f3c4b6; border-radius:10px; }")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 12, 14, 12)
        cv.setSpacing(8)
        msg = QLabel("⚠️ " + _esc(error_msg))
        msg.setWordWrap(True)
        msg.setStyleSheet("color:#c0492f; font-size:13.5px; background:transparent;")
        cv.addWidget(msg)
        btn = QPushButton("↻ 重试")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { background:#ffffff; border:1px solid #f3c4b6; border-radius:8px; "
            "padding:5px 16px; color:#c0492f; font-size:13px; } "
            "QPushButton:hover { background:#fde7e3; }")
        if on_retry is not None:
            btn.clicked.connect(lambda: on_retry())
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(btn, 0, Qt.AlignLeft)
        row.addStretch(1)
        cv.addLayout(row)
        a._add(card, top_gap=12)

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
        self._add_turn(holder, top_gap=8)

    def _at_bottom(self, slack=40):
        """用户当前是否贴在底部附近(slack px 容差)——决定要不要继续跟随滚动。
        必须在【追加内容之前】调用:插入控件后 maximum 要等下一轮布局才更新,
        此刻读到的还是追加前的位置,正好反映"追加前用户在不在底"。"""
        sb = self.verticalScrollBar()
        return sb.value() >= sb.maximum() - slack

    def _scroll_to_bottom(self):
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())


def _parse_tool_tag(text):
    """从 tool_tag 文本里抽工具名——去掉开头的 emoji 图标(🔧/📋/✏️ 等)和空白。"""
    import re
    s = (text or "").strip()
    s = re.sub(r'^[\U0001F000-\U0001FAFF☀-➿️←-⇿\s]+', '', s)
    return s.strip()
