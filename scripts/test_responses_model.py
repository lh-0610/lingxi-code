"""Responses API 模型类型（OpenAI Responses 协议，DeepSeek V4-Flash 等）的接入测试。

覆盖 models.py 的 responses 分支：
- builtin 列表里注册了 responses 类型
- _create_llm 对 responses 返回带 output_version="responses/v1" 的 ChatOpenAI
- 思考开关联动：关闭时注入 reasoning.effort=none，开启时不注入
- custom_models 的 protocol="responses" 分支
- get_model_config_issues 对 responses 校验 key
- _wrap_system_for_cache 对 responses 走纯字符串 system（非 Anthropic 缓存块）
"""
from src import models
from src.models import MODEL_LIST, _create_llm_uncached as _create_llm, get_model_config_issues
from src import config as _cfg


# ── builtin 注册 ──

def test_responses_type_registered_in_builtin_list(monkeypatch):
    """responses_models 里的模型应作为 ('型号','responses','型号',True) 注册进 MODEL_LIST。"""
    monkeypatch.setattr(_cfg, "RESPONSES_MODELS", ["deepseek-v4-flash", "deepseek-v4-pro"])
    # 重算 builtin 列表（_build_builtin_model_list 从 config 常量读取）
    from src.models import _build_builtin_model_list
    bl = _build_builtin_model_list()
    entries = [e for e in bl if e[1] == "responses"]
    assert entries, "responses 类型未注册进 builtin 列表"
    assert ("deepseek-v4-flash", "responses", "deepseek-v4-flash", True) in entries


def _find_responses_index():
    """找到第一个 responses 类型在 MODEL_LIST 里的 index；找不到返回 -1。"""
    for i, (_, mtype, _, _) in enumerate(MODEL_LIST):
        if mtype == "responses":
            return i
    return -1


def test_create_llm_responses_output_version(monkeypatch):
    """responses 分支应创建 ChatOpenAI 且 output_version='responses/v1'。"""
    idx = _find_responses_index()
    assert idx >= 0, "需要 config 里有 responses_models 条目（默认 deepseek-v4-flash）"
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    monkeypatch.setattr(models, "RESPONSES_BASE_URL", "https://api.deepseek.com")
    llm = _create_llm(model_index=idx, reasoning=True)
    # ChatOpenAI 的 output_version 可直接读（model_dump/dict 不暴露它）
    assert llm.__class__.__name__ == "ChatOpenAI"
    assert llm.output_version == "responses/v1", f"期望 responses/v1，实际 {llm.output_version!r}"
    assert llm.model_name  # 有模型名


def test_create_llm_responses_thinking_off_injects_reasoning(monkeypatch):
    """responses 关闭思考时应在 extra_body 里带 reasoning.effort=none（DeepSeek 默认思考）。"""
    idx = _find_responses_index()
    assert idx >= 0
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    monkeypatch.setattr(models, "RESPONSES_BASE_URL", "https://api.deepseek.com")
    llm = _create_llm(model_index=idx, reasoning=False)
    extra = llm.extra_body or {}
    assert extra.get("reasoning", {}).get("effort") == "none", f"关闭思考应带 effort=none，实际 {extra}"


def test_create_llm_responses_thinking_on_no_reasoning_injection(monkeypatch):
    """responses 开启思考时不应注入 reasoning.effort（让服务端默认思考）。"""
    idx = _find_responses_index()
    assert idx >= 0
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    monkeypatch.setattr(models, "RESPONSES_BASE_URL", "https://api.deepseek.com")
    llm = _create_llm(model_index=idx, reasoning=True)
    extra = llm.extra_body or {}
    assert "reasoning" not in extra, f"开启思考不应注入 reasoning，实际 {extra}"


