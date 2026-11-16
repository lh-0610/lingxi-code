import copy
import json
import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import run_ledger
from src.run_ledger import RunLedger, append_record, normalize


MODEL = {"id": "model-a", "name": "模型甲", "type": "api"}
OTHER = {"id": "model-b", "name": "模型乙", "type": "cli"}
KEYS = ("rounds", "input", "output", "total", "cache_read", "cache_write", "reasoning")
BAD_COUNTS = [True, False, -1, 1.0, "2", float("nan"), float("inf"), [], {}]
BAD_PRICES = [True, False, -1, "2", float("nan"), float("inf"), [], {}, None]


def usage(**changes):
    result = {"input": 100, "output": 20, "total": 120,
              "cache_read": 40, "cache_write": 10, "reasoning": 8}
    result.update(changes)
    return result


def price(**changes):
    result = {"currency": "CNY", "input_miss_per_million": 2,
              "input_hit_per_million": 0.5, "output_per_million": 6}
    result.update(changes)
    return result


def row_map(snapshot):
    return {(row["scope"], row["model"]["id"], row["model"]["type"]): row
            for row in snapshot["models"]}


def test_import_is_standard_library_only():
    code = (
        "import sys; import src.run_ledger; "
        "assert 'src.paths' not in sys.modules; "
        "assert 'src.config' not in sys.modules; "
        "assert not any(k.startswith(('PySide', 'PyQt', 'langchain')) for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_initial_snapshot_has_zero_counts_and_no_amount():
    ledger = RunLedger(MODEL)
    snapshot = ledger.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=0.25)})
    assert set(snapshot) == {"version", "main", "subagents", "total", "tools",
                             "subagent_tools", "models", "cache_breaks", "cost",
                             "equivalent_tokens"}
    assert snapshot["version"] == 1
    for key in ("main", "subagents", "total"):
        assert snapshot[key] == dict.fromkeys(KEYS, 0)
    assert snapshot["tools"] == snapshot["subagent_tools"] == {}
    assert snapshot["cache_breaks"] == []
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None
    for row in snapshot["models"]:
        assert row["usage"] == dict.fromkeys(KEYS, 0)
        assert row["cost"] is row["equivalent_tokens"] is None
    assert normalize(snapshot) == snapshot


def test_multiple_rounds_and_reasoning_are_not_double_counted():
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage(total=999))
    ledger.record_usage(usage(input=50, output=10, total=None, cache_read=20,
                              cache_write=5, reasoning=4), rounds=2)
    snapshot = ledger.snapshot({MODEL["id"]: price()})
    assert snapshot["main"] == dict(zip(KEYS, [3, 150, 30, 180, 60, 15, 12]))
    assert snapshot["total"] == snapshot["main"]
    assert snapshot["subagents"] == dict.fromkeys(KEYS, 0)
    assert snapshot["cost"]["amount"] == pytest.approx((90 * 2 + 60 * 0.5 + 30 * 6) / 1e6)
    assert snapshot["equivalent_tokens"] is None


def test_missing_fields_remain_unknown_after_later_known_usage():
    ledger = RunLedger(MODEL)
    ledger.record_usage({"input": 10}, rounds=None)
    ledger.record_usage(usage())
    counts = ledger.snapshot()["main"]
    assert counts["input"] == 110
    assert all(counts[key] is None for key in KEYS if key != "input")
    assert ledger.snapshot()["total"] == counts


def test_reported_total_is_kept_only_when_components_are_unknown():
    ledger = RunLedger(MODEL)
    ledger.record_usage({"total": 99}, rounds=None)
    counts = ledger.snapshot()["main"]
    assert counts["total"] == 99
    assert counts["input"] is counts["output"] is counts["rounds"] is None
    ledger.record_usage({"input": 1})
    assert ledger.snapshot()["total"]["total"] is None


@pytest.mark.parametrize("bad", BAD_COUNTS)
@pytest.mark.parametrize("key", ["input", "output", "cache_read", "cache_write", "reasoning"])
def test_invalid_counts_are_unknown(key, bad):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage(**{key: bad}))
    ledger.record_usage(usage())
    assert ledger.snapshot()["main"][key] is None
    json.dumps(ledger.snapshot(), allow_nan=False)


