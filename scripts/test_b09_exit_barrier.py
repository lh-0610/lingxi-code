"""B09a 统一退出屏障：停止后启动下一轮，必须等旧 worker 真正退出。

回答三个问题：
1. **同会话不双开**——is_generating 被强制停止立刻置 False，但旧线程还在收尾
   （执行工具 / 存盘 / 写结束记录）；这时发送、重试、遥控、继续都不得启动新线程。
2. **等待不丢输入**——待启动请求在正式接纳前，文字与附件原样保留；接纳成功只清
   实际发送的那份（等待期间的新编辑、新附件一律不动）；超时 / 取消 / 失效时如实
   说明未发送。
3. **等待非阻塞**——轮询走带 receiver 上下文的 QTimer（窗口销毁自动取消），
   不在主线程 join；超时不强行放行，旧线程还在跑这一事实不被掩盖。

全部用例走真实 ChatUI 入口 + Qt 事件循环 + 可控假 worker（threading.Event），
不碰真实模型 / CLI / Telegram。
"""
import base64
import gc
import os
import threading
import time
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QEvent

from src import agent, memory, result_view, run_records, session, state
from src.agent_result import AgentResult
from src.ui.chat_window import ChatUI


# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    """任何用例都不许碰真实模型、本地 Claude CLI 或网络。"""
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


@pytest.fixture(autouse=True)
def _memory_write_guard(monkeypatch, isolated_memory):
    """所有 chat_memory 写入（会话正文 / index / sidecar）都必须落在隔离数据根。

    paths.set_data_dir 是线程本地的：isolated_memory 只覆盖主线程。worker 里做
    真实存盘的用例若忘记在线程入口设置数据根，这里当场失败，而不是把测试会话
    写进真实 chat_memory（实测一次用例越界写正文 + index 共 6 次）。
    """
    import src.paths as _paths
    from src import memory as _memory
    expected = os.path.normcase(str(_paths.memory_dir()))
    bad = []
    real_write = _memory._atomic_write_json

    def _spy_write(path, *a, **k):
        d = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
        if d != expected:
            bad.append(d)
        return real_write(path, *a, **k)

    monkeypatch.setattr(_memory, "_atomic_write_json", _spy_write)
    yield
    assert not bad, f"chat_memory 写入越界 {len(bad)} 次: {sorted(set(bad))[:3]}"


@contextmanager
def _worker_data_root(root):
    """工作线程入口用：把主线程取好的隔离数据根带进线程本地，退出时还原。

    paths.set_data_dir 是线程本地的——isolated_memory 只覆盖主线程，worker 里
    做真实存盘（恢复检查 / begin / finalize / save_session）必须自己带根。
    root 必须在【主线程】启动线程之前用 paths.get_data_dir() 取好再传进来：
    进了线程再取，拿到的就是默认根（等于没隔离）。
    """
    import src.paths as _paths
    _paths.set_data_dir(root)
    try:
        yield
    finally:
        _paths.set_data_dir(None)


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


def _wait_until(app, pred, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture()
def fake_loop(monkeypatch):
    """可控假 agent_loop：每轮卡在自己的 gate 上，模拟"worker 一直在跑 / 收尾"。"""
    calls = []

    def _loop(ui, *, resume=None):
        gate = threading.Event()
        rec = {"thread": threading.current_thread(), "gate": gate,
               "session": session.current_session(), "resume": resume}
        calls.append(rec)
        gate.wait(10)
        return AgentResult("completed")

    monkeypatch.setattr(agent, "agent_loop", _loop)
    return calls


@pytest.fixture()
def ui(qapp, monkeypatch, isolated_memory, fake_loop):
    """完整真实 ChatUI：模型 / MCP / 网络全部离线；屏障等待提速（20ms 一跳）。"""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("Model/network access is prohibited in barrier tests")

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

    from src.ui.chat_window import ChatUI as _ChatUI
    ui = _ChatUI()
    ui._RESUME_WAIT_MS = 20          # 实例属性遮蔽类属性：等待提速，超时语义不变
    yield ui
    if state.ui_ref is ui:
        state.ui_ref = None
    _destroy_ui_cleanly(ui, qapp)


def _destroy_ui_cleanly(ui, app):
    """完整清理序列（同 test_sidebar_load_more）：先放完零秒回调再真正销毁。

    屏障的轮询定时器带 receiver 上下文，窗口销毁即自动取消——这里销毁后继续
    放事件、放行旧 worker，验证不发生"打到已删除控件"的异常。
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


def _toasts(ui):
    """把 toast 换成可读记录（实例属性遮蔽真实弹窗）。"""
    if not hasattr(ui, "_toast_log"):
        ui._toast_log = []
        ui._show_toast = lambda text, duration=1500: ui._toast_log.append(str(text))
    return ui._toast_log


def _type(ui, text):
    ui.entry.setPlainText(text)
    ui._check_input_state()


_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _add_image(ui, tmp_path, name):
    path = os.path.join(str(tmp_path), name)
    with open(path, "wb") as f:
        f.write(_PNG_1PX)
    ui._add_pending_image(path)
    return path


def _history_texts(sess):
    out = []
    for m in sess.chat_history:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.append("".join(b.get("text", "") for b in c if isinstance(b, dict)))
    return out


def _plant_finishing_worker(sess):
    """手工摆出"已停止但旧 worker 还在收尾"的状态：is_generating=False、线程活着。"""
    release = threading.Event()
    worker = threading.Thread(target=release.wait, args=(10,), daemon=True)
    worker.start()
    sess.last_worker = worker
    sess.is_generating = False
    return worker, release


def _make_saved_session(sess):
    """按生产语义把会话落盘拿到 id：真实环境里历史以 SystemMessage + 用户消息开头，
    首轮发送的 save 必然发生。conftest 的全新 active 会话历史是**空列表**——不先补
    消息，save_session 会按"历史过短"跳过、会话永远没有 id，重置/重载核对无从谈起。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    if len(sess.chat_history) <= 1:
        sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="开场")]
    memory.save_session(session=sess)
    assert sess.current_session_id
    return sess


