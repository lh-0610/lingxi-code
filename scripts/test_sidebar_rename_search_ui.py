"""侧栏会话重命名 / 跨会话搜索 UI 测试（真 ChatUI，offscreen，离线）。

覆盖：右键重命名菜单接线、重命名对话框→后端→刷新、搜索框→worker→bridge→结果渲染、
结果点击跳转、过期结果丢弃、切工作区清空搜索。后端 rename/search 走真实实现。
"""
import gc
import os
import sys

import pytest
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def chat_ui(qt_app, monkeypatch, isolated_memory):
    import socket
    from PySide6.QtCore import QEvent

    def _forbidden(*a, **k):
        raise AssertionError("sidebar rename/search UI 测试禁止网络")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import models, mcp_client, tools, config, state, agent
    from src.ui.chat_window import ChatUI

    def _no_llm(*a, **k):
        raise AssertionError("sidebar rename/search UI 测试禁止建/调模型")

    monkeypatch.setattr(models, "_create_llm", _no_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_create_llm", _no_llm)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    yield ui
    import shiboken6
    if shiboken6.isValid(ui):
        if state.ui_ref is ui:
            state.ui_ref = None
        ui.close()
        qt_app.processEvents()
        qt_app.processEvents()
        ui.deleteLater()
        gc.collect()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        qt_app.processEvents()


def _save(title, messages, *, kind="code"):
    from src import memory, session as sm
    s = sm.Session()
    s.session_kind = kind
    s.current_session_title = title
    s.chat_history = [SystemMessage(content="sys")] + messages
    memory.save_session(session=s)
    return s


def _expand_all(ui):
    """强制展开所有分组，让会话行可见（默认非当前项目折叠）。"""
    ui._collapsed_init_done = True
    ui._collapsed_projects = set()


def _history_rows(ui):
    from src.ui.widgets import HistoryRow
    return ui.history_widget.findChildren(HistoryRow)


def _wait_search(ui, qt_app, timeout=5.0):
    """轮询事件循环直到搜索 worker 完成（bridge 跨线程信号需 processEvents 投递）。"""
    import time
    deadline = time.monotonic() + timeout
    while ui._sidebar_search_pending and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()


# ── 重命名 ───────────────────────────────────────────────
class TestRenameUI:
    def test_row_has_custom_context_menu(self, chat_ui, qt_app, project_dir):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QPushButton
        _save("旧标题", [HumanMessage(content="hi"), AIMessage(content="yo")])
        _expand_all(chat_ui)
        chat_ui._refresh_session_list()
        qt_app.processEvents()
        btns = [b for b in chat_ui.history_widget.findChildren(QPushButton)
                if b.property("class") in ("historyItem", "historyItemActive")]
        assert btns
        assert all(b.contextMenuPolicy() == Qt.CustomContextMenu for b in btns)

    def test_rename_dialog_updates_disk_and_sidebar(self, chat_ui, qt_app, project_dir, monkeypatch):
        from PySide6.QtWidgets import QInputDialog, QPushButton
        import json
        from src.paths import memory_dir
        s = _save("旧标题", [HumanMessage(content="hi"), AIMessage(content="yo")])
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("新名字", True)))
        chat_ui._rename_session_dialog(s.current_session_id, "旧标题")
        qt_app.processEvents()
        data = json.loads(open(os.path.join(memory_dir(), f"{s.current_session_id}.json"),
                               encoding="utf-8").read())
        assert data["title"] == "新名字"
        _expand_all(chat_ui)
        chat_ui._refresh_session_list()
        qt_app.processEvents()
        tips = [b.toolTip() for b in chat_ui.history_widget.findChildren(QPushButton)
                if b.property("class") in ("historyItem", "historyItemActive")]
        assert "新名字" in tips

    def test_rename_empty_shows_toast(self, chat_ui, project_dir, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("   ", True)))
        toasts = []
        monkeypatch.setattr(chat_ui, "_show_toast", lambda t, d=1500: toasts.append(t))
        chat_ui._rename_session_dialog(s.current_session_id, "旧")
        assert toasts
        assert s.current_session_title == "旧"     # 未改

    def test_rename_cancel_noop(self, chat_ui, project_dir, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        s = _save("旧", [HumanMessage(content="hi"), AIMessage(content="yo")])
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("随便", False)))
        chat_ui._rename_session_dialog(s.current_session_id, "旧")
        assert s.current_session_title == "旧"

    def test_rename_current_session_syncs_state(self, chat_ui, project_dir, monkeypatch):
        from PySide6.QtWidgets import QInputDialog
        from src import state, session as sm
        s = sm.get_active()
        s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi"),
                          AIMessage(content="yo")]
        from src import memory
        memory.save_session(session=s)
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("当前会话新名", True)))
        chat_ui._rename_session_dialog(s.current_session_id, "新对话")
        assert state.current_session_title == "当前会话新名"


