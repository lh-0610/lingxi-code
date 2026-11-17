"""对话区右边的文件面板：点任务面板里的改动文件，就在这里开一个标签页看它此刻的差异。

像 Claude 桌面版那样停靠在对话区右边——不弹模态、不挡对话，看文件的同时照常能读能输入：

- 一个文件一个标签页，同一个文件再点只是切过去，不会重复开；标签上有 ×，面板自己也有
  一个关闭按钮，关到没有标签时面板自动收起（对话区恢复原来的宽度）；
- 顶部一行是当前文件的完整路径，正文按行显示、带行号（新文件一侧的行号）；diff 里新增的
  行标绿、删除的行标红，hunk 头和 diff 头另给色调；代码行再做一层语法高亮（关键字、字符串、
  注释、数字、函数 / 类名分开上色，见 ui/syntax_highlight.py）——整行底色保留，颜色只叠在
  文字上；认不出语言、没装语法库或文件太大时就不高亮，照原样显示；
- 配色全部现取当前主题（亮色 / 暗色），不用模块级常量——切主题时要能跟着变；
- 左边缘可以拖动改宽度（和任务面板同一套手感），宽度由 ChatUI 记进界面偏好；
- 除了文件和浏览器，「后台」（后台任务）和「调试」（原 F12 独立窗口）也是这里的标签
  （open_widget_tab）：控件由 ChatUI 持有、常驻复用，关标签只是摘下来藏起，不销毁、不停命令。

面板只负责画：差异内容由 ChatUI 用 tools_git.file_diff_lines 取好后交进来。
"""
from __future__ import annotations

from typing import Callable

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from . import syntax_highlight
from .fonts import CAPTION, MONO_CSS, mono_font

# 每一类行的前景色主题键：新增绿、删除红，其余走常规文字色
_ROW_COLOR = {
    "add": "mv_ok",
    "del": "mv_bad",
    "hunk": "mv_indigo",
    "meta": "mv_muted",
    "ctx": "mv_text",
    "note": "mv_text_sec",
}
# 需要铺一层淡底的行：绿 / 红各按主题色和面板底色混出来，亮暗两套都能看清
_TINTED = {"add": "mv_ok", "del": "mv_bad", "hunk": "mv_indigo_soft"}


def _norm(path: str) -> str:
    """同一个文件的两种写法（反斜杠 / 正斜杠）算同一个标签页。"""
    return (path or "").replace("\\", "/")


# 浏览器标签的哨兵 tabData（不是文件路径）：paths() 只用来判面板空不空，无持久化顾虑
BROWSER_KEY = "lingxi:browser"
BACKGROUND_KEY = "lingxi:background"
DEBUG_KEY = "lingxi:debug"


def _mix(color: str, base: str, ratio: float) -> QColor:
    """把 color 按 ratio 混进 base：给新增 / 删除行铺一层很淡的同色底。"""
    c, b = QColor(color), QColor(base)
    if not c.isValid() or not b.isValid():
        return QColor(color)
    return QColor(
        round(c.red() * ratio + b.red() * (1 - ratio)),
        round(c.green() * ratio + b.green() * (1 - ratio)),
        round(c.blue() * ratio + b.blue() * (1 - ratio)),
    )


