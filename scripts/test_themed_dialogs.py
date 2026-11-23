"""主题弹窗（src/ui/dialogs.py）+ 替换后的真实调用点。

弹窗是模态 exec()：每个用例先挂一个带 receiver 的零秒定时器，等弹窗起来后按键 / 点按钮，
再调被测函数。安全阀：5 秒内没被关掉就强制取消，免得写错的用例把整套测试卡死。
窗口按 CLAUDE.md 的 _destroy_ui_cleanly 顺序销毁。
"""
import gc
import io
import os
import tokenize

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QWidget

from src import config, state
from src.ui import dialogs, theme

_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "ui")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    gc.collect()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def _light_theme():
    before = theme.active_theme()
    theme.set_active_theme("light")
    yield
    theme.set_active_theme(before)


@pytest.fixture()
def host(qapp):
    """一个可见的父窗口：弹窗会盖住它（带遮罩）。"""
    w = QWidget()
    w.resize(880, 600)
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def drive(receiver, action, seen=None):
    """弹窗 exec 起来之后对它做 action(dlg)。seen 收集弹窗本身，方便事后断言。"""
    def run():
        dlg = dialogs.active_dialog()
        if dlg is None:
            QTimer.singleShot(10, receiver, run)
            return
        if seen is not None:
            seen.append(dlg)
        action(dlg)

    def bail():
        dlg = dialogs.active_dialog()
        if dlg is not None:
            dlg.reject()

    QTimer.singleShot(0, receiver, run)
    QTimer.singleShot(5000, receiver, bail)


def key(dlg, k, mod=Qt.NoModifier):
    QTest.keyClick(dlg.focusWidget() or dlg, k, mod)


def click(dlg, k):
    QTest.mouseClick(dlg.buttons[k], Qt.LeftButton)


# ── 返回值与键盘 ───────────────────────────────────────────────────

class TestQuestion:
    def test_click_primary_returns_yes(self, host):
        drive(host, lambda d: click(d, dialogs.Yes))
        assert dialogs.question(host, "确认？", "正文") == dialogs.Yes

    def test_escape_returns_no(self, host):
        drive(host, lambda d: key(d, Qt.Key_Escape))
        assert dialogs.question(host, "确认？", "正文") == dialogs.No

    def test_close_button_equals_escape(self, host):
        drive(host, lambda d: QTest.mouseClick(d.close_button, Qt.LeftButton))
        assert dialogs.question(host, "确认？", "正文") == dialogs.No

    def test_enter_triggers_primary_by_default(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Return), seen)
        assert dialogs.question(host, "确认？", "正文") == dialogs.Yes
        assert seen[0].default_key == dialogs.Yes

    def test_enter_respects_explicit_default(self, host):
        """回退对话原来默认 Cancel：Enter 等于取消（设计稿「Enter 取消」）。"""
        drive(host, lambda d: key(d, Qt.Key_Enter))
        r = dialogs.question(host, "回退？", "x", dialogs.Ok | dialogs.Cancel, dialogs.Cancel)
        assert r == dialogs.Cancel

    def test_enter_follows_focused_button(self, host):
        def act(d):
            d.buttons[dialogs.No].setFocus()
            key(d, Qt.Key_Return)
        drive(host, act)
        assert dialogs.question(host, "确认？", "正文") == dialogs.No

    def test_labels_and_order(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Escape), seen)
        dialogs.question(host, "确认？", "x", labels={dialogs.Yes: "回退"})
        d = seen[0]
        assert d.buttons[dialogs.Yes].text() == "回退"
        assert d.buttons[dialogs.No].text() == "取消"
        # 次按钮在左、主按钮在右
        assert d.buttons[dialogs.No].x() < d.buttons[dialogs.Yes].x()
        assert d.buttons[dialogs.Yes].role == dialogs.PRIMARY
        assert d.windowTitle() == "确认？"


