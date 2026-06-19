"""
tests/test_worktree.py — src/worktree.py 的单元测试

使用真实临时 git 仓库测试 git worktree 创建/回收，
mock 掉 PySide6 / Qt 依赖（测试环境无 GUI）。
"""

import os
import sys
import shutil
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 把项目根加入 sys.path ──
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


# ── fixture: 创建一个临时 git 仓库（含至少一次 commit） ──
@pytest.fixture
def git_repo(tmp_path):
    """创建一个临时 git 仓库，返回其 Path。"""
    repo = tmp_path / "project"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init"], cwd=str(repo), check=True,
                   capture_output=True, env=env)
    # 禁用 GPG 签名（CI 环境可能没有 key）
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=str(repo),
                   check=True, capture_output=True)
    # 需要至少一个 commit 才能 worktree
    readme = repo / "README.md"
    readme.write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True,
                   capture_output=True, env=env)
    return repo


@pytest.fixture
def non_git_dir(tmp_path):
    """一个普通非 git 目录。"""
    d = tmp_path / "nongit"
    d.mkdir()
    return d


# ── 清理 _WORKTREES 全局状态 ──
@pytest.fixture(autouse=True)
def clean_worktrees():
    """每个测试前后确保 _WORKTREES 字典干净。"""
    import src.worktree as wt
    wt._WORKTREES.clear()
    yield
    # 尝试清理残留 worktree（避免 git 目录锁）
    for sid, info in list(wt._WORKTREES.items()):
        try:
            wt._cleanup_worktree(info["path"])
        except Exception:
            pass
    wt._WORKTREES.clear()


# ═══════════════════════════════════════════════
# is_git_repo
# ═══════════════════════════════════════════════
class TestIsGitRepo:
    def test_git_repo_true(self, git_repo):
        from src.worktree import is_git_repo
        assert is_git_repo(git_repo) is True

    def test_non_git_dir_false(self, non_git_dir):
        from src.worktree import is_git_repo
        assert is_git_repo(non_git_dir) is False

    def test_nonexistent_path_false(self, tmp_path):
        from src.worktree import is_git_repo
        assert is_git_repo(tmp_path / "nonexistent") is False

    def test_string_path(self, git_repo):
        from src.worktree import is_git_repo
        assert is_git_repo(str(git_repo)) is True


