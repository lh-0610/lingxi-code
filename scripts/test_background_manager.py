"""B10a 的操作边界：真实进程/发送层与失败注入，不调用真实模型。"""
import io
import re
import subprocess
import sys
import threading
import time

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from src import background as bg, limits, memory, session
from src.tools import (
    _run_command, list_background_commands, read_background_output,
    run_command, stop_background_command,
)


@pytest.fixture(autouse=True)
def manager_env(project_dir, isolated_memory, monkeypatch):
    yield
    # 恢复终止助手再清理真实进程；失败注入不能污染下一条用例。
    monkeypatch.undo()
    bg.stop_all(wait_timeout=3)
    with bg._LOCK:
        bg._tasks.clear()
        bg._closing = False


class Proc:
    def __init__(self, code=None, stdout=None):
        self.code = code
        self.pid = 0
        self.stdout = stdout

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.code

    def kill(self):
        self.code = -9


def register(proc=None, **kwargs):
    return bg.start("fixture", proc or Proc(), cwd="fixture-cwd", project=None,
                    **bg.capture_session_identity(session.current_session()),
                    spawn_reader=False, **kwargs)


def spawn(script):
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)
    bg_id = bg.start("real child", proc, cwd=None, project=None,
                     **bg.capture_session_identity(session.current_session()))
    return bg_id, proc


def wait_snapshot(bg_id, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = bg.get_snapshot(bg_id)
        if snap and predicate(snap):
            return snap
        time.sleep(0.02)
    pytest.fail(f"后台任务未达到预期状态: {bg.get_snapshot(bg_id)}")


def test_snapshot_and_shutdown_do_not_reenter_registry_lock():
    # 子进程限定整个调用链的时间，旧实现的自死锁不会把 pytest 也挂住。
    code = """
from src import background as b
class P:
    def poll(self): return 0
    def wait(self, timeout=None): return 0
i = b.start('done', P(), cwd=None, project=None, session_id='s',
            session_key='', is_subagent=False, run_id='', task_id=None,
            spawn_reader=False)
assert b.get_snapshot(i)['exit_code'] == 0
assert b.list_snapshots()[0]['bg_id'] == i
assert b.stop_all(.1)[0]['running'] is False
"""
    child = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           timeout=10, text=True, encoding="utf-8")
    assert child.returncode == 0, child.stderr


def test_snapshot_observes_poll_once_and_never_invents_zero():
    class Changing(Proc):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls <= 2 else 7

    proc = Changing()
    bg_id = register(proc)
    snap = bg.get_snapshot(bg_id)
    assert snap["running"] is True and snap["exit_code"] is None
    assert proc.calls == 2  # 注册一次、快照一次
    snap = bg.get_snapshot(bg_id)
    assert snap["running"] is False and snap["exit_code"] == 7


def test_poll_failure_is_unknown_and_does_not_send_kill(monkeypatch):
    class Unknown(Proc):
        def poll(self):
            raise OSError("cannot observe")

    bg_id = register(Unknown())
    calls = []
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: calls.append(a))
    snap = bg.get_snapshot(bg_id)
    assert snap["running"] is None and snap["exit_code"] is None
    assert "cannot observe" in read_background_output.func(bg_id)
    result = bg.stop(bg_id, .01)
    assert not result["confirmed"] and calls == []


def test_exit_time_is_observed_without_a_ui_refresh():
    bg_id, proc = spawn("print('done', flush=True)")
    try:
        proc.wait(5)
        # 只等退出观察线程；不以 get_snapshot 帮它登记退出。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with bg._LOCK:
                ended = bg._tasks[bg_id].ended_at
            if ended is not None:
                break
            time.sleep(.02)
        assert ended is not None
        time.sleep(1.1)
        snap = bg.get_snapshot(bg_id)
        assert snap["elapsed_s"] < 1
        assert snap["ended_at"] == ended
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def test_spawn_freezes_caller_before_popen_switches_foreground(monkeypatch):
    a = session.get_active()
    a.current_session_id = "session-A"
    a.current_task = {"id": "task-A"}
    a.active_run_id = "run-A"
    b = session.Session()
    b.current_session_id = "session-B"
    real_popen = subprocess.Popen

    def switching(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        session.set_active(b)
        return proc

    monkeypatch.setattr(subprocess, "Popen", switching)
    response = _run_command(f'"{sys.executable}" -c "print(1)"', None, True)
    bg_id = re.search(r"\[(bg\d+-[0-9a-f]+)\]", response).group(1)
    snap = bg.get_snapshot(bg_id)
    assert (snap["session_id"], snap["task_id"], snap["run_id"]) == (
        "session-A", "task-A", "run-A")
    assert snap["owner_id"] == a.background_owner_id


def test_tools_do_not_read_or_stop_another_session(monkeypatch):
    a = session.get_active()
    bg_id = register()
    b = session.Session()
    session.set_active(b)
    kill_calls = []
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: kill_calls.append(a))
    assert "没有后台命令" in list_background_commands.func()
    assert "未找到" in read_background_output.func(bg_id)
    assert "其他会话" in stop_background_command.func(bg_id)
    assert not kill_calls
    session.set_active(a)
    assert bg_id in list_background_commands.func()


