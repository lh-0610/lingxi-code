"""真实命令/MCP 修改必须进入完成验收；未知检查不能当通过。"""
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

from src import config, session, state, tools, verification, workspace_changes


def python_command(program):
    args = [sys.executable, "-c", program]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def test_command_write_marks_dirty_and_requires_validation(project_dir):
    target = project_dir / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    result = tools.run_command.func(python_command(
        "from pathlib import Path; Path('app.py').write_text('value = 2\\n')"))
    v = session.get_verification()
    assert "退出码: 0" in result
    assert "app.py" in v["code_dirty_files"]
    assert verification.get_verification_gaps(v)


def test_readonly_command_does_not_create_dirty_files(project_dir):
    (project_dir / "app.py").write_text("value = 1\n", encoding="utf-8")
    tools.run_command.func(python_command("print('read only')"))
    assert verification.get_verification_gaps(session.get_verification()) == []


def test_rewriting_identical_bytes_does_not_invalidate_tests(project_dir):
    target = project_dir / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tools.run_command.func(python_command(
        "from pathlib import Path; p=Path('app.py'); p.write_bytes(p.read_bytes())"))
    assert session.get_verification()["dirty_files"] == []


def test_later_change_invalidates_previous_test_result(project_dir):
    target = project_dir / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tools.run_command.func(python_command("print('started')"))
    v = session.get_verification()
    verification.mark_tests(v, True)
    verification.mark_diff_reviewed(v)
    target.write_text("value = 2\n", encoding="utf-8")
    assert verification.get_verification_gaps(v)
    assert v["tests_passed"] is None
    assert not v["diff_reviewed"]


def test_tracking_failure_stays_unverified(project_dir, monkeypatch):
    def fail(*args):
        raise PermissionError("cannot inspect workspace")

    monkeypatch.setattr(workspace_changes, "_snapshot", fail)
    tools.run_command.func(python_command("print('done')"))
    gaps = verification.get_verification_gaps(session.get_verification())
    assert any("cannot inspect workspace" in gap for gap in gaps)


def test_missing_pytest_leaves_explicit_gap(project_dir, monkeypatch):
    monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", False)
    tools.write_file.func("app.py", "value = 2\n")
    original = subprocess.run

    def missing_pytest(args, **kwargs):
        if isinstance(args, list) and "pytest" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="No module named pytest")
        return original(args, **kwargs)

    monkeypatch.setattr(tools.subprocess, "run", missing_pytest)
    tools.run_tests.func()
    v = session.get_verification()
    verification.mark_diff_reviewed(v)
    assert v["tests_passed"] is None
    assert any("pytest 未安装" in gap for gap in verification.get_verification_gaps(v))


def test_real_check_output_triggers_repair(project_dir, monkeypatch):
    (project_dir / "app.py").write_text("missing()\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_run_code_check", lambda path: ("app.py:1: F821 missing", "ruff"))
    v = session.get_verification()
    verification.mark_dirty(v, "app.py")
    result = tools.check_code.func("app.py")
    allowed, reason = verification.check_repair_allowed(v, "check_code", result)
    assert allowed
    assert reason
    assert v["failure_diagnosis"]["attempt"] == 1


def test_mcp_local_write_is_tracked(project_dir, monkeypatch):
    from src import streaming
    from src.subagent import HeadlessUI

    def write(args):
        (project_dir / "app.py").write_text("value = 2\n", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(streaming, "get_tool_map", lambda: {"mcp_test_write": SimpleNamespace(invoke=write)})
    streaming._execute_tool({"name": "mcp_test_write", "args": {}, "id": "mcp-write"}, HeadlessUI())
    assert "app.py" in session.get_verification()["code_dirty_files"]


def test_validation_after_command_is_not_invalidated_again(project_dir, monkeypatch):
    from src import streaming
    from src.subagent import HeadlessUI

    target = project_dir / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tools.run_command.func(python_command(
        "from pathlib import Path; Path('app.py').write_text('value = 2\\n')"))
    v = session.get_verification()

    def validated(args):
        verification.mark_tests(v, True)
        verification.mark_diff_reviewed(v)
        return "all passed"

    monkeypatch.setattr(streaming, "get_tool_map", lambda: {"run_tests": SimpleNamespace(invoke=validated)})
    streaming._execute_tool({"name": "run_tests", "args": {"path": "."}, "id": "tests"}, HeadlessUI())
    assert verification.get_verification_gaps(v) == []


def test_agent_cannot_silently_finish_after_command_write(project_dir, monkeypatch):
    from langchain_core.messages import AIMessage, SystemMessage
    from src import agent
    from src.subagent import HeadlessUI

    sess = session.current_session()
    sess.project = str(project_dir)
    sess.current_model_index = 0
    sess.chat_history = [SystemMessage(content="offline")]
    state.ui_ref = HeadlessUI()
    monkeypatch.setattr(agent, "MODEL_LIST", [("offline", "cloud", "offline", False)])
    monkeypatch.setattr(agent, "save_session", lambda: None)
    monkeypatch.setattr(agent, "maybe_generate_session_title", lambda: None)
    count = 0

    def stream(ui):
        nonlocal count
        count += 1
        calls = [{"name": "run_command", "args": {"command": python_command(
            "from pathlib import Path; Path('app.py').write_text('value = 2\\n')")}, "id": "write"}] if count == 1 else []
        text = "" if calls else "已完成。"
        return text, calls, {"input": 0, "output": 0, "total": 0}, AIMessage(content=text, tool_calls=calls)

    monkeypatch.setattr(agent, "_stream_with_tools", stream)
    ui = HeadlessUI()
    agent.agent_loop(ui)
    assert count == 3
    assert "验证仍未完整完成" in ui.text()
    assert "app.py" in sess.verification["code_dirty_files"]
