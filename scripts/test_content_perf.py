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


# ── 打开长会话（2026-11-24）：重画历史省下来的几份工作 ──

def test_tool_icons_are_rendered_once_and_handed_out_as_copies(qt_app, monkeypatch):
    from PySide6.QtGui import QColor, QPainter
    from src.ui import message_view as m
    renders = []
    real = m._render_icon
    monkeypatch.setattr(m, "_render_icon", lambda *a: (renders.append(a), real(*a))[1])
    monkeypatch.setattr(m, "_ICON_CACHE", {})
    a = m._icon(m.SVG_COPY, "#112233", 14)
    b = m._icon(m.SVG_COPY, "#112233", 14)
    m._icon(m.SVG_COPY, "#445566", 14)
    assert len(renders) == 2, "同一个图标同一个颜色又解析、渲染了一遍"
    before = b.toImage()
    p = QPainter(a)                       # 调用方在拿到的那张上画东西：缓存里那张不能跟着变
    p.fillRect(0, 0, 14, 14, QColor("#ff0000"))
    p.end()
    assert m._icon(m.SVG_COPY, "#112233", 14).toImage() == before


def _count_polish(monkeypatch):
    from PySide6.QtWidgets import QCommonStyle
    seen = []
    real = QCommonStyle.unpolish

    def spy(style, w, *a):
        seen.append(w)
        return real(style, w, *a)
    monkeypatch.setattr(QCommonStyle, "unpolish", spy)
    return seen


def test_tool_card_restyles_only_when_its_style_property_changes(qt_app, monkeypatch):
    """每次 _sync 都对状态和行头 unpolish / polish（重画两个长会话 1830 次）：值没变就不做。"""
    from src.ui.message_view import ToolCallCard, _ToolGroup
    card = ToolCallCard("📖 读取文件", "a.py")
    group = _ToolGroup()
    group.add_row(card)
    seen = _count_polish(monkeypatch)
    card.append_output("第一行\n")
    card.set_body("a.py")
    group.refresh()
    assert seen == [], "样式属性没变也重新套了一遍样式"
    card.append_output("❌ 失败：找不到文件\n")
    assert card.tone == "bad" and card._status.property("tone") == "bad"
    assert card._status in seen, "状态变成失败：要重新套样式（变红）"
    seen.clear()
    card.toggle()
    assert card.property("open") == "true" and card.header in seen, "展开：行头要换成展开的样式"


def test_tool_card_builds_rarely_used_labels_only_when_needed(qt_app):
    from PySide6.QtWidgets import QLabel
    from src.ui.message_view import ToolCallCard
    plain = ToolCallCard("📖 读取文件", "a.py")
    plain.append_output("内容\n")
    plain.set_stats("")                    # 没有增删行数（读文件、搜索这类）也不建
    assert (plain._script_info, plain._stats, plain._clock, plain._tail, plain._files) == (None,) * 5
    assert len(plain.findChildren(QLabel)) <= 6
    assert plain.clock_text() == "" and plain.changed_files_text() == ""

    card = ToolCallCard("⚡ 执行命令", "pytest -q")
    card.set_changed_files(["src/a.py"])
    card.start_clock()
    card._t0 -= 5                          # 跑了 5 秒：出转圈、走秒、最新一行输出
    card._tick_clock()
    card.append_output("collected 3 items\n")
    v, h = card.layout(), card._hlay
    assert v.indexOf(card._tail) == v.indexOf(card._files) - 1, "最新一行输出在改动的文件上面"
    assert h.indexOf(card._clock) == h.indexOf(card._chev) - 1, "走秒在箭头前面"
    assert card._tail.isVisibleTo(card) and card._tail.full() == "collected 3 items"
    assert card.clock_text().startswith("运行中")
    card.append_output("3 passed\n")
    assert card._tail.full() == "3 passed", "运行中露的是最新一行"
    card.stop_clock()
    assert not card._tail.isVisibleTo(card) and card.clock_text() == ""
    assert "改动了 1 个文件" in card.changed_files_text()

    edit = ToolCallCard("✏️ 编辑文件", "a.py")
    edit.set_stats("+12 −3")
    edit.set_body("python -\n⁣print(1)\n")   # 交了脚本（顺带测「脚本 N 行」排在增删行数前面）
    assert h is not edit._hlay and edit._hlay.indexOf(edit._script_info) < edit._hlay.indexOf(edit._stats) \
        < edit._hlay.indexOf(edit._status)


def _history(*msgs):
    from src import session
    sess = session.get_active()
    sess.chat_history = list(msgs)
    return sess


def _call(cid, path):
    return {"name": "read_file", "args": {"path": path}, "id": cid, "type": "tool_call"}


