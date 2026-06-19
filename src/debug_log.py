"""F12 Debug Inspector 的数据层：内存 ring buffer 收集 LLM 调用记录。

- 每次 `_stream_with_tools` 调用产生一条 record（在 streaming.py 里 instrument）
- 内存最多保留 MAX_RECORDS=50 条，关 app 清空（不写盘）
- Qt Signal 通知 Inspector UI 刷新；UI 没打开时数据照样收集，打开就有
- 含大量 base64 的 multimodal 消息会先 sanitize（截到前 200 字），单条 record 不会膨胀到几 MB
"""
import json
import threading
import time
import uuid
from collections import deque
from typing import Any, Optional

# Qt 可选：headless 部署（服务器版，无 PySide6）只用数据层，Signal 通知降级为 no-op。
# 桌面端行为完全不变（有 PySide6 时走原 QObject + Signal 路径）。
try:
    from PySide6.QtCore import QObject as _QtBase, Signal as _QtSignal
    _HAS_QT = True
except ImportError:
    _QtBase = object
    _QtSignal = None
    _HAS_QT = False

from .limits import DEBUG_BASE64_PREVIEW_CHARS, DEBUG_MAX_RECORDS, DEBUG_TEXT_PREVIEW_CHARS

MAX_RECORDS = DEBUG_MAX_RECORDS
_BASE64_PREVIEW = DEBUG_BASE64_PREVIEW_CHARS  # 多模态 image base64 截断阈值
_TEXT_PREVIEW = DEBUG_TEXT_PREVIEW_CHARS      # 单条文本消息过长时截断


class _Recorder(_QtBase):
    """单例——有 Qt 时挂 Signal 给 Inspector UI，headless 时纯内存 ring buffer。"""
    if _HAS_QT:
        record_added = _QtSignal(dict)  # 新增一条 record 时 emit

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._records: deque = deque(maxlen=MAX_RECORDS)

    def add(self, record: dict) -> None:
        with self._lock:
            self._records.append(record)
        # signal 在 worker 线程 emit；接收槽默认 AutoConnection 会自动 queue 到 UI 线程
        if _HAS_QT:
            self.record_added.emit(record)

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


# 全局单例
recorder = _Recorder()


def _sanitize_content(content: Any) -> Any:
    """递归裁剪 base64 / 过长文本，保持 record 体积可控。"""
    if isinstance(content, str):
        if len(content) > _TEXT_PREVIEW:
            return content[:_TEXT_PREVIEW] + f"... [text cropped, total {len(content)} chars]"
        return content
    if isinstance(content, list):
        return [_sanitize_content(x) for x in content]
    if isinstance(content, dict):
        out = {}
        for k, v in content.items():
            if k == "data" and isinstance(v, str) and len(v) > _BASE64_PREVIEW:
                # Anthropic image base64
                out[k] = v[:_BASE64_PREVIEW] + f"... [base64 cropped, total {len(v)} chars]"
            elif k == "url" and isinstance(v, str) and v.startswith("data:image") and len(v) > _BASE64_PREVIEW:
                # OpenAI image_url
                out[k] = v[:_BASE64_PREVIEW] + f"... [data url cropped, total {len(v)} chars]"
            else:
                out[k] = _sanitize_content(v)
        return out
    return content


def _serialize_message(msg) -> dict:
    """LangChain Message → 可 JSON 化的 dict（裁剪过 base64）。"""
    cls = msg.__class__.__name__
    role_map = {
        "SystemMessage": "system",
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "AIMessageChunk": "assistant",
        "ToolMessage": "tool",
    }
    out = {
        "role": role_map.get(cls, cls.lower()),
        "content": _sanitize_content(getattr(msg, "content", "")),
    }
    if cls in ("AIMessage", "AIMessageChunk"):
        tcs = getattr(msg, "tool_calls", None)
        if tcs:
            out["tool_calls"] = [
                {"name": tc.get("name"), "args": _sanitize_content(tc.get("args", {}))}
                for tc in tcs if isinstance(tc, dict)
            ]
    if cls == "ToolMessage":
        out["tool_call_id"] = getattr(msg, "tool_call_id", "")
    return out


