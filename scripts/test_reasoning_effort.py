"""思考强度（reasoning_effort）后端测试。

覆盖：
- 档位归一化 / 布尔派生（off/low/high/max ↔ reasoning_enabled）
- 各档位实际发给模型的参数（responses 内置 / 自定义 responses / 只开关的 ollama /
  不支持思考的模型忽略该字段）
- 按模型类型返回支持档位（supported_efforts / supported_efforts_for）
- LLM 缓存 key 按生效档位区分
- 会话存盘 / 读回，以及旧会话只有 reasoning_enabled 时的推断
"""
import json
import os

import pytest

from src import models, reasoning
from src import session as _session
from src import state
from src.memory import load_session, save_session


# ── 档位归一化与布尔派生 ──

def test_normalize_accepts_efforts_and_aliases():
    for v in ("off", "low", "high", "max"):
        assert reasoning.normalize(v) == v
    assert reasoning.normalize("OFF") == "off"
    assert reasoning.normalize(" none ") == "off"
    assert reasoning.normalize("medium") == "high"     # 无 medium 档，归到默认档
    assert reasoning.normalize(None) == "high"
    assert reasoning.normalize("乱写") == "high"
    assert reasoning.normalize("乱写", default="low") == "low"


def test_normalize_accepts_legacy_bool():
    assert reasoning.normalize(True) == "high"
    assert reasoning.normalize(False) == "off"


def test_enabled_is_not_off():
    assert reasoning.enabled("low") is True
    assert reasoning.enabled("max") is True
    assert reasoning.enabled("off") is False
    assert reasoning.enabled(None) is True     # 坏值收成默认档 → 开


def test_legacy_to_effort():
    assert reasoning.legacy_to_effort(True) == "high"
    assert reasoning.legacy_to_effort(False) == "off"


# ── reasoning_enabled 作为派生视图 ──

def test_session_reasoning_enabled_derived_from_effort():
    s = _session.Session()
    s.reasoning_effort = "off"
    assert s.reasoning_enabled is False
    for effort in ("low", "high", "max"):
        s.reasoning_effort = effort
        assert s.reasoning_enabled is True


def test_session_reasoning_enabled_setter_maps_to_effort():
    s = _session.Session()
    s.reasoning_enabled = False
    assert s.reasoning_effort == "off"
    s.reasoning_enabled = True
    assert s.reasoning_effort == "high"


def test_state_proxy_exposes_both_fields():
    """state.reasoning_effort 是存储字段；state.reasoning_enabled 仍可读可写（派生）。"""
    old = state.reasoning_effort
    try:
        state.reasoning_effort = "max"
        assert state.reasoning_enabled is True
        assert _session.get_active().reasoning_effort == "max"
        state.reasoning_enabled = False
        assert _session.get_active().reasoning_effort == "off"
        assert state.reasoning_enabled is False
    finally:
        state.reasoning_effort = old


# ── 支持档位查询 ──

def test_supported_efforts_by_model_type(monkeypatch):
    assert models.supported_efforts("responses", "deepseek-flash", True) == (
        "off", "low", "high", "max")
    assert models.supported_efforts("ollama", "qwen3.5:latest", True) == ("off", "on")
    # 不支持思考的类型
    for mtype in ("anthropic", "mimo", "gemini", "deepseek", "cloud", "claude-code"):
        assert models.supported_efforts(mtype, "x", False) == ()
    # 声明支持思考但类型没有可用分档参数 → 空（不乱发字段）
    assert models.supported_efforts("anthropic", "x", True) == ()


def test_supported_efforts_custom_depends_on_protocol(monkeypatch):
    monkeypatch.setattr(models, "_lookup_custom_model",
                        lambda model_id: {"protocol": "responses"})
    assert models.supported_efforts("custom", "gpt-5", True) == ("off", "low", "high", "max")
    monkeypatch.setattr(models, "_lookup_custom_model",
                        lambda model_id: {"protocol": "openai"})
    assert models.supported_efforts("custom", "gpt-5", True) == ()
    # 没声明支持思考
    monkeypatch.setattr(models, "_lookup_custom_model",
                        lambda model_id: {"protocol": "responses"})
    assert models.supported_efforts("custom", "gpt-5", False) == ()


