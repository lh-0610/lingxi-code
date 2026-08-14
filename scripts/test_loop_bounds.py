"""agent 主循环的边界：轮次上限 + 上下文溢出的压缩重试。

这两条守的是"跑飞"的场景：模型不停返回 tool_calls 时主循环原本没有自然终点，
唯一的刹车是用户点停止；provider 报上下文溢出时原本会退避重试三次、每次必然再溢出。
"""
import pytest

from src.llm_errors import ContextOverflowError


class _UI:
    """记录 agent 往 UI 推了什么，供断言。"""

    def __init__(self):
        self.messages = []
        self.retries = []

    def show_message(self, text, tag="ai_msg"):
        self.messages.append(str(text))

    def render_final_markdown(self, text, speak=True):
        self.messages.append(str(text))

    def show_retry(self, text):
        self.retries.append(str(text))

    def show_token_usage(self, *a, **k):
        pass

    def remove_thinking_indicator(self):
        pass

    def update_thinking_indicator(self, text):
        pass

    def show_plan(self, items):
        pass

    def text(self):
        return "".join(self.messages)


@pytest.fixture()
def agent_env(monkeypatch, tmp_path):
    """把 agent_loop 需要的外部依赖都换成桩，只留主循环逻辑本身。"""
    from src import agent as _agent
    from src import session as _session
    from src.models import MODEL_LIST
    sess = _session.Session()
    sess.agent_mode = "act"
    sess.project = str(tmp_path)
    # 默认 model index 0 是 Claude Code（走本地 CLI 分支，绕开主循环）；挑一个走
    # 正常流式路径的 API 模型。is_subagent=True 跳过收尾的标题生成 / 手机通知。
    sess.current_model_index = next(
        i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    sess.is_subagent = True
    _session.bind_thread(sess)
    monkeypatch.setattr(_agent, "save_session", lambda *a, **k: None)
    yield _agent, sess
    _session.bind_thread(None)


def _stub_endless_tool_calls(monkeypatch, _agent, counter):
    """让 _stream_with_tools 每次都返回一个 tool_call —— 模拟"模型停不下来"。"""
    from langchain_core.messages import AIMessage

    def _fake_stream(ui):
        counter["n"] += 1
        return ("",
                [{"name": "read_file", "args": {"path": "x"}, "id": f"c{counter['n']}"}],
                {"input": 1, "output": 1, "total": 2},
                None)
    monkeypatch.setattr(_agent, "_stream_with_tools", _fake_stream)
    monkeypatch.setattr(_agent, "_execute_tool", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "_build_ai_message",
                        lambda g, t, tcs: AIMessage(content=t or "x"))


def _set_cap(monkeypatch, value):
    """改生效中的轮次上限（agent_loop 每轮重读 config，所以 patch 模块属性即可）。"""
    from src import config as _cfg
    monkeypatch.setattr(_cfg, "AGENT_MAX_ROUNDS", value)


class TestRoundCap:
    def test_infinite_tool_calls_stops_at_cap(self, agent_env, monkeypatch):
        """模型每轮都返回 tool_calls → 必须在上限处停下，而不是无限跑。"""
        _agent, sess = agent_env
        _set_cap(monkeypatch, 6)
        calls = {"n": 0}
        _stub_endless_tool_calls(monkeypatch, _agent, calls)

        ui = _UI()
        _agent.agent_loop(ui)

        assert calls["n"] <= 6, f"跑了 {calls['n']} 轮，没被上限 6 拦住"
        assert any("轮次上限" in m for m in ui.messages), "触顶后没告诉用户"

    def test_cap_message_enters_history(self, agent_env, monkeypatch):
        """结论要写进 chat_history：只弹 UI 的话，用户接着发消息时模型看不到自己被截断了，
        会以为任务顺利做完。"""
        _agent, sess = agent_env
        _set_cap(monkeypatch, 3)
        _stub_endless_tool_calls(monkeypatch, _agent, {"n": 0})
        _agent.agent_loop(_UI())
        assert any("轮次上限" in str(getattr(m, "content", "")) for m in sess.chat_history)

    def test_zero_means_unlimited(self, agent_env, monkeypatch):
        """上限设 0 = 不限：必须跑得比任何有限上限都远，且永远不提"轮次上限"。

        用 stop_flag 在第 80 轮踩刹车收尾——不然这个测试真的会无限跑，正好也验证了
        "0 时唯一的刹车是用户点停止"这个语义。
        """
        _agent, sess = agent_env
        _set_cap(monkeypatch, 0)
        calls = {"n": 0}
        from langchain_core.messages import AIMessage

        def _fake_stream(ui):
            calls["n"] += 1
            if calls["n"] >= 80:
                sess.stop_flag = True
            return ("", [{"name": "read_file", "args": {}, "id": f"c{calls['n']}"}],
                    {"input": 1, "output": 1, "total": 2}, None)
        monkeypatch.setattr(_agent, "_stream_with_tools", _fake_stream)
        monkeypatch.setattr(_agent, "_execute_tool", lambda *a, **k: None)
        monkeypatch.setattr(_agent, "_build_ai_message",
                            lambda g, t, tcs: AIMessage(content="x"))

        ui = _UI()
        _agent.agent_loop(ui)
        sess.stop_flag = False

        assert calls["n"] >= 80, f"设 0 却只跑了 {calls['n']} 轮，被上限拦住了"
        assert not any("轮次上限" in m for m in ui.messages), "不限模式不该报轮次上限"

    def test_negative_config_treated_as_unlimited(self):
        """config 里写负数按 0（不限）处理，不能变成"每轮都触顶"。"""
        from src import config as _cfg
        assert _cfg.AGENT_MAX_ROUNDS >= 0

    def test_normal_task_unaffected(self, agent_env, monkeypatch):
        """一轮就给纯文本回复的正常任务不受影响。"""
        _agent, sess = agent_env
        from langchain_core.messages import AIMessage
        monkeypatch.setattr(_agent, "_stream_with_tools",
                            lambda ui: ("做完了", [], {"input": 1, "output": 1, "total": 2}, None))
        monkeypatch.setattr(_agent, "_build_ai_message",
                            lambda g, t, tcs: AIMessage(content=t))
        ui = _UI()
        _agent.agent_loop(ui)
        assert not any("轮次上限" in m for m in ui.messages)


class TestOverflowRecovery:
    def test_squeeze_increments_then_succeeds(self, agent_env, monkeypatch):
        """第一次溢出 → 收紧档位重发 → 成功。不该把错误抛给用户。"""
        _agent, sess = agent_env
        sess.overflow_squeeze = 0
        seen = {"n": 0}

        def _fake_stream(ui):
            seen["n"] += 1
            if seen["n"] == 1:
                raise ContextOverflowError("maximum context length exceeded")
            return "压缩后成功", [], {"input": 1, "output": 1, "total": 2}, None
        monkeypatch.setattr(_agent, "_stream_with_tools", _fake_stream)
        monkeypatch.setattr(_agent, "_build_ai_message", lambda g, t, tcs: __import__(
            "langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content=t))

        ui = _UI()
        _agent.agent_loop(ui)
        assert seen["n"] == 2
        assert sess.overflow_squeeze == 1, "没有收紧预算档位"
        assert any("压缩历史后重试" in m for m in ui.messages)

    def test_gives_up_after_cap_without_crashing(self, agent_env, monkeypatch):
        """收紧到封顶仍溢出 → 明确告诉用户，且不能因为结果变量未定义而 NameError。"""
        _agent, sess = agent_env
        sess.overflow_squeeze = 0

        def _always_overflow(ui):
            raise ContextOverflowError("still too long")
        monkeypatch.setattr(_agent, "_stream_with_tools", _always_overflow)

        ui = _UI()
        _agent.agent_loop(ui)      # 不抛异常即通过
        assert any("上下文窗口" in r for r in ui.retries)
        assert sess.overflow_squeeze <= _agent._MAX_OVERFLOW_RETRIES


class TestBudgetSqueeze:
    def test_squeeze_shrinks_budget(self, monkeypatch):
        from src.streaming import _current_history_budget
        from src import session as _session
        sess = _session.Session()
        _session.bind_thread(sess)
        try:
            sess.overflow_squeeze = 0
            base = _current_history_budget()
            sess.overflow_squeeze = 1
            once = _current_history_budget()
            sess.overflow_squeeze = 2
            twice = _current_history_budget()
            assert once < base and twice < once
        finally:
            _session.bind_thread(None)

    def test_never_below_floor(self, monkeypatch):
        """收紧不能把预算压到没法工作。"""
        from src.streaming import _current_history_budget
        from src import session as _session
        sess = _session.Session()
        _session.bind_thread(sess)
        try:
            sess.overflow_squeeze = 99
            assert _current_history_budget() >= 8_000
        finally:
            _session.bind_thread(None)
