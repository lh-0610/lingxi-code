"""对话区右边的停靠文件面板：点改动文件开标签页（不弹模态）、内容带行号、新增绿删除红、
关掉后对话区恢复原来的宽度，看文件时对话照常能输入。

Qt 部分全部 offscreen，模型 / MCP / 网络离线，偏好落在 isolated_memory 里。
"""
import gc

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QTextBlock, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTabBar

from src import agent, state
from src.ui import ChatUI
from src.ui.theme import THEMES


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, n=6):
    for _ in range(n):
        app.processEvents()


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in file panel tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import mcp_client, models, tools
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    window = ChatUI()
    window.resize(1440, 900)
    window.show()
    _pump(qapp)
    yield window
    _destroy_ui_cleanly(window, qapp)


def _destroy_ui_cleanly(ui, app):
    """完整清理序列，照 scripts/test_sidebar_load_more.py 的写法。

    先后顺序不能反：窗口还活着时先放完挂着的零秒回调，再 deleteLater + DeferredDelete
    真正销毁；留到解释器退出时做会 access violation。
    """
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
def git_project(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git 未安装")
    repo = tmp_path / "proj"
    repo.mkdir()
    for args in (["init"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    (repo / "app.py").write_text("a = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    return repo


# ── tools_git：逐行差异 ──────────────────────────────────────

def test_diff_lines_carry_new_file_line_numbers(git_project):
    from src.tools_git import file_diff_lines
    (git_project / "app.py").write_text("a = 2\nb = 3\n", encoding="utf-8")
    rows = file_diff_lines(str(git_project), "app.py")
    kinds = [kind for _no, kind, _text in rows]
    assert "add" in kinds and "del" in kinds
    adds = [(no, text) for no, kind, text in rows if kind == "add"]
    assert adds == [(1, "+a = 2"), (2, "+b = 3")], "新增行带新文件一侧的行号"
    dels = [text for _no, kind, text in rows if kind == "del"]
    assert dels == ["-a = 1"], "删除行没有新行号"


def test_untracked_file_body_is_numbered_additions(git_project):
    from src.tools_git import file_diff_lines
    (git_project / "new.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    rows = file_diff_lines(str(git_project), "new.py")
    assert rows[0][1] == "note" and "新文件" in rows[0][2]
    assert [(no, kind, text) for no, kind, text in rows[1:]] == [
        (1, "add", "x = 1"), (2, "add", "y = 2")], "新文件正文按新增行逐行编号"


def test_clean_file_shows_current_content_instead_of_one_line(git_project):
    """和上次提交没有差异：说明一行加当前全文，逐行编号、按普通上下文行。

    点回复里的文件多半是这种情况——只写一句没有差异等于什么都没看到。
    """
    from src.tools_git import file_diff_lines
    rows = file_diff_lines(str(git_project), "app.py")
    assert rows[0][1] == "note" and "没有差异" in rows[0][2]
    assert [(no, kind, text) for no, kind, text in rows[1:]] == [
        (1, "ctx", "a = 1")], "没有差异时也给出当前全文，按普通上下文行逐行编号"


def _commit_file(repo, name, data: bytes):
    import subprocess
    (repo / name).write_bytes(data)
    for args in (["add", name], ["commit", "-qm", name]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)


def test_clean_long_file_is_not_cut_at_the_diff_limit(git_project):
    """大文件（比如 chat_window.py）全文不能按差异的 20000 字截：回复里 `xxx.py:835` 要滚得到。"""
    from src.tools_git import file_diff_lines
    _commit_file(git_project, "big.py", "".join(f"x_{i} = {i}\n" for i in range(3000)).encode())
    rows = file_diff_lines(str(git_project), "big.py")
    assert (2000, "ctx", "x_1999 = 1999") in rows


def test_clean_huge_file_says_it_was_cut(git_project, monkeypatch):
    from src import tools_git
    monkeypatch.setattr(tools_git, "_FULL_TEXT_MAX_CHARS", 50)
    _commit_file(git_project, "big.py", "".join(f"x_{i} = {i}\n" for i in range(100)).encode())
    rows = tools_git.file_diff_lines(str(git_project), "big.py", max_chars=10)
    assert rows[-1][1] == "note" and "文件太长" in rows[-1][2], "截断了要写明，不能看起来像文件就这么长"


def test_clean_binary_file_is_not_shown_as_text(git_project):
    from src.tools_git import file_diff_lines
    _commit_file(git_project, "logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    rows = file_diff_lines(str(git_project), "logo.png")
    assert len(rows) == 1 and rows[0][1] == "note" and "二进制" in rows[0][2]


# ── FilePanel 本身 ─────────────────────────────────────────

def _panel(qapp, keep):
    """建一个独立的面板（没有父控件），登记进 keep，用例结束后统一销毁。"""
    from src.ui.file_panel import FilePanel
    panel = FilePanel(None, theme_lookup=lambda k: THEMES["light"][k])
    keep.append(panel)
    return panel


@pytest.fixture()
def keep(qapp):
    """用例里建的独立控件，结束后销毁——留着没人管的顶层控件会在退出时踩到已删对象。"""
    panels = []
    yield panels
    for panel in panels:
        panel.close()
        panel.deleteLater()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_same_file_opens_one_tab_and_switches_back(qapp, keep):
    panel = _panel(qapp, keep)
    panel.open_file("a.py", [(1, "add", "+x")])
    panel.open_file("b.py", [(1, "add", "+y")])
    assert panel.paths() == ["a.py", "b.py"]
    assert panel.current_path() == "b.py"

    panel.open_file("a.py", [(1, "add", "+x2")])
    assert panel.paths() == ["a.py", "b.py"], "同一个文件不再开一个标签"
    assert panel.current_path() == "a.py", "再点就切过去"
    assert "x2" in panel._panes["a.py"].toPlainText(), "切过去时内容刷成最新"

    panel.open_file("src\\c.py", [])
    panel.open_file("src/c.py", [])
    assert panel.paths().count("src\\c.py") == 1, "反斜杠 / 正斜杠算同一个文件"


def test_tab_has_a_close_button_and_closing_the_last_tab_hides_the_panel(qapp, keep):
    from PySide6.QtWidgets import QPushButton
    panel = _panel(qapp, keep)
    panel.open_file("a.py", [])
    panel.open_file("b.py", [])
    closed = []
    panel.closed.connect(lambda: closed.append(True))

    btns = [b for b in panel.findChildren(QPushButton) if b.objectName() == "fileTabClose"]
    assert len(btns) == 2, "每个标签带 ×"
    # 按标签取按钮：findChildren 的顺序不保证跟标签顺序一致
    b_btn = panel.tabs.tabButton(panel._tab_index("b.py"), QTabBar.RightSide)
    QTest.mouseClick(b_btn, Qt.LeftButton)
    assert panel.paths() == ["a.py"], "关掉的是它自己那个标签"
    assert not closed

    panel.close_all()
    assert panel.paths() == [] and not panel.isVisible()
    assert closed == [True], "面板自己收起，通知主窗口重算宽度"


def test_rows_are_numbered_and_colored(qapp, keep):
    panel = _panel(qapp, keep)
    panel.open_file("a.py", [(1, "ctx", " a = 1"), (2, "add", "+b = 2"), (None, "del", "-c = 3")])
    view = panel._panes["a.py"]
    text = view.toPlainText()
    assert "1 │  a = 1" in text, "正文带行号"
    assert "2 │ +b = 2" in text
    assert "  │ -c = 3" in text, "删除行没有新行号，留空位对齐"

    blocks = []
    block = view.document().begin()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    add_block, del_block = blocks[1], blocks[2]
    assert THEMES["light"]["mv_ok"] in _colors(add_block), "新增行绿"
    assert THEMES["light"]["mv_bad"] in _colors(del_block), "删除行红"
    assert add_block.blockFormat().background().color().name() != THEMES["light"]["mv_view_bg"], \
        "新增行铺一层淡绿底"


def _colors(block: QTextBlock) -> list:
    """这一块里出现过的前景色（行号一段、正文一段各一个格式）。

    PySide6 的 QTextBlock.iterator 只能 for 遍历、拿不到片段对象，改走光标逐字读格式。
    """
    doc = block.document()
    cur = QTextCursor(doc)
    out = []
    for i in range(max(0, block.length() - 1)):
        cur.setPosition(block.position() + i)
        out.append(cur.charFormat().foreground().color().name())
    return out


def test_theme_lookup_is_read_live(qapp, keep):
    panel = _panel(qapp, keep)
    # 真实的 unified diff 一定带 hunk 头；没有 hunk 头的 add 行是「新文件正文」，不剥首字符
    panel.open_file("a.py", [(None, "hunk", "@@ -0,0 +1 @@"), (1, "add", "+x")])
    panel.theme_lookup = lambda k: THEMES["dark"][k]
    panel.apply_theme()
    block = panel._panes["a.py"].document().begin().next()
    assert THEMES["dark"]["mv_ok"] in _colors(block), "切到暗色：正文按新主题重画"


# ── ChatUI 集成 ────────────────────────────────────────────

def test_clicking_a_changed_file_docks_the_panel(ui, qapp, git_project):
    from PySide6.QtWidgets import QWidget
    from src import session
    (git_project / "app.py").write_text("a = 2\nb = 3\n", encoding="utf-8")
    sess = session.get_active()
    sess.project = str(git_project)
    sess.verification["dirty_files"] = ["app.py"]
    sess.verification["change_revision"] = 3

    ui._render_plan_panel()
    _pump(qapp)
    rows = [w for w in ui.plan_panel.findChildren(QWidget) if w.objectName() == "fileRow"]
    assert rows

    chat_width = ui.chat_area.width()
    QTest.mouseClick(rows[0], Qt.LeftButton)
    _pump(qapp)

    panel = ui.file_panel
    assert panel.isVisible() and panel.paths() == ["app.py"]
    assert "app.py" in panel.path_label.text(), "面板顶部显示文件路径"
    assert ui.chat_area.width() < chat_width, "面板占的是右边一条，不挡对话"

    # 再点一次同一个文件：还是只有一个标签
    QTest.mouseClick(rows[0], Qt.LeftButton)
    _pump(qapp)
    assert panel.paths() == ["app.py"]

    panel.close_all()
    _pump(qapp)
    assert not panel.isVisible()
    assert ui.chat_area.width() == chat_width, "关掉面板：对话区恢复原来的宽度"


def test_typing_keeps_working_while_the_panel_is_open(ui, qapp, tmp_path):
    from src import session
    sess = session.get_active()
    sess.project = str(tmp_path)          # 不是 git 仓库也行：面板会把"没有差异"如实显示

    ui._on_task_panel_file_clicked("app.py")
    _pump(qapp)
    assert ui.file_panel.isVisible()

    ui.entry.setFocus()
    QTest.keyClicks(ui.entry, "hi")
    _pump(qapp)
    assert ui.entry.toPlainText() == "hi", "看文件的时候输入框照常能打字"


def test_panel_width_is_remembered(ui, qapp):
    from src.ui.prefs import _load_ui_prefs
    ui._on_task_panel_file_clicked("whatever.py")
    _pump(qapp)
    ui.file_panel.width_changed.emit(520)
    assert _load_ui_prefs().get("file_panel_width") == 520
    assert ui._file_panel_wanted_width == 520


def test_narrow_window_hides_the_panel_but_keeps_its_tabs(ui, qapp):
    ui._on_task_panel_file_clicked("a.py")
    _pump(qapp)
    assert ui.file_panel.isVisible()

    ui.resize(900, 700)
    _pump(qapp)
    assert not ui.file_panel.isVisible(), "放不下时先藏起来，不把对话挤没"
    assert ui.file_panel.paths() == ["a.py"], "标签页留着"

    ui.resize(1440, 900)
    _pump(qapp)
    assert ui.file_panel.isVisible(), "窗口宽回来：面板回来，文件还在"


def test_theme_toggle_recolors_the_panel(ui, qapp):
    ui._on_task_panel_file_clicked("a.py")
    _pump(qapp)
    ui._toggle_theme()
    _pump(qapp)
    theme = ui.theme
    view = ui.file_panel._panes["a.py"]
    assert THEMES[theme]["mv_view_bg"] in view.styleSheet()


def test_task_panel_yields_to_the_file_panel_when_space_is_short(ui, qapp):
    """侧栏 + 任务面板 + 文件面板三栏挤不下时，任务面板先让成浮层，对话栏还留得下顶栏；
    关掉文件面板，任务面板自己停靠回来。"""
    ui._task_panel_open = True
    ui._apply_task_panel_collapse()
    _pump(qapp)
    assert ui._task_panel_docked and ui.plan_panel.isVisible()

    ui._on_task_panel_file_clicked("a.py")
    _pump(qapp)
    assert ui.file_panel.isVisible()
    assert not ui._task_panel_docked and not ui.plan_panel.isVisible()
    rest = ui.width() - ui._sidebar_shown_width() - ui.file_panel.width()
    assert rest >= ui._TASK_PANEL_MIN_CHAT, "对话栏不窄于顶栏最窄一档，按钮不会叠在一起"

    ui.file_panel.close_all()
    _pump(qapp)
    assert ui._task_panel_docked and ui.plan_panel.isVisible()


# ── 结果卡的「查看改动」：也是在同一个文件面板里开标签页 ────────────────

def _commit_all(repo):
    import subprocess
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                   capture_output=True, stdin=subprocess.DEVNULL)


def _toasts(ui, monkeypatch):
    """把提示条换成记录器：真实 toast 是浮在窗口上的 QLabel + 定时器，测试里只关心文案。"""
    shown = []
    monkeypatch.setattr(ui, "_show_toast", lambda text, duration=1500: shown.append(text))
    return shown


def test_result_card_opens_one_tab_per_changed_file(ui, qapp, monkeypatch, git_project):
    from src import session
    (git_project / "a.py").write_text("a = 2\n", encoding="utf-8")
    (git_project / "b.py").write_text("b = 1\n", encoding="utf-8")
    _commit_all(git_project)
    (git_project / "a.py").write_text("a = 3\n", encoding="utf-8")
    (git_project / "b.py").write_text("b = 2\n", encoding="utf-8")
    session.get_active().project = str(git_project)
    toasts = _toasts(ui, monkeypatch)

    ui._on_result_view_diff({"work_dir": str(git_project), "project": str(git_project),
                             "files": ["a.py", "b.py", "a.py", "src\\c.py"]})
    _pump(qapp)

    panel = ui.file_panel
    assert panel.isVisible(), "结果卡的查看改动走右侧停靠面板，不弹模态窗口"
    assert panel.paths() == ["a.py", "b.py", "src\\c.py"], "一个文件一个标签页，重复路径只开一个"
    assert "a = 3" in panel._panes["a.py"].toPlainText(), "差异按这个文件的当前状态取"
    assert toasts == [], "没超过上限就不用提示"


def test_result_card_opens_at_most_eight_tabs_and_says_how_many_are_left(ui, qapp, monkeypatch,
                                                                          git_project):
    from src import session
    session.get_active().project = str(git_project)
    files = [f"f{i}.py" for i in range(11)]
    for name in files:
        (git_project / name).write_text("x = 1\n", encoding="utf-8")
    toasts = _toasts(ui, monkeypatch)

    ui._on_result_view_diff({"work_dir": str(git_project), "project": str(git_project),
                             "files": files})
    _pump(qapp)

    assert ui.file_panel.paths() == files[:8], "最多开 8 个标签页"
    assert toasts, "超出的文件要提示"
    assert "3" in toasts[-1] and "8" in toasts[-1], f"要说清还剩几个没开：{toasts[-1]}"


def test_result_card_diffs_the_runs_own_work_dir(ui, qapp, monkeypatch, git_project, tmp_path):
    """worktree 里跑的那一轮：改动在隔离区，主会话的项目目录里根本没有它。"""
    import subprocess
    from src import session
    wt = tmp_path / "wt"
    wt.mkdir()
    for args in (["git", "init", "-q"],
                 ["git", "config", "user.email", "t@example.com"],
                 ["git", "config", "user.name", "T"]):
        subprocess.run(args, cwd=str(wt), check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)
    (wt / "app.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(wt)
    (wt / "app.py").write_text("x = 987654\n", encoding="utf-8")
    session.get_active().project = str(git_project)     # 主会话的工作区里改的是别的文件

    ui._on_result_view_diff({"work_dir": str(wt), "project": str(git_project),
                             "files": ["app.py"]})
    _pump(qapp)

    text = ui.file_panel._panes["app.py"].toPlainText()
    assert "987654" in text, "拿主会话的项目目录去 diff，隔离区的改动就看不见了"
    assert "没有差异" not in text


def test_result_card_diff_view_is_read_only(ui, qapp, git_project):
    """只读查看：不开标签页当"已审阅"，验证状态一个字段都不动。"""
    from src import session
    (git_project / "app.py").write_text("a = 2\n", encoding="utf-8")
    sess = session.get_active()
    sess.project = str(git_project)
    sess.verification["dirty_files"] = ["app.py"]
    before = {k: (list(v) if isinstance(v, list) else v)
              for k, v in sess.verification.items() if k != "change_revision"}

    ui._on_result_view_diff({"work_dir": str(git_project), "project": str(git_project),
                             "files": ["app.py"]})
    _pump(qapp)

    assert "app.py" in ui.file_panel.paths(), "先确认真的开了标签页"
    after = {k: (list(v) if isinstance(v, list) else v)
             for k, v in sess.verification.items() if k != "change_revision"}
    assert after == before, "看 diff 不能替模型记「已审阅」"
    assert sess.verification.get("diff_reviewed") is False


# ── 浏览器标签（第 6 步）：懒加载 / 生命周期 / 徽标 / 真实交互 / 关闭竞态 ──

@pytest.fixture(scope="module")
def browser_server():
    import http.server
    import os
    import threading
    fixtures = os.path.join(os.path.dirname(__file__), "browser_fixtures")

    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=fixtures, **kw)

        def do_GET(self):
            if self.path.startswith("/slow"):
                import time
                time.sleep(8)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html><body>slow</body></html>")
                return
            super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def browser_ui(ui, qapp, browser_server):
    yield ui
    from src.browser import page
    if page.is_browser_open():
        page.close_browser()
    _pump(qapp, 20)


def _wait_page(qapp, pred, timeout=15):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _pump_deletes(qapp, n=30):
    """deleteLater 的 DeferredDelete 裸 processEvents 不一定派发，显式送一遍。"""
    from PySide6.QtCore import QEvent
    for _ in range(n):
        qapp.processEvents()
        qapp.sendPostedEvents(None, QEvent.DeferredDelete)


def test_browser_tab_lifecycle(browser_ui, qapp, browser_server):
    """开标签才创建 view；与文件标签并存；切走隐藏不销毁；关标签释放；能重开。"""
    from src.browser import page
    from src.ui.file_panel import BROWSER_KEY
    ui = browser_ui
    assert page.is_browser_open() is False, "构造 ChatUI / 文件面板不该拉起浏览器"

    ui.file_panel.open_browser(browser_server + "/form.html")
    _pump(qapp)
    assert page.is_browser_open() is True
    assert BROWSER_KEY in ui.file_panel.paths()
    assert ui.file_panel.current_path() == BROWSER_KEY
    # 标签内容走 LeftSide 按钮（ElideMiddle 对 CJK 误判余量，tabText 会被截成"..."）：
    # 设计稿是页面图标 + 页面标题，加载完显示页面标题
    from PySide6.QtWidgets import QTabBar
    from src.ui.file_panel import _BrowserTabLabel
    bi = ui.file_panel._tab_index(BROWSER_KEY)
    tab_label = ui.file_panel.tabs.tabButton(bi, QTabBar.LeftSide)
    assert isinstance(tab_label, _BrowserTabLabel)
    assert ui.file_panel.tabs.tabText(bi) == ""
    assert _wait_page(qapp, lambda: "form.html" in page.get_browser().page.url().toString())
    assert _wait_page(qapp, lambda: tab_label.text() == "表单页"), tab_label.text()

    ui.file_panel.open_file("a.py", [(1, "ctx", "x = 1")])
    _pump(qapp)
    assert "a.py" in ui.file_panel.paths() and BROWSER_KEY in ui.file_panel.paths()
    browser_index = ui.file_panel._tab_index(BROWSER_KEY)
    ui.file_panel.tabs.setCurrentIndex(browser_index)
    _pump(qapp)
    assert ui.file_panel.stack.currentWidget() is ui.file_panel._browser_view
    file_index = ui.file_panel._tab_index("a.py")
    ui.file_panel.tabs.setCurrentIndex(file_index)
    _pump(qapp)
    assert page.is_browser_open() is True, "切走只是隐藏，不销毁页面"

    ui.file_panel.close_file(BROWSER_KEY)
    _pump(qapp, 20)
    assert page.is_browser_open() is False, "关标签释放 view/page"
    assert "a.py" in ui.file_panel.paths()

    ui.file_panel.open_browser(browser_server + "/form.html")
    _pump(qapp)
    assert page.is_browser_open() is True, "关了还能重开"
    ui.file_panel.close_all()
    _pump(qapp, 20)
    assert page.is_browser_open() is False
    assert not ui.file_panel.isVisible()


def test_status_card_follows_bridge_activity(browser_ui, qapp, browser_server):
    """模型操作页面：页面描靛蓝边 + 底部状态卡「灵犀正在点击」，标签上出小圆点；
    动作之间的短空档不收（免得一闪一闪），停下一会儿才收。"""
    import time
    from src.browser.bridge import get_bridge
    ui = browser_ui
    ui.file_panel.open_browser(browser_server + "/form.html")
    _pump(qapp)
    view = ui.file_panel._browser_view
    label = ui.file_panel._browser_tab_label
    get_bridge().activity.emit("点击")
    _pump(qapp)
    assert view.status_card.isVisible() and view.model_active
    assert view.status_card.title.text() == "灵犀正在点击"
    assert view.page_frame.property("active") == "true"
    assert label.dot.isVisible()
    get_bridge().activity.emit("")
    _pump(qapp)
    assert view.status_card.isVisible(), "动作之间的空档不收"
    get_bridge().activity.emit("输入")
    _pump(qapp)
    assert "第 2 步" in view.status_card.sub.text()
    get_bridge().activity.emit("")
    deadline = time.time() + 3
    while view.model_active and time.time() < deadline:
        _pump(qapp, 2)
        time.sleep(0.05)
    assert not view.status_card.isVisible() and not label.dot.isVisible()
    assert view.page_frame.property("active") == "false"
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_status_card_stop_stops_the_driving_session(browser_ui, qapp, browser_server, monkeypatch):
    from src import session
    from src.browser import bridge as _bridge
    ui = browser_ui
    ui.file_panel.open_browser(browser_server + "/form.html")
    _pump(qapp)
    view = ui.file_panel._browser_view
    bg_sess = session.Session()                    # 在后台驱动浏览器的另一个会话
    monkeypatch.setattr(_bridge.get_bridge(), "_driver", bg_sess)
    _bridge.get_bridge().activity.emit("点击")
    _pump(qapp)
    view.status_card.stop_btn.click()
    _pump(qapp)
    assert bg_sess.stop_flag is True
    assert session.get_active().stop_flag is not True, "前台会话不受牵连"
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_empty_state_lists_local_services_and_recent(browser_ui, qapp, browser_server):
    from src import background as bg
    from src import session
    from src.browser import security
    from src.ui import browser_panel

    class _Proc:
        pid = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    ident = bg.capture_session_identity(session.get_active())
    bg_id = bg.start("python -m http.server", _Proc(), cwd=".", project=None, **ident,
                     spawn_reader=False)
    bg.append_output(bg_id, "Serving on " + browser_server + "/\n")
    try:
        ui = browser_ui
        ui.file_panel.open_browser("")
        _pump(qapp)
        view = ui.file_panel._browser_view
        assert view.content.currentWidget() is view.empty
        assert view.tab_title() == "新标签页"
        assert len(view.empty.service_buttons) == 1
        view.empty.service_buttons[0].click()
        assert _wait_page(qapp, lambda: view.content.currentWidget() is view.page_frame)
        sess = session.get_active()
        assert security.parse_origin(browser_server) in sess.browser_origin_allowlist, \
            "人点「打开」灵犀自己起的服务 = 信任这个来源（同后台面板）"
        # 「最近打开」是进程内的列表，前面的用例可能已经记过这个地址：等页面真的加载完再看
        assert _wait_page(qapp, lambda: view.page_url().startswith(browser_server)
                          and not view._loading)
        assert any(r["url"].startswith(browser_server) for r in browser_panel.recent_pages())
        assert view.chip.isVisible() and view.chip.text() == "本机 · 已信任", view.chip.text()
        ui.file_panel.close_all()
        _pump(qapp, 20)
    finally:
        with bg._LOCK:
            bg._tasks.pop(bg_id, None)


def test_connection_refused_shows_our_error_page(browser_ui, qapp):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                      # 拿一个此刻没人监听的端口
    ui = browser_ui
    ui.file_panel.open_browser(f"http://127.0.0.1:{port}/")
    view = ui.file_panel._browser_view
    assert _wait_page(qapp, lambda: view.failed, timeout=15)
    assert view.content.currentWidget() is view.error_page
    assert view.error_page.title.text() == f"连不上 127.0.0.1:{port}"
    assert "没有服务在运行" in view.error_page.body.text()
    assert view.error_page.background_btn.isVisibleTo(view.error_page), "本机地址才给「去后台看看」"
    assert ui.file_panel._browser_tab_label.text() == "无法访问"
    seen = []
    ui.file_panel.background_requested.connect(lambda: seen.append(1))
    view.error_page.background_btn.click()
    assert seen == [1]
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_address_bar_typing_navigates(browser_ui, qapp, browser_server):
    """真实交互：地址栏键入网址 + 回车 → 页面真的导航过去。"""
    from src.browser import page
    ui = browser_ui
    ui.file_panel.open_browser()
    _pump(qapp)
    ui.activateWindow()
    view = ui.file_panel._browser_view
    view.address.setFocus()
    _pump(qapp)
    QTest.keyClicks(view.address, browser_server + "/form.html")
    QTest.keyClick(view.address, Qt.Key_Return)
    assert _wait_page(qapp, lambda: "form.html" in page.get_browser().page.url().toString()), \
        "回车后应导航到输入网址"
    assert _wait_page(qapp, lambda: "表单页" in page.get_browser().page.title()), \
        "标题随加载完成更新（titleChanged 比 url 晚到）"
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_invalid_url_flashes_and_does_not_navigate(browser_ui, qapp, browser_server):
    from src.browser import page
    ui = browser_ui
    ui.file_panel.open_browser(browser_server + "/form.html")
    assert _wait_page(qapp, lambda: "form.html" in page.get_browser().page.url().toString())
    view = ui.file_panel._browser_view
    view.navigate("file:///c:/windows/win.ini")
    _pump(qapp)
    assert view.badge.isVisible() and "不支持的协议" in view.badge.text()
    assert "form.html" in page.get_browser().page.url().toString(), "非法网址不该导航"
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_f12_docks_devtools_under_the_page(browser_ui, qapp, browser_server):
    """设计稿：开发者工具停靠在页面下方（不是另一个窗口），再按 F12 收起；能弹出成单独窗口。"""
    from src.browser import page
    ui = browser_ui
    ui.file_panel.open_browser(browser_server + "/form.html")
    _pump(qapp)
    ui.activateWindow()
    qapp.setActiveWindow(ui)
    view = ui.file_panel._browser_view
    view.address.setFocus()
    _pump(qapp)
    QTest.keyClick(view.address, Qt.Key_F12)
    _pump(qapp, 10)
    b = page.get_browser()
    assert view.devtools_panel.isVisible() and view._devtools_view is not None
    assert not view._devtools_view.isWindow(), "停靠在面板里"
    assert b.page.devToolsPage() is view._devtools_view.page()
    assert view.devtools_btn.isChecked()
    QTest.keyClick(view.address, Qt.Key_F12)
    _pump(qapp, 5)
    assert not view.devtools_panel.isVisible() and not view.devtools_btn.isChecked()
    view._open_devtools()
    view.devtools_popout_btn.click()
    _pump(qapp, 10)
    assert not view.devtools_panel.isVisible()
    assert getattr(b, "_devtools_view", None) is not None, "弹出成单独窗口"
    b._devtools_view.close()
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_close_tab_while_model_operating(browser_ui, qapp, browser_server):
    """硬要求 3：面板关掉时模型正在加载 → 立刻回"浏览器已关闭"，不崩不卡；之后能重开。"""
    import threading
    import time
    from src.browser import page
    from src.browser.bridge import get_bridge
    from src.ui.file_panel import BROWSER_KEY
    ui = browser_ui
    ui.file_panel.open_browser()
    _pump(qapp)
    b = get_bridge()
    box = {}

    def worker():
        box["r"] = b.call("open", timeout=20, url=browser_server + "/slow")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert _wait_page(qapp, lambda: b._open_started, timeout=5), "导航应已开始"
    t0 = time.perf_counter()
    ui.file_panel.close_file(BROWSER_KEY)
    _pump(qapp)
    t.join(timeout=5)
    elapsed = (time.perf_counter() - t0) * 1000
    r = box.get("r")
    assert r and r["ok"] is False and "浏览器已关闭" in r.get("error", ""), r
    assert elapsed < 3000, f"应立刻返回，实际 {elapsed:.0f}ms"
    assert not t.is_alive(), "worker 不该卡死"

    r2 = {}

    def reopen():
        r2["r"] = b.call("open", timeout=20, url=browser_server + "/form.html")

    t2 = threading.Thread(target=reopen, daemon=True)
    t2.start()
    assert _wait_page(qapp, lambda: "r" in r2, timeout=25)
    assert r2["r"]["ok"] is True, r2
    assert page.is_browser_open() is True
    page.close_browser()
    _pump(qapp, 20)


def test_model_close_removes_tab(browser_ui, qapp, browser_server):
    """模型调 browser_close：view 销毁 → 面板自动收标签。"""
    from src.browser import page
    from src.ui.file_panel import BROWSER_KEY
    ui = browser_ui
    ui.file_panel.open_browser(browser_server + "/form.html")
    assert _wait_page(qapp, lambda: "form.html" in page.get_browser().page.url().toString())
    page.close_browser()          # 等价于桥的 close op 对页面的处理
    _pump_deletes(qapp)
    assert BROWSER_KEY not in ui.file_panel.paths()
    assert ui.file_panel._browser_view is None


def test_localhost_link_opens_panel_but_public_does_not(browser_ui, qapp, browser_server):
    from src.browser import page
    from src.ui.file_panel import BROWSER_KEY
    ui = browser_ui
    ui._on_file_link_clicked("https://example.com/article")
    _pump(qapp)
    assert BROWSER_KEY not in ui.file_panel.paths(), "公网链接不进侧栏浏览器"
    ui._on_file_link_clicked(browser_server + "/form.html")
    _pump(qapp)
    assert BROWSER_KEY in ui.file_panel.paths()
    assert _wait_page(qapp, lambda: "form.html" in page.get_browser().page.url().toString())
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_open_browser_in_panel_trusts_origin(browser_ui, qapp):
    from src import session
    from src.ui.file_panel import BROWSER_KEY
    ui = browser_ui
    sess = session.get_active()
    ui._open_browser_in_panel("http://localhost:5173/app", sess)
    _pump(qapp)
    assert "http://localhost:5173" in sess.browser_origin_allowlist
    assert BROWSER_KEY in ui.file_panel.paths()
    ui.file_panel.close_all()
    _pump(qapp, 20)


def test_chatui_browser_subprocess_exit_zero_and_lazy():
    """硬要求 1+2：offscreen 子进程构造 ChatUI 后 WebEngine 模块数为 0；
    开浏览器标签→导航→关标签→销毁窗口→自然退出，退出码必须为 0（profile 常驻回归）。"""
    import os
    import subprocess
    import sys
    import tempfile
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = tempfile.mkdtemp(prefix="lingxi-browser-ui-")
    code = (
        "import functools, http.server, os, threading, time\n"
        "from PySide6.QtWidgets import QApplication\n"
        "from PySide6.QtCore import QEvent\n"
        "app = QApplication([])\n"
        "from src import models\n"
        "models._create_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError('no llm'))\n"
        "from src.ui import ChatUI\n"
        "ChatUI._show_current_model_config_warning = lambda self: None\n"
        "w = ChatUI()\n"
        "assert not [m for m in __import__('sys').modules if 'WebEngine' in m], 'ChatUI 构造 import 了 WebEngine'\n"
        "print('lazy-ok', flush=True)\n"
        "fixtures = os.path.join(os.getcwd(), 'scripts', 'browser_fixtures')\n"
        "H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=fixtures)\n"
        "srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)\n"
        "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "base = f'http://127.0.0.1:{srv.server_address[1]}'\n"
        "from src.browser import page\n"
        "from src.ui.file_panel import BROWSER_KEY\n"
        "w.file_panel.open_browser(base + '/form.html')\n"
        "deadline = time.time() + 20\n"
        "while time.time() < deadline and 'form.html' not in page.get_browser().page.url().toString():\n"
        "    app.processEvents(); time.sleep(0.01)\n"
        "assert 'form.html' in page.get_browser().page.url().toString(), '导航失败'\n"
        "print('loaded', flush=True)\n"
        "w.file_panel.close_file(BROWSER_KEY)\n"
        "for _ in range(100):\n"
        "    app.processEvents()\n"
        "assert not page.is_browser_open()\n"
        "w.close(); app.processEvents(); app.processEvents()\n"
        "w.deleteLater(); app.sendPostedEvents(None, QEvent.DeferredDelete); app.processEvents()\n"
        "srv.shutdown()\n"
        "print('done', flush=True)\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1",
               PYTHONIOENCODING="utf-8", LINGXI_DATA_DIR=data, LINGXI_LOG_DIR=data)
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    assert r.returncode == 0, (
        f"exit={r.returncode}\nstdout={r.stdout[-500:]!r}\nstderr={r.stderr[-1500:]!r}")
    assert b"lazy-ok" in r.stdout and b"loaded" in r.stdout and b"done" in r.stdout


# ── 复审 7/14：模型开页自动开标签且 view 可见；手动开浏览器入口 ──────────

@pytest.fixture(scope="module")
def fp_server():
    import http.server
    import os
    import threading
    fixtures = os.path.join(os.path.dirname(__file__), "browser_fixtures")

    class _H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=fixtures, **kw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _fp_run(app, fn, *a, **kw):
    import threading
    import time
    box = {}

    def w():
        try:
            box["r"] = fn(*a, **kw)
        except Exception as e:
            box["r"] = f"EXC {type(e).__name__}: {e}"

    t = threading.Thread(target=w, daemon=True)
    t.start()
    end = time.time() + 40
    while t.is_alive() and time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    t.join(timeout=2)
    return box.get("r", "TIMEOUT")


def test_model_open_opens_tab_and_view_visible(ui, qapp, fp_server, monkeypatch):
    """7：模型 browser_open 自动开面板标签，view 显示、页面不 hidden。"""
    from src import tools_browser as tb
    from src.browser import page as page_mod
    from src.browser import security as sec
    from src.ui.file_panel import BROWSER_KEY
    from src import session as _session
    sess = _session.current_session()
    sess.browser_origin_allowlist.add(sec.parse_origin(fp_server))
    _fp_run(qapp, tb.browser_open.func, fp_server + "/form.html")
    _pump(qapp, 20)
    assert BROWSER_KEY in ui.file_panel.paths(), "模型开页应自动开浏览器标签"
    view = page_mod._browser.view
    assert view.isVisible(), "view 应显示（不 hidden）"
    from src.browser.bridge import get_bridge
    r = _fp_run(qapp, get_bridge().call, "js", code="document.visibilityState")
    assert r.get("value") == "visible", r
    ui.file_panel.close_all()
    _pump(qapp, 10)


def test_manual_open_browser_button(ui, qapp, fp_server):
    """14：标签栏手动入口开空白页；关后再点复用上次地址。"""
    from src.browser import page as page_mod
    from src.ui.file_panel import BROWSER_KEY
    ui.file_panel.open_browser_btn.click()
    _pump(qapp, 10)
    assert BROWSER_KEY in ui.file_panel.paths()
    ui.file_panel.open_browser(fp_server + "/form.html")
    _pump(qapp, 10)
    ui.file_panel.close_file(BROWSER_KEY)
    _pump(qapp, 10)
    assert BROWSER_KEY not in ui.file_panel.paths()
    ui.file_panel.open_browser_btn.click()
    _pump(qapp, 20)
    assert BROWSER_KEY in ui.file_panel.paths()
    import time
    end = time.time() + 10
    while time.time() < end and "form.html" not in page_mod._browser.page.url().toString():
        qapp.processEvents()
        time.sleep(0.01)
    assert "form.html" in page_mod._browser.page.url().toString(), "手动入口应复用上次地址"
    ui.file_panel.close_all()
    _pump(qapp, 10)
