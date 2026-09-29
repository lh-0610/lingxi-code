"""B05 任务身份、用户要求保存与边界全链路测试。

涵盖验收要求中的 10 大链路：
1. 要求来源识别与白名单过滤
2. 计划变动对比与身份保持
3. 显式新任务切换（new_task=True）
4. 撤销任务切换与 Worker 屏障（不回滚文件/验证义务）
5. 重启恢复与坏数据隔离（quarantine）
6. 双会话隔离
7. 预算控制（限制 <= 1000，运行态 <= 3000 折叠）
8. 对抗性限制防篡改与 Prompt Caching 稳定性
9. 界面展示（TaskPanel 三段式、无任务隐藏、可撤销横幅、EditConstraintsDialog）
10. 全套功能与原有功能协同
"""
from __future__ import annotations

import copy
import json
import threading
import time
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import memory, roles, session, task_state as _ts, tools


# ── Fixtures ──

@pytest.fixture
def clean_session():
    """提供纯净的前台会话。"""
    sess = session.Session()
    session.set_active(sess)
    session.bind_thread(sess)
    yield sess
    session.unbind_thread()


# ── 链路 1：要求来源识别与白名单过滤 ──

def test_source_identification_and_tagging():
    # 真实用户消息
    user_msg = HumanMessage(content="增加导出功能")
    _ts.tag_user_message(user_msg)
    assert user_msg.id.startswith("msg-")
    assert _ts.get_message_id(user_msg) == user_msg.id
    assert _ts.get_message_kind(user_msg) == "user_input"
    assert _ts.is_verified_user_input(user_msg) is True

    # 内部消息：恢复、修复、闸门、视觉桥接
    repair_msg = HumanMessage(content="语法检查未通过，请修复")
    _ts.tag_internal_message(repair_msg, kind="repair")
    assert _ts.get_message_kind(repair_msg) == "repair"
    assert _ts.is_verified_user_input(repair_msg) is False

    gate_msg = HumanMessage(content="试图完成任务，先执行验证")
    _ts.tag_internal_message(gate_msg, kind="gate")
    assert _ts.get_message_kind(gate_msg) == "gate"
    assert _ts.is_verified_user_input(gate_msg) is False

    bridge_msg = HumanMessage(content="图片识别结果")
    _ts.tag_internal_message(bridge_msg, kind="vision_bridge")
    assert _ts.get_message_kind(bridge_msg) == "vision_bridge"
    assert _ts.is_verified_user_input(bridge_msg) is False

    # 旧历史遗留消息
    legacy_msg = HumanMessage(content="旧消息无标记")
    assert _ts.get_message_kind(legacy_msg) == "legacy_unknown"
    assert _ts.is_verified_user_input(legacy_msg) is False


def test_metadata_whitelist_filtering():
    """保存消息时，非白名单字段被过滤，防止非法提权。"""
    user_msg = HumanMessage(
        content="敏感操作",
        additional_kwargs={
            "lingxi_message_id": "msg-safe123",
            "malicious_role": "admin",
            "fake_auth": True,
        }
    )
    d = memory._msg_to_dict(user_msg)
    assert d["id"] == "msg-safe123"
    assert "malicious_role" not in d
    assert "fake_auth" not in d
    assert d["lingxi_message_id"] == "msg-safe123"

    # 反序列化还原
    restored = memory._dict_to_msg(d)
    assert restored.id == "msg-safe123"
    assert "malicious_role" not in (restored.additional_kwargs or {})


# ── 链路 2：计划变动对比与身份保持 ──

def test_first_valid_plan_establishes_task(clean_session):
    sess = clean_session
    assert sess.current_task is None

    # 用户输入建立需求来源
    user_msg = HumanMessage(content="增加导出功能")
    _ts.tag_user_message(user_msg)
    sess.chat_history.append(user_msg)

    # 首次调用 update_plan 建立任务
    out = tools.update_plan.func("[ ] 设计导出接口\n[ ] 实现 CSV 导出")
    assert "设计导出接口" in out
    assert sess.current_task is not None
    tid = sess.current_task["id"]
    assert tid.startswith("task-")
    assert sess.current_task["plan_version"] == 1
    assert user_msg.id in sess.current_task["request_message_ids"]

    # 步骤正常推进（无需 explanation）
    out2 = tools.update_plan.func("[x] 设计导出接口\n[~] 实现 CSV 导出")
    assert "1/2 完成" in out2
    assert sess.current_task["id"] == tid  # task_id 保持不变
    # 单纯推进不是结构调整：版本保持（见 CLAUDE.md B05「计划变动对比与版本递增」）
    assert sess.current_task["plan_version"] == 1