class PaneView(QPlainTextEdit):
    """一个文件的差异正文：只读、不换行（代码行不折），配色由面板按当前主题刷进来。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("filePaneView")
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QFrame.NoFrame)
        self.setFont(mono_font(CAPTION))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 只读浏览区不该抢走输入框的焦点：点了也能继续在对话里打字
        self.setFocusPolicy(Qt.NoFocus)
        # 从回复正文的带行号链接打开时要滚到的行（0 = 不滚）；重画内容后仍然滚到这里
        self.goto_line = 0

    def apply_theme(self, bg: str, fg: str):
        self.setStyleSheet(
            f"QPlainTextEdit#filePaneView {{ background: {bg}; color: {fg};"
            f" border: none; padding: 4px 0; }}"
            f"QScrollBar:horizontal, QScrollBar:vertical {{ background: transparent; }}"
        )


class FilePanel(QFrame):
    """停靠在对话区右侧的文件面板（见模块说明）。"""

    MIN_WIDTH = 320
    MAX_WIDTH = 680
    DEFAULT_WIDTH = 460
    _GRIP = 6                      # 左边缘这么宽的一条可以拖动改宽度
    DEFAULT_HINT = "此刻相对上次提交的差异（当前状态，不是哪一轮保存下来的历史）"

    closed = Signal()              # 用户关掉了面板（标签都关完了 / 点了关闭按钮）
    width_changed = Signal(int)    # 拖完松手时发，ChatUI 记进界面偏好

    def __init__(self, parent: QWidget | None, theme_lookup: Callable[[str], str]):
        super().__init__(parent)
        self.setObjectName("filePanel")
        self.theme_lookup = theme_lookup
        self.setVisible(False)
        self.setFixedWidth(self.DEFAULT_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setMouseTracking(True)          # 鼠标移到左边缘时换成左右拖动的光标
        self._drag_from = None               # 拖宽时：(按下时的全局 x, 按下时的宽度)
        # ChatUI 给：窗口放不下时最多能拖多宽
        self.max_width_fn: Callable[[], int] | None = None
        self._panes: dict[str, PaneView] = {}   # 归一化路径 → 正文
        self._rows: dict[str, list] = {}        # 归一化路径 → 交进来的逐行差异（切主题要重画）
        self._hints: dict[str, str] = {}        # 归一化路径 → 内容说明（diff / 新文件 / 无差异）
        self._spans: dict[str, dict] = {}       # 归一化路径 → {行下标: 高亮范围}（切主题不用重解析）
        self._browser_view = None               # 浏览器标签内容（第一次 open_browser 才创建，懒加载）
        self._browser_tab_label = None          # 浏览器标签文字（LeftSide 按钮，见 open_browser）
        self._browser_last_url = ""             # 手动入口复用的上次地址
        self._widget_tabs: dict[str, dict] = {}  # 后台 / 调试这类常驻控件标签：key → 控件与文案
        self._build_ui()
        self.apply_theme()

    # ── 主题 ──
    def _t(self, key: str) -> str:
        try:
            return self.theme_lookup(key)
        except Exception:
            return "#64748b"

    def apply_theme(self):
        """面板、标签条、正文都按当前主题重新上色（正文要重画：行底色是逐块格式）。"""
        bg = self._t("sidebar_bg")
        line = self._t("sidebar_border")
        text = self._t("text")
        sec = self._t("mv_text_sec")
        chip = self._t("mv_chip_border")
        self.setStyleSheet(
            f"QFrame#filePanel {{ background: {bg}; border: none; border-left: 1px solid {line}; }}"
            f"QLabel#filePanelPath {{ font-family: {MONO_CSS}; font-size: 12px; color: {text};"
            " background: transparent; }"
            f"QLabel#filePanelHint {{ font-size: 12px; color: {sec}; background: transparent; }}"
            f"QPushButton#filePanelClose {{ border: none; border-radius: 6px; background: transparent;"
            f" color: {sec}; font-size: 14px; }}"
            f"QPushButton#filePanelClose:hover {{ background: {self._t('mv_indigo_soft')}; color: {text}; }}"
            f"QPushButton#filePanelBrowser {{ border: 1px solid {chip}; border-radius: 6px;"
            f" background: transparent; color: {sec}; font-size: 12px; padding: 2px 8px; }}"
            f"QPushButton#filePanelBrowser:hover {{ background: {self._t('mv_indigo_soft')}; color: {text}; }}"
            # 标签（设计稿「右侧面板」）：平铺文字 + 选中的那个底下一条 2px 靛蓝线，整条标签栏底下一条细线
            f"QTabBar#fileTabs {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {line}; }}"
            f"QTabBar::tab {{ background: transparent; color: {sec}; padding: 0 6px; min-height: 32px; min-width: 0px;"
            f" margin-right: 2px; border: none; border-bottom: 2px solid transparent; font-size: 13px; }}"
            f"QTabBar::tab:selected {{ color: {self._t('dlg_nav_active_text')}; font-weight: 600;"
            f" border-bottom: 2px solid {self._t('dlg_nav_active_text')}; }}"
            f"QTabBar::tab:hover:!selected {{ color: {text}; }}"
            # 标签上的关闭：22px 的点击区、12px 的线性 ×，悬停给一块浅底（原来 13px 的「×」字太小）
            "QPushButton#fileTabClose { border: none; background: transparent; border-radius: 6px;"
            " padding: 0px; margin-left: 2px; }"
            f"QPushButton#fileTabClose:hover {{ background: {self._t('dlg_hover_bg')}; }}"
            f"QStackedWidget#fileStack {{ background: transparent; border: none; }}"
        )
        icon = self._close_icon()
        for btn in self.tabs.findChildren(QPushButton, "fileTabClose"):
            btn.setIcon(icon)
        for view in self._panes.values():
            view.apply_theme(self._t("mv_view_bg"), self._t("mv_text"))
            self._fill(view, self._rows.get(self._path_of(view)) or [])
        if self._browser_view is not None and shiboken6.isValid(self._browser_view):
            self._browser_view.apply_theme()
        self._sync_header()

    # ── 搭建 ──
    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)

        top = QWidget(self)
        top.setObjectName("filePanelTop")
        top.setStyleSheet("#filePanelTop { background: transparent; }")
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(6)

        self.path_label = QLabel(top)
        self.path_label.setObjectName("filePanelPath")
        self.path_label.setFont(mono_font(CAPTION))
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.path_label.setMinimumWidth(0)

        self.close_btn = QPushButton("×", top)
        self.close_btn.setObjectName("filePanelClose")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("关闭文件面板")
        self.close_btn.clicked.connect(self.close_all)

        # 用户手动开浏览器入口（复审 14）：空白页或上次地址
        self.open_browser_btn = QPushButton("浏览器", top)
        self.open_browser_btn.setObjectName("filePanelBrowser")
        self.open_browser_btn.setCursor(Qt.PointingHandCursor)
        self.open_browser_btn.setToolTip("打开浏览器标签（空白页或上次的地址）")
        self.open_browser_btn.clicked.connect(
            lambda: self.open_browser(self._browser_last_url or ""))

        top_lay.addWidget(self.path_label, 1)
        top_lay.addWidget(self.open_browser_btn, 0)
        top_lay.addWidget(self.close_btn, 0)
        lay.addWidget(top)

        self.hint_label = QLabel(top)
        self.hint_label.setObjectName("filePanelHint")
        self.hint_label.setText(self.DEFAULT_HINT)
        lay.addWidget(self.hint_label)

        self.tabs = QTabBar(self)
        self.tabs.setObjectName("fileTabs")
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setFocusPolicy(Qt.NoFocus)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        lay.addWidget(self.tabs)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("fileStack")
        lay.addWidget(self.stack, 1)

    # ── 标签页 ──
    def paths(self) -> list[str]:
        """开着的标签页（按标签顺序，原始路径写法）。"""
        return [self.tabs.tabData(i) for i in range(self.tabs.count())]

    def current_path(self) -> str:
        i = self.tabs.currentIndex()
        return self.tabs.tabData(i) if i >= 0 else ""

    def has_path(self, rel_path: str) -> bool:
        return _norm(rel_path) in self._panes

    def open_file(self, rel_path: str, rows: list, hint: str = "", line: int = 0):
        """开一个文件标签页；已经开着就切过去，并把内容刷成最新（不重复开）。

        line > 0 时滚到那一行（回复正文里带行号的链接），0 表示停在文件开头。
        """
        key = _norm(rel_path)
        if not key:
            return
        self._hints[key] = hint or self.DEFAULT_HINT
        if key in self._panes:
            view = self._panes[key]
            view.goto_line = int(line or 0)
            self._rows[key] = list(rows or [])
            self._fill(view, self._rows[key])
            self.tabs.setCurrentIndex(self._tab_index(key))
            self._sync_header()
            self._show_self()
            return
        view = PaneView(self.stack)
        view.goto_line = int(line or 0)
        view.apply_theme(self._t("mv_view_bg"), self._t("mv_text"))
        self._panes[key] = view
        self._rows[key] = list(rows or [])
        self._fill(view, self._rows[key])
        self.stack.addWidget(view)
        name = key.rsplit("/", 1)[-1] or key
        index = self.tabs.addTab(name)
        self.tabs.setTabData(index, rel_path)
        self.tabs.setTabToolTip(index, key)
        btn = self._tab_close_button(f"关闭 {name}", lambda _checked=False, p=key: self.close_file(p))
        self.tabs.setTabButton(index, QTabBar.RightSide, btn)
        self.tabs.setCurrentIndex(index)
        # addTab 就会发 currentChanged，那时 tabData 还没设上（_sync_header 读到空路径就退出了）；
        # 这里补一次，否则第一个标签的顶部路径会一直是空的。
        self._sync_header()
        self._show_self()

    def open_browser(self, url: str = ""):
        """开"浏览器"标签（已开就切过去）；url 非空则导航。第一次点开才创建 view（懒加载）。"""
        if url:
            self._browser_last_url = url
        if self._browser_view is not None and shiboken6.isValid(self._browser_view):
            index = self._tab_index(BROWSER_KEY)
            if index >= 0:
                self.tabs.setCurrentIndex(index)
            if url:
                self._browser_view.navigate(url)
            self._sync_header()
            self._show_self()
            return
        from .browser_panel import BrowserView   # 惰性 import：构造面板不碰 WebEngine
        view = BrowserView(self.stack, theme_lookup=self.theme_lookup)
        self._browser_view = view
        self.stack.addWidget(view)
        index = self.tabs.addTab("")
        # 标签文字走 LeftSide 按钮而非 tabText：ElideMiddle 对 CJK 会误判余量把
        # "浏览器"截成"..."（原生字体下实测），elide 只作用 tabText，按钮文字不受影响
        label = QLabel("浏览器", self.tabs)
        label.setStyleSheet(self._tab_label_qss(False))
        self.tabs.setTabButton(index, QTabBar.LeftSide, label)
        self._browser_tab_label = label
        self.tabs.setTabData(index, BROWSER_KEY)
        self.tabs.setTabToolTip(index, "模型可操作的侧边栏浏览器（F12 开发者工具）")
        btn = self._tab_close_button("关闭浏览器标签",
                                     lambda _checked=False: self.close_file(BROWSER_KEY))
        self.tabs.setTabButton(index, QTabBar.RightSide, btn)
        view.closed.connect(lambda: self.close_file(BROWSER_KEY))
        self.tabs.setCurrentIndex(index)
        if url:
            view.navigate(url)
        self._sync_header()
        self._show_self()

    def open_widget_tab(self, key: str, label_text: str, widget: QWidget, *, header: str,
                        hint: str, tooltip: str = ""):
        """开一个常驻控件标签（后台 / 调试）：已开就切过去。控件由调用方持有，关标签只摘下来
        藏起，不销毁——再开还是同一个控件（后台面板的选中项、调试面板的记录都还在）。"""
        index = self._tab_index(key)
        if index < 0:
            self.stack.addWidget(widget)
            index = self.tabs.addTab("")
            # 文字走 LeftSide 按钮：同浏览器标签，ElideMiddle 对 CJK 会误判余量截成"..."
            label = QLabel(label_text, self.tabs)
            label.setStyleSheet(self._tab_label_qss(False))
            self.tabs.setTabButton(index, QTabBar.LeftSide, label)
            self.tabs.setTabData(index, key)
            if tooltip:
                self.tabs.setTabToolTip(index, tooltip)
            btn = self._tab_close_button(f"收起「{label_text}」标签",
                                         lambda _checked=False, k=key: self.close_file(k))
            self.tabs.setTabButton(index, QTabBar.RightSide, btn)
            self._widget_tabs[key] = {"widget": widget, "label": label, "text": label_text,
                                      "header": header, "hint": hint, "badge": 0}
        self.tabs.setCurrentIndex(index)
        self._on_tab_changed(index)
        self._show_self()

    def set_tab_badge(self, key: str, count: int):
        """标签文字后面的数字（后台标签：还在运行的命令数）；0 不显示。"""
        entry = self._widget_tabs.get(key)
        if entry is None:
            return
        entry["badge"] = max(0, int(count or 0))
        label = entry["label"]
        if shiboken6.isValid(label):
            label.setText(f"{entry['text']} {entry['badge']}" if entry["badge"] else entry["text"])
            label.adjustSize()
            index = self._tab_index(key)
            if index >= 0:
                # 标签宽度是按放上去时的按钮大小算的；重新挂一次让标签栏按新文字重排，不然数字被裁掉
                self.tabs.setTabButton(index, QTabBar.LeftSide, label)
                label.show()          # setTabButton 会先把"旧按钮"藏起来——旧的就是它自己

    def is_tab_current(self, key: str) -> bool:
        return self.isVisible() and _norm(self.current_path()) == key

    def _detach_widget_tab(self, key: str):
        entry = self._widget_tabs.pop(key, None)
        if entry is None:
            return
        widget = entry["widget"]
        if shiboken6.isValid(widget):
            self.stack.removeWidget(widget)
            widget.hide()                 # 后台面板隐藏只停刷新，不停命令

    def close_file(self, rel_path: str):
        """关掉一个标签页；最后一个关掉时面板一起收起。"""
        key = _norm(rel_path)
        if key in self._widget_tabs:
            index = self._tab_index(key)
            if index >= 0:
                self.tabs.removeTab(index)
            self._detach_widget_tab(key)
            if self.tabs.count() == 0:
                self.close_all()
            else:
                self._on_tab_changed(self.tabs.currentIndex())
            return
        if key == BROWSER_KEY:
            index = self._tab_index(key)
            if index < 0:
                return
            view = self._browser_view
            self._browser_view = None
            self._browser_tab_label = None
            self.tabs.removeTab(index)
            if view is not None:
                self.stack.removeWidget(view)
                view.release()          # 释放 view/page，profile 常驻（§11.6）
                view.deleteLater()
            if self.tabs.count() == 0:
                self.close_all()
            else:
                self._on_tab_changed(self.tabs.currentIndex())
            return
        index = self._tab_index(key)
        if index < 0:
            return
        view = self._panes.pop(key, None)
        self._rows.pop(key, None)
        self._spans.pop(key, None)
        self._hints.pop(key, None)
        self.tabs.removeTab(index)
        if view is not None:
            self.stack.removeWidget(view)
            view.deleteLater()
        if self.tabs.count() == 0:
            self.close_all()
        else:
            self._on_tab_changed(self.tabs.currentIndex())

    def close_all(self):
        """关掉所有标签页并收起面板（对话区恢复原来的宽度）。"""
        if self._browser_view is not None:
            view = self._browser_view
            self._browser_view = None
            self._browser_tab_label = None
            self.stack.removeWidget(view)
            view.release()
            view.deleteLater()
        for key in list(self._panes):
            view = self._panes.pop(key)
            self._rows.pop(key, None)
            self._spans.pop(key, None)
            self.stack.removeWidget(view)
            view.deleteLater()
        self._hints.clear()
        while self.tabs.count():
            self.tabs.removeTab(0)
        for key in list(self._widget_tabs):
            self._detach_widget_tab(key)
        self._sync_header()
        self.setVisible(False)
        self.closed.emit()

    def _tab_index(self, key: str) -> int:
        for i in range(self.tabs.count()):
            if _norm(self.tabs.tabData(i)) == key:
                return i
        return -1

    def _path_of(self, view: PaneView) -> str:
        for key, v in self._panes.items():
            if v is view:
                return key
        return ""

    def _show_self(self):
        if not self.isVisible():
            self.setVisible(True)

    def _on_tab_changed(self, index: int):
        self.stack.setCurrentIndex(index)
        self._sync_header()

    def _close_icon(self):
        from PySide6.QtGui import QIcon
        from .dialogs import _CLOSE_GLYPH, _svg_pixmap
        dpr = self.devicePixelRatioF() if self.isVisible() else 2.0
        return QIcon(_svg_pixmap(_CLOSE_GLYPH, self._t("mv_text_sec"), 12, view=16, stroke=1.8,
                                 dpr=max(dpr, 1.0)))

    def _tab_close_button(self, tooltip: str, slot) -> QPushButton:
        """标签上的关闭按钮：线性 × 图标（跟主题换色），22px 点击区。"""
        from PySide6.QtCore import QSize
        btn = QPushButton(self.tabs)
        btn.setObjectName("fileTabClose")
        btn.setFixedSize(22, 22)
        btn.setIcon(self._close_icon())
        btn.setIconSize(QSize(12, 12))
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setAccessibleName(tooltip)
        btn.clicked.connect(slot)
        return btn

    def _tab_label_qss(self, selected: bool) -> str:
        """浏览器 / 后台 / 调试标签的文字是 LeftSide 上的 QLabel，不吃 QTabBar::tab 的颜色，
        选中态（靛蓝 + 加粗）自己刷，和文件标签的 :selected 一致。"""
        color = self._t("dlg_nav_active_text") if selected else self._t("mv_text_sec")
        weight = 600 if selected else 400
        return (f"background: transparent; font-size: 13px; padding-left: 4px;"
                f" color: {color}; font-weight: {weight};")

    def _sync_header(self):
        key = _norm(self.current_path())
        if self._browser_tab_label is not None and shiboken6.isValid(self._browser_tab_label):
            # QLabel 不继承 QTabBar::tab 的 color，选中/未选中自己刷
            self._browser_tab_label.setStyleSheet(self._tab_label_qss(key == BROWSER_KEY))
        for wkey, entry in self._widget_tabs.items():
            if shiboken6.isValid(entry["label"]):
                entry["label"].setStyleSheet(self._tab_label_qss(key == wkey))
        if key in self._widget_tabs:
            entry = self._widget_tabs[key]
            self.path_label.setText(entry["header"])
            self.path_label.setToolTip(entry["header"])
            self.hint_label.setText(entry["hint"])
            return
        if key == BROWSER_KEY:
            self.path_label.setText("浏览器")
            self.path_label.setToolTip("模型可操作的侧边栏浏览器")
            self.hint_label.setText("模型可操作的侧边栏浏览器 · F12 开发者工具")
            return
        if not key:
            self.path_label.setText("")
            self.path_label.setToolTip("")
            self.hint_label.setText(self.DEFAULT_HINT)
            return
        self.hint_label.setText(self._hints.get(key, self.DEFAULT_HINT))
        # 路径比面板宽时从中间省略：文件名和后缀保留，前面的目录不影响辨认
        avail = max(60, self.width() - 60)
        self.path_label.setText(
            self.path_label.fontMetrics().elidedText(key, Qt.ElideMiddle, avail))
        self.path_label.setToolTip(key)

    # ── 正文 ──
    def _spans_for(self, key: str, rows: list) -> dict:
        """这个文件的语法高亮范围；内容换了才重新解析（切主题只重画、不再解析一遍）。

        返回 {行下标: [(起, 止, 类别)]}，空字典表示这一份内容不高亮（语言认不出 / 没装
        语法库 / 文件太大）。
        """
        cached = self._spans.get(key)
        if cached is not None and cached[0] is rows:
            return cached[1]
        spans = syntax_highlight.highlight_rows(rows, key) if key else {}
        self._spans[key] = (rows, spans)
        return spans

    def _insert_highlighted(self, cursor: QTextCursor, text: str, runs: list,
                            base: QColor, cache: dict):
        """按 [(起, 止, 类别)] 把一行文字分段插入：没有范围的片段用 base（正文 / 新增 / 删除色）。"""
        base_fmt = QTextCharFormat()
        base_fmt.setForeground(base)
        pos = 0
        length = len(text)
        for start, end, category in runs:
            start = max(pos, min(length, start))
            end = max(start, min(length, end))
            if start > pos:
                cursor.setCharFormat(base_fmt)
                cursor.insertText(text[pos:start])
            if end > start:
                color = cache.get(category)
                if color is None:
                    color = cache[category] = QColor(self._t(syntax_highlight.token_for(category)))
                fmt = QTextCharFormat()
                fmt.setForeground(color)
                cursor.setCharFormat(fmt)
                cursor.insertText(text[start:end])
            pos = end
        if pos < length:
            cursor.setCharFormat(base_fmt)
            cursor.insertText(text[pos:])

    def _fill(self, view: PaneView, rows: list):
        """把 [(行号, 种类, 文本)] 画进正文：等宽、带行号、按种类上色。

        行号宽度按最大行号定（最少 3 位），没有行号的行（删除行、hunk 头）留空位，
        正文左边因此始终对齐。

        能高亮的行（新增 / 删除 / 上下文）文字改用语法色，整行底色和行号色调不变；
        没被语法覆盖的部分仍按行种类上色（新增绿、删除红），和以前一样。
        """
        rows = rows or []          # 不复制：_spans_for 靠对象身份判断内容有没有换
        spans = self._spans_for(self._path_of(view), rows)
        span_colors: dict[str, QColor] = {}
        bg = self._t("mv_view_bg")
        numbers = [no for no, _kind, _text in rows if isinstance(no, int)]
        width = max(3, len(str(max(numbers)))) if numbers else 0
        num_fg = QColor(self._t("mv_muted2"))
        doc = view.document()
        doc.clear()
        cursor = QTextCursor(doc)
        target_pos = None
        for index, (no, kind, text) in enumerate(rows):
            if target_pos is None and view.goto_line and no == view.goto_line:
                target_pos = cursor.position()     # 带行号打开：记下这一行的位置
            block = QTextBlockFormat()
            tint = _TINTED.get(kind)
            if tint:
                block.setBackground(QColor(self._t(tint)) if tint == "mv_indigo_soft"
                                    else _mix(self._t(tint), bg, 0.14))
            cursor.setBlockFormat(block)
            if width:
                num_fmt = QTextCharFormat()
                num_fmt.setForeground(num_fg)
                cursor.setCharFormat(num_fmt)
                cursor.insertText((str(no).rjust(width) if isinstance(no, int) else " " * width)
                                  + " │ ")
            runs = spans.get(index) or []
            row_fg = QColor(self._t(_ROW_COLOR.get(kind, "mv_text")))
            self._insert_highlighted(cursor, text, runs, row_fg, span_colors)
            cursor.insertBlock()
        cursor.setCharFormat(QTextCharFormat())
        if target_pos is None:
            view.moveCursor(QTextCursor.Start)
            view.ensureCursorVisible()
            return
        # 行号落到某一行：把这一行滚到可见区域上方一点，看得出上下文
        target = QTextCursor(doc)
        target.setPosition(target_pos)
        view.setTextCursor(target)
        view.centerCursor()
        view.ensureCursorVisible()

    # ── 拖左边缘改宽度 ──
    def is_dragging(self) -> bool:
        """正在拖左边缘改宽度：这期间别让外部按可用空间改写宽度（会跟手抢）。"""
        return self._drag_from is not None

    def _max_width(self) -> int:
        limit = self.MAX_WIDTH
        if self.max_width_fn is not None:
            try:
                limit = min(limit, int(self.max_width_fn()))
            except Exception:
                pass
        return max(self.MIN_WIDTH, limit)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().x() <= self._GRIP:
            self._drag_from = (event.globalPosition().x(), self.width())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_from is not None:
            start_x, start_w = self._drag_from
            width = int(start_w + (start_x - event.globalPosition().x()))   # 往左拖变宽
            self.setFixedWidth(max(self.MIN_WIDTH, min(self._max_width(), width)))
            event.accept()
            return
        self.setCursor(Qt.SizeHorCursor if event.position().x() <= self._GRIP else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_from is not None:
            self._drag_from = None
            self._sync_header()          # 宽度变了，路径按新宽度重新省略
            self.width_changed.emit(self.width())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        # 窗口变窄时 ChatUI 会压窄面板：路径按新宽度重新省略
        super().resizeEvent(event)
        self._sync_header()

    def leaveEvent(self, event):
        if self._drag_from is None:
            self.unsetCursor()
        super().leaveEvent(event)