# ══════════════════════════════════════════════════════════════
# 核心：停止后发送要等旧 worker 真正退出
# ══════════════════════════════════════════════════════════════

class TestSendWaitsForWorkerExit:

    def test_send_after_stop_waits_then_starts_exactly_once(self, ui, qapp, fake_loop):
        """停止 → is_generating=False 但旧 worker 卡在收尾 → 发送排队等待；
        旧 worker 退出后新请求恰好执行一次。"""
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        worker1 = fake_loop[0]["thread"]
        sess = session.get_active()
        assert sess.is_generating is True

        ui._on_send_click()                     # 点停止：非阻塞，立刻恢复
        assert sess.is_generating is False
        assert worker1.is_alive(), "旧 worker 还卡在收尾，线程应当活着"

        _type(ui, "second")
        ui._on_send_click()                     # 立刻再发：只能排队
        _pump(qapp, 0.3)
        assert len(fake_loop) == 1, "旧 worker 没退出前不能开新一轮"
        assert ui.entry.toPlainText() == "second", "等待接纳期间输入必须保留"
        assert not any("second" in t for t in _history_texts(sess)), "未接纳不进历史"
        req = ui._pending_run_for(sess)
        assert req is not None and req.source == "send" and req.text == "second"

        fake_loop[0]["gate"].set()
        worker1.join(5)
        assert not worker1.is_alive()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2), "退出后新请求必须执行"
        _pump(qapp, 0.3)
        assert len(fake_loop) == 2, "连点 / 重复回调不能造成第二次启动"
        assert ui.entry.toPlainText() == "", "接纳成功才清理输入"
        assert _history_texts(sess).count("second") == 1
        assert ui._pending_run_for(sess) is None

    def test_enter_while_still_generating_is_rejected_not_queued(self, ui, qapp, fake_loop):
        """仍正常生成时按 Enter 发送：明确拒绝，不排队（排队只属于停止后的收尾窗口）。"""
        _toasts(ui)
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()

        _type(ui, "趁生成中再发一条")
        ui._send_message()
        _pump(qapp, 0.2)
        assert len(fake_loop) == 1
        assert ui._pending_run_for(sess) is None, "正常生成中不新增排队"
        assert ui.entry.toPlainText() == "趁生成中再发一条"
        assert any("生成中" in t for t in _toasts(ui))

        fake_loop[0]["gate"].set()
        _pump(qapp, 0.3)

    def test_double_click_queues_one_request_only(self, ui, qapp, fake_loop):
        """收尾窗口里连点发送：只有一条待启动请求，退出后恰好一轮。"""
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        ui._on_send_click()                     # 停止
        _toasts(ui)
        _type(ui, "second")
        ui._on_send_click()                     # 排队
        ui._on_send_click()                     # 重复点击
        ui._on_send_click()
        _pump(qapp, 0.3)
        sess = session.get_active()
        assert len(fake_loop) == 1
        assert any("未重复登记" in t for t in _toasts(ui))
        fake_loop[0]["gate"].set()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        _pump(qapp, 0.3)
        assert len(fake_loop) == 2
        assert _history_texts(sess).count("second") == 1

    def test_finished_signal_before_thread_exit_still_waits(self, ui, qapp, fake_loop):
        """finished 信号先到、线程后退出：仍要等线程退出，不得带病启动。"""
        sess = session.get_active()
        worker, release = _plant_finishing_worker(sess)
        _type(ui, "hello")
        ui._send_message()                      # 旧 worker 活着 → 排队
        _pump(qapp, 0.2)
        assert ui._pending_run_for(sess) is not None

        ui._on_finished_sess(sess, None)        # 模拟 finished 已投递、线程还活着
        _pump(qapp, 0.3)
        assert fake_loop == [], "线程没退出就不能启动"
        assert ui._pending_run_for(sess) is not None, "回到轮询继续等，不丢请求"

        release.set()
        worker.join(5)
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        _pump(qapp, 0.2)
        assert len(fake_loop) == 1

    def test_late_finished_of_old_run_does_not_disturb_new_run(self, ui, qapp, fake_loop):
        """旧 worker 的迟到 finished：不解除新运行的忙碌状态。"""
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        old_token = sess.worker_token

        ui._on_send_click()                     # 停止
        _type(ui, "second")
        ui._on_send_click()                     # 排队
        fake_loop[0]["gate"].set()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        assert sess.is_generating is True, "新一轮在跑"

        ui.bridge.finished.emit(sess, old_token)   # 旧 worker 的迟到完成信号
        _pump(qapp, 0.3)
        assert sess.is_generating is True, "迟到信号不能解除新运行的忙碌"
        assert fake_loop[1]["gate"] is not None

        fake_loop[1]["gate"].set()
        _pump(qapp, 0.3)

    def test_worker_thread_registered_before_target_runs(self, ui, qapp, fake_loop,
                                                         monkeypatch):
        """P1 复现：Thread.start() 之后、线程真正进入 target 之前，线程身份必须已经
        登记——否则这个窗口里点停止清掉 is_generating，再发送就绕过屏障双开。"""
        from src import session as _session_mod
        entered = []
        block = threading.Event()

        def _stub_run_agent(sess=None, resume=None):
            # 卡在 target 的最前面：模拟"线程已启动、还没执行任何登记"
            entered.append(threading.current_thread())
            block.wait(10)
            tok = object()
            sess.worker_token = tok
            _session_mod.bind_thread(sess)
            try:
                pass
            finally:
                _session_mod.unbind_thread()
                ui.bridge.finished.emit(sess, tok)

        monkeypatch.setattr(ui, "_run_agent", _stub_run_agent)
        _make_saved_session(session.get_active())

        _type(ui, "first")
        ui._on_send_click()
        sess = session.get_active()
        thread1 = sess.last_worker
        assert thread1 is not None, "start() 返回后线程身份必须已登记，屏障才看得到这个窗口"
        assert thread1.is_alive()
        assert sess.is_generating is True

        ui._on_send_click()                     # 停止：is_generating 被立刻清掉
        assert sess.is_generating is False
        assert thread1.is_alive(), "线程还没退出（卡在 target 第一行）"

        _type(ui, "second")
        ui._on_send_click()                     # 这个窗口里再发送：只能排队
        _pump(qapp, 0.3)
        assert len(fake_loop) == 0, "第一条线程还活着，绝不能已经双开第二轮"
        assert len(entered) == 1
        assert ui._pending_run_for(sess) is not None, "窗口里的发送要排队等退出"
        assert sess.last_worker is thread1

        block.set()                             # 线程走完 target → finished → 接纳
        assert _wait_until(qapp, lambda: len(entered) == 2), "退出后新请求恰好一次"
        _pump(qapp, 0.3)
        assert len(entered) == 2, "重复回调不能造成第二次启动"
        assert ui._pending_run_for(sess) is None
        assert ui.entry.toPlainText() == ""


