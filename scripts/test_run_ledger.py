"""运行账本经过真实调用边界、记录落盘与结果卡的集成测试。"""
import io
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage

from src import config, memory, paths, roles, run_records, session, state, streaming
from src.agent_result import AgentResult
from src.subagent import HeadlessUI


def _usage(input_tokens=1000, output_tokens=100, cache_read=800, reasoning=25):
    return {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens,
            "cache_read": cache_read, "cache_write": 0, "reasoning": reasoning}


@pytest.fixture()
def runtime(project_dir, isolated_memory, tmp_path, monkeypatch):
    from src import agent
    sess = session.current_session()
    sess.project = str(project_dir)
    monkeypatch.setattr(state, "current_project", str(project_dir))
    sess.current_model_index = next(i for i, model in enumerate(agent.MODEL_LIST)
                                    if model[1] not in ("ollama", "claude-code"))
    sess.chat_history = [SystemMessage(content="固定系统"), HumanMessage(content="测试任务")]
    session.register(sess)
    log_dir = tmp_path / "账本日志"
    log_dir.mkdir()
    monkeypatch.setattr(paths, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(config, "MODEL_PRICING", {}, raising=False)
    monkeypatch.setattr(roles, "get_system_prompt", lambda: "固定系统")
    monkeypatch.setattr(roles, "get_volatile_context", lambda: "状态一")
    monkeypatch.setattr(streaming, "_current_history_budget", lambda: 200000)
    monkeypatch.setattr(streaming, "_trim_watermarks", lambda budget: (budget, budget // 2))
    monkeypatch.setattr(streaming, "current_model_supports_vision", lambda: False)
    ui = HeadlessUI()
    run = run_records.begin_run(sess)
    return SimpleNamespace(agent=agent, sess=sess, run=run, ui=ui, log_dir=log_dir,
                           memory_dir=isolated_memory, project_dir=project_dir)


def _finish(runtime):
    return run_records.finalize_run(runtime.sess, runtime.run, AgentResult("completed"))


def test_rounds_tools_and_persistence(runtime):
    r = runtime
    r.agent._record_round_usage(r.ui, _usage())
    r.agent._record_round_usage(r.ui, _usage(500, 50, 400, 15))
    (r.project_dir / "example.txt").write_text("工具输出\n", encoding="utf-8")
    streaming._execute_tool({"name": "read_file", "args": {"path": "example.txt"}, "id": "read-1"}, r.ui)
    assert "工具输出" in r.sess.chat_history[-1].content
    result_chars = len(r.sess.chat_history[-1].content)

    report = _finish(r)

    ledger = report.snapshot["ledger"]
    assert ledger["total"] == {"rounds": 2, "input": 1500, "output": 150, "total": 1650,
                               "cache_read": 1200, "cache_write": 0, "reasoning": 40}
    assert ledger["tools"]["read_file"] == {"calls": 1, "result_chars": result_chars}
    saved = json.loads((r.memory_dir / f"{r.sess.current_session_id}.json").read_text(encoding="utf-8"))
    assert saved["progress"]["last_run"]["ledger"] == ledger
    normalized, error = run_records.normalize_last_run(json.loads(json.dumps(r.run)))
    assert not error and normalized["ledger"] == ledger
    lines = (r.log_dir / "run_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["run_id"] == r.run["id"] and row["ledger"] == ledger
    assert "测试任务" not in lines[0] and "工具输出" not in lines[0]
    _finish(r)
    assert (r.log_dir / "run_ledger.jsonl").read_text(encoding="utf-8").splitlines() == lines


def test_next_run_resets_only_run_accumulator(runtime):
    r = runtime
    r.agent._record_round_usage(r.ui, _usage())
    _finish(r)
    run_records.begin_run(r.sess)

    assert r.sess.run_ledger.snapshot()["total"]["total"] == 0
    assert r.sess.session_token_usage["total"] == 1100


def test_unknown_usage_still_counts_a_round(runtime):
    r = runtime
    r.agent._record_round_usage(r.ui, {})

    ledger = _finish(r).snapshot["ledger"]

    assert ledger["total"]["rounds"] == 1
    assert ledger["total"]["input"] is None
    assert ledger["total"]["cache_read"] is None
    assert ledger["cost"] is None


def test_tool_preinvocation_is_counted_once(runtime):
    r = runtime
    streaming._execute_tool({"name": "read_file", "args": {"path": "不存在.txt"}, "id": "prefetch"},
                            r.ui, _preinvoked="预取结果")

    assert _finish(r).snapshot["ledger"]["tools"]["read_file"] == {"calls": 1, "result_chars": 4}


def test_rejected_tool_is_not_counted_as_executed(runtime):
    r = runtime
    r.sess.agent_mode = "plan"
    streaming._execute_tool({"name": "write_file", "args": {"path": "a.py", "content": "内容"},
                             "id": "blocked"}, r.ui)

    assert _finish(r).snapshot["ledger"]["tools"] == {}


def test_explore_usage_reaches_parent(runtime, monkeypatch):
    from src import tools_explore
    r = runtime
    r.agent._record_round_usage(r.ui, _usage())

    def child_loop(ui):
        child = session.current_session()
        run_records.begin_run(child)
        r.agent._record_round_usage(ui, _usage(200, 20, 100, 5))
        child.chat_history.append(AIMessage(content="探索报告"))
        return AgentResult("completed")

    monkeypatch.setattr(r.agent, "agent_loop", child_loop)
    assert "探索报告" in tools_explore.explore.func("查找测试入口", "quick")

    ledger = _finish(r).snapshot["ledger"]
    assert ledger["main"]["input"] == 1000
    assert ledger["subagents"]["input"] == 200
    assert ledger["total"]["input"] == 1200
    assert ledger["total"]["rounds"] == 2
    assert len((r.log_dir / "run_ledger.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_spawn_usage_reaches_parent(runtime, monkeypatch):
    from src import subagent, worktree
    r = runtime

    def child_loop(ui):
        run_records.begin_run(session.current_session())
        r.agent._record_round_usage(ui, _usage(200, 20, 100, 5))
        return AgentResult("completed")

    def create(child, root, child_id):
        child.worktree = root
        return root

    monkeypatch.setattr(worktree, "is_git_repo", lambda root: True)
    monkeypatch.setattr(worktree, "create", create)
    monkeypatch.setattr(worktree, "changed_files", lambda root: [])
    monkeypatch.setattr(worktree, "finish", lambda child, **kwargs: (True, "完成"))
    monkeypatch.setattr(subagent, "_run_agent_loop", child_loop)
    subagent.spawn(["任务一", "任务二"], str(r.project_dir))

    ledger = _finish(r).snapshot["ledger"]
    assert ledger["subagents"]["input"] == 400
    assert ledger["total"]["input"] == 400
    assert ledger["main"]["rounds"] == 0


def test_explore_final_report_is_charged_to_child(runtime, monkeypatch):
    from src import tools_explore
    r = runtime

    def child_loop(ui):
        run_records.begin_run(session.current_session())
        r.agent._record_round_usage(ui, _usage(200, 20, 100, 5))
        return AgentResult("limit_reached")

    model = SimpleNamespace(invoke=lambda history: AIMessage(content="补充报告", usage_metadata={
        "input_tokens": 300, "output_tokens": 30, "total_tokens": 330,
        "input_token_details": {"cache_read": 200}, "output_token_details": {"reasoning": 10}}))
    monkeypatch.setattr(r.agent, "agent_loop", child_loop)
    monkeypatch.setattr(r.agent, "resolve_bound_llm", lambda child: (model, model))
    assert "补充报告" in tools_explore.explore.func("查找入口", "quick")

    ledger = _finish(r).snapshot["ledger"]
    assert ledger["subagents"]["input"] == 500
    assert ledger["subagents"]["rounds"] == 2
    assert ledger["main"]["input"] == 0


def test_claude_cli_preserves_unknown_cache_and_counts_tools(runtime, monkeypatch):
    from src import claude_code, result_view
    r = runtime
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "cli-read", "name": "Read", "input": {"file_path": "a.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "cli-read", "content": "读取结果"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
         "usage": {"input_tokens": 900, "output_tokens": 100}},
    ]

    class Process:
        stdin = io.StringIO()
        stdout = io.StringIO("\n".join(json.dumps(e) for e in events))
        returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    with monkeypatch.context() as patch:
        patch.setattr(claude_code.subprocess, "Popen", lambda *args, **kwargs: Process())
        patch.setattr(claude_code, "get_external_agent_context", lambda: "")
        assert claude_code.claude_code_loop(r.ui).status == "unverified"
    report = _finish(r)
    ledger = report.snapshot["ledger"]
    assert ledger["total"]["rounds"] == 3
    assert ledger["total"]["input"] == 900
    assert ledger["total"]["cache_read"] is None
    assert ledger["total"]["reasoning"] is None
    assert ledger["tools"]["Read"] == {"calls": 1, "result_chars": 4}
    assert "命中率未知" in result_view.describe(report.snapshot)["usage_lines"][0]


@pytest.mark.parametrize("source", ["usage_metadata", "response_metadata", "additional_kwargs"])
def test_reasoning_tokens_are_extracted(source):
    if source == "usage_metadata":
        fields = {source: {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                           "output_token_details": {"reasoning": 30}}}
    else:
        fields = {source: {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                    "completion_tokens_details": {"reasoning_tokens": 30}}}}
    usage = streaming._extract_usage(SimpleNamespace(**fields))
    assert usage["reasoning"] == 30
    assert usage["total"] == 150


@pytest.mark.parametrize("pricing,expected", [
    ({}, None),
    ({"cache_hit_equivalent_ratio": 0.1}, "未命中等价 token 380"),
    ({"currency": "CNY", "input_miss_per_million": 2,
      "input_hit_per_million": 0.2, "output_per_million": 4}, "CNY 0.000960"),
])
def test_result_card_usage_text(runtime, monkeypatch, pricing, expected):
    from src import result_view
    r = runtime
    monkeypatch.setattr(config, "MODEL_PRICING", {r.run["model"]["id"]: pricing})
    r.agent._record_round_usage(r.ui, _usage())
    report = _finish(r)

    text = result_view.describe(report.snapshot)["usage_lines"][0]

    for part in ("1 轮", "1,000", "100", "80.0%"):
        assert part in text
    if expected:
        assert expected in text
    else:
        assert "费用" not in text and "等价" not in text
    assert text in result_view.plain_text(result_view.describe(report.snapshot))
    monkeypatch.setattr(config, "MODEL_PRICING", {})
    assert result_view.describe(report.snapshot)["usage_lines"][0] == text


def test_old_record_without_ledger_still_loads(runtime):
    from src import result_view
    raw = dict(runtime.run)
    raw.pop("ledger", None)
    normalized, error = run_records.normalize_last_run(raw)
    assert not error
    snapshot = run_records.build_result_snapshot(runtime.sess, normalized)
    assert result_view.describe(snapshot).get("usage_lines", []) == []


def _prepare(runtime):
    return streaming._prepare_stream_history(runtime.ui)[0]


def test_prepare_records_local_changes_not_volatile_tail(runtime, monkeypatch):
    r = runtime
    r.sess.chat_history.extend([
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "tool-1"}]),
        ToolMessage(content="原始结果", tool_call_id="tool-1"),
    ])
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage())
    monkeypatch.setattr(roles, "get_volatile_context", lambda: "状态二")
    _prepare(r)
    assert r.sess.run_ledger.snapshot()["cache_breaks"] == []
    r.agent._record_round_usage(r.ui, _usage())
    r.sess.chat_history[3] = ToolMessage(content="改变后的结果", tool_call_id="tool-1")
    _prepare(r)

    event = r.sess.run_ledger.snapshot()["cache_breaks"][-1]
    assert event["round"] == 3 and event["message_index"] == 4
    assert event["source"] == "发送前" and event["reason"] == "未知"
    assert "前缀" not in r.ui.text()


