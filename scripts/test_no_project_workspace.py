"""无项目时的工作目录：专门的空工作区，不是进程 cwd（源码运行时那是灵犀仓库本身）。

回归背景（实测）：无项目对话里让模型做个小网页，index.html / app.js 写进了灵犀仓库根，
它验证时还跑了 pytest，收集到的是灵犀自己的测试。
"""

import os

import pytest

from src import config, roles, session as _session, state
from src import tools_common


@pytest.fixture()
def no_project(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                  # 进程 cwd 是别处：工具不该落在这里
    s = _session.Session()
    s.project = None
    _session.bind_thread(s)
    old = state.current_project
    state.current_project = None
    try:
        yield s
    finally:
        state.current_project = old
        _session.bind_thread(None)


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


class TestNoProjectDir:
    def test_tools_run_in_the_workspace_not_the_process_cwd(self, no_project, tmp_path):
        cwd = tools_common._project_cwd()
        assert not _same(cwd, tmp_path), "回退到进程 cwd 就是这个 bug"
        assert _same(cwd, tools_common.no_project_dir())
        assert os.path.isdir(cwd), "不存在就建"

    def test_tests_land_under_the_isolated_data_root(self, no_project):
        from src.paths import get_data_dir
        assert _same(tools_common.no_project_dir(), os.path.join(get_data_dir(), "workspace"))

    def test_config_overrides_the_location(self, no_project, monkeypatch, tmp_path):
        target = tmp_path / "我的工作区"
        monkeypatch.setattr(config, "DEFAULT_WORKSPACE", str(target))
        assert _same(tools_common._project_cwd(), target)
        assert target.is_dir()

    def test_relative_paths_resolve_into_the_workspace(self, no_project):
        resolved = tools_common._resolve_path("index.html")
        assert _same(os.path.dirname(resolved), tools_common.no_project_dir())

    def test_an_open_project_still_wins(self, no_project, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        no_project.project = str(proj)
        assert _same(tools_common._project_cwd(), proj)


class TestPromptNamesTheWorkspace:
    def test_no_project_prompt_tells_the_model_where_tools_run(self, no_project):
        prompt = roles.get_system_prompt()
        assert "# 工作目录" in prompt
        assert tools_common.no_project_dir() in prompt

    def test_project_prompt_has_no_workspace_note(self, no_project, tmp_path):
        no_project.project = str(tmp_path)
        state.current_project = str(tmp_path)
        assert "无项目工作区" not in roles.get_system_prompt()

    def test_prompt_is_stable_between_calls(self, no_project):
        """进 system 前缀的内容必须稳定，不然每轮都打断 prompt 缓存。"""
        assert roles.get_system_prompt() == roles.get_system_prompt()
