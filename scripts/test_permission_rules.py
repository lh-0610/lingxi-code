"""持久化权限规则：确认卡上「允许并记住」跨会话、跨重启生效，且绕不过危险判定。

分三层测：
- 存储层（permission_rules）：落盘 / 去重 / 范围 / 规范化 / 损坏与瞬时错误的失败方向
- 确认层（confirm_command / confirm_edit）：新会话照样放行；危险命令、脚本改文件、
  Git 写、派子 Agent、"(patch)" 一律仍然问
- 界面层（设置 → 权限）：列得出、撤销后落盘规则和会话内存白名单一起清掉
"""
import builtins
import json
from types import SimpleNamespace

import pytest

from src import config, permission_modes as pm, permission_rules as rules, session
from src.ui.confirm_bars import ConfirmBarsMixin


class _Host(ConfirmBarsMixin):
    """假宿主：要弹卡时记下"问了"并直接拒绝，不卡住等人点（同 test_permission_modes）。"""

    def __init__(self):
        self.asked = []
        host = self

        class _Signal:
            def emit(self, *args):
                result, done = args[-2], args[-1]
                host.asked.append(args[0])
                result["allow"] = False
                done.set()

        self.bridge = SimpleNamespace(confirm_request=_Signal(), edit_confirm_request=_Signal())


@pytest.fixture
def sess(monkeypatch):
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    s = session.Session()
    session.bind_thread(s)
    yield s
    session.unbind_thread()


def _asked_command(command, **kw):
    host = _Host()
    allowed, _ = host.confirm_command(command, **kw)
    return allowed and not host.asked


def _asked_edit(path):
    host = _Host()
    allowed, _ = host.confirm_edit(path, "--- a\n+++ b\n")
    return allowed and not host.asked


# ── 存储层 ────────────────────────────────────────────────────────

def test_rule_survives_a_restart(sess):
    """落盘的意义：换一个全新 Session（等价于重启后新开会话）仍然放行。"""
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    assert _asked_command("git status")
    session.unbind_thread()
    fresh = session.Session()
    session.bind_thread(fresh)
    assert not fresh.command_prefix_allowlist, "新会话内存里是空的"
    assert _asked_command("git log --oneline"), "靠落盘规则放行，不是靠内存白名单"


def test_dedupe_same_kind_value_scope():
    assert rules.add_rule(rules.KIND_COMMAND, "git", None) is not None
    assert rules.add_rule(rules.KIND_COMMAND, "git", None) is None
    assert len(rules.list_rules()) == 1


