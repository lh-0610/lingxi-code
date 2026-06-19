"""路径常量 + 日志初始化。

`_app_data_dir()` 永远指向**项目根目录**（src/ 的上一级）。
这样无论模块文件被搬到哪个子目录，对 config.json / chat_memory / logs 的
访问位置都不变。
"""
import os
import sys
import logging
import threading as _threading
from datetime import datetime, timedelta


def _app_data_dir():
    """运行时**可写数据**根目录（config.json / chat_memory / logs）：
    - 开发期：src/ 的上一级目录（项目根）—— 方便开发时数据就在项目里
    - PyInstaller 打包后：`%APPDATA%\\灵犀`（Windows 标准用户数据目录）
      不放 exe 旁边的原因：1) 装到 Program Files 时 exe 目录没写权限；
      2) 不污染安装目录；3) 重装/更新 exe 不丢历史和配置。
    """
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "灵犀")
    # __file__ = .../src/paths.py，往上 2 层得到项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resource_dir():
    """**只读打包资源**根目录（icons / roles 等随包发布的文件）：
    - 开发期：项目根（同 APP_DIR）
    - PyInstaller 打包后：sys._MEIPASS（onefile 解压的临时目录 / onedir 的 _internal）
      —— 注意这跟 APP_DIR（exe 目录）**不是同一个**，打包资源必须从这里读。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_DIR = _app_data_dir()
RESOURCE_DIR = _resource_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
MEMORY_DIR = os.path.join(APP_DIR, "chat_memory")
MEMORY_INDEX = os.path.join(MEMORY_DIR, "index.json")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
ROLE_CONFIG = os.path.join(MEMORY_DIR, "role_config.json")
MCP_DIR = os.path.join(APP_DIR, "mcp")


# ── 按用户/上下文的数据根(多用户 Web 用)────────────────────────────────────
# 桌面端不调 set_data_dir → get_data_dir() 返回默认 APP_DIR,行为与重构前完全一致。
# Web 端每个登录用户(及其 worker 线程)set_data_dir(<用户目录>),让 chat_memory /
# projects / long_term_memory / role_config 落到各自子目录,实现数据隔离。
_data_ctx = _threading.local()


def set_data_dir(path):
    """设置当前线程的数据根目录;path=None 还原为默认(APP_DIR)。"""
    if path is None:
        _data_ctx.dir = None
        return
    _data_ctx.dir = path
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def get_data_dir():
    """当前上下文的数据根:线程本地覆盖优先,否则默认 APP_DIR。"""
    return getattr(_data_ctx, "dir", None) or APP_DIR


def memory_dir():
    """chat_memory 目录(按当前上下文)。默认上下文回退到模块级 MEMORY_DIR,
    保证桌面端 + 测试 monkeypatch(paths.MEMORY_DIR)行为不变。"""
    d = getattr(_data_ctx, "dir", None)
    return os.path.join(d, "chat_memory") if d else MEMORY_DIR


def memory_index():
    return os.path.join(memory_dir(), "index.json")


def role_config():
    return os.path.join(memory_dir(), "role_config.json")


def projects_file():
    return os.path.join(memory_dir(), "projects.json")


def long_term_memory_file():
    return os.path.join(memory_dir(), "long_term_memory.json")


os.makedirs(APP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# 首次启动：APP_DIR 下没有 config.json，就从打包的 config.example.json 复制一份过去。
# （打包后 config.example.json 在 RESOURCE_DIR/_MEIPASS；config.py 紧接着会读 CONFIG_PATH）
if not os.path.exists(CONFIG_PATH):
    _example = os.path.join(RESOURCE_DIR, "config.example.json")
    if os.path.exists(_example):
        try:
            import shutil
            shutil.copyfile(_example, CONFIG_PATH)
        except Exception:
            pass


def _cleanup_old_logs(days=30):
    cutoff = datetime.now() - timedelta(days=days)
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if not os.path.isfile(path) or not name.lower().endswith(".log"):
            continue
        try:
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                os.remove(path)
        except Exception:
            pass


# 全局 logger 配置（与原 agent.py 行为完全一致：按日期分文件 + 同时输出到控制台）
_log_file = os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y%m%d')}.log")
_cleanup_old_logs()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("agent")

# 第三方库 INFO 噪声压到 WARNING：httpx 会把完整请求 URL（含 Telegram bot token）写进
# INFO 日志（存 30 天，等于明文泄漏 token）；也顺手消掉每 30s 一条 getUpdates 的刷屏。
for _noisy_logger in ("httpx", "httpcore", "telegram"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
