"""运行结果卡（Qt 控件）。

只负责把 `src/result_view.describe()` 算好的结论画出来——**判断在那边，画在这边**。
分开的理由很实际：卡片上"能不能说检查通过了"是要被逐条测试钉死的判断，
混在布局代码里就只能靠截图去验。

配色沿用 `message_view._P` 的设计 token，跟消息流保持同一套观感。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
)

from .message_view import _P, _SANS, _MONO


# 状态色调 → (左侧色条, 标题色)。用色条而不是整卡染色：结果卡常和失败输出挨着，
# 整片色块会把消息流搞得很吵。
_TONE = {
    "ok":          (_P["indigo"], _P["text"]),
    "warn":        (_P["orange"], _P["text"]),
    "bad":         ("#d64545", _P["text"]),
    "muted":       (_P["muted3"], _P["text2"]),
    "interrupted": (_P["orange"], _P["text"]),
    "running":     (_P["indigo"], _P["text2"]),
}

_STATUS_COLOR = {
    "passed": "#3a8a5f",
    "failed": "#d64545",
    "timeout": _P["orange"],
    "not_run": _P["muted"],
    "cancelled": _P["muted"],
    "error": "#d64545",
    "unknown": _P["muted"],
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
    out = []
    for line in text.splitlines() or [""]:
        while len(line) > chunk:
            out.append(line[:chunk])
            line = line[chunk:]
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


class ResultCard(QFrame):
    """一轮运行的结果卡。所有文案来自 `result_view.describe()` 的返回。"""

    view_diff_requested = Signal(object)     # 携带本卡的 view dict（含会话/项目/run_id）
    view_output_requested = Signal(object)

    def __init__(self, view: dict, parent=None):
        super().__init__(parent)
        self.view = dict(view or {})
        self.setObjectName("resultCard")
        accent, title_color = _TONE.get(self.view.get("tone"), _TONE["muted"])
        self.setStyleSheet(
            f"#resultCard {{ background:{_P['result_bg']}; border:1px solid {_P['card_border']};"
            f" border-left:3px solid {accent}; border-radius:10px; }}")

        col = QVBoxLayout(self)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(6)

        col.addWidget(_label(self.view.get("title", ""), color=title_color, size=14, bold=True))

        if self.view.get("display_source") == "restored":
            # 重绘出来的历史结果：说清它是上一次的，别让用户以为刚刚又跑了一轮。
            col.addWidget(_label("（上次运行的结果）", color=_P["muted2"], size=11))

        if self.view.get("reason"):
            col.addWidget(_label(_soft_wrap(self.view["reason"]),
                                 color=_P["text_sec"], size=12))

        self._add_files(col)
        self._add_validations(col)
        self._add_pending(col)
        self._add_save_note(col)
        self._add_buttons(col)

    # ── 各区块 ──

    def _add_files(self, col):
        files = self.view.get("files") or []
        if not files:
            return
        col.addWidget(_label(self.view.get("files_caption", ""),
                             color=_P["text_sec"], size=12, bold=True))
        shown = files if not self.view.get("files_folded") else files[:6]
        col.addWidget(_label(_paths_text(shown), color=_P["text_sec"], size=12, mono=True))
        if self.view.get("files_folded"):
            rest = files[6:]
            more = _label(f"…… 另有 {len(rest)} 个", color=_P["muted2"], size=11)
            col.addWidget(more)
            box = _label(_paths_text(rest), color=_P["text_sec"], size=12, mono=True)
            box.setVisible(False)
            col.addWidget(box)
            btn = QPushButton("展开全部")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"border:none; background:transparent; color:{_P['indigo']}; font-size:11px;")
            btn.clicked.connect(lambda: (box.setVisible(not box.isVisible()),
                                         btn.setText("收起" if box.isVisible() else "展开全部")))
            col.addWidget(btn, 0, Qt.AlignLeft)

    def _add_validations(self, col):
        rows = self.view.get("validations") or []
        if not rows:
            # 纯问答不画空的检查面板——一个"验证：（空）"的框只会让人以为哪里出错了。
            return
        col.addWidget(_label("实际执行的检查", color=_P["text_sec"], size=12, bold=True))
        for row in rows:
            # 直接 addLayout，**不套一层 QWidget**：包一层的话那个壳自己不传递
            # heightForWidth，整张卡的高度被算少，底部按钮被卡片边框切掉半截
            # （截图里见过，而且只在检查行多的卡上出现）。
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(8)
            target = f"{row['checker']}"
            if row.get("path"):
                # 路径同样要中间省略：它没有空格，QLabel 断不开，整行的 minimumSizeHint
                # 就是那条路径的完整宽度，把外面的 HBox 连同整张卡一起撑出可视区。
                target += f" · {_elide_middle(row['path'])}"
            h.addWidget(_label(target, color=_P["text3"], size=12), 1)
            # 被后一次复查取代的旧结论要标出来，否则卡上会并排摆着
            # "失败" 和 "通过" 两个矛盾的结果，用户无从判断哪个算数。
            if row.get("stale"):
                suffix = "（已过期）"
            elif row.get("superseded"):
                suffix = "（已被复查取代）"
            else:
                suffix = ""
            state = row["label"] + suffix
            color = (_P["muted"] if (row.get("stale") or row.get("superseded"))
                     else _STATUS_COLOR.get(row["status"], _P["muted"]))
            h.addWidget(_label(state, color=color, size=12, bold=True, wrap=False,
                               shrink=False), 0)
            col.addLayout(h)
            detail = row.get("detail") or ""
            if row.get("summary"):
                detail = f"{detail} · {row['summary'].splitlines()[0]}" if detail else row["summary"]
            if detail:
                # 摘要只占**一行**并中间省略：它是等宽字体，一旦换行成好几行就是整张卡里
                # 最宽的东西，窄窗口下把卡片顶出可视区。完整输出在「查看验证输出」里。
                one_line = detail.replace("\n", " ")
                col.addWidget(_label(_elide_middle(one_line, 40), color=_P["muted2"],
                                     size=11, mono=True, wrap=False))
        if self.view.get("has_stale"):
            col.addWidget(_label("标为已过期的检查在其之后又有文件改动，结论不再代表当前代码。",
                                 color=_P["orange"], size=11))
        if self.view.get("has_superseded"):
            col.addWidget(_label("标为已被复查取代的，是同一项检查更早的一次结果，以最新一次为准。",
                                 color=_P["muted2"], size=11))

    def _add_pending(self, col):
        if not self.view.get("has_pending"):
            return
        col.addWidget(_label("待验证", color=_P["orange"], size=12, bold=True))
        text = self.view.get("pending_reason") or "存在尚未验证的改动"
        col.addWidget(_label(_soft_wrap(text), color=_P["text_sec"], size=12))

    def _add_save_note(self, col):
        note = self.view.get("save_note")
        if not note:
            return
        # 保存问题**单独一行**，与上面的运行结论互不冒充：这一轮该失败还是失败，
        # 存不上是另一件事。
        col.addWidget(_label(_soft_wrap(f"⚠️ {note}"), color="#d64545", size=11))

    def _add_buttons(self, col):
        specs = []
        if self.view.get("can_view_diff"):
            specs.append(("查看改动", self.view_diff_requested))
        if self.view.get("can_view_output"):
            specs.append(("查看验证输出", self.view_output_requested))
        # B04 还没做，所以这里**不放**"继续任务"——放一个点不动的按钮比没有更糟。
        if not specs:
            return
        h = QHBoxLayout()
        h.setContentsMargins(0, 4, 0, 0)
        h.setSpacing(8)
        for text, signal in specs:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            # 给个下限高度：卡片内容多的时候，布局会把最后一行压扁成一条空边框，
            # 按钮上的字整个消失（截图里见过）。
            btn.setMinimumHeight(30)
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
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
