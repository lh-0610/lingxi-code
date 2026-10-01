"""B10a 后台任务管理测试：归属冻结、真实状态、停止流程、输出上限、退出清理。

真实短命令走正常路径，可控 FakeProc 走异常路径（停止超时、reader 失败）。
旧契约（停止后记录消失）已按新契约调整为：停止不删记录，检查真实退出与
结果记录保留。测试在仓库外隔离副本与 basetemp 中运行，不碰真实模型。
"""
import re
import subprocess
import sys
import threading
import time
from collections import deque

import pytest

from src import background, limits, session, state
from src.background import (
    get_snapshot, start, stop, stop_all,
)
from src.tools import (
    run_command, read_background_output, stop_background_command, stop_all_background,
)

_SLEEP_CMD = f'"{sys.executable}" -c "import time; time.sleep(30)"'
_ECHO_CMD = f'"{sys.executable}" -c "print(987654)"'
_FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(3)"'


def _bg_id_from(result: str) -> str:
    m = re.search(r"\[(bg\d+-[0-9a-f]+)\]", result)
    assert m, f"返回里没有 bg_id: {result!r}"
    return m.group(1)


def _wait_for(bg_id: str, marker: str, timeout: float = 8.0) -> str:
    """轮询 read_background_output 直到含 marker 或超时（reader 是异步线程）。"""
    deadline = time.time() + timeout
    out = ""
    while time.time() < deadline:
        out = read_background_output.func(bg_id)
        if marker in out:
            return out
        time.sleep(0.1)
    return out


