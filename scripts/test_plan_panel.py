"""update_plan → ui.show_plan 接线测试：工具调用应把解析后的 items 推给 UI。"""
from src import tools, state


class _FakeUI:
    def __init__(self):
        self.calls = []

    def show_plan(self, items):
        self.calls.append(items)


def test_update_plan_pushes_to_ui(monkeypatch):
    ui = _FakeUI()
    monkeypatch.setattr(state, "ui_ref", ui)
    out = tools.update_plan.func("[x] 读 config\n[~] 改 state\n[ ] 加 UI")
    assert ui.calls, "update_plan 应调用 ui.show_plan"
    items = ui.calls[-1]
    assert [it["status"] for it in items] == ["done", "in_progress", "pending"]
    assert "3" in out          # 返回串含步骤总数
