"""测试 apply_patch 工具：批量文件补丁。"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.tools import apply_patch, _parse_patch


@pytest.fixture(autouse=True)
def no_auto_check(monkeypatch):
    # apply_patch 落盘后对每个文件调 _run_code_check；测试里关掉它，免得真跑 ruff 干扰断言
    monkeypatch.setattr("src.tools._run_code_check", lambda *a, **k: ("", None))


@pytest.fixture
def project_dir(tmp_path):
    # 必须设 state.current_project（apply_patch 走 _project_cwd，优先用它）——只 chdir 不够：
    # 全套里 state.current_project 会被别的测试污染，导致路径解析到错误目录。
    from src import state
    d = tmp_path / "project"
    d.mkdir()
    old_project, old_ui, old_cwd = state.current_project, state.ui_ref, os.getcwd()
    state.current_project = str(d)
    state.ui_ref = None              # 无 UI → 写文件/确认自动放行
    os.chdir(d)
    yield d
    os.chdir(old_cwd)
    state.current_project = old_project
    state.ui_ref = old_ui


# ═══════════════════════════════════════════════════════════
#  解析测试
# ═══════════════════════════════════════════════════════════
class TestParsePatch:
    """_parse_patch 的各种解析情况。"""

    def test_parse_update_single_hunk(self):
        content = (
            "*** Begin Patch\n"
            "*** Update File: src/utils.py\n"
            "@@\n"
            " import os\n"
            "-def old():\n"
            "+def new():\n"
            "     pass\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert errors == []
        assert len(ops) == 1
        op = ops[0]
        assert op["action"] == "update"
        assert op["path"] == "src/utils.py"
        assert len(op["hunks"]) == 1
        hunk = op["hunks"][0]
        assert hunk["hint"] == ""
        assert len(hunk["lines"]) == 4

    def test_parse_update_multi_hunk(self):
        content = (
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "@@\n"
            " import os\n"
            "-OLD = 1\n"
            "+NEW = 1\n"
            "@@\n"
            " import sys\n"
            "-DEBUG = False\n"
            "+DEBUG = True\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert errors == []
        assert len(ops) == 1
        assert len(ops[0]["hunks"]) == 2

    def test_parse_multi_file(self):
        content = (
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            "-x = 1\n"
            "+x = 2\n"
            "*** Add File: b.py\n"
            "+# new file\n"
            "*** Delete File: c.py\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert errors == []
        assert len(ops) == 3
        assert [op["action"] for op in ops] == ["update", "add", "delete"]

    def test_parse_add_file(self):
        content = (
            "*** Begin Patch\n"
            "*** Add File: src/new.py\n"
            "+# hello\n"
            "+x = 1\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert errors == []
        assert ops[0]["action"] == "add"
        assert ops[0]["new_lines"] == ["# hello", "x = 1"]

    def test_parse_blank_line_as_context(self):
        """空行（无空格前缀）应视为上下文行。"""
        content = (
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            " x = 1\n"
            "\n"
            " y = 2\n"
            "-z = 3\n"
            "+z = 4\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert errors == []
        hunk = ops[0]["hunks"][0]
        # 空行应被保留在 hunk lines 中
        assert "" in [l for l in hunk["lines"] if l == ""]

    def test_parse_missing_begin(self):
        content = "*** End Patch\n"
        ops, errors = _parse_patch(content)
        assert errors != []
        assert "Begin Patch" in errors[0]

    def test_parse_unknown_header(self):
        content = (
            "*** Begin Patch\n"
            "*** Move File: x.py\n"
            "*** End Patch\n"
        )
        ops, errors = _parse_patch(content)
        assert any("无法识别" in e for e in errors)


# ═══════════════════════════════════════════════════════════
#  应用测试
# ═══════════════════════════════════════════════════════════
class TestApplyPatch:
    """apply_patch 的正常应用逻辑。"""

    def test_update_single_hunk(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        file = project_dir / "src" / "utils.py"
        file.parent.mkdir(parents=True)
        file.write_text("import os\n\ndef hello():\n    pass\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: src/utils.py\n"
            "@@\n"
            "-def hello():\n"
            "+def world():\n"
            "     pass\n"
            "*** End Patch\n"
        )
        assert "1 个修改" in result
        assert file.read_text(encoding="utf-8") == "import os\n\ndef world():\n    pass\n"

    def test_update_multi_hunk(self, project_dir, monkeypatch):
        """多 hunk 更新同一文件。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        file = project_dir / "app.py"
        file.write_text("import os\nimport sys\n\nDEBUG = False\nLOG = False\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            " import os\n"
            " import sys\n"
            "+import json\n"
            "@@\n"
            "-DEBUG = False\n"
            "+DEBUG = True\n"
            " LOG = False\n"
            "*** End Patch\n"
        )
        assert "1 个修改" in result
        content = file.read_text(encoding="utf-8")
        assert "import json" in content
        assert "DEBUG = True" in content

    def test_update_multi_file(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        a = project_dir / "a.py"
        b = project_dir / "b.py"
        a.write_text("x = 1\n", encoding="utf-8")
        b.write_text("y = 2\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            "-x = 1\n"
            "+x = 10\n"
            "*** Update File: b.py\n"
            "@@\n"
            "-y = 2\n"
            "+y = 20\n"
            "*** End Patch\n"
        )
        assert "2 个修改" in result
        assert a.read_text(encoding="utf-8") == "x = 10\n"
        assert b.read_text(encoding="utf-8") == "y = 20\n"

    def test_add_file(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Add File: src/new_module.py\n"
            "+# New module\n"
            "+class Foo:\n"
            "+    pass\n"
            "*** End Patch\n"
        )
        assert "1 个新增" in result
        content = (project_dir / "src" / "new_module.py").read_text(encoding="utf-8")
        assert content == "# New module\nclass Foo:\n    pass\n"

    def test_delete_file(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        file = project_dir / "old.py"
        file.write_text("old content\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Delete File: old.py\n"
            "*** End Patch\n"
        )
        assert "1 个删除" in result
        assert not file.exists()

    def test_update_add_delete_combo(self, project_dir, monkeypatch):
        """同时修改、新增、删除文件。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        old = project_dir / "old.txt"
        old.write_text("old\n", encoding="utf-8")
        mod = project_dir / "mod.txt"
        mod.write_text("before\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: mod.txt\n"
            "@@\n"
            "-before\n"
            "+after\n"
            "*** Add File: new.txt\n"
            "+brand new\n"
            "*** Delete File: old.txt\n"
            "*** End Patch\n"
        )
        assert "1 个新增" in result
        assert "1 个修改" in result
        assert "1 个删除" in result
        assert mod.read_text(encoding="utf-8") == "after\n"
        assert (project_dir / "new.txt").read_text(encoding="utf-8") == "brand new\n"
        assert not old.exists()

    def test_noop_patch(self, project_dir, monkeypatch):
        """只有 Begin/End → 成功但 0 个变化。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))
        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** End Patch\n"
        )
        assert "为空" in result


