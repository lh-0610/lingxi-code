"""B06 复核发现的六处问题（每条都走真实路径，不打桩绕过被测分支）。

共同主题：**结果卡上的结论不能比证据更乐观**，而且展示这件事本身不许改坏
会话路由、证据持久化和缓存归属这些底下的东西。
"""
import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import config, memory, result_view, run_records, session, tools, verification
from src.agent_result import AgentResult


def _new_session(**fields):
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="修一下登录"),
                         AIMessage(content="好")]
    for key, value in fields.items():
        setattr(sess, key, value)
    session.register(sess)
    return sess


def _saved(sess):
    memory.save_session(session=sess)
    return sess.current_session_id


def _finish(sess, status="completed", reason=""):
    return run_records.finalize_run(sess, sess.last_run, AgentResult(status, reason)).snapshot


# ── ① 看完 diff 不能留下线程绑定 ──

class TestThreadBindingNotLeaked:
    def test_view_diff_restores_the_original_binding(self, isolated_memory, tmp_path):
        """主线程原来没绑定，看完 diff 之后就该还是没绑定。

        用 `current_session()` 存旧值是错的——未绑定的主线程拿到的是 active，
        "还原"反而把主线程**永久绑死**在那个会话上；之后切会话，`get_active()` 是新的、
        `current_session()` 还是旧的，所有经会话代理的保存都会写错人。
        """
        from src.ui.chat_window import ChatUI
        sess_a = _new_session()
        _saved(sess_a)
        sess_b = _new_session()
        sess_b.chat_history[1] = HumanMessage(content="另一个任务")
        _saved(sess_b)

        session.set_active(sess_a)
        assert session.get_bound() is None, "前置条件：主线程未绑定"

        class _Host:
            _on_result_view_diff = ChatUI._on_result_view_diff

            def _show_toast(self, *a):
                pass

            def _show_text_dialog(self, *a):
                pass

        _Host()._on_result_view_diff({"work_dir": str(tmp_path), "project": str(tmp_path)})

        assert session.get_bound() is None, "看完 diff 之后主线程还绑着"
        session.set_active(sess_b)
        assert session.current_session() is sess_b, "切到 B 之后仍然路由到旧会话 A"

    def test_restore_bound_puts_an_existing_binding_back(self, isolated_memory):
        """worker 线程本来就绑着的话，借用之后要还回去，不是解绑。"""
        worker_sess = _new_session()
        session.bind_thread(worker_sess)
        try:
            previous = session.get_bound()
            session.bind_thread(_new_session())
            session.restore_bound(previous)
            assert session.current_session() is worker_sess
        finally:
            session.unbind_thread()


# ── ② mypy 致命退出不能记成通过 ──

@pytest.fixture()
def tool_session(isolated_memory, tmp_path):
    sess = _new_session()
    _saved(sess)
    sess.project = str(tmp_path)
    session.bind_thread(sess)
    session.set_active(sess)
    run_records.begin_run(sess)
    yield sess
    session.unbind_thread()


