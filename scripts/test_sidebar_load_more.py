"""侧栏「加载更多」（历史保留的界面半边）。

真实 ChatUI + 真实 index.json（隔离数据根）：每组初始显示 30 条，组尾「加载更多」
按步长追加；编码项目 / 无项目 / 知识库分组各自计数互不串用；普通刷新不重置用户
已展开的数量；点击第 31 条能加载，删除第 31 条真实生效。
"""
import gc
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton

from src import config, memory, paths, session as _session, state
from src.ui.widgets import HistoryRow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    # 模块收尾：各用例的窗口已在自己的夹具里销毁，这里再清一遍延迟删除与未投递
    # 事件，保证 QApplication 对象本身退出时没有任何悬着的 Qt 对象。
    gc.collect()
    app.processEvents()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def _destroy_ui_cleanly(ui, app):
    """完整清理序列（幂等）：窗口与延迟回收全部在 QApplication 存活期间处理完。

    顺序不能反：先在窗口**存活**时放完挂着的零秒回调（如
    _resize_input_container 的 QTimer.singleShot(0, …)）——它们必须打到活对象上；
    再 deleteLater + DeferredDelete 真正销毁。解释器退出时的清理顺序不定，
    留到那时做的代价是 Windows 上 access violation（退出码 -1073741819）。
    不用 os._exit / 吞异常 / 固定等待掩盖：指向已销毁对象的回调会当场报错。
    """
    import shiboken6
    if not shiboken6.isValid(ui):
        return                                       # 已被销毁（用例内清理过）→ 幂等返回
    if state.ui_ref is ui:
        state.ui_ref = None                          # 全局引用不再抓着窗口
    ui.close()                                       # 真实 closeEvent（释放挂着的确认请求等）
    app.processEvents()
    app.processEvents()
    ui.deleteLater()                                 # 调度销毁整棵控件树
    gc.collect()                                     # 拆掉 Python 侧 wrapper 循环引用
    app.sendPostedEvents(None, QEvent.DeferredDelete)  # 立即执行销毁
    app.processEvents()


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    """完整 ChatUI（真实侧栏），模型 / MCP / 网络全部离线。"""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("Model/network access is prohibited in sidebar tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import models, mcp_client, tools

    def _no_llm(*args, **kwargs):
        raise AssertionError("sidebar tests must not create or call a model")

    monkeypatch.setattr(models, "_create_llm", _no_llm)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    from src.ui.chat_window import ChatUI
    from src import agent
    monkeypatch.setattr(agent, "_create_llm", _no_llm)
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(state, "llm", state.llm)
    monkeypatch.setattr(state, "llm_with_tools", state.llm_with_tools)
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)
    monkeypatch.setattr(state, "ui_ref", None)
    ui = ChatUI()
    yield ui
    _destroy_ui_cleanly(ui, qapp)


def _register_project(tmp_path, name):
    """注册一个真实存在的项目目录，返回**存储用的归一化路径**（会话归属要用它）。"""
    from src import projects as _projects
    path = str(tmp_path / name)
    os.makedirs(path, exist_ok=True)
    assert _projects.add_project(path)
    normalized = os.path.normpath(path).replace("\\", "/")
    assert normalized in [p["path"] for p in _projects.list_projects()]
    return normalized


def _make_sessions(n, *, project=None, kind="code", tag="sess"):
    """真实落盘 n 个会话（正文 + 索引），id 确定性命名，返回 id 列表（旧→新）。"""
    ids = []
    for i in range(n):
        s = _session.Session()
        s.current_session_id = f"{tag}-{i:03d}"
        if project is not None:
            s.project = project
        if kind == "rag":
            s.session_kind = "rag"
            s.rag_kb_dir = "C:/kb"
        s.chat_history = [SystemMessage(content="sys"),
                          HumanMessage(content=f"{tag} {i:03d} 的对话正文")]
        _session.register(s)
        outcome = memory.save_session_report(session=s)
        assert outcome.body_written and outcome.index_written, outcome.error
        ids.append(s.current_session_id)
    return ids


def _layout_widgets(ui):
    """侧栏列表布局里的控件序列（顺序即显示顺序）。

    末尾的 stretch 是 QSpacerItem（.widget() 为 None），在这里自然被跳过；
    不能按"去掉最后一个"的方式处理，那会把末组真实的「加载更多」按钮一起丢掉。
    """
    lay = ui.history_layout
    out = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            out.append(w)
    return out


def _groups(ui):
    """按分组标题切分布局控件：[{"title", "rows", "more"}]，顺序即渲染顺序。"""
    groups = []
    current = None
    for w in _layout_widgets(ui):
        if isinstance(w, QPushButton) and w.objectName().startswith("projectHeader"):
            current = {"title": w.text(), "rows": [], "more": []}
            groups.append(current)
        elif isinstance(w, HistoryRow):
            if current is not None:
                current["rows"].append(w)
        elif isinstance(w, QPushButton) and w.objectName() == "loadMoreBtn":
            if current is not None:
                current["more"].append(w)
    return groups


