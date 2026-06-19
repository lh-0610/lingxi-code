"""LSP 客户端测试

覆盖 spec 要求的 3 组场景：
- 没装语言服务器：find_definition / find_references 返回含"未安装"提示、不抛错
- 装了语言服务器：临时项目中 find_definition 指向 def 行、find_references 含两处
- 向后兼容：工具正确注册到 ALL_TOOLS / TOOL_DISPLAY_NAMES / PLAN_MODE_READONLY_TOOLS
"""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── 跳过条件（仅用于需要真实 LSP 服务器的类） ────────────
_HAS_SERVER = shutil.which("pyright-langserver") is not None or shutil.which("pylsp") is not None
_needs_server = pytest.mark.skipif(
    not _HAS_SERVER,
    reason="未安装 pyright-langserver / pylsp，LSP 真实调用测试需跳过",
)


# ── 注册检查（不需要 LSP 服务器也能跑） ─────────────────
class TestRegistration:
    """确认两个工具正确注册。"""

    def test_all_tools_contains(self):
        from src.tools import ALL_TOOLS
        tool_names = [t.name if hasattr(t, "name") else str(t) for t in ALL_TOOLS]
        assert "find_definition" in tool_names
        assert "find_references" in tool_names

    def test_display_names(self):
        from src.tools import TOOL_DISPLAY_NAMES
        assert "find_definition" in TOOL_DISPLAY_NAMES
        assert "find_references" in TOOL_DISPLAY_NAMES

    def test_in_readonly_tools(self):
        from src.streaming import PLAN_MODE_READONLY_TOOLS
        assert "find_definition" in PLAN_MODE_READONLY_TOOLS
        assert "find_references" in PLAN_MODE_READONLY_TOOLS


# ── find_definition（需 LSP 服务器） ────────────────────
@_needs_server
class TestFindDefinitionLSP:
    """LSP 服务器已安装时，验证 find_definition 能跳转到 def 行。"""

    def test_definition_found(self, project_dir):
        """临时项目中 find_definition 指向 def foo 那行。"""
        src = project_dir / "sample.py"
        src.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "foo()\n",
            encoding="utf-8",
        )
        from src.tools import find_definition

        # path=相对路径, line=4 (foo() 所在行，1基), symbol="foo"
        result = find_definition.func(name="foo", path="sample.py", line=4)
        # 应指向 def foo 那行（第1行）
        assert "1" in result  # 行号
        assert "sample.py" in result or "sample" in result
        assert "未安装" not in result

    def test_definition_not_found(self, project_dir):
        """符号不存在时返回友好提示。"""
        src = project_dir / "sample.py"
        src.write_text("x = 1\n", encoding="utf-8")
        from src.tools import find_definition

        result = find_definition.func(name="nonexistent_xyz", path="sample.py", line=1)
        assert "未找到" in result or "找不到" in result or "无结果" in result


# ── find_references（需 LSP 服务器） ────────────────────
@_needs_server
class TestFindReferencesLSP:
    """LSP 服务器已安装时，验证 find_references 能找到定义+调用。"""

    def test_references_found(self, project_dir):
        """find_references 应返回 def foo 那行 + foo() 调用那行。"""
        src = project_dir / "sample.py"
        src.write_text(
            "def foo():\n"
            "    return 1\n"
            "\n"
            "foo()\n",
            encoding="utf-8",
        )
        from src.tools import find_references

        # line=1 (def foo), symbol="foo"
        result = find_references.func(name="foo", path="sample.py", line=1)
        # 应该至少有两个位置
        assert result.count("\n") >= 1  # 至少2行
        assert "sample.py" in result


# ── 无服务器降级（mock 无服务器场景） ────────────────────
class TestNoServerFallback:
    """模拟无 LSP 服务器 + 无 jedi 时，工具返回'未安装'提示、不抛错。"""

    @staticmethod
    def _disable_lsp_and_jedi(lsp_mod, monkeypatch, conv_attr):
        """关掉 LSP（_find_server→None、清单例、conv 函数→None）并屏蔽 jedi 导入。

        屏蔽 jedi 用 sys.modules[jedi]=None：Python 3.12+ 已移除老式 meta_path
        find_module 钩子，让模块名映射到 None 才能稳定地令 `import jedi` 抛 ImportError。
        monkeypatch 在测试结束自动还原 sys.modules。
        """
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: None)
        with lsp_mod._server_lock:
            lsp_mod._server = None
        monkeypatch.setattr(lsp_mod, conv_attr, lambda *a, **kw: None)
        monkeypatch.setitem(sys.modules, "jedi", None)

    def test_definition_no_server(self, project_dir, monkeypatch):
        """无服务器 + 无 jedi 时 find_definition 返回'未安装'提示。"""
        src = project_dir / "sample.py"
        src.write_text("def foo():\n    return 1\n", encoding="utf-8")

        import src.lsp_client as lsp_mod
        self._disable_lsp_and_jedi(lsp_mod, monkeypatch, "definition")

        from src.tools import find_definition
        result = find_definition.func(name="foo", path="sample.py", line=1)
        assert "未安装" in result

    def test_references_no_server(self, project_dir, monkeypatch):
        """无服务器 + 无 jedi 时 find_references 返回'未安装'提示。"""
        src = project_dir / "sample.py"
        src.write_text("def foo():\n    return 1\nfoo()\n", encoding="utf-8")

        import src.lsp_client as lsp_mod
        self._disable_lsp_and_jedi(lsp_mod, monkeypatch, "references")

        from src.tools import find_references
        result = find_references.func(name="foo", path="sample.py", line=1)
        assert "未安装" in result


# ── lsp_client 模块级测试 ──────────────────────────────
class TestLspClientModule:
    """lsp_client 模块本身的基础功能。"""

    def test_get_server_no_server(self, monkeypatch):
        """无服务器时 get_server 返回 None。"""
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: None)
        with lsp_mod._server_lock:
            lsp_mod._server = None

        result = lsp_mod.get_server()
        assert result is None

    def test_shutdown_idempotent(self):
        """shutdown 多次调用不报错。"""
        import src.lsp_client as lsp_mod
        lsp_mod.shutdown()
        lsp_mod.shutdown()  # 不应抛异常

    def test_uri_to_rel_roundtrip(self, project_dir):
        """_ls_uri_to_rel 把 file:/// URI 转回相对路径。"""
        import src.lsp_client as lsp_mod
        import src.state as state
        state.current_project = str(project_dir)
        test_file = project_dir / "test.py"
        test_file.write_text("x=1\n", encoding="utf-8")
        uri = test_file.as_uri()
        result = lsp_mod._ls_uri_to_rel(uri)
        assert result == "test.py"