class TestDanger:
    def test_default_focus_is_cancel(self, host):
        seen = []
        drive(host, lambda d: (seen.append(d.focusWidget()), key(d, Qt.Key_Escape)))
        assert dialogs.confirm_danger(host, "删除？", "无法恢复。", "删除") is False
        assert isinstance(seen[0], dialogs.DialogButton)
        assert seen[0].key == dialogs.No and seen[0].text() == "取消"

    def test_enter_equals_cancel(self, host):
        drive(host, lambda d: key(d, Qt.Key_Return))
        assert dialogs.confirm_danger(host, "删除？", "无法恢复。", "删除") is False

    def test_enter_on_focused_danger_button_still_cancels(self, host):
        """危险按钮只能点 / 按空格，Enter 钉在取消上。"""
        def act(d):
            d.buttons[dialogs.Yes].setFocus()
            key(d, Qt.Key_Return)
        drive(host, act)
        assert dialogs.confirm_danger(host, "删除？", "无法恢复。", "删除") is False

    def test_click_danger_confirms(self, host):
        seen = []
        drive(host, lambda d: click(d, dialogs.Yes), seen)
        assert dialogs.confirm_danger(host, "删除？", "无法恢复。", "删除") is True
        assert seen[0].buttons[dialogs.Yes].role == dialogs.DANGER
        assert seen[0].buttons[dialogs.Yes].text() == "删除"

    def test_space_on_danger_button_confirms(self, host):
        def act(d):
            d.buttons[dialogs.Yes].setFocus()
            QTest.keyClick(d.buttons[dialogs.Yes], Qt.Key_Space)
        drive(host, act)
        assert dialogs.confirm_danger(host, "删除？", "无法恢复。", "删除") is True


class TestNotices:
    def test_information_escape_returns_ok(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Escape), seen)
        assert dialogs.information(host, "知识库", "请先选择目录。") == dialogs.Ok
        assert seen[0].buttons[dialogs.Ok].text() == "确定"
        assert seen[0].tone == "info"

    def test_critical_uses_error_tone_and_got_it(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Return), seen)
        assert dialogs.critical(host, "保存失败", "写不进去。", detail="PermissionError") == dialogs.Ok
        d = seen[0]
        assert d.tone == "error"
        assert d.buttons[dialogs.Ok].text() == "知道了"
        assert d.detail_box.toPlainText() == "PermissionError"

    def test_success_with_action(self, host):
        drive(host, lambda d: key(d, Qt.Key_Return))
        r = dialogs.success(host, "已保存", "要重启。", dialogs.Yes | dialogs.No, dialogs.Yes,
                            labels={dialogs.Yes: "立即重启", dialogs.No: "稍后"})
        assert r == dialogs.Yes

    def test_success_escape_is_later(self, host):
        drive(host, lambda d: key(d, Qt.Key_Escape))
        r = dialogs.success(host, "已保存", "要重启。", dialogs.Yes | dialogs.No, dialogs.Yes,
                            labels={dialogs.Yes: "立即重启", dialogs.No: "稍后"})
        assert r == dialogs.No

    def test_no_parent_has_no_scrim(self, qapp):
        receiver = QWidget()
        seen = []
        drive(receiver, lambda d: key(d, Qt.Key_Escape), seen)
        dialogs.information(None, "提示", "没有父窗口也能开。")
        assert seen[0]._scrim is False
        assert seen[0].width() < 700
        receiver.deleteLater()

    def test_scrim_covers_parent(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Escape), seen)
        dialogs.information(host, "提示", "盖住父窗口。")
        d = seen[0]
        assert d._scrim is True
        assert d.size() == host.size()
        assert d.card.width() == dialogs.CARD_WIDTH


class TestCopyButton:
    def test_copy_puts_detail_on_clipboard(self, host, qapp):
        detail = "D:\\some\\very\\long\\path\\" + "x" * 120
        seen = []

        def act(d):
            seen.append(d)
            QTest.mouseClick(d.copy_button, Qt.LeftButton)
            seen.append(d.copy_button.text())
            key(d, Qt.Key_Escape)
        drive(host, act)
        dialogs.warning(host, "找不到", "路径不存在。", detail=detail, detail_label="路径")
        assert QApplication.clipboard().text() == detail
        assert seen[1] == "已复制"

    def test_long_detail_wraps_inside_card(self, host):
        """长路径没有空格也得折行，不撑破卡片。"""
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Escape), seen)
        dialogs.critical(host, "x", "y", detail="C:\\" + "a" * 300)
        d = seen[0]
        assert d.card.width() == dialogs.CARD_WIDTH
        assert d.detail_box.height() > 40
        assert d.detail_box.height() <= dialogs._DETAIL_MAX_H

    def test_not_copyable_has_no_button(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Escape), seen)
        dialogs.confirm_danger(host, "x", "y", "z", detail="作用范围", copyable=False)
        assert seen[0].copy_button is None


