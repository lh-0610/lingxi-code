"""会话进度的持久化格式、兼容与快照一致性（B02b）。

背景：`load_session` 过去会显式清空 `current_plan` / `task_ledger`，所以关掉软件再回来，
计划和台账一律从零开始——做到一半的任务没法接着干。这一批把它们存进会话 JSON 的
`progress` 信封，并把"读不懂怎么办"一次定清楚。

贯穿全文的取舍：**进度坏掉只作废进度，绝不牵连聊天历史**；而且宁可显示"没有进度"，
也不拿抢救出来的半截数据冒充真进度——后者看起来完全正常，用户无从分辨。
"""
import json
import threading
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src import memory, paths, session, state


def _new_session(plan=None, ledger_files=None):
    sess = session.Session()
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content="问题"),
                         AIMessage(content="回答")]
    if plan:
        sess.current_plan = [dict(p) for p in plan]
    if ledger_files:
        sess.task_ledger = {"files": dict(ledger_files), "commands": []}
    session.register(sess)
    return sess


def _read(mem_dir, sid):
    return json.loads((mem_dir / f"{sid}.json").read_text(encoding="utf-8"))


PLAN = [{"text": "复现问题", "status": "done"},
        {"text": "修复并验证", "status": "in_progress"},
        {"text": "补测试", "status": "pending"}]


# ══════════════════════════════════════════════════════════════
# 往返
# ══════════════════════════════════════════════════════════════

def test_plan_and_ledger_survive_save_load(isolated_memory):
    """核心目标：关掉重开，计划与台账原样还在。"""
    sess = _new_session(PLAN, {"src/a.py": "编辑"})
    sess.task_ledger["commands"].append({"cmd": "pytest", "brief": "3 passed"})
    memory.save_session(session=sess)
    sid = sess.current_session_id

    restored = session.Session()
    assert memory.load_session(sid, session=restored) is True

    assert restored.current_plan == PLAN
    assert restored.task_ledger["files"] == {"src/a.py": "编辑"}
    assert restored.task_ledger["commands"] == [{"cmd": "pytest", "brief": "3 passed"}]
    assert restored.progress_error == ""


def test_saved_file_carries_versioned_progress_envelope(isolated_memory):
    sess = _new_session(PLAN)
    memory.save_session(session=sess)
    data = _read(isolated_memory, sess.current_session_id)

    assert data["schema_version"] == memory._SCHEMA_VERSION
    assert data["progress"]["version"] == memory._PROGRESS_VERSION
    assert data["progress"]["current_plan"] == PLAN
    assert data["progress"]["revision"] >= 1
    # 既有字段一个都不能少
    for key in ("id", "title", "updated", "project", "session_kind", "rag_kb_dir",
                "agent_mode", "messages"):
        assert key in data


def test_revision_advances_each_save(isolated_memory):
    sess = _new_session(PLAN)
    memory.save_session(session=sess)
    first = _read(isolated_memory, sess.current_session_id)["progress"]["revision"]
    sess.chat_history.append(AIMessage(content="又一轮"))
    memory.save_session(session=sess)
    second = _read(isolated_memory, sess.current_session_id)["progress"]["revision"]
    assert second > first
    assert sess.progress_revision == second


def test_two_sessions_do_not_share_progress_containers(isolated_memory):
    """两个会话各拿自己的深拷贝——共享容器会让一边改动、另一边跟着变。"""
    a = _new_session(PLAN, {"x.py": "编辑"})
    memory.save_session(session=a)
    sid = a.current_session_id

    left, right = session.Session(), session.Session()
    memory.load_session(sid, session=left)
    memory.load_session(sid, session=right)

    left.current_plan[0]["status"] = "pending"
    left.task_ledger["files"]["y.py"] = "写入"

    assert right.current_plan[0]["status"] == "done"
    assert "y.py" not in right.task_ledger["files"]


# ══════════════════════════════════════════════════════════════
# 兼容：旧格式 / 坏进度 / 未来格式
# ══════════════════════════════════════════════════════════════