def _wait_until_snap(bg_id, pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = get_snapshot(bg_id)
        if snap and pred(snap):
            return True
        time.sleep(0.1)
    return bool(pred(get_snapshot(bg_id)))


@pytest.fixture()
def bg_env(project_dir):
    """无 UI（免确认）+ 测试后杀光所有后台进程并清空注册表。"""
    old_ui = state.ui_ref
    state.ui_ref = None
    try:
        yield
    finally:
        stop_all_background()
        with background._LOCK:
            background._tasks.clear()
        state.ui_ref = old_ui


@pytest.fixture()
def two_sessions():
    """两个可切换的会话（归属冻结测试用）。"""
    a = session.Session()
    b = session.Session()
    old_active = session.get_active()
    session.set_active(a)
    yield a, b
    session.set_active(old_active)


# ══════════════════════════════════════════════════════════════
# 身份与归属
# ══════════════════════════════════════════════════════════════

class TestAttribution:

    def test_two_commands_from_two_sessions_attributed_separately(
            self, bg_env, two_sessions):
        """两个后台命令并行运行，归属按创建时的会话分开冻结。"""
        a, b = two_sessions
        from src.tools import _run_command
        old_binding = session.get_bound()
        session.bind_thread(a)
        try:
            r1 = _run_command(_SLEEP_CMD, None, True)
        finally:
            session.restore_bound(old_binding)
        id1 = _bg_id_from(r1)

        session.bind_thread(b)
        try:
            r2 = _run_command(_SLEEP_CMD, None, True)
        finally:
            session.restore_bound(old_binding)
        id2 = _bg_id_from(r2)

        s1, s2 = get_snapshot(id1), get_snapshot(id2)
        assert s1["session_id"] != s2["session_id"], "归属按创建会话分开"
        assert s1["session_id"].startswith("unsaved:"), "未存盘会话用运行期标识"
        assert s1["running"] and s2["running"]

    def test_switch_session_and_project_do_not_rewrite_records(
            self, bg_env, two_sessions, tmp_path, monkeypatch):
        """切前台会话 / 项目后，旧记录的归属不变（冻结，不跟随 active）。"""
        a, _b = two_sessions
        from src import state as _state
        from src.tools import _run_command
        _state.current_project = None
        old_binding = session.get_bound()
        session.bind_thread(a)
        try:
            r1 = _run_command(_SLEEP_CMD, None, True)
        finally:
            session.restore_bound(old_binding)
        bg_id = _bg_id_from(r1)
        before = get_snapshot(bg_id)

        other = session.Session()
        other.project = str(tmp_path / "另一个项目")
        session.set_active(other)                 # 切前台会话 + 项目归属
        _state.current_project = str(tmp_path / "另一个项目")

        after = get_snapshot(bg_id)
        assert after["session_id"] == before["session_id"], "会话归属不变"
        assert after["project"] == before["project"], "项目锚点不变"
        assert after["cwd"] == before["cwd"], "执行目录不变"

    def test_subagent_source_recorded_not_foreground(self, bg_env, tmp_path):
        """子 Agent 创建的后台任务标记 is_subagent，归属指向子 Agent 会话。"""
        sess = session.Session()
        sess.is_subagent = True
        sess.worktree = str(tmp_path)          # 子 Agent 命令只允许在隔离区内
        session.register(sess)
        session.bind_thread(sess)
        try:
            from src.tools import _run_command
            # 子 Agent 禁止显式引用 worktree 外路径；解释器由测试 PATH 提供。
            r = _run_command('python -c "import time; time.sleep(30)"', None, True)
        finally:
            session.unbind_thread()
        snap = get_snapshot(_bg_id_from(r))
        assert snap["is_subagent"] is True, "子 Agent 来源如实标记"
        assert snap["session_id"].startswith("unsaved:")
        active = session.get_active()
        assert snap["session_id"] != (active.current_session_id
                                      or f"unsaved:{active.key}"), "不记成前台会话"

    def test_run_id_and_task_id_frozen_at_creation(self, bg_env):
        sess = session.Session()
        sess.active_run_id = "run-abc"
        sess.current_task = {"id": "task-xyz"}
        session.register(sess)
        session.bind_thread(sess)
        try:
            from src.tools import _run_command
            r = _run_command(_SLEEP_CMD, None, True)
        finally:
            session.unbind_thread()
        snap = get_snapshot(_bg_id_from(r))
        assert snap["run_id"] == "run-abc"
        assert snap["task_id"] == "task-xyz"
        # 创建后清空任务 → 旧记录不变（冻结，不跟随）
        sess.current_task = None
        assert get_snapshot(snap["bg_id"])["task_id"] == "task-xyz"


# ══════════════════════════════════════════════════════════════
# 真实状态：输出 / 退出码 / 耗时
# ══════════════════════════════════════════════════════════════

class TestRealState:

    def test_output_and_exit_code_zero(self, bg_env):
        r = run_command.func(_ECHO_CMD, background=True)
        bg_id = _bg_id_from(r)
        out = _wait_for(bg_id, "987654")
        assert "987654" in out
        assert bg_id in out and "会话" in out
        assert _wait_until_snap(bg_id, lambda s: not s["running"])
        snap = get_snapshot(bg_id)
        assert snap["exit_code"] == 0
        assert snap["end_kind"] == "natural"
        elapsed1 = snap["elapsed_s"]
        time.sleep(1.2)
        assert get_snapshot(bg_id)["elapsed_s"] == elapsed1, "结束后耗时不再增长"

    def test_nonzero_exit_honest(self, bg_env):
        r = run_command.func(_FAIL_CMD, background=True)
        bg_id = _bg_id_from(r)
        assert _wait_until_snap(bg_id, lambda s: not s["running"])
        snap = get_snapshot(bg_id)
        assert snap["exit_code"] == 3
        assert snap["end_kind"] == "natural"

    def test_read_includes_attribution(self, bg_env):
        r = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(r)
        out = read_background_output.func(bg_id)
        assert "会话" in out, "输出头部带归属会话"
        stop_background_command.func(bg_id)


# ══════════════════════════════════════════════════════════════
# 停止流程：确认退出才算停止；记录不删；重复/并发/竞态安全
# ══════════════════════════════════════════════════════════════

class TestStopFlow:

    def test_stop_confirms_and_keeps_record(self, bg_env):
        r = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(r)
        result = stop_background_command.func(bg_id)
        assert "已停止" in result and "已确认主进程退出" in result
        snap = get_snapshot(bg_id)
        assert snap is not None, "记录保留（不再先删后停）"
        assert snap["running"] is False
        assert snap["stop_requested"] is True
        assert snap["end_kind"] == "stopped"
        assert snap["exit_code"] is not None

    def test_stop_missing(self, bg_env):
        assert "未找到" in stop_background_command.func("bg_nope")

    def test_repeat_stop_after_confirmed_is_honest(self, bg_env):
        r = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(r)
        stop_background_command.func(bg_id)
        again = stop_background_command.func(bg_id)
        assert "此前停止请求后已退出" in again, "重复停止不能改写成自然退出"
        assert get_snapshot(bg_id) is not None

    def test_stop_timeout_keeps_record_and_allows_retry(self, bg_env):
        """停止超时（进程赖着不死）：如实返回、记录保留、之后可重试。"""

        class _StickyProc:
            """kill 无效、poll 一直 None；wait 超时。可切换为退出。"""
            def __init__(self):
                self.returncode = None
                self.stdout = None
                self._exited = False
                self.pid = 0

            def poll(self):
                return self.returncode if not self._exited else 0

            def wait(self, timeout=None):
                if self._exited:
                    self.returncode = 0
                    return 0
                raise subprocess.TimeoutExpired(cmd="sticky", timeout=timeout)

            def kill(self):
                pass

        sticky = _StickyProc()
        bg_id = start(_SLEEP_CMD, sticky, cwd=None, project=None,
                      session_id="s-test", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=False)
        result = stop(bg_id, wait_timeout=0.3)
        assert result["alive"] is True and result.get("confirmed") is False
        snap = get_snapshot(bg_id)
        assert snap is not None and snap["stop_requested"] is True
        assert snap["running"] is True, "赖着不死的进程不被标成已退出"

        # 之后进程退出（重试场景）：确认停止
        sticky._exited = True
        result2 = stop(bg_id, wait_timeout=1.0)
        assert result2["confirmed"] is True
        assert get_snapshot(bg_id)["end_kind"] in ("natural", "stopped")

    def test_concurrent_stops_are_safe(self, bg_env):
        """两个调用同时停止同一任务：都安全，记录一致。"""
        r = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(r)
        results = []

        def _do():
            results.append(stop_background_command.func(bg_id))

        threads = [threading.Thread(target=_do) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert all(not t.is_alive() for t in threads), "停止调用必须结束"
        snap = get_snapshot(bg_id)
        assert snap is not None and snap["running"] is False
        assert snap["stop_requested"] is True

    def test_stop_vs_natural_exit_race(self, bg_env):
        """停止与自然退出竞态：进程已自行退出时如实说明，不冒充"已停止"。"""
        r = run_command.func(_ECHO_CMD, background=True)
        bg_id = _bg_id_from(r)
        assert _wait_until_snap(bg_id, lambda s: not s["running"])
        result = stop_background_command.func(bg_id)
        assert "已自行退出" in result
        assert "已停止 [" not in result
        assert get_snapshot(bg_id)["end_kind"] == "natural"

    def test_stop_one_does_not_affect_the_other(self, bg_env):
        r1 = run_command.func(_SLEEP_CMD, background=True)
        r2 = run_command.func(_SLEEP_CMD, background=True)
        id1, id2 = _bg_id_from(r1), _bg_id_from(r2)
        stop_background_command.func(id1)
        assert get_snapshot(id1)["running"] is False
        assert get_snapshot(id2)["running"] is True, "停一个不影响另一个"
        stop_background_command.func(id2)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill /T")
    def test_windows_stop_covers_child_process(self, bg_env, tmp_path):
        """Windows 停止实际覆盖子进程树：父进程起的子进程也被杀掉，
        不只检查返回文案。"""
        child_script = tmp_path / "parent.py"
        child_script.write_text(
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print('CHILD_PID', p.pid, flush=True)\n"
            "time.sleep(60)\n", encoding="utf-8")
        r = run_command.func(
            f'"{sys.executable}" "{child_script}"', background=True)
        bg_id = _bg_id_from(r)
        out = _wait_for(bg_id, "CHILD_PID")
        match = re.search(r"CHILD_PID (\d+)", out)
        assert match, out
        # 固定句柄核实这一条真实子进程，不用 PID 字符串匹配 tasklist。
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000 | 0x0001, False, int(match.group(1)))
        assert handle, "前置：子进程确实在跑"
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 258
            result = stop_background_command.func(bg_id)
            assert "已停止" in result
            assert kernel.WaitForSingleObject(handle, 10000) == 0, "子进程也应被终止"
        finally:
            if kernel.WaitForSingleObject(handle, 0) == 258:
                kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)


