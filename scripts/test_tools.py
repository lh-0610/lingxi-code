"""工具层单元测试（非 UI 依赖部分）

覆盖：search_in_file / list_directory / run_command / edit_file 部分逻辑
注意：edit_file 的核心定位逻辑已有 test_edit_robustness.py 覆盖，
此处只补 edge-case 和其他工具。
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.tools import (
    _realign_indent,
)


class TestProjectInstructions:
    def test_loads_root_to_target_and_excludes_sibling(self, project_dir):
        (project_dir / "AGENTS.md").write_text("root-rule", encoding="utf-8")
        frontend = project_dir / "frontend"
        backend = project_dir / "backend"
        frontend.mkdir()
        backend.mkdir()
        (frontend / "CLAUDE.md").write_text("frontend-rule", encoding="utf-8")
        (backend / "CLAUDE.md").write_text("backend-rule", encoding="utf-8")
        target = frontend / "page.py"
        target.write_text("", encoding="utf-8")

        from src.tools import get_project_instructions
        result = get_project_instructions.func("frontend/page.py")

        assert "root-rule" in result
        assert "frontend-rule" in result
        assert "backend-rule" not in result
        assert "frontend/CLAUDE.md" in result

    def test_rejects_path_outside_current_project(self, project_dir, tmp_path):
        outside = tmp_path / "outside.py"
        outside.write_text("", encoding="utf-8")

        from src.tools import get_project_instructions
        result = get_project_instructions.func(str(outside))

        assert "不在当前项目根目录" in result


# ── search_in_file ──────────────────────────────────────
class TestSearchInFile:
    """纯文件操作，不依赖 state。"""

    def test_basic_match(self, project_dir):
        f = project_dir / "data.txt"
        f.write_text("aaa\nbbb\nccc\nbbb\n", encoding="utf-8")
        from src.tools import search_in_file

        result = search_in_file.func(str(f), "bbb")
        assert "2 处匹配" in result
        assert "2:" in result
        assert "4:" in result

    def test_no_match(self, project_dir):
        f = project_dir / "data.txt"
        f.write_text("aaa\nbbb\n", encoding="utf-8")
        from src.tools import search_in_file

        result = search_in_file.func(str(f), "zzz")
        assert "未找到" in result

    def test_file_not_found(self, project_dir):
        from src.tools import search_in_file

        result = search_in_file.func(str(project_dir / "nope.txt"), "x")
        assert "失败" in result or "不存在" in result

    def test_directory_path_hints_search_files(self, project_dir):
        """传目录（而非文件）→ 友好提示改用 search_files，不抛 Errno 13。"""
        from src.tools import search_in_file

        result = search_in_file.func(str(project_dir), "x")
        assert "目录" in result and "search_files" in result

    def test_pagination(self, project_dir):
        f = project_dir / "lines.txt"
        f.write_text("\n".join(f"line {i}" for i in range(50)) + "\n", encoding="utf-8")
        from src.tools import search_in_file

        r1 = search_in_file.func(str(f), "line", offset=0, limit=10)
        assert "处匹配" in r1
        assert "1-10" in r1
        assert "还有" in r1
        r2 = search_in_file.func(str(f), "line", offset=10, limit=10)
        assert "处匹配" in r2
        assert "11-20" in r2


# ── list_directory ──────────────────────────────────────
class TestListDirectory:
    def test_basic_listing(self, project_dir):
        (project_dir / "a.py").write_text("x", encoding="utf-8")
        (project_dir / "subdir").mkdir()
        (project_dir / "subdir" / "b.py").write_text("y", encoding="utf-8")
        from src.tools import list_directory

        result = list_directory.func(str(project_dir))
        assert "a.py" in result
        assert "subdir" in result

    def test_not_a_directory(self, project_dir):
        f = project_dir / "file.txt"
        f.write_text("x", encoding="utf-8")
        from src.tools import list_directory

        result = list_directory.func(str(f))
        assert any(k in result for k in ("不存在", "不是目录", "错误", "失败", "无效"))

    def test_dirs_before_files(self, project_dir):
        """文件夹排在文件前面（--group-directories-first 风格）"""
        # 创建文件和目录，故意用会干扰字母序的名字
        (project_dir / "z_file.txt").write_text("z", encoding="utf-8")
        (project_dir / "a_file.txt").write_text("a", encoding="utf-8")
        (project_dir / "m_dir").mkdir()
        (project_dir / "b_dir").mkdir()
        from src.tools import list_directory

        result = list_directory.func(str(project_dir))
        lines = [l for l in result.splitlines() if l.startswith(("📁", "📄"))]
        dir_indices = [i for i, l in enumerate(lines) if l.startswith("📁")]
        file_indices = [i for i, l in enumerate(lines) if l.startswith("📄")]
        assert dir_indices, "应包含目录"
        assert file_indices, "应包含文件"
        assert max(dir_indices) < min(file_indices), \
            f"所有目录应排在文件之前，实际目录行{dir_indices}，文件行{file_indices}"
        # 各自内部也应按字母序
        dir_names = [l.split("📁 ", 1)[1].rstrip("/") for l in lines if l.startswith("📁")]
        file_names = [l.split("📄 ", 1)[1].split("  (")[0] for l in lines if l.startswith("📄")]
        assert dir_names == sorted(dir_names), f"目录未按字母序: {dir_names}"
        assert file_names == sorted(file_names), f"文件未按字母序: {file_names}"


# ── run_command ──────────────────────────────────────────
class TestRunCommand:
    def test_basic_echo(self):
        from src.tools import run_command

        result = run_command.func('echo hello')
        assert "hello" in result.lower() or "hello" in result

    def test_command_python(self):
        """run_command 有 30s 超时，这里只测短命令不会超时。"""
        from src.tools import run_command

        result = run_command.func(f'"{sys.executable}" -c "print(1+1)"')
        assert "2" in result


# ── edit_file 辅助函数 ──────────────────────────────────
class TestEditHelpers:
    def test_realign_indent(self):
        # model 输出 4 空格缩进，文件用 tab
        result = _realign_indent(
            "    def foo():\n        return 1",  # new 用 4 空格
            "\t",                                   # file unit = tab
            "    ",                                 # model unit = 4 spaces
        )
        # 应该把 4 空格 → tab
        assert "\t" in result
        assert "return 1" in result


# ── edit_file 完整调用（无 UI） ────────────────────────
class TestEditFileIntegration:
    """测试 edit_file 的完整流程，state.ui_ref=None 免确认。"""

    def test_basic_edit(self, project_dir, sample_py_file):
        from src.tools import edit_file

        result = edit_file.func(
            str(sample_py_file),
            'print("hello world")',
            'print("hello edited")',
        )
        assert "成功" in result or "✅" in result
        content = sample_py_file.read_text(encoding="utf-8")
        assert "hello edited" in content
        assert "hello world" not in content

    def test_replace_all(self, project_dir):
        f = project_dir / "repeat.txt"
        f.write_text("aaa bbb aaa bbb aaa", encoding="utf-8")
        from src.tools import edit_file

        result = edit_file.func(str(f), "aaa", "xxx", replace_all=True)
        assert "成功" in result or "✅" in result
        content = f.read_text(encoding="utf-8")
        assert content == "xxx bbb xxx bbb xxx"

    def test_l2_match_does_not_glue_next_line(self, project_dir):
        """L2(去行尾空白)匹配后不应吞掉文件末匹配行的换行，把下一行黏上来。"""
        f = project_dir / "glue.py"
        # 第二行带尾随空格 → old(无尾空格) L1 精确匹配失败、落到 L2
        f.write_text("def f():\n    x = 1   \n    return x\ndef g():\n    pass\n", encoding="utf-8")
        from src.tools import edit_file

        result = edit_file.func(
            str(f),
            "def f():\n    x = 1\n    return x",   # 无尾随空格、无尾换行
            "def f():\n    x = 2\n    return x",
        )
        assert "成功" in result or "✅" in result
        content = f.read_text(encoding="utf-8")
        assert "x = 2" in content
        # 关键：return x 与 def g() 仍各自成行，没被黏成 "return xdef g():"
        assert "    return x\ndef g():" in content
        assert "return xdef g" not in content

    def test_old_string_not_found(self, project_dir, sample_py_file):
        from src.tools import edit_file

        result = edit_file.func(
            str(sample_py_file),
            "this_does_not_exist_in_file",
            "replacement",
        )
        assert "失败" in result or "未找到" in result or "未命中" in result


# ── 独立运行 ────────────────────────────────────────────
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