def test_supported_efforts_for_model_index(monkeypatch):
    idx = _find_index("responses")
    assert idx >= 0
    assert models.supported_efforts_for(idx) == ("off", "low", "high", "max")
    ollama = _find_index("ollama")
    if ollama >= 0:
        assert models.supported_efforts_for(ollama) == ("off", "on")
    invalid = _find_index("anthropic")
    if invalid >= 0:
        assert models.supported_efforts_for(invalid) == ()
    assert models.supported_efforts_for(10_000) == ()


# ── 实际发出去的参数 ──

def _find_index(mtype):
    for i, (_, t, _, _) in enumerate(models.MODEL_LIST):
        if t == mtype:
            return i
    return -1


def _responses_llm(monkeypatch, effort):
    idx = _find_index("responses")
    assert idx >= 0, "需要 config 里有 responses_models 条目（默认 deepseek-flash）"
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    monkeypatch.setattr(models, "RESPONSES_BASE_URL", "https://api.deepseek.com")
    return models._create_llm_uncached(model_index=idx, effort=effort)


@pytest.mark.parametrize("effort,wire", [
    ("off", "none"),
    ("low", "low"),
    ("high", "high"),
    ("max", "max"),
])
def test_responses_effort_sent(monkeypatch, effort, wire):
    """DeepSeek Responses：off 发 none，low/high/max 原样发。"""
    llm = _responses_llm(monkeypatch, effort)
    extra = llm.extra_body or {}
    assert extra.get("reasoning", {}).get("effort") == wire, f"{effort} → 实际 {extra}"


def test_responses_legacy_bool_still_works(monkeypatch):
    """未改的旧调用点仍按布尔传 reasoning=：True → high，False → none。"""
    idx = _find_index("responses")
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    llm_off = models._create_llm_uncached(model_index=idx, reasoning=False)
    assert (llm_off.extra_body or {}).get("reasoning", {}).get("effort") == "none"
    llm_on = models._create_llm_uncached(model_index=idx, reasoning=True)
    assert (llm_on.extra_body or {}).get("reasoning", {}).get("effort") == "high"


def test_custom_responses_effort_sent(monkeypatch):
    monkeypatch.setattr(models, "MODEL_LIST",
                        [("My GPT", "custom", "gpt-5", True)])
    monkeypatch.setattr(models, "_lookup_custom_model",
                        lambda model_id: {"protocol": "responses", "api_key": "sk-test",
                                          "base_url": "https://gw.example.com/v1"})
    for effort, wire in (("off", "none"), ("low", "low"), ("max", "max")):
        llm = models._create_llm_uncached(model_index=0, effort=effort)
        extra = llm.extra_body or {}
        assert extra.get("reasoning", {}).get("effort") == wire, f"{effort} → {extra}"


def test_ollama_only_switches(monkeypatch):
    """只能开关的类型：off 不传，low/high/max 一律按"开"。"""
    idx = _find_index("ollama")
    assert idx >= 0, "需要 config 里有 ollama 模型（默认 qwen3.5:latest）"
    off = models._create_llm_uncached(model_index=idx, effort="off")
    # 不传该参数时 ChatOllama 的字段是 None（不是 False），只要求"不是开"
    assert not getattr(off, "reasoning", False)
    for effort in ("low", "high", "max"):
        on = models._create_llm_uncached(model_index=idx, effort=effort)
        assert getattr(on, "reasoning", False) is True, f"{effort} 应传 reasoning=True"


def test_unsupported_model_ignores_effort(monkeypatch):
    """不支持思考的类型忽略档位：任何档位都不注入 reasoning 参数。"""
    monkeypatch.setattr(models, "MODEL_LIST",
                        [("deepseek-v4-pro", "deepseek", "deepseek-v4-pro", False)])
    monkeypatch.setattr(models, "DEEPSEEK_API_KEY", "sk-test")
    for effort in ("off", "low", "high", "max"):
        llm = models._create_llm_uncached(model_index=0, effort=effort)
        extra = llm.extra_body or {}
        # 只有既有的"禁用 thinking"（langchain 回传不了 reasoning_content），与档位无关
        assert "reasoning" not in extra
        assert extra.get("thinking") == {"type": "disabled"}