def test_prepare_system_change_and_server_side_drop(runtime, monkeypatch):
    r = runtime
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage())
    monkeypatch.setattr(roles, "get_system_prompt", lambda: "新系统")
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage(cache_read=100))
    assert len(r.sess.run_ledger.snapshot()["cache_breaks"]) == 1
    assert r.sess.run_ledger.snapshot()["cache_breaks"][0]["reason"] == "system 变了"
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage(cache_read=900))
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage(cache_read=100))
    events = _finish(r).snapshot["ledger"]["cache_breaks"]
    assert len(events) == 2 and events[-1]["source"] == "服务端侧"


def test_eviction_reports_once_and_folded_prefix_stays_stable(runtime, monkeypatch):
    r = runtime
    for i in range(14):
        call_id = f"read-{i}"
        r.sess.chat_history.extend([
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": f"{i}.py"}, "id": call_id}]),
            ToolMessage(content="代码内容" * 1000, tool_call_id=call_id),
        ])
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage())
    monkeypatch.setattr(streaming, "_trim_watermarks", lambda budget: (100, 0))
    _prepare(r)
    assert r.sess.evicted_tool_results
    r.agent._record_round_usage(r.ui, _usage())
    events = r.sess.run_ledger.snapshot()["cache_breaks"]
    assert len(events) == 1 and events[0]["reason"] == "工具结果回收"
    _prepare(r)
    assert r.sess.run_ledger.snapshot()["cache_breaks"] == events


def test_prefix_state_survives_run_boundary_but_not_new_session(runtime, monkeypatch):
    r = runtime
    _prepare(r)
    r.agent._record_round_usage(r.ui, _usage())
    _finish(r)
    r.run = run_records.begin_run(r.sess)
    monkeypatch.setattr(roles, "get_system_prompt", lambda: "新的系统")
    _prepare(r)
    assert r.sess.run_ledger.snapshot()["cache_breaks"][0]["reason"] == "system 变了"
    memory.reset_history(session=r.sess)
    assert r.sess.cache_prefix is None
