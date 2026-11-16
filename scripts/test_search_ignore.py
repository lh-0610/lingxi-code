"""用真实 Git 仓库验证搜索的忽略规则与共享文件清单。"""
import subprocess

import pytest

from src import code_graph
from src.tools import code_map, search_files


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), check=True,
                          capture_output=True, encoding="utf-8", stdin=subprocess.DEVNULL).stdout


@pytest.fixture()
def ignored_repo(project_dir):
    _git(project_dir, "init")
    (project_dir / ".gitignore").write_text("chat_memory/\nlogs/\n", encoding="utf-8")
    (project_dir / "src").mkdir()
    (project_dir / "src" / "app.py").write_text("NEEDLE = '源码'\n", encoding="utf-8")
    _git(project_dir, "add", ".gitignore", "src/app.py")
    (project_dir / "chat_memory").mkdir()
    (project_dir / "chat_memory" / "sess.json").write_text('"NEEDLE"\n', encoding="utf-8")
    (project_dir / "logs").mkdir()
    (project_dir / "logs" / "run.log").write_text("NEEDLE 日志\n", encoding="utf-8")
    (project_dir / "new.py").write_text("NEEDLE = '未跟踪'\n", encoding="utf-8")
    return project_dir


class TestGitignoreRespected:
    def test_ignored_dirs_absent_and_untracked_present(self, ignored_repo):
        out = search_files.func("NEEDLE")

        assert "src/app.py（1 处）" in out
        assert "new.py（1 处）" in out
        assert "chat_memory" not in out
        assert "run.log" not in out

    def test_ignored_long_lines_do_not_crowd_out_source(self, ignored_repo):
        for i in range(30):
            (ignored_repo / "chat_memory" / f"sess{i}.json").write_text(
                '{"content": "' + "x" * 20000 + "NEEDLE" + "y" * 2000 + '"}\n',
                encoding="utf-8")

        out = search_files.func("NEEDLE")

        assert len(out) < 2000
        assert "找到 2 处匹配" in out
        assert "src/app.py（1 处）" in out

    def test_tracked_file_stays_searchable_after_being_ignored(self, ignored_repo):
        _git(ignored_repo, "add", "new.py")
        (ignored_repo / ".gitignore").write_text("chat_memory/\nlogs/\nnew.py\n", encoding="utf-8")

        out = search_files.func("NEEDLE")

        assert "new.py（1 处）" in out

    def test_file_pattern_and_brace_expansion_still_work(self, ignored_repo):
        (ignored_repo / "a.ts").write_text("NEEDLE\n", encoding="utf-8")
        (ignored_repo / "b.tsx").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", file_pattern="*.{ts,tsx}")

        assert "a.ts（1 处）" in out and "b.tsx（1 处）" in out
        assert "app.py" not in out and "new.py" not in out

    def test_empty_git_listing_does_not_enable_ignored_root_files(self, project_dir):
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        (project_dir / "private.txt").write_text("NEEDLE\n", encoding="utf-8")
        assert _git(project_dir, "ls-files", "-co", "--exclude-standard", "-z") == ""

        out = search_files.func("NEEDLE")

        assert "未在" in out
        assert "private.txt" not in out

    def test_directory_ignoring_its_contents_is_searchable_when_named(self, ignored_repo):
        """这条原来断言"搜不到"，现在反过来。

        旧行为：目录内 .gitignore 写 `*` 时，被忽略的是**目录里的内容**，`check-ignore <目录>`
        说目录本身没被忽略，于是不退回遍历、点名也搜不到（旧注释说这是有意的）。
        但需求是"用户点名了被忽略的目录就要能搜到"，所以判定改成问 Git"在这个目录里新建一个
        普通文件会不会被忽略"：`data/<探测名>` 匹配目录内的 `*`，判为是 → 遍历这个目录。
        默认的全项目搜索不受影响：不点名时 private.txt 照旧搜不到。
        """
        directory = ignored_repo / "data"
        directory.mkdir()
        (directory / ".gitignore").write_text("*\n", encoding="utf-8")
        (directory / "private.txt").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="data")

        assert "private.txt（1 处）" in out
        assert "private.txt" not in search_files.func("NEEDLE")

    def test_empty_pattern_match_does_not_enable_ignored_files(self, ignored_repo):
        out = search_files.func("NEEDLE", file_pattern="*.json")

        assert "未在" in out
        assert "sess.json" not in out


