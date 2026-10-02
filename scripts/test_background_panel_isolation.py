"""真实 pytest 子进程验证：模拟进程的 teardown 不能调用系统 taskkill。"""
import os
from pathlib import Path
import subprocess
import sys


def test_fake_process_teardown_never_launches_taskkill(tmp_path):
    # Popen 保持类形态，兼容 Windows asyncio 的 Popen 子类；只拦截系统终止命令。
    # 即便旧 fixture 把调用异常吞掉，最终仍由 attempts 捕获，且不会真杀进程。
    code = r'''
import subprocess
from pathlib import Path
import sys
attempts = []
class GuardedPopen(subprocess.Popen):
    def __init__(self, command, *args, **kwargs):
        pieces = [str(x) for x in command] if isinstance(command, (list, tuple)) else str(command).split()
        if Path(pieces[0]).name.lower() in {"taskkill", "taskkill.exe"}:
            attempts.append(pieces)
            raise RuntimeError("Synthetic-process test must not invoke taskkill")
        super().__init__(command, *args, **kwargs)
subprocess.Popen = GuardedPopen
import pytest
result = pytest.main([
    "scripts/test_background_panel.py::test_hide_close_and_reopen_only_change_refresh",
    "-q", "--basetemp", sys.argv[1],
])
assert not attempts, f"System termination was attempted during teardown: {attempts}"
raise SystemExit(result)
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "child-basetemp")],
        cwd=Path(__file__).resolve().parent.parent,
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
