"""中文、跟随主题的右键菜单（设计稿「右键菜单 · 输入框与聊天记录」「右键菜单 · 浏览器网页」）。

Qt 自带的输入框 / 文本框右键菜单是英文的（Undo / Copy / Select All…，没加载翻译），
样式也只是套了全局 QMenu。这里在 QApplication 上装一个事件过滤器：右键落在文本类控件上、
而且它没有自定义菜单（contextMenuPolicy 是默认的）时，换成我们自己的菜单：

- **能编辑的**（QLineEdit / QTextEdit / QPlainTextEdit）：撤销、重做 · 剪切、复制、粘贴、
  粘贴为纯文本（富文本框才有）· 全选；按当前状态置灰（没选中不能复制、只读不能粘贴……），
  密码框不给复制 / 剪切；
- **只读的**（只读编辑框、QTextBrowser、可选中的 QLabel——聊天记录就是一段段 QLabel）：
  复制 · 复制为 Markdown（聊天正文有源文时）· 引用到输入框 · 链接相关 · 全选。

网页里的右键菜单不走这里（QWebEngineView 不是文本控件），在 browser_panel 里按
QWebEngineContextMenuRequest 另做，菜单外观共用 ``make_menu``。

「引用到输入框」「在浏览器面板打开」要找主窗口：``install(app, host)`` 记下 host
（ChatUI），它提供 ``context_menu_quote(text)`` 和 ``context_menu_open_link(href)``。
"""
from __future__ import annotations

import weakref
from contextlib import contextmanager

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QLabel, QLineEdit, QMenu, QPlainTextEdit, QTextEdit, QWidget,
)

from .fonts import UI_CSS
from .theme import color as _c

_filter = None
_host_ref = None


def _host():
    return _host_ref() if _host_ref is not None else None


def install(app: QApplication, host=None):
    """装一次全局过滤器（重复调用只更新 host）。"""
    global _filter, _host_ref
    if host is not None:
        _host_ref = weakref.ref(host)
    if _filter is None:
        _filter = _ContextMenuFilter(app)
        app.installEventFilter(_filter)


_suspend_depth = 0


@contextmanager
def suspended():
    """批量建控件期间临时摘掉全局过滤器，结束装回去（可嵌套）。

    应用级过滤器是 Python 写的：界面上**每个事件**都要从 C++ 进一趟 Python。人手操作时
    事件很稀，感觉不到；重画长会话（371 条消息、92 张工具卡）一口气产生约 9 万个事件，
    这一层把「知识库切回编码」从 0.8s 拖到 1.6s（逐个提交二分实测，第 5 批引入）。
    重画期间主线程忙着，不可能弹右键菜单；期间的右键事件排在队列里，装回去之后才分发。"""
    global _suspend_depth
    app = QApplication.instance()
    if _filter is None or app is None:
        yield
        return
    if _suspend_depth == 0:
        app.removeEventFilter(_filter)
    _suspend_depth += 1
    try:
        yield
    finally:
        _suspend_depth -= 1
        if _suspend_depth == 0:
            app.installEventFilter(_filter)


def make_menu(parent: QWidget | None = None) -> QMenu:
    """主题右键菜单的外壳：圆角 10、内边距 6、项高 32、选中靛蓝浅底（设计稿）。"""
    menu = QMenu(parent)
    menu.setObjectName("lxContextMenu")
    # 圆角要透明底；系统阴影是方的，和圆角对不上，关掉用描边
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground, True)
    menu.setStyleSheet(
        f"QMenu#lxContextMenu {{ background: {_c('menu_bg')}; border: 1px solid {_c('menu_border')};"
        f" border-radius: 10px; padding: 6px; font-family: {UI_CSS}; }}"
        f"QMenu#lxContextMenu::item {{ padding: 7px 18px 7px 10px; border-radius: 6px;"
        f" font-size: 13px; color: {_c('menu_text')}; min-width: 196px; }}"
        f"QMenu#lxContextMenu::item:selected {{ background: {_c('menu_sel_bg')};"
        f" color: {_c('menu_sel_text')}; }}"
        f"QMenu#lxContextMenu::item:disabled {{ color: {_c('menu_disabled')}; }}"
        f"QMenu#lxContextMenu::separator {{ height: 1px; background: {_c('menu_separator')};"
        f" margin: 5px 4px; }}"
    )
    return menu


def add_item(menu: QMenu, text: str, slot=None, *, shortcut: str = "", enabled: bool = True) -> QAction:
    """加一项：快捷键只作显示（制表符后右对齐），不注册成真的快捷键。"""
    act = QAction(f"{text}\t{shortcut}" if shortcut else text, menu)
    act.setEnabled(enabled)
    if slot is not None:
        act.triggered.connect(lambda _=False: slot())
    menu.addAction(act)
    return act


def _plain_len(label: QLabel) -> int:
    if label.textFormat() == Qt.PlainText:
        return len(label.text())
    doc = QTextDocument()
    doc.setHtml(label.text())
    return len(doc.toPlainText())


def _label_selection(label: QLabel) -> str:
    # QLabel 选中的富文本里段落之间是 U+2029，复制出去换成普通换行
    return label.selectedText().replace(" ", "\n").replace(" ", "\n")


def _link_items(menu: QMenu, href: str, text: str = ""):
    """链接的几项（设计稿「消息区：回复里的链接」）：网页给两种打开方式，左键按设置走、
    右键都能选；再是复制地址 / 复制链接文字。"""
    host = _host()
    if href.startswith(("http://", "https://")):
        if host is not None:
            add_item(menu, "在侧栏浏览器打开", lambda: host.open_link_in_panel(href))
            add_item(menu, "用系统浏览器打开", lambda: host.open_link_in_system(href))
            menu.addSeparator()
    elif href and host is not None:
        add_item(menu, "打开", lambda: host.context_menu_open_link(href))
    add_item(menu, "复制链接地址", lambda: QApplication.clipboard().setText(href))
    if text:
        add_item(menu, "复制链接文字", lambda: QApplication.clipboard().setText(text))