# ══════════════════════════════════════════════════════════════
# 输入保留：等待不清空；接纳只清实际发送的那份；超时保留
# ══════════════════════════════════════════════════════════════

class TestInputPreserved:

    def test_timeout_keeps_input_and_starts_nothing(self, ui, qapp, fake_loop):
        """等待超时：不启动新 worker、不假装旧运行结束，输入原样保留、之后可重试。"""
        _toasts(ui)
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        ui._on_send_click()                     # 停止 → 收尾窗口
        _type(ui, "重要消息")
        ui._send_message()                      # 排队
        ui._RESUME_WAIT_LIMIT = 2               # 两跳就超时
        _pump(qapp, 0.4)

        assert len(fake_loop) == 1, "超时不能强行放行"
        assert ui._pending_run_for(sess) is None, "超时要释放本次等待占用"
        assert ui.entry.toPlainText() == "重要消息", "输入必须保留，可稍后重试"
        assert not any("重要消息" in t for t in _history_texts(sess))
        assert any("未自动发送" in t for t in _toasts(ui))
        assert fake_loop[0]["thread"].is_alive(), "旧线程仍在运行的事实不被掩盖"

        # 释放后允许之后重试：旧 worker 退出，再点发送即可正常发出
        fake_loop[0]["gate"].set()
        fake_loop[0]["thread"].join(5)
        _pump(qapp, 0.4)
        assert len(fake_loop) == 1, "超时撤回的请求不会被补发"
        ui._send_message()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        fake_loop[1]["gate"].set()
        _pump(qapp, 0.3)
        assert _history_texts(sess).count("重要消息") == 1

    def test_admission_clears_only_what_was_sent(self, ui, qapp, fake_loop, tmp_path,
                                                 monkeypatch):
        """等待期间改草稿 / 加附件：启动后只清理实际发送的那份。"""
        monkeypatch.setattr(agent, "current_model_supports_vision", lambda: True)
        _type(ui, "hello")
        _add_image(ui, tmp_path, "a.png")   # 第一轮的附件，不在清理断言之列
        ui._send_message()                  # 第一轮直接发出（含 hello + a.png）
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        ui._on_send_click()                     # 停止 → 收尾窗口

        _type(ui, "second")
        img2 = _add_image(ui, tmp_path, "b.png")
        ui._send_message()                      # 排队（second + b.png）
        _pump(qapp, 0.2)
        # 等待期间继续编辑草稿、再加一张图
        ui.entry.setPlainText("second 加了后缀")
        img3 = _add_image(ui, tmp_path, "c.png")

        fake_loop[0]["gate"].set()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        _pump(qapp, 0.3)

        sent = _history_texts(sess)
        assert any(t.startswith("second\n") or t == "second" or "second" in t for t in sent), \
            "发出的是登记时的快照"
        assert not any("加了后缀" in t for t in sent), "等待期间的新编辑不属于这份请求"
        assert ui.entry.toPlainText() == "second 加了后缀", "新编辑保留"
        assert [p for p, _ in ui._pending_images] == [img3], "新加的附件保留"
        assert img2 not in [p for p, _ in ui._pending_images], "实际发送的那份被清理"

    def test_input_untouched_when_queue_is_cancelled_by_switch(self, ui, qapp, fake_loop):
        """等待中切走会话：请求取消、不后台发送、输入留在输入框。"""
        _toasts(ui)
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        ui._on_send_click()                     # 停止
        _type(ui, "还没发出去的话")
        ui._send_message()
        _pump(qapp, 0.2)
        assert ui._pending_run_for(sess) is not None

        other = session.Session()
        ui._activate_session(other)             # 真实切会话入口（取消钩子在这里）
        _pump(qapp, 0.2)
        assert ui._pending_run_for(sess) is None, "切走即明确取消"
        assert any("已取消" in t for t in _toasts(ui))
        assert ui.entry.toPlainText() == "还没发出去的话", "原输入有清楚的保留位置"

        fake_loop[0]["gate"].set()              # 旧 worker 退出也不得在后台发
        worker = fake_loop[0]["thread"]
        worker.join(5)
        _pump(qapp, 0.5)
        assert len(fake_loop) == 1, "不把输入发到别处"


