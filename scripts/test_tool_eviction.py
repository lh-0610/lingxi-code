"""工具结果回收（水位线式）—— 自检测试。

直接构造 message 列表测 `_trim_tool_results`（不依赖 UI / 真实 LLM），再用真实的
`_prepare_stream_history` 和会话存取各走一遍。

守的是一条不报错的性质：回收会改写发给模型的前文，而 DeepSeek / Anthropic 的前缀缓存要求
前缀逐字节不变。折过的结果下一轮换了个折法、或者每轮都多折一条，一切照常工作，只是每轮
都按未命中计费。
"""
import json
import os

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from src import memory, session as _session, state, streaming
from src.streaming import (
    _estimate_tokens,
    _trim_tool_results,
    _trim_watermarks,
)


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


def _trim(msgs, ids=None, high=50, low=0, **kw):
    """小水位触发一次回收；low=0 = 能折的全折。"""
    return _trim_tool_results(msgs, [] if ids is None else ids, high=high, low=low, **kw)


def _tool(out, tid):
    return next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == tid)


# ── 1. 没到高水位不动 ──

def test_below_high_no_eviction():
    """几条短消息，high=10_000 → 返回原列表、evicted==0、不记任何 id。"""
    msgs = [
        SystemMessage(content="你好"),
        HumanMessage(content="短问题"),
        AIMessage(content="短回答"),
        ToolMessage(content="短结果", tool_call_id="t1"),
    ]
    ids = []
    out, evicted = _trim(msgs, ids, high=10_000, low=5_000)
    assert evicted == 0
    assert out is msgs  # 未动 → 返回同一对象
    assert ids == []


# ── 2. 回收旧的大结果 ──

def test_evict_old_large_results():
    """超过高水位时，旧的大 ToolMessage 被换成存根，最近 keep_recent 条保持完整。"""
    msgs, old_ids, recent_ids = _build_history(
        num_old_tools=8, old_char_count=2000, num_recent_tools=6,
    )
    ids = []
    out, evicted = _trim(msgs, ids)
    assert evicted == len(old_ids)
    assert ids == old_ids, "折过哪些要记下来，之后每轮照样折"

    for tid in old_ids:
        assert "已回收" in _tool(out, tid).content, f"旧 ToolMessage {tid} 未被回收"

    # 最近 6 条应保持完整（内容不含"已回收"）
    for tid in recent_ids:
        tm = _tool(out, tid)
        assert "已回收" not in tm.content, f"最近 ToolMessage {tid} 不应被回收"
        assert tm.content.startswith("R"), f"最近 ToolMessage {tid} 内容被改了"


# ── 3. 配对不破 ──

def test_pairing_preserved():
    """被回收的 ToolMessage 的 tool_call_id 与原来一致；消息条数不变。"""
    msgs, old_ids, recent_ids = _build_history(num_old_tools=8, old_char_count=2000)
    original_count = len(msgs)

    out, evicted = _trim(msgs)
    assert len(out) == original_count, "消息条数不应变"

    orig_ids = [m.tool_call_id for m in msgs if isinstance(m, ToolMessage)]
    out_ids = [m.tool_call_id for m in out if isinstance(m, ToolMessage)]
    assert out_ids == orig_ids, "tool_call_id 序列应完全一致"


# ── 4. 小结果不回收 ──

def test_small_results_not_evicted():
    """旧 ToolMessage 内容 < min_chars（100 字）→ 即使超水位也保持原样。"""
    msgs, old_ids, recent_ids = _build_history(
        num_old_tools=8, old_char_count=100, num_recent_tools=6,
        small_old=True,
    )
    out, evicted = _trim(msgs, min_chars=1000)
    assert evicted == 0
    for tid in old_ids:
        assert "已回收" not in _tool(out, tid).content


# ── 5. 不碰其它类型 ──

def test_other_message_types_untouched():
    """HumanMessage / AIMessage 再大也不被截。"""
    big_text = "A" * 5000
    msgs, _, _ = _build_history(num_old_tools=8, old_char_count=2000)
    msgs[1:1] = [HumanMessage(content=big_text), AIMessage(content=big_text)]
    out, evicted = _trim(msgs)
    assert evicted > 0
    assert next(m for m in out if isinstance(m, HumanMessage)).content == big_text
    assert next(m for m in out if isinstance(m, AIMessage)).content == big_text


# ── 6. 不改原历史 ──

