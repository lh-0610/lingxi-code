"""claude_code._build_claude_cmd 权限映射 + 命令行长度规避 smoke test。

只测纯函数的 flag 构造（不起 subprocess、不调 claude）：
  Plan        → --permission-mode plan（只读，且永不带 dangerous）
  Act + skip  → --dangerously-skip-permissions
  Act + 无skip → --permission-mode acceptEdits
system prompt 走 --append-system-prompt-file（文件路径，不内联）；用户 prompt 不进命令行
（一律走 stdin）——避开 Windows ~32K 命令行长度限制。
"""
from src.claude_code import _build_claude_cmd


def _cmd(**kw):
    base = dict(agent_mode="act", skip_permissions=False, model="",
                system_prompt_file=None)
    base.update(kw)
    return _build_claude_cmd(**base)


class TestBuildClaudeCmd:
    def test_plan_mode_readonly(self):
        cmd = _cmd(agent_mode="plan")
        assert cmd[cmd.index("--permission-mode") + 1] == "plan"
        assert "--dangerously-skip-permissions" not in cmd

    def test_act_skip_permissions(self):
        cmd = _cmd(agent_mode="act", skip_permissions=True)
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd

    def test_act_no_skip_uses_acceptedits(self):
        cmd = _cmd(agent_mode="act", skip_permissions=False)
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert "--dangerously-skip-permissions" not in cmd

    def test_plan_overrides_skip(self):
        # Plan 即使 skip 配置开，也必须只读：plan 优先、且永不带 dangerous
        cmd = _cmd(agent_mode="plan", skip_permissions=True)
        assert cmd[cmd.index("--permission-mode") + 1] == "plan"
        assert "--dangerously-skip-permissions" not in cmd

    def test_system_prompt_via_file_not_inline(self):
        # system prompt 走文件，绝不内联进命令行（防 32K 超限）
        cmd = _cmd(system_prompt_file="/tmp/sys.txt")
        assert cmd[cmd.index("--append-system-prompt-file") + 1] == "/tmp/sys.txt"
        assert "--append-system-prompt" not in cmd   # 不是内联版
        assert "--system-prompt" not in cmd          # 也不是替换版

    def test_no_system_prompt_file_omitted(self):
        cmd = _cmd(system_prompt_file=None)
        assert "--append-system-prompt-file" not in cmd

    def test_model_flag(self):
        cmd = _cmd(model="claude-x")
        assert cmd[cmd.index("--model") + 1] == "claude-x"

    def test_prompt_never_on_cmdline(self):
        # 命令行里只应有已知 flag/值，没有任何"用户 prompt"位置参数（prompt 走 stdin）
        cmd = _cmd(model="claude-x", system_prompt_file="/tmp/s.txt")
        allowed = {
            "claude", "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--model", "claude-x",
            "--append-system-prompt-file", "/tmp/s.txt",
        }
        assert set(cmd) <= allowed
