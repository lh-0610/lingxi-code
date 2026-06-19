"""M2 工具结果分级回收 —— 自检测试。

直接构造 message 列表测 `_evict_old_tool_results`（不依赖 UI / 真实 LLM）。
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.streaming import _evict_old_tool_results


def _make_tool_msg(tool_call_id: str, char_count: int, label: str = "x") -> ToolMessage:
    """构造指定长度的 ToolMessage。"""
    return ToolMessage(content=label * char_count, tool_call_id=tool_call_id)


def _build_history(num_old_tools: int = 8, old_char_count: int = 2000,
                   num_recent_tools: int = 6, recent_char_count: int = 2000,
                   small_old: bool = False):
    """构造一条典型的多轮对话历史。

    返回 (messages, old_tool_ids, recent_tool_ids)。
    """
    msgs: list = [SystemMessage(content="你是一个助手")]
    old_ids: list[str] = []
    recent_ids: list[str] = []

    # 旧轮次：每个包含 Human + AI + Tool
    for i in range(num_old_tools):
        hid = f"old_{i}"
        old_ids.append(hid)
        char_count = 100 if small_old else old_char_count
        msgs.append(HumanMessage(content=f"旧问题 {i}"))
        msgs.append(AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": f"f{i}"}, "id": hid}]))
        msgs.append(_make_tool_msg(hid, char_count, label="O"))

    # 最近轮次
    for i in range(num_recent_tools):
        rid = f"recent_{i}"
        recent_ids.append(rid)
        msgs.append(HumanMessage(content=f"新问题 {i}"))
        msgs.append(AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": f"r{i}"}, "id": rid}]))
        msgs.append(_make_tool_msg(rid, recent_char_count, label="R"))

    return msgs, old_ids, recent_ids


# ── 1. 未超预算不动 ──

def test_below_budget_no_eviction():
    """几条短消息，budget=10_000 → 返回原列表、evicted==0。"""
    msgs = [
        SystemMessage(content="你好"),
        HumanMessage(content="短问题"),
        AIMessage(content="短回答"),
        ToolMessage(content="短结果", tool_call_id="t1"),
    ]
    out, evicted = _evict_old_tool_results(msgs, budget=10_000)
    assert evicted == 0
    assert out is msgs  # 未动 → 返回同一对象


# ── 2. 回收旧的大结果 ──

def test_evict_old_large_results():
    """超预算时，旧的大 ToolMessage 被换成存根，最近 keep_recent 条保持完整。"""
    msgs, old_ids, recent_ids = _build_history(
        num_old_tools=8, old_char_count=2000, num_recent_tools=6,
    )
    # budget=50 确保触发
    out, evicted = _evict_old_tool_results(msgs, budget=50)
    assert evicted > 0

    # 收集输出中所有 ToolMessage
    out_tools = [m for m in out if isinstance(m, ToolMessage)]

    # 旧的应该被回收（含"已回收"关键词）
    for tid in old_ids:
        tm = next((m for m in out_tools if m.tool_call_id == tid), None)
        assert tm is not None, f"旧 ToolMessage {tid} 丢失"
        assert "已回收" in tm.content, f"旧 ToolMessage {tid} 未被回收"

    # 最近 6 条应保持完整（内容不含"已回收"）
    for tid in recent_ids:
        tm = next((m for m in out_tools if m.tool_call_id == tid), None)
        assert tm is not None, f"最近 ToolMessage {tid} 丢失"
        assert "已回收" not in tm.content, f"最近 ToolMessage {tid} 不应被回收"
        assert tm.content.startswith("R"), f"最近 ToolMessage {tid} 内容被改了"


# ── 3. 配对不破 ──

def test_pairing_preserved():
    """被回收的 ToolMessage 的 tool_call_id 与原来一致；消息条数不变。"""
    msgs, old_ids, recent_ids = _build_history(num_old_tools=8, old_char_count=2000)
    original_count = len(msgs)

    out, evicted = _evict_old_tool_results(msgs, budget=50)
    assert len(out) == original_count, "消息条数不应变"

    # 每条 tool_call_id 都在（顺序和 ID 不变）
    orig_ids = [m.tool_call_id for m in msgs if isinstance(m, ToolMessage)]
    out_ids = [m.tool_call_id for m in out if isinstance(m, ToolMessage)]
    assert out_ids == orig_ids, "tool_call_id 序列应完全一致"


# ── 4. 小结果不回收 ──

def test_small_results_not_evicted():
    """旧 ToolMessage 内容 < min_chars（100 字）→ 即使超预算也保持原样。"""
    msgs, old_ids, recent_ids = _build_history(
        num_old_tools=8, old_char_count=100, num_recent_tools=6,
        small_old=True,
    )
    out, evicted = _evict_old_tool_results(msgs, budget=50, min_chars=1000)
    # old_char_count=100 < min_chars=1000 → 不回收
    assert evicted == 0

    # 所有旧 ToolMessage 原样
    for tid in old_ids:
        tm = next((m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == tid), None)
        assert tm is not None
        assert "已回收" not in tm.content


# ── 5. 不碰其它类型 ──

def test_other_message_types_untouched():
    """HumanMessage / AIMessage 再大也不被截。"""
    big_text = "A" * 5000
    msgs = [
        SystemMessage(content="系统"),
        HumanMessage(content=big_text),
        AIMessage(content=big_text),
        ToolMessage(content="X" * 2000, tool_call_id="t_old"),
        ToolMessage(content="Y" * 2000, tool_call_id="t_old2"),
        ToolMessage(content="Z" * 2000, tool_call_id="t_old3"),
        ToolMessage(content="W" * 2000, tool_call_id="t_old4"),
        ToolMessage(content="V" * 2000, tool_call_id="t_old5"),
        ToolMessage(content="U" * 2000, tool_call_id="t_old6"),
        ToolMessage(content="T" * 2000, tool_call_id="t_old7"),
        ToolMessage(content="S" * 2000, tool_call_id="t_old8"),
        # 至少 keep_recent=6 条 ToolMessage 在最近
        HumanMessage(content="最新问题"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r1"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r1"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r2"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r2"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r3"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r3"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r4"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r4"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r5"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r5"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "r6"}]),
        ToolMessage(content="R" * 2000, tool_call_id="r6"),
    ]
    out, _ = _evict_old_tool_results(msgs, budget=50)

    # Human/AIMessage 内容不变
    human_out = next(m for m in out if isinstance(m, HumanMessage) and m.content == big_text)
    assert human_out.content == big_text
    ai_out = next(m for m in out if isinstance(m, AIMessage) and m.content == big_text)
    assert ai_out.content == big_text


# ── 6. 不改原历史 ──

def test_original_history_unchanged():
    """传入的原 list 里的 ToolMessage 对象 content 不变（函数返回的是新对象）。"""
    msgs, old_ids, _ = _build_history(num_old_tools=8, old_char_count=2000)
    # 保存原 content
    original_contents = {id(m): m.content for m in msgs if isinstance(m, ToolMessage)}

    out, evicted = _evict_old_tool_results(msgs, budget=50)
    assert evicted > 0

    # 原 msgs 中的 ToolMessage content 不变
    for m in msgs:
        if isinstance(m, ToolMessage):
            assert m.content == original_contents[id(m)], f"原 ToolMessage {m.tool_call_id} 内容被改了"

    # out 中的应该是新对象（不是同一个）
    out_tools = [m for m in out if isinstance(m, ToolMessage)]
    for ot in out_tools:
        if "已回收" in ot.content:
            assert id(ot) not in {id(m) for m in msgs if isinstance(m, ToolMessage)}, \
                "回收后的应是新对象"


# ── 7. 全绿 + 不破坏其它测试 ──
# 这条由 pytest 自动跑，见文件头 import 和下面的烟雾测试

def test_no_eviction_when_no_tool_messages():
    """没有 ToolMessage 时不触发回收。"""
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    out, evicted = _evict_old_tool_results(msgs, budget=1)
    assert evicted == 0
    assert out is msgs