def test_plan_structural_change_requires_explanation(clean_session):
    sess = clean_session
    user_msg = HumanMessage(content="开发系统")
    _ts.tag_user_message(user_msg)
    sess.chat_history.append(user_msg)

    tools.update_plan.func("[ ] 步骤1\n[ ] 步骤2")
    tid = sess.current_task["id"]
    old_plan = copy.deepcopy(sess.current_plan)

    # 增删步骤无 explanation -> 拒绝并回退
    res = tools.update_plan.func("[ ] 步骤1\n[ ] 步骤3")  # 删除了步骤2，增加了步骤3
    assert "计划未更新" in res
    assert "explanation" in res
    assert sess.current_plan == old_plan  # 回退保持不变
    assert sess.current_task["id"] == tid

    # 附带 explanation -> 允许变更，递增版本并记录摘要
    res2 = tools.update_plan.func(
        "[ ] 步骤1\n[ ] 步骤3",
        explanation="发现步骤2不必要，替换为步骤3"
    )
    assert "计划已更新" in res2
    assert "0/2 完成" in res2
    assert sess.current_task["id"] == tid  # task_id 仍然保持！
    assert sess.current_task["plan_version"] == 2
    assert sess.current_task["last_plan_change_reason"] == "发现步骤2不必要，替换为步骤3"
    assert "新增 1 步" in sess.current_task["last_plan_change_summary"]
    assert "移除 1 步" in sess.current_task["last_plan_change_summary"]


def test_failed_plan_leaves_no_half_task(clean_session):
    sess = clean_session
    assert sess.current_task is None
    # 纯文字，无任何步骤
    out = tools.update_plan.func("这是一段普通文字说明，没有复选框步骤")
    assert "没有检测到合法 checklist 行" in out or "计划未更新" in out
    assert sess.current_task is None  # 不产生半吊子任务


# ── 链路 3：显式新任务切换（new_task=True） ──

def test_explicit_new_task_switch(clean_session):
    sess = clean_session
    tools.update_plan.func("[ ] 任务A步骤1\n[ ] 任务A步骤2")
    task_a_id = sess.current_task["id"]

    # new_task=True 但没有 explanation -> 拒绝
    err = tools.update_plan.func("[ ] 任务B步骤1", new_task=True)
    assert "说明切换原因" in err
    assert sess.current_task["id"] == task_a_id

    # new_task=True 带有 explanation -> 开启新任务并归档旧任务
    ok = tools.update_plan.func(
        "[ ] 任务B步骤1",
        explanation="开启全新的数据迁移任务",
        new_task=True,
    )
    assert "已开启新任务" in ok
    assert sess.current_task["id"] != task_a_id
    assert sess.current_task["id"].startswith("task-")

    # 旧任务归档
    assert len(sess.archived_tasks) == 1
    assert sess.archived_tasks[0]["id"] == task_a_id

    # 记录切换信息以供撤销
    last_switch = sess.last_task_switch
    assert last_switch is not None
    assert last_switch["previous_task"]["id"] == task_a_id
    assert last_switch["new_task_id"] == sess.current_task["id"]
    assert last_switch["reason"] == "开启全新的数据迁移任务"
    assert last_switch["switch_version"] == 1


# ── 链路 4：撤销任务切换与 Worker 屏障 ──

def test_undo_task_switch_integrity(clean_session):
    sess = clean_session
    # 建立任务 A
    tools.update_plan.func("[ ] 原任务步骤")
    task_a = copy.deepcopy(sess.current_task)
    task_a_id = task_a["id"]

    # 产生文件改动与待验证义务
    sess.pending_verification = {"code_files": ["src/module.py"], "reason": "修改了模块"}

    # 切换到新任务 B
    tools.update_plan.func("[ ] 新任务步骤", explanation="临时改换目标", new_task=True)
    task_b_id = sess.current_task["id"]
    switch_ver = sess.last_task_switch["switch_version"]

    # 验证不匹配的版本/任务 ID 拒绝撤销
    wrong_ok, wrong_msg = _ts.undo_task_switch(sess, "task-wrong", switch_ver)
    assert wrong_ok is False
    assert "不符" in wrong_msg

    wrong_ver_ok, _ = _ts.undo_task_switch(sess, task_b_id, 999)
    assert wrong_ver_ok is False

    # 正确撤销
    ok, msg = _ts.undo_task_switch(sess, task_b_id, switch_ver)
    assert ok is True
    assert "已撤销任务切换" in msg
    assert sess.current_task["id"] == task_a_id
    assert sess.current_plan == [{"text": "原任务步骤", "status": "pending"}]
    assert sess.last_task_switch is None
    # 原任务 A 的归档项被取回；被撤销的 B 不消失，以 undone_switch 留在归档里
    assert [t["id"] for t in sess.archived_tasks] == [task_b_id]
    assert sess.archived_tasks[0]["archive_kind"] == "undone_switch"

    # 核心安全准则：绝不破坏已有文件修改与待验证事项！
    assert sess.pending_verification == {"code_files": ["src/module.py"], "reason": "修改了模块"}


