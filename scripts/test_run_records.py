"""运行记录、统一收尾、独立 inflight 与待验证义务（B03）。

这一批要回答的是三个"程序自己也说不清"的问题：

1. **上一轮到底怎么结束的？** 正常完成、用户停止、异常、提前返回必须都留下记录，
   而且是同一条路径留的。写在主循环内部的话，每加一个 `return` 就多一个漏网路径。
2. **崩溃的那一刻，那个写文件的工具执行了没有？** 这一段不是原子写能消除的。
   能做的是有记录地说"结果未知"，既不说成功、也不说没执行，更不自动重放。
3. **上一轮改了没验证的东西，下一轮还记得吗？** `reset_verification` 每轮清空 dirty 文件，
   恢复逻辑排在它前面就等于每轮开头静默清零。

贯穿全文的取舍：**运行结果与保存结果分开**。保存失败不能冒充保存成功，
也不能把已经发生的操作抹掉。
"""
import json
import os
import subprocess
import sys
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src import memory, paths, run_records, session, state, verification
from src.agent_result import AgentResult


def _in_test_thread(data_dir, sess, fn):
    """子线程里跑一段测试代码。

    `paths.set_data_dir` 是**线程本地**的：不在子线程里显式设一遍，它会回落到真实的
    APP_DIR，测试就会往仓库的 chat_memory/ 里写东西。
    """
    def _run():
        paths.set_data_dir(data_dir)
        session.bind_thread(sess)
        try:
            fn()
        finally:
            session.unbind_thread()
            paths.set_data_dir(None)
    return _run


class _UI:
    def __init__(self):
        self.messages = []

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


def _read(mem_dir, sid):
    return json.loads((mem_dir / f"{sid}.json").read_text(encoding="utf-8"))


def _saved(sess):
    """存一次盘并返回 session_id。"""
    memory.save_session(session=sess)
    return sess.current_session_id


# ══════════════════════════════════════════════════════════════
# 运行身份
# ══════════════════════════════════════════════════════════════

