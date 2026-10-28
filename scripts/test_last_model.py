"""打开软件时默认选上次实际调用的模型。

按 类型 + model_id + 名字 记（不记序号，config 改了模型列表序号就对不上）；
没记过、记的模型已经从列表里删掉、文件坏了，都退回 config 的 default_model_id。
"""
import json

import pytest

from src import models


@pytest.fixture()
def model_list(monkeypatch):
    entries = [
        ("Claude Code", "claude-code", "claude-code", False),
        ("mimo-v2.5-pro", "mimo", "mimo-v2.5-pro", True),
        ("deepseek-flash", "deepseek", "deepseek-flash", True),
        ("⚙ deepseek-v4.1-flash", "custom", "deepseek-flash", True),
    ]
    monkeypatch.setattr(models, "MODEL_LIST", entries)
    return entries


def test_remembers_by_identity_not_position(isolated_memory, model_list):
    assert models.last_model_index() is None, "没记过"
    models.remember_last_model(3)
    assert models.last_model_index() == 3, "同一个 model_id 也分得清是哪一项（类型不同）"
    saved = json.loads((isolated_memory / "last_model.json").read_text(encoding="utf-8"))
    assert saved == {"name": "⚙ deepseek-v4.1-flash", "type": "custom", "model_id": "deepseek-flash"}

    # config 里在前面加了一个模型：序号变了，照样认得出
    model_list.insert(1, ("qwen-max", "cloud", "qwen-max", False))
    assert models.last_model_index() == 4
    # 只改了显示名：类型和 model_id 没变，仍算同一个
    model_list[4] = ("deepseek 4.1", "custom", "deepseek-flash", True)
    assert models.last_model_index() == 4


def test_removed_model_or_broken_file_means_not_remembered(isolated_memory, model_list):
    models.remember_last_model(2)
    model_list.pop(2)
    assert models.last_model_index() is None, "记的模型已经删了"
    (isolated_memory / "last_model.json").write_text("{坏的", encoding="utf-8")
    assert models.last_model_index() is None


def test_startup_prefers_the_last_model_then_the_configured_default(isolated_memory, model_list,
                                                                    monkeypatch):
    from src import agent, config
    monkeypatch.setattr(agent, "MODEL_LIST", model_list)
    monkeypatch.setattr(config, "DEFAULT_MODEL_ID", "mimo-v2.5-pro")
    assert agent._resolve_default_model_index() == 1, "没记过：按 default_model_id"
    models.remember_last_model(2)
    assert agent._resolve_default_model_index() == 2, "记过：用上次的"


@pytest.fixture()
def run_env(monkeypatch, tmp_path, isolated_memory):
    from langchain_core.messages import AIMessage
    from src import agent as _agent
    from src import notify
    from src import session as _session
    from src.models import MODEL_LIST
    sess = _session.Session()
    sess.agent_mode = "act"
    sess.project = str(tmp_path)
    sess.current_model_index = next(
        i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama"))
    _session.bind_thread(sess)
    monkeypatch.setattr(_agent, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    monkeypatch.setattr(notify, "notify", lambda *a, **k: None)
    monkeypatch.setattr(notify, "notify_long", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "_stream_with_tools",
                        lambda ui: ("做完了", [], {"input": 1, "output": 1, "total": 2}, None))
    monkeypatch.setattr(_agent, "_build_ai_message", lambda g, t, tcs: AIMessage(content=t))
    yield _agent, sess
    _session.bind_thread(None)


class _UI:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_a_run_records_the_model_it_used(run_env):
    _agent, sess = run_env
    _agent.agent_loop(_UI())
    assert models.last_model_index() == sess.current_model_index


def test_subagent_runs_do_not_count(run_env):
    _agent, sess = run_env
    sess.is_subagent = True
    _agent.agent_loop(_UI())
    assert models.last_model_index() is None
