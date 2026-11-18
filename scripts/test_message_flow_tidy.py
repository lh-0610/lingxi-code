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
        assert not [f for f in self._seps(mv) if f.parentWidget() is not None
                    and f.parentWidget().parentWidget() is not None]
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

    def test_plain_links_get_accent_without_underline(self):
        html = _md("服务在 http://localhost:8765/ 上")
        assert '<a href="http://localhost:8765/" style="color:#5b6cf0;text-decoration:none;">' in html