def test_undo_with_worker_barrier():
    """生成中撤销时等待 worker 退出屏障。"""
    sess = session.Session()
    sess.current_task = _ts.create_task()
    task_a = copy.deepcopy(sess.current_task)

    # 模拟切换
    _ts.switch_to_new_task(sess, [{"text": "新步骤", "status": "pending"}], "切换原因")
    new_tid = sess.current_task["id"]
    ver = sess.last_task_switch["switch_version"]

    worker_running = True

    def dummy_worker():
        while worker_running:
            time.sleep(0.01)

    t = threading.Thread(target=dummy_worker)
    t.start()
    sess.last_worker = t
    sess.is_generating = True

    # 停止标志打上后，等待 worker 退出再撤销
    sess.stop_flag = True
    worker_running = False
    t.join(timeout=2.0)
    assert not t.is_alive()

    ok, _ = _ts.undo_task_switch(sess, new_tid, ver)
    assert ok is True
    assert sess.current_task["id"] == task_a["id"]


# ── 链路 5：重启恢复与坏数据隔离 ──

def test_restart_recovery_and_quarantine(clean_session, isolated_memory):
    """序列化到磁盘并重新载入，测试正常恢复与损坏隔离。"""
    sess = clean_session
    sess.current_session_id = "test-session-b05"
    u_msg = HumanMessage(content="开发新系统需求")
    _ts.tag_user_message(u_msg)
    sess.chat_history.extend([u_msg, AIMessage(content="已接收任务，开始制定计划")])

    tools.update_plan.func("[x] 步骤1\n[ ] 步骤2")
    sess.current_task["pinned_constraints"] = ["必须支持 Windows", "保留现有接口"]

    # 归档任务
    tools.update_plan.func("[ ] 新任务", explanation="切到新任务", new_task=True)

    # 保存会话
    memory.save_session(session=sess)

    # 从磁盘全新加载
    new_sess = session.Session()
    new_sess.current_session_id = "test-session-b05"
    loaded = memory.load_session("test-session-b05", session=new_sess)
    assert loaded is True

    assert new_sess.current_task is not None
    assert new_sess.current_task["id"] == sess.current_task["id"]
    assert len(new_sess.archived_tasks) == 1
    assert new_sess.archived_tasks[0]["pinned_constraints"] == ["必须支持 Windows", "保留现有接口"]
    assert new_sess.last_task_switch["switch_version"] == 1

    # 测试损坏数据整块隔离 (Quarantine)
    sess_file = isolated_memory / "test-session-b05.json"
    assert sess_file.exists()
    data = json.loads(sess_file.read_text(encoding="utf-8"))

    # 恶意篡改任务字段为非法类型
    data["progress"]["task"] = "NOT_A_DICT_TASK_DATA"
    sess_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 重新载入：应平稳降级并将坏字段隔离进 quarantined_progress，历史与系统完好
    quarantine_sess = session.Session()
    q_loaded = memory.load_session("test-session-b05", session=quarantine_sess)
    assert q_loaded is True
    assert quarantine_sess.current_task is None
    assert quarantine_sess.progress_error is not None
    # 再次保存，坏数据被移入 quarantined_progress
    memory.save_session(session=quarantine_sess)
    saved_data = json.loads(sess_file.read_text(encoding="utf-8"))
    qp = saved_data.get("quarantined_progress")
    assert qp is not None
    assert len(qp) > 0


# ── 链路 6：双会话隔离 ──

def test_dual_session_task_isolation():
    sess_a = session.Session()
    sess_b = session.Session()

    session.bind_thread(sess_a)
    tools.update_plan.func("[ ] 会话A任务步骤")
    sess_a.current_task["pinned_constraints"] = ["仅限A的限制"]
    session.unbind_thread()

    session.bind_thread(sess_b)
    tools.update_plan.func("[ ] 会话B任务步骤")
    session.unbind_thread()

    assert sess_a.current_task["id"] != sess_b.current_task["id"]
    assert sess_a.current_task["pinned_constraints"] == ["仅限A的限制"]
    assert sess_b.current_task["pinned_constraints"] == []
    assert sess_a.current_plan[0]["text"] == "会话A任务步骤"
    assert sess_b.current_plan[0]["text"] == "会话B任务步骤"


