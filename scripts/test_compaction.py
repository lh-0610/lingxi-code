"""_compact_history 的单元测试（无网络依赖）。
patch 目标是 src.state.llm，因为 _compact_history 通过 state.llm.invoke 调用 LLM。
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import state


class _FakeLLM:
    """可配置返回值的假 LLM，用于测试压缩调用。"""
    def __init__(self, content="这是一段压缩摘要"):
        self._content = content
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return SimpleNamespace(content=self._content)


def _make_history(n=50):
    """生成 n 轮对话（System + n*(Human + AI)），每条 300 字确保超 budget。"""
    msgs = [SystemMessage(content="You are a helpful assistant.")]
    for i in range(n):
        msgs.append(HumanMessage(content=f"问题 {i}：" + "x" * 300))
        msgs.append(AIMessage(content=f"回答 {i}：" + "y" * 300))
    return msgs


def _set_llm(fake):
    """把假 LLM 挂到 state.llm 上，返回旧值供恢复。"""
    old = getattr(state, "llm", None)
    state.llm = fake
    return old


def _extract_summary(new_msgs):
    """从压缩后的 messages 列表中提取 [历史摘要] 内容。"""
    for m in new_msgs:
        c = getattr(m, "content", "") or ""
        if isinstance(c, str) and c.startswith("[历史摘要]:\n"):
            return c[len("[历史摘要]:\n"):]
    return ""


# ── 小 budget 保证必触发压缩 ──
_SMALL_BUDGET = 200


class TestCompactionSummaryClipping:
    """测试 1&2：summary ≤ max_chars → 原样；> max_chars → 裁剪。"""

    def teardown_method(self):
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0

    def test_short_summary_returned_as_is(self):
        import src.streaming as streaming
        history = _make_history(30)
        short = "短摘要"
        fake = _FakeLLM(content=short)
        old = _set_llm(fake)
        try:
            new_msgs, dropped = streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old
        summary = _extract_summary(new_msgs)
        assert summary == short

    def test_long_summary_clipped_to_max_chars(self):
        import src.streaming as streaming
        from src.limits import COMPACTION_SUMMARY_MAX_CHARS
        history = _make_history(30)
        long_summary = "A" * (COMPACTION_SUMMARY_MAX_CHARS + 500)
        fake = _FakeLLM(content=long_summary)
        old = _set_llm(fake)
        try:
            new_msgs, dropped = streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old
        summary = _extract_summary(new_msgs)
        assert len(summary) <= COMPACTION_SUMMARY_MAX_CHARS
        assert summary.endswith("…")


class TestCompactionFallback:
    """测试 3：LLM 抛异常 → 降级到直接裁剪。"""

    def test_llm_exception_falls_back_to_plain_text(self):
        import src.streaming as streaming
        history = _make_history(30)
        # 清缓存，确保必走 LLM 路径（触发 BrokenLLM 异常）
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0

        class BrokenLLM:
            def invoke(self, messages):
                raise RuntimeError("API down")

        old = _set_llm(BrokenLLM())
        try:
            new_msgs, dropped = streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old

        # 降级到 _maybe_trim_history：应有占位符 [历史已自动裁剪]
        assert dropped > 0
        contents = [getattr(m, "content", "") or "" for m in new_msgs]
        assert any("[历史已自动裁剪" in c for c in contents)


class TestCompactionCache:
    """测试 4：已缓存且未过期 → 跳过 LLM，直接返回缓存。"""

    def test_cache_hit_skips_llm(self):
        import src.streaming as streaming
        history = _make_history(30)  # len=61 (1 system + 30*2)
        # 模拟缓存：covered_upto 覆盖了中段（cut = 61 - 10 = 51）
        state.compaction["summary"] = "已缓存的摘要"
        state.compaction["covered_upto"] = 60  # >= cut(51)
        fake = _FakeLLM()
        old = _set_llm(fake)
        try:
            new_msgs, dropped = streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old
        summary = _extract_summary(new_msgs)
        assert summary == "已缓存的摘要"
        assert fake.call_count == 0  # LLM 不应被调用

    def teardown_method(self):
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0


class TestCompactionFullCoverage:
    """测试 5：covered_upto 已覆盖全对话 → 缓存命中，直接用旧摘要。"""

    def test_all_messages_covered_returns_cached(self):
        import src.streaming as streaming
        history = _make_history(20)  # len = 41
        # covered_upto >= cut(41 - 10 = 31)，让缓存命中
        state.compaction["summary"] = "旧摘要"
        state.compaction["covered_upto"] = 100  # 覆盖全部

        fake = _FakeLLM()
        old = _set_llm(fake)
        try:
            new_msgs, dropped = streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old

        summary = _extract_summary(new_msgs)
        assert summary == "旧摘要"
        assert fake.call_count == 0

    def teardown_method(self):
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0


class TestCompactionRolling:
    """回归：二次（滚动）压缩只发『上次覆盖点之后的新增段』+ 旧摘要，
    不把已压进旧摘要的消息当原文重发一遍（否则二次压缩比不压更费 token）。"""

    def teardown_method(self):
        state.compaction["summary"] = ""
        state.compaction["covered_upto"] = 0

    def test_rolling_compaction_no_double_count(self):
        import src.streaming as streaming

        captured = {}

        class _SpyLLM:
            def invoke(self, messages):
                captured["full_text"] = messages[-1].content
                return SimpleNamespace(content="S2")

        history = _make_history(40)  # len=81, cut = 81 - 20 = 61
        # 模拟第一次已压缩到 covered_upto=41（messages[1:41] 已进 S1 摘要）
        state.compaction["summary"] = "S1-旧摘要"
        state.compaction["covered_upto"] = 41

        old = _set_llm(_SpyLLM())
        try:
            streaming._compact_history(history, budget=_SMALL_BUDGET)
        finally:
            state.llm = old

        ft = captured["full_text"]
        assert "S1-旧摘要" in ft       # 旧摘要拼入
        assert "问题 20：" in ft        # 新增段 messages[41:61] 应有
        assert "问题 0：" not in ft     # 已在旧摘要里，不该作为原文重发
        assert "问题 19：" not in ft    # 同上（index 39 < 41，属旧区间）
