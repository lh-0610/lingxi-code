"""B04 第二轮复核（02f22ec）指出的三处。

1. **A 目录的义务能被 B 目录的检查清掉**：义务按目录记着，放行却只看全局的 tests_passed /
   diff_reviewed。实测：命令在 A 改了文件后中断，在 B 重开、只在 B 跑测试看 diff，真实
   agent_loop 返回 completed，A 的改动从没被检查过。现在验证状态记下测试 / diff **实际在哪儿做的**
   （`tests_roots` / `diff_roots`）和改动文件的实际位置（`dirty_abs`，随义务跨轮持久化），
   放行要求检查覆盖义务所在的位置；不覆盖就保持未验证，并说明该去哪儿补查。
2. **cd 之后 work_root 记错**：`run_command` / `run_tests` / `check_code` 在 `_shell_cwd()` 里
   执行，执行前记录却一律按 `_project_cwd()` 记。现在按工具自己的目录规则记。
3. **损坏条目的"留底"丢原文**：只存每条前 300 字、最多 20 条，随后却清掉全部原文。
   现在原文整份写进单独的隔离文件，历史里只放摘要和引用；留底失败就不摘。
"""
import inspect
import json
import os
import sys

import pytest
from langchain_core.messages import HumanMessage

from src import memory, recovery, run_records, session, state, verification

from test_b04_recovery import _UI, _bind, _code_session, _make_repo, _reopen, _sidecar
from test_b04_review_fixes import _no_project_session, _write_passing_test


@pytest.fixture()
def repo(tmp_path):
    return _make_repo(tmp_path / "proj")


