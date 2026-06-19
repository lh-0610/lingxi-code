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
