"""M3 按模型上下文窗口设预算 —— 自检测试。

测 context_window_for（src/models.py）和 _current_history_budget（src/streaming.py）。
用 monkeypatch 改 config / MODEL_LIST，不依赖 UI / 真实 LLM。
"""

import pytest

from src.limits import (
    HISTORY_SAFETY_MARGIN,
    HISTORY_TOKEN_BUDGET,
    MAX_HISTORY_BUDGET,
)
from src.models import (
    MODEL_LIST as _MODEL_LIST_SNAPSHOT,
    _DEFAULT_CONTEXT_WINDOWS,
    _FALLBACK_CONTEXT_WINDOW,
    context_window_for,
    _max_tokens_for,
)

# 在模块导入时（fixture 执行前）快照完整 MODEL_LIST
_REAL_MODEL_LIST = list(_MODEL_LIST_SNAPSHOT)


# ── context_window_for 测试 ──


def test_builtin_model_resolution():
    """内置模型映射表能正确解析。"""
    for mid, expected in _DEFAULT_CONTEXT_WINDOWS.items():
        assert context_window_for("", mid) == expected, f"{mid} → {expected}"


def test_config_override(monkeypatch):
    """config.json custom_models 里的 context_window 覆盖内置映射。"""
    from src import models

    monkeypatch.setattr(
        models, "CUSTOM_MODELS", [{"model_id": "my-model", "context_window": 999_999}]
    )
    assert context_window_for("custom", "my-model") == 999_999


def test_model_context_windows_override(monkeypatch):
    """config.json 的 model_context_windows 按 model_id 覆盖内置窗口（最高优先）。"""
    from src import models

    monkeypatch.setattr(models, "MODEL_CONTEXT_WINDOWS", {"mimo-v2.5-pro": 12_345})
    assert context_window_for("mimo", "mimo-v2.5-pro") == 12_345


def test_fallback():
    """未知模型返回 _FALLBACK_CONTEXT_WINDOW（65536）。"""
    assert context_window_for("", "nonexistent-model-xyz") == _FALLBACK_CONTEXT_WINDOW


def test_context_window_for_exception_safety():
    """context_window_for 遇异常时返回 _FALLBACK_CONTEXT_WINDOW。"""
    assert context_window_for("", None) == _FALLBACK_CONTEXT_WINDOW  # type: ignore[arg-type]
    assert context_window_for("", "") == _FALLBACK_CONTEXT_WINDOW


# ── _current_history_budget 测试 ──


@pytest.fixture()
def _use_real_models(monkeypatch):
    """把模块导入时快照的完整 MODEL_LIST 注入 streaming 模块。"""
    from src import streaming

    monkeypatch.setattr(streaming, "MODEL_LIST", _REAL_MODEL_LIST)


def _find_model(substring: str) -> tuple[int, tuple]:
    """在 _REAL_MODEL_LIST 中找包含 substring 的模型，返回 (idx, entry)。"""
    for i, m in enumerate(_REAL_MODEL_LIST):
        if substring in m[2]:
            return i, m
    pytest.skip(f"MODEL_LIST 中无 '{substring}' 模型（当前 {len(_REAL_MODEL_LIST)} 个）")


def test_global_bounds(_use_real_models):
    """预算上限钳位：大窗口模型预算不超过 MAX_HISTORY_BUDGET。

    找一个 context_window >= MAX_HISTORY_BUDGET 的模型，验证 raw budget 被钳位。
    """
    from src import streaming

    # 优先 gemini（1M），否则任意大窗口模型
    for kw in ("gemini", "claude", "mimo"):
        idx, m = next(((i, e) for i, e in enumerate(_REAL_MODEL_LIST) if kw in e[2]), (None, None))
        if idx is not None and context_window_for(m[0], m[2]) > MAX_HISTORY_BUDGET:
            break
    else:
        pytest.skip("无 context_window > MAX_HISTORY_BUDGET 的模型")

    orig_idx = streaming.state.current_model_index
    streaming.state.current_model_index = idx
    try:
        assert streaming._current_history_budget() == MAX_HISTORY_BUDGET
    finally:
        streaming.state.current_model_index = orig_idx


def test_exception_fallback(_use_real_models):
    """_current_history_budget 异常时回退到 HISTORY_TOKEN_BUDGET。"""
    from src import streaming

    orig_idx = streaming.state.current_model_index
    streaming.state.current_model_index = len(_REAL_MODEL_LIST)  # 越界 → IndexError
    try:
        assert streaming._current_history_budget() == HISTORY_TOKEN_BUDGET
    finally:
        streaming.state.current_model_index = orig_idx


def test_deepseek_budget_capped(_use_real_models):
    """DeepSeek V4 窗口 1M(2026-04 起官方全线 1M)→ 预算被 MAX_HISTORY_BUDGET 钳到上限。"""
    from src import streaming

    ds_idx, m = _find_model("deepseek")
    assert context_window_for(m[0], m[2]) == 1_048_576
    orig_idx = streaming.state.current_model_index
    streaming.state.current_model_index = ds_idx
    try:
        assert streaming._current_history_budget() == MAX_HISTORY_BUDGET
    finally:
        streaming.state.current_model_index = orig_idx


def test_claude_200k_budget(_use_real_models):
    """Claude 系列 → 200K 窗口，预算 = 200000 - max_tokens - margin。"""
    from src import streaming

    # 优先 claude-3.5，否则任一 claude（要求 ctx ≥ 200K）
    idx, m = next(
        (
            (i, e)
            for i, e in enumerate(_REAL_MODEL_LIST)
            if "claude" in e[2]
            and context_window_for(e[0], e[2]) >= 200_000
        ),
        (None, None),
    )
    if idx is None:
        pytest.skip("无 ctx≥200K 的 Claude 模型")

    orig_idx = streaming.state.current_model_index
    streaming.state.current_model_index = idx
    try:
        expected = 200_000 - _max_tokens_for(m[0], m[2]) - HISTORY_SAFETY_MARGIN
        assert streaming._current_history_budget() == expected
    finally:
        streaming.state.current_model_index = orig_idx
