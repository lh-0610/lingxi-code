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


# 浏览器标签的哨兵 tabData（不是文件路径）：paths() 只用来判面板空不空，无持久化顾虑。
# 每个网页一个标签：lingxi:browser#<标签 id>；单独的 BROWSER_KEY 指「当前那个网页标签」
BROWSER_KEY = "lingxi:browser"


def browser_key(tab_id) -> str:
    return f"{BROWSER_KEY}#{tab_id}"


def is_browser_key(key) -> bool:
    return bool(key) and (key == BROWSER_KEY or str(key).startswith(BROWSER_KEY + "#"))


def browser_tab_id(key):
    try:
        return int(str(key).split("#", 1)[1])
    except (IndexError, ValueError):
        return None
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


class _BrowserTabLabel(QWidget):
    """浏览器标签的左侧内容：页面图标 + 标题（过长省略）+ 模型操作中的小圆点。"""

    MAX_TITLE_PX = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("browserTabLabel")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)
        self.icon = QLabel(self)
        self.icon.setFixedSize(14, 14)
        self.icon.setScaledContents(True)
        self.title = QLabel("新标签页", self)
        self.dot = QLabel(self)
        self.dot.setFixedSize(7, 7)
        self.dot.setVisible(False)
        lay.addWidget(self.icon)
        lay.addWidget(self.title)
        lay.addWidget(self.dot)
        self._full = "新标签页"

    def text(self) -> str:
        return self._full

    def set_state(self, title: str, icon, *, busy: bool, failed: bool):
        self._full = title or "新标签页"
        fm = self.title.fontMetrics()
        self.title.setText(fm.elidedText(self._full, Qt.ElideRight, self.MAX_TITLE_PX))
        if icon is not None and not icon.isNull():
            self.icon.setPixmap(icon.pixmap(28, 28))
        self.dot.setVisible(busy and not failed)
        self.adjustSize()

    def apply(self, qss: str, theme):
        self.title.setStyleSheet(qss)
        self.dot.setStyleSheet(f"background: {theme('dlg_input_focus')}; border-radius: 3px;")
        self.adjustSize()


