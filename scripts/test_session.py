"""会话管理 单元测试

覆盖：save_session / load_session / list_sessions / delete_session
    / reset_history / move_sessions_to_no_project / _sanitize_title
    / _msg_to_dict / _dict_to_msg 序列化往返

注意：这些函数依赖全局 state，用 monkeypatch 隔离路径后直接调用。
"""
import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

from src import state
from src.memory import (
    save_session,
    load_session,
    list_sessions,
    delete_session,
    reset_history,
    move_sessions_to_no_project,
    _msg_to_dict,
    _dict_to_msg,
    _sanitize_title,
    _extract_text_content,
    _build_ai_message,
)


# ── _sanitize_title ─────────────────────────────────────
class TestSanitizeTitle:
    def test_normal_text(self):
        assert _sanitize_title("你好世界") == "你好世界"

    def test_strips_quotes_and_punctuation(self):
        assert _sanitize_title("「标题」") == "标题"
        assert _sanitize_title('"标题"') == "标题"

    def test_truncates_at_16(self):
        result = _sanitize_title("a" * 20)
        assert len(result) == 16

    def test_empty_returns_empty(self):
        assert _sanitize_title("") == ""
        assert _sanitize_title("   ") == ""
        assert _sanitize_title(None) == ""

    def test_newlines_become_spaces(self):
        assert _sanitize_title("hello\nworld") == "hello world"


# ── _msg_to_dict / _dict_to_msg 往返 ────────────────────
class TestMessageSerialization:
    def test_human_message_roundtrip(self):
        msg = HumanMessage(content="hello")
        d = _msg_to_dict(msg)
        assert d["type"] == "HumanMessage"
        assert d["content"] == "hello"
        restored = _dict_to_msg(d)
        assert isinstance(restored, HumanMessage)
        assert restored.content == "hello"

    def test_ai_message_roundtrip(self):
        msg = AIMessage(content="hi there")
        d = _msg_to_dict(msg)
        assert d["type"] == "AIMessage"
        restored = _dict_to_msg(d)
        assert isinstance(restored, AIMessage)
        assert restored.content == "hi there"

    def test_ai_message_with_tool_calls(self):
        msg = AIMessage(
            content="calling tool",
            tool_calls=[{"name": "read_file", "args": {"path": "x.py"}, "id": "tc1"}],
        )
        d = _msg_to_dict(msg)
        assert "tool_calls" in d
        restored = _dict_to_msg(d)
        assert restored.tool_calls == msg.tool_calls

    def test_ai_message_with_reasoning(self):
        msg = AIMessage(
            content="answer",
            additional_kwargs={"reasoning_content": "thinking..."},
        )
        d = _msg_to_dict(msg)
        assert d["reasoning_content"] == "thinking..."
        restored = _dict_to_msg(d)
        assert restored.additional_kwargs["reasoning_content"] == "thinking..."

    def test_tool_message_roundtrip(self):
        msg = ToolMessage(content="result", tool_call_id="tc1")
        d = _msg_to_dict(msg)
        assert d["type"] == "ToolMessage"
        assert d["tool_call_id"] == "tc1"
        restored = _dict_to_msg(d)
        assert isinstance(restored, ToolMessage)
        assert restored.tool_call_id == "tc1"

    def test_system_message_roundtrip(self):
        msg = SystemMessage(content="system prompt")
        d = _msg_to_dict(msg)
        assert d["type"] == "SystemMessage"
        restored = _dict_to_msg(d)
        assert isinstance(restored, SystemMessage)

    def test_none_msg_returns_unknown(self):
        d = _msg_to_dict(None)
        assert d["type"] == "Unknown"
        assert d["content"] == ""


# ── _extract_text_content ───────────────────────────────
class TestExtractTextContent:
    def test_string_content(self):
        class Fake:
            content = "hello"
        assert _extract_text_content(Fake()) == "hello"

    def test_list_content_with_text_blocks(self):
        class Fake:
            content = [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]
        assert _extract_text_content(Fake()) == "answer"

    def test_list_content_mixed(self):
        class Fake:
            content = [
                {"type": "text", "text": "line1"},
                "line2",
                {"type": "text", "text": "line3"},
            ]
        result = _extract_text_content(Fake())
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result

    def test_none_content(self):
        class Fake:
            content = None
        assert _extract_text_content(Fake()) != ""  # should fallback to str()


# ── _build_ai_message ───────────────────────────────────
class TestBuildAiMessage:
    def test_simple_text(self):
        class Gathered:
            content = "hello"
            additional_kwargs = {}
        msg = _build_ai_message(Gathered(), "hello", [])
        assert isinstance(msg, AIMessage)
        assert msg.content == "hello"

    def test_with_thinking_blocks(self):
        class Gathered:
            content = [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]
            additional_kwargs = {}
        msg = _build_ai_message(Gathered(), "answer", [])
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2

    def test_none_gathered(self):
        msg = _build_ai_message(None, "fallback text", [])
        assert msg.content == "fallback text"


