"""输入框底栏的模式按钮和菜单（原来顶栏的「计划 / 执行」）。

Qt 部分 offscreen；菜单的 exec 换成只记录、不阻塞，模型 / MCP / 网络离线。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest

from src import agent, permission_modes as pm, session, state
from src.ui import ChatUI


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in mode menu tests")

    from src import mcp_client, models, tools
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    window.resize(1440, 900)
    window.show()
    _pump(qapp)
    yield window
    import shiboken6
    if shiboken6.isValid(window):
        if state.ui_ref is window:
            state.ui_ref = None
        window.close()
        qapp.processEvents()
        window.deleteLater()
        gc.collect()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


@pytest.fixture()
def opened(monkeypatch):
    """打开的菜单：exec 不阻塞，只把菜单留下来给用例看。"""
    from src.ui.widgets import NumberedMenu
    menus = []
    monkeypatch.setattr(NumberedMenu, "exec", lambda self, *a, **k: menus.append(self))
    return menus


def _items(menu):
    from src.ui.widgets import DescribedMenuItem
    return [a.defaultWidget() for a in menu.numbered
            if isinstance(a.defaultWidget(), DescribedMenuItem)]


def test_mode_button_sits_left_of_send_and_header_has_no_plan_act(ui):
    bottom = ui._composer_bottom
    widgets = [bottom.itemAt(i).widget() for i in range(bottom.count())]
    assert widgets.index(ui.mode_btn) == widgets.index(ui.send_btn) - 1
    assert not hasattr(ui, "plan_btn") and not hasattr(ui, "mode_seg")
    assert ui.mode_btn.text() == "手动"


def test_menu_lists_five_modes_with_numbers_and_a_check(ui, qapp, opened):
    pm.apply(session.get_active(), "auto")
    ui._show_mode_menu()
    menu = opened[-1]
    from PySide6.QtWidgets import QLabel
    rows = _items(menu)
    titles = [r.findChild(QLabel, "itemTitle").text() for r in rows]
    assert titles == ["自动", "手动", "接受编辑", "计划", "跳过权限确认"]
    assert [r.findChild(QLabel, "itemTrailing").text() for r in rows] == ["1", "2", "3", "4", "5"]
    checked = [r.findChild(QLabel, "itemTitle").text() for r in rows if r.findChild(QLabel, "itemCheck")]
    assert checked == ["自动"]


def test_number_key_picks_a_mode(ui, qapp, opened):
    ui._show_mode_menu()
    QTest.keyClick(opened[-1], Qt.Key_3)
    _pump(qapp)
    assert pm.current(session.get_active()) == "accept_edits"
    assert ui.mode_btn.text() == "接受编辑"


def test_plan_is_still_plan_mode(ui):
    ui._choose_mode("plan")
    assert session.get_active().agent_mode == "plan" and ui.mode_btn.text() == "计划"
    ui._choose_mode("manual")
    assert session.get_active().agent_mode == "act"


def test_bypass_asks_first_and_is_never_remembered(ui, monkeypatch):
    from src.ui.prefs import _load_ui_prefs
    ui._choose_mode("auto")
    monkeypatch.setattr(ChatUI, "_confirm_bypass_mode", lambda self: False)
    ui._choose_mode("bypass")
    assert pm.current(session.get_active()) == "auto", "取消就不切"

    monkeypatch.setattr(ChatUI, "_confirm_bypass_mode", lambda self: True)
    ui._choose_mode("bypass")
    assert pm.current(session.get_active()) == "bypass" and ui.mode_btn.text() == "跳过权限确认"
    assert _load_ui_prefs().get("permission_mode") == "auto", "默认还是上次的非跳过模式"
    from src.ui.prefs import inherited_permission_mode
    assert inherited_permission_mode(session.get_active()) == "auto", "新会话不继承跳过"


def test_button_follows_the_active_session(ui):
    other = session.Session()
    pm.apply(other, "accept_edits")
    session.register(other)
    session.set_active(other)
    ui._sync_header_from_session()
    assert ui.mode_btn.text() == "接受编辑"
