"""验证状态管理 + 完成闸门机制的专项测试。

覆盖：
1. verification.py 的纯函数逻辑（mark_dirty, mark_check, mark_tests, mark_diff_reviewed, get_verification_gaps）
2. tools.py 的状态标记集成（写文件 / check_code / run_tests / git_diff）
3. agent.py 的闸门检查逻辑
"""

import os
import sys
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.verification import (
    new_verification,
    reset_verification,
    mark_dirty,
    mark_check,
    mark_tests,
    mark_diff_reviewed,
    get_verification_gaps,
    needs_verification,
)


# ── 纯函数测试（verification.py）──


class TestNewVerification:
    """测试 new_verification() 默认值。"""

    def test_defaults_clean(self):
        v = new_verification()
        assert v["dirty_files"] == []
        assert v["code_dirty_files"] == []
        assert v["checks"] == {}
        assert v["tests_run"] is False
        assert v["tests_passed"] is None
        assert v["tests_reason"] == ""
        assert v["diff_reviewed"] is False
        assert v["gate_prompted"] is False

    def test_fresh_instances_independent(self):
        """两个新实例不共享可变字段。"""
        v1 = new_verification()
        v2 = new_verification()
        mark_dirty(v1, "a.py")
        assert v2["dirty_files"] == []


class TestResetVerification:
    """测试 reset_verification() 重置功能。"""

    def test_reset_after_modifications(self):
        v = new_verification()
        mark_dirty(v, "foo.py")
        mark_tests(v, True, "10 passed")
        mark_diff_reviewed(v)
        v["gate_prompted"] = True

        reset_verification(v)
        assert v["dirty_files"] == []
        assert v["code_dirty_files"] == []
        assert v["checks"] == {}
        assert v["tests_run"] is False
        assert v["tests_passed"] is None
        assert v["diff_reviewed"] is False
        assert v["gate_prompted"] is False