# ── save_session / load_session ─────────────────────────
class TestSaveLoadSession:
    def _setup_session(self, isolated_memory):
        """设置一个最小可保存的会话。"""
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content="system"))
        state.chat_history.append(HumanMessage(content="用户问题"))
        state.chat_history.append(AIMessage(content="助手回答"))
        state.current_session_id = None
        state.current_session_title = None

    def test_save_and_load_roundtrip(self, isolated_memory):
        self._setup_session(isolated_memory)
        save_session()
        assert state.current_session_id is not None

        sid = state.current_session_id
        # 清空后重新加载
        state.chat_history.clear()
        assert load_session(sid) is True
        assert len(state.chat_history) == 3
        assert state.current_session_id == sid

    def test_reset_history_drops_recycled_session_from_registry(self, isolated_memory):
        """切角色卡/恢复默认走 reset_history：把当前会话回收成新对话后，必须把旧 id 从注册表
        摘掉，否则点击侧栏旧会话会命中这个被清空的对象、显示空白且加载不出（回归 #会话加载）。"""
        import src.session as _session
        self._setup_session(isolated_memory)
        save_session()
        sid = state.current_session_id
        assert sid is not None
        assert _session.get(sid) is not None          # 存盘后在注册表

        reset_history()                               # 回收当前会话成空白新对话
        assert state.current_session_id is None
        assert _session.get(sid) is None              # 旧 id 已摘除（不再命中空对象）

        # 旧会话内容仍在盘上，重新读盘能完整恢复
        assert load_session(sid) is True
        assert len(state.chat_history) == 3
        assert state.current_session_id == sid

    def test_save_skips_short_history(self, isolated_memory):
        """只有 system message 时不应保存。"""
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content="system"))
        state.current_session_id = None
        save_session()
        assert state.current_session_id is None  # 没有真正保存

    def test_load_nonexistent_returns_false(self, isolated_memory):
        assert load_session("nonexistent_id") is False

    def test_save_auto_generates_id(self, isolated_memory):
        self._setup_session(isolated_memory)
        assert state.current_session_id is None
        save_session()
        assert state.current_session_id is not None
        assert len(state.current_session_id) > 0

    def test_save_uses_first_user_text_as_title(self, isolated_memory):
        self._setup_session(isolated_memory)
        save_session()
        assert state.current_session_title == "用户问题" or state.current_session_title is None

    def test_save_session_file_exists(self, isolated_memory):
        self._setup_session(isolated_memory)
        save_session()
        session_file = str(isolated_memory / f"{state.current_session_id}.json")
        assert os.path.exists(session_file)
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "messages" in data
        assert len(data["messages"]) == 3


# ── list_sessions / delete_session ──────────────────────
class TestListDeleteSessions:
    def _create_session(self, isolated_memory, title="test", project=None):
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content="system"))
        state.chat_history.append(HumanMessage(content=title))
        state.chat_history.append(AIMessage(content="reply"))
        state.current_session_id = None
        state.current_session_title = None
        state.current_project = project
        save_session()
        return state.current_session_id

    def test_list_returns_saved_sessions(self, isolated_memory):
        sid = self._create_session(isolated_memory)
        sessions = list_sessions(project_filter="__all__")
        assert any(s["id"] == sid for s in sessions)

    def test_list_filters_by_project(self, isolated_memory):
        s1 = self._create_session(isolated_memory, title="global", project=None)
        s2 = self._create_session(isolated_memory, title="proj", project="/some/path")
        state.current_project = None
        global_sessions = list_sessions(project_filter="__current__")
        assert any(s["id"] == s1 for s in global_sessions)
        assert not any(s["id"] == s2 for s in global_sessions)

    def test_delete_removes_session(self, isolated_memory):
        sid = self._create_session(isolated_memory)
        delete_session(sid)
        sessions = list_sessions(project_filter="__all__")
        assert not any(s["id"] == sid for s in sessions)
        assert load_session(sid) is False

    def test_delete_nonexistent_no_error(self, isolated_memory):
        delete_session("no_such_id")  # 不应抛异常


# ── move_sessions_to_no_project ─────────────────────────
class TestMoveSessionsToNoProject:
    def test_move_sessions(self, isolated_memory):
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content="system"))
        state.chat_history.append(HumanMessage(content="project chat"))
        state.chat_history.append(AIMessage(content="reply"))
        state.current_session_id = None
        state.current_session_title = None
        state.current_project = "/old/project"
        save_session()
        sid = state.current_session_id

        moved = move_sessions_to_no_project("/old/project")
        assert moved == 1

        sessions = list_sessions(project_filter="__all__")
        moved_session = [s for s in sessions if s["id"] == sid][0]
        assert moved_session["project"] is None

    def test_no_move_for_empty_path(self, isolated_memory):
        assert move_sessions_to_no_project("") == 0
        assert move_sessions_to_no_project(None) == 0


# ── reset_history ───────────────────────────────────────
class TestResetHistory:
    def test_reset_clears_and_adds_system(self, isolated_memory):
        state.chat_history.clear()
        state.chat_history.append(SystemMessage(content="system"))
        state.chat_history.append(HumanMessage(content="msg"))
        state.current_session_id = "test_id"
        state.current_session_title = "test"

        reset_history()

        assert len(state.chat_history) == 1
        assert isinstance(state.chat_history[0], SystemMessage)
        assert state.current_session_id is None
        assert state.current_session_title is None


# ── 独立运行 ────────────────────────────────────────────
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