def test_project_rule_does_not_leak_to_another_project(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    rules.add_rule(rules.KIND_COMMAND, "git", a)
    assert rules.allows_command("git", a)
    assert not rules.allows_command("git", b), "别的项目没授权过"
    assert not rules.allows_command("git", None), "无项目也不该吃到项目级规则"


def test_global_rule_applies_everywhere(tmp_path):
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    assert rules.allows_command("git", str(tmp_path / "any"))
    assert rules.allows_command("git", None)


def test_edit_path_normalized_once(tmp_path):
    """相对 / 绝对、大小写、重复分隔符都算同一条规则，不攒出一堆重复条目。"""
    target = tmp_path / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    assert rules.add_rule(rules.KIND_EDIT_PATH, str(target), None) is not None
    assert rules.add_rule(rules.KIND_EDIT_PATH, str(target).upper(), None) is None
    assert len(rules.list_rules()) == 1
    assert rules.allows_edit(str(target), None)


def test_mcp_rule_is_per_tool():
    rules.add_rule(rules.KIND_COMMAND, "mcp:fetch", None)
    assert rules.allows_command("mcp:fetch", None)
    assert not rules.allows_command("mcp:write_file", None), "记住一个 MCP 工具不等于记住全部"


def test_remove_rule_stops_allowing():
    rule = rules.add_rule(rules.KIND_COMMAND, "git", None)
    assert rules.remove_rule(rule["id"])["value"] == "git"
    assert not rules.allows_command("git", None)
    assert rules.remove_rule(rule["id"]) is None, "重复撤销不报错、返回 None"


def test_untrustable_values_are_never_stored():
    assert rules.add_rule(rules.KIND_EDIT_PATH, "(patch)", None) is None
    assert rules.add_rule(rules.KIND_COMMAND, "", None) is None
    assert rules.add_rule("bogus", "x", None) is None
    assert rules.list_rules() == []


def test_corrupt_file_fails_closed_and_can_be_rebuilt(isolated_memory):
    path = rules._paths.permission_rules_file()
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ 这不是 JSON")
    assert rules.list_rules() == []
    assert not rules.allows_command("git", None), "读不懂就当没授权，退回问用户"
    assert rules.add_rule(rules.KIND_COMMAND, "git", None) is not None
    assert rules.allows_command("git", None)


def test_transient_read_error_never_wipes_the_store(isolated_memory, monkeypatch):
    """文件被占用这类瞬时错误：本次按无规则处理（问用户），且绝不把空结构写回去。"""
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    path = rules._paths.permission_rules_file()
    before = open(path, encoding="utf-8").read()

    real_open = builtins.open

    def locked(target, *a, **k):
        if str(target) == path:
            raise OSError("文件被占用")
        return real_open(target, *a, **k)

    monkeypatch.setattr(builtins, "open", locked)
    assert not rules.allows_command("git", None)
    assert not rules.allows_edit("C:/p/a.py", None)
    assert rules.list_rules() == []
    assert rules.add_rule(rules.KIND_COMMAND, "pytest", None) is None, "读失败时不写盘"
    assert rules.remove_rule("whatever") is None
    monkeypatch.undo()
    assert open(path, encoding="utf-8").read() == before, "原有规则一条都没丢"


# ── 确认层：放行 ──────────────────────────────────────────────────

def test_persisted_command_rule_allows_in_manual_mode(sess):
    pm.apply(sess, "manual")
    rules.add_rule(rules.KIND_COMMAND, "pytest", None)
    assert _asked_command("pytest -k foo")
    assert not _asked_command("python -m pytest"), "只记住了 pytest，没记住 python"


def test_persisted_edit_rule_allows(sess, tmp_path):
    pm.apply(sess, "manual")
    target = tmp_path / "a.py"
    rules.add_rule(rules.KIND_EDIT_PATH, str(target), None)
    assert _asked_edit(str(target))
    assert not _asked_edit(str(tmp_path / "b.py"))


def test_rule_scope_follows_the_confirming_session(tmp_path):
    """后台会话的确认在主线程处理：范围要按发起确认的那个会话的项目算，不是前台的。"""
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    rules.add_rule(rules.KIND_COMMAND, "git", a)
    foreground = session.Session()
    foreground.project = b
    session.set_active(foreground)
    worker = session.Session()
    worker.project = a
    session.bind_thread(worker)
    try:
        assert _asked_command("git status"), "发起会话在 a 项目，规则生效"
    finally:
        session.unbind_thread()
    session.bind_thread(foreground)
    try:
        assert not _asked_command("git status"), "前台在 b 项目，不吃 a 的规则"
    finally:
        session.unbind_thread()


# ── 确认层：必须仍然问（负向）──────────────────────────────────────

@pytest.mark.parametrize("command, remembered", [
    ("rm -rf build", "rm"),                       # 危险命令
    ("del /s /q build", "del"),
    ("git reset --hard HEAD", "git"),
    ("cd sub && rm -rf /", "cd"),                 # 信任 cd 不能绕过后面接的危险操作
])
def test_destructive_command_ignores_persisted_rule(sess, command, remembered):
    pm.apply(sess, "manual")
    rules.add_rule(rules.KIND_COMMAND, remembered, None)
    assert not _asked_command(command), "落盘规则不能成为危险命令的后门"


def test_script_that_writes_files_ignores_persisted_rule(sess):
    pm.apply(sess, "manual")
    rules.add_rule(rules.KIND_COMMAND, "python", None)
    assert not _asked_command('python -c "open(\'x.txt\', \'w\').write(\'y\')"'), \
        "跑会写文件的脚本每次都问，记住了 python 也不行"
    assert _asked_command("python --version"), "不写文件的 python 命令照样放行"


@pytest.mark.parametrize("kind, command, tool", [
    (pm.KIND_GIT_WRITE, "将执行 Git 写操作：git commit -m x", ""),
    (pm.KIND_SPAWN, "将派出并行子 Agent", ""),
])
def test_always_ask_kinds_ignore_persisted_rule(sess, kind, command, tool):
    pm.apply(sess, "manual")
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    rules.add_rule(rules.KIND_COMMAND, "将派出并行子", None)
    assert not _asked_command(command, kind=kind, tool=tool)


def test_mcp_rule_is_per_tool_in_auto_mode(sess):
    """MCP 的语义与会话白名单一致：记住过的那一个放行，没记住的在自动模式下仍然问。"""
    pm.apply(sess, "auto")
    rules.add_rule(rules.KIND_COMMAND, "mcp:fetch", None)
    assert _asked_command("将调用 MCP 工具 fetch", kind=pm.KIND_MCP, tool="fetch")
    assert not _asked_command("将调用 MCP 工具 delete_repo", kind=pm.KIND_MCP, tool="delete_repo"), \
        "记住一个 MCP 工具不等于记住全部"


def test_unremembered_mcp_still_asks_in_auto_mode(sess):
    pm.apply(sess, "auto")
    assert not _asked_command("将调用 MCP 工具 fetch", kind=pm.KIND_MCP, tool="fetch")


def test_patch_placeholder_never_allowed_even_if_a_rule_exists(sess):
    pm.apply(sess, "manual")
    # 绕过 add_rule 的校验直接塞一条脏规则（旧文件 / 手改），确认层仍要挡住
    path = rules._paths.permission_rules_file()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rules": [{"id": "x1", "kind": rules.KIND_EDIT_PATH, "value": "(patch)",
                              "scope": "global", "project": "", "created": ""}]}, f)
    assert not _asked_edit("(patch)")


