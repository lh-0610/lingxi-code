"""内容区卡顿（2026-11-23 实测）：守住几处省下来的开销，同时不能把结果弄错。

实测（真实渲染，用户最长的会话）：流式输出九千字时每段 144ms、越写越卡 → 14ms；拖窗口每步
80ms → 68ms；打开长会话 1.05s → 0.93s。这里不比毫秒（机器不同、会抖），比的是「该省的那份
工作真的没做」和「省了之后显示照样对」。
"""
import gc

import pytest
from PySide6.QtCore import QEvent


@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


def _view():
    from src.ui.message_view import MessageView
    mv = MessageView()
    mv.resize(900, 900)
    mv.show()
    return mv


def _reply(mv):
    mv.handle("你\n", "user_label")
    mv.handle("写长一点\n\n", "user_msg")
    mv.handle("deepseek\n", "ai_label")
    return mv._cur_assistant


def _bodies(turn):
    from src.ui.message_view import BodyText
    return [b for b in turn.findChildren(BodyText) if b.isVisibleTo(turn)]


# ── 流式按段落拆开，结束时合回一块 ──

def test_streaming_splits_paragraphs_and_only_touches_the_tail(qt_app, monkeypatch):
    """以前整段回复塞进一个 QLabel，每来几个字全文重新排版。现在一段一个块，写完的段落不再动。"""
    from src.ui.message_view import BodyText
    mv = _view()
    turn = _reply(mv)
    mv.handle("第一段。\n\n第二段", "ai_msg")
    _pump(qt_app)
    first = _bodies(turn)[0]
    sets = []
    orig = BodyText.setText
    monkeypatch.setattr(BodyText, "setText",
                        lambda self, t: (sets.append(self), orig(self, t))[1])
    for _ in range(5):
        mv.handle("还在写第二段", "ai_msg")
    mv.handle("\n\n第三段", "ai_msg")
    _pump(qt_app)
    assert first not in sets, "第一段写完了还在被重设（每次重排全文）"
    texts = [b.text() for b in _bodies(turn)]
    assert len(texts) == 3 and texts[0] == "第一段。" and texts[2] == "第三段"
    assert texts[1].startswith("第二段") and texts[1].endswith("还在写第二段")
    mv.close()


def test_finalize_merges_back_into_one_markdown_block(qt_app):
    mv = _view()
    turn = _reply(mv)
    mv.handle("第一段。\n\n第二段。\n\n第三段。", "ai_msg")
    _pump(qt_app)
    assert len(_bodies(turn)) == 3
    mv.finalize_markdown("<p>第一段。</p><p>第二段。</p><p>第三段。</p>", "第一段。\n\n第二段。\n\n第三段。")
    _pump(qt_app)
    bodies = _bodies(turn)
    assert len(bodies) == 1, "流式时拆出来的段落块要拿掉，合回一个渲染好的块"
    assert "第三段" in bodies[0].text() and bodies[0].property("lx_markdown").startswith("第一段")
    mv.close()


# ── 正文标签自己记「这个宽度多高」──

def test_height_cache_is_dropped_when_text_or_font_changes(qt_app):
    from PySide6.QtGui import QFont
    from src.ui.message_view import BodyText
    lab = BodyText("短")
    one = lab.heightForWidth(300)
    lab.setText("很长的一段话，" * 60)
    assert lab.heightForWidth(300) > one, "换了文字还在用旧高度"
    long_h = lab.heightForWidth(300)
    f = QFont(lab.font())
    f.setPointSize(f.pointSize() * 2)
    lab.setFont(f)
    assert lab.heightForWidth(300) > long_h, "换了字号还在用旧高度"
    lab.deleteLater()


def test_frozen_paragraphs_are_not_laid_out_again(qt_app, monkeypatch):
    """实测流式时每来一段，前面写完的每一段都被按两个宽度重新排版。"""
    from PySide6.QtWidgets import QLabel
    mv = _view()
    turn = _reply(mv)
    for i in range(12):
        mv.handle(f"第 {i} 段，有一点长度的文字。" * 4 + "\n\n", "ai_msg")
    _pump(qt_app)
    frozen = _bodies(turn)[:-2]
    real = QLabel.heightForWidth
    laid = []
    monkeypatch.setattr(QLabel, "heightForWidth", lambda self, w: (laid.append(self), real(self, w))[1])
    mv.handle("又来几个字", "ai_msg")
    _pump(qt_app)
    assert not [b for b in laid if b in frozen], "写完的段落又被重新排版了"
    mv.close()


# ── 切会话：清掉的旧消息立刻看不见，删挪到下一轮事件循环 ──

def test_clear_hides_old_turns_at_once_and_search_skips_them(qt_app):
    from src.ui.message_view import BodyText
    mv = _view()
    turn = _reply(mv)
    mv.handle("旧会话里独有的字眼", "ai_msg")
    _pump(qt_app)
    old = _bodies(turn)
    mv.clear()
    assert all(not b.isVisibleTo(mv.widget()) for b in old), "清掉的消息还看得见"
    assert mv.find_text("旧会话里独有") == (0, 0), "删完之前搜索也不能搜到旧会话"
    from PySide6.QtWidgets import QApplication
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()
    assert not [b for b in mv.widget().findChildren(BodyText)], "下一轮事件循环里真的删掉"
    mv.close()


# ── 改窗口大小：空白页没显示就不摆它；图标按（文件，颜色）缓存 ──

@pytest.fixture()
def ui(qt_app, monkeypatch, isolated_memory):
    import socket
    from src import agent, mcp_client, models, state, tools
    from src.ui import ChatUI

    def _no_net(*a, **k):
        raise AssertionError("no network")

    monkeypatch.setattr(socket.socket, "connect", _no_net)
    monkeypatch.setattr(models, "_create_llm", _no_net)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    w = ChatUI()
    w.resize(1300, 860)
    w.show()
    _pump(qt_app)
    yield w
    import shiboken6
    if shiboken6.isValid(w):
        if state.ui_ref is w:
            state.ui_ref = None
        w.close()
        qt_app.processEvents()
        w.deleteLater()
        gc.collect()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        qt_app.processEvents()


def test_hidden_empty_state_is_not_laid_out_on_resize(ui, qt_app, monkeypatch):
    calls = []
    monkeypatch.setattr(type(ui), "_position_empty_state", lambda self: calls.append(1))
    ui.empty_state.show()
    ui._refresh_responsive_layout()
    assert calls == [1], "空白页显示着：照常摆"
    ui.empty_state.hide()
    ui._refresh_responsive_layout()
    assert calls == [1], "空白页没显示还在摆它（每次改大小都重画它的图标）"


def test_svg_icons_are_rendered_once_per_file_and_color(ui, monkeypatch):
    renders = []
    real = type(ui)._render_svg_icon
    monkeypatch.setattr(type(ui), "_render_svg_icon",
                        lambda self, *a: (renders.append(a), real(self, *a))[1])
    type(ui)._svg_icon_cache = {}
    a = ui._svg_icon("settings_lucide.svg", "#112233")
    b = ui._svg_icon("settings_lucide.svg", "#112233")
    c = ui._svg_icon("settings_lucide.svg", "#445566")
    assert a is b and c is not a
    assert len(renders) == 2, "同一个图标同一个颜色又从磁盘读、又渲染了一遍"