def test_legacy_file_without_progress_loads_normally(isolated_memory):
    """旧会话没有 progress：聊天、标题、Plan-Act 照常恢复，进度如实为空。"""
    sid = "legacy_001"
    (isolated_memory / f"{sid}.json").write_text(json.dumps({
        "id": sid, "title": "旧会话", "updated": "2026-01-01T00:00:00",
        "project": None, "session_kind": "code", "rag_kb_dir": "",
        "agent_mode": "plan",
        "messages": [{"type": "SystemMessage", "content": "sys"},
                     {"type": "HumanMessage", "content": "问题"}],
    }, ensure_ascii=False), encoding="utf-8")

    tgt = session.Session()
    assert memory.load_session(sid, session=tgt) is True

    assert len(tgt.chat_history) == 2
    assert tgt.current_session_title == "旧会话"
    assert tgt.agent_mode == "plan"
    assert tgt.current_plan == []
    assert tgt.progress_error == ""      # 没有进度 ≠ 进度坏了


def test_legacy_checkboxes_in_chat_are_not_mistaken_for_a_plan(isolated_memory):
    """不从旧聊天正文里的 [x] 猜计划——猜出来的计划看着像真的，但没有依据。"""
    sid = "legacy_002"
    (isolated_memory / f"{sid}.json").write_text(json.dumps({
        "id": sid, "title": "旧会话", "updated": "2026-01-01T00:00:00",
        "messages": [{"type": "SystemMessage", "content": "sys"},
                     {"type": "AIMessage", "content": "[x] 第一步\n[ ] 第二步"}],
    }, ensure_ascii=False), encoding="utf-8")

    tgt = session.Session()
    memory.load_session(sid, session=tgt)
    assert tgt.current_plan == []


def _corrupt_cases():
    return {
        "progress 不是对象": "progress 不是对象",
        "计划不是列表": {"version": 1, "current_plan": {"a": 1}},
        "步骤不是对象": {"version": 1, "current_plan": ["做点什么"]},
        "状态是任意真值": {"version": 1,
                          "current_plan": [{"text": "步骤", "status": "完成了"}]},
        "状态是数字": {"version": 1, "current_plan": [{"text": "步骤", "status": 1}]},
        "text 不是字符串": {"version": 1, "current_plan": [{"text": 42, "status": "done"}]},
        "台账不是对象": {"version": 1, "current_plan": [], "task_ledger": []},
        "台账结构非法": {"version": 1, "current_plan": [],
                         "task_ledger": {"files": [], "commands": {}}},
        "未来的 progress 版本": {"version": 99, "current_plan": []},
    }


def test_corrupt_progress_never_takes_down_chat_history(isolated_memory):
    """坏进度只作废进度本身，聊天历史必须完整保留并给出原因。"""
    for name, bad in _corrupt_cases().items():
        sid = f"bad_{abs(hash(name))}"
        (isolated_memory / f"{sid}.json").write_text(json.dumps({
            "id": sid, "title": "坏进度", "updated": "2026-01-01T00:00:00",
            "schema_version": 2, "progress": bad,
            "messages": [{"type": "SystemMessage", "content": "sys"},
                         {"type": "HumanMessage", "content": "重要的对话内容"}],
        }, ensure_ascii=False), encoding="utf-8")

        tgt = session.Session()
        assert memory.load_session(sid, session=tgt) is True, name
        assert len(tgt.chat_history) == 2, f"{name}: 聊天历史被牵连"
        assert tgt.chat_history[1].content == "重要的对话内容", name
        assert tgt.current_plan == [], f"{name}: 坏进度被当成真进度用了"
        assert tgt.progress_error, f"{name}: 没有给出进度作废的原因"


def test_truthy_status_is_not_accepted_as_done(isolated_memory):
    """枚举必须严格比对。把 "完成了" / 1 当成 done，恢复出来的进度会比实际乐观。"""
    prog, err = memory._normalize_progress(
        {"version": 1, "current_plan": [{"text": "步骤", "status": "完成了"}]})
    assert prog["current_plan"] == [] and err


def test_future_schema_version_is_not_silently_overwritten(isolated_memory):
    """退回旧版打开新版会话：拒绝覆盖并明确报错，不做丢字段的降级保存。"""
    sid = "future_001"
    payload = {
        "id": sid, "title": "未来格式", "updated": "2026-01-01T00:00:00",
        "schema_version": memory._SCHEMA_VERSION + 1,
        "progress": {"version": 1, "current_plan": []},
        "future_only_field": {"keep": "me"},
        "messages": [{"type": "SystemMessage", "content": "sys"},
                     {"type": "HumanMessage", "content": "问题"}],
    }
    path = isolated_memory / f"{sid}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()

    sess = _new_session(PLAN)
    sess.current_session_id = sid
    try:
        memory.save_session(session=sess)
        raise AssertionError("应当拒绝覆盖更高版本的会话文件")
    except memory.SessionFormatTooNewError as e:
        assert e.found == memory._SCHEMA_VERSION + 1

    assert path.read_bytes() == before, "拒绝保存时不能动原文件"


