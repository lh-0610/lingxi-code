"""补齐工具层的高频边界回归测试。"""
from types import SimpleNamespace

from src import state
from src.tools import (
    append_file,
    forget,
    read_file,
    read_files,
    remember,
    search_files,
    write_file,
)


class TestReadFile:
    def test_relative_path_uses_project_root_and_paginates(self, project_dir):
        (project_dir / "notes.txt").write_text("a\nb\nc\n", encoding="utf-8")

        result = read_file.func("notes.txt", offset=2, limit=1)

        assert "2: b" in result
        assert "1 行未读" in result
        assert "offset=3" in result

    def test_clamps_invalid_offset_and_limit(self, project_dir):
        (project_dir / "notes.txt").write_text("a\nb\n", encoding="utf-8")

        result = read_file.func("notes.txt", offset=0, limit=0)

        assert "1: a" in result
        assert "2: b" not in result

    def test_empty_file(self, project_dir):
        (project_dir / "empty.txt").write_text("", encoding="utf-8")

        assert read_file.func("empty.txt") == "（空文件）"

    def test_paths_reads_several_files_in_one_call(self, project_dir):
        (project_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
        (project_dir / "b.py").write_text("y = 2\n", encoding="utf-8")

        result = read_files.func(["a.py", "b.py", "missing.py", "a.py"])

        assert "### a.py" in result and "1: x = 1" in result
        assert "### b.py" in result and "1: y = 2" in result
        assert "### missing.py" in result and "读取失败" in result    # 读不到的照样报出来
        assert result.count("### a.py") == 1                            # 重复的只读一次

    def test_paths_caps_file_count_and_says_what_was_skipped(self, project_dir):
        from src import tools
        for i in range(tools._READ_MANY_MAX_FILES + 2):
            (project_dir / f"f{i}.txt").write_text("z\n", encoding="utf-8")

        result = read_files.func([f"f{i}.txt" for i in range(tools._READ_MANY_MAX_FILES + 2)])

        assert result.count("### ") == tools._READ_MANY_MAX_FILES
        assert f"f{tools._READ_MANY_MAX_FILES}.txt" in result and "没读" in result

    def test_paths_stops_reading_at_char_budget(self, project_dir, monkeypatch):
        from src import tools
        monkeypatch.setattr(tools, "_READ_MANY_MAX_CHARS", 50)
        (project_dir / "big.txt").write_text("line\n" * 40, encoding="utf-8")
        (project_dir / "next.txt").write_text("tail\n", encoding="utf-8")

        result = read_files.func(["big.txt", "next.txt"])

        assert "### next.txt" in result and "没读" in result and "tail" not in result

    def test_run_command_description_names_the_shell(self):
        """模型调用 run_command 时看的是工具说明：shell 和工作目录要写在这里。"""
        import os
        from src.tools import run_command
        assert ("cmd.exe" if os.name == "nt" else "/bin/sh") in run_command.description
        assert "不用每条都先 cd" in run_command.description
        if os.name == "nt":
            assert 'set "X=1"' in run_command.description      # cmd 的 set 会把 && 前的空格算进值里

    def test_run_tests_runs_qt_offscreen(self, project_dir, monkeypatch):
        """测试子进程默认离屏：别在用户桌面上弹窗口或 Qt 错误框（用户已经设了就不覆盖）。"""
        import subprocess
        from src import tools
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, stdout="1 passed in 0.01s", stderr="")

        monkeypatch.setattr(tools.subprocess, "run", fake_run)
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        tools.run_tests.func()
        assert seen.get("QT_QPA_PLATFORM") == "offscreen"

        monkeypatch.setenv("QT_QPA_PLATFORM", "windows")
        tools.run_tests.func()
        assert seen.get("QT_QPA_PLATFORM") == "windows"


