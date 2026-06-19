"""会话级状态隔离测试（多会话并发地基，Phase 1）。

区别于 test_session.py（测会话持久化 save/load）：这里守护 session.py + state.py
代理的核心保证——会话级状态每会话一份、worker 线程互不串台、全局字段不受影响。
后续 Phase 改动时这些断言不能破。
"""
import threading

from src import state, session


def _fresh_active():
    """复位 active 为干净 Session，避免污染其它测试（active 是进程级单例）。"""
    session.set_active(session.Session())


def test_session_fields_independent():
    """两个 Session 的可变默认值（list/dict）不共享同一对象。"""
    a, b = session.Session(), session.Session()
    a.chat_history.append("x")
    a.compaction["summary"] = "s"
    a.session_token_usage["total"] = 99
    assert b.chat_history == []
    assert b.compaction["summary"] == ""
    assert b.session_token_usage["total"] == 0


def test_state_proxy_routes_to_active():
    """state.X 会话级字段读写落到 active session；全局字段仍是普通模块变量。"""
    s = session.Session()
    session.set_active(s)
    try:
        state.chat_history = ["hello"]
        assert s.chat_history == ["hello"]
        state.current_plan = state.parse_plan("[ ] step")
        assert len(s.current_plan) == 1
        state.compaction["covered_upto"] = 7  # dict mutate 也落到 active
        assert s.compaction["covered_upto"] == 7
        state.current_model_index = 3  # 全局字段不走 property
        assert state.current_model_index == 3
    finally:
        _fresh_active()


def test_thread_bind_isolates_from_active():
    """worker 线程 bind 自己的 session 后，写入不污染主线程 active。"""
    main = session.Session()
    session.set_active(main)
    state.stop_flag = True  # 主线程 active 置 True

    res = {}

    def worker():
        own = session.Session()
        session.bind_thread(own)
        try:
            state.stop_flag = False  # 落到 worker 自己的 session
            res["worker_stop"] = state.stop_flag
            res["is_own"] = session.current_session() is own
        finally:
            session.unbind_thread()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert res["worker_stop"] is False
    assert res["is_own"] is True
    assert main.stop_flag is True  # 主线程 active 未被 worker 污染
    _fresh_active()


def test_unbound_thread_uses_active():
    """未绑定会话的线程，current_session() 返回 active。"""
    s = session.Session()
    session.set_active(s)
    assert session.current_session() is s
    _fresh_active()
