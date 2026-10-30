"""只读探索子代理（tools_explore.explore）：离线跑真实的子代理主循环，只把模型换成桩。

守的是：子代理每轮都拿到自己的提示词（streaming 每轮重渲染系统提示词）、只绑定也只能用读/搜工具、
按彻底程度的轮次上限停下并交回已查到的内容、主任务停止时跟着停、不弄脏主会话。
"""
import socket

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage

from src.agent_result import AgentResult


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("Network is disabled for explore tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    from src import agent, config, paths, session, state
    paths.set_data_dir(str(tmp_path / "data"))
    monkeypatch.setattr(agent, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(config, "AUTO_CHECK_AFTER_EDIT", False)
    monkeypatch.setattr(config, "NOTIFY_ENABLED", False)
    monkeypatch.setattr(state, "ui_ref", None)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("def send():\n    return build_llm()\n", encoding="utf-8")
    parent = session.Session()
    parent.current_model_index = next(
        i for i, m in enumerate(agent.MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    parent.project = str(project)
    parent.chat_history = [SystemMessage(content="main"), HumanMessage(content="修 bug")]
    session.bind_thread(parent)
    yield agent, parent, project
    session.unbind_thread()
    paths.set_data_dir(None)


def fake_model(monkeypatch, agent, rounds, final="（收尾报告）"):
    """每次 stream 按 rounds 取一步；记下每轮模型看到的消息。invoke 用于跑满上限后的收尾报告。"""
    class Model:
        def __init__(self):
            self.calls = 0
            self.seen = []

        def stream(self, messages):
            self.seen.append(list(messages))
            step = rounds[min(self.calls, len(rounds) - 1)]
            self.calls += 1
            if callable(step):
                step = step()
            yield from step

        def invoke(self, messages):
            self.seen.append(list(messages))
            return AIMessage(content=final)

    model = Model()
    monkeypatch.setattr(agent, "resolve_bound_llm", lambda sess: (model, model))
    return model


def call(name, args, i=1):
    return [AIMessageChunk(content="", tool_calls=[{"name": name, "args": args, "id": f"c{i}"}])]


def say(text):
    return [AIMessageChunk(content=text)]


def test_returns_the_child_report_and_leaves_parent_alone(runtime, monkeypatch):
    from src.tools_explore import explore
    agent, parent, project = runtime
    before = list(parent.chat_history)
    model = fake_model(monkeypatch, agent, [
        call("search_files", {"regex": "def send"}),
        say("结论：发送入口在 app.py:1，调用 build_llm。"),
    ])

    out = explore.func("发送是从哪里调模型的", "quick")

    assert "结论：发送入口在 app.py:1" in out and "（探索子代理：quick" in out
    assert parent.chat_history == before                  # 子代理的过程不进主会话
    # streaming 每轮都会重渲染系统提示词：第二轮看到的也得是探索提示词，不能被换回主提示词
    assert len(model.seen) == 2
    for messages in model.seen:
        assert "只读的代码探索助手" in str(messages[0].content)
    assert "（彻底程度：quick）" in str(model.seen[0][1].content)


def test_child_cannot_write_or_run_commands(runtime, monkeypatch):
    from src.tools_explore import explore
    agent, parent, project = runtime
    model = fake_model(monkeypatch, agent, [
        call("write_file", {"path": "hacked.txt", "content": "x"}, 1),
        call("run_command", {"command": "echo x > ran.txt"}, 2),
        say("结论：只读。"),
    ])

    out = explore.func("随便看看", "quick")

    assert not (project / "hacked.txt").exists() and not (project / "ran.txt").exists()
    refusals = [m.content for m in model.seen[-1] if type(m).__name__ == "ToolMessage"]
    assert len(refusals) == 2 and all("只读的探索子代理" in r for r in refusals)
    assert "结论：只读" in out


def test_binds_only_the_allowed_tools(runtime, monkeypatch):
    agent, parent, project = runtime
    from src.tools_explore import EXPLORE_TOOL_NAMES
    bound = {}

    class LLM:
        def bind_tools(self, tools):
            bound["names"] = {t.name for t in tools}
            return self

    monkeypatch.setattr(agent, "_create_llm", lambda *a, **k: LLM())
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    from src import session
    child = session.Session()
    child.current_model_index = parent.current_model_index
    child.allowed_tools = set(EXPLORE_TOOL_NAMES)
    agent.resolve_bound_llm(child)
    assert bound["names"] and bound["names"] <= EXPLORE_TOOL_NAMES
    assert not bound["names"] & {"write_file", "edit_file", "run_command", "run_tests", "explore", "spawn_agents"}


def test_limit_hands_back_what_was_found(runtime, monkeypatch):
    """跑满 quick 档上限：不能只交回一句"已达上限"，要让模型把已查到的写成报告交回来。"""
    from src import tools_explore
    agent, parent, project = runtime
    n = {"i": 0}

    def endless():
        n["i"] += 1
        return call("search_files", {"regex": f"x{n['i']}"}, n["i"])

    model = fake_model(monkeypatch, agent, [endless], final="结论：查到一半，入口在 app.py:1。")

    out = tools_explore.explore.func("查到底", "quick")

    assert n["i"] == tools_explore._DEPTHS["quick"][0]       # 子代理自己的上限，不是设置里的 100
    assert tools_explore._LIMIT_NOTE in out and "入口在 app.py:1" in out
    last_request = model.seen[-1]
    assert "不要再调用工具" in str(last_request[-1].content)
    assert not any("轮次上限" in str(getattr(m, "content", "")) for m in last_request[1:-1]
                   if isinstance(m, AIMessage))


def test_stops_when_the_parent_stops(runtime, monkeypatch):
    from src.tools_explore import explore
    agent, parent, project = runtime

    def stop_parent_then_search():
        parent.stop_flag = True
        return call("search_files", {"regex": "x"})

    fake_model(monkeypatch, agent, [stop_parent_then_search, say("不该走到这")])

    assert "探索已停止" in explore.func("查", "medium")


def test_empty_question_is_rejected_without_a_model_call(runtime, monkeypatch):
    from src.tools_explore import explore
    agent, parent, project = runtime
    model = fake_model(monkeypatch, agent, [say("x")])
    assert "没给问题" in explore.func("  ")
    assert model.calls == 0


def test_failed_child_says_so(runtime, monkeypatch):
    from src import tools_explore
    agent, parent, project = runtime
    monkeypatch.setattr(agent, "agent_loop", lambda ui: AgentResult("failed", "模型挂了"))
    out = tools_explore.explore.func("查", "quick")
    assert "没有得到结果" in out and "模型挂了" in out
