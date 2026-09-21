"""安全 Git 工作流工具测试：git_status / git_stage / git_unstage / git_commit。

同时保留 git_diff / git_log 已有测试。
需要 git 才有意义，没装则整文件跳过。
"""
import subprocess
import shutil

import pytest

from src.tools import (
    git_diff, git_log, git_status, git_stage, git_unstage, git_commit,
    build_git_write_confirmation, ALL_TOOLS, TOOL_DISPLAY_NAMES,
)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git 未安装")


@pytest.fixture(autouse=True)
def project_dir(tmp_path, monkeypatch):
    """切换工作目录和 state.current_project 到临时目录，测试完还原。"""
    from src import state
    from src import session as _session

    old_project = state.current_project
    _sess = _session.get_active()
    old_sess_proj = _sess.project

    monkeypatch.chdir(tmp_path)
    state.current_project = str(tmp_path)
    _sess.project = str(tmp_path)

    yield tmp_path

    state.current_project = old_project
    _sess.project = old_sess_proj


# ── 工具注册测试 ──────────────────────────────────

class TestToolRegistration:
    """13. 工具注册完整：ALL_TOOLS / TOOL_MAP / TOOL_DISPLAY_NAMES。"""

    def test_new_git_tools_in_all_tools(self):
        names = {t.name for t in ALL_TOOLS}
        assert "git_status" in names
        assert "git_stage" in names
        assert "git_unstage" in names
        assert "git_commit" in names

    def test_new_git_tools_in_display_names(self):
        assert "git_status" in TOOL_DISPLAY_NAMES
        assert "git_stage" in TOOL_DISPLAY_NAMES
        assert "git_unstage" in TOOL_DISPLAY_NAMES
        assert "git_commit" in TOOL_DISPLAY_NAMES

    def test_plan_mode_whitelist(self):
        """14. git_status 在 PLAN_MODE_READONLY_TOOLS / PARALLEL_SAFE_TOOLS / NO_ARG_OK_TOOLS。"""
        from src.streaming import PLAN_MODE_READONLY_TOOLS, PARALLEL_SAFE_TOOLS, NO_ARG_OK_TOOLS
        assert "git_status" in PLAN_MODE_READONLY_TOOLS
        assert "git_status" in PARALLEL_SAFE_TOOLS
        assert "git_status" in NO_ARG_OK_TOOLS

    def test_write_tools_not_in_plan_readonly(self):
        """15. git_stage / git_unstage / git_commit 不在 Plan 只读白名单。"""
        from src.streaming import PLAN_MODE_READONLY_TOOLS
        assert "git_stage" not in PLAN_MODE_READONLY_TOOLS
        assert "git_unstage" not in PLAN_MODE_READONLY_TOOLS
        assert "git_commit" not in PLAN_MODE_READONLY_TOOLS


# ── git_diff / git_log 已有测试（保留）──────────────────────────────

class TestGitDiffLog:
    """原有测试：非 git 仓库降级 + 路径逃逸防护。"""

    def test_diff_not_a_repo(self, project_dir):
        assert "不是 git 仓库" in git_diff.func("")

    def test_log_not_a_repo(self, project_dir):
        assert "不是 git 仓库" in git_log.func("")

    def test_diff_path_escape_rejected(self, project_dir):
        assert "不允许" in git_diff.func("../")

    def test_log_path_escape_rejected(self, project_dir):
        assert "不允许" in git_log.func("../")


# ── 辅助：在临时目录里初始化 git 仓库 ──────────────────────

@pytest.fixture()
def git_repo(tmp_path):
    """创建一个已初始化的 git 仓库（含初始提交 + user.name/email），注入 state。"""
    from src import state
    from src import session as _session

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True,
                   capture_output=True, encoding="utf-8")
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"],
                   cwd=str(repo), check=True, capture_output=True)
    # 创建初始提交，确保 HEAD 存在（git_status 依赖 git diff-index HEAD）
    (repo / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), check=True,
                   capture_output=True)

    old_project = state.current_project
    old_ui = state.ui_ref
    state.current_project = str(repo)
    state.ui_ref = None
    _sess = _session.get_active()
    old_sess_proj = _sess.project
    _sess.project = str(repo)

    yield repo

    state.current_project = old_project
    state.ui_ref = old_ui
    _sess.project = old_sess_proj


# ── git_diff 与验证义务的接线（真实跑 git，不打桩）───────────────