def test_custom_openai_protocol_ignores_effort(monkeypatch):
    """自定义 OpenAI 兼容协议没有通用分档字段，声明支持思考也不发。"""
    monkeypatch.setattr(models, "MODEL_LIST", [("My", "custom", "m", True)])
    monkeypatch.setattr(models, "_lookup_custom_model",
                        lambda model_id: {"protocol": "openai", "api_key": "k"})
    llm = models._create_llm_uncached(model_index=0, effort="max")
    assert "reasoning" not in (llm.extra_body or {})


# ── 缓存 key 按档位区分 ──

def test_llm_cache_key_differs_by_effort(monkeypatch):
    idx = _find_index("responses")
    assert idx >= 0
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test")
    monkeypatch.setattr(models, "_LLM_CACHE", {})
    low = models._create_llm(idx, effort="low")
    low_again = models._create_llm(idx, effort="low")
    high = models._create_llm(idx, effort="high")
    maxi = models._create_llm(idx, effort="max")
    assert low is low_again, "同档位应复用缓存"
    assert len({id(low), id(high), id(maxi)}) == 3, "不同档位必须建不同实例"
    assert len(models._LLM_CACHE) == 3
    assert (maxi.extra_body or {})["reasoning"]["effort"] == "max"


def test_llm_cache_key_normalizes_unsupported_models(monkeypatch):
    """不支持思考的模型：不同档位落到同一个缓存条目（实际不发参数）。"""
    monkeypatch.setattr(models, "MODEL_LIST", [("deepseek-v4-pro", "deepseek",
                                                "deepseek-v4-pro", False)])
    monkeypatch.setattr(models, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(models, "_LLM_CACHE", {})
    a = models._create_llm(0, effort="low")
    b = models._create_llm(0, effort="max")
    assert a is b
    assert len(models._LLM_CACHE) == 1


# ── 会话存盘 / 读回 ──

def _new_session_with_history():
    from langchain_core.messages import AIMessage, HumanMessage
    s = _session.Session()
    s.chat_history = [HumanMessage(content="你好"), AIMessage(content="你好呀")]
    return s


def test_save_session_persists_reasoning_effort(isolated_memory):
    s = _new_session_with_history()
    s.reasoning_effort = "max"
    save_session(session=s)
    path = os.path.join(str(isolated_memory), f"{s.current_session_id}.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["reasoning_effort"] == "max"


def test_save_session_normalizes_bad_effort(isolated_memory):
    s = _new_session_with_history()
    s.reasoning_effort = "乱写"
    save_session(session=s)
    path = os.path.join(str(isolated_memory), f"{s.current_session_id}.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["reasoning_effort"] == "high"


def test_load_session_restores_reasoning_effort(isolated_memory):
    s = _new_session_with_history()
    s.reasoning_effort = "low"
    save_session(session=s)
    sid = s.current_session_id

    target = _session.Session()
    target.reasoning_effort = "max"     # 读盘应覆盖
    assert load_session(sid, session=target)
    assert target.reasoning_effort == "low"
    assert target.reasoning_enabled is True


@pytest.mark.parametrize("legacy_flag,expected", [(True, "high"), (False, "off")])
def test_load_session_infers_effort_from_legacy_flag(isolated_memory, legacy_flag, expected):
    """旧会话只有布尔 reasoning_enabled：True → high，False → off。"""
    sid = "20250101_000000_000000"
    data = {
        "id": sid,
        "title": "旧会话",
        "agent_mode": "act",
        "reasoning_enabled": legacy_flag,
        "messages": [
            {"type": "HumanMessage", "content": "hi"},
            {"type": "AIMessage", "content": "hello"},
        ],
    }
    path = os.path.join(str(isolated_memory), f"{sid}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    target = _session.Session()
    assert load_session(sid, session=target)
    assert target.reasoning_effort == expected
    assert target.reasoning_enabled is (legacy_flag is True)


def test_load_session_defaults_when_neither_field(isolated_memory):
    """两个字段都没有（更老的会话）→ 默认档，与旧版默认"开"一致。"""
    sid = "20250101_000001_000000"
    data = {
        "id": sid,
        "title": "更旧",
        "messages": [
            {"type": "HumanMessage", "content": "hi"},
            {"type": "AIMessage", "content": "hello"},
        ],
    }
    with open(os.path.join(str(isolated_memory), f"{sid}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    target = _session.Session()
    assert load_session(sid, session=target)
    assert target.reasoning_effort == reasoning.DEFAULT
    assert target.reasoning_enabled is True
