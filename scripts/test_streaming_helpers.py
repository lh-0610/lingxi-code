"""streaming.py 中无网络依赖的核心辅助逻辑测试。"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import src.streaming as streaming


class TestPrettyArgs:
    def test_restores_newlines_without_breaking_windows_paths(self):
        result = streaming._pretty_args({
            "path": r"C:\name\file.txt",
            "content": "line1\nline2",
        })

        assert r"C:\name\file.txt" in result
        assert "line1\nline2" in result


class TestHistorySizing:
    def test_detects_image_blocks(self):
        history = [HumanMessage(content=[
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,X"}},
        ])]

        assert streaming._history_has_image_blocks(history) is True
        assert streaming._history_has_image_blocks([HumanMessage(content="hi")]) is False

    def test_estimates_text_images_and_tool_args(self):
        history = [
            HumanMessage(content=[{"type": "text", "text": "1234567890"}, {"type": "image"}]),
            AIMessage(content="", tool_calls=[{"name": "x", "args": {"a": "bc"}, "id": "1"}]),
        ]

        expected_chars = 10 + 1000 + len(str({"a": "bc"}))
        assert streaming._estimate_tokens(history) == int(expected_chars * 0.7)

    def test_trim_keeps_system_placeholder_and_recent_messages(self):
        history = [
            SystemMessage(content="system"),
            HumanMessage(content="old " * 100),
            AIMessage(content="recent answer"),
            HumanMessage(content="recent question"),
        ]

        trimmed, dropped = streaming._maybe_trim_history(history, budget=1, keep_recent=2)

        assert dropped == 1
        assert trimmed[0] is history[0]
        assert "跳过中间 1 条消息" in trimmed[1].content
        assert trimmed[-2:] == history[-2:]


class TestSystemMessageNormalization:
    def test_keeps_consecutive_leading_system_messages(self):
        history = [
            SystemMessage(content="system 1"),
            SystemMessage(content="system 2"),
            HumanMessage(content="hello"),
        ]

        normalized = streaming._normalize_nonleading_system_messages(history)

        assert normalized[:2] == history[:2]
        assert normalized[2] is history[2]

    def test_converts_nonleading_system_without_mutating_history(self):
        late_system = SystemMessage(content="continue verification")
        history = [
            SystemMessage(content="system"),
            HumanMessage(content="hello"),
            AIMessage(content="done"),
            late_system,
        ]

        normalized = streaming._normalize_nonleading_system_messages(history)

        assert isinstance(normalized[-1], HumanMessage)
        assert "内部系统指令" in normalized[-1].content
        assert "continue verification" in normalized[-1].content
        assert history[-1] is late_system
        assert isinstance(history[-1], SystemMessage)


class TestSanitizeToolPairs:
    def test_dangling_tool_use_gets_placeholder_result(self):
        # 停止/删除留下：AIMessage 有 tool_call 但缺对应 ToolMessage
        history = [
            HumanMessage(content="q"),
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "t1"}]),
            HumanMessage(content="next"),
        ]
        out = streaming._sanitize_tool_pairs(history)
        assert isinstance(out[2], ToolMessage) and out[2].tool_call_id == "t1"
        assert out[3] is history[2]

    def test_drops_orphan_tool_result(self):
        # 压缩把 tool_use 压走，只剩孤儿 ToolMessage
        history = [
            HumanMessage(content="q"),
            ToolMessage(content="result", tool_call_id="gone"),
            AIMessage(content="answer"),
        ]
        out = streaming._sanitize_tool_pairs(history)
        assert all(not isinstance(m, ToolMessage) for m in out)

    def test_keeps_well_formed_pairs_unchanged(self):
        history = [
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "a"}]),
            ToolMessage(content="ok", tool_call_id="a"),
        ]
        out = streaming._sanitize_tool_pairs(history)
        assert len(out) == 2 and out[0] is history[0] and out[1] is history[1]

    def test_partial_answer_fills_only_missing(self):
        history = [
            AIMessage(content="", tool_calls=[
                {"name": "x", "args": {}, "id": "a"},
                {"name": "y", "args": {}, "id": "b"},
            ]),
            ToolMessage(content="ok", tool_call_id="a"),
        ]
        out = streaming._sanitize_tool_pairs(history)
        ids = {m.tool_call_id for m in out if isinstance(m, ToolMessage)}
        assert ids == {"a", "b"}


class TestSystemPromptCache:
    def test_anthropic_wraps_system_prompt_with_cache_control(self):
        original = [SystemMessage(content="old"), HumanMessage(content="hello")]

        wrapped = streaming._wrap_system_for_cache(original, "fresh", provider="anthropic")

        assert wrapped[0].content == [{
            "type": "text",
            "text": "fresh",
            "cache_control": {"type": "ephemeral"},
        }]
        assert wrapped[1] is original[1]
        assert original[0].content == "old"

    def test_openai_compatible_provider_keeps_plain_string(self):
        wrapped = streaming._wrap_system_for_cache(
            [SystemMessage(content="old")], "fresh", provider="cloud",
        )

        assert wrapped[0].content == "fresh"


class TestExtractUsage:
    def test_prefers_usage_metadata_and_computes_total(self):
        gathered = SimpleNamespace(usage_metadata={
            "input_tokens": 7,
            "output_tokens": 3,
        })

        assert streaming._extract_usage(gathered) == {"input": 7, "output": 3, "total": 10}

    def test_falls_back_to_response_metadata(self):
        gathered = SimpleNamespace(
            usage_metadata=None,
            response_metadata={"token_usage": {
                "prompt_tokens": 4,
                "completion_tokens": 6,
                "total_tokens": 10,
            }},
        )

        assert streaming._extract_usage(gathered) == {"input": 4, "output": 6, "total": 10}


class TestToolCallCollection:
    def test_keeps_known_calls_and_fail_opens_invalid_args(self, monkeypatch):
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"read_file": object()})
        gathered = SimpleNamespace(
            tool_calls=[
                {"name": "read_file", "args": {"path": "a.py"}, "id": "ok"},
                {"name": "unknown", "args": {}, "id": "skip"},
            ],
            invalid_tool_calls=[
                {"name": "read_file", "args": "{bad json", "id": "bad"},
            ],
        )

        assert streaming._collect_tool_calls(gathered) == [
            {"name": "read_file", "args": {"path": "a.py"}, "id": "ok"},
            {"name": "read_file", "args": {}, "id": "bad"},
        ]

    def test_synthesizes_unique_ids_for_idless_duplicate_calls(self, monkeypatch):
        """provider 不返回 id 时，同名工具调两次不应撞成同一个 id（否则下一轮 400）。"""
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"read_file": object()})
        gathered = SimpleNamespace(
            tool_calls=[
                {"name": "read_file", "args": {"path": "a.py"}, "id": None},
                {"name": "read_file", "args": {"path": "b.py"}, "id": ""},
            ],
            invalid_tool_calls=[],
        )

        result = streaming._collect_tool_calls(gathered)
        ids = [tc["id"] for tc in result]
        assert len(result) == 2
        assert len(set(ids)) == 2, f"id 应唯一，实际 {ids}"
        assert all(i for i in ids), "不应有空 id"

    def test_extracts_anthropic_thinking_blocks(self):
        gathered = SimpleNamespace(content=[
            {"type": "thinking", "thinking": "first"},
            {"type": "text", "text": "answer"},
            {"type": "thinking", "thinking": "second"},
        ])

        assert streaming._extract_thinking(gathered) == "first\nsecond"


class TestStreamRetry:
    def test_retries_startup_failure_before_first_chunk(self, monkeypatch):
        attempts = []

        class FakeLlm:
            def stream(self, _messages):
                attempts.append("called")
                if len(attempts) == 1:
                    raise RuntimeError("temporary")
                yield "ok"

        monkeypatch.setattr(streaming, "STREAM_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(streaming.time, "sleep", lambda _seconds: None)

        assert list(streaming._stream_chunks_with_retry(FakeLlm(), [])) == ["ok"]
        assert len(attempts) == 2

    def test_does_not_retry_after_a_chunk_was_yielded(self, monkeypatch):
        attempts = []

        class FakeLlm:
            def stream(self, _messages):
                attempts.append("called")
                yield "first"
                raise RuntimeError("late failure")

        monkeypatch.setattr(streaming, "STREAM_RETRY_ATTEMPTS", 3)

        with pytest.raises(RuntimeError, match="late failure"):
            list(streaming._stream_chunks_with_retry(FakeLlm(), []))
        assert len(attempts) == 1
