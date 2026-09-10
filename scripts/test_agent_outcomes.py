"""Offline terminal outcomes, including real child loops and worktree merges."""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
from dataclasses import FrozenInstanceError

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from src.agent_result import AgentResult


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("Network is disabled for agent outcome tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    from src import agent, config, paths, session, state, subagent, worktree
    paths.set_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(agent, "save_session", lambda: None)
    monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", False)
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(subagent, "get_system_prompt", lambda: "Offline task")
    monkeypatch.setattr(state, "ui_ref", None)
    parent = session.Session()
    parent.current_model_index = next(
        i for i, m in enumerate(agent.MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    parent.is_subagent = True
    parent.project = str(tmp_path)
    parent.chat_history = [SystemMessage(content="Offline"), HumanMessage(content="Do the task")]
    session.bind_thread(parent)
    yield agent, parent, subagent.HeadlessUI()
    worktree.cleanup_all()
    session.unbind_thread()
    paths.set_data_dir(None)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=root, check=True)
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True, env=env)
    return root


def model_stream(monkeypatch, agent, rounds):
    """Leave streaming/parser/tool execution intact; replace only the model."""
    class Model:
        calls = 0

        def stream(self, messages):
            step = rounds[self.calls]
            self.calls += 1
            if callable(step):
                step = step()
            if isinstance(step, Exception):
                # A partial chunk avoids retry backoff, as with an interrupted SDK stream.
                yield AIMessageChunk(content="partial")
                raise step
            yield from step

    model = Model()
    monkeypatch.setattr(agent, "resolve_bound_llm", lambda sess: (model, model))
    return model


def write_chunk():
    return AIMessageChunk(content="", tool_calls=[{
        "name": "write_file", "args": {"path": "app.py", "content": "value = 2\n"}, "id": "write",
    }])


def test_result_is_immutable():
    with pytest.raises(FrozenInstanceError):
        AgentResult("completed").status = "failed"


def test_real_child_model_failure_keeps_partial_worktree(runtime, repo, monkeypatch):
    from src import subagent, worktree
    agent, parent, _ = runtime
    model = model_stream(monkeypatch, agent, [[write_chunk()], RuntimeError("SDK failed after write")])
    result = subagent.spawn(["Change app.py and verify"], str(repo))[0]
    assert model.calls == 2
    assert result["status"] == "failed"
    assert "SDK failed after write" in result["detail"]
    assert result["merge"] == "skipped"
    assert (repo / "app.py").read_text() == "value = 1\n"
    assert parent.verification["dirty_files"] == []
    paths = [info["path"] for info in worktree._WORKTREES.values()]
    assert len(paths) == 1
    assert Path(paths[0], "app.py").read_text() == "value = 2\n"
    assert paths[0] in result["detail"]


def test_real_child_unverified_does_not_merge(runtime, repo, monkeypatch):
    from src import subagent, worktree
    agent, parent, _ = runtime
    model = model_stream(monkeypatch, agent, [
        [write_chunk()], [AIMessageChunk(content="Done")], [AIMessageChunk(content="Cannot verify")],
    ])
    result = subagent.spawn(["Change app.py"], str(repo))[0]
    assert model.calls == 3
    assert result["status"] == "unverified"
    assert "测试" in result["reason"]
    assert result["merge"] == "skipped"
    assert (repo / "app.py").read_text() == "value = 1\n"
    assert worktree._WORKTREES


@pytest.mark.parametrize("committed", [False, True])
def test_real_child_success_invalidates_parent_validation(runtime, repo, monkeypatch, committed):
    from src import session, subagent, verification
    agent, parent, _ = runtime
    verification.mark_tests(parent.verification, True)
    verification.mark_diff_reviewed(parent.verification)
    verification.mark_check(parent.verification, "app.py", True)

    def verified_finish():
        child = session.current_session()
        # Test evidence is synthetic; the real loop must consult this child's gate.
        verification.mark_tests(child.verification, True)
        verification.mark_diff_reviewed(child.verification)
        if committed:
            subprocess.run(["git", "add", "app.py"], cwd=child.worktree, check=True)
            subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                            "commit", "-qm", "child"], cwd=child.worktree, check=True)
        return [AIMessageChunk(content="Verified")]

    model_stream(monkeypatch, agent, [[write_chunk()], verified_finish])
    result = subagent.spawn(["Change app.py and verify"], str(repo))[0]
    assert result["status"] == "completed", result
    assert result["merge"] == "ok", result
    assert result["files_changed"] == ["app.py"]
    assert (repo / "app.py").read_text() == "value = 2\n"
    v = parent.verification
    assert v["dirty_files"] == ["app.py"]
    assert v["code_dirty_files"] == ["app.py"]
    assert not v["tests_run"] and v["tests_passed"] is None
    assert not v["diff_reviewed"] and "app.py" not in v["checks"]


