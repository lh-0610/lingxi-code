"""首次上手引导测试：

- get_model_config_issues 的缺 key 提示指向「设置」（而非叫人改 config.json）
- has_usable_model：只认填好 key 的云模型；ollama / claude-code 不计
"""
from src import models


def test_config_issue_points_to_settings(monkeypatch):
    """没填 key 时提示指向「设置」，不再叫人改 config.json。"""
    idx = next(i for i, m in enumerate(models.MODEL_LIST) if m[1] == "mimo")
    monkeypatch.setattr(models, "MIMO_API_KEY", "")
    issues = models.get_model_config_issues(idx)
    assert issues, "缺 key 应有提示"
    assert "设置" in issues[0]
    assert "config.json" not in issues[0]


def _blank_all_cloud_keys(monkeypatch):
    for k in ("CLOUD_API_KEY", "ANTHROPIC_API_KEY", "MIMO_API_KEY",
              "GOOGLE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setattr(models, k, "")
    monkeypatch.setattr(models, "CUSTOM_MODELS", [])


def test_has_usable_model_false_when_no_keys(monkeypatch):
    """所有云模型 key 都空 → 没有可用模型。"""
    _blank_all_cloud_keys(monkeypatch)
    assert models.has_usable_model() is False


def test_has_usable_model_true_with_one_key(monkeypatch):
    """填好一个云模型(mimo)的 key → 有可用模型。"""
    _blank_all_cloud_keys(monkeypatch)
    monkeypatch.setattr(models, "MIMO_API_KEY", "tp-realkey-123")
    assert models.has_usable_model() is True


def test_local_models_not_counted(monkeypatch):
    """ollama / claude-code 需本机搭建,无云 key 时不算"可用"(新用户多半没搭)。"""
    _blank_all_cloud_keys(monkeypatch)
    monkeypatch.setattr(models, "MODEL_LIST", [
        ("Qwen 本地", "ollama", "qwen3.5:latest", False),
        ("Claude Code", "claude-code", "claude", False),
    ])
    assert models.has_usable_model() is False


def test_custom_model_with_key_counts(monkeypatch):
    """自定义模型填了 api_key → 算可用。"""
    _blank_all_cloud_keys(monkeypatch)
    monkeypatch.setattr(models, "MODEL_LIST", [
        ("⚙ MyModel", "custom", "my-model", False),
    ])
    monkeypatch.setattr(models, "CUSTOM_MODELS", [
        {"model_id": "my-model", "api_key": "sk-real-xyz", "protocol": "openai"},
    ])
    assert models.has_usable_model() is True
