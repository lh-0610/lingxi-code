"""左侧栏重设计（设计稿「左侧栏：编码与知识库两个工作区」）的行为。

真实 ChatUI + 隔离数据根（夹具复用 test_sidebar_load_more）：会话行平时右侧只放一个
状态（转圈 / 待确认 / 绿点 / 时间），悬停才出「⋯」；删除收进「⋯」菜单、生成中置灰；
分组标题「当前」/ 收起时的数量；标题按宽度省略；底栏后台任务数量；知识库空列表说明。
"""
# 夹具从 test_sidebar_load_more 导入，用例参数与导入同名是 pytest 夹具的正常写法
# ruff: noqa: F811
import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from src import session as _session
from src.ui.sidebar import _relative_time
from src.ui.widgets import ElidedButton, GroupHeader
from test_sidebar_load_more import (_group, _groups, _make_sessions,  # noqa: F401
                                    _register_project, qapp, ui)


def _settle():
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def _show(ui):
    ui.resize(1180, 800)
    ui.show()
    ui._refresh_session_list()
    _settle()


def _right_side(row):
    """会话行右侧控件（标题按钮之外）中当前可见的那些。"""
    from PySide6.QtWidgets import QWidget
    return [w for w in row.children()
            if isinstance(w, QWidget) and not isinstance(w, ElidedButton) and w.isVisibleTo(row)]


def _hover(widget, inside=True):
    if inside:
        ev = QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5))
        QApplication.sendEvent(widget, ev)
    else:
        QApplication.sendEvent(widget, QEvent(QEvent.Leave))


class TestRelativeTime:
    NOW = datetime(2026, 11, 18, 15, 30)

    def test_buckets(self):
        f = lambda iso: _relative_time(iso, now=self.NOW)  # noqa: E731
        assert f("2026-11-18T15:29:40") == "刚刚"
        assert f("2026-11-18T15:18:00") == "12 分钟前"
        assert f("2026-11-18T09:05:00") == "09:05"
        assert f("2026-11-17T23:59:00") == "昨天"
        assert f("2026-11-02T10:00:00") == "11/2"
        assert f("2025-12-31T10:00:00") == "2025/12/31"
        assert f("") == "" and f("乱码") == ""


