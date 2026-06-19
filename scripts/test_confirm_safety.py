"""确认卡安全逻辑测试：危险命令判定 / base 命令提取 / 命令规范化。

这些是 run_command 确认的安全核心——_is_destructive_command 决定危险命令
**不给"记住"选项**（防 AI 被永久授权后 rm -rf）。务必覆盖危险/安全/绕过用例。
都是 staticmethod，直接调用，不需 Qt 实例。
"""
import pytest

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
