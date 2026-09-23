"""运行结果卡：证据采集、结论判定、信号隔离与 Qt 渲染（B06）。

这一批要回答的是"用户看到的结论有没有依据"。贯穿全文的取舍：

- **卡片上每一句都要有程序证据**。模型正文说"已完成"不算证据；没跑过检查就不说
  "检查通过"；`unverified` 不翻译成"代码写完了只是没测"。
- **没跑起来 ≠ 跑了且失败**。未安装 / 超时 / 取消必须各自可见，退出码拿不到就是 None，
  绝不填 0 冒充成功。
- **保存结果与运行结果分开**。存不上是另一件事，不能把这一轮的结论改掉。
"""
import json
import os
import subprocess
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import config, memory, result_view, run_records, session, tools, verification
from src.agent_result import AgentResult


# ══════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════

class _UI:
    def __init__(self):
        self.messages = []
        self.results = []

    def show_message(self, text, tag="ai_msg"):
        self.messages.append(str(text))

    def render_final_markdown(self, text, speak=True):
        self.messages.append(str(text))

    def show_retry(self, text):
        self.messages.append(str(text))

    def show_token_usage(self, *a, **k):
        pass

    def remove_thinking_indicator(self):
        pass

    def deliver_run_result(self, sess, run_id, snapshot):
        self.results.append((sess, run_id, snapshot))

    def text(self):
        return "".join(self.messages)


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
    """跑完一轮并拿到快照（不经 UI）。"""
    run = sess.last_run
    report = run_records.finalize_run(sess, run, AgentResult(status, reason))
    return report.snapshot


def _describe(snapshot, live_run_id=None):
    return result_view.describe(snapshot, live_run_id=live_run_id)


# ══════════════════════════════════════════════════════════════
# 真实证据采集
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def tool_session(isolated_memory, tmp_path):
    """一个锚定到临时项目的会话，工具真的在那里跑。"""
    sess = _new_session()
    _saved(sess)
    sess.project = str(tmp_path)   # 首次保存之后再锚定（save 会把它重置成全局值）
    session.bind_thread(sess)
    session.set_active(sess)
    run_records.begin_run(sess)
    yield sess
    session.unbind_thread()


def _only(records, kind=None):
    rows = [r for r in records if kind is None or r["kind"] == kind]
    assert rows, f"没有采集到 {kind or '任何'} 证据"
    return rows[-1]


