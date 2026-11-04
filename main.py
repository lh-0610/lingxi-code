import sys
import os
import ctypes

# 设置独立 AppID（Windows 任务栏图标）
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("lingxi.ai.desktop")
except Exception:
    pass

# 高 DPI 清晰渲染
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon

# 兼容 PyInstaller 打包：打包后资源位于 sys._MEIPASS
if getattr(sys, "frozen", False):
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)

    # 只开一个：已经有一个在跑，就把它的窗口叫出来，自己退出。放在加载界面之前——界面要好几秒，
    # 这期间再点图标的那一下也得认出来；窗口还没建好时收到的请求不用管，它马上就会自己出来。
    from src import single_instance
    from src.paths import APP_DIR
    _instance_name = single_instance.server_name(APP_DIR)
    _main_window = []
    if not single_instance.acquire(_instance_name, lambda: _main_window and show_window(_main_window[0])):
        # 已经有一个拿着锁（可能刚启动、还没挂上服务，等它一会儿）
        if single_instance.notify_running(_instance_name, wait_s=5):
            sys.exit(0)
        # 拿着锁却叫不应（卡死了？）：照常启动，总比点了没反应强

    from src.ui import ChatUI
    from src.ui.fonts import ui_font
    from src.floating import create_tray, show_window

    app_font = ui_font(13)
    app_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(app_font)

    # 设置应用图标
    icon_path = os.path.join(BASE_DIR, "icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 不让最后一个可见窗口关闭时整个 app 退出，由系统托盘维持后台
    app.setQuitOnLastWindowClosed(False)

    window = ChatUI()
    window._hide_on_close = True  # 关闭按钮 → 隐藏而非退出
    window.show()
    _main_window.append(window)

    # 系统托盘（关窗后维持后台，双击唤起）
    tray = create_tray(app, window, icon_path=icon_path if os.path.exists(icon_path) else None)

    # 启动 Telegram 遥控轮询（config 启用 + bot token 齐备时自动生效）
    from src.telegram_poll import start as _tg_poll_start, shutdown as _tg_poll_shutdown
    _tg_poll_start()

    # 退出前清理后台命令和各类子进程，避免端口残留
    def _cleanup_on_exit():
        # 先让出单实例的名字：下面的清理要好几秒，这期间再点图标该开一个新的，
        # 而不是去叫一个正在退出的进程
        single_instance.release()
        # 停止所有后台命令（dev server / watch 等）
        try:
            from src.tools import stop_all_background
            stop_all_background(shutdown=True)
        except Exception:
            pass
        # 关闭 LSP 客户端（释放 pyright/pylsp 子进程）
        try:
            from src.lsp_client import shutdown as _lsp_shutdown
            _lsp_shutdown()
        except Exception:
            pass
        # 关闭 MCP 守护线程（释放 Server-Process 子进程）
        try:
            from src.mcp_client import shutdown as _mcp_shutdown
            _mcp_shutdown()
        except Exception:
            pass
        # 停止 Telegram 遥控轮询
        try:
            _tg_poll_shutdown()
        except Exception:
            pass
        # worktree 隔离区由用户显式恢复/丢弃，退出时保留以避免丢失改动。
    app.aboutToQuit.connect(_cleanup_on_exit)

    sys.exit(app.exec())