# ═══════════════════════════════════════════════
# _sanitize_branch
# ═══════════════════════════════════════════════
class TestSanitizeBranch:
    def test_simple(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("abc-123")
        assert result == "abc-123"

    def test_special_chars(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("hello world!@#$%")
        # 空格和特殊字符应被替换为 -
        assert " " not in result
        assert "!" not in result
        assert result.startswith("session-")

    def test_leading_dash_stripped(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("---abc")
        assert not result.startswith("-")

    def test_max_length(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("a" * 200)
        assert len(result) <= 100

    def test_empty_fallback(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("")
        assert result  # 不应为空

    def test_only_special_chars(self):
        from src.worktree import _sanitize_branch
        result = _sanitize_branch("!@#$%^&*()")
        assert result  # 不应为空，应有 fallback


# ═══════════════════════════════════════════════
# _is_within
# ═══════════════════════════════════════════════
class TestIsWithin:
    def test_within(self, tmp_path):
        from src.worktree import _is_within
        parent = tmp_path / "parent"
        child = parent / "sub" / "file.txt"
        assert _is_within(child, parent) is True

    def test_equal(self, tmp_path):
        from src.worktree import _is_within
        p = tmp_path / "dir"
        assert _is_within(p, p) is True

    def test_outside(self, tmp_path):
        from src.worktree import _is_within
        a = tmp_path / "a"
        b = tmp_path / "b"
        assert _is_within(b, a) is False

    def test_dotdot_escape(self, tmp_path):
        from src.worktree import _is_within
        parent = tmp_path / "parent"
        parent.mkdir()
        escape = parent / ".." / "other"
        assert _is_within(escape, parent) is False


# ═══════════════════════════════════════════════
# create（核心功能）
# ═══════════════════════════════════════════════
class TestCreate:
    def test_basic_create(self, git_repo):
        """创建 worktree，返回路径确实在磁盘上。"""
        from src.worktree import create, _WORKTREES
        session = MagicMock()
        session.worktree = None
        path = create(session, git_repo, session_id="s1")
        assert path is not None
        assert os.path.isdir(path)
        assert "s1" in _WORKTREES
        # worktree 是一个 git 仓库（有自己的 .git 文件）
        assert os.path.exists(os.path.join(path, "README.md"))

    def test_create_sets_session_worktree(self, git_repo):
        """create 应设置 session.worktree。"""
        from src.worktree import create
        session = MagicMock()
        session.worktree = None
        path = create(session, git_repo, session_id="s2")
        assert session.worktree == path

    def test_non_git_returns_none(self, non_git_dir):
        """非 git 仓库应返回 None，不崩溃。"""
        from src.worktree import create, _WORKTREES
        session = MagicMock()
        session.worktree = None
        result = create(session, non_git_dir, session_id="s3")
        assert result is None
        assert "s3" not in _WORKTREES

    def test_has_uncommitted_changes_detects_dirty_repo(self, git_repo):
        from src.worktree import has_uncommitted_changes
        assert has_uncommitted_changes(str(git_repo)) is False
        (git_repo / "dirty.txt").write_text("x", encoding="utf-8")
        assert has_uncommitted_changes(str(git_repo)) is True

    def test_idempotent_existing(self, git_repo):
        """同一 session 重复 create 应复用现有 worktree。"""
        from src.worktree import create, _WORKTREES
        session = MagicMock()
        session.worktree = None
        p1 = create(session, git_repo, session_id="s4")
        p2 = create(session, git_repo, session_id="s4")
        assert p1 == p2
        assert len(_WORKTREES) == 1

    def test_reuses_existing_disk_worktree_after_registry_loss(self, git_repo):
        """模拟重启：内存注册表没了，但磁盘上的同名 worktree 应复用。"""
        from src.worktree import create, _WORKTREES
        session = MagicMock()
        session.worktree = None
        p1 = create(session, git_repo, session_id="s_reuse")
        assert p1 is not None

        _WORKTREES.clear()
        session.worktree = None
        p2 = create(session, git_repo, session_id="s_reuse")

        assert p2 == p1
        assert session.worktree == p1
        assert _WORKTREES["s_reuse"]["path"] == p1

    def test_two_sessions_different_worktrees(self, git_repo):
        """不同 session 应得到不同 worktree 路径。"""
        from src.worktree import create
        s1 = MagicMock(); s1.worktree = None
        s2 = MagicMock(); s2.worktree = None
        p1 = create(s1, git_repo, session_id="s5")
        p2 = create(s2, git_repo, session_id="s6")
        assert p1 != p2
        assert os.path.isdir(p1)
        assert os.path.isdir(p2)

    def test_lock_prevents_concurrent_create(self, git_repo):
        """并发 create 不会冲突。"""
        from src.worktree import create
        results = {}
        errors = []

        def worker(sid):
            try:
                s = MagicMock(); s.worktree = None
                p = create(s, git_repo, session_id=sid)
                results[sid] = p
            except Exception as e:
                errors.append((sid, e))

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 5
        # 所有路径都不相同
        paths = list(results.values())
        assert len(set(paths)) == 5

    def test_create_does_not_pollute_main_git_status(self, git_repo):
        """创建 worktree 后，.lingxi-worktrees/ 不应作为未跟踪文件出现在主项目 git status，
        has_uncommitted_changes 也不应因此误报（走 .git/info/exclude 本地忽略）。"""
        from src.worktree import create, has_uncommitted_changes
        session = MagicMock()
        session.worktree = None
        create(session, git_repo, session_id="s_exclude")

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert ".lingxi-worktrees" not in status.stdout, \
            f"隔离目录污染了主项目 status: {status.stdout!r}"
        assert has_uncommitted_changes(str(git_repo)) is False
        # info/exclude 里确实写了规则
        exclude = git_repo / ".git" / "info" / "exclude"
        assert ".lingxi-worktrees/" in exclude.read_text(encoding="utf-8")

    def test_exclude_not_duplicated_on_repeated_create(self, git_repo):
        """多次创建（不同 session）不应在 info/exclude 里重复写同一行。"""
        from src.worktree import create
        for i in range(3):
            s = MagicMock(); s.worktree = None
            create(s, git_repo, session_id=f"dup{i}")
        exclude = (git_repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert exclude.count(".lingxi-worktrees/") == 1

    def test_commit_files_to_worktree_visible_in_main(self, git_repo):
        """
        隔离验证：在 worktree 里创建文件并 commit，
        主仓库 git log 能看到新 commit；worktree 文件不出现在主仓库工作目录。
        """
        from src.worktree import create, _WORKTREES
        session = MagicMock()
        session.worktree = None
        wt_path = create(session, git_repo, session_id="s_commit")

        # 在 worktree 里创建一个新文件
        new_file = os.path.join(wt_path, "worktree_only.txt")
        with open(new_file, "w", encoding="utf-8") as f:
            f.write("created in worktree\n")

        # 在 worktree 里 commit
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "add", "."], cwd=wt_path, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "wt commit"], cwd=wt_path,
                       check=True, capture_output=True, env=env)

        info = _WORKTREES["s_commit"]
        branch = info["branch"]
        result = subprocess.run(
            ["git", "log", "--oneline", branch],
            cwd=str(git_repo), capture_output=True, text=True
        )
        assert "wt commit" in result.stdout

        # 主仓库工作目录没有 worktree_only.txt
        assert not os.path.exists(str(git_repo / "worktree_only.txt"))

        # worktree 工作目录有它
        assert os.path.exists(new_file)


# ═══════════════════════════════════════════════
# finish
# ═══════════════════════════════════════════════
class TestFinish:
    def test_finish_removes_worktree(self, git_repo):
        from src.worktree import create, finish, _WORKTREES
        session = MagicMock()
        session.worktree = None
        path = create(session, git_repo, session_id="f1")
        assert os.path.isdir(path)
        finish(session)
        assert not os.path.isdir(path)
        assert "f1" not in _WORKTREES
        assert session.worktree is None

    def test_finish_no_worktree_noop(self):
        """session 没有 worktree 时 finish 不报错。"""
        from src.worktree import finish
        session = MagicMock()
        session.worktree = None
        finish(session)  # 不应抛异常

    def test_finish_nonexistent_path(self, git_repo):
        """worktree 路径已被手动删了，finish 也不应崩溃。"""
        from src.worktree import create, finish, _WORKTREES
        session = MagicMock()
        session.worktree = None
        path = create(session, git_repo, session_id="f2")
        # 手动删掉目录
        shutil.rmtree(path, ignore_errors=True)
        finish(session)  # 不应抛异常
        assert "f2" not in _WORKTREES

    def test_finish_apply_changes_restores_to_main_project(self, git_repo):
        """恢复隔离模式时，worktree 的改动应应用回主项目工作区。"""
        from src.worktree import create, finish
        session = MagicMock()
        session.worktree = None
        wt_path = create(session, git_repo, session_id="f_apply")

        tracked_file = os.path.join(wt_path, "README.md")
        with open(tracked_file, "w", encoding="utf-8") as f:
            f.write("from isolated worktree\n")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert session.worktree is None
        assert not os.path.isdir(wt_path)
        assert (git_repo / "README.md").read_text(encoding="utf-8") == "from isolated worktree\n"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "README.md"],
            cwd=str(git_repo), capture_output=True, text=True
        )
        assert status.stdout.rstrip("\r\n") == " M README.md"

    def test_finish_loaded_session_without_registry_removes_git_metadata(self, git_repo):
        """重启后只有 worktree 路径、注册表为空时，finish 也应走 git worktree remove。"""
        from src.worktree import create, finish, _WORKTREES
        session = MagicMock()
        session.worktree = None
        wt_path = create(session, git_repo, session_id="f_loaded")
        _WORKTREES.clear()

        ok, msg = finish(session, apply_changes=False)
        assert ok, msg
        assert session.worktree is None
        assert not os.path.isdir(wt_path)

        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(git_repo), capture_output=True, text=True
        )
        assert wt_path not in result.stdout