@pytest.mark.parametrize("bad", BAD_COUNTS + [None])
def test_invalid_rounds_and_tools_propagate_unknown(bad):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage(), rounds=bad)
    ledger.record_usage(usage())
    ledger.record_tool("read_file", bad, calls=bad)
    ledger.record_tool("read_file", 5)
    snapshot = ledger.snapshot()
    assert snapshot["main"]["rounds"] is None
    assert snapshot["tools"]["read_file"] == {"calls": None, "result_chars": None}


@pytest.mark.parametrize("raw", [None, [], "body", 12])
def test_missing_usage_payload_is_a_round_with_unknown_tokens(raw):
    ledger = RunLedger(MODEL)
    ledger.record_usage(raw)
    assert ledger.snapshot()["main"] == {key: 1 if key == "rounds" else None for key in KEYS}


def test_tools_store_totals_not_bodies_or_parameters():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    parent.record_tool("read_file", 10, calls=2)
    child.record_tool("read_file", 30, calls=3)
    child.record_tool("search_files", None)
    child.record_tool("search_files", "不能保存的工具正文")
    snapshot = parent.snapshot()
    assert snapshot["tools"] == {"read_file": {"calls": 5, "result_chars": 40},
                                 "search_files": {"calls": 2, "result_chars": None}}
    assert snapshot["subagent_tools"] == {"read_file": {"calls": 3, "result_chars": 30},
                                          "search_files": {"calls": 2, "result_chars": None}}
    assert snapshot["subagents"] == dict.fromkeys(KEYS, 0)
    assert "不能保存" not in json.dumps(snapshot, ensure_ascii=False)


def test_scope_model_identity_and_snapshot_inputs_are_isolated():
    model = dict(MODEL, api_key="secret")
    parent = RunLedger(model)
    model["id"] = "changed"
    child = RunLedger(MODEL, parent)
    switched = dict(MODEL, type="cli", name="同标识另一后端")
    parent.record_usage(usage())
    parent.record_usage(usage(), model=switched)
    switched["type"] = "changed"
    child.record_usage(usage(), model=OTHER)
    child.record_usage(usage())
    snapshot = parent.snapshot()
    rows = row_map(snapshot)
    assert set(rows) == {("main", MODEL["id"], "api"), ("main", MODEL["id"], "cli"),
                         ("subagent", MODEL["id"], "api"), ("subagent", OTHER["id"], "cli")}
    assert snapshot["main"]["total"] == snapshot["subagents"]["total"] == 240
    assert snapshot["total"]["total"] == 480
    assert "secret" not in json.dumps(snapshot)
    snapshot["models"][0]["model"]["id"] = "mutated"
    snapshot["models"][0]["usage"]["input"] = -1
    snapshot["main"]["input"] = -1
    assert parent.snapshot()["main"]["input"] == 200
    assert all(r["model"]["id"] != "mutated" for r in parent.snapshot()["models"])


def test_safe_events_forward_immediately_with_scope_and_no_payloads():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    event = {"round": 2, "position": None, "before_hash": "a" * 64,
             "after_hash": "b" * 64, "tool": "read_file", "scope": "main",
             "args": {"token": "secret"}, "content": "private body"}
    child.record_event(event)
    main_event = {"round": 1, "position": 8, "hash": "c" * 64}
    parent.record_event(main_event)
    event["before_hash"] = "changed"
    snapshot = parent.snapshot()
    assert snapshot["cache_breaks"] == [
        {"round": 2, "position": None, "before_hash": "a" * 64,
         "after_hash": "b" * 64, "tool": "read_file", "scope": "subagent"},
        dict(main_event, scope="main"),
    ]
    assert child.snapshot()["cache_breaks"][0]["scope"] == "main"
    snapshot["cache_breaks"][0]["round"] = 999
    assert parent.snapshot()["cache_breaks"][0]["round"] == 2
    assert "secret" not in json.dumps(parent.snapshot())
    assert normalize(parent.snapshot()) == parent.snapshot()


@pytest.mark.parametrize("reason,source", [
    ("工具列表变了", "发送前"), ("system 变了", "发送前"), ("工具结果回收", "发送前"),
    ("历史压缩", "发送前"), ("未知", "发送前"),
    ("命中率骤降（未检测到本地前缀变化）", "服务端侧"),
])
def test_fixed_cache_event_tags_survive_snapshot_and_normalize(reason, source):
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    event = {"round": 2, "message_index": 3, "reason": reason, "source": source,
             "previous_hit_rate": 0.9, "hit_rate": 0.2}
    child.record_event(event)
    snapshot = parent.snapshot()
    assert snapshot["cache_breaks"] == [dict(event, scope="subagent")]
    assert normalize(snapshot) == snapshot