@pytest.mark.parametrize("outcome", [
    None, "completed", AgentResult("unknown", "unknown status"),
    AgentResult("cancelled", "stopped"), AgentResult("unverified", "no tests"),
    AgentResult("limit_reached", "too many rounds"), AgentResult("failed", "model failed"),
])
def test_noncompletion_never_merges(runtime, repo, monkeypatch, outcome):
    from src import session, subagent
    child_paths = []

    def fake_loop(ui):
        path = session.current_session().worktree
        child_paths.append(path)
        Path(path, "app.py").write_text("partial\n")
        return outcome

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
    result = subagent.spawn(["task"], str(repo))[0]
    assert result["merge"] == "skipped"
    assert result["status"] != "completed"
    assert "保留 worktree" in result["detail"]
    assert Path(child_paths[0]).is_dir()
    assert (repo / "app.py").read_text() == "value = 1\n"


def test_parent_stop_prevents_completed_child_merge(runtime, repo, monkeypatch):
    from src import session, subagent
    _, parent, _ = runtime

    def fake_loop(ui):
        Path(session.current_session().worktree, "app.py").write_text("partial\n")
        parent.stop_flag = True
        return AgentResult("completed")

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
    result = subagent.spawn(["task"], str(repo))[0]
    assert result["status"] == "cancelled"
    assert result["merge"] == "skipped"
    assert (repo / "app.py").read_text() == "value = 1\n"


def test_change_list_failure_keeps_worktree(runtime, repo, monkeypatch):
    from src import subagent, worktree
    monkeypatch.setattr(subagent, "_run_agent_loop", lambda ui: AgentResult("completed"))

    def unreadable(path):
        raise OSError("base metadata unavailable")

    monkeypatch.setattr(worktree, "changed_files", unreadable)
    result = subagent.spawn(["task"], str(repo))[0]
    assert result["status"] == "completed"
    assert result["merge"] == "skipped"
    assert "base metadata unavailable" in result["detail"]
    assert "保留 worktree" in result["detail"]
    assert worktree._WORKTREES


@pytest.mark.parametrize("chunks", [[], [AIMessageChunk(content="<think>only thinking</think>")]])
def test_empty_response_fails(runtime, monkeypatch, chunks):
    agent, _, ui = runtime
    model_stream(monkeypatch, agent, [chunks])
    assert agent.agent_loop(ui).status == "failed"


def test_stream_stop_is_cancelled(runtime, monkeypatch):
    agent, parent, ui = runtime

    def stopped():
        parent.stop_flag = True
        return [AIMessageChunk(content="partial")]

    model_stream(monkeypatch, agent, [stopped])
    assert agent.agent_loop(ui).status == "cancelled"


def test_gate_error_is_unverified(runtime, monkeypatch):
    from src import verification
    agent, _, ui = runtime
    model_stream(monkeypatch, agent, [[AIMessageChunk(content="Done")]])

    def broken(v):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(verification, "get_verification_gaps", broken)
    result = agent.agent_loop(ui)
    assert result.status == "unverified"
    assert "gate unavailable" in result.reason


