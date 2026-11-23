"""设置热更新：改 config.json（设置里保存 / 手改文件）不用重启就生效。

① config.reload 就地重算常量、报出变了哪些；文件坏了什么都不动
② 模型列表就地重建，各会话的模型对回新列表；删掉的退回默认模型
③ 新的密钥真的进了模型客户端（以前 models 在导入期抄走了 key，改了也用旧的）
④ 遥控轮询换线程时，还卡在长轮询里的旧线程不再处理它拿到的消息
⑤ 设置对话框保存当场生效，只有标题栏还要重启；手改 config.json 也生效
"""
import gc
import json
import os

import pytest
from PySide6.QtCore import QEvent

from src import agent, config, config_apply, models, session, state


def _deepseek_index():
    return next(i for i, (_, t, m, _) in enumerate(models.MODEL_LIST)
                if (t, m) == ("deepseek", "deepseek-flash"))


# ── ① config.reload ─────────────────────────────────────────

def test_reload_updates_constants_in_place_and_reports_changes():
    changed = config.reload({"deepseek_api_key": "sk-one", "agent_max_rounds": 7})
    assert config.DEEPSEEK_API_KEY == "sk-one"
    assert config.AGENT_MAX_ROUNDS == 7
    # 派生项跟着重算（responses 没单独配 key 时复用 deepseek 的）
    assert config.RESPONSES_API_KEY == "sk-one"
    assert {"DEEPSEEK_API_KEY", "AGENT_MAX_ROUNDS", "RESPONSES_API_KEY"} <= changed
    # 同样的内容再来一次：什么都没变
    assert config.reload({"deepseek_api_key": "sk-one", "agent_max_rounds": 7}) == set()
    # 去掉的键回到默认值，而不是留着上一次的
    config.reload({})
    assert config.DEEPSEEK_API_KEY == "" and config.AGENT_MAX_ROUNDS != 7


def test_reload_uses_the_given_content_not_a_second_read(tmp_path, monkeypatch):
    """传进来的内容就是要用的：不再读一遍盘（两次读之间文件可能正被写一半）。"""
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    (tmp_path / "config.json").write_text('{"deepseek_api_key": "sk-on-disk"', encoding="utf-8")
    config.reload({"deepseek_api_key": "sk-given"})
    assert config.DEEPSEEK_API_KEY == "sk-given"


def test_broken_file_keeps_current_config(tmp_path, monkeypatch):
    config.reload({"deepseek_api_key": "sk-keep"})
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    path.write_text('{"deepseek_api_key": "sk-half', encoding="utf-8")   # 写到一半
    assert config.reload() is None
    assert config_apply.reload_and_apply() is None
    assert config.DEEPSEEK_API_KEY == "sk-keep"   # 没被清成空配置
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    assert config.reload() is None
    assert config.DEEPSEEK_API_KEY == "sk-keep"


# ── ② 模型列表 ───────────────────────────────────────────────

def test_model_list_is_rebuilt_in_place():
    """`from .models import MODEL_LIST` 抄走的是同一个 list：换对象的话 agent 那边还指着旧表。"""
    alias = models.MODEL_LIST
    assert agent.MODEL_LIST is alias
    config.reload({"custom_models": [{"name": "新模型", "model_id": "m-new", "protocol": "openai"}]})
    models.refresh_model_list()
    assert models.MODEL_LIST is alias and agent.MODEL_LIST is alias
    assert ("⚙ 新模型", "custom", "m-new", False) in alias


def test_sessions_follow_their_model_and_fall_back_when_it_is_removed(monkeypatch):
    # 回退走「打开时选哪个」：上次用的模型（别的测试可能记过）不算，只看 default_model_id
    monkeypatch.setattr(models, "last_model_index", lambda: None)
    config.reload({"deepseek_models": ["deepseek-flash", "deepseek-v4-pro"]})
    config_apply.apply({"DEEPSEEK_MODELS"})
    keep = _deepseek_index()
    gone = next(i for i, (_, t, m, _) in enumerate(models.MODEL_LIST) if m == "deepseek-v4-pro")
    active = session.get_active()
    other = session.Session()
    other_key = session.register(other)
    try:
        active.current_model_index = keep
        other.current_model_index = gone
        # 在前面加两个 mimo 模型：deepseek 的下标往后挪；v4-pro 删掉
        res = config_apply.reload_and_apply({
            "mimo_models": ["mimo-a", "mimo-b", "mimo-v2.5-pro"],
            "deepseek_models": ["deepseek-flash"],
        })
        assert res.models_changed
        assert models.MODEL_LIST[active.current_model_index][1:3] == ("deepseek", "deepseek-flash")
        assert active.current_model_index != keep    # 真的挪了位置，不是碰巧没动
        # v4-pro 没了：退回默认模型（default_model_id 默认 mimo-v2.5-pro），并报出来
        assert models.MODEL_LIST[other.current_model_index][2] == "mimo-v2.5-pro"
        # 注册表里可能还有别的测试留下的会话，只看这两个
        mine = [(s, old) for s, old, _ in res.model_fallbacks if s in (active, other)]
        assert mine == [(other, "deepseek-v4-pro")]
    finally:
        session.drop(other_key)


