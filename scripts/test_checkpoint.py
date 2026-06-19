"""checkpoint 的真实 git 仓库回归测试。"""
import shutil
import subprocess
from datetime import datetime

import pytest

import src.checkpoint as checkpoint


pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git 未安装")


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "LingXi Tests")
    tracked = repo / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    checkpoint.clear_all_checkpoints()
    checkpoint._is_git_cache.clear()
    yield repo
    checkpoint.clear_all_checkpoints()
    checkpoint._is_git_cache.clear()


class TestCheckpointUndo:
    def test_clean_tracked_file_restores_head(self, git_repo):
        path = git_repo / "tracked.txt"
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, message = checkpoint.undo_last_checkpoint()

        assert ref.startswith("__HEAD__")
        assert ok is True
        assert "恢复到 HEAD" in message
        assert path.read_text(encoding="utf-8") == "original\n"

    def test_new_file_is_removed(self, git_repo):
        path = git_repo / "created.txt"
        checkpoint.make_checkpoint(str(git_repo), "write_file", str(path))
        path.write_text("created by ai\n", encoding="utf-8")

        ok, message = checkpoint.undo_last_checkpoint()

        assert ok is True
        assert "撤销新建文件" in message
        assert not path.exists()

    def test_dirty_tracked_file_restores_pre_ai_content(self, git_repo):
        path = git_repo / "tracked.txt"
        path.write_text("user edit\n", encoding="utf-8")
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, _ = checkpoint.undo_last_checkpoint()

        assert ref and not ref.startswith("stash@{")
        assert ok is True
        assert path.read_text(encoding="utf-8") == "user edit\n"

    def test_untracked_file_restores_pre_ai_content(self, git_repo):
        path = git_repo / "draft.txt"
        path.write_text("user draft\n", encoding="utf-8")
        ref = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, _ = checkpoint.undo_last_checkpoint()

        assert ref and not ref.startswith("stash@{")
        assert ok is True
        assert path.read_text(encoding="utf-8") == "user draft\n"

    def test_same_second_checkpoints_restore_in_order(self, git_repo, monkeypatch):
        path = git_repo / "tracked.txt"
        path.write_text("user edit\n", encoding="utf-8")

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 10, 12, 0, 0, 123456, tzinfo=tz)

        monkeypatch.setattr(checkpoint, "datetime", FixedDatetime)

        ref1 = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit 1\n", encoding="utf-8")
        ref2 = checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit 2\n", encoding="utf-8")

        ok2, _ = checkpoint.undo_last_checkpoint()
        assert ok2 is True
        assert path.read_text(encoding="utf-8") == "ai edit 1\n"

        ok1, _ = checkpoint.undo_last_checkpoint()
        assert ok1 is True
        assert path.read_text(encoding="utf-8") == "user edit\n"
        assert ref1 != ref2

    def test_new_file_outside_project_is_not_deleted(self, git_repo, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        checkpoint._push_stack(
            str(git_repo), "__HEAD__:00:00:00", "write_file", str(outside),
            existed=False, tracked=False,
        )

        ok, message = checkpoint.undo_last_checkpoint()

        assert ok is False
        assert "超出项目范围" in message
        assert outside.read_text(encoding="utf-8") == "keep me\n"

    def test_failed_undo_keeps_checkpoint_for_retry(self, git_repo, tmp_path):
        """撤销失败时快照必须保留在栈上，让用户能再次重试（修‘pop 后失败丢状态’）。"""
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        checkpoint._push_stack(
            str(git_repo), "__HEAD__:00:00:00", "write_file", str(outside),
            existed=False, tracked=False,
        )
        assert checkpoint.has_undoable_checkpoint() is True

        ok, _ = checkpoint.undo_last_checkpoint()
        assert ok is False
        # 关键：失败后快照仍在，可重试
        assert checkpoint.has_undoable_checkpoint() is True

    def test_successful_undo_consumes_checkpoint(self, git_repo):
        """撤销成功后才弹栈：成功一次后栈应为空。"""
        path = git_repo / "tracked.txt"
        checkpoint.make_checkpoint(str(git_repo), "edit_file", str(path))
        path.write_text("ai edit\n", encoding="utf-8")

        ok, _ = checkpoint.undo_last_checkpoint()
        assert ok is True
        assert checkpoint.has_undoable_checkpoint() is False