@pytest.mark.parametrize("events,code,status", [
    ([], 0, "failed"),
    ([{"type": "user", "message": {"content": [{"type": "tool_result", "content": "partial tool output"}]}}], 0, "failed"),
    ([{"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}], 0, "failed"),
    ([{"type": "result", "subtype": "success", "is_error": False}], 1, "failed"),
    ([{"type": "result", "subtype": "success", "is_error": False}], 0, "unverified"),
    ([{"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["SDK failed"]}], 0, "failed"),
    ([{"type": "result", "subtype": "error_max_turns", "is_error": True}], 0, "limit_reached"),
])
def test_claude_cli_outcome_requires_evidence(runtime, monkeypatch, events, code, status):
    from src import claude_code
    _, _, ui = runtime

    class Process:
        stdin = io.StringIO()
        stdout = io.StringIO("\n".join(json.dumps(e) for e in events))
        returncode = code

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(claude_code.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(claude_code, "get_external_agent_context", lambda: "")
    monkeypatch.setattr(claude_code, "save_session", lambda: None)
    assert claude_code.claude_code_loop(ui).status == status


def test_claude_cli_missing_command_fails(runtime, monkeypatch):
    from src import claude_code
    _, _, ui = runtime

    def missing(*a, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(claude_code.subprocess, "Popen", missing)
    monkeypatch.setattr(claude_code, "get_external_agent_context", lambda: "")
    assert claude_code.claude_code_loop(ui).status == "failed"


def test_verification_gate_can_recover_to_completed(runtime, monkeypatch):
    from src import verification
    agent, parent, ui = runtime

    def dirty_reply():
        verification.mark_dirty(parent.verification, "app.py")
        return [AIMessageChunk(content="Done")]

    def verified_reply():
        verification.mark_tests(parent.verification, True)
        verification.mark_diff_reviewed(parent.verification)
        return [AIMessageChunk(content="Verified")]

    model = model_stream(monkeypatch, agent, [dirty_reply, verified_reply])
    assert agent.agent_loop(ui).status == "completed"
    assert model.calls == 2


@pytest.mark.parametrize("mode", ["stopped", "rag", "ollama", "cli_none"])
def test_early_exits_have_explicit_outcomes(runtime, monkeypatch, mode):
    agent, parent, ui = runtime
    if mode == "stopped":
        parent.stop_flag = True
    elif mode in {"rag", "cli_none"}:
        parent.current_model_index = next(i for i, m in enumerate(agent.MODEL_LIST) if m[1] == "claude-code")
        parent.rag_mode = mode == "rag"
        monkeypatch.setattr(agent, "_claude_code_loop", lambda ui: None)
    else:
        parent.current_model_index = next(i for i, m in enumerate(agent.MODEL_LIST) if m[1] == "ollama")
        monkeypatch.setattr(agent, "check_ollama", lambda: False)
    result = agent.agent_loop(ui)
    assert result.status == ("cancelled" if mode == "stopped" else "failed")
    assert result.reason


def test_claude_cli_stop_prevents_launch(runtime, monkeypatch):
    from src import claude_code
    _, parent, ui = runtime
    parent.stop_flag = True

    def unexpected(*a, **kw):
        pytest.fail("Cancelled Claude CLI must not launch")

    monkeypatch.setattr(claude_code.subprocess, "Popen", unexpected)
    assert claude_code.claude_code_loop(ui).status == "cancelled"


def test_merge_cleanup_failure_invalidates_parent_validation(runtime, repo, monkeypatch):
    from src import session, subagent, verification, worktree
    _, parent, _ = runtime
    verification.mark_tests(parent.verification, True)
    verification.mark_diff_reviewed(parent.verification)

    def completed(ui):
        Path(session.current_session().worktree, "app.py").write_text("value = 2\n")
        return AgentResult("completed")

    def cleanup_failed(child, *, apply_changes):
        assert apply_changes
        (repo / "app.py").write_text(Path(child.worktree, "app.py").read_text())
        return False, "清理隔离区失败"

    monkeypatch.setattr(subagent, "_run_agent_loop", completed)
    monkeypatch.setattr(worktree, "finish", cleanup_failed)
    result = subagent.spawn(["task"], str(repo))[0]
    assert result["status"] == "completed"
    assert result["merge"] == "conflict"
    assert "清理隔离区失败" in result["detail"]
    assert "保留 worktree" in result["detail"]
    assert parent.verification["dirty_files"] == ["app.py"]
    assert parent.verification["tests_passed"] is None
    assert not parent.verification["diff_reviewed"]