# ══════════════════════════════════════════════════════════════
# 失效与拒绝：会话重置 / 项目漂移 / 删除会话 / 各入口不可绕过
# ══════════════════════════════════════════════════════════════

class TestInvalidation:

    def _queued_request(self, ui, qapp, fake_loop, text="待发消息"):
        _make_saved_session(session.get_active())
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        ui._on_send_click()                     # 停止 → 收尾窗口
        _toasts(ui).clear()
        _type(ui, text)
        ui._send_message()
        _pump(qapp, 0.2)
        assert ui._pending_run_for(sess) is not None
        return sess, fake_loop[0]

    def test_session_reset_invalidates_pending_at_admission(self, ui, qapp, fake_loop):
        """等待中会话被重置（角色卡切换等回收 Session 对象的路径）：接纳时核对出
        会话已不是原来那个，拒绝发送、不把输入塞进新对话。"""
        sess, rec = self._queued_request(ui, qapp, fake_loop)
        agent.reset_history()                   # 模拟重置（header 钩子之外的安全网）
        _pump(qapp, 0.1)

        rec["gate"].set()
        rec["thread"].join(5)
        _pump(qapp, 0.5)
        assert len(fake_loop) == 1, "会话已重置，不能把旧请求发进去"
        assert any("重置" in t or "已取消" in t for t in _toasts(ui))
        assert ui._pending_run_for(sess) is None

    def test_project_change_invalidates_pending_at_admission(self, ui, qapp, fake_loop):
        """等待中项目归属被换掉：接纳时核对失败，不把消息发到换了项目的会话。"""
        sess, rec = self._queued_request(ui, qapp, fake_loop)
        sess.project = "D:/别处/另一个项目"      # 模拟搬家 / 归属迁移

        rec["gate"].set()
        rec["thread"].join(5)
        _pump(qapp, 0.5)
        assert len(fake_loop) == 1
        assert any("项目归属" in t for t in _toasts(ui))
        assert ui.entry.toPlainText() == "待发消息"

    def test_delete_session_cancels_pending(self, ui, qapp, fake_loop):
        """删除会话：挂着的待启动请求一并取消。"""
        sess, rec = self._queued_request(ui, qapp, fake_loop)
        assert sess.current_session_id
        ui._delete_session(sess.current_session_id)   # 真实删除入口（钩子在 UI 层）
        _pump(qapp, 0.2)
        assert ui._pending_run_for(sess) is None, "会话没了，请求必须取消"
        assert any("已取消" in t for t in _toasts(ui))
        rec["gate"].set()
        rec["thread"].join(5)
        _pump(qapp, 0.3)
        assert len(fake_loop) == 1

    def test_worktree_change_invalidates_pending_at_admission(self, ui, qapp, fake_loop,
                                                              tmp_path):
        """等待期间隔离区换了（项目根没动）：接纳时核对实际工作目录，拒绝发送。"""
        sess, rec = self._queued_request(ui, qapp, fake_loop)
        sess.worktree = str(tmp_path / "另一个隔离区")     # 项目归属没变、工作目录变了

        rec["gate"].set()
        rec["thread"].join(5)
        _pump(qapp, 0.5)
        assert len(fake_loop) == 1, "工作目录已换，不能把消息发进新隔离区"
        assert any("工作目录" in t for t in _toasts(ui))
        assert ui.entry.toPlainText() == "待发消息"

    def test_delete_background_session_after_stop_waits_for_finalize(self, ui, qapp,
                                                                     monkeypatch,
                                                                     isolated_memory):
        """P1 复现：停止后 is_generating 已 False，删除不得跳过等待——否则旧 worker
        收尾时的 save_session 会把已删会话连正文带索引原样存回来。"""
        from src.ui.chat_window import ChatUI
        from src import agent as _agent
        from src import paths as _paths

        release = threading.Event()

        def _loop(ui_, *, resume=None):
            with _worker_data_root(data_root):
                release.wait(10)                    # 模拟卡在工具里
                memory.save_session(session=session.current_session())  # 真实收尾保存
                return AgentResult("completed")

        monkeypatch.setattr(agent, "agent_loop", _loop)
        data_root = _paths.get_data_dir()       # 主线程取根（isolated_memory 已设置）
        # join 超时缩到 0.2s：屏障语义不变，测试不等 3 秒
        ui._stop_session_generation = lambda s, wait=False, timeout=3.0: \
            ChatUI._stop_session_generation(ui, s, wait=wait, timeout=0.2)

        sess = _make_saved_session(session.get_active())
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        assert path.exists()

        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: bool(agent.list_sessions("__all__")))
        ui._on_send_click()                     # 真实停止：标志清了、线程还活着
        worker = sess.last_worker
        assert worker is not None and worker.is_alive()

        ui._activate_session(session.Session())  # 切走：旧会话变后台
        assert session.get(sid) is sess

        _toasts(ui)
        ui._delete_session(sid)                 # 此时删除：必须等不到就放弃
        _pump(qapp, 0.2)
        assert path.exists(), "旧线程还活着，正文不得被删"
        assert session.get(sid) is sess, "旧线程还活着，删除必须整体放弃"
        assert any("删除未执行" in t for t in _toasts(ui))

        release.set()                           # 旧 worker 收尾：真实 save_session
        worker.join(5)
        _pump(qapp, 0.5)
        assert path.exists(), "删除被放弃后，收尾保存照常发生（没有被删一半的状态）"

        ui._delete_session(sid)                 # 线程已退出，现在删除才能落地
        _pump(qapp, 0.3)
        assert not path.exists()
        assert all(s["id"] != sid for s in _agent.list_sessions("__all__"))
        release.set()
        _pump(qapp, 0.5)
        assert not path.exists(), "线程已死，删除后不可能复活"

    def test_delete_active_session_after_stop_waits_for_finalize(self, ui, qapp, monkeypatch,
                                                                 isolated_memory):
        """活动会话同判据：停止后立即删活动会话，也要等旧线程真正退出。"""
        from src.ui.chat_window import ChatUI

        monkeypatch.setattr(agent, "agent_loop", lambda ui_, *, resume=None:
                            AgentResult("completed"))
        ui._force_stop_generation = lambda wait=False, timeout=3.0: \
            ChatUI._force_stop_generation(ui, wait=wait, timeout=0.2)

        sess = _make_saved_session(session.get_active())
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"

        stuck = threading.Event()
        worker = threading.Thread(target=stuck.wait, args=(10,), daemon=True)
        worker.start()
        sess.last_worker = worker
        sess.is_generating = False              # 已停止、线程还在收尾

        _toasts(ui)
        ui._delete_session(sid)
        _pump(qapp, 0.2)
        assert path.exists(), "旧线程活着，删除必须放弃"
        assert session.get(sid) is sess
        assert any("删除未执行" in t for t in _toasts(ui))

        stuck.set()
        worker.join(5)
        _pump(qapp, 0.3)
        ui._delete_session(sid)
        _pump(qapp, 0.3)
        assert not path.exists()
        _pump(qapp, 0.3)
        assert not path.exists()

    def test_retry_cannot_bypass_the_barrier(self, ui, qapp, fake_loop):
        """重试在正常生成中 / 收尾窗口都被拒，且明确反馈。"""
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()

        ui._on_retry()
        _pump(qapp, 0.2)
        assert len(fake_loop) == 1 and ui._pending_run_for(sess) is None

        ui._on_send_click()                     # 停止 → 收尾窗口（is_generating=False）
        _toasts(ui).clear()
        ui._on_retry()
        _pump(qapp, 0.2)
        assert len(fake_loop) == 1, "旧 worker 没退出前重试不得开新一轮"
        assert any("重试" in t for t in _toasts(ui)), "拒绝要有明确反馈"
        assert ui._pending_run_for(sess) is None, "重试不排队"

        fake_loop[0]["gate"].set()
        _pump(qapp, 0.3)

    def test_remote_is_queued_not_dropped_and_bound_to_its_session(self, ui, qapp, fake_loop):
        """遥控注入：收尾窗口排队等待（不静默丢弃），归属在接收时确定。"""
        notes = []
        ui._notify_remote_unaccepted = lambda msg: notes.append(str(msg))
        sess, rec = self._queued_request_remote(ui, qapp, fake_loop)
        assert ui._pending_run_for(sess) is not None
        assert any("将在" in n for n in notes), "排队等待也要给手机端回执"

        rec["gate"].set()
        rec["thread"].join(5)
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        _pump(qapp, 0.3)
        assert fake_loop[1]["session"] is sess, "归属在接收时确定，不随后台漂移"
        assert any("remote-msg" in t for t in _history_texts(sess))
        assert session.get_active().remote_session is True

        fake_loop[1]["gate"].set()
        _pump(qapp, 0.3)

    def _queued_request_remote(self, ui, qapp, fake_loop):
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()
        ui._on_send_click()                     # 停止 → 收尾窗口
        ui.submit_from_remote("remote-msg")     # 从"遥控线程"投递（信号跨线程）
        assert _wait_until(qapp, lambda: ui._pending_run_for(sess) is not None)
        return sess, fake_loop[0]

    def test_remote_while_generating_gets_explicit_feedback(self, ui, qapp, fake_loop):
        """正常生成中遥控注入：明确回执，不静默丢弃，也不排队。"""
        notes = []
        ui._notify_remote_unaccepted = lambda msg: notes.append(str(msg))
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        sess = session.get_active()

        ui.submit_from_remote(" remote ")
        _pump(qapp, 0.3)
        assert len(fake_loop) == 1
        assert ui._pending_run_for(sess) is None
        assert any("生成中" in n for n in notes), "手机端必须收到未发送的回执"

        fake_loop[0]["gate"].set()
        _pump(qapp, 0.3)

    def test_remote_double_submit_single_pending(self, ui, qapp, fake_loop):
        """重复遥控注入：只有一条待启动请求，第二次明确回执。"""
        notes = []
        ui._notify_remote_unaccepted = lambda msg: notes.append(str(msg))
        sess, rec = self._queued_request_remote(ui, qapp, fake_loop)
        ui.submit_from_remote("remote-2")
        _pump(qapp, 0.3)
        assert len(fake_loop) == 1
        assert any("已有一条" in n for n in notes)
        assert ui._pending_run_for(sess).text == "remote-msg"

        rec["gate"].set()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2)
        _pump(qapp, 0.3)
        assert _history_texts(sess).count("remote-msg") == 1
        assert not any("remote-2" in t for t in _history_texts(sess))
        fake_loop[1]["gate"].set()
        _pump(qapp, 0.3)

    def test_remote_immediate_send_failure_notifies_phone(self, ui, qapp, fake_loop,
                                                          monkeypatch):
        """立即发送分支失败同样要有手机端回执（与等待后接纳的分支一致）。"""
        import types
        from src.ui import chat_window as cw

        class _BrokenThread:
            def __init__(self, *a, **k):
                raise RuntimeError("cannot start new thread")

        monkeypatch.setattr(cw, "threading", types.SimpleNamespace(
            Thread=_BrokenThread,
            current_thread=threading.current_thread,
            Event=threading.Event))
        notes = []
        ui._notify_remote_unaccepted = lambda msg: notes.append(str(msg))
        sess = _make_saved_session(session.get_active())

        ui.submit_from_remote("remote-immediate")
        _pump(qapp, 0.3)
        assert fake_loop == [], "线程没起来就没有新一轮"
        assert ui._pending_run_for(sess) is None
        assert any("发送失败" in n for n in notes), "立即发送失败也要给手机端回执"

    def test_continue_task_is_refused_while_a_send_is_pending(self, ui, qapp, fake_loop):
        """「继续任务」与待发送消息互斥：先处理消息，不允许两轮同时被接纳。"""
        sess, rec = self._queued_request(ui, qapp, fake_loop)
        run = run_records.begin_run(sess)
        report = run_records.finalize_run(sess, run, AgentResult("cancelled", "用户停止"))
        view = result_view.describe(report.snapshot)

        ui._on_result_continue(view)
        _pump(qapp, 0.2)
        assert len(fake_loop) == 1
        assert sess.resume_pending is False, "被拒绝时不得占用屏障"
        assert any("等待" in t for t in _toasts(ui))

        rec["gate"].set()
        rec["thread"].join(5)
        _pump(qapp, 0.4)
        assert len(fake_loop) == 2, "消息先发出，之后才能继续"


