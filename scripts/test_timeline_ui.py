"""对话时间轴 UI 测试：用户气泡的操作按钮/信号 + TimelineMixin 编排逻辑。

MessageView 用真 Qt（offscreen）断言按钮与信号；TimelineMixin 用轻量宿主桩接住
协作方法（重绘/发送/激活等），后端 truncate/rewind/fork 走真实实现。
"""
import json
import os
import sys

import pytest
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.ui.timeline import TimelineMixin


@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _history():
    return [
        SystemMessage(content="sys"),
        HumanMessage(content="q1"),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        AIMessage(content="a2"),
    ]


# ── MessageView：按钮与信号 ───────────────────────────────
class TestUserActionButtons:
    def _view_with_user(self, qt_app):
        from src.ui.message_view import MessageView
        mv = MessageView()
        mv.resize(760, 900)
        mv.handle("你\n", "user_label")
        mv.handle("修一下登录\n\n", "user_msg")
        return mv

    def test_three_buttons_added(self, qt_app):
        from PySide6.QtWidgets import QPushButton
        mv = self._view_with_user(qt_app)
        mv.add_user_actions(1)
        mv.show()
        qt_app.processEvents()
        btns = [b for b in mv.findChildren(QPushButton) if b.objectName() == "msgAction"]
        assert len(btns) == 3
        mv.close()

    def test_buttons_emit_index(self, qt_app):
        from PySide6.QtWidgets import QPushButton
        mv = self._view_with_user(qt_app)
        mv.add_user_actions(3)
        mv.show()
        qt_app.processEvents()
        seen = {"edit": [], "rewind": [], "fork": []}
        mv.user_edit_requested.connect(lambda i: seen["edit"].append(i))
        mv.user_rewind_requested.connect(lambda i: seen["rewind"].append(i))
        mv.user_fork_requested.connect(lambda i: seen["fork"].append(i))
        btns = [b for b in mv.findChildren(QPushButton) if b.objectName() == "msgAction"]
        for b in btns:
            b.click()
        qt_app.processEvents()
        assert seen == {"edit": [3], "rewind": [3], "fork": [3]}
        mv.close()

    def test_no_user_turn_is_noop(self, qt_app):
        from PySide6.QtWidgets import QPushButton
        from src.ui.message_view import MessageView
        mv = MessageView()
        mv.add_user_actions(1)                      # 没有用户气泡 → 不建按钮
        assert not [b for b in mv.findChildren(QPushButton) if b.objectName() == "msgAction"]
        mv.close()


# ── TimelineMixin：编排逻辑（宿主桩 + 真后端）──────────────
class _Host(TimelineMixin):
    def __init__(self):
        self.toasts = []
        self.redraws = 0
        self.resets = 0
        self.empty = 0
        self.refreshed = 0
        self.synced = 0
        self.activated = []
        self.sent = []
        self.busy = False
        self.edit_result = "edited text"
        self.edit_seen = None
        self.confirm = True

    def _worker_alive(self, sess):
        return self.busy

    def _show_toast(self, text, duration=1500):
        self.toasts.append(text)

    def _redraw_chat(self):
        self.redraws += 1

    def _reset_render_state(self):
        self.resets += 1

    def _show_empty_state(self):
        self.empty += 1

    def _refresh_session_list(self):
        self.refreshed += 1

    def _sync_header_from_session(self):
        self.synced += 1

    def _scroll_to_bottom(self):
        pass

    def _activate_session(self, sess):
        self.activated.append(sess)

    def _do_send(self, text, images=None, message_id=None):
        self.sent.append(text)
        return True

    def _show_timeline_edit_dialog(self, old_text, note=None):
        #  mirrors 真对话框：返回 strip 过的正文，取消返回 None；记下收到的原文/提示供断言
        self.edit_seen = (old_text, note)
        return self.edit_result.strip() if isinstance(self.edit_result, str) else self.edit_result

    def _confirm_rewind(self):
        return self.confirm


def _active_with_history():
    from src import session as sm
    s = sm.get_active()
    s.chat_history = _history()
    return s


