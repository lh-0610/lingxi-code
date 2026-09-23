"""B04 第三轮复核（b9f95ec）：覆盖判定不能跨过 Git 工作区的边界。

`verification.covers()` 早先只看路径包含关系，而灵犀的隔离区就放在主项目的
`.lingxi-worktrees/` 下面——路径上"被包含"，却是另一份文件现场。复核用真实 worktree、
真实测试和 diff 复现：隔离区里改了 app.py 后中断，重开回到主项目，只跑主项目的测试、
看主项目的 diff，返回 completed，隔离区里的改动从没被检查过；同一个 worktree 放在项目外
就正确返回 unverified。子模块、独立的嵌套仓库是同一类边界：共享（或不共享）Git 历史都不要紧，
不是同一份文件现场，主项目的测试和 diff 就替它背不了书。
"""
import os

import pytest
from langchain_core.messages import HumanMessage

from src import agent, run_records, session, state, verification, worktree

from test_b04_recovery import _UI, _bind, _code_session, _git, _make_repo, _reopen
from test_b04_review_fixes import _write_passing_test
from test_b04_second_review import _ChecksThenDone


@pytest.fixture()
def repo(tmp_path):
    return _make_repo(tmp_path / "proj")


@pytest.fixture(autouse=True)
def _no_real_models_or_services(monkeypatch):
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    monkeypatch.setattr(agent, "maybe_generate_session_title", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_post_run_notify", lambda *a, **k: None)


def _add_worktree(repo, tmp_path, nested):
    """用灵犀自己的排除机制 + 真实 `git worktree add` 建一个隔离区。"""
    worktree._ensure_worktree_excluded(str(repo))
    wt = repo / ".lingxi-worktrees" / "review-wt" if nested else tmp_path / "outside-wt"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    return wt


def _interrupted_in_worktree(repo, wt):
    """隔离区里的命令改了 app.py，结果还没交回去就中断了（sidecar 有记录、没有回执）。"""
    sess = _code_session(repo)
    sess.worktree = str(wt)
    _bind(sess)
    try:
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="run_command", tool_call_id="pending", args={})
        (wt / "app.py").write_text("x = 99\n", encoding="utf-8")
    finally:
        session.unbind_thread()
    return sess.current_session_id


class TestChecksDoNotCrossWorkTreeBoundaries:
    @pytest.mark.parametrize("nested", [True, False])
    def test_main_checkout_checks_do_not_cover_the_interrupted_worktree(
            self, isolated_memory, repo, tmp_path, monkeypatch, nested):
        """复核原场景：只在主项目跑测试、看 diff，不能了结隔离区里的义务——放在项目里也一样。"""
        _write_passing_test(repo)
        wt = _add_worktree(repo, tmp_path, nested)
        sid = _interrupted_in_worktree(repo, wt)
        again = _reopen(sid)
        assert again.worktree is None, "隔离区使用权不跨进程恢复"
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        monkeypatch.setattr(agent, "_stream_with_tools", _ChecksThenDone())
        try:
            result = agent.agent_loop(_UI(), resume={"expected_run_id": again.last_run["id"],
                                                     "notes": []})
        finally:
            session.unbind_thread()

        assert _git(wt, "diff", "--", "app.py"), "改动只在隔离区里"
        assert _git(repo, "diff") == "", "主项目的 diff 看不见它"
        assert result.status == "unverified"
        gaps = "\n".join(verification.gaps_from_state(again.verification))
        assert os.path.realpath(wt) in gaps, "要点名那个隔离区"
        assert "另一个 Git 工作区" in gaps

    def test_tests_run_inside_the_worktree_do_cover_it(self, isolated_memory, repo, tmp_path,
                                                       monkeypatch):
        """对照：cd 进隔离区补跑测试，测试这一项就了结了；diff 仍按提示保持未验证。"""
        from src.tools import run_command, run_tests
        from src.tools_git import git_diff
        from src import recovery
        _write_passing_test(repo)
        wt = _add_worktree(repo, tmp_path, nested=True)
        sid = _interrupted_in_worktree(repo, wt)
        monkeypatch.setattr(state, "ui_ref", None)
        again = _reopen(sid)
        _bind(again)
        try:
            assert recovery.prepare_and_apply(again, mode="continue", ui=_UI(),
                                              expected_run_id=again.last_run["id"])
            run_records.begin_run(again)
            run_tests.func()
            git_diff.func()
            before = "\n".join(verification.get_verification_gaps(again.verification))
            assert "测试是在" in before and os.path.realpath(wt) in before
            run_command.func(command=f"cd {wt}")
            run_tests.func()                        # run_tests 跟随 cd，在隔离区里跑
            after = "\n".join(verification.get_verification_gaps(again.verification))
            assert "测试是在" not in after, "在隔离区里跑过测试，这一项已覆盖"
            assert "git_diff 查看的是" in after, "diff 还是在主项目看的，照实说"
        finally:
            session.unbind_thread()

    def test_an_edit_inside_a_nested_repository_is_not_covered_by_the_parent(
            self, isolated_memory, repo, tmp_path, monkeypatch):
        """子模块 / 嵌套仓库：真实写文件落在里面，主项目的测试和 diff 都替它背不了书。"""
        from src.tools import run_command, run_tests, write_file
        from src.tools_git import git_diff
        _write_passing_test(repo)
        nested = _make_repo(repo / "vendor" / "lib")
        # 测试文件别和主项目同名：主项目跑 pytest 会递归收集到这里，同名模块会冲突报错
        (nested / "test_nested_lib.py").write_text("def test_ok():\n    assert True\n",
                                                   encoding="utf-8")
        _git(nested, "add", "-A")
        _git(nested, "commit", "-q", "-m", "nested test")
        monkeypatch.setattr(state, "ui_ref", None)
        sess = _code_session(repo)
        _bind(sess)
        try:
            run_records.begin_run(sess)
            write_file.func(path="vendor/lib/app.py", content="x = 7\n")
            run_tests.func()
            git_diff.func()
            gaps = "\n".join(verification.get_verification_gaps(sess.verification))
            assert "没有覆盖" in gaps and "vendor" in gaps
            run_command.func(command=f"cd {nested}")
            run_tests.func()
            after = "\n".join(verification.get_verification_gaps(sess.verification))
            assert "测试是在" not in after, "在嵌套仓库里跑的测试覆盖它自己的文件"
        finally:
            session.unbind_thread()