def _group(ui, title_part):
    for g in _groups(ui):
        if title_part in g["title"]:
            return g
    return None


def _row_title_button(row):
    for btn in row.findChildren(QPushButton):
        if btn.property("class") in ("historyItem", "historyItemActive"):
            return btn
    return None


def _row_delete_button(row):
    for btn in row.findChildren(QPushButton):
        if btn.objectName() == "historyDeleteBtn":
            return btn
    return None


def test_group_shows_30_then_load_more(ui, isolated_memory, tmp_path):
    """每组初始 30 条 + 「加载更多」；点两次后全部可见、按钮消失。"""
    registered = _register_project(tmp_path, "proj-a")
    _make_sessions(75, project=registered, tag="pa")
    ui._refresh_session_list()

    g = _group(ui, "proj-a")
    assert g is not None and len(g["rows"]) == 30
    assert len(g["more"]) == 1
    assert "45" in g["more"][0].text()

    g["more"][0].click()                       # 真实按钮点击 → +30
    g = _group(ui, "proj-a")
    assert len(g["rows"]) == 60
    assert "15" in g["more"][0].text()

    g["more"][0].click()
    g = _group(ui, "proj-a")
    assert len(g["rows"]) == 75
    assert g["more"] == [], "全部可见后不应再有加载更多按钮"
    # 旧会话找得到：第 31 条（pa-030）与最后一条（pa-074）都在已显示的行里
    titles = [b.text() for row in g["rows"]
              for b in row.findChildren(QPushButton)
              if b.property("class") in ("historyItem", "historyItemActive")]
    assert any("030" in t for t in titles) and any("074" in t for t in titles)


def test_refresh_does_not_reset_expanded_count(ui, isolated_memory, tmp_path):
    """普通列表刷新（重渲染）不把用户已展开的数量重置回 30。"""
    registered = _register_project(tmp_path, "proj-r")
    _make_sessions(50, project=registered, tag="pr")
    ui._refresh_session_list()
    g = _group(ui, "proj-r")
    g["more"][0].click()
    assert len(_group(ui, "proj-r")["rows"]) == 50

    ui._refresh_session_list()                 # 保存 / 删除 / 切会话都会触发刷新
    ui._refresh_session_list()
    assert len(_group(ui, "proj-r")["rows"]) == 50


def test_groups_expand_independently(ui, isolated_memory, tmp_path):
    """编码项目 / 无项目分组各自计数，展开其一不影响其他。"""
    px = _register_project(tmp_path, "proj-x")
    py = _register_project(tmp_path, "proj-y")
    _make_sessions(40, project=px, tag="px")
    _make_sessions(35, project=py, tag="py")
    _make_sessions(32, project=None, tag="np")
    ui._refresh_session_list()

    gx, gy, gn = _group(ui, "proj-x"), _group(ui, "proj-y"), _group(ui, "历史会话")
    assert (len(gx["rows"]), len(gy["rows"]), len(gn["rows"])) == (30, 30, 30)
    assert all(len(g["more"]) == 1 for g in (gx, gy, gn))

    gx["more"][0].click()
    gx, gy, gn = _group(ui, "proj-x"), _group(ui, "proj-y"), _group(ui, "历史会话")
    assert len(gx["rows"]) == 40 and gx["more"] == []
    assert (len(gy["rows"]), len(gn["rows"])) == (30, 30)
    assert len(gy["more"]) == 1 and len(gn["more"]) == 1


def test_rag_group_load_more_separate_from_no_project(ui, isolated_memory, tmp_path):
    """知识库分组与编码「历史会话」都以 project=None 渲染，展示数量必须各自独立。"""
    _make_sessions(33, project=None, kind="code", tag="np")
    _make_sessions(36, project=None, kind="rag", tag="rg")

    # 知识库工作区
    rag_sess = _session.get_active()
    rag_sess.session_kind = "rag"
    rag_sess.rag_kb_dir = "C:/kb"
    ui._refresh_session_list()
    g = _group(ui, "知识库对话")
    assert len(g["rows"]) == 30 and len(g["more"]) == 1
    g["more"][0].click()
    assert len(_group(ui, "知识库对话")["rows"]) == 36

    # 切回编码工作区：无项目分组的展示数量仍是初始 30（两者互不串用）；
    # 编码无项目会话 33 条，只提示余下的 3 条（知识库的 36 条不算进来）
    rag_sess.session_kind = "code"
    ui._refresh_session_list()
    gn = _group(ui, "历史会话")
    assert len(gn["rows"]) == 30 and len(gn["more"]) == 1
    assert "3" in gn["more"][0].text()


