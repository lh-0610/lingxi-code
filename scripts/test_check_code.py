"""check_code / 自我校验闭环测试。

语法错误用 py_compile 也能抓（无需 ruff），所以主用例不依赖 ruff；
ruff 专属的 F 检查（未定义名等"语法对但有 bug"）单独 skipif。
_auto_check_suffix 读 config 的开关，用 monkeypatch 控制。
"""
import os
import shutil
import sys

import pytest

from src import config
from src.tools import check_code, _auto_check_suffix, write_file, _bundled_ruff


class TestCheckCode:
    def test_syntax_error_caught(self, project_dir):
        (project_dir / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")
        assert "检查发现问题" in check_code.func("bad.py")

    def test_clean_file_passes(self, project_dir):
        (project_dir / "good.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        assert "通过" in check_code.func("good.py")

    def test_path_escape_rejected(self, project_dir):
        assert "不允许" in check_code.func("../x.py")

    def test_missing_path(self, project_dir):
        assert "指定" in check_code.func("")

    def test_nonexistent_file(self, project_dir):
        assert "不存在" in check_code.func("nope.py")

    def test_unsupported_language_without_command(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        (project_dir / "a.xyz").write_text("x", encoding="utf-8")
        assert "没有可用的检查器" in check_code.func("a.xyz")

    def test_custom_check_command(self, project_dir, monkeypatch):
        # 非 Python 文件 + 自定义命令：用 py 起一个一定报错的命令验证它真被调用
        monkeypatch.setattr(
            config, "CHECK_COMMAND",
            'python -c "import sys; sys.exit(1)"',
        )
        (project_dir / "x.rs").write_text("fn main(){}", encoding="utf-8")
        r = check_code.func("x.rs")
        assert "check_command" in r and "问题" in r

    def test_py_compile_inprocess_fallback(self, project_dir, monkeypatch):
        # 强制无 ruff（模拟打包后 sys.executable=exe 且系统无 ruff 二进制）→ 走内置 compile() 进程内查语法。
        # 这条进程内兜底正是打包(frozen)后唯一可靠的语法检查路径，必须覆盖。
        import importlib.util as _u
        monkeypatch.setattr(_u, "find_spec", lambda name, *a, **k: None)
        monkeypatch.setattr("src.tools.shutil.which", lambda *a, **k: None)
        (project_dir / "syn.py").write_text("def g(:\n    pass\n", encoding="utf-8")
        r = check_code.func("syn.py")
        assert "py_compile" in r and "SyntaxError" in r


class TestAutoCheckSuffix:
    def test_suffix_on_buggy_when_enabled(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", True)
        p = project_dir / "bad.py"
        p.write_text("def f(:\n", encoding="utf-8")
        s = _auto_check_suffix(str(p))
        assert "自动校验" in s

    def test_no_suffix_when_clean(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", True)
        p = project_dir / "good.py"
        p.write_text("x = 1\n", encoding="utf-8")
        assert _auto_check_suffix(str(p)) == ""

    def test_disabled_returns_empty(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", False)
        p = project_dir / "bad.py"
        p.write_text("def f(:\n", encoding="utf-8")
        assert _auto_check_suffix(str(p)) == ""

    def test_write_file_appends_auto_check(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", True)
        monkeypatch.setattr("src.tools._checkpoint.make_checkpoint", lambda *a: None)
        r = write_file.func("bad.py", "def f(:\n")
        assert "成功写入" in r and "自动校验" in r


import importlib.util as _ilu
_HAS_MYPY = _ilu.find_spec("mypy") is not None


@pytest.mark.skipif(not _HAS_MYPY, reason="mypy 未安装")
class TestTypeCheck:
    """mypy 类型检查:抓 ruff 抓不到的"参数/签名错"(MiMo 臆造 API 的典型)。"""

    def test_call_arg_caught(self, project_dir):
        # 参数数量错——ruff 抓不到,mypy 的 call-arg 能抓
        (project_dir / "t.py").write_text(
            "def f(a, b):\n    return a + b\n\nf(1, 2, 3)\n", encoding="utf-8")
        r = check_code.func("t.py")
        assert "检查发现问题" in r
        assert "call-arg" in r or "arguments" in r.lower()

    def test_correct_call_passes(self, project_dir):
        (project_dir / "t2.py").write_text(
            "def f(a, b):\n    return a + b\n\nf(1, 2)\n", encoding="utf-8")
        assert "通过" in check_code.func("t2.py")

    def test_toggle_off_skips_type_check(self, project_dir, monkeypatch):
        # 关掉类型检查后,只有 call-arg 错误(ruff 不报)的文件应通过
        monkeypatch.setattr(config, "TYPE_CHECK_AFTER_EDIT", False)
        (project_dir / "t3.py").write_text(
            "def f(a, b):\n    return a + b\n\nf(1, 2, 3)\n", encoding="utf-8")
        assert "通过" in check_code.func("t3.py")

    def test_dynamic_attr_not_noisy(self, project_dir):
        # attr-defined 已排除:对"动态属性"不该误报(否则模型追噪声)
        (project_dir / "t4.py").write_text(
            "import types\n"
            "m = types.SimpleNamespace()\n"
            "print(m.whatever_dynamic_attr)\n", encoding="utf-8")
        # 不应因 attr-defined 报"检查发现问题"(ruff 也不报这个)
        assert "通过" in check_code.func("t4.py")


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff 未安装")
class TestRuffSpecific:
    def test_undefined_name_caught(self, project_dir):
        # 语法没错但用了未定义名 → 只有 ruff(F821) 能抓，py_compile 抓不到
        (project_dir / "u.py").write_text("y = undefined_name_xyz\n", encoding="utf-8")
        assert "检查发现问题" in check_code.func("u.py")


class TestBundledRuff:
    def test_none_in_dev(self):
        # 开发期（非 frozen、无 _MEIPASS）→ 找不到随包 ruff
        assert _bundled_ruff() is None

    def test_found_in_meipass(self, monkeypatch, tmp_path):
        # 模拟打包：_MEIPASS 下放个 ruff(.exe) → _bundled_ruff 应命中
        name = "ruff.exe" if os.name == "nt" else "ruff"
        fake = tmp_path / name
        fake.write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert _bundled_ruff() == str(fake)
