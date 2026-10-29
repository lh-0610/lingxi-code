"""测试本身不能碰用户的真实环境（隔离在 scripts/conftest.py 里，任何 src 模块导入之前就做）。

原来跑一遍测试：用户配置的 MCP 服务（memory / sequential-thinking / context7）会被真的拉起来，
测试记录写进程序目录下的 logs/当天.log——排查问题时日志里混着"修一下登录""scratch.txt"
这类测试内容，还会顺手清掉用户 30 天前的旧日志。
"""
import logging
import os


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:      # 不在同一个盘
        return False


def test_logs_go_to_a_temporary_folder():
    from src import paths
    real = os.path.join(paths.APP_DIR, "logs")
    files = [h.baseFilename for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert files, "日志仍然写文件（写到临时目录）"
    assert not any(_inside(f, real) for f in files), files
    assert not _inside(paths.LOG_DIR, real)


def test_configured_mcp_servers_are_never_started():
    import src.agent  # noqa: F401  它一导入就会开后台线程去初始化 MCP
    from src import mcp_client
    assert mcp_client.init_mcp() == []
    assert mcp_client._bg_thread is None, "真实的 init_mcp 跑过（它会起守护线程连 server）"
    assert not mcp_client._sessions
