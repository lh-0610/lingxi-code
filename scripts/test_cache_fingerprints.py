import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.cache_fingerprints import cache_drop, compare_prefix, fingerprint_history


def history():
    return [
        {"role": "system", "content": "稳定 系统\n"},
        {"role": "user", "content": "请检查"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}
            ],
        },
        {"role": "tool", "content": "原始工具结果", "tool_call_id": "call-1"},
        {
            "role": "user",
            "content": "本轮台账",
            "additional_kwargs": {"lingxi_kind": "volatile", "lingxi_internal": True},
        },
    ]


TOOLS = ["read_file", "write_file"]


class FingerprintTests(unittest.TestCase):
    def test_snapshot_contains_only_hashes_and_tail_flag(self):
        snapshot = fingerprint_history(history(), TOOLS)
        self.assertEqual(set(snapshot), {"system", "messages", "tools", "volatile_tail"})
        self.assertEqual(snapshot["system"], snapshot["messages"][:1])
        self.assertEqual(len(snapshot["messages"]), 5)
        self.assertIs(snapshot["volatile_tail"], True)
        for digest in snapshot["messages"] + snapshot["system"] + [snapshot["tools"]]:
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        serialized = json.dumps(snapshot, ensure_ascii=False)
        for text in ("稳定", "台账", "read_file", "call-1", "a.py"):
            self.assertNotIn(text, serialized)
        self.assertEqual(json.loads(serialized), snapshot)

    def test_stable_json_sha256(self):
        payload = {
            "type": "user", "content": " 原文\n", "name": None,
            "tool_call_id": None, "tool_calls": [], "additional_kwargs": {},
        }
        digest = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(
            fingerprint_history([{"role": "user", "content": " 原文\n"}], [])["messages"],
            [digest],
        )

    def test_objects_and_offline_role_or_type_are_equivalent(self):
        messages = history()
        typed = copy.deepcopy(messages)
        objects = []
        for message in typed:
            role = message.pop("role")
            message["type"] = {"user": "human", "assistant": "ai"}.get(role, role)
            objects.append(SimpleNamespace(**message))
        expected = fingerprint_history(messages, TOOLS)
        self.assertEqual(fingerprint_history(typed, TOOLS), expected)
        self.assertEqual(fingerprint_history(objects, TOOLS), expected)

    def test_real_langchain_messages_when_available(self):
        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
        except ModuleNotFoundError:
            self.skipTest("当前环境未安装 LangChain")
        objects = [
            SystemMessage(content="系统"),
            HumanMessage(content="问题"),
            AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "a.py"}, "id": "call-1"}
            ]),
            ToolMessage(content="结果", tool_call_id="call-1"),
            HumanMessage(content="台账", additional_kwargs={"lingxi_kind": "volatile"}),
        ]
        offline = [message.model_dump() for message in objects]
        self.assertEqual(fingerprint_history(objects, TOOLS), fingerprint_history(offline, TOOLS))

    def test_chat_message_uses_explicit_role(self):
        obj = SimpleNamespace(type="chat", role="critic", content="建议")
        self.assertEqual(
            fingerprint_history([obj], []),
            fingerprint_history([{"role": "critic", "content": "建议"}], []),
        )

    def test_json_key_order_is_stable_but_content_whitespace_is_not(self):
        before = history()
        after = copy.deepcopy(before)
        before[2]["tool_calls"][0]["args"] = {"path": "a.py", "offset": 1}
        after[2]["tool_calls"][0]["args"] = {"offset": 1, "path": "a.py"}
        self.assertEqual(fingerprint_history(before, TOOLS), fingerprint_history(after, TOOLS))
        after[1]["content"] += " "
        self.assertNotEqual(fingerprint_history(before, TOOLS), fingerprint_history(after, TOOLS))

    def test_message_ids_usage_and_internal_metadata_are_ignored(self):
        before = history()
        after = copy.deepcopy(before)
        for message in after:
            message.update(id="随机消息-id", usage_metadata={"input_tokens": 123},
                           response_metadata={"request_id": "随机请求-id"})
            message.setdefault("additional_kwargs", {}).update(
                lingxi_trace="随机值", lingxi_internal=False,
            )
        self.assertEqual(fingerprint_history(before, TOOLS), fingerprint_history(after, TOOLS))

    def test_internal_metadata_does_not_change_message_hash(self):
        plain = {"role": "user", "content": "正文"}
        marked = dict(plain, additional_kwargs={"lingxi_kind": "volatile"})
        before = fingerprint_history([plain], [])
        after = fingerprint_history([marked], [])
        self.assertEqual(before["messages"], after["messages"])
        self.assertIs(before["volatile_tail"], False)
        self.assertIs(after["volatile_tail"], True)

    def test_tool_arguments_and_call_ids_are_semantic(self):
        for key, value in (
            ("args", {"path": "b.py"}), ("id", "call-2"), ("name", "write_file"),
            ("args", {"path": "a.py", "id": "参数-id", "lingxi_kind": "参数"}),
        ):
            with self.subTest(key=key, value=value):
                before = history()
                after = copy.deepcopy(before)
                after[2]["tool_calls"][0][key] = value
                self.assertNotEqual(
                    fingerprint_history(before, TOOLS)["messages"][2],
                    fingerprint_history(after, TOOLS)["messages"][2],
                )

    def test_other_sending_fields_are_semantic(self):
        for key, value in (
            ("name", "工具名"), ("tool_call_id", "call-2"), ("role", "user"),
            ("additional_kwargs", {"reasoning_content": "思考内容"}),
            ("additional_kwargs", {"tool_calls": [{"id": "raw-call-2"}]}),
        ):
            with self.subTest(key=key):
                before = history()
                after = copy.deepcopy(before)
                after[3][key] = value
                self.assertNotEqual(
                    fingerprint_history(before, TOOLS)["messages"][3],
                    fingerprint_history(after, TOOLS)["messages"][3],
                )

    def test_content_blocks_and_tool_calls_keep_array_order(self):
        for key, value in (
            ("content", [{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]),
            ("tool_calls", [{"id": "call-1"}, {"id": "call-2"}]),
        ):
            with self.subTest(key=key):
                before = {"role": "assistant", key: value}
                after = {"role": "assistant", key: value[::-1]}
                self.assertNotEqual(fingerprint_history([before], []),
                                    fingerprint_history([after], []))

    def test_empty_history_and_generators(self):
        empty = fingerprint_history([], [])
        self.assertEqual(empty["system"], [])
        self.assertEqual(empty["messages"], [])
        self.assertIs(empty["volatile_tail"], False)
        self.assertEqual(fingerprint_history(iter(history()), iter(TOOLS)),
                         fingerprint_history(history(), TOOLS))

    def test_inputs_are_not_modified(self):
        messages = history()
        original = copy.deepcopy(messages)
        tools = TOOLS.copy()
        fingerprint_history(messages, tools)
        self.assertEqual(messages, original)
        self.assertEqual(tools, TOOLS)

    def test_module_imports_without_site_packages_or_qt(self):
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
            "from src.cache_fingerprints import fingerprint_history; "
            "assert fingerprint_history([], [])['messages'] == []; "
            "assert not any(k.startswith(('PySide', 'PyQt', 'langchain')) for k in sys.modules)"
        )
        result = subprocess.run([sys.executable, "-B", "-I", "-S", "-c", code],
                                capture_output=True, text=True, timeout=15,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)


