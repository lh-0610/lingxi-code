"""对话时间轴操作：编辑用户消息并重发 / 回退到某轮 / 从某轮分叉。

三个操作都以"被点的那条用户消息的下标 index"为边界，语义统一为「这条之前」：
  - 编辑：截断到 index（丢弃这条及之后），把编辑后的文本作为新一轮重发；
  - 回退：截断到 index 并落盘（丢弃这条及之后）；
  - 分叉：把前缀 [0:index] 复制成一个新会话，原会话不动。

只动对话与运行态，**绝不撤销磁盘文件改动**（见 memory.truncate_history 契约）。
都会改/复制历史，所以先过"worker 还活着就拒绝"的闸（与 retry 同源，不能和正在跑的那轮抢）。
"""
from langchain_core.messages import HumanMessage

from .. import agent
from .. import session as _session
from ..content_blocks import block_text

# 发送时程序追加进 send_text 的提示前缀（见 send._do_send 的图片提示、
# chat_window._expand_file_mentions 的 @ 引用提示）。它们不是用户打的字，
# 编辑框里必须剥掉，否则每次编辑重发都会把提示越叠越多。
_EDIT_HINT_MARKERS = (
    "[用户用 @ 引用了以下文件/目录",
    "[本次随消息上传了",
)


class TimelineMixin:
    def _human_at(self, index):
        """chat_history[index] 若是真实用户消息则返回它，否则 None（越界 / 不是用户消息 / 内部桥接）。"""
        hist = agent.chat_history
        if not isinstance(index, int) or index < 1 or index >= len(hist):
            return None
        msg = hist[index]
        if not isinstance(msg, HumanMessage):
            return None
        if (getattr(msg, "additional_kwargs", None) or {}).get("lingxi_internal"):
            return None
        return msg

    @staticmethod
    def _user_message_text(msg):
        """取用户【原始输入】：剥掉程序追加的 @ 引用提示 / 图片路径提示，只留用户自己打的字。"""
        content = msg.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(t for t in (block_text(b) for b in content) if t)
        else:
            text = ""
        cut = len(text)
        for marker in _EDIT_HINT_MARKERS:
            i = text.find(marker)
            if i != -1:
                cut = min(cut, i)
        return text[:cut].rstrip()

    @staticmethod
    def _message_has_images(msg):
        """消息里是否带图片块（Anthropic/MiMo 的 image、OpenAI 的 image_url）。
        图片块只存 base64、不存源路径，重发无法可靠还原，故编辑时只告知用户图片不保留。"""
        content = msg.content
        if not isinstance(content, list):
            return False
        return any(isinstance(b, dict) and b.get("type") in ("image", "image_url")
                   for b in content)

    def _timeline_busy(self, sess):
        return self._worker_alive(sess) or getattr(sess, "resume_pending", False)

    def _show_timeline_edit_dialog(self, old_text, note=None):
        """编辑消息对话框。返回新正文（strip 过）；取消返回 None。独立成方法便于测试整只替换。
        note 非空时在编辑框上方显示一行提醒（如「图片不会保留」）。"""
        from PySide6.QtWidgets import (QDialog, QPlainTextEdit, QVBoxLayout,
                                       QDialogButtonBox, QLabel)
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑消息")
        lay = QVBoxLayout(dlg)
        if note:
            lbl = QLabel(note)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
        edit = QPlainTextEdit(old_text)
        edit.setMinimumSize(560, 220)
        lay.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return None
        return edit.toPlainText().strip()

    def _confirm_rewind(self):
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("回退对话")
        box.setIcon(QMessageBox.Question)
        box.setText("回退将丢弃这一轮及之后的所有对话。")
        box.setInformativeText("磁盘上的文件改动不会被撤销（要撤销文件请用顶栏的「撤销」）。继续？")
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Ok

    def _on_user_edit(self, index):
        sess = _session.get_active()
        if self._timeline_busy(sess):
            self._show_toast("上一轮仍在结束，请稍后再编辑", 3000)
            return
        msg = self._human_at(index)
        if msg is None:
            self._show_toast("这条消息已不在当前对话里", 2500)
            self._redraw_chat()
            return
        note = None
        if self._message_has_images(msg):
            note = "这条消息原本带有图片。编辑重发只发送文字，图片不会保留；如需图片请重新上传。"
        new_text = self._show_timeline_edit_dialog(self._user_message_text(msg), note=note)
        if not new_text:
            return
        agent.truncate_history(sess, index)
        self._reset_render_state()
        self._redraw_chat()
        self._do_send(new_text)

    def _on_user_rewind(self, index):
        sess = _session.get_active()
        if self._timeline_busy(sess):
            self._show_toast("上一轮仍在结束，请稍后再回退", 3000)
            return
        if self._human_at(index) is None:
            self._show_toast("这条消息已不在当前对话里", 2500)
            self._redraw_chat()
            return
        if not self._confirm_rewind():
            return
        agent.rewind_session(sess, index)
        self._reset_render_state()
        if len(sess.chat_history) > 1:
            self._redraw_chat()
            self._scroll_to_bottom()
        else:
            self._show_empty_state()
        self._refresh_session_list()
        self._sync_header_from_session()

    def _on_user_fork(self, index):
        sess = _session.get_active()
        if self._timeline_busy(sess):
            self._show_toast("上一轮仍在结束，请稍后再分叉", 3000)
            return
        if self._human_at(index) is None:
            self._show_toast("这条消息已不在当前对话里", 2500)
            self._redraw_chat()
            return
        new = agent.fork_session(sess, index)
        self._activate_session(new)
        self._show_toast("已分叉为新对话")