import pytest

from src import state, session
from src.tools import update_plan, set_step_status


# ── 创建 / 解析 ──

def test_parse_and_render():
    state.current_plan = []
    out = update_plan.func("[x] 第一步\n[~] 第二步\n[ ] 第三步")
    assert len(state.current_plan) == 3
    assert state.current_plan[0]["status"] == "done"
    assert state.current_plan[1]["status"] == "in_progress"
    assert state.current_plan[2]["status"] == "pending"
    assert "1/3 完成" in out


def test_empty_without_reason_keeps_unfinished_plan():
    update_plan.func("[ ] 临时")
    out = update_plan.func("")
    assert state.current_plan == [{"text": "临时", "status": "pending"}]
    assert "未更新" in out


def test_explicit_cancel_clears():
    update_plan.func("[ ] 临时")
    out = update_plan.func("", explanation="用户取消此任务")
    assert state.current_plan == []
    assert "清空" in out
    assert "用户取消此任务" in out


def test_tolerant_formats():
    """容错：markdown 列表前缀 / 大写 / checkbox 空格变体 / 完成字符变体。"""
    state.current_plan = []
    update_plan.func(
        "- [ ] 列表前缀\n"      # markdown "- " 前缀
        "* [X] 大写完成\n"       # "* " 前缀 + 大写 X
        "1. [~] 数字前缀\n"      # "1. " 前缀
        "[ x ] 内部空格\n"       # checkbox 内多空格
        "[✓] 对勾完成\n"         # ✓ 当完成
        "没有方框的一行"          # 无 checkbox → 忽略，避免摘要/分析污染
    )
    p = state.current_plan
    assert len(p) == 5
    assert p[0] == {"text": "列表前缀", "status": "pending"}
    assert p[1] == {"text": "大写完成", "status": "done"}
    assert p[2] == {"text": "数字前缀", "status": "in_progress"}
    assert p[3] == {"text": "内部空格", "status": "done"}
    assert p[4] == {"text": "对勾完成", "status": "done"}
    state.current_plan = []


def test_history_summary_is_not_added_to_plan():
    """压缩摘要意外续接到参数时，只保留摘要前的 checklist。"""
    out = update_plan.func(
        "[x] 第一步\n"
        "[~] 第二步\n"
        "[ ] 第三步 [历史摘要]:\n"
        "**用户目标**\n"
        "1. 这不是计划项"
    )
    assert len(state.current_plan) == 3
    assert state.current_plan[-1]["text"] == "第三步"
    assert "1/3 完成" in out


def test_invalid_text_does_not_overwrite_existing_plan():
    update_plan.func("[~] 保留中的任务")
    out = update_plan.func('{"plan": "普通分析文本"}')
    assert state.current_plan == [{"text": "保留中的任务", "status": "in_progress"}]
    assert "未更新" in out


def test_update_plan_allows_status_only_update():
    update_plan.func("[~] A\n[ ] B")
    out = update_plan.func("[x] A\n[~] B")
    assert "计划已更新" in out
    assert state.current_plan == [
        {"text": "A", "status": "done"},
        {"text": "B", "status": "in_progress"},
    ]


# ── 结构保护：不猜测相似度，不将改写误当进度更新 ──

def test_explicit_new_task_replaces():
    update_plan.func("[ ] A\n[ ] B")
    out = update_plan.func("[x] C", explanation="用户改为新任务 C")
    assert "计划已更新" in out
    assert state.current_plan == [{"text": "C", "status": "done"}]


def test_removal_of_unfinished_requires_reason():
    update_plan.func("[x] A\n[~] B\n[ ] C")
    out = update_plan.func("[x] A\n[~] B")
    assert "计划未更新" in out
    assert state.current_plan == [
        {"text": "A", "status": "done"},
        {"text": "B", "status": "in_progress"},
        {"text": "C", "status": "pending"},
    ]


