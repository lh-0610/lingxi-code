"""并行工具调用：_can_parallel 判定 + _parallel_invoke 执行 / 失败日志。

守护 Codex review 3 的两个 P3：
- P3①：空参数（{}）的只读工具（list_directory/code_map/git_log…带默认参数）应能并行，
  此前判定多了个 `and tc.get("args")`，空参被错误退回串行。
- P3②：并行工具失败时绕过了串行 _execute_tool 的错误日志，应仍留 ERROR 日志可排查。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import state
from src.streaming import (
    _can_parallel, _parallel_invoke, _execute_tool, NO_ARG_OK_TOOLS,
)


# ── _can_parallel 判定 ──────────────────────────────────
class TestCanParallel:
    def test_empty_args_readonly_tools_can_parallel(self):
        """P3① 守护：list_directory / code_map 等空参数只读工具，{} 调也应能并行。"""
        tcs = [{"name": "list_directory", "args": {}},
               {"name": "code_map", "args": {}}]
        assert _can_parallel(tcs) is True

    def test_multiple_readonly_with_args(self):
        tcs = [{"name": "read_file", "args": {"path": "a.py"}},
               {"name": "search_files", "args": {"pattern": "x"}}]
        assert _can_parallel(tcs) is True

    def test_single_tool_not_parallel(self):
        assert _can_parallel([{"name": "read_file", "args": {"path": "a"}}]) is False

    def test_write_tool_blocks_parallel(self):
        """混入写类工具 → 整批退回串行（保序、避免并发写）。"""
        tcs = [{"name": "read_file", "args": {"path": "a"}},
               {"name": "edit_file", "args": {"path": "a", "old_string": "x", "new_string": "y"}}]
        assert _can_parallel(tcs) is False

    def test_plan_mode_blocks_parallel(self):
        # 用本来可并行的组合（都空参合法），确保 False 只因 plan 模式、不被别的条件干扰
        tcs = [{"name": "list_directory", "args": {}}, {"name": "code_map", "args": {}}]
        state.agent_mode = "plan"
        try:
            assert _can_parallel(tcs) is False
        finally:
            state.agent_mode = "act"

    def test_remote_session_blocks_parallel(self):
        tcs = [{"name": "list_directory", "args": {}}, {"name": "code_map", "args": {}}]
        state.remote_session = True
        try:
            assert _can_parallel(tcs) is False
        finally:
            state.remote_session = False

    def test_required_arg_tool_empty_blocks_parallel(self):
        """P3 守护：缺必填参数的空参调用（read_file {}）混入 → 退串行，不做无意义并行预取。
        read_file 不在 NO_ARG_OK_TOOLS，空参进并行只会预取必失败、产生噪声日志，
        回放阶段还要再被空参保护拦一次；让它走串行、直接拿到清晰的重试指引。"""
        tcs = [{"name": "read_file", "args": {}},        # 缺必填 path
               {"name": "list_directory", "args": {}}]    # 空参合法
        assert _can_parallel(tcs) is False


# ── _parallel_invoke 执行 + 失败日志 ────────────────────
class TestParallelInvoke:
    def test_empty_args_tool_executes(self, project_dir):
        """空参数只读工具并行执行能拿到真实结果（list_directory 默认列项目根）。"""
        (project_dir / "marker.txt").write_text("x", encoding="utf-8")
        res = _parallel_invoke([{"name": "list_directory", "args": {}},
                                {"name": "list_directory", "args": {}}])
        assert len(res) == 2
        assert "marker.txt" in res[0] and "marker.txt" in res[1]

    def test_failure_is_logged(self, project_dir, caplog):
        """P3② 守护：并行工具失败 → 返回失败串 + 留 ERROR 日志（绕过串行路径也别丢日志）。"""
        with caplog.at_level(logging.ERROR):
            res = _parallel_invoke([{"name": "read_file", "args": {}}])  # 缺必填 path → invoke 抛
        assert "失败" in res[0]
        assert any("执行失败" in rec.getMessage() for rec in caplog.records), \
            "并行工具失败必须留 ERROR 日志"


# ── 并行预取 → _execute_tool 回放完整路径（Codex review 4 盲区）──
class TestPreinvokedReplay:
    """覆盖"并行预取后按序 _execute_tool(_preinvoked=...) 回放"的完整路径。
    此前只测了 _parallel_invoke，漏掉回放阶段空参保护误拦、丢弃预取结果的 P2：
    code_map {} 等默认参数工具被并行预取成功，回放时却被空参保护 early-return 拦掉。"""

    def test_no_arg_ok_covers_default_arg_tools(self):
        """空参合法名单必须覆盖所有默认参数齐全的只读工具，且不含必填参数工具。"""
        assert {"code_map", "git_diff", "git_log", "list_background_commands"} <= NO_ARG_OK_TOOLS
        assert "read_file" not in NO_ARG_OK_TOOLS   # 必填 path，空参确实该拦

    def test_preinvoked_empty_arg_tool_uses_result(self, project_dir):
        """code_map {} 被并行预取后回放：用预取结果，不被空参保护拦成"参数为空"。"""
        from unittest.mock import MagicMock
        from langchain_core.messages import ToolMessage
        ui = MagicMock()
        ui.confirm_command.return_value = (True, "")
        state.chat_history = []
        _execute_tool({"name": "code_map", "args": {}, "id": "c1"}, ui,
                      _preinvoked="【预取符号地图 alpha】")
        last = state.chat_history[-1]
        assert isinstance(last, ToolMessage)
        assert "预取符号地图 alpha" in last.content      # 用了预取结果
        assert "参数为空" not in last.content             # 没被空参保护拦

    def test_empty_arg_required_tool_still_blocked(self, project_dir):
        """read_file {}（缺必填 path）串行调用仍被空参保护拦、给重试指引——别误放行。"""
        from unittest.mock import MagicMock
        from langchain_core.messages import ToolMessage
        ui = MagicMock()
        state.chat_history = []
        _execute_tool({"name": "read_file", "args": {}, "id": "c2"}, ui, _preinvoked=None)
        last = state.chat_history[-1]
        assert isinstance(last, ToolMessage)
        assert "参数为空" in last.content or "重新调用" in last.content