def test_bound_subagent_cannot_control_foreground_process():
    parent = session.get_active()
    parent_id = register()
    child = session.Session()
    child.is_subagent = True
    binding = session.get_bound()
    session.bind_thread(child)
    try:
        child_id = register()
        text = list_background_commands.func()
        assert child_id in text and parent_id not in text
        assert "其他会话" in stop_background_command.func(parent_id)
    finally:
        session.restore_bound(binding)
    assert parent.background_owner_id != child.background_owner_id


def test_unsaved_ownership_survives_save_and_rekey():
    sess = session.get_active()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="user")]
    bg_id = register()
    identity = bg.get_snapshot(bg_id)
    memory.save_session(session=sess)
    assert sess.current_session_id
    assert sess.background_owner_id == identity["owner_id"]
    assert bg_id in list_background_commands.func()
    assert bg.get_snapshot(bg_id)["session_id"].startswith("unsaved:")


@pytest.mark.parametrize("operation", ["reset", "load_other", "load_same"])
def test_session_reuse_does_not_inherit_control(operation):
    sess = session.get_active()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="user")]
    memory.save_session(session=sess)
    own_id = sess.current_session_id
    bg_id = register()
    if operation == "reset":
        memory.reset_history(session=sess)
    else:
        load_id = own_id
        if operation == "load_other":
            other = session.Session()
            other.chat_history = [SystemMessage(content="sys"), HumanMessage(content="other")]
            memory.save_session(session=other)
            load_id = other.current_session_id
        assert memory.load_session(load_id, session=sess)
    assert (bg_id in list_background_commands.func()) == (operation == "load_same")


def test_failed_request_does_not_turn_natural_exit_into_stopped(monkeypatch):
    proc = Proc()
    bg_id = register(proc)
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: {
        "request_sent": False, "error": "denied"})
    failed = bg.stop(bg_id, .01)
    assert failed["alive"] and failed["stop_error"] == "denied"
    proc.code = 0
    final = bg.stop(bg_id, .01)
    assert final["confirmed"] and final["already_exited"]
    assert final["end_kind"] == "natural"


def test_stop_retry_clears_failure_and_does_not_evict_live_record(monkeypatch):
    proc = Proc()
    bg_id = register(proc)
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: {
        "request_sent": False, "error": "denied"})
    assert bg.stop(bg_id, .01)["stop_error"] == "denied"
    for _ in range(limits.BG_MAX_RETAINED_EXITED + 2):
        register(Proc(0))
    assert bg.get_snapshot(bg_id)["running"] is True

    def kill(p, **kwargs):
        p.code = -9
        return {"request_sent": True}

    monkeypatch.setattr(bg, "_kill_proc_tree", kill)
    done = bg.stop(bg_id, .01)
    assert done["confirmed"] and done["stop_error"] == ""
    assert done["stop_attempts"] == 2


def test_wait_error_keeps_record_and_original_reason(monkeypatch):
    class WaitError(Proc):
        def wait(self, timeout=None):
            raise OSError("wait broke")

    bg_id = register(WaitError())
    monkeypatch.setattr(bg, "_kill_proc_tree", lambda *a, **k: {"request_sent": True})
    result = bg.stop(bg_id, .01)
    assert result["alive"] is True and "wait broke" in result["stop_error"]
    assert bg.get_snapshot(bg_id) is not None


