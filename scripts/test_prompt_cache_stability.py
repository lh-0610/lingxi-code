"""system prompt 的跨轮稳定性 —— prompt caching 能不能命中全看这个。

背景：_wrap_system_for_cache 把整个 system prompt 塞进一个 cache_control:ephemeral
块。Anthropic/MiMo 的缓存以**前缀逐字节相同**为条件，块内容一变缓存就失效。

而 task_ledger 在每次工具执行后都更新、current_plan 在每次 update_plan 后更新——
以前它们被拼进 system prompt，于是每轮的 system 都不同，缓存**每轮必然 miss**：
机制写对了，却被里面装的东西废掉了，每轮按全价重算角色卡+项目上下文+项目规则+记忆。

现在它们挪到 get_volatile_context()，由 streaming 追加到发送历史尾部。这些测试守住
这个性质——它极易被无意中破坏（往 get_system_prompt 里再拼一个"当前 xxx"就够了），
而破坏之后一切照常工作，只是**静默地开始烧钱**，没有任何报错会提醒你。
"""

import pytest

from src import roles, session as _session, state


@pytest.fixture()
def sess(tmp_path):
    s = _session.Session()
    s.project = str(tmp_path)
    _session.bind_thread(s)
    old = state.current_project
    state.current_project = str(tmp_path)
    yield s
    state.current_project = old
    _session.bind_thread(None)


class TestSystemPromptStability:
    def test_ledger_changes_do_not_touch_system_prompt(self, sess):
        """台账每执行一个工具就变——这是最致命的一个，必须完全不影响 system。"""
        before = roles.get_system_prompt()
        sess.task_ledger = {"files": {"a.py": "edit"}, "commands": []}
        after_one = roles.get_system_prompt()
        sess.task_ledger = {"files": {"a.py": "edit", "b.py": "write"},
                            "commands": [{"cmd": "pytest", "brief": "2 failed"}]}
        after_two = roles.get_system_prompt()
        assert before == after_one == after_two

    def test_plan_changes_do_not_touch_system_prompt(self, sess):
        before = roles.get_system_prompt()
        sess.current_plan = [{"text": "步骤一", "status": "pending"}]
        mid = roles.get_system_prompt()
        sess.current_plan = [{"text": "步骤一", "status": "done"},
                             {"text": "步骤二", "status": "in_progress"}]
        assert before == mid == roles.get_system_prompt()

    def test_mode_switch_does_not_touch_system_prompt(self, sess):
        """Plan/Act 来回切也不该打掉缓存。"""
        sess.agent_mode = "act"
        act = roles.get_system_prompt()
        sess.agent_mode = "plan"
        assert roles.get_system_prompt() == act

    def test_stable_parts_still_present(self, sess):
        """稳定内容不能被误删——拆分不是把东西弄丢。"""
        p = roles.get_system_prompt()
        assert "你是一个有帮助的AI助手" in p
        assert "当前日期" in p

    def test_project_rules_still_invalidate(self, sess, tmp_path):
        """项目规则**应该**进 system（它稳定），改了它缓存失效是正确且必要的。"""
        before = roles.get_system_prompt()
        (tmp_path / ".lingxirules").write_text("- 用 4 空格", encoding="utf-8")
        assert roles.get_system_prompt() != before


class TestVolatileContext:
    def test_empty_when_nothing_to_say(self, sess):
        """Act 模式 + 无计划 + 无台账 → 空串，调用方不追加任何消息。"""
        sess.agent_mode = "act"
        sess.current_plan = []
        sess.task_ledger = {"files": {}, "commands": []}
        assert roles.get_volatile_context() == ""

    def test_carries_all_three(self, sess):
        sess.agent_mode = "plan"
        sess.current_plan = [{"text": "改实现", "status": "pending"}]
        sess.task_ledger = {"files": {"a.py": "edit"}, "commands": []}
        v = roles.get_volatile_context()
        assert "Plan（计划）模式" in v
        assert "改实现" in v
        assert "a.py" in v

    def test_wrapped_in_system_reminder(self, sess):
        """包成 system-reminder：这是系统注入的运行态，不是用户的新要求，
        模型不该把它当指令去执行。"""
        sess.current_plan = [{"text": "x", "status": "pending"}]
        v = roles.get_volatile_context()
        assert v.startswith("<system-reminder>") and v.endswith("</system-reminder>")