# ── ③ 新密钥进客户端 ─────────────────────────────────────────

def test_new_key_reaches_the_model_client():
    config_apply.reload_and_apply({"deepseek_api_key": "sk-old"})
    sess = session.get_active()
    sess.current_model_index = _deepseek_index()
    llm, _ = agent.resolve_bound_llm(sess)
    assert llm.openai_api_key.get_secret_value() == "sk-old"

    res = config_apply.reload_and_apply({"deepseek_api_key": "sk-new",
                                         "deepseek_base_url": "https://example.invalid/v1"})
    assert res.models_changed
    llm, _ = agent.resolve_bound_llm(sess)
    assert llm.openai_api_key.get_secret_value() == "sk-new"
    assert str(llm.openai_api_base).startswith("https://example.invalid")


def test_unrelated_change_does_not_touch_models(monkeypatch):
    called = []
    monkeypatch.setattr(config_apply, "_refresh_models", lambda: called.append(1) or [])
    res = config_apply.reload_and_apply({"web_search_api_key": "tvly-x", "agent_max_rounds": 9})
    assert res.changed and not res.models_changed and called == []


# ── ④ 遥控轮询换线程 ─────────────────────────────────────────

def test_stale_poll_thread_drops_updates_after_restart(monkeypatch):
    from src import telegram_poll as poll

    class _Resp:
        def __init__(self, status, data=None):
            self.status_code, self._data, self.text = status, data, ""

        def json(self):
            return self._data

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        if params.get("offset") == -1:
            return _Resp(500)
        # 这一轮长轮询期间用户改了配置：shutdown 让代数 +1、新线程起来了
        poll._gen += 1
        return _Resp(200, {"ok": True, "result": [
            {"update_id": 7, "message": {"from": {"id": 42}, "text": "跑一下测试"}}]})

    dispatched = []
    monkeypatch.setattr(poll.httpx, "get", fake_get)
    monkeypatch.setattr(poll, "_dispatch", dispatched.append)
    monkeypatch.setattr(poll.config, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(poll, "_running", True)
    monkeypatch.setattr(poll, "_gen", 3)
    poll._poll_loop(3, "https://api.telegram.org/botX")
    assert dispatched == []          # 留给新线程收，旧线程不执行
    assert len(calls) == 2           # 并且自己退出了


def test_telegram_restarts_only_when_its_settings_change(monkeypatch):
    from src import telegram_poll as poll
    events = []
    monkeypatch.setattr(poll, "shutdown", lambda: events.append("stop"))
    monkeypatch.setattr(poll, "start", lambda: events.append("start"))
    config_apply.reload_and_apply({"agent_max_rounds": 11})
    assert events == []
    config_apply.reload_and_apply({"agent_max_rounds": 11,
                                   "notify": {"telegram_bot_token": "1:abc", "telegram_chat_id": "9"},
                                   "remote_control": {"enabled": True}})
    assert events == ["stop", "start"]


def test_mcp_reconnects_when_servers_or_switch_change(monkeypatch):
    started = []
    monkeypatch.setattr(config_apply, "_restart_mcp_bg", lambda: started.append(1))
    res = config_apply.reload_and_apply({"mcp_servers": {"a": {"command": "x"}}})
    assert res.mcp_restarting and started == [1]
    res = config_apply.reload_and_apply({"mcp_servers": {"a": {"command": "x"}}, "mcp_enabled": False})
    assert res.mcp_restarting and started == [1, 1]
    res = config_apply.reload_and_apply({"mcp_servers": {"a": {"command": "x"}}, "mcp_enabled": False,
                                         "agent_max_rounds": 5})
    assert not res.mcp_restarting and started == [1, 1]


def test_mcp_restart_clears_previous_round(monkeypatch):
    from src import mcp_client
    mcp_client._server_tools["old"] = ["t"]
    mcp_client.MCP_DISPLAY_NAMES["mcp_old_t"] = "旧"
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: ["new-tool"])
    assert mcp_client.restart() == ["new-tool"]
    assert "old" not in mcp_client._server_tools and "mcp_old_t" not in mcp_client.MCP_DISPLAY_NAMES


