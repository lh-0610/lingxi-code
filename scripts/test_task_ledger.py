"""M1 自动任务台账（Task Ledger）测试。

覆盖规格"自检"中全部 5 组用例：
  1. helper 单元测试（不依赖 UI）
  2. 会话字段（Session 默认值 / 读写独立性）
  3. 注入（roles.get_system_prompt 包含台账内容）
  4. reset（reset_history 后台账清空）
  5. 全量 pytest 不破坏其它测试（跑 scripts/ 验证）
"""
from src import state
from src import session as _session
from src import memory


# ──────────────────────────────────────
# 1. helper 单元测试
# ──────────────────────────────────────

class TestHelperRecordTool:
    """record_tool_in_ledger + render_task_ledger 核心逻辑。"""

    def test_edit_file_records_path(self):
        """edit_file 成功 → files[path] = '已编辑'。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "edit_file", {"path": "a.py"}, "成功编辑 a.py")
        assert led["files"]["a.py"] == "已编辑"

    def test_write_file_updates_same_path(self):
        """同一文件先 edit 再 write → 动作词更新，files 里只有一条。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "edit_file", {"path": "a.py"}, "成功编辑 a.py")
        state.record_tool_in_ledger(led, "write_file", {"path": "a.py"}, "成功写入 a.py")
        assert led["files"]["a.py"] == "已创建/覆盖"
        assert len(led["files"]) == 1

    def test_run_tests_and_max_commands(self):
        """run_tests 记进 commands；连记 10 次 → 只留最近 8 条。"""
        led = state.new_task_ledger()
        for i in range(10):
            state.record_tool_in_ledger(led, "run_tests", {}, f"测试结果第{i}次通过 blah blah")
        assert len(led["commands"]) == 8
        # 最老的两条（0,1）已丢掉
        assert "第2次" in led["commands"][0]["brief"]

    def test_failed_result_not_recorded(self):
        """result 以 '工具执行失败' 开头的调用不记入台账。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "edit_file", {"path": "b.py"}, "工具执行失败: 权限不足")
        assert "b.py" not in led["files"]
        state.record_tool_in_ledger(led, "run_command", {"command": "ls"}, "工具执行失败: 超时")
        assert len(led["commands"]) == 0

    def test_rejected_edit_not_recorded(self):
        """用户拒绝写入（_confirm_file_write 返回 '已拒绝…'）不记——否则把没真改的文件误记成已改。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "edit_file", {"path": "c.py"}, "已拒绝：用户不允许此次写入。")
        assert "c.py" not in led["files"]

    def test_failed_edit_not_recorded(self):
        """edit_file 自身失败（'失败：…'，如 old_string 没匹配 / 文件不存在）不记。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "edit_file", {"path": "d.py"}, "失败：文件不存在 d.py")
        assert "d.py" not in led["files"]
        state.record_tool_in_ledger(led, "write_file", {"path": "e.py"}, "失败：路径超出项目范围，不允许")
        assert "e.py" not in led["files"]

    def test_render_empty_and_non_empty(self):
        """空台账 → 空串；记了东西后含文件名/命令。"""
        led = state.new_task_ledger()
        assert state.render_task_ledger(led) == ""
        state.record_tool_in_ledger(led, "edit_file", {"path": "x.py"}, "成功")
        txt = state.render_task_ledger(led)
        assert "x.py" in txt
        assert "已编辑" in txt

    def test_render_with_commands(self):
        """run_command 记录渲染后包含命令和 brief。"""
        led = state.new_task_ledger()
        state.record_tool_in_ledger(led, "run_command", {"command": "pytest -x"}, "1 passed in 0.5s")
        txt = state.render_task_ledger(led)
        assert "pytest -x" in txt
        assert "1 passed" in txt

    def test_none_ledger_returns_empty(self):
        """传 None 或非 dict → 空串，不崩。"""
        assert state.render_task_ledger(None) == ""  # type: ignore[arg-type]
        assert state.render_task_ledger("not a dict") == ""  # type: ignore[arg-type]
        state.record_tool_in_ledger(None, "edit_file", {"path": "x"}, "ok")  # type: ignore[arg-type]

    def test_run_command_records_command(self):
        """run_command 的 cmd 取 args['command'] 截断 60 字。"""
        led = state.new_task_ledger()
        long_cmd = "echo " + "x" * 100
        state.record_tool_in_ledger(led, "run_command", {"command": long_cmd}, "ok")
        assert len(led["commands"]) == 1
        assert len(led["commands"][0]["cmd"]) <= 60


# ──────────────────────────────────────
# 2. 会话字段
# ──────────────────────────────────────

class TestSessionField:
    """state.task_ledger 读写 + Session 独立性。"""

    def test_state_task_ledger_default(self):
        """state.task_ledger 默认值是空台账。"""
        tl = state.task_ledger
        assert isinstance(tl, dict)
        assert tl == {"files": {}, "commands": []}

    def test_state_task_ledger_write_read(self):
        """写入后读回一致。"""
        state.task_ledger["files"]["hello.py"] = "已编辑"
        assert state.task_ledger["files"]["hello.py"] == "已编辑"

    def test_session_independent_dicts(self):
        """新建 Session() 的 task_ledger 是独立空 dict（不共享同一对象）。"""
        s1 = _session.Session()
        s2 = _session.Session()
        s1.task_ledger["files"]["a.py"] = "已编辑"
        assert "a.py" not in s2.task_ledger["files"]
        assert s1.task_ledger is not s2.task_ledger


# ──────────────────────────────────────
# 3. 注入
# ──────────────────────────────────────

class TestInjection:
    """roles.get_system_prompt() 末尾包含台账内容。"""

    def test_ledger_in_system_prompt(self):
        """手动写入台账后，system prompt 包含文件名和'当前任务进度'。"""
        state.task_ledger["files"]["x.py"] = "已编辑"
        from src import roles
        prompt = roles.get_system_prompt()
        assert "x.py" in prompt
        assert "当前任务进度" in prompt
        assert "已改动的文件" in prompt

    def test_empty_ledger_not_injected(self):
        """空台账不注入。"""
        state.task_ledger = state.new_task_ledger()
        from src import roles
        prompt = roles.get_system_prompt()
        assert "当前任务进度" not in prompt


# ──────────────────────────────────────
# 4. reset
# ──────────────────────────────────────

class TestReset:
    """reset_history() 后台账清空。"""

    def test_reset_clears_ledger(self, isolated_memory):
        """reset_history() 后 task_ledger == 空台账。"""
        state.task_ledger["files"]["z.py"] = "已编辑"
        state.task_ledger["commands"].append({"cmd": "ls", "brief": "ok"})
        memory.reset_history()
        assert state.task_ledger == {"files": {}, "commands": []}
