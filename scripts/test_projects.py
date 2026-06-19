"""项目管理持久化测试（src/projects.py）：增删 / 当前项目切换 / 显示名。

用 isolated_memory 把 PROJECTS_FILE 重定向到 tmp，tmp_path 造真实目录（add_project 校验 isdir）。
"""
import src.projects as projects


class TestProjects:
    def test_add_and_list(self, isolated_memory, tmp_path):
        d = tmp_path / "myproj"
        d.mkdir()
        assert projects.add_project(str(d)) is True
        lst = projects.list_projects()
        assert len(lst) == 1 and lst[0]["name"] == "myproj"

    def test_add_duplicate_skipped(self, isolated_memory, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        assert projects.add_project(str(d)) is True
        assert projects.add_project(str(d)) is False     # 同路径已存在
        assert len(projects.list_projects()) == 1

    def test_add_invalid_path(self, isolated_memory):
        assert projects.add_project(str("/nope/xyz/not_a_dir")) is False

    def test_set_get_current(self, isolated_memory, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        projects.add_project(str(d))
        path = projects.list_projects()[0]["path"]
        projects.set_current(path)
        assert projects.get_current() == path
        projects.set_current(None)
        assert projects.get_current() is None

    def test_remove_resets_current(self, isolated_memory, tmp_path):
        d = tmp_path / "p"
        d.mkdir()
        projects.add_project(str(d))
        path = projects.list_projects()[0]["path"]
        projects.set_current(path)
        assert projects.remove_project(path) is True
        assert projects.get_current() is None           # 移除当前项目 → 回退无项目
        assert projects.list_projects() == []

    def test_remove_nonexistent(self, isolated_memory):
        assert projects.remove_project("/not/there") is False

    def test_current_name(self, isolated_memory, tmp_path):
        assert projects.get_current_name() == "无项目（全局）"
        d = tmp_path / "cool"
        d.mkdir()
        projects.add_project(str(d))
        projects.set_current(projects.list_projects()[0]["path"])
        assert projects.get_current_name() == "cool"
