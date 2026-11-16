from collections.abc import Iterable, Mapping
from fractions import Fraction
import hashlib
import json
import math


def _field(message, name, default=None):
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _message_type(message):
    kind = _field(message, "type")
    if not kind or kind == "chat":
        kind = _field(message, "role", kind)
    return {"human": "user", "ai": "assistant"}.get(kind, kind)


def _hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_history(messages: Iterable, tool_names: Iterable[str]) -> dict:
    """只保留哈希；messages 包含 system，system 是其中系统消息的哈希列表。"""
    message_hashes = []
    system_hashes = []
    volatile_tail = False
    for message in messages:
        kind = _message_type(message)
        additional = _field(message, "additional_kwargs") or {}
        volatile_tail = additional.get("lingxi_kind") == "volatile"
        payload = {
            "type": kind,
            "content": _field(message, "content", ""),
            "name": _field(message, "name"),
            "tool_call_id": _field(message, "tool_call_id"),
            "tool_calls": _field(message, "tool_calls") or [],
            "additional_kwargs": {
                key: value for key, value in additional.items() if not key.startswith("lingxi_")
            },
        }
        digest = _hash(payload)
        message_hashes.append(digest)
        if kind == "system":
            system_hashes.append(digest)
    return {
        "system": system_hashes,
        "messages": message_hashes,
        "tools": _hash(list(tool_names)),
        "volatile_tail": volatile_tail,
    }


def compare_prefix(
    previous: dict | None, current: dict, *, round_index: int,
    evicted: bool = False, compacted: bool = False,
) -> dict | None:
    """只比较上一轮稳定前缀，调用方每轮用 current 更新基线。"""
    if previous is None:
        return None
    if previous["tools"] != current["tools"]:
        return {
            "round": round_index, "message_index": 0,
            "reason": "工具列表变了", "source": "发送前",
        }
    stable = previous["messages"]
    if previous["volatile_tail"]:
        stable = stable[:-1]
    for index, old_hash in enumerate(stable):
        new_hash = current["messages"][index] if index < len(current["messages"]) else None
        if old_hash == new_hash:
            continue
        if old_hash in previous["system"] or new_hash in current["system"]:
            reason = "system 变了"
        elif evicted:
            reason = "工具结果回收"
        elif compacted:
            reason = "历史压缩"
        else:
            reason = "未知"
        return {
            "round": round_index, "message_index": index + 1,
            "reason": reason, "source": "发送前",
        }
    return None


def _hit_rate(usage) -> Fraction | None:
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input")
    cache_read = usage.get("cache_read")
    for value in (input_tokens, cache_read):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
    if input_tokens <= 0 or not 0 <= cache_read <= input_tokens:
        return None
    return Fraction(cache_read) / Fraction(input_tokens)


def cache_drop(
    previous_usage, current_usage, *, round_index: int, prefix_changed: bool = False,
) -> dict | None:
    """命中率使用 0～1 比例；事件仅提示可能的服务端原因，并非确证。"""
    if prefix_changed:
        return None
    previous_rate = _hit_rate(previous_usage)
    current_rate = _hit_rate(current_usage)
    if previous_rate is None or current_rate is None:
        return None
    if previous_rate < Fraction(1, 2) or previous_rate - current_rate < Fraction(3, 10):
        return None
    return {
        "round": round_index,
        "source": "服务端侧",
        "reason": "命中率骤降（未检测到本地前缀变化）",
        "previous_hit_rate": float(previous_rate),
        "hit_rate": float(current_rate),
    }
