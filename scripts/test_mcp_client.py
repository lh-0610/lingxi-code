"""mcp_client 测试：stdio 命令的路径解析（_resolve_stdio_command）。

纯函数，只依赖 os + 模块级 CONFIG_PATH。monkeypatch CONFIG_PATH 到一个假目录后断言：
- 绝对路径原样返回
- 裸命令名（npx / uvx / python）不动 —— 交给 PATH 查找
- 相对路径（以 . 开头 / 含分隔符 / .. 上跳）相对 config.json 所在目录解析为绝对路径
- ~ 和 $VAR 先展开再分类

注：mcp_client 顶层不 import mcp（懒导入在各函数内），所以无需装 mcp 包即可测这个函数。
"""
import os
import sys

import pytest

from src import mcp_client


# config.json 假装放在 /fake/cfgdir/ 下（平台相关的绝对路径）
CFG_DIR = os.path.join(os.path.abspath(os.sep), "fake", "cfgdir")
FAKE_CONFIG = os.path.join(CFG_DIR, "config.json")


@pytest.fixture()
def fake_config(monkeypatch):
    """把 mcp_client 模块级 CONFIG_PATH 指到固定假路径，让相对解析可断言。"""
    monkeypatch.setattr(mcp_client, "CONFIG_PATH", FAKE_CONFIG)
    return FAKE_CONFIG


def _expected_rel(rel):
    """函数对相对路径的预期结果：相对 config 目录拼接后取 abspath。"""
    return os.path.abspath(os.path.join(CFG_DIR, rel))


class TestBareCommandUnchanged:
    def test_plain_names(self, fake_config):
        for cmd in ("npx", "uvx", "python", "node", "docker"):
            assert mcp_client._resolve_stdio_command(cmd) == cmd

    def test_dotless_word_is_path_command(self, fake_config):
        # "server" 没分隔符也不以 . 开头 → 当成 PATH 上的命令，不解析成路径
        assert mcp_client._resolve_stdio_command("server") == "server"


class TestAbsoluteKept:
    def test_absolute_returned_asis(self, fake_config):
        abspath = os.path.join(os.path.abspath(os.sep), "usr", "local", "bin", "mcp-fs")
        result = mcp_client._resolve_stdio_command(abspath)
        assert result == abspath
        assert os.path.isabs(result)


class TestRelativeResolvedToConfigDir:
    def test_leading_dot_slash(self, fake_config):
        rel = os.path.join(".", "servers", "fs.py")
        result = mcp_client._resolve_stdio_command(rel)
        assert result == _expected_rel(rel)
        assert os.path.isabs(result)

    def test_contains_separator_without_leading_dot(self, fake_config):
        # 含分隔符但不以 . 开头，仍应解析成相对 config 目录的绝对路径
        rel = os.path.join("servers", "fs.py")
        result = mcp_client._resolve_stdio_command(rel)
        assert result == _expected_rel(rel)
        assert os.path.isabs(result)

    def test_parent_dir(self, fake_config):
        rel = os.path.join("..", "shared", "server.js")
        result = mcp_client._resolve_stdio_command(rel)
        assert result == _expected_rel(rel)
        assert os.path.isabs(result)
        # .. 应跳出 cfgdir（落到 /fake/shared）
        assert "cfgdir" not in os.path.normpath(result).split(os.sep)


class TestExpansion:
    def test_expandvars_to_bare_command(self, fake_config, monkeypatch):
        # $VAR 展开成裸命令 → 仍当 PATH 命令，不动
        monkeypatch.setenv("MCP_TEST_CMD", "npx")
        assert mcp_client._resolve_stdio_command("$MCP_TEST_CMD") == "npx"

    def test_expandvars_to_absolute(self, fake_config, monkeypatch):
        absdir = os.path.join(os.path.abspath(os.sep), "opt", "tools")
        monkeypatch.setenv("MCP_TOOLS_DIR", absdir)
        result = mcp_client._resolve_stdio_command(os.path.join("$MCP_TOOLS_DIR", "srv"))
        assert result == os.path.join(absdir, "srv")
        assert os.path.isabs(result)

    def test_expanduser_tilde_is_absolute(self, fake_config):
        rel = os.path.join("~", "some", "srv")
        expanded = os.path.expanduser(rel)
        if expanded == rel:
            pytest.skip("无可展开的 home 目录")
        result = mcp_client._resolve_stdio_command(rel)
        # ~ 展开后通常是绝对路径 → 原样返回（不再相对 config 解析）
        assert result == os.path.expandvars(expanded)