def test_click_row_31_loads_that_session(ui, isolated_memory, tmp_path):
    """真实点击第 31 条会话行：会话被加载（当前会话切换、正文恢复）。"""
    from src import agent
    registered = _register_project(tmp_path, "proj-l")
    ids = _make_sessions(40, project=registered, tag="pl")
    ui._refresh_session_list()

    g = _group(ui, "proj-l")
    assert len(g["rows"]) == 30
    g["more"][0].click()
    rows = _group(ui, "proj-l")["rows"]
    assert len(rows) == 40

    # 展示按更新时间倒序（新→旧）：界面上的第 31 行是第 31 新的会话
    target_sid = ids[-1 - 30]
    btn = _row_title_button(rows[30])
    assert btn is not None
    btn.click()                                # 真实点击 → _load_session

    assert agent.current_session_id == target_sid
    active = _session.get_active()
    assert active.current_session_id == target_sid
    assert any(getattr(m, "content", "") == "pl 009 的对话正文" for m in active.chat_history)


def test_delete_row_31_really_deletes(ui, isolated_memory, tmp_path):
    """真实点击第 31 条的删除按钮：索引条目与正文一并删除，后续保存不复活。"""
    from src import agent
    registered = _register_project(tmp_path, "proj-d")
    ids = _make_sessions(40, project=registered, tag="pd")
    ui._refresh_session_list()
    _group(ui, "proj-d")["more"][0].click()
    rows = _group(ui, "proj-d")["rows"]
    assert len(rows) == 40

    victim = ids[-1 - 30]                      # 界面第 31 行（展示为新→旧）
    body = os.path.join(paths.memory_dir(), f"{victim}.json")
    assert os.path.exists(body)
    del_btn = _row_delete_button(rows[30])
    assert del_btn is not None
    del_btn.click()                            # 真实点击 → _delete_session

    assert not os.path.exists(body)
    assert victim not in {e["id"] for e in memory.list_sessions("__all__")}
    g = _group(ui, "proj-d")
    assert len(g["rows"]) == 39                # 展开数量保持 40，剩余不足则按钮消失
    assert g["more"] == []

    # 后续保存别的会话（索引重写）不复活被删会话
    _make_sessions(1, project=registered, tag="pd-new")
    assert victim not in {e["id"] for e in memory.list_sessions("__all__")}
    assert agent.current_session_id != victim


def test_equal_timestamps_have_stable_order(ui, isolated_memory, tmp_path):
    """updated 相同的会话按 id 倒序稳定展示，刷新不换位。"""
    registered = _register_project(tmp_path, "proj-t")
    _make_sessions(10, project=registered, tag="st")
    # 手工把同组全部条目的 updated 改成同一时刻
    with memory._LOCK:
        with open(memory.memory_index(), "r", encoding="utf-8") as f:
            index = json.load(f)
        for item in index:
            item["updated"] = "2026-01-01T00:00:00"
        memory._atomic_write_json(memory.memory_index(), index)
    ui._refresh_session_list()

    g = _group(ui, "proj-t")
    assert len(g["rows"]) == 10
    titles_first = [_row_title_button(r).text() for r in g["rows"]]
    ui._refresh_session_list()
    titles_again = [_row_title_button(r).text() for r in _group(ui, "proj-t")["rows"]]
    assert titles_first == titles_again
    # 稳定顺序 = id 倒序（同刻创建越晚的越靠前）。标题取首条用户消息（"st 00N 的对话正文"）
    expect_prefixes = [f"st {i:03d}" for i in range(9, -1, -1)]
    assert [t[:6] for t in titles_first] == expect_prefixes


def test_load_session_cleanup_then_event_loop_has_no_late_callbacks(
        ui, qapp, monkeypatch, isolated_memory, tmp_path):
    """加载会话（安排最长 700ms 的滚动回调）→ 完整清理 → 继续事件循环。

    窗口销毁后，尚未到期的定时回调必须随窗口销毁被取消（chat_window 的
    singleShot 都带 receiver 上下文），不得打向已删除的控件。延迟回调抛出的
    异常经 sys.excepthook 记录并逐条断言为空，而不是只看进程退出码。
    """
    from src import agent
    registered = _register_project(tmp_path, "proj-c")
    ids = _make_sessions(40, project=registered, tag="pc")
    ui._refresh_session_list()
    _group(ui, "proj-c")["more"][0].click()
    rows = _group(ui, "proj-c")["rows"]
    _row_title_button(rows[30]).click()            # 真实加载 → 安排 0..700ms 滚动回调
    target_sid = ids[-1 - 30]
    assert agent.current_session_id == target_sid

    hits = []

    def _hook(tp, val, tb):
        hits.append(f"{tp.__name__}: {val}")

    monkeypatch.setattr(sys, "excepthook", _hook)

    _destroy_ui_cleanly(ui, qapp)                  # 与夹具完全相同的完整清理

    # 清理后继续事件循环，覆盖最长 700ms 延迟回调的到期窗口
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert hits == [], f"销毁后的窗口仍收到 {len(hits)} 次定时回调: {hits[:5]}"
