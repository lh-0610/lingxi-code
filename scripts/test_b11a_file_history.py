"""B11a 文件改动的安全撤销：真实写入入口 + 预检/执行接口的全场景回归。

覆盖（对应任务 §八）：
- 已跟踪 / 未跟踪 / 新建文件的写入与逐字节恢复（CRLF、BOM 原样保留）；
- 同一文件连续修改的逐次撤销，以及跳过后续修改的拒绝；
- AI 修改后用户手动修改、预检后再次修改 → 冲突拒绝；
- 两个会话操作同一文件、不同工作区、linked worktree 的归属隔离；
- 拒绝、确认期间被外部修改、写入失败、备份/写后记录失败；
- 恢复材料缺失、损坏、路径越界、目标变符号链接/junction；
- Git 暂存区 / 用户 stash / 无关文件不变；撤销后旧验证结论作废并落盘；
- 新解释器读盘后预检 + 恢复（跨进程以实际保存的材料为准）。

写入一律走 `streaming._execute_tool`（真实调用入口，含 B03 执行前记录与提交），
不直接调工具函数；测试不碰真实模型 / Claude CLI / 网络（主动阻断夹具）。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time

import pytest

from src import file_history as fh
from src import memory, run_records, session, state


# ══════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    """任何用例都不许碰真实模型、本地 Claude CLI 或网络（主动阻断，不靠默认不被调用）。"""
    from src import agent, claude_code, models

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型或 Claude CLI")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    import socket
    monkeypatch.setattr(socket.socket, "connect", _forbidden)


@pytest.fixture(autouse=True)
def _memory_write_guard(monkeypatch, isolated_memory):
    """所有 chat_memory 写入（会话正文 / index / sidecar / 撤销记录）都必须落在
    隔离数据根内。本文件的工具调用都在主线程，主线程的隔离根即为全部线程的根；
    线程类用例显式把主线程取好的根带进线程。越界当场失败。"""
    import src.paths as _paths
    from src import memory as _memory
    expected = os.path.normcase(str(_paths.memory_dir()))
    bad = []
    real_write = _memory._atomic_write_json

    def _spy_write(path, *a, **k):
        d = os.path.normcase(os.path.dirname(os.path.abspath(str(path))))
        if os.path.commonpath([d, expected]) != expected:
            bad.append(d)
        return real_write(path, *a, **k)

    monkeypatch.setattr(_memory, "_atomic_write_json", _spy_write)
    yield
    assert not bad, f"chat_memory 写入越界 {len(bad)} 次: {sorted(set(bad))[:3]}"


@pytest.fixture()
def workspace(tmp_path):
    """隔离的项目工作区（非 Git；需要 Git 的用例自建仓库）。"""
    proj = tmp_path / "ws"
    proj.mkdir()
    state.current_project = str(proj)
    return proj


def _make_saved_session(project):
    """把 active 会话按生产语义落盘拿到 id，并绑定到当前线程（工具执行入口要求）。

    首次 save 会把会话锚定到**当时的全局 current_project**（memory.save_session 的
    生产语义），所以这里同步设置全局当前项目，否则工具落点会回到上一个项目。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    state.current_project = str(project)
    sess = session.get_active()
    sess.project = str(project)
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="改一下文件")]
    memory.save_session(session=sess)
    assert sess.current_session_id
    assert os.path.normcase(sess.project or "") == os.path.normcase(str(project))
    session.bind_thread(sess)
    return sess


def _second_session(project):
    """再开一个真实会话（独立 session_id）：多会话归属用例用。"""
    sess = session.Session()
    session.set_active(sess)
    session.register(sess)
    return _make_saved_session(project)


class _FakeUI:
    """_execute_tool 需要的最小 UI：收集消息；confirm_edit 默认放行。"""

    def __init__(self, approve=True):
        self.messages = []
        self.approve = approve
        self.on_confirm = None      # 可注入"确认期间做点别的"（模拟用户/外部改动）

    def show_message(self, text, tag=""):
        self.messages.append((tag, str(text)))

    def confirm_edit(self, full, diff_text):
        if self.on_confirm is not None:
            self.on_confirm()
        return self.approve, None


_call_counter = {"n": 0}


def _drive(name, args, ui=None):
    """真实调用入口：streaming._execute_tool。返回 (工具结果文本, fake ui)。

    生产环境里文件工具只在一轮运行（begin_run 之后）内执行；这里同样给当前
    会话设置运行身份，让 B03 记录与 B11a 恢复记录拿到真实的 run_id。"""
    from src import streaming

    _call_counter["n"] += 1
    ui = ui or _FakeUI()
    sess = session.current_session()
    if getattr(sess, "active_run_id", None) is None:
        sess.active_run_id = f"run-test-{_call_counter['n']}"
    tc = {"name": name, "args": args, "id": f"call-{_call_counter['n']}"}
    streaming._execute_tool(tc, ui)
    tool_msg = sess.chat_history[-1]
    assert tool_msg.type == "tool" and tool_msg.tool_call_id == tc["id"]
    return tool_msg.content, ui


@pytest.fixture()
def bound_session(workspace):
    sess = _make_saved_session(workspace)
    yield sess
    session.unbind_thread()


def _latest(sess, workspace=None):
    root = workspace if workspace is not None else fh.session_workspace()
    return fh.latest_undoable(sess.current_session_id, root)


def _record_path(cid):
    return fh._record_path(cid)


# ══════════════════════════════════════════════════════════════
# 基础：三类文件 + 字节级恢复
# ══════════════════════════════════════════════════════════════

