"""run_command 的三条硬约束回归测试（来自对照 DeepSeek Harness 的 defensive-patterns）：

1. 超时/中断/退出码/已产生的输出是**互相独立**的事实，必须各报各的——早先实现把输出
   的读取放在超时分支之后，超时时模型一个字都拿不到（用户在 UI 上看得见，只有模型瞎）。
2. 子进程不该继承宿主的密钥类环境变量（命令串是模型拼的）。
3. 退出清理要等进程真的停下，不能只发 kill 就返回（否则端口还占着）。
"""
import time

import pytest

from src import state
from src.tools import (
    run_command, stop_all_background, _bg_procs, _bg_lock, _scrubbed_env,
)


@pytest.fixture(autouse=True)
def _no_ui():
    """ui_ref=None → run_command 免确认直接执行。"""
    old = getattr(state, "ui_ref", None)
    state.ui_ref = None
    yield
    state.ui_ref = old


class TestOrthogonalOutcomes:
    def test_timeout_still_returns_produced_output(self):
        """超时被杀，但超时前已经打出来的东西必须带回去——那正是最有诊断价值的部分。"""
        cmd = ('python -c "import sys,time; print(\'PARTIAL_MARKER\'); '
               'sys.stdout.flush(); time.sleep(30)"')
        out = run_command.func(cmd, timeout=2)
        assert "超时" in out
        assert "PARTIAL_MARKER" in out, f"超时分支把已产生的输出丢了: {out!r}"

    def test_interrupt_still_returns_produced_output(self, monkeypatch):
        """用户中断同理：中断前的输出不能丢。"""
        import threading

        def _set_stop_soon():
            time.sleep(1.5)
            state.stop_flag = True
        state.stop_flag = False
        t = threading.Thread(target=_set_stop_soon, daemon=True)
        t.start()
        try:
            cmd = ('python -c "import sys,time; print(\'BEFORE_STOP\'); '
                   'sys.stdout.flush(); time.sleep(30)"')
            out = run_command.func(cmd, timeout=60)
            assert "中断" in out
            assert "BEFORE_STOP" in out, f"中断分支把已产生的输出丢了: {out!r}"
        finally:
            state.stop_flag = False
            t.join(timeout=5)

    def test_normal_exit_reports_code_and_output(self):
        out = run_command.func('python -c "print(\'HELLO\')"', timeout=30)
        assert "退出码: 0" in out and "HELLO" in out

    def test_nonzero_exit_still_returns_output(self):
        """非零退出码不能吞掉输出（诊断信息通常就在里面）。"""
        out = run_command.func(
            'python -c "import sys; print(\'FAILED_DETAIL\'); sys.exit(3)"', timeout=30)
        assert "退出码: 3" in out and "FAILED_DETAIL" in out


class TestEnvScrub:
    def test_secret_like_names_dropped(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "sk-should-not-leak")
        monkeypatch.setenv("SOME_SECRET", "s3cr3t")
        monkeypatch.setenv("GH_TOKEN", "ghp_xxx")
        monkeypatch.setenv("DB_PASSWORD", "hunter2")
        monkeypatch.setenv("PLAIN_VAR", "keep-me")
        env = _scrubbed_env()
        assert env is not None
        for name in ("MY_API_KEY", "SOME_SECRET", "GH_TOKEN", "DB_PASSWORD"):
            assert name not in env, f"{name} 不该传给子进程"
        assert env.get("PLAIN_VAR") == "keep-me"      # 无关变量原样保留

    def test_keep_list_allows_precise_passthrough(self, monkeypatch):
        """需要密钥的命令（aws / npm publish）靠精确放行，而不是整个关掉脱敏。"""
        from src import config as _cfg
        monkeypatch.setenv("NEEDED_TOKEN", "yes")
        monkeypatch.setenv("OTHER_TOKEN", "no")
        monkeypatch.setattr(_cfg, "RUN_COMMAND_ENV_KEEP", ["needed_token"])  # 大小写不敏感
        env = _scrubbed_env()
        assert env.get("NEEDED_TOKEN") == "yes"
        assert "OTHER_TOKEN" not in env

    def test_disabled_returns_none(self, monkeypatch):
        """关掉脱敏 → 返回 None，调用方不传 env，行为与脱敏前完全一致。"""
        from src import config as _cfg
        monkeypatch.setattr(_cfg, "RUN_COMMAND_SCRUB_ENV", False)
        assert _scrubbed_env() is None

    def test_subprocess_really_cannot_see_secret(self, monkeypatch):
        """端到端：子进程里读不到密钥变量。"""
        monkeypatch.setenv("LINGXI_TEST_API_KEY", "sk-leak-canary")
        out = run_command.func(
            'python -c "import os; print(\'VAL=\' + os.environ.get(\'LINGXI_TEST_API_KEY\',\'<absent>\'))"',
            timeout=30)
        assert "VAL=<absent>" in out, f"密钥泄漏到子进程: {out!r}"
        assert "sk-leak-canary" not in out


class TestShutdownQuiescence:
    def test_stop_all_background_waits_for_exit(self):
        """退出清理返回时进程必须真的没了——只发 kill 不等，端口可能还占着。"""
        res = run_command.func('python -c "import time; time.sleep(30)"', background=True)
        assert "bg" in res
        with _bg_lock:
            procs = [info["proc"] for info in _bg_procs.values()]
        assert procs, "后台进程没注册上"
        stop_all_background()
        for p in procs:
            assert p.poll() is not None, "stop_all_background 返回时进程仍在运行"
        assert not _bg_procs

    def test_stop_all_background_is_safe_when_empty(self):
        stop_all_background()          # 不该抛
        assert not _bg_procs
