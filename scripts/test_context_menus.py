"""中文、跟随主题的右键菜单（src/ui/context_menus.py + 浏览器网页菜单）。

菜单是 exec() 阻塞的：真实右键的用例先挂零秒定时器，等弹出菜单出现后记下它的项再关掉。
"""
import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import (
    QApplication, QLabel, QLineEdit, QPlainTextEdit, QTextEdit, QWidget,
)

from src import state
from src.ui import context_menus as cm
from src.ui import theme


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    gc.collect()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def _light():
    before = theme.active_theme()
    theme.set_active_theme("light")
    yield
    theme.set_active_theme(before)


def items(menu):
    """[(文字, 快捷键, 可用)]，分隔线记成 '-'。"""
    out = []
    for a in menu.actions():
        if a.isSeparator():
            out.append("-")
            continue
        text, _, sc = a.text().partition("\t")
        out.append((text, sc, a.isEnabled()))
    return out


def labels(menu):
    return [i[0] if i != "-" else "-" for i in items(menu)]


def enabled(menu, text):
    return next(i[2] for i in items(menu) if i != "-" and i[0] == text)


def trigger(menu, text):
    next(a for a in menu.actions() if a.text().partition("\t")[0] == text).trigger()


class _Host:
    def __init__(self):
        self.quoted, self.opened = [], []

    def context_menu_quote(self, text):
        self.quoted.append(text)

    def context_menu_open_link(self, href):
        self.opened.append(href)

    def open_link_in_panel(self, href):
        self.opened.append(("panel", href))

    def open_link_in_system(self, href):
        self.opened.append(("system", href))


@pytest.fixture()
def host(qapp):
    h = _Host()
    cm.install(qapp, host=h)
    yield h
    cm._host_ref = None


# ── 文本控件 ──────────────────────────────────────────────────────

def test_line_edit_menu_is_chinese_with_shortcuts_and_states(qapp):
    w = QLineEdit()
    w.setText("hello")
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert labels(menu) == ["撤销", "重做", "-", "剪切", "复制", "粘贴", "-", "全选"]
    assert ("撤销", "Ctrl+Z", False) in items(menu)          # 没改过 → 不能撤销
    assert not enabled(menu, "复制"), "没选中不能复制"
    assert menu.objectName() == "lxContextMenu"
    assert theme.THEMES["light"]["menu_sel_bg"] in menu.styleSheet()
    w.selectAll()
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert enabled(menu, "复制") and enabled(menu, "剪切")
    QApplication.clipboard().setText("")
    trigger(menu, "复制")
    assert QApplication.clipboard().text() == "hello"


def test_password_field_never_offers_copy(qapp):
    w = QLineEdit()
    w.setEchoMode(QLineEdit.Password)
    w.setText("secret")
    w.selectAll()
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert not enabled(menu, "复制") and not enabled(menu, "剪切")


def test_rich_text_edit_offers_paste_as_plain_text(qapp):
    w = QTextEdit()
    QApplication.clipboard().setText("纯文本")
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert "粘贴为纯文本" in labels(menu)
    trigger(menu, "粘贴为纯文本")
    assert w.toPlainText() == "纯文本"
    plain = QTextEdit()
    plain.setAcceptRichText(False)
    assert "粘贴为纯文本" not in labels(cm.build_text_menu(plain, QPoint(1, 1)))


def test_read_only_text_only_copies(qapp, host):
    w = QPlainTextEdit()
    w.setPlainText("输出一行")
    w.setReadOnly(True)
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert labels(menu) == ["复制", "-", "全选"]
    w.selectAll()
    menu = cm.build_text_menu(w, QPoint(1, 1))
    assert labels(menu) == ["复制", "引用到输入框", "-", "全选"]
    trigger(menu, "引用到输入框")
    assert host.quoted == ["输出一行"]


def test_chat_label_copy_markdown_quote_and_link(qapp, host):
    lb = QLabel()
    lb.setTextFormat(Qt.RichText)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
    lb.setText('已启动，地址是 <a href="http://localhost:5173/">http://localhost:5173/</a>')
    lb.setProperty("lx_markdown", "已启动，地址是 http://localhost:5173/")
    lb.setProperty("lx_hover_link", "http://localhost:5173/")
    lb.setSelection(0, 3)
    menu = cm.build_text_menu(lb, QPoint(1, 1))
    assert labels(menu) == ["复制", "复制为 Markdown", "引用到输入框", "-",
                            "在侧栏浏览器打开", "用系统浏览器打开", "-", "复制链接地址",
                            "复制链接文字", "-", "全选"]
    trigger(menu, "复制为 Markdown")
    assert QApplication.clipboard().text() == "已启动，地址是 http://localhost:5173/"
    trigger(menu, "在侧栏浏览器打开")
    assert host.opened == [("panel", "http://localhost:5173/")]
    trigger(menu, "复制链接文字")
    assert QApplication.clipboard().text() == "http://localhost:5173/"
    trigger(menu, "全选")
    assert lb.selectedText().startswith("已启动")