def test_concurrent_stops_send_one_request_and_do_not_block_snapshots(monkeypatch):
    proc = Proc()
    bg_id = register(proc)
    other_id = register()
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def kill(p, **kwargs):
        calls.append(p)
        entered.set()
        assert release.wait(3)
        p.code = -9
        return {"request_sent": True}

    monkeypatch.setattr(bg, "_kill_proc_tree", kill)
    worker = threading.Thread(target=lambda: results.append(bg.stop(bg_id, 3)))
    worker.start()
    try:
        assert entered.wait(2)
        # 如果请求在全局锁内，另一任务的快照会阻塞到 release 超时。
        start_time = time.monotonic()
        assert bg.get_snapshot(other_id)["running"] is True
        second = bg.stop(bg_id, .03)
        assert time.monotonic() - start_time < .5
        assert second["in_progress"] and not second["confirmed"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(calls) == 1
    assert results[0]["confirmed"]


def test_shutdown_requests_all_before_waiting_and_has_one_budget(monkeypatch):
    requests, waits = [], []

    class Sticky(Proc):
        def wait(self, timeout=None):
            waits.append(self)
            assert len(requests) == 3
            time.sleep(timeout or 0)
            raise subprocess.TimeoutExpired("sticky", timeout)

    ids = [register(Sticky()) for _ in range(3)]

    def bounded_request(proc, timeout):
        requests.append(proc)
        time.sleep(timeout)
        return {"request_sent": False, "error": "request timed out"}

    monkeypatch.setattr(bg, "_kill_proc_tree", bounded_request)
    start_time = time.monotonic()
    snapshots = bg.stop_all(.25)
    assert time.monotonic() - start_time < .6
    assert len(waits) == 3
    assert all(s["running"] is True and s["stop_error"] for s in snapshots)
    assert all(bg.get_snapshot(i) for i in ids)


def test_windows_taskkill_failure_keeps_tree_diagnostic(monkeypatch):
    monkeypatch.setattr(bg.sys, "platform", "win32")
    proc = Proc()
    bg_id = register(proc)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout=b"access denied"))
    text = stop_background_command.func(bg_id)
    result = bg.get_snapshot(bg_id)
    assert result["running"] is False and result["exit_code"] == -9
    assert "access denied" in result["tree_error"]
    assert "access denied" in text and "进程树其余成员" in text


def test_zero_budget_does_not_launch_taskkill(monkeypatch):
    bg_id = register()
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    result = bg.stop(bg_id, 0)
    assert not result["confirmed"] and not result["stop_dispatched"]
    assert "预算" in result["stop_error"] and not calls


def test_manager_thread_start_failure_cleans_up_real_process(monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.PIPE)

    def fail(*args, **kwargs):
        raise RuntimeError("no threads")

    monkeypatch.setattr(threading.Thread, "start", fail)
    try:
        with pytest.raises(bg.BackgroundStartError) as caught:
            bg.start("start failure", proc, cwd=None, project=None,
                     **bg.capture_session_identity(session.current_session()))
        assert proc.poll() is not None
        snap = bg.get_snapshot(caught.value.bg_id)
        assert "no threads" in snap["read_error"]
        assert proc.stdout.closed
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def test_shutdown_rejects_new_launch_before_popen(monkeypatch):
    bg.shutdown(.01)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    text = _run_command('python -c "print(1)"', None, True)
    assert "应用正在退出" in text and calls == []