# ══════════════════════════════════════════════════════════════
# 输出上限与 reader 失败
# ══════════════════════════════════════════════════════════════

class _FakeStdout:
    """可控 stdout：read() 按预置块返回，可在第 N 次后抛异常。"""
    def __init__(self, chunks, raise_after=None):
        self._chunks = list(chunks)
        self._raise_after = raise_after
        self._n = 0

    def read(self, n=-1):
        if self._raise_after is not None and self._n >= self._raise_after:
            raise OSError("pipe broken")
        if not self._chunks:
            return b""
        self._n += 1
        return self._chunks.pop(0)


class TestOutputLimits:

    def test_many_lines_bounded_by_char_cap(self, bg_env, monkeypatch):
        """大量输出：字符总量上限生效，截断事实与丢弃量如实记录。"""
        monkeypatch.setattr(limits, "BG_MAX_OUTPUT_CHARS", 500)
        line_expr = "'x' * 120 + chr(10)"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             f"import sys\nfor _ in range(20): sys.stdout.write({line_expr})\n"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        bg_id = start("flooder", proc, cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None)
        try:
            assert _wait_until_snap(bg_id, lambda s: s["output_truncated"])
            snap = get_snapshot(bg_id)
            assert snap["output_chars"] <= 500, "硬上限不为标记留额外空间"
            assert snap["output_dropped_chars"] > 0, "丢弃量如实计数"
            assert snap["output_chars"] == len("".join(snap["output_tail"]))
            assert snap["output_total_chars"] == snap["output_chars"] + snap["output_dropped_chars"]
        finally:
            proc.kill()
            proc.wait(10)

    def test_no_newline_long_output_bounded(self, bg_env):
        """无换行的超长输出也受限：reader 的临时 buf 不得无限积累。"""
        big = b"z" * (limits.BG_MAX_NO_NEWLINE_BYTES + 30_000)

        class _P:
            returncode = None
            pid = 0
            stdout = _FakeStdout([big, b""])

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

            def kill(self):
                pass

        bg_id = start("no-newline", _P(), cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            snap = get_snapshot(bg_id)
            if snap["output_complete"]:
                break
            time.sleep(0.05)
        snap = get_snapshot(bg_id)
        assert snap["output_chars"] <= limits.BG_MAX_OUTPUT_CHARS
        assert snap["output_total_lines"] >= 2, "无换行超长输出被分段入缓冲"

    def test_reader_failure_keeps_output_and_reports(self, bg_env):
        """reader 读取失败：已读内容保留 + read_error 单独报告（不吞异常）。"""
        stdout = _FakeStdout([b"first line\n", b"second "], raise_after=2)
        proc = type("P", (), {"stdout": stdout, "returncode": None, "pid": 0,
                              "poll": lambda self: None,
                              "wait": lambda self, timeout=None: (
                                  (_ for _ in ()).throw(
                                      subprocess.TimeoutExpired("x", 1)))})()
        bg_id = start("boom", proc, cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=True)
        assert _wait_until_snap(bg_id, lambda s: bool(s["read_error"]) and s["output_complete"])
        snap = get_snapshot(bg_id)
        assert "pipe broken" in snap["read_error"]
        assert any("first line" in ln
                   for ln in snap["output_tail"]), "已读到的内容保留"
        assert "second " in "".join(snap["output_tail"]), "已读半行不能随异常消失"
        assert snap["output_total_lines"] >= 1, "读取失败与无输出可区分"


# ══════════════════════════════════════════════════════════════
# 快照与并发
# ══════════════════════════════════════════════════════════════

class TestSnapshotContract:

    def test_snapshot_has_no_internal_objects(self, bg_env):
        r = run_command.func(_SLEEP_CMD, background=True)
        snap = get_snapshot(_bg_id_from(r))
        for key, value in snap.items():
            assert not hasattr(value, "poll"), f"{key} 暴露了 Popen"
            assert not isinstance(value, deque), f"{key} 暴露了 deque"
        assert "proc" not in snap and "output" not in snap
        stop_background_command.func(_bg_id_from(r))

    def test_snapshot_mutation_does_not_affect_internal(self, bg_env):
        r = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(r)
        snap = get_snapshot(bg_id)
        snap["command"] = "tampered"
        snap["output_tail"].append("tampered")
        assert get_snapshot(bg_id)["command"] != "tampered"
        assert all("tampered" not in ln
                   for ln in get_snapshot(bg_id)["output_tail"])
        stop_background_command.func(bg_id)

    def test_concurrent_snapshot_and_output(self, bg_env):
        """并发读快照与写输出不报错（reader append vs 快照 list 拷贝）。"""
        script = "import sys\nfor i in range(3000): print('line', i)"
        r = run_command.func(f'"{sys.executable}" -c "{script}"', background=True)
        bg_id = _bg_id_from(r)
        errors = []

        def _read_loop():
            for _ in range(40):
                try:
                    get_snapshot(bg_id)
                    read_background_output.func(bg_id, tail=10)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=_read_loop) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert all(not t.is_alive() for t in threads), "快照读取不能死锁"
        assert errors == []
        stop_background_command.func(bg_id)