class TestEvidenceFromRealExecution:
    def test_passing_pytest_records_argv_cwd_and_exit_code(self, tool_session, tmp_path):
        (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n",
                                             encoding="utf-8")
        out = tools.run_tests.func("")
        record = _only(tool_session.verification["evidence"], "tests")

        assert record["status"] == "passed", out
        assert record["exit_code"] == 0
        assert record["checker"] == "pytest"
        # argv 必须是**真的跑的那条**，不是从模型最后一句话反推的
        assert record["argv"][1:3] == ["-m", "pytest"]
        assert os.path.realpath(record["cwd"]) == os.path.realpath(str(tmp_path))
        assert record["started_at"] and record["duration_ms"] >= 0

    def test_failing_pytest_is_failed_not_not_run(self, tool_session, tmp_path):
        (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n",
                                              encoding="utf-8")
        tools.run_tests.func("")
        record = _only(tool_session.verification["evidence"], "tests")
        assert record["status"] == "failed"
        assert record["exit_code"] == 1

    def test_no_tests_collected_is_not_a_pass(self, tool_session, tmp_path):
        """pytest 退出码 5 = 一个用例都没收集到。说"检查通过"会让用户以为代码被测过。"""
        tools.run_tests.func("")
        record = _only(tool_session.verification["evidence"], "tests")
        assert record["status"] == "not_run"
        assert record["exit_code"] == 5
        assert tool_session.verification["tests_passed"] is None

    def test_missing_pytest_is_not_run_with_no_exit_code(self, tool_session, monkeypatch):
        """没装 = 没跑起来。**绝不能**伪造 exit_code=0。"""
        def _boom(*a, **k):
            raise FileNotFoundError("python not found")

        monkeypatch.setattr(subprocess, "run", _boom)
        tools.run_tests.func("")
        record = _only(tool_session.verification["evidence"], "tests")
        assert record["status"] == "not_run"
        assert record["exit_code"] is None
        assert "未安装" in record["summary"]

    def test_timeout_is_its_own_status(self, tool_session, monkeypatch):
        def _slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

        monkeypatch.setattr(subprocess, "run", _slow)
        tools.run_tests.func("", timeout=1)
        record = _only(tool_session.verification["evidence"], "tests")
        assert record["status"] == "timeout"
        assert record["exit_code"] is None

    def test_check_code_records_the_real_custom_command(self, tool_session, tmp_path,
                                                        monkeypatch):
        marker = tmp_path / "ran.marker"
        command = (f'"{sys.executable}" -c '
                   f'"import pathlib; pathlib.Path(r\'{marker}\').write_text(\'x\')"')
        monkeypatch.setattr(config, "CHECK_COMMAND", command)
        (tmp_path / "a.rs").write_text("fn main(){}", encoding="utf-8")

        tools.check_code.func("a.rs")

        assert marker.exists(), "命令没真跑，这条用例就证明不了什么"
        record = _only(tool_session.verification["evidence"], "check")
        assert record["checker"] == "check_command"
        assert record["status"] == "passed"
        assert record["exit_code"] == 0
        assert sys.executable.split(os.sep)[-1] in record["command"]

    def test_failing_custom_command_keeps_its_exit_code(self, tool_session, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(config, "CHECK_COMMAND",
                            f'"{sys.executable}" -c "import sys; sys.exit(3)"')
        (tmp_path / "a.rs").write_text("fn main(){}", encoding="utf-8")
        tools.check_code.func("a.rs")
        record = _only(tool_session.verification["evidence"], "check")
        assert record["status"] == "failed"
        assert record["exit_code"] == 3

    def test_python_check_records_each_checker_separately(self, tool_session, tmp_path,
                                                          monkeypatch):
        """ruff 过了、mypy 超时是很常见的组合；合成一条记录就说不出来。"""
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        real_run = subprocess.run

        def _mypy_times_out(cmd, *a, **k):
            if isinstance(cmd, list) and any("mypy" in str(c) for c in cmd):
                raise subprocess.TimeoutExpired(cmd="mypy", timeout=90)
            return real_run(cmd, *a, **k)

        monkeypatch.setattr(config, "TYPE_CHECK_AFTER_EDIT", True)
        monkeypatch.setattr(subprocess, "run", _mypy_times_out)
        tools.check_code.func("a.py")

        checkers = {r["checker"]: r for r in tool_session.verification["evidence"]}
        assert checkers, "至少要有一个检查器的记录"
        if "mypy" in checkers:
            assert checkers["mypy"]["status"] == "timeout"
        assert any(c in checkers for c in ("ruff", "py_compile"))

    def test_auto_check_after_edit_shares_the_same_evidence_path(self, tool_session, tmp_path,
                                                                 monkeypatch):
        """编辑后的自动检查不该是另一套：它和手动 check_code 共用采集机制。"""
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", True)
        target = tmp_path / "bad.py"
        target.write_text("def f(:\n", encoding="utf-8")
        tools._auto_check_suffix(str(target))

        records = tool_session.verification["evidence"]
        assert records, "自动检查也必须留证据，不能是另一套"
        # 可能同时跑了 ruff 和 mypy：语法错至少要被其中一个抓到，各自独立成条。
        assert any(r["status"] == "failed" for r in records)
        assert all(r["path"].endswith("bad.py") for r in records)
        assert all(r["kind"] == "check" for r in records)

    def test_secrets_in_the_command_are_redacted(self, tool_session, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CHECK_COMMAND",
                            f'"{sys.executable}" -c "pass" --api_key=SUPERSECRETVALUE')
        (tmp_path / "a.rs").write_text("fn main(){}", encoding="utf-8")
        tools.check_code.func("a.rs")
        record = _only(tool_session.verification["evidence"], "check")
        assert "SUPERSECRETVALUE" not in json.dumps(record, ensure_ascii=False)
        assert "***" in record["command"]


# ══════════════════════════════════════════════════════════════
# 过期判定
# ══════════════════════════════════════════════════════════════

class TestStaleness:
    def test_editing_after_a_check_marks_it_stale(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        v = sess.verification
        verification.record_evidence(v, kind="tests", checker="pytest", status="passed",
                                     exit_code=0, argv=["py", "-m", "pytest"], cwd="/p")
        verification.mark_dirty(v, "src/a.py")     # 检查之后又改了

        snapshot = _finish(sess, "unverified")
        rows = result_view.validation_rows(snapshot)
        assert rows[0]["stale"] is True
        assert result_view.has_fresh_pass(snapshot) is False
        # 历史记录**保留**，不能删掉后显示成"从未检查"
        assert rows[0]["status"] == "passed"

    def test_checking_again_after_the_edit_is_fresh(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        v = sess.verification
        verification.record_evidence(v, kind="tests", checker="pytest", status="passed",
                                     exit_code=0)
        verification.mark_dirty(v, "src/a.py")
        verification.record_evidence(v, kind="tests", checker="pytest", status="passed",
                                     exit_code=0)

        snapshot = _finish(sess, "completed")
        rows = result_view.validation_rows(snapshot)
        assert [r["stale"] for r in rows] == [True, False]
        assert result_view.has_fresh_pass(snapshot) is True

    def test_a_plain_save_does_not_expire_evidence(self, isolated_memory):
        """只保存不该让刚跑完的检查过期——这正是不能拿 progress_revision 当版本的原因。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="passed", exit_code=0)
        before = sess.progress_revision
        for _ in range(3):
            sess.chat_history.append(AIMessage(content="无关的一轮"))
            memory.save_session(session=sess)
        assert sess.progress_revision > before

        snapshot = _finish(sess, "completed")
        assert result_view.validation_rows(snapshot)[0]["stale"] is False

    def test_a_blind_period_expires_evidence(self, isolated_memory, tmp_path):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="passed", exit_code=0)
        verification.mark_blind_period(sess.verification, str(tmp_path), "枚举超时")
        snapshot = _finish(sess, "unverified")
        assert result_view.validation_rows(snapshot)[0]["stale"] is True


# ══════════════════════════════════════════════════════════════
# 结论判定
# ══════════════════════════════════════════════════════════════

class TestDescribe:
    def _snapshot(self, isolated_memory, status, *, evidence=None, dirty=None):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        for path in (dirty or []):
            verification.mark_dirty(sess.verification, path)
        for kwargs in (evidence or []):
            verification.record_evidence(sess.verification, **kwargs)
        return _finish(sess, status)

    @pytest.mark.parametrize("status, expect", [
        ("completed", "本轮回复结束"),
        ("failed", "本轮执行失败"),
        ("cancelled", "已停止"),
        ("unverified", "本轮结束，仍有未验证事项"),
        ("limit_reached", "已达到运行上限"),
    ])
    def test_every_terminal_status_has_its_own_title(self, isolated_memory, status, expect):
        view = _describe(self._snapshot(isolated_memory, status))
        assert view["title"] == expect

    def test_completed_with_fresh_evidence_says_checks_passed(self, isolated_memory):
        snapshot = self._snapshot(isolated_memory, "completed", evidence=[
            {"kind": "tests", "checker": "pytest", "status": "passed", "exit_code": 0}])
        assert _describe(snapshot)["title"] == "本轮执行结束，已执行检查通过"

    def test_plain_question_shows_no_check_panel(self, isolated_memory):
        """纯问答：没有检查面板，也绝不出现"测试通过"字样。"""
        view = _describe(self._snapshot(isolated_memory, "completed"))
        assert view["has_validations"] is False
        assert view["validations"] == []
        assert "通过" not in view["title"]
        assert "测试" not in result_view.plain_text(view)

    def test_model_claiming_done_does_not_override_unverified(self, isolated_memory):
        """模型正文说"已完成"，程序未验证 → 卡片仍显示未验证。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        sess.chat_history.append(AIMessage(content="任务已完成，一切正常！"))
        snapshot = _finish(sess, "unverified", "代码文件被修改但尚未运行测试")

        view = _describe(snapshot)
        assert view["title"] == "本轮结束，仍有未验证事项"
        assert view["has_pending"] is True
        # 不能翻译成"写完了只是没测"
        assert "已完成" not in view["title"]

    def test_interrupted_run_shows_interrupted(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)     # 只开始、不收尾
        restored = session.Session()
        memory.load_session(sid, session=restored)
        view = _describe(run_records.snapshot_from_loaded(restored))
        assert view["title"] == "上次运行被中断"

    def test_a_running_worker_is_not_called_interrupted(self, isolated_memory):
        """正在跑的那一轮显示成"上次运行被中断"是明确的误报。"""
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)
        snapshot = run_records.build_result_snapshot(sess, run)
        assert _describe(snapshot, live_run_id=run["id"])["title"] == "本轮仍在运行"
        assert _describe(snapshot, live_run_id="别的一轮")["title"] == "上次运行被中断"

    def test_files_use_involved_wording_not_net_diff(self, isolated_memory):
        view = _describe(self._snapshot(isolated_memory, "unverified",
                                        dirty=["src/a.py", "src/b.py"]))
        assert view["files_caption"] == "本轮涉及 2 个文件"
        assert "净修改" not in result_view.plain_text(view)

    def test_long_file_list_is_folded(self, isolated_memory):
        files = [f"src/f{i}.py" for i in range(20)]
        view = _describe(self._snapshot(isolated_memory, "unverified", dirty=files))
        assert view["files_folded"] is True
        assert len(view["files"]) == 20

    def test_task_id_null_works(self, isolated_memory):
        snapshot = self._snapshot(isolated_memory, "completed")
        assert snapshot["task_id"] is None
        assert _describe(snapshot)["title"]      # 不因为没有任务身份就画不出来

    def test_save_failure_is_separate_from_the_run_result(self, isolated_memory, monkeypatch):
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)
        monkeypatch.setattr(memory, "_atomic_write_json",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满了")))
        report = run_records.finalize_run(sess, run, AgentResult("completed", "做完了"))
        monkeypatch.undo()

        view = _describe(report.snapshot)
        assert view["title"] == "本轮回复结束", "保存失败不能把运行结论改掉"
        assert "磁盘满了" in view["save_note"]

    def test_body_saved_but_index_failed_is_said_precisely(self, isolated_memory, monkeypatch):
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)
        monkeypatch.setattr(memory, "_update_index",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("索引写不了")))
        report = run_records.finalize_run(sess, run, AgentResult("completed"))
        monkeypatch.undo()
        assert "索引" in _describe(report.snapshot)["save_note"]

    def test_successful_save_shows_no_save_warning(self, isolated_memory):
        view = _describe(self._snapshot(isolated_memory, "completed"))
        assert view["save_note"] == ""

    def test_view_entries_need_real_material(self, isolated_memory):
        snapshot = self._snapshot(isolated_memory, "completed")
        assert _describe(snapshot)["can_view_output"] is False, "没有记录就不给查看入口"
        with_evidence = self._snapshot(isolated_memory, "completed", evidence=[
            {"kind": "tests", "checker": "pytest", "status": "failed", "exit_code": 1,
             "argv": ["py", "-m", "pytest"], "summary": "1 failed"}])
        assert _describe(with_evidence)["can_view_output"] is True