class TestRunIdentity:
    def test_run_id_is_independent_of_session_and_task(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)

        assert run["id"] != sess.current_session_id
        assert run["task_id"] is None, "B03 还没有任务身份机制，task_id 必须允许为空"
        assert run["phase"] == "running"
        assert run["outcome"] is None, "还没结束就不该有 outcome"
        assert run["started_at"]

    def test_each_run_gets_a_fresh_id(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        first = run_records.begin_run(sess)["id"]
        run_records.finalize_run(sess, sess.last_run, AgentResult("completed"))
        second = run_records.begin_run(sess)["id"]
        assert first != second

    def test_source_is_the_last_real_user_message(self, isolated_memory):
        sess = _new_session()
        sess.chat_history.append(HumanMessage(content="顺便加个日志"))
        _saved(sess)
        run = run_records.begin_run(sess)
        assert run["source"]["kind"] == "user_message"
        assert run["source"]["text"] == "顺便加个日志"

    def test_auto_repair_prompt_is_not_treated_as_a_user_request(self, isolated_memory):
        """自动修复提示是程序写的，不是新的真实用户要求。

        不跳过的话，一轮自动修复就把"用户要求了什么"改写成程序自己生成的诊断文字——
        恰恰在最需要这条线索的长任务里最先失真。
        """
        sess = _new_session()
        sess.chat_history.append(HumanMessage(
            content="⚠️ 自动诊断：刚才的 run_tests 失败了",
            additional_kwargs={"lingxi_internal": True}))
        _saved(sess)
        run = run_records.begin_run(sess)
        assert run["source"]["text"] == "修一下登录"
        assert run["source"]["internal_skipped"] == 1

    def test_internal_marker_survives_save_and_load(self, isolated_memory):
        """标记必须跟着落盘：重开会话后来源判定要还能跳过它。"""
        sess = _new_session()
        sess.chat_history.append(HumanMessage(
            content="[内部验证要求]", additional_kwargs={"lingxi_internal": True}))
        sid = _saved(sess)

        restored = session.Session()
        assert memory.load_session(sid, session=restored) is True
        assert restored.chat_history[-1].additional_kwargs.get("lingxi_internal") is True
        assert run_records.describe_source(restored.chat_history)["text"] == "修一下登录"

    def test_image_only_message_still_yields_a_source(self, isolated_memory):
        sess = _new_session()
        sess.chat_history.append(HumanMessage(content=[{"type": "image_url", "image_url": {}}]))
        _saved(sess)
        assert run_records.begin_run(sess)["source"]["text"] == "[图片]"

    def test_no_user_message_reports_unknown_not_a_guess(self, isolated_memory):
        sess = _new_session()
        sess.chat_history = [SystemMessage(content="sys"), AIMessage(content="hi")]
        sess.current_session_id = "manual"
        assert run_records.describe_source(sess.chat_history)["kind"] == "unknown"

    def test_subagent_runs_are_not_recorded(self, isolated_memory):
        sess = _new_session(is_subagent=True)
        assert run_records.begin_run(sess) is None
        assert sess.last_run is None


# ══════════════════════════════════════════════════════════════
# 统一收尾
# ══════════════════════════════════════════════════════════════

class TestFinalize:
    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled",
                                        "unverified", "limit_reached"])
    def test_every_terminal_status_lands_on_disk(self, isolated_memory, status):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        report = run_records.finalize_run(sess, run, AgentResult(status, "原因"))

        assert report.saved is True
        stored = _read(isolated_memory, sid)["progress"]["last_run"]
        assert stored["phase"] == "ended"
        assert stored["outcome"] == status
        assert stored["reason"] == "原因"
        assert stored["ended_at"]

    def test_repeat_finalize_does_not_write_a_second_record(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        run_records.finalize_run(sess, run, AgentResult("completed", "第一次"))
        first_rev = _read(isolated_memory, sid)["progress"]["revision"]

        again = run_records.finalize_run(sess, run, AgentResult("failed", "第二次"))

        assert again.duplicate is True
        assert again.saved is False
        stored = _read(isolated_memory, sid)
        assert stored["progress"]["last_run"]["outcome"] == "completed"
        assert stored["progress"]["last_run"]["reason"] == "第一次"
        assert stored["progress"]["revision"] == first_rev, "重复收尾不该再存一次盘"

    def test_late_finalize_from_an_old_run_does_not_overwrite_the_new_one(self, isolated_memory):
        """同一会话里旧 run 的迟到收尾：新运行的状态不能被它盖掉。

        真实场景是用户点"停止"后紧接着又发了一条——旧 worker 还在退出、新 worker 已经起来。
        """
        sess = _new_session()
        sid = _saved(sess)
        old_run = run_records.begin_run(sess)
        new_run = run_records.begin_run(sess)          # 新一轮开始，旧的还没收尾

        report = run_records.finalize_run(sess, old_run, AgentResult("completed", "旧的"))

        assert report.stale is True
        assert report.saved is False
        assert sess.last_run["id"] == new_run["id"]
        assert sess.last_run["phase"] == "running"

        run_records.finalize_run(sess, new_run, AgentResult("cancelled", "新的"))
        stored = _read(isolated_memory, sid)["progress"]["last_run"]
        assert stored["id"] == new_run["id"]
        assert stored["outcome"] == "cancelled"

    def test_two_sessions_do_not_cross_contaminate(self, isolated_memory):
        first = _new_session()
        second = _new_session()
        second.chat_history[1] = HumanMessage(content="另一个任务")
        sid_a, sid_b = _saved(first), _saved(second)
        assert sid_a != sid_b

        run_a = run_records.begin_run(first)
        run_b = run_records.begin_run(second)
        run_records.finalize_run(second, run_b, AgentResult("failed", "B 失败"))
        run_records.finalize_run(first, run_a, AgentResult("completed", "A 完成"))

        stored_a = _read(isolated_memory, sid_a)["progress"]["last_run"]
        stored_b = _read(isolated_memory, sid_b)["progress"]["last_run"]
        assert stored_a["id"] == run_a["id"] and stored_a["outcome"] == "completed"
        assert stored_b["id"] == run_b["id"] and stored_b["outcome"] == "failed"

    def test_concurrent_finalize_from_two_threads_keeps_each_session_intact(self, isolated_memory):
        sessions = []
        for i in range(2):
            sess = _new_session()
            sess.chat_history[1] = HumanMessage(content=f"任务 {i}")
            _saved(sess)
            sessions.append((sess, run_records.begin_run(sess)))

        errors = []
        data_dir = paths.get_data_dir()

        def _make(sess, run, status):
            def _finish():
                try:
                    run_records.finalize_run(sess, run, AgentResult(status))
                except Exception as error:   # pragma: no cover - 失败时要看到原因
                    errors.append(error)
            return _in_test_thread(data_dir, sess, _finish)

        threads = [threading.Thread(target=_make(s, r, st))
                   for (s, r), st in zip(sessions, ["completed", "cancelled"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        for (sess, run), status in zip(sessions, ["completed", "cancelled"]):
            stored = _read(isolated_memory, sess.current_session_id)["progress"]["last_run"]
            assert stored["id"] == run["id"]
            assert stored["outcome"] == status

    def test_save_failure_does_not_masquerade_as_saved(self, isolated_memory, monkeypatch):
        """保存失败仍如实返回运行结果，但 saved=False 且用户看得到独立提示。"""
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)

        def _boom(path, data, **kwargs):
            raise OSError("磁盘满了")

        monkeypatch.setattr(memory, "_atomic_write_json", _boom)
        ui = _UI()
        report = run_records.finalize_run(sess, run, AgentResult("completed", "做完了"), ui=ui)

        assert report.result.status == "completed", "保存失败不能把运行结果改掉"
        assert report.saved is False
        assert "磁盘满了" in report.save.error.args[0]
        assert "最新进度未保存" in ui.text()

    def test_interrupted_run_stays_running_on_disk(self, isolated_memory):
        """进程在收尾之前退出：磁盘上留下 phase=running。

        重开时展示"上次运行被中断"，**不凭空生成**一个 completed / failed。
        """
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        memory.save_session(session=sess)               # 模拟工具边界的那次保存

        restored = session.Session()
        memory.load_session(sid, session=restored)

        assert restored.last_run["phase"] == "running"
        assert restored.last_run["outcome"] is None
        assert restored.active_run_id is None, "加载不等于在跑"
        assert "中断" in run_records.describe_last_run(restored.last_run)
        # 磁盘事实不被加载改写
        assert _read(isolated_memory, sid)["progress"]["last_run"]["phase"] == "running"


# ══════════════════════════════════════════════════════════════
# agent_loop 的统一 begin/finalize
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def loop_env(monkeypatch, isolated_memory, tmp_path):
    """真实走 agent_loop，只把模型流式换成桩。"""
    from src import agent as _agent
    from src.models import MODEL_LIST

    sess = _new_session()
    sess.agent_mode = "act"
    sess.project = str(tmp_path)
    sess.current_model_index = next(
        i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    _saved(sess)
    session.bind_thread(sess)
    session.set_active(sess)
    # 收尾里的标题生成会真调模型，评测之外一律挡掉。
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    yield _agent, sess
    session.unbind_thread()


class TestAgentLoopBoundary:
    def test_normal_completion_is_recorded(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("做完了", [], {"input": 1, "output": 1, "total": 2}, None))
        result = _agent.agent_loop(_UI())

        assert result.status == "completed"
        assert sess.last_run["phase"] == "ended"
        assert sess.last_run["outcome"] == "completed"
        assert sess.active_run_id is None

    def test_cancellation_is_recorded(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        sess.stop_flag = True
        result = _agent.agent_loop(_UI())

        assert result.status == "cancelled"
        assert sess.last_run["outcome"] == "cancelled"
        assert sess.last_run["phase"] == "ended"

    def test_exception_is_recorded_not_swallowed_into_running(self, loop_env, monkeypatch):
        _agent, sess = loop_env

        def _explode(ui):
            raise RuntimeError("provider 崩了")

        monkeypatch.setattr(_agent, "_stream_with_tools", _explode)
        result = _agent.agent_loop(_UI())

        assert result.status == "failed"
        assert sess.last_run["phase"] == "ended"
        assert sess.last_run["outcome"] == "failed"
        assert "provider 崩了" in sess.last_run["reason"]

    def test_base_exception_still_finalizes(self, loop_env, monkeypatch):
        """KeyboardInterrupt 不是 Exception，却同样需要留下结束记录。"""
        _agent, sess = loop_env

        def _interrupt(ui):
            raise KeyboardInterrupt()

        monkeypatch.setattr(_agent, "_stream_with_tools", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            _agent.agent_loop(_UI())

        assert sess.last_run["phase"] == "ended"
        assert sess.last_run["outcome"] == "failed", "没有 AgentResult 时按预置的失败态记录"

    def test_early_return_from_ollama_branch_is_recorded(self, loop_env, monkeypatch):
        """提前返回也要收尾——这类路径最容易在主循环内部漏掉。"""
        _agent, sess = loop_env
        from src.models import MODEL_LIST
        sess.current_model_index = next(i for i, m in enumerate(MODEL_LIST) if m[1] == "ollama")
        monkeypatch.setattr(_agent, "check_ollama", lambda: False)

        result = _agent.agent_loop(_UI())

        assert result.status == "failed"
        assert sess.last_run["phase"] == "ended"
        assert sess.last_run["outcome"] == "failed"

    def test_claude_code_branch_is_recorded(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        from src.models import MODEL_LIST
        sess.current_model_index = next(
            i for i, m in enumerate(MODEL_LIST) if m[1] == "claude-code")
        monkeypatch.setattr(_agent, "_claude_code_loop",
                            lambda ui: AgentResult("unverified", "外部验收无法核实"))

        result = _agent.agent_loop(_UI())

        assert result.status == "unverified"
        assert sess.last_run["outcome"] == "unverified"
        assert "无法核实" in sess.last_run["reason"]

    def test_evidence_records_changed_files_and_validation(self, loop_env, monkeypatch):
        _agent, sess = loop_env
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("好了", [], {"input": 0, "output": 0, "total": 0}, None))
        # 模拟本轮改过文件并跑过测试（在 begin_run 重置之后发生）
        original = run_records.begin_run

        def _begin_then_dirty(s, **kwargs):
            run = original(s, **kwargs)
            verification.mark_dirty(s.verification, "src/a.py")
            verification.mark_tests(s.verification, True, "3 passed")
            verification.mark_diff_reviewed(s.verification)
            return run

        monkeypatch.setattr(run_records, "begin_run", _begin_then_dirty)
        _agent.agent_loop(_UI())

        evidence = sess.last_run["evidence"]
        assert evidence["changed_files"] == ["src/a.py"]
        assert evidence["diff_reviewed"] is True
        assert {"kind": "tests", "passed": True, "reason": "3 passed"} in evidence["validation_runs"]


# ══════════════════════════════════════════════════════════════
# 独立 inflight sidecar
# ══════════════════════════════════════════════════════════════

class TestInflight:
    def test_sidecar_is_a_separate_small_file(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                    args={"path": "src/a.py", "content": "x" * 50000})

        path = isolated_memory / f"{sid}.inflight.json"
        assert path.exists()
        doc = json.loads(path.read_text(encoding="utf-8"))
        entry = doc["operations"][0]
        assert entry["tool"] == "write_file"
        assert entry["paths"] == ["src/a.py"]
        assert entry["state"] == "dispatched", "已调度 ≠ 副作用已发生"
        assert path.stat().st_size < 2000, "sidecar 不该复制长参数"
        assert "content" not in json.dumps(entry)
        # 不写进主 JSON
        assert "inflight" not in json.dumps(_read(isolated_memory, sid))

    def test_record_carries_the_identity_needed_to_match_a_receipt(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="run_command", tool_call_id="c1",
                                         args={"command": "npm i"})
        assert op.session_id == sid
        assert op.run_id == run["id"]
        assert op.operation_id and op.operation_id != op.run_id
        assert op.base_revision == sess.progress_revision

    def test_two_operations_do_not_overwrite_each_other(self, isolated_memory):
        """同会话并发：sidecar 是列表，一条记录结构上不可能盖掉另一条未完成的。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        first = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                            args={"path": "a.py"})
        second = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c2",
                                             args={"path": "b.py"})

        doc = json.loads((isolated_memory / f"{sid}.inflight.json").read_text(encoding="utf-8"))
        ids = {op["operation_id"] for op in doc["operations"]}
        assert ids == {first.operation_id, second.operation_id}

        run_records.commit_operation(sess, first)
        doc = json.loads((isolated_memory / f"{sid}.inflight.json").read_text(encoding="utf-8"))
        assert [op["operation_id"] for op in doc["operations"]] == [second.operation_id]

    def test_concurrent_begin_operation_keeps_every_record(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        made = []
        lock = threading.Lock()
        data_dir = paths.get_data_dir()

        def _make(index):
            def _begin():
                op = run_records.begin_operation(
                    sess, tool="edit_file", tool_call_id=f"c{index}", args={"path": f"{index}.py"})
                with lock:
                    made.append(op)
            return _in_test_thread(data_dir, sess, _begin)

        threads = [threading.Thread(target=_make(i)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        doc = json.loads((isolated_memory / f"{sid}.inflight.json").read_text(encoding="utf-8"))
        assert len(doc["operations"]) == 8
        assert {op["operation_id"] for op in doc["operations"]} == {o.operation_id for o in made}

    def test_write_failure_raises_so_the_tool_is_not_executed(self, isolated_memory, monkeypatch):
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        monkeypatch.setattr(run_records, "_write_inflight",
                            lambda sid, doc: (_ for _ in ()).throw(OSError("只读磁盘")))

        with pytest.raises(run_records.InflightWriteError) as caught:
            run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                        args={"path": "a.py"})
        assert "只读磁盘" in str(caught.value)

    def test_unreadable_sidecar_aborts_instead_of_overwriting(self, isolated_memory):
        """读不懂现有 sidecar 时不能新建覆盖——那会抹掉另一个未完成操作的唯一线索。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        (isolated_memory / f"{sid}.inflight.json").write_text("{坏", encoding="utf-8")

        with pytest.raises(run_records.InflightWriteError):
            run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                        args={"path": "a.py"})
        assert (isolated_memory / f"{sid}.inflight.json").read_text(encoding="utf-8") == "{坏"

    def test_commit_clears_only_after_the_snapshot_lands(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        sess.chat_history.append(ToolMessage(content="写好了", tool_call_id="c1"))

        report = run_records.commit_operation(sess, op)

        assert report.committed is True and report.marker_cleared is True
        assert not (isolated_memory / f"{sid}.inflight.json").exists()
        receipt = _read(isolated_memory, sid)["progress"]["last_committed_operation"]
        assert receipt["operation_id"] == op.operation_id
        assert receipt["run_id"] == op.run_id
        assert receipt["revision"] == _read(isolated_memory, sid)["progress"]["revision"]

    def test_commit_keeps_the_marker_when_the_snapshot_fails(self, isolated_memory, monkeypatch):
        """保存失败时**绝不能先删标记**——那等于把唯一线索丢了。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="run_command", tool_call_id="c1",
                                         args={"command": "rm x"})
        monkeypatch.setattr(memory, "_atomic_write_json",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("写不进去")))

        ui = _UI()
        report = run_records.commit_operation(sess, op, ui=ui)

        assert report.committed is False and report.marker_cleared is False
        assert (isolated_memory / f"{sid}.inflight.json").exists()
        assert "写不进去" in str(report.save.error)
        assert "结果未知" in ui.text()

    def test_receipt_revision_is_not_rewritten_by_later_saves(self, isolated_memory):
        """回执要钉在它当初落盘的那一版，不能跟着后续保存漂移。"""
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        run_records.commit_operation(sess, op)
        pinned = _read(isolated_memory, sid)["progress"]["last_committed_operation"]["revision"]

        sess.chat_history.append(AIMessage(content="又一轮"))
        memory.save_session(session=sess)
        after = _read(isolated_memory, sid)["progress"]
        assert after["revision"] > pinned
        assert after["last_committed_operation"]["revision"] == pinned

    def test_no_session_id_yet_reports_honestly(self, isolated_memory):
        """首次保存前会话文件都不存在，恢复无从谈起——返回 None，不假装记录已建立。"""
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="sys")]
        assert run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                           args={"path": "a.py"}) is None

    def test_subagent_writes_no_sidecar(self, isolated_memory):
        sess = _new_session(is_subagent=True, current_session_id="sub1")
        assert run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                           args={"path": "a.py"}) is None
        assert not (isolated_memory / "sub1.inflight.json").exists()

    def test_deleting_a_session_removes_its_sidecar(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                    args={"path": "a.py"})
        assert (isolated_memory / f"{sid}.inflight.json").exists()

        memory.delete_session(sid)
        assert not (isolated_memory / f"{sid}.inflight.json").exists()
        assert [s["id"] for s in memory.list_sessions("__all__")] == []


# ══════════════════════════════════════════════════════════════
# 中断窗口
# ══════════════════════════════════════════════════════════════

class TestInterruptWindows:
    """四个时点各崩一次，看程序说了什么。判据始终是身份 + 回执，不是 revision 大小。"""

    def _prepare(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        memory.save_session(session=sess)
        return sess, sid

    def test_window_a_marker_written_tool_not_returned(self, isolated_memory):
        """① inflight 已写、工具还没返回 → 结果未知。"""
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="append_file", tool_call_id="c1",
                                         args={"path": "log.txt"})
        entries, error = run_records.classify_inflight(sid)

        assert not error
        assert [e["status"] for e in entries] == ["unknown"]
        assert entries[0]["operation"]["operation_id"] == op.operation_id
        assert "不自动重放" in entries[0]["detail"]

    def test_window_b_tool_ran_but_snapshot_not_saved(self, isolated_memory, monkeypatch):
        """② 工具执行完、主快照还没写 → 依然结果未知，标记留着。"""
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="append_file", tool_call_id="c1",
                                         args={"path": "log.txt"})
        sess.chat_history.append(ToolMessage(content="已追加", tool_call_id="c1"))
        monkeypatch.setattr(memory, "_atomic_write_json",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("断电")))
        run_records.commit_operation(sess, op)
        monkeypatch.undo()

        entries, _ = run_records.classify_inflight(sid)
        assert [e["status"] for e in entries] == ["unknown"]

    def test_window_c_snapshot_saved_marker_not_cleared(self, isolated_memory, monkeypatch):
        """③ 主快照已提交、sidecar 还没清 → 认出来是 committed，可安全清理。"""
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="append_file", tool_call_id="c1",
                                         args={"path": "log.txt"})
        sess.chat_history.append(ToolMessage(content="已追加", tool_call_id="c1"))
        monkeypatch.setattr(run_records, "_clear_operation",
                            lambda s, sid_, oid: (False, "模拟清理失败"))
        report = run_records.commit_operation(sess, op)
        monkeypatch.undo()

        assert report.committed is True and report.marker_cleared is False
        entries, _ = run_records.classify_inflight(sid)
        assert [e["status"] for e in entries] == ["committed"]

        unknown, _ = run_records.sweep_committed(sess, sid)
        assert unknown == []
        assert not (isolated_memory / f"{sid}.inflight.json").exists()

    def test_window_d_earlier_receipt_still_recognised_after_a_later_commit(self, isolated_memory,
                                                                           monkeypatch):
        """A 的清理失败、B 又提交了：A 不能因为"回执已经是 B 的"就退回结果未知。"""
        sess, sid = self._prepare(isolated_memory)
        first = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                            args={"path": "a.py"})
        monkeypatch.setattr(run_records, "_clear_operation", lambda s, i, o: (False, "失败"))
        run_records.commit_operation(sess, first)
        monkeypatch.undo()

        second = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c2",
                                             args={"path": "b.py"})
        run_records.commit_operation(sess, second)

        entries, _ = run_records.classify_inflight(sid)
        statuses = {e["operation"]["operation_id"]: e["status"] for e in entries}
        assert statuses.get(first.operation_id) == "committed"

    def test_a_bigger_revision_alone_never_counts_as_committed(self, isolated_memory):
        """只看 revision 变大是错的：别的保存同样推进它。"""
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="run_command", tool_call_id="c1",
                                         args={"command": "npm i"})
        for _ in range(3):
            sess.chat_history.append(AIMessage(content="无关的一轮"))
            memory.save_session(session=sess)

        assert _read(isolated_memory, sid)["progress"]["revision"] > op.base_revision
        entries, _ = run_records.classify_inflight(sid)
        assert [e["status"] for e in entries] == ["unknown"]

    def test_receipt_from_another_session_is_not_accepted(self, isolated_memory):
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        forged = {"operation_id": op.operation_id, "run_id": op.run_id,
                  "session_id": "别的会话", "revision": 99}
        entries, _ = run_records.classify_inflight(sid, last_receipt=forged, recent_receipts=[])
        assert [e["status"] for e in entries] == ["unknown"]

    def test_receipt_from_another_run_is_not_accepted(self, isolated_memory):
        sess, sid = self._prepare(isolated_memory)
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        forged = {"operation_id": op.operation_id, "run_id": "run-别的",
                  "session_id": sid, "revision": 99}
        entries, _ = run_records.classify_inflight(sid, last_receipt=forged, recent_receipts=[])
        assert [e["status"] for e in entries] == ["unknown"]

    def test_load_sweeps_committed_but_keeps_unknown_for_diagnosis(self, isolated_memory,
                                                                   monkeypatch):
        sess, sid = self._prepare(isolated_memory)
        done = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                           args={"path": "a.py"})
        monkeypatch.setattr(run_records, "_clear_operation", lambda s, i, o: (False, "失败"))
        run_records.commit_operation(sess, done)
        monkeypatch.undo()
        stuck = run_records.begin_operation(sess, tool="run_command", tool_call_id="c2",
                                            args={"command": "npm i"})

        restored = session.Session()
        memory.load_session(sid, session=restored)

        doc = json.loads((isolated_memory / f"{sid}.inflight.json").read_text(encoding="utf-8"))
        remaining = [op["operation_id"] for op in doc["operations"]]
        assert remaining == [stuck.operation_id]
        assert done.operation_id not in remaining


# ══════════════════════════════════════════════════════════════
# 正文成功但索引失败
# ══════════════════════════════════════════════════════════════

class TestBodyOkIndexFails:
    def test_body_written_index_failed_is_reported_step_by_step(self, isolated_memory,
                                                                monkeypatch):
        sess = _new_session()
        sid = _saved(sess)
        monkeypatch.setattr(memory, "_update_index",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("索引写不了")))
        sess.chat_history.append(AIMessage(content="新内容"))
        outcome = memory.save_session_report(session=sess)

        assert outcome.body_written is True
        assert outcome.index_written is False
        assert outcome.fully_saved is False
        assert "索引写不了" in str(outcome.error)
        assert _read(isolated_memory, sid)["messages"][-1]["content"] == "新内容"

    def test_marker_is_cleared_because_the_receipt_did_land(self, isolated_memory, monkeypatch):
        """索引失败**不影响**回执：它在正文里，所以 sidecar 可以清。

        用一个成功布尔值推断全部存盘步骤的话，这里会误判成"没存上"而留下标记，
        下次启动就会为一个其实已经提交的操作报"结果未知"。
        """
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        monkeypatch.setattr(memory, "_update_index",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("索引写不了")))
        ui = _UI()
        report = run_records.commit_operation(sess, op, ui=ui)

        assert report.committed is True
        assert report.marker_cleared is True
        assert report.save.fully_saved is False
        assert "索引" in ui.text(), "索引失败要照实说，不能静默"
        assert _read(isolated_memory, sid)["progress"]["last_committed_operation"][
            "operation_id"] == op.operation_id

    def test_original_error_survives_a_failing_cleanup(self, isolated_memory, monkeypatch):
        """清理失败不能遮住原始错误。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        monkeypatch.setattr(run_records, "_clear_operation",
                            lambda s, i, o: (False, "删不掉临时文件"))
        report = run_records.commit_operation(sess, op)

        assert report.committed is True
        assert report.marker_cleared is False
        assert report.clear_error == "删不掉临时文件"

    def test_save_session_still_raises_for_existing_callers(self, isolated_memory, monkeypatch):
        sess = _new_session()
        _saved(sess)
        monkeypatch.setattr(memory, "_update_index",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("索引写不了")))
        sess.chat_history.append(AIMessage(content="x"))
        with pytest.raises(OSError):
            memory.save_session(session=sess)


# ══════════════════════════════════════════════════════════════
# 待验证义务
# ══════════════════════════════════════════════════════════════

class TestPendingVerification:
    def test_unverified_changes_are_persisted(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, run, AgentResult("unverified", "没跑测试"))

        pending = _read(isolated_memory, sid)["progress"]["pending_verification"]
        assert pending["files"] == ["src/a.py"]
        assert pending["code_files"] == ["src/a.py"]
        assert "尚未运行测试" in pending["reason"]
        assert pending["run_id"] == run["id"]

    def test_next_run_init_does_not_drop_unresolved_obligations(self, isolated_memory):
        """核心：`reset_verification` 清空 dirty 文件，恢复必须排在它之后。"""
        sess = _new_session()
        _saved(sess)
        first = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, first, AgentResult("unverified"))

        run_records.begin_run(sess)              # 用户只回了一句"继续"
        assert sess.verification["dirty_files"] == ["src/a.py"]
        assert sess.verification["code_dirty_files"] == ["src/a.py"]
        assert verification.get_verification_gaps(sess.verification), "闸门仍要求补验证"

    def test_obligations_survive_a_restart(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, run, AgentResult("unverified"))

        restored = session.Session()
        memory.load_session(sid, session=restored)
        assert restored.pending_verification["files"] == ["src/a.py"]

        run_records.begin_run(restored)
        assert restored.verification["dirty_files"] == ["src/a.py"]

    def test_historic_test_success_is_not_a_current_pass(self, isolated_memory):
        """上一轮测试过了但之后又改了文件；下一轮开头不能把成功恢复成通行状态。"""
        sess = _new_session()
        _saved(sess)
        first = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.mark_tests(sess.verification, True, "3 passed")
        verification.mark_dirty(sess.verification, "src/b.py")     # 测试之后又改
        run_records.finalize_run(sess, first, AgentResult("unverified"))

        run_records.begin_run(sess)
        assert sess.verification["tests_run"] is False
        assert sess.verification["tests_passed"] is None
        assert set(sess.verification["dirty_files"]) == {"src/a.py", "src/b.py"}

    def test_fully_verified_run_clears_the_obligation(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.mark_tests(sess.verification, True, "3 passed")
        verification.mark_diff_reviewed(sess.verification)
        run_records.finalize_run(sess, run, AgentResult("completed"))

        pending = _read(isolated_memory, sid)["progress"]["pending_verification"]
        assert pending["files"] == [] and pending["reason"] == ""

        run_records.begin_run(sess)
        assert sess.verification["dirty_files"] == []

    def test_plan_ticks_never_become_verification_evidence(self, isolated_memory):
        """计划打勾是模型自述，与程序验证结果完全分离。"""
        sess = _new_session()
        sess.current_plan = [{"text": "修复并验证", "status": "done"}]
        _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, run, AgentResult("unverified"))

        assert sess.pending_verification["files"] == ["src/a.py"]
        assert verification.get_verification_gaps(sess.verification)

    def test_tracking_incomplete_reason_is_kept_and_can_be_resolved(self, isolated_memory,
                                                                    tmp_path):
        """枚举不完整的原因要跨轮保留——但也要有出口，否则闸门永远过不去。"""
        sess = _new_session()
        _saved(sess)
        root = str(tmp_path / "proj")
        os.makedirs(root, exist_ok=True)
        run = run_records.begin_run(sess)
        sess.verification["tracking_errors"] = {root: "枚举项目文件超时"}
        sess.verification["dirty_files"] = ["src/a.py"]
        run_records.finalize_run(sess, run, AgentResult("unverified"))
        assert sess.pending_verification["tracking_incomplete"] == {root: "枚举项目文件超时"}

        run_records.begin_run(sess)
        assert sess.verification["tracking_errors"] == {root: "枚举项目文件超时"}
        # 下一轮能完整枚举了 → 原因消掉（但那一轮的改动仍然照常标脏）
        verification.get_verification_gaps(sess.verification)
        assert sess.verification["tracking_errors"] == {}

    def test_new_session_starts_without_inherited_obligations(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, run, AgentResult("unverified"))

        memory.reset_history(session=sess)
        assert sess.pending_verification["files"] == []
        assert sess.last_run is None
        assert sess.last_committed_operation is None
        assert sess.progress_revision == 0


# ══════════════════════════════════════════════════════════════
# 格式兼容与字段闭合
# ══════════════════════════════════════════════════════════════

class TestFormatCompatibility:
    def test_old_file_without_new_fields_loads_normally(self, isolated_memory):
        sid = "20240101_000000_000000"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "旧会话", "updated": "2024-01-01",
            "messages": [{"type": "HumanMessage", "content": "你好"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        assert sess.last_run is None
        assert sess.pending_verification == run_records.empty_pending_verification()
        assert sess.progress_error == ""
        assert len(sess.chat_history) == 1

    def test_new_fields_round_trip_save_load_save(self, isolated_memory):
        """写进去 → 读出来 → 再写回去，不能被归一化悄悄丢掉。"""
        sess = _new_session()
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        op = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                         args={"path": "a.py"})
        sess.chat_history.append(ToolMessage(content="ok", tool_call_id="c1"))
        run_records.commit_operation(sess, op)
        verification.mark_dirty(sess.verification, "src/a.py")
        run_records.finalize_run(sess, run, AgentResult("unverified", "没跑测试"))
        first = _read(isolated_memory, sid)["progress"]

        restored = session.Session()
        memory.load_session(sid, session=restored)
        restored.chat_history.append(AIMessage(content="再存一次"))
        memory.save_session(session=restored)
        second = _read(isolated_memory, sid)["progress"]

        for key in ("last_run", "pending_verification", "last_committed_operation"):
            assert second[key] == first[key], f"{key} 在二次保存后被改掉了"
        assert second["recent_operations"] == first["recent_operations"]
        assert second["revision"] == first["revision"] + 1

    @pytest.mark.parametrize("bad, expect", [
        ({"phase": "飞了", "id": "r1"}, "phase"),
        ({"phase": "ended", "id": "r1", "outcome": "差不多完成了"}, "outcome"),
        ({"phase": "ended"}, "id"),
        ("不是对象", "不是对象"),
        ({"phase": "ended", "id": "r1", "version": 99}, "无法识别"),
    ])
    def test_corrupt_last_run_voids_progress_but_not_chat(self, isolated_memory, bad, expect):
        sid = "20240101_000000_000001"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "坏记录", "updated": "2024-01-01",
            "schema_version": 2,
            "progress": {"version": 1, "revision": 3,
                         "current_plan": [{"text": "做事", "status": "done"}],
                         "last_run": bad},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        assert len(sess.chat_history) == 2, "进度坏掉不能拖垮聊天历史"
        assert sess.last_run is None
        assert sess.current_plan == [], "整块作废，不能只挑坏的那个字段丢"
        assert expect in sess.progress_error

    def test_corrupt_pending_verification_voids_progress(self, isolated_memory):
        sid = "20240101_000000_000002"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "坏义务", "updated": "2024-01-01", "schema_version": 2,
            "progress": {"version": 1, "pending_verification": {"files": [1, 2]}},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        assert len(sess.chat_history) == 2
        assert "非字符串路径" in sess.progress_error

    def test_bad_receipts_are_dropped_not_fatal(self, isolated_memory):
        """回执是附加证据：坏一条只让对应操作退回未知，不该让整块进度作废。"""
        last, recent = run_records.normalize_receipts(
            {"operation_id": "op1", "run_id": "r1", "session_id": "s1", "revision": 4},
            [{"operation_id": 123}, "垃圾",
             {"operation_id": "op0", "run_id": "r1", "session_id": "s1"}])
        assert last["operation_id"] == "op1" and last["revision"] == 4
        assert [r["operation_id"] for r in recent] == ["op0", "op1"]

    def test_unknown_progress_is_quarantined_before_being_overwritten(self, isolated_memory):
        """B02b 的未知进度留底在新字段下仍然成立。"""
        sid = "20240101_000000_000003"
        raw = {"version": 1, "last_run": {"phase": "飞了"}}
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "x", "updated": "2024-01-01", "schema_version": 2,
            "progress": raw,
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        memory.load_session(sid, session=sess)
        sess.chat_history.append(AIMessage(content="新一轮"))
        memory.save_session(session=sess)

        stored = _read(isolated_memory, sid)
        assert stored["quarantined_progress"][0]["data"] == raw

    def test_future_schema_version_is_still_refused(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        data = _read(isolated_memory, sid)
        data["schema_version"] = 99
        (isolated_memory / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")

        sess.chat_history.append(AIMessage(content="x"))
        with pytest.raises(memory.SessionFormatTooNewError):
            memory.save_session(session=sess)


# ══════════════════════════════════════════════════════════════
# 工具边界的实际接线
# ══════════════════════════════════════════════════════════════

class TestExecuteToolWiring:
    """通过真实的 `_execute_tool` 验证记录时机，不是直接调 run_records。"""

    def _session_for_tools(self, tmp_path):
        sess = _new_session()
        sess.project = str(tmp_path)
        sess.agent_mode = "act"
        _saved(sess)
        session.bind_thread(sess)
        session.set_active(sess)
        run_records.begin_run(sess)
        return sess

    def test_side_effect_tool_records_before_and_commits_after(self, isolated_memory, tmp_path,
                                                               monkeypatch):
        from src import streaming
        sess = self._session_for_tools(tmp_path)
        seen = {}

        class _Tool:
            def invoke(self, args):
                # 工具运行的**当下**，记录必须已经在磁盘上。
                doc = json.loads((isolated_memory / f"{sess.current_session_id}.inflight.json")
                                 .read_text(encoding="utf-8"))
                seen["during"] = [op["tool"] for op in doc["operations"]]
                return "已写入"

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"write_file": _Tool()})
        streaming._execute_tool({"name": "write_file", "args": {"path": "a.py"}, "id": "c1"}, _UI())

        assert seen["during"] == ["write_file"]
        assert not (isolated_memory / f"{sess.current_session_id}.inflight.json").exists()
        stored = _read(isolated_memory, sess.current_session_id)
        assert stored["progress"]["last_committed_operation"]["tool"] == "write_file"
        assert stored["messages"][-1]["content"] == "已写入"

    def test_read_only_tool_writes_no_sidecar(self, isolated_memory, tmp_path, monkeypatch):
        from src import streaming
        sess = self._session_for_tools(tmp_path)

        class _Tool:
            def invoke(self, args):
                return "文件内容"

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"read_file": _Tool()})
        streaming._execute_tool({"name": "read_file", "args": {"path": "a.py"}, "id": "c1"}, _UI())
        assert not (isolated_memory / f"{sess.current_session_id}.inflight.json").exists()

    def test_unknown_and_mcp_tools_are_treated_as_side_effecting(self):
        assert run_records.needs_record("mcp_memory_store") is True
        assert run_records.needs_record("some_future_tool") is True
        assert run_records.needs_record("remember") is True, "写长期记忆是副作用"
        assert run_records.needs_record("notify_user") is True, "推送发出去就收不回来"
        assert run_records.needs_record("read_file") is False

    def test_inflight_write_failure_aborts_the_tool(self, isolated_memory, tmp_path, monkeypatch):
        """记录写不成功 → 工具**根本不被调用**，并且明确报错。"""
        from src import streaming
        self._session_for_tools(tmp_path)
        called = {"n": 0}

        class _Tool:
            def invoke(self, args):
                called["n"] += 1
                return "不该跑到这里"

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"write_file": _Tool()})
        monkeypatch.setattr(run_records, "_write_inflight",
                            lambda sid, doc: (_ for _ in ()).throw(OSError("只读磁盘")))

        ui = _UI()
        streaming._execute_tool({"name": "write_file", "args": {"path": "a.py"}, "id": "c1"}, ui)

        assert called["n"] == 0, "没有恢复点就动手，比根本没有这套记录更糟"
        assert "只读磁盘" in ui.text()
        last = state.chat_history[-1]
        assert isinstance(last, ToolMessage) and last.tool_call_id == "c1"
        assert "没有被调用" in last.content

    def test_rejected_tool_leaves_no_dispatch_record(self, isolated_memory, tmp_path, monkeypatch):
        """Plan 模式拦下的调用没有进入执行，不该留"已调度"记录。"""
        from src import streaming
        sess = self._session_for_tools(tmp_path)
        sess.agent_mode = "plan"

        class _Tool:
            def invoke(self, args):
                raise AssertionError("Plan 模式不该执行写工具")

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"write_file": _Tool()})
        streaming._execute_tool({"name": "write_file", "args": {"path": "a.py"}, "id": "c1"}, _UI())
        assert not (isolated_memory / f"{sess.current_session_id}.inflight.json").exists()

    def test_failing_tool_still_commits_a_receipt(self, isolated_memory, tmp_path, monkeypatch):
        """执行失败也可能改了一半文件——回执记的是"结果已记录"，不是"没有副作用"。"""
        from src import streaming
        sess = self._session_for_tools(tmp_path)

        class _Tool:
            def invoke(self, args):
                raise RuntimeError("写到一半盘满了")

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"write_file": _Tool()})
        streaming._execute_tool({"name": "write_file", "args": {"path": "a.py"}, "id": "c1"}, _UI())

        stored = _read(isolated_memory, sess.current_session_id)
        assert stored["progress"]["last_committed_operation"]["tool"] == "write_file"
        assert "写到一半盘满了" in stored["messages"][-1]["content"]
        assert not (isolated_memory / f"{sess.current_session_id}.inflight.json").exists()


