from src import state
from src.tools import update_plan, set_step_status


# ── 保留不变的旧测试（整份替换后行为一致） ──

def test_parse_and_render():
    state.current_plan = []
    out = update_plan.func("[x] 第一步\n[~] 第二步\n[ ] 第三步")
    assert len(state.current_plan) == 3
    assert state.current_plan[0]["status"] == "done"
    assert state.current_plan[1]["status"] == "in_progress"
    assert state.current_plan[2]["status"] == "pending"
    assert "1/3 完成" in out


def test_empty_clears():
    update_plan.func("[ ] 临时")
    out = update_plan.func("")
    assert state.current_plan == []
    assert "清空" in out


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


# ── 改写的旧测试（旧断言的是"模糊合并/拒绝"，现在是整份替换） ──

def test_full_overwrite_replaces():
    """整份替换：新计划完全覆盖旧计划，允许删步骤。"""
    update_plan.func("[ ] A\n[ ] B")
    out = update_plan.func("[x] C")
    assert "计划已更新" in out
    assert state.current_plan == [{"text": "C", "status": "done"}]


def test_full_overwrite_allows_removal_of_unfinished():
    """整份替换：允许删未完成步骤，不需要调整说明。"""
    update_plan.func("[x] A\n[~] B\n[ ] C")
    out = update_plan.func("[x] A\n[~] B")
    assert "计划已更新" in out
    assert state.current_plan == [
        {"text": "A", "status": "done"},
        {"text": "B", "status": "in_progress"},
    ]


def test_reword_replaces_text():
    """整份替换：改写措辞后 current_plan 用模型发的新文字（不再冻结旧文字）。"""
    update_plan.func("[~] 实现登录功能\n[ ] 写测试")
    out = update_plan.func("[x] 实现登录\n[~] 写测试")   # 第一步措辞被改短
    assert "计划已更新" in out
    assert state.current_plan == [
        {"text": "实现登录", "status": "done"},         # 用新文字，不再冻结旧文字
        {"text": "写测试", "status": "in_progress"},
    ]


def test_append_new_step_replaces():
    """整份替换：新增一步时完全按新列表覆盖。"""
    update_plan.func("[x] A\n[~] B")
    out = update_plan.func("[x] A\n[x] B\n[ ] C")
    assert "计划已更新" in out
    assert [it["text"] for it in state.current_plan] == ["A", "B", "C"]
    assert state.current_plan[0]["status"] == "done"
    assert state.current_plan[1]["status"] == "done"
    assert state.current_plan[2] == {"text": "C", "status": "pending"}


def test_structural_change_no_reason_needed():
    """整份替换：直接替换结构，不需要"调整说明"。"""
    update_plan.func("[~] 调研实现\n[ ] 修改代码")
    out = update_plan.func("[~] 修改代码\n[ ] 跑测试")
    assert "计划已更新" in out
    assert [it["text"] for it in state.current_plan] == ["修改代码", "跑测试"]


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