class TestEdit:
    def test_edit_truncates_and_resends(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host._on_user_edit(1)
        assert host.sent == ["edited text"]
        assert len(s.chat_history) == 1            # 截断到 index 1（_do_send 桩不再追加）
        assert host.resets >= 1 and host.redraws >= 1

    def test_edit_busy_rejected(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host.busy = True
        host._on_user_edit(1)
        assert host.sent == []
        assert len(s.chat_history) == 5            # 没动
        assert host.toasts

    def test_edit_cancel_keeps_history(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host.edit_result = None
        host._on_user_edit(1)
        assert host.sent == []
        assert len(s.chat_history) == 5

    def test_edit_empty_text_keeps_history(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host.edit_result = "   "
        host._on_user_edit(1)
        assert host.sent == []
        assert len(s.chat_history) == 5

    def test_edit_invalid_index(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host._on_user_edit(99)
        assert host.sent == []
        assert len(s.chat_history) == 5
        assert host.redraws >= 1


def _stored_with_hints(original="看看 @src/app.py 这个文件"):
    """复刻 send._do_send + chat_window._expand_file_mentions 真正拼进历史的 send_text：
    原始输入 + @ 引用提示 + 图片路径提示（顺序与生产一致：@ 在前、图片在后）。"""
    at_hint = ("\n\n[用户用 @ 引用了以下文件/目录，请【务必先调用对应工具读取其内容】"
               "再据此回答，不要凭空作答]：\n  - src/app.py（文件）→ 用 read_file 读取")
    img_hint = ("[本次随消息上传了 1 张图片，本地路径"
                "（需要把图片当文件处理时可直接把路径传给工具）：C:/tmp/a.png]")
    return original + at_hint + "\n\n" + img_hint


class TestEditPreservesOriginalInput:
    def test_user_message_text_strips_program_hints_str(self):
        original = "看看 @src/app.py 这个文件"
        msg = HumanMessage(content=_stored_with_hints(original))
        assert TimelineMixin._user_message_text(msg) == original

    def test_user_message_text_strips_program_hints_blocks(self):
        original = "看看 @src/app.py 这个文件"
        msg = HumanMessage(content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
            {"type": "text", "text": _stored_with_hints(original)},
        ])
        assert TimelineMixin._user_message_text(msg) == original

    def test_edit_dialog_gets_original_and_image_warning(self, isolated_memory):
        s = _active_with_history()
        original = "看看 @src/app.py 这个文件"
        s.chat_history[1] = HumanMessage(content=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            {"type": "text", "text": _stored_with_hints(original)},
        ])
        host = _Host()
        host._on_user_edit(1)
        old_text, note = host.edit_seen
        assert old_text == original                 # 编辑框只给原始输入，提示被剥掉
        assert note and "图片" in note              # 明确告知图片不会保留
        assert host.sent == ["edited text"]

    def test_edit_plain_text_has_no_image_warning(self, isolated_memory):
        _active_with_history()                      # "q1" 纯文字、无图片
        host = _Host()
        host._on_user_edit(1)
        old_text, note = host.edit_seen
        assert old_text == "q1"
        assert note is None


class TestRewind:
    def test_rewind_persists(self, isolated_memory, project_dir):
        from src import memory
        s = _active_with_history()
        memory.save_session(session=s)
        sid = s.current_session_id
        host = _Host()
        host._on_user_rewind(3)
        assert len(s.chat_history) == 3
        assert host.refreshed >= 1 and host.synced >= 1
        data = json.loads((isolated_memory / f"{sid}.json").read_text(encoding="utf-8"))
        assert len(data["messages"]) == 3

    def test_rewind_cancel(self, isolated_memory, project_dir):
        s = _active_with_history()
        host = _Host()
        host.confirm = False
        host._on_user_rewind(3)
        assert len(s.chat_history) == 5

    def test_rewind_busy_rejected(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host.busy = True
        host._on_user_rewind(3)
        assert len(s.chat_history) == 5
        assert host.toasts

    def test_rewind_to_empty_shows_empty_state(self, isolated_memory, project_dir):
        from src import memory
        s = _active_with_history()
        memory.save_session(session=s)
        sid = s.current_session_id
        host = _Host()
        host._on_user_rewind(1)
        assert len(s.chat_history) == 1
        assert host.empty >= 1
        assert s.current_session_id == sid          # 回退到空保留 id（#2：文件撤销记录靠它续命）


class TestFork:
    def test_fork_activates_new_session(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host._on_user_fork(3)
        assert len(host.activated) == 1
        new = host.activated[0]
        assert new is not s
        assert len(new.chat_history) == 3
        assert len(s.chat_history) == 5            # 源不动

    def test_fork_busy_rejected(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host.busy = True
        host._on_user_fork(3)
        assert host.activated == []
        assert host.toasts

    def test_fork_invalid_index(self, isolated_memory):
        s = _active_with_history()
        host = _Host()
        host._on_user_fork(0)
        assert host.activated == []


class TestHumanAt:
    def test_skips_internal_and_non_human(self, isolated_memory):
        from src import task_state as ts
        s = _active_with_history()
        internal = HumanMessage(content="bridge")
        ts.tag_internal_message(internal, "resume")
        s.chat_history.insert(2, internal)
        host = _Host()
        assert host._human_at(1) is not None       # 真实用户消息
        assert host._human_at(2) is None           # 内部桥接
        assert host._human_at(3) is None           # AIMessage
        assert host._human_at(0) is None           # SystemMessage
        assert host._human_at(99) is None


# ── 真 ChatUI：信号接线 + 重绘挂按钮 + 点击端到端 ───────────
@pytest.fixture()
def chat_ui(qt_app, monkeypatch, isolated_memory):
    import gc
    import socket
    from PySide6.QtCore import QEvent

    def _forbidden(*a, **k):
        raise AssertionError("timeline UI 测试禁止网络")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import models, mcp_client, tools, config, state, agent
    from src.ui.chat_window import ChatUI

    def _no_llm(*a, **k):
        raise AssertionError("timeline UI 测试禁止建/调模型")

    monkeypatch.setattr(models, "_create_llm", _no_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_create_llm", _no_llm)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    yield ui
    import shiboken6
    if shiboken6.isValid(ui):
        if state.ui_ref is ui:
            state.ui_ref = None
        ui.close()
        qt_app.processEvents()
        qt_app.processEvents()
        ui.deleteLater()
        gc.collect()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        qt_app.processEvents()


def _user_action_rows(ui):
    from PySide6.QtWidgets import QWidget
    return [w for w in ui.chat_area.findChildren(QWidget) if w.objectName() == "userActions"]


def _row_buttons(row):
    from PySide6.QtWidgets import QPushButton
    return [b for b in row.findChildren(QPushButton) if b.objectName() == "msgAction"]


class TestChatUIWiring:
    def _populate(self, ui):
        from src import session as sm
        s = sm.get_active()
        s.chat_history = _history()
        ui._reset_render_state()
        ui._redraw_chat()
        return s

    def test_replay_attaches_actions_to_each_user_turn(self, chat_ui, qt_app):
        self._populate(chat_ui)
        qt_app.processEvents()
        rows = _user_action_rows(chat_ui)
        assert len(rows) == 2                      # 两条用户消息各一行
        assert all(len(_row_buttons(r)) == 3 for r in rows)

    def test_edit_button_truncates_and_resends(self, chat_ui, qt_app, monkeypatch):
        s = self._populate(chat_ui)
        monkeypatch.setattr(chat_ui, "_show_timeline_edit_dialog", lambda old, note=None: "new q1")
        sent = []
        monkeypatch.setattr(chat_ui, "_do_send",
                            lambda text, images=None, message_id=None: sent.append(text) or True)
        qt_app.processEvents()
        _row_buttons(_user_action_rows(chat_ui)[0])[0].click()   # 第一行编辑按钮（index 1）
        qt_app.processEvents()
        assert sent == ["new q1"]
        assert len(s.chat_history) == 1

    def test_rewind_button_truncates(self, chat_ui, qt_app, monkeypatch, project_dir):
        s = self._populate(chat_ui)
        monkeypatch.setattr(chat_ui, "_confirm_rewind", lambda: True)
        qt_app.processEvents()
        _row_buttons(_user_action_rows(chat_ui)[1])[1].click()   # 第二行回退按钮（index 3）
        qt_app.processEvents()
        assert len(s.chat_history) == 3

    def test_fork_button_activates_new_session(self, chat_ui, qt_app):
        from src import session as sm
        s = self._populate(chat_ui)
        qt_app.processEvents()
        _row_buttons(_user_action_rows(chat_ui)[1])[2].click()   # 第二行分叉按钮（index 3）
        qt_app.processEvents()
        active = sm.get_active()
        assert active is not s
        assert len(active.chat_history) == 3
        assert len(s.chat_history) == 5            # 源会话不动


# ── 真编辑对话框：note 标签确实渲染（只桩掉阻塞的模态 exec）──
class TestEditDialogRealQt:
    def _open(self, chat_ui, monkeypatch, old_text, note):
        from PySide6.QtWidgets import QDialog
        seen = []

        def _fake_exec(self):
            seen.append(self)
            return QDialog.Accepted
        monkeypatch.setattr(QDialog, "exec", _fake_exec)
        chat_ui._show_timeline_edit_dialog(old_text, note=note)
        return seen[0]

    def test_note_label_rendered_above_edit(self, chat_ui, qt_app, monkeypatch):
        from PySide6.QtWidgets import QLabel, QPlainTextEdit
        note = "这条消息原本带有图片。编辑重发只发送文字，图片不会保留；如需图片请重新上传。"
        dlg = self._open(chat_ui, monkeypatch, "原始输入", note)
        assert note in [l.text() for l in dlg.findChildren(QLabel)]
        edits = dlg.findChildren(QPlainTextEdit)
        assert edits and edits[0].toPlainText() == "原始输入"

    def test_no_note_means_no_extra_label(self, chat_ui, qt_app, monkeypatch):
        from PySide6.QtWidgets import QLabel
        dlg = self._open(chat_ui, monkeypatch, "纯文字", None)
        assert dlg.findChildren(QLabel) == []