def _anchor_text(html: str, href: str) -> str:
    """富文本里这个链接显示的文字（去掉标签和末尾的箭头）。"""
    import html as _html
    import re
    m = re.search(r'<a href="' + re.escape(_html.escape(href, quote=True)) + r'"[^>]*>(.*?)</a>',
                  html or "", re.S) or re.search(
        r'<a href="' + re.escape(href) + r'"[^>]*>(.*?)</a>', html or "", re.S)
    if not m:
        return ""
    text = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    return text.rstrip("  ↗▸").strip()


def build_text_menu(w: QWidget, pos) -> QMenu | None:
    """按控件类型和状态拼菜单；不认识的控件返回 None（交回 Qt 默认处理）。pos 是控件坐标
    （QTextEdit / QPlainTextEdit 用视口坐标）。"""
    menu = make_menu(w)
    cb_text = QApplication.clipboard().text()
    if isinstance(w, QLineEdit):
        ro = w.isReadOnly()
        secret = w.echoMode() != QLineEdit.Normal
        sel = w.hasSelectedText()
        add_item(menu, "撤销", w.undo, shortcut="Ctrl+Z", enabled=not ro and w.isUndoAvailable())
        add_item(menu, "重做", w.redo, shortcut="Ctrl+Y", enabled=not ro and w.isRedoAvailable())
        menu.addSeparator()
        add_item(menu, "剪切", w.cut, shortcut="Ctrl+X", enabled=not ro and sel and not secret)
        add_item(menu, "复制", w.copy, shortcut="Ctrl+C", enabled=sel and not secret)
        add_item(menu, "粘贴", w.paste, shortcut="Ctrl+V", enabled=not ro and bool(cb_text))
        menu.addSeparator()
        add_item(menu, "全选", w.selectAll, shortcut="Ctrl+A", enabled=bool(w.text()))
        return menu
    if isinstance(w, (QTextEdit, QPlainTextEdit)):
        ro = w.isReadOnly()
        cur = w.textCursor()
        sel = cur.hasSelection()
        doc = w.document()
        if not ro:
            add_item(menu, "撤销", w.undo, shortcut="Ctrl+Z", enabled=doc.isUndoAvailable())
            add_item(menu, "重做", w.redo, shortcut="Ctrl+Y", enabled=doc.isRedoAvailable())
            menu.addSeparator()
            add_item(menu, "剪切", w.cut, shortcut="Ctrl+X", enabled=sel)
            add_item(menu, "复制", w.copy, shortcut="Ctrl+C", enabled=sel)
            add_item(menu, "粘贴", w.paste, shortcut="Ctrl+V", enabled=w.canPaste())
            if isinstance(w, QTextEdit) and w.acceptRichText():
                add_item(menu, "粘贴为纯文本", lambda: w.insertPlainText(cb_text),
                         shortcut="Ctrl+Shift+V", enabled=bool(cb_text))
        else:
            add_item(menu, "复制", w.copy, shortcut="Ctrl+C", enabled=sel)
            if sel:
                _quote_item(menu, cur.selectedText().replace(" ", "\n"))
            if isinstance(w, QTextEdit):
                href = w.anchorAt(pos)
                if href:
                    menu.addSeparator()
                    _link_items(menu, href)
        menu.addSeparator()
        add_item(menu, "全选", w.selectAll, shortcut="Ctrl+A", enabled=not doc.isEmpty())
        return menu
    if isinstance(w, QLabel):
        sel = w.hasSelectedText()
        text = _label_selection(w) if sel else ""
        add_item(menu, "复制", lambda: QApplication.clipboard().setText(text),
                 shortcut="Ctrl+C", enabled=sel)
        md = w.property("lx_markdown")
        if md:
            add_item(menu, "复制为 Markdown", lambda: QApplication.clipboard().setText(md))
        if sel:
            _quote_item(menu, text)
        href = w.property("lx_hover_link") or ""
        if href:
            menu.addSeparator()
            _link_items(menu, href, _anchor_text(w.text(), href))
        menu.addSeparator()
        n = _plain_len(w)
        add_item(menu, "全选", lambda: w.setSelection(0, n), shortcut="Ctrl+A", enabled=n > 0)
        return menu
    menu.deleteLater()
    return None


def _quote_item(menu: QMenu, text: str):
    host = _host()
    if host is not None and text.strip():
        add_item(menu, "引用到输入框", lambda: host.context_menu_quote(text))


def _text_target(obj):
    """右键事件落在谁身上：QLineEdit / 可选中的 QLabel 是它自己；QTextEdit / QPlainTextEdit
    的事件先到视口，换成编辑框本身。"""
    if isinstance(obj, QLineEdit):
        return obj
    if isinstance(obj, (QTextEdit, QPlainTextEdit)):
        return obj
    if isinstance(obj, QLabel):
        flags = obj.textInteractionFlags()
        if flags & (Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard):
            return obj
        return None
    parent = obj.parentWidget() if isinstance(obj, QWidget) else None
    if isinstance(parent, (QTextEdit, QPlainTextEdit)) and parent.viewport() is obj:
        return parent
    return None


class _ContextMenuFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() != QEvent.ContextMenu:
            return False
        target = _text_target(obj)
        if target is None or target.contextMenuPolicy() != Qt.DefaultContextMenu:
            return False
        if target.property("lx_native_menu"):
            return False
        menu = build_text_menu(target, event.pos())
        if menu is None:
            return False
        menu.exec(event.globalPos())
        menu.deleteLater()
        return True

