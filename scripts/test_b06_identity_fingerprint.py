"""检查身份必须用指纹，不能用被截断的展示数据（B06 第三轮复核）。

展示字段是要脱敏和截断的（argv 每项 300 字、命令 400 字）。拿它们比对身份，
长参数的差异会被整个抹掉：两组 `pytest -k <700 余字符>` 只有末尾不同，截断后
一模一样，于是第一组的失败被第二组"取代"、卡片写成"已执行检查通过"——
而第二组根本没跑那个失败用例。
"""
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, result_view, run_records, session, verification
from src.agent_result import AgentResult


# 比 _ARGV_ITEM_MAX(300) 长很多，确保截断一定发生
_LONG_PREFIX = "test_common_" + "x" * 700


def _new_session():
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="跑测试"),
                         AIMessage(content="好")]
    session.register(sess)
    return sess


def _saved(sess):
    memory.save_session(session=sess)
    return sess.current_session_id


def _finish(sess, status="completed"):
    return run_records.finalize_run(sess, sess.last_run, AgentResult(status)).snapshot


def _record(sess, *, status, exit_code, k_expr, cwd="/proj"):
    return verification.record_evidence(
        sess.verification, kind="tests", checker="pytest", cwd=cwd,
        status=status, exit_code=exit_code,
        argv=["python", "-m", "pytest", "-k", k_expr])


class TestFingerprintDrivesIdentity:
    def test_long_args_differing_only_at_the_end_are_not_merged(self, isolated_memory):
        """两条只在末尾不同的超长 -k：截断后展示字段一样，身份必须仍然不同。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        first = _record(sess, status="failed", exit_code=1,
                        k_expr=_LONG_PREFIX + " or test_failure")
        second = _record(sess, status="passed", exit_code=0,
                         k_expr=_LONG_PREFIX + " or test_success")

        # 前置条件：展示用的 argv 确实已经被截断成一样的了
        assert first["argv"] == second["argv"], "这条用例的前提是展示字段相同"
        assert first["identity"] != second["identity"], "指纹必须区分开"

        snapshot = _finish(sess)
        assert result_view.summarize_checks(snapshot) == "unresolved", (
            "第二组没跑那个失败用例，不能替第一组背书")
        view = result_view.describe(snapshot)
        assert view["title"] == "本轮执行结束，检查未全部通过"
        assert all(not r["superseded"] for r in view["validations"])

    def test_identical_long_args_retry_still_supersedes(self, isolated_memory):
        """同一条超长命令重试通过：该取代还是要取代，别把功能一起挡掉。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        same = _LONG_PREFIX + " or test_failure"
        first = _record(sess, status="failed", exit_code=1, k_expr=same)
        second = _record(sess, status="passed", exit_code=0, k_expr=same)

        assert first["identity"] == second["identity"]
        snapshot = _finish(sess)
        assert result_view.summarize_checks(snapshot) == "clear"
        view = result_view.describe(snapshot)
        assert view["validations"][0]["superseded"] is True
        assert view["validations"][1]["superseded"] is False

    def test_same_command_different_directory_is_not_merged(self, isolated_memory):
        """目录进指纹：主仓库跑过不代表隔离区也过了。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        same = _LONG_PREFIX + " or test_failure"
        first = _record(sess, status="failed", exit_code=1, k_expr=same, cwd="/main")
        second = _record(sess, status="passed", exit_code=0, k_expr=same, cwd="/worktree")
        assert first["identity"] != second["identity"]
        assert result_view.summarize_checks(_finish(sess)) == "unresolved"

    def test_fingerprint_is_stable_for_the_same_inputs(self):
        args = dict(kind="tests", checker="pytest", cwd="/p", path="",
                    argv=["python", "-m", "pytest"], command="")
        assert (verification.identity_fingerprint(**args)
                == verification.identity_fingerprint(**args))

    def test_fingerprint_does_not_leak_the_command(self, isolated_memory):
        """指纹是单向哈希：完整命令里带密钥也不会因为存了指纹而泄露。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        record = verification.record_evidence(
            sess.verification, kind="check", checker="check_command", cwd="/p",
            status="passed", exit_code=0,
            command="run --api_key=SUPERSECRETVALUE" + "y" * 500)
        assert "SUPERSECRETVALUE" not in record["identity"]
        assert "SUPERSECRETVALUE" not in json.dumps(record, ensure_ascii=False)

    def test_long_custom_command_differing_past_the_cut_is_not_merged(self, isolated_memory):
        """自定义命令按 400 字截断，超出部分的差异同样不能被抹掉。"""
        sess = _new_session()
        _saved(sess)
        run_records.begin_run(sess)
        base = "check " + "z" * 500
        first = verification.record_evidence(
            sess.verification, kind="check", checker="check_command", cwd="/p",
            status="failed", exit_code=1, command=base + " --target=alpha")
        second = verification.record_evidence(
            sess.verification, kind="check", checker="check_command", cwd="/p",
            status="passed", exit_code=0, command=base + " --target=beta")

        assert first["command"] == second["command"], "前提：展示用的命令已被截断成一样"
        assert first["identity"] != second["identity"]
        assert result_view.summarize_checks(_finish(sess)) == "unresolved"


