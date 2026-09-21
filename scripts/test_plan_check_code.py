"""Plan 模式不允许执行 check_code。

`check_code` 名字听起来像只读分析，但只要配了 config 的 `check_command` 它就执行那条命令，
那是**用户配的任意命令**（`_run_code_check` 先判 check_command、再判扩展名，
所以 Python 文件同样走它）。Plan 模式承诺"只调研、不动手"，所以这条必须在程序的实际分发
入口挡住——只在提示词里劝模型别调不算拦截，模型不听话的时候恰恰是最需要它生效的时候。

因此本文件一律走真实的 `streaming._execute_tool`，并用一条**会创建标记文件**的
`check_command` 当探针：文件在不在，就是"子进程到底起没起来"的硬证据。
只断言白名单里少了一个名字是不够的——那证明不了分发路径真的拦住了。
"""
import sys

import pytest

from src import config, session, streaming


def _plan_reject_text(history):
    """取最后一条 ToolMessage 的文本。"""
    from langchain_core.messages import ToolMessage
    for msg in reversed(history):
        if isinstance(msg, ToolMessage):
            return msg
    return None


class _UI:
    def __init__(self):
        self.messages = []

    def show_message(self, text, tag="ai_msg"):
        self.messages.append(str(text))

    def render_final_markdown(self, text, speak=True):
        self.messages.append(str(text))

    def show_retry(self, text):
        self.messages.append(str(text))

    def show_token_usage(self, *a, **k):
        pass

    def remove_thinking_indicator(self):
        pass

    def text(self):
        return "".join(self.messages)


@pytest.fixture()
def marker_probe(project_dir, monkeypatch):
    """配一条会写标记文件的 check_command，并给一个非 Python 的目标文件。

    标记文件是唯一可信的判据：`check_command` 真被执行过，它才会出现。
    """
    marker = project_dir / "check_ran.marker"
    target = project_dir / "x.rs"
    target.write_text("fn main(){}", encoding="utf-8")
    command = (
        f'"{sys.executable}" -c '
        f'"import pathlib; pathlib.Path(r\'{marker}\').write_text(\'ran\')"'
    )
    monkeypatch.setattr(config, "CHECK_COMMAND", command)
    # 会话级 chat_history 要干净，拒绝信息才好定位
    sess = session.get_active()
    sess.chat_history = []
    return marker, sess


def _dispatch(name, args, call_id, ui=None):
    streaming._execute_tool({"name": name, "args": args, "id": call_id}, ui or _UI())


class TestPlanModeBlocksCheckCode:
    def test_plan_rejects_before_any_subprocess(self, marker_probe, monkeypatch):
        """Plan 调用 check_code：不 invoke、不起子进程、不留标记文件。"""
        marker, sess = marker_probe
        sess.agent_mode = "plan"
        invoked = {"n": 0}

        real_map = streaming.get_tool_map()

        class _Spy:
            def invoke(self, args):
                invoked["n"] += 1
                return real_map["check_code"].invoke(args)

        monkeypatch.setattr(streaming, "get_tool_map",
                            lambda: {**real_map, "check_code": _Spy()})

        _dispatch("check_code", {"path": "x.rs"}, "call-plan-1")

        assert invoked["n"] == 0, "工具根本不该被 invoke"
        assert not marker.exists(), "check_command 起过子进程了——拦截没有发生在执行之前"

    def test_plan_rejection_is_returned_against_the_right_tool_call_id(self, marker_probe):
        marker, sess = marker_probe
        sess.agent_mode = "plan"

        _dispatch("check_code", {"path": "x.rs"}, "call-plan-2")

        msg = _plan_reject_text(sess.chat_history)
        assert msg is not None, "必须回一条工具结果，否则模型这一轮的协议是断的"
        assert msg.tool_call_id == "call-plan-2"
        assert "Plan 模式只进行调研" in msg.content
        assert "切换到 Act 模式" in msg.content

    def test_plan_rejection_tells_the_model_not_to_work_around_it(self, marker_probe):
        """通用文案会让模型以为是误判，转头用 run_command 跑等价的 ruff。要明说别绕。"""
        marker, sess = marker_probe
        sess.agent_mode = "plan"
        _dispatch("check_code", {"path": "x.rs"}, "call-plan-3")

        content = _plan_reject_text(sess.chat_history).content
        assert "不要自行改模式" in content
        assert "不要改用别的工具" in content

    def test_plan_blocks_check_code_even_without_a_check_command(self, project_dir,
                                                                 monkeypatch):
        """没配 check_command 也一样拦——权限不取决于用户配没配东西。"""
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        sess = session.get_active()
        sess.chat_history = []
        sess.agent_mode = "plan"
        (project_dir / "a.py").write_text("x = 1\n", encoding="utf-8")

        _dispatch("check_code", {"path": "a.py"}, "call-plan-4")

        msg = _plan_reject_text(sess.chat_history)
        assert msg is not None and msg.tool_call_id == "call-plan-4"
        assert "Plan 模式只进行调研" in msg.content

    def test_plan_still_allows_ordinary_read_only_tools(self, project_dir):
        """别误伤：Plan 的正常调研工具照常可用。"""
        sess = session.get_active()
        sess.chat_history = []
        sess.agent_mode = "plan"
        (project_dir / "readme.txt").write_text("hello plan", encoding="utf-8")

        _dispatch("read_file", {"path": "readme.txt"}, "call-plan-5")

        msg = _plan_reject_text(sess.chat_history)
        assert msg is not None and msg.tool_call_id == "call-plan-5"
        assert "hello plan" in msg.content
        assert "已拒绝执行" not in msg.content


