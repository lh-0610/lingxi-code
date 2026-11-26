"""离线回放工具的回归：合成会话跑**真实**的发送准备流程，前缀稳定性必须成立。

会话在测试里现造（几十轮、每轮一个大工具结果），不读 chat_memory/、不联网、不落盘——
真实会话不进仓库。守的性质和 test_tool_eviction 一样，但这里经过完整的
`_prepare_stream_history`（system 重渲染、尾部易变消息、剥 reasoning、硬截断、压缩、修配对），
而不是只测 `_trim_tool_results` 一步。
"""
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import replay_sessions
from src import config, roles, state, streaming


def _synthetic(rounds=60, chars=4000):
    """一轮 = 模型发一个 read_file 调用 + 一条大工具结果；最后一轮是收尾正文。"""
    messages = [SystemMessage(content="固定系统提示"), HumanMessage(content="把登录超时的重试补上")]
    for i in range(rounds):
        call_id = f"call_{i}"
        messages.append(AIMessage(content="", tool_calls=[
            {"name": "read_file", "args": {"path": f"src/m{i}.py"}, "id": call_id}]))
        messages.append(ToolMessage(content=f"{i:03d}" + "x" * chars, tool_call_id=call_id))
    messages.append(AIMessage(content="改完了，测试通过。"))
    return messages


