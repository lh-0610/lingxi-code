"""输入框底栏的模型按钮（原顶栏的模型下拉 + 「思考」开关）。

Qt 部分 offscreen；菜单的 exec 换成只记录、不阻塞，模型 / MCP / 网络离线。
模型清单用一份固定的 4 项：Claude Code（不用 Key）、一个缺 Key 的 deepseek、
一个本地 ollama（只能开关思考）、一个 Responses 模型（可选四档）。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest

from src import agent, models, session, state
from src.ui import ChatUI

# 下标 → 名字 / 用途
CLAUDE_CODE = 0      # 不需要 Key，不支持思考
NO_KEY_DEEPSEEK = 1  # 缺 Key → 进「更多模型」
LOCAL_OLLAMA = 2     # 本地模型，不需要 Key，思考只有 关 / 开
RESPONSES = 3        # 四档：关 / 低 / 高 / 最高

MODELS = [
    ("Claude Code", "claude-code", "claude", False),
    ("deepseek-flash", "deepseek", "deepseek-flash", False),
    ("qwen3.5:latest", "ollama", "qwen3.5:latest", True),
    ("deepseek-v4.1-flash", "responses", "deepseek-flash", True),
]


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


@pytest.fixture()
def model_list(monkeypatch):
    """固定模型清单；Responses 那条填好 key（其余缺 key 的走「更多模型」）。"""
    monkeypatch.setattr(models, "MODEL_LIST", MODELS)
    monkeypatch.setattr(agent, "MODEL_LIST", MODELS)
    monkeypatch.setattr(models, "RESPONSES_API_KEY", "sk-test", raising=False)
    return MODELS


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory, model_list):
    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in model menu tests")

    from src import mcp_client, tools
    # 建 LLM 一律失败（_activate_llm 会吞掉并置空），测试里既不联网也不拼工具
    monkeypatch.setattr(agent, "_create_llm", _forbidden)
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(models, "_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    window.resize(1440, 900)
    window.show()
    _pump(qapp)
    yield window
    import shiboken6
    if shiboken6.isValid(window):
        if state.ui_ref is window:
            state.ui_ref = None
        window.close()
        qapp.processEvents()
        window.deleteLater()
        gc.collect()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


@pytest.fixture()
def opened(monkeypatch):
    """打开的菜单：exec 不阻塞，只把菜单留下来给用例看。"""
    from src.ui.widgets import NumberedMenu
    menus = []
    monkeypatch.setattr(NumberedMenu, "exec", lambda self, *a, **k: menus.append(self))
    return menus


def _items(menu):
    from src.ui.widgets import DescribedMenuItem
    return [w for w in (getattr(a, "defaultWidget", None) and a.defaultWidget()
                        for a in menu.actions())
            if isinstance(w, DescribedMenuItem)]


def _titles(menu):
    from PySide6.QtWidgets import QLabel
    return [r.findChild(QLabel, "itemTitle").text() for r in _items(menu)]


def _trailing(menu):
    from PySide6.QtWidgets import QLabel
    return [r.findChild(QLabel, "itemTrailing").text() for r in _items(menu)]


def _checked(menu):
    from PySide6.QtWidgets import QLabel
    return [r.findChild(QLabel, "itemTitle").text() for r in _items(menu)
            if r.findChild(QLabel, "itemCheck")]


def _set_current(ui, index, effort="high"):
    session.get_active().current_model_index = index
    session.get_active().reasoning_effort = effort
    ui._refresh_model_btn()


def test_button_sits_left_of_mode_and_header_dropped_both_widgets(ui):
    bottom = ui._composer_bottom
    widgets = [bottom.itemAt(i).widget() for i in range(bottom.count())]
    assert widgets.index(ui.model_btn) == widgets.index(ui.mode_btn) - 1
    assert not hasattr(ui, "model_combo") and not hasattr(ui, "think_btn")


def test_button_shows_model_and_effort_only_when_supported(ui):
    _set_current(ui, CLAUDE_CODE)
    assert ui.model_btn.text() == "Claude Code", "不支持思考的只显示模型名"
    _set_current(ui, RESPONSES, "high")
    assert ui.model_btn.text() == "deepseek-v4.1-flash · 高"
    _set_current(ui, LOCAL_OLLAMA, "low")
    assert ui.model_btn.text() == "qwen3.5:latest · 开", "只能开关的模型按 开 / 关 显示"


def test_menu_lists_usable_models_with_numbers_and_a_check(ui, opened):
    _set_current(ui, LOCAL_OLLAMA)
    ui._show_model_menu()
    menu = opened[-1]
    # 缺 Key 的 deepseek 不在主菜单里（它进「更多模型」）
    assert _titles(menu) == ["Claude Code", "qwen3.5:latest", "deepseek-v4.1-flash",
                             "思考强度", "更多模型"]
    assert _trailing(menu) == ["1", "2", "3", "›", "›"]
    assert _checked(menu) == ["qwen3.5:latest"]


def test_number_key_picks_a_model(ui, qapp, opened):
    _set_current(ui, CLAUDE_CODE)
    ui._show_model_menu()
    QTest.keyClick(opened[-1], Qt.Key_3)
    _pump(qapp)
    assert session.get_active().current_model_index == RESPONSES
    assert session.get_active().model_user_choice is True, "亲手选的模型要记下来"
    assert ui.model_btn.text() == "deepseek-v4.1-flash · 高"


def test_effort_submenu_lists_supported_levels_and_applies_the_choice(ui, qapp, opened):
    _set_current(ui, RESPONSES, "low")
    ui._open_effort_menu()
    menu = opened[-1]
    assert _titles(menu) == ["关", "低", "高", "最高"]
    assert _trailing(menu) == ["1", "2", "3", "4"]
    assert _checked(menu) == ["低"]
    QTest.keyClick(menu, Qt.Key_4)
    _pump(qapp)
    assert session.get_active().reasoning_effort == "max"
    assert ui.model_btn.text() == "deepseek-v4.1-flash · 最高"


def test_only_switchable_model_gets_off_and_on(ui, opened):
    _set_current(ui, LOCAL_OLLAMA, "high")
    ui._open_effort_menu()
    menu = opened[-1]
    assert _titles(menu) == ["关", "开"]
    assert _checked(menu) == ["开"]


def test_no_effort_item_for_a_model_without_thinking(ui, opened):
    _set_current(ui, CLAUDE_CODE)
    ui._show_model_menu()
    assert "思考强度" not in _titles(opened[-1]), "不支持思考就没有这一项"


def test_more_models_lists_the_ones_missing_keys_and_only_explains(ui, opened, monkeypatch):
    toasts = []
    monkeypatch.setattr(ChatUI, "_show_toast", lambda self, text, *a, **k: toasts.append(text))
    _set_current(ui, CLAUDE_CODE)
    ui._open_more_models_menu()
    menu = opened[-1]
    assert _titles(menu) == ["deepseek-flash"]
    assert "deepseek_api_key" in _items(menu)[0].findChild(
        __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel, "itemDesc").text()

    _items(menu)[0].action.trigger()
    assert session.get_active().current_model_index == CLAUDE_CODE, "缺 Key 的模型不切换过去"
    assert toasts and "设置" in toasts[-1]


def test_switching_while_generating_stops_the_run_first(ui, monkeypatch):
    stops = []
    monkeypatch.setattr(ChatUI, "_force_stop_generation", lambda self, *a, **k: stops.append(1))
    session.get_active().is_generating = True
    _set_current(ui, CLAUDE_CODE)
    ui._choose_model(RESPONSES)
    assert stops == [1], "生成中切模型先把这一轮停掉"
    assert session.get_active().current_model_index == RESPONSES


def test_switch_to_a_model_without_thinking_turns_effort_off(ui):
    _set_current(ui, RESPONSES, "max")
    ui._choose_model(CLAUDE_CODE)
    assert session.get_active().reasoning_effort == "off"
    assert ui.model_btn.text() == "Claude Code"


def test_button_follows_the_active_session(ui):
    other = session.Session()
    other.current_model_index = LOCAL_OLLAMA
    other.reasoning_effort = "off"
    session.register(other)
    session.set_active(other)
    ui._sync_header_from_session()
    assert ui.model_btn.text() == "qwen3.5:latest · 关"
    assert other.model_user_choice is False, "切会话的同步不算用户亲手选模型"
