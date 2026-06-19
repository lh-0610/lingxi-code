"""后台命令测试：run_command(background=True) + 注册表 + 3 个管理工具 + 退出清理。

用真实短命令起进程（不 mock），fixture 保证每个测试后 stop_all_background 清理，
不残留。state.ui_ref=None 时 run_command 免确认直接执行。
"""
import re
import time

import pytest

from src import state
from src.tools import (
    run_command, read_background_output, list_background_commands,
    stop_background_command, stop_all_background, _new_bg_id, _bg_procs,
)

# 一个会持续运行的命令（够测"运行中"/stop），和一个立即打印后退出的命令
_SLEEP_CMD = 'python -c "import time; time.sleep(10)"'


def _bg_id_from(result: str) -> str:
    m = re.search(r"\[(bg\d+)\]", result)
    assert m, f"返回里没有 bg_id: {result!r}"
    return m.group(1)


def _wait_for(bg_id: str, marker: str, timeout: float = 6.0) -> str:
    """轮询 read_background_output 直到含 marker 或超时（reader 是异步线程）。"""
    deadline = time.time() + timeout
    out = ""
    while time.time() < deadline:
        out = read_background_output.func(bg_id)
        if marker in out:
            return out
        time.sleep(0.1)
    return out


@pytest.fixture()
def bg_env():
    """无 UI（免确认）+ 测试后杀光所有后台进程。"""
    old_ui = state.ui_ref
    state.ui_ref = None
    yield
    stop_all_background()
    state.ui_ref = old_ui


class TestNewBgId:
    def test_unique_and_format(self):
        ids = [_new_bg_id() for _ in range(5)]
        assert len(set(ids)) == 5                       # 全唯一
        assert all(re.fullmatch(r"bg\d+", i) for i in ids)


class _FakeProc:
    """假进程：poll() 返回 None=运行中 / 整数=已退出。"""
    def __init__(self, exited: bool):
        self.returncode = 0 if exited else None
    def poll(self):
        return self.returncode


class TestBgEviction:
    """_evict_old_exited_bg：已退出项有界淘汰，运行中的永不动。"""

    def test_evicts_oldest_exited_keeps_running(self):
        from src.tools import _evict_old_exited_bg, _bg_lock, _bg_procs
        from src.limits import BG_MAX_RETAINED_EXITED
        with _bg_lock:
            _bg_procs.clear()
            # 2 个运行中（最老）+ 超过上限的已退出
            _bg_procs["run_a"] = {"proc": _FakeProc(False), "command": "a", "output": [], "start_ts": 1.0}
            _bg_procs["run_b"] = {"proc": _FakeProc(False), "command": "b", "output": [], "start_ts": 2.0}
            for i in range(BG_MAX_RETAINED_EXITED + 3):
                _bg_procs[f"exit_{i}"] = {"proc": _FakeProc(True), "command": f"e{i}",
                                          "output": [], "start_ts": 100.0 + i}
            _evict_old_exited_bg()
            keys = set(_bg_procs.keys())
        try:
            # 运行中的两个必须都在
            assert "run_a" in keys and "run_b" in keys
            # 已退出的被裁到上限
            exited_left = [k for k in keys if k.startswith("exit_")]
            assert len(exited_left) == BG_MAX_RETAINED_EXITED
            # 被淘汰的是最老的（exit_0/1/2），最新的保留
            assert "exit_0" not in keys
            assert f"exit_{BG_MAX_RETAINED_EXITED + 2}" in keys
        finally:
            with _bg_lock:
                _bg_procs.clear()

    def test_noop_when_under_limit(self):
        from src.tools import _evict_old_exited_bg, _bg_lock, _bg_procs
        with _bg_lock:
            _bg_procs.clear()
            _bg_procs["e1"] = {"proc": _FakeProc(True), "command": "x", "output": [], "start_ts": 1.0}
            _evict_old_exited_bg()
            n = len(_bg_procs)
            _bg_procs.clear()
        assert n == 1


class TestBackgroundLifecycle:
    def test_start_returns_bg_id_and_registers(self, bg_env):
        result = run_command.func(_SLEEP_CMD, background=True)
        assert "已后台启动" in result
        bg_id = _bg_id_from(result)
        assert bg_id in _bg_procs

    def test_read_output_captures_stdout(self, bg_env):
        result = run_command.func('python -c "print(987654)"', background=True)
        bg_id = _bg_id_from(result)
        out = _wait_for(bg_id, "987654")
        assert "987654" in out
        assert bg_id in out                             # 头部带 [bgN] 状态行

    def test_running_status(self, bg_env):
        result = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(result)
        assert "运行中" in read_background_output.func(bg_id)

    def test_list_shows_command(self, bg_env):
        result = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(result)
        listing = list_background_commands.func()
        assert bg_id in listing
        assert "运行中" in listing

    def test_stop_removes_from_registry(self, bg_env):
        result = run_command.func(_SLEEP_CMD, background=True)
        bg_id = _bg_id_from(result)
        assert bg_id in _bg_procs
        stop_result = stop_background_command.func(bg_id)
        assert "已停止" in stop_result
        assert bg_id not in _bg_procs

    def test_stop_all_clears_registry(self, bg_env):
        run_command.func(_SLEEP_CMD, background=True)
        run_command.func(_SLEEP_CMD, background=True)
        assert len(_bg_procs) >= 2
        stop_all_background()
        assert len(_bg_procs) == 0

    def test_list_empty_when_none(self, bg_env):
        assert "没有后台命令" in list_background_commands.func()

    def test_read_nonexistent(self, bg_env):
        assert "未找到" in read_background_output.func("bg_nope")

    def test_stop_nonexistent(self, bg_env):
        assert "未找到" in stop_background_command.func("bg_nope")
