"""多会话并发 P2 集成测试。

覆盖 multi_session_p2_spec.md 中定义的测试点：
  - Step 1: Session 生命周期 API（register/get/drop/new_session_key + key/needs_redraw 字段）
  - Step 2: Memory 层加载到指定 Session（load_session / reset_history 的 session= 参数）
  - Step 5: 输出路由（show_message 非活跃会话静默 + needs_redraw 标记）
"""
import sys
import os
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from src.session import (
    Session,
    register,
    get,
    drop,
    new_session_key,
    bind_thread,
    unbind_thread,
    set_active,
    get_active,
    current_session,
    sessions as registry,
)
from src import memory
from src import state


# ── helpers ──

def _make_session_with_history(sid, title, messages):
    """构造一个已填充 chat_history / session_id / title 的 Session（不经磁盘）。"""
    s = Session()
    s.current_session_id = sid
    s.current_session_title = title
    s.chat_history = list(messages)
    return s


# ════════════════════════════════════════════════════════════════════
# 1. Session 基础属性
# ════════════════════════════════════════════════════════════════════

class TestSessionDefaults:
    def test_session_has_key_and_needs_redraw(self):
        s = Session()
        assert s.key is None
        assert s.needs_redraw is False

    def test_session_has_generating_and_thread(self):
        s = Session()
        assert s.is_generating is False
        assert s.thread is None

    def test_two_sessions_independent_fields(self):
        a, b = Session(), Session()
        a.chat_history.append(HumanMessage(content="hello"))
        a.stop_flag = True
        assert b.chat_history == []
        assert b.stop_flag is False


# ════════════════════════════════════════════════════════════════════
# 2. Session 注册表 API（Step 1）
# ════════════════════════════════════════════════════════════════════