class FilePanel(QFrame):
    """停靠在对话区右侧的文件面板（见模块说明）。"""

    MIN_WIDTH = 320
    MAX_WIDTH = 680
    DEFAULT_WIDTH = 460
    _GRIP = 6                      # 左边缘这么宽的一条可以拖动改宽度
    DEFAULT_HINT = "此刻相对上次提交的差异（当前状态，不是哪一轮保存下来的历史）"

    closed = Signal()              # 用户关掉了面板（标签都关完了 / 点了关闭按钮）
    browser_stop_requested = Signal()   # 浏览器状态卡「停止」→ ChatUI 停驱动浏览器的那个会话
    background_requested = Signal()     # 浏览器错误页「去后台看看」→ ChatUI 开后台标签
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
        # 浏览器：每个网页一个标签（key = lingxi:browser#<标签 id>），标签 id → (BrowserView, 标签文字)
        self._bviews: dict[int, tuple] = {}
        self._browser_last_url = ""             # 手动入口复用的上次地址
        self._widget_tabs: dict[str, dict] = {}  # 后台 / 调试这类常驻控件标签：key → 控件与文案
        self._build_ui()
        self.apply_theme()
        # 浏览器开 / 关标签、当前标签换了：面板跟着变（模型点开新标签也看得见）。
        # page 模块顶层不碰 WebEngine，这里接信号不会把它拉起来
        from ..browser import page as _bpage
        _bpage._notifier.tab_added.connect(self._on_browser_tab_added)
        _bpage._notifier.tab_closed.connect(self._on_browser_tab_closed)
        _bpage._notifier.current_changed.connect(self._on_browser_current_changed)
        _bpage._notifier.popup_blocked.connect(self._on_popup_blocked)

    # ── 浏览器标签的兼容入口：只有一个浏览器标签时代的调用方 / 测试 ──
    @property
    def _browser_view(self):
        """当前浏览器标签的视图（没开浏览器是 None）。"""
        entry = self._bviews.get(self._current_browser_id())
        return entry[0] if entry else None

    @property
    def _browser_tab_label(self):
        entry = self._bviews.get(self._current_browser_id())
        return entry[1] if entry else None

    def _current_browser_id(self):
        from ..browser import page as _bpage
        cur = _bpage.current_tab_id()
        if cur in self._bviews:
            return cur
        return next(iter(self._bviews), None)

    def has_browser(self) -> bool:
        return bool(self._bviews)

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
        self.setStyleSheet(
            f"QFrame#filePanel {{ background: {bg}; border: none; border-left: 1px solid {line}; }}"
            f"QLabel#filePanelPath {{ font-family: {MONO_CSS}; font-size: 12px; color: {text};"
            " background: transparent; }"
            f"QLabel#filePanelHint {{ font-size: 12px; color: {sec}; background: transparent; }}"
            f"QPushButton#filePanelClose {{ border: none; border-radius: 6px; background: transparent;"
            f" color: {sec}; font-size: 14px; }}"
            f"QPushButton#filePanelClose:hover {{ background: {self._t('mv_indigo_soft')}; color: {text}; }}"
            # 标签（设计稿「右侧面板」）：平铺文字 + 选中的那个底下一条 2px 靛蓝线，整条标签栏底下一条细线
            f"QWidget#filePanelTabRow {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {line}; }}"
            "QTabBar#fileTabs { background: transparent; border: none; }"
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
        from PySide6.QtCore import QSize as _QSize
        self.close_btn.setIcon(self._close_icon(14))
        self.close_btn.setIconSize(_QSize(14, 14))
        for view in self._panes.values():
            view.apply_theme(self._t("mv_view_bg"), self._t("mv_text"))
            self._fill(view, self._rows.get(self._path_of(view)) or [])
        for view, _label in self._bviews.values():
            if shiboken6.isValid(view):
                view.apply_theme()
        self._sync_header()

    # ── 搭建 ──
    def _build_ui(self):
        # 设计稿「右侧面板」：最上面一行是标签条（右端「浏览器」入口和收起 ×），
        # 文件路径 / 说明只在文件标签下出现（浏览器 / 后台 / 调试有自己的工具栏）
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 8)
        lay.setSpacing(6)

        tab_row = QWidget(self)
        tab_row.setObjectName("filePanelTabRow")
        tab_row.setAttribute(Qt.WA_StyledBackground, True)
        tab_lay = QHBoxLayout(tab_row)
        tab_lay.setContentsMargins(0, 0, 0, 0)
        tab_lay.setSpacing(4)
        self.tab_row = tab_row

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

        self.close_btn = QPushButton(tab_row)
        self.close_btn.setObjectName("filePanelClose")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("关闭右侧面板（标签都会关掉；后台命令不会停）")
        self.close_btn.setAccessibleName("收起面板")
        self.close_btn.clicked.connect(self.close_all)

        top_lay.addWidget(self.path_label, 1)

        self.hint_label = QLabel(top)
        self.hint_label.setObjectName("filePanelHint")
        self.hint_label.setText(self.DEFAULT_HINT)

        self.tabs = QTabBar(tab_row)
        self.tabs.setObjectName("fileTabs")
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        self.tabs.setElideMode(Qt.ElideMiddle)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setFocusPolicy(Qt.NoFocus)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        tab_lay.addWidget(self.tabs, 1)
        tab_lay.addWidget(self.close_btn, 0, Qt.AlignVCenter)
        lay.addWidget(tab_row)

        # 文件标签才有的路径 + 说明
        self.file_header = QWidget(self)
        self.file_header.setObjectName("filePanelFileHeader")
        self.file_header.setStyleSheet("#filePanelFileHeader { background: transparent; }")
        fh = QVBoxLayout(self.file_header)
        fh.setContentsMargins(0, 2, 0, 0)
        fh.setSpacing(2)
        fh.addWidget(top)
        fh.addWidget(self.hint_label)
        lay.addWidget(self.file_header)

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
        """开浏览器（已开就切到当前那个网页标签）；url 非空则在当前标签导航。
        浏览器第一次打开才创建（懒加载）；每个网页一个标签，见 _ensure_browser_view。"""
        if url:
            self._browser_last_url = url
        from ..browser import page as _bpage
        b = _bpage.get_browser()
        for tid in list(b.tabs):
            self._ensure_browser_view(tid)
        cur = b.current_id
        index = self._tab_index(browser_key(cur))
        if index >= 0:
            self.tabs.setCurrentIndex(index)
        entry = self._bviews.get(cur)
        if url and entry is not None:
            entry[0].navigate(url)
        self._sync_header()
        self._show_self()

    def _ensure_browser_view(self, tab_id):
        """给浏览器的一个标签建面板标签（已有就不动）。返回是否新建了。"""
        from ..browser import page as _bpage
        b = _bpage._browser
        if tab_id in self._bviews or b is None or b.tab(tab_id) is None:
            return False
        from .browser_panel import BrowserView   # 惰性 import：构造面板不碰 WebEngine
        view = BrowserView(self.stack, theme_lookup=self.theme_lookup, tab_id=tab_id)
        self.stack.addWidget(view)
        key = browser_key(tab_id)
        # 新网页标签排在最后一个网页标签后面（文件标签不夹在中间）
        last = max((i for i in range(self.tabs.count()) if is_browser_key(self.tabs.tabData(i))),
                   default=self.tabs.count() - 1)
        index = self.tabs.insertTab(last + 1, "")
        # insertTab 的位置和 stack 的顺序要一致：stack 按标签下标切
        self.stack.removeWidget(view)
        self.stack.insertWidget(index, view)
        # 标签内容走 LeftSide 按钮而非 tabText：ElideMiddle 对 CJK 会误判余量截成"..."
        # （原生字体下实测）。设计稿：页面图标 + 页面标题 + 模型操作中的小圆点
        label = _BrowserTabLabel(self.tabs)
        self.tabs.setTabButton(index, QTabBar.LeftSide, label)
        self.tabs.setTabData(index, key)
        self.tabs.setTabToolTip(index, "模型可操作的侧边栏浏览器（F12 开发者工具）")
        btn = self._tab_close_button("关闭这个网页",
                                     lambda _checked=False, k=key: self.close_file(k))
        self.tabs.setTabButton(index, QTabBar.RightSide, btn)
        self._bviews[tab_id] = (view, label)
        view.closed.connect(lambda t=tab_id: self._drop_browser_view(t))
        view.tab_state_changed.connect(lambda t=tab_id: self._refresh_browser_tab(t))
        view.stop_requested.connect(self.browser_stop_requested)
        view.background_requested.connect(self.background_requested)
        # 「更多」里的关闭浏览器：整个浏览器（所有网页）
        view.close_requested.connect(lambda: self.close_file(BROWSER_KEY))
        self._refresh_browser_tab(tab_id)
        return True

    def _drop_browser_view(self, tab_id):
        """浏览器那边已经关了这个标签：只拆面板上的标签和视图（不再去关一次）。"""
        entry = self._bviews.pop(tab_id, None)
        if entry is None:
            return
        view, _label = entry
        index = self._tab_index(browser_key(tab_id))
        if index >= 0:
            self.tabs.removeTab(index)
        if shiboken6.isValid(view):
            self.stack.removeWidget(view)
            view.detach()
            view.deleteLater()
        if self.tabs.count() == 0:
            self.close_all()
        else:
            self._on_tab_changed(self.tabs.currentIndex())

    def _on_browser_tab_added(self, tab_id):
        from ..browser import page as _bpage
        if not self.isVisible() and not self._bviews:
            return        # 面板还没开浏览器：等 open_browser 一起建（browser_created 会开）
        if self._ensure_browser_view(tab_id) and _bpage.current_tab_id() == tab_id:
            index = self._tab_index(browser_key(tab_id))
            if index >= 0:
                self.tabs.setCurrentIndex(index)     # 模型点开的新标签：切过去让用户看见
            self._show_self()

    def _on_browser_tab_closed(self, tab_id):
        self._drop_browser_view(tab_id)

    def _on_browser_current_changed(self, tab_id):
        """模型切了标签：用户正看着浏览器就跟着切（看着文件时不打断）。"""
        if not is_browser_key(self.current_path()):
            return
        index = self._tab_index(browser_key(tab_id))
        if index >= 0 and index != self.tabs.currentIndex():
            self.tabs.setCurrentIndex(index)

    def _on_popup_blocked(self, url):
        view = self._browser_view
        if view is not None and shiboken6.isValid(view):
            view.flash(f"页面自己弹出的新窗口已拦截：{url}")

    def open_last_browser(self):
        """用户手动开浏览器（顶栏的地球按钮，复审 14）：已开就切过去；关过就复用上次地址，没有就空白页。"""
        self.open_browser(self._browser_last_url or "")

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
            # 整个浏览器（所有网页）：各标签的视图由 tab_closed 通知拆掉
            if not self._bviews:
                return
            for view, _label in list(self._bviews.values()):
                if shiboken6.isValid(view):
                    view.detach()
            from ..browser import page as _bpage
            _bpage.close_browser()        # 释放 view/page，profile 常驻（§11.6）
            for tid in list(self._bviews):
                self._drop_browser_view(tid)
            return
        if is_browser_key(key):
            # 一个网页：浏览器那边关掉它（最后一个等于关浏览器），面板标签随 tab_closed 拆
            tid = browser_tab_id(key)
            entry = self._bviews.get(tid)
            if entry is None:
                return
            entry[0].release()
            self._drop_browser_view(tid)
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
        if self._bviews:
            views = [v for v, _l in self._bviews.values()]
            self._bviews.clear()
            for view in views:
                if shiboken6.isValid(view):
                    self.stack.removeWidget(view)
                    view.detach()
                    view.deleteLater()
            from ..browser import page as _bpage
            _bpage.close_browser()
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
        if key == BROWSER_KEY:          # 「当前那个网页标签」
            cur = self._current_browser_id()
            if cur is None:
                return -1
            key = browser_key(cur)
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
        key = self.tabs.tabData(index) if index >= 0 else ""
        if is_browser_key(key) and browser_tab_id(key) is not None:
            # 用户点了某个网页标签：模型之后的操作也作用于它（双向同步）
            from ..browser import page as _bpage
            _bpage.set_current_tab(browser_tab_id(key))
        self._sync_header()

    def _close_icon(self, size: int = 12):
        from PySide6.QtGui import QIcon
        from .dialogs import _CLOSE_GLYPH, _svg_pixmap
        dpr = self.devicePixelRatioF() if self.isVisible() else 2.0
        return QIcon(_svg_pixmap(_CLOSE_GLYPH, self._t("mv_text_sec"), size, view=16, stroke=1.8,
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

    def _refresh_browser_tab(self, tab_id=None):
        """网页标签跟着页面走：标题、图标、出错、模型操作中的小圆点。"""
        if tab_id is None:
            tab_id = self._current_browser_id()
        entry = self._bviews.get(tab_id)
        if entry is None:
            return
        view, label = entry
        if not shiboken6.isValid(label) or not shiboken6.isValid(view):
            return
        label.set_state(view.tab_title(), view.tab_icon(), busy=view.model_active,
                        failed=view.failed)
        index = self._tab_index(browser_key(tab_id))
        if index >= 0:
            p = view.page_url()
            tip = (view.tab_title() + "\n" + p) if p and p != "about:blank" \
                else "模型可操作的侧边栏浏览器（F12 开发者工具）"
            self.tabs.setTabToolTip(index, tip)
            # 宽度跟着标题变：重挂一次让标签栏重排（setTabButton 会先藏"旧按钮"，就是它自己）
            self.tabs.setTabButton(index, QTabBar.LeftSide, label)
            label.show()

    def _tab_label_qss(self, selected: bool) -> str:
        """浏览器 / 后台 / 调试标签的文字是 LeftSide 上的 QLabel，不吃 QTabBar::tab 的颜色，
        选中态（靛蓝 + 加粗）自己刷，和文件标签的 :selected 一致。"""
        color = self._t("dlg_nav_active_text") if selected else self._t("mv_text_sec")
        weight = 600 if selected else 400
        return (f"background: transparent; font-size: 13px; padding-left: 4px;"
                f" color: {color}; font-weight: {weight};")

    def _sync_header(self):
        key = _norm(self.current_path())
        self.file_header.setVisible(bool(key) and not is_browser_key(key)
                                    and key not in self._widget_tabs)
        for tid, (_view, label) in self._bviews.items():
            if shiboken6.isValid(label):
                # 标签上的 QLabel 不继承 QTabBar::tab 的 color，选中/未选中自己刷
                label.apply(self._tab_label_qss(key == browser_key(tid)), self._t)
        for wkey, entry in self._widget_tabs.items():
            if shiboken6.isValid(entry["label"]):
                entry["label"].setStyleSheet(self._tab_label_qss(key == wkey))
        if key in self._widget_tabs:
            entry = self._widget_tabs[key]
            self.path_label.setText(entry["header"])
            self.path_label.setToolTip(entry["header"])
            self.hint_label.setText(entry["hint"])
            return
        if is_browser_key(key):
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
