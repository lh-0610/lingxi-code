"""任务面板像 Claude / Codex 的侧栏：由用户开关、没有任务也在（空状态）、
放不下时改成盖在对话区上的浮层、拖宽有上限且会记住。

用真实 ChatUI（offscreen，模型 / MCP / 网络全部离线），偏好落在 isolated_memory 里。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest

from src import agent, state
from src.ui import ChatUI
from src.ui.prefs import _load_ui_prefs


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
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in task panel tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
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
    # 清理顺序见 test_sidebar_load_more._destroy_ui_cleanly
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


def test_open_by_default_and_shows_an_empty_state(ui):
    panel = ui.plan_panel
    assert panel.isVisible() and ui._task_panel_docked, "宽窗口默认打开、停靠在右边"
    assert ui.task_panel_btn.isChecked()
    assert panel.empty_box.isVisible(), "没有任务也在，显示空状态说明"
    assert "还没有任务" in panel.empty_title.text()


def test_toggle_is_remembered(ui, qapp):
    ui._toggle_task_panel()
    _pump(qapp)
    assert not ui.plan_panel.isVisible() and not ui.task_panel_btn.isChecked()
    assert _load_ui_prefs().get("task_panel_collapsed") is True
    ui._toggle_task_panel()
    _pump(qapp)
    assert ui.plan_panel.isVisible()
    assert _load_ui_prefs().get("task_panel_collapsed") is False


def test_narrow_window_opens_it_as_an_overlay(ui, qapp):
    ui.resize(1000, 800)
    _pump(qapp)
    panel = ui.plan_panel
    assert not ui._task_panel_docked and not panel.isVisible(), "放不下：默认收起，不挤对话区"
    chat_width = ui.chat_area.width()

    ui._toggle_task_panel()
    _pump(qapp)
    host = panel.parentWidget()
    header = ui.model_combo.parentWidget()
    header_bottom = header.mapTo(host, QPoint(0, header.height())).y()
    assert panel.isVisible() and ui.task_panel_btn.isChecked()
    assert panel.geometry().right() == host.width() - 1, "浮层贴在窗口最右边"
    assert panel.y() == header_bottom and panel.geometry().bottom() == host.height() - 1, \
        "从顶栏下面开始到窗口底：顶栏上开关面板的按钮不能被盖住"
    assert ui.chat_area.width() == chat_width, "浮层盖在对话区上，不改变对话区宽度"
    assert _load_ui_prefs().get("task_panel_collapsed") is None, "浮层只是这一次打开，不改偏好"

    QTest.keyClick(ui, Qt.Key_Escape)
    _pump(qapp)
    assert not panel.isVisible(), "Esc 收起浮层"

    ui.resize(1440, 900)
    _pump(qapp)
    assert ui._task_panel_docked and panel.isVisible(), "放得下了：回到停靠，按偏好显示"


def test_drag_left_edge_to_resize_with_a_limit(ui, qapp):
    panel = ui.plan_panel
    start = panel.width()
    QTest.mousePress(panel, Qt.LeftButton, pos=QPoint(2, 300))
    QTest.mouseMove(panel, QPoint(2 - 100, 300))
    QTest.mouseRelease(panel, Qt.LeftButton, pos=QPoint(2 - 100, 300))
    _pump(qapp)
    assert panel.width() == start + 100, "往左拖变宽"
    assert _load_ui_prefs().get("task_panel_width") == panel.width(), "宽度记住"

    QTest.mousePress(panel, Qt.LeftButton, pos=QPoint(2, 300))
    QTest.mouseMove(panel, QPoint(2 - 3000, 300))
    QTest.mouseRelease(panel, Qt.LeftButton, pos=QPoint(2 - 3000, 300))
    _pump(qapp)
    assert panel.width() <= panel.MAX_WIDTH
    assert ui.width() - ui._sidebar_shown_width() - panel.width() >= ui._TASK_PANEL_MIN_CHAT, \
        "停靠时不能把对话栏挤到放不下顶栏"
