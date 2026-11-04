"""run_command 的 stdin：多行脚本一次跑完（command="python -"，代码放 stdin）。

以前 cmd.exe 里写不了多行，模型把试验拆成一条条 `python -c "..."`（文件面板语法高亮那次
87 轮里有 25 条），或者在项目里写临时脚本、跑完再删。确认卡上要看得到这段代码；危险判定、
子 Agent 的沙箱检查也要看它——脚本里调 subprocess 和直接写在命令行里一样能删东西。
"""
from types import SimpleNamespace

import pytest

from src import config, permission_modes as pm, session, state, tools
from src.streaming import describe_tool_call
from src.ui.confirm_bars import ConfirmBarsMixin

_SCRIPT = (
    "def greet(name):\n"
    "    return f'你好，{name}'\n"
    "\n"
    "for who in ('灵犀', 'stdin'):\n"
    "    print(greet(who))\n"
)


@pytest.fixture
def headless(monkeypatch):
    """没有界面、无界面放行打开（conftest 默认如此）：run_command 不弹确认直接跑。"""
    monkeypatch.setattr(state, "ui_ref", None)


def test_a_multi_line_script_runs_in_one_go(project_dir, headless):
    out = tools.run_command.func("python -", stdin=_SCRIPT, timeout=60)
    assert "退出码: 0" in out
    assert "你好，灵犀" in out and "你好，stdin" in out, out


def test_the_script_can_import_project_modules_and_leaves_no_file(project_dir, headless):
    (project_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    before = {p.name for p in project_dir.iterdir()}
    out = tools.run_command.func("python -", stdin="import helper\nprint('value', helper.VALUE)\n", timeout=60)
    assert "value 42" in out, out
    after = {p.name for p in project_dir.iterdir()} - {"__pycache__"}
    assert after == before, "试验脚本不该在项目里留下文件"


def test_stdin_cannot_go_to_the_background(project_dir, headless):
    out = tools.run_command.func("python -", stdin="print(1)\n", background=True)
    assert "不能和 background 一起用" in out


def test_the_card_gets_the_script_and_a_refusal_stops_it(project_dir, monkeypatch):
    marker = project_dir / "ran.txt"
    asked = []

    def confirm_command(command, stdin=""):
        asked.append((command, stdin))
        return False, "先别跑"

    monkeypatch.setattr(state, "ui_ref", SimpleNamespace(confirm_command=confirm_command,
                                                          show_message=lambda *a, **k: None))
    script = f"open(r'{marker}', 'w').write('x')\n"
    out = tools.run_command.func("python -", stdin=script)
    assert asked == [("python -", script)], "确认卡上要列出交进去的整段代码"
    assert "已拒绝" in out and "先别跑" in out
    assert not marker.exists()


def test_the_subagent_sandbox_check_reads_the_script(project_dir, headless, monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "_subagent_command_rejection",
                        lambda text, cwd=None: seen.append(text) or "拒绝：引用了 worktree 外路径")
    out = tools.run_command.func("python -", stdin="open('C:/elsewhere/x.txt').read()\n")
    assert out.startswith("拒绝") and "C:/elsewhere/x.txt" in seen[0]


# ── 确认规则：和一条 python 命令一样，但脚本内容也算 ──

class _Host(ConfirmBarsMixin):
    """假窗口：要弹卡片时记下来并直接拒绝，不卡住等人点。"""

    def __init__(self):
        self.asked = []
        host = self

        class _Signal:
            def emit(self, command, result, done):
                host.asked.append(result)
                result["allow"] = False
                done.set()

        self.bridge = SimpleNamespace(confirm_request=_Signal())


@pytest.fixture
def sess(monkeypatch):
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    s = session.Session()
    session.bind_thread(s)
    yield s
    session.unbind_thread()


def _decide(sess, mode, stdin):
    pm.apply(sess, mode)
    host = _Host()
    allowed, _ = host.confirm_command("python -", stdin=stdin)
    return ("放行" if allowed and not host.asked else "询问"), host.asked


def test_a_plain_script_follows_the_mode_like_a_python_command(sess):
    assert _decide(sess, "auto", _SCRIPT)[0] == "放行"
    verdict, asked = _decide(sess, "manual", _SCRIPT)
    assert verdict == "询问"
    assert asked[0]["remember_key"] == "python", "「记住」和 python 命令共用一个（信任所有 python 类）"
    assert asked[0]["stdin"] == _SCRIPT


def test_a_data_losing_call_inside_the_script_still_asks_in_auto_mode(sess):
    script = "import subprocess\nsubprocess.run('git reset --hard', shell=True)\n"
    verdict, asked = _decide(sess, "auto", script)
    assert verdict == "询问"
    assert asked[0]["destructive"] is True, "危险的不给「记住」"


def test_trusting_python_also_trusts_scripts_but_an_old_exact_entry_does_not(sess):
    pm.apply(sess, "manual")
    sess.command_allowlist.add(ConfirmBarsMixin._normalize_command("python -"))
    host = _Host()
    assert host.confirm_command("python -", stdin=_SCRIPT)[0] is False and host.asked, \
        "精确白名单只认整条命令串：同一条 python - 后面跟的是别的代码"
    sess.command_prefix_allowlist.add("python")
    host = _Host()
    assert host.confirm_command("python -", stdin=_SCRIPT)[0] is True and not host.asked


def test_the_tool_card_shows_the_command_then_the_script():
    _, detail = describe_tool_call("run_command", {"command": "python -", "stdin": _SCRIPT})
    first, rest = detail.split("\n", 1)
    assert first == "python -" and rest == _SCRIPT
    assert describe_tool_call("run_command", {"command": "dir"})[1] == "dir"
