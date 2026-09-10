"""Real Qt cards with two bound workers; all I/O uses isolated data and fake models."""
import socket
import threading
import time
from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from src import config, memory, paths, session, state


def _forbidden(*args, **kwargs):
    raise AssertionError("Model/network access is prohibited in confirmation tests")


@pytest.fixture
def confirmation_ui(monkeypatch, isolated_memory):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import models, mcp_client, tools
    fake_llm = Mock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.invoke.side_effect = _forbidden
    fake_llm.stream.side_effect = _forbidden
    monkeypatch.setattr(models, "_create_llm", lambda *args: fake_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    from src.ui.chat_window import ChatUI
    from src import agent
    from PySide6.QtWidgets import QApplication
    monkeypatch.setattr(agent, "_create_llm", lambda *args: fake_llm)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(state, "llm", state.llm)
    monkeypatch.setattr(state, "llm_with_tools", state.llm_with_tools)
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    workers, sessions, errors = [], [], []

    def new_session():
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="test"), HumanMessage(content="question")]
        session.register(sess)
        memory.save_session(session=sess)
        sessions.append(sess)
        return sess

    def start(sess, kind, target):
        output = []

        def worker():
            paths.set_data_dir(str(isolated_memory.parent))
            session.bind_thread(sess)
            try:
                output.append(ui.confirm_command(target) if kind == "command"
                              else ui.confirm_edit(target, "-old\n+new"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                session.unbind_thread()
                paths.set_data_dir(None)

        thread = threading.Thread(target=worker, daemon=True)
        workers.append(thread)
        thread.start()
        return thread, output

    def pump_until(check):
        deadline = time.monotonic() + 5
        while not check():
            app.processEvents()
            assert time.monotonic() < deadline, "Qt confirmation event timeout"
        app.processEvents()

    try:
        yield ui, new_session, start, pump_until
    finally:
        app.processEvents()
        ui._release_pending_confirm()
        ui._release_pending_edit()
        for sess in sessions:
            if sess.pending_confirm:
                _release_background(sess)
        for thread in workers:
            thread.join(5)
        ui.hide()
        ui.deleteLater()
        app.processEvents()
        for sess in sessions:
            session.drop(sess.key)
        assert not any(thread.is_alive() for thread in workers)
        assert not errors, errors


def _holder(ui, kind):
    return getattr(ui, f"_{kind}_confirm_result_holder")


def _trust(sess, kind):
    return sess.command_prefix_allowlist if kind == "command" else sess.edit_path_allowlist


def _release_background(sess):
    pending = sess.pending_confirm
    sess.pending_confirm = None
    pending[-2]["allow"] = False
    pending[-1].set()


@pytest.mark.parametrize("kind,target,next_target", [
    ("command", "git status", "git log"),
    ("edit", "same.py", "same.py"),
])
@pytest.mark.parametrize("background_kind", ["command", "edit"])
@pytest.mark.parametrize("keyboard", [False, True])
def test_remember_belongs_to_visible_card(
    confirmation_ui, kind, target, next_target, background_kind, keyboard,
):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    ui, new_session, start, pump = confirmation_ui
    a, b = new_session(), new_session()
    session.set_active(a)
    ta, ra = start(a, kind, target)
    pump(lambda: _holder(ui, kind) is not None)
    tb, rb = start(b, background_kind, "python --version" if background_kind == "command" else "other.py")
    pump(lambda: b.pending_confirm is not None)
    assert _holder(ui, kind)["_session"] is a
    assert session.get_active() is a
    if keyboard:
        QTest.keyClick(getattr(ui, f"{kind}_confirm_bar"), Qt.Key_3)
    else:
        (ui._cmd_remember_btn if kind == "command" else ui._edit_trust_btn).click()
    ta.join(5)
    assert not ta.is_alive()
    assert ra == [(True, "")]
    assert _trust(a, kind) == ({"git"} if kind == "command" else {target})
    assert not _trust(b, kind)
    _release_background(b)
    tb.join(5)
    assert rb == [(False, "")]
    # A's grant must not let B bypass the next related operation.
    tb2, rb2 = start(b, kind, next_target)
    pump(lambda: b.pending_confirm is not None or bool(rb2))
    assert b.pending_confirm is not None
    assert rb2 == []
    _release_background(b)
    tb2.join(5)
    assert rb2 == [(False, "")]


@pytest.mark.parametrize("kind,target", [("command", "git status"), ("edit", "same.py")])
@pytest.mark.parametrize("feedback", ["", "try another approach"])
def test_reject_stops_card_owner_not_active(confirmation_ui, kind, target, feedback):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    ui, new_session, start, pump = confirmation_ui
    a, b = new_session(), new_session()
    session.set_active(a)
    ta, result = start(a, kind, target)
    pump(lambda: _holder(ui, kind) is not None)
    session.set_active(b)
    getattr(ui, f"{kind}_confirm_feedback").setText(feedback)
    QTest.keyClick(getattr(ui, f"{kind}_confirm_bar"), Qt.Key_Escape)
    ta.join(5)
    assert result == [(False, feedback)]
    assert a.stop_flag is (not bool(feedback))
    assert not b.stop_flag


@pytest.mark.parametrize("kind,target", [("command", "git status"), ("edit", "same.py")])
@pytest.mark.parametrize("allow", [False, True])
def test_missing_owner_never_falls_back_to_active(confirmation_ui, kind, target, allow):
    ui, new_session, _, _ = confirmation_ui
    active = new_session()
    session.set_active(active)
    result, done = {}, threading.Event()
    if kind == "command":
        ui._on_confirm_request(target, result, done)
        ui._resolve_command_confirm(allow, remember=True)
    else:
        ui._on_edit_confirm_request(target, "-old\n+new", result, done)
        ui._resolve_edit_confirm(allow, remember=True)
    assert done.is_set()
    assert not active.stop_flag
    assert not _trust(active, kind)


@pytest.mark.parametrize("kind,target", [("command", "git status"), ("edit", "same.py")])
@pytest.mark.parametrize("allow", [False, True])
def test_phone_confirmation_retains_request_owner(confirmation_ui, monkeypatch, kind, target, allow):
    from src.ui import confirm_bars
    ui, new_session, start, pump = confirmation_ui
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", True)
    monkeypatch.setattr(confirm_bars.telegram_push, "push_confirm", lambda *args, **kwargs: None)
    monkeypatch.setattr(confirm_bars.telegram_push, "edit_message_text", _forbidden)
    a, b = new_session(), new_session()
    session.set_active(a)
    thread, result = start(a, kind, target)
    pump(lambda: _holder(ui, kind) is not None)
    session.set_active(b)
    with confirm_bars._pending_lock:
        ids = [cid for cid, entry in confirm_bars._pending_remote_confirms.items()
               if entry["result"].get("_session") is a]
    assert len(ids) == 1
    assert confirm_bars._resolve_remote_confirm(ids[0], allow, remember=allow)
    thread.join(5)
    assert result == [(allow, "")]
    assert a.stop_flag is (not allow)
    assert not b.stop_flag
    assert bool(_trust(a, kind)) is allow
    assert not _trust(b, kind)
    pump(lambda: _holder(ui, kind) is None)
