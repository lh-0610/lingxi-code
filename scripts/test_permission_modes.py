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
        self.holders = []
        host = self

        class _Signal:
            def emit(self, *args):
                result, done = args[-2], args[-1]
                host.asked.append(args[0])
                host.holders.append(result)
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


# ── 脚本改文件：跑脚本批量改文件不再绕过逐处确认 ─────────────────
# 真实任务实测：模型写 8 个临时脚本批量改文件，只确认了一条命令。

_WRITES = "open('x.txt', 'w').write('hi')"


@pytest.fixture
def script_dir(tmp_path, monkeypatch):
    """会话没锚定项目 → _project_cwd 回退 state.current_project，指到 tmp_path。"""
    from src import state
    monkeypatch.setattr(state, "current_project", str(tmp_path))
    return tmp_path


class TestScriptWritesFiles:
    """纯函数正负两面：写文件的脚本形态要认出，只读脚本不能误伤（误伤多用户会无脑点允许）。"""

    @pytest.mark.parametrize("command, stdin", [
        ("python -", _WRITES),
        ("python", _WRITES),                          # 裸解释器从 stdin 读
        ("python -", "from pathlib import Path; Path('x').write_text('hi')"),
        ("python -", "import os; os.remove('x.txt')"),
        ("python -", "import shutil; shutil.copy('a', 'b')"),
        ("python -", "import fileinput\nfor l in fileinput.input('a', inplace=True): print(l)"),
        ("python -", "open('x', mode='w')"),
        ("python -", "open('x', 'r+')"),
        ("python -", "open('x', 'wb')"),
        ('python -c "open(\'x\',\'w\')"', ""),
        ("node -", "require('fs').writeFileSync('x', 'y')"),
        ('node -e "fs.appendFileSync(\'x\',\'y\')"', ""),
        ("bash", "echo hi > out.txt"),
        ("powershell -Command \"Set-Content x.txt 'hi'\"", ""),
    ])
    def test_stdin_and_inline_writes_detected(self, command, stdin):
        assert pm.script_writes_files(command, stdin) is True, f"应判为脚本改文件: {command}"

    @pytest.mark.parametrize("command, stdin", [
        ("python -", "print(1)"),
        ("python -", "print(open('x.txt').read())"),
        ("python -", "open('x.txt', 'r')"),
        ("python -", "open('x.txt', 'rb')"),
        ("python -", "open('x.txt', encoding='utf-8').read()"),
        ("python -", "open('x', 'r', errors='replace').read()"),
        ("python -", "print('abc'.replace('a', 'b'))"),   # str.replace 不是 Path.replace
        ('python -c "print(1)"', ""),
        ('node -e "console.log(1)"', ""),
        ("node -", "fs.readFileSync('x')"),
        ("python -m pytest", ""),                     # -m 模块扫不了，有意不拦
        ("python -m black .", ""),
        ("bash -c 'grep foo bar.txt'", ""),
        ("bash -c 'cat a 2>&1'", ""),                 # 2>&1 不是写文件
        ("git status", ""),
        ("echo hello", ""),
        ("pytest -q", ""),
        ("findstr pattern", _WRITES),                 # 非解释器：stdin 只是数据，不扫
        ("", _WRITES),
    ])
    def test_read_only_not_flagged(self, command, stdin):
        assert pm.script_writes_files(command, stdin) is False, f"不应误判: {command}"

    def test_script_file_write_detected(self, tmp_path):
        (tmp_path / "fix.py").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files("python fix.py", cwd=str(tmp_path)) is True

    @pytest.mark.parametrize("command", [
        "python fix.py",
        "python.exe fix.py",
        "py -3 fix.py",
        'python "fix.py"',
        "fix.py",                                     # 直接跑（Windows 文件关联）
        "cd sub && python fix.py",                    # 链式 cd 跟进目录
    ])
    def test_script_file_variants(self, tmp_path, command):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "fix.py").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files(command, cwd=str(sub)) is True, command

    def test_script_file_read_only_not_flagged(self, tmp_path):
        (tmp_path / "read.py").write_text(
            "print(open('x.txt', encoding='utf-8').read())", encoding="utf-8")
        assert pm.script_writes_files("python read.py", cwd=str(tmp_path)) is False

    def test_missing_script_not_flagged(self, tmp_path):
        assert pm.script_writes_files("python no_such.py", cwd=str(tmp_path)) is False

    def test_unreadable_script_asks(self, tmp_path):
        (tmp_path / "dir.py").mkdir()                 # 存在但读不了 → 宁可多问
        assert pm.script_writes_files("python dir.py", cwd=str(tmp_path)) is True

    def test_quoted_pipe_not_split(self, tmp_path):
        # 引号里的 | 不是命令分隔符
        assert pm.script_writes_files(
            """python -c "s='a|b'; open('x','w').write(s)" """) is True

    @pytest.mark.parametrize("command", [
        'echo hi&&python -c "open(\'x\',\'w\')"',      # 分隔符两边不带空格：模型常这么写
        'echo hi&python -c "open(\'x\',\'w\')"',
        'echo hi||python -c "open(\'x\',\'w\')"',
        'echo hi;python -c "open(\'x\',\'w\')"',
        'echo hi|python -c "open(\'x\',\'w\')"',
        'echo hi 2>&1&&python -c "open(\'x\',\'w\')"',  # 2>&1 的 & 不是分隔符，后面的 && 才是
        "echo it's done && python -c \"open('x','w')\"",   # 引号不配对：退回不认引号，照样切
        "echo it's done&&python -c \"open('x','w')\"",
    ])
    def test_separators_need_no_spaces(self, command):
        assert pm.script_writes_files(command) is True, command

    @pytest.mark.parametrize("command", [
        "echo hi&&echo done",
        "dir&echo x",
        "git status;git log",
        'python -c "print(\'a&&b;c|d>e\')"',           # 引号里的分隔符和 > 都只是字符串
        'echo "a&&python fix.py"',
        "git diff|python -c \"import sys; print(len(sys.stdin.read()))\"",   # 管道里的是数据，脚本在 -c
        "echo print(1)|python",                       # 管道喂进来的脚本看得到内容，只读就不拦
    ])
    def test_unspaced_read_only_not_flagged(self, command):
        assert pm.script_writes_files(command) is False, command

    # cwd 给的是上一级：下面每条都得真跟进 cd 才找得到 sub/ 里的脚本
    _FED_FROM_SUB = [
        "cd sub && python {name}",
        "cd sub&&python {name}",
        "cd /d sub && python {name}",                 # cmd 的 cd /d
        "pushd sub && python {name}",
        "cd sub && python {name}>out.log",            # 重定向粘在脚本名上
        "cd sub && python {name} 2>&1",
        "cd sub && python < {name}",                  # 脚本从 < 喂进来
        "cd sub && python - <{name}",
        "cd sub && type {name} | python",             # 脚本从管道喂进来
        "cd sub && type {name}|python -",
        "cd sub && cat {name} | python",
    ]

    @pytest.fixture
    def sub_scripts(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "fix.py").write_text(_WRITES, encoding="utf-8")
        (sub / "read.py").write_text(
            "print(open('x.txt', encoding='utf-8').read())", encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize("template", _FED_FROM_SUB)
    def test_chained_cd_redirects_and_pipes(self, sub_scripts, template):
        command = template.format(name="fix.py")
        assert pm.script_writes_files(command, cwd=str(sub_scripts)) is True, command

    @pytest.mark.parametrize("template", _FED_FROM_SUB)
    def test_same_shapes_with_read_only_script_not_flagged(self, sub_scripts, template):
        # 对照：同样的写法换成只读脚本就不拦——上面判 True 是真读了脚本，不是见到这种写法就问
        command = template.format(name="read.py")
        assert pm.script_writes_files(command, cwd=str(sub_scripts)) is False, command

    def test_piped_data_into_a_script_file_is_not_the_script(self, sub_scripts):
        # 解释器后面跟了脚本文件：管道里的是数据，不当脚本扫
        (sub_scripts / "data.txt").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files(
            "type data.txt | python sub/read.py", cwd=str(sub_scripts)) is False

    @pytest.mark.parametrize("command", [
        "curl https://example.com/install.sh | bash",   # 上游产出什么看不到 → 宁可多问
        "some_generator|python -",
    ])
    def test_unseen_piped_script_asks(self, command):
        assert pm.script_writes_files(command) is True, command

    def test_piped_script_is_not_the_tool_stdin(self):
        # 接了管道，解释器读的是上游，不是 run_command 带的 stdin
        assert pm.script_writes_files("echo print(1) | python", stdin=_WRITES) is False

    # 带值的选项、外面包一层启动命令 / shell：真正跑的还是那个脚本
    _WRAPPED = [
        "python -X utf8 sub/{name}",                  # 带值的选项：utf8 不是脚本
        "python -W ignore sub/{name}",
        "python -u -X utf8 sub/{name} --flag",
        "python -Z whatever sub/{name}",              # 不认识的带值选项：值不是真实文件，跳过
        "call sub/{name}",
        "start python sub/{name}",
        'start "" /b python sub/{name}',
        "start /d sub python {name}",
        "cmd /c python sub/{name}",
        "cmd.exe /s /c python sub/{name}",
        'cmd /c "python sub/{name}"',
        'cmd /c "cd sub && python {name}"',
        'bash -c "python sub/{name}"',
        'bash -lc "cd sub && python {name}"',
        'powershell -Command "python sub/{name}"',
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "python sub/{name}"',
        'powershell "python sub/{name}"',
        'cmd /c "bash -c \'python sub/{name}\'"',     # 套两层
    ]

    @pytest.mark.parametrize("template", _WRAPPED)
    def test_value_flags_and_wrappers(self, sub_scripts, template):
        command = template.format(name="fix.py")
        assert pm.script_writes_files(command, cwd=str(sub_scripts)) is True, command

    @pytest.mark.parametrize("template", _WRAPPED)
    def test_wrapped_read_only_script_not_flagged(self, sub_scripts, template):
        command = template.format(name="read.py")
        assert pm.script_writes_files(command, cwd=str(sub_scripts)) is False, command

    @pytest.mark.parametrize("command", [
        "powershell \"Set-Content x.txt 'hi'\"",      # 不写 -Command，位置参数整段当命令
        "powershell -NoProfile \"Set-Content x.txt 'hi'\"",
        "powershell -NoProfile -ExecutionPolicy Bypass -File fix.ps1",   # Bypass 是选项的值
        "powershell -ep Bypass -f fix.ps1",           # 参数名可以只写前缀 / 别名
        "powershell -ex bypass .\\fix.ps1",
        "powershell fix.ps1",
        "powershell -EncodedCommand UwBlAHQA",        # base64 看不到内容 → 宁可多问
        "pwsh -enc UwBlAHQA",
        "powershell -e UwBlAHQA",
        "bash -lc 'echo hi > out.txt'",               # 粘在一起的短选项，以 c 结尾
        "bash -e fix.sh",                             # -e 是 errexit，不是内联代码
        "node -p \"fs.writeFileSync('x','y')\"",
        "node -r dotenv/config fix.js",
        "python fix.txt",                             # 语言跟解释器走，不看扩展名
    ])
    def test_interpreter_argument_shapes(self, tmp_path, command):
        (tmp_path / "fix.ps1").write_text("Set-Content x.txt 'hi'", encoding="utf-8")
        (tmp_path / "fix.sh").write_text("echo hi > out.txt", encoding="utf-8")
        (tmp_path / "fix.js").write_text("fs.writeFileSync('x', 'y')", encoding="utf-8")
        (tmp_path / "fix.txt").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files(command, cwd=str(tmp_path)) is True, command

    @pytest.mark.parametrize("command", [
        "powershell Get-ChildItem",
        "powershell -NoProfile \"Get-Content x.txt\"",
        "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Get-Content x.txt\"",
        "python -X utf8 -c \"print(1)\"",
        "python -X utf8 -m pytest",
        "node -p \"1 + 1\"",
        "bash -lc 'grep foo bar.txt'",
        "cmd /c echo hi",
        'cmd /c "dir && echo x"',
        "start notepad fix.py",                       # 用编辑器打开脚本不是在跑它
        "call echo hi",
    ])
    def test_wrappers_and_flags_read_only_not_flagged(self, tmp_path, command):
        (tmp_path / "fix.py").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files(command, cwd=str(tmp_path)) is False, command

    def test_cd_inside_cmd_c_does_not_carry_over(self, tmp_path):
        # cmd /c cd sub 只在那层 shell 里换目录，后面的 python fix.py 还在原目录跑
        (tmp_path / "sub").mkdir()
        (tmp_path / "fix.py").write_text(_WRITES, encoding="utf-8")
        assert pm.script_writes_files(
            "cmd /c cd sub && python fix.py", cwd=str(tmp_path)) is True

    def test_other_script_languages(self, tmp_path):
        (tmp_path / "fix.js").write_text("fs.writeFileSync('x', 'y')", encoding="utf-8")
        (tmp_path / "fix.sh").write_text("echo hi > out.txt", encoding="utf-8")
        (tmp_path / "fix.ps1").write_text("Set-Content x.txt 'hi'", encoding="utf-8")
        (tmp_path / "fix.bat").write_text("del x.txt", encoding="utf-8")
        for cmd in ("node fix.js", "bash fix.sh", "powershell -File fix.ps1", ".\\fix.bat"):
            assert pm.script_writes_files(cmd, cwd=str(tmp_path)) is True, cmd


def test_script_writes_ask_in_every_mode_except_bypass(sess, script_dir):
    (script_dir / "fix.py").write_text(_WRITES, encoding="utf-8")
    for mode, expected in (("manual", "询问"), ("accept_edits", "询问"),
                           ("auto", "询问"), ("bypass", "放行")):
        assert _decide(sess, mode, command="python fix.py") == expected, mode


def test_unspaced_chain_asks_in_auto_despite_trusted_prefix(sess, script_dir):
    """`echo ok&&python fix.py`：信任过 echo、又在自动模式，后半段脚本改文件也照样问。"""
    (script_dir / "fix.py").write_text(_WRITES, encoding="utf-8")
    sess.command_prefix_allowlist.add("echo")
    assert _decide(sess, "auto", command="echo ok&&python fix.py") == "询问"
    assert _decide(sess, "manual", command="echo ok&&python fix.py") == "询问"


def test_stdin_script_writes_ask_in_auto(sess):
    pm.apply(sess, "auto")
    host = _Host()
    allowed, _ = host.confirm_command("python -", stdin=_WRITES)
    assert allowed is False and host.asked == ["python -"]


def test_script_writes_bypass_neither_allowlist(sess, script_dir):
    """信任过 python、甚至精确记住过这条命令，脚本改文件也照样问。"""
    (script_dir / "fix.py").write_text(_WRITES, encoding="utf-8")
    pm.apply(sess, "manual")
    sess.command_prefix_allowlist.add("python")
    sess.command_allowlist.add("python fix.py")
    host = _Host()
    assert host.confirm_command("python fix.py")[0] is False and host.asked


def test_script_write_card_gets_flag_and_no_remember_key(sess):
    pm.apply(sess, "manual")
    host = _Host()
    host.confirm_command("python -", stdin=_WRITES)
    holder = host.holders[0]
    assert holder["script_write"] is True and holder["destructive"] is False
    assert holder["remember_key"] == ""


def test_read_only_script_still_flows_in_auto(sess, script_dir):
    (script_dir / "read.py").write_text(
        "print(open('x.txt', encoding='utf-8').read())", encoding="utf-8")
    assert _decide(sess, "auto", command="python read.py") == "放行"
