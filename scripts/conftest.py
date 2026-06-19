"""共用 fixtures，供 scripts/ 下各测试脚本使用。

使用方法：在测试脚本里加
    from conftest import project_dir, isolated_memory, ...
或直接 pytest scripts/test_xxx.py（pytest 会自动发现同目录 conftest）
"""
import os
import sys
import pytest

# 确保能 import src
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _fresh_active_session():
    """每个测试前给一个干净的 active session。

    会话级化后 active 是进程级单例，会话级字段（project/model_index/agent_mode/白名单等）
    会跨测试残留——比如上个测试设了 active.project，下个直接 monkeypatch state.current_project
    的测试就读不到（current_project() 优先会话级、非 _UNSET 不回退全局）。每测试 fresh active
    让这些字段回默认（project=_UNSET → 回退全局），测试间互不干扰。

    还要清线程绑定：bind_thread 是线程级的，pytest 全程同一主线程——若某测试（如直接调
    subagent.spawn，它会把调用线程绑到父会话）跑完没解绑，后续测试 current_session() 会串到
    残留会话。每测试前后 unbind 一次，保证 current_session() 干净回退 active。
    """
    from src import session as _session
    from src import state as _state
    from src import paths as _paths
    # 中和全局 current_project：import src.agent 时会 state.current_project = projects.get_current()
    # 把【开发者 app 里当前打开的项目】恢复进来（如手动切到别的项目测试）。不清的话，不自己设
    # current_project 的测试（如 test_related_files 依赖项目根=本仓库）会被串到那个项目而失败。
    _state.current_project = None
    # 清掉上个测试可能残留的"按用户数据根"线程本地上下文（多用户重构引入；线程本地跨测试不自动清）。
    _paths.set_data_dir(None)
    _session.unbind_thread()
    _session.set_active(_session.Session())
    yield
    _session.unbind_thread()
    _paths.set_data_dir(None)


@pytest.fixture()
def project_dir(tmp_path):
    """创建临时项目目录，并注入 state.current_project / state.ui_ref。

    用法：def test_xxx(project_dir): ...
    测试结束后自动恢复原 state。
    """
    from src import state
    from src import session as _session

    proj = tmp_path / "myproject"
    proj.mkdir()
    old_project = state.current_project
    old_ui = state.ui_ref
    state.current_project = str(proj)
    state.ui_ref = None  # 无 UI，写文件自动放行
    # _project_cwd 现在优先用【当前会话】锚定的 project（会话级），所以也把当前会话的
    # project 设成这个临时项目，否则会沿用上一个测试残留的 active.project。
    _sess = _session.get_active()
    old_sess_proj = _sess.project
    _sess.project = str(proj)

    yield proj

    state.current_project = old_project
    state.ui_ref = old_ui
    _sess.project = old_sess_proj


@pytest.fixture()
def isolated_memory(tmp_path):
    """把数据根重定向到 tmp_path，不污染真实数据。

    多用户重构后,路径走 paths.set_data_dir(<根>) 的线程本地上下文,
    chat_memory / index / projects / long_term_memory / role_config 全部跟着落到
    tmp_path/chat_memory 下——一处设置,无需再逐模块 patch 常量。

    用法：def test_xxx(isolated_memory): ...
    """
    import src.paths as _paths
    _paths.set_data_dir(str(tmp_path))
    mem_dir = tmp_path / "chat_memory"
    mem_dir.mkdir(exist_ok=True)
    yield mem_dir
    _paths.set_data_dir(None)


@pytest.fixture()
def sample_py_file(project_dir):
    """在 project_dir 下创建一个示例 Python 文件，返回 Path。"""
    content = (
        "import os\n"
        "import sys\n"
        "\n"
        "def hello():\n"
        '    print("hello world")\n'
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Greeter:\n"
        "    def greet(self, name):\n"
        '        return f"Hello, {name}!"\n'
    )
    fpath = project_dir / "sample.py"
    fpath.write_text(content, encoding="utf-8")
    return fpath


@pytest.fixture()
def clean_state():
    """临时重置全局 state，测试结束后恢复。"""
    from src import state

    old = {
        "current_model_index": state.current_model_index,
        "reasoning_enabled": state.reasoning_enabled,
        "current_session_id": state.current_session_id,
        "current_session_title": state.current_session_title,
        "agent_mode": state.agent_mode,
        "chat_history": state.chat_history[:],
        "session_token_usage": dict(state.session_token_usage),
    }

    state.current_session_id = None
    state.current_session_title = None
    state.chat_history = []
    state.session_token_usage = {"input": 0, "output": 0, "total": 0}
    state.agent_mode = "act"

    yield state

    for k, v in old.items():
        setattr(state, k, v)
