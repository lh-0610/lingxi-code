"""计划工具到 Qt 信号的接线、后台隔离和排队消息一致性。"""
import threading
from unittest.mock import Mock

import pytest

from src import tools, state, session


class _FakeUI:
    def __init__(self):
        self.calls = []

    def show_plan(self, items):
        self.calls.append(items)


def test_update_plan_pushes_to_ui(monkeypatch):
    ui = _FakeUI()
    monkeypatch.setattr(state, "ui_ref", ui)
    out = tools.update_plan.func("[x] 读 config\n[~] 改 state\n[ ] 加 UI")
    assert ui.calls, "update_plan 应调用 ui.show_plan"
    items = ui.calls[-1]
    assert [it["status"] for it in items] == ["done", "in_progress", "pending"]
    assert "3" in out          # 返回串含步骤总数


@pytest.fixture
def plan_ui(monkeypatch, isolated_memory):
    """使用真实 Qt 队列和生产路由方法，不创建完整窗口或调用模型。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from src import models, mcp_client
    fake_llm = Mock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.invoke.side_effect = AssertionError("unexpected model call")
    fake_llm.stream.side_effect = AssertionError("unexpected model call")
    monkeypatch.setattr(models, "_create_llm", lambda *args: fake_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    from src.ui.chat_window import ChatUI
    from src.ui.widgets import SignalBridge
    from PySide6.QtCore import QObject, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    class PlanHost(QObject):
        show_plan = ChatUI.show_plan
        _on_plan_update = ChatUI._on_plan_update

        def __init__(self):
            super().__init__()
            self.calls = []
            self.bridge = SignalBridge()
            self.bridge.show_plan.connect(self._on_plan_update, Qt.QueuedConnection)

        def _render_plan_panel(self, items):
            self.calls.append([dict(it) for it in items])

    host = PlanHost()
    monkeypatch.setattr(state, "ui_ref", host)
    yield host, app
    app.processEvents()


def _worker_update(sess, plan):
    errors = []

    def work():
        session.bind_thread(sess)
        try:
            tools.update_plan.func(plan)
        except BaseException as error:
            errors.append(error)
        finally:
            session.unbind_thread()

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors


def test_background_plan_does_not_replace_foreground(plan_ui):
    ui, app = plan_ui
    foreground = session.get_active()
    tools.update_plan.func("[~] 前台任务")
    app.processEvents()
    background = session.Session()
    _worker_update(background, "[~] 后台任务")
    app.processEvents()
    assert ui.calls == [foreground.current_plan]
    assert background.current_plan == [{"text": "后台任务", "status": "in_progress"}]
    assert background.needs_redraw


def test_switch_before_signal_delivery_ignores_previous_session(plan_ui):
    ui, app = plan_ui
    first = session.get_active()
    _worker_update(first, "[~] 第一个会话")
    second = session.Session()
    session.set_active(second)
    tools.update_plan.func("[~] 第二个会话")
    app.processEvents()
    assert ui.calls == [second.current_plan]


def test_queued_old_progress_does_not_redraw_stale_snapshot(plan_ui):
    ui, app = plan_ui
    tools.update_plan.func("[~] A\n[ ] B")
    tools.set_step_status.func(1, "完成")
    app.processEvents()
    assert ui.calls == [[
        {"text": "A", "status": "done"},
        {"text": "B", "status": "in_progress"},
    ]]


def test_signal_carries_copy_of_items(plan_ui):
    ui, app = plan_ui
    current = session.get_active()
    items = [{"text": "原步骤", "status": "pending"}]
    current.current_plan = [dict(it) for it in items]
    ui.show_plan(items)
    items[0]["text"] = "调用者后来改写"
    app.processEvents()
    assert ui.calls == [[{"text": "原步骤", "status": "pending"}]]