def test_empty_remember_key_never_matches():
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    assert not rules.allows_command("", None)
    assert not rules.allows_edit("", None)


def test_bypass_mode_still_allows_everything(sess):
    pm.apply(sess, "bypass")
    assert _asked_command("rm -rf build")
    assert _asked_edit("(patch)")


# ── 界面层：设置 → 权限 ───────────────────────────────────────────

@pytest.fixture
def settings_dialog(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    yield dlg
    dlg.deleteLater()
    app.processEvents()


def _revoke_buttons(dlg):
    from PySide6.QtWidgets import QPushButton
    return [b for b in dlg._rules_container.findChildren(QPushButton) if b.text() == "撤销"]


def test_rules_tab_lists_rules_and_revokes_one(settings_dialog, sess):
    from PySide6.QtWidgets import QLabel
    session.set_active(sess)      # 撤销要能清到当前打开的会话（未存盘的新会话就是 active）
    rules.add_rule(rules.KIND_COMMAND, "git", None)
    rules.add_rule(rules.KIND_EDIT_PATH, "C:/proj/a.py", None)
    sess.command_prefix_allowlist.add("git")
    sess.edit_path_allowlist.add("C:/proj/a.py")
    settings_dialog._refresh_rules_ui()

    buttons = _revoke_buttons(settings_dialog)
    assert len(buttons) == 2, "两条规则各有一个撤销按钮"
    labels = [w.text() for w in settings_dialog._rules_container.findChildren(QLabel)]
    assert "git" in labels and "全局" in labels

    buttons[0].click()
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)   # 行控件走 deleteLater，同项目其它 UI 测试
    remaining = rules.list_rules()
    assert len(remaining) == 1
    # 撤销立即生效：落盘规则和本次运行中会话的内存白名单一起清掉
    gone = "git" if remaining[0]["kind"] == rules.KIND_EDIT_PATH else "C:/proj/a.py"
    if gone == "git":
        assert "git" not in sess.command_prefix_allowlist
        assert not rules.allows_command("git", None)
    else:
        assert not sess.edit_path_allowlist
        assert not rules.allows_edit("C:/proj/a.py", None)
    assert len(_revoke_buttons(settings_dialog)) == 1, "列表跟着刷新"


