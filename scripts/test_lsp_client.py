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
        """关掉 LSP（_find_server→None、清语言组单例表、conv 函数→None）并屏蔽 jedi 导入。

        屏蔽 jedi 用 sys.modules[jedi]=None：Python 3.12+ 已移除老式 meta_path
        find_module 钩子，让模块名映射到 None 才能稳定地令 `import jedi` 抛 ImportError。
        monkeypatch 在测试结束自动还原 sys.modules 和 _servers。
        """
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: None)
        monkeypatch.setattr(lsp_mod, "_servers", {})
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
        monkeypatch.setattr(lsp_mod, "_servers", {})

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


# ── 多语言：扩展名→语言组 / languageId ────────────────────
class TestLanguageRouting:
    """按文件扩展名选语言组和 didOpen 的 languageId（不写死 python）。"""

    @pytest.mark.parametrize("path,group", [
        ("a.py", "python"), ("a.pyw", "python"),
        ("a.ts", "typescript"), ("a.tsx", "typescript"), ("a.mts", "typescript"),
        ("a.js", "typescript"), ("a.jsx", "typescript"), ("a.mjs", "typescript"),
        ("a.go", "go"), ("a.rs", "rust"),
        ("a.c", "cpp"), ("a.h", "cpp"), ("a.cpp", "cpp"), ("a.hpp", "cpp"),
        ("A.java", "java"),
        ("a.txt", None), ("a.md", None), ("noext", None),
    ])
    def test_group_for_path(self, path, group):
        import src.lsp_client as lsp_mod
        assert lsp_mod.language_group_for_path(path) == group

    @pytest.mark.parametrize("path,lang_id", [
        ("a.py", "python"),
        ("a.ts", "typescript"), ("a.tsx", "typescriptreact"),
        ("a.js", "javascript"), ("a.jsx", "javascriptreact"),
        ("a.go", "go"), ("a.rs", "rust"),
        ("a.c", "c"), ("a.h", "cpp"), ("a.cpp", "cpp"),
        ("A.java", "java"),
        ("a.unknown", "plaintext"),
    ])
    def test_language_id_for_path(self, path, lang_id):
        import src.lsp_client as lsp_mod
        assert lsp_mod._language_id_for_path(path) == lang_id

    def test_group_case_insensitive(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod.language_group_for_path("A.PY") == "python"
        assert lsp_mod.language_group_for_path("A.TSX") == "typescript"


# ── 多语言：启动参数 ──────────────────────────────────────
class TestMakeArgs:
    """裸命令补该组默认 stdio 参数；带参数的原样切；jdtls 补 -data 工作区。"""

    def test_pylsp_no_stdio(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod._make_args("pylsp", "python", "/p") == ["pylsp"]

    def test_pyright_stdio(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod._make_args("pyright-langserver", "python", "/p") == [
            "pyright-langserver", "--stdio"]

    def test_typescript_stdio(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod._make_args("typescript-language-server", "typescript", "/p") == [
            "typescript-language-server", "--stdio"]

    def test_gopls_bare(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod._make_args("gopls", "go", "/p") == ["gopls"]

    def test_jdtls_data_workspace(self):
        import src.lsp_client as lsp_mod
        args = lsp_mod._make_args("jdtls", "java", "/proj")
        assert args[0] == "jdtls"
        assert "-data" in args
        assert args[args.index("-data") + 1]

    def test_jdtls_workspace_is_outside_project_root(self, tmp_path, isolated_memory):
        """jdtls 的 -data 工作区不能落在项目根里：否则 git_diff 把它当未跟踪文件，
        平白多出一块"未审阅的改动"，完成闸门永远收不了（#10）。"""
        import src.lsp_client as lsp_mod
        from src.tools_common import _path_inside
        proj = tmp_path / "proj"
        proj.mkdir()
        args = lsp_mod._make_args("jdtls", "java", str(proj))
        data = args[args.index("-data") + 1]
        assert not _path_inside(data, str(proj)), "jdtls 工作区不能落在项目根里"

    def test_jdtls_workspace_distinguished_by_project_root(self, tmp_path, isolated_memory):
        """不同项目根 → 不同工作区；同一项目根稳定（索引缓存可跨次复用）。"""
        import src.lsp_client as lsp_mod
        p1 = tmp_path / "p1"
        p2 = tmp_path / "p2"
        p1.mkdir()
        p2.mkdir()
        d1 = lsp_mod._make_args("jdtls", "java", str(p1))[2]
        d2 = lsp_mod._make_args("jdtls", "java", str(p2))[2]
        assert d1 != d2
        assert lsp_mod._make_args("jdtls", "java", str(p1))[2] == d1

    def test_custom_multipart_preserved(self):
        import src.lsp_client as lsp_mod
        assert lsp_mod._make_args("my-server --foo --bar", "go", "/p") == [
            "my-server", "--foo", "--bar"]


# ── 多语言：config 覆盖候选命令 ───────────────────────────
class TestCandidateCommands:
    def test_builtin_default(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "LSP_LANGUAGES", {})
        assert lsp_mod._candidate_commands("go") == ["gopls"]

    def test_override_string(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "LSP_LANGUAGES", {"go": "my-gopls --stdio"})
        assert lsp_mod._candidate_commands("go") == ["my-gopls --stdio"]

    def test_override_list(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "LSP_LANGUAGES", {"rust": ["ra-a", "ra-b"]})
        assert lsp_mod._candidate_commands("rust") == ["ra-a", "ra-b"]

    def test_override_bad_type_ignored(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "LSP_LANGUAGES", {"go": 123})
        assert lsp_mod._candidate_commands("go") == ["gopls"]


# ── 多语言：get_server 按组独立缓存 + 无服务器降级 ─────────
class _FakeServer:
    """假的 _LspServer：不启动子进程，只记录 group / root，供缓存测试。"""
    instances: list = []

    def __init__(self, cmd, group="python"):
        self.cmd = cmd
        self.group = group
        import src.lsp_client as lsp_mod
        self.root = str(lsp_mod._get_project_root())
        self.running = True
        self._initialized = True
        self.shutdown_called = False

        class _P:
            pid = 4321
        self._proc = _P()
        _FakeServer.instances.append(self)

    def shutdown(self):
        self.shutdown_called = True
        self.running = False


class TestGetServerPerGroup:
    def test_no_server_for_group_returns_none(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_servers", {})
        monkeypatch.setattr(lsp_mod, "_find_server_for", lambda g: None)
        assert lsp_mod.get_server("go") is None

    def test_python_uses_find_server(self, monkeypatch):
        """python 组走无参 _find_server（历史打桩点），不走 _find_server_for。"""
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_servers", {})
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: None)
        called = {"for": False}

        def _for(g):
            called["for"] = True
            return None
        monkeypatch.setattr(lsp_mod, "_find_server_for", _for)
        assert lsp_mod.get_server("python") is None
        assert called["for"] is False

    def test_separate_instance_per_group(self, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_servers", {})
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: "pyright-langserver")
        monkeypatch.setattr(lsp_mod, "_find_server_for", lambda g: f"{g}-server")
        monkeypatch.setattr(lsp_mod, "_LspServer", _FakeServer)
        _FakeServer.instances = []

        py = lsp_mod.get_server("python")
        go = lsp_mod.get_server("go")
        assert py is not None and go is not None
        assert py is not go
        assert py.group == "python" and go.group == "go"
        # 再次取同组 → 命中缓存，不新建
        assert lsp_mod.get_server("go") is go
        assert len(_FakeServer.instances) == 2

    def test_shutdown_clears_all_groups(self, monkeypatch):
        import src.lsp_client as lsp_mod
        fake = {}
        monkeypatch.setattr(lsp_mod, "_servers", fake)
        monkeypatch.setattr(lsp_mod, "_find_server", lambda: "pyright-langserver")
        monkeypatch.setattr(lsp_mod, "_find_server_for", lambda g: f"{g}-server")
        monkeypatch.setattr(lsp_mod, "_LspServer", _FakeServer)
        _FakeServer.instances = []

        lsp_mod.get_server("python")
        lsp_mod.get_server("go")
        assert len(fake) == 2
        lsp_mod.shutdown()
        assert fake == {}
        assert all(s.shutdown_called for s in _FakeServer.instances)


# ── 多语言：非 Python 文件导航降级（不硬用 jedi）──────────
class TestNonPythonFallback:
    """指定非 .py 文件、对应语言服务器未装 → 诚实提示装服务器 / 用 search_files，不抛错。"""

    def test_definition_ts_hint(self, project_dir, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_servers", {})
        monkeypatch.setattr(lsp_mod, "_find_server_for", lambda g: None)
        (project_dir / "a.ts").write_text(
            "function greet() { return 1; }\ngreet();\n", encoding="utf-8")
        from src.tools import find_definition
        result = find_definition.func(name="greet", path="a.ts")
        assert "不是 Python 文件" in result
        assert "search_files" in result

    def test_references_go_hint(self, project_dir, monkeypatch):
        import src.lsp_client as lsp_mod
        monkeypatch.setattr(lsp_mod, "_servers", {})
        monkeypatch.setattr(lsp_mod, "_find_server_for", lambda g: None)
        (project_dir / "a.go").write_text(
            "package main\nfunc greet() int { return 1 }\nfunc main() { greet() }\n",
            encoding="utf-8")
        from src.tools import find_references
        result = find_references.func(name="greet", path="a.go")
        assert "不是 Python 文件" in result
        assert "search_files" in result

    def test_module_definition_unknown_ext_none(self, project_dir):
        """module.definition 对认不出语言组的扩展名直接返回 None（不去起服务器）。"""
        import src.lsp_client as lsp_mod
        (project_dir / "a.txt").write_text("hello\n", encoding="utf-8")
        assert lsp_mod.definition("hello", "a.txt", 1) is None
        assert lsp_mod.references("hello", "a.txt", 1) is None


# ── Windows：npm 装的 .cmd 垫片必须解析成完整路径才起得来 ──────────
# 最小 LSP 应答器：回应 initialize / shutdown，收到 exit 退出，其余通知忽略。
_MINIMAL_LSP = r'''
import sys, json

def read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        s = line.decode("ascii", "replace").strip()
        if s == "":
            break
        if ":" in s:
            k, v = s.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    n = int(headers.get("content-length", 0))
    return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))

def send(msg):
    data = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(("Content-Length: %d\r\n\r\n" % len(data)).encode("ascii") + data)
    sys.stdout.buffer.flush()

while True:
    m = read_msg()
    if m is None:
        break
    method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {}}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
    elif method == "exit":
        break
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows .cmd 垫片启动问题只在 Windows 复现")
class TestWindowsCmdShim:
    """npm 装的语言服务器在 Windows 上是 .cmd 垫片：CreateProcess 不查 PATHEXT，
    裸名字 Popen 会 WinError 2。argv[0] 必须用 shutil.which 解析成完整路径才起得来。
    真实启动一个最小 LSP 服务器（.cmd 垫片包着 Python 应答器）验证，不打桩 Popen。"""

    def test_cmd_shim_server_starts_and_handshakes(self, tmp_path, monkeypatch):
        from src import lsp_client, state
        responder = tmp_path / "lsp_responder.py"
        responder.write_text(_MINIMAL_LSP, encoding="utf-8")
        shim_dir = tmp_path / "bin"
        shim_dir.mkdir()
        shim = shim_dir / "fake-lsp-shim.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{responder}"\r\n', encoding="utf-8")
        monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ["PATH"])
        monkeypatch.setattr(state, "current_project", str(tmp_path))
        assert shutil.which("fake-lsp-shim"), "垫片应当能被 which 按 PATHEXT 找到（.cmd）"

        srv = lsp_client._LspServer("fake-lsp-shim", group="python")
        try:
            assert srv.running, "裸名字起不来：argv[0] 必须解析成 .cmd 完整路径"
            assert srv._initialized, "initialize 握手应当完成"
        finally:
            srv.shutdown()
