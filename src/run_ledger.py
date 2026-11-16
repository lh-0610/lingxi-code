import copy
import json
import logging
import math
import os
import threading


logger = logging.getLogger("agent")
_COUNT_KEYS = ("rounds", "input", "output", "total", "cache_read", "cache_write", "reasoning")
_MODEL_KEYS = ("id", "name", "type")
_TOOL_KEYS = ("calls", "result_chars")
_ROW_KEYS = ("model", "scope", "usage", "cost", "equivalent_tokens")
_SNAPSHOT_KEYS = ("version", "main", "subagents", "total", "tools", "subagent_tools",
                  "models", "cache_breaks", "cost", "equivalent_tokens")
_RATE_KEYS = ("input_miss_per_million", "input_hit_per_million", "output_per_million")
_EVENT_NAMES = frozenset(("model_id", "model_name", "model_type", "tool", "tool_name"))
_EVENT_TAGS = {
    "reason": frozenset(("工具列表变了", "system 变了", "工具结果回收", "历史压缩", "未知",
                         "命中率骤降（未检测到本地前缀变化）")),
    "source": frozenset(("发送前", "服务端侧")),
}
_EVENT_PRIVATE = frozenset(("args", "arguments", "params", "parameters", "prompt",
                            "messages", "message", "content", "text", "body", "result"))
_APPEND_LOCK = threading.Lock()


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _number(value):
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def _add(left, right):
    return None if left is None or right is None else left + right


def _empty_counts():
    return dict.fromkeys(_COUNT_KEYS, 0)


def _merge(target, delta):
    for key, value in delta.items():
        target[key] = _add(target[key], value)


def _model(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] if isinstance(raw.get(key), str) else "" for key in _MODEL_KEYS}


def _has_keys(raw, keys):
    return isinstance(raw, dict) and raw.keys() == set(keys)


def _valid_model(raw):
    return _has_keys(raw, _MODEL_KEYS) and all(isinstance(value, str) for value in raw.values())


def _valid_event_field(key, value):
    if not isinstance(key, str) or key in _EVENT_PRIVATE or key == "scope":
        return False
    if key == "model":
        return _valid_model(value)
    if key in _EVENT_NAMES:
        return value is None or isinstance(value, str)
    if key in _EVENT_TAGS:
        return value is None or isinstance(value, str) and value in _EVENT_TAGS[key]
    if key == "hash" or key.endswith("_hash"):
        return value is None or (
            isinstance(value, str) and bool(value)
            and all(char in "0123456789abcdefABCDEF" for char in value)
        )
    return value is None or _number(value)


def _event(raw):
    if not isinstance(raw, dict):
        return None
    result = {}
    for key, value in raw.items():
        if key == "model" and isinstance(value, dict):
            value = _model(value)
        if _valid_event_field(key, value):
            result[key] = copy.deepcopy(value)
    return result or None


def _active(usage):
    return usage["rounds"] != 0 or any(value is not None and value > 0 for value in usage.values())


def _currency(value):
    return isinstance(value, str) and bool(value.strip())


def _estimate(usage, rates):
    if not _active(usage) or not isinstance(rates, dict):
        return None, None
    input_tokens, output_tokens, cached = (usage[key] for key in ("input", "output", "cache_read"))
    if any(value is None for value in (input_tokens, output_tokens, cached)) or cached > input_tokens:
        return None, None
    missed = input_tokens - cached
    equivalent = None
    ratio = rates.get("cache_hit_equivalent_ratio")
    if _number(ratio) and ratio <= 1:
        try:
            equivalent = missed + cached * ratio + output_tokens
            if not _number(equivalent):
                equivalent = None
        except OverflowError:
            equivalent = None
    cost = None
    currency = rates.get("currency")
    values = [rates.get(key) for key in _RATE_KEYS]
    if _currency(currency) and all(_number(value) for value in values):
        miss_rate, hit_rate, output_rate = values
        try:
            amount = (missed * miss_rate + cached * hit_rate + output_tokens * output_rate) / 1e6
            if _number(amount):
                cost = {"currency": currency, "amount": amount}
        except OverflowError:
            pass
    return cost, equivalent


def _sum_numbers(values):
    if not values or any(value is None for value in values):
        return None
    try:
        total = sum(values)
        return total if _number(total) else None
    except OverflowError:
        return None


def _sum_costs(rows):
    if not rows or any(row["cost"] is None for row in rows):
        return None
    currency = rows[0]["cost"]["currency"]
    if any(row["cost"]["currency"] != currency for row in rows):
        return None
    amount = _sum_numbers([row["cost"]["amount"] for row in rows])
    return {"currency": currency, "amount": amount} if amount is not None else None


