"""B11a 撤销按钮：真实 ChatUI + Qt 事件循环 + 可控假 worker。

回答四个问题：
1. **worker 真正退出前不撤销**——is_generating 被停止立刻置 False、旧线程还在收尾时，
   点撤销只等待不执行；等待超时取消，绝不到点硬做。
2. **撤销期间占住既有运行准入闸**——resume_pending 置位后，同一会话的新一轮发送 /
   队列派发都被 _gate_new_run_request 挡住（不另写一套只看 is_generating 的判断）。
3. **只撤当前会话、当前工作区的记录**——预检不通过（冲突/无记录）时明确说明、
   文件不动、材料保留。
4. **结果分开报告**——恢复、验证义务接入、保存各有交代。

全程不碰真实模型 / Claude CLI / 网络（主动阻断夹具）。
"""
import gc
import os
import threading
import time

import pytest
from PySide6.QtCore import QEvent

from src import agent, file_history as fh, memory, session, state
from src.ui.chat_window import ChatUI


# ══════════════════════════════════════════════════════════════
# 夹具（与 test_b09_exit_barrier 同一套纪律）
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


@pytest.fixture(autouse=True)
def _memory_write_guard(monkeypatch, isolated_memory):
    import src.paths as _paths
    from src import memory as _memory
    expected = os.path.normcase(str(_paths.memory_dir()))
    bad = []
    real_write = _memory._atomic_write_json

    def _spy_write(path, *a, **k):
        d = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
        if os.path.commonpath([d, expected]) != expected:
            bad.append(d)
        return real_write(path, *a, **k)

    monkeypatch.setattr(_memory, "_atomic_write_json", _spy_write)
    yield
    assert not bad, f"chat_memory 写入越界 {len(bad)} 次: {sorted(set(bad))[:3]}"


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait_until(app, pred, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("Model/network access is prohibited in undo button tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import mcp_client, models, tools

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    from src import config
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)

    ui = ChatUI()
    ui._RESUME_WAIT_MS = 10         # 实例属性遮蔽类属性：等待提速，超时语义不变
    ui._RESUME_WAIT_LIMIT = 40
    yield ui
    if state.ui_ref is ui:
        state.ui_ref = None
    _destroy_ui_cleanly(ui, qapp)


def _destroy_ui_cleanly(ui, app):
    import shiboken6
    if not shiboken6.isValid(ui):
        return
    if state.ui_ref is ui:
        state.ui_ref = None
    ui.close()
    app.processEvents()
    app.processEvents()
    ui.deleteLater()
    gc.collect()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def _toasts(ui):
    if not hasattr(ui, "_toast_log"):
        ui._toast_log = []
        ui._show_toast = lambda text, duration=1500: ui._toast_log.append(str(text))
    return ui._toast_log


@pytest.fixture()
def workspace(tmp_path):
    proj = tmp_path / "ws"
    proj.mkdir()
    return proj


_call_counter = {"n": 0}


def _setup_session_with_record(ui, workspace, *, name="button.txt",
                               pre="v1\n", written="v2\n"):
    """真实路径造一条可撤销记录：保存会话 → 绑定 → _execute_tool 写文件。

    ChatUI 构造时把 state.ui_ref 指到了自己，写文件的 diff 确认卡会阻塞等点击；
    造数期间临时置空 ui_ref 让写入自动放行（CLI/无 UI 的既有语义），随后还原。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    state.current_project = str(workspace)
    sess = session.get_active()
    sess.project = str(workspace)
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="改文件")]
    memory.save_session(session=sess)
    session.bind_thread(sess)

    (workspace / name).write_text(pre, encoding="utf-8")
    _call_counter["n"] += 1
    sess.active_run_id = f"run-ui-{_call_counter['n']}"
    from src import streaming
    saved_ui_ref = state.ui_ref
    state.ui_ref = None
    try:
        streaming._execute_tool(
            {"name": "write_file", "args": {"path": name, "content": written},
             "id": f"call-ui-{_call_counter['n']}"}, ui)
    finally:
        state.ui_ref = saved_ui_ref
    cand = fh.latest_undoable(sess.current_session_id, fh.session_workspace())
    assert cand is not None, "造数失败：没有可撤销记录"
    return sess, cand


def _plant_finishing_worker(sess):
    """摆出"已停止但旧 worker 还在收尾"：is_generating=False、线程活着。"""
    release = threading.Event()
    worker = threading.Thread(target=release.wait, args=(10,), daemon=True)
    worker.start()
    sess.last_worker = worker
    sess.is_generating = False
    return worker, release


# ══════════════════════════════════════════════════════════════
# 用例
# ══════════════════════════════════════════════════════════════

class TestUndoButtonBarrier:
    def test_worker_alive_undo_waits_then_cancels_on_timeout(self, ui, workspace):
        sess, cand = _setup_session_with_record(ui, workspace)
        worker, release = _plant_finishing_worker(sess)
        try:
            toasts = _toasts(ui)
            ui._on_undo_click()
            assert sess.resume_pending is True      # 等待期间占住屏障
            # 条件等待：固定泵送对 40×10ms 的 tick 余量不足，负载高会翻车
            assert _wait_until(qapp_instance(),
                               lambda: any("撤销已取消" in t for t in toasts), 5.0)
            assert "撤销已取消" in "\n".join(toasts)
            assert sess.resume_pending is False     # 取消后释放
            target = workspace / "button.txt"
            assert target.read_text(encoding="utf-8") == "v2\n"   # 未被恢复
            record, _ = fh._load_record(cand["checkpoint_id"])
            assert record["phase"] == fh.PHASE_UNDOABLE           # 材料原样保留
        finally:
            release.set()
            worker.join(5)

    def test_worker_exit_then_undo_executes(self, ui, workspace):
        sess, cand = _setup_session_with_record(ui, workspace)
        worker, release = _plant_finishing_worker(sess)
        try:
            _toasts(ui)
            target = workspace / "button.txt"
            ui._on_undo_click()
            assert sess.resume_pending is True
            release.set()                        # 旧 worker 真正退出
            assert _wait_until(qapp_instance(),
                               lambda: target.read_text(encoding="utf-8") == "v1\n")
            assert sess.resume_pending is False
            record, _ = fh._load_record(cand["checkpoint_id"])
            assert record["phase"] == fh.PHASE_UNDONE
            # 验证义务接入：恢复改变了文件，旧结论作废
            assert "button.txt" in (sess.pending_verification or {}).get("files", [])
        finally:
            release.set()
            worker.join(5)

    def test_gate_blocks_new_runs_during_undo_wait(self, ui, workspace):
        sess, _cand = _setup_session_with_record(ui, workspace)
        worker, release = _plant_finishing_worker(sess)
        try:
            _toasts(ui)
            ui._on_undo_click()
            assert sess.resume_pending is True
            # 既有运行准入闸被占住：不另写只看 is_generating 的判断
            assert ui._gate_new_run_request(sess, "send", text="x") == "busy"
            assert ui._gate_new_run_request(sess, "queue", text="x") == "busy"
            assert ui._gate_new_run_request(sess, "retry") == "busy"
        finally:
            release.set()
            worker.join(5)
            assert _wait_until(qapp_instance(),
                               lambda: sess.resume_pending is False)
            # 屏障释放后恢复接纳（这里只验证闸门状态，不真起 worker）
            assert ui._gate_new_run_request(sess, "send", text="x") == "start"

    def test_session_switch_cancels_undo(self, ui, workspace):
        sess, cand = _setup_session_with_record(ui, workspace)
        worker, release = _plant_finishing_worker(sess)
        try:
            toasts = _toasts(ui)
            ui._on_undo_click()
            assert sess.resume_pending is True
            other = session.Session()
            session.set_active(other)            # 等待期间切走会话
            assert _wait_until(qapp_instance(),
                               lambda: any("已切换到其它会话" in t for t in toasts), 5.0)
            assert "已切换到其它会话" in "\n".join(toasts)
            assert sess.resume_pending is False
            target = workspace / "button.txt"
            assert target.read_text(encoding="utf-8") == "v2\n"
            record, _ = fh._load_record(cand["checkpoint_id"])
            assert record["phase"] == fh.PHASE_UNDOABLE
        finally:
            release.set()
            worker.join(5)


class TestUndoButtonQueries:
    def test_no_records_shows_toast(self, ui, workspace):
        state.current_project = str(workspace)
        sess = session.get_active()
        sess.project = str(workspace)
        from langchain_core.messages import HumanMessage, SystemMessage
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        memory.save_session(session=sess)
        session.bind_thread(sess)
        toasts = _toasts(ui)
        ui._on_undo_click()
        assert any("没有可撤销" in t for t in toasts)

    def test_conflict_reported_and_file_untouched(self, ui, workspace):
        sess, cand = _setup_session_with_record(ui, workspace)
        target = workspace / "button.txt"
        target.write_text("user-newer\n", encoding="utf-8")   # AI 写入后用户手动改
        toasts = _toasts(ui)
        ui._on_undo_click()
        assert any("又被修改" in t for t in toasts)
        assert target.read_text(encoding="utf-8") == "user-newer\n"
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDOABLE           # 材料保留
        assert sess.resume_pending is False

    def test_button_disabled_without_records(self, ui, workspace):
        state.current_project = str(workspace)
        sess = session.get_active()
        sess.project = str(workspace)
        from langchain_core.messages import HumanMessage, SystemMessage
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        memory.save_session(session=sess)
        ui._style_undo_btn()
        assert ui.undo_btn.isEnabled() is False

    def test_button_enabled_after_recorded_write(self, ui, workspace):
        _setup_session_with_record(ui, workspace)
        ui._style_undo_btn()
        assert ui.undo_btn.isEnabled() is True

    def test_pending_send_request_blocks_undo(self, ui, workspace):
        sess, _cand = _setup_session_with_record(ui, workspace)
        toasts = _toasts(ui)
        import types

        ui._pending_run_for = lambda s: types.SimpleNamespace(cancelled=False)
        ui._on_undo_click()
        assert any("请先处理再撤销" in t for t in toasts)
        assert sess.resume_pending is False
        target = workspace / "button.txt"
        assert target.read_text(encoding="utf-8") == "v2\n"


class TestUndoResultReporting:
    def test_results_reported_separately_on_save_failure(self, ui, workspace, monkeypatch):
        sess, cand = _setup_session_with_record(ui, workspace)
        toasts = _toasts(ui)
        from src import memory as _memory

        def _boom(*a, **k):
            raise OSError("save failed (simulated)")

        monkeypatch.setattr(_memory, "save_session_report", _boom)
        ui._on_undo_click()
        monkeypatch.undo()
        joined = "\n".join(toasts)
        assert "已撤销" in joined                       # 恢复成功
        assert "保存失败" in joined                     # 保存失败单独说
        target = workspace / "button.txt"
        assert target.read_text(encoding="utf-8") == "v1\n"
        assert sess.resume_pending is False
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDONE

    def test_changed_but_unverified_restore_still_invalidates(self, ui, workspace,
                                                              monkeypatch):
        """P1-3：恢复写入完成但读回校验失败——按钮不得说"撤销未执行"，
        必须报告已恢复 + 校验未完成，并照常作废旧验证结论。"""
        sess, _cand = _setup_session_with_record(ui, workspace)
        toasts = _toasts(ui)

        monkeypatch.setattr(fh, "_read_bytes_now", lambda path: (None, "injected"))
        ui._on_undo_click()
        monkeypatch.undo()

        joined = "\n".join(toasts)
        assert "已撤销" in joined                       # 不是"撤销未执行"
        assert "校验" in joined                         # 校验失败单独说明
        target = workspace / "button.txt"
        assert target.read_text(encoding="utf-8") == "v1\n"     # 文件确实恢复了
        assert "button.txt" in (sess.pending_verification or {}).get("files", [])
        assert sess.verification["tests_passed"] is None
        assert sess.resume_pending is False


def qapp_instance():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance()
