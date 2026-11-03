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


def _decide(sess, mode, *, command=None, edit=None, kind="command", tool=""):
    pm.apply(sess, mode)
    host = _Host()
    if command is not None:
        allowed, _ = host.confirm_command(command, kind=kind, tool=tool)
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
    assert _decide(sess, mode, command=_GIT_WRITE, kind=pm.KIND_GIT_WRITE) == git
    assert _decide(sess, mode, command=_MCP, kind=pm.KIND_MCP, tool="fetch") == mcp


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


# ── 安全复查（外部评审指出的三处）──────────────────────────────

@pytest.mark.parametrize("command", [
    "git clean -fdx",
    "git clean --force -d",
    "git reset --hard HEAD~1",
    "git checkout -- .",
    "git checkout .",
    "git restore src/app.py",
    "git push --force origin main",
    "git push -f",
    "git push origin --delete feature",
    "git branch -D feature",
    "git stash drop",
    "git stash clear",
    "rd /s /q build",
])
def test_auto_mode_still_asks_for_data_losing_git_and_rd(sess, command):
    """"自动"靠危险命令清单拦：以前清单里一条 git 都没有，git clean -fdx 会不问直接删掉没进 git 的文件。"""
    assert ConfirmBarsMixin._is_destructive_command(command), "危险命令也不给「记住」"
    assert _decide(sess, "auto", command=command) == "询问"
    assert _decide(sess, "bypass", command=command) == "放行"


@pytest.mark.parametrize("command", ["git status", "git diff", "git checkout main", "git log -5",
                                     "git clean -n", "git push origin main"])
def test_ordinary_git_commands_are_not_flagged(command):
    assert not ConfirmBarsMixin._is_destructive_command(command)


def test_trusting_a_patch_never_trusts_every_later_patch(sess):
    """apply_patch 的确认用占位名 "(patch)"：信任它等于以后所有补丁、改任何文件都不再问。"""
    from src.ui.confirm_bars import _trustable_path
    assert not _trustable_path("(patch)") and _trustable_path("C:/p/a.py")
    sess.edit_path_allowlist.add("(patch)")          # 哪怕名单里已经混进来了
    assert _decide(sess, "manual", edit="(patch)") == "询问"


def test_spawning_sub_agents_asks_first(project_dir, monkeypatch):
    """子 Agent 改文件、跑命令都不再逐条问、做完直接合并：派之前得先问，"手动"模式下才不会被绕过去。"""
    from src import state, subagent, tools
    asked = []
    monkeypatch.setattr(state, "ui_ref", SimpleNamespace(
        confirm_command=lambda text, **kw: (asked.append((text, kw)), (False, "先别派"))[1]))
    monkeypatch.setattr(subagent, "spawn", lambda *a, **k: pytest.fail("用户拒绝了还在派"))
    out = tools.spawn_agents.func(["改 A 模块", "改 B 模块"])
    assert "已拒绝" in out and "先别派" in out
    text, kw = asked[0]
    assert kw.get("kind") == pm.KIND_SPAWN, "按类型判定，不靠文案开头"
    assert text.startswith(pm.SPAWN_AGENTS_PREFIX) and "改 A 模块" in text


def test_spawn_prompt_follows_the_mode_rules(sess):
    for mode, expected in (("auto", "询问"), ("manual", "询问"), ("bypass", "放行")):
        assert _decide(sess, mode, command="派两个子 Agent", kind=pm.KIND_SPAWN) == expected


def test_risk_comes_from_the_kind_not_the_wording(sess):
    """文案开头改了也不影响判定：以前靠"将执行 Git 写操作"这几个字认，文案一改就悄悄失效。"""
    assert _decide(sess, "auto", command="提交一下", kind=pm.KIND_GIT_WRITE) == "询问"
    assert _decide(sess, "auto", command="将执行 Git 写操作：其实只是 ls") == "放行", "普通命令不看文案前缀"


def test_remembering_one_mcp_tool_does_not_trust_all_of_them(sess):
    """以前"记住"取文案第一个词（"将调用"），记住一个 MCP 工具等于以后所有 MCP 工具都不再问。"""
    pm.apply(sess, "manual")
    sess.command_prefix_allowlist.add("mcp:fetch")
    host = _Host()
    assert host.confirm_command("将调用 MCP 工具 fetch", kind=pm.KIND_MCP, tool="fetch")[0] is True
    assert not host.asked
    assert host.confirm_command("将调用 MCP 工具 delete_repo", kind=pm.KIND_MCP, tool="delete_repo")[0] is False
    assert host.asked


def test_no_ui_means_refuse_unless_explicitly_allowed(project_dir, monkeypatch):
    """没有确认界面时默认拒绝（fail-closed）：以前 ui_ref 为 None 就直接放行改文件、跑命令。"""
    from src import state, tools
    monkeypatch.setattr(state, "ui_ref", None)
    monkeypatch.setattr(state, "headless_auto_approve", False)
    out = tools.write_file.func("x.txt", "hi")
    assert "没有确认界面" in out and not (project_dir / "x.txt").exists()
    assert "没有确认界面" in tools.run_command.func("echo hi")
    assert "没有确认界面" in tools.spawn_agents.func(["改 A"])

    monkeypatch.setattr(state, "headless_auto_approve", True)      # 测试 / 无界面运行显式打开
    assert "成功写入" in tools.write_file.func("x.txt", "hi")