# ══════════════════════════════════════════════════════════════
# 会话独立与启动失败
# ══════════════════════════════════════════════════════════════

class TestSessionIndependenceAndSpawnFailure:

    def test_session_b_runs_while_session_a_is_finishing(self, ui, qapp, fake_loop):
        """A 会话收尾等待时，B 会话照常发送、照常运行（不加全应用级互斥）。"""
        _type(ui, "first")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 1)
        ui._on_send_click()                     # A 停止 → 旧 worker 还在收尾

        other = session.Session()
        ui._activate_session(other)
        _type(ui, "B 会话的消息")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(fake_loop) == 2), \
            "A 的收尾不得阻塞 B 的新运行"
        assert fake_loop[1]["session"] is other
        assert fake_loop[0]["thread"].is_alive(), "A 的旧 worker 此刻还没退"

        fake_loop[1]["gate"].set()
        fake_loop[0]["gate"].set()
        _pump(qapp, 0.4)

    def test_spawn_failure_reports_honestly_and_keeps_input(self, ui, qapp, fake_loop,
                                                            monkeypatch):
        """新 worker 启动失败：如实反馈、不留半截历史、输入仍可重试。"""
        import types
        from src.ui import chat_window as cw

        class _BrokenThread:
            def __init__(self, *a, **k):
                raise RuntimeError("cannot start new thread")

        fake_threading = types.SimpleNamespace(Thread=_BrokenThread,
                                               current_thread=threading.current_thread,
                                               Event=threading.Event)
        monkeypatch.setattr(cw, "threading", fake_threading)

        _toasts(ui)
        _type(ui, "只此一份的草稿")
        ui._on_send_click()
        _pump(qapp, 0.3)
        sess = session.get_active()
        assert fake_loop == [], "线程没起来就没有新一轮"
        assert ui.entry.toPlainText() == "只此一份的草稿", "输入保留，可以重试"
        assert sess.is_generating is False, "启动失败不能把会话卡在生成中"
        assert any("发送失败" in t for t in _toasts(ui))
        assert not any("只此一份的草稿" in t for t in _history_texts(sess)), \
            "半截历史要撤掉，重发不重复"


    def test_spawn_failure_leaves_no_trace_on_disk_or_task(self, ui, qapp, fake_loop,
                                                           monkeypatch):
        """P2 复现：启动失败后磁盘上不能留着"从未发送"的消息，任务要求引用也要撤回。"""
        import types
        from langchain_core.messages import AIMessage, SystemMessage
        from src import memory as _memory
        from src.ui import chat_window as cw

        class _BrokenThread:
            def __init__(self, *a, **k):
                raise RuntimeError("cannot start new thread")

        monkeypatch.setattr(cw, "threading", types.SimpleNamespace(
            Thread=_BrokenThread,
            current_thread=threading.current_thread,
            Event=threading.Event))

        sess = session.get_active()
        sess.chat_history = [SystemMessage(content="sys"), AIMessage(content="旧回复")]
        memory.save_session(session=sess)
        sid = sess.current_session_id
        sess.current_task = {"request_message_ids": [], "request_sources": {}}

        _toasts(ui)
        _type(ui, "只此一份的草稿")
        ui._on_send_click()
        _pump(qapp, 0.3)

        assert fake_loop == [], "线程没起来就没有新一轮"
        assert ui.entry.toPlainText() == "只此一份的草稿", "输入保留，可以重试"
        assert not any("只此一份的草稿" in t for t in _history_texts(sess)), "内存历史撤回"
        assert sess.current_task["request_message_ids"] == [], "任务要求引用一并撤回"

        other = session.Session()
        assert _memory.load_session(sid, session=other)
        assert not any("只此一份的草稿" in t for t in _history_texts(other)),             "磁盘不能记着一条从未发送的消息"


