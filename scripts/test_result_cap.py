"""M4 单条工具结果 token 硬上限 —— 自检测试。

直接构造 message 列表测 `_cap_oversized_tool_results`（不依赖 UI / 真实 LLM）。
按 docs/context_result_cap_m4_spec.md "自检" 章节的 9 条写全。
"""

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src.streaming import (
    _cap_oversized_tool_results,
    _evict_old_tool_results,
)


def _make_tool_msg(tool_call_id: str, char_count: int, label: str = "x") -> ToolMessage:
    """构造指定长度的 ToolMessage。"""
    return ToolMessage(content=label * char_count, tool_call_id=tool_call_id)


# ── 1. 未超预算不动 ──


def test_below_budget_no_capping():
    """短消息 + budget=10_000 → 原样返回、capped==0、out is msgs。"""
    msgs = [
        SystemMessage(content="你好"),
        HumanMessage(content="短问题"),
        AIMessage(content="短回答"),
        ToolMessage(content="短结果", tool_call_id="t1"),
    ]
    out, capped = _cap_oversized_tool_results(msgs, budget=10_000)
    assert capped == 0
    assert out is msgs


# ── 2. 截大结果 ──


def test_cap_large_result():
    """一条 40_000 字符的 ToolMessage + budget=50 → 被截；
    新内容含"中段"+"已截断"标记；长度远小于 40_000；
    开头 == 原开头前 12_000、结尾 == 原结尾后 6_000。"""
    big_content = "A" * 20_000 + "B" * 20_000  # 40_000 chars
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="h"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "t_big"}]),
        ToolMessage(content=big_content, tool_call_id="t_big"),
    ]
    out, capped = _cap_oversized_tool_results(msgs, budget=50)
    assert capped == 1

    out_tool = next(m for m in out if isinstance(m, ToolMessage))
    assert "中段" in out_tool.content
    assert "已截断" in out_tool.content
    # 长度远小于 40_000（head=12_000 + tail=6_000 + 标记 ≈ 18_050）
    assert len(out_tool.content) < 20_000
    # 开头 == 原开头前 12_000
    assert out_tool.content[:12_000] == big_content[:12_000]
    # 结尾（去标记后）== 原结尾后 6_000
    assert out_tool.content[-6_000:] == big_content[-6_000:]


# ── 3. 最近的也截（与 M2 的关键区别） ──


def test_cap_recent_large_result():
    """大结果放在最后一条 ToolMessage（最近）→ 仍被截。"""
    big_content = "X" * 40_000
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="h1"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "t1"}]),
        ToolMessage(content="small result", tool_call_id="t1"),
        HumanMessage(content="h2"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "t2"}]),
        ToolMessage(content=big_content, tool_call_id="t2"),  # 最近的
    ]
    out, capped = _cap_oversized_tool_results(msgs, budget=50)
    assert capped == 1

    # t2（最近的）应被截
    t2_out = next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "t2")
    assert "中段" in t2_out.content
    assert len(t2_out.content) < 20_000

    # t1（小的）原样
    t1_out = next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "t1")
    assert t1_out.content == "small result"


# ── 4. 配对不破 ──


def test_pairing_preserved():
    """被截的 ToolMessage tool_call_id 不变；消息条数不变。"""
    big = "Z" * 40_000
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="h"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "tc1"}]),
        ToolMessage(content=big, tool_call_id="tc1"),
    ]
    original_count = len(msgs)
    out, capped = _cap_oversized_tool_results(msgs, budget=50)
    assert len(out) == original_count, "消息条数不应变"

    out_tool = next(m for m in out if isinstance(m, ToolMessage))
    assert out_tool.tool_call_id == "tc1", "tool_call_id 应不变"


# ── 5. 小结果不截 ──


def test_small_result_not_capped():
    """内容 < cap（1000 字）→ 即使超预算也原样。"""
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="h"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "t_small"}]),
        ToolMessage(content="S" * 1000, tool_call_id="t_small"),
    ]
    out, capped = _cap_oversized_tool_results(msgs, budget=50, cap=24_000)
    assert capped == 0

    out_tool = next(m for m in out if isinstance(m, ToolMessage))
    assert out_tool.content == "S" * 1000


# ── 6. 不碰其它类型 ──