# ══════════════════════════════════════════════════════════════
# 真·新进程读盘
# ══════════════════════════════════════════════════════════════

_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from src import paths
paths.set_data_dir(sys.argv[2])
from src import memory, run_records, session

sid = sys.argv[3]
sess = session.Session()
assert memory.load_session(sid, session=sess) is True

# 先记下**读出来的**上一轮记录，再开新一轮——begin_run 会把 last_run 换成新的。
loaded_run = dict(sess.last_run)
entries, error = run_records.classify_inflight(sid)

# 下一轮初始化：必须保留上次未解决的验证义务
run_records.begin_run(sess)

print(json.dumps({
    "plan": sess.current_plan,
    "last_run_phase": loaded_run["phase"],
    "last_run_id": loaded_run["id"],
    "last_run_outcome": loaded_run["outcome"],
    "source_text": loaded_run["source"].get("text"),
    "describe": run_records.describe_last_run(loaded_run),
    "pending_files": sess.pending_verification["files"],
    "inflight": [(e["status"], e["operation"]["tool"]) for e in entries],
    "inflight_error": error,
    "dirty_after_begin": sess.verification["dirty_files"],
    "tests_passed_after_begin": sess.verification["tests_passed"],
    "new_run_id": sess.last_run["id"],
}, ensure_ascii=False))
'''


class TestFreshProcess:
    def test_a_brand_new_interpreter_reads_the_records_from_disk(self, isolated_memory, tmp_path):
        """同进程里新建一个 Session 再 load，证明不了跨进程恢复——必须真起一个解释器。

        这一条同时覆盖：中断留在 running、结果未知的操作不被认成成功、
        待验证义务在下一轮初始化后仍在、历史测试成功不变成当前通行状态。
        """
        sess = _new_session()
        sess.current_plan = [{"text": "复现问题", "status": "done"},
                             {"text": "修复并验证", "status": "in_progress"}]
        sid = _saved(sess)
        run = run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "src/a.py")
        verification.mark_tests(sess.verification, True, "3 passed")
        verification.mark_dirty(sess.verification, "src/a.py")   # 测试之后又改
        run_records.refresh_pending_verification(sess, run_id=run["id"])
        memory.save_session(session=sess)
        # 这一笔故意不提交：模拟进程死在工具返回与结果保存之间
        run_records.begin_operation(sess, tool="append_file", tool_call_id="c9",
                                    args={"path": "log.txt"})

        script = tmp_path / "child.py"
        script.write_text(_CHILD, encoding="utf-8")
        env = dict(os.environ)
        # 子进程的日志会往 stderr 写中文；不固定编码的话 Windows 默认 GBK，
        # 读回来直接 UnicodeDecodeError，测试失败原因会错指成"子进程没输出"。
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(script),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             str(isolated_memory.parent), sid],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout.strip().splitlines()[-1])

        assert out["plan"] == [{"text": "复现问题", "status": "done"},
                               {"text": "修复并验证", "status": "in_progress"}]
        assert out["last_run_phase"] == "running"
        assert out["last_run_outcome"] is None
        assert out["last_run_id"] == run["id"]
        assert out["source_text"] == "修一下登录"
        assert "中断" in out["describe"]
        assert out["inflight"] == [["unknown", "append_file"]]
        assert out["inflight_error"] == ""
        assert out["pending_files"] == ["src/a.py"]
        assert out["new_run_id"] != out["last_run_id"], "新进程里继续是新的一轮运行"
        assert out["dirty_after_begin"] == ["src/a.py"], "新进程的第一轮不能把未了义务清掉"
        assert out["tests_passed_after_begin"] is None, "历史测试成功只是历史证据"


# ══════════════════════════════════════════════════════════════
# 保存开销
# ══════════════════════════════════════════════════════════════

class TestSaveCost:
    def test_outcome_reports_real_elapsed_and_bytes(self, isolated_memory):
        sess = _new_session()
        _saved(sess)
        outcome = memory.save_session_report(session=sess)
        assert outcome.bytes_written > 0
        assert outcome.elapsed_ms >= 0
        assert outcome.bytes_written == os.path.getsize(
            isolated_memory / f"{sess.current_session_id}.json")

    def test_sidecar_stays_small_while_history_grows(self, isolated_memory):
        """sidecar 去掉了"执行前全量重写"的成本；操作后仍有一次全量主快照。"""
        sess = _new_session()
        for i in range(300):
            sess.chat_history.append(AIMessage(content=f"第 {i} 轮的长回复 " + "内容" * 200))
        sid = _saved(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="write_file", tool_call_id="c1",
                                    args={"path": "a.py"})

        body = os.path.getsize(isolated_memory / f"{sid}.json")
        side = os.path.getsize(isolated_memory / f"{sid}.inflight.json")
        assert body > 200_000
        assert side < 1_000, f"sidecar 不该随历史增长（{side} 字节）"