class TestSessionRow:
    def test_idle_row_shows_time_and_more_only_on_hover(self, ui, tmp_path):
        _make_sessions(1, tag="idle")
        _show(ui)
        row = _group(ui, "无项目")["rows"][0]
        more = next(b for b in row.findChildren(QPushButton) if b.objectName() == "sbMoreBtn")
        time_lbl = next(w for w in row.findChildren(QLabel) if w.objectName() == "sbTime")
        assert time_lbl.isVisibleTo(row) and not more.isVisibleTo(row)
        assert not any(b.objectName() == "historyDeleteBtn" for b in row.findChildren(QPushButton)), \
            "行上不再挂删除按钮"
        _hover(row)
        assert more.isVisibleTo(row) and not time_lbl.isVisibleTo(row), "悬停：时间换成「⋯」"
        _hover(row, inside=False)
        assert time_lbl.isVisibleTo(row) and not more.isVisibleTo(row)

    def test_status_rows_show_one_status_and_no_time(self, ui, tmp_path):
        ids = _make_sessions(3, tag="st")
        gen, pend, done = (_session.get(i) for i in ids)
        gen.is_generating = True
        pend.pending_confirm = {"x": 1}
        done.needs_redraw = True
        _show(ui)
        rows = {r.property("session_id"): r for r in _group(ui, "无项目")["rows"]}
        for sid, expect in ((ids[0], "GeneratingBadge"), (ids[1], "待确认"), (ids[2], "sbDoneDot")):
            row = rows[sid]
            assert not any(w.objectName() == "sbTime" for w in row.findChildren(QLabel)), \
                "有状态时不再显示时间"
            right = _right_side(row)
            names = [w.__class__.__name__ for w in right] + [w.objectName() for w in right] \
                + [w.text() for w in right if isinstance(w, QLabel)]
            assert expect in names, (sid, names)
            assert len(right) == 1, f"右侧只放一个状态：{names}"

    def test_closing_the_menu_elsewhere_clears_the_stuck_hover(self, ui, tmp_path, monkeypatch):
        """点「⋯」弹菜单、再点对话区关掉：菜单抢着鼠标，行和「⋯」收不到 Leave，Qt 还当
        鼠标在上面——悬停底色卡住、「⋯」的底框留在原地（用户实测）。关菜单时要纠正。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QCursor
        _make_sessions(1, tag="mh")
        _show(ui)
        row = _group(ui, "无项目")["rows"][0]
        more = next(b for b in row.findChildren(QPushButton) if b.objectName() == "sbMoreBtn")
        time_lbl = next(w for w in row.findChildren(QLabel) if w.objectName() == "sbTime")
        _hover(row)
        row.set_menu_open(True)
        for w in (row, more):
            w.setAttribute(Qt.WA_UnderMouse, True)     # 菜单弹出前鼠标就在行和「⋯」上
        far = row.mapToGlobal(QPoint(-500, -500))       # 关菜单时鼠标在对话区
        monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: far))
        row.set_menu_open(False)
        assert not row.testAttribute(Qt.WA_UnderMouse) and not more.testAttribute(Qt.WA_UnderMouse)
        assert not more.isVisibleTo(row) and time_lbl.isVisibleTo(row)

    def test_menu_has_rename_and_delete_greyed_while_generating(self, ui, tmp_path):
        ids = _make_sessions(2, tag="mn")
        _session.get(ids[0]).is_generating = True
        texts = [a.text() for a in ui._session_row_menu(ids[1], "t").actions() if a.text()]
        assert texts == ["重命名", "删除会话"]
        delete = [a for a in ui._session_row_menu(ids[0], "t", True).actions()
                  if a.text() == "删除会话"][0]
        assert not delete.isEnabled()

    def test_long_title_is_elided_by_width_not_by_char_count(self, ui, tmp_path):
        from src import memory
        s = _session.Session()
        s.current_session_id = "long-001"
        title = "在后台启动 python http.server 8765 并用浏览器打开验证首页能不能正常访问"
        s.current_session_title = title
        from langchain_core.messages import HumanMessage, SystemMessage
        s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="x")]
        _session.register(s)
        memory.save_session_report(session=s)
        _show(ui)
        row = _group(ui, "无项目")["rows"][0]
        btn = next(b for b in row.findChildren(ElidedButton))
        assert btn.full_text() == title and btn.toolTip() == title
        assert btn.text().endswith("…") and btn.text() != title
        assert "..." not in btn.text(), "不再按 12 个字硬截"
        assert btn.fontMetrics().horizontalAdvance(btn.text()) <= btn.width()


class TestGroupHeader:
    def test_current_project_chip_and_collapsed_count(self, ui, tmp_path):
        from src import projects as _projects
        from src import state
        cur = _register_project(tmp_path, "proj-cur")
        other = _register_project(tmp_path, "proj-other")
        _make_sessions(2, project=cur, tag="c")
        _make_sessions(3, project=other, tag="o")
        _projects.set_current(cur)
        state.current_project = cur
        ui._collapsed_projects = {other}
        _show(ui)
        headers = {h.full_text(): h for h in ui.history_widget.findChildren(GroupHeader)}
        chips = [w.text() for w in headers["proj-cur"].findChildren(QLabel) if w.objectName() == "sbChip"]
        assert chips == ["当前"]
        counts = [w.text() for w in headers["proj-other"].findChildren(QLabel)
                  if w.objectName() == "sbCount"]
        assert counts == ["3"], "收起的分组显示会话数"
        assert "无项目" in headers and "历史会话" not in headers

    def test_registered_project_more_appears_on_hover(self, ui, tmp_path):
        p = _register_project(tmp_path, "proj-m")
        _make_sessions(1, project=p, tag="m")
        _show(ui)
        h = next(h for h in ui.history_widget.findChildren(GroupHeader) if h.full_text() == "proj-m")
        more = next(b for b in h.findChildren(QPushButton) if b.objectName() == "sbMoreBtn")
        assert not more.isVisibleTo(h)
        _hover(h)
        assert more.isVisibleTo(h)


class TestChrome:
    def test_no_subtitle_no_history_label_new_chat_text(self, ui):
        _show(ui)
        labels = [w.text() for w in ui.sidebar.findChildren(QLabel)]
        assert "local & cloud" not in labels and "历史记录" not in labels
        assert ui.new_chat_btn.text() == "新对话"

    def test_background_badge_counts_running_commands(self, ui, monkeypatch):
        from src import background
        monkeypatch.setattr(background, "list_snapshots",
                            lambda **kw: [{"running": True}, {"running": True}, {"running": False}])
        ui._refresh_bg_badge()
        assert ui._bg_badge.text() == "2" and ui._bg_badge.isVisibleTo(ui.background_btn)
        monkeypatch.setattr(background, "list_snapshots", lambda **kw: [{"running": False}])
        ui._refresh_bg_badge()
        assert not ui._bg_badge.isVisibleTo(ui.background_btn)

    def test_empty_kb_list_explains_where_answers_come_from(self, ui, monkeypatch, tmp_path):
        from src import config
        kb = tmp_path / "我的资料"
        kb.mkdir()
        monkeypatch.setattr(config, "RAG_KB_DIR", str(kb))
        rag = _session.get_active()
        rag.session_kind = "rag"
        _show(ui)
        texts = [w.text() for w in ui.history_widget.findChildren(QLabel)]
        assert "还没有知识库对话" in texts
        assert any("我的资料" in t and "出处" in t for t in texts)


def test_history_redraw_runs_without_the_global_menu_filter(ui, monkeypatch):
    """重画历史（切会话 / 切工作区都走它）期间全局右键过滤器是摘掉的，画完装回。"""
    from src.ui import context_menus as cm
    depth = []
    monkeypatch.setattr(type(ui), "_replay_history",
                        lambda self, hist: depth.append(cm._suspend_depth) or True)
    ui._redraw_chat()
    assert depth == [1]
    assert cm._suspend_depth == 0