def test_create_llm_custom_responses_protocol(monkeypatch):
    """custom_models 里 protocol='responses' 的条目应走 output_version='responses/v1'。"""
    # 造一个指向 custom 条的 MODEL_LIST 元组（type='custom'），并 patch 反查函数返回 responses 配置
    monkeypatch.setattr(
        models, "MODEL_LIST",
        [
            ("Claude Code", "claude-code", "claude", False),
            ("My GPT", "custom", "gpt-5", True),  # supports_think=True
        ],
    )
    monkeypatch.setattr(
        models, "_lookup_custom_model",
        lambda model_id: {
            "name": "My GPT",
            "model_id": model_id,
            "protocol": "responses",
            "api_key": "sk-test",
            "base_url": "https://my-gateway.example.com/v1",
            "supports_think": True,
        },
    )
    import src.state as _st
    monkeypatch.setattr(_st, "current_model_index", 1)
    llm = _create_llm(model_index=1, reasoning=False)
    assert llm.__class__.__name__ == "ChatOpenAI"
    assert llm.output_version == "responses/v1", f"期望 responses/v1，实际 {llm.output_version!r}"
    assert llm.model_name == "gpt-5"
    assert "my-gateway.example.com" in (llm.openai_api_base or "")
    extra = llm.extra_body or {}
    assert extra.get("reasoning", {}).get("effort") == "none", "关闭思考应带 effort=none"


# ── key 校验 ──

def test_get_model_config_issues_responses_requires_key(monkeypatch):
    """responses 类型在没有填 key 时应提示配置 responses_api_key。"""
    idx = _find_responses_index()
    assert idx >= 0
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "")
    issues = get_model_config_issues(idx)
    assert any("responses_api_key" in i for i in issues), f"期望提示缺 key，实际 {issues}"


def test_get_model_config_issues_responses_key_ok(monkeypatch):
    """填好 key 的 responses 模型不应报缺 key。"""
    idx = _find_responses_index()
    assert idx >= 0
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-valid-not-placeholder")
    issues = get_model_config_issues(idx)
    assert not any("responses_api_key" in i for i in issues), f"填了 key 仍报缺：{issues}"


# ── _wrap_system_for_cache：responses 走纯字符串 ──

def test_wrap_system_responses_plain_string(monkeypatch):
    """responses provider 的 system 应保持纯字符串（不能走 Anthropic cache block）。"""
    from langchain_core.messages import SystemMessage
    from src.streaming import _wrap_system_for_cache
    msgs = [SystemMessage(content="old")] + [ ]
    out = _wrap_system_for_cache(msgs, "新鲜 system", provider="responses")
    # 纯字符串形态 → 无 cache_control、且 first.content 是 str 而非 list
    head = out[0]
    assert isinstance(head.content, str), f"responses system 应为字符串，实际 {type(head.content)}"
    assert head.content == "新鲜 system"
    assert not isinstance(head.content, list)


def test_wrap_system_custom_responses_plain_string(monkeypatch):
    """custom 但 protocol='responses' 时 system 也应保持纯字符串。"""
    from langchain_core.messages import SystemMessage
    from src.streaming import _wrap_system_for_cache
    # provider='custom' 时 _wrap_system_for_cache 会反查 protocol；直接 patch 成 responses 配置
    monkeypatch.setattr(
        models, "_lookup_custom_model",
        lambda model_id: {
            "name": "x", "model_id": model_id, "protocol": "responses", "api_key": "k",
        },
    )
    msgs = [SystemMessage(content="old")]
    out = _wrap_system_for_cache(msgs, "新鲜 system", provider="custom")
    head = out[0]
    assert isinstance(head.content, str), f"custom-responses system 应为字符串，实际 {type(head.content)}"


# ── Responses content block（output_text / reasoning）端到端渲染与持久化 ──
# LangChain responses/v1 的正文块是 output_text、思考块是 reasoning(summary_text)，
# 与 Anthropic 的 text/thinking 不同。若各处只认 text/thinking，Responses 内容会被静默
# 丢弃：展示空白、Debug 无思考、历史存空、工具回合缺 reasoning item 触发 400。

