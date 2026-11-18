"""工具调用一行一条、连续的收成一组（设计稿「消息区：工具调用改成一行一条」）。

真 Qt 控件（offscreen），走 MessageView.handle 这条和实时 / 重画历史共用的入口。
"""
# 夹具从 test_message_view 导入，用例参数与导入同名是 pytest 夹具的正常写法
# ruff: noqa: F811
import pytest

from test_message_view import _pump, _round_with_reply, _tool, _view, qt_app  # noqa: F401


def _rows(mv):
    from src.ui.message_view import ToolCallCard
    return mv.findChildren(ToolCallCard)


def _groups(mv):
    from src.ui.message_view import _ToolGroup
    return mv.findChildren(_ToolGroup)


class TestGrouping:
    def test_one_or_two_calls_stay_flat_the_third_brings_a_header(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "📂 列出目录", ".", "a.py\nb.py\n")
        _tool(mv, "📖 读取文件", "a.py", "x = 1\n")
        mv.show()
        _pump(qt_app)
        (g,) = _groups(mv)
        assert not g.head.isVisibleTo(mv), "一两条直接平铺"
        _tool(mv, "📖 读取多个文件", "b.py、c.py", "...\n")
        _pump(qt_app)
        assert g.head.isVisibleTo(mv)
        assert g.summary_text() == "列了 1 个目录，读取了 3 个文件", "读多个文件按文件数算"
        mv.close()

    def test_edit_rows_stand_alone_and_split_groups(self, qt_app):
        from src.ui.message_view import _ToolGroup
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "📖 读取文件", "a.py", "x\n")
        _tool(mv, "🪄 精确编辑", "a.py", "成功编辑 a.py\n")
        _tool(mv, "📖 读取文件", "b.py", "y\n")
        mv.show()
        _pump(qt_app)
        edit = next(r for r in _rows(mv) if r.kind == "edit")
        p = edit.parentWidget()
        while p is not None and not isinstance(p, _ToolGroup):
            p = p.parentWidget()
        assert p is None, "改文件单独一行，不进组"
        assert len(_groups(mv)) == 2, "改文件把前后的调用分成两组"
        mv.close()

    def test_other_tools_are_counted_as_other(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "⚡ 执行命令", "ls", "退出码: 0\n")
        _tool(mv, "📌 Git 状态", "", "clean\n")
        _tool(mv, "📋 更新计划", "{}", "ok\n")
        (g,) = _groups(mv)
        assert g.summary_text() == "运行了 1 条命令，调用了 2 次其他工具"
        mv.close()


class TestOutcome:
    @pytest.mark.parametrize("output, tone, label", [
        ("退出码: 1\nFAILED test_x\n", "bad", "退出码 1"),
        ("collected 3 items\n3 passed\n\n✓ 退出码 0\n", "", ""),
        ("失败：文件不存在 /x\n", "bad", "失败"),
        ("读取失败: Permission denied\n", "bad", "读取失败"),
        ("❌ 测试失败\n", "bad", "失败"),
        ("⏹ 用户中断\n", "warn", "已停止"),
    ])
    def test_clear_signals_mark_the_row(self, qt_app, output, tone, label):
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "⚡ 执行命令", "pytest", output)
        (row,) = _rows(mv)
        assert row.tone == tone and row.status_text == label
        assert row._status.isVisibleTo(row) == bool(label)
        mv.close()

    @pytest.mark.parametrize("output", [
        "退出码: 0\ntests/test_error_handling.py::test_error PASSED\nFAILED=0\n",
        "def handle_error():\n    raise ValueError('失败的情况')\n",
        "Error: 这一行只是文件内容里的字\n",
    ])
    def test_words_like_error_or_failed_in_normal_output_are_not_a_failure(self, qt_app, output):
        """宁可不标也不错标：正常输出里出现 error / FAILED / 失败字样不算失败。"""
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "📖 读取文件", "src/errors.py", output)
        (row,) = _rows(mv)
        assert row.tone == "" and not row._status.isVisibleTo(row)
        mv.close()

    def test_a_failure_shows_in_the_header_and_keeps_the_group_open(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "⚡ 执行命令", "node --check app.js", "退出码: 0\n")
        _tool(mv, "⚡ 执行命令", "pytest -q", "退出码: 1\n1 failed\n")
        _tool(mv, "⚡ 执行命令", "python fix.py", "退出码: 0\n")
        mv.handle("划掉没生效，我改一下。", "ai_msg")      # 这一组结束
        mv.show()
        _pump(qt_app)
        (g,) = _groups(mv)
        assert g.head.fail.text() == "· 1 条失败" and g.head.fail.isVisibleTo(mv)
        assert g.rows_box.isVisibleTo(mv), "有失败的组不自动收起"
        assert g.node_tone() == "bad", "线上那一组的节点变红"
        mv.close()