def test_redraw_skips_copy_buttons_that_would_be_removed_at_once(ui, monkeypatch):
    """过渡回复（后面紧跟工具卡）挂的复制按钮马上会被拿掉：重画时不建，结果和建了再拿掉一样。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from PySide6.QtWidgets import QWidget
    _history(HumanMessage(content="第一问"), AIMessage(content="第一问的回答"),
             HumanMessage(content="第二问"),
             AIMessage(content="我先看一下文件", tool_calls=[_call("t1", "a.py")]),
             ToolMessage(content="内容", tool_call_id="t1"))
    built = []
    real = type(ui.chat_area).add_message_actions
    monkeypatch.setattr(type(ui.chat_area), "add_message_actions",
                        lambda self, **k: (built.append(1), real(self, **k))[1])
    ui._redraw_chat()
    assert built == [1], "过渡回复的复制按钮建了又拿掉"
    rows = ui.chat_area.widget().findChildren(QWidget, "msgActions")
    assert len(rows) == 1, "只有第一问的回答挂按钮"
    # 第二轮停在工具上：和建了再拿掉一样，第一问的按钮不再常显，改成悬停才出现
    assert ui.chat_area._latest_actions is None and ui.chat_area._cur_assistant.actions is None
    btns = rows[0].findChildren(QWidget)
    assert btns and all(b.isHidden() for b in btns)


def test_redraw_builds_rounds_hidden_and_shows_them_once(ui, qt_app, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from src.ui.message_view import _Round
    _history(HumanMessage(content="问"),
             AIMessage(content="过渡", tool_calls=[_call("t1", "a.py")]),
             ToolMessage(content="内容", tool_call_id="t1"),
             AIMessage(content="最终回答"))
    hidden_while_building, cache_on = [], []
    real = type(ui)._replay_history

    def spy(self, hist):
        from PySide6.QtCore import Qt
        from src.ui import markdown_render
        out = real(self, hist)
        rounds = self.chat_area.widget().findChildren(_Round)
        # 刚加进显示着的列表、还没轮到显示的控件也算 isHidden：要看是不是被显式藏起来的
        hidden_while_building.extend(
            r.isHidden() and r.testAttribute(Qt.WA_WState_ExplicitShowHide)
            for r in rounds if r.parentWidget() is not None)
        cache_on.append(markdown_render._path_cache is not None)
        return out
    monkeypatch.setattr(type(ui), "_replay_history", spy)
    ui._redraw_chat()
    assert hidden_while_building and all(hidden_while_building), "重画期间新的轮次要藏着建"
    assert cache_on == [True], "重画期间文件链接的路径要走缓存（cached_paths）"
    content = ui.chat_area.widget()
    shown = [r for r in content.findChildren(_Round) if r.isVisibleTo(content)]
    assert len(shown) == 1, "画完一起显示"
    turn = shown[0].assistant_turns[-1]
    assert turn._summary is not None and turn._summary.isVisibleTo(content), "收起过程照常（藏着时收的）"
    from src.ui.message_view import ToolCallCard
    assert all(not c.isVisibleTo(content) for c in content.findChildren(ToolCallCard)
               if c.window() is ui), "收起的工具卡显示出来后仍是收着的"


def test_batch_nests_and_shows_only_at_the_outermost_end(qt_app):
    mv = _view()
    with mv.batch():
        with mv.batch():
            turn = _reply(mv)
        assert mv._cur_round.isHidden(), "内层结束不显示"
    assert not mv._cur_round.isHidden() and turn.isVisibleTo(mv.widget())
    mv.close()


def test_file_links_resolve_each_path_once_while_redrawing(tmp_path, monkeypatch):
    from src.ui import markdown_render as mr
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    calls = []
    real = mr.os.path.realpath
    monkeypatch.setattr(mr.os.path, "realpath", lambda p, *a, **k: (calls.append(p), real(p, *a, **k))[1])
    md = "看 `src/a.py` 和 `src/a.py:3`，还有 `src/b.py`"
    root = str(tmp_path)
    with mr.cached_paths():
        first = mr._linkify_file_paths(md, root)
        n = len(calls)
        again = mr._linkify_file_paths(md, root)
        assert len(calls) == n, "重画期间同一个路径又问了一遍文件系统"
    assert first == again and first.count("](") == 2, "a.py 两处都成链接，不存在的 b.py 不链"
    (tmp_path / "src" / "b.py").write_text("y")
    assert mr._linkify_file_paths(md, root).count("](") == 3, "重画之外每次都查：新写出来的文件要认得出"
    assert mr._path_cache is None


def test_plan_panel_does_not_restyle_when_theme_is_unchanged(ui, monkeypatch):
    from src import session
    panel = ui.plan_panel
    sets = []
    monkeypatch.setattr(panel, "setStyleSheet", lambda qss: sets.append(qss))
    panel.render_session(session.get_active())
    panel.render_session(session.get_active())
    assert sets == [], "主题没变，切会话时又把面板样式表整个重设"
    real = panel.theme_lookup
    monkeypatch.setattr(panel, "theme_lookup", lambda k: "#010203" if k == "sidebar_bg" else real(k))
    panel.render_session(session.get_active())
    assert len(sets) == 1 and "#010203" in sets[0], "换了主题要重设"
    panel.set_overlay(not panel._overlay)
    assert len(sets) == 2, "浮层 / 停靠切换要重设"