def test_rules_tab_empty_state(settings_dialog):
    settings_dialog._refresh_rules_ui()
    assert _revoke_buttons(settings_dialog) == []
    from PySide6.QtWidgets import QLabel
    texts = [w.text() for w in settings_dialog._rules_container.findChildren(QLabel)]
    assert any("还没有记住任何权限" in t for t in texts)


# ── 端到端：真实卡片上点「允许并记住」─────────────────────────────

@pytest.fixture
def real_ui(monkeypatch, isolated_memory):
    """真 ChatUI（offscreen），模型和网络一律禁掉——只测确认这条路径。"""
    import socket
    from unittest.mock import Mock

    def _forbidden(*a, **k):
        raise AssertionError("确认测试里不许访问模型 / 网络")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import agent, mcp_client, models, state, tools
    fake = Mock()
    fake.bind_tools.return_value = fake
    fake.invoke.side_effect = _forbidden
    fake.stream.side_effect = _forbidden
    monkeypatch.setattr(models, "_create_llm", lambda *a: fake)
    monkeypatch.setattr(agent, "_create_llm", lambda *a: fake)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    try:
        yield ui, app
    finally:
        ui._release_pending_confirm()
        ui._release_pending_edit()
        ui.hide()
        ui.deleteLater()
        app.processEvents()


def test_clicking_remember_persists_and_spares_the_next_session(real_ui, isolated_memory):
    import threading
    import time

    from src import paths

    ui, app = real_ui
    sess = session.get_active()
    pm.apply(sess, "manual")
    out = []

    def worker():
        paths.set_data_dir(str(isolated_memory.parent))
        session.bind_thread(sess)
        try:
            out.append(ui.confirm_command("git status"))
        finally:
            session.unbind_thread()
            paths.set_data_dir(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while ui._command_confirm_result_holder is None:
        app.processEvents()
        assert time.monotonic() < deadline, "确认卡没弹出来"
    assert ui._cmd_remember_btn.isVisible(), "普通命令要给「允许并记住」"
    ui._cmd_remember_btn.click()
    t.join(5)
    assert not t.is_alive()
    assert out and out[0][0] is True
    assert rules.allows_command("git", None), "点了记住就该落盘"

    # 换一个全新会话（等价于重启后新开对话）：不再弹卡，直接放行
    fresh = session.Session()
    session.set_active(fresh)
    allowed, _ = ui.confirm_command("git log --oneline")
    assert allowed and ui._command_confirm_result_holder is None
    assert not ui.command_confirm_bar.isVisible()


def test_dangerous_command_card_has_no_remember_button(real_ui, isolated_memory):
    import threading
    import time

    from src import paths

    ui, app = real_ui
    sess = session.get_active()
    pm.apply(sess, "manual")
    out = []

    def worker():
        paths.set_data_dir(str(isolated_memory.parent))
        session.bind_thread(sess)
        try:
            out.append(ui.confirm_command("rm -rf build"))
        finally:
            session.unbind_thread()
            paths.set_data_dir(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while ui._command_confirm_result_holder is None:
        app.processEvents()
        assert time.monotonic() < deadline
    assert not ui._cmd_remember_btn.isVisible(), "危险命令不给记住，落盘规则也就无从产生"
    ui._cmd_deny_btn.click()
    t.join(5)
    assert rules.list_rules() == []