# ══════════════════════════════════════════════════════════════
# 持久化与兼容
# ══════════════════════════════════════════════════════════════

class TestPersistence:
    def test_structured_evidence_round_trips(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=2, cwd="D:/p",
                                     argv=["py", "-m", "pytest"], summary="1 failed")
        _finish(sess, "unverified")
        first = json.loads((isolated_memory / f"{sid}.json").read_text(
            encoding="utf-8"))["progress"]["last_run"]["evidence"]

        restored = session.Session()
        memory.load_session(sid, session=restored)
        row = restored.last_run["evidence"]["validation_runs"][0]
        assert row["argv"] == ["py", "-m", "pytest"]
        assert row["exit_code"] == 2 and row["status"] == "failed"
        assert row["cwd"] == "D:/p"

        restored.chat_history.append(AIMessage(content="再存一次"))
        memory.save_session(session=restored)
        second = json.loads((isolated_memory / f"{sid}.json").read_text(
            encoding="utf-8"))["progress"]["last_run"]["evidence"]
        assert second == first, "二次保存不能把证据字段改掉"

    def test_snapshot_is_isolated_from_later_mutation(self, isolated_memory):
        """信号排队期间下一轮改了状态，排队中的快照不能跟着变。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        snapshot = _finish(sess, "unverified")

        run_records.begin_run(sess)                       # 新一轮开始
        verification.mark_dirty(sess.verification, "src/NEW.py")
        sess.pending_verification["files"].append("src/NEW.py")

        assert snapshot["evidence"]["changed_files"] == ["src/a.py"]
        assert "src/NEW.py" not in snapshot["pending_verification"]["files"]

    def test_old_session_without_evidence_still_renders(self, isolated_memory):
        sid = "20240101_000000_000900"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "旧会话", "updated": "2024-01-01", "schema_version": 2,
            "progress": {"version": 1, "revision": 2,
                         "last_run": {"version": 1, "id": "run-old", "phase": "ended",
                                      "outcome": "completed"}},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        view = _describe(run_records.snapshot_from_loaded(sess))
        assert view["title"] == "本轮回复结束"
        # 旧会话本来就没采集过命令 / 退出码，**不能凭空捏造**
        assert view["validations"] == []
        assert view["can_view_output"] is False

    def test_corrupt_evidence_does_not_take_down_the_chat(self, isolated_memory):
        sid = "20240101_000000_000901"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "坏证据", "updated": "2024-01-01", "schema_version": 2,
            "progress": {"version": 1,
                         "last_run": {"version": 1, "id": "run-x", "phase": "ended",
                                      "outcome": "completed",
                                      "evidence": {"validation_runs": ["垃圾", 5, None]}}},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        assert len(sess.chat_history) == 2, "坏证据不能牵连聊天历史"
        assert sess.last_run["evidence"]["validation_runs"] == []

    def test_unreadable_status_degrades_to_unknown_not_passed(self):
        out = run_records._normalize_validation_run(
            {"status": "看起来还行", "kind": "tests", "exit_code": "0"})
        assert out["status"] == "unknown", "读不懂绝不能变成「通过」"
        assert out["exit_code"] is None, "字符串 \"0\" 不是退出码 0"


# ══════════════════════════════════════════════════════════════
# 信号隔离（真实 Qt 队列）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def qt_host(monkeypatch, isolated_memory):
    """用生产的槽方法搭一个最小宿主，走真实 Qt 队列，不建整窗口、不调模型。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from unittest.mock import Mock
    from src import mcp_client, models
    fake_llm = Mock()
    fake_llm.bind_tools.return_value = fake_llm
    fake_llm.stream.side_effect = AssertionError("unexpected model call")
    fake_llm.invoke.side_effect = AssertionError("unexpected model call")
    monkeypatch.setattr(models, "_create_llm", lambda *a: fake_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)

    from PySide6.QtCore import QObject, Qt
    from PySide6.QtWidgets import QApplication
    from src.ui.chat_window import ChatUI
    from src.ui.widgets import SignalBridge

    app = QApplication.instance() or QApplication([])

    class Host(QObject):
        deliver_run_result = ChatUI.deliver_run_result
        _on_run_result = ChatUI._on_run_result

        def __init__(self):
            super().__init__()
            self.rendered = []
            self.refreshed = 0
            self._rendered_result_run_id = ""
            self.bridge = SignalBridge()
            self.bridge.run_result.connect(self._on_run_result, Qt.QueuedConnection)

        def _render_result_card(self, snapshot):
            self.rendered.append(snapshot)
            self._rendered_result_run_id = snapshot.get("run_id") or ""

        def _refresh_session_list(self):
            self.refreshed += 1

    host = Host()
    yield host, app
    app.processEvents()


