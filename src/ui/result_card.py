"""运行结果卡（Qt 控件）。

只负责把 `src/result_view.describe()` 算好的结论画出来——**判断在那边，画在这边**。
分开的理由很实际：卡片上"能不能说检查通过了"是要被逐条测试钉死的判断，
混在布局代码里就只能靠截图去验。

配色沿用 `message_view._P` 的设计 token，跟消息流保持同一套观感。

长相（设计稿「消息区：结果卡精简成一行」）：一行——状态圆标 · 短标题 · 一串摘要（涉及几个
文件 · 检查名 / 几项没过）· 「查看改动」· 展开箭头。点开才是详情：每项检查只列最新结论，
过期 / 被复查取代的收成一行「还有 N 条更早的检查记录」。只有"通过"默认收着；没过、中断、
有未验证事项、保存出错时默认展开，没做完的卡「继续任务」仍排在检查清单前面。
"""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from .message_view import _MONO, _P, _SANS, _ElidedLabel, _icon


# 状态色调 → (卡片底色, 边框, 标题 / 圆标颜色)。存的是 _P 的键名，画卡时按当前主题取色。
_TONE = {
    "ok":          ("result_bg", "card_border", "ok"),
    "warn":        ("gate_bg", "gate_border", "gate_text"),
    "bad":         ("err_bg", "err_border", "err_text"),
    "muted":       ("result_bg", "card_border", "muted"),
    "interrupted": ("gate_bg", "gate_border", "gate_text"),
    "running":     ("result_bg", "card_border", "indigo"),
}

# 状态圆标：实心圆 + 白色（深色主题里是底色）的符号
_MARK = {
    "ok": '<path d="m7.5 12.5 3 3 6-6.5" fill="none" stroke="{ink}" stroke-width="2.4" '
          'stroke-linecap="round" stroke-linejoin="round"/>',
    "warn": '<path d="M12 7v6M12 16.5v.5" fill="none" stroke="{ink}" stroke-width="2.4" '
            'stroke-linecap="round"/>',
    "bad": '<path d="M8.5 8.5l7 7M15.5 8.5l-7 7" fill="none" stroke="{ink}" stroke-width="2.4" '
           'stroke-linecap="round"/>',
    "interrupted": '<path d="M9.5 8.5v7M14.5 8.5v7" fill="none" stroke="{ink}" stroke-width="2.4" '
                   'stroke-linecap="round"/>',
}


def _status_mark(tone, color):
    ink = _P["view_bg"]
    mark = _MARK.get(tone, _MARK["warn"] if tone == "muted" else _MARK["ok"])
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           f'<circle cx="12" cy="12" r="10" fill="currentColor"/>{mark.format(ink=ink)}</svg>')
    lab = QLabel()
    lab.setFixedSize(16, 16)
    lab.setPixmap(_icon(svg, color, 16))
    return lab


_STATUS_COLOR = {
    "passed": "ok",
    "failed": "bad",
    "timeout": "orange",
    "not_run": "muted",
    "cancelled": "muted",
    "error": "bad",
    "unknown": "muted",
}


# 单条路径的显示上限。超长路径里没有空格，Qt 换不了行，会把整张卡横向撑开、
# 在关掉横向滚动条的消息流里直接被裁掉。中间省略是标准做法，完整路径仍可在
# "查看验证输出"里看到。
_PATH_DISPLAY_MAX = 56


def _elide_middle(text, limit=_PATH_DISPLAY_MAX):
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    return f"{text[:head]}…{text[-(limit - head - 1):]}"


def _soft_wrap(text, chunk=42):
    """给长到没有断点的字符串塞入换行机会。

    工具摘要里常见几百字符不带空格的输出（编译器一行报错、base64、长路径）。
    Qt 断不开它，整张卡就被横向撑开，而消息流关掉了横向滚动条 —— 结果是直接被裁掉。
    """
    if not text:
        return ""

    def _wordish(ch):
        return ch.isascii() and (ch.isalnum() or ch in "_-./\\:")

    out = []
    for line in text.splitlines() or [""]:
        while len(line) > chunk:
            # 往回找一个不在英文单词 / 路径中间的断点：早先一律硬切第 chunk 个字，
            # 「run_tests」被切成「ru / n_tests」（用户截图）。中文任意两字之间都能断。
            cut = chunk
            while cut > chunk // 2 and _wordish(line[cut - 1]) and _wordish(line[cut]):
                cut -= 1
            if cut <= chunk // 2:
                cut = chunk          # 整段都是一个超长单词（base64 / 长路径）：只能硬切
            out.append(line[:cut])
            line = line[cut:].lstrip(" ")
        out.append(line)
    return "\n".join(out)


