"""切会话不改侧栏顺序（用户实测：点哪个会话，哪个就跑到第一个）。

根因：切会话前会把当前会话存一遍，而每次保存都把 updated 刷成现在——侧栏按 updated 倒序，
看过一眼的会话就被顶到最前。现在只有对话内容真的变了才刷新 updated。
"""
import gc
import json
import os
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from PySide6.QtCore import QEvent

from src import memory, session as _session, state
from src.paths import memory_dir, memory_index


def _save(title, text, sid):
    s = _session.Session()
    s.current_session_id = sid
    s.current_session_title = title
    s.chat_history = [SystemMessage(content="sys"), HumanMessage(content=text),
                      AIMessage(content="好的")]
    memory.save_session(session=s)
    return s


def _index_updated():
    with open(memory_index(), encoding="utf-8") as f:
        return {e["id"]: e["updated"] for e in json.load(f)}


def _body_updated(sid):
    with open(os.path.join(memory_dir(), f"{sid}.json"), encoding="utf-8") as f:
        return json.load(f)["updated"]


def test_unchanged_save_keeps_updated(isolated_memory):
    s = _save("A", "hi", "20261117_000000_000001")
    before_index, before_body = _index_updated()[s.current_session_id], _body_updated(s.current_session_id)
    time.sleep(0.01)
    memory.save_session(session=s)                 # 内容没变：切会话时的那次保存
    assert _index_updated()[s.current_session_id] == before_index
    assert _body_updated(s.current_session_id) == before_body


def test_regenerated_system_prompt_is_not_new_activity(isolated_memory):
    """切到会话时系统提示词按当前代码重新生成：跨天（「今天是 X 月 X 日」变了）或提示词改过
    之后，它和存盘时不一样。这不是对话内容，不能刷新 updated（用户实测：隔天点开会话又被顶到
    最前、显示「刚刚」）。"""
    s = _save("A", "hi", "20261117_000000_000003")
    before_index, before_body = _index_updated()[s.current_session_id], _body_updated(s.current_session_id)
    time.sleep(0.01)
    s.chat_history[0] = SystemMessage(content="sys\n\n# 当前日期\n今天是 2026年11月18日。")
    memory.save_session(session=s)
    assert _index_updated()[s.current_session_id] == before_index
    assert _body_updated(s.current_session_id) == before_body
    with open(os.path.join(memory_dir(), f"{s.current_session_id}.json"), encoding="utf-8") as f:
        assert "11月18日" in json.load(f)["messages"][0]["content"], "新提示词照样存下来"


def test_new_message_refreshes_updated(isolated_memory):
    s = _save("A", "hi", "20261117_000000_000002")
    before = _index_updated()[s.current_session_id]
    time.sleep(0.01)
    s.chat_history.append(HumanMessage(content="再问一句"))
    memory.save_session(session=s)
    assert _index_updated()[s.current_session_id] > before


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    from src import tools
    from src.ui.chat_window import ChatUI
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    monkeypatch.setattr(state, "ui_ref", None)
    w = ChatUI()
    yield w
    if state.ui_ref is w:
        state.ui_ref = None
    w.close()
    qapp.processEvents()
    w.deleteLater()
    gc.collect()
    qapp.sendPostedEvents(None, QEvent.DeferredDelete)
    qapp.processEvents()


def test_clicking_sessions_does_not_reorder_sidebar(ui, qapp):
    ids = []
    for i, title in enumerate(["最早", "中间", "最新"]):
        ids.append(_save(title, f"问题 {i}", f"20261117_00000{i}_000000").current_session_id)
        time.sleep(0.01)
    order = [e["id"] for e in memory.list_sessions("__all__")]
    for sid in (ids[0], ids[1], ids[2], ids[0]):   # 来回点
        ui._load_session(sid)
        qapp.processEvents()
        time.sleep(0.01)
    ui._new_chat()                                  # 离开最后一个会话（又存一次）
    assert [e["id"] for e in memory.list_sessions("__all__")] == order
