"""消息流整理（设计稿「消息区：消息流整理」）：

① 用户消息的编辑 / 回退 / 分叉放在气泡右边同一行，悬停才出现，不占高度
② 继续任务 / 恢复说明是一行小字，细节收进「详情」；实时那两段并成一条
③ 过程摘要整行可点，行尾箭头
④ 回答正文：标题字号压到正文附近、列表悬挂缩进、行内代码用正文色、链接用强调色
⑤ 只有最新一条回答的复制 / 重新生成常显
⑥ 轮与轮之间一条很淡的分隔线
"""
import re

import pytest

from src.ui.markdown_render import MarkdownRenderMixin


@pytest.fixture()
def qt_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=5):
    for _ in range(n):
        app.processEvents()


def _view(width=760):
    from src.ui.message_view import MessageView
    mv = MessageView()
    mv.resize(width, 900)
    return mv


def _enter(w):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtWidgets import QApplication
    QApplication.sendEvent(w, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))


def _leave(w):
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication
    QApplication.sendEvent(w, QEvent(QEvent.Leave))


def _answer(mv, text="好的。", model="deepseek-v4-flash"):
    mv.handle(f"{model}\n", "ai_label")
    mv.finalize_markdown(text, text)
    mv.add_message_actions(on_copy=lambda: None, on_regen=lambda: None)
    mv.end_run()


def _user(mv, text, index):
    mv.handle("你\n", "user_label")
    mv.handle(text + "\n", "user_msg")
    mv.add_user_actions(index)


class TestUserActions:
    def test_actions_sit_beside_the_bubble_and_show_on_hover(self, qt_app):
        from PySide6.QtWidgets import QPushButton
        from src.ui.message_view import UserTurn
        mv = _view()
        _user(mv, "启动一下服务", 0)
        mv.show()
        _pump(qt_app)
        ut = mv.findChildren(UserTurn)[0]
        row = next(w for w in mv.findChildren(object) if getattr(w, "objectName", lambda: "")() == "userActions")
        assert row.parentWidget() is ut, "按钮在气泡那一行里，不另起一行"
        buttons = row.findChildren(QPushButton)
        assert len(buttons) == 3
        h0, bubble_w = ut.height(), ut.bubble.width()
        assert not any(b.isVisible() for b in buttons), "平时不显示"
        _enter(ut)
        _pump(qt_app)
        assert all(b.isVisible() for b in buttons), "鼠标移上去才出现"
        assert ut.height() == h0 and ut.bubble.width() == bubble_w, "显隐不改高度、气泡不重排"
        _leave(ut)
        _pump(qt_app)
        assert not any(b.isVisible() for b in buttons)
        mv.close()

    def test_hidden_buttons_keep_their_room(self, qt_app):
        from src.ui.message_view import UserTurn
        mv = _view()
        _user(mv, "很长的一句话" * 30, 0)
        mv.show()
        _pump(qt_app)
        ut = mv.findChildren(UserTurn)[0]
        w0 = ut.bubble.width()
        _enter(ut)
        _pump(qt_app)
        assert ut.bubble.width() == w0, "长消息折行时，按钮出现也不挤窄气泡（藏起来时照样占位）"
        mv.close()

    def test_buttons_still_emit_the_index(self, qt_app):
        from PySide6.QtWidgets import QPushButton
        from src.ui.message_view import UserTurn
        mv = _view()
        _user(mv, "q", 4)
        got = []
        mv.user_rewind_requested.connect(got.append)
        ut = mv.findChildren(UserTurn)[0]
        ut.findChildren(QPushButton)[1].click()
        assert got == [4]
        mv.close()