class PrefixTests(unittest.TestCase):
    def check_change(self, before, after, index, reason, **flags):
        previous = fingerprint_history(before, TOOLS)
        current = fingerprint_history(after, TOOLS)
        originals = copy.deepcopy((previous, current))
        self.assertEqual(compare_prefix(previous, current, round_index=7, **flags), {
            "round": 7, "message_index": index, "reason": reason, "source": "发送前",
        })
        self.assertEqual((previous, current), originals)

    def test_first_snapshot_and_identical_history_do_not_report(self):
        snapshot = fingerprint_history(history(), TOOLS)
        self.assertIsNone(compare_prefix(None, snapshot, round_index=1))
        self.assertIsNone(compare_prefix(snapshot, snapshot, round_index=2))

    def test_system_change(self):
        after = history()
        after[0]["content"] += "新规则"
        self.check_change(history(), after, 1, "system 变了", evicted=True, compacted=True)

    def test_only_volatile_tail_changes(self):
        after = history()
        after[-1]["content"] = "台账已更新"
        self.assertIsNone(compare_prefix(fingerprint_history(history(), TOOLS),
                                        fingerprint_history(after, TOOLS), round_index=2))

    def test_tool_eviction(self):
        after = history()
        after[3]["content"] = "已回收工具结果"
        self.check_change(history(), after, 4, "工具结果回收", evicted=True, compacted=True)

    def test_history_compaction(self):
        after = [history()[0], {"role": "user", "content": "历史摘要"}, history()[-1]]
        self.check_change(history(), after, 2, "历史压缩", compacted=True)

    def test_appending_messages_with_or_without_volatile_tail_is_normal(self):
        for volatile in (False, True):
            with self.subTest(volatile=volatile):
                before = history() if volatile else history()[:-1]
                after = history()[:-1] + [{"role": "assistant", "content": "新回复"}]
                if volatile:
                    after.append(dict(history()[-1], content="新台账"))
                self.assertIsNone(compare_prefix(fingerprint_history(before, TOOLS),
                                                fingerprint_history(after, TOOLS), round_index=2))

    def test_ordinary_tail_user_change_is_not_ignored(self):
        for extra in ({}, {"lingxi_internal": True}, {"lingxi_kind": "other"}):
            with self.subTest(extra=extra):
                before = history()[:-1] + [{
                    "role": "user", "content": "<system-reminder>普通用户原文</system-reminder>",
                    "additional_kwargs": extra,
                }]
                after = copy.deepcopy(before)
                after[-1]["content"] = "普通用户改文"
                self.check_change(before, after, 5, "未知")

    def test_current_marker_cannot_hide_previous_stable_tail(self):
        before = history()
        before[-1].pop("additional_kwargs")
        after = history()
        after[-1]["content"] = "新台账"
        self.check_change(before, after, 5, "未知")

    def test_only_previous_last_volatile_message_is_excluded(self):
        before = history() + [dict(history()[-1], content="另一条台账")]
        after = copy.deepcopy(before)
        after[-2]["content"] = "前一条台账改动"
        self.check_change(before, after, 5, "未知")

    def test_removing_volatile_tail_is_normal(self):
        self.assertIsNone(compare_prefix(fingerprint_history(history(), TOOLS),
                                        fingerprint_history(history()[:-1], TOOLS), round_index=2))

    def test_deletion_reports_first_missing_or_shifted_message(self):
        before = history()[:-1]
        for index in (0, 1, 3):
            with self.subTest(index=index):
                after = copy.deepcopy(before)
                del after[index]
                self.check_change(before, after, index + 1, "system 变了" if index == 0 else "未知")

    def test_first_difference_wins(self):
        after = history()
        after[1]["content"] = "新问题"
        after[3]["content"] = "新结果"
        self.check_change(history(), after, 2, "未知")

    def test_tool_order_changes_before_messages(self):
        after = history()
        after[0]["content"] = "新系统"
        previous = fingerprint_history(history(), TOOLS)
        for tools in (TOOLS[::-1], TOOLS[:1], TOOLS + [TOOLS[0]]):
            with self.subTest(tools=tools):
                self.assertEqual(compare_prefix(previous, fingerprint_history(after, tools),
                                                round_index=3), {
                    "round": 3, "message_index": 0, "reason": "工具列表变了", "source": "发送前",
                })

    def test_stable_repeated_eviction_does_not_report_again(self):
        folded = history()
        folded[3]["content"] = "稳定回收存根"
        self.check_change(history(), folded, 4, "工具结果回收", evicted=True)
        next_history = copy.deepcopy(folded)
        next_history.insert(-1, {"role": "assistant", "content": "继续"})
        self.assertIsNone(compare_prefix(fingerprint_history(folded, TOOLS),
                                        fingerprint_history(next_history, TOOLS),
                                        round_index=8, evicted=True, compacted=True))

    def test_nonleading_system_and_appended_system(self):
        before = history()[:-1] + [{"role": "system", "content": "补充规则"}]
        after = copy.deepcopy(before)
        after[-1]["content"] = "新规则"
        self.check_change(before, after, 5, "system 变了")
        self.assertIsNone(compare_prefix(fingerprint_history(history()[:-1], TOOLS),
                                        fingerprint_history(before, TOOLS), round_index=2))


