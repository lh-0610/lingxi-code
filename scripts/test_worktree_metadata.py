"""真实 Git 回归：隔离区的创建基点、原始 checkout 和失效元数据保护。"""

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from src import worktree as wt


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "皓 梁")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "ll1816606771@gmail.com")
    wt._WORKTREES.clear()
    yield
    wt._WORKTREES.clear()


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=True,
        text=True, encoding="utf-8",
    ).stdout.strip()


def commit(repo, message):
    git(repo, "add", "--all")
    git(repo, "-c", "commit.gpgSign=false", "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    git(project, "init", "-b", "main")
    for name in ("agent.txt", "user.txt", "删除.txt"):
        (project / name).write_text(f"original {name}\n", encoding="utf-8")
    commit(project, "baseline")
    return project


def create(project, sid="child"):
    session = SimpleNamespace(worktree=None)
    path = wt.create(session, str(project), session_id=sid)
    assert path is not None
    return session, Path(path)


def index_path(project):
    return Path(os.path.realpath(project / git(project, "rev-parse", "--git-path", "index")))


def metadata_path(path):
    return Path(git(path, "rev-parse", "--absolute-git-dir")) / wt._METADATA_FILE


@pytest.mark.parametrize("child_commits", [False, True])
def test_moving_head_preserves_independent_user_commit(repo, child_commits):
    base = git(repo, "rev-parse", "HEAD")
    session, path = create(repo)
    (repo / "user.txt").write_text("important new user content\n", encoding="utf-8")
    (repo / "new-user.txt").write_text("new user file\n", encoding="utf-8")
    commit(repo, "user advances HEAD")
    head = git(repo, "rev-parse", "HEAD")
    (path / "agent.txt").write_text("agent improvement\n", encoding="utf-8")
    if child_commits:
        commit(path, "child commits its work")
    assert wt._WORKTREES["child"]["base_sha"] == base
    assert wt.changed_files(str(path)) == ["agent.txt"]
    before_index = index_path(repo).read_bytes()

    ok, msg = wt.finish(session, apply_changes=True)

    assert ok, msg
    assert (repo / "user.txt").read_text() == "important new user content\n"
    assert (repo / "new-user.txt").read_text() == "new user file\n"
    assert (repo / "agent.txt").read_text() == "agent improvement\n"
    assert index_path(repo).read_bytes() == before_index
    assert git(repo, "rev-parse", "HEAD") == head
    assert not path.exists()


@pytest.mark.parametrize("restore", ["direct", "finish", "create"])
def test_linked_target_is_not_main_checkout(repo, tmp_path, restore):
    linked = tmp_path / "selected-project"
    git(repo, "worktree", "add", "-b", "selected", str(linked), "HEAD")
    (linked / "user.txt").write_text("linked branch baseline\n", encoding="utf-8")
    commit(linked, "selected project diverges")
    base = git(linked, "rev-parse", "HEAD")
    session, path = create(linked)
    metadata = json.loads(metadata_path(path).read_text())
    assert metadata["project_path"] == str(linked.resolve())
    assert metadata["base_sha"] == base
    assert metadata == wt._WORKTREES["child"]
    (path / "agent.txt").write_text("only selected checkout\n", encoding="utf-8")
    commit(path, "committed child modification")
    (linked / "new-user.txt").write_text("user advances linked HEAD\n", encoding="utf-8")
    commit(linked, "advance linked checkout")
    (repo / "user.txt").write_text("staged main user edit\n", encoding="utf-8")
    git(repo, "add", "--", "user.txt")
    (linked / "staged.txt").write_text("staged selected user edit\n", encoding="utf-8")
    git(linked, "add", "--", "staged.txt")
    if restore != "direct":
        wt._WORKTREES.clear()
    if restore == "create":
        session.worktree = None
        assert wt.create(session, str(linked), "child") == str(path)
        assert wt._WORKTREES["child"] == metadata
    main_index = index_path(repo).read_bytes()
    linked_index = index_path(linked).read_bytes()
    main_files = {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}
    main_head = git(repo, "rev-parse", "HEAD")

    ok, msg = wt.finish(session, apply_changes=True)

    assert ok, msg
    assert (linked / "agent.txt").read_text() == "only selected checkout\n"
    assert (linked / "user.txt").read_text() == "linked branch baseline\n"
    assert (linked / "new-user.txt").read_text() == "user advances linked HEAD\n"
    assert {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()} == main_files
    assert index_path(repo).read_bytes() == main_index
    assert index_path(linked).read_bytes() == linked_index
    assert git(repo, "rev-parse", "HEAD") == main_head
    assert str(path) not in git(repo, "worktree", "list", "--porcelain")


def test_changed_files_net_committed_uncommitted_untracked_and_deleted(repo):
    session, path = create(repo)
    (path / "agent.txt").write_text("committed then reverted\n", encoding="utf-8")
    (path / "committed.txt").write_text("child commit\n", encoding="utf-8")
    commit(path, "committed child changes")
    (path / "agent.txt").write_text("original agent.txt\n", encoding="utf-8")
    (path / "user.txt").write_text("staged child edit\n", encoding="utf-8")
    git(path, "add", "--", "user.txt")
    (path / "删除.txt").unlink()
    (path / " 中文 新文件.txt").write_text("untracked\n", encoding="utf-8")
    before_index = index_path(path).read_bytes()
    wt._WORKTREES.clear()

    assert set(wt.changed_files(str(path))) == {"committed.txt", "user.txt", "删除.txt", " 中文 新文件.txt"}
    assert index_path(path).read_bytes() == before_index
    ok, msg = wt.finish(session, apply_changes=True)
    assert ok, msg
    assert (repo / "agent.txt").read_text() == "original agent.txt\n"
    assert (repo / "committed.txt").read_text() == "child commit\n"
    assert not (repo / "删除.txt").exists()
    assert (repo / " 中文 新文件.txt").read_text() == "untracked\n"


@pytest.mark.parametrize("damage", ["missing", "json", "base", "target", "identity", "registry"])
def test_invalid_metadata_fails_closed_and_allows_explicit_discard(repo, damage):
    session, path = create(repo)
    (path / "precious.txt").write_text("must not be deleted\n", encoding="utf-8")
    meta_path = metadata_path(path)
    meta = json.loads(meta_path.read_text())
    if damage == "missing":
        meta_path.unlink()
    elif damage == "json":
        meta_path.write_text("{invalid", encoding="utf-8")
    elif damage == "registry":
        wt._WORKTREES["child"]["base_sha"] = "0" * 40
    else:
        key, value = {
            "base": ("base_sha", "0" * 40),
            "target": ("project_path", str(path)),
            "identity": ("project_git_dir", str(repo / "wrong-admin")),
        }[damage]
        meta[key] = value
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    before_index = index_path(repo).read_bytes()
    with pytest.raises(wt.WorktreeMetadataError, match="元数据"):
        wt.changed_files(str(path))
    ok, msg = wt.finish(session, apply_changes=True)
    assert not ok and "拒绝自动合并" in msg
    wt.cleanup_all()
    assert "child" in wt._WORKTREES
    assert path.is_dir() and session.worktree == str(path)
    if damage == "registry":
        # 注册表损坏本身不能被重复 create 静默掩盖。
        assert wt.create(session, str(repo), "child") is None
    else:
        wt._WORKTREES.clear()
        assert wt.create(session, str(repo), "child") is None
    assert (path / "precious.txt").read_text() == "must not be deleted\n"
    assert not (repo / "precious.txt").exists()
    assert index_path(repo).read_bytes() == before_index
    ok, msg = wt.finish(session, apply_changes=False)
    assert ok, msg
    assert not path.exists() and session.worktree is None
    assert str(path) not in git(repo, "worktree", "list", "--porcelain")


def test_create_pins_the_resolved_sha_even_if_head_moves(repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    original_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[:3] == ["git", "worktree", "add"]:
            (repo / "new-user.txt").write_text("concurrent user commit\n", encoding="utf-8")
            commit(repo, "HEAD moves before worktree add")
            assert command[-1] == base
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "run", run)
    _, path = create(repo)
    assert git(path, "rev-parse", "HEAD") == base
    assert wt._WORKTREES["child"]["base_sha"] == base
    assert not (path / "new-user.txt").exists()


def test_changed_files_git_failure_is_not_empty(repo, monkeypatch):
    _, path = create(repo)
    original_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[:3] == ["git", "add", "-A"]:
            raise subprocess.CalledProcessError(1, command, stderr=b"index write failed")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(wt.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        wt.changed_files(str(path))
    assert path.is_dir()
