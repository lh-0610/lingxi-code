"""scripts/test_titlebar.py 的子进程：Windows 平台插件下真窗口的最大化 / 还原。

离屏测试走不到 Win32 那一半，这里开一个透明、不抢焦点的无边框窗口，挂上 FramelessController，
看最大化按钮和系统那边的最大化 / 还原（双击标题栏、拖到屏幕顶、拖标题栏还原都走系统）对得上：
- 按钮最大化后系统也认为最大化了（IsZoomed）——否则拖标题栏系统只当普通移动，窗口不会缩小；
- 系统最大化后按钮能还原；系统还原后按钮图标跟上。
每项检查打一行，全过最后打 OK。
"""
import ctypes
import os
import sys

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
os.environ.pop("QT_QPA_PLATFORM", None)          # 要真窗口
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget  # noqa: E402

app = QApplication(sys.argv)
from src.ui.titlebar import FramelessController  # noqa: E402

user32 = ctypes.windll.user32
WM_SYSCOMMAND, SC_MAXIMIZE, SC_RESTORE = 0x0112, 0xF030, 0xF120


class Win(QWidget):
    def __init__(self):
        super().__init__()
        self._frameless = FramelessController(self)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addStretch()
        top.addWidget(self._frameless.buttons)
        lay.addLayout(top)
        lay.addStretch()
        self.resize(800, 500)

    def nativeEvent(self, t, m):
        h = self._frameless.handle(t, m)
        return h if h is not None else super().nativeEvent(t, m)

    def showEvent(self, e):
        super().showEvent(e)
        self._frameless.apply_native_frame()
        self._frameless.buttons.sync_state()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.WindowStateChange:
            self._frameless.buttons.sync_state()


def pump(ms=300):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


failed = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        failed.append(name)


w = Win()
w.setAttribute(Qt.WA_ShowWithoutActivating, True)
w.setWindowOpacity(0.0)                          # 跑测试时屏幕上看不见
w.show()
pump()
hwnd = int(w.winId())
btns = w._frameless.buttons


def snap():
    return (bool(user32.IsZoomed(hwnd)), w.isMaximized(), btns.max_btn.maximized)


btns.toggle_maximized(); pump()
check("按钮最大化：系统、Qt、按钮图标都是最大化", snap() == (True, True, True), snap())
btns.toggle_maximized(); pump()
check("按钮还原：三处都回到普通", snap() == (False, False, False), snap())

user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_MAXIMIZE, 0); pump()
check("系统最大化：按钮图标跟上", snap() == (True, True, True), snap())
btns.toggle_maximized(); pump()
check("系统最大化后点按钮能还原", snap() == (False, False, False), snap())

btns.toggle_maximized(); pump()
user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0); pump()
check("系统还原（如拖标题栏）：Qt 和按钮图标跟上", snap() == (False, False, False), snap())

# 别处调了 Qt 的 showMaximized（只摆成屏幕大小，系统不认）：按钮以系统为准显示「最大化」，
# 点一下变成真最大化，而不是对系统眼里没最大化的窗口发「还原」、按钮失灵
w.showMaximized(); pump()
btns.sync_state()
check("Qt 假最大化：按钮图标按系统状态显示", btns.max_btn.maximized is False, snap())
btns.toggle_maximized(); pump()
check("Qt 假最大化后点按钮：变成系统最大化", snap() == (True, True, True), snap())
btns.toggle_maximized(); pump()
check("再点还原", snap() == (False, False, False), snap())

# Qt 假最大化时双击标题栏（系统最大化）：Qt 状态本来就是最大化、不发 WindowStateChange，
# 按钮图标只能靠 WM_SIZE 跟上
w.showMaximized(); pump()
btns.sync_state()
user32.SendMessageW(hwnd, WM_SYSCOMMAND, SC_MAXIMIZE, 0); pump()
check("Qt 假最大化时系统最大化：按钮图标跟上", snap() == (True, True, True), snap())
btns.toggle_maximized(); pump()

w.close()
pump(50)
print("OK" if not failed else "FAILED: " + ", ".join(failed))