# ══════════════════════════════════════════════════════════════
# 继续任务的归属：worker 必须绑定它自己的会话
# ══════════════════════════════════════════════════════════════

def _api_models():
    from src.models import MODEL_LIST
    return [i for i, m in enumerate(MODEL_LIST) if m[1] not in ("claude-code", "ollama")]


class TestContinueBindsItsOwnSession:

    def test_continue_after_switch_before_thread_enters(self, ui, qapp, monkeypatch,
                                                        isolated_memory):
        """P1 复现：点「继续任务」后、线程进入主体前切到 B——worker 必须仍绑定 A。

        丢了 args=(sess,) 时 _run_agent 会取"线程启动那一刻的 active"（已是 B）：
        覆盖 B 的 worker 身份、恢复检查拒掉继续、收尾却把 B 标成空闲，屏障随之失效。
        恢复检查与 begin/finalize 走真实实现，只有模型执行用桩。
        """
        import types
        from src import models, paths as _paths, recovery, run_records
        from src.ui import chat_window as cw

        monkeypatch.setattr(models, "get_model_config_issues", lambda *a, **k: [])
        hold = threading.Event()

        class _DelayedThread(threading.Thread):
            """start() 立即返回，但线程停在 target 第一行之前，直到放行。"""

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                target = self._target

                def _runner(*a2, **k2):
                    hold.wait(10)
                    target(*a2, **k2)

                self._target = _runner

        monkeypatch.setattr(cw, "threading", types.SimpleNamespace(
            Thread=_DelayedThread,
            current_thread=threading.current_thread,
            Event=threading.Event))

        calls = []
        loop_gate = threading.Event()

        def _shaped_loop(ui_, *, resume=None):
            """真实恢复检查 + begin/finalize，只把模型执行换成可控桩。"""
            with _worker_data_root(data_root):
                s = session.current_session()
                calls.append({"resume": resume, "session": s})
                if not recovery.before_run(s, ui=ui_, resume=resume):
                    return AgentResult("failed", "恢复检查未通过")
                run = run_records.begin_run(s)
                loop_gate.wait(10)
                run_records.finalize_run(s, run, AgentResult("completed"))
                return AgentResult("completed")

        monkeypatch.setattr(agent, "agent_loop", _shaped_loop)
        data_root = _paths.get_data_dir()       # 主线程取根（isolated_memory 已设置）

        # A：已存盘、有一轮被取消的运行记录，可点「继续任务」
        sess_a = session.get_active()
        _make_saved_session(sess_a)
        sess_a.current_model_index = _api_models()[0]
        run = run_records.begin_run(sess_a)
        report = run_records.finalize_run(sess_a, run, AgentResult("cancelled", "用户停止"))
        view = result_view.describe(report.snapshot)

        ui._on_result_continue(view)
        sess_a = session.get_active()
        assert sess_a.is_generating is True
        assert sess_a.last_worker is not None, "start() 返回后线程身份必须已登记"
        assert len(calls) == 0, "线程还停在 target 之前"

        # 线程进入主体之前切到 B
        ui._activate_session(session.Session())
        sess_b = session.get_active()
        assert sess_b is not sess_a

        hold.set()                              # 放行：线程此刻才进入 _run_agent
        assert _wait_until(qapp, lambda: len(calls) == 1)
        assert calls[0]["session"] is sess_a, "worker 必须绑定「继续」所属的会话"
        assert calls[0]["resume"]["expected_run_id"] == view["run_id"]
        assert sess_a.is_generating is True
        # B 的 worker 身份没被动过：屏障在 B 上仍然有效
        assert sess_b.is_generating is False
        assert sess_b.last_worker is None
        assert sess_b.worker_token is None

        loop_gate.set()                         # A 这一轮正常收尾
        assert _wait_until(qapp, lambda: sess_a.is_generating is False)
        _pump(qapp, 0.3)
        assert len(calls) == 1
        assert sess_b.is_generating is False

        # B 照常发送、照常绑定 B（屏障没有被串会话破坏）
        _type(ui, "B 自己的消息")
        ui._on_send_click()
        assert _wait_until(qapp, lambda: len(calls) == 2)
        assert calls[1]["session"] is sess_b
        loop_gate.set()
        _pump(qapp, 0.4)