class TestFingerprintPersistence:
    def test_fingerprint_survives_save_and_reload(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        same = _LONG_PREFIX + " or test_failure"
        _record(sess, status="failed", exit_code=1, k_expr=same)
        _record(sess, status="passed", exit_code=0, k_expr=same)
        live = _finish(sess)

        restored = session.Session()
        memory.load_session(sid, session=restored)
        loaded = run_records.snapshot_from_loaded(restored)

        live_fps = [r["identity"] for r in result_view.validation_rows(live)]
        loaded_fps = [r["identity"] for r in result_view.validation_rows(loaded)]
        assert loaded_fps == live_fps and all(loaded_fps)
        assert result_view.summarize_checks(loaded) == "clear", "重载后归并结论要一致"

    def test_reloaded_distinct_fingerprints_stay_distinct(self, isolated_memory):
        sess = _new_session()
        sid = _saved(sess)
        run_records.begin_run(sess)
        _record(sess, status="failed", exit_code=1, k_expr=_LONG_PREFIX + " or test_failure")
        _record(sess, status="passed", exit_code=0, k_expr=_LONG_PREFIX + " or test_success")
        _finish(sess)

        restored = session.Session()
        memory.load_session(sid, session=restored)
        loaded = run_records.snapshot_from_loaded(restored)
        assert result_view.summarize_checks(loaded) == "unresolved"

    def test_legacy_records_without_a_fingerprint_never_supersede(self, isolated_memory):
        """本批之前存下的记录没有指纹 —— 各算各的，不靠截断数据蒙一个"通过"。"""
        sid = "20240101_000000_007700"
        run = {
            "version": 1, "id": "run-legacy", "phase": "ended", "outcome": "completed",
            "evidence": {"changed_files": [], "diff_reviewed": False, "change_revision": 0,
                         "validation_runs": [
                             {"id": "ev-1", "kind": "tests", "checker": "pytest",
                              "status": "failed", "exit_code": 1, "cwd": "/p",
                              "argv": ["py", "-m", "pytest"]},
                             {"id": "ev-2", "kind": "tests", "checker": "pytest",
                              "status": "passed", "exit_code": 0, "cwd": "/p",
                              "argv": ["py", "-m", "pytest"]}]},
        }
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "旧", "updated": "2024-01-01", "schema_version": 2,
            "progress": {"version": 1, "last_run": run},
            "messages": [{"type": "HumanMessage", "content": "你好"},
                         {"type": "AIMessage", "content": "在"}],
        }, ensure_ascii=False), encoding="utf-8")

        sess = session.Session()
        assert memory.load_session(sid, session=sess) is True
        snapshot = run_records.snapshot_from_loaded(sess)
        rows = result_view.validation_rows(snapshot)
        assert [r["identity"] for r in rows] == ["", ""]
        assert result_view.summarize_checks(snapshot) == "unresolved", (
            "没有指纹就不合并——保守方向")