class TestIncludeIgnored:
    def test_finds_ignored_files_through_tool_schema(self, ignored_repo):
        out = search_files.invoke({"regex": "NEEDLE", "include_ignored": True})

        assert "chat_memory/sess.json（1 处）" in out
        assert "logs/run.log（1 处）" in out
        assert "src/app.py（1 处）" in out and "new.py（1 处）" in out

    def test_still_skips_noise_dirs(self, ignored_repo):
        for name in ("node_modules", ".cache", "build"):
            (ignored_repo / name).mkdir()
            (ignored_repo / name / "noise.py").write_text("NEEDLE\n", encoding="utf-8")
        (ignored_repo / ".git" / "noise.py").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", include_ignored=True)

        assert "noise.py" not in out
        assert "chat_memory/sess.json（1 处）" in out

    def test_file_pattern_still_applies(self, ignored_repo):
        out = search_files.func("NEEDLE", file_pattern="*.py", include_ignored=True)

        assert "src/app.py（1 处）" in out and "new.py（1 处）" in out
        assert "sess.json" not in out and "run.log" not in out


class TestNamedPathOverridesIgnore:
    @pytest.mark.parametrize("path", ["logs", "logs/", "chat_memory"])
    def test_ignored_directory_falls_back_to_walk(self, ignored_repo, path):
        assert _git(ignored_repo / path, "ls-files", "-co", "--exclude-standard", "-z") == ""

        out = search_files.func("NEEDLE", path=path)

        expected = "sess.json" if path == "chat_memory" else "run.log"
        assert f"{expected}（1 处）" in out
        assert "app.py" not in out

    def test_ignored_absolute_directory(self, ignored_repo):
        out = search_files.func("NEEDLE", path=str(ignored_repo / "logs"))

        assert "run.log（1 处）" in out

    def test_ignored_single_file_ignores_file_pattern_and_size(self, ignored_repo):
        (ignored_repo / "logs" / "run.log").write_text("x" * 1_100_000 + "NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="logs/run.log", file_pattern="*.py")

        assert "run.log（1 处）" in out
        assert "file_pattern" not in out


class TestNamedDirectoryWithIgnoredContents:
    """点名"目录本身没被忽略、但里面的内容全被忽略"的目录，也要能搜到。

    临时目录 / 构建目录最常见的两种写法：目录内 .gitignore 写 `*`，或根 .gitignore 写 `build/*`。
    两种写法里 `git check-ignore <目录>` 都说目录本身没被忽略，而 Git 清单是空的——旧代码
    因此不退回遍历，点名也搜不到。第三种 `logs/`（目录本身被忽略）本来就过，作对照。
    """

    def test_own_gitignore_star(self, project_dir):
        _git(project_dir, "init")
        (project_dir / "tmp").mkdir()
        (project_dir / "tmp" / ".gitignore").write_text("*\n", encoding="utf-8")
        (project_dir / "tmp" / "run.log").write_text("NEEDLE\n", encoding="utf-8")
        assert _git(project_dir / "tmp", "ls-files", "-co", "--exclude-standard", "-z") == ""

        out = search_files.func("NEEDLE", path="tmp")

        assert "run.log（1 处）" in out

    def test_root_gitignore_contents_rule(self, project_dir):
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("build/*\n", encoding="utf-8")
        (project_dir / "build").mkdir()
        (project_dir / "build" / "out.txt").write_text("NEEDLE\n", encoding="utf-8")
        assert _git(project_dir / "build", "ls-files", "-co", "--exclude-standard", "-z") == ""

        out = search_files.func("NEEDLE", path="build")

        assert "out.txt（1 处）" in out

    def test_root_gitignore_whole_directory(self, project_dir):
        """对照组：目录本身被忽略，旧代码就能搜到，改动不能把它弄坏。"""
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("logs/\n", encoding="utf-8")
        (project_dir / "logs").mkdir()
        (project_dir / "logs" / "run.log").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="logs")

        assert "run.log（1 处）" in out

    def test_star_with_negated_gitignore(self, project_dir):
        """`*` + `!.gitignore` 变体：清单里还剩 tmp/.gitignore 一个文件，**不为空**，
        所以"清单为空才退回遍历"管不到它——还得看目录里有没有被忽略的文件。"""
        _git(project_dir, "init")
        (project_dir / "tmp").mkdir()
        (project_dir / "tmp" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
        (project_dir / "tmp" / "run.log").write_text("NEEDLE\n", encoding="utf-8")
        listing = _git(project_dir / "tmp", "ls-files", "-co", "--exclude-standard", "-z")
        assert listing == ".gitignore\0"

        out = search_files.func("NEEDLE", path="tmp")

        assert "run.log（1 处）" in out

    def test_naming_the_project_root_still_respects_ignore(self, project_dir):
        """点名的目录正好是项目根时不算"点名子目录"，否则等于给默认搜索开了后门。"""
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        (project_dir / "private.txt").write_text("NEEDLE\n", encoding="utf-8")

        for path in (".", str(project_dir)):
            out = search_files.func("NEEDLE", path=path)
            assert "未在" in out, path
            assert "private.txt" not in out, path

    def test_named_mixed_directory_still_respects_ignore(self, project_dir):
        """这条上一轮断言"被忽略的 secret.log 也一起搜到"，现在反过来。

        上一轮的规则是"点名目录里只要有一个被忽略文件就整目录遍历"。普通源码目录几乎总有
        被忽略的日志 / .env / __pycache__，那条规则等于点名子目录就不再遵守 .gitignore，
        放密钥的 .env.local 会被搜出来交给模型。现在改成问 Git"在这个目录里新建一个普通
        文件会不会被忽略"：`mixed/<探测名>` 不被忽略（规则只匹配 `*.log`），所以照常用
        Git 清单，secret.log 不出现。
        """
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("mixed/*.log\n", encoding="utf-8")
        (project_dir / "mixed").mkdir()
        (project_dir / "mixed" / "kept.py").write_text("NEEDLE\n", encoding="utf-8")
        (project_dir / "mixed" / "secret.log").write_text("NEEDLE\n", encoding="utf-8")
        _git(project_dir, "add", ".gitignore", "mixed/kept.py")

        out = search_files.func("NEEDLE", path="mixed")

        assert "kept.py（1 处）" in out
        assert "secret.log" not in out


class TestNamedSourceDirectoryKeepsIgnoreRules:
    """点名普通源码目录时忽略规则照旧生效——上一轮"有被忽略文件就遍历"的回归出在这里。"""

    def test_log_and_env_local_are_not_searched(self, project_dir):
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("*.log\n.env.local\n", encoding="utf-8")
        (project_dir / "src").mkdir()
        (project_dir / "src" / "app.py").write_text("NEEDLE = '已跟踪'\n", encoding="utf-8")
        _git(project_dir, "add", ".gitignore", "src/app.py")
        (project_dir / "src" / "new.py").write_text("NEEDLE = '未跟踪'\n", encoding="utf-8")
        (project_dir / "src" / "debug.log").write_text("NEEDLE 日志\n", encoding="utf-8")
        (project_dir / "src" / ".env.local").write_text("NEEDLE=密钥\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="src")

        assert "app.py（1 处）" in out and "new.py（1 处）" in out
        assert "debug.log" not in out and ".env.local" not in out

    def test_unanchored_build_rule_does_not_match_deep_build(self, project_dir):
        """根 .gitignore 的 `build/*` 只锚在仓库根：deep/build 里的文件本来就不被忽略，
        点名 deep/build 要能搜到（探测路径 deep/build/<名> 不匹配 `build/*`）。"""
        _git(project_dir, "init")
        (project_dir / ".gitignore").write_text("build/*\n", encoding="utf-8")
        (project_dir / "deep" / "build").mkdir(parents=True)
        (project_dir / "deep" / "build" / "out.txt").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="deep/build")

        assert "out.txt（1 处）" in out


class TestNonGitFallback:
    def test_non_git_project_searches_everything(self, project_dir):
        (project_dir / ".gitignore").write_text("chat_memory/\n", encoding="utf-8")
        (project_dir / "app.py").write_text("NEEDLE\n", encoding="utf-8")
        (project_dir / "chat_memory").mkdir()
        (project_dir / "chat_memory" / "sess.json").write_text("NEEDLE\n", encoding="utf-8")
        (project_dir / "node_modules").mkdir()
        (project_dir / "node_modules" / "noise.py").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE")

        assert "app.py（1 处）" in out
        assert "chat_memory/sess.json（1 处）" in out
        assert "noise.py" not in out


class TestSubdirectoryProjectRoot:
    def test_repo_one_level_above_project_root(self, project_dir):
        repo = project_dir.parent
        _git(repo, "init")
        (repo / ".gitignore").write_text("chat_memory/\n", encoding="utf-8")
        (repo / "outside.py").write_text("NEEDLE\n", encoding="utf-8")
        (project_dir / "src").mkdir()
        (project_dir / "src" / "app.py").write_text("NEEDLE\n", encoding="utf-8")
        _git(repo, "add", ".gitignore", "outside.py", f"{project_dir.name}/src/app.py")
        (project_dir / "new.py").write_text("NEEDLE\n", encoding="utf-8")
        (project_dir / "chat_memory").mkdir()
        (project_dir / "chat_memory" / "sess.json").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE")

        assert "src/app.py（1 处）" in out
        assert "new.py（1 处）" in out
        assert f"{project_dir.name}/src/app.py" not in out
        assert "outside.py" not in out and "sess.json" not in out

    @pytest.mark.parametrize("rule_in_repo_root", [True, False])
    def test_named_ignored_directory_inside_subdirectory_project(self, project_dir, rule_in_repo_root):
        """项目根是仓库子目录时，探测路径要相对**仓库根**拼（不是相对项目根），
        否则 check-ignore 找不到那条规则，点名被忽略的目录又搜不到了。"""
        repo = project_dir.parent
        _git(repo, "init")
        if rule_in_repo_root:
            (repo / ".gitignore").write_text(f"{project_dir.name}/build/*\n", encoding="utf-8")
        else:
            (project_dir / ".gitignore").write_text("build/*\n", encoding="utf-8")
        (project_dir / "build").mkdir()
        (project_dir / "build" / "out.txt").write_text("NEEDLE\n", encoding="utf-8")

        out = search_files.func("NEEDLE", path="build")

        assert "out.txt（1 处）" in out


class TestSharedFileList:
    @pytest.mark.parametrize("tracked", [False, True])
    @pytest.mark.parametrize("relative", [" a.py", " 空格目录/a.py"])
    def test_leading_whitespace_in_git_paths_is_preserved(self, project_dir, tracked, relative):
        _git(project_dir, "init")
        path = project_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NEEDLE\n", encoding="utf-8")
        if tracked:
            _git(project_dir, "add", "--", relative)

        out = search_files.func("NEEDLE")

        assert f"{relative}（1 处）" in out
        assert list(code_graph.project_files(str(project_dir), (".py",))) == [(str(path), relative)]

    @pytest.mark.parametrize("use_git", [False, True])
    def test_project_files_supports_filename_patterns(self, project_dir, use_git):
        if use_git:
            _git(project_dir, "init")
        for name in ("test_a.py", "app.py", "test_b.ts", "README", "notes.txt"):
            (project_dir / name).write_text("NEEDLE\n", encoding="utf-8")

        files = list(code_graph.project_files(str(project_dir), patterns=["test_*.py", "README"]))

        assert {rel for _, rel in files} == {"test_a.py", "README"}
        assert all(absolute == str(project_dir / rel) for absolute, rel in files)
        assert {rel for _, rel in code_graph.project_files(str(project_dir), (".py",))} == {
            "test_a.py", "app.py"}
        assert list(code_graph.project_files(str(project_dir), ())) == []

    def test_search_and_code_map_agree_on_source_files(self, ignored_repo):
        for path in ("src/app.py", "new.py", "chat_memory/hidden.py"):
            (ignored_repo / path).write_text("def shared_symbol():\n    pass\n", encoding="utf-8")

        search = search_files.func("shared_symbol", file_pattern="*.py")
        mapped = code_map.func()

        for name in ("src/app.py", "new.py"):
            assert name in search and name in mapped
        assert "hidden.py" not in search and "hidden.py" not in mapped

    def test_directory_search_keeps_file_size_limit(self, ignored_repo):
        (ignored_repo / "large.py").write_text("NEEDLE\n" + "x" * 1_100_000, encoding="utf-8")

        assert "large.py" not in search_files.func("NEEDLE")
        assert "large.py" not in search_files.func("NEEDLE", include_ignored=True)


class TestNestedRepo:
    @pytest.mark.parametrize("gitlink", [False, True])
    def test_does_not_recurse_from_parent_but_named_path_works(self, ignored_repo, gitlink):
        nested = ignored_repo / "nested"
        nested.mkdir()
        _git(nested, "init")
        (nested / "inner.py").write_text("NEEDLE\n", encoding="utf-8")
        _git(nested, "add", "inner.py")
        _git(nested, "-c", "user.name=Tester", "-c", "user.email=test@example.com",
             "commit", "-m", "测试嵌套仓库")
        if gitlink:
            _git(ignored_repo, "add", "nested")

        out = search_files.func("NEEDLE")

        assert "src/app.py（1 处）" in out
        assert "inner.py" not in out
        assert "inner.py（1 处）" in search_files.func("NEEDLE", path="nested")
        assert "nested/inner.py（1 处）" in search_files.func("NEEDLE", include_ignored=True)
