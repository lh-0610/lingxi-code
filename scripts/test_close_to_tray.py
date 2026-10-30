"""点 × 收进托盘、托盘「退出」才真退（同 Codex）。

真实 ChatUI（离线夹具借 test_sidebar_load_more）：
- × 只隐藏，不退出、不拒掉等你确认的命令，第一次冒托盘气泡；
- 托盘不可用时关窗走退出流程；
- 退出：没任务直接退；有对话 / 后台命令在跑先确认，取消就什么都不动；
- 确认退出后 closeEvent 真关（Qt 6 的 quit 会去关可见窗口，拒绝会把退出打断）。
"""
import threading

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from src import background, session as _session
from src.ui import chat_window as _cw
from src.ui.widgets import QuitConfirmDialog
from test_sidebar_load_more import qapp, ui  # noqa: F401  真实 ChatUI 的离线夹具


class _FakeTray:
    def __init__(self, visible=True):
        self._visible = visible
        self.messages = []

    def isVisible(self):
        return self._visible

    def showMessage(self, title, text, *args):
        self.messages.append((title, text))


@pytest.fixture()
def quits(monkeypatch):
    calls = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: calls.append("quit")))
    return calls


@pytest.fixture()
def tray_ui(ui, qapp):  # noqa: F811
    ui._hide_on_close = True
    ui._tray = _FakeTray()
    ui.show()
    qapp.processEvents()
    yield ui
    ui._hide_on_close = False  # 夹具收尾的 ui.close() 要真关
    ui._quitting = False


def _pending_confirm(ui):
    """挂一条等用户点「允许」的命令确认请求，返回 worker 那头等的 Event。"""
    done = threading.Event()
    ui._command_confirm_result_holder = {}
    ui._command_confirm_done_event = done
    return done


def test_close_hides_to_tray_and_keeps_work(tray_ui, quits):
    done = _pending_confirm(tray_ui)
    assert tray_ui.close() is False                 # × 被拦下
    assert not tray_ui.isVisible()
    assert quits == []
    assert not done.is_set()                         # 等你确认的命令还挂着，回来还能点
    assert len(tray_ui._tray.messages) == 1          # 第一次收进托盘冒气泡

    tray_ui.show()
    tray_ui.close()
    assert len(tray_ui._tray.messages) == 1          # 只提示一次


def test_close_without_tray_goes_through_quit(tray_ui, quits, monkeypatch):
    tray_ui._tray = _FakeTray(visible=False)         # 托盘不可用：没处可收
    monkeypatch.setattr(tray_ui, "_running_work_summary", lambda: "")
    tray_ui.close()
    assert quits == ["quit"]


def test_quit_when_idle_quits_directly(tray_ui, quits, monkeypatch):
    monkeypatch.setattr(tray_ui, "_running_work_summary", lambda: "")
    monkeypatch.setattr(_cw.QuitConfirmDialog, "exec",
                        lambda self: pytest.fail("没任务在跑不该弹确认"))
    done = _pending_confirm(tray_ui)
    tray_ui.request_quit()
    assert quits == ["quit"]
    assert done.is_set()                             # 挂着的确认被唤醒，worker 不会卡 5 分钟


def test_quit_with_running_work_asks_first(tray_ui, quits, monkeypatch):
    monkeypatch.setattr(tray_ui, "_running_work_summary", lambda: "1 个对话还在运行")
    answer = {"code": QDialog.Rejected}
    monkeypatch.setattr(_cw.QuitConfirmDialog, "exec", lambda self: answer["code"])

    tray_ui.request_quit()                           # 取消：什么都不动
    assert quits == []
    assert not getattr(tray_ui, "_quitting", False)
    assert tray_ui.isVisible()

    answer["code"] = QDialog.Accepted                # 仍然退出
    tray_ui.request_quit()
    assert quits == ["quit"]
    assert tray_ui._quitting is True
    assert tray_ui.close() is True                   # quit 去关窗时不再被收进托盘


def test_running_work_summary_counts_chats_and_commands(tray_ui, monkeypatch):
    class _S:
        def __init__(self, generating=False, alive=False):
            self.is_generating = generating
            self.last_worker = type("T", (), {"is_alive": lambda self: alive})()

    monkeypatch.setattr(_session, "live_sessions",
                        lambda: [_S(generating=True), _S(alive=True), _S()])
    monkeypatch.setattr(background, "list_snapshots",
                        lambda **kw: [{"running": True}, {"running": False}, {"running": None}])
    assert tray_ui._running_work_summary() == "2 个对话、1 个后台命令还在运行"

    monkeypatch.setattr(_session, "live_sessions", lambda: [_S()])
    monkeypatch.setattr(background, "list_snapshots", lambda **kw: [])
    assert tray_ui._running_work_summary() == ""


def test_tray_exit_goes_through_request_quit(tray_ui, qapp, monkeypatch):
    from src.floating import create_tray
    calls = []
    monkeypatch.setattr(tray_ui, "request_quit", lambda: calls.append("request_quit"))
    tray = create_tray(qapp, tray_ui)
    try:
        assert tray_ui._tray is tray
        exit_action = [a for a in tray.contextMenu().actions() if a.text() == "退出"][0]
        exit_action.trigger()
        assert calls == ["request_quit"]
    finally:
        tray.hide()
        tray_ui._tray = _FakeTray()
        tray.deleteLater()


def test_quit_dialog_defaults_to_cancel(qapp):
    dialog = QuitConfirmDialog("1 个对话还在运行")
    try:
        default = [b for b in dialog.findChildren(QPushButton) if b.isDefault()]
        assert [b.text() for b in default] == ["取消"]   # 误点后按 Enter 不会杀任务
    finally:
        dialog.deleteLater()
