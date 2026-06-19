import os
import sys
import subprocess
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True, env=env)
    return repo


@pytest.fixture(autouse=True)
def clean_worktrees():
    import src.worktree as wt
    wt.cleanup_all()
    yield
    wt.cleanup_all()


def _task_text():
    import src.session as session
    return session.current_session().chat_history[-1].content


class TestHeadlessUI:
    def test_capture_and_confirm(self):
        from src.subagent import HeadlessUI

        ui = HeadlessUI(label="t")
        ui.show_message("hello", "ai_msg")
        ui.render_final_markdown(" world")

        assert ui.confirm_command("anything") == (True, "")
        assert ui.confirm_edit("a.py", "diff") == (True, "")
        assert "hello" in ui.text()
        assert "world" in ui.text()


class TestSubagentSpawn:
    def test_subagent_blocks_outside_paths(self, tmp_path):
        import src.session as session
        import src.tools as tools

        prev = session.get_active()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        child = session.Session()
        child.is_subagent = True
        child.project = str(tmp_path)
        child.worktree = str(worktree)
        session.set_active(child)
        try:
            outside = tmp_path / "outside.txt"
            result = tools.write_file.func(str(outside), "x")
            assert "worktree" in result or "子 Agent" in result
            assert not outside.exists()

            inside = tools.write_file.func("inside.txt", "ok")
            assert "成功" in inside
            assert (worktree / "inside.txt").read_text(encoding="utf-8") == "ok"
        finally:
            session.set_active(prev)

    def test_subagent_blocks_drive_relative_path(self, tmp_path):
        """Windows 盘符相对绝对路径(\\Windows\\...)会逃出 worktree，沙箱必须拦下。"""
        import src.session as session
        import src.tools as tools

        prev = session.get_active()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        child = session.Session()
        child.is_subagent = True
        child.project = str(tmp_path)
        child.worktree = str(worktree)
        session.set_active(child)
        try:
            rej = tools._subagent_command_rejection(r"type \Windows\System32\drivers\etc\hosts")
            assert rej and "worktree 外路径" in rej
            # worktree 内的相对命令不应被误拦
            assert tools._subagent_command_rejection("python build.py") == ""
        finally:
            session.set_active(prev)

    def test_subagent_blocks_outside_commands(self, tmp_path):
        import src.session as session
        import src.tools as tools

        prev = session.get_active()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        child = session.Session()
        child.is_subagent = True
        child.project = str(tmp_path)
        child.worktree = str(worktree)
        session.set_active(child)
        try:
            outside = tmp_path / "outside.txt"
            cmd = f'python -c "print(r\"{outside}\")"'
            result = tools.run_command.func(cmd)
            assert "worktree" in result or "子 Agent" in result
        finally:
            session.set_active(prev)

    def test_spawn_two_tasks_merges_different_files(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.tools as tools
        import src.session as session

        def fake_loop(ui):
            cwd = Path(tools._project_cwd())
            task = _task_text()
            name = "a.py" if "a.py" in task else "b.py"
            (cwd / name).write_text(f"created by {name}\n", encoding="utf-8")
            session.current_session().chat_history.append(AIMessage(content=f"done {name}"))

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)

        results = subagent.spawn(["edit a.py", "edit b.py"], str(git_repo), None)

        assert [r["merge"] for r in results] == ["ok", "ok"]
        assert (git_repo / "a.py").read_text(encoding="utf-8") == "created by a.py\n"
        assert (git_repo / "b.py").read_text(encoding="utf-8") == "created by b.py\n"
        assert results[0]["summary"].startswith("done")

    def test_spawn_subagent_that_commits_merges_back(self, git_repo, monkeypatch):
        """子 Agent 在自己 worktree 里 git_commit 后，提交的改动也要合并回主项目（防静默丢失）。"""
        import src.subagent as subagent
        import src.tools as tools
        import src.session as session

        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

        def fake_loop(ui):
            cwd = Path(tools._project_cwd())
            (cwd / "feat.py").write_text("print('feat')\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "subagent commit"], cwd=str(cwd),
                           check=True, capture_output=True, env=env)
            session.current_session().chat_history.append(AIMessage(content="done"))

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        results = subagent.spawn(["build feat"], str(git_repo), None)

        assert results[0]["merge"] == "ok", results[0]
        assert (git_repo / "feat.py").read_text(encoding="utf-8") == "print('feat')\n"

    def test_spawn_restores_parent_thread_binding(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.session as session

        parent = session.Session()
        session.bind_thread(parent)

        def fake_loop(ui):
            session.current_session().chat_history.append(AIMessage(content="done"))

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        try:
            subagent.spawn(["edit a.py"], str(git_repo), None)
            assert session.current_session() is parent
        finally:
            session.unbind_thread()

    def test_spawn_agents_tool_rejects_recursive_child(self, git_repo, monkeypatch):
        import src.session as session
        import src.state as state
        import src.tools as tools

        child = session.Session()
        child.is_subagent = True
        child.project = str(git_repo)
        session.set_active(child)
        monkeypatch.setattr(state, "current_project", str(git_repo))

        assert "不能再派生" in tools.spawn_agents.invoke({"tasks": ["x"]})

    def test_max_concurrent_is_four(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.session as session

        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_loop(ui):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.15)
            session.current_session().chat_history.append(AIMessage(content="done"))
            with lock:
                active -= 1

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        results = subagent.spawn([f"task {i}" for i in range(7)], str(git_repo), None)

        assert max_active <= 4
        assert len(results) == 7

    def test_conflict_keeps_second_worktree(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.tools as tools
        import src.session as session

        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        (git_repo / "same.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "same.txt"], cwd=str(git_repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(git_repo), check=True, capture_output=True, env=env)

        def fake_loop(ui):
            cwd = Path(tools._project_cwd())
            task = _task_text()
            (cwd / "same.txt").write_text(("one\n" if "one" in task else "two\n"), encoding="utf-8")
            session.current_session().chat_history.append(AIMessage(content=task))

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        results = subagent.spawn(["write one", "write two"], str(git_repo), None)

        assert results[0]["merge"] == "ok"
        assert results[1]["merge"] == "conflict"
        assert "保留 worktree" in results[1]["detail"]
        assert (git_repo / "same.txt").read_text(encoding="utf-8") == "one\n"

    def test_timeout_does_not_hang(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.session as session

        def fake_loop(ui):
            while not session.current_session().stop_flag:
                time.sleep(0.05)

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        monkeypatch.setattr(subagent, "_TIMEOUT_SECONDS", 0.2)

        results = subagent.spawn(["hang"], str(git_repo), None)
        assert results[0]["merge"] == "timeout"

    def test_non_git_rejected(self, tmp_path, monkeypatch):
        import src.subagent as subagent

        d = tmp_path / "plain"
        d.mkdir()
        monkeypatch.setattr(subagent.worktree, "is_git_repo", lambda _path: False)
        results = subagent.spawn(["x"], str(d), None)
        assert "需 git 仓库" in results[0]["summary"]

    def test_parallel_sessions_route_to_own_worktrees(self, git_repo, monkeypatch):
        import src.subagent as subagent
        import src.tools as tools
        import src.session as session

        seen = {}
        lock = threading.Lock()

        def fake_loop(ui):
            cwd = tools._project_cwd()
            task = _task_text()
            with lock:
                seen[task] = cwd
            Path(cwd, f"{task}.txt").write_text(task, encoding="utf-8")
            session.current_session().chat_history.append(AIMessage(content=task))

        monkeypatch.setattr(subagent, "_run_agent_loop", fake_loop)
        subagent.spawn(["x", "y", "z"], str(git_repo), None)

        assert len(seen) == 3
        assert len(set(seen.values())) == 3
        assert all(".lingxi-worktrees" in p for p in seen.values())
