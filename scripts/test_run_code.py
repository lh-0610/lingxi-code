"""代码模式（tools_code.run_code）：一段程序一轮里调好几次只读工具；越界的写法在执行前就拒绝。

安全测试守的是"模型顺手写出越界的东西"——import、碰下划线属性、经栈帧 / 格式串 / 回溯摸到全局变量、
直接读写文件、死循环。正常用法：位置参数对上工具签名、print 交回、顶层 return、出错报对行号。
"""
import pytest

from src import tools_code
from src.tools_code import CodeRejected, run_program


@pytest.fixture
def proj(project_dir):
    (project_dir / "a.py").write_text("def send():\n    return 1\n", encoding="utf-8")
    (project_dir / "b.py").write_text("x = 2\n", encoding="utf-8")
    return project_dir


def test_one_program_calls_several_read_tools(proj):
    out = run_program(
        'for p in ["a.py", "b.py"]:\n'
        '    print(p, tools.read_file(p).splitlines()[0])\n'
        'hits = tools.search_files(regex="def send", context=0)\n'
        'return len(hits.splitlines())\n')
    assert "a.py    1: def send():" in out and "b.py    1: x = 2" in out
    assert "返回值：" in out
    assert "程序调了 3 次工具：read_file、search_files" in out


def test_keyword_arguments_and_tool_errors_report_the_line(proj):
    out = run_program('print("ok")\ntools.read_file(path="a.py", offset=1, limit=1)\ntools.read_file()\n')
    assert out.startswith("ok")
    assert "❌ 第 3 行出错" in out


@pytest.mark.parametrize("code", [
    "import os",
    "from os import system",
    "__import__('os')",
    "print(().__class__.__bases__)",
    "print('{0.__class__}'.format(1))",
    "print('{x}'.format_map({}))",
    "g = (x for x in [1])\nprint(g.gi_frame.f_globals)",
    "try:\n    1/0\nexcept Exception as e:\n    print(e.__traceback__)",
    "class A:\n    pass",
    "print(str.mro())",
    "f = lambda: 1\nprint(f.__code__)",
    "_x = 1",
    "def _f():\n    pass",
    "with tools as t:\n    pass",
    "global x",
    "def f():\n    yield 1",
    "print(tools.__dict__)",
])
def test_out_of_bounds_code_is_refused_before_running(code):
    with pytest.raises(CodeRejected):
        tools_code.check_code(code)


@pytest.mark.parametrize("name", ["open", "eval", "exec", "compile", "getattr", "setattr", "vars",
                                  "globals", "locals", "type", "input", "breakpoint", "object"])
def test_dangerous_builtins_are_not_there(proj, name):
    out = run_program(f"print({name})")
    assert "NameError" in out


def test_endless_loop_is_stopped(proj):
    out = run_program("while True:\n    pass\n", time_limit=1)
    assert "超过 1 秒" in out


def test_output_is_capped(proj, monkeypatch):
    monkeypatch.setattr(tools_code, "_OUTPUT_MAX_CHARS", 100)
    out = run_program('print("x" * 500)')
    assert "只交回前 100 字" in out and len(out) < 400


def test_tool_reports_refusal_instead_of_raising(proj):
    assert tools_code.run_code.func("import os").startswith("❌ 程序没有运行")


def test_only_read_tools_are_reachable(proj):
    out = run_program("tools.write_file(path='c.py', content='x')")
    assert "AttributeError" in out and not (proj / "c.py").exists()


def test_registered_as_read_only_everywhere():
    from src import agent, run_records, streaming
    from src.tools import ALL_TOOLS, TOOL_DISPLAY_NAMES
    from src.tools_explore import EXPLORE_TOOL_NAMES
    assert "run_code" in {t.name for t in ALL_TOOLS} and "run_code" in TOOL_DISPLAY_NAMES
    for allowed in (streaming.PARALLEL_SAFE_TOOLS, streaming.PLAN_MODE_READONLY_TOOLS,
                    run_records.SIDE_EFFECT_FREE_TOOLS, agent._READ_ONLY_TOOLS, EXPLORE_TOOL_NAMES):
        assert "run_code" in allowed
    assert set(tools_code.CODE_TOOL_NAMES) <= set(run_records.SIDE_EFFECT_FREE_TOOLS)


def test_remote_readonly_mode_refuses_run_code(monkeypatch, clean_state):
    """程序能读黑名单保护的文件，遥控只读模式下不能放行。"""
    from unittest.mock import MagicMock
    from langchain_core.messages import ToolMessage
    from src import config, state, streaming
    monkeypatch.setattr(state, "remote_session", True)
    monkeypatch.setattr(config, "REMOTE_MODE", "safe_readonly")
    streaming._execute_tool({"name": "run_code", "args": {"code": "print(tools.read_file('config.json'))"},
                             "id": "1"}, MagicMock())
    last = state.chat_history[-1]
    assert isinstance(last, ToolMessage) and "拒绝" in last.content


def test_ui_summary_uses_description_or_first_line():
    from src.streaming import describe_tool_call
    assert describe_tool_call("run_code", {"code": "x = 1\nprint(x)", "description": "读两个文件"})[1] == "读两个文件"
    assert describe_tool_call("run_code", {"code": "x = 1\nprint(x)"})[1] == "x = 1"
    assert describe_tool_call("run_code", {"code": "   "})[1] == ""
