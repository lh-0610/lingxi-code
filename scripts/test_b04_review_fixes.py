"""B04 首轮复核（43568a0）指出的三处，以及修复时顺带发现的同类问题。

1. **无项目会话丢掉结果未知的写入**：无项目会话的工具落在进程工作目录，而恢复只在
   "会话有项目根"时才保留盲区——根目录为空就整个丢掉，sidecar 照样摘掉，没跑任何检查
   也按 completed 收尾。现在执行前记录写下工具实际的工作目录；实在确定不了就挂在
   "工作目录无法确定"的占位键上，**目录不知道 ≠ 没有改动**。
2. **项目搬家后义务追着旧目录**：盲区还挂在已不存在的旧路径上，restore 给它种了空基线，
   每次复查都枚举失败、重新作废测试与 diff 结论，按提示补查也收不了尾。现在确认是同一个
   项目后迁移活动追踪路径（历史锚点保留原值），且任何不存在的目录都不再种基线。
3. **锚点坏了打不开聊天**：`errors` 被改成数字，`load_session` 直接抛 TypeError；保存路径
   也做同一次归一化，于是之后每次保存都失败。现在逐字段严格校验、坏了降级成"无法核对"，
   进度归一化本身也不再往外抛。

顺带：sidecar 里解析不了的条目原先被静默丢弃（唯一那条坏了就等于"没有未决操作"，下一次
改写还会把它抹掉），现在保留、按结果未知处理，交接到恢复说明之后才摘除。
"""
import json
import os
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from src import memory, recovery, run_records, session, verification
from src.agent_result import AgentResult

# 复用 B04 用例的造数函数（夹具在下面就地定义：从别的测试模块导入夹具会被当成重定义）。
from test_b04_recovery import (
    _Spy, _Stream, _UI, _api_models, _bind, _code_session, _git, _make_repo, _reopen, _sidecar,
)


@pytest.fixture()
def repo(tmp_path):
    return _make_repo(tmp_path / "proj")


@pytest.fixture()
def no_config_issues(monkeypatch):
    from src import models
    monkeypatch.setattr(models, "get_model_config_issues", lambda index=None: [])


@pytest.fixture(autouse=True)
def _no_real_models_or_services(monkeypatch):
    """同 test_b04_recovery：误走到真实模型 / Claude CLI 就当场失败，外加挡掉标题与通知。"""
    from src import agent as _agent
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(_agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "_post_run_notify", lambda *a, **k: None)


def _no_project_session():
    """无项目会话：工具落在进程工作目录（受支持的用法，不是异常情形）。"""
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="把 app.py 改一下"),
                         AIMessage(content="好")]
    sess.agent_mode = "act"
    sess.current_model_index = _api_models()[0]
    session.register(sess)
    memory.save_session(session=sess)
    sess.project = None
    memory.save_session(session=sess)
    return sess