# ── 远程工具包装（mcp 1.x 驼峰 / 2.x 下划线字段都要认）──

class _OldStyleTool:
    """mcp 1.x 的 Tool：字段是 inputSchema。"""

    def __init__(self, name, schema):
        self.name, self.description, self.inputSchema = name, f"old {name}", schema


@pytest.fixture()
def server_tools(monkeypatch):
    registry = {}
    monkeypatch.setattr(mcp_client, "_server_tools", registry)
    monkeypatch.setattr(mcp_client, "MCP_DISPLAY_NAMES", {})
    return registry


def _args(tool):
    return set(tool.args_schema.model_fields)


def test_wraps_tools_from_the_installed_mcp_and_the_old_field_name(server_tools):
    """mcp 2.x 把 inputSchema 改成了 input_schema：只认旧名字时整个 MCP 守护线程会崩，一个工具都注册不上。"""
    from mcp.types import Tool
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    server_tools["docs"] = [Tool(name="search-docs", description="搜文档", inputSchema=schema)]
    server_tools["legacy"] = [_OldStyleTool("read", schema)]
    tools = {t.name: t for t in mcp_client._build_mcp_tools()}
    assert set(tools) == {"mcp_docs_search_docs", "mcp_legacy_read"}
    assert _args(tools["mcp_docs_search_docs"]) == {"query"} == _args(tools["mcp_legacy_read"])


def test_list_typed_parameters(server_tools):
    """sequential-thinking 的参数类型写成 ["integer", "null"]：拿列表查表会抛 unhashable。"""
    schema = {"type": "object", "properties": {"revisesThought": {"type": ["integer", "null"]},
                                               "thought": {"type": "string"}},
              "required": ["thought"]}
    server_tools["seq"] = [_OldStyleTool("think", schema)]
    (tool,) = mcp_client._build_mcp_tools()
    model = tool.args_schema
    assert model(thought="x", revisesThought=None).revisesThought is None
    assert model(thought="x", revisesThought=2).revisesThought == 2


def test_one_broken_tool_does_not_take_the_others_down(server_tools):
    class Broken:
        name = "broken"
        description = ""

        @property
        def input_schema(self):
            raise RuntimeError("schema 读不出来")

    schema = {"type": "object", "properties": {}}
    server_tools["srv"] = [Broken(), _OldStyleTool("fine", schema)]
    assert [t.name for t in mcp_client._build_mcp_tools()] == ["mcp_srv_fine"]


def test_tool_errors_are_reported_as_errors(server_tools, monkeypatch):
    """工具自己报错（is_error）时要标明是错：不标的话模型会把报错文字当成正常结果。"""
    import asyncio
    import threading
    from types import SimpleNamespace

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class FakeSession:
        def __init__(self, failing):
            self.failing = failing

        async def call_tool(self, name, arguments=None):
            return SimpleNamespace(content=[SimpleNamespace(text="找不到这个文档")], is_error=self.failing)

    try:
        monkeypatch.setattr(mcp_client, "_mcp_loop", loop)
        server_tools["docs"] = [_OldStyleTool("get", {"type": "object", "properties": {}})]
        (tool,) = mcp_client._build_mcp_tools()
        monkeypatch.setattr(mcp_client, "_sessions", {"docs": FakeSession(True)})
        assert tool.invoke({}) == "MCP 工具报错：找不到这个文档"
        monkeypatch.setattr(mcp_client, "_sessions", {"docs": FakeSession(False)})
        assert tool.invoke({}) == "找不到这个文档"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_missing_start_command_is_caught_before_spawning(tmp_path):
    env = dict(os.environ)
    assert not mcp_client._command_exists(str(tmp_path / ".venv" / "Scripts" / "uvx.exe"), env)
    assert mcp_client._command_exists(sys.executable, env)
    assert not mcp_client._command_exists("surely-not-a-real-command-xyz", env)