# ═══════════════════════════════════════════════
# cleanup_all
# ═══════════════════════════════════════════════
class TestCleanupAll:
    def test_cleanup_all(self, git_repo):
        from src.worktree import create, cleanup_all, _WORKTREES
        paths = []
        for i in range(3):
            s = MagicMock(); s.worktree = None
            p = create(s, git_repo, session_id=f"c{i}")
            paths.append(p)
        assert len(_WORKTREES) == 3
        cleanup_all()
        assert len(_WORKTREES) == 0
        for p in paths:
            assert not os.path.isdir(p)

    def test_cleanup_all_empty(self):
        """没有 worktree 时 cleanup_all 不报错。"""
        from src.worktree import cleanup_all
        cleanup_all()  # 不应抛异常


# ═══════════════════════════════════════════════
# _apply_changes_to_project 合并边界（隔离区改动回主项目）
# ═══════════════════════════════════════════════
class TestApplyChangesEdgeCases:
    """worktree.finish(apply_changes=True) 把隔离区改动合回主项目的各种边界。

    这些是真实使用里高频但单测没覆盖的合并场景：删除 / 二进制 / 中文名 / 嵌套新目录 /
    主项目有无关脏文件 / 同文件冲突回滚。AI 在隔离区改完后『恢复隔离改动』走的就是这条路。
    """

    @staticmethod
    def _commit(repo, *, msg="c"):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=str(repo), check=True,
                       capture_output=True, env=env)

    def _wt(self, git_repo, sid):
        from src.worktree import create
        session = MagicMock()
        session.worktree = None
        wt_path = create(session, git_repo, session_id=sid)
        assert wt_path is not None
        return session, wt_path

    def test_apply_file_deletion(self, git_repo):
        """隔离区删掉一个被跟踪文件 → 合并后主项目里该文件也消失。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "del")
        os.remove(os.path.join(wt_path, "README.md"))

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert not (git_repo / "README.md").exists()

    def test_apply_cjk_filename(self, git_repo):
        """隔离区新建中文名文件 → 合并后主项目正确出现（quotepath=false 回归）。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "cjk")
        (Path(wt_path) / "数据文件.txt").write_text("中文内容\n", encoding="utf-8")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "数据文件.txt").read_text(encoding="utf-8") == "中文内容\n"

    def test_apply_binary_file(self, git_repo):
        """隔离区新建二进制文件 → 合并后字节完全一致（--binary 路径）。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "bin")
        blob = bytes(range(256)) * 4  # 含 \x00 等非文本字节
        (Path(wt_path) / "data.bin").write_bytes(blob)

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "data.bin").read_bytes() == blob

    def test_apply_new_nested_dir(self, git_repo):
        """隔离区在新建的多层子目录里加文件 → 合并后目录与文件都出现在主项目。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "nested")
        nested = Path(wt_path) / "pkg" / "sub"
        nested.mkdir(parents=True)
        (nested / "mod.py").write_text("X = 1\n", encoding="utf-8")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == "X = 1\n"

    def test_apply_preserves_unrelated_dirty_file(self, git_repo):
        """主项目里有『无关文件』的未提交改动 → 合并隔离区不应把它冲掉。

        真实场景：用户正在主项目手改 keep.txt，AI 子 Agent 合并它改的 README，
        用户没存的 keep.txt 改动必须保住。
        """
        from src.worktree import finish
        # keep.txt 必须在 create 之前提交，worktree 才能从含它的 HEAD 分叉
        (git_repo / "keep.txt").write_text("v1\n", encoding="utf-8")
        self._commit(git_repo, msg="add keep")

        session, wt_path = self._wt(git_repo, "unrelated")
        # 主项目：手改 keep.txt（未提交脏）
        (git_repo / "keep.txt").write_text("用户正在编辑的内容\n", encoding="utf-8")
        # 隔离区：改 README.md
        (Path(wt_path) / "README.md").write_text("wt edit\n", encoding="utf-8")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "README.md").read_text(encoding="utf-8") == "wt edit\n"
        # 关键：用户对 keep.txt 的未提交改动被保住
        assert (git_repo / "keep.txt").read_text(encoding="utf-8") == "用户正在编辑的内容\n"

    def test_apply_committed_changes_in_worktree(self, git_repo):
        """AI 在隔离区里 git_commit 后再恢复 → 提交过的改动必须合并回主项目（防静默丢失）。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "committed")
        (Path(wt_path) / "feature.py").write_text("print(1)\n", encoding="utf-8")
        # 模拟 AI 调 git_commit 在隔离区提交
        self._commit(wt_path, msg="add feature")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "feature.py").read_text(encoding="utf-8") == "print(1)\n"

    def test_apply_committed_plus_uncommitted(self, git_repo):
        """隔离区里既有已提交、又有提交后的未提交改动 → 两者都要合并回主项目。"""
        from src.worktree import finish
        session, wt_path = self._wt(git_repo, "mixed")
        (Path(wt_path) / "committed.py").write_text("a = 1\n", encoding="utf-8")
        self._commit(wt_path, msg="commit part")
        # 提交后再改：已提交文件追加 + 新未提交文件
        (Path(wt_path) / "committed.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (Path(wt_path) / "uncommitted.py").write_text("c = 3\n", encoding="utf-8")

        ok, msg = finish(session, apply_changes=True)
        assert ok, msg
        assert (git_repo / "committed.py").read_text(encoding="utf-8") == "a = 1\nb = 2\n"
        assert (git_repo / "uncommitted.py").read_text(encoding="utf-8") == "c = 3\n"

    def test_apply_conflict_rolls_back_main_cleanly(self, git_repo):
        """主项目与隔离区改了同一文件的同一处 → 合并冲突，主项目内容还原干净、worktree 保留。"""
        from src.worktree import finish, _WORKTREES
        session, wt_path = self._wt(git_repo, "conflict")
        # 主项目脏改 README
        (git_repo / "README.md").write_text("MAIN-EDIT\n", encoding="utf-8")
        # 隔离区也改 README 的同一处（不同内容）
        (Path(wt_path) / "README.md").write_text("WT-EDIT\n", encoding="utf-8")

        ok, msg = finish(session, apply_changes=True)
        assert ok is False
        # 主项目 README 必须还是用户的脏改，没被污染成冲突标记 / WT-EDIT
        assert (git_repo / "README.md").read_text(encoding="utf-8") == "MAIN-EDIT\n"
        # 冲突时 worktree 保留，session.worktree 不清，注册表回填
        assert os.path.isdir(wt_path)
        assert session.worktree == wt_path
        assert "conflict" in _WORKTREES


# ═══════════════════════════════════════════════
# tools.py _project_cwd 路由
# ═══════════════════════════════════════════════
class TestProjectCwdRouting:
    """验证 _project_cwd 在有 worktree 时路由到隔离目录。"""

    def test_routes_to_worktree(self, git_repo, monkeypatch):
        """当前线程会话有 worktree 时，_project_cwd 返回 worktree 路径。"""
        import src.state as state
        import src.session as session_mod
        from src.worktree import create

        # 创建一个 worktree
        sess = session_mod.Session()
        wt_path = create(sess, git_repo, session_id="cwd1")
        session_mod.set_active(sess)
        monkeypatch.setattr(state, "current_project", str(git_repo))

        from src.tools import _project_cwd
        result = _project_cwd()
        assert result == wt_path

    def test_routes_to_project_when_no_worktree(self, git_repo, monkeypatch):
        """无 worktree 时 _project_cwd 返回 current_project。"""
        import src.state as state
        import src.session as session_mod

        sess = session_mod.Session()
        session_mod.set_active(sess)
        monkeypatch.setattr(state, "current_project", str(git_repo))

        from src.tools import _project_cwd
        result = _project_cwd()
        assert result == str(git_repo)

    def test_routes_to_project_when_worktree_path_missing(self, git_repo, monkeypatch):
        """worktree 路径失效时 _project_cwd 回退到 current_project。"""
        import src.state as state
        import src.session as session_mod

        sess = session_mod.Session()
        sess.worktree = str(git_repo / "missing-worktree")
        session_mod.set_active(sess)
        monkeypatch.setattr(state, "current_project", str(git_repo))

        from src.tools import _project_cwd
        result = _project_cwd()
        assert result == str(git_repo)