# ── 搜索 ─────────────────────────────────────────────────
class TestSearchUI:
    def test_search_box_exists(self, chat_ui):
        assert chat_ui._sidebar_search_input is not None
        assert chat_ui._sidebar_search_query == ""

    def test_start_search_sets_pending_then_completes(self, chat_ui, qt_app, project_dir):
        _save("登录重构", [HumanMessage(content="修复登录超时"), AIMessage(content="好")])
        chat_ui._start_sidebar_search("登录")
        assert chat_ui._sidebar_search_query == "登录"
        assert chat_ui._sidebar_search_pending is True
        _wait_search(chat_ui, qt_app)
        assert chat_ui._sidebar_search_pending is False
        assert len(chat_ui._sidebar_search_results) == 1
        assert len(_history_rows(chat_ui)) == 1     # 结果行替换了分组列表

    def test_empty_search_restores_groups(self, chat_ui, qt_app, project_dir):
        from PySide6.QtWidgets import QPushButton
        _save("甲", [HumanMessage(content="共同词"), AIMessage(content="a")])
        chat_ui._start_sidebar_search("共同词")
        _wait_search(chat_ui, qt_app)
        chat_ui._start_sidebar_search("")
        qt_app.processEvents()
        assert chat_ui._sidebar_search_query == ""
        headers = [b for b in chat_ui.history_widget.findChildren(QPushButton)
                   if b.objectName() in ("projectHeader", "projectHeaderActive")]
        assert headers                              # 恢复分组视图

    def test_search_done_renders_rows(self, chat_ui, qt_app, project_dir):
        from src import agent
        _save("标题A", [HumanMessage(content="缓存方案"), AIMessage(content="a")])
        _save("标题B", [HumanMessage(content="缓存问题"), AIMessage(content="b")])
        results = agent.search_sessions("缓存")
        chat_ui._sidebar_search_query = "缓存"
        chat_ui._on_session_search_done("缓存", results)
        qt_app.processEvents()
        assert len(_history_rows(chat_ui)) == 2

    def test_stale_search_done_ignored(self, chat_ui, project_dir):
        chat_ui._sidebar_search_query = "新词"
        chat_ui._sidebar_search_results = []
        chat_ui._on_session_search_done("旧词", [{"id": "x", "title": "t"}])
        assert chat_ui._sidebar_search_results == []   # 过期结果被丢弃

    def test_result_click_loads_session(self, chat_ui, qt_app, project_dir):
        from PySide6.QtWidgets import QPushButton
        from src import session as sm, agent
        s = _save("可跳转", [HumanMessage(content="独特关键词"), AIMessage(content="a")])
        chat_ui._start_sidebar_search("独特关键词")
        _wait_search(chat_ui, qt_app)
        rows = _history_rows(chat_ui)
        assert len(rows) == 1
        btn = rows[0].findChildren(QPushButton)[0]
        btn.click()
        qt_app.processEvents()
        assert sm.get_active().current_session_id == s.current_session_id

    def test_pending_shows_searching_hint(self, chat_ui, qt_app):
        from PySide6.QtWidgets import QLabel
        chat_ui._sidebar_search_query = "词"
        chat_ui._sidebar_search_pending = True
        chat_ui._refresh_session_list()
        qt_app.processEvents()
        hints = [l.text() for l in chat_ui.history_widget.findChildren(QLabel)
                 if l.objectName() == "historyEmptyHint"]
        assert "搜索中…" in hints

    def test_no_results_shows_hint(self, chat_ui, qt_app):
        from PySide6.QtWidgets import QLabel
        chat_ui._sidebar_search_query = "词"
        chat_ui._sidebar_search_pending = False
        chat_ui._sidebar_search_results = []
        chat_ui._refresh_session_list()
        qt_app.processEvents()
        hints = [l.text() for l in chat_ui.history_widget.findChildren(QLabel)
                 if l.objectName() == "historyEmptyHint"]
        assert "无匹配会话" in hints

    def test_clear_sidebar_search(self, chat_ui):
        chat_ui._sidebar_search_query = "词"
        chat_ui._sidebar_search_results = [{"id": "x"}]
        chat_ui._sidebar_search_pending = True
        chat_ui._sidebar_search_input.setText("词")
        chat_ui._clear_sidebar_search()
        assert chat_ui._sidebar_search_query == ""
        assert chat_ui._sidebar_search_results == []
        assert chat_ui._sidebar_search_pending is False
        assert chat_ui._sidebar_search_input.text() == ""

    def test_workspace_switch_clears_search(self, chat_ui, qt_app, project_dir):
        chat_ui._sidebar_search_input.setText("词")
        chat_ui._start_sidebar_search("词")
        _wait_search(chat_ui, qt_app)
        chat_ui.switch_workspace("rag")
        qt_app.processEvents()
        assert chat_ui._sidebar_search_query == ""
        assert chat_ui._sidebar_search_input.text() == ""