class TestMessageActions:
    def _buttons(self, turn):
        from PySide6.QtWidgets import QPushButton
        return [b for b in turn.findChildren(QPushButton) if b.objectName() == "msgAction"]

    def test_only_the_latest_answer_keeps_them_visible(self, qt_app):
        from src.ui.message_view import AssistantTurn
        mv = _view()
        _user(mv, "第一问", 0)
        _answer(mv, "第一答")
        _user(mv, "第二问", 2)
        _answer(mv, "第二答")
        mv.show()
        _pump(qt_app)
        first, second = mv.findChildren(AssistantTurn)
        assert all(b.isVisible() for b in self._buttons(second)), "最新一条常显"
        assert not any(b.isVisible() for b in self._buttons(first)), "更早的平时不显示"
        h0 = first.height()
        _enter(first)
        _pump(qt_app)
        assert all(b.isVisible() for b in self._buttons(first)), "移到那段回复上才出现"
        assert first.height() == h0, "出现时下面的内容不跳"
        _leave(first)
        _pump(qt_app)
        assert not any(b.isVisible() for b in self._buttons(first))
        _leave(second)
        _pump(qt_app)
        assert all(b.isVisible() for b in self._buttons(second)), "最新一条移开也不藏"
        mv.close()

    def test_clear_forgets_the_latest(self, qt_app):
        mv = _view()
        _user(mv, "q", 0)
        _answer(mv)
        old = mv._latest_actions
        mv.clear()
        assert mv._latest_actions is None
        _user(mv, "q2", 0)
        _answer(mv)
        assert mv._latest_actions is not old
        mv.close()


class TestRecoveryNote:
    def test_live_resume_lines_merge_into_one_quiet_note(self, qt_app):
        from src.ui.message_view import Notice, RecoveryNote
        mv = _view()
        _user(mv, "做个页面", 0)
        mv.handle("\n继续任务 · 使用模型「deepseek-v4-flash」· 执行模式\n", "recovery_note")
        mv.handle("\n🔄 继续任务\n现场变化：2 个文件内容已变\n部分现场无法完整核对\n", "recovery_note")
        mv.show()
        _pump(qt_app)
        notes = mv.findChildren(RecoveryNote)
        assert len(notes) == 1, "实时继续的两段并成一条"
        assert not mv.findChildren(Notice), "不再画成通栏灰底的程序提示"
        note = notes[0]
        assert note.title.text() == "继续任务 · 使用模型「deepseek-v4-flash」· 执行模式"
        assert note.detail.text() == "现场变化：2 个文件内容已变\n部分现场无法完整核对", \
            "第二段的「继续任务」不重复，其余接在细节里"
        assert note.link.isVisible() and not note.detail.isVisible(), "细节收着，点「详情」才出"
        note.link.click()
        _pump(qt_app)
        assert note.detail.isVisible() and note.link.text() == "收起"
        mv.close()

    def test_replayed_note_strips_emoji_and_hides_link_without_details(self, qt_app):
        from src.ui.message_view import RecoveryNote
        mv = _view()
        mv.handle("🔄 继续任务\n\n", "recovery_note")
        mv.show()
        _pump(qt_app)
        note = mv.findChildren(RecoveryNote)[0]
        assert note.title.text() == "继续任务"
        assert not note.link.isVisible(), "没有细节就不挂「详情」"
        mv.close()

    def test_different_title_is_kept_as_a_detail(self, qt_app):
        from src.ui.message_view import RecoveryNote
        mv = _view()
        mv.handle("继续任务 · 使用模型「m」· 执行模式\n", "recovery_note")
        mv.handle("🔄 上次运行没有正常结束，已补充恢复提示\n", "recovery_note")
        note = mv.findChildren(RecoveryNote)[0]
        assert "上次运行没有正常结束" in note.detail.text(), "标题对不上的那句不丢"
        mv.close()

    def test_replay_routes_recovery_messages_to_the_quiet_note(self, monkeypatch):
        from langchain_core.messages import HumanMessage
        from src.recovery import RESUME_KIND
        from src.ui.chat_window import ChatUI

        msg = HumanMessage(content="给模型的全文", additional_kwargs={
            "lingxi_internal": True, "lingxi_kind": RESUME_KIND,
            "lingxi_recovery": {"note": "🔄 继续任务\n现场变化：1 个文件已删除"}})
        calls = []

        class _Fake:
            chat_area = None

            def _append_html(self, text, tag):
                calls.append((text, tag))

        fake = _Fake()
        fake._is_hidden_bridge_message = lambda m: ChatUI._is_hidden_bridge_message(fake, m)
        fake._recovery_note_text = ChatUI._recovery_note_text
        monkeypatch.setattr("src.agent.get_current_role_name", lambda: "", raising=False)
        ChatUI._replay_history(fake, [msg])
        assert calls and calls[0][1] == "recovery_note"
        assert "现场变化" in calls[0][0]