def test_reword_does_not_replace_or_guess_status():
    """措辞变化时整次拒绝；不能猜测新旧步骤相似就把任务标完成。"""
    update_plan.func("[~] 实现登录功能\n[ ] 写测试")
    out = update_plan.func("[x] 实现登录\n[~] 写测试")   # 第一步措辞被改短
    assert "计划未更新" in out
    assert state.current_plan == [
        {"text": "实现登录功能", "status": "in_progress"},
        {"text": "写测试", "status": "pending"},
    ]


def test_new_dependency_can_be_added_with_reason():
    update_plan.func("[x] A\n[~] B")
    out = update_plan.func("[x] A\n[x] B\n[ ] C", explanation="发现还需要迁移 C")
    assert "计划已更新" in out
    assert [it["text"] for it in state.current_plan] == ["A", "B", "C"]
    assert state.current_plan[0]["status"] == "done"
    assert state.current_plan[1]["status"] == "done"
    assert state.current_plan[2] == {"text": "C", "status": "pending"}


@pytest.mark.parametrize("explanation", ["", " \n\t"])
def test_structural_change_requires_nonempty_reason(explanation):
    update_plan.func("[~] 调研实现\n[ ] 修改代码")
    out = update_plan.func("[~] 修改代码\n[ ] 跑测试", explanation=explanation)
    assert "计划未更新" in out
    assert [it["text"] for it in state.current_plan] == ["调研实现", "修改代码"]


def test_reorder_requires_reason():
    update_plan.func("[~] A\n[ ] B")
    out = update_plan.func("[ ] B\n[~] A")
    assert "计划未更新" in out
    assert [it["text"] for it in state.current_plan] == ["A", "B"]
    out = update_plan.func("[ ] B\n[~] A", explanation="发现 B 是 A 的前置条件")
    assert "计划已更新" in out
    assert [it["text"] for it in state.current_plan] == ["B", "A"]


def test_stale_status_snapshot_cannot_reset_progress():
    update_plan.func("[x] A\n[~] B\n[ ] C")
    out = update_plan.func("[ ] A\n[ ] B\n[ ] C")
    assert "计划未更新" in out
    assert [it["status"] for it in state.current_plan] == ["done", "in_progress", "pending"]


def test_explicit_rework_can_reset_progress():
    update_plan.func("[x] A\n[~] B")
    out = update_plan.func("[~] A\n[ ] B", explanation="集成测试发现 A 需要返工，B 暂停")
    assert "计划已更新" in out
    assert [it["status"] for it in state.current_plan] == ["in_progress", "pending"]