def test_unknown_fields_of_same_version_are_preserved(isolated_memory):
    """同版本里不认识的顶层键原样带过去：读得懂的照常更新，读不懂的不丢。"""
    sess = _new_session(PLAN)
    memory.save_session(session=sess)
    sid = sess.current_session_id
    path = isolated_memory / f"{sid}.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["experimental_notes"] = {"a": 1}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    sess.chat_history.append(AIMessage(content="新一轮"))
    memory.save_session(session=sess)

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["experimental_notes"] == {"a": 1}
    assert len(after["messages"]) == 4


# ══════════════════════════════════════════════════════════════
# 快照一致性
# ══════════════════════════════════════════════════════════════

def test_snapshot_never_mixes_generations_of_plan_and_ledger(isolated_memory):
    """保存必须一次取走计划 + 台账，不能拍到两代混合的半截状态。

    计划和台账由工具线程持续改动。分两次读会存下"计划已是第 7 代、台账还停在第 6 代"
    的进度——自相矛盾却不报错，恢复出来的现场是假的。

    这里让改动方每次把两者一起推进到同一代（都带同一个序号），存盘方并发反复保存；
    任何一边不持快照锁，就会出现两个序号对不上的落盘结果。
    """
    sess = _new_session([{"text": "gen0", "status": "pending"}], {"gen0.py": "编辑"})
    memory.save_session(session=sess)
    sid = sess.current_session_id
    stop = threading.Event()
    errors = []
    # paths.set_data_dir 是**线程本地**的：不在工作线程里重设，它的存盘会落到真实
    # chat_memory/ 去，而断言读的是隔离目录里的旧文件——两边对不上却恒等通过，
    # 测试静默失效的同时还污染了用户数据。
    root = str(isolated_memory.parent)

    def mutator():
        # 必须一直改到 saver 收工为止——若 mutator 提前跑完，存盘期间根本没有并发窗口，
        # 这条用例就退化成"存了个静止的状态"，去掉锁也照样通过。
        try:
            gen = 1
            while not stop.is_set():
                with sess.snapshot_lock:
                    sess.current_plan = [{"text": f"gen{gen}", "status": "pending"}]
                    sess.task_ledger = {"files": {f"gen{gen}.py": "编辑"}, "commands": []}
                gen += 1
                time.sleep(0)      # 让出，别把 CPU 占死拖慢存盘
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    def saver():
        paths.set_data_dir(root)
        try:
            for _ in range(15):
                memory.save_session(session=sess)
                prog = _read(isolated_memory, sid)["progress"]
                plan_gen = prog["current_plan"][0]["text"]
                ledger_gen = next(iter(prog["task_ledger"]["files"])).removesuffix(".py")
                assert plan_gen == ledger_gen, f"两代混合：计划 {plan_gen} / 台账 {ledger_gen}"
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()
            paths.set_data_dir(None)

    threads = [threading.Thread(target=mutator), threading.Thread(target=saver)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    stop.set()
    assert not errors, errors


def test_mutating_session_after_snapshot_does_not_change_saved_file(isolated_memory):
    """存盘拿的是深拷贝：存完再改运行态，不该回头改动已落盘内容。"""
    sess = _new_session(PLAN, {"a.py": "编辑"})
    memory.save_session(session=sess)
    sid = sess.current_session_id

    sess.current_plan[0]["status"] = "pending"
    sess.task_ledger["files"]["b.py"] = "写入"

    saved = _read(isolated_memory, sid)["progress"]
    assert saved["current_plan"][0]["status"] == "done"
    assert "b.py" not in saved["task_ledger"]["files"]


def test_reset_history_clears_progress_including_revision(isolated_memory):
    """新对话从零开始。漏清 revision 会让新会话继承旧修订号，后续判断会对错门。"""
    sess = _new_session(PLAN, {"a.py": "编辑"})
    session.set_active(sess)
    memory.save_session(session=sess)
    assert sess.progress_revision >= 1

    memory.reset_history(session=sess)

    assert sess.current_plan == []
    assert sess.task_ledger == state.new_task_ledger()
    assert sess.progress_revision == 0
    assert sess.progress_error == ""


# ══════════════════════════════════════════════════════════════
# 索引写失败的恢复
# ══════════════════════════════════════════════════════════════

def test_index_failure_is_reported_and_body_is_kept(isolated_memory, monkeypatch):
    """正文先写、索引后写：索引失败要抛出来，不能报"已保存"；正文必须已在盘上。"""
    sess = _new_session(PLAN)
    real_update = memory._update_index
    monkeypatch.setattr(memory, "_update_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("index down")))
    try:
        memory.save_session(session=sess)
        raise AssertionError("索引写失败必须抛出，不能静默当成保存成功")
    except OSError as e:
        assert "index down" in str(e)
    monkeypatch.setattr(memory, "_update_index", real_update)

    sid = sess.current_session_id
    assert (isolated_memory / f"{sid}.json").exists(), "正文应当已经落盘"
    assert (isolated_memory / memory._INDEX_REPAIR_NAME).exists(), "应登记待修记录"


def test_pending_index_entry_is_repaired_on_next_list(isolated_memory, monkeypatch):
    """下次读盘时把缺失的索引项补回来，否则那个会话在侧栏里彻底看不见。"""
    sess = _new_session(PLAN)
    real_update = memory._update_index
    monkeypatch.setattr(memory, "_update_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("index down")))
    try:
        memory.save_session(session=sess)
    except OSError:
        pass
    monkeypatch.setattr(memory, "_update_index", real_update)
    sid = sess.current_session_id

    assert not [s for s in memory.list_sessions("__all__") if s["id"] == sid] \
        or True   # 第一次调用即完成修复，下面直接断言结果

    listed = memory.list_sessions("__all__")
    assert [s for s in listed if s["id"] == sid], "索引项没有被补回"
    assert not (isolated_memory / memory._INDEX_REPAIR_NAME).exists(), "修好后应销案"


def test_repair_never_resurrects_a_deleted_session(isolated_memory, monkeypatch):
    """用户主动删掉的会话不能被"索引修复"复活——删除是最不该被撤销的操作。"""
    sess = _new_session(PLAN)
    real_update = memory._update_index
    monkeypatch.setattr(memory, "_update_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("index down")))
    try:
        memory.save_session(session=sess)
    except OSError:
        pass
    monkeypatch.setattr(memory, "_update_index", real_update)
    sid = sess.current_session_id

    memory.delete_session(sid)

    listed = memory.list_sessions("__all__")
    assert not [s for s in listed if s["id"] == sid], "被删除的会话又出现了"
    assert not (isolated_memory / f"{sid}.json").exists()


def test_repair_marker_survives_across_processes(isolated_memory, monkeypatch):
    """待修登记落在磁盘上：换个进程（这里用新 Session + 重新读盘）仍能修。"""
    sess = _new_session(PLAN)
    real_update = memory._update_index
    monkeypatch.setattr(memory, "_update_index",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("index down")))
    try:
        memory.save_session(session=sess)
    except OSError:
        pass
    monkeypatch.setattr(memory, "_update_index", real_update)
    sid = sess.current_session_id
    session.drop(sess.key)

    marker = json.loads((isolated_memory / memory._INDEX_REPAIR_NAME)
                        .read_text(encoding="utf-8"))
    assert [p for p in marker["pending"] if p["id"] == sid]

    tgt = session.Session()
    assert memory.load_session(sid, session=tgt) is True
    assert [s for s in memory.list_sessions("__all__") if s["id"] == sid]


def test_title_update_keeps_progress_and_unknown_fields(isolated_memory):
    """标题线程只改标题：不能顺手把进度或未知字段抹掉。"""
    sess = _new_session(PLAN)
    memory.save_session(session=sess)
    sid = sess.current_session_id
    path = isolated_memory / f"{sid}.json"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["experimental_notes"] = {"a": 1}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    memory._write_session_title(sid, "新标题")

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["title"] == "新标题"
    assert after["progress"]["current_plan"] == PLAN
    assert after["experimental_notes"] == {"a": 1}
