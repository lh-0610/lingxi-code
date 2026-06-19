"""失败自动诊断与修复循环专项测试。"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.verification import (
    _extract_failure_summary,
    _is_failure_result,
    check_repair_allowed,
    inject_repair_prompt,
    new_verification,
    reset_verification,
)


def _dirty_state():
    v = new_verification()
    v["dirty_files"] = ["src/foo.py"]
    v["code_dirty_files"] = ["src/foo.py"]
    return v


class TestFailureDetection:
    def test_pytest_failure(self):
        assert _is_failure_result("❌ 1 failed", "run_tests") is True

    def test_pytest_success(self):
        assert _is_failure_result("✅ 10 passed，全部通过", "run_tests") is False

    def test_check_failure(self):
        assert _is_failure_result("⚠️ ruff 发现问题", "check_code") is True

    def test_unrelated_tool_never_triggers(self):
        assert _is_failure_result("❌ failed", "edit_file") is False

    def test_summary_prefers_failed_line(self):
        text = "noise\nFAILED tests/test_x.py::test_one - AssertionError\nmore"
        assert "test_one" in _extract_failure_summary(text)


class TestSessionRepairState:
    def test_requires_code_changes(self):
        v = new_verification()
        allowed, reason = check_repair_allowed(
            v, "run_tests", "❌ 1 failed",
        )
        assert allowed is False
        assert reason is None

    def test_attempts_are_session_local(self):
        v1 = _dirty_state()
        v2 = _dirty_state()

        assert check_repair_allowed(v1, "run_tests", "❌ 1 failed")[0] is True

        assert v1["failure_diagnosis"]["attempt"] == 1
        assert v2["failure_diagnosis"]["attempt"] == 0

    def test_stops_after_three_attempts(self):
        v = _dirty_state()
        for _ in range(3):
            assert check_repair_allowed(v, "run_tests", "❌ 1 failed")[0] is True

        allowed, reason = check_repair_allowed(v, "run_tests", "❌ 1 failed")
        assert allowed is False
        assert "3" in reason

    def test_success_clears_failure_state(self):
        v = _dirty_state()
        check_repair_allowed(v, "run_tests", "❌ 1 failed")

        allowed, reason = check_repair_allowed(
            v, "run_tests", "✅ 10 passed，全部通过",
        )

        assert allowed is False
        assert reason is None
        assert v["failure_diagnosis"]["attempt"] == 0
        assert v["failure_diagnosis"]["tool"] == ""

    def test_reset_clears_failure_state(self):
        v = _dirty_state()
        check_repair_allowed(v, "check_code", "⚠️ syntax error")

        reset_verification(v)

        assert v["failure_diagnosis"]["attempt"] == 0
        assert v["failure_diagnosis"]["reason"] == ""

    def test_prompt_contains_attempt_and_reason(self):
        v = _dirty_state()
        check_repair_allowed(
            v, "run_tests",
            "FAILED scripts/test_x.py::test_one - AssertionError",
        )

        prompt = inject_repair_prompt(v)

        assert "1/3" in prompt
        assert "test_one" in prompt
        assert "重新运行" in prompt


class TestAgentRepairIntegration:
    def test_agent_injects_human_message(self, monkeypatch):
        from src import agent, session as session_mod, state
        from src.verification import mark_dirty

        sess = session_mod.Session()
        sess.chat_history = [SystemMessage(content="system")]
        session_mod.set_active(sess)
        state.current_model_index = 0
        state.agent_mode = "act"

        calls = {"n": 0}

        def fake_stream(_ui):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    "",
                    [{"name": "run_tests", "args": {}, "id": "test-1"}],
                    {"input": 0, "output": 0, "total": 0},
                    AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "run_tests", "args": {}, "id": "test-1",
                        }],
                    ),
                )
            return (
                "无法继续修复。",
                [],
                {"input": 0, "output": 0, "total": 0},
                AIMessage(content="无法继续修复。"),
            )

        def fake_execute(tc, _ui, _preinvoked=None):
            mark_dirty(sess.verification, "src/foo.py")
            sess.chat_history.append(
                __import__(
                    "langchain_core.messages", fromlist=["ToolMessage"],
                ).ToolMessage(
                    content="❌ 1 failed\nFAILED scripts/test_x.py::test_one",
                    tool_call_id=tc["id"],
                )
            )

        monkeypatch.setattr(agent, "MODEL_LIST", [("fake", "cloud", "fake", {})])
        monkeypatch.setattr(agent, "_stream_with_tools", fake_stream)
        monkeypatch.setattr(agent, "_execute_tool", fake_execute)
        monkeypatch.setattr(agent, "save_session", lambda *a, **k: None)
        monkeypatch.setattr(
            agent, "maybe_generate_session_title", lambda *a, **k: None,
        )

        class _Emitter:
            def emit(self, *args, **kwargs):
                pass

        class _UI:
            bridge = type("Bridge", (), {"sessions_refresh": _Emitter()})()

            def show_message(self, *args, **kwargs):
                pass

            def render_final_markdown(self, *args, **kwargs):
                pass

            def show_token_usage(self, *args, **kwargs):
                pass

        agent.agent_loop(_UI())

        repair_messages = [
            msg for msg in sess.chat_history
            if "自动诊断" in str(getattr(msg, "content", ""))
        ]
        assert repair_messages
        assert isinstance(repair_messages[0], HumanMessage)