def test_public_link_offers_both_ways_to_open(qapp, host):
    """设计稿「回复里的链接」：左键按设置走，右键两种打开方式都给，外加复制链接文字（标题）。"""
    lb = QLabel()
    lb.setTextFormat(Qt.RichText)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lb.setText('见 <a href="https://example.com/a?x=1&amp;y=2" style="color:#000">'
               '示例官网<span>&#8201;↗</span></a>')
    lb.setProperty("lx_hover_link", "https://example.com/a?x=1&y=2")
    menu = cm.build_text_menu(lb, QPoint(1, 1))
    assert labels(menu)[2:6] == ["在侧栏浏览器打开", "用系统浏览器打开", "-", "复制链接地址"]
    trigger(menu, "用系统浏览器打开")
    assert host.opened == [("system", "https://example.com/a?x=1&y=2")]
    trigger(menu, "复制链接文字")
    assert QApplication.clipboard().text() == "示例官网", "去掉箭头，只要标题"


# ── 全局过滤器：真实右键事件 ─────────────────────────────────────

def _right_click(qapp, target, widget_for_pos=None):
    """发一个真实的 ContextMenu 事件，弹出的菜单出现后记下它的项并关掉。"""
    seen = []

    def grab():
        popup = QApplication.activePopupWidget()
        if popup is None:
            QTimer.singleShot(10, target, grab)
            return
        seen.append((popup.objectName(), labels(popup)))
        popup.close()

    QTimer.singleShot(0, target, grab)
    QTimer.singleShot(3000, target, lambda: QApplication.activePopupWidget() and
                      QApplication.activePopupWidget().close())
    w = widget_for_pos or target
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), w.mapToGlobal(QPoint(5, 5)))
    QApplication.sendEvent(w, ev)
    return seen


def test_filter_replaces_default_menu_of_line_edit(qapp, host):
    w = QLineEdit()
    w.show()
    seen = _right_click(qapp, w)
    assert seen and seen[0][0] == "lxContextMenu" and "全选" in seen[0][1]
    w.deleteLater()


def test_filter_handles_text_edit_viewport(qapp, host):
    w = QPlainTextEdit()
    w.show()
    seen = _right_click(qapp, w, w.viewport())
    assert seen and seen[0][0] == "lxContextMenu"
    w.deleteLater()


def test_widgets_with_their_own_menu_are_left_alone(qapp, host):
    w = QLineEdit()
    w.setContextMenuPolicy(Qt.CustomContextMenu)
    asked = []
    w.customContextMenuRequested.connect(lambda p: asked.append(p))
    w.show()
    ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5), w.mapToGlobal(QPoint(5, 5)))
    QApplication.sendEvent(w, ev)
    assert asked, "自定义菜单照常走自己的"
    assert QApplication.activePopupWidget() is None
    w.deleteLater()


def test_non_selectable_label_is_ignored(qapp, host):
    lb = QLabel("普通文字")
    assert cm._text_target(lb) is None


# ── ChatUI：引用到输入框 / 聊天正文带源文 ───────────────────────────

def _destroy(ui, app):
    import shiboken6
    if not shiboken6.isValid(ui):
        return
    if state.ui_ref is ui:
        state.ui_ref = None
    ui.close()
    app.processEvents()
    ui.deleteLater()
    gc.collect()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture()
def chat_ui(qapp, monkeypatch, isolated_memory):
    from src import tools
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    yield ui
    _destroy(ui, qapp)
    cm._host_ref = None


def test_quote_to_input_box(chat_ui):
    """引用进输入框上方的卡片（设计稿「消息区：引用」），不往框里塞「> 」；
    发送时拼成 Markdown 引用。"""
    chat_ui.entry.setPlainText("看看这个")
    chat_ui.context_menu_quote("第一行\n\n第二行")
    assert chat_ui.entry.toPlainText() == "看看这个"
    assert chat_ui._pending_quotes == ["第一行\n\n第二行"]
    assert chat_ui._compose_input() == "> 第一行\n>\n> 第二行\n\n看看这个"
    assert cm._host() is chat_ui