def _paths_text(paths):
    """一行一个路径：比用顿号连成一长串好读，也天然给了换行点。"""
    return "\n".join(_elide_middle(p) for p in paths)


def _label(text, *, color, size=13, bold=False, mono=False, wrap=True, shrink=True):
    """卡片里的一行字。

    换行就只用 `setWordWrap(True)` + **默认**尺寸策略，和消息流里的 `BodyText` 一致。
    曾经给它加过 `QSizePolicy.Ignored` 想让长路径不撑宽卡片，代价是布局拿不到正确的
    heightForWidth：整张卡被算矮 130px，底部按钮被边框整条切掉，而且只在内容多的卡上
    出现。宽度的事交给外层滚动区（消息流本来就这么处理），高度必须让布局算对。
    """
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    family = _MONO if mono else _SANS
    weight = "600" if bold else "400"
    lab.setStyleSheet(
        f"color:{color}; font-family:{family}; font-size:{size}px; font-weight:{weight};"
        f" background:transparent;")
    if shrink and wrap:
        # 允许被压窄到 0（长路径靠换行消化），但**不动高度策略**。
        lab.setMinimumWidth(0)
    return lab


class _Head(QWidget):
    """卡片的那一行：整行可点（展开 / 收起）。"""
    clicked = Signal()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


def _flat_button(text, color):
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        f"QPushButton {{ border:none; background:transparent; color:{color}; font-family:{_SANS};"
        f" font-size:12px; padding:4px 8px; border-radius:7px; }}"
        f"QPushButton:hover {{ background:{_P['header_bg']}; }}")
    return btn