class TestWriteAndUndoBasics:
    def test_write_then_undo_restores_bytes(self, bound_session, workspace):
        target = workspace / "app.txt"
        original = "第一行\r\nsecond line\r\n"
        target.write_bytes(original.encode("utf-8"))
        result, _ = _drive("write_file", {"path": "app.txt", "content": "替换后的内容\n"})
        assert "成功写入" in result

        cand = _latest(bound_session)
        assert cand is not None and cand["tool"] == "write_file"
        pc = fh.precheck(cand["checkpoint_id"], session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_RESTORABLE, pc["reason"]

        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok" and done["restored"], done
        assert target.read_bytes() == original.encode("utf-8")   # CRLF 逐字节还原

    def test_edit_keeps_bom_and_crlf_on_undo(self, bound_session, workspace):
        target = workspace / "bom.py"
        original = b"\xef\xbb\xbfvalue = 1\r\n# tail\r\n"
        target.write_bytes(original)
        result, _ = _drive("edit_file", {"path": "bom.py",
                                         "old_string": "value = 1", "new_string": "value = 2"})
        assert "成功编辑" in result
        assert b"value = 2" in target.read_bytes()

        cand = _latest(bound_session)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"
        assert target.read_bytes() == original      # BOM + CRLF 原样保留

    def test_append_undo_restores_original(self, bound_session, workspace):
        target = workspace / "log.txt"
        original = "keep\n"
        target.write_text(original, encoding="utf-8")
        result, _ = _drive("append_file", {"path": "log.txt", "content": "appended\n"})
        assert "成功追加" in result
        assert target.read_text(encoding="utf-8") == "keep\nappended\n"

        cand = _latest(bound_session)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"
        assert target.read_text(encoding="utf-8") == original

    def test_new_file_undo_removes_it(self, bound_session, workspace):
        target = workspace / "created.txt"
        assert not target.exists()
        result, _ = _drive("write_file", {"path": "created.txt", "content": "new\n"})
        assert "成功写入" in result
        assert target.exists()

        cand = _latest(bound_session)
        assert cand["pre"]["existed"] is False
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok" and done["restored"]
        assert not target.exists()          # 确认仍是本次操作的产物后才删除
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDONE

    def test_record_fields_complete(self, bound_session, workspace):
        """§三：每条恢复记录都要明确关联既有身份与现场材料。"""
        bound_session.active_run_id = "run-b11a"          # 生产环境由 begin_run 设置
        bound_session.current_task = {"id": "task-b11a"}
        try:
            (workspace / "f.txt").write_text("x\n", encoding="utf-8")
            _drive("write_file", {"path": "f.txt", "content": "y\n"})
        finally:
            bound_session.active_run_id = None
        cand = _latest(bound_session)
        record, error = fh._load_record(cand["checkpoint_id"])
        assert error == ""
        assert record["session_id"] == bound_session.current_session_id
        assert record["run_id"] == "run-b11a"
        assert record["task_id"] == "task-b11a"
        assert record["checkpoint_id"].startswith("fh-")
        assert record["operation_id"].startswith("op-")     # B03 的具体 operation_id
        assert record["tool"] == "write_file"
        assert os.path.normcase(record["workspace"]) == os.path.normcase(str(workspace))
        assert os.path.normcase(record["path"]) == os.path.normcase(str(workspace / "f.txt"))
        assert record["pre"]["existed"] is True
        assert record["pre"]["sha256"]
        assert record["post"]["sha256"]
        assert record["phase"] == fh.PHASE_UNDOABLE


# ══════════════════════════════════════════════════════════════
# 顺序：同一文件连续修改 → 逐次撤销；跳过中间修改被拒绝
# ══════════════════════════════════════════════════════════════

class TestSequentialEdits:
    def test_two_edits_undo_stepwise_and_skip_refused(self, bound_session, workspace):
        target = workspace / "seq.txt"
        target.write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "seq.txt", "content": "v2\n"})
        _drive("write_file", {"path": "seq.txt", "content": "v3\n"})
        assert target.read_text(encoding="utf-8") == "v3\n"

        records = sorted(
            (r for _cid, r, _e in fh._scan_records() if r),
            key=lambda r: r["created_at"])
        first, second = records[0], records[1]

        # 跳过第二次修改直接恢复更早版本 → 当前内容 ≠ 第一次的写后版本 → 冲突拒绝
        skipped = fh.precheck(first["checkpoint_id"],
                              session_id=bound_session.current_session_id,
                              workspace=fh.session_workspace())
        assert skipped["status"] == fh.ST_CONFLICT, skipped["reason"]

        # 先撤最近一次 → 回到 v2
        done2 = fh.execute_undo(second["checkpoint_id"],
                                session_id=bound_session.current_session_id,
                                workspace=fh.session_workspace())
        assert done2["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v2\n"

        # 再撤第一次 → 回到 v1（版本链按序核对通过）
        done1 = fh.execute_undo(first["checkpoint_id"],
                                session_id=bound_session.current_session_id,
                                workspace=fh.session_workspace())
        assert done1["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v1\n"

    def test_post_fingerprint_is_written_version(self, bound_session, workspace):
        target = workspace / "m.txt"
        target.write_text("a\n", encoding="utf-8")
        _drive("write_file", {"path": "m.txt", "content": "c\n"})
        cand = _latest(bound_session)
        record, _ = fh._load_record(cand["checkpoint_id"])
        # 写后指纹 = 磁盘上的真实字节（含 Windows 文本模式写盘的换行翻译）
        assert record["post"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


# ══════════════════════════════════════════════════════════════
# 冲突：AI 改后用户手动改 / 预检后再改 / 文件被删
# ══════════════════════════════════════════════════════════════

class TestConflicts:
    def test_user_edit_after_ai_write_conflict(self, bound_session, workspace):
        target = workspace / "u.txt"
        target.write_text("ai-base\n", encoding="utf-8")
        _drive("write_file", {"path": "u.txt", "content": "ai-edit\n"})
        target.write_text("user-touch\n", encoding="utf-8")   # 用户手动改

        cand = _latest(bound_session)
        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_CONFLICT
        assert "又被修改" in pc["reason"]

        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] != "ok" and not done["restored"]
        assert target.read_text(encoding="utf-8") == "user-touch\n"   # 用户的修改不被覆盖
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDOABLE                   # 材料保留

    def test_modified_between_precheck_and_execute(self, bound_session, workspace):
        target = workspace / "race.txt"
        target.write_text("before\n", encoding="utf-8")
        _drive("write_file", {"path": "race.txt", "content": "after\n"})

        cand = _latest(bound_session)
        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_RESTORABLE

        target.write_text("user-changed-midway\n", encoding="utf-8")  # 预检之后又改
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == fh.ST_CONFLICT and not done["restored"]
        assert target.read_text(encoding="utf-8") == "user-changed-midway\n"

    def test_file_deleted_after_write_conflict(self, bound_session, workspace):
        target = workspace / "gone.txt"
        target.write_text("data\n", encoding="utf-8")
        _drive("write_file", {"path": "gone.txt", "content": "new-data\n"})
        os.remove(target)

        cand = _latest(bound_session)
        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_CONFLICT
        assert "删除" in pc["reason"]


# ══════════════════════════════════════════════════════════════
# 拒绝 / 取消 / 写入失败 / 备份与写后记录失败
# ══════════════════════════════════════════════════════════════

class TestFailures:
    def test_rejected_write_creates_no_record(self, workspace):
        _make_saved_session(workspace)
        state.ui_ref = _FakeUI(approve=False)
        try:
            result, _ = _drive("write_file", {"path": "r.txt", "content": "x\n"})
        finally:
            state.ui_ref = None
            session.unbind_thread()
        assert "已拒绝" in result
        assert not (workspace / "r.txt").exists()
        assert fh._scan_records() == []

    def test_target_modified_during_confirm_aborts_write(self, workspace):
        _make_saved_session(workspace)
        target = workspace / "c.txt"
        target.write_text("confirmed-view\n", encoding="utf-8")

        ui = _FakeUI(approve=True)

        def _external_edit():       # 确认卡打开期间，用户/外部程序改了文件
            target.write_text("external-edit\n", encoding="utf-8")

        ui.on_confirm = _external_edit
        state.ui_ref = ui               # 写盘确认卡经 state.ui_ref 弹出
        try:
            result, _ = _drive("write_file", {"path": "c.txt", "content": "ai\n"}, ui=ui)
        finally:
            state.ui_ref = None
            session.unbind_thread()
        assert "未写入" in result and "核对失败" in result
        assert target.read_text(encoding="utf-8") == "external-edit\n"   # 外部修改保留
        assert fh._scan_records() == []                                  # 没有假记录

    def test_write_failure_reports_separately(self, bound_session, workspace):
        # 目标是目录：写前读不了（记录 read_error），写盘失败——两个事实分别报告
        (workspace / "adir").mkdir()
        result, _ = _drive("write_file", {"path": "adir", "content": "x\n"})
        assert "写入失败" in result
        assert "不可撤销" in result
        assert _latest(bound_session) is None        # 材料不完整，不可撤销

    def test_backup_failure_reports_not_undoable(self, bound_session, workspace, monkeypatch):
        target = workspace / "nb.txt"
        target.write_text("orig\n", encoding="utf-8")

        real_atomic = fh._atomic_write_bytes

        def _boom(path, data):
            if path.endswith(".bin"):
                raise OSError("disk full (simulated)")
            return real_atomic(path, data)

        monkeypatch.setattr(fh, "_atomic_write_bytes", _boom)
        result, _ = _drive("write_file", {"path": "nb.txt", "content": "new\n"})
        monkeypatch.undo()
        assert "成功写入" in result                       # 写入本身成功
        assert target.read_text(encoding="utf-8") == "new\n"
        assert "不可撤销" in result and "备份" in result   # 备份失败单独报告
        assert _latest(bound_session) is None             # 没有可撤销的假记录

    def test_post_record_failure_stays_not_undoable(self, bound_session, workspace,
                                                    monkeypatch):
        target = workspace / "pf.txt"
        target.write_text("orig\n", encoding="utf-8")

        real_persist = fh._persist
        state_box = {"prepared_saved": False}

        def _persist(record):
            if state_box["prepared_saved"] and record.get("phase") == fh.PHASE_UNDOABLE:
                return False          # 模拟写后记录保存失败
            if record.get("phase") == fh.PHASE_PREPARED:
                state_box["prepared_saved"] = True
            return real_persist(record)

        monkeypatch.setattr(fh, "_persist", _persist)
        result, _ = _drive("write_file", {"path": "pf.txt", "content": "new\n"})
        monkeypatch.undo()
        assert "成功写入" in result and "不可撤销" in result
        assert target.read_text(encoding="utf-8") == "new\n"
        assert _latest(bound_session) is None
        cid, record, _e = [e for e in fh._scan_records() if e[1] is not None][0]
        assert record["phase"] == fh.PHASE_PREPARED       # 磁盘上没有"可撤销"的假记录
        pc = fh.precheck(cid, session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_UNSUPPORTED

    def test_unattributed_write_proceeds_without_record(self, workspace):
        """没有运行归属（未保存的会话、子 Agent、直接调用）→ 不建记录、如实说明。"""
        sess = session.get_active()
        sess.project = str(workspace)
        result, _ = _drive("write_file", {"path": "na.txt", "content": "x\n"})
        assert "成功写入" in result and "不可撤销" in result
        assert (workspace / "na.txt").read_text(encoding="utf-8") == "x\n"
        assert fh._scan_records() == []


# ══════════════════════════════════════════════════════════════
# 材料与边界：缺失 / 损坏 / 越界 / 符号链接 / junction
# ══════════════════════════════════════════════════════════════

class TestMaterialsAndBoundaries:
    def _write_once(self, sess, workspace, name="mat.txt", content="v2\n"):
        target = workspace / name
        target.write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": name, "content": content})
        return _latest(sess)

    def test_missing_blob_unsupported(self, bound_session, workspace):
        cand = self._write_once(bound_session, workspace)
        os.remove(fh._blob_path(cand["checkpoint_id"]))
        pc = fh.precheck(cand["checkpoint_id"], session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_UNSUPPORTED and "缺失" in pc["reason"]
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == fh.ST_UNSUPPORTED and not done["restored"]

    def test_corrupt_blob_unsupported(self, bound_session, workspace):
        cand = self._write_once(bound_session, workspace)
        with open(fh._blob_path(cand["checkpoint_id"]), "wb") as f:
            f.write(b"tampered-bytes")
        pc = fh.precheck(cand["checkpoint_id"], session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_UNSUPPORTED and "指纹" in pc["reason"]

    def test_corrupt_record_isolated_from_chat(self, bound_session, workspace):
        cand = self._write_once(bound_session, workspace)
        with open(_record_path(cand["checkpoint_id"]), "w", encoding="utf-8") as f:
            f.write("{corrupt json!!!")
        assert _latest(bound_session) is None
        pc = fh.precheck(cand["checkpoint_id"], session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_UNSUPPORTED and "损坏" in pc["reason"]
        # 只影响这一条撤销能力，不牵连聊天历史
        assert memory.load_session(bound_session.current_session_id) is not False
        assert len(bound_session.chat_history) >= 2

    def test_cross_session_refused(self, workspace):
        sess_a = _make_saved_session(workspace)
        target = workspace / "x.txt"
        target.write_text("a1\n", encoding="utf-8")
        _drive("write_file", {"path": "x.txt", "content": "a2\n"})
        cand_a = _latest(sess_a)
        session.unbind_thread()

        sess_b = _second_session(workspace)          # 另一个真实会话
        done = fh.execute_undo(cand_a["checkpoint_id"],
                               session_id=sess_b.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == fh.ST_UNSUPPORTED and "其它会话" in done["reason"]
        assert target.read_text(encoding="utf-8") == "a2\n"

    def test_cross_workspace_refused(self, bound_session, workspace, tmp_path):
        cand = self._write_once(bound_session, workspace)
        other = tmp_path / "other-ws"
        other.mkdir()
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=str(other))
        assert done["status"] == fh.ST_UNSUPPORTED and "工作区" in done["reason"]
        assert (workspace / "mat.txt").read_text(encoding="utf-8") == "v2\n"

    def test_symlink_target_detected(self, bound_session, workspace, tmp_path):
        cand = self._write_once(bound_session, workspace, name="ln.txt")
        target = workspace / "ln.txt"
        decoy = tmp_path / "decoy.txt"
        decoy.write_text("decoy\n", encoding="utf-8")
        try:
            os.remove(target)
            os.symlink(str(decoy), str(target))
        except (OSError, NotImplementedError):
            pytest.skip("当前环境无法创建符号链接")
        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_UNSUPPORTED
        assert "符号链接" in pc["reason"]
        assert decoy.read_text(encoding="utf-8") == "decoy\n"

    def test_junction_redirect_detected(self, bound_session, workspace):
        """父目录被 junction 顶替指向别处 → realpath 变化 → 拒绝恢复。"""
        sub_real = workspace / "sub"
        sub_real.mkdir()
        (sub_real / "j.txt").write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "sub/j.txt", "content": "v2\n"})
        cand = _latest(bound_session)

        elsewhere = workspace / "junction-target"
        elsewhere.mkdir()
        (elsewhere / "j.txt").write_text("elsewhere\n", encoding="utf-8")
        sub_real.rename(workspace / "sub-real")
        mk = subprocess.run(["cmd", "/c", "mklink", "/J",
                             str(workspace / "sub"), str(elsewhere)],
                            capture_output=True, text=True, errors="replace")
        if mk.returncode != 0:
            pytest.skip(f"无法创建 junction: {mk.stderr.strip()}")
        try:
            pc = fh.precheck(cand["checkpoint_id"],
                             session_id=bound_session.current_session_id,
                             workspace=fh.session_workspace())
            assert pc["status"] == fh.ST_UNSUPPORTED, pc["reason"]
            done = fh.execute_undo(cand["checkpoint_id"],
                                   session_id=bound_session.current_session_id,
                                   workspace=fh.session_workspace())
            assert done["status"] == fh.ST_UNSUPPORTED
            # junction 目标不被误改
            assert (elsewhere / "j.txt").read_text(encoding="utf-8") == "elsewhere\n"
        finally:
            try:
                os.rmdir(workspace / "sub")     # 只拆 junction，不动目标
            except OSError:
                pass
            shutil.rmtree(workspace / "sub-real", ignore_errors=True)
            shutil.rmtree(elsewhere, ignore_errors=True)


# ══════════════════════════════════════════════════════════════
# 多会话 / linked worktree
# ══════════════════════════════════════════════════════════════

class TestSessionsAndWorktrees:
    def test_two_sessions_same_file_stepwise(self, workspace):
        sess_a = _make_saved_session(workspace)
        target = workspace / "shared.txt"
        target.write_text("base\n", encoding="utf-8")
        _drive("write_file", {"path": "shared.txt", "content": "from-a\n"})
        cand_a = _latest(sess_a)
        session.unbind_thread()

        sess_b = _second_session(workspace)
        _drive("write_file", {"path": "shared.txt", "content": "from-b\n"})
        cand_b = _latest(sess_b)

        # A 的记录：当前内容已是 B 的写后版本 → 冲突，不误撤
        pc_a = fh.precheck(cand_a["checkpoint_id"], session_id=sess_a.current_session_id,
                           workspace=fh.session_workspace())
        assert pc_a["status"] == fh.ST_CONFLICT

        done_b = fh.execute_undo(cand_b["checkpoint_id"],
                                 session_id=sess_b.current_session_id,
                                 workspace=fh.session_workspace())
        assert done_b["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "from-a\n"

        # B 撤完，A 的记录现场一致了 → 可恢复
        pc_a2 = fh.precheck(cand_a["checkpoint_id"], session_id=sess_a.current_session_id,
                            workspace=fh.session_workspace())
        assert pc_a2["status"] == fh.ST_RESTORABLE
        done_a = fh.execute_undo(cand_a["checkpoint_id"],
                                 session_id=sess_a.current_session_id,
                                 workspace=fh.session_workspace())
        assert done_a["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "base\n"

    def test_linked_worktree_isolation(self, workspace, tmp_path):
        if shutil.which("git") is None:
            pytest.skip("git 未安装")
        repo = tmp_path / "repo"
        repo.mkdir()

        def _git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

        _git("init")
        _git("config", "user.email", "t@example.com")
        _git("config", "user.name", "t")
        (repo / "same.txt").write_text("head\n", encoding="utf-8")
        _git("add", "same.txt")
        _git("commit", "-m", "init")

        wt = tmp_path / "wt"
        subprocess.run(["git", "worktree", "add", "-b", "b11a-wt", str(wt)],
                       cwd=repo, check=True, capture_output=True)

        sess_main = _make_saved_session(repo)
        _drive("write_file", {"path": "same.txt", "content": "main-edit\n"})
        cand_main = _latest(sess_main, str(repo))
        session.unbind_thread()

        sess_wt = _second_session(wt)
        _drive("write_file", {"path": "same.txt", "content": "wt-edit\n"})
        cand_wt = _latest(sess_wt, str(wt))
        session.unbind_thread()

        # 各自只看到自己工作区的记录
        assert os.path.normcase(cand_main["workspace"]) == os.path.normcase(str(repo))
        assert os.path.normcase(cand_wt["workspace"]) == os.path.normcase(str(wt))
        assert _latest(sess_main, str(repo))["checkpoint_id"] == cand_main["checkpoint_id"]
        assert _latest(sess_wt, str(wt))["checkpoint_id"] == cand_wt["checkpoint_id"]

        # 撤隔离区的改动不动主仓库
        done = fh.execute_undo(cand_wt["checkpoint_id"],
                               session_id=sess_wt.current_session_id,
                               workspace=str(wt))
        assert done["status"] == "ok"
        assert (wt / "same.txt").read_text(encoding="utf-8") == "head\n"
        assert (repo / "same.txt").read_text(encoding="utf-8") == "main-edit\n"

        # 主仓库会话无法拿隔离区的记录撤销
        cross = fh.precheck(cand_wt["checkpoint_id"],
                            session_id=sess_main.current_session_id,
                            workspace=str(repo))
        assert cross["status"] == fh.ST_UNSUPPORTED


# ══════════════════════════════════════════════════════════════
# Git 边界：暂存区 / 用户 stash / 无关文件不变
# ══════════════════════════════════════════════════════════════

class TestGitBoundaries:
    def test_index_stash_and_unrelated_untouched(self, workspace):
        if shutil.which("git") is None:
            pytest.skip("git 未安装")
        repo = workspace

        def _git(*args, check=True):
            return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", check=check)

        _git("init")
        _git("config", "user.email", "t@example.com")
        _git("config", "user.name", "t")
        (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
        (repo / "unrelated.txt").write_text("user-edit\n", encoding="utf-8")
        (repo / "staged.txt").write_text("staged-v1\n", encoding="utf-8")
        _git("add", "tracked.txt", "staged.txt")
        _git("commit", "-m", "init")
        # 用户自己的操作：暂存一个文件 + 改一个无关文件 + 打一个自己的 stash
        (repo / "staged.txt").write_text("staged-v2\n", encoding="utf-8")
        _git("add", "staged.txt")
        (repo / "unrelated.txt").write_text("user-edit-2\n", encoding="utf-8")
        (repo / "stashme.txt").write_text("stash-v1\n", encoding="utf-8")
        _git("stash", "push", "-u", "-m", "user-own-stash", "stashme.txt")
        stash_before = _git("stash", "list").stdout

        sess = _make_saved_session(repo)
        _drive("write_file", {"path": "tracked.txt", "content": "ai-edit\n"})
        cand = _latest(sess)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=sess.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"
        session.unbind_thread()

        assert (repo / "tracked.txt").read_text(encoding="utf-8") == "v1\n"
        assert (repo / "unrelated.txt").read_text(encoding="utf-8") == "user-edit-2\n"
        staged = _git("diff", "--cached", "--name-only").stdout
        assert "staged.txt" in staged                     # 暂存区不变
        stash_after = _git("stash", "list").stdout
        assert stash_before == stash_after                # 用户 stash 不变
        assert "user-own-stash" in stash_after
        status = _git("status", "--porcelain").stdout
        assert "tracked.txt" not in status                # 恢复后与 HEAD 一致（干净）
        assert "staged.txt" in status                     # 仍是暂存过的状态


# ══════════════════════════════════════════════════════════════
# 验证义务与撤销记录
# ══════════════════════════════════════════════════════════════

class TestVerificationAndRecords:
    def test_undo_invalidates_tests_and_persists_obligation(self, bound_session, workspace):
        from src.verification import mark_tests
        target = workspace / "v.py"
        target.write_text("x = 1\n", encoding="utf-8")
        _drive("write_file", {"path": "v.py", "content": "x = 2\n"})
        mark_tests(bound_session.verification, True, "之前跑过", root=str(workspace))
        assert bound_session.verification["tests_passed"] is True

        cand = _latest(bound_session)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"

        # 撤销按钮与测试共用的会话侧簿记：作废旧结论 → 接入义务 → 保存
        book = fh.apply_undo_to_session(bound_session, done)
        assert book["verification_noted"] is True, book
        assert book["saved"] is True, book
        assert bound_session.verification["tests_passed"] is None
        rel = os.path.relpath(str(target), str(workspace)).replace("\\", "/")
        assert rel in bound_session.verification["dirty_files"]
        # 义务可靠保存到会话 JSON
        assert memory.load_session(bound_session.current_session_id) is not False
        assert rel in (bound_session.pending_verification or {}).get("files", [])
        # 撤销结果单独记录在恢复记录里
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDONE
        assert record["undo"]["at"] and record["undo"]["result"] == "ok"

    def test_bookkeeping_save_failure_reported_separately(self, bound_session, workspace,
                                                          monkeypatch):
        """文件恢复成功但会话保存失败 → 两个事实分别返回，不合并成"撤销成功"。"""
        target = workspace / "bk.txt"
        target.write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "bk.txt", "content": "v2\n"})
        cand = _latest(bound_session)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"

        from src import memory as _memory
        def _boom(*a, **k):
            raise OSError("save failed (simulated)")
        monkeypatch.setattr(_memory, "save_session_report", _boom)
        book = fh.apply_undo_to_session(bound_session, done)
        monkeypatch.undo()
        assert book["verification_noted"] is True      # 义务已在内存接入
        assert book["saved"] is False and "save failed" in book["save_error"]
        assert target.read_text(encoding="utf-8") == "v1\n"

    def test_restore_ok_record_save_fail_reported_separately(self, bound_session, workspace,
                                                             monkeypatch):
        target = workspace / "rs.txt"
        target.write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "rs.txt", "content": "v2\n"})
        cand = _latest(bound_session)

        real_persist = fh._persist

        def _persist(record):
            if record.get("phase") == fh.PHASE_UNDONE:
                return False            # 模拟"撤销后记录保存失败"
            return real_persist(record)

        monkeypatch.setattr(fh, "_persist", _persist)
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        monkeypatch.undo()
        assert done["status"] == "ok" and done["restored"] is True
        assert done["record_saved"] is False              # 分别报告，不掩盖
        assert "保存失败" in done["reason"]
        assert target.read_text(encoding="utf-8") == "v1\n"   # 文件确实恢复了
        # 磁盘上仍是 undoable，但当前内容 ≠ 写后版本 → 下次预检冲突，不会重复恢复
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDOABLE
        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_CONFLICT

    def test_delete_session_removes_records(self, workspace):
        sess = _make_saved_session(workspace)
        (workspace / "d.txt").write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "d.txt", "content": "v2\n"})
        assert _latest(sess) is not None
        sid = sess.current_session_id
        session.unbind_thread()
        memory.delete_session(sid)
        remaining = [r for _c, r, _e in fh._scan_records() if r is not None]
        assert all(r.get("session_id") != sid for r in remaining)

    def test_prune_keeps_latest_undoable_and_inflight(self, bound_session, workspace,
                                                      monkeypatch):
        monkeypatch.setattr(fh, "MAX_RECORDS", 10)
        fake = run_records.Operation(
            operation_id="op-prune", run_id="run-prune",
            session_id=bound_session.current_session_id,
            tool="write_file", tool_call_id="c", base_revision=0, task_id="")
        run_records.set_current_operation(fake)
        try:
            # 一条在途（prepared）记录：最旧，但受保护
            prep_target = workspace / "inflight.txt"
            prep = fh.prepare_write("write_file", str(prep_target), expected_raw=fh.NO_FILE)
            prep_target.write_bytes(fh.encode_text_bytes("partial\n"))

            ids = []
            for i in range(12):
                p = workspace / f"p{i}.txt"
                one = fh.prepare_write("write_file", str(p), expected_raw=fh.NO_FILE)
                assert one.record is not None and not one.abort
                data = fh.encode_text_bytes(f"v{i}\n")
                p.write_bytes(data)                             # 模拟工具写入（与指纹同源）
                ok, _why = fh.complete_write(one.record, expected_bytes=data)
                assert ok
                ids.append(one.record["checkpoint_id"])
        finally:
            run_records.clear_current_operation()

        records = [r for _c, r, _e in fh._scan_records() if r is not None]
        assert len(records) <= fh.MAX_RECORDS
        # 最新一条（p11）受保护：仍是可撤销的，且能完整撤销
        latest = _latest(bound_session)
        assert latest is not None
        assert os.path.normcase(latest["path"]) == os.path.normcase(str(workspace / "p11.txt"))
        # 最旧的已完成记录被淘汰
        assert fh._load_record(ids[0])[0] is None
        assert fh._load_record(ids[1])[0] is None
        # 在途记录未被淘汰（崩溃后仍能如实报告"写入未确认完成"）
        inflight, _err = fh._load_record(prep.record["checkpoint_id"])
        assert inflight is not None and inflight["phase"] == fh.PHASE_PREPARED
        # 最新一条仍可撤销
        done = fh.execute_undo(latest["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok"
        assert not (workspace / "p11.txt").exists()


# ══════════════════════════════════════════════════════════════
# 跨进程：新解释器读盘后预检 + 恢复
# ══════════════════════════════════════════════════════════════

_CHILD_SCRIPT = r'''
import os
import sys

data_dir, project, session_id, repo_root = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])

sys.path.insert(0, repo_root)
from src import paths
paths.set_data_dir(data_dir)          # 子进程显式设置隔离数据目录
from src import file_history as fh

root = os.path.realpath(project)
cand = fh.latest_undoable(session_id, root)
assert cand is not None, "新解释器找不到可撤销记录"
pc = fh.precheck(cand["checkpoint_id"], session_id=session_id, workspace=root)
assert pc["status"] == fh.ST_RESTORABLE, pc["reason"]
result = fh.execute_undo(cand["checkpoint_id"], session_id=session_id, workspace=root)
assert result["status"] == "ok" and result["restored"], result
print("CHILD-OK")
'''


class TestFreshInterpreter:
    def test_new_interpreter_prechecks_and_restores(self, bound_session, workspace, tmp_path):
        target = workspace / "fresh.txt"
        original = "origin\r\n"
        target.write_bytes(original.encode("utf-8"))
        _drive("write_file", {"path": "fresh.txt", "content": "rewritten\n"})
        assert target.read_text(encoding="utf-8") == "rewritten\n"

        import src.paths as _paths
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_SCRIPT,
             _paths.get_data_dir(), str(workspace),
             bound_session.current_session_id, repo_root],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=tmp_path,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert proc.returncode == 0, f"子进程失败:\n{proc.stdout}\n{proc.stderr}"
        assert "CHILD-OK" in proc.stdout
        assert target.read_bytes() == original.encode("utf-8")


# ══════════════════════════════════════════════════════════════
# 并发：写入记录与恢复共用一把锁（一致性烟测）
# ══════════════════════════════════════════════════════════════

class TestConcurrency:
    def test_concurrent_undo_and_prepare_stay_consistent(self, bound_session, workspace):
        """多线程同时撤销不同文件 + 同时建立新记录：互不交错、结果一致。

        注意：paths.set_data_dir 是线程本地的，线程入口必须带主线程取好的隔离根。"""
        import src.paths as _paths
        data_root = _paths.get_data_dir()      # 主线程取好再带进线程
        sid = bound_session.current_session_id
        root = fh.session_workspace()

        cids = []
        for i in range(3):
            name = f"cc{i}.txt"
            (workspace / name).write_text("v1\n", encoding="utf-8")
            _drive("write_file", {"path": name, "content": "v2\n"})
            cids.append(_latest(bound_session)["checkpoint_id"])

        fake = run_records.Operation(
            operation_id="op-cc", run_id="run-cc", session_id=sid,
            tool="write_file", tool_call_id="c", base_revision=0, task_id="")
        outcomes = {}

        def _undo(idx, cid):
            _paths.set_data_dir(data_root)
            try:
                outcomes[f"undo{idx}"] = fh.execute_undo(cid, session_id=sid, workspace=root)
            except Exception as error:      # pragma: no cover
                outcomes[f"undo{idx}"] = error

        def _prepare(idx):
            _paths.set_data_dir(data_root)
            run_records.set_current_operation(fake)
            try:
                p = workspace / f"new{idx}.txt"
                prep = fh.prepare_write("write_file", str(p), expected_raw=fh.NO_FILE)
                data = fh.encode_text_bytes("content\n")
                p.write_bytes(data)
                ok, _why = fh.complete_write(prep.record, expected_bytes=data)
                outcomes[f"prep{idx}"] = bool(prep.record is not None and ok and not prep.abort)
            except Exception as error:      # pragma: no cover
                outcomes[f"prep{idx}"] = error
            finally:
                run_records.clear_current_operation()

        threads = [threading.Thread(target=_undo, args=(i, cids[i])) for i in range(3)]
        threads += [threading.Thread(target=_prepare, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        for i in range(3):
            assert outcomes[f"undo{i}"]["status"] == "ok", outcomes[f"undo{i}"]
            # 原有文件的改动：恢复写前内容（文件重新出现，内容为 v1）
            assert (workspace / f"cc{i}.txt").read_text(encoding="utf-8") == "v1\n"
        for i in range(2):
            assert outcomes[f"prep{i}"] is True, outcomes[f"prep{i}"]
            assert (workspace / f"new{i}.txt").read_text(encoding="utf-8") == "content\n"


# ══════════════════════════════════════════════════════════════
# 复核修复回归（4 处 P1 + 1 处 P2）
# ══════════════════════════════════════════════════════════════

class TestReviewFixes:
    def test_post_fingerprint_not_claimed_from_external_save(self, bound_session, workspace):
        """P1-1：AI 写完后用户保存新内容——写后指纹必须是 AI 写出的那份，
        用户的内容只能让预检冲突，绝不能被撤销覆盖。"""
        import hashlib
        target = workspace / "claim.txt"
        target.write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "claim.txt", "content": "v2\n"})
        target.write_text("user-saved\n", encoding="utf-8")     # 用户随后保存了新内容

        cand = _latest(bound_session)
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["post"]["sha256"] == \
            hashlib.sha256(fh.encode_text_bytes("v2\n")).hexdigest()

        pc = fh.precheck(cand["checkpoint_id"],
                         session_id=bound_session.current_session_id,
                         workspace=fh.session_workspace())
        assert pc["status"] == fh.ST_CONFLICT
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == fh.ST_CONFLICT and not done["changed"]
        assert target.read_text(encoding="utf-8") == "user-saved\n"

    def test_readback_mismatch_rejected_as_post_failed(self, bound_session, workspace,
                                                       monkeypatch):
        """P1-1：写后核对发现磁盘现状与写出的内容不同（注入读回差异）→ post_failed，
        不认领那份内容、不可撤销；记录里也没有"可撤销"的假指纹。"""
        target = workspace / "rb.txt"
        target.write_text("v1\n", encoding="utf-8")

        monkeypatch.setattr(fh, "_read_bytes_now", lambda path: (b"external-wrote-this", ""))
        result, _ = _drive("write_file", {"path": "rb.txt", "content": "v2\n"})
        monkeypatch.undo()

        assert "成功写入" in result and "不可撤销" in result and "核对" in result
        assert target.read_text(encoding="utf-8") == "v2\n"      # 写入本身已完成
        assert _latest(bound_session) is None
        _cid, record, _e = [e for e in fh._scan_records() if e[1] is not None][0]
        assert record["phase"] == fh.PHASE_POST_FAILED
        import hashlib
        assert record["post"]["sha256"] != hashlib.sha256(b"external-wrote-this").hexdigest()

    def test_write_critical_section_serializes_and_rechecks(self, bound_session, workspace,
                                                            monkeypatch):
        """P1-2：A 的写入临界区（备份→写盘→定格）持锁期间，B 写同一文件必须等待；
        B 在锁内重新核对看到 A 的写入后按"确认期间被修改"拒绝——两个改动不互相覆盖。"""
        import src.paths as _paths
        data_root = _paths.get_data_dir()
        target = workspace / "race2.txt"
        target.write_text("orig\n", encoding="utf-8")

        sess_a = bound_session
        sess_b = _second_session(workspace)

        started = threading.Event()
        release = threading.Event()
        real_atomic = fh._atomic_write_bytes

        def _slow_blob(path, data):
            if path.endswith(".bin") and not started.is_set():
                started.set()
                release.wait(10)            # A 持锁停在备份处
            return real_atomic(path, data)

        monkeypatch.setattr(fh, "_atomic_write_bytes", _slow_blob)
        results = {}

        def _run(sid_key, sess, content):
            _paths.set_data_dir(data_root)
            session.bind_thread(sess)
            try:
                results[sid_key] = _drive("write_file",
                                          {"path": "race2.txt", "content": content})
            finally:
                session.unbind_thread()

        thread_a = threading.Thread(target=_run, args=("a", sess_a, "from-a\n"))
        thread_b = threading.Thread(target=_run, args=("b", sess_b, "from-b\n"))
        thread_a.start()
        assert started.wait(5)              # A 已持锁
        thread_b.start()
        time.sleep(0.2)                     # 给 B 时间到达锁门口
        release.set()
        thread_a.join(30)
        thread_b.join(30)
        monkeypatch.undo()

        assert "成功写入" in results["a"][0]
        assert "未写入" in results["b"][0] and "核对失败" in results["b"][0]
        assert target.read_bytes() == fh.encode_text_bytes("from-a\n")
        # B 被拒后没有留下记录；撤销 A 能完整恢复写前内容（B 的写入没有发生）
        remaining = [r for _c, r, _e in fh._scan_records() if r is not None]
        assert all(r["session_id"] == sess_a.current_session_id for r in remaining)
        done = fh.execute_undo(_latest(sess_a)["checkpoint_id"],
                               session_id=sess_a.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok" and done["changed"] is True
        assert target.read_text(encoding="utf-8") == "orig\n"

    def test_chain_gap_refused_even_when_content_matches(self, bound_session, workspace):
        """P2-5：v0→v1→v2→v1 后直接撤销第一次操作——当前内容与写后指纹恰好一致，
        版本链核对仍必须拒绝；逐次撤销不受影响。"""
        target = workspace / "chain.txt"
        target.write_text("v0\n", encoding="utf-8")
        _drive("write_file", {"path": "chain.txt", "content": "v1\n"})
        _drive("write_file", {"path": "chain.txt", "content": "v2\n"})
        _drive("write_file", {"path": "chain.txt", "content": "v1\n"})

        records = sorted((r for _c, r, _e in fh._scan_records() if r is not None),
                         key=lambda r: r["created_at"])
        first, second, third = records
        sid = bound_session.current_session_id
        ws = fh.session_workspace()

        pc = fh.precheck(first["checkpoint_id"], session_id=sid, workspace=ws)
        assert pc["status"] == fh.ST_UNSUPPORTED and "版本链" in pc["reason"]
        done = fh.execute_undo(first["checkpoint_id"], session_id=sid, workspace=ws)
        assert done["status"] == fh.ST_UNSUPPORTED and done["changed"] is False
        assert target.read_text(encoding="utf-8") == "v1\n"

        # 按序逐次撤销：C → B → A。中段 A 仍不可恢复（指纹冲突或版本链不完整都算拒绝）
        assert fh.execute_undo(third["checkpoint_id"], session_id=sid, workspace=ws)["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v2\n"
        pc_mid = fh.precheck(first["checkpoint_id"], session_id=sid, workspace=ws)
        assert pc_mid["status"] != fh.ST_RESTORABLE           # B 还没撤销，A 仍被挡
        assert fh.execute_undo(second["checkpoint_id"], session_id=sid, workspace=ws)["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v1\n"
        pc_last = fh.precheck(first["checkpoint_id"], session_id=sid, workspace=ws)
        assert pc_last["status"] == fh.ST_RESTORABLE
        assert fh.execute_undo(first["checkpoint_id"], session_id=sid, workspace=ws)["status"] == "ok"
        assert target.read_text(encoding="utf-8") == "v0\n"

    def test_changed_but_unverified_still_invalidates_verification(self, bound_session,
                                                                   workspace, monkeypatch):
        """P1-3：恢复写入已完成（os.replace 原子成功）但读回校验失败——结果必须
        如实说 changed=True / verified=False，会话簿记照常作废旧验证结论，
        不能因为校验失败就把整次恢复说成"未执行"。"""
        from src.verification import mark_tests
        target = workspace / "uv.py"
        target.write_text("x = 1\n", encoding="utf-8")
        _drive("write_file", {"path": "uv.py", "content": "x = 2\n"})
        mark_tests(bound_session.verification, True, "之前跑过", root=str(workspace))
        assert bound_session.verification["tests_passed"] is True

        cand = _latest(bound_session)
        monkeypatch.setattr(fh, "_read_bytes_now", lambda path: (None, "injected readback failure"))
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        monkeypatch.undo()

        assert done["status"] == "ok"
        assert done["changed"] is True and done["verified"] is False
        assert target.read_text(encoding="utf-8") == "x = 1\n"    # 文件确实恢复了
        assert "校验" in done["reason"]
        book = fh.apply_undo_to_session(bound_session, done)
        assert book["verification_noted"] is True
        assert bound_session.verification["tests_passed"] is None  # 旧结论已作废
        record, _ = fh._load_record(cand["checkpoint_id"])
        assert record["phase"] == fh.PHASE_UNDONE
        assert record["undo"]["result"] == "unverified"

    def test_corrupt_pre_field_is_not_treated_as_new_file(self, bound_session, workspace):
        """P1-4：pre.existed 缺失的记录必须整体按损坏处理，绝不能把原有文件当成
        "本次新建"而删除。覆盖两种损坏形态：只删 existed 键；以及缺失 existed 但
        其余字段齐全（伪装成正常的"新建文件"记录）。"""
        import json
        target = workspace / "keep.txt"
        target.write_text("precious\n", encoding="utf-8")
        _drive("write_file", {"path": "keep.txt", "content": "v2\n"})
        cand = _latest(bound_session)
        sid = bound_session.current_session_id
        ws = fh.session_workspace()

        path = _record_path(cand["checkpoint_id"])
        original = json.loads(open(path, encoding="utf-8").read())

        for pre_shape in (
                {**original["pre"], "existed": None},              # 只破坏 existed 的类型
                {"sha256": "", "size": 0, "blob": "", "read_error": ""},  # 缺 existed 键
        ):
            raw = dict(original)
            raw["pre"] = pre_shape
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)

            assert _latest(bound_session) is None
            pc = fh.precheck(cand["checkpoint_id"], session_id=sid, workspace=ws)
            assert pc["status"] == fh.ST_UNSUPPORTED, (pre_shape, pc["reason"])
            done = fh.execute_undo(cand["checkpoint_id"], session_id=sid, workspace=ws)
            assert done["status"] == fh.ST_UNSUPPORTED and done["changed"] is False
            assert target.exists() and target.read_text(encoding="utf-8") == "v2\n"

    def test_append_is_true_append_not_whole_file_rewrite(self, bound_session, workspace,
                                                          monkeypatch):
        """P1（第三轮）：追加必须是真追加——备份之后、写入之前其它进程追加的内容
        绝不能被"旧内容＋追加内容"的整文件覆盖；并发变化只禁用此次撤销。"""
        target = workspace / "ap.txt"
        target.write_text("base|", encoding="utf-8")

        real_read_blob = fh._read_blob

        def _external_append_then_blob(record):
            data, err = real_read_blob(record)
            with open(target, "a", encoding="utf-8") as f:
                f.write("EXTERNAL|")            # 在备份之后、真追加之前落到磁盘
            return data, err

        monkeypatch.setattr(fh, "_read_blob", _external_append_then_blob)
        result, _ = _drive("append_file", {"path": "ap.txt", "content": "AI|"})
        monkeypatch.undo()

        assert "成功追加" in result
        # 外部追加的内容必须还在；撤销入口对这次改动不可用
        assert target.read_text(encoding="utf-8") == "base|EXTERNAL|AI|"
        assert "不可撤销" in result
        assert _latest(bound_session) is None
        _cid, record, _e = [e for e in fh._scan_records() if e[1] is not None][0]
        assert record["phase"] == fh.PHASE_POST_FAILED

    def test_append_to_new_file_is_undoable(self, bound_session, workspace):
        """P2（第三轮）：目标原本不存在时，追加的基础内容明确是 b""——
        记录必须可撤销，不得变成 post_failed。"""
        target = workspace / "fresh-append.txt"
        assert not target.exists()
        result, _ = _drive("append_file", {"path": "fresh-append.txt", "content": "AI\n"})
        assert "成功追加" in result and "不可撤销" not in result

        cand = _latest(bound_session)
        assert cand is not None and cand["tool"] == "append_file"
        assert cand["pre"]["existed"] is False
        done = fh.execute_undo(cand["checkpoint_id"],
                               session_id=bound_session.current_session_id,
                               workspace=fh.session_workspace())
        assert done["status"] == "ok" and done["changed"] is True
        assert not target.exists()          # 撤销 = 删除本次新建的文件

    def test_corrupt_field_types_do_not_block_other_records(self, bound_session, workspace):
        """P2（第三轮）：pre.sha256 是数字/列表、created_at 是数字、undo 值类型错误等
        → 只让那一条记录无效；扫描不得被 TypeError 打断，同会话完好记录照常可查询、
        可撤销。"""
        import json
        (workspace / "c1.txt").write_text("v1\n", encoding="utf-8")
        _drive("write_file", {"path": "c1.txt", "content": "v2\n"})
        (workspace / "c2.txt").write_text("w1\n", encoding="utf-8")
        _drive("write_file", {"path": "c2.txt", "content": "w2\n"})

        records = sorted((r for _c, r, _e in fh._scan_records() if r is not None),
                         key=lambda r: r["created_at"])
        first, second = records[0], records[1]
        sid = bound_session.current_session_id
        ws = fh.session_workspace()
        second_path = _record_path(second["checkpoint_id"])
        pristine = open(second_path, encoding="utf-8").read()

        shapes = [
            {"pre": {"existed": True, "sha256": 12345, "size": 3,
                     "blob": "x.bin", "read_error": ""}},          # sha 是数字
            {"pre": {"existed": True, "sha256": ["a"], "size": 3,
                     "blob": "x.bin", "read_error": ""}},          # sha 是列表
            {"created_at": 12345},                                  # 时间戳类型损坏
            {"undo": {"at": 1, "result": "ok", "detail": "",
                      "restored_sha256": ""}},                      # undo 值类型损坏
        ]
        for shape in shapes:
            raw = json.loads(pristine)
            raw.update(shape)
            with open(second_path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)

            # 损坏的那条按无效处理……
            pc = fh.precheck(second["checkpoint_id"], session_id=sid, workspace=ws)
            assert pc["status"] == fh.ST_UNSUPPORTED, shape
            # ……且不牵连另一条完好记录的查询
            assert _latest(bound_session) is not None
            assert _latest(bound_session)["checkpoint_id"] == first["checkpoint_id"]

        # 原样还原后一切照旧，完好记录可撤销
        with open(second_path, "w", encoding="utf-8") as f:
            f.write(pristine)
        done = fh.execute_undo(first["checkpoint_id"], session_id=sid, workspace=ws)
        assert done["status"] == "ok" and done["changed"] is True
        assert (workspace / "c1.txt").read_text(encoding="utf-8") == "v1\n"