def test_chat_body_keeps_markdown_source_and_hovered_link(chat_ui, qapp):
    from src.ui.message_view import BodyText
    chat_ui._render_markdown("**粗体** 和 `代码`")
    qapp.processEvents()
    bodies = chat_ui.chat_area.findChildren(BodyText)
    assert bodies and bodies[-1].property("lx_markdown") == "**粗体** 和 `代码`"
    bodies[-1].linkHovered.emit("http://localhost:5173/")
    assert bodies[-1].property("lx_hover_link") == "http://localhost:5173/"


# ── 网页右键菜单 ──────────────────────────────────────────────────

class _FakeReq:
    def __init__(self, link="", sel="", editable=False, flags=0):
        self._link, self._sel, self._editable, self._flags = link, sel, editable, flags

    def linkUrl(self):
        return QUrl(self._link)

    def selectedText(self):
        return self._sel

    def isContentEditable(self):
        return self._editable

    def editFlags(self):
        return self._flags


@pytest.fixture()
def browser_view(chat_ui, qapp):
    chat_ui.resize(1400, 860)
    chat_ui.show()
    chat_ui.file_panel.open_browser("")
    qapp.processEvents()
    view = chat_ui.file_panel._browser_view
    yield view
    chat_ui.file_panel.close_all()
    for _ in range(20):
        qapp.processEvents()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)


def test_page_menu_blank_area(browser_view):
    menu = browser_view.build_page_menu(_FakeReq())
    assert labels(menu) == ["后退", "前进", "重新加载", "-", "复制页面地址", "在系统浏览器中打开",
                            "-", "检查元素"]
    assert not enabled(menu, "后退") and not enabled(menu, "复制页面地址"), "空白页没有地址可复制"
    assert menu.objectName() == "lxContextMenu"


def test_page_menu_link_and_selection(browser_view, chat_ui):
    menu = browser_view.build_page_menu(_FakeReq(link="http://localhost:5173/docs", sel="token"))
    assert labels(menu) == ["打开链接", "复制链接地址", "在系统浏览器中打开链接", "-",
                            "复制", "把选中文字发到输入框", "-", "检查元素"]
    trigger(menu, "复制链接地址")
    assert QApplication.clipboard().text() == "http://localhost:5173/docs"
    trigger(menu, "把选中文字发到输入框")
    assert chat_ui._pending_quotes == ["token"], "网页里引用也进输入框的引用卡"


def test_page_menu_editable_field(browser_view):
    from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest as R
    ef = R.EditFlag
    menu = browser_view.build_page_menu(_FakeReq(editable=True, flags=ef.CanPaste | ef.CanSelectAll))
    assert labels(menu) == ["撤销", "重做", "-", "剪切", "复制", "粘贴", "-", "全选", "-", "检查元素"]
    assert enabled(menu, "粘贴") and not enabled(menu, "复制")


def test_view_uses_custom_menu(browser_view):
    assert browser_view._embedded.contextMenuPolicy() == Qt.CustomContextMenu


def test_file_dialogs_and_other_windows_unaffected(qapp, host):
    """菜单父对象是被右键的控件，不另开窗口；过滤器只认文本类控件。"""
    w = QWidget()
    assert cm._text_target(w) is None


def _filter_sees(qapp, monkeypatch):
    """给一个普通 QWidget 发右键事件，返回全局过滤器有没有看到它（_text_target 被调到）。"""
    seen = []
    monkeypatch.setattr(cm, "_text_target", lambda obj: seen.append(obj) or None)
    w = QWidget()
    QApplication.sendEvent(w, QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(1, 1)))
    w.deleteLater()
    return bool(seen)


def test_suspended_takes_the_global_filter_off_and_puts_it_back(qapp, monkeypatch):
    """批量建控件（重画长会话）时摘掉全局过滤器：每个事件都进一趟 Python，重画 371 条消息
    约 9 万个事件，把切回编码从 0.8s 拖到 1.6s（逐个提交二分实测）。可嵌套，最外层结束才装回。"""
    cm.install(qapp)
    assert _filter_sees(qapp, monkeypatch)
    with cm.suspended():
        with cm.suspended():
            assert not _filter_sees(qapp, monkeypatch)
        assert not _filter_sees(qapp, monkeypatch), "内层结束不能提前装回"
    assert _filter_sees(qapp, monkeypatch), "结束后右键菜单照常"


def test_suspended_puts_the_filter_back_even_if_rendering_fails(qapp, monkeypatch):
    cm.install(qapp)
    with pytest.raises(RuntimeError):
        with cm.suspended():
            raise RuntimeError("画到一半出错")
    assert _filter_sees(qapp, monkeypatch)
