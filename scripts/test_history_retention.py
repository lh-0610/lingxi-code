"""会话历史保留与侧栏加载更多（历史保留半边）。

核心断言：会话数量增长**永不**触发正文或索引条目的清理——删除只能由用户主动
发起（delete_session）。覆盖两种注册表状态：旧会话仍在内存中 / 已从注册表移除
（旧实现靠"打开中的会话不删盘"就能让只测前者的用例假通过）。
"""
import json
import os
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from src import memory, paths, session as _session


@pytest.fixture(autouse=True)
def _no_real_models_or_services(monkeypatch):
    """误走到真实模型 / Claude CLI / 通知就当场失败（同 B04/B05 用例）。"""
    from src import agent as _agent
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(_agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)


def _make_saved_session(i, *, project=None, kind="code"):
    """造一个真实落盘的会话（正文 + 索引条目），id 确定性命名便于断言。"""
    s = _session.Session()
    s.current_session_id = f"hist-{i:03d}"
    if project is not None:
        s.project = project
    if kind == "rag":
        s.session_kind = "rag"
        s.rag_kb_dir = "C:/kb"
    s.chat_history = [SystemMessage(content="sys"), HumanMessage(content=f"历史会话 {i:03d} 的正文")]
    _session.register(s)
    outcome = memory.save_session_report(session=s)
    assert outcome.body_written and outcome.index_written, outcome.error
    return s


def _body_path(sid):
    return os.path.join(paths.memory_dir(), f"{sid}.json")


@pytest.mark.parametrize("stay_registered", [False, True])
def test_sessions_beyond_50_keep_bodies_and_index_entries(isolated_memory, stay_registered):
    """超过 50 个会话：正文与索引条目全部保留，无论旧会话是否还在注册表里。

    removed 变体把大部分会话从注册表移除后再触发一次索引重写——旧实现会把这些
    "没人打开"的会话连正文带索引一起删掉；这条用例钉死该行为不再发生。
    """
    total = 60
    made = [_make_saved_session(i) for i in range(total)]
    if not stay_registered:
        # 模拟长会话场景：早先的会话早已从注册表移除（重启、被挤出等）
        for s in made[:55]:
            _session.drop(s.current_session_id)

    # 再触发一次索引重写（旧实现在这里截断 + 删盘）
    _make_saved_session(total)

    entries = memory.list_sessions("__all__")
    assert len(entries) == total + 1
    ids = {e["id"] for e in entries}
    for i in range(total + 1):
        sid = f"hist-{i:03d}"
        assert sid in ids, f"索引条目丢失: {sid}"
        assert os.path.exists(_body_path(sid)), f"会话正文被删除: {sid}"
        loader = _session.Session()
        assert memory.load_session(sid, session=loader), f"会话无法加载: {sid}"
        assert loader.chat_history[1].content == f"历史会话 {i:03d} 的正文"


def test_saving_old_session_refreshes_recency(isolated_memory):
    """保存旧会话会刷新它的 updated——展示层据此把它带回该分组的近期位置。"""
    for i in range(5):
        _make_saved_session(i)
    entries = {e["id"]: e for e in memory.list_sessions("__all__")}
    old_updated = entries["hist-000"]["updated"]

    # 重新保存最老的会话（追加一条消息再存）
    s = _session.Session()
    assert memory.load_session("hist-000", session=s)
    s.chat_history.append(HumanMessage(content="旧会话又有新消息"))
    memory.save_session(session=s)

    entries = {e["id"]: e for e in memory.list_sessions("__all__")}
    assert entries["hist-000"]["updated"] > old_updated
    assert entries["hist-000"]["updated"] == max(e["updated"] for e in entries.values())