def test_original_history_unchanged():
    """传入的原 list 里的 ToolMessage 对象 content 不变（函数返回的是新对象）。"""
    msgs, old_ids, _ = _build_history(num_old_tools=8, old_char_count=2000)
    original_contents = {id(m): m.content for m in msgs if isinstance(m, ToolMessage)}

    out, evicted = _trim(msgs)
    assert evicted > 0

    for m in msgs:
        if isinstance(m, ToolMessage):
            assert m.content == original_contents[id(m)], f"原 ToolMessage {m.tool_call_id} 内容被改了"
    originals = {id(m) for m in msgs}
    for ot in out:
        if isinstance(ot, ToolMessage) and "已回收" in ot.content:
            assert id(ot) not in originals, "回收后的应是新对象"


def test_no_eviction_when_no_tool_messages():
    """没有 ToolMessage 时不触发回收。"""
    msgs = [SystemMessage(content="s"), HumanMessage(content="h")]
    out, evicted = _trim(msgs, high=1)
    assert evicted == 0
    assert out is msgs


# ══════════════════════════════════════
# 水位线：一次折够、折过的不再变
# ══════════════════════════════════════

def _round(i, chars=4000, name="read_file", args=None):
    """一轮：模型发一个调用 + 工具结果。"""
    tid = f"call_{i}"
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": args or {"path": f"src/f{i}.py"}, "id": tid}]),
        ToolMessage(content=f"{i:03d}" + "x" * chars, tool_call_id=tid),
    ]


def test_stops_at_low_watermark():
    """折到低水位就停，不是把能折的全折掉——留着的内容模型还用得上。"""
    msgs = [SystemMessage(content="s")]
    for i in range(30):
        msgs += _round(i)
    total = _estimate_tokens(msgs)
    high, low = total - 1, total // 2
    ids = []
    out, evicted = _trim_tool_results(msgs, ids, high=high, low=low)
    assert 0 < evicted < 24, "只折到低水位为止（24 = 30 条减去受保护的最近 6 条）"
    assert _estimate_tokens(out) <= low
    # 再少折一条就还在低水位之上
    assert _estimate_tokens(_trim_tool_results(msgs, ids[:-1], high=10**9, low=0)[0]) > low
    assert ids == [f"call_{i}" for i in range(evicted)], "同一档里先折老的"


def test_prefix_stays_identical_between_trips():
    """核心性质：一次回收之后，接下来每一轮发出去的前缀和上一轮逐字节相同。

    以前的做法（超预算就把最近 6 条之外的全折掉）每轮都会多折一条，前缀每轮都断。"""
    history = [SystemMessage(content="s")]
    ids, prev, trips = [], None, []
    high, low = 30_000, 15_000        # 每轮约 1200 token，受保护的最近 6 条约 7200
    for i in range(60):
        history += _round(i)
        sent, evicted = _trim_tool_results(history, ids, high=high, low=low)
        dump = [(m.type, m.content) for m in sent]
        if evicted:
            trips.append(i)
        elif prev is not None:
            assert dump[:len(prev)] == prev, f"第 {i} 轮没有回收，前缀却变了"
        assert _estimate_tokens(sent) <= high + 2_000, "上下文被压在高水位附近"
        prev = dump
    assert 2 <= len(trips) <= 5, f"60 轮里应该只触发几次，实际 {trips}"
    gaps = [b - a for a, b in zip(trips, trips[1:])]
    assert min(gaps) >= 10, f"两次回收之间应该隔开十来轮，实际间隔 {gaps}"


def test_recorded_ids_applied_below_high():
    """折过的结果即使当前不超水位也照样折（重开会话、或者刚折完的下一轮）。"""
    msgs, old_ids, _ = _build_history(num_old_tools=3, old_char_count=2000)
    ids = [old_ids[1]]
    out, evicted = _trim_tool_results(msgs, ids, high=10**9, low=0)
    assert evicted == 0 and ids == [old_ids[1]]
    assert "已回收" in _tool(out, old_ids[1]).content
    assert "已回收" not in _tool(out, old_ids[0]).content


def test_stub_depends_only_on_content():
    """同一条结果，先折和后折出来的存根逐字节相同。"""
    msgs, old_ids, _ = _build_history(num_old_tools=8, old_char_count=2000)
    first, _ = _trim(msgs)
    again, _ = _trim_tool_results(msgs, list(old_ids), high=10**9, low=0)
    assert [m.content for m in first] == [m.content for m in again]