def test_cache_event_free_text_is_not_mistaken_for_fixed_tags():
    ledger = RunLedger(MODEL)
    ledger.record_event({"round": 2, "reason": "不能保存的正文", "source": "不能保存的参数"})
    assert ledger.snapshot()["cache_breaks"] == [{"round": 2, "scope": "main"}]


def test_grandchildren_forward_once_and_close_never_merges():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    grandchild = RunLedger(MODEL, child)
    grandchild.record_usage(usage(), rounds=None)
    grandchild.record_tool("read_file", 12)
    grandchild.record_event({"round": 1, "position": 3})
    before = parent.snapshot()
    assert before["total"]["total"] == 120
    assert before["subagents"]["rounds"] is None
    assert child.snapshot()["subagents"]["total"] == 120
    grandchild.close()
    grandchild.close()
    child.close()
    parent.close()
    assert parent.snapshot() == before


def test_closed_parent_and_captured_parent_prevent_late_writes(caplog):
    session = SimpleNamespace(ledger=RunLedger(MODEL))
    old_parent = session.ledger
    child = RunLedger(OTHER, old_parent)
    child.record_usage(usage())
    old_parent.close()
    before = old_parent.snapshot()
    session.ledger = RunLedger(MODEL)
    with caplog.at_level(logging.WARNING):
        child.record_usage(usage())
        child.record_tool("read_file", 30)
        child.record_event({"round": 2})
        old_parent.record_usage(usage())
        old_parent.record_tool("read_file", 10)
        old_parent.record_event({"round": 3})
    assert old_parent.snapshot() == before
    assert session.ledger.snapshot()["total"] == dict.fromkeys(KEYS, 0)
    assert len(caplog.records) >= 6
    child.close()
    before_child = child.snapshot()
    child.record_usage(usage())
    child.record_tool("read_file", 10)
    child.record_event({"round": 4})
    assert child.snapshot() == before_child


def test_concurrent_models_children_and_snapshots_never_nest_locks():
    held = threading.local()

    class CheckedLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            assert not getattr(held, "active", False)
            self.lock.acquire()
            held.active = True

        def __exit__(self, *exc):
            held.active = False
            self.lock.release()

    parent = RunLedger(MODEL)
    children = [RunLedger(OTHER, parent) for _ in range(4)]
    for ledger in [parent, *children]:
        ledger._lock = CheckedLock()
    start = threading.Barrier(9)

    def write(index):
        ledger = parent if index == 0 else children[index % len(children)]
        start.wait(timeout=10)
        for _ in range(60):
            ledger.record_usage(usage(), model=MODEL if index % 2 else OTHER)
            ledger.record_tool("read_file", 3)
            ledger.record_event({"round": index, "position": 1})
            snapshot = parent.snapshot()
            assert snapshot["total"]["input"] == sum(r["usage"]["input"] for r in snapshot["models"])

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(write, index) for index in range(9)]
        for future in futures:
            future.result(timeout=20)
    snapshot = parent.snapshot()
    assert snapshot["main"]["rounds"] == 60
    assert snapshot["subagents"]["rounds"] == 480
    assert snapshot["total"]["total"] == 540 * 120
    assert snapshot["tools"]["read_file"] == {"calls": 540, "result_chars": 1620}
    assert snapshot["subagent_tools"]["read_file"] == {"calls": 480, "result_chars": 1440}
    assert len(snapshot["cache_breaks"]) == 540
    assert sum(e["scope"] == "subagent" for e in snapshot["cache_breaks"]) == 480


def test_prices_are_only_from_snapshot_argument_and_ratio_is_optional():
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage())
    assert ledger.snapshot()["cost"] is ledger.snapshot()["equivalent_tokens"] is None
    pricing = {MODEL["id"]: price(cache_hit_equivalent_ratio=0.25)}
    before = copy.deepcopy(pricing)
    snapshot = ledger.snapshot(pricing)
    assert snapshot["cost"]["currency"] == "CNY"
    assert snapshot["cost"]["amount"] == pytest.approx(0.00026)
    assert snapshot["equivalent_tokens"] == 90
    assert pricing == before
    assert ledger.snapshot()["cost"] is None
    assert ledger.snapshot({MODEL["id"]: {"cache_hit_equivalent_ratio": 0.5}})["equivalent_tokens"] == 100
    assert ledger.snapshot({MODEL["id"]: {"cache_hit_equivalent_ratio": 0.5}})["cost"] is None