def test_deleted_session_never_resurrects(isolated_memory, monkeypatch):
    """用户主动删除的会话：后续保存、索引修复、重读都不会让它复活。"""
    for i in range(8):
        _make_saved_session(i)
    memory.delete_session("hist-003")
    assert not os.path.exists(_body_path("hist-003"))

    # ① 之后正常保存别的会话（索引重写）不复活它
    _make_saved_session(20)
    assert "hist-003" not in {e["id"] for e in memory.list_sessions("__all__")}

    # ② 索引修复路径也不复活：登记一条待修项后删除正文 → 修复时销案
    _make_saved_session(21)
    memory._record_index_repair({"id": "hist-004", "recorded": "2026-01-01T00:00:00"})
    memory.delete_session("hist-004")
    memory.list_sessions("__all__")     # 触发 _repair_pending_index_entries
    assert "hist-004" not in {e["id"] for e in memory.list_sessions("__all__")}
    assert not os.path.exists(_body_path("hist-004"))
    # 待修登记已销案，不会在后续重读中反复出现
    repair_path = memory._index_repair_file()
    if os.path.exists(repair_path):
        pending = json.loads(open(repair_path, encoding="utf-8").read()).get("pending", [])
        assert "hist-004" not in [p.get("id") for p in pending]

    # ③ 重启等价（新解释器重读，见 test_fresh_interpreter_rereads）也不会复活：
    #    正文不在盘上，修复路径以正文为唯一真相。


def test_fresh_interpreter_rereads_isolated_history(isolated_memory, tmp_path):
    """新解释器重新读取隔离数据：旧会话仍在索引里、仍能加载。"""
    total = 55
    for i in range(total):
        _make_saved_session(i)
    # 把最早的一批从注册表移除，覆盖"没人打开"的旧会话
    for i in range(50):
        _session.drop(f"hist-{i:03d}")

    runner = tmp_path / "reread_child.py"
    runner.write_text(
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from src import paths, memory, session\n"
        "paths.set_data_dir(sys.argv[2])          # 子进程同样显式隔离数据根\n"
        "entries = memory.list_sessions('__all__')\n"
        "s = session.Session()\n"
        "ok = memory.load_session('hist-000', session=s)\n"
        "print(json.dumps({'count': len(entries),\n"
        "                  'ids': sorted(e['id'] for e in entries),\n"
        "                  'oldest_loaded': ok,\n"
        "                  'oldest_first_msg': (s.chat_history[1].content if ok else None)},\n"
        "                 ensure_ascii=False))\n",
        encoding="utf-8")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(runner), repo_root, str(isolated_memory.parent)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, env={**os.environ, "PYTHONPATH": repo_root})
    assert proc.returncode == 0, proc.stderr[-2000:]
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["count"] == total
    assert "hist-000" in payload["ids"] and f"hist-{total - 1:03d}" in payload["ids"]
    assert payload["oldest_loaded"] is True
    assert payload["oldest_first_msg"] == "历史会话 000 的正文"


def test_index_write_failure_reports_and_repair_keeps_all_entries(isolated_memory, monkeypatch):
    """索引写入失败仍如实报告；下一次修复保留全部条目，不截断。"""
    real_atomic = memory._atomic_write_json
    total = 55
    for i in range(total):
        _make_saved_session(i)

    # 索引写失败（正文照常成功）：SaveOutcome 必须如实带出
    def _failing_index_write(path, data, **kw):
        if path == memory.memory_index():
            raise OSError("SIMULATED_INDEX_FAILURE")
        return real_atomic(path, data, **kw)

    monkeypatch.setattr(memory, "_atomic_write_json", _failing_index_write)
    # 手工构造（_make_saved_session 会断言索引成功，与本用例的失败注入冲突）
    s = _session.Session()
    s.current_session_id = "hist-090"
    s.chat_history = [SystemMessage(content="sys"), HumanMessage(content="索引失败的会话")]
    _session.register(s)
    outcome = memory.save_session_report(session=s)
    assert outcome.body_written is True and outcome.index_written is False
    assert outcome.error is not None

    # 修复（下次读盘触发）后：全部条目都在，包括失败那次与修复那次
    monkeypatch.setattr(memory, "_atomic_write_json", real_atomic)
    entries = memory.list_sessions("__all__")        # 触发 _repair_pending_index_entries
    ids = {e["id"] for e in entries}
    assert len(entries) == total + 1                 # hist-000..054 + hist-090（修复补回）
    for i in range(total):
        assert f"hist-{i:03d}" in ids
    assert "hist-090" in ids
    assert os.path.exists(_body_path("hist-090"))