def test_repeated_or_rejected_update_does_not_redraw(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(state, "ui_ref", SimpleNamespace(show_plan=calls.append))
    update_plan.func("[~] A\n[ ] B")
    update_plan.func("[~] A\n[ ] B")
    update_plan.func("[~] A 换个说法\n[ ] B")
    assert len(calls) == 1


def test_session_plans_are_guarded_independently():
    first = session.get_active()
    update_plan.func("[~] A")
    second = session.Session()
    session.bind_thread(second)
    try:
        update_plan.func("[~] B")
        set_step_status.func(1, "完成")
    finally:
        session.unbind_thread()
    assert first.current_plan == [{"text": "A", "status": "in_progress"}]
    assert second.current_plan == [{"text": "B", "status": "done"}]


# ── 新增：TestSetStepStatus ──

class TestSetStepStatus:
    """set_step_status 增量更新测试。"""

    def _setup_three_steps(self):
        """建 3 步计划 [ ] A, [ ] B, [ ] C。"""
        state.current_plan = []
        update_plan.func("[ ] A\n[ ] B\n[ ] C")
        assert len(state.current_plan) == 3

    def test_set_in_progress(self):
        self._setup_three_steps()
        out = set_step_status.func(2, "进行中")
        assert state.current_plan[1]["status"] == "in_progress"
        assert state.current_plan[0]["status"] == "pending"   # 不变
        assert state.current_plan[2]["status"] == "pending"   # 不变
        assert "0/3" in out                                    # done 计数 = 0

    def test_set_done(self):
        self._setup_three_steps()
        set_step_status.func(2, "进行中")
        out = set_step_status.func(1, "完成")
        assert state.current_plan[0]["status"] == "done"
        assert "1/3" in out

    def test_step_out_of_range_low(self):
        self._setup_three_steps()
        plan_before = [dict(it) for it in state.current_plan]
        out = set_step_status.func(0, "完成")
        assert "超出范围" in out
        assert state.current_plan == plan_before

    def test_step_out_of_range_high(self):
        self._setup_three_steps()
        plan_before = [dict(it) for it in state.current_plan]
        out = set_step_status.func(4, "完成")
        assert "超出范围" in out
        assert state.current_plan == plan_before

    def test_invalid_status(self):
        self._setup_three_steps()
        plan_before = [dict(it) for it in state.current_plan]
        out = set_step_status.func(1, "飞了")
        assert "状态无效" in out
        assert state.current_plan == plan_before

    def test_no_plan(self):
        state.current_plan = []
        out = set_step_status.func(1, "完成")
        assert "还没有计划" in out

    def test_status_aliases(self):
        """中英 / checkbox 字符都认。"""
        self._setup_three_steps()
        set_step_status.func(1, "done")
        assert state.current_plan[0]["status"] == "done"
        set_step_status.func(2, "x")
        assert state.current_plan[1]["status"] == "done"
        set_step_status.func(3, "完成")
        assert state.current_plan[2]["status"] == "done"

    def test_full_lifecycle(self):
        """完整生命周期：建计划 → 推进 → 完成。"""
        state.current_plan = []
        update_plan.func("[ ] 读代码\n[ ] 改代码\n[ ] 跑测试")
        set_step_status.func(1, "进行中")
        assert state.current_plan[0]["status"] == "in_progress"
        set_step_status.func(1, "完成")
        set_step_status.func(2, "进行中")
        assert state.current_plan[1]["status"] == "in_progress"
        set_step_status.func(2, "完成")
        set_step_status.func(3, "进行中")
        set_step_status.func(3, "完成")
        assert all(it["status"] == "done" for it in state.current_plan)

    def test_done_auto_advances_next_to_in_progress(self):
        """标完一步 done、且没有别的进行中步骤时，自动把下一个待办提为 in_progress——
        保证计划面板执行期间始终高亮"当前这一步"（修"模型只标 done、面板永远看不到进行中"）。"""
        self._setup_three_steps()                 # [ ]A [ ]B [ ]C
        set_step_status.func(1, "完成")            # A done → 自动把 B 提为进行中
        assert state.current_plan[0]["status"] == "done"
        assert state.current_plan[1]["status"] == "in_progress"
        assert state.current_plan[2]["status"] == "pending"
        set_step_status.func(2, "完成")            # B done → 自动把 C 提为进行中
        assert state.current_plan[1]["status"] == "done"
        assert state.current_plan[2]["status"] == "in_progress"
        set_step_status.func(3, "完成")            # 全部 done → 没有待办可提，如实 3/3
        assert all(it["status"] == "done" for it in state.current_plan)

    def test_done_does_not_override_explicit_in_progress(self):
        """模型已显式把某步设为进行中时，标另一步 done 不抢它的高亮（不重复提升）。"""
        self._setup_three_steps()
        set_step_status.func(3, "进行中")          # 显式把 C 设为进行中
        set_step_status.func(1, "完成")            # A done —— 已有进行中(C)，不该再提 B
        assert state.current_plan[0]["status"] == "done"
        assert state.current_plan[1]["status"] == "pending"      # B 不被自动提升
        assert state.current_plan[2]["status"] == "in_progress"  # C 保持

    def test_rework_requires_reason(self):
        self._setup_three_steps()
        set_step_status.func(1, "完成")
        out = set_step_status.func(1, "进行中")
        assert "进度未更新" in out
        assert state.current_plan[0]["status"] == "done"
        out = set_step_status.func(1, "进行中", explanation="测试发现该步骤还有遗漏")
        assert "遗漏" in out
        assert state.current_plan[0]["status"] == "in_progress"

    def test_update_keeps_previous_snapshot_immutable(self):
        self._setup_three_steps()
        previous = state.current_plan
        set_step_status.func(1, "完成")
        assert [it["status"] for it in previous] == ["pending", "pending", "pending"]
        assert state.current_plan[1]["status"] == "in_progress"