class ResultCard(QFrame):
    """一轮运行的结果卡。所有文案来自 `result_view.describe()` 的返回。"""

    view_diff_requested = Signal(object)     # 携带本卡的 view dict（含会话/项目/run_id）
    view_output_requested = Signal(object)
    continue_requested = Signal(object)      # 「继续任务」：同样只带本卡的 view
    open_file_requested = Signal(object, str)  # 点了「本轮涉及的文件」里的某一个

    def __init__(self, view: dict, parent=None):
        super().__init__(parent)
        self.view = dict(view or {})
        self.setObjectName("resultCard")
        bg_key, border_key, ink_key = _TONE.get(self.view.get("tone"), _TONE["muted"])
        self._border = _P[border_key]
        self._ink = _P[ink_key]
        self.setStyleSheet(
            f"#resultCard {{ background:{_P[bg_key]}; border:1px solid {self._border};"
            f" border-radius:10px; }}")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self._build_head(col)
        self._sep = QFrame()
        self._sep.setFixedHeight(1)
        self._sep.setStyleSheet(f"background:{self._border}; border:none;")
        col.addWidget(self._sep)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(36, 10, 14, 12)
        body.setSpacing(6)
        col.addWidget(self.body)

        if self.view.get("reason"):
            body.addWidget(_label(_soft_wrap(self.view["reason"]),
                                  color=_P["text_sec"], size=12))
        self._continue_btn = None
        if self.view.get("resumable"):
            # 没做完的卡：先说停在哪、给「继续任务」，长长的文件和检查清单放后面。
            # 以前按钮压在一串检查记录下面，跑满轮数时得往下翻才找得到。
            self._add_resume(body)
            self._add_buttons(body)
            self._add_files(body)
            self._add_validations(body)
            self._add_pending(body)
            self._add_usage(body)
            self._add_save_note(body)
        else:
            self._add_files(body)
            self._add_validations(body)
            self._add_pending(body)
            self._add_usage(body)
            self._add_save_note(body)
            self._add_buttons(body)

        self.set_open(bool(self.view.get("default_open", True)))

    # ── 那一行 ──
    def _build_head(self, col):
        self.head = _Head()
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setFixedHeight(40)
        self.head.clicked.connect(self.toggle)
        h = QHBoxLayout(self.head)
        h.setContentsMargins(12, 0, 8, 0)
        h.setSpacing(8)
        h.addWidget(_status_mark(self.view.get("tone"), self._ink), 0, Qt.AlignVCenter)
        self.title_label = QLabel(self.view.get("short_title") or self.view.get("title", ""))
        self.title_label.setToolTip(self.view.get("title", ""))
        self.title_label.setStyleSheet(
            f"color:{self._ink}; font-family:{_SANS}; font-size:13px; font-weight:600;"
            f" background:transparent;")
        h.addWidget(self.title_label, 0, Qt.AlignVCenter)
        meta = " · ".join(self.view.get("meta") or [])
        self.meta_label = _ElidedLabel(("· " + meta) if meta else "")
        self.meta_label.setStyleSheet(
            f"color:{_P['muted']}; font-family:{_SANS}; font-size:12px; background:transparent;")
        h.addWidget(self.meta_label, 1, Qt.AlignVCenter)
        if self.view.get("display_source") == "restored":
            # 重绘出来的历史结果：说清它是上一次的，别让用户以为刚刚又跑了一轮。
            chip = QLabel("上次")
            chip.setToolTip("上次运行的结果")
            chip.setStyleSheet(
                f"color:{_P['muted']}; background:{_P['header_bg']}; border-radius:6px;"
                f" padding:1px 6px; font-size:11px;")
            h.addWidget(chip, 0, Qt.AlignVCenter)
        self._head_diff = None
        if self.view.get("can_view_diff"):
            self._head_diff = _flat_button("查看改动", _P["indigo"])
            self._head_diff.clicked.connect(lambda _c=False: self.view_diff_requested.emit(self.view))
            h.addWidget(self._head_diff, 0, Qt.AlignVCenter)
        self._chev = QPushButton()
        self._chev.setFixedSize(28, 28)
        self._chev.setCursor(Qt.PointingHandCursor)
        self._chev.setIconSize(QSize(12, 12))
        self._chev.setStyleSheet(
            f"QPushButton {{ border:none; background:transparent; border-radius:7px; }}"
            f"QPushButton:hover {{ background:{_P['header_bg']}; }}")
        self._chev.clicked.connect(self.toggle)
        h.addWidget(self._chev, 0, Qt.AlignVCenter)
        col.addWidget(self.head)

    def is_open(self) -> bool:
        return self.body.isVisibleTo(self)

    def set_open(self, open_):
        from .message_view import SVG_CHEVRON_DOWN, SVG_CHEVRON_UP
        self.body.setVisible(open_)
        self._sep.setVisible(open_)
        self._chev.setIcon(QIcon(_icon(SVG_CHEVRON_UP if open_ else SVG_CHEVRON_DOWN, _P["muted"], 12)))
        self._chev.setAccessibleName("收起" if open_ else "展开")
        self._chev.setToolTip("收起" if open_ else "展开详情")
        if self._head_diff is not None:
            # 展开后下面有一排按钮，行上的「查看改动」就不重复了
            self._head_diff.setVisible(not open_)

    def toggle(self):
        from .message_view import _hold_scroll
        _hold_scroll(self)
        self.set_open(not self.is_open())

    # 线上的结果节点对齐那一行的中线
    def header_center(self, _w=None) -> int:
        return self.head.height() // 2

    # ── 各区块 ──

    def _add_usage(self, col):
        for line in self.view.get("usage_lines") or []:
            # 不用 _soft_wrap：这行每个字段都短、且以空格分隔，交给 Qt 按词换行；
            # 强插换行点会把"75.0%"劈成两半。
            col.addWidget(_label(line, color=_P["muted2"], size=12))

    def _add_files(self, col):
        files = self.view.get("files") or []
        if not files:
            return
        col.addWidget(_label(self.view.get("files_caption", ""),
                             color=_P["muted"], size=12))
        shown = files if not self.view.get("files_folded") else files[:6]
        col.addWidget(self._file_links(shown))
        if self.view.get("files_folded"):
            rest = files[6:]
            more = _label(f"…… 另有 {len(rest)} 个", color=_P["muted2"], size=12)
            col.addWidget(more)
            box = self._file_links(rest)
            box.setVisible(False)
            col.addWidget(box)
            btn = _flat_button("展开全部", _P["indigo"])
            btn.clicked.connect(lambda: (box.setVisible(not box.isVisible()),
                                         btn.setText("收起" if box.isVisible() else "展开全部")))
            col.addWidget(btn, 0, Qt.AlignLeft)

    def _file_links(self, paths):
        """一行一个文件，点了在右侧文件面板看这个文件的差异。"""
        from .message_view import _esc
        # href 只放序号：完整路径写进 href 的话标签原文一行几百字（长路径），也没必要
        # 链接样式写一次，每个文件单独一行原文（多个文件时原文也不会连成一长行）
        lines = (f"<style>a {{ color:{_P['indigo']}; text-decoration:none; }}</style>\n"
                 + "<br>\n".join(f'<a href="{i}">{_esc(_elide_middle(p))}</a>'
                                 for i, p in enumerate(paths)))
        lab = _label(lines, color=_P["text_sec"], size=12, mono=True)
        lab.setTextFormat(Qt.RichText)
        lab.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lab.setToolTip("点文件名在右侧看这个文件的改动")
        lab.linkActivated.connect(
            lambda href, ps=list(paths): self.open_file_requested.emit(self.view, ps[int(href)]))
        return lab

    def _check_row(self, col, row, *, show_suffix):
        # 直接 addLayout，**不套一层 QWidget**：包一层的话那个壳自己不传递
        # heightForWidth，整张卡的高度被算少，底部按钮被卡片边框切掉半截。
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        faded = row.get("stale") or row.get("superseded")
        color = _P["muted"] if faded else _P[_STATUS_COLOR.get(row["status"], "muted")]
        dot = QLabel()
        dot.setFixedSize(7, 7)
        dot.setStyleSheet(f"background:{color}; border-radius:3px;")
        h.addWidget(dot, 0, Qt.AlignVCenter)
        target = f"{row['checker']}"
        if row.get("path"):
            # 路径同样要中间省略：它没有空格，QLabel 断不开，整行的 minimumSizeHint
            # 就是那条路径的完整宽度，把外面的 HBox 连同整张卡一起撑出可视区。
            target += f" · {_elide_middle(row['path'])}"
        # 可换行 + 可压窄：「检查器 · 长路径」不换行的话会把卡片撑出可视区（用例钉着）。
        # 占满这一行：摘要放到下一行，不和它抢宽度（同一行时它被挤成三行）
        h.addWidget(_label(target, color=_P["text3"], size=12), 1)
        detail = row.get("detail") or ""
        if row.get("summary"):
            detail = f"{detail} · {row['summary'].splitlines()[0]}" if detail else row["summary"]
        suffix = ""
        if show_suffix:
            # 被后一次复查取代的旧结论要标出来，否则会并排摆着矛盾的结果
            suffix = ("（已过期）" if row.get("stale")
                      else "（已被复查取代）" if row.get("superseded") else "")
        h.addWidget(_label(row["label"] + suffix, color=color, size=12, bold=not faded,
                           wrap=False, shrink=False), 0)
        col.addLayout(h)
        if detail:
            # 摘要只占一行并中间省略（等宽长行会把卡片撑宽）：完整输出在「查看验证输出」里
            # 按实际宽度省略（不定死字数，也就没有最小宽度）
            sub = _ElidedLabel(detail.replace("\n", " ")[:300])
            sub.setStyleSheet(f"color:{_P['muted']}; font-family:{_SANS}; font-size:12px;"
                              f" background:transparent;")
            sub.setContentsMargins(15, 0, 0, 0)          # 和检查名对齐（圆点 7 + 间距 8）
            col.addWidget(sub)

    def _add_validations(self, col):
        rows = self.view.get("validations") or []
        if not rows:
            # 纯问答不画空的检查面板——一个"验证：（空）"的框只会让人以为哪里出错了。
            return
        primary = self.view.get("primary_rows")
        folded = self.view.get("folded_rows")
        if primary is None:              # 老的 view（没有分组）：全部当最新结论列
            primary, folded = rows, []
        col.addWidget(_label("实际执行的检查", color=_P["muted"], size=12))
        for row in primary:
            self._check_row(col, row, show_suffix=True)
        if any(r.get("stale") for r in primary):
            col.addWidget(_label("标为已过期的检查在其之后又有文件改动，结论不再代表当前代码。",
                                 color=_P["orange"], size=12))
        if folded:
            btn = _flat_button("› " + (self.view.get("folded_note")
                                       or f"还有 {len(folded)} 条更早的检查记录"), _P["muted"])
            col.addWidget(btn, 0, Qt.AlignLeft)
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(6)
            for row in folded:
                self._check_row(bl, row, show_suffix=True)
            if any(r.get("stale") for r in folded):
                bl.addWidget(_label("标为已过期的检查在其之后又有文件改动，结论不再代表当前代码。",
                                    color=_P["muted2"], size=12))
            if any(r.get("superseded") for r in folded):
                bl.addWidget(_label("标为已被复查取代的，是同一项检查更早的一次结果，以最新一次为准。",
                                    color=_P["muted2"], size=12))
            box.setVisible(False)
            col.addWidget(box)
            note = btn.text()[2:]
            btn.clicked.connect(lambda: (box.setVisible(not box.isVisible()),
                                         btn.setText(("⌄ " if box.isVisible() else "› ") + note)))

    def _add_pending(self, col):
        if not self.view.get("has_pending"):
            return
        col.addWidget(_label("待验证", color=_P["orange"], size=12, bold=True))
        text = self.view.get("pending_reason") or "存在尚未验证的改动"
        col.addWidget(_label(_soft_wrap(text), color=_P["text_sec"], size=12))

    def _add_resume(self, col):
        if not self.view.get("resumable"):
            return
        from ..result_view import resume_lines
        for line in resume_lines(self.view):
            col.addWidget(_label(_soft_wrap(line), color=_P["text_sec"], size=12))

    def _add_save_note(self, col):
        note = self.view.get("save_note")
        if not note:
            return
        # 保存问题**单独一行**，与上面的运行结论互不冒充：这一轮该失败还是失败，
        # 存不上是另一件事。
        col.addWidget(_label(_soft_wrap(note), color=_P["bad"], size=12))

    def _add_buttons(self, col):
        specs = []
        if self.view.get("resumable"):
            specs.append(("继续任务", self.continue_requested))
        if self.view.get("can_view_diff"):
            specs.append(("查看改动", self.view_diff_requested))
        if self.view.get("can_view_output"):
            specs.append(("查看验证输出", self.view_output_requested))
        if not specs:
            return
        h = QHBoxLayout()
        h.setContentsMargins(0, 4, 0, 0)
        h.setSpacing(8)
        for text, signal in specs:
            btn = QPushButton(text)
            if signal is self.continue_requested:
                self._continue_btn = btn
            btn.setCursor(Qt.PointingHandCursor)
            # 给个下限高度：卡片内容多的时候，布局会把最后一行压扁成一条空边框，
            # 按钮上的字整个消失（截图里见过）。
            btn.setMinimumHeight(30)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            if signal is self.continue_requested:
                # 没做完的卡上最该点的就是它：实心主色，和旁边的查看类按钮分开
                btn.setStyleSheet(
                    f"QPushButton {{ border:1px solid {_P['indigo']}; border-radius:8px;"
                    f" padding:5px 16px; background:{_P['indigo']}; color:{_P['view_bg']};"
                    f" font-family:{_SANS}; font-size:13px; font-weight:600; }}"
                    f"QPushButton:disabled {{ background:{_P['card_bg']}; color:{_P['muted2']};"
                    f" border-color:{_P['chip_border']}; }}")
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ border:1px solid {_P['chip_border']}; border-radius:8px;"
                    f" padding:5px 12px; background:{_P['card_bg']}; color:{_P['text3']};"
                    f" font-family:{_SANS}; font-size:12px; }}"
                    f"QPushButton:hover {{ border-color:{_P['chip_hover']}; }}")
            # 按钮带着**这张卡自己的** view 走，不去读当前前台会话——
            # 用户完全可能在点之前切到了别的会话。
            btn.clicked.connect(lambda _c=False, s=signal: s.emit(self.view))
            h.addWidget(btn)
        h.addStretch(1)
        col.addLayout(h)

    def set_continue_busy(self, busy):
        """点了继续、还没出结果：按钮暂时不可点。继续被拦下（没开出新一轮）时再放开。

        这只是界面提示；真正挡住重复启动的是 chat_window 的归属核对和 worker 屏障。
        """
        btn = self._continue_btn
        if btn is None:
            return
        btn.setEnabled(not busy)
        btn.setText("正在继续…" if busy else "继续任务")

    def mark_continued(self):
        """已经从这张卡开出了新的一轮：这张卡不再代表当前状态，按钮收起来。"""
        btn = self._continue_btn
        if btn is None:
            return
        btn.setEnabled(False)
        btn.setText("已继续")