class TestProcessSummary:
    def test_whole_row_toggles_and_arrow_tooltip_follows(self, qt_app):
        from src.ui.message_view import ToolCallCard, _ProcessSummary
        mv = _view()
        _user(mv, "q", 0)
        mv.handle("m\n", "ai_label")
        mv.handle("\n🔧 运行命令", "tool_tag")
        mv.handle("  ls\n", "tool_detail")
        mv.handle("", "reset_ai_reply")
        mv.finalize_markdown("好了", "好了")
        mv.end_run()
        mv.show()
        _pump(qt_app)
        summary = mv.findChildren(_ProcessSummary)[0]
        card = mv.findChildren(ToolCallCard)[0]
        assert summary.label.text() == "调用了 1 次工具"
        assert not summary.is_open() and summary.toolTip() == "展开过程"
        assert not card.isVisibleTo(mv)
        summary.click()                     # 整行就是按钮
        _pump(qt_app)
        assert summary.is_open() and summary.toolTip() == "收起过程" and card.isVisibleTo(mv)
        assert summary.width() < mv.viewport().width() // 2, "只有文字那么宽，不是整条通栏"
        mv.close()


class TestInterimReplies:
    def _rows(self, mv):
        from PySide6.QtWidgets import QWidget
        return [w for w in mv.findChildren(QWidget) if w.objectName() == "msgActions"
                and w.parent() is not None]

    def test_interim_reply_drops_its_buttons_final_keeps_them(self, qt_app):
        """用户实测：中间回复和工具行之间空出一大截——过渡回复底下那排复制按钮平时藏着、
        但占着位置。后面接了工具 / 思考的回复不挂按钮，最终回答照常挂。"""
        from src.ui.message_view import BodyText, ToolCallCard
        mv = _view()
        _user(mv, "看一下这个网站", 0)
        mv.handle("m\n", "ai_label")
        mv.finalize_markdown("我先打开这个网站看看内容。", "我先打开这个网站看看内容。")
        mv.add_message_actions(on_copy=lambda: None)          # 实时中间轮也走这一步
        mv.handle("\n🌐 抓取网页", "tool_tag")
        mv.handle("  https://example.com/\n", "tool_detail")
        mv.handle("", "reset_ai_reply")
        assert self._rows(mv) == [], "接了工具调用：过渡回复的按钮拿掉"
        mv.finalize_markdown("网页抓到了，我再想想。", "网页抓到了，我再想想。")
        mv.add_message_actions(on_copy=lambda: None)
        mv.handle("", "think_header")                            # 接思考也一样
        assert self._rows(mv) == []
        mv.finalize_markdown("结论：可信度不高。", "结论：可信度不高。")
        mv.add_message_actions(on_copy=lambda: None, on_regen=lambda: None)
        mv.end_run()
        mv.show()
        _pump(qt_app, 10)
        assert len(self._rows(mv)) == 1, "最终回答照常有复制 / 重新生成"
        final = next(b for b in mv.findChildren(BodyText) if "结论" in b.text())
        row = self._rows(mv)[0]
        assert row.parentWidget().parentWidget() is final.parentWidget().parentWidget()
        # 过渡回复和下面那行工具之间没有一整排按钮的空白
        interim = next(b for b in mv.findChildren(BodyText) if "我先打开" in b.text())
        tool = mv.findChildren(ToolCallCard)[0]
        from PySide6.QtCore import QPoint
        from src.ui.message_view import _ProcessSummary
        for summary in mv.findChildren(_ProcessSummary):
            summary.click()                                     # 展开过程，才量得到间距
        _pump(qt_app, 10)
        gap = tool.mapTo(mv.widget(), QPoint(0, 0)).y() - (
            interim.mapTo(mv.widget(), QPoint(0, 0)).y() + interim.height())
        assert gap < 28, f"过渡回复和工具行之间空了 {gap}px"

    def test_fetch_url_row_says_fetch_and_shows_only_the_url(self):
        """用户以为「打开网页」该用浏览器打开：fetch_url 是直接抓正文，叫「抓取网页」；
        max_chars 不当成目标显示。"""
        from src.streaming import describe_tool_call
        from src.ui.message_view import _tool_verb
        name, detail = describe_tool_call("fetch_url", {"url": "https://a.com/x", "max_chars": 12000})
        assert detail == "https://a.com/x"
        assert _tool_verb(name.split(" ", 1)[1]) == "抓取网页"
        assert describe_tool_call("web_search", {"query": "黎曼猜想", "max_results": 8})[1] == "黎曼猜想"