def test_launch_overlapping_shutdown_is_managed_and_stopped(monkeypatch):
    real_popen = subprocess.Popen
    processes = []

    def overlapping(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        # 模拟 Popen 已创建而尚未注册时主线程退出。
        bg.shutdown(.01)
        return proc

    monkeypatch.setattr(subprocess, "Popen", overlapping)
    # 终止助手自己也用 Popen，必须只在第一次创建时注入退出。
    def first_only(*args, **kwargs):
        if processes:
            return real_popen(*args, **kwargs)
        return overlapping(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", first_only)
    try:
        text = _run_command(f'"{sys.executable}" -c "import time; time.sleep(30)"', None, True)
        assert "后台管理启动失败" in text and "已确认主进程退出" in text
        assert processes[0].poll() is not None
        assert bg.list_snapshots()[0]["running"] is False
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(5)


def test_output_rolling_limits_have_exact_counters(monkeypatch):
    monkeypatch.setattr(limits, "BG_MAX_OUTPUT_LINES", 3)
    monkeypatch.setattr(limits, "BG_MAX_OUTPUT_CHARS", 20)
    bg_id = register()
    lines = [f"{i:02}\n" for i in range(40)]
    with bg._LOCK:
        for line in lines:
            bg._append_output_locked(bg._tasks[bg_id], line)
    snap = bg.get_snapshot(bg_id)
    assert snap["output_tail"] == lines[-3:]
    assert snap["output_chars"] == 9 and snap["output_dropped_chars"] == 111
    assert snap["output_dropped_lines"] == 37
    assert snap["output_total_chars"] == 120


@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
def test_no_newline_chunks_preserve_unicode_and_are_bounded(monkeypatch, encoding):
    monkeypatch.setattr(limits, "BG_MAX_NO_NEWLINE_BYTES", 7)
    text = "甲乙丙丁戊己庚辛壬癸" * 30
    proc = Proc(0, io.BytesIO(text.encode(encoding)))
    bg_id = bg.start("unicode", proc, cwd=None, project=None,
                     **bg.capture_session_identity(session.current_session()))
    snap = wait_snapshot(bg_id, lambda s: s["output_complete"])
    assert "".join(snap["output_tail"]) == text
    assert max(map(len, snap["output_tail"])) <= 7


def test_read_failure_preserves_partial_line_and_closes_stream():
    class Broken(io.BytesIO):
        def read(self, n=-1):
            if self.tell() >= len(self.getvalue()):
                raise OSError("reader broke")
            return super().read(n)

    stream = Broken(b"first\npartial")
    proc = Proc(0, stream)
    bg_id = bg.start("broken stream", proc, cwd=None, project=None,
                     **bg.capture_session_identity(session.current_session()))
    snap = wait_snapshot(bg_id, lambda s: s["output_complete"])
    assert "".join(snap["output_tail"]) == "first\npartial"
    assert stream.closed and "reader broke" in read_background_output.func(bg_id)


def test_real_output_flood_and_tool_delivery_are_bounded():
    bg_id, proc = spawn("import sys; sys.stdout.write('x'*300000 + 'THE_REAL_TAIL'); sys.exit(4)")
    try:
        snap = wait_snapshot(bg_id, lambda s: not s["running"] and s["output_complete"])
        assert snap["exit_code"] == 4
        assert snap["output_chars"] <= limits.BG_MAX_OUTPUT_CHARS
        assert len(snap["output_tail"]) <= limits.BG_MAX_OUTPUT_LINES
        assert snap["output_chars"] + snap["output_dropped_chars"] == 300013
        result = read_background_output.func(bg_id, tail=0)
        assert len(result) <= limits.BG_TOOL_OUTPUT_CHARS
        assert "缓冲已截断" in result and "本次只展示尾部" in result
        assert "THE_REAL_TAIL" in result
        from src.streaming import _cap_oversized_tool_results
        message = ToolMessage(content=result, tool_call_id="bg-output")
        sent, count = _cap_oversized_tool_results([message], budget=0)
        assert count == 0 and sent[0].content == result
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def test_exited_retention_is_capped_after_late_exit(monkeypatch):
    monkeypatch.setattr(limits, "BG_MAX_RETAINED_EXITED", 3)
    procs = [Proc() for _ in range(7)]
    ids = [register(p) for p in procs]
    assert len(bg.list_snapshots()) == 7
    for proc in procs[:-1]:
        proc.code = 0
    snapshots = bg.list_snapshots()
    assert len(snapshots) == 4
    assert bg.get_snapshot(ids[-1])["running"] is True
    assert bg.get_snapshot(ids[0]) is None


def test_background_success_does_not_create_verification_evidence():
    before = dict(session.current_session().verification)
    response = run_command.func(f'"{sys.executable}" -c "print(1)"', background=True)
    bg_id = re.search(r"\[(bg\d+-[0-9a-f]+)\]", response).group(1)
    wait_snapshot(bg_id, lambda s: not s["running"])
    after = session.current_session().verification
    assert after["tests_passed"] == before["tests_passed"]
    assert not after.get("evidence")


def test_new_interpreter_never_reuses_historical_bg_id():
    code = """
from src import background as b
class P:
    def poll(self): return 0
i = b.start('done', P(), cwd=None, project=None, session_id='s',
            session_key='', is_subagent=False, run_id='', task_id=None,
            spawn_reader=False)
print(i)
"""
    ids = []
    for _ in range(2):
        child = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               timeout=10, text=True, encoding="utf-8")
        assert child.returncode == 0, child.stderr
        ids.append(child.stdout.strip())
    assert ids[0] != ids[1]
    assert bg.stop(ids[0], .01)["found"] is False