class TestAppendVolatile:
    def _msgs(self):
        from langchain_core.messages import HumanMessage, SystemMessage
        return [SystemMessage(content="sys"), HumanMessage(content="干活")]

    def test_appends_at_the_very_end(self, sess):
        """必须追加在**最末尾**：前面任何一条变了，前缀缓存就从那里断掉。"""
        from src.streaming import _append_volatile_context
        sess.current_plan = [{"text": "步骤", "status": "pending"}]
        out = _append_volatile_context(self._msgs())
        assert len(out) == 3
        assert "步骤" in out[-1].content
        assert out[0].content == "sys" and out[1].content == "干活"   # 原消息原样

    def test_noop_when_empty(self, sess):
        from src.streaming import _append_volatile_context
        sess.agent_mode = "act"
        sess.current_plan = []
        sess.task_ledger = {"files": {}, "commands": []}
        msgs = self._msgs()
        assert len(_append_volatile_context(msgs)) == len(msgs)

    def test_safe_after_tool_message(self, sess):
        """历史以 ToolMessage 结尾（工具刚跑完）时追加 user 消息是合法的，
        不会破坏 tool_use/tool_result 的配对。"""
        from langchain_core.messages import ToolMessage
        from src.streaming import _append_volatile_context
        sess.current_plan = [{"text": "步骤", "status": "pending"}]
        msgs = self._msgs() + [ToolMessage(content="结果", tool_call_id="t1")]
        out = _append_volatile_context(msgs)
        assert out[-2].__class__.__name__ == "ToolMessage"
        assert out[-1].__class__.__name__ == "HumanMessage"

    def test_does_not_mutate_input(self, sess):
        """只改发送副本，绝不动传入的列表（那可能就是 state.chat_history）。"""
        from src.streaming import _append_volatile_context
        sess.current_plan = [{"text": "步骤", "status": "pending"}]
        msgs = self._msgs()
        _append_volatile_context(msgs)
        assert len(msgs) == 2

    def test_render_failure_does_not_break_the_turn(self, sess, monkeypatch):
        """运行态渲染失败只能跳过这一块，不能让整轮请求发不出去。"""
        import src.roles as _roles
        from src.streaming import _append_volatile_context

        def _boom():
            raise RuntimeError("渲染炸了")
        monkeypatch.setattr(_roles, "get_volatile_context", _boom)
        msgs = self._msgs()
        assert _append_volatile_context(msgs) == msgs


class TestCacheBlockActuallyStable:
    """端到端：连续两轮之间只有台账变化时，送进 cache_control 的那段必须一模一样。"""

    def test_cache_block_identical_across_rounds(self, sess):
        from src.streaming import _wrap_system_for_cache
        from langchain_core.messages import HumanMessage, SystemMessage

        def _cache_text():
            msgs = [SystemMessage(content="旧"), HumanMessage(content="干活")]
            wrapped = _wrap_system_for_cache(msgs, roles.get_system_prompt(), "anthropic")
            head = wrapped[0].content
            return head[0]["text"] if isinstance(head, list) else head

        sess.task_ledger = {"files": {"a.py": "edit"}, "commands": []}
        first = _cache_text()
        sess.task_ledger = {"files": {"a.py": "edit", "b.py": "write"},
                            "commands": [{"cmd": "pytest -q", "brief": "ok"}]}
        sess.current_plan = [{"text": "新步骤", "status": "done"}]
        second = _cache_text()
        assert first == second, "缓存块跨轮不一致 → prompt caching 会每轮 miss"


class TestCacheUsageTracking:
    """缓存命中量必须被采集——否则"system prompt 保持稳定"这件事无法验证。

    prompt caching 省的是**钱**不是 token：命中部分照样计进 input_tokens，只是按约
    10% 计费。只看 input/output 的话，缓存生效与否完全看不出来。
    """

    def test_anthropic_shape(self):
        from src.streaming import _extract_usage

        class _G:
            usage_metadata = {
                "input_tokens": 5000, "output_tokens": 100, "total_tokens": 5100,
                "input_token_details": {"cache_read": 4200, "cache_creation": 800},
            }
        u = _extract_usage(_G())
        assert u["cache_read"] == 4200 and u["cache_write"] == 800
        assert u["input"] == 5000        # 命中部分仍计进 input，别把它减掉

    def test_openai_compatible_shape(self):
        from src.streaming import _extract_cache_tokens
        assert _extract_cache_tokens({"prompt_tokens_details": {"cached_tokens": 3000}}) == (3000, 0)

    def test_deepseek_shape(self):
        from src.streaming import _extract_cache_tokens
        assert _extract_cache_tokens({"prompt_cache_hit_tokens": 2048}) == (2048, 0)

    def test_absent_is_zero_not_crash(self):
        from src.streaming import _extract_cache_tokens
        assert _extract_cache_tokens({"input_tokens": 10}) == (0, 0)
        assert _extract_cache_tokens(None) == (0, 0)

    def test_garbage_details_does_not_raise(self):
        """脏数据不能让整轮的 usage 统计崩掉。"""
        from src.streaming import _extract_cache_tokens
        assert _extract_cache_tokens({"input_token_details": "oops"}) == (0, 0)
        assert _extract_cache_tokens({"input_token_details": {"cache_read": "abc"}}) == (0, 0)

    def test_usage_dict_always_has_cache_keys(self):
        """下游（agent 累加、UI 显示）直接取这两个键，缺了会 KeyError。"""
        from src.streaming import _extract_usage
        assert set(_extract_usage(None)) >= {"input", "output", "total", "cache_read", "cache_write"}