def make_record(
    model: str,
    provider: str,
    endpoint: str,
    messages: list,
    tools: list[str],
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """构造一条 record 的"请求"部分。stream 结束后用 finalize_record 填响应。"""
    return {
        "id": uuid.uuid4().hex[:8],
        "ts": time.time(),
        "model": model,
        "provider": provider,
        "endpoint": endpoint,
        "_start_ns": time.perf_counter_ns(),
        "request": {
            "system_prompt": _sanitize_content(system_prompt or ""),
            "messages": [_serialize_message(m) for m in messages],
            "tools": tools,
            "max_tokens": max_tokens,
        },
        "response": None,
        "error": None,
        "elapsed_ms": 0,
    }


def finalize_record(
    record: dict,
    text: str = "",
    tool_calls: Optional[list] = None,
    usage: Optional[dict] = None,
    error: Optional[str] = None,
    thinking: str = "",
) -> None:
    """填充响应数据并加入 ring buffer + emit signal。"""
    record["elapsed_ms"] = (time.perf_counter_ns() - record.pop("_start_ns", 0)) // 1_000_000
    if error is not None:
        record["error"] = error
    record["response"] = {
        "text": _sanitize_content(text or ""),
        "tool_calls": [
            {"name": tc.get("name") if isinstance(tc, dict) else str(tc),
             "args": _sanitize_content(tc.get("args", {}) if isinstance(tc, dict) else {})}
            for tc in (tool_calls or [])
        ],
        "usage": usage or {},
        "thinking": _sanitize_content(thinking or ""),
    }
    recorder.add(record)


def make_api_record(model: str, provider: str, endpoint: str, request_body) -> dict:
    """给非 LLM 的外部 API 调用（如视频生成）造一条 F12 record。
    request_body（dict/str）塞进 messages 显示，沿用 LLM record 的结构、F12 直接能展示。"""
    return {
        "id": uuid.uuid4().hex[:8],
        "ts": time.time(),
        "model": model,
        "provider": provider,
        "endpoint": endpoint,
        "_start_ns": time.perf_counter_ns(),
        "request": {
            "system_prompt": "",
            "messages": [{"role": "请求", "content": _sanitize_content(request_body)}],
            "tools": [],
            "max_tokens": None,
        },
        "response": None,
        "error": None,
        "elapsed_ms": 0,
    }


def finalize_api_record(record: dict, result_text: str = "", error: Optional[str] = None) -> None:
    """填非 LLM API record 的响应/错误并入 ring buffer（F12 刷新）。"""
    record["elapsed_ms"] = (time.perf_counter_ns() - record.pop("_start_ns", 0)) // 1_000_000
    if error is not None:
        record["error"] = error
    record["response"] = {
        "text": _sanitize_content(result_text or ""),
        "tool_calls": [],
        "usage": {},
        "thinking": "",
    }
    recorder.add(record)


def record_summary(record: dict) -> str:
    """一行摘要，给 UI 列表用。"""
    import datetime as _dt
    ts = _dt.datetime.fromtimestamp(record["ts"]).strftime("%H:%M:%S")
    model = record.get("model", "?")
    elapsed = record.get("elapsed_ms", 0)
    if record.get("error"):
        return f"⚠ {ts}  {model}  失败"
    usage = (record.get("response") or {}).get("usage", {})
    total = usage.get("total", 0)
    return f"{ts}  {model}  {elapsed/1000:.1f}s · {total}tok"


def record_to_json(record: dict, indent: int = 2) -> str:
    """完整 record 的 JSON 字符串，给 Raw JSON 视图和复制按钮用。"""
    safe = {k: v for k, v in record.items() if not k.startswith("_")}
    return json.dumps(safe, ensure_ascii=False, indent=indent, default=str)
