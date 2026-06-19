"""run_command cd 跨命令留存测试：_parse_cd 解析 / _shell_cwd 回退。

都是纯函数（只读 state + os），用 project_dir fixture 设项目根、monkeypatch 设 shell_cwd。
"""
import os

from src import tools, state


class TestParseCd:
    def test_relative(self, project_dir, monkeypatch):
        monkeypatch.setattr(state, "shell_cwd", None)
        assert tools._parse_cd("cd src") == os.path.normpath(os.path.join(str(project_dir), "src"))

    def test_compound_is_not_pure_cd(self, project_dir):
        assert tools._parse_cd("cd src && ls") is None
        assert tools._parse_cd("cd a; rm b") is None
        assert tools._parse_cd("cd x | cat") is None

    def test_cdrom_not_misparsed(self, project_dir):
        assert tools._parse_cd("cdrom") is None

    def test_not_cd_command(self, project_dir):
        assert tools._parse_cd("ls -la") is None
        assert tools._parse_cd("git status") is None

    def test_bare_cd_and_tilde_to_root(self, project_dir, monkeypatch):
        monkeypatch.setattr(state, "shell_cwd", None)
        root = os.path.normpath(str(project_dir))
        assert tools._parse_cd("cd") == root
        assert tools._parse_cd("cd ~") == root

    def test_parent(self, project_dir, monkeypatch):
        sub = project_dir / "src"
        sub.mkdir()
        monkeypatch.setattr(state, "shell_cwd", str(sub))
        assert tools._parse_cd("cd ..") == os.path.normpath(str(project_dir))

    def test_quotes_stripped(self, project_dir, monkeypatch):
        monkeypatch.setattr(state, "shell_cwd", None)
        assert tools._parse_cd('cd "my dir"') == os.path.normpath(os.path.join(str(project_dir), "my dir"))

    def test_absolute_kept(self, project_dir):
        target = tools._parse_cd("cd " + os.path.abspath(os.sep))
        assert os.path.isabs(target)


class TestShellCwd:
    def test_none_falls_back_to_project_root(self, project_dir, monkeypatch):
        monkeypatch.setattr(state, "shell_cwd", None)
        assert tools._shell_cwd() == tools._project_cwd()

    def test_uses_shell_cwd_when_valid(self, project_dir, monkeypatch):
        sub = project_dir / "src"
        sub.mkdir()
        monkeypatch.setattr(state, "shell_cwd", str(sub))
        assert tools._shell_cwd() == str(sub)

    def test_invalid_shell_cwd_falls_back(self, project_dir, monkeypatch):
        monkeypatch.setattr(state, "shell_cwd", str(project_dir / "does_not_exist"))
        assert tools._shell_cwd() == tools._project_cwd()