# ══════════════════════════════════════════════════════════════
# 窗口销毁与延迟事件
# ══════════════════════════════════════════════════════════════

class TestWindowDestroy:

    def test_delayed_barrier_events_after_destroy(self, qapp, monkeypatch, isolated_memory,
                                                  fake_loop):
        """窗口销毁后延迟事件继续被处理：轮询定时器随 receiver 取消，
        不打已删除的控件树，进程正常收尾。"""
        import socket

        def _forbidden(*args, **kwargs):
            raise AssertionError("no network in barrier tests")

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
        try:
            _type(ui, "first")
            ui._on_send_click()
            assert _wait_until(qapp, lambda: len(fake_loop) == 1)
            sess = session.get_active()
            ui._on_send_click()                 # 停止 → 收尾窗口
            _type(ui, "窗口要关了")
            ui._send_message()                  # 排队（定时器已挂上）
            _pump(qapp, 0.1)                    # 至少放一跳，确认轮询活着
            assert ui._pending_run_for(sess) is not None

            toasts = []
            ui._show_toast = lambda text, duration=1500: toasts.append(str(text))
            _destroy_ui_cleanly(ui, qapp)       # 销毁窗口（receiver 上下文取消定时器）
            fake_loop[0]["gate"].set()          # 之后旧 worker 退出、finished 到达
            fake_loop[0]["thread"].join(5)
            _pump(qapp, 0.5)
            assert toasts == [], "销毁后不得再有回调打到已删除的窗口"
            assert len(fake_loop) == 1, "窗口没了，排队请求不自发执行"
        finally:
            _destroy_ui_cleanly(ui, qapp)       # 幂等：已销毁则直接返回
            if state.ui_ref is ui:
                state.ui_ref = None
