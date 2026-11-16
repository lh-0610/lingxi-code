"""测试本身不能碰用户的真实环境（隔离在 scripts/conftest.py 里，任何 src 模块导入之前就做）。

原来跑一遍测试：用户配置的 MCP 服务（memory / sequential-thinking / context7）会被真的拉起来，
测试记录写进程序目录下的 logs/当天.log——排查问题时日志里混着"修一下登录""scratch.txt"
这类测试内容，还会顺手清掉用户 30 天前的旧日志。

后来还实测出两桩更隐蔽的：test_file_panel 切一次主题就把用户真实的 theme_config.json 翻转
一次（THEME_CONFIG_PATH 在导入时按真实 MEMORY_DIR 拼死，isolated_memory 管不到它）；
test_rag_sidebar 刷新状态行就打开真实的 rag_index/chroma（那几条用例没用 isolated_memory）。
所以隔离改成**默认**：conftest 用 LINGXI_DATA_DIR 把整个数据根搬进临时目录，本文件守住这条线。

这里只断言"测试进程拿到的路径不在真实数据根下"，不做"跑完对比真实目录哈希"——用户可能同时
开着灵犀，真实目录本来就会变，那种断言会误报。
"""
import logging
import os


def _inside(path, root):
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:      # 不在同一个盘
        return False


def _real_app_data_dir():
    """用户真实的数据根（开发期 = 仓库根，src/paths.py 往上两层）。

    不能用 paths.APP_DIR：测试里它已经被 LINGXI_DATA_DIR 指到临时目录了，
    拿它当"真实根"会让下面所有断言变成自说自话。
    """
    from src import paths
    return os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))


def test_every_data_path_is_outside_the_real_data_dir():
    """所有数据路径都不在真实应用数据目录下——漏一个，某条用例就会写用户数据。"""
    from src import paths
    real = _real_app_data_dir()
    checked = {
        "APP_DIR": paths.APP_DIR,
        "MEMORY_DIR": paths.MEMORY_DIR,
        "MCP_DIR": paths.MCP_DIR,
        "LOG_DIR": paths.LOG_DIR,
        "CONFIG_PATH": paths.CONFIG_PATH,
        "memory_dir()": paths.memory_dir(),
        "memory_index()": paths.memory_index(),
        "role_config()": paths.role_config(),
        "projects_file()": paths.projects_file(),
        "long_term_memory_file()": paths.long_term_memory_file(),
        "permission_rules_file()": paths.permission_rules_file(),
        "rag_index_dir()": paths.rag_index_dir(),
        "theme_config_path()": paths.theme_config_path(),
    }
    inside = {name: path for name, path in checked.items() if _inside(path, real)}
    assert not inside, f"这些数据路径仍在真实数据根 {real} 下：{inside}"


def test_logs_go_to_a_temporary_folder():
    from src import paths
    real = os.path.join(_real_app_data_dir(), "logs")
    files = [h.baseFilename for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
    assert files, "日志仍然写文件（写到临时目录）"
    assert not any(_inside(f, real) for f in files), files
    assert not _inside(paths.LOG_DIR, real)


def test_theme_choice_follows_the_data_root(tmp_path):
    """主题落盘必须跟着数据根走，不能是导入时拼死的常量。

    原来的 THEME_CONFIG_PATH 就是常量：isolated_memory / set_data_dir 换了数据根，
    save_theme_choice 照样写用户真实的 chat_memory/theme_config.json。
    """
    from src import paths
    from src.ui import theme

    paths.set_data_dir(str(tmp_path))
    try:
        theme.save_theme_choice("dark")
        written = tmp_path / "chat_memory" / "theme_config.json"
        assert written.is_file(), "主题没写进当前数据根"
        assert theme.load_saved_theme() == "dark"
        assert os.path.normcase(str(written)) == os.path.normcase(paths.theme_config_path())
    finally:
        paths.set_data_dir(None)


def test_rag_store_dir_follows_the_data_root(tmp_path):
    """向量库目录同理：构造 VectorStore 时取的是当前数据根，不是导入时的真实索引。"""
    from src import paths
    from src.rag.store import VectorStore

    paths.set_data_dir(str(tmp_path))
    try:
        store_dir = VectorStore("隔离自检").dir
        assert _inside(store_dir, str(tmp_path)), store_dir
        assert not _inside(store_dir, _real_app_data_dir()), store_dir
    finally:
        paths.set_data_dir(None)


def test_configured_mcp_servers_are_never_started():
    import src.agent  # noqa: F401  它一导入就会开后台线程去初始化 MCP
    from src import mcp_client
    assert mcp_client.init_mcp() == []
    assert mcp_client._bg_thread is None, "真实的 init_mcp 跑过（它会起守护线程连 server）"
    assert not mcp_client._sessions