"""调试面板（原 F12 Debug Inspector 独立窗口）：现在是对话区右侧停靠栏的「调试」标签。

原来的独立窗口要置顶、模态框打开时让出置顶（不然两边都点不了）——停靠后没有这些问题。
这里测：F12 打开 / 收起标签、控件常驻复用、不是独立窗口、上下分栏（停靠栏窄）、切主题跟着变。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    from src import state, tools
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(state, "ui_ref", None)
    w = ChatUI()
    w.resize(1400, 860)
    w.show()
    qapp.processEvents()
    yield w
    if state.ui_ref is w:
        state.ui_ref = None
    w.close()
    qapp.processEvents()
    w.deleteLater()
    gc.collect()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_f12_opens_and_closes_the_debug_tab(ui, qapp):
    from src.ui.file_panel import DEBUG_KEY
    QTest.keyClick(ui, Qt.Key_F12)
    qapp.processEvents()
    insp = ui._debug_inspector
    assert ui.file_panel.is_tab_current(DEBUG_KEY)
    assert insp.isVisible() and not insp.isWindow()
    assert ui.file_panel.path_label.text() == "调试"
    QTest.keyClick(ui, Qt.Key_F12)
    qapp.processEvents()
    assert not ui.file_panel.is_tab_current(DEBUG_KEY) and not insp.isVisible()
    QTest.keyClick(ui, Qt.Key_F12)
    qapp.processEvents()
    assert ui._debug_inspector is insp and insp.isVisible()      # 常驻复用，不重建


def test_debug_and_background_tabs_coexist_with_files(ui, qapp):
    from src.ui.file_panel import BACKGROUND_KEY, DEBUG_KEY
    ui._toggle_debug_inspector()
    ui._open_background_panel()
    qapp.processEvents()
    keys = ui.file_panel.paths()
    assert keys.count(DEBUG_KEY) == 1 and keys.count(BACKGROUND_KEY) == 1
    assert ui.file_panel.is_tab_current(BACKGROUND_KEY)
    ui.file_panel.close_all()
    qapp.processEvents()
    assert not ui._background_panel.isVisible() and not ui._background_panel._timer.isActive()


def test_list_above_detail_and_theme(ui, qapp):
    from src.ui.theme import THEMES
    ui._toggle_debug_inspector()
    insp = ui._debug_inspector
    split = insp.list_widget.parentWidget()
    assert split.orientation() == Qt.Vertical
    ui.theme = "dark"
    ui._apply_theme()
    assert THEMES["dark"]["mv_text"] in insp.styleSheet()


def test_background_tab_shows_running_count(ui, qapp, monkeypatch):
    from src.ui.file_panel import BACKGROUND_KEY
    ui._open_background_panel()
    ui.file_panel.set_tab_badge(BACKGROUND_KEY, 2)
    qapp.processEvents()
    label = ui.file_panel._widget_tabs[BACKGROUND_KEY]["label"]
    assert label.text() == "后台 2"
    assert label.isVisible()                    # 重挂按钮后文字还在（setTabButton 会藏旧按钮）
    # 标签栏按新文字重排：文字整段放得下，不被裁掉
    index = ui.file_panel._tab_index(BACKGROUND_KEY)
    assert ui.file_panel.tabs.tabRect(index).width() >= label.sizeHint().width()
    ui.file_panel.set_tab_badge(BACKGROUND_KEY, 0)
    assert label.text() == "后台"


def test_tab_close_buttons_are_big_enough(ui, qapp):
    """标签上的关闭按钮：22px 点击区 + 图标（原来 16px 按钮里一个 13px 的「×」字，太小点不准）。"""
    from PySide6.QtWidgets import QPushButton
    ui._open_background_panel()
    ui._toggle_debug_inspector()
    btns = ui.file_panel.tabs.findChildren(QPushButton, "fileTabClose")
    assert len(btns) == 2
    for b in btns:
        assert b.width() >= 22 and b.height() >= 22
        assert not b.icon().isNull() and b.text() == ""