def test_other_message_types_untouched():
    """超大 HumanMessage / AIMessage 不被截。"""
    big = "B" * 50_000
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content=big),
        AIMessage(content=big),
        # 加一条大 ToolMessage 触发超预算
        ToolMessage(content="T" * 40_000, tool_call_id="t_cap"),
    ]
    out, _ = _cap_oversized_tool_results(msgs, budget=50)

    human_out = next(m for m in out if isinstance(m, HumanMessage))
    assert human_out.content == big
    ai_out = next(m for m in out if isinstance(m, AIMessage))
    assert ai_out.content == big


# ── 7. 不改原历史 ──


def test_original_history_unchanged():
    """原 list 里的 ToolMessage content 不变（返回新对象）。"""
    big = "M" * 40_000
    msgs = [
        SystemMessage(content="sys"),
        HumanMessage(content="h"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "t1"}]),
        ToolMessage(content=big, tool_call_id="t1"),
    ]
    original_contents = {id(m): m.content for m in msgs if isinstance(m, ToolMessage)}

    out, capped = _cap_oversized_tool_results(msgs, budget=50)
    assert capped == 1

    # 原 msgs 中的 ToolMessage content 不变
    for m in msgs:
        if isinstance(m, ToolMessage):
            assert m.content == original_contents[id(m)], \
                f"原 ToolMessage {m.tool_call_id} 内容被改了"

    # out 中被截的应是新对象
    out_tool = next(m for m in out if isinstance(m, ToolMessage))
    assert id(out_tool) != id(msgs[3]), "截断后应是新 ToolMessage 对象"


# ── 8. 与 M2 串联 ──


def test_cap_then_evict():
    """一条历史里既有"旧大结果"又有"最近大结果"，
    过 _cap_oversized_tool_results 再过 _evict_old_tool_results（都用小 budget）→
    最近的被截成头+尾、旧的被回收成存根、配对完整。"""
    big = "G" * 40_000
    msgs = [
        SystemMessage(content="sys"),
        # 旧轮次（8 条旧 ToolMessage，超出 keep_recent=6）
        *[m for i in range(8) for m in (
            HumanMessage(content=f"旧问题 {i}"),
            AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": f"old_{i}"}]),
            ToolMessage(content=big, tool_call_id=f"old_{i}"),
        )],
        # 最近轮次
        HumanMessage(content="新问题"),
        AIMessage(content="", tool_calls=[{"name": "r", "args": {}, "id": "recent_1"}]),
        ToolMessage(content=big, tool_call_id="recent_1"),
    ]
    original_count = len(msgs)

    # Step 1: M4 硬上限
    after_cap, capped = _cap_oversized_tool_results(msgs, budget=50)
    assert capped > 0

    # Step 2: M2 回收
    after_evict, evicted = _evict_old_tool_results(after_cap, budget=50)
    assert evicted > 0

    # 消息条数不变
    assert len(after_evict) == original_count

    # 最近的 recent_1：被 M4 截成头+尾（含"中段"标记），不含"已回收"
    r1 = next(m for m in after_evict if isinstance(m, ToolMessage) and m.tool_call_id == "recent_1")
    assert "中段" in r1.content, "最近的大结果应被 M4 截成头+尾"
    assert "已回收" not in r1.content, "最近的结果不应被 M2 回收"

    # 旧的 old_0：被 M2 回收成存根（含"已回收"）
    o0 = next(m for m in after_evict if isinstance(m, ToolMessage) and m.tool_call_id == "old_0")
    assert "已回收" in o0.content, "旧的大结果应被 M2 回收成存根"

    # 配对完整：所有 tool_call_id 都在
    orig_ids = [m.tool_call_id for m in msgs if isinstance(m, ToolMessage)]
    out_ids = [m.tool_call_id for m in after_evict if isinstance(m, ToolMessage)]
    assert out_ids == orig_ids, "tool_call_id 序列应完全一致"


# ── 9. 全绿 + 不破坏其它测试 ──
# 这条由 pytest 自动跑，见文件头 import 和下面的烟雾测试


def test_no_capping_when_no_tool_messages():
    """没有 ToolMessage 时不触发截断。"""
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    out, capped = _cap_oversized_tool_results(msgs, budget=1)
    assert capped == 0
    assert out is msgs
