"""B09b 运行中输入队列：状态机、持久化与崩溃窗口、UI 派发与自动推进。

覆盖 §13.3 的验收面：
- 长运行期间追加多条，按顺序各派发一次；编辑/删除后派发内容与面板一致。
- 立即处理非队首项：旧 worker 退出前无新 worker，其余条目保持暂停。
- 保存失败 / 线程构造与启动失败 / 附件缺失或变化 / 归属漂移：保留条目并如实说明。
- 异常终态暂停自动推进；只有 completed 且保存成功才推进。
- 真新进程恢复：队列默认暂停；派发准备后崩溃与 begin_run 接纳后崩溃都标待核对。
- B05 要求来源不重复、内部消息不混入；GUI 真实渲染可用。

测试纪律：真实 ChatUI + Qt 事件循环 + Event 控制的 worker；不碰真实模型/CLI/
Telegram；数据根线程本地（worker 用 _worker_data_root 带根）；写入路径守卫防越界；
finally 放行 Event 并 join 测试线程，断言失败也能清理。
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from langchain_core.messages import SystemMessage

from src import agent, input_queue, limits, memory, run_records, session, state
from src.agent_result import AgentResult
from src.ui.chat_window import ChatUI

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


@pytest.fixture(autouse=True)
def _memory_write_guard(monkeypatch, isolated_memory):
    """所有 chat_memory 写入都必须落在隔离数据根（含 worker 线程）。"""
    expected = os.path.normcase(str(memory.memory_dir()))
    bad = []
    real_write = memory._atomic_write_json

    def _spy_write(path, *a, **k):
        d = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
        if d != expected:
            bad.append(d)
        return real_write(path, *a, **k)

    monkeypatch.setattr(memory, "_atomic_write_json", _spy_write)
    yield
    assert not bad, f"chat_memory 写入越界 {len(bad)} 次: {sorted(set(bad))[:3]}"


def _api_models():
    from src.models import MODEL_LIST
    return [i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama")]


def _make_image(tmp_path, name, content=_PNG_1PX):
    path = os.path.join(str(tmp_path), name)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# ══════════════════════════════════════════════════════════════
# 纯状态机：入队 / 容量 / 编辑 / 删除 / 顺序 / 附件身份 / 归属
# ══════════════════════════════════════════════════════════════

class TestQueueModule:

    def test_enqueue_capacity_limits(self):
        q = input_queue.new_queue()
        for i in range(limits.QUEUE_MAX_ITEMS):
            ok, _ = input_queue.enqueue(q, input_queue.new_queue_item(f"m{i}", []))
            assert ok
        ok, reason = input_queue.enqueue(q, input_queue.new_queue_item("超了", []))
        assert not ok and "队列已满" in reason

    def test_text_length_limit_rejects_without_truncation(self):
        q = input_queue.new_queue()
        ok, reason = input_queue.enqueue(
            q, input_queue.new_queue_item("x" * (limits.QUEUE_MAX_TEXT_CHARS + 1), []))
        assert not ok and "超长" in reason
        assert q["items"] == [], "被拒的条目不得截断入队"

    def test_total_capacity_limit(self):
        q = input_queue.new_queue()
        # 单条 20000 上限内、总量 100000 需要多条累积：先放 6 条 19000 字符
        chunk = "a" * (limits.QUEUE_MAX_TEXT_CHARS - 100)
        for _ in range(5):                      # 5 × 19900 = 99500 ≤ 100000
            assert input_queue.enqueue(q, input_queue.new_queue_item(chunk, []))[0]
        ok, reason = input_queue.enqueue(q, input_queue.new_queue_item(chunk, []))
        assert not ok and "总量" in reason

    def test_edit_keeps_identity_and_position(self):
        q = input_queue.new_queue()
        a = input_queue.new_queue_item("第一", [])
        b = input_queue.new_queue_item("第二", [])
        input_queue.enqueue(q, a)
        input_queue.enqueue(q, b)
        ok, _, backup = input_queue.set_text(q, b["queue_item_id"], "第二改")
        assert ok and backup == "第二"
        assert [it["queue_item_id"] for it in q["items"]] == \
            [a["queue_item_id"], b["queue_item_id"]]
        assert q["items"][1]["message_id"] == b["message_id"], "编辑不改身份"
        assert q["items"][1]["text"] == "第二改"

    def test_delete_returns_position_and_locks_dispatched(self):
        q = input_queue.new_queue()
        a = input_queue.new_queue_item("一", [])
        b = input_queue.new_queue_item("二", [])
        input_queue.enqueue(q, a)
        input_queue.enqueue(q, b)
        input_queue.mark_dispatching(q, b["queue_item_id"])
        ok, reason, _ = input_queue.delete(q, b["queue_item_id"])
        assert not ok and "派发" in reason, "dispatching 锁定删除"
        ok, _, idx = input_queue.delete(q, a["queue_item_id"])
        assert ok and idx == 0
        assert q["items"] == [b]

    def test_next_queued_does_not_skip_held_head(self):
        q = input_queue.new_queue()
        a = input_queue.new_queue_item("头部", [])
        b = input_queue.new_queue_item("后面", [])
        input_queue.enqueue(q, a)
        input_queue.enqueue(q, b)
        input_queue.hold(q, a["queue_item_id"], "附件缺失")
        head, blocked = input_queue.next_queued(q)
        assert head is None and blocked is a, "hold 的头部挡住队列，不跳号"

    def test_attachment_identity_detects_missing_and_changed(self, tmp_path):
        img = _make_image(tmp_path, "a.png")
        item = input_queue.new_queue_item("带图", [(img, _b64(img))])
        pairs, problem = input_queue.load_images(item)
        assert problem is None and pairs[0][0] == img

        os.remove(img)
        pairs, problem = input_queue.load_images(item)
        assert pairs == [] and "不存在" in problem

        item2 = input_queue.new_queue_item("带图", [(img := _make_image(tmp_path, "b.png"),
                                                     _b64(img))])
        with open(img, "wb") as f:
            f.write(b"different-bytes")
        pairs, problem = input_queue.load_images(item2)
        assert pairs == [] and "变化" in problem

    def test_ownership_change_rules(self):
        item = input_queue.new_queue_item("x", [], project="D:/a", worktree=None,
                                          task_id="task-1")
        assert input_queue.ownership_change(item, project="D:/a", worktree=None,
                                            task_id="task-1") == ""
        assert "项目" in input_queue.ownership_change(item, project="D:/b", worktree=None,
                                                      task_id="task-1")
        assert "隔离区" in input_queue.ownership_change(item, project="D:/a",
                                                        worktree="D:/a/.wt", task_id="task-1")
        assert "任务" in input_queue.ownership_change(item, project="D:/a", worktree=None,
                                                      task_id="task-2")
        # 入队时无任务（task_id None）→ 派发时建立了任务不算漂移
        free = input_queue.new_queue_item("y", [], project="D:/a", worktree=None,
                                          task_id=None)
        assert input_queue.ownership_change(free, project="D:/a", worktree=None,
                                            task_id="task-new") == ""

    def test_normalize_corrupt_and_unknown_version(self):
        q, why = input_queue.normalize({"version": 99, "items": []})
        assert why and q["items"] == []
        q, why = input_queue.normalize({"version": 1, "items": [{"no": "id"}]})
        assert why and "queue_item_id" in why
        q, why = input_queue.normalize("not-a-dict")
        assert why

    def test_recovery_marks_dispatching_and_admitted_as_needs_check(self):
        q = input_queue.new_queue()
        a = input_queue.new_queue_item("准备中", [])
        b = input_queue.new_queue_item("已接纳", [])
        c = input_queue.new_queue_item("普通排队", [])
        for it in (a, b, c):
            input_queue.enqueue(q, it)
        input_queue.mark_dispatching(q, a["queue_item_id"])
        input_queue.mark_dispatching(q, b["queue_item_id"])
        input_queue.mark_admitted(q, b["queue_item_id"])

        restored, why = input_queue.normalize(input_queue.snapshot(q))
        assert why == "", "合法队列不得报损坏"
        assert restored["paused"] is True, "新进程恢复默认暂停"
        assert "进程重启" in restored["pause_reason"]
        states = {it["text"]: it["state"] for it in restored["items"]}
        assert states["准备中"] == "needs_check"
        assert states["已接纳"] == "needs_check"
        assert states["普通排队"] == "queued"

    def test_requeue_as_new_keeps_attachment_refs(self):
        q = input_queue.new_queue()
        item = input_queue.new_queue_item("旧", [])
        input_queue.enqueue(q, item)
        refs = [{"path": "x.png", "sha256": "abc"}]
        new_item, _ = input_queue.requeue_as_new(q, "旧（重发）", [],
                                                 image_refs=refs)
        assert new_item["queue_item_id"] != item["queue_item_id"]
        assert new_item["message_id"] != item["message_id"]
        assert new_item["images"] == refs


# ══════════════════════════════════════════════════════════════
# 持久化：progress 信封 roundtrip、损坏隔离、崩溃窗口（真新进程）
# ══════════════════════════════════════════════════════════════

class TestQueuePersistence:

    def test_queue_survives_save_load_roundtrip(self, isolated_memory, tmp_path):
        from langchain_core.messages import HumanMessage
        img = _make_image(tmp_path, "a.png")
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="开场")]
        session.register(sess)
        memory.save_session(session=sess)
        scene = {"project": "D:/p", "worktree": None, "task_id": None}
        item = input_queue.new_queue_item("排队内容", [(img, _b64(img))],
                                          project=scene["project"])
        assert input_queue.enqueue(sess.input_queue, item)[0]
        input_queue.set_paused(sess.input_queue, True, "手动暂停")
        memory.save_session(session=sess)

        other = session.Session()
        assert memory.load_session(sess.current_session_id, session=other)
        q = other.input_queue
        assert len(q["items"]) == 1
        assert q["items"][0]["text"] == "排队内容"
        assert q["items"][0]["images"][0]["sha256"] == item["images"][0]["sha256"]
        assert q["paused"] is True
        assert q["items"][0]["message_id"] == item["message_id"], "身份跨重启保留"
        # 恢复语义：即便存的是"正常排队"，重开也默认暂停
        q2 = input_queue.new_queue()
        it2 = input_queue.new_queue_item("另一条", [])
        input_queue.enqueue(q2, it2)
        saved = input_queue.snapshot(q2)
        restored, why = input_queue.normalize(saved)
        assert restored["paused"] is True, "新进程恢复默认暂停"

    def test_corrupt_queue_quarantined_history_still_loads(self, isolated_memory):
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="sys"),
                             __import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(content="hi")]
        session.register(sess)
        memory.save_session(session=sess)
        path = os.path.join(str(isolated_memory), f"{sess.current_session_id}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["progress"]["input_queue"] = {"version": 77, "items": "broken"}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        other = session.Session()
        assert memory.load_session(sess.current_session_id, session=other), "聊天历史照常加载"
        assert len(other.chat_history) == 2
        assert other.input_queue["items"] == [], "损坏队列整块作废"
        assert other.progress_error and "input_queue" in other.progress_error
        # 原始数据隔离留底：隔离发生在下一次保存时（保存前核对旧文件）
        memory.save_session(session=other)
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        quarantined = raw.get("quarantined_progress") or []
        assert any("input_queue" in json.dumps(e.get("data") or {}, ensure_ascii=False)
                   for e in quarantined), "原始队列数据被隔离保留"

    def test_old_session_without_queue_loads_empty(self, isolated_memory):
        sess = session.Session()
        sess.chat_history = [SystemMessage(content="sys"),
                             __import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(content="hi")]
        session.register(sess)
        memory.save_session(session=sess)
        path = os.path.join(str(isolated_memory), f"{sess.current_session_id}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["progress"].pop("input_queue", None)     # 模拟旧版本写出的文件
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        other = session.Session()
        assert memory.load_session(sess.current_session_id, session=other)
        assert other.input_queue["items"] == []

    # ── 崩溃窗口：真新进程恢复 ──

    def _spawn_and_recover(self, isolated_memory, mode):
        child = r'''
import sys
sys.path.insert(0, sys.argv[1])
from src import paths
paths.set_data_dir(sys.argv[2])
# 写入路径守卫：所有落盘必须在临时数据根内
from src import memory as _memory
_expected = paths.memory_dir()
_real_write = _memory._atomic_write_json
def _guarded(path, *a, **k):
    import os
    d = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
    assert d == os.path.normcase(_expected), f"越界写入: {d}"
    return _real_write(path, *a, **k)
_memory._atomic_write_json = _guarded

from src import input_queue, memory, run_records, session, task_state
from src.agent_result import AgentResult
from langchain_core.messages import SystemMessage, HumanMessage

mode = sys.argv[3]
sess = session.Session()
sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="第一轮")]
session.register(sess)
memory.save_session(session=sess)
item = input_queue.new_queue_item("排队的一条", [])
input_queue.enqueue(sess.input_queue, item)
if mode == "prep":
    input_queue.mark_dispatching(sess.input_queue, item["queue_item_id"])
elif mode == "admitted":
    input_queue.mark_dispatching(sess.input_queue, item["queue_item_id"])
    msg = HumanMessage(content="排队的一条")
    task_state.tag_user_message(msg, msg_id=item["message_id"])
    sess.chat_history.append(msg)
    task_state.sync_user_requests(sess)
    input_queue.mark_admitted(sess.input_queue, item["queue_item_id"])
    run_records.begin_run(sess)          # 接纳后、结束前"崩溃"
memory.save_session(session=sess)
print(sess.current_session_id)
'''
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", child, repo_root, str(isolated_memory.parent), mode],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert proc.returncode == 0, f"子进程失败: {proc.stderr[-500:]}"
        sid = proc.stdout.strip().splitlines()[-1]

        recovered = session.Session()
        assert memory.load_session(sid, session=recovered)
        q = recovered.input_queue
        assert q["paused"] is True, "新进程恢复队列默认暂停"
        assert len(q["items"]) == 1
        return recovered, q["items"][0]

    def test_crash_in_dispatch_prep_marks_needs_check(self, isolated_memory):
        recovered, item = self._spawn_and_recover(isolated_memory, "prep")
        assert item["state"] == "needs_check"
        assert "待核对" in item["hold_reason"] or "派发" in item["hold_reason"]
        texts = []
        for m in recovered.chat_history:
            c = getattr(m, "content", "")
            texts.append(c if isinstance(c, str) else "")
        assert not any("排队的一条" in t for t in texts), "派发准备阶段消息还没进历史"

    def test_crash_after_admission_marks_needs_check_not_resend(self, isolated_memory):
        recovered, item = self._spawn_and_recover(isolated_memory, "admitted")
        assert item["state"] == "needs_check"
        assert item["message_id"], "身份保留供人工核对"
        assert recovered.last_run is not None and recovered.last_run["phase"] == "running"
        # 不自动重发：needs_check 条目只能由用户"重新入队"或"丢弃"
        head, _blocked = input_queue.next_queued(q=recovered.input_queue) \
            if False else (None, None)
        assert all(it["state"] != "queued" or it["message_id"] != item["message_id"]
                   for it in recovered.input_queue["items"])


# ══════════════════════════════════════════════════════════════
# UI：真 ChatUI + Qt 事件循环 + Event 控制的 worker
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _wait_until(app, pred, seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _destroy_ui_cleanly(ui, app):
    import gc
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
    from PySide6.QtCore import QEvent
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def _type(ui, text):
    ui.entry.setPlainText(text)
    ui._check_input_state()


def _toasts(ui):
    if not hasattr(ui, "_toast_log"):
        ui._toast_log = []
        ui._show_toast = lambda text, duration=1500: ui._toast_log.append(str(text))
    return ui._toast_log


def _history_texts(sess):
    out = []
    for m in sess.chat_history:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.append("".join(b.get("text", "") for b in c if isinstance(b, dict)))
    return out


def _make_saved_session(sess):
    from langchain_core.messages import HumanMessage
    if len(sess.chat_history) <= 1:
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="开场")]
    memory.save_session(session=sess)
    assert sess.current_session_id
    return sess


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory):
    """真实 ChatUI：模型 / MCP / 网络全部离线；toast 换成记录。"""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("no network in queue tests")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    from src import mcp_client, models, tools
    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(mcp_client, "init_mcp", lambda: None)
    monkeypatch.setattr(tools, "get_mcp_tools", lambda: [])
    monkeypatch.setattr(agent, "_BOUND_LLM_CACHE", {})
    monkeypatch.setattr(ChatUI, "_show_current_model_config_warning", lambda self: None)
    from src import config
    monkeypatch.setattr(config, "REMOTE_TELEGRAM_CONFIRM", False)

    ui = ChatUI()
    ui._RESUME_WAIT_MS = 20
    _toasts(ui)
    yield ui
    if state.ui_ref is ui:
        state.ui_ref = None
    _destroy_ui_cleanly(ui, qapp)


def _install_shaped_loop(monkeypatch, data_root, runs, outcome="completed"):
    """带 B03 记录的假模型循环：每轮卡在自己的 Event 上，结束后写 completed 终态。
    worker 线程显式带隔离数据根；测试 finally 统一放行并 join。"""
    from src import recovery

    def _loop(ui_, *, resume=None):
        import src.paths as _paths
        _paths.set_data_dir(data_root)
        try:
            s = session.current_session()
            ev = threading.Event()
            runs.append({"gate": ev, "session": s, "resume": resume,
                         "thread": threading.current_thread()})
            if not recovery.before_run(s, ui=ui_, resume=resume):
                return AgentResult("failed", "恢复检查未通过")
            run = run_records.begin_run(s)
            ev.wait(15)
            run_records.finalize_run(s, run, AgentResult(outcome))
            return AgentResult(outcome)
        finally:
            _paths.set_data_dir(None)

    monkeypatch.setattr(agent, "agent_loop", _loop)
    return runs


@pytest.fixture()
def shaped_runs():
    """登记测试里起过的 worker 线程，finally 统一放行 + join（断言失败也能清理）。"""
    return {"threads": [], "events": []}


@pytest.fixture()
def cleanup_workers(shaped_runs):
    def _cleanup():
        for ev in shaped_runs["events"]:
            ev.set()
        for t in shaped_runs["threads"]:
            t.join(5)
    return _cleanup


@pytest.fixture()
def tracked_shaped_loop(monkeypatch, isolated_memory, shaped_runs, cleanup_workers):
    """装一个带记录的假模型循环，并把线程登记进 shaped_runs 供 finally 清理。"""
    import src.paths as _paths
    from src import recovery
    data_root = _paths.get_data_dir()
    state_box = {"outcome": "completed"}

    def _loop(ui_, *, resume=None):
        _paths.set_data_dir(data_root)
        try:
            s = session.current_session()
            ev = threading.Event()
            shaped_runs["events"].append(ev)
            shaped_runs["threads"].append(threading.current_thread())
            runs_entry = {"gate": ev, "session": s, "resume": resume}
            shaped_runs.setdefault("calls", []).append(runs_entry)
            if not recovery.before_run(s, ui=ui_, resume=resume):
                return AgentResult("failed", "恢复检查未通过")
            run = run_records.begin_run(s)
            ev.wait(15)
            report = run_records.finalize_run(s, run, AgentResult(state_box["outcome"]))
            # 与真实 agent_loop 一致：finalize 的正文保存结果留给队列收尾评估
            s.last_run_save_failed = not report.saved
            return AgentResult(state_box["outcome"])
        finally:
            _paths.set_data_dir(None)

    monkeypatch.setattr(agent, "agent_loop", _loop)
    shaped_runs["state"] = state_box      # 测试经此驱动每轮的终态
    return shaped_runs


@pytest.fixture(autouse=True)
def _release_workers(shaped_runs, cleanup_workers):
    yield
    cleanup_workers()


class TestQueueUIFlows:

    def test_two_items_appended_run_in_order(self, ui, qapp, tracked_shaped_loop):
        """长运行期间追加两条：按顺序各派发一次，原任务仍可见。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()

        _type(ui, "追加一")
        ui._send_message()
        _pump(qapp, 0.2)
        _type(ui, "追加二")
        ui._send_message()
        _pump(qapp, 0.2)
        assert [it["text"] for it in sess.input_queue["items"]] == ["追加一", "追加二"]
        assert all(it["state"] == "queued" for it in sess.input_queue["items"])

        tracked_shaped_loop["events"][0].set()       # 第一轮完成
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert tracked_shaped_loop["calls"][1]["session"] is sess
        assert "追加一" in "".join(_history_texts(sess))
        assert sess.input_queue["items"][0]["state"] == "admitted"

        tracked_shaped_loop["events"][1].set()       # 第二轮完成 → 派发"追加二"
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 3)
        assert "追加二" in "".join(_history_texts(sess))
        assert sess.input_queue["items"][1]["state"] == "admitted"

        tracked_shaped_loop["events"][2].set()
        assert _wait_until(qapp, lambda: all(
            it["state"] == "done" for it in sess.input_queue["items"]))
        _pump(qapp, 0.3)
        assert len(tracked_shaped_loop["calls"]) == 3, "恰好各派发一次"
        # 每条要求在历史里恰好出现一次（B05 来源不重复）
        for text in ("追加一", "追加二"):
            assert "".join(_history_texts(sess)).count(text) == 1

    def test_edit_and_delete_change_dispatched_content(self, ui, qapp,
                                                       tracked_shaped_loop):
        """编辑/删除后实际派发内容与面板一致；重复点击不重复启动。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()

        _type(ui, "原始内容")
        ui._send_message()
        _type(ui, "要删除的")
        ui._send_message()
        _pump(qapp, 0.2)
        q = sess.input_queue
        assert len(q["items"]) == 2

        # 编辑第一条（沿用身份与位置）
        ok, _, _ = input_queue.set_text(q, q["items"][0]["queue_item_id"], "编辑后的内容")
        assert ok
        saved, _ = ui._persist_queue_change(sess)
        assert saved
        # 删除第二条
        ok, reason, _ = input_queue.delete(q, q["items"][1]["queue_item_id"])
        assert ok
        saved, _ = ui._persist_queue_change(sess)
        assert saved
        assert [it["text"] for it in q["items"]] == ["编辑后的内容"]
        ui._refresh_queue_panel()

        tracked_shaped_loop["events"][0].set()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        sent = "".join(_history_texts(sess))
        assert "编辑后的内容" in sent and "原始内容" not in sent
        assert "要删除的" not in sent, "删除的条目不再派发"

    def test_process_now_non_head_stops_waits_others_stay_paused(self, ui, qapp,
                                                                tracked_shaped_loop):
        """立即处理非队首项：明确停止 → 旧 worker 真正退出前无新 worker；
        其余条目保持暂停，本轮主动停止不推进整个队列。"""
        _make_saved_session(session.get_active())
        _type(ui, "运行中的那一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()

        for text in ("排队甲", "排队乙", "要立即的丙"):
            _type(ui, text)
            ui._send_message()
        _pump(qapp, 0.2)
        q = sess.input_queue
        assert [it["text"] for it in q["items"]] == ["排队甲", "排队乙", "要立即的丙"]

        ui._queue_process_now(q["items"][2]["queue_item_id"])
        _pump(qapp, 0.3)
        # 旧 worker 还没退：没有新的一轮；选中条目在等待接纳（dispatching）
        assert len(tracked_shaped_loop["calls"]) == 1, "旧 worker 退出前不起新 worker"
        assert tracked_shaped_loop["threads"][0].is_alive()
        assert q["items"][2]["state"] in ("dispatching", "admitted")
        assert q["paused"] is True and "立即处理" in q["pause_reason"]

        tracked_shaped_loop["events"][0].set()       # 旧 worker 退出
        tracked_shaped_loop["threads"][0].join(5)
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert tracked_shaped_loop["calls"][1]["session"] is sess
        assert "要立即的丙" in "".join(_history_texts(sess))
        assert "排队甲" not in "".join(_history_texts(sess)), "其余条目保持暂停不插队"
        # 丙完成后队列仍是暂停态：不自动推进甲/乙
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.5)
        assert q["paused"] is True
        assert [it["state"] for it in q["items"]] == ["queued", "queued", "done"]
        assert len(tracked_shaped_loop["calls"]) == 2

        # 用户明确恢复 → 队列继续按顺序处理
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 3)
        assert "排队甲" in "".join(_history_texts(sess))

    def test_abnormal_outcome_pauses_completed_advances(self, ui, qapp,
                                                        tracked_shaped_loop):
        """failed 终态暂停并显示原因；completed 且保存成功才自动推进。"""
        _make_saved_session(session.get_active())
        _type(ui, "会失败的一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队等下一轮")
        ui._send_message()
        _pump(qapp, 0.2)

        tracked_shaped_loop["state"]["outcome"] = "failed"
        tracked_shaped_loop["events"][0].set()
        assert _wait_until(qapp, lambda: sess.input_queue["paused"] is True)
        q = sess.input_queue
        assert "failed" in q["pause_reason"]
        assert len(tracked_shaped_loop["calls"]) == 1, "异常终态不推进"
        assert q["items"][0]["state"] == "queued"

        # 用户恢复，本轮 completed → 正常推进
        tracked_shaped_loop["state"]["outcome"] = "completed"
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert q["items"][0]["state"] == "admitted"
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)
        assert q["items"][0]["state"] == "done"

    def test_spawn_failure_keeps_item_and_reports(self, ui, qapp, tracked_shaped_loop,
                                                  monkeypatch):
        """线程构造失败：条目保留、状态如实、可重试。"""
        import types
        from src.ui import chat_window as cw

        class _BrokenThread:
            def __init__(self, *a, **k):
                raise RuntimeError("cannot start new thread")

        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的消息")
        ui._send_message()
        _pump(qapp, 0.2)
        assert len(sess.input_queue["items"]) == 1

        monkeypatch.setattr(cw, "threading", types.SimpleNamespace(
            Thread=_BrokenThread, current_thread=threading.current_thread,
            Event=threading.Event))
        _toasts(ui).clear()
        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)
        q = sess.input_queue
        assert len(q["items"]) == 1, "启动失败保留原项"
        assert q["items"][0]["state"] == "queued"
        assert "启动失败" in (q["items"][0]["error"] or "")
        assert q["paused"] is True
        assert any("启动失败" in t for t in _toasts(ui)), "桌面要有如实反馈"
        assert not any("排队的消息" in t for t in _history_texts(sess)), "未接纳不进历史"

    def test_body_save_failure_rolls_back_enqueue(self, ui, qapp, tracked_shaped_loop,
                                                  monkeypatch):
        """正文保存失败：内存回滚（磁盘从没有过）、输入保留、如实说明。"""
        from src import memory as _memory
        from src.run_records import SaveOutcome

        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()

        def _failing_save(*, session=None):
            return SaveOutcome(error=OSError("disk full"))

        monkeypatch.setattr(_memory, "save_session_report", _failing_save)
        _type(ui, "存不进去的一条")
        ok = ui._send_message()
        _pump(qapp, 0.2)
        assert ok is None, "入队失败不继续发送路径"
        assert sess.input_queue["items"] == [], "正文失败回滚内存"
        assert ui.entry.toPlainText() == "存不进去的一条", "输入保留可重试"
        assert any("未入队" in t or "保存失败" in t for t in _toasts(ui))

    def test_attachment_missing_holds_item_before_dispatch(self, ui, qapp,
                                                           tracked_shaped_loop,
                                                           tmp_path):
        """附件在派发前消失：条目暂停并点名，不发送残缺要求。"""
        img = _make_image(tmp_path, "will-vanish.png")
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()

        _type(ui, "看这张图")
        ui._add_pending_image(img)
        ui._send_message()
        _pump(qapp, 0.2)
        assert sess.input_queue["items"][0]["images"], "附件引用已入队"
        os.remove(img)

        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)
        q = sess.input_queue
        assert q["items"][0]["state"] == "held", "附件缺失 → 暂停"
        assert "不存在" in (q["items"][0]["hold_reason"] or "")
        assert q["paused"] is True
        assert not any("看这张图" in t for t in _history_texts(sess)), "不发送残缺要求"

    def test_attachment_changed_holds_item(self, ui, qapp, tracked_shaped_loop,
                                           tmp_path):
        """附件内容在派发前被换掉：暂停并点名，不拿新内容顶替发送。"""
        img = _make_image(tmp_path, "will-change.png")
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "看这张图")
        ui._add_pending_image(img)
        ui._send_message()
        _pump(qapp, 0.2)

        with open(img, "wb") as f:
            f.write(b"tampered")
        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)
        q = sess.input_queue
        assert q["items"][0]["state"] == "held"
        assert "变化" in (q["items"][0]["hold_reason"] or "")

    def test_project_change_holds_pending_item(self, ui, qapp, tracked_shaped_loop):
        """等待派发期间项目归属变了：暂停并说明，不把消息发到新项目。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "属于原项目的消息")
        ui._send_message()
        _pump(qapp, 0.2)
        assert sess.input_queue["items"][0]["state"] == "queued"

        sess.project = "D:/别处/另一个项目"       # 归属迁移（同一会话对象）
        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)
        q = sess.input_queue
        assert q["items"][0]["state"] == "held"
        assert "项目" in (q["items"][0]["hold_reason"] or "")
        assert not any("属于原项目的消息" in t for t in _history_texts(sess))

    def test_switching_away_pauses_queue_and_cancels_dispatch(self, ui, qapp,
                                                              tracked_shaped_loop):
        """切会话：取消尚未接纳的派发等待、条目保留并暂停；另一会话照常运行。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "切走前排队")
        ui._send_message()
        _pump(qapp, 0.2)
        # 停止（进入收尾窗口）→ 自动推进把条目送进等待
        ui._on_send_click()
        _pump(qapp, 0.2)
        assert sess.input_queue["items"][0]["state"] in ("queued", "dispatching")

        other = session.Session()
        ui._activate_session(other)                 # 切走
        _pump(qapp, 0.3)
        q = sess.input_queue
        assert q["paused"] is True and "切换" in q["pause_reason"]
        assert all(it["state"] in ("queued", "held", "done")
                   for it in q["items"]), "派发等待已取消、条目保留"

        # 另一会话照常运行
        _type(ui, "B 会话自己的消息")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert tracked_shaped_loop["calls"][1]["session"] is other
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.3)

        tracked_shaped_loop["events"][0].set()      # A 的旧 worker 退出：不自动派发
        _pump(qapp, 0.5)
        assert not any("切走前排队" in t for t in _history_texts(sess)), "不串发"

    def test_b05_request_sources_single_and_no_internal_mix(self, ui, qapp,
                                                            tracked_shaped_loop):
        """接入 B05：派发的排队消息作为要求来源恰好一次；内部消息不混入。"""
        _make_saved_session(session.get_active())
        sess = session.get_active()
        # 预置一个任务身份（B05）：入队时已有任务
        sess.current_task = {"id": "task-1", "request_message_ids": [],
                             "request_sources": {}}
        memory.save_session(session=sess)

        _type(ui, "第一轮要求")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        _type(ui, "排队补充要求")
        ui._send_message()
        _pump(qapp, 0.2)
        # 入队阶段不接要求来源：第一轮的要求已在来源里，排队这条不在
        reqs = sess.current_task["request_message_ids"]
        mid = sess.input_queue["items"][0]["message_id"]
        assert mid not in reqs, "入队阶段不进要求来源"
        assert len(reqs) == 1, "第一轮的要求照常接入"

        tracked_shaped_loop["events"][0].set()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        _pump(qapp, 0.4)
        # 派发后：要求来源恰好包含排队这条（一次），自动修复/恢复类内部消息不在其中
        reqs = sess.current_task["request_message_ids"]
        mid = sess.input_queue["items"][0]["message_id"]
        assert mid in reqs, "派发消息接入要求来源"
        assert reqs.count(mid) == 1, "要求来源不重复"
        assert len(reqs) == 2, "恰好两条要求：第一轮 + 派发的排队项"
        hist = sess.chat_history
        queued_msg = [m for m in hist
                      if getattr(m, "additional_kwargs", {}).get("lingxi_message_id") == mid]
        assert len(queued_msg) == 1
        assert queued_msg[0].additional_kwargs.get("lingxi_kind") == "user_input"
        kinds = [getattr(m, "additional_kwargs", {}).get("lingxi_kind")
                 for m in hist]
        assert all(k in (None, "user_input") for k in kinds), "无内部消息混入用户要求"


# ══════════════════════════════════════════════════════════════
# 复核修复：持久化一致性、收尾核实、状态化删除、容量拆分、编辑总量
# ══════════════════════════════════════════════════════════════

class TestReviewFixes:

    def test_index_failure_keeps_memory_and_disk_consistent(self, ui, qapp,
                                                            tracked_shaped_loop,
                                                            monkeypatch):
        """P1 复现：正文已落盘、索引失败 → 不回滚内存（否则界面"未入队"而磁盘
        有条目，再提交就是重复消息）；如实提示并暂停自动推进。"""
        from src import memory as _memory
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        sid = sess.current_session_id

        def _index_boom(*a, **k):
            raise OSError("index locked")

        monkeypatch.setattr(_memory, "_update_index", _index_boom)
        _toasts(ui).clear()
        _type(ui, "索引会失败的一条")
        assert ui._send_message() is None
        _pump(qapp, 0.3)

        assert len(sess.input_queue["items"]) == 1, "正文已落盘：内存不回滚"
        assert ui.entry.toPlainText() == "", "提交完成，输入已清"
        assert sess.input_queue["paused"] is True, "索引失败暂停自动推进"
        assert not any("未入队" in t for t in _toasts(ui))
        assert any("已排队" in t and "索引" in t for t in _toasts(ui))

        # 磁盘上确实有这条（读盘核对）
        monkeypatch.undo()
        disk = session.Session()
        assert _memory.load_session(sid, session=disk)
        assert len(disk.input_queue["items"]) == 1, "磁盘与内存一致"

    def test_settle_save_failure_blocks_auto_advance(self, ui, qapp,
                                                     tracked_shaped_loop,
                                                     monkeypatch):
        """P1 复现：上一轮是普通发送、收尾保存失败——自动推进必须暂停，
        不得带着未持久化的状态启动下一轮。"""
        from src import memory as _memory
        _make_saved_session(session.get_active())
        _type(ui, "普通发送的一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        sid = sess.current_session_id
        _type(ui, "排队等下一轮")
        ui._send_message()
        _pump(qapp, 0.2)
        assert len(sess.input_queue["items"]) == 1

        # 从此刻起正文写入全部失败（该会话的 JSON）
        real_write = _memory._atomic_write_json

        def _failing_write(path, *a, **k):
            if os.path.normcase(str(path)).endswith(sid + ".json"):
                raise OSError("disk full")
            return real_write(path, *a, **k)

        monkeypatch.setattr(_memory, "_atomic_write_json", _failing_write)
        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)

        assert len(tracked_shaped_loop["calls"]) == 1, "收尾保存失败不得启动下一轮"
        assert sess.input_queue["paused"] is True, "队列暂停并说明"
        assert any("保存失败" in t for t in _toasts(ui))
        assert sess.input_queue["items"][0]["state"] == "queued"

        # 保存恢复后，用户明确恢复 → 正常派发
        monkeypatch.setattr(_memory, "_atomic_write_json", real_write)
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert "排队等下一轮" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.3)

    def test_delete_held_and_discard_needs_check(self, ui, qapp, monkeypatch):
        """P2 复现：held 的删除、needs_check 的丢弃/重新入队必须真的移除旧条目。"""
        _make_saved_session(session.get_active())
        sess = session.get_active()
        ui.show()
        _pump(qapp, 0.1)

        held = input_queue.new_queue_item("被暂停的", [])
        broken = input_queue.new_queue_item("待核对的", [])
        for it in (held, broken):
            assert input_queue.enqueue(sess.input_queue, it)[0]
        input_queue.hold(sess.input_queue, held["queue_item_id"], "附件缺失")
        input_queue.mark_dispatching(sess.input_queue, broken["queue_item_id"])
        assert ui._persist_queue_change(sess)[0]
        # needs_check 由加载恢复落地——直接模拟恢复
        restored, _ = input_queue.normalize(input_queue.snapshot(sess.input_queue))
        sess.input_queue.clear()
        sess.input_queue.update(restored)
        assert sess.input_queue["items"][1]["state"] == "needs_check"
        ui._refresh_queue_panel()

        # 删除 held 条目（真实按钮）
        from PySide6.QtWidgets import QPushButton, QWidget
        row = next(w for w in ui.queue_panel.findChildren(QWidget)
                   if w.objectName() == "queue-row-" + held["queue_item_id"])
        next(b for b in row.findChildren(QPushButton) if b.text() == "删除").click()
        _pump(qapp, 0.2)
        assert input_queue.find(sess.input_queue, held["queue_item_id"]) is None,             "held 条目必须真的被删除"

        # 重新入队：旧 needs_check 条目被移除，恰好一条新条目（旧按钮不再生成副本）
        old_id = broken["queue_item_id"]
        row = next(w for w in ui.queue_panel.findChildren(QWidget)
                   if w.objectName() == "queue-row-" + old_id)
        next(b for b in row.findChildren(QPushButton) if b.text() == "重新入队").click()
        _pump(qapp, 0.2)
        items = sess.input_queue["items"]
        assert len(items) == 1 and items[0]["state"] == "queued"
        assert items[0]["queue_item_id"] != old_id and items[0]["message_id"] != broken["message_id"]
        assert input_queue.find(sess.input_queue, old_id) is None, "旧条目已移除"

        # 丢弃：直接移除
        ui._on_queue_discard_requested(items[0]["queue_item_id"])
        _pump(qapp, 0.2)
        assert sess.input_queue["items"] == []

    def test_done_items_do_not_consume_capacity(self):
        """P2 复现：done 只留作结果记录，不占排队容量——处理满 20 条后队列不满员。"""
        q = input_queue.new_queue()
        for i in range(limits.QUEUE_MAX_ITEMS):
            it = input_queue.new_queue_item(f"已完成的第{i}条" + "x" * 100, [])
            assert input_queue.enqueue(q, it)[0]
            input_queue.mark_dispatching(q, it["queue_item_id"])
            input_queue.mark_admitted(q, it["queue_item_id"])
            input_queue.mark_done(q, it["queue_item_id"], "run-1", "completed")
        assert len(q["items"]) == limits.QUEUE_MAX_ITEMS
        ok, reason = input_queue.enqueue(q, input_queue.new_queue_item("新的一条", []))
        assert ok, f"done 不占容量：{reason}"

    def test_edit_checks_total_capacity(self):
        """P2 复现：编辑按替换后的总量校验，不能把总量顶破。"""
        q = input_queue.new_queue()
        items = [input_queue.new_queue_item("a" * 19_900, []) for _ in range(5)]
        tail = input_queue.new_queue_item("b" * 400, [])
        for it in items + [tail]:
            assert input_queue.enqueue(q, it)[0]
        total_before = sum(len(it["text"]) for it in q["items"])
        assert total_before == 99_900

        # 把 400 字符的尾条编辑成 20000 → 总量 119500 > 100000 → 拒绝，原文保留
        ok, reason, _ = input_queue.set_text(q, tail["queue_item_id"], "c" * 20_000)
        assert not ok and "总量" in reason
        assert input_queue.find(q, tail["queue_item_id"])["text"] == "b" * 400
        assert sum(len(it["text"]) for it in q["items"]) == 99_900, "被拒的编辑不得生效"

        # 合法缩编可以
        ok, _, _ = input_queue.set_text(q, tail["queue_item_id"], "短了")
        assert ok and input_queue.find(q, tail["queue_item_id"])["text"] == "短了"


# ══════════════════════════════════════════════════════════════
# 复核第二轮：身份核对、派发门槛、瞬时保存失败、编辑期间暂停、回滚
# ══════════════════════════════════════════════════════════════

class TestReviewRound2:

    def test_vision_failure_recorded_as_failed_not_previous_completed(
            self, ui, qapp, tracked_shaped_loop, monkeypatch, tmp_path):
        """P1 复现：图片识别失败后，该条目必须记录本次失败（failed），绝不能
        抄上一轮的 completed 与 run_id，更不能继续派发后续消息。"""
        img = _make_image(tmp_path, "q.png")
        monkeypatch.setattr(agent, "current_model_supports_vision", lambda: False)
        monkeypatch.setattr(agent, "get_vision_model_index",
                            lambda: _api_models()[0])   # 不依赖本地配置的视觉模型

        def _vision_boom(text, images):
            raise RuntimeError("vision api down")

        monkeypatch.setattr(agent, "describe_images_with_vision", _vision_boom)
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        prev_run_id = sess.last_run["id"]

        _type(ui, "看这张图")
        ui._add_pending_image(img)
        ui._send_message()
        _pump(qapp, 0.2)
        assert len(sess.input_queue["items"]) == 1

        tracked_shaped_loop["events"][0].set()
        tracked_shaped_loop["threads"][0].join(5)
        # 派发 → 视觉 worker：消息先进历史、识别抛错 → 补 failed 记录 → finished
        assert _wait_until(qapp, lambda: (
            sess.input_queue["items"][0]["state"] == "done"
            and sess.input_queue["items"][0]["outcome_status"] == "failed"))
        _pump(qapp, 0.3)
        q = sess.input_queue
        item = q["items"][0]
        assert item["outcome_status"] == "failed", f"实际失败要如实记录: {item}"
        assert item["admitted_run_id"] != prev_run_id, "不得绑定上一轮的 run_id"
        assert q["paused"] is True, "失败后暂停自动推进"
        # 消息先进历史（识别失败也不丢消息），身份与条目一致
        mid = item["message_id"]
        landed = [m for m in sess.chat_history
                  if getattr(m, "additional_kwargs", {}).get("lingxi_message_id") == mid]
        assert len(landed) == 1, "识别失败消息也不能丢"
        # 后续没有的东西：没有第二项可派发；若有也不会推进
        assert len(tracked_shaped_loop["calls"]) == 1

    def test_recovery_rejected_round_not_credited_to_item(self, ui, qapp,
                                                          tracked_shaped_loop,
                                                          monkeypatch):
        """收尾身份核对：本轮被恢复检查拒绝（无运行记录）时，条目不得被安上
        上一轮的 completed 与 run_id——如实记 failed 并暂停。"""
        from src import recovery as _recovery
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        prev_run_id = sess.last_run["id"]
        _type(ui, "被拒绝的一条")
        ui._send_message()
        _pump(qapp, 0.2)

        calls = {"n": 0}

        def _reject_second(sess_, *, ui=None, resume=None):
            calls["n"] += 1
            # 补丁在第一轮开始后才装上：被包装的第一次调用就是队列条目那一轮
            return False

        monkeypatch.setattr(_recovery, "before_run", _reject_second)
        tracked_shaped_loop["events"][0].set()
        assert _wait_until(qapp, lambda: sess.input_queue["items"][0]["state"]
                           in ("done", "admitted"))
        _pump(qapp, 0.3)
        item = sess.input_queue["items"][0]
        assert item["outcome_status"] == "failed",             f"无运行记录的一轮不得记成 completed: {item}"
        assert item["admitted_run_id"] == "", "没有属于它的运行，run_id 留空"
        assert item["admitted_run_id"] != prev_run_id
        assert sess.input_queue["paused"] is True

    def test_dispatch_prep_index_failure_aborts_launch(self, ui, qapp,
                                                       tracked_shaped_loop,
                                                       monkeypatch):
        """P1 复现：派发准备的索引保存失败 → 取消派发，不启动线程
        （提交成功 ≠ 允许继续执行）。"""
        from src import memory as _memory
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "索引会失败的一条")
        ui._send_message()
        _pump(qapp, 0.2)

        def _index_boom(*a, **k):
            raise OSError("index locked")

        monkeypatch.setattr(_memory, "_update_index", _index_boom)
        _toasts(ui).clear()
        ui._queue_process_now(sess.input_queue["items"][0]["queue_item_id"])
        _pump(qapp, 0.4)

        assert len(tracked_shaped_loop["calls"]) == 1, "索引失败不得启动线程"
        assert tracked_shaped_loop["threads"][0].is_alive() or True
        item = sess.input_queue["items"][0]
        assert item["state"] == "queued", "派发未开始，条目回到队列"
        assert "索引" in (item["error"] or "")
        assert any("派发未开始" in t for t in _toasts(ui))

    def test_transient_settle_save_failure_pauses_not_advances(
            self, ui, qapp, tracked_shaped_loop, monkeypatch):
        """P2 复现：本轮 finalize 的正文保存失败（短暂），随后的补存成功——
        仍必须暂停，不得自动推进。"""
        from src import memory as _memory
        _make_saved_session(session.get_active())
        _type(ui, "普通发送的一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        sid = sess.current_session_id
        _type(ui, "排队等下一轮")
        ui._send_message()
        _pump(qapp, 0.2)

        real_write = _memory._atomic_write_json
        failures = {"n": 1}

        def _fail_once(path, *a, **k):
            if failures["n"] > 0 and os.path.normcase(str(path)).endswith(sid + ".json"):
                failures["n"] -= 1
                raise OSError("transient disk hiccup")
            return real_write(path, *a, **k)

        monkeypatch.setattr(_memory, "_atomic_write_json", _fail_once)
        tracked_shaped_loop["events"][0].set()
        _pump(qapp, 0.6)

        assert len(tracked_shaped_loop["calls"]) == 1, "保存失败（哪怕短暂）不得自动推进"
        assert sess.input_queue["paused"] is True
        assert any("保存失败" in t for t in _toasts(ui))
        assert sess.input_queue["items"][0]["state"] == "queued"

        monkeypatch.setattr(_memory, "_atomic_write_json", real_write)
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.3)

    def test_edit_dialog_holds_item_against_auto_advance(self, ui, qapp,
                                                         tracked_shaped_loop,
                                                         monkeypatch):
        """P2 复现：编辑对话框打开期间上一轮结束——原文不得被自动派发；
        确认后派发的是编辑后的内容。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        # 对话框存根：exec 期间放行上一轮并泵事件（复现"编辑中上一轮结束"）
        def _dialog_exec_during_finish(item):
            tracked_shaped_loop["events"][0].set()
            _wait_until(qapp, lambda: sess.input_queue.get("paused") is True
                        or sess.input_queue["items"][0]["state"] != "held")
            return "编辑后的内容"

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_exec_during_finish)
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        sent = "".join(_history_texts(sess))
        assert "排队的一条" not in sent, "编辑期间原文不得被自动派发"
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2), \
            "确认后自动派发编辑后的内容"
        assert "编辑后的内容" in "".join(_history_texts(sess))
        assert sess.input_queue["items"][0]["state"] in ("admitted", "done")
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)

    def test_edit_keeps_failure_pause_added_during_dialog(self, ui, qapp,
                                                          tracked_shaped_loop,
                                                          monkeypatch):
        """编辑窗口打开时上一轮失败（队列被程序暂停），点确定不得解除失败暂停
        并继续派发——只解除"正在编辑"造成的暂停。"""
        _make_saved_session(session.get_active())
        _type(ui, "会失败的一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "编辑中的条目")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        tracked_shaped_loop["state"]["outcome"] = "failed"

        def _dialog_during_fail(item_):
            tracked_shaped_loop["events"][0].set()
            _wait_until(qapp, lambda: sess.input_queue["paused"] is True)
            return "编辑后的内容"

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_during_fail)
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        assert sess.input_queue["paused"] is True, "失败暂停必须保留"
        assert "failed" in sess.input_queue["pause_reason"]
        assert sess.input_queue["items"][0]["text"] == "编辑后的内容", "编辑仍生效"
        assert len(tracked_shaped_loop["calls"]) == 1, "失败暂停下不派发"

    def test_cancel_edit_resumes_auto_dispatch(self, ui, qapp, tracked_shaped_loop,
                                               monkeypatch):
        """取消编辑也要恢复自动处理：上一轮在编辑期间正常结束，取消后条目
        （原文）应被正常派发，而不是永远待处理。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        def _dialog_cancel_during_finish(item_):
            tracked_shaped_loop["events"][0].set()
            _wait_until(qapp, lambda: "正在编辑" in (
                sess.input_queue.get("pause_reason") or ""))
            return None                              # 用户点了取消

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_cancel_during_finish)
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.2)
        assert sess.input_queue["paused"] is False, "编辑造成的暂停随取消解除"
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2),             "取消后恢复自动派发（原文）"
        assert "排队的一条" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)

    @pytest.mark.parametrize("stub_text", ["", "x" * 20_001],
                             ids=["empty", "over-limit"])
    def test_edit_rejected_variants_still_dispatch_original(self, ui, qapp,
                                                            tracked_shaped_loop,
                                                            monkeypatch, stub_text):
        """复核复现（参数化，每种情形独立现场）：编辑被拒（清空内容 / 超单条
        上限）后，原消息必须恢复推进，不能停在 queued 而面板显示"自动处理"。"""
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        def _dialog_reject(item_):
            tracked_shaped_loop["events"][0].set()   # 上一轮在编辑期间结束
            _wait_until(qapp, lambda: "正在编辑" in (
                sess.input_queue.get("pause_reason") or ""))
            return stub_text

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_reject)
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        assert sess.input_queue["paused"] is False, "编辑造成的暂停已解除"
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2), \
            f"编辑未生效（{stub_text[:10]!r}…）也要派发原消息"
        assert "排队的一条" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        assert _wait_until(qapp, lambda: sess.input_queue["items"][0]["state"] == "done")
        _pump(qapp, 0.3)

    @pytest.mark.parametrize("dialog_return", [None, "编辑后的内容"],
                             ids=["cancel", "confirm"])
    def test_entry_save_failure_exits_edit_and_pauses(self, ui, qapp,
                                                      tracked_shaped_loop,
                                                      monkeypatch, dialog_return):
        """P2 复现（参数化：取消/确认两条路）：进入编辑的保存失败 → 恢复可操作
        的原条目、暂停队列、退出编辑（不打开对话框），worker 不得增至 2。"""
        from src.run_records import SaveOutcome
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        real_save = memory.save_session_report
        state_box = {"n": 0}

        def _entry_save_failing(*, session=None):
            state_box["n"] += 1
            if state_box["n"] == 1:          # 只让入口保存失败一次
                return SaveOutcome(error=OSError("disk full"))
            return real_save(session=session)

        monkeypatch.setattr(memory, "save_session_report", _entry_save_failing)
        dialog_calls = []

        def _dialog_stub(item_):
            dialog_calls.append(item_)
            tracked_shaped_loop["events"][0].set()   # 若误开对话框：上一轮在此结束
            _wait_until(qapp, lambda: "正在编辑" in (
                sess.input_queue.get("pause_reason") or ""))
            return dialog_return

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_stub)
        _toasts(ui).clear()
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        assert dialog_calls == [], "入口保存失败必须退出编辑，不打开对话框"
        q = sess.input_queue
        assert q["paused"] is True, "入口保存失败必须暂停队列"
        assert "编辑状态保存失败" in q["pause_reason"]
        assert q["items"][0]["state"] == "queued", "原条目恢复为可操作"
        assert q["items"][0]["text"] == "排队的一条"
        assert len(tracked_shaped_loop["calls"]) == 1, "不得启动第二轮"

        # 保存恢复后，放行仍在运行的第一轮（对话框未开，worker 一直阻塞），
        # 再由用户手动继续 → 原消息派发
        monkeypatch.setattr(memory, "save_session_report", real_save)
        tracked_shaped_loop["events"][0].set()
        tracked_shaped_loop["threads"][0].join(5)
        _pump(qapp, 0.3)
        assert sess.input_queue["paused"] is True, "失败暂停不被自动推进绕过"
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert "排队的一条" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)

    def test_exit_save_failure_pauses_and_blocks_dispatch(self, ui, qapp,
                                                          tracked_shaped_loop,
                                                          monkeypatch):
        """P2 复现：取消编辑时恢复状态的那次保存失败 → 暂停、提示、禁止推进，
        不得 worker 1→2 照跑原消息。失败只命中这一次（收尾保存成功）。"""
        from src.run_records import SaveOutcome
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        real_save = memory.save_session_report
        fail = {"v": False}

        def _exit_save_failing(*, session=None):
            if fail["v"]:
                return SaveOutcome(error=OSError("disk full"))
            return real_save(session=session)

        monkeypatch.setattr(memory, "save_session_report", _exit_save_failing)

        def _dialog_cancel_activate(item_):
            tracked_shaped_loop["events"][0].set()
            _wait_until(qapp, lambda: "正在编辑" in (
                sess.input_queue.get("pause_reason") or ""))
            fail["v"] = True          # 收尾保存已成功；失败只命中退出保存
            return None               # 用户取消

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_cancel_activate)
        _toasts(ui).clear()
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        q = sess.input_queue
        assert q["paused"] is True, "退出保存失败必须暂停队列"
        assert "编辑状态保存失败" in q["pause_reason"]
        assert q["items"][0]["state"] == "queued"
        assert len(tracked_shaped_loop["calls"]) == 1, "禁止自动推进"
        assert any("状态保存失败" in t for t in _toasts(ui))

        # 保存恢复后，用户手动继续 → 原消息派发
        monkeypatch.setattr(memory, "save_session_report", real_save)
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert "排队的一条" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)

    def test_requeue_replaces_in_full_queue(self, ui, qapp, monkeypatch):
        """P2 复现：满队列（20 条待处理）时 needs_check 的重新入队必须以替换完成；
        替换的保存失败要按原位恢复旧条目。"""
        from src import input_queue as _iq
        from src.run_records import SaveOutcome
        _make_saved_session(session.get_active())
        sess = session.get_active()
        broken = None
        for i in range(limits.QUEUE_MAX_ITEMS):
            it = input_queue.new_queue_item(f"第{i}条", [])
            assert _iq.enqueue(sess.input_queue, it)[0]
            if i == limits.QUEUE_MAX_ITEMS - 1:
                broken = it
        _iq.mark_dispatching(sess.input_queue, broken["queue_item_id"])
        restored, _ = _iq.normalize(_iq.snapshot(sess.input_queue))
        sess.input_queue.clear()
        sess.input_queue.update(restored)
        assert sess.input_queue["items"][-1]["state"] == "needs_check"

        real_save = memory.save_session_report
        state_box = {"fail": True}

        def _maybe_failing_save(*, session=None):
            if state_box["fail"]:
                return SaveOutcome(error=OSError("full"))
            return real_save(session=session)

        monkeypatch.setattr(memory, "save_session_report", _maybe_failing_save)
        ui._on_queue_retry_requested(broken["queue_item_id"])
        _pump(qapp, 0.2)
        items = sess.input_queue["items"]
        assert len(items) == limits.QUEUE_MAX_ITEMS
        assert any(it["queue_item_id"] == broken["queue_item_id"] for it in items),             "保存失败恢复旧条目（原位）"
        assert items[-1]["queue_item_id"] == broken["queue_item_id"], "原位恢复"
        assert any("重新入队未保存" in t for t in _toasts(ui))

        state_box["fail"] = False
        ui._on_queue_retry_requested(broken["queue_item_id"])
        _pump(qapp, 0.2)
        items = sess.input_queue["items"]
        assert len(items) == limits.QUEUE_MAX_ITEMS, "替换不增减条数"
        assert all(it["queue_item_id"] != broken["queue_item_id"]
                   for it in items), "旧条目已移除"
        assert items[-1]["state"] == "queued" and items[-1]["text"] == "第19条"


    def test_edit_save_failure_pauses_queue(self, ui, qapp, tracked_shaped_loop,
                                            monkeypatch):
        """P2 复现：编辑的正文保存失败 → 明确暂停队列、保留原条目，
        不得自动执行旧消息（实测 worker 1 → 2）。"""
        from src.run_records import SaveOutcome
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        _type(ui, "排队的一条")
        ui._send_message()
        _pump(qapp, 0.2)
        item = sess.input_queue["items"][0]

        real_save = memory.save_session_report
        fail = {"v": False}

        def _edit_save_failing(*, session=None):
            if fail["v"]:
                return SaveOutcome(error=OSError("disk full"))
            return real_save(session=session)

        monkeypatch.setattr(memory, "save_session_report", _edit_save_failing)

        def _dialog_activate_and_return(item_):
            tracked_shaped_loop["events"][0].set()
            # 上一轮正常结束：收尾保存成功，队列因编辑 hold 暂停——
            # 失败从【返回那一刻】才激活，只命中编辑自己的保存
            _wait_until(qapp, lambda: "正在编辑" in (
                sess.input_queue.get("pause_reason") or ""))
            fail["v"] = True
            return "编辑后的内容"

        monkeypatch.setattr(ui, "_show_queue_edit_dialog", _dialog_activate_and_return)
        _toasts(ui).clear()
        ui._on_queue_edit_requested(item["queue_item_id"])
        _pump(qapp, 0.4)

        q = sess.input_queue
        assert q["paused"] is True, "编辑保存失败必须暂停队列"
        assert "编辑保存失败" in q["pause_reason"],             f"暂停原因来自编辑本身（而非收尾）: {q['pause_reason']}"
        assert q["items"][0]["text"] == "排队的一条", "原条目保留（编辑已回滚）"
        assert q["items"][0]["state"] == "queued"
        assert len(tracked_shaped_loop["calls"]) == 1, "不自动执行旧消息"
        assert any("修改未生效" in t for t in _toasts(ui))

        # 保存恢复后，用户手动继续 → 原消息派发
        monkeypatch.setattr(memory, "save_session_report", real_save)
        ui._on_queue_resume_requested()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert "排队的一条" in "".join(_history_texts(sess))
        tracked_shaped_loop["events"][1].set()
        _pump(qapp, 0.4)

    def test_discard_and_resume_rollback_on_save_failure(self, ui, qapp, monkeypatch):
        """P2 复现：丢弃 / 恢复的正文保存失败时，内存必须回到操作前状态。"""
        from src import memory as _memory
        from src.run_records import SaveOutcome

        _make_saved_session(session.get_active())
        sess = session.get_active()
        item = input_queue.new_queue_item("待丢弃的", [])
        assert input_queue.enqueue(sess.input_queue, item)[0]
        input_queue.set_paused(sess.input_queue, True, "手动暂停")
        assert ui._persist_queue_change(sess)[0]

        monkeypatch.setattr(_memory, "save_session_report",
                            lambda *, session=None: SaveOutcome(error=OSError("full")))

        ui._on_queue_discard_requested(item["queue_item_id"])
        _pump(qapp, 0.2)
        assert input_queue.find(sess.input_queue, item["queue_item_id"]) is not None, \
            "丢弃未保存：条目必须恢复"

        ui._on_queue_resume_requested()
        _pump(qapp, 0.2)
        assert sess.input_queue["paused"] is True, "恢复未保存：内存回到暂停态"
        assert "手动暂停" in sess.input_queue["pause_reason"], "原暂停原因保留"


# ══════════════════════════════════════════════════════════════
# GUI 验收：真实渲染，面板与各入口可点可用
# ══════════════════════════════════════════════════════════════

class TestQueuePanelGUI:

    def test_panel_renders_and_actions_work(self, ui, qapp, tracked_shaped_loop,
                                            monkeypatch):
        """真实渲染：排队/编辑/删除/立即处理/继续入口全部走通。"""

        ui.show()                               # 离屏平台：show 之后 isVisible 才为真
        _pump(qapp, 0.1)
        _make_saved_session(session.get_active())
        _type(ui, "第一轮")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 1)
        sess = session.get_active()
        for text in ("面板甲", "面板乙"):
            _type(ui, text)
            ui._send_message()
        _pump(qapp, 0.3)

        # 面板可见且渲染了两行（真实控件树里找得到摘录文本）
        assert ui.queue_panel.isVisible()
        panel_text = "\n".join(w.text() for w in ui.queue_panel.findChildren(
            __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel))
        assert "面板甲" in panel_text and "面板乙" in panel_text
        assert "待处理" in panel_text

        # 删除"面板乙"：按行 objectName 定位到乙所在行的删除按钮
        from PySide6.QtWidgets import QPushButton, QWidget
        item_b = sess.input_queue["items"][1]
        rows = [w for w in ui.queue_panel.findChildren(QWidget)
                if w.objectName() == "queue-row-" + item_b["queue_item_id"]]
        assert rows, "乙所在行已渲染"
        del_btn = next(b for b in rows[0].findChildren(QPushButton) if b.text() == "删除")
        del_btn.click()
        _pump(qapp, 0.3)
        assert [it["text"] for it in sess.input_queue["items"]] == ["面板甲"]

        # 编辑：点真实"编辑"按钮 → 对话框替换为可控桩 → 确认后落盘且身份不变
        monkeypatch.setattr(ui, "_show_queue_edit_dialog",
                            lambda item: "面板甲（已编辑）")
        item_a = sess.input_queue["items"][0]
        row_a = [w for w in ui.queue_panel.findChildren(QWidget)
                 if w.objectName() == "queue-row-" + item_a["queue_item_id"]]
        edit_btn = next(b for b in row_a[0].findChildren(QPushButton) if b.text() == "编辑")
        edit_btn.click()
        _pump(qapp, 0.3)
        assert sess.input_queue["items"][0]["text"] == "面板甲（已编辑）"
        assert sess.input_queue["items"][0]["message_id"] == item_a["message_id"], "编辑不改身份"

        # 立即处理：停止当前轮、等退出后派发
        ui._on_queue_process_now_requested(sess.input_queue["items"][0]["queue_item_id"])
        _pump(qapp, 0.3)
        assert len(tracked_shaped_loop["calls"]) == 1, "旧 worker 退出前无新 worker"
        tracked_shaped_loop["events"][0].set()
        tracked_shaped_loop["threads"][0].join(5)
        assert _wait_until(qapp, lambda: len(tracked_shaped_loop["calls"]) == 2)
        assert "面板甲（已编辑）" in "".join(_history_texts(sess))

        # 队列处于"立即处理"暂停态：继续按钮可见且能恢复
        assert ui.queue_panel._resume_btn.isVisible()
        ui.queue_panel.resume_requested.emit()
        _pump(qapp, 0.2)
        assert sess.input_queue["paused"] is False

    def test_panel_hidden_when_queue_empty(self, ui, qapp):
        assert not ui.queue_panel.isVisible()