class TestChooseAndInput:
    ACTIONS = [("use", "改用当前模型继续", dialogs.PRIMARY),
               ("relocate", "重新选择项目目录…", dialogs.SECONDARY),
               ("copy", "复制原路径", dialogs.SECONDARY),
               ("close", "关闭", dialogs.TEXT)]

    def test_choose_returns_key(self, host):
        drive(host, lambda d: click(d, "relocate"))
        assert dialogs.choose(host, "暂时无法继续", "先处理：", self.ACTIONS) == "relocate"

    def test_choose_escape_and_close_return_none(self, host):
        drive(host, lambda d: key(d, Qt.Key_Escape))
        assert dialogs.choose(host, "暂时无法继续", "", self.ACTIONS) is None
        drive(host, lambda d: click(d, "close"))
        assert dialogs.choose(host, "暂时无法继续", "", self.ACTIONS) is None

    def test_choose_enter_is_primary_and_stacked(self, host):
        seen = []
        drive(host, lambda d: key(d, Qt.Key_Return), seen)
        assert dialogs.choose(host, "x", "", self.ACTIONS, bullets=["原项目目录已不存在：D:\\old"]) == "use"
        d = seen[0]
        # 主按钮占满一行，两个次按钮并排平分
        assert d.buttons["use"].width() > d.buttons["relocate"].width() * 1.5
        assert d.buttons["relocate"].y() == d.buttons["copy"].y()

    def test_get_text_enter_accepts(self, host):
        def act(d):
            assert isinstance(d.input, QLineEdit)
            assert d.focusWidget() is d.input
            d.input.setText("新标题")          # QTest.keyClicks 打中文字符在 offscreen 下会卡住
            QTest.keyClick(d.input, Qt.Key_Return)
        drive(host, act)
        assert dialogs.get_text(host, "重命名会话", "会话标题", "旧") == ("新标题", True)

    def test_get_text_escape_cancels(self, host):
        drive(host, lambda d: QTest.keyClick(d.input, Qt.Key_Escape))
        text, ok = dialogs.get_text(host, "重命名会话", "会话标题", "旧")
        assert ok is False and text == "旧"

    def test_multiline_ctrl_enter(self, host):
        def act(d):
            assert isinstance(d.input, QPlainTextEdit)
            QTest.keyClick(d.input, Qt.Key_End, Qt.ControlModifier)
            QTest.keyClick(d.input, Qt.Key_Return)          # 普通回车换行
            QTest.keyClicks(d.input, "b")
            QTest.keyClick(d.input, Qt.Key_Return, Qt.ControlModifier)
        drive(host, act)
        assert dialogs.get_text(host, "编辑消息", "消息内容", "a", multiline=True) == ("a\nb", True)


# ── 明暗主题取色 ───────────────────────────────────────────────────