class TestActModeStillRunsCheckCode:
    def test_act_actually_executes_the_configured_command(self, marker_probe):
        """Act 模式必须照常跑——标记文件出现，证明没有误伤实际执行。"""
        marker, sess = marker_probe
        sess.agent_mode = "act"
        assert not marker.exists()

        _dispatch("check_code", {"path": "x.rs"}, "call-act-1")

        assert marker.exists(), "Act 下 check_command 没被执行，拦截误伤了正常路径"
        msg = _plan_reject_text(sess.chat_history)
        assert msg is not None and msg.tool_call_id == "call-act-1"
        assert "已拒绝执行" not in msg.content

    def test_act_python_check_still_works(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "CHECK_COMMAND", "")
        sess = session.get_active()
        sess.chat_history = []
        sess.agent_mode = "act"
        (project_dir / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")

        _dispatch("check_code", {"path": "bad.py"}, "call-act-2")

        content = _plan_reject_text(sess.chat_history).content
        assert "检查发现问题" in content


class TestCheckCodeRecordingInvariants:
    """B03 的两条约束不能因为这次改动松掉。"""

    def test_check_code_still_needs_a_pre_execution_record(self):
        from src import run_records
        assert run_records.needs_record("check_code") is True

    def test_check_code_still_cannot_be_parallel_preinvoked(self):
        """并行预取会在 `_execute_tool` 之前把工具跑掉，那时 Plan 闸和执行前记录都还没过。"""
        sess = session.get_active()
        sess.agent_mode = "act"
        calls = [{"name": "check_code", "args": {"path": "a.py"}, "id": "1"},
                 {"name": "check_code", "args": {"path": "b.py"}, "id": "2"}]
        assert streaming._can_parallel(calls) is False

    def test_plan_mode_disables_parallel_preinvoke_entirely(self, project_dir):
        sess = session.get_active()
        sess.agent_mode = "plan"
        calls = [{"name": "read_file", "args": {"path": "a"}, "id": "1"},
                 {"name": "read_file", "args": {"path": "b"}, "id": "2"}]
        assert streaming._can_parallel(calls) is False


class TestPlanWhitelistDeclaration:
    def test_check_code_is_not_in_the_plan_whitelist(self):
        assert "check_code" not in streaming.PLAN_MODE_READONLY_TOOLS

    def test_run_tests_is_not_either(self):
        """同一条判据：它起的是项目自己的测试代码。"""
        assert "run_tests" not in streaming.PLAN_MODE_READONLY_TOOLS

    def test_plan_prompt_no_longer_implies_check_code_is_allowed(self):
        from src import roles
        sess = session.get_active()
        sess.agent_mode = "plan"
        text = roles.get_volatile_context()
        assert "check_code" in text and "❌ 禁止" in text
        allowed_line = next(line for line in text.splitlines() if "✅ 允许" in line)
        assert "check_code" not in allowed_line