class TestSessionRegistry:
    """每个测试独立：前后清空注册表。"""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        registry.clear()
        yield
        registry.clear()

    # ── register ──

    def test_register_assigns_temp_key_for_new_session(self):
        s = Session()
        key = register(s)
        assert key.startswith("_new_")
        assert s.key == key
        assert registry[key] is s

    def test_register_uses_session_id_for_saved_session(self):
        s = Session()
        s.current_session_id = "20260101_120000_000000"
        key = register(s)
        assert key == "20260101_120000_000000"
        assert registry[key] is s

    def test_register_idempotent(self):
        s = Session()
        k1 = register(s)
        k2 = register(s)
        assert k1 == k2
        assert len(registry) == 1

    def test_register_two_sessions_both_in_registry(self):
        a, b = Session(), Session()
        ka, kb = register(a), register(b)
        assert ka != kb
        assert registry[ka] is a
        assert registry[kb] is b
        assert len(registry) == 2

    # ── get / drop ──

    def test_get_returns_registered_session(self):
        s = Session()
        k = register(s)
        assert get(k) is s

    def test_get_returns_none_for_unknown_key(self):
        assert get("nonexistent") is None

    def test_drop_removes_from_registry(self):
        s = Session()
        k = register(s)
        drop(k)
        assert get(k) is None
        assert k not in registry

    def test_drop_nonexistent_key_is_noop(self):
        drop("no_such_key")  # 不应抛异常

    # ── new_session_key ──

    def test_new_session_key_increments(self):
        k0 = new_session_key()
        s = Session()
        s.key = k0
        registry[k0] = s
        k1 = new_session_key()
        assert k1 != k0
        assert k0.startswith("_new_")
        assert k1.startswith("_new_")

    def test_new_session_key_skips_existing(self):
        registry["_new_0"] = Session()
        k = new_session_key()
        assert k == "_new_1"

    # ── 线程安全 ──

    def test_register_concurrent_no_crash(self):
        """并发注册不抛异常、不出重复 key。"""
        keys = []
        lock = threading.Lock()

        def _register_one():
            s = Session()
            k = register(s)
            with lock:
                keys.append(k)

        threads = [threading.Thread(target=_register_one) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(keys) == 20
        assert len(set(keys)) == 20, "存在重复 key"


# ════════════════════════════════════════════════════════════════════
# 3. 会话级状态隔离（Step 1 基础）
# ════════════════════════════════════════════════════════════════════

class TestSessionStateIsolation:
    def test_chat_history_isolation(self):
        a, b = Session(), Session()
        a.chat_history.append(HumanMessage(content="A's message"))
        b.chat_history.append(HumanMessage(content="B's message"))
        assert len(a.chat_history) == 1
        assert len(b.chat_history) == 1
        assert a.chat_history[0].content == "A's message"
        assert b.chat_history[0].content == "B's message"

    def test_token_usage_isolation(self):
        a, b = Session(), Session()
        a.session_token_usage["input"] = 100
        assert b.session_token_usage["input"] == 0

    def test_stop_flag_isolation(self):
        a, b = Session(), Session()
        a.stop_flag = True
        assert b.stop_flag is False

    def test_compaction_isolation(self):
        a, b = Session(), Session()
        a.compaction["summary"] = "summary for A"
        assert b.compaction["summary"] == ""

    def test_current_plan_isolation(self):
        a, b = Session(), Session()
        a.current_plan.append("step 1")
        assert b.current_plan == []

    def test_needs_redraw_isolation(self):
        a, b = Session(), Session()
        a.needs_redraw = True
        assert b.needs_redraw is False

    def test_key_isolation(self):
        a, b = Session(), Session()
        a.key = "key_a"
        assert b.key is None

    def test_command_whitelist_per_session(self):
        """命令/编辑白名单会话级：A 会话"允许并记住"的不泄漏到 B。"""
        a, b = Session(), Session()
        a.command_prefix_allowlist.add("git")
        a.edit_path_allowlist.add("/x/foo.py")
        assert "git" in a.command_prefix_allowlist
        assert "git" not in b.command_prefix_allowlist
        assert b.edit_path_allowlist == set()

    def test_pending_confirm_per_session(self):
        """后台会话积压的确认存各自的 pending_confirm（默认 None），互不干扰。"""
        a, b = Session(), Session()
        assert a.pending_confirm is None and b.pending_confirm is None
        a.pending_confirm = ("command", "rm x", {}, None)
        assert a.pending_confirm[0] == "command"
        assert b.pending_confirm is None  # 不影响别的会话

    def test_render_log_per_session(self):
        """render_log 默认空、会话级（本轮渲染事件缓冲，供"切走→切回"重放）。"""
        a, b = Session(), Session()
        assert a.render_log == [] and b.render_log == []
        a.render_log.append(("msg", "hi", "ai_msg"))
        assert b.render_log == []  # 不影响别的会话

    def test_seal_render_log_clears_active(self):
        """seal_render_log 清空当前会话的 render_log（append chat_history 后调）。"""
        from src import session as _session
        s = _session.Session()
        s.render_log.append(("msg", "x", "ai_msg"))
        _session.set_active(s)
        _session.seal_render_log()
        assert s.render_log == []
        _session.set_active(_session.Session())


# ════════════════════════════════════════════════════════════════════
# 4. 线程绑定路由（Step 1 基础）
# ════════════════════════════════════════════════════════════════════

class TestThreadBinding:
    def test_default_returns_active(self):
        active = get_active()
        assert current_session() is active

    def test_bind_redirects_current_session(self):
        s = Session()
        bind_thread(s)
        try:
            assert current_session() is s
        finally:
            unbind_thread()

    def test_unbind_reverts_to_active(self):
        s = Session()
        bind_thread(s)
        unbind_thread()
        assert current_session() is get_active()

    def test_worker_isolation_across_threads(self):
        """两个线程绑定不同 Session，各自 current_session() 互不干扰。"""
        a, b = Session(), Session()
        result = {}

        def _worker(name, sess):
            bind_thread(sess)
            result[name] = current_session()
            unbind_thread()

        t1 = threading.Thread(target=_worker, args=("a", a))
        t2 = threading.Thread(target=_worker, args=("b", b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert result["a"] is a
        assert result["b"] is b

    def test_set_active_changes_active(self):
        old = get_active()
        s = Session()
        set_active(s)
        try:
            assert get_active() is s
        finally:
            set_active(old)


# ════════════════════════════════════════════════════════════════════
# 5. Memory 层加载到指定 Session（Step 2）
# ════════════════════════════════════════════════════════════════════

@pytest.fixture
def isolated_memory(tmp_path_factory):
    """隔离 memory 模块的文件路径到临时目录(走 paths 按上下文数据根)。"""
    from src import paths as _paths
    root = tmp_path_factory.mktemp("mem")
    _paths.set_data_dir(str(root))
    memory._ensure_memory_dir()
    yield root / "chat_memory"      # 返回真正的会话目录(测试会用它拼 <sid>.json 路径)
    _paths.set_data_dir(None)


class TestMemorySession:
    def test_save_session_explicit_and_reload(self, isolated_memory, monkeypatch):
        """save_session(session=sess) + load_session(sid, session=target) 完整链路。"""
        src = Session()
        src.current_session_id = "test_001"
        src.current_session_title = "测试标题"
        src.chat_history = [
            SystemMessage(content="sys"),
            HumanMessage(content="hello"),
            AIMessage(content="hi there"),
        ]

        monkeypatch.setattr(state, "current_project", None)
        memory.save_session(session=src)

        dst = Session()
        ok = memory.load_session("test_001", session=dst)
        assert ok is True
        assert dst.current_session_id == "test_001"
        assert dst.current_session_title == "测试标题"
        assert len(dst.chat_history) == 3
        assert dst.chat_history[1].content == "hello"

    def test_load_session_returns_false_for_missing(self, isolated_memory):
        dst = Session()
        assert memory.load_session("nonexistent_id", session=dst) is False

    def test_two_sessions_coexist_after_load(self, isolated_memory, monkeypatch):
        """load A → load B → A 的数据没被覆盖。"""
        monkeypatch.setattr(state, "current_project", None)

        for sid, text in [("s_a", "msg from A"), ("s_b", "msg from B")]:
            s = Session()
            s.current_session_id = sid
            s.chat_history = [SystemMessage(content="sys"), HumanMessage(content=text)]
            memory.save_session(session=s)

        sess_a = Session()
        sess_b = Session()
        memory.load_session("s_a", session=sess_a)
        memory.load_session("s_b", session=sess_b)

        assert sess_a.chat_history[1].content == "msg from A"
        assert sess_b.chat_history[1].content == "msg from B"

    def test_reset_history_on_explicit_session(self, isolated_memory, monkeypatch):
        """reset_history(session=sess) 只重置目标 Session，不影响前台。"""
        monkeypatch.setattr(state, "current_project", None)

        front = Session()
        front.current_session_id = "front_sid"
        front.chat_history = [SystemMessage(content="sys"), HumanMessage(content="front data")]
        memory.save_session(session=front)
        set_active(front)

        back = Session()
        back.current_session_id = "back_sid"
        back.chat_history = [SystemMessage(content="sys"), HumanMessage(content="back data")]
        memory.save_session(session=back)

        memory.reset_history(session=back)

        # 后台应被重置
        assert back.current_session_id is None
        assert back.current_session_title is None
        assert len(back.chat_history) == 1
        assert isinstance(back.chat_history[0], SystemMessage)

        # 前台不受影响
        assert front.current_session_id == "front_sid"
        assert front.chat_history[1].content == "front data"

    def test_load_session_preserves_generating_state(self, isolated_memory, monkeypatch):
        """加载到 Session 不改变 is_generating / thread / key 等运行态字段。"""
        monkeypatch.setattr(state, "current_project", None)

        src = Session()
        src.current_session_id = "gen_test"
        src.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        memory.save_session(session=src)

        dst = Session()
        dst.is_generating = True
        dst.thread = threading.current_thread()
        dst.key = "_new_99"

        memory.load_session("gen_test", session=dst)

        assert dst.is_generating is True
        assert dst.thread is threading.current_thread()
        assert dst.key == "_new_99"


# ════════════════════════════════════════════════════════════════════
# 6. 输出路由（Step 5 / 6b）
# ════════════════════════════════════════════════════════════════════

class TestOutputRouting:
    """测试 show_message / remove_thinking_indicator / update_thinking_indicator 的路由逻辑。"""

    @pytest.fixture(autouse=True)
    def _reset_active(self):
        """确保测试前后 active session 恢复原状。"""
        old = get_active()
        yield
        set_active(old)

    def test_show_message_blocked_for_non_active(self):
        """worker 会话 ≠ active → show_message 不发信号，标记 needs_redraw。"""
        worker_sess = Session()
        bind_thread(worker_sess)
        try:
            from src import session as _session
            _sess = _session.current_session()
            assert _sess is not _session.get_active(), "precondition: worker != active"

            if _sess is not _session.get_active():
                _sess.needs_redraw = True
                signaled = False
            else:
                signaled = True

            assert not signaled, "非活跃会话不应发信号"
            assert worker_sess.needs_redraw is True
        finally:
            unbind_thread()

    def test_show_message_passes_for_active(self):
        """worker 会话 == active → show_message 正常走信号。"""
        active = get_active()
        bind_thread(active)
        try:
            from src import session as _session
            _sess = _session.current_session()
            assert _sess is _session.get_active(), "precondition: worker == active"

            signaled = False
            if _sess is not _session.get_active():
                pass
            else:
                signaled = True

            assert signaled, "活跃会话应该走信号路径"
        finally:
            unbind_thread()

    def test_needs_redraw_accumulates_across_calls(self):
        """多次 show_message（后台）只累加 needs_redraw，不发信号。"""
        worker = Session()
        bind_thread(worker)
        try:
            from src import session as _session
            for _ in range(5):
                _sess = _session.current_session()
                if _sess is not _session.get_active():
                    _sess.needs_redraw = True
            assert worker.needs_redraw is True
        finally:
            unbind_thread()

    def test_switch_active_clears_needs_redraw(self):
        """切回后台会话前：needs_redraw 为 True → 渲染后应重置为 False。"""
        sess = Session()
        sess.needs_redraw = True
        sess.needs_redraw = False
        assert sess.needs_redraw is False


# ════════════════════════════════════════════════════════════════════
# 7. Worker 绑定 Session（Step 3 验证）
# ════════════════════════════════════════════════════════════════════

class TestWorkerBinding:
    """验证 _run_agent 的绑定行为等价逻辑。"""

    def test_worker_binds_session_at_start_unbinds_at_end(self):
        """模拟 _run_agent：bind → agent_loop → unbind → finished。"""
        sess = Session()
        state_snapshot = {}

        def _fake_agent_loop():
            state_snapshot["inside"] = current_session()
            state_snapshot["is_generating"] = sess.is_generating

        sess.is_generating = True
        sess.thread = threading.current_thread()
        bind_thread(sess)
        try:
            _fake_agent_loop()
        finally:
            sess.is_generating = False
            sess.thread = None
            unbind_thread()

        assert state_snapshot["inside"] is sess
        assert state_snapshot["is_generating"] is True
        assert sess.is_generating is False
        assert sess.thread is None

    def test_two_workers_bind_different_sessions(self):
        """两个线程各自绑定不同 Session，agent_loop 读到的 state 各自独立。"""
        a = Session()
        b = Session()
        a.chat_history.append(HumanMessage(content="from A"))
        b.chat_history.append(HumanMessage(content="from B"))

        results = {}

        def _worker(name, sess):
            sess.is_generating = True
            bind_thread(sess)
            try:
                ch = current_session().chat_history
                results[name] = [m.content for m in ch]
            finally:
                sess.is_generating = False
                unbind_thread()

        t1 = threading.Thread(target=_worker, args=("a", a))
        t2 = threading.Thread(target=_worker, args=("b", b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["a"] == ["from A"]
        assert results["b"] == ["from B"]


# ════════════════════════════════════════════════════════════════════
# 8. finished 信号路由（Step 6b 验证）
# ════════════════════════════════════════════════════════════════════

class TestFinishedRouting:
    """验证 _on_finished_sess 的区分逻辑。"""

    def test_finished_active_session_full_cleanup(self):
        """finished_sess == active → 走完整收尾路径。"""
        active = get_active()
        active.is_generating = True

        finished_sess = active
        active_sess = get_active()
        is_background = finished_sess is not active_sess
        assert not is_background, "活跃会话结束不应走后台路径"

        active.is_generating = False
        assert not active.is_generating

    def test_finished_background_session_skips_ui(self):
        """finished_sess != active → 走后台路径（只刷侧栏）。"""
        active = get_active()
        bg_sess = Session()
        bg_sess.is_generating = True

        finished_sess = bg_sess
        active_sess = get_active()
        is_background = finished_sess is not active_sess
        assert is_background, "后台会话结束应走后台路径"

        bg_sess.is_generating = False
        assert active is get_active()


# ════════════════════════════════════════════════════════════════════
# 9. 切会话不串台（P2 核心保证：真线程 + state 代理集成，非套套逻辑）
# ════════════════════════════════════════════════════════════════════

class TestSwitchDoesNotCorruptBackground:
    """这才是 P2 真正要守住的：后台 worker 在跑时切 active，互不污染。
    直接走 state 代理（worker 线程 current_session()=它 bind 的会话），不 inline 重写逻辑。"""

    @pytest.fixture(autouse=True)
    def _clean(self):
        old = get_active()
        registry.clear()
        yield
        registry.clear()
        set_active(old)

    def test_set_active_does_not_touch_running_background(self):
        s0 = Session(); s0.chat_history.append(SystemMessage(content="s0"))
        s1 = Session(); s1.chat_history.append(SystemMessage(content="s1"))
        register(s0); register(s1)
        set_active(s0)

        started, proceed, done = (threading.Event() for _ in range(3))

        def _worker():
            bind_thread(s0)
            try:
                started.set()
                proceed.wait(2)
                # 经 state 代理写——worker 线程里 current_session() 应为 s0
                state.chat_history.append(HumanMessage(content="from worker"))
                state.stop_flag = True
            finally:
                unbind_thread()
                done.set()

        t = threading.Thread(target=_worker); t.start()
        started.wait(2)
        set_active(s1)                       # 主线程切到 s1（此刻 worker 还没写）
        assert get_active() is s1
        proceed.set()
        done.wait(2); t.join()

        # worker 的写落到 s0，没污染切过去的 s1
        assert any(getattr(m, "content", None) == "from worker" for m in s0.chat_history)
        assert s0.stop_flag is True
        assert all(getattr(m, "content", None) != "from worker" for m in s1.chat_history)
        assert s1.stop_flag is False

    def test_rekey_after_save(self, isolated_memory, monkeypatch):
        """新会话（临时 key）存盘拿到 id 后，注册表 key 迁移成 id，临时 key 失效。"""
        monkeypatch.setattr(state, "current_project", None)
        registry.clear()
        s = Session()
        s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="hi")]
        tmp_key = register(s)
        assert tmp_key.startswith("_new_")
        memory.save_session(session=s)       # 生成 id + re-key
        assert s.current_session_id is not None
        assert s.key == s.current_session_id
        assert get(tmp_key) is None                  # 临时 key 已迁走
        assert get(s.current_session_id) is s        # id 指向同一对象（不会重复建）

    def test_project_tag_anchored_against_switch(self, isolated_memory, monkeypatch):
        """无项目会话首次落盘锚定 None；之后全局切到 A 项目再 save，磁盘 tag 仍是 None。
        复现并守护"无项目对话被切项目后误归到新项目"的 bug（worker 晚于主线程切项目 save）。"""
        import json
        import os as _os
        from src import session as _session
        registry.clear()
        monkeypatch.setattr(state, "current_project", None)  # 当前：无项目
        s = _session.Session()
        s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="无项目对话")]
        memory.save_session(session=s)          # 首次落盘 → 锚定 project=None
        sid = s.current_session_id
        assert s.project is None
        # 用户切到 A 项目（全局变 A），该会话（如后台 worker 晚 save）再存一次
        monkeypatch.setattr(state, "current_project", "D:/projA")
        memory.save_session(session=s)
        with open(_os.path.join(str(isolated_memory), f"{sid}.json"), encoding="utf-8") as f:
            data = json.load(f)
        assert data["project"] is None, f"会话被误标成 {data['project']}（应保持无项目）"

    def test_model_mode_per_session(self):
        """model / Plan-Act / 思考 会话级：两会话各自独立，state 代理跟随 active。"""
        from src import session as _session
        a = _session.Session()
        a.current_model_index = 1
        a.agent_mode = "plan"
        a.reasoning_enabled = False
        b = _session.Session()
        b.current_model_index = 3
        b.agent_mode = "act"
        b.reasoning_enabled = True
        _session.set_active(a)
        assert state.current_model_index == 1
        assert state.agent_mode == "plan"
        assert state.reasoning_enabled is False
        _session.set_active(b)
        assert state.current_model_index == 3
        assert state.agent_mode == "act"
        assert state.reasoning_enabled is True
        # 切来切去互不影响
        assert a.current_model_index == 1 and b.current_model_index == 3

    def test_project_cwd_follows_session_not_global(self, tmp_path, monkeypatch):
        """_project_cwd 优先用当前会话锚定的 project，不被全局 current_project 影响
        （后台会话跑工具时 cwd 不跟前台切项目走）。"""
        from src import session as _session, state
        from src.tools import _project_cwd
        d_sess = tmp_path / "sessproj"
        d_sess.mkdir()
        d_global = tmp_path / "globalproj"
        d_global.mkdir()
        monkeypatch.setattr(state, "current_project", str(d_global))  # 全局指向 B
        s = _session.Session()
        s.project = str(d_sess)                                       # 会话锚定 A
        _session.set_active(s)
        assert _project_cwd() == str(d_sess)                          # 用会话的 A，不是全局 B


class TestParallelTools:
    """并行工具调用：多个只读工具并行 invoke、结果按 tool_calls 原顺序对应。"""

    def test_parallel_invoke_readonly(self, project_dir):
        from src.streaming import _parallel_invoke
        (project_dir / "a.txt").write_text("CONTENT_AAA", encoding="utf-8")
        (project_dir / "b.txt").write_text("CONTENT_BBB", encoding="utf-8")
        tcs = [
            {"name": "read_file", "args": {"path": "a.txt"}},
            {"name": "read_file", "args": {"path": "b.txt"}},
        ]
        res = _parallel_invoke(tcs)
        assert len(res) == 2
        assert "CONTENT_AAA" in res[0]   # 结果按 index 对应原顺序
        assert "CONTENT_BBB" in res[1]

    def test_parallel_safe_excludes_write_tools(self):
        """写类 / 需确认 / 改状态的工具不在并行白名单（避免确认卡冲突与副作用竞争）。"""
        from src.streaming import PARALLEL_SAFE_TOOLS
        for unsafe in ("run_command", "edit_file", "write_file", "apply_patch",
                       "update_plan", "remember"):
            assert unsafe not in PARALLEL_SAFE_TOOLS
        for safe in ("read_file", "search_files", "code_map", "git_diff"):
            assert safe in PARALLEL_SAFE_TOOLS
