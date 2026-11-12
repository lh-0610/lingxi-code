"""搜索和 git_diff 交回的内容有上限 —— 工具结果一进上下文，之后每一轮都要重发。

真实任务里见过的两类：
- 搜项目根，一条结果 3 万字（约 1 万 token），其中 95% 是会话 JSON 里一行两万多字的 "content"；
- 模型给 git_diff 传 max_chars=20000~30000 把全部改动再看一遍，内容和它自己发出的改动重复。

单个文件的 diff 不设这个上限：那是看全一个大改动的唯一办法（非 Git 项目的审阅义务也靠它了结，
见 test_b11a_file_history 的 test_a_file_too_long_to_show_keeps_the_gate_closed）。
"""
import shutil
import subprocess

import pytest

from src.limits import GIT_DIFF_OVERVIEW_MAX_CHARS, SEARCH_FILES_MAX_CHARS, SEARCH_LINE_MAX_CHARS
from src.tools import _clip_search_line, git_diff, search_files, search_in_file
from src.tools_git import _clip_diff, _diff_char_limit


# ══════════════════════════════════════
# 搜索
# ══════════════════════════════════════

class TestSearchLineClip:
    def test_short_line_untouched(self):
        assert _clip_search_line("def foo(): pass") == "def foo(): pass"

    def test_long_line_keeps_the_match(self):
        line = "a" * 5000 + "NEEDLE" + "b" * 5000
        out = _clip_search_line(line, line.index("NEEDLE"))
        assert "NEEDLE" in out, "截的时候要留住匹配的地方，不是只留行首"
        assert out.startswith("…") and f"共 {len(line)} 字" in out
        assert len(out) < SEARCH_LINE_MAX_CHARS + 60

    def test_match_near_the_end(self):
        line = "a" * 5000 + "NEEDLE"
        out = _clip_search_line(line, line.index("NEEDLE"))
        assert "NEEDLE" in out and len(out) < SEARCH_LINE_MAX_CHARS + 60

    def test_search_files_clips_long_lines(self, project_dir):
        long_line = '{"content": "' + "x" * 20_000 + ' reasoning_effort ' + "y" * 20_000 + '"}'
        (project_dir / "data.json").write_text(long_line + "\n", encoding="utf-8")
        (project_dir / "a.py").write_text("reasoning_effort = 'high'\n", encoding="utf-8")
        out = search_files.func("reasoning_effort")
        assert "data.json（1 处）" in out and "a.py（1 处）" in out
        assert f"共 {len(long_line)} 字" in out
        assert len(out) < 1500, f"一行四万字的匹配不该整行交回，实际 {len(out)} 字"

    def test_context_lines_clipped_too(self, project_dir):
        (project_dir / "m.js").write_text("z" * 9000 + "\nneedle()\n" + "w" * 9000 + "\n", encoding="utf-8")
        out = search_files.func("needle", context=1)
        assert "needle()" in out and len(out) < 1500

    def test_search_in_file_clips_long_lines(self, project_dir):
        (project_dir / "log.txt").write_text("p" * 8000 + "ERROR here" + "q" * 8000 + "\n", encoding="utf-8")
        out = search_in_file.func("log.txt", "error")
        assert "ERROR here" in out and len(out) < 600


class TestSearchTotalCap:
    def _many(self, project_dir, files=40, per_file=1):
        for i in range(files):
            body = "".join(f"# filler line {j} of a file with some ordinary length to it\n" for j in range(6))
            (project_dir / f"mod_{i:02d}.py").write_text(
                body + "".join(f"target_{k} = compute_something({k})  # {'c' * 120}\n" for k in range(per_file)) + body,
                encoding="utf-8")

    def test_over_the_cap_is_cut_at_a_line_and_says_so(self, project_dir):
        self._many(project_dir, files=18)                 # 18 处 ≤ 20，带上下文
        out = search_files.func(r"target_\d+ =", context=5)
        body = out.split("\n", 1)[1]
        assert len(body) < SEARCH_FILES_MAX_CHARS + 200
        tail = out.splitlines()[-1]
        assert "只列了前" in tail and "context=0" in tail
        listed = sum(1 for ln in out.splitlines() if "target_" in ln and ": target_" in ln)
        assert f"只列了前 {listed} 处，还有 {18 - listed} 处未列出" in tail
        assert out.splitlines()[-2].lstrip()[0].isdigit(), "按行截，最后一行是完整的结果行"

    def test_under_the_cap_unchanged(self, project_dir):
        self._many(project_dir, files=3)
        out = search_files.func(r"target_\d+ =")
        assert "只列了前" not in out and out.count(": target_") == 3

    def test_max_results_note_kept_when_not_clipped(self, project_dir):
        self._many(project_dir, files=2, per_file=5)
        out = search_files.func(r"target_\d+ =", max_results=4, context=0)
        assert "仅显示前 4 处，还有 6 处未列出" in out


# ══════════════════════════════════════
# git_diff
# ══════════════════════════════════════

def _diff_section(name, lines):
    return (f"diff --git a/{name} b/{name}\nindex 111..222 100644\n--- a/{name}\n+++ b/{name}\n@@ -1 +1,{lines} @@\n"
            + "".join(f"+line {i} of {name} {'.' * 40}\n" for i in range(lines)) + "-old\n")


