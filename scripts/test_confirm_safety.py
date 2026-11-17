"""确认卡安全逻辑测试：危险命令判定 / base 命令提取 / 命令规范化 / 浏览器来源记住。

这些是 run_command 确认的安全核心——_is_destructive_command 决定危险命令
**不给"记住"选项**（防 AI 被永久授权后 rm -rf）。务必覆盖危险/安全/绕过用例。
静态方法直接调用，不需 Qt 实例；_resolve_command_confirm 用桩控件宿主测（不拉窗口）。
"""
import threading
from types import SimpleNamespace

import pytest

from src import config, memory, permission_modes as pm, permission_rules as rules
from src import session, telegram_push
from src.ui import confirm_bars as _cb
from src.ui.confirm_bars import ConfirmBarsMixin

_is_destructive = ConfirmBarsMixin._is_destructive_command
_extract_base = ConfirmBarsMixin._extract_base_command
_normalize = ConfirmBarsMixin._normalize_command


class TestNormalizeCommand:
    def test_collapses_whitespace(self):
        assert _normalize("  git   status  ") == "git status"

    def test_already_clean(self):
        assert _normalize("git status") == "git status"

    def test_empty(self):
        assert _normalize("") == ""

    def test_none(self):
        assert _normalize(None) == ""

    def test_newlines_and_tabs(self):
        assert _normalize("git\t status\n--short") == "git status --short"


class TestExtractBaseCommand:
    def test_simple(self):
        assert _extract_base("git status --short") == "git"

    def test_leading_whitespace(self):
        assert _extract_base("  python  -m pytest") == "python"

    def test_empty(self):
        assert _extract_base("") == ""

    def test_none(self):
        assert _extract_base(None) == ""

    def test_unix_path_prefix_basename(self):
        # 带路径前缀取 basename，不同安装路径的同一工具匹配同一前缀
        assert _extract_base("/usr/bin/git status") == "git"

    def test_lowercased(self):
        assert _extract_base("GIT status") == "git"

    def test_compound_returns_first(self):
        # "cd foo && git status" → "cd"（有意为之：不让"信任 cd"绕过后面的危险操作）
        assert _extract_base("cd foo && git status") == "cd"


class TestIsDestructiveCommand:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -fr foo",
        "rm -r dir",
        "rm --recursive bar",
        "rm --force baz",
        "sudo rm foo",
        "del /s /q C:\\temp",
        "del /f foo",
        "rmdir /s foo",
        "rmdir /q foo",
        "Remove-Item -Recurse foo",
        "Remove-Item -Force foo",
        "format C:",
        "format d:",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "sudo apt install x",
        "runas /user:admin cmd",
        "shutdown -h now",
        "reboot",
        "chmod 777 /etc/passwd",
        "echo x > /dev/sda",
        "DROP TABLE users",
        "drop database mydb",
        "TRUNCATE TABLE logs",
    ])
    def test_dangerous_true(self, cmd):
        assert _is_destructive(cmd) is True, f"应判为危险: {cmd}"

    @pytest.mark.parametrize("cmd", [
        "git status",
        "ls -la",
        "rm foo.txt",                          # 无 -r/-f → 不算危险
        "python script.py",
        "npm install",
        "echo hello",
        "pytest -q",
        "cat README.md",
        "format the string",                   # format 后不是盘符 → 不误判
        "git commit -m 'drop the old code'",   # drop 后非 table/database/schema
        "",
    ])
    def test_safe_false(self, cmd):
        assert _is_destructive(cmd) is False, f"应判为安全: {cmd}"

    def test_none(self):
        assert _is_destructive(None) is False

    def test_sql_comment_bypass_blocked(self):
        # 防注释绕过：/* */ 注释剥掉后仍能识别 DROP TABLE
        assert _is_destructive("/* harmless */ DROP TABLE x") is True


# ── 浏览器来源"记住"：只进本会话、不落盘、有后果不给记（§6.2，第 5 步）──

_ORIGIN = "http://localhost:5173"
_BROWSER_CARD = "将在浏览器点击 button「添加一行」（ref=e20）\n页面: http://localhost:5173/"