@pytest.fixture(autouse=True)
def _offline_pricing(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PRICING", {}, raising=False)


@pytest.fixture(autouse=True)
def _pinned_system_prompt(monkeypatch):
    """system 钉成固定短串：本文件守的是回收/前缀机制，不是提示词内容。

    _prepare_stream_history 每轮重渲染活的 SYSTEM_PROMPT，提示词一变每轮上下文估算就跟着变，
    回收点会漂移（浏览器段 +796 字符曾把最后一次回收顶到会话最后一轮，夹具红）。钉住后
    回收点稳定在 [26, 39, 52]（60 轮 / 4000 字符 / 水位 3 万），不再随提示词演化漂移。
    """
    monkeypatch.setattr(roles, "get_system_prompt", lambda *a, **k: "固定系统提示（回放测试钉住）")


@pytest.fixture(autouse=True)
def _pinned_tool_overhead(monkeypatch):
    """工具定义占的 token 也钉住（理由同上面的 system）：回收线扣掉的是整份请求里的工具定义，
    每加一个工具回收点就往前挪。2026-11-26 浏览器加了 4 个工具（+1.2K），两次回收的间隔从
    8 轮缩到 7 轮，夹具红——这里守的是回收机制，不是工具清单有多长。"""
    monkeypatch.setattr(streaming, "_tools_overhead_tokens", lambda: 12_000)


@pytest.fixture()
def trim_line(monkeypatch):
    """回收线设到 3 万：60 轮里能触发几次，又不至于每轮都触发。"""
    monkeypatch.setattr(config, "CONTEXT_TRIM_TOKENS", 30_000)


def _trips(report):
    return [row["round"] for row in report["rounds"] if row["evicted"]]


def test_prefix_stable_between_evictions(trim_line):
    report = replay_sessions.replay_session(_synthetic())
    rows = report["rounds"]
    trips = _trips(report)

    assert len(rows) == 61
    assert 2 <= len(trips) <= 6, f"60 轮里应该只触发几次回收，实际 {trips}"
    gaps = [b - a for a, b in zip(trips, trips[1:])]
    assert min(gaps) >= 8, f"两次回收之间应该隔开若干轮，实际间隔 {gaps}"
    # 回收会改写前文，那一轮断是应该的；其余每一轮都必须逐字节接着上一轮
    assert [row["round"] for row in rows if row["break_index"] is not None] == trips
    assert report["summary"]["breaks"] == len(trips)


def test_eviction_round_reports_reason_and_position(trim_line):
    report = replay_sessions.replay_session(_synthetic())
    for row in report["rounds"]:
        if not row["evicted"]:
            continue
        assert row["break_reason"] == "工具结果回收"
        assert row["break_index"] >= 1, "断裂位置要指到具体第几条消息"


def test_prefix_recovers_from_the_round_after_each_trip(trim_line):
    """回收那一轮之后，折叠照原样保持：下一轮起前缀立刻恢复稳定，不再连着断。"""
    rows = replay_sessions.replay_session(_synthetic())["rounds"]
    trips = _trips({"rounds": rows})
    assert len(trips) >= 2, f"回收太少（{trips}），下面的循环会空转，夹具失去守护意义"
    for trip in trips:
        following = [row for row in rows if trip < row["round"] <= trip + 3]
        assert following, f"第 {trip} 轮之后没有轮次了，构造的会话太短"
        assert all(row["break_index"] is None for row in following), (
            f"第 {trip} 轮回收之后又断了：{[(r['round'], r['break_reason']) for r in following]}")


def test_volatile_tail_changes_are_not_breaks(trim_line, monkeypatch):
    """每轮易变内容都不同（真实运行就是这样），不能算断裂。"""
    texts, llms = [], []

    def changing():
        llms.append(state.llm)
        texts.append(f"运行态 {len(texts)}")
        return texts[-1]

    monkeypatch.setattr(roles, "get_volatile_context", changing)
    report = replay_sessions.replay_session(_synthetic())

    assert len(texts) == len(report["rounds"]), "每轮都该渲染一次易变上下文"
    assert len(set(texts)) == len(texts), "构造的易变内容必须每轮都不同，否则这条用例是空跑"
    assert all(isinstance(llm, replay_sessions.OfflineLLM) for llm in llms), "回放不许用真实 LLM"
    assert [row["round"] for row in report["rounds"] if row["break_index"] is not None] == _trips(report)


def test_compaction_is_reported_not_executed(monkeypatch):
    """超预算时压缩要调 LLM：回放只报告"这一轮会触发压缩"，不真的压、不联网。"""
    monkeypatch.setattr(config, "CONTEXT_TRIM_TOKENS", 0)
    # 一条折不掉的巨型用户消息（回收只折工具结果）把估算顶过历史预算
    messages = [SystemMessage(content="固定系统提示"), HumanMessage(content="问" * 700_000)]
    messages += _synthetic(rounds=24)[2:]
    seen = []
    monkeypatch.setattr(roles, "get_volatile_context", lambda: seen.append(state.llm) or "运行态")

    report = replay_sessions.replay_session(messages)

    assert report["summary"]["compactions"] > 0, "这个构造应该触发压缩"
    assert report["rounds"][-1]["compaction"], "历史长到有了中段，就该报告本轮会压缩"
    assert not report["rounds"][0]["compaction"], "还没有中段可压时不该报"
    assert all(isinstance(llm, replay_sessions.OfflineLLM) for llm in seen)
    assert all(row["break_reason"] != "历史压缩" for row in report["rounds"]), "回放不该真的压缩"


def test_equivalent_tokens_when_no_price(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TRIM_TOKENS", 30_000)
    model_id = streaming.MODEL_LIST[replay_sessions.default_model_index()][2]
    monkeypatch.setattr(config, "MODEL_PRICING", {model_id: {"cache_hit_equivalent_ratio": 0.1}})

    summary = replay_sessions.replay_session(_synthetic())["summary"]

    assert summary["cost"] is None
    assert summary["equivalent_tokens"] == pytest.approx(
        summary["miss_tokens"] + 0.1 * summary["hit_tokens"] + summary["output_tokens"])


def test_cost_when_priced(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_TRIM_TOKENS", 30_000)
    model_id = streaming.MODEL_LIST[replay_sessions.default_model_index()][2]
    monkeypatch.setattr(config, "MODEL_PRICING", {model_id: {
        "currency": "CNY", "input_miss_per_million": 2,
        "input_hit_per_million": 0.2, "output_per_million": 8}})

    summary = replay_sessions.replay_session(_synthetic())["summary"]

    assert summary["equivalent_tokens"] is None, "配了单价就报金额，不再报等价 token"
    assert summary["cost"]["currency"] == "CNY"
    assert summary["cost"]["amount"] == pytest.approx(
        (summary["miss_tokens"] * 2 + summary["hit_tokens"] * 0.2
         + summary["output_tokens"] * 8) / 1e6)


def test_unpriced_reports_tokens_only(trim_line):
    report = replay_sessions.replay_session(_synthetic())
    summary = report["summary"]
    assert summary["cost"] is None and summary["equivalent_tokens"] is None
    assert summary["miss_tokens"] > 0 and summary["hit_tokens"] > 0
    assert summary["input_tokens"] == sum(row["tokens"] for row in report["rounds"])
    assert summary["peak_tokens"] == max(row["tokens"] for row in report["rounds"])
    assert summary["miss_tokens"] + summary["hit_tokens"] == summary["input_tokens"]


def test_loader_reads_only_the_given_directory(tmp_path):
    """会话来源由参数指定：只读传进来的目录，不碰真实 chat_memory。"""
    for sid, title in (("20260101_000000_000001", "甲"), ("20260101_000000_000002", "乙")):
        (tmp_path / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": title,
            "messages": [{"type": "SystemMessage", "content": "s"},
                         {"type": "HumanMessage", "content": "问"},
                         {"type": "AIMessage", "content": "答"}],
        }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "20260101_000000_000001.inflight.json").write_text("{}", encoding="utf-8")

    loaded = replay_sessions.load_sessions(str(tmp_path))
    assert [item["id"] for item in loaded] == ["20260101_000000_000001", "20260101_000000_000002"]
    assert isinstance(loaded[0]["messages"][2], AIMessage)

    picked = replay_sessions.load_sessions(str(tmp_path), ["20260101_000000_000002"])
    assert [item["id"] for item in picked] == ["20260101_000000_000002"]


def test_cli_summarizes_a_synthetic_directory(tmp_path, trim_line, capsys):
    """命令行入口整条走一遍：合成会话存成真实格式，再按目录回放并汇总。"""
    from src import memory

    sid = "20260101_000000_000001"
    (tmp_path / f"{sid}.json").write_text(json.dumps({
        "id": sid, "title": "合成会话",
        "messages": [memory._msg_to_dict(m) for m in _synthetic(rounds=30)],
    }, ensure_ascii=False), encoding="utf-8")

    assert replay_sessions.main(["--dir", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "合成会话" in out and "=== 汇总 ===" in out
    assert "1 个会话 · 31 轮" in out
    assert "局限" in out, "必须说明成本是近似"