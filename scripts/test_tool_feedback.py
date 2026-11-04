"""工具的反馈让模型少白跑几轮（来自密钥打码那次 68 轮、87 次调用的复盘 + 全部日志里的报错归类）。

- edit_file 改完回显那一段：以前只回"第 42 行附近替换 1 处"，模型常常再读一遍确认；
- 只更新进度的一轮补一句提醒：68 轮里 7 轮只调了 set_step_status / update_plan；
- run_code 放行精简版 re、别的 import 指向 run_command 的 stdin：日志里 11 次 import 被拒，5 次是 re。
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from src import agent, state
from src.tools import edit_file
from src.tools_code import CodeRejected, run_program


# ── edit_file 回显 ──

def _lines(n):
    return "".join(f"line{i}\n" for i in range(1, n + 1))


def test_an_edit_shows_the_changed_part_with_line_numbers(project_dir):
    target = project_dir / "a.txt"
    target.write_text(_lines(20), encoding="utf-8")
    out = edit_file.func("a.txt", "line10\n", "first\nsecond\n")
    assert out.startswith("成功编辑"), "台账按「成功」开头认"
    assert "改完这一段现在是" in out
    for row in ("   8: line8", "  10: first", "  11: second", "  13: line12"):
        assert row in out, out
    assert "line5" not in out, "只带前后两行"


def test_a_long_replacement_shows_only_its_ends(project_dir):
    target = project_dir / "a.txt"
    target.write_text(_lines(10), encoding="utf-8")
    body = "".join(f"new{i}\n" for i in range(30))
    out = edit_file.func("a.txt", "line5\n", body)
    assert "new0" in out and "new29" in out
    assert "new15" not in out and "中间 17 行是你刚写的" in out


def test_a_deletion_shows_where_it_was(project_dir):
    target = project_dir / "a.txt"
    target.write_text(_lines(10), encoding="utf-8")
    out = edit_file.func("a.txt", "line5\n", "")
    assert "   5: line6" in out and "   4: line4" in out


# ── 只记进度的一轮 ──

def _round(monkeypatch, calls):
    state.chat_history = []
    monkeypatch.setattr(agent, "_build_ai_message", lambda g, t, tcs: AIMessage(content="", tool_calls=tcs))
    monkeypatch.setattr(agent, "_execute_tool", lambda tc, ui, _preinvoked=None: state.chat_history.append(
        ToolMessage(content=f"{tc['name']} 完成", tool_call_id=tc["id"])))
    agent._run_tool_calls(SimpleNamespace(render_final_markdown=lambda *a, **k: None), calls, None, "")
    return [m.content for m in state.chat_history if isinstance(m, ToolMessage)]


def test_a_round_that_only_updates_progress_gets_a_nudge(monkeypatch):
    results = _round(monkeypatch, [{"name": "set_step_status", "args": {"step": 2, "status": "in_progress"},
                                    "id": "c1"}])
    assert results[-1].startswith("set_step_status 完成")
    assert "不用单独占一轮" in results[-1]


def test_progress_sent_along_with_real_work_is_left_alone(monkeypatch):
    results = _round(monkeypatch, [
        {"name": "set_step_status", "args": {"step": 2, "status": "in_progress"}, "id": "c1"},
        {"name": "read_file", "args": {"path": "a.py"}, "id": "c2"},
    ])
    assert not any("不用单独占一轮" in r for r in results)


# ── run_code 里的 import ──

def test_re_is_available_in_run_code():
    assert "['1', '22']" in run_program("import re\nprint(re.findall('[0-9]+', 'a1b22'))")
    assert "f00" in run_program("print(re.sub('o', '0', 'foo'))")
    assert "True" in run_program("import re as rx\nprint(bool(rx.search('b', 'abc')))")


def test_re_in_run_code_is_only_the_functions():
    """完整的 re 模块挂着 enum / functools，顺着 re.enum.sys 就能摸到整个运行环境。"""
    out = run_program("print(re.enum)")
    assert "has no attribute 'enum'" in out


def test_other_imports_point_to_run_command_stdin():
    for code in ("import os", "from pathlib import Path", "import re, os"):
        try:
            run_program(code)
        except CodeRejected as e:
            assert 'run_command(command="python -"' in str(e), str(e)
        else:
            raise AssertionError(f"{code} 应该被拒")


def test_missing_tools_and_open_say_what_to_use_instead():
    assert "这里只能调只读工具" in run_program("tools.check_code('a.py')")
    assert "读文件用 tools.read_file" in run_program("open('a.py')")