class _ResolveHost(ConfirmBarsMixin):
    """_resolve_command_confirm 的最小宿主：控件用桩，不拉 Qt 窗口。"""

    def __init__(self, holder):
        self._command_confirm_result_holder = holder
        self._command_confirm_done_event = threading.Event()
        self._command_confirm_destructive = bool(holder.get("destructive"))
        self._command_confirm_script_write = bool(holder.get("script_write"))
        self._command_confirm_remember_key = holder.get("remember_key")
        self._command_confirm_kind = holder.get("kind")
        self._command_confirm_browser_origin = holder.get("browser_origin", "")
        self._command_confirm_browser_action = holder.get("browser_action", "")
        self.command_confirm_feedback = SimpleNamespace(text=lambda: "")
        self.command_confirm_bar = SimpleNamespace(setVisible=lambda v: None)
        self.command_confirm_text = SimpleNamespace(
            clear=lambda: None, toPlainText=lambda: "")


def _browser_holder(sess, consequential=False):
    return {"_session": sess, "kind": pm.KIND_BROWSER, "destructive": consequential,
            "script_write": False, "remember_key": "", "browser_origin": _ORIGIN}


@pytest.fixture
def no_persist(monkeypatch):
    """browser 记住绝不落盘：谁调 _remember_persistently 谁就炸。"""
    def _boom(*a, **k):
        raise AssertionError("browser 来源记住不该调 _remember_persistently")
    monkeypatch.setattr(_cb, "_remember_persistently", _boom)


class TestBrowserRememberSessionOnly:
    def test_remember_goes_only_to_session_allowlist(self, no_persist):
        sess = session.Session()
        holder = _browser_holder(sess)
        host = _ResolveHost(holder)
        done = host._command_confirm_done_event
        host._resolve_command_confirm(True, remember=True)
        assert sess.browser_origin_allowlist == {_ORIGIN}
        assert sess.command_prefix_allowlist == set(), "不走命令前缀那套"
        assert holder["allow"] is True
        assert done.is_set()

    def test_consequential_forced_remember_not_whitelisted(self, no_persist):
        """有后果动作 destructive=True：按钮虽藏，数字键 3 / 强行 remember 也进不了白名单。"""
        sess = session.Session()
        holder = _browser_holder(sess, consequential=True)
        host = _ResolveHost(holder)
        host._resolve_command_confirm(True, remember=True)
        assert sess.browser_origin_allowlist == set()
        assert holder["allow"] is True, "动作本身允许，只是不记住"

    def test_allow_without_remember_adds_nothing(self, no_persist):
        sess = session.Session()
        host = _ResolveHost(_browser_holder(sess))
        host._resolve_command_confirm(True, remember=False)
        assert sess.browser_origin_allowlist == set()

    def test_new_session_does_not_inherit(self, no_persist):
        sess = session.Session()
        _ResolveHost(_browser_holder(sess))._resolve_command_confirm(True, remember=True)
        assert session.Session().browser_origin_allowlist == set()

    def test_save_and_load_does_not_inherit(self, no_persist, isolated_memory):
        from langchain_core.messages import AIMessage, HumanMessage
        sess = session.Session()
        sess.current_session_id = "browser-allowlist"
        sess.chat_history = [HumanMessage(content="你好"), AIMessage(content="在的")]
        _ResolveHost(_browser_holder(sess))._resolve_command_confirm(True, remember=True)
        assert sess.browser_origin_allowlist == {_ORIGIN}
        memory.save_session(session=sess)
        back = session.Session()
        assert memory.load_session(sess.current_session_id, session=back)
        assert back.browser_origin_allowlist == set(), "来源信任不落盘，重开不继承"

    def test_open_remember_goes_to_open_allowlist_only(self, no_persist):
        """打开卡上的"记住"只免后续打开：进 browser_open_allowlist，不进操作白名单。"""
        sess = session.Session()
        holder = _browser_holder(sess)
        holder["browser_action"] = "open"
        _ResolveHost(holder)._resolve_command_confirm(True, remember=True)
        assert sess.browser_open_allowlist == {_ORIGIN}
        assert sess.browser_origin_allowlist == set()

    def test_interact_remember_goes_to_origin_allowlist(self, no_persist):
        sess = session.Session()
        holder = _browser_holder(sess)
        holder["browser_action"] = "interact"
        _ResolveHost(holder)._resolve_command_confirm(True, remember=True)
        assert sess.browser_origin_allowlist == {_ORIGIN}
        assert sess.browser_open_allowlist == set()

    def test_open_allowlist_not_persisted(self, no_persist, isolated_memory):
        from langchain_core.messages import AIMessage, HumanMessage
        sess = session.Session()
        sess.current_session_id = "browser-open-allowlist"
        sess.chat_history = [HumanMessage(content="你好"), AIMessage(content="在的")]
        holder = _browser_holder(sess)
        holder["browser_action"] = "open"
        _ResolveHost(holder)._resolve_command_confirm(True, remember=True)
        memory.save_session(session=sess)
        back = session.Session()
        assert memory.load_session(sess.current_session_id, session=back)
        assert back.browser_open_allowlist == set()
        assert back.browser_origin_allowlist == set()

    def test_command_remember_still_persists(self, monkeypatch):
        """回归守护：命令路径的"记住"照常进会话前缀白名单 + 落盘，没被 browser 分支改坏。"""
        recorded = []
        monkeypatch.setattr(_cb, "_remember_persistently",
                            lambda kind, value, s: recorded.append((kind, value)))
        sess = session.Session()
        holder = {"_session": sess, "kind": pm.KIND_COMMAND, "destructive": False,
                  "script_write": False, "remember_key": "git", "browser_origin": ""}
        _ResolveHost(holder)._resolve_command_confirm(True, remember=True)
        assert sess.command_prefix_allowlist == {"git"}
        assert recorded == [(rules.KIND_COMMAND, "git")]
        assert sess.browser_origin_allowlist == set()