class TestSignalRouting:
    def test_result_reaches_the_card_through_the_real_queue(self, qt_host):
        host, app = qt_host
        sess = _new_session()
        _saved(sess)
        session.set_active(sess)
        run_records.begin_run(sess)
        snapshot = _finish(sess, "completed")

        host.deliver_run_result(sess, snapshot["run_id"], snapshot)
        assert host.rendered == [], "必须经过队列，不能同步直达"
        app.processEvents()
        assert [s["run_id"] for s in host.rendered] == [snapshot["run_id"]]

    def test_background_session_result_does_not_enter_the_foreground(self, qt_host):
        host, app = qt_host
        background = _new_session()
        _saved(background)
        foreground = _new_session()
        foreground.chat_history[1] = HumanMessage(content="另一个任务")
        _saved(foreground)
        session.set_active(foreground)

        run_records.begin_run(background)
        snapshot = _finish(background, "failed")
        host.deliver_run_result(background, snapshot["run_id"], snapshot)
        app.processEvents()

        assert host.rendered == [], "后台会话的结果不能插进前台会话"
        assert background.needs_redraw is True
        assert host.refreshed >= 1

    def test_late_result_from_an_old_run_does_not_overwrite(self, qt_host):
        host, app = qt_host
        sess = _new_session()
        _saved(sess)
        session.set_active(sess)

        run_records.begin_run(sess)
        old = _finish(sess, "cancelled")
        run_records.begin_run(sess)
        new = _finish(sess, "completed")

        host.deliver_run_result(sess, new["run_id"], new)
        app.processEvents()
        host.deliver_run_result(sess, old["run_id"], old)   # 迟到
        app.processEvents()

        assert [s["run_id"] for s in host.rendered] == [new["run_id"]]

    def test_duplicate_signal_renders_only_one_card(self, qt_host):
        host, app = qt_host
        sess = _new_session()
        _saved(sess)
        session.set_active(sess)
        run_records.begin_run(sess)
        snapshot = _finish(sess, "completed")

        for _ in range(3):
            host.deliver_run_result(sess, snapshot["run_id"], snapshot)
        app.processEvents()
        assert len(host.rendered) == 1

    def test_two_sessions_finishing_interleaved_keep_their_own_results(self, qt_host):
        host, app = qt_host
        first = _new_session()
        _saved(first)
        second = _new_session()
        second.chat_history[1] = HumanMessage(content="第二个")
        _saved(second)
        session.set_active(first)

        run_records.begin_run(first)
        run_records.begin_run(second)
        snap_second = _finish(second, "failed")
        snap_first = _finish(first, "completed")

        host.deliver_run_result(second, snap_second["run_id"], snap_second)
        host.deliver_run_result(first, snap_first["run_id"], snap_first)
        app.processEvents()

        assert [s["run_id"] for s in host.rendered] == [snap_first["run_id"]]
        assert second.last_result["outcome"] == "failed"
        assert first.last_result["outcome"] == "completed"


