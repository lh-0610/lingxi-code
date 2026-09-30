"""B05 独立核验指出的 11 项问题与补充发现的回归测试。

1. 任务要求串线：B 的补充会把 A 的要求重新带进来；归档的 A 含触发 B 的消息。
2. 旧消息被追认为用户原话：建立 / 同步 / 切换三条路径把缺 ID 的旧消息改标成 user_input。
3. 撤销误删归档、丢失新任务：pop() 删掉无关归档；撤销后新任务不再存在于任何清单。
4. 撤销屏障失效：worker 仍存活时超时照样执行；没有等待标志；切走会话后也继续执行。
5. 无证据显示验证通过：没有待验证文件就画绿；盲区不进面板也不进运行态。
6. 上下文预算失效：最新补充全文不受预算；中间补充只剩数量。
7. 编造摘要覆盖：历史里找不到原消息也声称"已在压缩摘要中覆盖"。
8. 固定限制改写原文：`--force 禁止使用` 被剥成 `force 禁止使用`。
9. 任务与计划不是原子更新：存盘能拍到"新任务 + 旧计划"或"任务已建立、计划为空"。
10. 保存失败被隐瞒：撤销与编辑限制都在保存失败时照样报成功。
11. 首次计划被当成任务切换：留下"切换到没有任务"的撤销记录；空计划也能开始新任务。
补充：历史压缩后，同步会删掉找不到原消息的要求引用。
P3：created_at 缺失补成当前时间；未知内部种类静默变成 repair。
"""
from __future__ import annotations

import copy
import inspect
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, paths, run_records, session, state, task_state as ts, tools