def test_skips_when_little_to_gain():
    """能折出来的太少就先不折：折了也降不下去，下一轮还会再触发，等于每轮都断缓存。"""
    msgs = [SystemMessage(content="s"), HumanMessage(content="问" * 40_000)]
    for i in range(8):
        msgs += _round(i, chars=1200)
    total = _estimate_tokens(msgs)
    ids = []
    out, evicted = _trim_tool_results(msgs, ids, high=total - 1, low=total // 2)
    assert evicted == 0 and out is msgs and ids == []
    # 超过真正的历史预算时不管多少都折
    out, evicted = _trim_tool_results(msgs, ids, high=total - 1, low=total // 2, hard_limit=total - 1)
    assert evicted == 2 and ids == ["call_0", "call_1"]


def _set_line(monkeypatch, value):
    from src import config as _cfg
    monkeypatch.setattr(_cfg, "CONTEXT_TRIM_TOKENS", value)


def test_watermarks_follow_setting_and_budget(monkeypatch):
    """回收线是设置项（context_trim_tokens）：涨过它触发，折到它的一半。"""
    from src.limits import CONTEXT_TRIM_TOKENS_DEFAULT
    assert _trim_watermarks(200_000) == (CONTEXT_TRIM_TOKENS_DEFAULT, CONTEXT_TRIM_TOKENS_DEFAULT // 2)
    _set_line(monkeypatch, 100_000)
    assert _trim_watermarks(200_000) == (100_000, 50_000)
    # 窗口小的模型：历史预算不到回收线，按预算和它的一半
    assert _trim_watermarks(24_000) == (24_000, 12_000)
    # 0 = 不提前回收，到预算才回收
    _set_line(monkeypatch, 0)
    assert _trim_watermarks(200_000) == (200_000, 100_000)


def test_line_above_default_cap_raises_history_budget(monkeypatch):
    """回收线设得比 20 万高（100 万窗口的模型）：历史预算跟着抬上去，不然 20 万就开始压缩、那条线永远到不了。"""
    from src import models
    from src.limits import MAX_HISTORY_BUDGET
    monkeypatch.setattr(models, "context_window_for", lambda *a: 1_048_576)
    monkeypatch.setattr(models, "_max_tokens_for", lambda *a: 16_384)
    assert streaming._current_history_budget() == MAX_HISTORY_BUDGET
    _set_line(monkeypatch, 400_000)
    assert streaming._current_history_budget() == 400_000
    assert _trim_watermarks(streaming._current_history_budget()) == (400_000, 200_000)
    # 再高也不超过模型窗口
    _set_line(monkeypatch, 5_000_000)
    assert streaming._current_history_budget() == 1_048_576 - 16_384 - 8_000
    # 窗口小的模型不受回收线影响
    monkeypatch.setattr(models, "context_window_for", lambda *a: 32_000)
    monkeypatch.setattr(models, "_max_tokens_for", lambda *a: 8_192)
    assert streaming._current_history_budget() == 32_000 - 8_192 - 8_000


def test_config_value_is_sanitized():
    """config.json 里写坏的值不能让程序起不来，也不能几轮就回收一次。"""
    from src.config import _context_trim_tokens_from
    from src.limits import CONTEXT_TRIM_TOKENS_DEFAULT, CONTEXT_TRIM_TOKENS_MIN
    assert _context_trim_tokens_from({}) == CONTEXT_TRIM_TOKENS_DEFAULT
    for raw, expected in [(0, 0), (-5, 0), (300_000, 300_000), ("120000", 120_000),
                          (500, CONTEXT_TRIM_TOKENS_MIN), ("很多", CONTEXT_TRIM_TOKENS_DEFAULT),
                          (True, CONTEXT_TRIM_TOKENS_DEFAULT)]:
        assert _context_trim_tokens_from({"context_trim_tokens": raw}) == expected, f"{raw!r} 应得到 {expected}"


def test_oldest_first_whatever_the_tool():
    """从最老的折起，不看是什么工具的结果（按内容排先后的五种办法回放下来没有区别）。"""
    msgs = [SystemMessage(content="s")]
    msgs += _round(0, name="search_files", args={"regex": "foo"})
    msgs += _round(1, args={"path": "src/a.py"})
    msgs += _round(2, name="run_command", args={"command": "dir"})
    msgs += _round(3, args={"path": "src/b.py"})
    for i in range(10, 16):                                   # 最近 6 条，受保护
        msgs += _round(i, chars=200)
    total = _estimate_tokens(msgs)
    one = _estimate_tokens(_round(0)) - 60                    # 折掉一条大约省这么多
    for count in (1, 2, 3, 4):
        ids = []
        _trim_tool_results(msgs, ids, high=total - 1, low=total - one * count + one // 2, hard_limit=0)
        assert ids == [f"call_{i}" for i in range(count)]


# ══════════════════════════════════════
# 真实的发送流程 + 会话存取
# ══════════════════════════════════════

class _UI:
    def __init__(self):
        self.shown = []

    def show_message(self, text, tag=None):
        self.shown.append(text)


@pytest.fixture()
def sess(isolated_memory, tmp_path, monkeypatch):
    s = _session.Session()
    s.project = str(tmp_path)
    _session.bind_thread(s)
    monkeypatch.setattr(state, "current_project", str(tmp_path))
    monkeypatch.setattr(streaming, "_current_history_budget", lambda: 200_000)
    _set_line(monkeypatch, 100_000)
    yield s
    _session.bind_thread(None)


def _sent_tools(history):
    return {m.tool_call_id: m.content for m in history if isinstance(m, ToolMessage)}


def test_prepare_stream_history_trims_once_and_remembers(sess):
    """走真实的 _prepare_stream_history：涨过 10 万折一次，之后几轮折出来的一模一样。"""
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")]
    for i in range(34):
        sess.chat_history += _round(i, chars=12_000)            # 每条约 3600 token
    ui = _UI()
    first, _ = streaming._prepare_stream_history(ui)
    assert sess.evicted_tool_results, "超过高水位应当回收"
    assert any("♻️" in t for t in ui.shown)
    est = _estimate_tokens(first)
    assert est <= 55_000, f"应降到低水位附近，实际 {est}"
    assert all(isinstance(m.content, str) and len(m.content) > 12_000
               for m in sess.chat_history if isinstance(m, ToolMessage)), "只动发送副本，会话历史保持完整"

    recorded = list(sess.evicted_tool_results)
    ui.shown.clear()
    for i in range(34, 38):
        sess.chat_history += _round(i, chars=12_000)
        again, _ = streaming._prepare_stream_history(ui)
        before = _sent_tools(first)
        assert {k: v for k, v in _sent_tools(again).items() if k in before} == before
    assert sess.evicted_tool_results == recorded, "没再涨过高水位，不该再折"
    assert not any("♻️" in t for t in ui.shown)


def test_second_trip_reports_the_size_actually_sent(sess):
    """第二次回收时，提示里的"折之前"是上一轮真正发出去的大小，不是把早就折掉的也算上的完整历史。"""
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")]
    ui = _UI()
    notices = []
    for i in range(60):
        sess.chat_history += _round(i, chars=12_000)
        streaming._prepare_stream_history(ui)
        notices += [t for t in ui.shown if "♻️" in t]
        ui.shown.clear()
    assert len(notices) >= 2, "60 轮每轮 3600 token，应当触发不止一次"
    for text in notices:
        before = int(text.split("约 ")[1].split("k")[0])
        assert 100 <= before <= 106, f"折之前应当刚过 10 万，提示里写的是 {before}k"


def test_evicted_ids_survive_reload(sess, isolated_memory):
    """折过哪些随会话落盘：重开后发出去的前缀和关掉之前一样。"""
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")]
    for i in range(34):
        sess.chat_history += _round(i, chars=12_000)
    before, _ = streaming._prepare_stream_history(_UI())
    recorded = list(sess.evicted_tool_results)
    assert recorded
    memory.save_session()
    sid = sess.current_session_id
    path = os.path.join(str(isolated_memory), f"{sid}.json")
    assert json.load(open(path, encoding="utf-8"))["evicted_tool_results"] == recorded

    fresh = _session.Session()
    assert memory.load_session(sid, session=fresh)
    assert fresh.evicted_tool_results == recorded
    fresh.project = sess.project
    _session.bind_thread(fresh)
    after, _ = streaming._prepare_stream_history(_UI())
    assert _sent_tools(after) == _sent_tools(before)
    assert fresh.evicted_tool_results == recorded


def test_save_drops_ids_of_removed_messages(sess, isolated_memory):
    """消息被删掉（重新生成 / 撤回）后，对应的回收记录不再落盘。"""
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")] + _round(0) + _round(1)
    sess.evicted_tool_results = ["call_0", "gone", "call_1"]
    memory.save_session()
    path = os.path.join(str(isolated_memory), f"{sess.current_session_id}.json")
    assert json.load(open(path, encoding="utf-8"))["evicted_tool_results"] == ["call_0", "call_1"]


def test_legacy_session_without_field(sess, isolated_memory):
    """旧会话文件没有这个键、或者被手改坏了 → 当成没折过，不能崩。"""
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")] + _round(0)
    memory.save_session()
    path = os.path.join(str(isolated_memory), f"{sess.current_session_id}.json")
    data = json.load(open(path, encoding="utf-8"))
    for broken in (None, "call_0", [1, None, "call_0", ""]):
        if broken is None:
            data.pop("evicted_tool_results", None)
        else:
            data["evicted_tool_results"] = broken
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        fresh = _session.Session()
        assert memory.load_session(sess.current_session_id, session=fresh)
        assert fresh.evicted_tool_results == (["call_0"] if isinstance(broken, list) else [])


def test_new_conversation_forgets_evictions(sess):
    sess.chat_history = [SystemMessage(content="s"), HumanMessage(content="开始")] + _round(0)
    sess.evicted_tool_results = ["call_0"]
    memory.reset_history(session=sess)
    assert sess.evicted_tool_results == []