class TestCoversUnit:
    def _repo_with_nested(self, tmp_path):
        repo = _make_repo(tmp_path / "proj")
        wt = _add_worktree(repo, tmp_path, nested=True)
        nested = _make_repo(repo / "vendor" / "lib")
        return repo, wt, nested

    def test_same_work_tree_is_covered(self, tmp_path):
        repo, _wt, _nested = self._repo_with_nested(tmp_path)
        (repo / "pkg").mkdir()
        assert verification.covers(str(repo), str(repo / "pkg" / "a.py"))
        assert verification.covers(str(repo / "pkg"), str(repo / "pkg" / "a.py"))
        assert not verification.covers(str(repo / "pkg"), str(repo / "b.py")), "子目录不覆盖上级"

    def test_a_linked_worktree_under_the_project_is_its_own_boundary(self, tmp_path):
        repo, wt, _nested = self._repo_with_nested(tmp_path)
        assert (wt / ".git").is_file(), "linked worktree 的 .git 是文件"
        assert not verification.covers(str(repo), str(wt / "app.py"))
        assert not verification.covers(str(repo), str(wt))
        assert verification.covers(str(wt), str(wt / "app.py"))

    def test_a_nested_independent_repository_is_its_own_boundary(self, tmp_path):
        repo, _wt, nested = self._repo_with_nested(tmp_path)
        assert (nested / ".git").is_dir()
        assert not verification.covers(str(repo), str(nested / "app.py"))
        assert verification.covers(str(nested), str(nested / "app.py"))

    def test_a_deleted_file_is_still_placed_in_its_work_tree(self, tmp_path):
        repo, wt, _nested = self._repo_with_nested(tmp_path)
        gone = wt / "gone" / "deleted.py"            # 目录和文件都不存在
        assert not verification.covers(str(repo), str(gone))
        assert verification.covers(str(wt), str(gone))

    def test_a_path_limited_check_inside_a_worktree(self, tmp_path):
        repo, wt, _nested = self._repo_with_nested(tmp_path)
        assert verification.covers(str(wt / "app.py"), str(wt / "app.py"))
        assert not verification.covers(str(repo / "app.py"), str(wt / "app.py"))

    def test_plain_directories_fall_back_to_path_containment(self, tmp_path):
        plain = tmp_path / "plain"
        (plain / "sub").mkdir(parents=True)
        assert verification.covers(str(plain), str(plain / "sub" / "a.txt"))
        assert not verification.covers(str(plain / "sub"), str(plain / "a.txt"))

    def test_a_plain_parent_does_not_cover_a_repository_inside_it(self, tmp_path):
        """在一个普通目录里跑的检查，替它下面的某个 Git 仓库背不了书。"""
        plain = tmp_path / "plain"
        plain.mkdir()
        inner = _make_repo(plain / "inner")
        assert not verification.covers(str(plain), str(inner / "app.py"))


def test_gap_computation_reuses_work_tree_lookups_within_one_call(tmp_path, monkeypatch):
    """同一次核对里每个路径只查一次所属工作区（逐级 stat 在 Windows 上不便宜）。"""
    repo = _make_repo(tmp_path / "proj")
    v = verification.new_verification()
    for i in range(20):
        verification.mark_dirty(v, f"f{i}.py", abs_path=str(repo / f"f{i}.py"))
    verification.mark_tests(v, True, root=str(repo))
    verification.mark_diff_reviewed(v, root=str(repo))
    calls = []
    real = verification._find_work_tree_root
    monkeypatch.setattr(verification, "_find_work_tree_root",
                        lambda p: (calls.append(p), real(p))[1])
    assert verification.gaps_from_state(v) == []
    assert len(calls) == len({os.path.normcase(os.path.normpath(p)) for p in calls}), "同一路径查了不止一次"
    assert len(calls) <= 21, f"20 个文件 + 1 个检查目录，却查了 {len(calls)} 次"