def _write_passing_test(root):
    (root / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add test")


# ══════════════════════════════════════════════════════════════
# 1. 无项目会话
# ══════════════════════════════════════════════════════════════

class TestNoProjectSession:
    def test_the_real_dispatch_records_where_the_tool_ran(self, isolated_memory, repo,
                                                          monkeypatch):
        """工具**执行的当下**，sidecar 里就要有它实际的工作目录。"""
        from src import streaming
        monkeypatch.chdir(repo)
        sess = _no_project_session()
        _bind(sess)
        run_records.begin_run(sess)
        seen = {}

        class _Command:
            def invoke(self, args):
                doc = _sidecar(isolated_memory, sess.current_session_id)
                seen["work_root"] = doc["operations"][0]["work_root"]
                return "ok"

        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"run_command": _Command()})
        try:
            streaming._execute_tool({"name": "run_command", "args": {"command": "echo x"},
                                     "id": "r1"}, _UI())
        finally:
            session.unbind_thread()
        assert os.path.normcase(seen["work_root"]) == os.path.normcase(os.path.realpath(repo))

    def test_the_root_follows_the_operations_session_not_the_calling_thread(
            self, isolated_memory, repo, tmp_path):
        """按传进 begin_operation 的会话算目录，不按当前线程碰巧绑着谁算。"""
        other = _make_repo(tmp_path / "somewhere-else")
        sess = _code_session(repo)
        run_records.begin_run(sess)
        foreground = _code_session(other)
        session.bind_thread(foreground)          # 线程绑的是别的会话
        try:
            run_records.begin_operation(sess, tool="write_file", tool_call_id="w1",
                                        args={"path": "a.py"})
        finally:
            session.unbind_thread()
        op = _sidecar(isolated_memory, sess.current_session_id)["operations"][0]
        assert os.path.normcase(op["work_root"]) == os.path.normcase(os.path.realpath(repo))
        assert session.get_bound() is None, "借用完要还原成原来的（未绑定）状态"

    def test_an_unknown_command_keeps_its_obligation_without_a_project(
            self, isolated_memory, repo, monkeypatch):
        """复核原场景：无项目、命令改了文件、没有回执 → 重开后只说"继续"，不能按 completed 收尾。"""
        from src import agent as _agent
        monkeypatch.chdir(repo)
        sess = _no_project_session()
        _bind(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="run_command", tool_call_id="cmd",
                                    args={"command": "write app.py"})
        (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
        session.unbind_thread()

        again = _reopen(sess.current_session_id)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        monkeypatch.setattr(_agent, "_stream_with_tools", _Stream(("完成了。",)))
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()

        assert result.status == "unverified"
        tracking = again.pending_verification["tracking_incomplete"]
        assert any(os.path.normcase(k) == os.path.normcase(os.path.realpath(repo))
                   for k in tracking), "盲区挂在命令实际的工作目录上"
        assert _sidecar(isolated_memory, again.current_session_id) is None, \
            "交接之后才摘：义务已经落进会话，sidecar 可以清"

    def test_an_undeterminable_directory_still_keeps_the_obligation(self, isolated_memory,
                                                                    monkeypatch, tmp_path):
        """旧记录没有 work_root、会话也没有项目：目录确定不了，义务照样保留，不当作没改动。"""
        from src import agent as _agent
        monkeypatch.chdir(tmp_path)
        sess = _no_project_session()
        run_records.begin_run(sess)
        path = run_records.inflight_path(sess.current_session_id)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "session_id": sess.current_session_id, "operations": [{
                "operation_id": "op-legacy", "run_id": sess.last_run["id"],
                "session_id": sess.current_session_id, "tool": "run_command",
                "tool_call_id": "cmd", "base_revision": 0, "paths": [],
                "started_at": "2026-09-22T00:00:00+00:00", "state": "dispatched"}]}, stream)

        again = _reopen(sess.current_session_id)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        monkeypatch.setattr(_agent, "_stream_with_tools", _Stream(("完成了。",)))
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()
        assert result.status == "unverified"
        assert recovery.UNKNOWN_ROOT in again.pending_verification["tracking_incomplete"]

    def test_the_unknown_root_obligation_can_actually_be_discharged(self, isolated_memory, repo,
                                                                    monkeypatch):
        """占位键不能变成死结：真跑测试、真看 diff 之后闸门要能放行。"""
        from src.tools import run_tests
        from src.tools_git import git_diff
        _write_passing_test(repo)
        monkeypatch.chdir(repo)
        sess = _no_project_session()
        sess.pending_verification = {"files": [], "code_files": [], "reason": "", "run_id": "",
                                     "tracking_incomplete": {recovery.UNKNOWN_ROOT: "未知写入"}}
        _bind(sess)
        try:
            run_records.begin_run(sess)
            assert verification.get_verification_gaps(sess.verification), "先要求补验证"
            assert "✅" in run_tests.func(path="test_sample.py")
            git_diff.func()
            assert verification.get_verification_gaps(sess.verification) == []
            assert not sess.verification.get("tracking_errors"), "占位键不该被当成目录去枚举"
        finally:
            session.unbind_thread()


_CRASH_NO_PROJECT = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from src import paths
paths.set_data_dir(sys.argv[2])
from src import agent, memory, models, session, streaming
from src.models import MODEL_LIST
from langchain_core.messages import HumanMessage