@pytest.mark.parametrize("key", ["input_miss_per_million", "input_hit_per_million", "output_per_million"])
@pytest.mark.parametrize("bad", BAD_PRICES)
def test_invalid_prices_never_produce_amounts(key, bad):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage())
    snapshot = ledger.snapshot({MODEL["id"]: price(**{key: bad}, cache_hit_equivalent_ratio=0.5)})
    assert snapshot["cost"] is None
    assert snapshot["equivalent_tokens"] == 100
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("bad", BAD_PRICES + [1.01])
def test_invalid_equivalent_ratios_are_unknown(bad):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage())
    snapshot = ledger.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=bad)})
    assert snapshot["cost"] is not None
    assert snapshot["equivalent_tokens"] is None


@pytest.mark.parametrize("ratio,expected", [(0, 80), (1, 120)])
def test_equivalent_ratio_endpoints(ratio, expected):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage())
    assert ledger.snapshot({MODEL["id"]: {"cache_hit_equivalent_ratio": ratio}})["equivalent_tokens"] == expected


@pytest.mark.parametrize("change", [{"input": None}, {"output": None}, {"cache_read": None}, {"cache_read": 101}])
def test_incomplete_or_inconsistent_usage_cannot_be_priced(change):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage(**change))
    snapshot = ledger.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=0.5)})
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None


@pytest.mark.parametrize("currency", [None, "", " ", 1, True, []])
def test_currency_is_required(currency):
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage())
    assert ledger.snapshot({MODEL["id"]: price(currency=currency)})["cost"] is None


def test_zero_rounds_with_only_zero_tokens_never_show_an_amount():
    ledger = RunLedger(MODEL)
    ledger.record_usage({"input": 0, "output": 0, "cache_read": 0}, rounds=0)
    snapshot = ledger.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=0.5)})
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None
    assert all(row["cost"] is row["equivalent_tokens"] is None for row in snapshot["models"])
    ledger.record_usage(usage(input=0, output=0, cache_read=0, cache_write=0, reasoning=0))
    assert ledger.snapshot({MODEL["id"]: price()})["cost"] == {"currency": "CNY", "amount": 0}


def test_zero_rates_are_valid_but_unused_models_do_not_poison_totals():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    child.record_usage(usage())
    pricing = {OTHER["id"]: price(input_miss_per_million=0, input_hit_per_million=0,
                                 output_per_million=0, cache_hit_equivalent_ratio=0)}
    snapshot = parent.snapshot(pricing)
    assert snapshot["cost"] == {"currency": "CNY", "amount": 0}
    assert snapshot["equivalent_tokens"] == 80
    parent.record_usage(usage())
    snapshot = parent.snapshot(pricing)
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None
    assert row_map(snapshot)[("subagent", OTHER["id"], "cli")]["cost"]["amount"] == 0


def test_multiple_models_mixed_currency_and_unknown_rows_keep_known_row_costs():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    parent.record_usage(usage())
    child.record_usage(usage())
    pricing = {MODEL["id"]: price(cache_hit_equivalent_ratio=0.5),
               OTHER["id"]: price(currency="USD", cache_hit_equivalent_ratio=0.25)}
    snapshot = parent.snapshot(pricing)
    assert snapshot["cost"] is None
    assert snapshot["equivalent_tokens"] == 190
    assert all(row["cost"] is not None for row in snapshot["models"])
    pricing[OTHER["id"]]["currency"] = "CNY"
    assert parent.snapshot(pricing)["cost"]["amount"] == pytest.approx(0.00052)
    child.record_usage({})
    snapshot = parent.snapshot(pricing)
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None
    assert row_map(snapshot)[("main", MODEL["id"], "api")]["cost"] is not None


def test_overflow_does_not_leak_nonfinite_cost_or_equivalent_tokens():
    ledger = RunLedger(MODEL)
    ledger.record_usage(usage(input=10 ** 400))
    snapshot = ledger.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=0.5)})
    assert snapshot["cost"] is snapshot["equivalent_tokens"] is None
    json.dumps(snapshot, allow_nan=False)