class TestFinishedGuard:
    def test_stale_finished_does_not_re_enable_the_button(self, monkeypatch, isolated_memory):
        """旧 worker 的迟到 finished 不能把新一轮正在用的按钮恢复成可发送。"""
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from src.ui.chat_window import ChatUI
        QApplication.instance() or QApplication([])

        sess = _new_session()
        session.set_active(sess)
        old_token, new_token = object(), object()
        sess.worker_token = new_token          # 新 worker 已经起来了

        class Host:
            _on_finished_sess = ChatUI._on_finished_sess
            _settle_resume_card = ChatUI._settle_resume_card    # B04：finished 顺带收拾继续按钮
            _resume_cards_by_session = ChatUI._resume_cards_by_session

            def __init__(self):
                self.btn_states = []
                self.refreshed = 0
                self._has_input = True

            def _update_btn_state(self, state):
                self.btn_states.append(state)

            def _refresh_session_list(self):
                self.refreshed += 1

        host = Host()
        host._on_finished_sess(sess, old_token)
        assert host.btn_states == [], "旧 worker 不该动按钮"
        assert host.refreshed == 1, "侧栏照刷（旧那轮的状态确实变了）"

        host._on_finished_sess(sess, new_token)
        assert host.btn_states == ["enabled"], "当前 worker 的 finished 才收尾"


