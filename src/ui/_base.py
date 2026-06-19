"""UI 包内部共用的路径常量。

区分两类目录（打包后它们不是同一个）：
- BASE_DIR：**只读打包资源**（icons / roles 等），打包后 = sys._MEIPASS
- 可写数据（config.json / theme_config / chat_memory）统一走 paths.py 的 APP_DIR
  —— 否则会出现"设置写到 _MEIPASS 临时目录、config.py 从 APP_DIR 读"的读写不一致，
  导致打包版设置存了重启不生效。
"""
import os
import sys

from ..paths import APP_DIR, MEMORY_DIR, CONFIG_PATH  # 可写数据：单一真相源


# 只读打包资源目录（icons 等）。打包后 = _MEIPASS
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    # __file__ = .../src/ui/_base.py，往上三层得到项目根目录
    BASE_DIR = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )


# 主题持久化文件（放可写的 chat_memory 目录，启动时恢复）
THEME_CONFIG_PATH = os.path.join(MEMORY_DIR, "theme_config.json")

# CONFIG_PATH 直接复用 paths.CONFIG_PATH（与 config.py 读取的是同一个文件，
# 保证 SettingsDialog 写入的位置 == config.py 读取的位置）。
# 通过本模块再导出一次，兼容 `from ._base import CONFIG_PATH` 的旧调用。
__all__ = ["BASE_DIR", "THEME_CONFIG_PATH", "CONFIG_PATH", "APP_DIR"]
