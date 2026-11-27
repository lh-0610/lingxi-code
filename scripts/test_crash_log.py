"""崩溃堆栈记录（src/crash_log.py）：真崩溃留下所有线程的 Python 堆栈；正常退出不留文件；
被接住的访问违例注明「不是崩溃」；上次强杀留下的空文件下次启动清掉。

崩溃要在子进程里造（faulthandler._sigsegv），本进程不受影响。
"""
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = textwrap.dedent("""
    import sys, threading, time
    sys.path.insert(0, {root!r})
    from src import crash_log
    crash_log.enable({d!r})

    def lingxi_worker_marker():
        time.sleep(30)

    threading.Thread(target=lingxi_worker_marker, daemon=True).start()
    time.sleep(0.2)
    mode = {mode!r}
    if mode == "crash":
        import faulthandler
        faulthandler._sigsegv()
    elif mode == "handled_av":
        import ctypes
        try:
            ctypes.string_at(0)
        except OSError:
            pass
""")


def _child(tmp_path, mode):
    code = CHILD.format(root=ROOT, d=str(tmp_path), mode=mode)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    files = sorted(p for p in tmp_path.iterdir() if p.name.startswith("crash_"))
    return r, files


def test_a_crash_leaves_every_threads_python_stack(tmp_path):
    r, files = _child(tmp_path, "crash")
    assert r.returncode != 0
    assert len(files) == 1, files
    text = files[0].read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error" in text, text
    assert "lingxi_worker_marker" in text, "所有线程都要记（崩在主线程时，后台线程在干什么也要看得到）"
    assert "Current thread" in text


def test_a_normal_exit_leaves_no_file(tmp_path):
    r, files = _child(tmp_path, "normal")
    assert r.returncode == 0, r.stderr
    assert files == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 上 faulthandler 先于异常处理触发")
def test_a_handled_access_violation_is_marked_not_a_crash(tmp_path):
    r, files = _child(tmp_path, "handled_av")
    assert r.returncode == 0, r.stderr
    assert len(files) == 1, files
    text = files[0].read_text(encoding="utf-8", errors="replace")
    assert "access violation" in text and "不是崩溃" in text, text


def test_old_empty_files_are_cleared_but_real_crashes_kept(tmp_path):
    (tmp_path / "crash_20260101_000000_1.log").write_text("", encoding="utf-8")
    (tmp_path / "crash_20260101_000000_2.log").write_text("Fatal Python error", encoding="utf-8")
    r, files = _child(tmp_path, "normal")
    assert r.returncode == 0, r.stderr
    assert [p.name for p in files] == ["crash_20260101_000000_2.log"]


def test_an_unusable_log_dir_does_not_break_startup(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    code = (f"import sys; sys.path.insert(0, {ROOT!r}); from src import crash_log; "
            f"print(crash_log.enable({str(blocker)!r}))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and r.stdout.strip() == "None", (r.stdout, r.stderr)