# ══════════════════════════════════════════════════════════════
# 真实 Qt 渲染
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _card_text(widget):
    from PySide6.QtWidgets import QLabel
    return "\n".join(w.text() for w in widget.findChildren(QLabel))


class TestQtRendering:
    def test_card_renders_real_widgets(self, qt_app, isolated_memory):
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, argv=["py", "-m", "pytest"],
                                     summary="1 failed", cwd="D:/p")
        view = _describe(_finish(sess, "unverified", "测试仍有失败项"))

        card = ResultCard(view)
        card.resize(560, 400)
        card.show()
        qt_app.processEvents()

        text = _card_text(card)
        assert "本轮结束，仍有未验证事项" in text
        assert "src/a.py" in text
        assert "pytest" in text and "失败" in text
        assert "退出码 1" in text
        card.close()

    def test_no_child_demands_an_absurd_minimum_width(self, qt_app, isolated_memory):
        """长路径 / 长错误不能把卡片顶出可视区。

        判据放在**每个子控件的 minimumSizeHint 宽度**上：滚动区 widgetResizable=True，
        内容会被压到视口宽度，压不下去的只有最小宽度，所以真正撑破可视区的就是它。
        （容器整体的 sizeHint 比视口大是正常的，不代表显示出问题——实测中它和肉眼
        看到的结果并不一致，所以这里钉的是机制，视觉验收另外用截图做。）
        """
        from PySide6.QtWidgets import QLabel
        from src.ui.result_card import ResultCard
        long_path = "src/" + "/".join(f"very_long_segment_{i}" for i in range(12)) + ".py"
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, long_path)
        verification.record_evidence(sess.verification, kind="check", checker="check_command",
                                     status="error", exit_code=None, path=long_path,
                                     summary="X" * 600, reason="Y" * 600)
        card = ResultCard(_describe(_finish(sess, "failed", "Z" * 800)))
        qt_app.processEvents()

        widest = max((lab.minimumSizeHint().width(), lab.text()[:50])
                     for lab in card.findChildren(QLabel))
        assert widest[0] <= 520, f"有控件的最小宽度到了 {widest[0]}px：{widest[1]}"
        card.close()

    def test_renders_at_several_widths_without_error(self, qt_app, isolated_memory):
        from src.ui.message_view import MessageView
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, argv=["py", "-m", "pytest"])
        view = _describe(_finish(sess, "unverified"))
        for width in (420, 620, 980, 1400):
            mv = MessageView()
            mv.resize(width, 700)
            card = mv.add_result_card(ResultCard(view))
            mv.show()
            for _ in range(2):
                qt_app.processEvents()
            assert card.isVisible() and card.height() > 0
            mv.close()

    def test_no_single_line_is_absurdly_long(self, qt_app, isolated_memory):
        """每一行都要有上限：等宽字体的长行是把卡片撑宽的元凶。"""
        from PySide6.QtWidgets import QLabel
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/" + "x" * 400 + ".py")
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, summary="E" * 900)
        card = ResultCard(_describe(_finish(sess, "failed")))
        for lab in card.findChildren(QLabel):
            for line in lab.text().splitlines():
                assert len(line) <= 100, f"有一行长到 {len(line)} 字符：{line[:60]}"
        card.close()

    def test_plain_question_card_has_no_check_section(self, qt_app, isolated_memory):
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        view = _describe(_finish(sess, "completed"))

        card = ResultCard(view)
        card.show()
        qt_app.processEvents()
        text = _card_text(card)
        assert "实际执行的检查" not in text
        assert "通过" not in text
        card.close()

    def test_continue_button_only_where_continuing_makes_sense(self, qt_app, isolated_memory):
        """B04 接上了继续入口：没做完的轮次给按钮，正常完成的不给（B06 时一律不放）。"""
        from PySide6.QtWidgets import QPushButton
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        sess.project = "D:/proj"
        run_records.begin_run(sess)
        unfinished = ResultCard(_describe(_finish(sess, "limit_reached")))
        assert "继续任务" in [b.text() for b in unfinished.findChildren(QPushButton)]
        unfinished.close()

        run_records.begin_run(sess)
        done = ResultCard(_describe(_finish(sess, "completed")))
        assert "继续任务" not in [b.text() for b in done.findChildren(QPushButton)]
        done.close()

    def test_buttons_carry_their_own_card_identity(self, qt_app, isolated_memory):
        """按钮带着**这张卡自己的**归属走，不去读当前前台会话。"""
        from PySide6.QtWidgets import QPushButton
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        sess.project = "D:/proj-of-this-card"
        run_records.begin_run(sess)
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, argv=["py"])
        view = _describe(_finish(sess, "failed"))

        card = ResultCard(view)
        got = []
        card.view_diff_requested.connect(got.append)
        for btn in card.findChildren(QPushButton):
            if btn.text() == "查看改动":
                btn.click()
        qt_app.processEvents()

        assert got and got[0]["project"] == "D:/proj-of-this-card"
        assert got[0]["run_id"] == view["run_id"]
        card.close()

    def test_card_renders_in_the_message_view(self, qt_app, isolated_memory):
        from src.ui.message_view import MessageView
        from src.ui.result_card import ResultCard
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        view = _describe(_finish(sess, "cancelled"))

        mv = MessageView()
        mv.resize(700, 500)
        card = mv.add_result_card(ResultCard(view))
        mv.show()
        qt_app.processEvents()
        assert card is not None and card.isVisible()
        assert "已停止" in _card_text(mv)
        mv.close()