class TestToggleNoFlash:
    """用户实测：点「查了 5 次网页」收起，用户气泡一瞬间被拉得很高（多出来的就是那几行的
    高度），整片闪一下。Qt 从里往外排版，里层先按外层没缩的旧高度排；展开反过来，最后
    那段回答会被先挤扁。点完返回的那一刻，各块就得是最终尺寸，事件循环里不能再变。"""

    def _conversation(self, qt_app):
        mv = _view()
        mv.resize(760, 600)
        _user(mv, "看一下这个网站https://example.com/", 0)
        mv.handle("deepseek\n", "ai_label")

        def interim(text):
            mv.finalize_markdown(text, text)
            mv.add_message_actions(on_copy=lambda: None)

        def tool(detail):
            mv.handle("\n🌐 抓取网页", "tool_tag")
            mv.handle(f"  {detail}\n", "tool_detail")
            mv.handle("", "reset_ai_reply")

        interim("我先打开这个网站看看内容。")
        tool("https://example.com/")
        interim("这个网站背后有具体事件。")
        for i in range(5):
            tool(f"https://site{i}.com/")
        final = "看完了。\n\n" + "\n\n".join(f"第 {i} 段：一些较长的回答正文内容。" * 3 for i in range(40))
        # 收起、展开过程两种状态下都要比窗口高：滚动条出现会让视口变窄、正文重新折行，
        # 那是正常的重排，不是这里要抓的闪
        mv.finalize_markdown(final, final)
        mv.add_message_actions(on_copy=lambda: None, on_regen=lambda: None)
        mv.end_run()
        mv.show()
        _pump(qt_app, 20)
        return mv

    def _assert_settled_on_return(self, qt_app, mv, click):
        from PySide6.QtWidgets import QWidget
        click()
        snap = {w: w.geometry() for w in mv.findChildren(QWidget) if w.isVisible()}
        _pump(qt_app, 20)
        content = mv.widget()
        moved = [(type(w).__name__, w.objectName(), g.getRect(), w.geometry().getRect())
                 for w, g in snap.items() if w is not content and w.geometry() != g]
        assert moved == [], f"点完之后还在变（会闪）：{moved[:6]}"

    def test_tool_group_toggle_settles_immediately(self, qt_app):
        from src.ui.message_view import _ProcessSummary, _ToolGroup, UserTurn
        mv = self._conversation(qt_app)
        for s in mv.findChildren(_ProcessSummary):
            if not s._open:
                s.click()
        _pump(qt_app, 20)
        group = next(g for g in mv.findChildren(_ToolGroup) if g.framed())
        bubble = mv.findChildren(UserTurn)[0].bubble
        base = bubble.height()
        for _ in range(4):                                   # 展开、收起各两次
            self._assert_settled_on_return(qt_app, mv, group.toggle)
            assert bubble.height() == base

    def test_process_summary_toggle_settles_immediately(self, qt_app):
        from src.ui.message_view import _ProcessSummary
        mv = self._conversation(qt_app)
        summary = mv.findChildren(_ProcessSummary)[0]
        for _ in range(4):
            self._assert_settled_on_return(qt_app, mv, summary.click)


