"""远程遥控安全分级拦截测试（streaming._execute_tool）：
chat_only 禁所有工具 / safe_readonly 只放只读 + 敏感文件黑名单。遥控安全底线，必测。

_execute_tool 拦截在执行工具之前 early-return，append 一条拒绝 ToolMessage 到 chat_history。
"""
from unittest.mock import MagicMock

from langchain_core.messages import ToolMessage

import src.streaming as streaming
from src import state, config


def _exec(tc):
    streaming._execute_tool(tc, MagicMock())
    return state.chat_history[-1] if state.chat_history else None


class TestRemoteSafety:
    def test_chat_only_blocks_run_command(self, monkeypatch, clean_state):
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "chat_only")
        last = _exec({"name": "run_command", "args": {"command": "ls"}, "id": "1"})
        assert isinstance(last, ToolMessage)
        assert "拒绝" in last.content and "chat_only" in last.content

    def test_chat_only_blocks_read_file(self, monkeypatch, clean_state):
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "chat_only")
        last = _exec({"name": "read_file", "args": {"path": "x.txt"}, "id": "1"})
        assert isinstance(last, ToolMessage) and "拒绝" in last.content

    def test_safe_readonly_blocks_write_tool(self, monkeypatch, clean_state):
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "safe_readonly")
        last = _exec({"name": "run_command", "args": {"command": "ls"}, "id": "1"})
        assert isinstance(last, ToolMessage)
        assert "拒绝" in last.content and "safe_readonly" in last.content

    def test_safe_readonly_blocks_sensitive_file(self, monkeypatch, clean_state):
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "safe_readonly")
        # read_file 是只读工具，但 config.json 在敏感黑名单 → 仍拦
        last = _exec({"name": "read_file", "args": {"path": "config.json"}, "id": "1"})
        assert isinstance(last, ToolMessage) and "拒绝" in last.content

    def test_allow_web_lets_fetch_through_in_chat_only(self, monkeypatch, clean_state):
        # allow_web_search=true:chat_only 下也放行 fetch_url / web_search(其它仍拦)
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "chat_only")
        monkeypatch.setattr(config, "REMOTE_ALLOW_WEB", True)

        class _FakeTool:
            def invoke(self, args): return "WEBOK"
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"fetch_url": _FakeTool(), "web_search": _FakeTool()})

        last = _exec({"name": "fetch_url", "args": {"url": "http://example.com"}, "id": "1"})
        assert isinstance(last, ToolMessage)
        assert "chat_only" not in (last.content or "")        # 没被纯对话拦
        # run_command 仍被 chat_only 拦(只放网络查询,不碰其它)
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"run_command": _FakeTool()})
        last2 = _exec({"name": "run_command", "args": {"command": "ls"}, "id": "2"})
        assert isinstance(last2, ToolMessage) and "chat_only" in last2.content

    def test_allow_web_off_still_blocks_fetch(self, monkeypatch, clean_state):
        monkeypatch.setattr(state, "remote_session", True)
        monkeypatch.setattr(config, "REMOTE_MODE", "chat_only")
        monkeypatch.setattr(config, "REMOTE_ALLOW_WEB", False)
        last = _exec({"name": "fetch_url", "args": {"url": "http://example.com"}, "id": "1"})
        assert isinstance(last, ToolMessage) and "chat_only" in last.content

    def test_no_remote_session_not_blocked_by_chat_only(self, monkeypatch, clean_state):
        # 非远程会话（PC 发起）：remote_session=False，安全拦截不触发
        monkeypatch.setattr(state, "remote_session", False)
        monkeypatch.setattr(config, "REMOTE_MODE", "chat_only")
        last = _exec({"name": "list_directory", "args": {"path": "."}, "id": "1"})
        if isinstance(last, ToolMessage):
            assert "chat_only" not in last.content    # 没被远程纯对话拦截