# ══════════════════════════════════════════════════════════════
# 淘汰与退出清理
# ══════════════════════════════════════════════════════════════

class _FakeProc:
    def __init__(self, exited):
        self.returncode = 0 if exited else None
        self.stdout = None
        self.pid = 0

    def poll(self):
        return self.returncode


class TestEvictionAndCleanup:

    def test_eviction_caps_ended_keeps_running(self, bg_env):
        with background._LOCK:
            background._tasks.clear()
        try:
            for i in range(2):
                start(f"run{i}", _FakeProc(False), cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=False)
            for i in range(limits.BG_MAX_RETAINED_EXITED + 3):
                start(f"e{i}", _FakeProc(True), cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=False)
            with background._LOCK:
                running = [t for t in background._tasks.values()
                           if t.proc.poll() is None]
                ended = [t for t in background._tasks.values()
                         if t.proc.poll() is not None]
            assert len(running) == 2, "运行中的永不淘汰"
            assert len(ended) <= limits.BG_MAX_RETAINED_EXITED
        finally:
            with background._LOCK:
                background._tasks.clear()

    def test_exit_cleanup_bounded_and_honest(self, bg_env):
        """退出清理有界：赖着不死的进程如实记录、不被标成已停止。"""

        class _StickyProc:
            returncode = None
            pid = 0
            stdout = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

            def kill(self):
                pass

        bg_id = start("sticky", _StickyProc(), cwd=None, project=None,
                      session_id="s", session_key="k", is_subagent=False,
                      run_id="", task_id=None, spawn_reader=False)
        snaps = stop_all(wait_timeout=0.3)
        mine = next(s for s in snaps if s["bg_id"] == bg_id)
        assert mine["running"] is True, "仍存活的进程不被标成已停止"
        assert mine["stop_requested"] is True
        with background._LOCK:
            background._tasks.clear()

    def test_stop_all_records_exit_info(self, bg_env):
        echo_id = _bg_id_from(run_command.func(_ECHO_CMD, background=True))
        assert _wait_until_snap(echo_id, lambda s: not s["running"])
        run_command.func(_SLEEP_CMD, background=True)
        snaps = stop_all(wait_timeout=8.0)
        assert len(snaps) >= 2
        echo = next(s for s in snaps if s["bg_id"] == echo_id)
        assert echo["end_kind"] == "natural" and not echo["stop_requested"]
        assert all(s["running"] is False for s in snaps)
        assert any(s["stop_requested"] for s in snaps)