class TestGitDiffMarksReviewed:
    """`git_diff` 什么时候算"审阅过"。

    这条线以前只有"直接调 mark_diff_reviewed"的测试，把真实工具分支整个绕过去了：
    空 diff 会提前 return，压根走不到标记那一步。于是"跑完 git_diff、结果确实是干净的"
    反而结不了验证义务——在没有 dirty 文件却仍要求审阅 diff 的场景（verification 的盲区）
    里，闸门就死在这儿。下面一律走真实的 `git_diff.func`。
    """

    def _fresh(self):
        from src import session as _session
        from src.verification import new_verification
        sess = _session.get_active()
        sess.verification = new_verification()
        return sess.verification

    def test_truly_clean_worktree_counts_as_reviewed(self, git_repo):
        """第一组：真正干净——没有未暂存改动、没有暂存、没有未跟踪。"""
        v = self._fresh()
        out = git_diff.func("")
        assert "工作区干净" in out
        assert v["diff_reviewed"] is True, "命令跑成功、整个项目确实干净，这就是一次完整审阅"

    def test_staged_only_does_not_count(self, git_repo):
        """第二组：只有已暂存的修改。

        默认 `git diff` 看不见它们——输出为空，但那份改动从头到尾没在任何 diff 里露过面。
        """
        v = self._fresh()
        (git_repo / "README.md").write_text("changed and staged", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(git_repo), check=True,
                       capture_output=True)

        out = git_diff.func("")
        assert v["diff_reviewed"] is False, "没看过的改动不能算审阅过"
        assert "工作区干净，没有未提交改动" not in out, "不能谎报整个项目干净"
        assert "已暂存的修改" in out and "README.md" in out
        assert "git_diff(staged=True)" in out, "要告诉模型去哪儿补看"

    def test_untracked_only_does_not_count(self, git_repo):
        """第三组：只有未跟踪的新文件。默认 diff 同样看不见。"""
        v = self._fresh()
        (git_repo / "new_module.py").write_text("value = 1\n", encoding="utf-8")

        out = git_diff.func("")
        assert v["diff_reviewed"] is False
        assert "工作区干净，没有未提交改动" not in out
        assert "未跟踪的新文件" in out and "new_module.py" in out
        assert "git_status" in out

    def test_gitignored_files_do_not_block_discharge(self, git_repo):
        """被 ignore 的构建产物/缓存不算"没审阅的改动"，否则义务又解不开了。"""
        v = self._fresh()
        (git_repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=str(git_repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignore build"], cwd=str(git_repo),
                       check=True, capture_output=True)
        (git_repo / "build").mkdir()
        (git_repo / "build" / "out.o").write_text("junk", encoding="utf-8")

        out = git_diff.func("")
        assert "工作区干净" in out
        assert v["diff_reviewed"] is True

    def test_probe_failure_keeps_the_obligation(self, git_repo, monkeypatch):
        """核对不了 = 不知道干不干净。不知道就不放行。"""
        from src import tools_git
        v = self._fresh()
        monkeypatch.setattr(tools_git, "_uncovered_changes",
                            lambda cwd: ([], [], "模拟核对失败"))
        out = git_diff.func("")
        assert v["diff_reviewed"] is False
        assert "无法核对" in out and "模拟核对失败" in out

    def test_non_empty_diff_counts_as_reviewed(self, git_repo):
        v = self._fresh()
        (git_repo / "README.md").write_text("changed", encoding="utf-8")
        out = git_diff.func("")
        assert "README.md" in out
        assert v["diff_reviewed"] is True

    def test_empty_scoped_diff_does_not_count(self, git_repo):
        """只看了一个文件、它没改动——说明不了别处，不能拿来结清整个工作区的义务。"""
        v = self._fresh()
        (git_repo / "other.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "other.py"], cwd=str(git_repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "add other"], cwd=str(git_repo), check=True,
                       capture_output=True)
        (git_repo / "README.md").write_text("changed but not looked at", encoding="utf-8")

        out = git_diff.func("other.py")
        assert "工作区干净" in out
        assert v["diff_reviewed"] is False

    def test_empty_staged_diff_does_not_count(self, git_repo):
        """暂存区空不代表工作区干净：改了没 add，这条命令根本看不见。"""
        v = self._fresh()
        (git_repo / "README.md").write_text("changed", encoding="utf-8")
        out = git_diff.func("", staged=True)
        assert "暂存区没有改动" in out
        assert v["diff_reviewed"] is False

    def test_failure_never_counts_as_reviewed(self, project_dir):
        """非 git 仓库 = 执行失败，不能放行。"""
        v = self._fresh()
        assert "不是 git 仓库" in git_diff.func("")
        assert v["diff_reviewed"] is False

    def test_path_escape_never_counts_as_reviewed(self, git_repo):
        v = self._fresh()
        assert "不允许" in git_diff.func("../")
        assert v["diff_reviewed"] is False


# ── git_status 测试 ──────────────────────────────────

class TestGitStatus:
    """1-2. git_status 在非 Git 仓库 / Git 仓库。"""

    def test_not_a_repo(self, project_dir):
        """1. 非 Git 仓库返回友好提示。"""
        result = git_status.func()
        assert "不是 git 仓库" in result

    def test_clean_repo(self, git_repo):
        """2a. 干净仓库。"""
        result = git_status.func()
        assert "仓库干净" in result or "工作区干净" in result or "无任何改动" in result
        assert "git_diff" in result  # 提示建议

    def test_repo_with_changes(self, git_repo):
        """2b. 有改动文件时输出分支和文件状态。"""
        (git_repo / "new.txt").write_text("hello", encoding="utf-8")
        result = git_status.func()
        assert "new.txt" in result
        assert "未跟踪" in result

    def test_no_git(self, project_dir):
        """git 不存在时。"""
        result = git_status.func()
        # project_dir 不是 git 仓库，应该返回"不是 git 仓库"
        assert "不是 git 仓库" in result


# ── git_stage 测试 ──────────────────────────────────

class TestGitStage:
    """3-7. git_stage 各种场景。"""

    def test_reject_empty_list(self, git_repo):
        """3. git_stage([]) 拒绝。"""
        result = git_stage.func([])
        assert "不能为空" in result

    def test_reject_dot(self, git_repo):
        """4. git_stage(["."]) 拒绝。"""
        result = git_stage.func(["."])
        assert "不允许" in result

    def test_reject_escape(self, git_repo):
        """5. git_stage(["../x"]) 拒绝越界。"""
        result = git_stage.func(["../x"])
        assert "超出项目范围" in result

    def test_reject_glob(self, git_repo):
        """拒绝通配符 *。"""
        result = git_stage.func(["*"])
        assert "不允许" in result

    def test_reject_shell_chars(self, git_repo):
        """拒绝 shell 注入。"""
        result = git_stage.func(["src && rm -rf"])
        assert "非法字符" in result

    def test_stage_single_file(self, git_repo):
        """6. git_stage(["file.txt"]) 成功暂存指定文件。"""
        f = git_repo / "file.txt"
        f.write_text("content", encoding="utf-8")
        result = git_stage.func(["file.txt"])
        assert "✅" in result
        assert "file.txt" in result
        assert "暂存区" in result

    def test_stage_dir_shows_files(self, git_repo):
        """7. git_stage(["dir"]) 成功后输出实际暂存文件列表。"""
        d = git_repo / "dir"
        d.mkdir()
        (d / "a.py").write_text("a", encoding="utf-8")
        (d / "b.py").write_text("b", encoding="utf-8")
        result = git_stage.func(["dir"])
        assert "✅" in result
        assert "dir/a.py" in result
        assert "dir/b.py" in result

    def test_stage_absolute_path(self, git_repo):
        """绝对路径在项目根内也应通过。"""
        f = git_repo / "abs.txt"
        f.write_text("abs", encoding="utf-8")
        result = git_stage.func([str(f)])
        assert "✅" in result
        assert "abs.txt" in result


# ── git_unstage 测试 ──────────────────────────────────

class TestGitUnstage:
    """8. git_unstage 只取消暂存，不改工作区。"""

    def test_unstage_preserves_workdir(self, git_repo):
        """8. 取消暂存后工作区内容不变。"""
        f = git_repo / "keep.txt"
        f.write_text("original", encoding="utf-8")
        git_stage.func(["keep.txt"])
        # 修改工作区文件
        f.write_text("modified", encoding="utf-8")
        # 暂存修改
        git_stage.func(["keep.txt"])
        # 取消暂存
        result = git_unstage.func(["keep.txt"])
        assert "✅" in result
        assert "工作区文件内容未被修改" in result
        # 工作区应仍是 modified
        assert f.read_text(encoding="utf-8") == "modified"

    def test_unstage_reject_empty(self, git_repo):
        result = git_unstage.func([])
        assert "不能为空" in result

    def test_unstage_reject_escape(self, git_repo):
        result = git_unstage.func(["../foo"])
        assert "超出项目范围" in result


# ── git_commit 测试 ──────────────────────────────────

class TestGitCommit:
    """9-12. git_commit 各种场景。"""

    def test_reject_empty_message(self, git_repo):
        """9. git_commit("") 拒绝空 message。"""
        result = git_commit.func("")
        assert "不能为空" in result

    def test_reject_whitespace_message(self, git_repo):
        """空白 message 拒绝。"""
        result = git_commit.func("  ")
        assert "不能为空" in result

    def test_reject_empty_staging(self, git_repo):
        """10. git_commit("msg") 在暂存区为空时拒绝。"""
        result = git_commit.func("init commit")
        assert "暂存区为空" in result

    def test_commit_success(self, git_repo):
        """11. git_commit("msg") 成功创建本地提交。"""
        f = git_repo / "hello.py"
        f.write_text("print('hello')", encoding="utf-8")
        git_stage.func(["hello.py"])
        result = git_commit.func("add hello.py")
        assert "✅ 提交成功" in result
        assert "add hello.py" in result
        assert "hello.py" in result
        assert "未执行 push" in result

    def test_commit_with_unstaged_files(self, git_repo):
        """12. 有未暂存文件时仍只提交暂存区，并提示未暂存文件。"""
        # 创建并暂存一个文件
        (git_repo / "a.txt").write_text("a", encoding="utf-8")
        git_stage.func(["a.txt"])
        # 再创建一个未暂存的文件
        (git_repo / "b.txt").write_text("b", encoding="utf-8")
        result = git_commit.func("commit a only")
        assert "✅ 提交成功" in result
        assert "b.txt" in result  # 提示 b.txt 未进入提交
        assert "不会进入本次提交" in result

    def test_commit_hash_in_output(self, git_repo):
        """提交后输出 commit hash。"""
        (git_repo / "f.txt").write_text("f", encoding="utf-8")
        git_stage.func(["f.txt"])
        result = git_commit.func("add f")
        # hash 应该是 7 字符短 hash
        import re
        assert re.search(r"提交成功: [0-9a-f]{7,}", result)


class TestGitWriteConfirmation:
    """Git 写工具必须经过执行器确认，不能只靠 prompt 约束模型。"""

    def test_rejected_stage_does_not_touch_index(self, git_repo):
        from unittest.mock import MagicMock
        from src import state
        from src.streaming import _execute_tool

        (git_repo / "blocked.txt").write_text("blocked", encoding="utf-8")
        ui = MagicMock()
        ui.confirm_command.return_value = (False, "")
        state.chat_history = []

        _execute_tool({
            "name": "git_stage",
            "args": {"paths": ["blocked.txt"]},
            "id": "stage-rejected",
        }, ui)

        ui.confirm_command.assert_called_once()
        status = subprocess.run(
            ["git", "status", "--short"], cwd=str(git_repo),
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
        assert "?? blocked.txt" in status
        assert "A  blocked.txt" not in status

    def test_allowed_stage_runs_after_confirmation(self, git_repo):
        from unittest.mock import MagicMock
        from src import state
        from src.streaming import _execute_tool

        (git_repo / "allowed.txt").write_text("allowed", encoding="utf-8")
        ui = MagicMock()
        ui.confirm_command.return_value = (True, "")
        state.chat_history = []

        _execute_tool({
            "name": "git_stage",
            "args": {"paths": ["allowed.txt"]},
            "id": "stage-allowed",
        }, ui)

        ui.confirm_command.assert_called_once()
        status = subprocess.run(
            ["git", "status", "--short"], cwd=str(git_repo),
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
        assert "A  allowed.txt" in status

    def test_commit_confirmation_lists_all_staged_files(self, git_repo):
        (git_repo / "user.txt").write_text("user change", encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", "user.txt"], cwd=str(git_repo),
            capture_output=True, text=True, encoding="utf-8", check=True,
        )

        text = build_git_write_confirmation(
            "git_commit", {"message": "reviewed commit"},
        )

        assert "reviewed commit" in text
        assert "user.txt" in text
        assert "当前暂存区的全部内容" in text


# ── 路径安全辅助测试 ──────────────────────────────────

class TestPathSafety:
    """额外的路径安全测试。"""

    def test_stage_rejects_nonexistent_path(self, git_repo):
        """不存在的路径也应拒绝（git add 本身会报错）。"""
        result = git_stage.func(["nonexistent.txt"])
        # git add 对不存在文件会返回错误
        assert "失败" in result or "出错" in result or "error" in result.lower() or "did not match" in result.lower()

    def test_validate_paths_rejects_non_list(self, git_repo):
        """paths 参数类型错误。"""
        result = git_stage.func("not_a_list")  # type: ignore
        assert "必须是字符串列表" in result