class TestClipDiff:
    def test_under_limit_untouched(self):
        text = _diff_section("a.py", 5)
        assert _clip_diff(text, 8000, single=False)[0] is text

    def test_cut_at_file_boundaries_and_lists_the_rest(self):
        text = _diff_section("a.py", 40) + _diff_section("b.py", 80) + _diff_section("c.py", 3)
        out = _clip_diff(text, 3000, single=False)[0]
        assert _diff_section("a.py", 40).rstrip("\n") in out, "放得下的文件整段给"
        assert "line 0 of b.py" not in out, "放不下的文件不给半截"
        assert "line 0 of c.py" not in out, "后面的文件不越过去塞进来，顺序和原 diff 一致"
        tail = out.rsplit("... [", 1)[1]
        assert "只完整展示了前 1 个文件" in tail
        assert "b.py（+80 −1）" in tail and "c.py（+3 −1）" in tail
        assert 'git_diff(path="那个文件")' in tail

    def test_first_file_alone_too_big(self):
        text = _diff_section("big.py", 400) + _diff_section("b.py", 2)
        out = _clip_diff(text, 2000, single=False)[0]
        assert out.startswith("diff --git a/big.py") and "只展示了它的开头" in out
        assert "big.py（+400 −1）" in out and "b.py（+2 −1）" in out

    def test_single_file_cut_plainly(self):
        text = _diff_section("a.py", 400)
        out = _clip_diff(text, 2000, single=True)[0]
        assert out.startswith(text[:2000]) and "max_chars 调大" in out

    def test_reports_which_files_were_shown_in_full(self):
        """第二项是审阅记录的依据：只有整段留下的算看过，半截的一个都不算。"""
        text = _diff_section("a.py", 40) + _diff_section("b.py", 80) + _diff_section("c.py", 3)
        assert _clip_diff(text, 8000, single=False)[1] == ["a.py", "b.py", "c.py"]
        assert _clip_diff(text, 3000, single=False)[1] == ["a.py"]
        assert _clip_diff(_diff_section("big.py", 400), 2000, single=False)[1] == []
        assert _clip_diff(_diff_section("a.py", 400), 2000, single=True)[1] == []


class TestDiffCharLimit:
    def test_overview_is_capped_whatever_is_asked(self, project_dir):
        (project_dir / "src").mkdir()
        assert _diff_char_limit("", 30000) == (GIT_DIFF_OVERVIEW_MAX_CHARS, False)
        assert _diff_char_limit("src", 30000) == (GIT_DIFF_OVERVIEW_MAX_CHARS, False)
        assert _diff_char_limit("", 3000) == (3000, False)

    def test_single_file_is_not_capped(self, project_dir):
        (project_dir / "a.py").write_text("x\n", encoding="utf-8")
        assert _diff_char_limit("a.py", 30000) == (30000, True)
        assert _diff_char_limit("deleted.py", 30000) == (30000, True), "删掉的文件也是单个文件"

    def test_garbage_falls_back(self, project_dir):
        assert _diff_char_limit("", "很多") == (GIT_DIFF_OVERVIEW_MAX_CHARS, False)


@pytest.fixture()
def git_repo(project_dir):
    if not shutil.which("git"):
        pytest.skip("git 未安装")

    def git(*args):
        subprocess.run(["git", *args], cwd=str(project_dir), check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Tester")
    for name in ("a.py", "b.py", "c.py"):
        (project_dir / name).write_text("old\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")
    for name, n in (("a.py", 30), ("b.py", 300), ("c.py", 300)):
        (project_dir / name).write_text("".join(f"{name} line {i} {'.' * 40}\n" for i in range(n)), encoding="utf-8")
    return project_dir


class TestGitDiffTool:
    def test_whole_project_capped_even_with_large_max_chars(self, git_repo):
        out = git_diff.func("", max_chars=30000)
        assert len(out) < GIT_DIFF_OVERVIEW_MAX_CHARS + 400
        assert "a.py line 29" in out and "b.py line 0" not in out
        assert "b.py（+300 −1）" in out and "c.py（+300 −1）" in out

    def test_single_file_shown_in_full(self, git_repo):
        out = git_diff.func("b.py", max_chars=30000)
        assert "b.py line 299" in out and "输出已截断" not in out

    def test_uncovered_note_survives_clipping(self, git_repo):
        """"还有没看到的改动"是完成闸门要模型看到的话，以前会跟着超长的 diff 一起被截掉。"""
        (git_repo / "staged.py").write_text("s = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged.py"], cwd=str(git_repo), check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
        for name in ("a.py", "b.py", "c.py"):
            subprocess.run(["git", "add", name], cwd=str(git_repo), check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
        for i in range(12):                                # 未跟踪的新文件：10 个附上、2 个只列名字
            (git_repo / f"new_{i:02d}.py").write_text(f"n = {i}\n" * 200, encoding="utf-8")
        out = git_diff.func("")
        assert "已暂存的修改" in out and "staged.py" in out
        assert out.rstrip().endswith("用 git_diff(path=...) 逐个查看")
