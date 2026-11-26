"""自绘标题栏（src/ui/titlebar.py）：命中测试、顶栏布局、窗口按钮。

Win32 那一半（NCCALCSIZE / 窗框样式 / DWM 阴影 / 贴边分屏）只能在真窗口上看，这里测平台无关的部分：
哪里能拖、哪里是按钮、四边改大小、最大化键报 HTMAXBUTTON（Win11 分屏布局靠它）、顶栏横跨右侧面板。
"""
import gc
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint
from PySide6.QtWidgets import QApplication

from src import state
from src.ui import titlebar as tb


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    gc.collect()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    from src import tools
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(state, "ui_ref", None)
    monkeypatch.setattr(tb, "enabled", lambda: True)          # offscreen 下也挂上自绘标题栏
    w = ChatUI()
    w.resize(1400, 860)
    w.show()
    for _ in range(5):
        qapp.processEvents()
    yield w
    if state.ui_ref is w:
        state.ui_ref = None
    w.close()
    qapp.processEvents()
    w.deleteLater()
    gc.collect()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_enabled_only_on_windows_platform_plugin(qapp):
    assert tb.enabled() is False, "offscreen 下不接管窗框"


def test_frameless_and_buttons_are_the_last_item_of_the_top_bar(ui):
    """参照 Claude 桌面版：窗口按钮是顶栏这一行的最后一项，贴着窗口右上角，和顶栏图标一条线。"""
    from PySide6.QtCore import Qt
    assert ui.windowFlags() & Qt.FramelessWindowHint
    btns = ui._frameless.buttons
    assert btns.isVisible() and btns.parentWidget() is ui._header_bar
    at = btns.mapTo(ui, QPoint(0, 0))
    assert at.x() + btns.width() == ui.width() and at.y() == 0
    assert (btns.width(), btns.height()) == (3 * tb.BUTTON_W, tb.BUTTON_H)
    assert btns.height() == ui._header_bar.height(), "和顶栏一样高，图标在一条线上"


def test_hit_test_edges_caption_and_controls(ui):
    btns = ui._frameless.buttons
    w, h = ui.width(), ui.height()

    def ht(x, y, maximized=False):
        return tb.hit_test(ui, QPoint(x, y), maximized=maximized, buttons=btns)

    assert ht(1, 300) == tb.HTLEFT and ht(w - 2, 300) == tb.HTRIGHT
    assert ht(300, 1) == tb.HTTOP and ht(300, h - 2) == tb.HTBOTTOM
    assert ht(1, 1) == tb.HTTOPLEFT and ht(w - 2, h - 2) == tb.HTBOTTOMRIGHT
    assert ht(1, 300, maximized=True) != tb.HTLEFT, "最大化后边缘不再改大小"
    # 最大化键报 HTMAXBUTTON（Win11 鼠标停上去弹分屏布局）；另两个键是普通按钮
    mb = btns.max_btn.mapTo(ui, btns.max_btn.rect().center())
    assert ht(mb.x(), mb.y()) == tb.HTMAXBUTTON
    cb = btns.close_btn.mapTo(ui, btns.close_btn.rect().center())
    assert ht(cb.x(), cb.y()) == tb.HTCLIENT
    # 顶栏空白处能拖；顶栏上的按钮照常点；顶部 56px 以下都是客户区
    header = ui._header_bar
    blank = header.mapTo(ui, QPoint(header.width() // 2, header.height() // 2))
    assert ht(blank.x(), blank.y()) == tb.HTCAPTION
    toggle = ui.sidebar_toggle_btn.mapTo(ui, ui.sidebar_toggle_btn.rect().center())
    assert ht(toggle.x(), toggle.y()) == tb.HTCLIENT
    assert ht(blank.x(), tb.CAPTION_HEIGHT + 20) == tb.HTCLIENT
    # 侧栏顶上的品牌区也能拖
    assert ht(120, 30) == tb.HTCAPTION


def test_side_panels_start_below_the_top_bar(ui, qapp):
    """顶栏一整行横跨对话区和右侧面板；右侧面板的标签行和关闭按钮在顶栏下面，
    不和窗口的最小化 / 关闭挤在同一行（用户实测：两个 × 挨着分不清）。"""
    ui.file_panel.open_file("a.py", [(1, "ctx", "x = 1")])
    for _ in range(5):
        qapp.processEvents()
    assert ui.file_panel.isVisible()
    header_bottom = ui._header_bar.mapTo(ui, QPoint(0, ui._header_bar.height())).y()
    tab_row_top = ui.file_panel.tab_row.mapTo(ui, QPoint(0, 0)).y()
    assert tab_row_top >= header_bottom
    header_right = ui._header_bar.mapTo(ui, QPoint(ui._header_bar.width(), 0)).x()
    panel_right = ui.file_panel.mapTo(ui, QPoint(ui.file_panel.width(), 0)).x()
    assert header_right >= panel_right, "顶栏横跨到右侧面板上方"
    ui.file_panel.close_all()


def test_maximize_button_reflects_state(ui, qapp):
    btns = ui._frameless.buttons
    ui.showMaximized()
    qapp.processEvents()
    btns.sync_state()
    assert btns.max_btn.maximized and btns.max_btn.toolTip() == "还原"
    ui.showNormal()
    qapp.processEvents()
    btns.sync_state()
    assert not btns.max_btn.maximized and btns.max_btn.toolTip() == "最大化"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 最大化只在 Windows 上有")
def test_maximize_goes_through_the_system_on_a_real_window():
    """按钮最大化要让系统也认为最大化了（Qt 的 showMaximized 对无边框窗口只是摆成屏幕大小，
    拖标题栏不会缩小）；系统那边最大化后按钮要能还原（Qt 的 showNormal 这时什么都不做）。
    离屏走不到 Win32，起子进程开真窗口跑 scripts/titlebar_native_check.py。"""
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(here, "titlebar_native_check.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=120, stdin=subprocess.DEVNULL)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines and lines[-1] == "OK", f"{r.stdout[-1500:]}\n{r.stderr[-1500:]}"


def test_settings_offers_native_title_bar_fallback(qapp, tmp_path, monkeypatch):
    import json
    from src.ui import settings_dialog as sd
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sd, "CONFIG_PATH", str(cfg))
    from src.ui import dialogs
    monkeypatch.setattr(dialogs.ThemedDialog, "exec", lambda self: 0)
    dlg = sd.SettingsDialog()
    assert "native_title_bar" in dlg.fields
    dlg.fields["native_title_bar"].setChecked(True)
    dlg._save()
    assert json.loads(cfg.read_text(encoding="utf-8"))["native_title_bar"] is True
    dlg.deleteLater()


def test_hover_block_is_centered_but_the_button_stays_full_height(qapp):
    """悬停底色是居中的圆角块，不铺满 56px 高的整列（用户反馈关闭键太高）；按钮本身仍是整列，
    最大化时鼠标甩到屏幕右上角照样点得中关闭。"""
    from PySide6.QtGui import QColor
    from src.ui.titlebar import BUTTON_H, HOVER_H, WindowButtons, _CaptionButton
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    wb = WindowButtons(host)
    close = next(b for b in wb.findChildren(_CaptionButton) if b.kind == "close")
    assert close.height() == BUTTON_H
    close.nc_hover = True
    img = close.grab().toImage()
    red = QColor("#c42b1c")
    mid = img.pixelColor(img.width() // 2 - 12, img.height() // 2)
    top = img.pixelColor(img.width() // 2, 2)
    assert mid == red, "中间是关闭键的红色悬停块"
    assert top != red and (BUTTON_H - HOVER_H) >= 12, "上沿留白，不是整列都红"