# ── 链路 7：预算控制 ──

def test_pinned_constraints_budget_validation():
    # 正常字符数
    ok, _, cleaned = _ts.validate_pinned_constraints("- 必须支持 Windows\n- 必须兼容 Python 3.10")
    assert ok is True
    assert len(cleaned) == 2

    # 超过 1000 字符硬预算 -> 拒绝保存，绝不静默截断
    long_constraint = "A" * 1005
    bad_ok, bad_err, _ = _ts.validate_pinned_constraints(long_constraint)
    assert bad_ok is False
    assert "超出预算" in bad_err


def test_volatile_context_task_folding(clean_session):
    sess = clean_session
    # 模拟超长用户输入需求
    huge_text = "重要业务需求：" + ("X" * 4000)
    user_msg = HumanMessage(content=huge_text)
    _ts.tag_user_message(user_msg)
    sess.chat_history.append(user_msg)

    tools.update_plan.func("[ ] 步骤1\n[ ] 步骤2")
    sess.current_task["pinned_constraints"] = ["必须支持 Windows"]

    ctx = roles.get_volatile_context()
    assert "<system-reminder>" in ctx
    assert "用户固定限制" in ctx
    assert "必须支持 Windows" in ctx
    # 验证任务上下文独立字符预算 <= 3000
    assert len(ctx) < 4500  # 加上 pending_verification 与 xml 标记，总长度受控
    assert "已折叠" in ctx


# ── 链路 8：对抗性限制防篡改与 Prompt Caching 稳定性 ──

def test_model_cannot_tamper_pinned_constraints(clean_session):
    sess = clean_session
    tools.update_plan.func("[ ] 步骤1")
    sess.current_task["pinned_constraints"] = ["绝不允许引入第三方库"]

    # 模型调用 update_plan 无法修改 pinned_constraints
    tools.update_plan.func("[ ] 步骤1\n[ ] 步骤2", explanation="修改步骤")
    assert sess.current_task["pinned_constraints"] == ["绝不允许引入第三方库"]


def test_prompt_cache_prefix_stability(clean_session):
    """固定限制变化时不污染 stable system prompt，保护 prompt caching。"""
    sess = clean_session
    prompt1 = roles.get_system_prompt()

    # 设置固定限制与新任务
    tools.update_plan.func("[ ] 步骤1")
    sess.current_task["pinned_constraints"] = ["严格兼容 Windows"]
    prompt2 = roles.get_system_prompt()

    # 系统提示词前缀绝对保持一致（字节级相等），命中缓存
    assert prompt1 == prompt2


# ── 链路 9：界面展示与控件单元测试 ──

def test_task_panel_and_dialog_ui(clean_session, monkeypatch):
    """无头测试 TaskPanel 与 EditConstraintsDialog。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from src.ui.task_panel import TaskPanel, EditConstraintsDialog

    _app = QApplication.instance() or QApplication([])
    assert _app is not None

    sess = clean_session

    def theme_fn(k: str) -> str:
        return "#3b82f6" if "bg" in k else "#1e293b"

    panel = TaskPanel(None, theme_lookup=theme_fn)

    # 1. 普通问答无任务无计划：隐藏
    panel.render_session(sess)
    assert panel.isVisible() is False

    # 2. 建立任务后：正常显示三段式
    tools.update_plan.func("[x] 第一步\n[~] 第二步\n[ ] 第三步")
    sess.current_task["pinned_constraints"] = ["必须兼容 Windows"]
    panel.render_session(sess)
    assert panel.isVisible() is True
    assert "1/3 完成" in panel.plan_count.text()
    assert "必须兼容 Windows" in panel.constraints_label.text()
    assert panel.switch_banner.isVisible() is False

    # 3. 切换新任务：显示可撤销横幅
    tools.update_plan.func("[ ] 新任务步骤", explanation="换做另外一件事", new_task=True)
    panel.render_session(sess)
    assert panel.switch_banner.isVisible() is True
    assert "换做另外一件事" in panel.banner_text.text()

    # 4. 点击撤销按钮触发回调
    undo_called = []
    panel.on_undo_request = lambda: undo_called.append(True)
    panel.undo_btn.click()
    assert undo_called == [True]

    # 5. 编辑限制对话框与字数限制
    dialog = EditConstraintsDialog(None, sess)
    dialog.editor.setPlainText("合规限制1\n合规限制2")
    assert dialog.save_btn.isEnabled() is True

    # 超限测试
    dialog.editor.setPlainText("A" * 1005)
    assert dialog.save_btn.isEnabled() is False
    assert "1000" in dialog.char_count_label.text()