def test_content_blocks_helpers():
    from src.content_blocks import (
        block_text, block_thinking, content_text, is_text_block, is_think_block)
    assert block_text({"type": "output_text", "text": "a"}) == "a"
    assert block_text({"type": "text", "text": "b"}) == "b"
    assert block_text({"type": "reasoning"}) == ""
    assert block_thinking(
        {"type": "reasoning",
         "summary": [{"type": "summary_text", "text": "x"}, {"type": "summary_text", "text": "y"}]}
    ) == "xy"
    assert block_thinking({"type": "thinking", "thinking": "z"}) == "z"
    assert is_text_block({"type": "output_text"}) and not is_text_block({"type": "reasoning"})
    assert is_think_block({"type": "reasoning"}) and not is_think_block({"type": "text"})
    assert content_text(
        [{"type": "output_text", "text": "a"}, {"type": "reasoning", "summary": []},
         {"type": "text", "text": "b"}]
    ) == "a\nb"
    assert content_text("plain") == "plain"


class _FakeUI:
    def __init__(self):
        self.msgs = []

    def show_message(self, text, tag):
        self.msgs.append((tag, text))

    def remove_thinking_indicator(self):
        pass


def test_handle_stream_chunk_renders_responses_blocks():
    """reasoning → 思考展示；output_text → 进 raw_text（最终答案唯一来源）并展示。"""
    import threading
    from langchain_core.messages import AIMessageChunk
    from src.streaming import _handle_stream_chunk, _StreamState
    st, ui, hb, phase = _StreamState(), _FakeUI(), threading.Event(), [None]
    _handle_stream_chunk(st, AIMessageChunk(content=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "想一下"}]}]),
        ui, hb, phase)
    _handle_stream_chunk(st, AIMessageChunk(content=[
        {"type": "output_text", "text": "你好"}]), ui, hb, phase)
    tags = [t for t, _ in ui.msgs]
    joined = "".join(x for _, x in ui.msgs)
    assert st.raw_text == "你好", f"正文未进 raw_text：{st.raw_text!r}"
    assert "think_msg" in tags and "想一下" in joined      # 思考展示
    assert "ai_msg" in tags and "你好" in joined           # 正文展示


def test_extract_thinking_responses_reasoning():
    from langchain_core.messages import AIMessageChunk
    from src.streaming import _extract_thinking
    g = AIMessageChunk(content=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "思考内容"}]}])
    assert "思考内容" in _extract_thinking(g)


def test_build_ai_message_keeps_responses_blocks():
    """output_text + reasoning 都保留；reasoning 整块保留（含 id）供下一轮回传防 400。"""
    from langchain_core.messages import AIMessageChunk
    from src.memory import _build_ai_message
    gathered = AIMessageChunk(content=[
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "t"}]},
        {"type": "output_text", "text": "答案"}])
    msg = _build_ai_message(gathered, "答案", [])
    assert isinstance(msg.content, list)
    types = [b.get("type") for b in msg.content]
    assert "reasoning" in types and "output_text" in types
    rblk = next(b for b in msg.content if b.get("type") == "reasoning")
    assert rblk.get("id") == "rs_1"                        # id 未丢


def test_build_ai_message_keeps_reasoning_without_visible_text():
    """纯工具调用回合（reasoning + tool_call、无 output_text）也要保留 reasoning item。"""
    from langchain_core.messages import AIMessageChunk
    from src.memory import _build_ai_message
    gathered = AIMessageChunk(content=[{"type": "reasoning", "id": "rs_2", "summary": []}])
    msg = _build_ai_message(gathered, "", [{"name": "f", "args": {}, "id": "c1"}])
    assert isinstance(msg.content, list)
    assert any(b.get("type") == "reasoning" and b.get("id") == "rs_2" for b in msg.content)


def test_extract_text_content_responses_output_text():
    from langchain_core.messages import AIMessage
    from src.memory import _extract_text_content
    m = AIMessage(content=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "t"}]},
        {"type": "output_text", "text": "答案"}])
    assert _extract_text_content(m) == "答案"
