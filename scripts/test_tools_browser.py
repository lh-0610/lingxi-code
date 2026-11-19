"""浏览器工具单测：不真开页面——monkeypatch _bridge_call 喂假桥响应（仿 test_web_fetch.py）。

闸门（模式×动作×来源）/ 上限 / 不可信围栏 / 动作 diff / 密钥防外泄都在工具层测；
真实 WebEngine 往返在 test_browser_bridge.py。
"""
from types import SimpleNamespace

import pytest

from src import config, permission_modes as pm, session, state
from src import tools_browser as tb
from src.browser import page, tree

_LOCAL = "http://localhost:5173/"
_PUBLIC = "https://example.com/"


class FakeUI:
    def __init__(self, allow=True, feedback=""):
        self.allow = allow
        self.feedback = feedback
        self.asked = []
        self.kwargs = []

    def confirm_command(self, text, kind="command", tool="", stdin="", **kw):
        self.asked.append((text, kind))
        self.kwargs.append(kw)
        return self.allow, self.feedback


class FakeBridge:
    """按 (op, lx method) 分发假响应，记下全部调用。"""

    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.snapshots = []      # all 模式快照队列（动作前/后各 pop 一份）

    def set(self, op, method, result, frame=None):
        key = (op, method) + ((frame,) if frame else ())
        self.handlers[key] = result

    def __call__(self, op, timeout=30, **args):
        self.calls.append((op, dict(args, timeout=timeout)))
        key = (op, args.get("method") if op == "lx" else None)
        frame = args.get("frame")
        h = self.handlers.get(key + (frame,)) if frame else None
        if h is None:
            h = self.handlers.get(key)
        if h is None:
            return {"ok": False, "error": f"无假响应: {key}"}
        return h(args.get("args", {})) if callable(h) else h

    def args_of(self, op, method=None):
        return [a for o, a in self.calls
                if o == op and (method is None or a.get("method") == method)]


def _setup_page(fake, url=_LOCAL, info=None):
    fake.set("lx", "state", {"ok": True, "url": url, "title": "T",
                             "readyState": "complete", "generation": 1})
    fake.set("qturl", None, {"ok": True, "url": url})
    fake.set("lx", "info", info or {
        "ok": True, "ref": "e20", "tag": "button", "type": "", "role": "button",
        "name": "添加一行", "inForm": False, "formMethod": "", "formAction": ""})
    fake.set("open", None, {"ok": True, "loaded": True, "url": url,
                            "title": "演示页", "loadMs": 12})
    fake.set("lx", "click", {"ok": True, "role": "button", "name": "添加一行"})
    fake.set("lx", "type", {"ok": True, "value": "你好"})
    fake.set("lx", "console", {"ok": True, "entries": [], "url": url})
    fake.set("console", None, {"ok": True, "entries": [], "url": url})
    fake.set("lx", "readText", {"ok": True, "chars": 4, "truncated": False,
                                "text": "正文内容", "source": "main", "url": url})
    fake.set("lx", "find", {"ok": True, "query": "保存", "count": 1, "url": url,
                            "matches": [{"ref": "e19", "role": "button",
                                         "name": "保存", "tag": "button"}]})
    fake.set("close", None, {"ok": True})

    def snap(args):
        if args.get("mode") == "all" and fake.snapshots:
            return fake.snapshots.pop(0)
        return {"ok": True, "generation": 1, "mode": args.get("mode", "interactive"),
                "url": url, "title": "演示页",
                "tree": '- main [ref=e6]\n  - button "添加一行" [ref=e20]',
                "stats": {"chars": 48, "lines": 2, "refs": 2,
                          "truncated": False, "jsMs": 1}}
    fake.set("lx", "snapshot", snap)