class TestFolding:
    def test_live_group_is_open_then_folds_when_the_next_block_arrives(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        for i in range(3):
            _tool(mv, "📖 读取文件", f"f{i}.py", "x\n")
        mv.show()
        _pump(qt_app)
        (g,) = _groups(mv)
        assert g.rows_box.isVisibleTo(mv), "实时跑着时展开，看得到一条条在跑"
        mv.handle("看完了。", "ai_msg")
        _pump(qt_app)
        assert not g.rows_box.isVisibleTo(mv), "这一组结束、没有失败：收起"
        g.toggle()
        _pump(qt_app)
        assert g.rows_box.isVisibleTo(mv), "点组头展开"
        mv.close()

    def test_redrawn_history_starts_folded(self, qt_app):
        mv = _view()
        mv.replaying = True
        _round_with_reply(mv)
        for i in range(4):
            _tool(mv, "📖 读取文件", f"f{i}.py", "x\n")
        mv.show()
        _pump(qt_app)
        (g,) = _groups(mv)
        assert g.head.isVisibleTo(mv) and not g.rows_box.isVisibleTo(mv)
        mv.close()


class TestLazyOutput:
    def test_folded_rows_build_no_output_box(self, qt_app):
        """重画长会话的大头：每次调用都建一个藏着的输出框。现在点开那一行才建。"""
        from PySide6.QtWidgets import QPlainTextEdit
        mv = _view()
        mv.replaying = True
        _round_with_reply(mv)
        for i in range(30):
            _tool(mv, "⚡ 执行命令", f"echo {i}", f"退出码: 0\n{i}\n")
        assert mv.findChildren(QPlainTextEdit) == []
        row = _rows(mv)[5]
        assert row.output_text().endswith("5")
        row.toggle()
        boxes = mv.findChildren(QPlainTextEdit)
        assert len(boxes) == 1 and boxes[0].toPlainText().endswith("5")
        mv.close()

    def test_output_after_opening_goes_straight_into_the_box(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        mv.handle("\n⚡ 执行命令", "tool_tag")
        mv.handle("  python -m pytest\n", "tool_detail")
        mv.handle("collected 2 items\n", "tool_result")
        (row,) = _rows(mv)
        row.toggle()
        mv.handle("2 passed\n", "tool_result")
        assert row._out.toPlainText().splitlines() == ["collected 2 items", "2 passed"]
        mv.close()


class TestEditRow:
    def test_stats_and_clicking_the_file_name(self, qt_app):
        from PySide6.QtCore import QPoint
        mv = _view()
        _round_with_reply(mv)
        mv.handle("\n🪄 精确编辑", "tool_tag")
        mv.handle("  src/app.js\n", "tool_detail")
        mv.handle("+12 −3", "tool_stat")
        mv.handle("成功编辑 src/app.js\n", "tool_result")
        mv.show()
        _pump(qt_app)
        (row,) = _rows(mv)
        assert "+12" in row._stats.text() and "−3" in row._stats.text()
        clicked = []
        mv.file_clicked.connect(clicked.append)
        row._on_head_clicked(row._summary.geometry().center())
        assert clicked == ["src/app.js"]
        row._on_head_clicked(QPoint(row.header.width() - 4, 10))
        assert clicked == ["src/app.js"], "点行的别处不是看差异"
        mv.close()

    def test_edit_stats_are_real_line_diffs(self):
        from src.streaming import describe_edit_stats
        assert describe_edit_stats("edit_file", {"old_string": "a\nb\nc", "new_string": "a\nB\nc\nd"}) == "+2 −1"
        assert describe_edit_stats("edit_file", {"old_string": "x", "new_string": "y",
                                                 "replace_all": True}) == "", "替换了几处不知道，不报"
        assert describe_edit_stats("write_file", {"content": "1\n2\n3"}) == "3 行", \
            "不知道原文件多少行：只说写了几行，不说成 +3"
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n+more\n*** End Patch"
        assert describe_edit_stats("apply_patch", {"patch": patch}) == "+2 −1"
        assert describe_edit_stats("read_file", {"path": "a"}) == ""

    def test_patch_row_names_its_files(self):
        from src.streaming import describe_tool_call
        patch = "*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Add File: b.txt\n+hi\n*** End Patch"
        _name, detail = describe_tool_call("apply_patch", {"patch": patch})
        assert detail.splitlines()[0] == "src/a.py、b.txt", "一行摘要是文件，不是「*** Begin Patch」"
        assert "*** Begin Patch" in detail, "补丁全文留给展开看"