def _forbidden(*a, **k):
    raise AssertionError("测试子进程不许调用真实模型或 Claude CLI")

models._create_llm = _forbidden
agent._claude_code_loop = _forbidden
agent._post_run_notify = lambda *a, **k: None
agent.maybe_generate_session_title = lambda *a, **k: None

sid = sys.argv[3]
sess = session.Session()
assert memory.load_session(sid, session=sess) is True
assert sess.project is None, "这条用例测的就是无项目会话"
sess.current_model_index = next(i for i, m in enumerate(MODEL_LIST)
                                if m[1] not in ("claude-code", "ollama"))
session.register(sess)
session.bind_thread(sess)
session.set_active(sess)
sess.chat_history.append(HumanMessage(content="把 app.py 改成 x = 99"))
memory.save_session(session=sess)

class _CommandThenDie:
    def invoke(self, args):
        with open("app.py", "w", encoding="utf-8") as f:     # 相对路径：落在进程工作目录
            f.write("x = 99\n")
            f.flush()
            os.fsync(f.fileno())
        os._exit(37)            # 副作用已发生、结果还没交回去

streaming.get_tool_map = lambda: {"run_command": _CommandThenDie()}
agent._stream_with_tools = lambda ui: (
    "", [{"name": "run_command", "args": {"command": "python fix.py"}, "id": "k1"}],
    {"input": 1, "output": 1, "total": 2}, None)

class _UI:
    def __getattr__(self, name):
        return lambda *a, **k: None

