"""mcp_client 测试：stdio 命令的路径解析（_resolve_stdio_command）。

纯函数，只依赖 os + 模块级 CONFIG_PATH。monkeypatch CONFIG_PATH 到一个假目录后断言：
- 绝对路径原样返回
- 裸命令名（npx / uvx / python）不动 —— 交给 PATH 查找
- 相对路径（以 . 开头 / 含分隔符 / .. 上跳）相对 config.json 所在目录解析为绝对路径
- ~ 和 $VAR 先展开再分类

注：mcp_client 顶层不 import mcp（懒导入在各函数内），所以无需装 mcp 包即可测这个函数。
"""
import os

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