class TestBrowserRemoteRemember:
    """手机端点"记住同类"的补加分支：同样只进会话白名单。"""

    @pytest.fixture
    def remote(self, monkeypatch):
        monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", True)
        pushed = []
        monkeypatch.setattr(telegram_push, "push_confirm",
                            lambda text, cid, is_destructive=False: pushed.append(is_destructive) or 123)
        monkeypatch.setattr(telegram_push, "edit_message_text", lambda *a, **k: None)
        sess = session.Session()
        session.bind_thread(sess)
        host = _ResolveHost.__new__(_ResolveHost)   # 只要 confirm_command，不装桩控件
        host.asked = []

        class _Signal:
            def emit(self, command, result, done):
                result["allow"] = True
                result["remember"] = True        # 模拟手机端点了"记住同类"
                result["by_remote"] = True
                done.set()
        host.bridge = SimpleNamespace(confirm_request=_Signal())
        yield SimpleNamespace(sess=sess, host=host, pushed=pushed)
        session.unbind_thread()

    def test_remote_remember_session_only(self, remote, no_persist):
        allowed, _ = remote.host.confirm_command(
            _BROWSER_CARD, kind=pm.KIND_BROWSER, browser_origin=_ORIGIN)
        assert allowed is True
        assert remote.sess.browser_origin_allowlist == {_ORIGIN}
        assert remote.pushed == [False], "普通动作手机端给记住按钮"

    def test_remote_open_remember_session_only(self, remote, no_persist):
        allowed, _ = remote.host.confirm_command(
            _BROWSER_CARD, kind=pm.KIND_BROWSER, browser_origin=_ORIGIN,
            browser_action="open")
        assert allowed is True
        assert remote.sess.browser_open_allowlist == {_ORIGIN}
        assert remote.sess.browser_origin_allowlist == set()

    def test_remote_consequential_no_remember_button_and_forced_remember_ignored(
            self, remote, no_persist):
        allowed, _ = remote.host.confirm_command(
            _BROWSER_CARD, kind=pm.KIND_BROWSER, browser_origin=_ORIGIN,
            browser_consequential=True)
        assert allowed is True
        assert remote.pushed == [True], "有后果手机端藏记住按钮"
        assert remote.sess.browser_origin_allowlist == set(), "强行 remember 也进不了白名单"