@pytest.fixture(autouse=True)
def _no_real_models_or_services(monkeypatch):
    from src import agent as _agent
    from src import claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(_agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    monkeypatch.setattr(_agent, "maybe_generate_session_title", lambda *a, **k: None)
    monkeypatch.setattr(_agent, "_post_run_notify", lambda *a, **k: None)


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ══════════════════════════════════════════════════════════════
# 1. 检查必须覆盖义务所在的目录
# ══════════════════════════════════════════════════════════════

class _ChecksThenDone:
    """模型桩：第一次调用要求 run_tests + git_diff（真工具），之后说完成了。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, ui):
        self.calls += 1
        if self.calls == 1:
            return "", [{"name": "run_tests", "args": {"path": "test_sample.py"}, "id": "t"},
                        {"name": "git_diff", "args": {}, "id": "d"}], \
                {"input": 1, "output": 1, "total": 2}, None
        return "完成了。", [], {"input": 1, "output": 1, "total": 2}, None


class TestChecksMustCoverTheObligation:
    def _interrupted_in(self, a_dir, monkeypatch):
        """无项目会话在 a_dir 里跑命令、改了文件后中断（sidecar 里没有回执）。"""
        monkeypatch.chdir(a_dir)
        sess = _no_project_session()
        _bind(sess)
        run_records.begin_run(sess)
        run_records.begin_operation(sess, tool="run_command", tool_call_id="unknown", args={})
        (a_dir / "app.py").write_text("x = 99\n", encoding="utf-8")
        session.unbind_thread()
        return sess.current_session_id

    @pytest.mark.parametrize("same_dir", [False, True])
    def test_checks_elsewhere_cannot_close_an_obligation_here(self, isolated_memory, repo,
                                                              tmp_path, monkeypatch, same_dir):
        """复核原场景：只在 B 跑测试看 diff，不能了结 A 的义务；同目录的对照组照常放行。"""
        from src import agent as _agent
        other = _make_repo(tmp_path / "other")
        _write_passing_test(repo)
        _write_passing_test(other)
        sid = self._interrupted_in(repo, monkeypatch)
        monkeypatch.chdir(repo if same_dir else other)
        again = _reopen(sid)
        _bind(again)
        again.chat_history.append(HumanMessage(content="继续"))
        monkeypatch.setattr(_agent, "_stream_with_tools", _ChecksThenDone())
        try:
            result = _agent.agent_loop(_UI())
        finally:
            session.unbind_thread()
        if same_dir:
            assert result.status == "completed", "在义务所在目录真跑检查就应当放行"
            return
        assert result.status == "unverified"
        gaps = "\n".join(verification.gaps_from_state(again.verification))
        assert "没有覆盖" in gaps and os.path.realpath(repo) in gaps, "要说清楚该去哪儿补查"

    def test_cd_back_and_test_there_covers_it(self, isolated_memory, repo, tmp_path,
                                              monkeypatch):
        """按提示 cd 回 A 补跑测试：测试这一项就了结了（diff 那项在无项目会话里仍按提示处理）。"""
        from src.tools import run_command, run_tests
        from src.tools_git import git_diff
        other = _make_repo(tmp_path / "other")
        _write_passing_test(repo)
        _write_passing_test(other)
        sid = self._interrupted_in(repo, monkeypatch)
        monkeypatch.chdir(other)
        monkeypatch.setattr(state, "ui_ref", None)
        again = _reopen(sid)
        _bind(again)
        try:
            recovery.prepare_and_apply(again, mode="message", ui=_UI())
            run_records.begin_run(again)
            run_tests.func(path="test_sample.py")          # 在 B 跑
            git_diff.func()                                # 无项目会话：git_diff 在进程目录 B 执行
            gaps = "\n".join(verification.get_verification_gaps(again.verification))
            assert "测试是在" in gaps and "没有覆盖" in gaps
            run_command.func(command=f"cd {repo}")
            run_tests.func()                              # 回到 A 跑（run_tests 跟随 cd）
            gaps = "\n".join(verification.get_verification_gaps(again.verification))
            assert "测试是在" not in gaps, "A 的测试这一项已经覆盖"
            assert "git_diff 查看的是" in gaps, "diff 还是在 B 看的，照实说"
        finally:
            session.unbind_thread()

    def test_in_run_edits_are_not_covered_by_tests_run_elsewhere(self, isolated_memory, repo,
                                                                 tmp_path, monkeypatch):
        """不只恢复出来的义务：本轮改了项目里的文件、cd 到别处跑测试，同样不能算测过。"""
        from src.tools import run_command, run_tests, write_file
        from src.tools_git import git_diff
        other = _make_repo(tmp_path / "other")
        _write_passing_test(other)
        _write_passing_test(repo)
        sess = _code_session(repo)
        monkeypatch.setattr(state, "ui_ref", None)
        _bind(sess)
        try:
            run_records.begin_run(sess)
            write_file.func(path="app.py", content="x = 5\n")
            git_diff.func()
            run_command.func(command=f"cd {other}")
            run_tests.func()
            gaps = "\n".join(verification.get_verification_gaps(sess.verification))
            assert "没有覆盖" in gaps and "app.py" in gaps
            run_command.func(command=f"cd {repo}")
            run_tests.func()
            assert verification.get_verification_gaps(sess.verification) == []
        finally:
            session.unbind_thread()

    def test_tests_in_a_subdirectory_only_cover_that_subdirectory(self, tmp_path):
        v = verification.new_verification()
        root = tmp_path / "p"
        (root / "sub").mkdir(parents=True)
        verification.mark_dirty(v, "sub/a.py", abs_path=str(root / "sub" / "a.py"))
        verification.mark_dirty(v, "b.py", abs_path=str(root / "b.py"))
        verification.mark_tests(v, True, root=str(root / "sub"))
        verification.mark_diff_reviewed(v, root=str(root))
        tests_gaps = [g for g in verification.gaps_from_state(v) if g.startswith("测试是在")]
        assert len(tests_gaps) == 1
        uncovered = tests_gaps[0].split("没有覆盖", 1)[1]
        assert os.path.realpath(root / "b.py") in uncovered
        assert os.path.realpath(root / "sub" / "a.py") not in uncovered
        assert not any(g.startswith("git_diff 查看的是") for g in verification.gaps_from_state(v))

    def test_a_path_limited_diff_only_covers_that_path(self, tmp_path):
        v = verification.new_verification()
        verification.mark_dirty(v, "a.txt", abs_path=str(tmp_path / "a.txt"))
        verification.mark_dirty(v, "b.txt", abs_path=str(tmp_path / "b.txt"))
        verification.mark_diff_reviewed(v, root=str(tmp_path / "a.txt"))
        gaps = "\n".join(verification.gaps_from_state(v))
        assert "git_diff 查看的是" in gaps and "b.txt" in gaps

    def test_file_locations_survive_a_restart_and_are_still_enforced(self, isolated_memory, repo,
                                                                     tmp_path, monkeypatch):
        """位置跟着义务跨轮：只存相对路径的话，下一轮换个目录跑检查就又能蒙混过去。"""
        from src.agent_result import AgentResult
        other = _make_repo(tmp_path / "other")
        sess = _code_session(repo)
        _bind(sess)
        try:
            run_records.begin_run(sess)
            (repo / "app.py").write_text("x = 3\n", encoding="utf-8")
            from src import tools_common
            tools_common._mark_current_dirty(str(repo / "app.py"))
            run_records.finalize_run(sess, sess.last_run, AgentResult("unverified", "没测"))
        finally:
            session.unbind_thread()
        stored = _read_pending(isolated_memory, sess.current_session_id)
        assert _same(stored["file_paths"]["app.py"], repo / "app.py")

        again = _reopen(sess.current_session_id)
        _bind(again)
        try:
            run_records.begin_run(again)
            verification.mark_tests(again.verification, True, root=str(other))
            verification.mark_diff_reviewed(again.verification, root=str(other))
            gaps = "\n".join(verification.gaps_from_state(again.verification))
            assert "没有覆盖" in gaps and "app.py" in gaps
        finally:
            session.unbind_thread()

    def test_legacy_pending_without_locations_still_loads(self, isolated_memory, repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["progress"]["pending_verification"] = {"files": ["app.py"], "code_files": ["app.py"],
                                                    "tracking_incomplete": {}, "reason": "",
                                                    "run_id": ""}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = _reopen(sid)
        assert again.progress_error == ""
        assert again.pending_verification["file_paths"] == {}

    def test_malformed_locations_are_rejected_like_the_rest_of_pending(self, isolated_memory,
                                                                        repo):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        path = isolated_memory / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["progress"]["pending_verification"]["file_paths"] = {"app.py": 5}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        again = _reopen(sid)
        assert "file_paths" in again.progress_error, "读不懂就报出来，不悄悄放宽"
        assert any(isinstance(m, HumanMessage) for m in again.chat_history)

    def test_a_vanished_blind_root_still_is_not_a_dead_end(self, tmp_path):
        """按位置核对不能把"目录已不存在"的盲区变回死结（上一轮修掉的那一类）。"""
        # 检查目录与那个已消失的目录毫无包含关系：能放行只可能是因为"目录不存在就不按位置核对"
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        gone = tmp_path / "gone"
        v = verification.new_verification()
        verification.mark_blind_period(v, str(gone), "未知写入")
        verification.mark_tests(v, True, root=str(elsewhere))
        verification.mark_diff_reviewed(v, root=str(elsewhere))
        assert verification.gaps_from_state(v) == []
        gone.mkdir()        # 对照：目录还在时，别处的检查不算覆盖
        assert any("没有覆盖" in g for g in verification.gaps_from_state(v))

    def test_a_relocated_obligation_is_checked_at_the_new_place(self, isolated_memory, repo,
                                                                tmp_path):
        """搬家后义务已迁到新位置：在新位置检查放行，在别处检查不放行。"""
        from src.ui.chat_window import ChatUI
        from src.agent_result import AgentResult
        other = _make_repo(tmp_path / "other")
        sess = _code_session(repo)
        _bind(sess)
        try:
            run_records.begin_run(sess)
            from src import tools_common
            (repo / "app.py").write_text("x = 4\n", encoding="utf-8")
            tools_common._mark_current_dirty(str(repo / "app.py"))
            run_records.finalize_run(sess, sess.last_run, AgentResult("unverified"))
        finally:
            session.unbind_thread()
        moved = tmp_path / "moved"
        repo.rename(moved)
        ok, _ = ChatUI._relocate_session_project(None, sess, str(moved), confirm=lambda *a: True)
        assert ok
        assert _same(sess.pending_verification["file_paths"]["app.py"], moved / "app.py")
        v = verification.new_verification()
        verification.restore_obligations(v, sess.pending_verification)
        verification.mark_tests(v, True, root=str(other))
        verification.mark_diff_reviewed(v, root=str(other))
        assert any("没有覆盖" in g for g in verification.gaps_from_state(v))
        v2 = verification.new_verification()
        verification.restore_obligations(v2, sess.pending_verification)
        verification.mark_tests(v2, True, root=str(moved))
        verification.mark_diff_reviewed(v2, root=str(moved))
        assert not any("没有覆盖" in g for g in verification.gaps_from_state(v2))


def _read_pending(mem_dir, sid):
    with open(os.path.join(str(mem_dir), f"{sid}.json"), encoding="utf-8") as stream:
        return json.load(stream)["progress"]["pending_verification"]


# ══════════════════════════════════════════════════════════════
# 2. 按工具自己的目录规则记 work_root
# ══════════════════════════════════════════════════════════════

class TestWorkRootFollowsTheToolsOwnRule:
    def test_a_command_after_cd_is_recorded_where_it_actually_ran(self, isolated_memory, repo,
                                                                  tmp_path, monkeypatch):
        """复核原场景：项目是 A，cd B 之后真实命令写在 B——记录必须是 B。"""
        from src import streaming
        from src.tools import run_command
        other = _make_repo(tmp_path / "actual-command-cwd")
        sess = _code_session(repo)
        _bind(sess)
        monkeypatch.setattr(state, "ui_ref", None)
        seen = {}

        class _Crash(BaseException):
            pass

        class _RealCommandThenCrash:
            def invoke(self, args):
                seen["work_root"] = _sidecar(isolated_memory, sess.current_session_id)[
                    "operations"][0]["work_root"]
                seen["output"] = run_command.func(**args)
                raise _Crash()          # 结果交不回去，等同进程死掉

        try:
            run_records.begin_run(sess)
            run_command.func(command=f"cd {other}")
            monkeypatch.setattr(streaming, "get_tool_map",
                                lambda: {"run_command": _RealCommandThenCrash()})
            command = (f'"{sys.executable}" -c "from pathlib import Path; '
                       f"Path('actual.txt').write_text('changed')\"")
            with pytest.raises(_Crash):
                streaming._execute_tool({"name": "run_command", "args": {"command": command},
                                         "id": "c"}, _UI())
        finally:
            session.unbind_thread()
        assert (other / "actual.txt").read_text(encoding="utf-8") == "changed"
        assert not (repo / "actual.txt").exists()
        assert _same(seen["work_root"], other)

    def test_file_tools_still_resolve_against_the_project(self, isolated_memory, repo, tmp_path,
                                                          monkeypatch):
        """文件工具的相对路径按项目根解析，cd 不影响它们——记录也照此写。"""
        from src.tools import run_command
        other = _make_repo(tmp_path / "elsewhere")
        sess = _code_session(repo)
        _bind(sess)
        monkeypatch.setattr(state, "ui_ref", None)
        try:
            run_records.begin_run(sess)
            run_command.func(command=f"cd {other}")
            run_records.begin_operation(sess, tool="write_file", tool_call_id="w",
                                        args={"path": "a.py"})
            run_records.begin_operation(sess, tool="run_tests", tool_call_id="t", args={})
        finally:
            session.unbind_thread()
        ops = {op["tool"]: op for op in _sidecar(isolated_memory, sess.current_session_id)[
            "operations"]}
        assert _same(ops["write_file"]["work_root"], repo)
        assert _same(ops["run_tests"]["work_root"], other), "run_tests 也跟随 cd"

    def test_the_shell_cwd_tool_list_matches_the_implementations(self):
        """清单与实现同步：tools.py 里调用 _shell_cwd() 的实现，恰好就是清单里的那几个工具。

        新加一个在 shell 目录执行的工具却忘了登记，执行前记录就会按项目根记错位置。
        """
        from src import tools, tools_common
        callers = sorted(name for name, fn in inspect.getmembers(tools, inspect.isfunction)
                         if fn.__module__ == tools.__name__
                         and "_shell_cwd()" in inspect.getsource(fn))
        # 实现函数 → 对外的工具名
        implemented_by = {"_run_command": "run_command", "_run_code_check": "check_code"}
        tool_callers = {implemented_by.get(n, n) for n in callers}
        for tool_obj in tools.ALL_TOOLS:
            fn = getattr(tool_obj, "func", None)
            if fn is not None and "_shell_cwd()" in inspect.getsource(fn):
                tool_callers.add(tool_obj.name)
        assert tool_callers == set(tools_common.SHELL_CWD_TOOLS), \
            f"调用 _shell_cwd() 的实现：{sorted(tool_callers)}；清单：{sorted(tools_common.SHELL_CWD_TOOLS)}"


# ══════════════════════════════════════════════════════════════
# 3. 损坏条目：展示可以截断，留底必须完整
# ══════════════════════════════════════════════════════════════

def _write_sidecar(mem_dir, sid, operations):
    with open(run_records.inflight_path(sid), "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "session_id": sid, "operations": operations}, stream)


def _raw_entries(n=21):
    return [{"tool": "run_command", "note": "x" * 350 + f"unique-tail-{i}"} for i in range(n)]


class TestMalformedEntriesAreArchivedInFull:
    def test_every_original_survives_in_the_archive(self, isolated_memory, repo):
        """复核原场景：21 条损坏记录，每条带超出预览长度的尾巴——摘除后原文一条不能少。"""
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        raw = _raw_entries()
        _write_sidecar(isolated_memory, sid, raw)
        again = _reopen(sid)
        assert recovery.prepare_and_apply(again, mode="message", ui=_UI())

        disk = json.loads((isolated_memory / f"{sid}.json").read_text(encoding="utf-8"))
        record = [m["lingxi_recovery"] for m in disk["messages"] if m.get("lingxi_recovery")][-1]
        assert record["unreadable_count"] == 21
        assert len(record["unreadable_entries"]) <= 20, "历史里只放摘要"
        archive = isolated_memory / record["unreadable_archive"]
        assert archive.exists(), "恢复说明里引用的隔离文件必须真的在"
        stored = json.loads(archive.read_text(encoding="utf-8"))
        assert stored["entries"] == raw, "原文完整、条数不少、内容不截"
        assert _sidecar(isolated_memory, sid) is None, "留底成功之后才摘"

    def test_a_failed_archive_keeps_the_originals_in_the_sidecar(self, isolated_memory, repo,
                                                                 monkeypatch):
        sess = _code_session(repo)
        run_records.begin_run(sess)
        sid = sess.current_session_id
        raw = _raw_entries(3)
        _write_sidecar(isolated_memory, sid, raw)
        again = _reopen(sid)
        real_write = memory._atomic_write_json

        def _fail_archive(path, doc):
            if ".inflight.unreadable-" in os.path.basename(path):
                raise OSError("磁盘满了")
            return real_write(path, doc)

        monkeypatch.setattr(memory, "_atomic_write_json", _fail_archive)
        assert recovery.prepare_and_apply(again, mode="message", ui=_UI())
        assert _sidecar(isolated_memory, sid)["operations"] == raw, "没留底就不能摘原文"
        note = again.chat_history[-1]
        assert "原文暂时留在执行前记录里" in note.content
        assert note.additional_kwargs["lingxi_recovery"]["unreadable_archive"] == ""

    def test_archiving_the_same_entries_twice_does_not_multiply_files(self, isolated_memory,
                                                                      repo):
        sess = _code_session(repo)
        sid = sess.current_session_id
        raw = _raw_entries(2)
        first, _ = run_records.archive_malformed(sid, raw)
        second, _ = run_records.archive_malformed(sid, raw)
        assert first == second
        archives = [p for p in os.listdir(isolated_memory) if ".inflight.unreadable-" in p]
        assert archives == [first]

    def test_only_archived_entries_are_removed(self, isolated_memory, repo):
        sess = _code_session(repo)
        sid = sess.current_session_id
        archived, late = {"tool": "a"}, {"tool": "b-appeared-later"}
        _write_sidecar(isolated_memory, sid, [archived, late])
        run_records.drop_malformed(sess, sid, [archived])
        assert _sidecar(isolated_memory, sid)["operations"] == [late]

    def test_deleting_the_session_removes_its_archives(self, isolated_memory, repo):
        sess = _code_session(repo)
        sid = sess.current_session_id
        name, _ = run_records.archive_malformed(sid, _raw_entries(1))
        assert (isolated_memory / name).exists()
        memory.delete_session(sid)
        assert not (isolated_memory / name).exists()