# ══════════════════════════════════════════════════════════════
# 跨进程重绘
# ══════════════════════════════════════════════════════════════

_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from src import paths
paths.set_data_dir(sys.argv[2])
from src import memory, result_view, run_records, session

sess = session.Session()
assert memory.load_session(sys.argv[3], session=sess) is True
snapshot = run_records.snapshot_from_loaded(sess)
view = result_view.describe(snapshot, live_run_id=None)
print(json.dumps({
    "title": view["title"],
    "files": view["files"],
    "validations": [(r["checker"], r["status"], r["exit_code"], r["stale"])
                    for r in view["validations"]],
    "display_source": view["display_source"],
    "has_pending": view["has_pending"],
}, ensure_ascii=False))
'''


class TestFreshProcessRedraw:
    def test_a_new_interpreter_rebuilds_the_card_from_disk(self, isolated_memory, tmp_path):
        """重开程序也要看得到上一轮结果——所以来源不能只有内存里的 render_log。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.record_evidence(sess.verification, kind="tests", checker="pytest",
                                     status="failed", exit_code=1, argv=["py", "-m", "pytest"])
        _finish(sess, "unverified", "测试仍有失败项")

        script = tmp_path / "child.py"
        script.write_text(_CHILD, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(script),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             str(isolated_memory.parent), sid],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=env)
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        assert out["title"] == "本轮结束，仍有未验证事项"
        assert out["files"] == ["src/a.py"]
        assert out["validations"] == [["pytest", "failed", 1, False]]
        assert out["display_source"] == "restored"
        assert out["has_pending"] is True


# ══════════════════════════════════════════════════════════════
# 统一收尾的接线
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def loop_env(monkeypatch, isolated_memory, tmp_path):
    from src import agent as _agent
    from src.models import MODEL_LIST
    sess = _new_session()
    sess.agent_mode = "act"
    _saved(sess)
    sess.project = str(tmp_path)
    # 重载后模型索引会回到默认（默认可能是 Claude Code），显式挑一个 API 模型，
    # 免得误触发真实 CLI。
    sess.current_model_index = next(
        i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    session.bind_thread(sess)
    session.set_active(sess)
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "_claude_code_loop",
                        lambda ui: (_ for _ in ()).throw(AssertionError("不该调 CLI")))
    yield _agent, sess
    session.unbind_thread()


class TestAgentLoopDelivery:
    def test_normal_run_delivers_one_result(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("好了", [], {"input": 0, "output": 0, "total": 0}, None))
        ui = _UI()
        _agent.agent_loop(ui)

        assert len(ui.results) == 1
        _, run_id, snapshot = ui.results[0]
        assert run_id == sess.last_run["id"] == snapshot["run_id"]
        assert snapshot["outcome"] == "completed"
        assert sess.last_result["run_id"] == run_id

    def test_exception_path_also_delivers(self, loop_env, monkeypatch):
        _agent, sess = loop_env

        def _boom(ui):
            raise RuntimeError("provider 崩了")

        monkeypatch.setattr(_agent, "_stream_with_tools", _boom)
        ui = _UI()
        _agent.agent_loop(ui)
        assert ui.results and ui.results[-1][2]["outcome"] == "failed"

    def test_cancelled_path_delivers(self, loop_env):
        _agent, sess = loop_env
        sess.stop_flag = True
        ui = _UI()
        _agent.agent_loop(ui)
        assert ui.results and ui.results[-1][2]["outcome"] == "cancelled"

    def test_claude_cli_success_still_reports_unverified(self, loop_env, monkeypatch):
        """CLI 的外部验收无法核实——不能凭它的 success 宣称检查通过。"""
        _agent, sess = loop_env
        from src.models import MODEL_LIST
        sess.current_model_index = next(
            i for i, m in enumerate(MODEL_LIST) if m[1] == "claude-code")
        monkeypatch.setattr(_agent, "_claude_code_loop",
                            lambda ui: AgentResult("unverified", "外部工具的验证结果无法核实"))
        ui = _UI()
        _agent.agent_loop(ui)

        view = _describe(ui.results[-1][2])
        assert view["title"] == "本轮结束，仍有未验证事项"
        assert view["validations"] == []

    def test_subagent_runs_deliver_nothing(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        sess.is_subagent = True
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("好了", [], {"input": 0, "output": 0, "total": 0}, None))
        ui = _UI()
        _agent.agent_loop(ui)
        assert ui.results == []

    def test_no_fake_ai_message_is_appended_for_the_card(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("好了", [], {"input": 0, "output": 0, "total": 0}, None))
        before = len(sess.chat_history)
        ui = _UI()
        _agent.agent_loop(ui)
        # 只多了模型那条真实回复，没有为了显示卡片而伪造的 AIMessage
        assert len(sess.chat_history) == before + 1
        assert sess.chat_history[-1].content == "好了"