# ═══════════════════════════════════════════════════════════
#  原子性测试
# ═══════════════════════════════════════════════════════════
class TestAtomicity:
    """一个文件出错 → 整个 patch 不写入任何文件。"""

    def test_hunk_match_failure_aborts_all(self, project_dir, monkeypatch):
        """hunk 匹配失败 → 0 文件被修改。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        a = project_dir / "a.py"
        b = project_dir / "b.py"
        a.write_text("x = 1\n", encoding="utf-8")
        b.write_text("y = 2\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            "-x = 999\n"
            "+x = 100\n"
            "*** Update File: b.py\n"
            "@@\n"
            "-y = 2\n"
            "+y = 20\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result
        assert a.read_text(encoding="utf-8") == "x = 1\n"  # 未被修改
        assert b.read_text(encoding="utf-8") == "y = 2\n"  # 未被修改

    def test_add_existing_file_aborts_all(self, project_dir, monkeypatch):
        """新增已存在文件 → 整个 patch 中止。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        existing = project_dir / "existing.py"
        existing.write_text("old\n", encoding="utf-8")
        other = project_dir / "other.py"
        other.write_text("keep\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Add File: existing.py\n"
            "+new content\n"
            "*** Update File: other.py\n"
            "@@\n"
            "-keep\n"
            "+changed\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result
        assert existing.read_text(encoding="utf-8") == "old\n"
        assert other.read_text(encoding="utf-8") == "keep\n"

    def test_delete_nonexistent_aborts_all(self, project_dir, monkeypatch):
        """删除不存在的文件 → 整个 patch 中止。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        real = project_dir / "real.py"
        real.write_text("alive\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Delete File: ghost.py\n"
            "*** Update File: real.py\n"
            "@@\n"
            "-alive\n"
            "+dead\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result
        assert real.read_text(encoding="utf-8") == "alive\n"


# ═══════════════════════════════════════════════════════════
#  安全性测试
# ═══════════════════════════════════════════════════════════
class TestSecurity:
    """路径逃逸防护。"""

    def test_rejects_path_traversal_update(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: ../escape.py\n"
            "@@\n"
            "-dummy\n"
            "+hacked\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result
        assert "超出" in result

    def test_rejects_path_traversal_add(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Add File: ../../etc/evil\n"
            "+malicious\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result

    def test_rejects_path_traversal_delete(self, project_dir, monkeypatch):
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Delete File: ../../../important.txt\n"
            "*** End Patch\n"
        )
        assert "Patch 校验失败" in result


# ═══════════════════════════════════════════════════════════
#  边界测试
# ═══════════════════════════════════════════════════════════
class TestEdgeCases:
    """边界情况。"""

    def test_empty_content_patch(self):
        result = apply_patch.func("")
        assert "Patch 格式错误" in result

    def test_context_only_hunk(self, project_dir, monkeypatch):
        """只有上下文行、没有增删的 hunk → 无变化。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        f = project_dir / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            " x = 1\n"
            "*** End Patch\n"
        )
        # 没有实际变化 → 空 patch
        assert "为空" in result

    def test_subdirectory_file(self, project_dir, monkeypatch):
        """操作子目录中的文件（自动创建目录）。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (True, None))

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Add File: deep/nested/dir/file.py\n"
            "+# deep file\n"
            "*** End Patch\n"
        )
        assert "1 个新增" in result
        assert (project_dir / "deep" / "nested" / "dir" / "file.py").exists()

    def test_user_reject_patch(self, project_dir, monkeypatch):
        """用户拒绝 → 不写入文件。"""
        monkeypatch.setattr("src.tools._confirm_file_write", lambda *a: (False, "用户拒绝了此次文件写入操作。"))

        f = project_dir / "a.py"
        f.write_text("original\n", encoding="utf-8")

        result = apply_patch.func(
            "*** Begin Patch\n"
            "*** Update File: a.py\n"
            "@@\n"
            "-original\n"
            "+changed\n"
            "*** End Patch\n"
        )
        assert "拒绝" in result
        assert f.read_text(encoding="utf-8") == "original\n"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