def saved_snapshot():
    parent = RunLedger(MODEL)
    child = RunLedger(OTHER, parent)
    parent.record_usage(usage(reasoning=None))
    child.record_usage({"input": 5}, rounds=None)
    child.record_tool("read_file", None)
    child.record_event({"round": 2, "position": None, "hash": "a" * 64})
    return parent.snapshot({MODEL["id"]: price(cache_hit_equivalent_ratio=0.5)})


def test_normalize_preserves_every_field_null_and_saved_amount_without_repricing():
    raw = saved_snapshot()
    raw["models"][0]["cost"]["amount"] = 123.456
    result = normalize(json.loads(json.dumps(raw, ensure_ascii=False, allow_nan=False)))
    assert result == raw
    assert result["subagents"]["rounds"] is None
    result["models"][0]["cost"]["amount"] = 0
    result["cache_breaks"][0]["round"] = 0
    result["tools"]["read_file"]["calls"] = 0
    assert raw["models"][0]["cost"]["amount"] == 123.456
    assert raw["cache_breaks"][0]["round"] == 2
    assert raw["tools"]["read_file"]["calls"] == 1


@pytest.mark.parametrize("raw", [None, {}, [], "legacy", {"version": 0}, {"version": 2}])
def test_normalize_old_or_invalid_records_are_none(raw):
    assert normalize(raw) is None


@pytest.mark.parametrize("path,bad", [
    (("version",), True), (("version",), 2), (("main", "input"), -1),
    (("total", "rounds"), True), (("subagents", "output"), "0"),
    (("main", "total"), 1.5), (("main", "cache_read"), float("nan")),
    (("cost",), {"currency": "CNY", "amount": float("inf")}),
    (("equivalent_tokens",), False), (("equivalent_tokens",), -1),
    (("models",), {}), (("models", 0, "scope"), "other"),
    (("models", 0, "model", "id"), 1), (("models", 0, "usage", "reasoning"), True),
    (("models", 0, "cost", "currency"), ""), (("models", 0, "cost", "amount"), "1"),
    (("models", 0, "equivalent_tokens"), float("nan")),
    (("tools", "read_file", "calls"), -1), (("tools", "read_file", "result_chars"), True),
    (("subagent_tools",), []), (("cache_breaks",), {}),
    (("cache_breaks", 0, "scope"), "other"), (("cache_breaks", 0, "round"), float("nan")),
    (("cache_breaks", 0, "content"), "private body"),
])
def test_normalize_rejects_invalid_nested_schema_without_forging_zero(path, bad):
    raw = saved_snapshot()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    assert normalize(raw) is None


def test_normalize_rejects_missing_or_extra_fixed_fields():
    raw = saved_snapshot()
    del raw["main"]["reasoning"]
    assert normalize(raw) is None
    raw = saved_snapshot()
    raw["body"] = "private body"
    assert normalize(raw) is None


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    from src import paths
    monkeypatch.setattr(paths, "LOG_DIR", str(tmp_path))
    return tmp_path / "run_ledger.jsonl"


def test_append_record_concurrent_jsonl_is_parseable_and_not_deduplicated(log_path):
    snapshot = saved_snapshot()
    records = [{"run_id": f"run-{index}", "session_id": "会话\n甲", "model": MODEL,
                "started_at": "2026-11-12T00:00:00Z", "outcome": "completed", "ledger": snapshot}
               for index in range(100)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(append_record, records))
    assert append_record(records[0])
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 101
    parsed = [json.loads(line) for line in lines]
    assert {r["run_id"] for r in parsed} == {r["run_id"] for r in records}
    assert all(r["ledger"] == snapshot for r in parsed)
    assert "模型甲" in text
    assert parsed[-1] == records[0]


def test_append_oserror_is_logged_and_returns_false(log_path, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise OSError("写盘失败")

    monkeypatch.setattr(run_ledger, "open", fail, raising=False)
    with caplog.at_level(logging.WARNING):
        assert append_record({"run_id": "safe"}) is False
    assert caplog.records
    assert not log_path.exists()


@pytest.mark.parametrize("record", [{"ledger": float("nan")}, {"ledger": float("inf")},
                                     {"ledger": object()}, None, []])
def test_append_invalid_json_cannot_leave_a_partial_line(log_path, record):
    assert append_record({"run_id": "before"})
    assert append_record(record) is False
    assert append_record({"run_id": "after"})
    assert [json.loads(line)["run_id"] for line in log_path.read_text(encoding="utf-8").splitlines()] == ["before", "after"]
