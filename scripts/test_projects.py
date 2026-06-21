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

    def test_add_save_failure_reports_false(self, isolated_memory, tmp_path, monkeypatch):
        """写盘失败（磁盘满/无权限）时 add_project 必须返回 False，不能假成功；残留 .tmp 要清掉。"""
        import os as _os
        d = tmp_path / "p"
        d.mkdir()

        def _boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(projects.os, "replace", _boom)

        assert projects.add_project(str(d)) is False     # 写失败 → 不报成功
        assert projects.list_projects() == []            # 也没真正落盘
        leftover = projects.projects_file() + f".{_os.getpid()}.tmp"
        assert not _os.path.exists(leftover)             # 临时文件已清理

    def test_corrupt_json_backed_up_on_write(self, isolated_memory, tmp_path):
        """写路径遇到损坏的 projects.json：先备份 .corrupt 再重建，不静默覆盖、不抛。"""
        import os as _os
        with open(projects.projects_file(), "w", encoding="utf-8") as f:
            f.write("{ this is not valid json")
        d = tmp_path / "p"
        d.mkdir()
        assert projects.add_project(str(d)) is True                  # 重建成功
        assert _os.path.exists(projects.projects_file() + ".corrupt")  # 坏文件已留底
        assert any(p["path"].endswith("/p") for p in projects.list_projects())

    def test_corrupt_backup_failure_aborts_write(self, isolated_memory, tmp_path, monkeypatch):
        """损坏文件【备份失败】时，写操作必须中止、不覆盖原文件（防永久丢数据）。"""
        corrupt = "{ broken json \xff"
        with open(projects.projects_file(), "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(corrupt)

        def _boom(*a, **k):
            raise OSError("cannot copy")
        monkeypatch.setattr("shutil.copy2", _boom)

        d = tmp_path / "p"
        d.mkdir()
        assert projects.add_project(str(d)) is False     # 中止，不假成功
        # 原文件保持原样：没被空数据覆盖（损坏内容仍在，留待人工恢复）
        with open(projects.projects_file(), encoding="utf-8", errors="surrogateescape") as f:
            assert f.read() == corrupt

    def test_current_name(self, isolated_memory, tmp_path):
        assert projects.get_current_name() == "无项目（全局）"
        d = tmp_path / "cool"
        d.mkdir()
        projects.add_project(str(d))
        projects.set_current(projects.list_projects()[0]["path"])
        assert projects.get_current_name() == "cool"