def test_title_bar_still_needs_restart():
    res = config_apply.reload_and_apply({"native_title_bar": True})
    assert res.restart_needed
    res = config_apply.reload_and_apply({"native_title_bar": True, "deepseek_api_key": "sk-x"})
    assert not res.restart_needed


# ── ⑤ 设置对话框 + 文件监视（真 ChatUI）──────────────────────

@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def ui(qt_app, monkeypatch, isolated_memory):
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in hot reload tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import mcp_client, tools
    from src.ui import ChatUI
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    toasts = []
    monkeypatch.setattr(window, "_show_toast", lambda text, duration=1500: toasts.append(text))
    window.toasts = toasts
    window.resize(1200, 800)
    window.show()
    qt_app.processEvents()
    yield window
    import shiboken6
    if shiboken6.isValid(window):
        if state.ui_ref is window:
            state.ui_ref = None
        window.close()
        qt_app.processEvents()
        window.deleteLater()
        gc.collect()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        qt_app.processEvents()


def _settings(monkeypatch, tmp_path, cfg):
    from src.ui import dialogs
    from src.ui import settings_dialog as sd
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(sd, "CONFIG_PATH", str(path))
    asked = []
    monkeypatch.setattr(dialogs, "success", lambda *a, **k: asked.append(a[2]) or dialogs.No)
    return sd.SettingsDialog(), path, asked


def test_settings_save_applies_without_restart(ui, qt_app, tmp_path, monkeypatch):
    dlg, path, asked = _settings(monkeypatch, tmp_path, {})
    dlg.fields["deepseek_api_key"].setText("sk-from-settings")
    dlg.fields["agent_max_rounds"].setText("12")
    dlg._save()
    try:
        assert config.DEEPSEEK_API_KEY == "sk-from-settings"
        assert config.AGENT_MAX_ROUNDS == 12
        assert asked == []                      # 不再弹「要重启」
        assert dlg.applied_result.models_changed
        assert json.loads(path.read_text(encoding="utf-8"))["deepseek_api_key"] == "sk-from-settings"
        assert not os.path.exists(str(path) + ".tmp")
        ui._on_config_applied(dlg.applied_result, from_settings=True)
        assert ui.toasts[-1] == "设置已保存，已生效"
    finally:
        dlg.deleteLater()


def test_settings_save_asks_restart_only_for_title_bar(ui, qt_app, tmp_path, monkeypatch):
    dlg, _, asked = _settings(monkeypatch, tmp_path, {})
    dlg.fields["native_title_bar"].setChecked(True)
    dlg._save()
    try:
        assert len(asked) == 1 and "标题栏" in asked[0]
    finally:
        dlg.deleteLater()


def test_custom_model_added_in_settings_shows_up_and_button_refreshes(ui, qt_app, tmp_path, monkeypatch):
    dlg, _, _ = _settings(monkeypatch, tmp_path, {})
    dlg._custom_models = [{"name": "我的模型", "model_id": "mine", "protocol": "openai",
                           "api_key": "sk-1", "base_url": "https://x.invalid/v1"}]
    dlg._save()
    try:
        names = [m[0] for m in models.MODEL_LIST]
        assert "⚙ 我的模型" in names
        # 当前会话的模型还是原来那个（下标对回去了），按钮写的也还是它
        cur = models.MODEL_LIST[session.get_active().current_model_index][0]
        ui._on_config_applied(dlg.applied_result, from_settings=True)
        assert ui.model_btn.text().startswith(cur[:8])
    finally:
        dlg.deleteLater()


def test_hand_edited_config_file_is_picked_up(ui, qt_app):
    """手改 config.json：文件监视看到变化，等一会儿重读，改动生效并提示。"""
    import time
    from src import paths
    path = paths.CONFIG_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"deepseek_api_key": "sk-hand-edit"}, f)
        deadline = time.monotonic() + 5
        while config.DEEPSEEK_API_KEY != "sk-hand-edit" and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.02)
        assert config.DEEPSEEK_API_KEY == "sk-hand-edit"
        assert ui.toasts[-1] == "config.json 的改动已生效"

        # 写坏了：保持原配置，告诉用户没生效
        time.sleep(0.05)   # mtime 分辨率
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"deepseek_api_key": ')
        deadline = time.monotonic() + 5
        while "格式不对" not in (ui.toasts[-1] if ui.toasts else "") and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.02)
        assert "格式不对" in ui.toasts[-1]
        assert config.DEEPSEEK_API_KEY == "sk-hand-edit"
    finally:
        if os.path.exists(path):
            os.remove(path)
        for _ in range(5):
            qt_app.processEvents()