class TestMarkDirty:
    """测试 mark_dirty() 标记脏文件。"""

    def test_mark_single_file(self):
        v = new_verification()
        mark_dirty(v, "src/main.py")
        assert "src/main.py" in v["dirty_files"]
        assert "src/main.py" in v["code_dirty_files"]

    def test_mark_non_code_file(self):
        """非代码文件不进 code_dirty_files。"""
        v = new_verification()
        mark_dirty(v, "README.md")
        assert "README.md" in v["dirty_files"]
        assert v["code_dirty_files"] == []

    def test_mark_json_file(self):
        """JSON 文件不进 code_dirty_files。"""
        v = new_verification()
        mark_dirty(v, "config.json")
        assert "config.json" in v["dirty_files"]
        assert v["code_dirty_files"] == []

    def test_dedup_same_file(self):
        """同一文件重复标记不重复。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_dirty(v, "a.py")
        assert v["dirty_files"].count("a.py") == 1

    def test_empty_path_ignored(self):
        v = new_verification()
        mark_dirty(v, "")
        assert v["dirty_files"] == []

    def test_code_extension_coverage(self):
        """多种代码扩展名都进 code_dirty_files。"""
        v = new_verification()
        for ext in [".py", ".js", ".ts", ".go", ".rs", ".java", ".cpp", ".vue"]:
            mark_dirty(v, f"file{ext}")
        assert len(v["code_dirty_files"]) == 8

    def test_write_invalidate_check(self):
        """写文件使该文件的静态检查结果作废。"""
        v = new_verification()
        mark_check(v, "a.py", True, "ruff")
        mark_dirty(v, "a.py")
        assert "a.py" not in v["checks"]

    def test_write_invalidate_tests_for_code_files(self):
        """写代码文件使测试结果作废。"""
        v = new_verification()
        mark_tests(v, True, "10 passed")
        mark_dirty(v, "a.py")
        assert v["tests_run"] is False
        assert v["tests_passed"] is None

    def test_write_non_code_not_invalidate_tests(self):
        """写非代码文件不使测试结果作废。"""
        v = new_verification()
        mark_tests(v, True, "10 passed")
        mark_dirty(v, "README.md")
        assert v["tests_run"] is True
        assert v["tests_passed"] is True

    def test_write_invalidate_diff_reviewed(self):
        """写文件使 diff 审查状态重置。"""
        v = new_verification()
        mark_diff_reviewed(v)
        mark_dirty(v, "a.py")
        assert v["diff_reviewed"] is False


class TestMarkCheck:
    """测试 mark_check() 静态检查记录。"""

    def test_passed(self):
        v = new_verification()
        mark_check(v, "a.py", True, "ruff")
        assert v["checks"]["a.py"]["passed"] is True
        assert v["checks"]["a.py"]["checker"] == "ruff"

    def test_failed(self):
        v = new_verification()
        mark_check(v, "a.py", False, "ruff")
        assert v["checks"]["a.py"]["passed"] is False

    def test_unknown(self):
        """无法确定（不支持的语言）。"""
        v = new_verification()
        mark_check(v, "foo.xyz", None, "")
        assert v["checks"]["foo.xyz"]["passed"] is None


class TestMarkTests:
    """测试 mark_tests() 测试结果记录。"""

    def test_all_passed(self):
        v = new_verification()
        mark_tests(v, True, "10 passed")
        assert v["tests_run"] is True
        assert v["tests_passed"] is True
        assert v["tests_reason"] == "10 passed"

    def test_has_failures(self):
        v = new_verification()
        mark_tests(v, False, "2 failed, 8 passed")
        assert v["tests_passed"] is False

    def test_unknown(self):
        v = new_verification()
        mark_tests(v, None, "无法确定")
        assert v["tests_passed"] is None
        assert v["tests_reason"] == "无法确定"


class TestGetVerificationGaps:
    """测试 get_verification_gaps() 间隙检测。"""

    def test_no_changes_no_gaps(self):
        v = new_verification()
        assert get_verification_gaps(v) == []

    def test_code_change_no_tests(self):
        v = new_verification()
        mark_dirty(v, "a.py")
        gaps = get_verification_gaps(v)
        assert len(gaps) == 2
        assert any("尚未运行测试" in g for g in gaps)
        assert any("git_diff" in g for g in gaps)

    def test_code_change_tests_passed(self):
        """代码改动 + 测试通过但未看 diff = 仍有间隙。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_tests(v, True, "10 passed")
        gaps = get_verification_gaps(v)
        assert any("git_diff" in g for g in gaps)

    def test_code_change_tests_failed(self):
        """测试有失败 = 有间隙。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_tests(v, False, "2 failed")
        gaps = get_verification_gaps(v)
        assert any("测试未通过" in g for g in gaps)

    def test_check_failed(self):
        """静态检查失败 = 有间隙（不论测试结果）。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_tests(v, True, "10 passed")
        mark_check(v, "a.py", False, "ruff")
        gaps = get_verification_gaps(v)
        assert any("静态检查未通过" in g for g in gaps)

    def test_non_code_change_no_test_gap(self):
        """非代码文件改动不要求测试。"""
        v = new_verification()
        mark_dirty(v, "README.md")
        gaps = get_verification_gaps(v)
        assert not any("测试" in g for g in gaps)
        assert any("git_diff" in g for g in gaps)

    def test_multiple_files_multiple_gaps(self):
        """多个文件多种问题。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_dirty(v, "b.py")
        mark_check(v, "a.py", False, "ruff")
        mark_check(v, "b.py", False, "ruff")
        # 未跑测试 + 静态检查失败 → 至少 2 个间隙
        gaps = get_verification_gaps(v)
        assert len(gaps) >= 2

    def test_all_green(self):
        """所有验证通过 = 无间隙。"""
        v = new_verification()
        mark_dirty(v, "a.py")
        mark_tests(v, True, "10 passed")
        mark_check(v, "a.py", True, "ruff")
        mark_diff_reviewed(v)
        assert get_verification_gaps(v) == []


class TestNeedsVerification:
    """测试 needs_verification() 快速判断。"""

    def test_no_changes(self):
        v = new_verification()
        assert needs_verification(v) is False

    def test_has_changes(self):
        v = new_verification()
        mark_dirty(v, "a.py")
        assert needs_verification(v) is True


# ── 边界场景 ──


class TestEdgeCases:
    """边界与组合场景。"""

    def test_check_only_no_dirty(self):
        """有检查结果但无脏文件（理论上不会发生）。"""
        v = new_verification()
        mark_check(v, "a.py", True, "ruff")
        assert get_verification_gaps(v) == []

    def test_mark_dirty_none_path(self):
        """传 None 不应崩溃。"""
        v = new_verification()
        mark_dirty(v, None)
        assert v["dirty_files"] == []

    def test_mark_check_none_path(self):
        v = new_verification()
        mark_check(v, None, True, "ruff")
        assert v["checks"] == {}

    def test_gate_prompted_field(self):
        """gate_prompted 字段可手动设置/重置。"""
        v = new_verification()
        v["gate_prompted"] = True
        assert v["gate_prompted"] is True
        reset_verification(v)
        assert v["gate_prompted"] is False

    def test_chinese_in_reason(self):
        """reason 可含中文。"""
        v = new_verification()
        mark_tests(v, None, "pytest 未安装")
        assert "未安装" in v["tests_reason"]


class TestAgentGateIntegration:
    """最小集成：确保 agent_loop 的完成闸门实际可达。"""

    def test_first_final_text_is_blocked_until_verification_attempt(self, monkeypatch):
        from src import agent, session as _session, state

        sess = _session.Session()
        sess.chat_history = [SystemMessage(content="system")]
        _session.set_active(sess)
        state.current_model_index = 0
        state.agent_mode = "act"

        calls = {"n": 0}

        def fake_stream(ui):
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    "",
                    [{"name": "fake_write", "args": {}, "id": "1"}],
                    {"input": 0, "output": 0, "total": 0},
                    AIMessage(content="", tool_calls=[{"name": "fake_write", "args": {}, "id": "1"}]),
                )
            if calls["n"] == 2:
                return (
                    "我已经完成了。",
                    [],
                    {"input": 0, "output": 0, "total": 0},
                    AIMessage(content="我已经完成了。"),
                )
            return (
                "已尝试验证，但仍有风险。",
                [],
                {"input": 0, "output": 0, "total": 0},
                AIMessage(content="已尝试验证，但仍有风险。"),
            )

        def fake_execute_tool(tc, ui, _preinvoked=None):
            mark_dirty(_session.get_verification(), "src/foo.py")

        monkeypatch.setattr(agent, "MODEL_LIST", [("fake", "cloud", "fake", {})])
        monkeypatch.setattr(agent, "_stream_with_tools", fake_stream)
        monkeypatch.setattr(agent, "_execute_tool", fake_execute_tool)
        monkeypatch.setattr(agent, "save_session", lambda *a, **k: None)
        monkeypatch.setattr(agent, "maybe_generate_session_title", lambda *a, **k: None)

        class _Emitter:
            def emit(self, *args, **kwargs):
                pass

        class _Bridge:
            sessions_refresh = _Emitter()

        class _UI:
            bridge = _Bridge()
            messages = []
            rendered = []

            def show_message(self, text, tag):
                self.messages.append((text, tag))

            def render_final_markdown(self, text, speak=True):
                self.rendered.append(text)

            def show_token_usage(self, *args, **kwargs):
                pass

        ui = _UI()
        agent.agent_loop(ui)

        assert calls["n"] == 3
        gate_messages = [
            m for m in sess.chat_history
            if "内部验证要求" in getattr(m, "content", "")
        ]
        assert len(gate_messages) == 1
        assert isinstance(gate_messages[0], HumanMessage)
        assert any("验证仍未完整完成" in text for text in ui.rendered)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