class TestWriteTools:
    def test_write_file_creates_parent_directories(self, project_dir, monkeypatch):
        result = write_file.func("nested/new.txt", "hello")

        assert "成功写入" in result
        assert (project_dir / "nested" / "new.txt").read_text(encoding="utf-8") == "hello"

    def test_append_file_appends_content(self, project_dir, monkeypatch):
        path = project_dir / "notes.txt"
        path.write_text("first\n", encoding="utf-8")

        result = append_file.func("notes.txt", "second\n")

        assert "成功追加" in result
        assert path.read_text(encoding="utf-8") == "first\nsecond\n"

    def test_write_file_rejection_keeps_original_content(self, project_dir):
        path = project_dir / "notes.txt"
        path.write_text("before", encoding="utf-8")
        old_ui = state.ui_ref
        state.ui_ref = SimpleNamespace(confirm_edit=lambda *_: (False, "先别改"))
        try:
            result = write_file.func("notes.txt", "after")
        finally:
            state.ui_ref = old_ui

        assert "已拒绝" in result
        assert "先别改" in result
        assert path.read_text(encoding="utf-8") == "before"


class TestSearchFiles:
    def test_filters_patterns_and_ignored_directories(self, project_dir):
        (project_dir / "src").mkdir()
        (project_dir / "src" / "main.py").write_text("TODO: python\n", encoding="utf-8")
        (project_dir / "src" / "main.ts").write_text("TODO: typescript\n", encoding="utf-8")
        (project_dir / "node_modules").mkdir()
        (project_dir / "node_modules" / "ignored.py").write_text("TODO: ignore\n", encoding="utf-8")

        result = search_files.func("TODO", path=".", file_pattern="*.py")

        assert "src/main.py（1 处）" in result and "1: TODO: python" in result
        assert "main.ts" not in result
        assert "ignored.py" not in result

    def test_supports_brace_expansion_and_truncation(self, project_dir):
        (project_dir / "a.ts").write_text("hit\nhit\n", encoding="utf-8")
        (project_dir / "b.tsx").write_text("hit\n", encoding="utf-8")

        result = search_files.func("hit", file_pattern="*.{ts,tsx}", max_results=2)

        assert "找到 3 处匹配" in result
        assert "仅显示前 2 处" in result
        assert "还有 1 处未列出" in result

    def test_rejects_invalid_regex(self, project_dir):
        assert "正则不合法" in search_files.func("[", path=".")

    def test_groups_by_file_with_context_lines(self, project_dir):
        """每处匹配带前后几行（上下文行用 `-`、匹配行用 `:`），相邻的合成一段——多数时候不用再 read_file。"""
        (project_dir / "a.py").write_text("one\ntwo\nhit\nfour\nfive\nsix\nseven\nhit\nnine\n", encoding="utf-8")

        result = search_files.func("hit", path=".", file_pattern="*.py")

        assert "a.py（2 处）" in result and "分布在 1 个文件" in result
        for line in ("    1- one", "    3: hit", "    5- five", "    6- six", "    8: hit", "    9- nine"):
            assert line in result
        assert "    4- four" in result and result.count("four") == 1     # 两段上下文不重复列

    def test_many_hits_drop_context(self, project_dir):
        from src.limits import SEARCH_FILES_CONTEXT_MAX_HITS
        (project_dir / "many.py").write_text("ctx\nhit\n" * (SEARCH_FILES_CONTEXT_MAX_HITS + 1), encoding="utf-8")

        result = search_files.func("hit", path=".", file_pattern="*.py")

        assert "只列匹配行" in result and "- ctx" not in result

    def test_context_zero_lists_only_matching_lines(self, project_dir):
        (project_dir / "a.py").write_text("before\nhit\nafter\n", encoding="utf-8")

        result = search_files.func("hit", path=".", file_pattern="*.py", context=0)

        assert "2: hit" in result and "before" not in result and "只列匹配行" not in result


class TestMemoryToolWrappers:
    def test_remember_and_forget_roundtrip(self, isolated_memory):
        assert "已记住" in remember.func("用户偏好 pytest")
        assert "无需重复保存" in remember.func("用户偏好 pytest")

        result = forget.func("pytest")

        assert "已删除 1 条记忆" in result
        assert "用户偏好 pytest" in result

    def test_forget_reports_no_matches(self, isolated_memory):
        assert "未找到" in forget.func("不存在")
