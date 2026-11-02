"""开工前的代码地图（src/repo_map.py）：挑得准、对不上时不给、接进系统提示词和主循环。"""
import pytest

from src import repo_map


def _project(tmp_path, n_filler=45):
    """一个够大的项目：一堆无关文件 + 任务面板 / 聊天窗口两个相关文件。"""
    root = tmp_path / "proj"
    (root / "src" / "ui").mkdir(parents=True)
    for i in range(n_filler):
        (root / "src" / f"util_{i}.py").write_text(
            f"# 杂项工具 {i}\nimport os\n\ndef helper_{i}(x):\n    return x + {i}\n", encoding="utf-8")
    (root / "src" / "ui" / "task_panel.py").write_text(
        "# 任务面板：右侧显示计划步骤\nclass TaskPanel:\n    def render_plan(self, items):\n        pass\n\n"
        "    def set_collapsed(self, collapsed):\n        pass\n", encoding="utf-8")
    (root / "src" / "ui" / "chat_window.py").write_text(
        "from src.ui.task_panel import TaskPanel\n\nclass ChatUI:\n    def _on_plan_update(self, items):\n"
        "        # 新计划到达时刷新任务面板\n        self.plan_panel.render_plan(items)\n", encoding="utf-8")
    return root


TASK = "任务面板有新计划时自动弹出：_on_plan_update 收到新计划就打开 task panel"


def test_picks_the_files_the_task_is_about(tmp_path):
    root = _project(tmp_path)
    ranked, _symbols, n = repo_map.rank_files(str(root), TASK)
    assert n == 47
    assert {r for r, _ in ranked[:2]} == {"src/ui/task_panel.py", "src/ui/chat_window.py"}

    text = repo_map.build_task_map(str(root), TASK)
    assert text.startswith("# 代码地图")
    assert "src/ui/chat_window.py" in text and "def _on_plan_update" in text
    assert "util_3.py" not in text                        # 无关文件不列


def test_no_map_for_small_talk_or_small_projects(tmp_path):
    root = _project(tmp_path)
    assert repo_map.build_task_map(str(root), "你好，今天天气怎么样") == ""
    small = _project(tmp_path / "small", n_filler=5)
    assert repo_map.build_task_map(str(small), TASK) == ""     # 小项目模型自己翻得过来


def test_terms_split_identifiers_and_chinese(tmp_path):
    words, runs = repo_map.task_terms("修 _on_plan_update 和 TaskPanel 的任务面板")
    assert {"_on_plan_update", "plan", "update", "taskpanel", "task", "panel"} <= words
    assert any("任务面板" in grams for grams in runs)


def test_test_files_rank_below_the_source_they_quote(tmp_path):
    """测试文件常原样抄着任务里的话，不能把要改的源码挤下去。"""
    root = _project(tmp_path)
    (root / "src" / "test_quotes_task.py").write_text(f"# {TASK}\n# {TASK}\n", encoding="utf-8")
    ranked, _, _ = repo_map.rank_files(str(root), TASK)
    names = [r for r, _ in ranked]
    assert names.index("src/ui/chat_window.py") < names.index("src/test_quotes_task.py")


def test_system_prompt_carries_the_task_map(monkeypatch):
    from src import roles, session
    sess = session.current_session()
    monkeypatch.setattr(sess, "task_map", "# 代码地图（测试）\nsrc/x.py")
    assert roles.get_system_prompt().rstrip().endswith("# 代码地图（测试）\nsrc/x.py")
    monkeypatch.setattr(sess, "task_map", "")
    assert "# 代码地图" not in roles.get_system_prompt()


@pytest.fixture
def loop_session(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage
    from src import agent, session  # noqa: F401  agent 第一次导入会重置当前会话的 chat_history，得在绑定前导入
    sess = session.Session()
    sess.project = str(_project(tmp_path))
    sess.chat_history = [SystemMessage(content="sys"), HumanMessage(content=TASK)]
    session.bind_thread(sess)
    yield sess
    session.unbind_thread()


class _UI:
    def __init__(self):
        self.messages = []

    def show_message(self, text, tag="ai_msg"):
        self.messages.append(text)


def test_main_loop_prepares_the_map_before_the_first_round(loop_session):
    from src import agent
    ui = _UI()
    agent._prepare_task_map(ui)
    assert "src/ui/task_panel.py" in loop_session.task_map
    assert any("相关文件给模型参考" in m for m in ui.messages)


def test_no_map_for_explore_children_or_when_switched_off(loop_session, monkeypatch):
    from src import agent, config
    loop_session.system_prompt_override = "explore prompt"
    agent._prepare_task_map(_UI())
    assert loop_session.task_map == ""

    loop_session.system_prompt_override = None
    monkeypatch.setattr(config, "AUTO_TASK_MAP", False)
    agent._prepare_task_map(_UI())
    assert loop_session.task_map == ""


def test_a_new_message_replaces_the_old_map(loop_session):
    from langchain_core.messages import HumanMessage
    from src import agent
    agent._prepare_task_map(_UI())
    assert loop_session.task_map
    loop_session.chat_history.append(HumanMessage(content="你好"))
    agent._prepare_task_map(_UI())
    assert loop_session.task_map == ""                     # 不沿用上一个任务的地图