@pytest.fixture(autouse=True)
def _no_real_models_or_services(monkeypatch):
    """误走到真实模型 / Claude CLI 就当场失败（同 B04 用例）。"""
    from src import agent as _agent
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(_agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


@pytest.fixture
def sess(isolated_memory, monkeypatch):
    s = session.Session()
    s.project = None
    s.current_session_id = "b05-review"
    s.chat_history = [SystemMessage(content="offline")]
    session.set_active(s)
    session.bind_thread(s)
    monkeypatch.setattr(state, "ui_ref", None)
    yield s
    session.unbind_thread()


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def user(s, text):
    """同 GUI 发送入口：打标、追加、同步补充要求。"""
    m = ts.tag_user_message(HumanMessage(content=text))
    s.chat_history.append(m)
    ts.sync_user_requests(s)
    return m


def plan(text="A step", **kw):
    return tools.update_plan.func("[ ] " + text, **kw)


def switch(text="B step"):
    return tools.update_plan.func("[ ] " + text, explanation="用户换了目标", new_task=True)


def texts(s, task=None):
    return [r["text"] for r in ts.task_requests(s, task)]


def saved_json(isolated_memory, sid="b05-review"):
    return json.loads((isolated_memory / f"{sid}.json").read_text(encoding="utf-8"))


# ── 1. 任务边界 ──

def test_new_task_supplements_do_not_pull_old_requests_back(sess):
    user(sess, "A: 重构登录")
    plan()
    user(sess, "A 补充：别动数据库")
    b = user(sess, "B: 加导出")
    switch()
    assert sess.current_task["request_message_ids"] == [b.id]
    user(sess, "B 补充：CSV")
    assert texts(sess) == ["B: 加导出", "B 补充：CSV"]


def test_archived_task_does_not_keep_the_switch_trigger(sess):
    a = user(sess, "A: 重构登录")
    plan()
    a2 = user(sess, "A 补充")
    b = user(sess, "B: 加导出")
    switch()
    archived = sess.archived_tasks[0]
    assert archived["request_message_ids"] == [a.id, a2.id]
    assert b.id not in sess.last_task_switch["previous_task"]["request_message_ids"]


# ── 2. 来源不被追认 ──

@pytest.mark.parametrize("entry", ["create", "sync", "switch"])
def test_legacy_message_never_retagged_as_user_input(sess, isolated_memory, entry):
    legacy = memory._dict_to_msg({"type": "HumanMessage", "content": "【完成闸门】你改了 app.py 但没有运行测试"})
    sess.chat_history.append(legacy)
    if entry == "create":
        plan()
    else:
        sess.current_task = ts.create_task()
        if entry == "sync":
            ts.sync_user_requests(sess)
        else:
            switch()
    memory.save_session(session=sess)
    raw = (isolated_memory / "b05-review.json").read_text(encoding="utf-8")
    assert '"lingxi_kind": "user_input"' not in raw
    assert ts.get_message_kind(legacy) == "legacy_unknown"


def test_legacy_request_kept_but_labelled_unconfirmed(sess):
    legacy = memory._dict_to_msg({"type": "HumanMessage", "content": "旧会话里的原始要求"})
    sess.chat_history.append(legacy)
    plan()
    rows = ts.task_requests(sess)
    assert [r["kind"] for r in rows] == ["legacy_unknown"]
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "来源未确认" in ctx


def test_supplements_only_accept_verified_user_input(sess):
    user(sess, "真实要求")
    plan()
    sess.chat_history.append(HumanMessage(content="未打标的消息"))
    gate = ts.tag_internal_message(HumanMessage(content="闸门提示"), kind="gate")
    sess.chat_history.append(gate)
    ts.sync_user_requests(sess)
    assert texts(sess) == ["真实要求"]


def test_request_source_kind_survives_reload(sess, isolated_memory):
    legacy = memory._dict_to_msg({"type": "HumanMessage", "content": "旧要求"})
    sess.chat_history.append(legacy)
    plan()
    user(sess, "新补充")
    memory.save_session(session=sess)
    other = session.Session()
    assert memory.load_session("b05-review", session=other)
    assert [r["kind"] for r in ts.task_requests(other)] == ["legacy_unknown", "user_input"]


# ── 3. 撤销不误删、不丢任务 ──

def test_undo_only_removes_its_own_archive_and_keeps_new_task(sess):
    sess.archived_tasks = [{"id": "task-unrelated", "request_message_ids": []}]
    a = user(sess, "A")
    plan()
    b = user(sess, "B")
    switch()
    b_id = sess.current_task["id"]
    a_id = sess.last_task_switch["previous_task"]["id"]
    ok, _ = ts.undo_task_switch(sess, b_id, sess.last_task_switch["switch_version"])
    assert ok
    assert sess.current_task["id"] == a_id
    assert [t["id"] for t in sess.archived_tasks] == ["task-unrelated", b_id]
    assert sess.archived_tasks[-1]["archive_kind"] == "undone_switch"
    # 撤销意味着 B 不是新任务：它的要求并入 A 作为补充
    assert sess.current_task["request_message_ids"] == [a.id, b.id]


def test_undo_after_new_supplement_keeps_it(sess):
    user(sess, "A")
    plan()
    user(sess, "B")
    switch()
    c = user(sess, "B 的补充")
    ts.undo_task_switch(sess, *(sess.last_task_switch[k] for k in ("new_task_id", "switch_version")))
    assert c.id in sess.current_task["request_message_ids"]
    # 撤销后再发的消息仍归当前任务，不会被认成已归档任务的
    d = user(sess, "撤销后再补充")
    assert d.id in sess.current_task["request_message_ids"]


def test_undo_of_old_format_record_without_previous_task(sess):
    sess.archived_tasks = [{"id": "task-unrelated", "request_message_ids": []}]
    sess.current_task = ts.create_task(task_id="task-new")
    sess.last_task_switch = {"new_task_id": "task-new", "switch_version": 1, "previous_task": None,
                             "previous_plan": []}
    ok, msg = ts.undo_task_switch(sess, "task-new", 1)
    assert not ok and "无需撤销" in msg
    assert sess.last_task_switch is None
    assert sess.current_task["id"] == "task-new"
    assert [t["id"] for t in sess.archived_tasks] == ["task-unrelated"]


def test_undone_archive_survives_reload(sess, isolated_memory):
    user(sess, "A")
    plan()
    user(sess, "B")
    switch()
    ts.undo_task_switch(sess, *(sess.last_task_switch[k] for k in ("new_task_id", "switch_version")))
    memory.save_session(session=sess)
    other = session.Session()
    assert memory.load_session("b05-review", session=other)
    assert other.progress_error == ""
    assert [t["archive_kind"] for t in other.archived_tasks] == ["undone_switch"]


# ── 4. 撤销屏障 ──

def _ui_host(executed=None, *, wait_limit=3):
    from src.ui.chat_window import ChatUI

    class Host:
        _RESUME_WAIT_MS = 100
        _RESUME_WAIT_LIMIT = wait_limit
        _on_undo_task_switch = ChatUI._on_undo_task_switch
        _wait_worker_and_undo_switch = ChatUI._wait_worker_and_undo_switch
        _undo_switch_abort = ChatUI._undo_switch_abort
        # B09a：撤销入口现在会先核对有没有待启动请求、忙碌判据走 _worker_alive，
        # 借入真实实现
        _worker_alive = ChatUI._worker_alive
        _pending_runs_by_session = ChatUI._pending_runs_by_session
        _pending_run_for = ChatUI._pending_run_for

        def __init__(self):
            self.messages, self.toasts, self.stops = [], [], 0

        def show_message(self, text, tag="system"):
            self.messages.append((text, tag))

        def _show_toast(self, text, duration=1500):
            self.toasts.append(text)

        def _force_stop_generation(self, wait=False):
            self.stops += 1
            session.get_active().is_generating = False
            return True

        def _execute_undo_task_switch(self, *args):
            if executed is not None:
                executed.append(args)
            else:
                ChatUI._execute_undo_task_switch(self, *args)

    return Host()


@pytest.fixture
def timers(monkeypatch):
    """把 QTimer.singleShot 换成手动推进：测试里一步一步地走屏障。

    生产代码的 singleShot 现在带 receiver 上下文（窗口销毁时自动取消回调），
    桩按 (ms, receiver, fn) 三参接收——receiver 在这里无需记录，只收集回调。
    """
    from src.ui import chat_window
    pending = []
    monkeypatch.setattr(chat_window, "QTimer",
                        SimpleNamespace(singleShot=lambda ms, receiver, fn: pending.append(fn)))
    return pending


def _switched(sess):
    user(sess, "A")
    plan()
    user(sess, "B")
    switch()


def test_barrier_times_out_without_undoing(sess, timers):
    _switched(sess)
    executed = []
    host = _ui_host(executed, wait_limit=2)
    sess.last_worker = SimpleNamespace(is_alive=lambda: True)
    host._on_undo_task_switch()
    assert sess.resume_pending is True
    while timers:
        timers.pop(0)()
    assert executed == []
    assert sess.resume_pending is False
    assert host.toasts and "没有执行" in host.toasts[-1]


def test_barrier_stops_generation_and_undoes_after_worker_exits(sess, timers):
    _switched(sess)
    executed = []
    host = _ui_host(executed)
    alive = {"v": True}
    sess.last_worker = SimpleNamespace(is_alive=lambda: alive["v"])
    sess.is_generating = True
    host._on_undo_task_switch()
    assert host.stops == 1 and sess.resume_pending is True and executed == []
    alive["v"] = False
    timers.pop(0)()
    assert len(executed) == 1
    assert sess.resume_pending is False


def test_barrier_abandons_when_foreground_changes(sess, timers):
    _switched(sess)
    executed = []
    host = _ui_host(executed)
    sess.last_worker = SimpleNamespace(is_alive=lambda: True)
    host._on_undo_task_switch()
    session.set_active(session.Session())
    timers.pop(0)()
    assert executed == []
    assert sess.resume_pending is False
    assert "取消" in host.toasts[-1]


def test_barrier_refuses_while_another_wait_is_pending(sess, timers):
    _switched(sess)
    executed = []
    host = _ui_host(executed)
    sess.resume_pending = True
    host._on_undo_task_switch()
    assert executed == [] and not timers
    assert sess.resume_pending is True     # 不替别人的等待放行


# ── 5. 验证状态只陈述有证据的事实 ──

def _panel_text(sess):
    from src.ui.task_panel import TaskPanel
    panel = TaskPanel(None, lambda key: "#eeeeee")
    try:
        panel.render_session(sess)
        return panel.verification_status_label.text()
    finally:
        panel.close()


def test_panel_without_evidence_is_not_green(sess, app):
    user(sess, "A")
    plan()
    text = _panel_text(sess)
    assert "通过" not in text
    assert "没有验证证据" in text


def test_panel_blind_period_is_shown(sess, app):
    user(sess, "A")
    plan()
    sess.pending_verification["tracking_incomplete"] = {"C:/unknown": "unobserved"}
    text = _panel_text(sess)
    assert "盲区" in text and "通过" not in text


def test_panel_passed_evidence_says_only_what_ran(sess, app):
    from src import verification
    user(sess, "A")
    plan()
    verification.record_evidence(sess.verification, kind="tests", status="passed", exit_code=0,
                                 argv=["pytest"], cwd="C:/p")
    text = _panel_text(sess)
    assert "已执行的检查通过（1 项）" in text and "不代表" in text


def test_panel_stale_evidence_does_not_count(sess, app):
    from src import verification
    user(sess, "A")
    plan()
    verification.record_evidence(sess.verification, kind="tests", status="passed", exit_code=0,
                                 argv=["pytest"], cwd="C:/p")
    sess.verification["change_revision"] += 1
    assert ts.verification_status(sess)["state"] == "none"


def test_blind_period_reaches_model_context(sess):
    user(sess, "A")
    plan()
    sess.pending_verification["tracking_incomplete"] = {"C:/unobserved-root": "UNKNOWN_PERIOD_42"}
    text = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "盲区" in text and "UNKNOWN_PERIOD_42" in text


# ── 6. 上下文预算 ──

def _ctx(sess, max_chars=3000):
    return "\n\n".join(ts.format_task_volatile_context(sess, max_chars=max_chars))


def test_huge_latest_supplement_stays_within_budget(sess):
    user(sess, "initial")
    plan()
    last = user(sess, "x" * 20000)
    text = _ctx(sess)
    assert len(text) <= 3000
    assert last.id in text and "已折叠" in text
    assert "initial" in text


def test_budget_holds_with_full_constraints_and_everything_long(sess):
    user(sess, "原" * 5000)
    plan()
    for i in range(12):
        user(sess, f"补充{i}：" + "y" * 900)
    sess.current_task["pinned_constraints"] = ["限" * 99] * 10     # 990 字符，贴近上限
    sess.current_task["last_plan_change_reason"] = "r" * 1000
    sess.pending_verification["code_files"] = [f"src/f{i}.py" for i in range(20)]
    sess.pending_verification["tracking_incomplete"] = {f"C:/r{i}": "z" * 300 for i in range(6)}
    text = _ctx(sess)
    assert len(text) <= 3000
    assert text.count("限" * 99) == 10          # 固定限制一字不少


def test_middle_supplements_are_shown_not_just_counted(sess):
    user(sess, "原始")
    plan()
    user(sess, "中间一")
    user(sess, "中间二")
    user(sess, "最新")
    text = _ctx(sess)
    for t in ("原始", "中间一", "中间二", "最新"):
        assert t in text
    assert text.index("原始") < text.index("中间一") < text.index("中间二") < text.index("最新")


@pytest.mark.parametrize("budget", [300, 800, 1500, 3000])
def test_budget_is_a_hard_ceiling(sess, budget):
    user(sess, "a" * 4000)
    plan()
    for _ in range(5):
        user(sess, "b" * 1200)
    assert len(_ctx(sess, budget)) <= budget


# ── 7. 不编造摘要覆盖 ──

def test_missing_original_is_reported_as_missing(sess):
    rows = ts.extract_task_requests([], ["msg-gone"])
    assert rows[0]["kind"] == "missing"
    assert "压缩摘要中覆盖" not in rows[0]["text"]
    assert "已不在" in rows[0]["text"]


def test_missing_reference_survives_sync(sess):
    user(sess, "仍在历史里")
    plan()
    sess.current_task["request_message_ids"].insert(0, "msg-compacted-away")
    ts.sync_user_requests(sess)
    assert sess.current_task["request_message_ids"][0] == "msg-compacted-away"


# ── 8. 固定限制保持原文 ──
# 最终契约（复验裁定）：按输入原文保存，不解析、不剥除任何行首字符——项目符号只
# 用于显示层。早先"首次保存剥一层 `- `"的旧预期（连同前两轮探针里的对应断言）
# 一并废弃：只要还在根据内容猜哪些字符属于用户，改一个句尾、敲两层横线，字面量
# 就会丢。打开→保存→重开→修改→再存，用户写下的字符不得增删。

@pytest.mark.parametrize("raw, expected", [
    ("--force 禁止使用", ["--force 禁止使用"]),
    ("-- 分隔符后的参数不许改", ["-- 分隔符后的参数不许改"]),
    ("*.env 文件不提交", ["*.env 文件不提交"]),
    ("- 列表项\n* 星号项\n• 圆点项\n1. 编号项",
     ["- 列表项", "* 星号项", "• 圆点项", "1. 编号项"]),
    ("- * 必须作为字面参数传入，不允许展开", ["- * 必须作为字面参数传入，不允许展开"]),
    ("- - 必须作为字面参数传入，不允许替换", ["- - 必须作为字面参数传入，不允许替换"]),
    ("- 必须作为字面参数传入", ["- 必须作为字面参数传入"]),
    ("* 必须作为字面参数传入", ["* 必须作为字面参数传入"]),
    ("1. 禁止删除测试\n10. 禁止改接口", ["1. 禁止删除测试", "10. 禁止改接口"]),
])
def test_constraints_preserve_user_text(raw, expected):
    ok, err, cleaned = ts.validate_pinned_constraints(raw)
    assert ok, err
    assert cleaned == expected


def test_constraints_reopen_resave_roundtrip_is_identical():
    """不解析即天然可逆：任何输入保存后，重开（不改）再存逐字一致。"""
    for raw in ("- * 必须作为字面参数传入，不允许展开",
                "- - 必须作为字面参数传入，不允许替换",
                "- 必须作为字面参数传入",
                "* 必须作为字面参数传入",
                "--force 禁止使用"):
        ok, err, stored = ts.validate_pinned_constraints(raw)
        assert ok, err
        assert stored == [raw]
        # 对话框重开时把存储条目按行回填（不添加项目符号），再保存走同一校验
        ok2, err2, again = ts.validate_pinned_constraints("\n".join(stored))
        assert ok2, err2
        assert again == stored


def test_edited_suffix_keeps_every_character():
    """已保存的条目改句尾后保存：整行按原文落盘，任何前缀字符不得丢失。"""
    stored = ["- * 这两个参数必须按顺序原样传入"]
    edited = stored[0] + "，包括导入命令"
    ok, err, cleaned = ts.validate_pinned_constraints("\n".join(stored + [edited]))
    assert ok, err
    assert cleaned == stored + [edited]


# ── 9. 任务与计划原子更新（真实存盘，控制线程交错）──

@pytest.mark.parametrize("initial", [False, True])
def test_save_cannot_observe_half_task_plan(sess, isolated_memory, initial):
    user(sess, "A")
    if not initial:
        plan()
        user(sess, "B")
    old_plan = copy.deepcopy(sess.current_plan)
    old_id = (sess.current_task or {}).get("id")
    reached, release = threading.Event(), threading.Event()
    errors = []
    target = ts.create_or_attach_task if initial else ts.switch_to_new_task
    source, first = inspect.getsourcelines(target)
    plan_line = next(first + i for i, line in enumerate(source)
                     if line.strip().startswith("sess.current_plan ="))

    def trace(frame, event, arg):
        if frame.f_code is target.__code__ and event == "line" and frame.f_lineno == plan_line:
            reached.set()
            release.wait(5)
        return trace

    data_dir = str(isolated_memory.parent)

    def writer():
        paths.set_data_dir(data_dir)
        session.bind_thread(sess)
        sys.settrace(trace)
        try:
            plan("first valid plan") if initial else switch()
        except BaseException as exc:   # noqa: BLE001 - 线程里的异常要带回主线程
            errors.append(exc)
        finally:
            sys.settrace(None)
            session.unbind_thread()

    def saver():
        paths.set_data_dir(data_dir)
        try:
            memory.save_session(session=sess)
        except BaseException as exc:   # noqa: BLE001
            errors.append(exc)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=saver)
    w.start()
    try:
        assert reached.wait(5), "writer 没走到计划赋值那一行"
        r.start()
        r.join(0.5)          # 能在这段时间里存完，就说明它没被快照锁挡住——那才是问题
    finally:
        release.set()
        w.join(5)
        r.join(5)
    assert not errors, errors
    p = saved_json(isolated_memory)["progress"]
    assert not (p["task"] and p["task"]["id"] != old_id and p["current_plan"] == old_plan), p