def _all_snap(text, url=_LOCAL):
    return {"ok": True, "generation": 1, "mode": "all", "url": url, "title": "演示页",
            "tree": text, "stats": {"chars": len(text), "lines": len(text.splitlines()),
                                    "refs": 0, "truncated": False, "jsMs": 1}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    s = session.Session()
    session.bind_thread(s)
    fake, ui = FakeBridge(), FakeUI()
    monkeypatch.setattr(tb, "_bridge_call", fake)
    monkeypatch.setattr(page, "_browser", object())      # 假装已有打开的页面
    monkeypatch.setattr(state, "ui_ref", ui)
    monkeypatch.setattr(state, "headless_auto_approve", False)
    _setup_page(fake)
    # 真实流程里 ref 来自快照签发；click/type 校验签发登记（直接登记，不产生桥调用）
    tb._remember_refs({"tree": '- main [ref=e6]\n  - button "添加一行" [ref=e20]',
                       "matches": [{"ref": "e9"}, {"ref": "e19"}], "docId": ""}, None)
    yield SimpleNamespace(sess=s, fake=fake, ui=ui)
    session.unbind_thread()


# ── browser_open ──────────────────────────────────────────────

class TestOpen:
    def test_rejects_non_http_before_any_call(self, env):
        out = tb.browser_open.func("file:///c:/windows/win.ini")
        assert "不支持的协议" in out and env.fake.calls == []

    def test_localhost_opens_without_asking_in_manual(self, env):
        pm.apply(env.sess, "manual")
        out = tb.browser_open.func(_LOCAL)
        assert not env.ui.asked
        assert "演示页" in out and "已打开" in out
        assert '<browser-content url="http://localhost:5173/" untrusted>' in out
        assert "[ref=e20]" in out, "首份交互快照要带 ref"
        assert env.fake.args_of("open")[0]["url"] == _LOCAL

    def test_public_asks_in_manual_and_refusal_stops(self, env):
        pm.apply(env.sess, "manual")
        env.ui.allow, env.ui.feedback = False, "先别开"
        out = tb.browser_open.func(_PUBLIC)
        assert "已拒绝" in out and "先别开" in out
        assert env.ui.asked and env.ui.asked[0][1] == pm.KIND_BROWSER
        assert not env.fake.args_of("open"), "拒绝了就不能真打开"

    def test_public_allowed_after_confirm(self, env):
        pm.apply(env.sess, "manual")
        out = tb.browser_open.func(_PUBLIC)
        assert len(env.ui.asked) == 1 and "已打开" in out

    @pytest.mark.parametrize("mode", ["auto", "bypass", "plan"])
    def test_public_open_no_ask_in_auto_bypass_plan(self, env, mode):
        pm.apply(env.sess, mode)
        assert "已打开" in tb.browser_open.func(_PUBLIC)
        assert not env.ui.asked

    def test_timeout_clamped_and_passed(self, env):
        tb.browser_open.func(_LOCAL, timeout=1)
        assert env.fake.args_of("open")[0]["timeout"] == 5


# ── 只读四件套：不弹卡、围栏、上限 ─────────────────────────────

class TestReadonly:
    def test_snapshot_never_asks_even_public_manual(self, env):
        _setup_page(env.fake, url=_PUBLIC)
        pm.apply(env.sess, "manual")
        out = tb.browser_snapshot.func()
        assert not env.ui.asked
        assert '<browser-content url="https://example.com/" untrusted>' in out

    def test_snapshot_cap_clamped_to_hard_limit(self, env):
        tb.browser_snapshot.func(mode="all", max_chars=999999)
        args = env.fake.args_of("lx", "snapshot")[0]["args"]
        assert args["maxChars"] == 24000 and args["mode"] == "all"

    def test_snapshot_bad_mode_and_root_ref(self, env):
        assert "interactive" in tb.browser_snapshot.func(mode="bogus")
        assert not env.fake.args_of("lx", "snapshot")
        tb.browser_snapshot.func(root_ref="e6")
        assert env.fake.args_of("lx", "snapshot")[0]["args"]["rootRef"] == "e6"

    def test_no_page_open_gives_hint(self, env, monkeypatch):
        monkeypatch.setattr(page, "_browser", None)
        assert "browser_open" in tb.browser_snapshot.func()
        assert "browser_open" in tb.browser_find.func("保存")
        assert "browser_open" in tb.browser_click.func("e20")

    def test_find_formats_matches_with_refs(self, env):
        out = tb.browser_find.func("保存")
        assert "[ref=e19]" in out and "保存" in out and "untrusted" in out

    def test_find_no_match(self, env):
        env.fake.set("lx", "find", {"ok": True, "query": "zzz", "matches": [],
                                    "count": 0, "url": _LOCAL})
        assert "没找到" in tb.browser_find.func("zzz")

    def test_read_text_truncation_note(self, env):
        env.fake.set("lx", "readText", {"ok": True, "chars": 100, "truncated": True,
                                        "text": "长" * 100, "source": "main", "url": _LOCAL})
        out = tb.browser_read_text.func(max_chars=20)
        assert "已截断" in out and "max_chars" in out

    def test_console_filters_by_level(self, env):
        env.fake.set("console", None, {"ok": True, "url": _LOCAL, "entries": [
            {"level": "error", "text": "boom", "t": 1},
            {"level": "warn", "text": "careful", "t": 2}]})
        out = tb.browser_console.func()
        assert "boom" in out and "careful" not in out
        both = tb.browser_console.func(level="all")
        assert "boom" in both and "careful" in both
        assert "error" in tb.browser_console.func(level="bogus")

    def test_console_empty_and_clear(self, env):
        assert "没有 error 级别" in tb.browser_console.func()
        out = tb.browser_console.func(clear=True)
        assert "已清空" in out
        assert env.fake.args_of("console")[-1]["clear"] is True


# ── click / type 闸门与 diff ──────────────────────────────────

class TestInteractGate:
    def test_localhost_first_click_asks_in_manual(self, env):
        pm.apply(env.sess, "manual")
        out = tb.browser_click.func("e20")
        assert len(env.ui.asked) == 1 and env.ui.asked[0][1] == pm.KIND_BROWSER
        assert "添加一行" in env.ui.asked[0][0]
        assert "已点击" in out and env.fake.args_of("lx", "click")

    def test_trusted_origin_skips_ask(self, env):
        env.sess.browser_origin_allowlist.add("http://localhost:5173")
        pm.apply(env.sess, "manual")
        assert "已点击" in tb.browser_click.func("e20")
        assert not env.ui.asked

    def test_consequential_asks_even_in_auto(self, env):
        env.fake.set("lx", "info", {"ok": True, "tag": "button", "type": "submit",
                                    "role": "button", "name": "保存", "inForm": True,
                                    "formMethod": "post", "formAction": "/save"})
        pm.apply(env.sess, "auto")
        tb.browser_click.func("e19")
        assert len(env.ui.asked) == 1, "submit 按钮在自动模式也要问"
        assert "后果" in env.ui.asked[0][0]

    def test_allowlist_cannot_bypass_consequential(self, env):
        env.sess.browser_origin_allowlist.add("http://localhost:5173")
        pm.apply(env.sess, "auto")
        env.fake.set("lx", "info", {"ok": True, "tag": "button", "type": "",
                                    "role": "button", "name": "删除账户", "inForm": False})
        tb.browser_click.func("e20")
        assert len(env.ui.asked) == 1, "来源白名单绕不过有后果判定"

    def test_plan_blocks_click_without_asking(self, env):
        pm.apply(env.sess, "plan")
        out = tb.browser_click.func("e20")
        assert "Plan" in out and not env.ui.asked
        assert not env.fake.args_of("lx", "click")

    def test_bad_ref_fails_before_click(self, env):
        env.fake.set("lx", "info", {"ok": False, "error": "ref 不存在: e99（页面变了就重新 snapshot）"})
        out = tb.browser_click.func("e99")
        assert ("ref 不存在" in out or "失效" in out) and not env.fake.args_of("lx", "click")

    def test_refusal_stops_click(self, env):
        pm.apply(env.sess, "manual")
        env.ui.allow = False
        out = tb.browser_click.func("e20")
        assert "已拒绝" in out and not env.fake.args_of("lx", "click")


class TestDelta:
    def test_diff_attached_after_click(self, env):
        pm.apply(env.sess, "auto")
        env.fake.snapshots = [
            _all_snap('- list [ref=e10]\n  - listitem "旧行"'),
            _all_snap('- list [ref=e10]\n  - listitem "旧行"\n  - listitem "新行 1"'),
        ]
        out = tb.browser_click.func("e20")
        assert "页面变化:" in out and '+  - listitem "新行 1"' in out
        assert "untrusted" in out, "diff 也是页面内容，要围栏"
        snaps = env.fake.args_of("lx", "snapshot")
        assert len(snaps) == 2 and all(s["args"]["mode"] == "all" for s in snaps)

    def test_empty_diff_says_no_visible_change(self, env):
        pm.apply(env.sess, "auto")
        out = tb.browser_click.func("e20")
        assert tree.DELTA_EMPTY in out

    def test_delta_false_skips_snapshots(self, env):
        pm.apply(env.sess, "auto")
        tb.browser_click.func("e20", delta=False)
        assert not env.fake.args_of("lx", "snapshot")

    def test_navigation_invalidates_refs(self, env):
        pm.apply(env.sess, "auto")
        env.fake.snapshots = [_all_snap("- main"), _all_snap("- main", url=_LOCAL + "next")]
        out = tb.browser_click.func("e20")
        assert "已导航到" in out and "重新 browser_snapshot" in out


class TestType:
    def test_secret_blocked_on_public_before_confirm(self, env, monkeypatch):
        _setup_page(env.fake, url=_PUBLIC)
        monkeypatch.setattr("src.secret_mask.collect_secrets",
                            lambda *a, **k: {"sk-abcdefgh12345": ("API_KEY", False)})
        pm.apply(env.sess, "auto")
        out = tb.browser_type.func("e9", "key=sk-abcdefgh12345")
        assert "已拒绝" in out and "API_KEY" in out
        assert not env.ui.asked and not env.fake.args_of("lx", "type")

    def test_secret_allowed_on_localhost(self, env, monkeypatch):
        monkeypatch.setattr("src.secret_mask.collect_secrets",
                            lambda *a, **k: {"sk-abcdefgh12345": ("API_KEY", False)})
        pm.apply(env.sess, "auto")
        out = tb.browser_type.func("e9", "sk-abcdefgh12345")
        assert "已拒绝" not in out and env.fake.args_of("lx", "type")

    def test_submit_in_form_is_consequential(self, env):
        env.fake.set("lx", "info", {"ok": True, "tag": "input", "type": "text",
                                    "role": "textbox", "name": "用户名", "inForm": True,
                                    "formMethod": "post", "formAction": "/login"})
        pm.apply(env.sess, "auto")
        tb.browser_type.func("e9", "admin", submit=True)
        assert len(env.ui.asked) == 1, "表单内回车提交要问"
        assert env.fake.args_of("lx", "type")[0]["args"]["submit"] is True

    def test_plain_typing_on_localhost_bypass_no_ask(self, env):
        pm.apply(env.sess, "bypass")
        out = tb.browser_type.func("e9", "你好", clear=False)
        assert not env.ui.asked
        assert '"你好"' in out, "回显输入后的值"
        assert env.fake.args_of("lx", "type")[0]["args"]["clear"] is False

    def test_public_typing_asks_in_manual(self, env):
        _setup_page(env.fake, url=_PUBLIC)
        pm.apply(env.sess, "manual")
        tb.browser_type.func("e9", "hello")
        assert len(env.ui.asked) == 1


# ── 闸门兜底：无界面 / 子 Agent / 确认异常 ─────────────────────

class TestGateFallbacks:
    def test_no_ui_refuses_by_default(self, env, monkeypatch):
        monkeypatch.setattr(state, "ui_ref", None)
        pm.apply(env.sess, "manual")
        out = tb.browser_click.func("e20")
        assert "没有确认界面" in out and not env.fake.args_of("lx", "click")

    def test_headless_auto_approve_lets_through(self, env, monkeypatch):
        monkeypatch.setattr(state, "ui_ref", None)
        monkeypatch.setattr(state, "headless_auto_approve", True)
        pm.apply(env.sess, "manual")
        assert "已点击" in tb.browser_click.func("e20")

    def test_subagent_skips_per_action_confirm(self, env):
        env.sess.is_subagent = True
        _setup_page(env.fake, url=_PUBLIC)
        pm.apply(env.sess, "manual")
        assert "已点击" in tb.browser_click.func("e20")
        assert not env.ui.asked

    def test_subagent_refuses_consequential_actions(self, env):
        """复审 #20：浏览器不在 worktree 隔离里，子 Agent 的提交 / 删除改的是真实数据。"""
        env.sess.is_subagent = True
        _setup_page(env.fake, info={"ok": True, "ref": "e20", "tag": "button", "type": "button",
                                    "role": "button", "name": "删除账户", "inForm": False})
        pm.apply(env.sess, "auto")
        out = tb.browser_click.func("e20")
        assert "子 Agent 不能做有后果的浏览器操作" in out and not env.fake.args_of("lx", "click")
        assert not env.ui.asked
        pm.apply(env.sess, "bypass")      # 用户选了跳过所有确认：照旧放行
        assert "已点击" in tb.browser_click.func("e20")

    def test_confirm_dialog_error_refuses(self, env, monkeypatch):
        pm.apply(env.sess, "manual")

        def _boom(*a, **k):
            raise RuntimeError("dialog exploded")
        monkeypatch.setattr(env.ui, "confirm_command", _boom)
        out = tb.browser_click.func("e20")
        assert "已拒绝" in out and not env.fake.args_of("lx", "click")


# ── 确认卡携带的来源 / 有后果标志（第 5 步：会话来源白名单接线）─────

class TestConfirmCardPayload:
    def test_click_passes_origin_and_not_consequential(self, env):
        pm.apply(env.sess, "manual")
        tb.browser_click.func("e20")
        kw = env.ui.kwargs[-1]
        assert kw["browser_origin"] == "http://localhost:5173"
        assert kw["browser_consequential"] is False

    def test_submit_click_passes_consequential(self, env):
        env.fake.set("lx", "info", {"ok": True, "tag": "button", "type": "submit",
                                    "role": "button", "name": "保存", "inForm": True,
                                    "formMethod": "post", "formAction": "/save"})
        pm.apply(env.sess, "auto")
        tb.browser_click.func("e19")
        kw = env.ui.kwargs[-1]
        assert kw["browser_consequential"] is True
        assert kw["browser_origin"] == "http://localhost:5173"

    def test_type_submit_passes_consequential(self, env):
        env.fake.set("lx", "info", {"ok": True, "tag": "input", "type": "text",
                                    "role": "textbox", "name": "用户名", "inForm": True,
                                    "formMethod": "post", "formAction": "/login"})
        pm.apply(env.sess, "auto")
        tb.browser_type.func("e9", "admin", submit=True)
        assert env.ui.kwargs[-1]["browser_consequential"] is True

    def test_open_passes_normalized_origin(self, env):
        pm.apply(env.sess, "manual")
        tb.browser_open.func("https://Example.com:443/path?q=1")
        kw = env.ui.kwargs[-1]
        assert kw["browser_origin"] == "https://example.com", "默认端口省略、host 小写"
        assert kw["browser_consequential"] is False


# ── 审后修复：frame 门控 / 密钥按 frame / 打开与操作授权分离 ─────────

class TestFrameGateAndOpenRemember:
    def test_secret_blocked_in_public_frame_even_if_top_localhost(self, env, monkeypatch):
        env.fake.set("lx", "state", {"ok": True, "url": _PUBLIC}, frame="f.1")
        env.fake.set("qturl", None, {"ok": True, "url": _PUBLIC}, frame="f.1")
        tb.browser_snapshot.func(frame="f.1")
        monkeypatch.setattr("src.secret_mask.collect_secrets",
                            lambda *a, **k: {"sk-abcdefgh12345": ("API_KEY", False)})
        pm.apply(env.sess, "auto")
        out = tb.browser_type.func("e20", "key=sk-abcdefgh12345", frame="f.1")
        assert "已拒绝" in out and "API_KEY" in out
        assert not env.ui.asked and not env.fake.args_of("lx", "type")

    def test_public_frame_click_asks_in_manual_even_top_trusted(self, env):
        env.sess.browser_origin_allowlist.add("http://localhost:5173")
        env.fake.set("lx", "state", {"ok": True, "url": _PUBLIC}, frame="f.1")
        env.fake.set("qturl", None, {"ok": True, "url": _PUBLIC}, frame="f.1")
        tb.browser_snapshot.func(frame="f.1")
        pm.apply(env.sess, "manual")
        tb.browser_click.func("e20", frame="f.1")
        assert len(env.ui.asked) == 1, "顶层 trusted 不能替公网 iframe 免确认"
        assert env.ui.kwargs[-1]["browser_origin"] == "https://example.com"
        assert "iframe:" in env.ui.asked[-1][0]

    def test_open_allowlist_does_not_exempt_interact(self, env):
        env.sess.browser_open_allowlist.add("http://localhost:5173")
        pm.apply(env.sess, "manual")
        tb.browser_click.func("e20")
        assert len(env.ui.asked) == 1, "打开记住不免点击/输入"

    def test_open_allowlist_exempts_open_only(self, env):
        env.sess.browser_open_allowlist.add("https://example.com")
        env.fake.set("lx", "state", {"ok": True, "url": _PUBLIC})
        pm.apply(env.sess, "manual")
        assert "已打开" in tb.browser_open.func(_PUBLIC)
        assert not env.ui.asked
        tb.browser_click.func("e20")
        assert len(env.ui.asked) == 1, "打开记住之后点击仍要问"

    def test_gate_passes_browser_action(self, env):
        pm.apply(env.sess, "manual")
        tb.browser_open.func(_PUBLIC)
        assert env.ui.kwargs[-1]["browser_action"] == "open"
        tb.browser_click.func("e20")
        assert env.ui.kwargs[-1]["browser_action"] == "interact"


# ── close 与注册 ──────────────────────────────────────────────

def test_close_needs_no_confirm(env):
    pm.apply(env.sess, "manual")
    out = tb.browser_close.func()
    assert "已关闭" in out and not env.ui.asked
    assert env.fake.args_of("close")


def test_all_eight_tools_registered_with_display_names():
    from src.tools import ALL_TOOLS, TOOL_DISPLAY_NAMES
    names = {t.name for t in ALL_TOOLS}
    expected = {"browser_open", "browser_snapshot", "browser_find", "browser_read_text",
                "browser_click", "browser_type", "browser_console", "browser_close",
                "browser_screenshot"}
    assert expected <= names
    assert all(n in TOOL_DISPLAY_NAMES for n in expected)


def test_plan_whitelist_covers_readonly_only():
    from src.streaming import PLAN_MODE_READONLY_TOOLS
    assert {"browser_open", "browser_snapshot", "browser_find",
            "browser_read_text", "browser_console", "browser_screenshot"} <= PLAN_MODE_READONLY_TOOLS
    assert not {"browser_click", "browser_type", "browser_close"} & PLAN_MODE_READONLY_TOOLS