agent.agent_loop(_UI())
'''


class TestRecoveryCheckFailure:
    def test_a_crashing_check_on_a_plain_message_still_holds_the_obligation(
            self, isolated_memory, repo, monkeypatch):
        """恢复核对自己抛了异常：新消息照常处理，但"没核对成"不能变成"没有改动"。"""
        from src import agent as _agent
        sess = _code_session(repo)
        _bind(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="run_command", tool_call_id="cmd",
                                    args={"command": "x"})
        session.unbind_thread()

        def _broken(*a, **k):
            raise RuntimeError("核对时出了意外")

        monkeypatch.setattr(recovery, "inspect_site", _broken)
        again = _reopen(sess.current_session_id)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        stream = _Stream(("完成了。",))
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()
        assert stream.sent, "新消息照常处理，不被拦住"
        assert result.status == "unverified"
        assert any("核对时出了意外" in reason
                   for reason in again.pending_verification["tracking_incomplete"].values())
        assert _sidecar(isolated_memory, again.current_session_id)["operations"], \
            "没有交接成功就不摘执行前记录"

    def test_a_crashing_check_on_continue_starts_nothing(self, isolated_memory, repo, monkeypatch):
        from src import agent as _agent
        sess = _code_session(repo)
        run_records.begin_run(sess)

        def _broken(*a, **k):
            raise RuntimeError("核对时出了意外")

        monkeypatch.setattr(recovery, "inspect_site", _broken)
        again = _reopen(sess.current_session_id)
        _bind(again)
        stream = _Stream()
        monkeypatch.setattr(_agent, "_stream_with_tools", stream)
        try:
            result = _agent.agent_loop(_UI(), resume={"expected_run_id": again.last_run["id"],
                                                      "notes": []})
        finally:
            session.unbind_thread()
        assert result.status == "failed" and stream.sent == []


class TestNoProjectRealProcess:
    def test_crash_in_one_directory_is_verified_from_another_process(self, isolated_memory, repo,
                                                                     tmp_path, monkeypatch):
        """子进程在 repo 里跑命令、改完文件就死掉；本进程的工作目录是别处，重开后只说"继续"。

        盲区必须挂在**子进程当时的**工作目录上——拿本进程现在的目录去猜就是另一个地方。
        """
        from src import agent as _agent
        sess = _no_project_session()
        sid = sess.current_session_id
        script = tmp_path / "crash_no_project.py"
        script.write_text(_CRASH_NO_PROJECT, encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, str(script),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
             str(isolated_memory.parent), sid],
            cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=env)
        assert proc.returncode == 37, f"子进程该以 os._exit(37) 死掉: {proc.stderr[-2000:]}"
        assert (repo / "app.py").read_text(encoding="utf-8") == "x = 99\n"
        ops = _sidecar(isolated_memory, sid)["operations"]
        assert [op["tool"] for op in ops] == ["run_command"]
        assert os.path.normcase(ops[0]["work_root"]) == os.path.normcase(os.path.realpath(repo))

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)             # 本进程在另一个目录
        spy = _Spy()
        from src import streaming
        monkeypatch.setattr(streaming, "get_tool_map", lambda: {"run_command": spy})
        monkeypatch.setattr(_agent, "_stream_with_tools", _Stream(("看起来好了。",)))
        again = _reopen(sid)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()

        assert spy.calls == [], "结果未知的命令不重放"
        assert result.status == "unverified"
        keys = [os.path.normcase(k) for k in again.pending_verification["tracking_incomplete"]]
        assert os.path.normcase(os.path.realpath(repo)) in keys
        assert os.path.normcase(os.path.realpath(elsewhere)) not in keys, \
            "不能拿本进程现在的工作目录冒充当时的"


# ══════════════════════════════════════════════════════════════
# 2. 项目搬家
# ══════════════════════════════════════════════════════════════

def _relocate(sess, picked, confirm=lambda *a: True):
    from src.ui.chat_window import ChatUI
    return ChatUI._relocate_session_project(None, sess, picked, confirm=confirm)


class TestRelocatedProject:
    def _moved_with_blind_period(self, repo, tmp_path):
        _write_passing_test(repo)
        sess = _code_session(repo)
        _bind(sess)
        run_records.begin_run(sess)
        verification.mark_blind_period(sess.verification, os.path.realpath(repo),
                                       "unknown command effects")
        run_records.finalize_run(sess, sess.last_run, AgentResult("unverified"))
        session.unbind_thread()
        moved = tmp_path / "moved_project"
        repo.rename(moved)
        return sess, moved

    def test_after_moving_real_tests_and_diff_can_close_the_task(self, isolated_memory, repo,
                                                                 tmp_path, no_config_issues):
        """复核原场景：搬家、继续、真跑测试通过、真看 diff 干净 → 闸门必须放行。"""
        from src.tools import run_tests
        from src.tools_git import git_diff
        sess, moved = self._moved_with_blind_period(repo, tmp_path)
        ok, _ = _relocate(sess, str(moved))
        assert ok
        assert recovery.prepare_and_apply(sess, mode="continue", ui=_UI(),
                                          expected_run_id=sess.last_run["id"])
        _bind(sess)
        try:
            run_records.begin_run(sess)
            assert "✅" in run_tests.func(path="test_sample.py")
            git_diff.func()
            assert verification.get_verification_gaps(sess.verification) == []
        finally:
            session.unbind_thread()

    def test_active_paths_move_and_historical_anchor_stays(self, isolated_memory, repo, tmp_path,
                                                           no_config_issues):
        sess, moved = self._moved_with_blind_period(repo, tmp_path)
        old_real = os.path.normcase(os.path.realpath(repo))
        recorded_work_dir = sess.last_run["work_dir"]
        recorded_root = sess.last_run["workspace"]["root"]
        ok, _ = _relocate(sess, str(moved))
        assert ok
        keys = [os.path.normcase(k) for k in sess.pending_verification["tracking_incomplete"]]
        assert os.path.normcase(os.path.realpath(moved)) in keys
        assert old_real not in keys, "活动追踪路径跟着搬走"
        assert sess.last_run["work_dir"] == recorded_work_dir, "历史锚点保留原值用于诊断"
        assert sess.last_run["workspace"]["root"] == recorded_root
        pre = recovery.precheck(sess)
        assert any("已不存在" in n for n in pre.notes), "说清楚原目录没了，不是冒充隔离区"

    def test_an_unknown_write_recorded_at_the_old_location_follows_the_move(
            self, isolated_memory, repo, tmp_path, no_config_issues):
        sess = _code_session(repo)
        _bind(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="write_file", tool_call_id="w1",
                                    args={"path": "app.py"})
        session.unbind_thread()
        moved = tmp_path / "moved_project"
        repo.rename(moved)
        again = _reopen(sess.current_session_id)
        ok, _ = _relocate(again, str(moved))
        assert ok
        assert recovery.prepare_and_apply(again, mode="continue", ui=_UI(),
                                          expected_run_id=again.last_run["id"])
        keys = [os.path.normcase(k) for k in again.pending_verification["tracking_incomplete"]]
        assert os.path.normcase(os.path.realpath(moved)) in keys
        assert os.path.normcase(os.path.realpath(repo)) not in keys
        assert "app.py" in again.pending_verification["files"]

    def test_a_blind_period_on_a_vanished_directory_is_not_a_dead_end(self, isolated_memory, repo,
                                                                      tmp_path):
        """没搬家、只是那个目录没了：盲区照样要求补验证，但不能反复枚举一个不存在的目录。"""
        from src.tools import run_tests
        from src.tools_git import git_diff
        _write_passing_test(repo)
        gone = tmp_path / "gone-dir"
        sess = _code_session(repo)
        sess.pending_verification = {"files": [], "code_files": [], "reason": "", "run_id": "",
                                     "tracking_incomplete": {str(gone): "那里曾有未知写入"}}
        _bind(sess)
        try:
            run_records.begin_run(sess)
            assert verification.get_verification_gaps(sess.verification)
            run_tests.func(path="test_sample.py")
            git_diff.func()
            assert verification.get_verification_gaps(sess.verification) == []
            assert str(gone) not in (sess.verification.get("tracking_errors") or {})
        finally:
            session.unbind_thread()


# ══════════════════════════════════════════════════════════════
# 3. 坏锚点 / 坏进度不能拖垮聊天
# ══════════════════════════════════════════════════════════════

_BAD_VALUES = [123, "text", ["x"], {"k": 1}, True, None, 1.5]
_ANCHOR_FIELDS = ["root", "is_git", "git_head", "git_branch", "git_roots", "fingerprints",
                  "errors"]


def _legit(field, value):
    """锚点各字段的合法取值——与 workspace_anchor 的契约一致，用来判断哪些值该被降级。"""
    if field == "root":
        return isinstance(value, str)
    if field == "is_git":
        return isinstance(value, bool)
    if field in ("git_head", "git_branch"):
        return value is None or isinstance(value, str)
    if field == "git_roots":
        return value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value))
    if field == "errors":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if field == "fingerprints":
        return isinstance(value, dict) and not value
    return False


class TestCorruptAnchor:
    @pytest.mark.parametrize("field", _ANCHOR_FIELDS)
    def test_any_bad_anchor_field_degrades_only_the_anchor(self, isolated_memory, repo, field):
        sess = _code_session(repo)
        verification.mark_dirty(sess.verification, "app.py")
        run_records.begin_run(sess)
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for bad in _BAD_VALUES:
            data = json.loads(json.dumps(original))
            anchor = data["progress"]["last_run"]["workspace"]
            anchor[field] = bad
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            again = session.Session()
            assert memory.load_session(sid, session=again), f"{field}={bad!r} 让聊天打不开"
            assert any(isinstance(m, HumanMessage) for m in again.chat_history)
            assert again.progress_error == "", "只降级锚点，不作废整份进度"
            workspace = again.last_run["workspace"]
            if _legit(field, bad):
                assert "invalid" not in workspace, f"{field}={bad!r} 是合法值，不该被降级"
            else:
                assert set(workspace) == {"invalid"}, f"{field}={bad!r} 应当降级成'无法核对'"
            session.register(again)
            memory.save_session(session=again)            # 之后的保存也不能坏
            assert memory.load_session(sid, session=session.Session())

    def test_a_corrupt_anchor_is_reported_and_held_as_an_obligation(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        verification.mark_dirty(sess.verification, "app.py")
        run_records.finalize_run(sess, sess.last_run, AgentResult("cancelled", "停"))
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["progress"]["last_run"]["workspace"]["errors"] = 123
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = _reopen(sid)
        check = recovery.inspect_site(again)
        assert any("已损坏" in r for r in check.site["incomplete"])
        recovery.apply_resume(again, check)
        assert "已损坏" in again.chat_history[-1].content
        run_records.begin_run(again)
        blind = again.verification.get("unknown_changes") or {}
        assert any("已损坏" in reason for reason in blind.values()), "核对不了就要求补验证"

    @pytest.mark.parametrize("where,value", [
        (("last_run",), 5), (("last_run", "evidence"), "x"), (("last_run", "model"), [1]),
        (("last_run", "source"), 3), (("pending_verification",), "p"),
        (("pending_verification", "tracking_incomplete"), [1]),
        (("recent_operations",), "r"), (("last_committed_operation",), 9),
        (("current_plan",), {"a": 1}), (("task_ledger",), 4),
    ])
    def test_no_progress_corruption_keeps_the_chat_from_loading(self, isolated_memory, repo,
                                                                where, value):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        node = data["progress"]
        for key in where[:-1]:
            node = node[key]
        node[where[-1]] = value
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = session.Session()
        assert memory.load_session(sid, session=again)
        assert [m.content for m in again.chat_history if isinstance(m, HumanMessage)] \
            == ["修一下登录"]
        session.register(again)
        memory.save_session(session=again)

    def test_normalization_never_raises_even_on_unexpected_errors(self, monkeypatch):
        """最后一道：各字段校验漏掉的意外异常，也只能让进度作废，不能往外抛。"""
        def _explode(raw):
            raise RuntimeError("意料之外")
        monkeypatch.setattr(run_records, "normalize_last_run", _explode)
        progress, why = memory._normalize_progress({"last_run": {"id": "r"}}, "sid")
        assert why and "RuntimeError" in why
        assert progress["last_run"] is None and progress["current_plan"] == []


# ══════════════════════════════════════════════════════════════
# 顺带：sidecar 里解析不了的条目
# ══════════════════════════════════════════════════════════════

def _sidecar_with(isolated_memory, sess, operations):
    path = run_records.inflight_path(sess.current_session_id)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "session_id": sess.current_session_id,
                   "operations": operations}, stream)


class TestMalformedSidecarEntry:
    def test_a_lone_corrupt_entry_is_not_read_as_no_pending_operation(self, isolated_memory, repo,
                                                                      monkeypatch):
        """唯一那条记录坏了：以前被静默滤掉，等于"没有未决操作"，按 completed 收尾。"""
        from src import agent as _agent
        sess = _code_session(repo)
        run_records.begin_run(sess)
        _sidecar_with(isolated_memory, sess, [{"tool": "run_command"}])   # 缺 operation_id
        again = _reopen(sess.current_session_id)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        monkeypatch.setattr(_agent, "_stream_with_tools", _Stream(("完成了。",)))
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()
        assert result.status == "unverified"
        note = next(m for m in again.chat_history
                    if isinstance(m, HumanMessage)
                    and m.additional_kwargs.get("lingxi_kind") == recovery.RECOVERY_KIND)
        assert "无法解析" in note.content
        assert note.additional_kwargs["lingxi_recovery"]["unreadable_entries"], "原文预览留底"
        assert _sidecar(isolated_memory, again.current_session_id) is None, "交接之后才摘掉"

    def test_writing_the_sidecar_does_not_erase_a_corrupt_entry(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        _sidecar_with(isolated_memory, sess, [123])
        op = run_records.begin_operation(sess, tool="write_file", tool_call_id="w1",
                                         args={"path": "a.py"})
        assert 123 in _sidecar(isolated_memory, sess.current_session_id)["operations"]
        run_records._clear_operation(sess, sess.current_session_id, op.operation_id)
        assert _sidecar(isolated_memory, sess.current_session_id)["operations"] == [123], \
            "还剩解析不了的条目就不能删文件"

    def test_classification_reports_corrupt_entries_as_unreadable(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        _sidecar_with(isolated_memory, sess, [{"operation_id": 5}, "junk"])
        entries, error = run_records.classify_inflight(sess.current_session_id)
        assert error == ""
        assert [e["status"] for e in entries] == ["unreadable", "unreadable"]
