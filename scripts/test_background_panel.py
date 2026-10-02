"""B10b：真实 Qt 入口、队列回执与真实子进程，禁止调用模型/CLI。"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src import background as bg, session, state


@pytest.fixture
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    app = QApplication.instance() or QApplication([])
    original_font = QFont(app.font())
    if not getattr(app, "_b10b_fonts_loaded", False):
        for name in ("msyh.ttc", "consola.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
        app._b10b_fonts_loaded = True
    font = QFont("Microsoft YaHei")
    font.setPixelSize(14)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    yield app
    app.setFont(original_font)


def wait(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "未达到预期状态"


def dispose(widget, app):
    from PySide6.QtCore import QEvent
    import shiboken6
    if shiboken6.isValid(widget):
        widget.close()
        widget.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


@pytest.fixture(autouse=True)
def manager_env(project_dir, isolated_memory, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("真实模型/CLI/网络不可用于面板测试")

    from src import models, mcp_client
    from unittest.mock import Mock
    fake_llm = Mock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.invoke.side_effect = forbidden
    fake_llm.stream.side_effect = forbidden
    import socket
    # 同时保留 Python 真子进程，用可执行文件身份阻断真实 CLI。
    real_popen = subprocess.Popen
    real_kill_tree = bg._kill_proc_tree

    def guarded_popen(args, *a, **k):
        exe = str(args[0] if isinstance(args, (list, tuple)) else args).lower()
        assert "claude" not in exe, "测试禁止启动 Claude CLI"
        return real_popen(args, *a, **k)

    def kill_test_process(proc, timeout=3.0):
        if isinstance(proc, Proc):
            # 模拟对象没有系统进程身份；不能把它的 pid=0 传给 taskkill。
            proc.kill()
            return {"request_sent": True, "tree_error": "", "error": ""}
        return real_kill_tree(proc, timeout=timeout)

    # 单独持有安全守卫：恢复用例自己的失败注入时，守卫仍覆盖清理阶段。
    with pytest.MonkeyPatch.context() as guard:
        guard.setattr(models, "_create_llm", lambda *a, **k: fake_llm)
        guard.setattr(mcp_client, "init_mcp", lambda: None)
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        guard.setattr(subprocess, "Popen", guarded_popen)
        guard.setattr(bg, "_kill_proc_tree", kill_test_process)
        try:
            yield
        finally:
            monkeypatch.undo()
            try:
                bg.stop_all(wait_timeout=3)
            finally:
                with bg._LOCK:
                    bg._tasks.clear()
                    bg._closing = False


@pytest.fixture
def panel(qapp):
    from src.ui.background_panel import BackgroundPanel
    from src.ui.theme import THEMES
    widget = BackgroundPanel(THEMES["light"].__getitem__)
    widget.show()
    qapp.processEvents()
    yield widget
    dispose(widget, qapp)


class Proc:
    def __init__(self, code=None):
        self.code = code
        self.pid = 0

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.code

    def kill(self):
        self.code = -9


def register(proc=None, sess=None, command="fixture"):
    sess = sess or session.get_active()
    return bg.start(command, proc or Proc(), cwd="frozen-cwd", project="frozen-project",
                    **bg.capture_session_identity(sess), spawn_reader=False)


def select(panel, bg_id):
    from PySide6.QtCore import Qt
    panel.refresh()
    for index in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(index)
        if item.data(0, Qt.UserRole) == bg_id:
            panel.tree.setCurrentItem(item)
            assert panel._selected_id == bg_id
            return item
    pytest.fail(f"列表中没有 {bg_id}")


def spawn(script, sess):
    proc = subprocess.Popen([sys.executable, "-u", "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    return bg.start(script, proc, cwd="real-cwd", project="real-project",
                    **bg.capture_session_identity(sess)), proc


@pytest.mark.parametrize("fields,label", [
    ({"running": True}, "运行中"),
    ({"running": None, "exit_code": None}, "状态未知"),
    ({"running": True, "stop_in_progress": True}, "停止中"),
    ({"running": True, "stop_dispatched": True}, "已请求停止"),
    ({"running": True, "stop_error": "denied"}, "停止未确认"),
    ({"running": False, "exit_code": 0}, "主进程正常退出"),
    ({"running": False, "exit_code": 5}, "主进程异常退出"),
    ({"running": False, "exit_code": None}, "退出码未知"),
    ({"running": False, "exit_code": -9, "end_kind": "stopped"}, "停止请求后"),
])
def test_states_never_claim_task_verified(fields, label):
    from src.ui.background_panel import process_status
    text, _ = process_status(fields)
    assert label in text
    assert "验证通过" not in text and "任务完成" not in text


def test_two_real_commands_stop_only_selected(panel, qapp):
    first = session.get_active()
    first.current_session_id = "session-A"
    first.active_run_id = "run-A"
    second = session.Session()
    second.current_session_id = "session-B"
    a, pa = spawn("import time; print('output-A', flush=True); time.sleep(30)", first)
    b, pb = spawn("import time; print('output-B', flush=True); time.sleep(30)", second)
    try:
        wait(qapp, lambda: "output-A" in "".join(bg.get_snapshot(a)["output_tail"]))
        wait(qapp, lambda: "output-B" in "".join(bg.get_snapshot(b)["output_tail"]))
        item = select(panel, a)
        assert "session-A" in item.text(2)
        assert item.text(4) == "—"
        assert "run-A" in panel.metadata.toPlainText()
        assert panel.output.toPlainText().strip() == "output-A"
        session.set_active(second)
        panel.stop_btn.click()
        wait(qapp, lambda: not panel._busy)
        assert pa.poll() is not None and pb.poll() is None
        assert "已确认主进程退出" in panel.stop_note.text()
        select(panel, b)
        assert panel.output.toPlainText().strip() == "output-B"
        assert "session-B" in panel.metadata.toPlainText()
    finally:
        bg.stop(a, wait_timeout=3)
        bg.stop(b, wait_timeout=3)
        pa.wait(timeout=5)
        pb.wait(timeout=5)


def test_real_nonzero_exit_and_timer_refresh(panel, qapp):
    bg_id, proc = spawn("import sys; print('failed-output'); sys.exit(7)", session.get_active())
    proc.wait(timeout=5)
    wait(qapp, lambda: bg_id in panel._snapshots and panel._snapshots[bg_id]["output_complete"])
    item = select(panel, bg_id)
    assert item.text(4) == "7"
    assert "异常退出" in panel.detail_title.text()
    assert "failed-output" in panel.output.toPlainText()
    assert not panel.stop_btn.isEnabled()


def test_stopping_is_async_duplicate_safe_and_late_reply_stays_with_id(panel, qapp, monkeypatch):
    a, b = register(), register(command="other")
    select(panel, a)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, threads = [], []
    original_stop = bg.stop

    def delayed(bg_id, **kwargs):
        calls.append(bg_id)
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        result = original_stop(bg_id, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(bg, "stop", delayed)
    main_thread = threading.get_ident()
    try:
        panel.stop_btn.click()
        assert entered.wait(2)
        assert not panel.stop_btn.isEnabled()
        panel._stop_selected()  # 即使程序直接重复触发也不启动第二条线程。
        select(panel, b)
        from PySide6.QtCore import QTimer
        responsive = []
        QTimer.singleShot(0, panel, lambda: responsive.append(True))
        wait(qapp, lambda: responsive)
        assert calls == [a] and threads[0] != main_thread
        assert "已确认主进程退出" not in panel.stop_note.text()
        release.set()
        assert finished.wait(3)
        # 回执是队列 Signal，不由工作线程直接改控件。
        assert a in panel._busy
        wait(qapp, lambda: a not in panel._busy)
        assert panel._selected_id == b and panel.stop_btn.isEnabled()
        assert "已确认主进程退出" not in panel.stop_note.text()
        select(panel, a)
        assert "已确认主进程退出" in panel.stop_note.text()
    finally:
        release.set()
        wait(qapp, lambda: not panel._busy)


def test_stop_failure_keeps_record_and_can_retry(panel, qapp, monkeypatch):
    bg_id = register()
    select(panel, bg_id)
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k:
                        {"request_sent": False, "error": "permission denied"})
    panel.stop_btn.click()
    wait(qapp, lambda: not panel._busy)
    assert bg.get_snapshot(bg_id)["running"] is True
    assert "尚未确认" in panel.stop_note.text()
    assert "permission denied" in panel.output_note.toPlainText()
    assert panel.stop_btn.text() == "重试停止" and panel.stop_btn.isEnabled()

    def accepted(proc, **kwargs):
        proc.kill()
        return {"request_sent": True, "tree_error": "descendants not verified"}

    monkeypatch.setattr(bg, "_kill_proc_tree", accepted)
    panel.stop_btn.click()
    wait(qapp, lambda: not panel._busy)
    assert bg.get_snapshot(bg_id)["stop_attempts"] == 2
    assert "已确认主进程退出" in panel.stop_note.text()
    assert "descendants not verified" in panel.output_note.toPlainText()
    assert not panel.stop_btn.isEnabled()


def test_exit_before_click_does_not_dispatch_stop(panel, monkeypatch):
    proc = Proc()
    bg_id = register(proc)
    select(panel, bg_id)
    proc.code = 0
    calls = []
    monkeypatch.setattr(bg, "stop", lambda *a, **k: calls.append(a))
    panel.stop_btn.click()
    assert not calls and not panel._busy
    assert "正常退出" in panel.detail_title.text()


def test_frozen_sources_and_current_scope_do_not_change_session_binding(panel):
    a = session.get_active()
    a.current_session_id, a.active_run_id = "A", "run-A"
    a_id = register(sess=a)
    b = session.Session()
    b.current_session_id, b.is_subagent = "child-B", True
    b_id = register(sess=b)
    select(panel, b_id)
    assert "子 Agent" in panel.metadata.toPlainText()
    assert "frozen-cwd" in panel.metadata.toPlainText()
    assert session.get_bound() is None
    session.set_active(b)
    state.current_project = "other-project"
    panel.refresh()
    assert "frozen-project" in panel.metadata.toPlainText()
    assert session.get_active() is b and session.get_bound() is None
    panel.scope_combo.setCurrentIndex(1)
    assert set(panel._snapshots) == {b_id}
    session.set_active(a)
    panel.refresh()
    assert set(panel._snapshots) == {a_id}
    assert not panel.stop_btn.isEnabled()  # 原选择 B 不会自动指向 A。
    assert "不在当前列表" in panel.detail_title.text()
    panel.scope_combo.setCurrentIndex(0)
    assert set(panel._snapshots) == {a_id, b_id}
    assert "child-B" in panel.metadata.toPlainText()


def test_output_tail_plaintext_loss_and_read_error(panel, monkeypatch):
    monkeypatch.setattr(bg.limits, "BG_MAX_OUTPUT_CHARS", 80)
    bg_id = register()
    with bg._LOCK:
        task = bg._tasks[bg_id]
        bg._append_output_locked(task, "x" * 100)
        bg._append_output_locked(task, "<script>raw-output</script>\n")
        task.read_error = "broken pipe"
    select(panel, bg_id)
    assert panel.output.toPlainText() == "<script>raw-output</script>\n"
    notes = panel.output_note.toPlainText()
    assert "100" in notes and "无法补读" in notes and "broken pipe" in notes
    assert "读取失败" in notes and "未捕获到输出" not in notes


def test_output_incomplete_distinct_from_no_output(panel):
    bg_id = register()
    with bg._LOCK:
        bg._tasks[bg_id].reader_done.clear()
    select(panel, bg_id)
    assert "仍在读取" in panel.output_note.toPlainText()
    with bg._LOCK:
        bg._tasks[bg_id].reader_done.set()
    panel.refresh()
    assert "未捕获到输出" in panel.output_note.toPlainText()


def test_reading_scrollback_does_not_jump_to_end(panel, qapp):
    bg_id = register()
    with bg._LOCK:
        bg._append_output_locked(bg._tasks[bg_id], "\n".join(f"line {i}" for i in range(200)))
    select(panel, bg_id)
    qapp.processEvents()
    bar = panel.output.verticalScrollBar()
    assert bar.maximum() > 10
    bar.setValue(10)
    assert not panel.follow_box.isChecked()
    with bg._LOCK:
        bg._append_output_locked(bg._tasks[bg_id], "\nnext-line")
    panel.refresh()
    assert bar.value() == 10
    panel.follow_box.setChecked(True)
    assert bar.value() == bar.maximum()


def test_evicted_selection_never_controls_another_record(panel, monkeypatch):
    monkeypatch.setattr(bg.limits, "BG_MAX_RETAINED_EXITED", 1)
    a_id = register(Proc(0))
    select(panel, a_id)
    b_id = register(Proc(0), command="second")
    panel.refresh()
    assert set(panel._snapshots) == {b_id}
    assert panel._selected_id == a_id
    assert not panel.stop_btn.isEnabled()
    assert "不能据此判断" in panel.output_note.toPlainText()
    calls = []
    monkeypatch.setattr(bg, "stop", lambda *a, **k: calls.append(a))
    panel._stop_selected()
    assert not calls


def test_refresh_failure_shows_stale_data_and_disables_control(panel, monkeypatch):
    bg_id = register()
    select(panel, bg_id)
    original = bg.list_snapshots

    def fail(**kwargs):
        raise OSError("cannot snapshot")

    monkeypatch.setattr(bg, "list_snapshots", fail)
    panel.refresh()
    assert "旧数据" in panel.list_note.text()
    assert not panel.stop_btn.isEnabled()
    panel._stop_selected()
    assert not panel._busy
    monkeypatch.setattr(bg, "list_snapshots", original)
    panel.refresh()
    assert panel.stop_btn.isEnabled()


def test_click_snapshot_failure_dispatches_nothing(panel, monkeypatch):
    bg_id = register()
    select(panel, bg_id)
    calls = []

    def fail(*args, **kwargs):
        raise OSError("cannot poll")

    monkeypatch.setattr(bg, "get_snapshot", fail)
    monkeypatch.setattr(bg, "stop", lambda *a, **k: calls.append(a))
    panel.stop_btn.click()
    assert not calls and not panel._busy
    assert "未请求停止" in panel.stop_note.text()


def test_unknown_process_and_failed_stop_remain_unknown(panel, qapp, monkeypatch):
    class Unknown(Proc):
        def poll(self):
            raise OSError("lost process status")

    bg_id = register(Unknown())
    item = select(panel, bg_id)
    assert item.text(4) == "—" and "状态未知" in item.text(1)
    calls = []
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: calls.append(a))
    panel.stop_btn.click()
    wait(qapp, lambda: not panel._busy)
    assert not calls
    assert "状态未知" in panel.detail_title.text()
    assert "尚未确认" in panel.stop_note.text()
    assert "lost process status" in panel.output_note.toPlainText()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_narrow_window_preserves_command_width_and_scrollable_diagnostics(panel, qapp, theme):
    from src.ui.theme import THEMES
    panel._color = THEMES[theme].__getitem__
    panel.apply_theme()
    bg_id = register()
    with bg._LOCK:
        bg._tasks[bg_id].read_error = "long diagnostic " * 100
    select(panel, bg_id)
    panel.resize(600, 520)
    qapp.processEvents()
    assert panel.tree.columnWidth(0) >= 220
    assert panel.tree.horizontalScrollBar().maximum() > 0
    assert panel.output_note.height() <= 85
    assert panel.output_note.verticalScrollBar().maximum() > 0
    assert panel.output.height() >= 90
    assert panel.output.viewport().height() >= 60
    assert panel.output.parentWidget().rect().contains(panel.output.geometry())


def test_expanded_details_and_stop_feedback_keep_output_inside_its_containers(panel, qapp):
    bg_id = register()
    with bg._LOCK:
        task = bg._tasks[bg_id]
        task.read_error = "long diagnostic " * 100
        bg._append_output_locked(task, "\n".join(f"output line {i}" for i in range(30)))
    select(panel, bg_id)
    panel.resize(600, 560)
    panel.info_btn.click()
    panel._feedback[bg_id] = "停止尚未确认：" + "long error " * 8
    panel._render_selected()

    def visible_output():
        from PySide6.QtCore import QPoint, QRect
        parent = panel.output.parentWidget()
        while parent is not None:
            # 检查输出本身在各层祖先里的可见范围；容器的边框或留白不代表输出。
            bounds = QRect(panel.output.mapTo(parent, QPoint()), panel.output.size())
            if not parent.rect().contains(bounds):
                return False
            if parent is panel:
                break
            parent = parent.parentWidget()
        return panel.output.viewport().height() >= 60

    wait(qapp, visible_output)
    assert panel.metadata.isVisible() and panel.stop_note.isVisible()
    assert panel.output.height() >= 90
    assert panel.output.verticalScrollBar().maximum() > 0


def test_stop_thread_start_failure_reported(panel, monkeypatch):
    bg_id = register()
    select(panel, bg_id)
    monkeypatch.setattr(threading.Thread, "start", lambda *a:
                        (_ for _ in ()).throw(RuntimeError("thread unavailable")))
    panel.stop_btn.click()
    assert not panel._busy
    assert "无法启动停止线程" in panel.stop_note.text()
    assert bg.get_snapshot(bg_id)["running"] is True and panel.stop_btn.isEnabled()


def test_hide_close_and_reopen_only_change_refresh(panel, qapp):
    bg_id = register()
    select(panel, bg_id)
    assert panel._timer.isActive()
    panel.close()
    assert not panel._timer.isActive() and bg.get_snapshot(bg_id)["running"] is True
    panel.show()
    qapp.processEvents()
    assert panel._timer.isActive() and panel._selected_id == bg_id


def test_destroyed_panel_tolerates_late_stop_receipt(panel, qapp, monkeypatch):
    bg_id = register()
    select(panel, bg_id)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_stop = bg.stop
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))

    def delayed(bg_id, **kwargs):
        entered.set()
        assert release.wait(5)
        result = original_stop(bg_id, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(bg, "stop", delayed)
    try:
        panel.stop_btn.click()
        assert entered.wait(2)
        dispose(panel, qapp)
        release.set()
        assert finished.wait(3)
        wait(qapp, lambda: not any(t.name == f"bg-stop-{bg_id}" for t in threading.enumerate()))
        assert not errors
        assert bg.get_snapshot(bg_id)["running"] is False
    finally:
        release.set()


def test_real_sidebar_entry_reuses_panel_and_hides_with_main_window(qapp, monkeypatch):
    from src import tools
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    ui = ChatUI()
    try:
        ui.show()
        qapp.processEvents()
        ui.background_btn.click()
        widget = ui._background_panel
        assert widget.isVisible() and not widget.isModal() and widget._timer.isActive()
        ui.background_btn.click()
        assert ui._background_panel is widget
        ui.hide()
        assert not widget.isVisible() and not widget._timer.isActive()
        ui.show()
        ui.background_btn.click()
        assert widget.isVisible()
        ui.theme = "dark"
        ui._apply_theme()
        assert ui._t("win_bg") in widget.styleSheet()
    finally:
        state.ui_ref = None
        dispose(ui, qapp)


def test_new_process_has_no_old_pid_control(panel):
    bg_id = register()
    code = """
import sys
from src import background
assert background.list_snapshots() == []
assert background.stop(sys.argv[1])['found'] is False
"""
    result = subprocess.run([sys.executable, "-c", code, bg_id], capture_output=True,
                            text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