class TestCheckerFatalExit:
    def test_mypy_fatal_exit_is_error_not_passed(self, tool_session, tmp_path, monkeypatch):
        """筛完没剩下诊断 ≠ 检查跑成功了。

        mypy 退出码 ≥2 是致命错误（语法错、用法错），它根本没完成分析。
        记成 passed 会让卡片说"检查通过"，而文件其实连语法都不对。
        """
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        monkeypatch.setattr(config, "TYPE_CHECK_AFTER_EDIT", True)
        (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
        real_run = subprocess.run

        def _mypy_fatal(cmd, *a, **k):
            if isinstance(cmd, list) and any("mypy" in str(c) for c in cmd):
                return subprocess.CompletedProcess(
                    cmd, 2, stdout="broken.py:1: error: invalid syntax  [syntax]\n", stderr="")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(subprocess, "run", _mypy_fatal)
        tools.check_code.func("broken.py")

        by_checker = {r["checker"]: r for r in tool_session.verification["evidence"]}
        assert "mypy" in by_checker, "跑过就要留记录"
        mypy = by_checker["mypy"]
        assert mypy["status"] == "error", "退出码 2 不能记成 passed"
        assert mypy["exit_code"] == 2
        assert mypy["summary"], "错误摘要不能被丢掉"

    def test_filtered_out_low_signal_errors_still_count_as_passed(self, tool_session,
                                                                  tmp_path, monkeypatch):
        """退出码 1 + 高信号筛选后无命中 = 按我们的判据通过。这是有意的，别一起改掉。"""
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        monkeypatch.setattr(config, "TYPE_CHECK_AFTER_EDIT", True)
        (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
        real_run = subprocess.run

        def _mypy_low_signal(cmd, *a, **k):
            if isinstance(cmd, list) and any("mypy" in str(c) for c in cmd):
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="ok.py:1: error: x  [attr-defined]\n", stderr="")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(subprocess, "run", _mypy_low_signal)
        tools.check_code.func("ok.py")

        mypy = {r["checker"]: r for r in tool_session.verification["evidence"]}.get("mypy")
        assert mypy is not None and mypy["status"] == "passed"
        assert mypy["exit_code"] == 1, "退出码照实记，用户能自己看出是被筛掉的"


# ── ③ 一条通过不能代表整轮通过 ──

class TestCheckSummary:
    def test_one_pass_plus_one_failure_is_not_a_pass(self, isolated_memory):
        """静态检查过了、测试挂了——标题不能概括成"已执行检查通过"。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="check", checker="ruff",
                                     status="passed", exit_code=0)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1)
        snapshot = _finish(sess, "completed")

        assert result_view.summarize_checks(snapshot) == "unresolved"
        assert result_view.has_fresh_pass(snapshot) is False
        assert result_view.describe(snapshot)["title"] == "本轮执行结束，检查未全部通过"

    @pytest.mark.parametrize("statuses, expect", [
        ([("passed", False), ("passed", False)], "clear"),
        ([("passed", False), ("failed", False)], "unresolved"),
        ([("passed", False), ("not_run", False)], "unresolved"),
        ([("passed", False), ("timeout", False)], "unresolved"),
        ([("passed", False), ("unknown", False)], "unresolved"),
        # 只剩过期记录：跑过，但结论已经不代表当前代码，不能算通过
        ([("passed", True)], "unresolved"),
        # 过期的失败被之后一次新鲜的通过取代 → 算通过
        ([("failed", True), ("passed", False)], "clear"),
        ([], "none"),
    ])
    def test_summary_rules(self, isolated_memory, statuses, expect):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        for status, make_stale in statuses:
            verification.record_evidence(sess.verification, kind="check", checker="c",
                                         status=status, exit_code=0)
            if make_stale:
                verification.mark_dirty(sess.verification, "src/after.py")
        assert result_view.summarize_checks(_finish(sess, "completed")) == expect

    def test_all_passed_still_says_checks_passed(self, isolated_memory):
        """别把正向情形一起改坏了。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        for checker in ("ruff", "pytest"):
            verification.record_evidence(sess.verification, kind="check", checker=checker,
                                         status="passed", exit_code=0)
        view = result_view.describe(_finish(sess, "completed"))
        assert view["title"] == "本轮执行结束，已执行检查通过"


# ── ④ 隔离区里跑的轮次要记实际工作目录 ──

class TestWorkDir:
    def test_worktree_run_records_its_real_working_directory(self, isolated_memory,
                                                             tmp_path):
        sess = _new_session()
        _saved(sess)
        sess.project = str(tmp_path / "main")
        sess.worktree = str(tmp_path / "wt")
        run_records.begin_run(sess)
        snapshot = _finish(sess, "completed")

        assert snapshot["work_dir"] == str(tmp_path / "wt")
        assert snapshot["project"] == str(tmp_path / "main"), "项目锚点仍要如实记录"
        assert result_view.describe(snapshot)["work_dir"] == str(tmp_path / "wt")

    def test_plain_run_falls_back_to_the_project_root(self, isolated_memory, tmp_path):
        sess = _new_session()
        _saved(sess)
        sess.project = str(tmp_path)
        run_records.begin_run(sess)
        snapshot = _finish(sess, "completed")
        assert snapshot["work_dir"] == str(tmp_path)

    def test_diff_button_uses_the_work_dir(self, isolated_memory, tmp_path):
        """真建一个 git 仓库改点东西，确认 diff 走的是 work_dir 而不是项目根。"""
        import shutil
        if not shutil.which("git"):
            pytest.skip("git 未安装")
        from src.ui.chat_window import ChatUI
        repo = tmp_path / "wt"
        repo.mkdir()
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "T"]):
            subprocess.run(args, cwd=str(repo), check=True, capture_output=True)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                       capture_output=True)
        (repo / "a.py").write_text("x = 987654\n", encoding="utf-8")

        shown = {}

        class _Host:
            _on_result_view_diff = ChatUI._on_result_view_diff

            def _show_toast(self, *a):
                shown["toast"] = a

            def _show_text_dialog(self, title, text):
                shown["text"] = text

        _Host()._on_result_view_diff({"work_dir": str(repo),
                                      "project": str(tmp_path / "main")})
        assert "987654" in shown["text"], "diff 查到主仓库去了，隔离区的改动看不见"