class RunLedger:
    def __init__(self, model: dict, parent=None):
        self._model = _model(model)
        self._parent = parent
        self._lock = threading.Lock()
        self._closed = False
        key = ("main", self._model["id"], self._model["type"])
        self._models = {key: {"model": self._model, "scope": "main", "usage": _empty_counts()}}
        self._tools = {}
        self._subagent_tools = {}
        self._events = []

    def record_usage(self, usage, *, model=None, rounds=1):
        raw = usage if isinstance(usage, dict) else {}
        counts = {key: _count(raw.get(key)) for key in _COUNT_KEYS}
        counts["rounds"] = _count(rounds)
        # 分项齐全时重算总量，否则仅保留有效上报总量，思考量不重复相加。
        if counts["input"] is not None and counts["output"] is not None:
            counts["total"] = counts["input"] + counts["output"]
        descriptor = self._model if model is None else _model(model)
        self._record("usage", (descriptor, counts))

    def record_tool(self, name, result_chars, *, calls=1):
        if not isinstance(name, str) or not name:
            logger.warning("运行账本忽略无效工具名")
            return
        self._record("tool", (name, {"calls": _count(calls), "result_chars": _count(result_chars)}))

    def record_event(self, event):
        event = _event(event)
        if event is not None:
            self._record("event", event)

    def _record(self, kind, data):
        ledger, scope = self, "main"
        while ledger is not None:
            with ledger._lock:
                closed = ledger._closed
                if not closed:
                    ledger._apply(kind, data, scope)
                parent = ledger._parent
            if closed:
                logger.warning("运行账本已关闭，忽略迟到的%s记录", kind)
                return
            # 释放当前账本锁后才进入创建时捕获的父账本，不在收尾重复合并。
            ledger, scope = parent, "subagent"

    def _apply(self, kind, data, scope):
        if kind == "usage":
            descriptor, counts = data
            key = (scope, descriptor["id"], descriptor["type"])
            if key not in self._models:
                self._models[key] = {"model": dict(descriptor), "scope": scope, "usage": _empty_counts()}
            _merge(self._models[key]["usage"], counts)
        elif kind == "tool":
            name, counts = data
            for tools in (self._tools, self._subagent_tools) if scope == "subagent" else (self._tools,):
                if name not in tools:
                    tools[name] = dict.fromkeys(_TOOL_KEYS, 0)
                _merge(tools[name], counts)
        else:
            self._events.append(dict(copy.deepcopy(data), scope=scope))

    def snapshot(self, pricing=None):
        with self._lock:
            models, tools, subagent_tools, events = copy.deepcopy(
                (list(self._models.values()), self._tools, self._subagent_tools, self._events)
            )
        pricing = pricing if isinstance(pricing, dict) else {}
        main, subagents = _empty_counts(), _empty_counts()
        for row in models:
            _merge(main if row["scope"] == "main" else subagents, row["usage"])
            row["cost"], row["equivalent_tokens"] = _estimate(row["usage"], pricing.get(row["model"]["id"]))
        total = dict(main)
        _merge(total, subagents)
        active = [row for row in models if _active(row["usage"])]
        return {"version": 1, "main": main, "subagents": subagents, "total": total,
                "tools": tools, "subagent_tools": subagent_tools, "models": models,
                "cache_breaks": events, "cost": _sum_costs(active),
                "equivalent_tokens": _sum_numbers([row["equivalent_tokens"] for row in active])}

    def close(self):
        with self._lock:
            self._closed = True


def record_cache_event(sess, event) -> None:
    """缓存断裂进日志和运行账本，**不弹给用户**：它是计费诊断，不是要用户处理的错误。"""
    if not isinstance(event, dict):
        return
    ledger = getattr(sess, "run_ledger", None)
    if ledger is not None:
        ledger.record_event(event)
    if event.get("source") == "服务端侧":
        logger.info("前缀缓存疑似服务端失效：第 %s 轮命中率 %.1f%% → %.1f%%（本地前缀未变）",
                    event.get("round"), 100 * event.get("previous_hit_rate", 0),
                    100 * event.get("hit_rate", 0))
        return
    index = event.get("message_index")
    where = f"从第 {index} 条消息起" if index else "工具列表"
    logger.info("前缀缓存断裂：第 %s 轮%s与上一轮不同（%s）",
                event.get("round"), where, event.get("reason"))


def _valid_counts(raw, keys=_COUNT_KEYS):
    return _has_keys(raw, keys) and all(value is None or _count(value) is not None for value in raw.values())


def _valid_cost(raw):
    return raw is None or (
        _has_keys(raw, ("currency", "amount"))
        and _currency(raw["currency"]) and _number(raw["amount"])
    )


def _valid_equivalent(raw):
    return raw is None or _number(raw)


def _valid_tools(raw):
    return isinstance(raw, dict) and all(
        isinstance(name, str) and bool(name) and _valid_counts(counts, _TOOL_KEYS)
        for name, counts in raw.items()
    )


def _valid_event(raw):
    return isinstance(raw, dict) and raw.get("scope") in ("main", "subagent") and all(
        key == "scope" or _valid_event_field(key, value) for key, value in raw.items()
    )


def normalize(raw) -> dict | None:
    if not _has_keys(raw, _SNAPSHOT_KEYS) or type(raw["version"]) is not int or raw["version"] != 1:
        return None
    if not all(_valid_counts(raw[key]) for key in ("main", "subagents", "total")):
        return None
    if not all(_valid_tools(raw[key]) for key in ("tools", "subagent_tools")):
        return None
    if not _valid_cost(raw["cost"]) or not _valid_equivalent(raw["equivalent_tokens"]):
        return None
    if not isinstance(raw["models"], list) or not isinstance(raw["cache_breaks"], list):
        return None
    for row in raw["models"]:
        if (not _has_keys(row, _ROW_KEYS) or not _valid_model(row["model"])
                or row["scope"] not in ("main", "subagent") or not _valid_counts(row["usage"])
                or not _valid_cost(row["cost"]) or not _valid_equivalent(row["equivalent_tokens"])):
            return None
    if not all(_valid_event(event) for event in raw["cache_breaks"]):
        return None
    # 只验证完整结构，不补零、不读取配置重算历史金额。
    return copy.deepcopy(raw)


def append_record(record) -> bool:
    if not isinstance(record, dict):
        logger.warning("运行账本追加失败：记录不是字典")
        return False
    try:
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError, OverflowError):
        logger.warning("运行账本追加失败：记录无法编码为有效JSON")
        return False
    try:
        from . import paths

        with _APPEND_LOCK:
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            with open(os.path.join(paths.LOG_DIR, "run_ledger.jsonl"), "a", encoding="utf-8") as stream:
                stream.write(line)
    except OSError as error:
        logger.warning("运行账本追加失败：%s", error)
        return False
    return True