class TestThemeColors:
    @pytest.mark.parametrize("name, primary, danger, card", [
        ("light", "#4a59e0", "#c43d27", "#ffffff"),
        ("dark", "#95a0fa", "#f09a8d", "#20242b"),
    ])
    def test_buttons_and_card_follow_theme(self, host, name, primary, danger, card):
        theme.set_active_theme(name)
        seen = []
        drive(host, lambda d: (seen.append({k: b.colors() for k, b in d.buttons.items()}),
                               seen.append(d.styleSheet()), key(d, Qt.Key_Escape)))
        dialogs.confirm_danger(host, "x", "y", "删除")
        assert seen[0][dialogs.Yes]["bg"] == danger
        assert f"background: {card}" in seen[1]
        theme.set_active_theme(name)
        seen.clear()
        drive(host, lambda d: (seen.append(d.buttons[dialogs.Yes].colors()), key(d, Qt.Key_Escape)))
        dialogs.question(host, "x", "y")
        assert seen[0]["bg"] == primary

    @pytest.mark.parametrize("tone, key_", [("info", "info"), ("success", "ok"),
                                            ("warning", "warn"), ("error", "err")])
    def test_icon_tile_tone(self, host, tone, key_):
        seen = []
        drive(host, lambda d: (seen.append(d.styleSheet()), key(d, Qt.Key_Escape)))
        dialogs.message(host, "x", "y", tone=tone)
        assert theme.THEMES["light"][f"dlg_{key_}_bg"] in seen[0]

    def test_both_themes_define_every_dialog_token(self):
        light = {k for k in theme.THEMES["light"] if k.startswith("dlg_")}
        dark = {k for k in theme.THEMES["dark"] if k.startswith("dlg_")}
        assert light and light == dark

    @pytest.mark.parametrize("bg, fg", [("#4a59e0", "#ffffff"), ("#c43d27", "#ffffff"),
                                        ("#95a0fa", "#16181d"), ("#f09a8d", "#16181d")])
    def test_solid_buttons_pass_contrast(self, bg, fg):
        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
            return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
        a, b = sorted((lum(bg), lum(fg)), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 4.5

    def test_qcolor_parses_rgba(self):
        c = dialogs.qcolor("rgba(22, 26, 38, 0.42)")
        assert (c.red(), c.green(), c.blue(), c.alpha()) == (22, 26, 38, 107)


# ── 防回归：src/ui 里不再有原生消息框 / 输入框 ─────────────────────────

def test_no_native_message_or_input_boxes_in_ui():
    """只看代码里的名字（注释和字符串不算）；QFileDialog 文件选择框不在此列。"""
    hits = []
    for name in sorted(os.listdir(_UI_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_UI_DIR, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and tok.string in ("QMessageBox", "QInputDialog"):
                hits.append(f"{name}:{tok.start[0]} {tok.string}")
    assert not hits, "src/ui 里还有原生弹窗：" + ", ".join(hits)


# ── 替换后的真实调用点 ─────────────────────────────────────────────

def _destroy_ui_cleanly(ui, app):
    """同 test_sidebar_load_more：先在窗口存活时放完零秒回调，再 deleteLater + DeferredDelete。"""
    import shiboken6
    if not shiboken6.isValid(ui):
        return
    if state.ui_ref is ui:
        state.ui_ref = None
    ui.close()
    app.processEvents()
    app.processEvents()
    ui.deleteLater()
    gc.collect()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture()
def chat_ui(qapp, monkeypatch, isolated_memory):
    """完整 ChatUI，模型 / MCP / 网络全部离线。"""
    import socket

    def _forbidden(*a, **k):
        raise AssertionError("dialog tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import agent, mcp_client, models, tools
    from src.ui.chat_window import ChatUI

    def _no_llm(*a, **k):
        raise AssertionError("dialog tests must not create a model")

    monkeypatch.setattr(models, "_create_llm", _no_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_create_llm", _no_llm)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(state, "llm", state.llm)
    monkeypatch.setattr(state, "llm_with_tools", state.llm_with_tools)
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    ui.show()
    qapp.processEvents()
    yield ui
    _destroy_ui_cleanly(ui, qapp)


class TestCallSites:
    def test_bypass_mode_enter_cancels(self, chat_ui):
        seen = []
        drive(chat_ui, lambda d: key(d, Qt.Key_Return), seen)
        assert chat_ui._confirm_bypass_mode() is False
        d = seen[0]
        assert d.danger and d.tone == "warning"
        assert d.buttons[dialogs.Yes].text() == "仍要切换"
        assert d.detail_box.toPlainText().startswith("当前会话 · ")
        assert d._scrim is True                       # 盖住主窗口

    def test_bypass_mode_click_switches(self, chat_ui):
        drive(chat_ui, lambda d: click(d, dialogs.Yes))
        assert chat_ui._confirm_bypass_mode() is True

    def test_rewind_default_cancel(self, chat_ui):
        drive(chat_ui, lambda d: key(d, Qt.Key_Return))
        assert chat_ui._confirm_rewind() is False
        drive(chat_ui, lambda d: click(d, dialogs.Ok))
        assert chat_ui._confirm_rewind() is True

    def test_rename_through_real_dialog(self, chat_ui, qapp, project_dir):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
        from src import memory, session as sm
        s = sm.Session()
        s.current_session_title = "旧标题"
        s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi"),
                          AIMessage(content="yo")]
        memory.save_session(session=s)

        def act(d):
            assert d.input.text() == "旧标题"
            d.input.setText("改过的名字")
            QTest.keyClick(d.input, Qt.Key_Return)
        drive(chat_ui, act)
        chat_ui._rename_session_dialog(s.current_session_id, "旧标题")
        qapp.processEvents()
        titles = {e["id"]: e["title"] for e in memory.list_sessions()}
        assert titles[s.current_session_id] == "改过的名字"

    def test_resume_blocked_copy_path(self, chat_ui, monkeypatch):
        from types import SimpleNamespace
        toasts = []
        monkeypatch.setattr(chat_ui, "_show_toast", lambda t, d=1500: toasts.append(t))
        pre = SimpleNamespace(blocking=["原项目目录已不存在：D:\\old\\shop-api"],
                              model_missing=False, project_missing="D:\\old\\shop-api")
        seen = []
        drive(chat_ui, lambda d: click(d, "copy_path"), seen)
        chat_ui._show_resume_blocked(None, {}, None, pre)
        assert QApplication.clipboard().text() == "D:\\old\\shop-api"
        assert toasts == ["已复制原项目路径"]
        assert set(seen[0].buttons) == {"relocate", "copy_path", "close"}

    def test_resume_blocked_escape_does_nothing(self, chat_ui, monkeypatch):
        from types import SimpleNamespace
        called = []
        monkeypatch.setattr(chat_ui, "_on_result_continue", lambda *a, **k: called.append(1))
        pre = SimpleNamespace(blocking=["上次用的模型已不在模型列表里"], model_missing=True,
                              project_missing="")
        drive(chat_ui, lambda d: key(d, Qt.Key_Escape))
        chat_ui._show_resume_blocked(None, {}, None, pre)
        assert called == []


def test_settings_delete_custom_model_is_danger(qapp, tmp_path, monkeypatch):
    from src.ui import settings_dialog as sd
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sd, "CONFIG_PATH", str(cfg_path))
    # 设置页的自定义模型列表取自 config.CUSTOM_MODELS（启动时解析好的）
    monkeypatch.setattr(config, "CUSTOM_MODELS",
                        [{"name": "m1", "model_id": "id1", "protocol": "openai"}])
    dlg = sd.SettingsDialog()
    dlg.show()
    qapp.processEvents()
    try:
        n = len(dlg._custom_models)
        assert n == 1
        seen = []
        drive(dlg, lambda d: key(d, Qt.Key_Return), seen)
        dlg._on_delete_custom_model(0)
        assert len(dlg._custom_models) == 1            # Enter 等于取消
        assert seen[0].danger and seen[0].buttons[dialogs.Yes].text() == "删除"
        drive(dlg, lambda d: click(d, dialogs.Yes))
        dlg._on_delete_custom_model(0)
        assert dlg._custom_models == []
    finally:
        dlg.close()
        dlg.deleteLater()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)
        qapp.processEvents()


# ── 第 2 批：自带窗框的 QDialog 换成窗内卡片 ─────────────────────────

class TestCardDialog:
    def test_inline_overlay_with_scrim_and_requested_size(self, host):
        seen = []
        drive(host, lambda d: (seen.append((d.isWindow(), d.parentWidget(), d._scrim,
                                            d.card.size(), d.size())), d.reject()))
        dlg = dialogs.CardDialog(host, title="标题", width=520, height=300)
        dlg.exec()
        is_window, parent, scrim, card, size = seen[0]
        # 窗内覆盖层：父窗口里的子控件，不是另一个顶层窗（主窗口标题栏按钮才点得动）
        assert is_window is False and parent is host
        assert scrim is True and size == host.size()
        assert (card.width(), card.height()) == (520, 300)

    def test_card_shrinks_to_small_window(self, qapp):
        small = QWidget()
        small.resize(600, 400)
        small.show()
        qapp.processEvents()
        seen = []
        drive(small, lambda d: (seen.append(d.card.size()), d.reject()))
        dialogs.CardDialog(small, title="大", width=800, height=620).exec()
        assert seen[0].width() <= 600 - 2 * dialogs.CardDialog.MARGIN
        assert seen[0].height() <= 400 - 2 * dialogs.CardDialog.MARGIN
        small.deleteLater()

    def test_escape_and_close_button_reject(self, host):
        from PySide6.QtWidgets import QDialog
        drive(host, lambda d: QTest.keyClick(d, Qt.Key_Escape))
        assert dialogs.CardDialog(host, title="x").exec() == QDialog.Rejected
        drive(host, lambda d: QTest.mouseClick(d.close_button, Qt.LeftButton))
        assert dialogs.CardDialog(host, title="x").exec() == QDialog.Rejected

    def test_enter_in_line_edit_triggers_default_save(self, host):
        """自定义模型编辑器：填好字段在输入框里按 Enter = 保存（和原来 setDefault 一致）。"""
        from src.ui.settings_dialog import _CustomModelEditor
        dlg = _CustomModelEditor(host, {"name": "m", "model_id": "id-1"})

        def act(d):
            assert d.save_btn.isDefault()
            d.f_name.setFocus()
            QTest.keyClick(d.f_name, Qt.Key_Return)
        drive(host, act)
        dlg.exec()
        assert dlg.result_data and dlg.result_data["model_id"] == "id-1"
        assert dlg.body.layout() is not None and dlg.save_btn.role == dialogs.PRIMARY

    def test_text_viewer_copy_all(self, host):
        dlg = dialogs.TextViewerDialog(host, "验证输出", "line1\nline2")

        def act(d):
            QTest.mouseClick(d.copy_button, Qt.LeftButton)
            d.reject()
        drive(host, act)
        dlg.exec()
        assert QApplication.clipboard().text() == "line1\nline2"
        assert dlg.copy_button.text() == "已复制"


class TestSettingsCard:
    @pytest.fixture()
    def settings(self, qapp, host, tmp_path, monkeypatch):
        from src.ui import settings_dialog as sd
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(sd, "CONFIG_PATH", str(cfg))
        dlg = sd.SettingsDialog(host)
        yield dlg
        dlg.deleteLater()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_left_nav_replaces_tab_bar(self, settings, host):
        seen = []

        def act(d):
            seen.append((d.card.size(), d.tabs.tabBar().isVisible()))
            seen.append(d.save_btn.isDefault())         # 打开期间保存是默认按钮（Enter）
            QTest.mouseClick(d.nav_buttons[3], Qt.LeftButton)
            seen.append((d.tabs.currentIndex(), [b.isChecked() for b in d.nav_buttons]))
            d.reject()
        drive(host, act)
        settings.exec()
        (size, bar_visible), save_default, (idx, checked) = seen
        # host 是 880×600：高度缩到窗口里留边，宽度照设计稿 800
        assert (size.width(), size.height()) == (800, 600 - 2 * dialogs.CardDialog.MARGIN)
        assert save_default is True
        assert bar_visible is False
        assert idx == 3 and checked == [False, False, False, True, False]
        assert [b.text() for b in settings.nav_buttons] == ["模型", "远程", "知识库", "权限", "高级"]
        assert settings.hint_label.text() == "保存后立即生效"
        assert settings.cancel_btn.role == dialogs.SECONDARY

    @pytest.mark.parametrize("name", ["light", "dark"])
    def test_nav_and_card_follow_theme(self, qapp, host, tmp_path, monkeypatch, name):
        from src.ui import settings_dialog as sd
        theme.set_active_theme(name)
        monkeypatch.setattr(sd, "CONFIG_PATH", str(tmp_path / "none.json"))
        dlg = sd.SettingsDialog(host)
        p = theme.THEMES[name]
        assert p["dlg_nav_active_bg"] in dlg.body.styleSheet()
        assert p["dlg_nav_bg"] in dlg.body.styleSheet()
        assert f"background: {p['dlg_bg']}" in dlg.styleSheet()
        dlg.deleteLater()


class TestQuitConfirm:
    def test_enter_keeps_running(self, host):
        from PySide6.QtWidgets import QDialog
        from src.ui.widgets import QuitConfirmDialog
        drive(host, lambda d: key(d, Qt.Key_Return))
        dlg = QuitConfirmDialog("1 个对话还在运行", host)
        assert dlg.exec() == QDialog.Rejected
        assert dlg.danger and dlg.buttons["quit"].role == dialogs.DANGER

    def test_click_quit_accepts(self, host):
        from PySide6.QtWidgets import QDialog
        from src.ui.widgets import QuitConfirmDialog
        drive(host, lambda d: click(d, "quit"))
        assert QuitConfirmDialog("1 个后台命令还在运行", host).exec() == QDialog.Accepted


def test_no_native_framed_dialogs_in_ui():
    """对话框一律是 dialogs.CardDialog / ThemedDialog（窗内卡片）；后台任务和调试是右侧停靠栏的
    标签（第 3 批），也不再是独立窗口。"""
    import ast
    allowed = {"dialogs.py"}
    hits = []
    for name in sorted(os.listdir(_UI_DIR)):
        if not name.endswith(".py") or name in allowed:
            continue
        with open(os.path.join(_UI_DIR, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "QDialog":
                        hits.append(f"{name}:{node.lineno} class {node.name}(QDialog)")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("QDialog", "QDialogButtonBox"):
                hits.append(f"{name}:{node.lineno} {node.func.id}(...)")
    assert not hits, "还有自带窗框的对话框：" + ", ".join(hits)


class TestInlineOverlay:
    """弹窗是主窗口里的覆盖层：标题栏按钮照常可点（不是应用级模态顶层窗），窗口缩放跟着变，
    焦点和按键收在卡片里，关掉后焦点还回去。"""

    def test_not_an_app_modal_window(self, host):
        seen = []
        drive(host, lambda d: (seen.append((QApplication.activeModalWidget(), d.isWindow())),
                               key(d, Qt.Key_Escape)))
        dialogs.information(host, "提示", "x")
        modal, is_window = seen[0]
        assert modal is None and is_window is False

    def test_follows_window_resize(self, host, qapp):
        seen = []

        def act(d):
            host.resize(1200, 820)
            qapp.processEvents()
            seen.append((d.size(), d.card.size()))
            d.reject()
        drive(host, act)
        dialogs.CardDialog(host, title="设置", width=800, height=620).exec()
        size, card = seen[0]
        assert (size.width(), size.height()) == (1200, 820)
        assert (card.width(), card.height()) == (800, 620)     # 窗口够大了，不再缩

    def test_tab_stays_inside(self, host):
        from PySide6.QtWidgets import QLineEdit as _LE
        outside = _LE(host)
        outside.show()
        seen = []

        def act(d):
            for _ in range(6):
                QTest.keyClick(d.focusWidget() or d, Qt.Key_Tab)
                seen.append(d.isAncestorOf(QApplication.focusWidget() or d.focusWidget()))
            key(d, Qt.Key_Escape)
        drive(host, act)
        dialogs.question(host, "确认？", "x")
        assert seen and all(seen)

    def test_window_shortcuts_do_not_fire(self, host):
        from PySide6.QtGui import QKeySequence, QShortcut
        fired = []
        sc = QShortcut(QKeySequence("Ctrl+K"), host)
        sc.activated.connect(lambda: fired.append(1))

        def act(d):
            QTest.keyClick(d.focusWidget() or d, Qt.Key_K, Qt.ControlModifier)
            key(d, Qt.Key_Escape)
        drive(host, act)
        dialogs.question(host, "确认？", "x")
        assert fired == []

    def test_scrim_swallows_clicks(self, qapp):
        class Host(QWidget):
            clicks = 0

            def mousePressEvent(self, e):
                Host.clicks += 1
        h = Host()
        h.resize(800, 600)
        h.show()
        qapp.processEvents()

        def act(d):
            QTest.mouseClick(d, Qt.LeftButton, pos=d.rect().topLeft() + QPoint(5, 5))
            d.reject()
        drive(h, act)
        dialogs.CardDialog(h, title="x").exec()
        assert Host.clicks == 0
        h.deleteLater()

    def test_focus_returns_after_close(self, host, qapp):
        from PySide6.QtWidgets import QLineEdit as _LE
        entry = _LE(host)
        entry.show()
        host.activateWindow()
        entry.setFocus()
        qapp.processEvents()
        drive(host, lambda d: key(d, Qt.Key_Escape))
        dialogs.question(host, "确认？", "x")
        qapp.processEvents()
        assert host.focusWidget() is entry