# ── ⑤ 证据要在工具边界进持久化记录 ──

class TestEvidencePersistedAtToolBoundary:
    def test_committed_tool_result_carries_its_check_evidence(self, isolated_memory,
                                                              tmp_path):
        """工具结果已提交、还没收尾就崩——检查记录不能一条都不剩。"""
        sess = _new_session()
        sid = _saved(sess)
        sess.project = str(tmp_path)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="check_code", tool_call_id="c1",
                                         args={"path": "a.py"})
        verification.record_evidence(sess.verification, kind="check", checker="ruff",
                                     status="passed", exit_code=0, path="a.py")
        run_records.commit_operation(sess, op)       # 到此为止，**不 finalize**

        restored = session.Session()
        memory.load_session(sid, session=restored)
        rows = restored.last_run["evidence"]["validation_runs"]
        assert [r["checker"] for r in rows] == ["ruff"]
        assert rows[0]["exit_code"] == 0
        assert restored.last_run["phase"] == "running", "没收尾就是中断，不是完成"

    def test_restored_evidence_is_history_not_a_current_pass(self, isolated_memory,
                                                             tmp_path):
        """恢复出来的证据只作历史展示，绝不回填成新一轮的通行凭证。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="check_code", tool_call_id="c1",
                                         args={"path": "a.py"})
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="passed", exit_code=0)
        run_records.commit_operation(sess, op)

        restored = session.Session()
        memory.load_session(sid, session=restored)
        run_records.begin_run(restored)
        assert restored.verification["evidence"] == []
        assert restored.verification["tests_passed"] is None


# ── ⑥ 重置 / 换会话之后不能再画旧卡 ──

class TestResultCacheInvalidation:
    def test_reset_history_clears_the_cache(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        _finish(sess, "completed")
        assert sess.last_result is not None

        memory.reset_history(session=sess)
        assert sess.last_result is None
        assert sess.last_run is None
        assert run_records.snapshot_from_loaded(sess) is None

    def test_loading_another_session_into_the_same_object_clears_it(self, isolated_memory):
        first = _new_session()
        _saved(first)
        run_records.begin_run(first)
        _finish(first, "completed")

        other = _new_session()
        other.chat_history[1] = HumanMessage(content="另一段历史")
        sid_other = _saved(other)

        memory.load_session(sid_other, session=first)   # 复用对象装别人
        assert first.last_result is None

    def test_redraw_rejects_a_snapshot_from_an_older_run(self, isolated_memory):
        from src.ui.chat_window import ChatUI
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        old = _finish(sess, "completed")
        run_records.begin_run(sess)                     # 新一轮，last_run 换人

        assert ChatUI._result_belongs_here(sess, old) is False

    def test_redraw_accepts_the_current_run(self, isolated_memory):
        from src.ui.chat_window import ChatUI
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        current = _finish(sess, "completed")
        assert ChatUI._result_belongs_here(sess, current) is True

    def test_redraw_rejects_a_snapshot_from_another_session(self, isolated_memory):
        from src.ui.chat_window import ChatUI
        first = _new_session()
        _saved(first)
        run_records.begin_run(first)
        foreign = _finish(first, "completed")

        second = _new_session()
        second.chat_history[1] = HumanMessage(content="另一个")
        _saved(second)
        run_records.begin_run(second)
        _finish(second, "completed")
        # 伪造成同一个 run_id，但会话不同 → 仍然要拒
        foreign["run_id"] = second.last_run["id"]
        assert ChatUI._result_belongs_here(second, foreign) is False