# ── 10. 保存失败要看得见 ──

def test_undo_save_failure_is_reported(sess, monkeypatch):
    from src.ui.chat_window import ChatUI
    _switched(sess)

    def fail(**kwargs):
        raise OSError("DISK_SAVE_FAILED")

    # 在正文写盘这一层失败（调用方改用 save_session_report，替换 save_session 已拦不住它）
    monkeypatch.setattr(memory, "_atomic_write_json", lambda *a, **k: fail())
    shown = []
    host = SimpleNamespace(show_message=lambda *a: shown.append(a))
    ChatUI._execute_undo_task_switch(host, sess, sess.last_task_switch["new_task_id"],
                                     sess.last_task_switch["switch_version"])
    text = str(shown)
    assert "保存失败" in text and "DISK_SAVE_FAILED" in text
    assert shown[-1][1] == "error"


def test_constraints_save_failure_rejected_and_rolled_back(sess, app, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    plan()
    sess.current_task["pinned_constraints"] = ["原有限制"]

    def fail(**kwargs):
        raise OSError("DISK_SAVE_FAILED")

    # 在正文写盘这一层失败（调用方改用 save_session_report，替换 save_session 已拦不住它）
    monkeypatch.setattr(memory, "_atomic_write_json", lambda *a, **k: fail())
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText("新限制")
        dialog._on_save()
        assert dialog.result() != QDialog.Accepted
        assert "保存失败" in dialog.char_count_label.text()
        assert sess.current_task["pinned_constraints"] == ["原有限制"]
    finally:
        dialog.close()


def test_constraints_save_failure_does_not_leave_a_new_task(sess, app, monkeypatch):
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    sess.current_plan = [{"text": "旧格式计划", "status": "pending"}]   # 有计划、没任务
    monkeypatch.setattr(memory, "_atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText("新限制")
        dialog._on_save()
        assert sess.current_task is None
    finally:
        dialog.close()


# ── 11. 首次计划不是切换 ──

def test_first_plan_with_new_task_is_plain_creation(sess):
    user(sess, "第一件事")
    out = tools.update_plan.func("[ ] 第一步", explanation="开始任务", new_task=True)
    assert sess.current_task is not None
    assert sess.last_task_switch is None
    assert sess.archived_tasks == []
    assert "第一步" in out


def test_empty_plan_cannot_start_task(sess):
    user(sess, "请求")
    out = tools.update_plan.func("", explanation="开始任务", new_task=True)
    assert sess.current_task is None
    assert "至少需要一个计划步骤" in out


def test_empty_plan_cannot_switch_task(sess):
    user(sess, "A")
    plan()
    tid = sess.current_task["id"]
    out = tools.update_plan.func("", explanation="换任务", new_task=True)
    assert sess.current_task["id"] == tid and sess.last_task_switch is None
    assert "至少需要一个计划步骤" in out


# ── P3 ──

def test_missing_created_at_stays_unknown():
    raw = ts.create_task()
    raw["created_at"] = None
    normalized, why = ts.normalize_task(raw)
    assert why == "" and normalized["created_at"] == ""


def test_unknown_internal_kind_is_rejected():
    with pytest.raises(ValueError):
        ts.tag_internal_message(HumanMessage(content="x"), kind="typo-not-a-kind")


def test_every_production_internal_kind_is_accepted():
    for kind in ("resume", "recovery", "repair", "gate", "vision_bridge"):
        assert ts.get_message_kind(ts.tag_internal_message(HumanMessage(content="x"), kind=kind)) == kind


def test_describe_source_kind_contract():
    plain = HumanMessage(content="hello")
    tagged = ts.tag_user_message(HumanMessage(content="hello"))
    legacy = memory._dict_to_msg({"type": "HumanMessage", "content": "hello"})
    got = [run_records.describe_source([m]) for m in (plain, tagged, legacy)]
    assert [g["kind"] for g in got] == ["user_message", "user_input", "legacy_unknown"]
    assert [g["verified_user"] for g in got] == [False, True, False]


def test_provider_ai_id_is_persisted_but_never_user_input():
    data = memory._msg_to_dict(AIMessage(content="hi", id="provider-run-123"))
    assert data["id"] == "provider-run-123"
    assert not ts.is_any_user_input(memory._dict_to_msg(data))


# ══════════════════════════════════════════════════════════════
# 第二轮复核
# ══════════════════════════════════════════════════════════════

# ── R2-1. 对话框编辑期间任务变了，不能把限制写到别的任务上 ──

def test_constraints_dialog_refuses_after_task_switch(sess, app):
    from PySide6.QtWidgets import QDialog
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    plan()
    a_id = sess.current_task["id"]
    dialog = EditConstraintsDialog(None, sess)
    try:
        user(sess, "B")
        switch()                                   # worker 在对话框打开期间切了任务
        dialog.editor.setPlainText("A 专属：不要改数据库")
        dialog._on_save()
        assert dialog.result() != QDialog.Accepted
        assert "任务已变化" in dialog.char_count_label.text()
        assert sess.current_task["pinned_constraints"] == []
        a_archive = next(t for t in sess.archived_tasks if t["id"] == a_id)
        assert a_archive["pinned_constraints"] == []
        # 用户确认后再点一次：存到现在的任务上，且是明确的第二次操作
        dialog._on_save()
        assert sess.current_task["pinned_constraints"] == ["A 专属：不要改数据库"]
    finally:
        dialog.close()


def test_constraints_dialog_refuses_when_task_appeared(sess, app):
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    dialog = EditConstraintsDialog(None, sess)      # 打开时还没有任务
    try:
        plan()                                      # 模型建立了首个任务
        tid = sess.current_task["id"]
        dialog.editor.setPlainText("限制")
        dialog._on_save()
        assert sess.current_task["id"] == tid
        assert sess.current_task["pinned_constraints"] == []
    finally:
        dialog.close()


def test_constraints_dialog_same_task_saves_first_time(sess, app, isolated_memory):
    from PySide6.QtWidgets import QDialog
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    plan()
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText("保持 API 兼容")
        dialog._on_save()
        assert dialog.result() == QDialog.Accepted
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == ["保持 API 兼容"]
    finally:
        dialog.close()


# ── R2-2. 正文已落盘、只有索引失败：不回滚，也不说成没保存 ──

def _index_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("INDEX_LOCKED")
    monkeypatch.setattr(memory, "_update_index", boom)


def test_constraints_body_written_index_failed_keeps_constraints(sess, app, isolated_memory, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "A")
    plan()
    memory.save_session(session=sess)
    _index_fails(monkeypatch)
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText("禁止 force push")
        dialog._on_save()
        disk = saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"]
        assert disk == ["禁止 force push"]
        assert sess.current_task["pinned_constraints"] == disk     # 内存与磁盘一致
        assert dialog.result() == QDialog.Accepted
        assert "INDEX_LOCKED" in dialog.save_warning and "已保存" in dialog.save_warning
    finally:
        dialog.close()


def test_undo_body_written_index_failed_not_reported_as_unsaved(sess, isolated_memory, monkeypatch):
    from src.ui.chat_window import ChatUI
    _switched(sess)
    memory.save_session(session=sess)
    a_id = sess.last_task_switch["previous_task"]["id"]
    _index_fails(monkeypatch)
    shown = []
    host = SimpleNamespace(show_message=lambda *a: shown.append(a))
    ChatUI._execute_undo_task_switch(host, sess, sess.last_task_switch["new_task_id"],
                                     sess.last_task_switch["switch_version"])
    assert saved_json(isolated_memory)["progress"]["task"]["id"] == a_id
    text = str(shown)
    assert "已保存" in text and "INDEX_LOCKED" in text
    assert "尚未写入磁盘" not in text


# ── R2-3. 单纯推进进度不抹掉调整原因、不涨版本 ──

def test_progress_keeps_last_adjustment_reason(sess):
    user(sess, "A")
    tools.update_plan.func("[ ] a1\n[ ] a2")
    tools.update_plan.func("[ ] a1\n[ ] a2\n[ ] a3", explanation="发现需要迁移数据库")
    task = sess.current_task
    ver = task["plan_version"]
    summary = task["last_plan_change_summary"]
    tools.update_plan.func("[x] a1\n[ ] a2\n[ ] a3")
    assert sess.current_plan[0]["status"] == "done"
    assert task["last_plan_change_reason"] == "发现需要迁移数据库"
    assert task["last_plan_change_summary"] == summary
    assert task["plan_version"] == ver


def test_progress_with_explanation_is_still_not_structural(sess):
    user(sess, "A")
    tools.update_plan.func("[ ] a1\n[ ] a2")
    ver = sess.current_task["plan_version"]
    tools.update_plan.func("[x] a1\n[ ] a2", explanation="第一步做完了")
    assert sess.current_task["plan_version"] == ver


def test_regression_and_structure_changes_still_recorded(sess):
    user(sess, "A")
    tools.update_plan.func("[x] a1\n[ ] a2")
    ver = sess.current_task["plan_version"]
    tools.update_plan.func("[ ] a1\n[ ] a2", explanation="a1 返工")
    assert sess.current_task["plan_version"] == ver + 1
    assert sess.current_task["last_plan_change_reason"] == "a1 返工"
    tools.update_plan.func("[ ] a1", explanation="a2 不做了")
    assert sess.current_task["plan_version"] == ver + 2
    assert "移除 1 步" in sess.current_task["last_plan_change_summary"]


# ── R2-4. 撤销后马上重做切换，触发消息要回到新任务 ──

def test_redo_switch_after_undo_takes_trigger_back(sess):
    user(sess, "A")
    plan()
    user(sess, "B 新目标")
    switch()
    sw = sess.last_task_switch
    ok, _ = ts.undo_task_switch(sess, sw["new_task_id"], sw["switch_version"])
    assert ok and "B 新目标" in texts(sess)          # 撤销后并回原任务
    switch()
    assert texts(sess) == ["B 新目标"]
    a_archive = [t for t in sess.archived_tasks if t.get("archive_kind") == "switched"][-1]
    assert "B 新目标" not in texts(sess, a_archive)


# ── R2-5. 清空计划不建立任务 ──

def test_clearing_plan_without_task_does_not_create_one(sess):
    user(sess, "A")
    sess.current_plan = [{"text": "旧格式计划", "status": "pending"}]
    out = tools.update_plan.func("", explanation="清空")
    assert sess.current_plan == []
    assert sess.current_task is None
    assert "已清空" in out


# ── R2-6. 跑过但没有结论，不说成"尚未执行检查" ──

@pytest.mark.parametrize("status", ["not_run", "cancelled", "unknown"])
def test_panel_inconclusive_evidence_is_not_called_never_run(sess, app, status):
    from src import verification
    user(sess, "A")
    plan()
    verification.record_evidence(sess.verification, kind="tests", status=status,
                                 argv=["pytest"], cwd="C:/p")
    text = _panel_text(sess)
    assert "尚未执行检查" not in text
    assert "没有得到通过结论" in text
    assert "已执行的检查通过" not in text


# ══════════════════════════════════════════════════════════════
# 12. 复验后续（第三轮独立复验的 3 个问题）
#
# 一、已解除的验证义务仍被报为"待验证"：dirty_files / 盲区只是"本轮涉及过"的记录，
#     义务是否仍未解除复用 verification.gaps_from_state 判定；运行中以运行态实时
#     计算，不拿上一次持久化快照当依据。回归走真实 run_tests / git_diff。
# 二、固定限制原样重存丢字符：按输入原文保存，不解析、不剥除任何行前缀——项目
#     符号只用于显示层（见第 8 节的最终契约）。
# 三、任务上下文突破 3000 且挤掉目标：对最终实际输出计数；固定限制完整保留、目标
#     及来源保底展示空间、装不下的段显式注明省略。
# ══════════════════════════════════════════════════════════════

from src.agent_result import AgentResult          # noqa: E402
from test_b04_recovery import _git, _make_repo    # noqa: E402


def _real_repo_session(sess, monkeypatch, tmp_path, name="real-project"):
    """真实 git 仓库 + 会话锚定到它（同时设会话级与全局 current_project，同 GUI 现场）。"""
    root = _make_repo(tmp_path / name)
    (root / "test_app.py").write_text("def test_app():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    _git(root, "add", "test_app.py")
    _git(root, "commit", "-qm", "add real passing test")
    monkeypatch.setattr(state, "current_project", str(root))
    sess.project = str(root)
    sess.shell_cwd = str(root)
    return root


@pytest.mark.parametrize("obligation", ["changed_file", "blind_period"])
def test_discharged_obligations_are_not_reported_still_pending(sess, isolated_memory, tmp_path, monkeypatch, obligation):
    """文件修改 / 盲区 → 真实跑测试 + 看 diff → 缺口解除后，面板与模型上下文都不再要求重复验证。"""
    from src import verification
    root = _real_repo_session(sess, monkeypatch, tmp_path)
    user(sess, "verify the change")
    plan()
    run = run_records.begin_run(sess)
    if obligation == "changed_file":
        (root / "app.py").write_text("x = 2\n", encoding="utf-8")
        verification.mark_dirty(sess.verification, "app.py", abs_path=str(root / "app.py"))
    else:
        verification.mark_blind_period(sess.verification, str(root), "prior tracking gap")
    tools.run_tests.func(path="test_app.py")
    tools.git_diff.func()
    assert sess.verification["tests_passed"] is True
    assert verification.gaps_from_state(sess.verification) == []
    run_records.finalize_run(sess, run, AgentResult("completed"))
    assert sess.last_run["outcome"] == "completed"
    assert not sess.pending_verification["files"]
    assert not sess.pending_verification["tracking_incomplete"]

    st = ts.verification_status(sess)
    assert st["state"] == "checked", {"gaps": st["gaps"], "pending": sess.pending_verification}
    # 显示通过不靠删除记录：改动记录、盲区记录与检查证据都还在
    if obligation == "changed_file":
        assert sess.verification["dirty_files"] == ["app.py"]
        assert sess.verification["dirty_abs"], "位置记录保留"
    else:
        assert sess.verification["unknown_changes"], "盲区记录保留"
    assert sess.verification["evidence"], "检查证据保留"
    # 面板与模型上下文使用同一结论：不再出现待完成验证的要求，也不把记录当义务
    assert "待完成的验证" not in _panel_text(sess)
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "尚未解决的验证义务" not in ctx, ctx


def test_failed_check_keeps_obligation_visible(sess, isolated_memory, tmp_path, monkeypatch):
    """检查失败：义务保留，面板与上下文都指出要修复。"""
    from src import verification
    root = _real_repo_session(sess, monkeypatch, tmp_path)
    user(sess, "verify the change")
    plan()
    run_records.begin_run(sess)
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    verification.mark_dirty(sess.verification, "app.py", abs_path=str(root / "app.py"))
    (root / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n", encoding="utf-8")
    tools.run_tests.func(path="test_bad.py")
    assert sess.verification["tests_passed"] is False
    st = ts.verification_status(sess)
    assert st["state"] in ("pending", "blind") and st["gaps"], st
    assert "待完成的验证" in _panel_text(sess)
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "尚未解决的验证义务" in ctx


def test_uncovered_location_keeps_obligation(sess, isolated_memory, tmp_path, monkeypatch):
    """义务所在目录属于另一个 Git 工作区（嵌套仓库）：主仓的测试与 diff 不解除它。"""
    from src import verification
    root = _real_repo_session(sess, monkeypatch, tmp_path)
    nested = _make_repo(root / "nested")           # 独立 .git：主仓测试/diff 都覆盖不到
    user(sess, "verify the change")
    plan()
    run_records.begin_run(sess)
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    verification.mark_dirty(sess.verification, "app.py", abs_path=str(root / "app.py"))
    (nested / "mod.py").write_text("y = 3\n", encoding="utf-8")
    verification.mark_dirty(sess.verification, "nested/mod.py", abs_path=str(nested / "mod.py"))
    tools.run_tests.func(path="test_app.py")
    tools.git_diff.func()
    st = ts.verification_status(sess)
    assert st["gaps"], "主仓的检查不得解除嵌套工作区里的义务"
    assert any("nested" in g for g in st["gaps"]), st["gaps"]
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "nested" in ctx


def test_edit_after_checks_reopens_obligation_and_stales_evidence(sess, isolated_memory, tmp_path, monkeypatch):
    """检查通过后再次修改：义务回来，先前的通过证据作废（历史成功不是通行凭证）。"""
    from src import verification
    root = _real_repo_session(sess, monkeypatch, tmp_path)
    user(sess, "verify the change")
    plan()
    run_records.begin_run(sess)
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    verification.mark_dirty(sess.verification, "app.py", abs_path=str(root / "app.py"))
    tools.run_tests.func(path="test_app.py")
    tools.git_diff.func()
    assert ts.verification_status(sess)["state"] == "checked"
    # 之后又改了同一个文件
    verification.mark_dirty(sess.verification, "app.py", abs_path=str(root / "app.py"))
    st = ts.verification_status(sess)
    assert st["gaps"] and st["state"] in ("pending", "blind"), st
    assert st["passed"] == 0, "改动之后的旧通过证据不能继续充当通过结论"


def test_persisted_snapshot_is_the_only_clue_before_run_state_exists(sess, isolated_memory, monkeypatch):
    """运行态还是全新的（刚加载、恢复未并入）时，义务以持久化快照为准，且不会显示"已验证"。

    begin_run 把快照并回运行态后，结论仍由运行态实时判定给出，两边不断裂。
    """
    user(sess, "A")
    plan()
    sess.pending_verification["files"] = ["src/left.py"]
    sess.pending_verification["tracking_incomplete"] = {"C:/unobserved": "blind period"}
    # 运行态仍是全新（未 begin_run）：快照是唯一线索
    st = ts.verification_status(sess)
    assert st["state"] in ("pending", "blind") and st["gaps"], st
    assert any("src/left.py" in g for g in st["gaps"]), st["gaps"]
    assert any("C:/unobserved" in g for g in st["gaps"]), st["gaps"]
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "src/left.py" in ctx and "C:/unobserved" in ctx

    # begin_run 之后快照并入运行态（mark_dirty 打回未验证）：仍未解除，结论一致
    run_records.begin_run(sess)
    assert sess.verification["dirty_files"], "快照义务应并入运行态"
    st2 = ts.verification_status(sess)
    assert st2["state"] in ("pending", "blind") and st2["gaps"], st2
    assert "待完成的验证" in _panel_text(sess)


def test_snapshot_obligations_reach_panel_and_context(sess, app):
    user(sess, "A")
    plan()
    sess.pending_verification["tracking_incomplete"] = {"C:/unobserved-root": "UNKNOWN_PERIOD_42"}
    assert "待完成的验证" in _panel_text(sess)
    ctx = "\n\n".join(ts.format_task_volatile_context(sess))
    assert "C:/unobserved-root" in ctx and "UNKNOWN_PERIOD_42" in ctx


# ── 12.2 固定限制：对话框真实保存往返 ──

def test_constraints_dialog_roundtrip_preserves_literal_star(sess, app, isolated_memory):
    """真实对话框：打开 → 保存 → 重开（不改）→ 再存，内容逐字一致且落盘一致。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "literal shell arguments")
    plan()
    raw = "- * 必须作为字面参数传入，不允许展开"
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText(raw)
        dialog._on_save()
        expected = [raw]
        assert sess.current_task["pinned_constraints"] == expected
    finally:
        dialog.close()
    reopened = EditConstraintsDialog(None, sess)
    try:
        assert reopened.editor.toPlainText() == expected[0]
        reopened._on_save()                     # 不做任何修改再保存
        saved = saved_json(isolated_memory)
        assert saved["progress"]["task"]["pinned_constraints"] == expected
    finally:
        reopened.close()


def test_constraints_dialog_roundtrip_preserves_flags_and_numbers(sess, app, isolated_memory):
    """--force、字面量 *、数字开头：打开、保存、重开、再存全部保持原文。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "cli flags")
    plan()
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText("--force 禁止使用\n* 必须作为字面参数传入\n1. 数字编号也是原文")
        dialog._on_save()
        expected = ["--force 禁止使用", "* 必须作为字面参数传入", "1. 数字编号也是原文"]
        assert sess.current_task["pinned_constraints"] == expected
    finally:
        dialog.close()
    reopened = EditConstraintsDialog(None, sess)
    try:
        assert reopened.editor.toPlainText() == "\n".join(expected)
        reopened._on_save()
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == expected
    finally:
        reopened.close()


@pytest.mark.parametrize("argument", ["-", "*"])
def test_dialog_resave_preserves_literal_argument(sess, app, isolated_memory, argument):
    """字面量 `-` / `*` 开头的限制：按输入原文落盘，重开不改再存逐字一致。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "keep literal arguments")
    plan()
    raw = f"- {argument} 必须作为字面参数传入，不允许替换"
    first = EditConstraintsDialog(None, sess)
    try:
        first.editor.setPlainText(raw)
        first._on_save()
        expected = [raw]
        assert sess.current_task["pinned_constraints"] == expected
    finally:
        first.close()
    again = EditConstraintsDialog(None, sess)
    try:
        assert again.editor.toPlainText() == expected[0]
        again._on_save()
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == expected
    finally:
        again.close()


@pytest.mark.parametrize("argument", ["-", "*"])
def test_dialog_edited_suffix_keeps_leading_literal(sess, app, isolated_memory, argument):
    """已保存的条目改句尾后保存：整行按原文落盘，开头的字面量字符不得丢失。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "preserve literal command arguments")
    plan()
    first = EditConstraintsDialog(None, sess)
    try:
        first.editor.setPlainText(f"- {argument} 必须作为字面参数传入，不允许替换")
        first._on_save()
        expected = [f"- {argument} 必须作为字面参数传入，不允许替换"]
        assert sess.current_task["pinned_constraints"] == expected
    finally:
        first.close()
    reopened = EditConstraintsDialog(None, sess)
    try:
        assert reopened.editor.toPlainText() == expected[0]
        edited = expected[0] + "，包括导入命令"
        reopened.editor.setPlainText(edited)
        reopened._on_save()
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == [edited]
    finally:
        reopened.close()


@pytest.mark.parametrize("raw", [
    "- * 这两个参数必须按顺序原样传入",
    "- - 这两个参数必须按顺序原样传入",
    "- 必须作为字面参数传入",
    "* 必须作为字面参数传入",
    "--force 禁止使用",
])
def test_dialog_saves_the_advertised_verbatim_input(sess, app, isolated_memory, raw):
    """界面承诺按原文保存：输入什么落盘什么（镜像复验探针的五种输入形状）。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "keep literal arguments")
    plan()
    dialog = EditConstraintsDialog(None, sess)
    try:
        dialog.editor.setPlainText(raw)
        dialog._on_save()
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == [raw]
    finally:
        dialog.close()


def test_dialog_edit_existing_multi_symbol_prefix_keeps_all_characters(sess, app, isolated_memory):
    """已存的"- * 参数"只改句尾再保存：所有字符原样保留（镜像复验探针）。"""
    from src.ui.task_panel import EditConstraintsDialog
    user(sess, "keep literal arguments")
    plan()
    raw = "- * 这两个参数必须按顺序原样传入"
    sess.current_task["pinned_constraints"] = [raw]
    memory.save_session(session=sess)
    dialog = EditConstraintsDialog(None, sess)
    try:
        assert dialog.editor.toPlainText() == raw
        edited = raw + "，包括导入命令"
        dialog.editor.setPlainText(edited)
        dialog._on_save()
        assert saved_json(isolated_memory)["progress"]["task"]["pinned_constraints"] == [edited]
    finally:
        dialog.close()


# ── 12.3 任务上下文硬预算：固定限制完整、目标来源可见 ──

def _ctx_text(sess, max_chars=3000):
    return "\n\n".join(ts.format_task_volatile_context(sess, max_chars=max_chars))


@pytest.mark.parametrize("case", ["many_short_constraints", "long_paths_and_blind_periods"])
def test_fixed_sections_obey_hard_budget(sess, case):
    """独立复现的两个场景：合法固定限制 + 各种长内容，最终输出 ≤ 3000 且目标来源仍可见。"""
    user(sess, "the actual goal")
    plan()
    original_id = sess.current_task["request_message_ids"][0]
    if case == "many_short_constraints":
        raw = "\n".join(["禁"] * 1000)
    else:
        raw = "必须保留兼容性" * 120          # 840 字符
        sess.pending_verification["files"] = ["src/" + "a" * 180 + str(i) + ".py" for i in range(8)]
        sess.pending_verification["tracking_incomplete"] = {
            f"C:/root{i}/" + "b" * 140: "tracking problem " * 12 for i in range(4)}
    valid, why, constraints = ts.validate_pinned_constraints(raw)
    assert valid, why
    sess.current_task["pinned_constraints"] = constraints
    text = _ctx_text(sess)
    assert len(text) <= 3000, {"chars": len(text)}
    assert "the actual goal" in text, "任务目标被挤掉"
    assert f"`{original_id}`" in text, "目标来源（消息 ID）不可见"


def test_budget_keeps_constraints_complete_and_marks_omissions(sess):
    """长义务说明装不下时：折叠并注明省略了什么，补读入口指向真实面板与工具；限制一字不少。"""
    user(sess, "原" * 5000)
    plan()
    for i in range(12):
        user(sess, f"补充{i}：" + "y" * 900)
    sess.current_task["pinned_constraints"] = ["限" * 99] * 10
    sess.current_task["last_plan_change_reason"] = "r" * 1000
    sess.pending_verification["code_files"] = [f"src/f{i}.py" for i in range(20)]
    sess.pending_verification["tracking_incomplete"] = {f"C:/r{i}": "z" * 300 for i in range(6)}
    text = _ctx_text(sess)
    assert len(text) <= 3000
    assert text.count("限" * 99) == 10, "已接受的固定限制不能在渲染时被裁掉"
    assert "未逐条展开" in text, "义务被折叠时要注明省略了什么"
    assert "run_tests" in text and "git_diff" in text, "补读提示必须是真实可用的工具"
    assert "原" * 10 in text, "原始要求至少保留开头（保底空间）"


@pytest.mark.parametrize("count,size", [(1000, 1), (20, 50), (1, 1000)])
@pytest.mark.parametrize("source", ["verified", "legacy"])
def test_goal_reserve_keeps_original_goal_with_long_supplement(sess, source, count, size):
    """原始目标是保底中的保底：固定限制、多处盲区、长计划说明再挤，其正文与消息 ID 不可缺席。

    每种（来源 × 固定限制形态）扫 6 档盲区数量 × 7 档盲区说明长度 × 2 档计划原因
    长度 = 84 种预算组合，逐一检查总长 ≤3000 且原始目标的正文与来源可见。
    legacy 来源（旧会话未打标消息，头部带来源注记）同样适用。
    """
    text = "ORIGINAL_GOAL: build a csv exporter. " * 20
    if source == "legacy":
        from langchain_core.messages import HumanMessage
        original = HumanMessage(content=text)
        sess.chat_history.append(original)
    else:
        original = user(sess, text)
    plan()
    original_id = sess.current_task["request_message_ids"][0]
    if source == "legacy":
        assert ts.get_message_kind(original) == "legacy_unknown"
        assert original_id == ts.get_message_id(original)
    user(sess, "LATEST_SUPPLEMENT: keep the coding style. " * 20)
    ts.sync_user_requests(sess)
    ok, reason, constraints = ts.validate_pinned_constraints("\n".join(["约" * size] * count))
    assert ok, reason
    sess.current_task["pinned_constraints"] = constraints
    cases = 0
    for root_count in range(1, 7):
        for reason_size in [0, 50, 100, 150, 200, 250, 300]:
            sess.pending_verification["tracking_incomplete"] = {
                f"C:/pending/root{i}": "观察失败" + "x" * reason_size for i in range(root_count)}
            for plan_size in [0, 300]:
                sess.current_task["last_plan_change_reason"] = "p" * plan_size
                context = _ctx_text(sess)
                cases += 1
                assert len(context) <= 3000, (source, count, size, root_count, reason_size, plan_size, len(context))
                assert original_id in context and "ORIGINAL_GOAL" in context, (
                    f"原始目标被挤掉: source={source}, constraints=({count},{size}), roots={root_count}, "
                    f"reason_size={reason_size}, plan_size={plan_size}, chars={len(context)}, "
                    f"latest_visible={'LATEST_SUPPLEMENT' in context}")
    assert cases == 84


def test_goal_reserve_compresses_verification_and_plan_first(sess):
    """保底挤不下时先压缩验证说明与计划解释，而不是丢原始目标。"""
    original = user(sess, "ORIGINAL_GOAL: keep this visible. " * 10)
    plan()
    user(sess, "LATEST: " + "y" * 2000)
    ts.sync_user_requests(sess)
    sess.current_task["pinned_constraints"] = ["约" * 50] * 20
    sess.pending_verification["tracking_incomplete"] = {
        f"C:/pending/root{i}": "观察失败" + "x" * 300 for i in range(6)}
    sess.current_task["last_plan_change_reason"] = "p" * 300
    text = _ctx_text(sess)
    assert len(text) <= 3000
    assert original.id in text and "ORIGINAL_GOAL" in text
    # 目标在场时，被压缩的义务说明必须留下交代，不能整段消失
    assert "尚未解决的验证义务" in text
    assert "未逐条展开" in text
