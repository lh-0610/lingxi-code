"""回复正文里的项目文件路径：行内代码里的路径渲染成可点链接，点了在右侧文件面板里打开
（带行号的滚到那一行）；没有差异的文件给出当前全文。

路径识别在 markdown 渲染那一步（MarkdownRenderMixin._md_to_html），只认项目里真实存在的
文件；代码块里的路径不处理。Qt 部分全部 offscreen。
"""
import gc
import subprocess

import pytest
from PySide6.QtCore import QEvent, Qt

from src.ui.markdown_render import MarkdownRenderMixin, parse_file_link


class _Host:
    """_md_to_html 只用到宿主的这几样。"""

    def __init__(self):
        self._code_blocks = {}

    def _t(self, key):
        return "transparent" if "border" in key or key == "md_blockquote_bg" else "#222838"

    def _inline_svg_img(self, *args, **kwargs):
        return ""


def _render(md):
    return MarkdownRenderMixin._md_to_html(_Host(), md)


@pytest.fixture()
def proj(tmp_path):
    """当前会话的项目指到临时目录：正文里的路径按它认（和文件面板取差异是同一个根）。"""
    from src import session
    root = tmp_path / "proj"
    (root / "src" / "ui").mkdir(parents=True)
    (root / "src" / "ui" / "task_panel.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    session.get_active().project = str(root)
    return root


@pytest.fixture()
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


# ── markdown 渲染：行内代码 → 链接 ────────────────────────────

def test_inline_code_path_becomes_a_link(proj):
    html = _render("改这里的 `src/ui/task_panel.py`。")
    assert 'href="lingxi-file:src/ui/task_panel.py"' in html
    assert parse_file_link("lingxi-file:src/ui/task_panel.py") == ("src/ui/task_panel.py", 0)


def test_link_text_still_looks_like_inline_code(proj):
    """链接文字还是那段行内代码，看上去和普通代码没两样。"""
    html = _render("改这里的 `src/ui/task_panel.py`。")
    assert "<code" in html and ">src/ui/task_panel.py</code>" in html


def test_path_with_line_number_keeps_the_line(proj):
    html = _render("见 `src/ui/task_panel.py:835`")
    assert 'href="lingxi-file:src/ui/task_panel.py:835"' in html
    assert parse_file_link("lingxi-file:src/ui/task_panel.py:835") == ("src/ui/task_panel.py", 835)


def test_line_range_takes_the_first_line(proj):
    assert 'href="lingxi-file:src/ui/task_panel.py:1"' in _render("见 `src/ui/task_panel.py:1-2`")


def test_unknown_path_stays_plain(proj):
    """写错的路径、目录、命令片段都保持原样，不当链接。"""
    html = _render("没有 `src/nope.py`，目录 `src/ui`，命令 `pytest -q`。")
    assert "lingxi-file:" not in html
    assert "src/nope.py" in html and "pytest -q" in html


def test_fenced_code_is_not_linked(proj):
    html = _render("```\nsrc/ui/task_panel.py:1\n```\n")
    assert "lingxi-file:" not in html
    assert "src/ui/task_panel.py:1" in html, "代码块里的路径原样显示"


def test_escape_absolute_and_drive_paths_are_not_linked(proj, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    html = _render(f"`../outside.py` `{outside}` `C:/Windows/win.ini` `/etc/passwd`")
    assert "lingxi-file:" not in html


def test_parse_file_link_ignores_other_hrefs():
    assert parse_file_link("action:copy_code:0") is None
    assert parse_file_link("https://example.com") is None
    assert parse_file_link("") is None


# ── 消息流：链接可点，文字照样能选 ───────────────────────────

def test_body_text_keeps_text_selection_and_allows_links(qt_app):
    from src.ui.message_view import BodyText
    lbl = BodyText()
    flags = lbl.textInteractionFlags()
    assert flags & Qt.TextSelectableByMouse, "正文仍然要能选中、复制文字"
    assert flags & Qt.LinksAccessibleByMouse, "链接要能点"
    lbl.deleteLater()
    _pump(qt_app)


def test_clicking_a_body_link_reaches_the_message_view(qt_app, proj):
    from src.ui.message_view import BodyText, MessageView
    mv = MessageView()
    clicked = []
    mv.file_link_clicked.connect(clicked.append)
    mv.handle("m\n", "ai_label")
    mv.finalize_markdown(_render("见 `src/ui/task_panel.py:2`"))

    body = mv.findChildren(BodyText)[0]
    href = "lingxi-file:src/ui/task_panel.py:2"
    assert href in body.text(), "渲染完就带上了链接"
    body.linkActivated.emit(href)          # 等价于用户点了这段链接
    assert clicked == [href], "点了要转给宿主（ChatUI 在文件面板里打开）"
    mv.deleteLater()
    _pump(qt_app)


# ── 文件面板：滚到链接带的行号 ──────────────────────────────

def _panel(qt_app):
    from src.ui.file_panel import FilePanel
    from src.ui.theme import THEMES
    panel = FilePanel(None, theme_lookup=lambda k: THEMES["light"][k])
    panel.resize(460, 400)
    panel.show()
    _pump(qt_app)
    return panel


def _numbered_rows(n):
    return [(no, "ctx", f"line {no}") for no in range(1, n + 1)]


def test_open_file_with_line_puts_the_cursor_on_that_line(qt_app):
    panel = _panel(qt_app)
    panel.open_file("big.py", _numbered_rows(200), line=120)
    view = panel._panes["big.py"]
    assert view.textCursor().blockNumber() == 119, "带行号打开：停在那一行"
    assert view.verticalScrollBar().value() > 0, "滚下去了，不是停在文件开头"

    panel.open_file("big.py", _numbered_rows(200))
    assert view.textCursor().blockNumber() == 0, "不带行号再打开：回到开头"
    panel.close()
    panel.deleteLater()
    _pump(qt_app)


# ── ChatUI 集成：和任务面板「改动的文件」同一个入口 ─────────────

@pytest.fixture()
def ui(qt_app, monkeypatch, isolated_memory):
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in reply link tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import agent, mcp_client, models, tools
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    from src.ui import ChatUI
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    window.resize(1440, 900)
    window.show()
    _pump(qt_app)
    yield window
    _destroy_ui_cleanly(window, qt_app)


def _destroy_ui_cleanly(ui, app):
    """照 scripts/test_file_panel.py 的顺序销毁：先放完零秒回调，再 deleteLater 真销毁。"""
    import shiboken6
    from src import state
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
def git_project(tmp_path):
    import shutil
    if not shutil.which("git"):
        pytest.skip("git 未安装")
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)
    return repo


def test_file_link_opens_the_panel_at_that_line(ui, qt_app, git_project):
    """没有改动的文件：面板给出当前全文（带行号），并滚到链接带的那一行。"""
    from src import session
    session.get_active().project = str(git_project)

    ui._on_file_link_clicked("lingxi-file:app.py:3")
    _pump(qt_app)

    panel = ui.file_panel
    assert panel.isVisible() and panel.paths() == ["app.py"], "和任务面板的改动文件同一个入口"
    view = panel._panes["app.py"]
    text = view.toPlainText()
    assert "1 │ a = 1" in text, "没有差异也要看到当前内容"
    assert "3 │ c = 3" in text
    assert "没有差异" in text
    assert view.textCursor().blockNumber() == 3, "滚到第 3 行（前面有一行说明）"
    panel.close_all()
    _pump(qt_app)


def test_unknown_href_opens_nothing(ui, qt_app):
    ui._on_file_link_clicked("action:copy_code:0")
    _pump(qt_app)
    assert not ui.file_panel.isVisible()
    assert ui.file_panel.paths() == []
