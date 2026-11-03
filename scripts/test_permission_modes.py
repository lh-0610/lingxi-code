"""权限模式：每种模式下哪些操作直接放行、哪些先问（输入框底栏"模式"菜单背后的规则）。

放行只在 confirm_command / confirm_edit 开头判一次。用一个假宿主代替窗口：要弹确认卡时它记下
"问了"并直接拒绝，不会卡住等人点。
"""
from types import SimpleNamespace

import pytest

from src import config, memory, permission_modes as pm, session
from src.ui.confirm_bars import ConfirmBarsMixin

_GIT_WRITE = "将执行 Git 写操作：git commit -m x"
_MCP = "将调用 MCP 工具 fetch，参数:\n{}"


class _Host(ConfirmBarsMixin):
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


def _decide(sess, mode, *, command=None, edit=None):
    pm.apply(sess, mode)
    host = _Host()
    if command is not None:
        allowed, _ = host.confirm_command(command)
    else:
        allowed, _ = host.confirm_edit(edit, "--- a\n+++ b\n")
    return "放行" if allowed and not host.asked else "询问"


@pytest.mark.parametrize("mode, edit, plain, risky, git, mcp", [
    ("manual",       "询问", "询问", "询问", "询问", "询问"),
    ("accept_edits", "放行", "询问", "询问", "询问", "询问"),
    ("auto",         "放行", "放行", "询问", "询问", "询问"),
    ("bypass",       "放行", "放行", "放行", "放行", "放行"),
])
def test_what_each_mode_lets_through(sess, mode, edit, plain, risky, git, mcp):
    assert _decide(sess, mode, edit="C:/p/a.py") == edit
    assert _decide(sess, mode, command="python -m pytest") == plain
    assert _decide(sess, mode, command="rm -rf build") == risky
    assert _decide(sess, mode, command=_GIT_WRITE) == git
    assert _decide(sess, mode, command=_MCP) == mcp


def test_plan_mode_asks_for_everything_and_keeps_the_exec_rule(sess):
    pm.apply(sess, "auto")
    pm.apply(sess, "plan")
    assert sess.agent_mode == "plan" and pm.current(sess) == "plan"
    assert _decide(sess, "plan", command="python -m pytest") == "询问"
    assert sess.permission_mode == "auto", "切到计划不丢执行时的规则"


def test_allowlist_still_works_in_manual(sess):
    pm.apply(sess, "manual")
    sess.command_prefix_allowlist.add("python")
    host = _Host()
    assert host.confirm_command("python -m pytest")[0] is True and not host.asked


def test_mode_survives_save_and_load_but_bypass_does_not(sess, isolated_memory):
    """跳过权限确认不落盘：重开程序后不该有会话悄悄停在"什么都不问"。"""
    from langchain_core.messages import AIMessage, HumanMessage
    for mode, expected in (("auto", "auto"), ("accept_edits", "accept_edits"), ("bypass", "manual")):
        s = session.Session()
        s.current_session_id = f"perm-{mode}"
        s.chat_history = [HumanMessage(content="你好"), AIMessage(content="在的")]   # 空会话不落盘
        pm.apply(s, mode)
        memory.save_session(session=s)
        back = session.Session()
        assert memory.load_session(s.current_session_id, session=back)
        assert pm.current(back) == expected


def test_old_sessions_and_snapshots_read_as_manual():
    assert pm.normalize(None) == "manual" and pm.normalize("whatever") == "manual"
    assert pm.snapshot_mode({"agent_mode": "act"}) == "manual"
    assert pm.snapshot_mode({"agent_mode": "plan", "permission_mode": "auto"}) == "plan"
    assert pm.label("bypass") == "跳过权限确认模式"