class TestResizeNoFlash:
    @pytest.mark.parametrize("paragraphs", [30, 1], ids=["long", "short"])
    def test_width_change_lays_out_on_the_spot(self, qt_app, paragraphs):
        """用户实测：收起右侧面板，消息区变宽的那一下整列按旧边距铺到右边再缩回居中；
        打开面板反过来被挤成窄窄一条。resizeEvent 返回时就得是最终排版。
        短对话（内容不满一屏、列表高度不变）也要：那时改高度不会顺带重排。"""
        from PySide6.QtWidgets import QWidget
        from src.ui.message_view import MessageView, COLUMN_MAX, COLUMN_SIDE, _Round
        host = QWidget()
        host.resize(2000, 900)
        mv = MessageView(host)
        mv.setGeometry(0, 0, 1000, 900)
        _user(mv, "看一下", 0)
        text = "\n\n".join(f"第 {i} 段：一些较长的回答正文内容，" * 6 for i in range(paragraphs))
        _answer(mv, text)
        host.show()
        _pump(qt_app, 20)
        rnd = mv.findChildren(_Round)[0]
        # 宽窗口（栏宽不变，只平移）和比栏还窄的窗口（栏宽跟着变）都要当场排好
        for width in (1700, 1000, 1700, 1000, 700, 820, 600, 1700):
            mv.setGeometry(0, 0, width, 900)           # 子控件：Resize 同步送达
            assert rnd.width() == min(COLUMN_MAX, mv.viewport().width() - 2 * COLUMN_SIDE)
            assert abs(rnd.mapTo(mv.viewport(), rnd.rect().center()).x()
                       - mv.viewport().width() // 2) <= 2, "当场就居中"
            snap = {w: w.geometry() for w in mv.findChildren(QWidget) if w.isVisible()}
            _pump(qt_app, 20)
            moved = [(type(w).__name__, w.objectName(), g.getRect(), w.geometry().getRect())
                     for w, g in snap.items() if w.geometry() != g]
            assert moved == [], f"宽度变了之后还在变（会闪）：{moved[:4]}"

    def test_wide_resize_does_not_relayout_the_column(self, qt_app):
        """用户实测：长会话开关侧栏、浏览器卡顿。窗口比消息栏宽时栏宽不变，宽度变化只该平移列表，
        不把整列重新排一遍（原来改边距每次都重排，40 轮对话一次 200ms）。
        判据钉机制：缩放前后列表的排版高度缓存还在——再问一次同宽度的高度是现成的。"""
        import time
        from PySide6.QtWidgets import QWidget
        from src.ui.message_view import MessageView
        host = QWidget()
        host.resize(2000, 900)
        mv = MessageView(host)
        mv.setGeometry(0, 0, 1200, 900)
        for r in range(8):
            _user(mv, f"问题 {r}", r)
            text = "\n\n".join(f"第 {i} 段：一些较长的回答正文内容，" * 6 for i in range(15))
            _answer(mv, text)
        host.show()
        _pump(qt_app, 20)
        content = mv.widget()
        x0, w0 = content.x(), content.width()
        relaid = []
        from PySide6.QtCore import QObject, QEvent

        class Watch(QObject):
            def eventFilter(self, o, e):
                if e.type() == QEvent.Resize and o is not content and o.parent() is not None \
                        and isinstance(o, QWidget) and content.isAncestorOf(o):
                    relaid.append(type(o).__name__)
                return False

        watch = Watch()
        qt_app.installEventFilter(watch)
        try:
            mv.setGeometry(0, 0, 1700, 900)
            _pump(qt_app, 10)
        finally:
            qt_app.removeEventFilter(watch)
        assert content.width() == w0 and content.x() > x0, "栏宽不变，只是挪到新的中间"
        assert relaid == [], f"栏宽没变却有子块被重新排版：{relaid[:5]}"
        # 从头算一遍整列排版高度要多久，和缩放一次比：缩放不该再从头算（滚动区问 sizeHint 时
        # Qt 会按另一个宽度把整列算一遍、冲掉缓存，见 _Content）。各取三次里最快的，免得偶发抖动
        lay = mv._col

        def cold_hfw():
            for b in mv.findChildren(QWidget):
                b.updateGeometry()
            t0 = time.perf_counter()
            lay.totalHeightForWidth(content.width())
            return time.perf_counter() - t0

        def one_resize(w):
            t0 = time.perf_counter()
            mv.setGeometry(0, 0, w, 900)
            return time.perf_counter() - t0

        cold = min(cold_hfw() for _ in range(3))
        _pump(qt_app, 10)
        resize = min(one_resize(w) for w in (1500, 1600, 1700))
        assert resize < cold * 0.2, f"缩放一次 {resize * 1000:.2f}ms，从头排一遍 {cold * 1000:.2f}ms"


class TestToolBodyAlign:
    def test_expanded_box_lines_up_with_row_background(self, qt_app):
        """用户实测：展开后行头整条有底色，输出框却缩进去 31，看着像错位。框和行头左边对齐，
        参数文字和框里的字对齐。"""
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QWidget
        from src.ui.message_view import ToolCallCard
        mv = _view()
        _user(mv, "看一下", 0)
        mv.handle("m\n", "ai_label")
        mv.handle("\n🔧 更新计划", "tool_tag")
        mv.handle('  {\n  "steps": ["读现有文件", "写页面", "跑测试", "起服务"]\n}\n', "tool_detail")
        mv.handle("计划已更新\n", "tool_result")
        mv.end_run()
        mv.show()
        _pump(qt_app, 10)
        row = mv.findChildren(ToolCallCard)[0]
        row.toggle()
        _pump(qt_app, 10)
        head = next(w for w in row.findChildren(QWidget) if w.objectName() == "toolRowHead")
        x = lambda w: w.mapTo(row, QPoint(0, 0)).x()
        assert row._out.isVisible()
        assert x(row._out) == x(head), "输出框和行头（底色）左边对齐"
        assert row._args.isVisible(), "多行参数：展开后先列参数"
        text_x = x(row._args) + row._args.contentsMargins().left()
        assert abs(text_x - (x(row._out) + 11)) <= 1, "参数文字和框里的字对齐"


class TestRoundSeparator:
    def _seps(self, mv):
        from PySide6.QtWidgets import QFrame
        return [f for f in mv.findChildren(QFrame) if f.objectName() == "roundSep"]

    def test_one_line_between_two_rounds(self, qt_app):
        mv = _view()
        _user(mv, "一", 0)
        _answer(mv)
        assert not self._seps(mv), "第一轮上面不画线"
        _user(mv, "二", 2)
        _answer(mv)
        _user(mv, "三", 4)
        assert len(self._seps(mv)) == 2
        mv.close()

    def test_result_card_without_round_adds_no_line(self, qt_app):
        from PySide6.QtWidgets import QLabel
        mv = _view()
        _user(mv, "一", 0)
        mv._cur_round = None
        mv.add_result_card(QLabel("卡"))
        assert not self._seps(mv)
        mv.close()

    def test_clear_removes_lines(self, qt_app):
        mv = _view()
        _user(mv, "一", 0)
        _user(mv, "二", 2)
        mv.clear()
        _pump(qt_app)
        # 清掉的轮先藏起来、下一轮事件循环再删（见 MessageView.clear）：看得见的线一条都不能剩
        assert not [f for f in self._seps(mv) if f.isVisibleTo(mv.widget())]
        mv.close()


class TestThreadReachesTheAnswer:
    def test_line_runs_to_the_last_line_of_the_answer_not_the_buttons(self, qt_app):
        from PySide6.QtCore import QPoint
        from src.ui.message_view import BodyText, _Round
        mv = _view()
        _user(mv, "q", 0)
        _answer(mv, "第一段\n\n第二段\n\n第三段")
        mv.show()
        _pump(qt_app, 10)
        r = mv.findChildren(_Round)[0]
        body = mv.findChildren(BodyText)[-1]
        body_bottom = body.mapTo(r, QPoint(0, 0)).y() + body.height()
        assert body_bottom - 8 <= r._content_bottom() <= body_bottom, \
            "画到回答最后一行；下面那排按钮（藏着也占位）不算"
        mv.close()

    def test_final_answer_gets_its_own_node(self, qt_app):
        from src.ui.message_view import BodyText, _Round
        mv = _view()
        _user(mv, "q", 0)
        mv.handle("m\n", "ai_label")
        mv.handle("我去看看。", "ai_msg")
        mv.handle("\n🔧 运行命令", "tool_tag")
        mv.handle("  ls\n", "tool_detail")
        mv.handle("", "reset_ai_reply")
        r = mv.findChildren(_Round)[0]
        assert r.answer_body() is None, "停在工具上：前面那段只是过渡回复，不标成回答"
        mv.handle("看完了，一切正常。", "ai_msg")
        mv.add_message_actions(on_copy=lambda: None)
        mv.end_run()
        mv.show()
        _pump(qt_app)
        answer = r.answer_body()
        assert isinstance(answer, BodyText) and "一切正常" in answer.text(), \
            "以回答收尾：最后那段正文（跳过下面那排按钮）"
        mv.handle("⚠️ 后面又来了一条提示\n", "tool_result")
        _pump(qt_app)
        assert r.answer_body() is None, "回答后面还有别的块：这一轮不算以回答收尾"
        mv.close()

    def test_user_only_round_draws_no_stub(self, qt_app):
        from src.ui.message_view import _Round
        mv = _view()
        _user(mv, "q", 0)
        mv.show()
        _pump(qt_app)
        assert mv.findChildren(_Round)[0]._content_bottom() == 0
        mv.close()


class TestStreamedBody:
    def test_trailing_newlines_do_not_pad_the_gap_before_a_tool(self, qt_app):
        from src.ui.message_view import BodyText
        mv = _view()
        _user(mv, "查 IP", 0)
        mv.handle("m\n", "ai_label")
        mv.handle("\n我来查一下", "ai_msg")
        mv.handle("：\n第二行\n\n", "ai_msg")
        b = mv.findChildren(BodyText)[0]
        assert b.text() == "我来查一下：<br>第二行", "首尾换行不画，中间的照常"
        mv.close()


class _Host:
    def __init__(self):
        self._code_blocks = {}

    def _t(self, key):
        return {"md_code_text": "#2b3142", "md_code_bg": "#f1f2f5", "mv_indigo": "#5b6cf0",
                "mv_muted": "#6b7183"}.get(key, "transparent" if "border" in key else "#222838")

    def _inline_svg_img(self, *a, **k):
        return ""


def _md(text):
    return MarkdownRenderMixin._md_to_html(_Host(), text)


class TestAnswerTypography:
    def test_headings_are_sized_paragraphs(self):
        html = _md("前言\n\n## 服务状态\n\n内容\n\n# 大标题\n\n#### 小标题")
        assert not re.search(r"<h\d", html), "Qt 给 <hN> 套自己的字号，style 盖不住"
        assert 'margin:18px 0 6px 0;font-size:16px;font-weight:600' in html
        assert 'font-size:18px' in html and 'font-size:14px;font-weight:600' in html

    def test_leading_heading_has_no_top_gap(self):
        html = _md("## 结论\n\n好了")
        body = re.sub(r'^<div[^>]*>', '', html)
        assert body.startswith('<p style="margin:0 0 6px 0;font-size:16px'), body[:80]

    def test_paragraphs_only_keep_a_bottom_margin(self):
        html = _md("一段\n\n两段")
        assert html.count('<p style="margin:0 0 8px 0;">') == 2

    def test_inline_code_uses_body_ink(self):
        html = _md("看 `bg1-d0622607` 这个")
        assert re.search(r'<code style="background:#f1f2f5;color:#2b3142;padding:0 4px;', html)

    def test_list_is_a_two_column_table(self):
        html = _md("说明：\n\n- 第一项很长很长\n- 第二项")
        assert html.count("<tr>") == 2
        assert re.search(r'<td width="16" valign="top" style="color:#6b7183;[^"]*">•</td>'
                         r'<td valign="top"[^>]*>第一项很长很长</td>', html), html
        numbered = _md("步骤：\n\n1. 改\n2. 测")
        assert re.search(r'<td width="22"[^>]*>2\.</td><td[^>]*>测</td>', numbered)

    def test_web_links_get_accent_with_a_light_underline(self):
        """设计稿「回复里的链接」：强调色 + 浅一档的下划线 + 箭头（本机 ▸，公网 ↗）。"""
        html = _md("服务在 http://localhost:8765/ 上，文档见 [官网](https://example.com/a)")
        assert '<a href="http://localhost:8765/" style="color:#5b6cf0;text-decoration:underline;' in html
        assert ">localhost:8765<" in html and "▸" in html
        assert ">官网<" in html and "↗" in html
