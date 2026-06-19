"""run_tests 工具测试：pytest 输出解析（_parse_pytest_output）+ 路径逃逸防护。

核心解析是纯函数，喂假 pytest 文本验证；不嵌套真跑 pytest（端到端已手动验过）。
_resolve_python 决定用哪个解释器跑 pytest（打包 frozen 安全）。
"""
import os
import sys

from src.tools import _parse_pytest_output, run_tests, _resolve_python


class TestParsePytestOutput:
    def test_all_passed(self):
        out = _parse_pytest_output("....\n=== 5 passed in 1.20s ===")
        assert "5 passed" in out
        assert "✅" in out or "全部通过" in out

    def test_failed_with_cases(self):
        text = (
            "FAILED scripts/test_x.py::test_foo - AssertionError: assert 1 == 2\n"
            "FAILED scripts/test_y.py::test_bar - KeyError: 'k'\n"
            "=== 2 failed, 3 passed in 0.50s ==="
        )
        out = _parse_pytest_output(text)
        assert "2 failed" in out and "3 passed" in out
        assert "test_foo" in out and "test_bar" in out      # 失败用例都列出
        assert "AssertionError" in out                       # 错误摘要带上

    def test_error_counted_as_failed(self):
        out = _parse_pytest_output("=== 1 error in 0.10s ===")
        assert "1 failed" in out      # error 计入失败数

    def test_unparseable_falls_back_not_empty(self):
        out = _parse_pytest_output("collection blew up: some weird traceback xyz123")
        assert out.strip()             # 非空
        assert "xyz123" in out         # 退回原始输出尾部

    def test_summary_picks_tail_failed_block(self):
        # FAILED 块之前有别的输出，解析只取末尾连续的 FAILED 行
        text = (
            "some test progress dots ....F..\n"
            "=== FAILURES ===\n"
            "lots of traceback ...\n"
            "=== short test summary info ===\n"
            "FAILED a.py::t1 - ValueError\n"
            "=== 1 failed, 2 passed in 0.3s ==="
        )
        out = _parse_pytest_output(text)
        assert "1 failed" in out and "2 passed" in out
        assert "t1" in out

    def test_elapsed_shown_when_positive(self):
        out = _parse_pytest_output("=== 3 passed in 0.1s ===", elapsed=1.5)
        assert "1.50s" in out           # 传入耗时 → 摘要里显示（X.XXs）

    def test_elapsed_hidden_when_zero(self):
        out = _parse_pytest_output("=== 3 passed in 0.1s ===")   # 不传（默认 0）
        assert "s）" not in out          # 0 → 不显示耗时（向后兼容）


class TestRunTestsGuard:
    def test_path_escape_rejected(self, project_dir):
        # ../ 逃出项目根 → 直接拒绝，不跑 pytest
        out = run_tests.func("../")
        assert "不允许" in out or "项目范围" in out


class TestResolvePython:
    def test_dev_uses_sys_executable(self, project_dir, monkeypatch):
        # 开发期（非 frozen）、项目无 venv → 用 sys.executable
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        assert _resolve_python() == sys.executable

    def test_project_venv_preferred(self, project_dir, monkeypatch):
        # 项目内有 venv → 优先用它的 python（即便开发期）
        bindir = "Scripts" if os.name == "nt" else "bin"
        pyname = "python.exe" if os.name == "nt" else "python"
        vpy = project_dir / ".venv" / bindir
        vpy.mkdir(parents=True)
        (vpy / pyname).write_text("", encoding="utf-8")
        assert _resolve_python() == str(vpy / pyname)

    def test_frozen_skips_exe_uses_system(self, project_dir, monkeypatch):
        # 打包后 sys.executable=exe 不能 -m pytest：无项目 venv → 退到系统 python
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr("src.tools.shutil.which",
                            lambda n, *a, **k: r"C:\sys\python.exe" if n in ("python", "python3") else None)
        assert _resolve_python() == r"C:\sys\python.exe"
