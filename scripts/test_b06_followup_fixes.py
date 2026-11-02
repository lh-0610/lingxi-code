"""B06 第二轮复核的三处边界（都走真实路径）。

共同主题：**卡片上的结论要和闸门、和磁盘上的事实对得上**——重试通过了就别再说没过，
历史那轮在哪跑的就记在哪，记的目录没了就明说，别悄悄换一个目录查。
"""
import os
import shutil
import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, result_view, run_records, session, tools, verification
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


# ── ① 同一项检查重试通过，旧失败要让位 ──

class TestRetrySupersedesFailure:
    def test_same_check_passing_on_retry_resolves_the_earlier_failure(self, isolated_memory,
                                                                      tmp_path):
        """真跑同一条 pytest 两次：先失败、后通过，中间**不改文件**。

        旧失败不会因为"过期"退场（文件没变），所以按 all() 算会一直把它算进去，
        卡片停在"检查未全部通过"，而完成闸门早就认了 tests_passed=True——
        两边对同一件事给出相反结论。
        """
        sess = _new_session()
        _saved(sess)
        sess.project = str(tmp_path)
        session.bind_thread(sess)
        session.set_active(sess)
        try:
            run_records.begin_run(sess)
            target = tmp_path / "test_flaky.py"
            target.write_text("def test_x():\n    assert False\n", encoding="utf-8")
            tools.run_tests.func("test_flaky.py")
            assert sess.verification["tests_passed"] is False

            # 修好测试本身（不动被测代码），再跑同一条命令
            target.write_text("def test_x():\n    assert True\n", encoding="utf-8")
            tools.run_tests.func("test_flaky.py")
            assert sess.verification["tests_passed"] is True

            snapshot = _finish(sess, "completed")
        finally:
            session.unbind_thread()

        rows = result_view.validation_rows(snapshot)
        assert [r["status"] for r in rows] == ["failed", "passed"], "两次都要留记录"
        assert result_view.summarize_checks(snapshot) == "clear", (
            "重试通过之后，卡片不能还说检查没过")
        view = result_view.describe(snapshot)
        assert view["title"] == "本轮执行结束，已执行检查通过"
        assert view["validations"][0]["superseded"] is True, "旧失败要标成已被取代"
        assert view["validations"][1]["superseded"] is False
        assert view["has_superseded"] is True

    def test_a_different_target_is_not_superseded(self, isolated_memory):
        """对两个不同文件跑 ruff 是两项检查，后一个不该把前一个顶掉。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="check", checker="ruff",
                                     status="failed", exit_code=1, path="a.py", cwd="/p")
        verification.record_evidence(sess.verification, kind="check", checker="ruff",
                                     status="passed", exit_code=0, path="b.py", cwd="/p")
        snapshot = _finish(sess, "completed")

        assert result_view.summarize_checks(snapshot) == "unresolved"
        assert all(not r["superseded"] for r in result_view.describe(snapshot)["validations"])

    def test_a_different_command_is_not_superseded(self, isolated_memory):
        """同一个检查器、不同参数也是两项检查（`pytest -k a` 与 `pytest -k b`）。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, cwd="/p",
                                     argv=["py", "-m", "pytest", "-k", "a"])
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="passed", exit_code=0, cwd="/p",
                                     argv=["py", "-m", "pytest", "-k", "b"])
        assert result_view.summarize_checks(_finish(sess, "completed")) == "unresolved"

    def test_a_different_directory_is_not_superseded(self, isolated_memory):
        """同一条命令在两个目录跑是两项检查——隔离区跑过不代表主仓库也过了。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, cwd="/main",
                                     argv=["py", "-m", "pytest"])
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="passed", exit_code=0, cwd="/worktree",
                                     argv=["py", "-m", "pytest"])
        assert result_view.summarize_checks(_finish(sess, "completed")) == "unresolved"

    def test_passing_then_failing_is_still_unresolved(self, isolated_memory):
        """顺序反过来：先过后挂，以最新的为准，不能被旧的通过救回来。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        for status, code in (("passed", 0), ("failed", 1)):
            verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                         status=status, exit_code=code, cwd="/p",
                                         argv=["py", "-m", "pytest"])
        assert result_view.summarize_checks(_finish(sess, "completed")) == "unresolved"

    def test_edit_after_a_retry_still_expires_it(self, isolated_memory):
        """取代不等于免疫：最新那次之后又改了文件，照样过期。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        for status, code in (("failed", 1), ("passed", 0)):
            verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                         status=status, exit_code=code, cwd="/p",
                                         argv=["py", "-m", "pytest"])
        verification.mark_dirty(sess.verification, "src/a.py")
        assert result_view.summarize_checks(_finish(sess, "unverified")) == "unresolved"


# ── ② 重启后隔离区归属不能丢 ──

class TestWorkDirSurvivesRestart:
    def test_work_dir_is_persisted_on_the_run_record(self, isolated_memory, tmp_path):
        """重开之后 Session.worktree 早没了，靠当时的会话状态重算只会算出主仓库。"""
        sess = _new_session()
        sid = _saved(sess)
        sess.project = str(tmp_path / "main")
        sess.worktree = str(tmp_path / "wt")
        run_records.begin_run(sess)
        _finish(sess, "completed")

        restored = session.Session()
        memory.load_session(sid, session=restored)
        assert restored.worktree is None, "worktree 是运行态，不该被恢复成可用的隔离区"
        assert restored.last_run["work_dir"] == str(tmp_path / "wt")

        snapshot = run_records.snapshot_from_loaded(restored)
        assert snapshot["work_dir"] == str(tmp_path / "wt")
        assert result_view.describe(snapshot)["work_dir"] == str(tmp_path / "wt")

    def test_work_dir_is_persisted_at_the_tool_boundary_too(self, isolated_memory, tmp_path):
        """崩在收尾之前时，历史卡片同样要知道那轮在哪跑的。"""
        sess = _new_session()
        sid = _saved(sess)
        sess.project = str(tmp_path / "main")
        sess.worktree = str(tmp_path / "wt")
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="check_code", tool_call_id="c1",
                                         args={"path": "a.py"})
        run_records.commit_operation(sess, op)       # 不 finalize

        restored = session.Session()
        memory.load_session(sid, session=restored)
        assert restored.last_run["work_dir"] == str(tmp_path / "wt")

    def test_old_session_without_work_dir_falls_back(self, isolated_memory, tmp_path):
        """旧会话没这个字段——回退到会话的项目根，不崩。"""
        import json
        sid = "20240101_000000_009900"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "旧", "updated": "2024-01-01", "schema_version": 2,
            "progress": {"version": 1, "last_run": {"version": 1, "id": "run-old",
                                                    "phase": "ended", "outcome": "completed"}},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        memory.load_session(sid, session=sess)
        sess.project = str(tmp_path)
        assert sess.last_run["work_dir"] == ""
        assert run_records.snapshot_from_loaded(sess)["work_dir"] == str(tmp_path)


# ── ③ 记录的目录失效时不能回退到进程 cwd ──

def _diff_host():
    """结果卡「查看改动」的无窗口宿主：只装被测的方法 + 一个记录调用次数的假文件面板。

    真正的面板是 Qt 控件（布局、宽度由 ChatUI 管），这里只关心"按哪个目录取了哪些文件"。
    """
    from src.ui.chat_window import ChatUI

    class _Panel:
        def __init__(self):
            self.opened = {}

        def open_file(self, rel_path, rows, hint=""):
            self.opened[str(rel_path)] = list(rows)

    class _Host:
        _on_result_view_diff = ChatUI._on_result_view_diff
        _open_file_in_panel = ChatUI._open_file_in_panel
        # 原方法是 staticmethod，取出后已是普通函数，这里要重新包一层才不会被绑成 self
        _unique_changed_files = staticmethod(ChatUI._unique_changed_files)
        _RESULT_DIFF_TAB_LIMIT = ChatUI._RESULT_DIFF_TAB_LIMIT

        def __init__(self):
            self.file_panel = _Panel()
            self.toasts = []
            self.dialogs = []

        def _settle_file_panel(self):
            pass        # 面板停靠 / 让位是 Qt 布局的事，这里不涉及

        def _show_toast(self, text, duration=1500):
            self.toasts.append(text)

        def _show_text_dialog(self, title, text):
            self.dialogs.append((title, text))

    return _Host()


class TestMissingWorkDir:
    def test_vanished_directory_is_reported_not_silently_redirected(self, isolated_memory,
                                                                    tmp_path):
        """目录没了就明说。

        `_project_cwd()` 在路径不存在时会回退到**进程 cwd**，于是 git_diff 悄悄查了
        灵犀自己的目录、回一句"工作区干净"，而弹窗标题还写着那个不存在的路径。
        """
        gone = tmp_path / "moved_away"
        host = _diff_host()
        host._on_result_view_diff({"work_dir": str(gone), "project": str(gone),
                                   "files": ["a.py"]})

        assert host.dialogs, "目录没了要说清楚，不能什么都不显示"
        title, text = host.dialogs[0]
        assert "不可用" in title
        assert str(gone) in text
        assert "工作区干净" not in text, "不能拿别处的查询结果冒充这一轮"
        assert host.file_panel.opened == {}, "不能退回别的目录去开标签页"
        assert session.get_bound() is None, "提前返回也不能留下线程绑定"

    def test_moved_worktree_does_not_read_the_process_cwd(self, isolated_memory, tmp_path):
        """把隔离区整个挪走（改动都还在），旧卡片不能去查进程当前目录。"""
        if not shutil.which("git"):
            pytest.skip("git 未安装")
        original = tmp_path / "wt"
        original.mkdir()
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "T"]):
            subprocess.run(args, cwd=str(original), check=True, capture_output=True, stdin=subprocess.DEVNULL)
        (original / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(original), check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(original), check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        (original / "a.py").write_text("x = 987654\n", encoding="utf-8")
        shutil.move(str(original), str(tmp_path / "wt_moved"))
        assert not os.path.isdir(original)

        host = _diff_host()
        host._on_result_view_diff({"work_dir": str(original), "project": str(original),
                                   "files": ["a.py"]})
        assert "不可用" in host.dialogs[0][0]
        assert "diff --git" not in host.dialogs[0][1]
        assert host.file_panel.opened == {}

    def test_existing_directory_still_works(self, isolated_memory, tmp_path):
        """别把正常路径一起挡掉。"""
        if not shutil.which("git"):
            pytest.skip("git 未安装")
        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (["git", "init", "-q"],
                     ["git", "config", "user.email", "t@example.com"],
                     ["git", "config", "user.name", "T"]):
            subprocess.run(args, cwd=str(repo), check=True, capture_output=True, stdin=subprocess.DEVNULL)
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        (repo / "a.py").write_text("x = 42\n", encoding="utf-8")

        host = _diff_host()
        host._on_result_view_diff({"work_dir": str(repo), "project": str(repo),
                                   "files": ["a.py"]})
        assert host.dialogs == [], "目录还在就不该弹说明窗口"
        rows = host.file_panel.opened["a.py"]
        assert any("x = 42" in text for _no, _kind, text in rows)
        assert session.get_bound() is None

    def test_no_recorded_files_is_said_out_loud(self, isolated_memory, tmp_path):
        """这一轮没记下改动文件：明确提示，不去 diff 整个工作区。"""
        host = _diff_host()
        host._on_result_view_diff({"work_dir": str(tmp_path), "project": str(tmp_path),
                                   "files": []})
        assert host.file_panel.opened == {}
        assert host.toasts and host.toasts[-1] == "这一轮没有记录到改动的文件"
        assert session.get_bound() is None
