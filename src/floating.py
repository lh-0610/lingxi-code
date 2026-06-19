"""系统托盘图标。

主聊天窗口关闭后由托盘维持后台运行；托盘提供「打开对话 / 退出」，双击唤起窗口。
（桌面宠物已移除——本应用专注代码助手；娱乐属性以后另开独立应用。）
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def _restore_window(win):
    """显示主窗口，但**保留它原本的最大化/全屏状态**。

    别直接用 showNormal()——它会把最大化/全屏的窗口强制还原成普通尺寸（缩回默认
    1000×700）。这里去掉最小化标志、保留 Maximized/FullScreen，再 show()。
    """
    st = win.windowState()
    # 清掉最小化标志，保留最大化 / 全屏
    if st & Qt.WindowMinimized:
        win.setWindowState(st & ~Qt.WindowMinimized)
    win.show()  # show() 按当前 windowState 显示，最大化/全屏都不动


def create_tray(app, chat_window, icon_path=None):
    """创建系统托盘图标，主聊天窗口关闭后由托盘维持后台。双击 / 「打开对话」唤起窗口。"""
    tray = QSystemTrayIcon(app)
    if icon_path and os.path.exists(icon_path):
        tray.setIcon(QIcon(icon_path))
    else:
        tray.setIcon(app.style().standardIcon(app.style().StandardPixmap.SP_ComputerIcon))
    tray.setToolTip("灵犀 AI 助手（双击唤起对话，右键退出）")

    def _show_chat():
        _restore_window(chat_window)
        chat_window.raise_()
        chat_window.activateWindow()

    # parent=chat_window 让 menu 生命周期跟主窗口绑定，否则函数返回后 menu 被 GC，托盘右键就没反应
    menu = QMenu(chat_window)

    a_chat = QAction("打开对话", menu)
    a_chat.triggered.connect(_show_chat)
    menu.addAction(a_chat)

    menu.addSeparator()

    a_exit = QAction("退出", menu)
    a_exit.triggered.connect(app.quit)
    menu.addAction(a_exit)

    tray.setContextMenu(menu)
    tray._menu = menu  # 双保险

    def _on_activated(reason):
        if reason == QSystemTrayIcon.Trigger:
            _show_chat()
    tray.activated.connect(_on_activated)

    tray.show()
    return tray
