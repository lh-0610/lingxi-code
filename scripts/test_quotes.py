"""引用（设计稿「消息区：引用」）：

① 选中消息流里的文字 → 浮出「引用 · 复制」；点别处 / Esc / 滚动就收
② 引用不往输入框塞「> 」，是输入框上方一张卡；最多 5 张，× 去掉
③ 气泡里开头的「> 」行画成引用块，不再显示「>」和空行（以前发过的消息也一样）
发给模型的仍是 Markdown 引用。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from src import agent, state
from src.ui.quotes import MAX_QUOTES, compose, split_leading_quotes


# ── 文本 ─────────────────────────────────────────────────────

def test_compose_and_split_round_trip():
    t = compose(["61,570,595,788 字节"], "这是多少？")
    assert t == "> 61,570,595,788 字节\n\n这是多少？"
    assert split_leading_quotes(t) == (["61,570,595,788 字节"], "这是多少？")
    two = compose(["第一段\n\n还是第一段", "第二段"], "问题")
    assert two == "> 第一段\n>\n> 还是第一段\n\n> 第二段\n\n问题"
    assert split_leading_quotes(two) == (["第一段\n\n还是第一段", "第二段"], "问题")
    assert compose([], " 只有正文 ") == "只有正文"
    assert compose(["只引用"], "") == "> 只引用"
    assert split_leading_quotes("> 只引用") == (["只引用"], "")


def test_split_only_takes_leading_quotes():
    """只认开头：正文里的「>」是用户自己写的，原样留着；旧格式（右键引用塞进输入框的
    「> …」+ 两个空行）也认得。"""
    assert split_leading_quotes("普通 > 不是引用") == ([], "普通 > 不是引用")
    assert split_leading_quotes("x\n> 中间的") == ([], "x\n> 中间的")
    assert split_leading_quotes("> 61\n\n\n这是多少？\n\n") == (["61"], "这是多少？")
    assert split_leading_quotes(">a\n>b\nrest") == (["a\nb"], "rest")
    assert split_leading_quotes("") == ([], "")


# ── 气泡 ─────────────────────────────────────────────────────

@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=8):
    for _ in range(n):
        app.processEvents()


def _view():
    from src.ui.message_view import MessageView
    mv = MessageView()
    mv.resize(900, 800)
    return mv


def test_bubble_draws_quotes_without_the_marker(qt_app):
    """用户实测：气泡里原样显示「> 61,570,595,788 字节」和两行空白。"""
    from src.ui.message_view import UserTurn
    from src.ui.quotes import QuoteBlock
    mv = _view()
    mv.handle("你\n", "user_label")
    mv.handle("> 61,570,595,788 字节\n\n\n这是多少？\n\n", "user_msg")      # 旧格式，重画历史
    mv.show()
    _pump(qt_app)
    turn = mv.findChildren(UserTurn)[0]
    assert turn.quotes() == ["61,570,595,788 字节"]
    blocks = turn.findChildren(QuoteBlock)
    assert [b.text() for b in blocks] == ["61,570,595,788 字节"]
    body = turn.body.text()
    assert "&gt;" not in body and ">61" not in body and "61,570" not in body
    assert "这是多少？" in body and "<br>" not in body, "正文只剩用户的话，没有空行"
    assert blocks[0].y() < turn.body.y(), "引用在正文上面"


def test_bubble_with_only_a_quote_keeps_its_node(qt_app):
    """只引用不说话：正文藏起来，线上的节点照样画（原来节点锚在正文上，正文藏了就没了）。"""
    from src.ui.message_view import UserTurn, _Round
    mv = _view()
    mv.append_user_text(compose(["只引用的一段"], ""))
    mv.show()
    _pump(qt_app)
    turn = mv.findChildren(UserTurn)[0]
    assert not turn.body.isVisibleTo(turn) and turn.quotes() == ["只引用的一段"]
    rnd = mv.findChildren(_Round)[0]
    w, kind, info = rnd._nodes[0]
    assert kind == "user" and w.isVisibleTo(rnd)


def test_live_send_and_redraw_look_the_same(qt_app):
    """实时发送（append_user_text）和重画历史（handle user_msg）同一套渲染，@文件 都加粗。"""
    from src.ui.message_view import UserTurn
    text = compose(["引用"], "看 @src/agent.py")
    a, b = _view(), _view()
    a.append_user_text(text + "\n\n")
    b.handle("你\n", "user_label")
    b.handle(text + "\n\n", "user_msg")
    ta, tb = a.findChildren(UserTurn)[0], b.findChildren(UserTurn)[0]
    assert ta.quotes() == tb.quotes() == ["引用"]
    assert ta.body.text() == tb.body.text()
    assert "<b" in ta.body.text() and "@src/agent.py" in ta.body.text()


def test_long_quote_is_clamped_to_three_lines_with_ellipsis(qt_app):
    from src.ui.quotes import ClampText
    from src.ui.fonts import ui_font
    long = "很长的一段引用文字，" * 80
    c = ClampText(long, 3, "#000", ui_font(13))
    lines = c._lines(300)
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert c.heightForWidth(300) == round(3 * 19)
    short = ClampText("短", 3, "#000", ui_font(13))
    assert short._lines(300) == ["短"]
    assert c.toolTip().startswith("很长的一段引用文字")


# ── 输入框 + 选中浮条（真 ChatUI）─────────────────────────────

@pytest.fixture()
def ui(qt_app, monkeypatch, isolated_memory):
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in quote tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import mcp_client, models, tools
    from src.ui import ChatUI
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    window.resize(1440, 900)
    window.show()
    _pump(qt_app)
    yield window
    import shiboken6
    if shiboken6.isValid(window):
        if state.ui_ref is window:
            state.ui_ref = None
        window.close()
        qt_app.processEvents()
        window.deleteLater()
        gc.collect()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        qt_app.processEvents()


def _cards(ui):
    from src.ui.quotes import QuoteCard
    return [c for c in ui.quote_area.findChildren(QuoteCard) if c.parent() is not None]


def test_quote_goes_to_a_card_not_into_the_text(ui, qt_app):
    ui.entry.setPlainText("这是多少？")
    ui.context_menu_quote("61,570,595,788 字节")
    _pump(qt_app)
    assert ui.entry.toPlainText() == "这是多少？", "输入框里不再塞「> 」"
    assert [c.text() for c in _cards(ui)] == ["61,570,595,788 字节"]
    assert ui.quote_area.isVisible()
    assert ui._compose_input() == "> 61,570,595,788 字节\n\n这是多少？"
    ui.context_menu_quote("61,570,595,788 字节")                    # 同一段不重复
    assert len(_cards(ui)) == 1
    _cards(ui)[0].close_btn.click()
    _pump(qt_app)
    assert _cards(ui) == [] and not ui.quote_area.isVisible()
    assert ui._compose_input() == "这是多少？"


def test_at_most_five_quotes(ui, qt_app, monkeypatch):
    toasts = []
    monkeypatch.setattr(ui, "_show_toast", lambda msg, *a, **k: toasts.append(msg))
    for i in range(MAX_QUOTES + 1):
        ui.context_menu_quote(f"第 {i} 段")
    assert len(_cards(ui)) == MAX_QUOTES
    assert toasts and str(MAX_QUOTES) in toasts[-1]


def test_quote_alone_can_be_sent_and_send_clears_the_cards(ui, qt_app, monkeypatch):
    """只引用不打字也能发；发出去的是 Markdown 引用；发送后卡片清掉。"""
    sent = []
    monkeypatch.setattr(ui, "_do_send", lambda text, images=None, message_id=None: sent.append(text) or True)
    monkeypatch.setattr(ui, "_gate_new_run_request", lambda *a, **k: "start")
    ui.context_menu_quote("一段")
    _pump(qt_app)
    assert ui._has_input, "有引用卡就能发送"
    ui._send_message()
    assert sent == ["> 一段"]
    assert _cards(ui) == [] and ui.entry.toPlainText() == ""


def _release_on(widget, pos):
    from PySide6.QtWidgets import QApplication
    g = widget.mapToGlobal(pos)
    ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(pos), QPointF(g), Qt.LeftButton,
                     Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


def _press_on(widget, pos):
    from PySide6.QtWidgets import QApplication
    g = widget.mapToGlobal(pos)
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(pos), QPointF(g), Qt.LeftButton,
                     Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


def _click_via_window(ui, widget):
    """像真实鼠标那样点：事件先到顶层窗口（QWindow）再分发到控件。直接 widget.click() 或
    QTest.mouseClick(widget) 会跳过窗口那一层——用户实测点「引用」没反应就藏在那一层。"""
    from PySide6.QtTest import QTest
    c = widget.mapTo(ui, widget.rect().center())
    QTest.mouseClick(ui.windowHandle(), Qt.LeftButton, Qt.NoModifier, c)


def test_real_click_on_quote_button_adds_a_card(ui, qt_app):
    """用户实测：选中后点浮条上的「引用」，输入框里没出卡片。真实点击先经过顶层窗口，
    过滤器在那一份按下事件上就把浮条收了，按钮藏了、点击落空。"""
    from PySide6.QtTest import QTest
    from src.ui.message_view import BodyText
    mv = ui.chat_area
    mv.handle("m\n", "ai_label")
    mv.finalize_markdown("解压后的证明数据：61,570,595,788 字节。后面还有一些字。",
                         "解压后的证明数据：61,570,595,788 字节。后面还有一些字。")
    mv.end_run()
    _pump(qt_app, 20)
    body = [b for b in mv.findChildren(BodyText) if "61,570" in b.text()][-1]
    win = ui.windowHandle()
    start = body.mapTo(ui, QPoint(4, 10))
    QTest.mousePress(win, Qt.LeftButton, Qt.NoModifier, start)          # 真实拖选，经窗口分发
    for x in range(10, 200, 20):
        QTest.mouseMove(win, body.mapTo(ui, QPoint(x, 10)))
    QTest.mouseRelease(win, Qt.LeftButton, Qt.NoModifier, body.mapTo(ui, QPoint(200, 10)))
    _pump(qt_app)
    bar = mv.selection_bar
    assert bar.isVisible() and bar.text(), "拖选后浮条出来"
    picked = bar.text()
    _click_via_window(ui, bar.quote_btn)
    _pump(qt_app)
    assert [c.text() for c in _cards(ui)] == [picked], "点「引用」进了输入框的卡片"
    assert ui.quote_area.isVisible()


def test_selecting_reply_text_offers_quote_and_copy(ui, qt_app):
    from PySide6.QtWidgets import QApplication
    from src.ui.message_view import BodyText
    mv = ui.chat_area
    mv.handle("m\n", "ai_label")
    mv.finalize_markdown("解压后的证明数据：61,570,595,788 字节。", "解压后的证明数据：61,570,595,788 字节。")
    mv.end_run()
    _pump(qt_app)
    body = [b for b in mv.findChildren(BodyText) if "61,570" in b.text()][-1]
    bar = mv.selection_bar
    _release_on(body, QPoint(5, 5))                                    # 没选中：不出
    _pump(qt_app)
    assert not bar.isVisible()
    plain = "解压后的证明数据：61,570,595,788 字节。"
    start = plain.index("61")
    body.setSelection(start, len("61,570,595,788 字节"))
    _release_on(body, QPoint(40, 8))
    _pump(qt_app)
    assert bar.isVisible() and bar.text() == "61,570,595,788 字节"
    assert bar.parentWidget() is mv
    bar.copy_btn.click()
    assert QApplication.clipboard().text() == "61,570,595,788 字节" and not bar.isVisible()
    _release_on(body, QPoint(40, 8))
    _pump(qt_app)
    assert bar.isVisible()
    bar.quote_btn.click()
    _pump(qt_app)
    assert not bar.isVisible()
    assert [c.text() for c in _cards(ui)] == ["61,570,595,788 字节"], "点「引用」进了输入框的卡片"
    assert ui.entry.toPlainText() == ""


def test_selection_bar_goes_away_on_press_escape_or_scroll(ui, qt_app):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    mv = ui.chat_area
    for i in range(30):
        mv.handle("m\n", "ai_label")
        mv.finalize_markdown(f"第 {i} 段回复。" * 5, f"第 {i} 段回复。" * 5)
        mv.end_run()
    _pump(qt_app)
    bar = mv.selection_bar
    bar.show_for("x", mv.mapToGlobal(QPoint(200, 200)))
    _press_on(mv.viewport(), QPoint(10, 10))
    assert not bar.isVisible(), "点别处收起"
    bar.show_for("x", mv.mapToGlobal(QPoint(200, 200)))
    QApplication.sendEvent(mv, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not bar.isVisible(), "Esc 收起"
    bar.show_for("x", mv.mapToGlobal(QPoint(200, 200)))
    sb = mv.verticalScrollBar()
    sb.setValue(max(0, sb.value() - 100) if sb.value() else sb.maximum() // 2)
    assert not bar.isVisible(), "滚动收起"


def test_selection_in_the_input_box_does_not_offer_quote(ui, qt_app):
    """只在消息流里出：输入框、对话框里选字不出浮条。"""
    ui.entry.setPlainText("自己打的字")
    ui.entry.selectAll()
    _release_on(ui.entry.viewport(), QPoint(5, 5))
    _pump(qt_app)
    assert not ui.chat_area.selection_bar.isVisible()


# ── 收尾：排队面板 · Ctrl+F（设计稿「消息区 · 引用收尾」）─────────────

def _queue_panel(qt_app, texts, width=760):
    from src import input_queue
    from src.ui.queue_panel import QueuePanel
    from src.ui.theme import color as theme_color
    panel = QueuePanel(theme_color)
    q = input_queue.new_queue()
    for t in texts:
        input_queue.enqueue(q, input_queue.new_queue_item(t, []))
    panel.resize(width, 400)
    panel.render_queue(q)
    panel.show()
    _pump(qt_app)
    return panel


def _row_texts(panel):
    from src.ui.queue_panel import _QueueText
    return panel.findChildren(_QueueText)


def test_queue_row_writes_quote_and_text_apart(qt_app):
    """原来整段压成一行：「> 61,570,595,788 字节 这是多少？」——「>」露在外面，分不出哪句是问的。"""
    from PySide6.QtWidgets import QLabel
    panel = _queue_panel(qt_app, [
        compose(["61,570,595,788 字节"], "这是多少？"),
        compose(["源码归档：lean.tar.gz，239,275,554 字节", "61,570,595,788 字节"], "这两个哪个大？"),
        compose(["只引用的一段"], ""),
        "普通的一条",
    ])
    one, two, only, plain = _row_texts(panel)
    assert one.quote_label.full() == "61,570,595,788 字节" and one.body_label.full() == "这是多少？"
    assert one.more_label is None
    assert two.quote_label.full().startswith("源码归档") and two.more_label.text() == "等 2 段"
    assert only.body_label is None and only.quote_label.full() == "只引用的一段"
    assert plain.quote_label is None and plain.body_label.full() == "普通的一条"
    shown = [lbl.text() for lbl in panel.findChildren(QLabel)]
    assert not any(t.lstrip().startswith(">") for t in shown), "「>」不再露出来"
    assert one.toolTip() == compose(["61,570,595,788 字节"], "这是多少？"), "悬停看全文"
    panel.close()


def test_queue_row_quote_takes_at_most_four_tenths(qt_app):
    from src.ui.queue_panel import _QUOTE_SHARE
    long = "很长的一段引用，" * 30
    panel = _queue_panel(qt_app, [compose([long], "问题"), compose(["短"], "问题"), compose([long], "")])
    with_body, short, alone = _row_texts(panel)
    assert 0 < with_body.quote_label.width() <= int(with_body.width() * _QUOTE_SHARE)
    assert with_body.body_label.width() > with_body.width() * 0.4, "正文拿到剩下的宽度"
    assert with_body.quote_label.text().endswith("…")
    for row in (with_body, short):
        q, b = row.quote_label.geometry(), row.body_label.geometry()
        assert q.right() < b.left(), "引用和正文不叠在一起"
    assert short.quote_label.width() == short.quote_label.natural_width(), "短引用按自己的宽度，不留空白"
    assert alone.quote_label.width() > alone.width() * _QUOTE_SHARE, "没写正文：引用占满"
    panel.close()


def _quote_clamp(mv):
    from src.ui.quotes import ClampText
    return mv.findChildren(ClampText)[0]


def test_find_reaches_quote_blocks_in_order(qt_app):
    """原来 Ctrl+F 只搜 QLabel：气泡里的引用块（自己画字）搜不到。"""
    from PySide6.QtWidgets import QLabel
    mv = _view()
    mv.append_user_text(compose(["61,570,595,788 字节"], "这是多少？"))
    mv.handle("大约 61.6 GB，也就是 615 亿字节。", "ai_msg")
    mv.show()
    _pump(qt_app)
    clamp = _quote_clamp(mv)
    assert mv.find_text("字节") == (1, 2), "引用里那处在回复前面，排第一"
    assert clamp._highlight == (15, 2)
    assert not any(l.hasSelectedText() for l in mv.findChildren(QLabel))
    assert mv.find_text("字节") == (2, 2)
    assert clamp._highlight is None, "跳到别处：引用块里的标记去掉"
    mv.clear_find()
    mv.close()


def test_find_hit_paints_the_selection_color(qt_app):
    from PySide6.QtGui import QPalette
    mv = _view()
    mv.append_user_text(compose(["61,570,595,788 字节"], "这是多少？"))
    mv.show()
    _pump(qt_app)
    clamp = _quote_clamp(mv)
    hl = clamp.palette().color(QPalette.Highlight)

    def has_highlight():
        img = clamp.grab().toImage()
        return any(img.pixelColor(x, y) == hl for x in range(0, img.width(), 1)
                   for y in range(0, img.height(), 2))

    assert not has_highlight()
    mv.find_text("字节")
    _pump(qt_app)
    assert has_highlight(), "命中处铺上选区色"
    mv.clear_find()
    _pump(qt_app)
    assert not has_highlight()
    mv.close()


def test_find_hit_in_folded_lines_expands_the_quote_then_folds_back(qt_app):
    from PySide6.QtWidgets import QLabel
    mv = _view()
    long = "代码导航按扩展名选语言服务器，" * 12 + "Java 用 jdtls。"
    mv.append_user_text(compose([long], "Java 那个要怎么装？"))
    mv.handle("装 jdtls 要先装 JDK。", "ai_msg")
    mv.show()
    _pump(qt_app)
    clamp = _quote_clamp(mv)
    folded_h = clamp.height()
    assert len(clamp._lines(clamp.width())) == 3
    reply = [l for l in mv.findChildren(QLabel) if "要先装 JDK" in l.text()][0]
    reply_y = reply.mapTo(mv.widget(), reply.rect().topLeft()).y()
    assert mv.find_text("jdtls") == (1, 2)
    # 不等事件循环：返回时就已经排好（否则有一帧引用叠在下面的回复上，闪一下）
    assert clamp.height() > folded_h, "命中在第三行之后：整段展开，当场排好"
    assert reply.mapTo(mv.widget(), reply.rect().topLeft()).y() > reply_y, "下面的回复当场让开"
    _pump(qt_app)
    assert clamp.is_expanded() and clamp.height() > folded_h
    assert clamp._lines(clamp.width())[-1].endswith("jdtls。")
    assert mv.find_text("jdtls") == (2, 2)
    assert clamp.height() == folded_h, "跳到下一处：当场收回三行"
    _pump(qt_app)
    assert not clamp.is_expanded() and clamp.height() == folded_h
    mv.find_text("jdtls")
    _pump(qt_app)
    assert clamp.is_expanded()
    mv.clear_find()
    _pump(qt_app)
    assert not clamp.is_expanded() and clamp.height() == folded_h, "关掉搜索：收回"
    mv.close()


def test_visible_hit_does_not_expand(qt_app):
    mv = _view()
    long = "开头就有关键词，" + "后面是很长的补充说明，" * 30
    mv.append_user_text(compose([long], "问题"))
    mv.show()
    _pump(qt_app)
    clamp = _quote_clamp(mv)
    assert mv.find_text("关键词") == (1, 1)
    assert not clamp.is_expanded(), "命中就在看得见的前三行：不展开"
    mv.clear_find()
    mv.close()
