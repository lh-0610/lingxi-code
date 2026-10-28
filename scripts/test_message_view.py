"""消息流时间线（A5）：工具卡收输出、上一轮收起过程、计划步骤分段、回复的用时、重绘推演。

真 Qt 控件（offscreen）：断言的是用户看得到的东西——卡片里有没有那几行输出、
按钮会不会被切掉、收起后还能不能展开——而不是内部字段。
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, result_view, run_records, session
from src.agent_result import AgentResult


@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=3):
    for _ in range(n):
        app.processEvents()


def _view(width=760):
    from src.ui.message_view import MessageView
    mv = MessageView()
    mv.resize(width, 900)
    return mv


def _round_with_reply(mv, model="deepseek-v4-flash"):
    mv.handle("你\n", "user_label")
    mv.handle("修一下登录\n\n", "user_msg")
    mv.handle(f"{model}\n", "ai_label")


def _tool(mv, tag, detail, *outputs):
    mv.handle(f"\n{tag}", "tool_tag")
    mv.handle(f"  {detail}\n", "tool_detail")
    for out in outputs:
        mv.handle(out, "tool_result")
    mv.handle("", "reset_ai_reply")


class TestToolCards:
    def test_output_goes_into_its_card_and_later_notes_do_not(self, qt_app):
        from src.ui.message_view import Notice, ToolCallCard
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "📖 读取文件", "src/login.py", "def login():\n    return True\n")
        mv.handle("\n⚠️ 超出上下文窗口，正在压缩历史后重试…\n", "tool_result")
        mv.show()
        _pump(qt_app)

        card = mv.findChildren(ToolCallCard)[0]
        assert "def login():" in card.output_text()
        assert card._tail.full() == "def login():", "一次给完的结果，收起时露第一行"
        assert "超出上下文窗口" not in card.output_text(), "工具结束后的程序提示不能算进工具输出"
        notices = [n.text() for n in mv.findChildren(Notice)]
        assert any(t.startswith("超出上下文窗口") for t in notices), "提示单独成条，去掉 emoji"
        mv.close()

    def test_streamed_lines_accumulate_and_the_last_line_shows_collapsed(self, qt_app):
        from src.ui.message_view import ToolCallCard
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "⚡ 执行命令", "python -m pytest -q",
              "collected 3 items\n", "...\n", "3 passed in 0.12s\n", "\n✓ 退出码 0\n")
        mv.show()
        _pump(qt_app)

        card = mv.findChildren(ToolCallCard)[0]
        assert card.output_text().splitlines() == [
            "collected 3 items", "...", "3 passed in 0.12s", "✓ 退出码 0"]
        assert card._count.text() == "输出 4 行"
        assert card._tail.isVisibleTo(card) and card._tail.full() == "✓ 退出码 0"
        assert not card._out.isVisibleTo(card), "默认收起，完整输出要点开才看"
        card.toggle()
        _pump(qt_app)
        assert card._out.isVisibleTo(card) and not card._tail.isVisibleTo(card)
        mv.close()

    def _expanded_card(self, qt_app, width, *outputs):
        from src.ui.message_view import ToolCallCard
        mv = _view(width)
        _round_with_reply(mv)
        _tool(mv, "✏️ 写入文件", "/tmp/qs/q.py", *outputs)
        mv.show()
        _pump(qt_app)
        card = mv.findChildren(ToolCallCard)[0]
        card.toggle()
        _pump(qt_app, 6)
        return mv, card

    def test_one_line_output_has_no_scrollbar(self, qt_app):
        """一行输出：框正好放下，不能冒出一根只剩两个箭头的滚动条（用户截图里的样子）。"""
        mv, card = self._expanded_card(
            qt_app, 760, "未写入：目标路径经符号链接/junction 解析后超出当前工作区，拒绝写入\n")
        bar = card._out.verticalScrollBar()
        assert not bar.isVisible()
        assert bar.maximum() == 0, "内容没有被截"
        mv.close()

    def test_wrapped_long_line_is_not_cut(self, qt_app):
        mv, card = self._expanded_card(qt_app, 420, "很长的一行输出，" * 40 + "\n")
        bar = card._out.verticalScrollBar()
        assert card._out.document().size().height() > 1, "窄窗口下这一行会折成多行"
        assert not bar.isVisible() and bar.maximum() == 0, "折行后的每一行都要露出来"
        mv.close()

    def test_long_output_scrolls_inside_a_capped_box(self, qt_app):
        from src.ui.message_view import ToolCallCard
        mv, card = self._expanded_card(qt_app, 760, *[f"line {i}\n" for i in range(40)])
        line_h = card._out.fontMetrics().lineSpacing()
        assert card._out.height() < (ToolCallCard._OUT_MAX_LINES + 2) * line_h, "最多露出约 16 行"
        assert card._out.verticalScrollBar().maximum() > 0, "其余的在框里滚动"
        mv.close()

    def test_one_line_summary_of_json_args(self):
        from src.ui.message_view import _one_line
        assert _one_line('{\n  "path": "src/ui/fonts.py",\n  "fix": false\n}') == "src/ui/fonts.py"
        assert _one_line('{\n  "step": 1,\n  "status": "完成"\n}') == "1  完成"
        # 多行字符串值（_pretty_args 还原了真换行）只取第一行
        assert _one_line('{\n  "plan": "[x] 读懂\n[~] 修复",\n  "explanation": "开始"\n}') == "[x] 读懂  开始"
        assert _one_line("python -m pytest -q") == "python -m pytest -q"
        assert _one_line("") == ""

    def test_tool_tag_carries_claude_code_args_on_the_same_line(self):
        from src.ui.message_view import _parse_tool_tag
        assert _parse_tool_tag("\n🔧 Bash  ls -la\n") == ("Bash", "ls -la")
        assert _parse_tool_tag("\n📖 读取文件") == ("读取文件", "")


class TestRounds:
    def test_new_round_folds_the_previous_process_but_keeps_the_reply(self, qt_app):
        from src.ui.message_view import BodyText, ThinkingChip, ToolCallCard, _ProcessSummary
        mv = _view()
        _round_with_reply(mv)
        mv.handle("", "think_header")
        mv.handle("先看登录函数", "think_msg")
        mv.handle("", "think_collapse")
        _tool(mv, "📖 读取文件", "src/login.py", "def login(): ...\n")
        mv.handle("改好了。", "ai_msg")
        mv.handle("你\n", "user_label")
        mv.handle("再跑一下测试\n", "user_msg")
        mv.show()
        _pump(qt_app)

        card = mv.findChildren(ToolCallCard)[0]
        chip = mv.findChildren(ThinkingChip)[0]
        body = mv.findChildren(BodyText)[0]
        assert not card.isVisibleTo(mv) and not chip.isVisibleTo(mv)
        assert body.isVisibleTo(mv), "上一轮的回复正文照常显示"
        summary = mv.findChildren(_ProcessSummary)[0]
        assert summary.findChildren(type(body).__mro__[1])[0].text() == "调用了 1 次工具，思考 1 次"
        summary.btn.click()
        _pump(qt_app)
        assert card.isVisibleTo(mv) and summary.btn.text() == "收起过程"
        mv.close()

    def test_result_card_inside_a_round_keeps_its_buttons(self, qt_app, isolated_memory):
        """结果卡放进轮次容器后，高度仍要算对：底部的「继续任务」不能被边框切掉。"""
        from PySide6.QtWidgets import QPushButton
        from src.ui.result_card import ResultCard
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="修一下登录"),
                             AIMessage(content="好")]
        session.register(sess)
        memory.save_session(session=sess)
        sess.project = "D:/proj"
        run_records.begin_run(sess)
        report = run_records.finalize_run(sess, sess.last_run,
                                          AgentResult("limit_reached", "已达单次交互轮次上限"))
        view = result_view.describe(report.snapshot)

        for width in (420, 760):
            mv = _view(width)
            _round_with_reply(mv)
            mv.handle("先看看登录函数。", "ai_msg")
            card = mv.add_result_card(ResultCard(view))
            mv.show()
            _pump(qt_app)
            btn = next(b for b in card.findChildren(QPushButton) if b.text() == "继续任务")
            bottom = btn.mapTo(card, btn.rect().bottomLeft()).y()
            assert btn.isVisible() and bottom <= card.height(), "按钮不能被卡片边框切掉"
            mv.close()


class TestFollowBottom:
    """停在底部时内容变高要跟到底（标签、结果卡要再排几轮版才定高，一次性定时滚动会停在半路）；
    用户往上翻了就不动；点展开 / 收起不能把视图拽走。"""

    def _long_rounds(self, mv, n):
        for i in range(n):
            mv.handle("你\n", "user_label")
            mv.handle(f"第 {i} 个问题\n", "user_msg")
            mv.handle("deepseek-v4-flash\n", "ai_label")
            mv.handle("一段很长的回复，会换好几行。" * 30, "ai_msg")

    def _shown(self, qt_app):
        mv = _view(600)
        mv.resize(600, 320)
        mv.show()
        _pump(qt_app)
        return mv

    def test_follows_to_the_very_bottom_as_content_grows(self, qt_app):
        mv = self._shown(qt_app)
        self._long_rounds(mv, 3)
        _pump(qt_app, 8)
        sb = mv.verticalScrollBar()
        assert sb.maximum() > 0 and sb.value() == sb.maximum()
        mv.handle("再补一段。" * 60, "ai_msg")
        mv.add_message_actions(on_copy=lambda: None)
        _pump(qt_app, 8)
        assert sb.value() == sb.maximum(), "一轮里后来的块（换行正文、按钮）也要跟到底"
        mv.close()

    def test_scrolling_up_stops_following_and_back_down_resumes(self, qt_app):
        mv = self._shown(qt_app)
        self._long_rounds(mv, 3)
        _pump(qt_app, 8)
        sb = mv.verticalScrollBar()
        sb.setValue(0)                         # 用户往上翻
        mv.handle("流式追加的内容。" * 60, "ai_msg")
        _pump(qt_app, 8)
        assert sb.value() == 0, "往上翻着看历史时，不能被流式追加拽回底部"

        sb.setValue(sb.maximum())              # 用户自己滚回底部
        mv.handle("又来一段。" * 60, "ai_msg")
        _pump(qt_app, 8)
        assert sb.value() == sb.maximum()

        sb.setValue(0)
        mv.scroll_to_end()                     # 发消息 / 点"回到底部"
        mv.handle("后续内容。" * 60, "ai_msg")
        _pump(qt_app, 8)
        assert sb.value() == sb.maximum()
        mv.close()

    def test_expanding_a_card_keeps_the_view_still(self, qt_app):
        from src.ui.message_view import ToolCallCard
        mv = self._shown(qt_app)
        self._long_rounds(mv, 2)
        _tool(mv, "⚡ 执行命令", "pytest -q", *[f"line {i}\n" for i in range(12)])
        _pump(qt_app, 8)
        sb = mv.verticalScrollBar()
        before, old_max = sb.value(), sb.maximum()
        assert before == old_max
        mv.findChildren(ToolCallCard)[0].toggle()
        _pump(qt_app, 8)
        assert sb.maximum() > old_max and sb.value() == before, "展开卡片时视图停在原处"
        mv.close()


class TestFind:
    def test_counts_wraps_and_selects(self, qt_app):
        from PySide6.QtWidgets import QLabel
        mv = _view()
        _round_with_reply(mv)
        mv.handle("登录失败是因为 token 过期。再看一下 Token 刷新。", "ai_msg")
        mv.show()
        _pump(qt_app)
        assert mv.find_text("token") == (1, 2), "不分大小写"
        assert mv.find_text("token") == (2, 2)
        assert mv.find_text("token") == (1, 2), "到头绕回第一处"
        assert mv.find_text("token", backward=True) == (2, 2)
        selected = [lbl.selectedText() for lbl in mv.findChildren(QLabel) if lbl.hasSelectedText()]
        assert selected == ["Token"]
        assert mv.find_text("不存在的词") == (0, 0)
        assert not any(lbl.hasSelectedText() for lbl in mv.findChildren(QLabel)), "没找到时清掉旧选中"
        mv.close()

    def test_opens_a_folded_round_to_reach_the_hit(self, qt_app):
        from src.ui.message_view import ToolCallCard
        mv = _view()
        _round_with_reply(mv)
        _tool(mv, "🔍 跨文件搜索", '{\n  "pattern": "refresh_token",\n  "path": "src"\n}', "src/auth.py:12\n")
        mv.handle("找到了。", "ai_msg")
        mv.handle("你\n", "user_label")                  # 新一轮：上一轮的工具卡收起来
        mv.handle("下一个问题\n", "user_msg")
        mv.show()
        _pump(qt_app)
        card = mv.findChildren(ToolCallCard)[0]
        assert not card.isVisibleTo(mv)
        assert mv.find_text("refresh_token") == (1, 1)
        _pump(qt_app)
        assert card.isVisibleTo(mv) and card.is_open(), "命中在收起的过程和卡片里：都要打开"
        mv.close()


class TestJumpToStep:
    def test_jumps_into_a_folded_round_and_highlights(self, qt_app):
        from src.ui.message_view import StepHeader
        mv = _view(600)
        mv.resize(600, 320)
        _round_with_reply(mv)
        mv.on_plan([{"text": "读懂登录", "status": "in_progress"}, {"text": "修复", "status": "pending"}])
        _tool(mv, "📖 读取文件", "src/login.py", "def login(): ...\n")
        mv.handle("一段很长的回复，会换好几行。" * 40, "ai_msg")
        mv.handle("你\n", "user_label")                  # 新一轮：上一轮的步骤分段收起来
        mv.handle("继续\n", "user_msg")
        mv.handle("m\n", "ai_label")
        mv.handle("后面的回复。" * 80, "ai_msg")
        mv.show()
        _pump(qt_app, 8)
        header = mv.findChildren(StepHeader)[0]
        assert not header.isVisibleTo(mv)

        assert mv.scroll_to_step(1, "读懂登录") is True
        _pump(qt_app, 8)
        assert header.isVisibleTo(mv), "收起的那一轮要先展开"
        sb = mv.verticalScrollBar()
        top = header.mapTo(mv.widget(), header.rect().topLeft()).y()
        assert sb.value() <= top < sb.value() + mv.viewport().height(), "滚到这一步看得见的位置"
        assert "stepHeader" in header.styleSheet(), "落点短暂高亮"
        mv.close()

    def test_unknown_step_is_reported(self, qt_app):
        mv = _view()
        _round_with_reply(mv)
        assert mv.scroll_to_step(3) is False, "消息流里没有这一步：交给调用方提示，不乱滚"
        mv.close()


class TestPlanSteps:
    def test_a_segment_per_step_change_and_status_follows_the_plan(self, qt_app):
        from src.ui.message_view import StepHeader
        mv = _view()
        _round_with_reply(mv)
        plan = [{"text": "读懂登录", "status": "in_progress"}, {"text": "修复", "status": "pending"}]
        mv.on_plan(plan)
        mv.on_plan([dict(it) for it in plan])          # 同一步重复上报：不再插
        assert [(h.n, h.status) for h in mv.findChildren(StepHeader)] == [(1, "in_progress")]

        mv.on_plan([{"text": "读懂登录", "status": "done"}, {"text": "修复", "status": "in_progress"}])
        assert [(h.n, h.status) for h in mv.findChildren(StepHeader)] == [(1, "done"), (2, "in_progress")]

        mv.on_plan([{"text": "读懂登录", "status": "done"}, {"text": "修复", "status": "done"}])
        assert [h.status for h in mv.findChildren(StepHeader)] == ["done", "done"]
        mv.close()

    def test_replay_follows_only_accepted_plan_changes(self):
        from src.ui.message_view import replay_plan_change as replay
        plan = replay([], "update_plan", {"plan": "[x] 读懂登录\n[~] 修复\n[ ] 测试"},
                      "计划已更新（1/3 完成）：…")
        assert [it["status"] for it in plan] == ["done", "in_progress", "pending"]
        assert replay(plan, "update_plan", {"plan": "[ ] 别的"}, "计划未更新：已有步骤的文字…") is None
        plan2 = replay(plan, "set_step_status", {"step": 2, "status": "完成"}, "已把第 2 步标为「done」")
        assert [it["status"] for it in plan2] == ["done", "done", "in_progress"], "完成一步后自动推进下一步"
        assert replay(plan2, "set_step_status", {"step": 9, "status": "完成"}, "已把第 9 步") is None
        assert replay(plan2, "read_file", {"path": "a"}, "…") is None


class TestReplyMeta:
    def test_live_reply_has_mode_and_clock_and_freezes_at_run_end(self, qt_app):
        from PySide6.QtWidgets import QLabel
        from src.ui.message_view import MetaLine
        mv = _view()
        mv.meta_provider = lambda: {"mode": "执行模式"}
        _round_with_reply(mv)
        meta = mv.findChildren(MetaLine)[0]
        texts = [lbl.text() for lbl in meta.findChildren(QLabel)]
        assert texts[:2] == ["deepseek-v4-flash", "执行模式"] and texts[2].startswith("进行中")
        assert meta.live
        mv.end_run()
        assert not meta.live and meta._clock.text().startswith("用时")
        mv.close()

    def test_replayed_reply_has_no_clock(self, qt_app):
        from PySide6.QtWidgets import QLabel
        from src.ui.message_view import MetaLine
        mv = _view()
        mv.meta_provider = lambda: {"mode": "执行模式"}
        mv.replaying = True
        _round_with_reply(mv)
        meta = mv.findChildren(MetaLine)[0]
        assert not meta.live
        assert [lbl.text() for lbl in meta.findChildren(QLabel)] == ["deepseek-v4-flash"], \
            "历史回复不知道当时的模式，不标"
        mv.close()

    def test_model_label_survives_save_and_load(self):
        msg = AIMessage(content="hi", additional_kwargs={"lingxi_model": "deepseek-v4-flash"})
        data = memory._msg_to_dict(msg)
        assert data["lingxi_model"] == "deepseek-v4-flash"
        back = memory._dict_to_msg(data)
        assert back.additional_kwargs.get("lingxi_model") == "deepseek-v4-flash"
        assert "lingxi_model" not in memory._msg_to_dict(AIMessage(content="old"))