class CacheDropTests(unittest.TestCase):
    def test_large_drop_is_only_a_possible_server_side_signal(self):
        previous = {"input": 100, "cache_read": 80}
        current = {"input": 200, "cache_read": 80}
        self.assertEqual(cache_drop(previous, current, round_index=9), {
            "round": 9, "source": "服务端侧",
            "reason": "命中率骤降（未检测到本地前缀变化）",
            "previous_hit_rate": 0.8, "hit_rate": 0.4,
        })
        self.assertEqual(previous, {"input": 100, "cache_read": 80})
        self.assertEqual(current, {"input": 200, "cache_read": 80})

    def test_thresholds_include_exactly_fifty_percent_and_thirty_points(self):
        for previous, current in ((50, 20), (70, 40), (100, 70)):
            with self.subTest(previous=previous, current=current):
                self.assertIsNotNone(cache_drop({"input": 100, "cache_read": previous},
                                               {"input": 100, "cache_read": current}, round_index=2))

    def test_normal_usage_does_not_report(self):
        for previous, current in ((49, 0), (80, 51), (80, 80), (50, 90), (0, 0)):
            with self.subTest(previous=previous, current=current):
                self.assertIsNone(cache_drop({"input": 100, "cache_read": previous},
                                            {"input": 100, "cache_read": current}, round_index=2))

    def test_unknown_invalid_and_missing_input_do_not_report(self):
        unknowns = [None, {}, {"input": 100}, {"cache_read": 80}]
        for field in ("input", "cache_read"):
            for value in (None, "unknown", "100", True, float("nan"), float("inf"), -1):
                unknowns.append({"input": 100, "cache_read": 50, field: value})
        unknowns.extend([{"input": 0, "cache_read": 0}, {"input": 10, "cache_read": 20}])
        for unknown in unknowns:
            with self.subTest(unknown=unknown):
                self.assertIsNone(cache_drop(unknown, {"input": 100, "cache_read": 0}, round_index=2))
                self.assertIsNone(cache_drop({"input": 100, "cache_read": 100}, unknown, round_index=2))

    def test_local_prefix_change_suppresses_server_side_signal(self):
        self.assertIsNone(cache_drop({"input": 100, "cache_read": 90},
                                    {"input": 100, "cache_read": 0},
                                    round_index=2, prefix_changed=True))

    def test_float_counts_are_supported(self):
        event = cache_drop({"input": 100.0, "cache_read": 70.0},
                           {"input": 100.0, "cache_read": 40.0}, round_index=2)
        self.assertIsNotNone(event)
        self.assertEqual(event["hit_rate"], 0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
