"""补齐工具层的高频边界回归测试。"""
from types import SimpleNamespace

from src import state
from src.tools import (
    append_file,
    forget,
    read_file,
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


class TestWriteTools:
    def test_write_file_creates_parent_directories(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._checkpoint.make_checkpoint", lambda *args: None)

        result = write_file.func("nested/new.txt", "hello")

        assert "成功写入" in result
        assert (project_dir / "nested" / "new.txt").read_text(encoding="utf-8") == "hello"

    def test_append_file_appends_content(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._checkpoint.make_checkpoint", lambda *args: None)
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

        assert "src/main.py:1:TODO: python" in result
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


class TestMemoryToolWrappers:
    def test_remember_and_forget_roundtrip(self, isolated_memory):
        assert "已记住" in remember.func("用户偏好 pytest")
        assert "无需重复保存" in remember.func("用户偏好 pytest")

        result = forget.func("pytest")

        assert "已删除 1 条记忆" in result
        assert "用户偏好 pytest" in result

    def test_forget_reports_no_matches(self, isolated_memory):
        assert "未找到" in forget.func("不存在")
