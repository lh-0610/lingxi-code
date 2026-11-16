"""UI 包内部共用的路径常量。

区分两类目录（打包后它们不是同一个）：
- BASE_DIR：**只读打包资源**（icons / roles 等），打包后 = sys._MEIPASS
- 可写数据（config.json / chat_memory）统一走 paths.py 的 APP_DIR
  —— 否则会出现"设置写到 _MEIPASS 临时目录、config.py 从 APP_DIR 读"的读写不一致，
  导致打包版设置存了重启不生效。

chat_memory 下的具体文件路径一律用 paths 的函数（memory_dir() / theme_config_path() 等）
在**调用时**取，不要在这里拼成常量——常量会把导入那一刻的数据根固化，线程本地上下文和
测试隔离都改不动它。
"""
import os
import sys

from ..paths import APP_DIR, CONFIG_PATH  # 可写数据：单一真相源


# 只读打包资源目录（icons 等）。打包后 = _MEIPASS
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    # __file__ = .../src/ui/_base.py，往上三层得到项目根目录
    BASE_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


# CONFIG_PATH 直接复用 paths.CONFIG_PATH（与 config.py 读取的是同一个文件，
# 保证 SettingsDialog 写入的位置 == config.py 读取的位置）。
# 通过本模块再导出一次，兼容 `from ._base import CONFIG_PATH` 的旧调用。
__all__ = ["BASE_DIR", "CONFIG_PATH", "APP_DIR"]
