"""自绘标题栏（Windows）：去掉系统那条白色标题栏，窗口按钮放进我们自己的顶栏。

做法是业内常见的「无边框 + 把原生窗框样式加回来」：
- Qt 侧 ``FramelessWindowHint``（Qt 不再给标题栏留位置），再用 Win32 把 ``WS_THICKFRAME`` /
  ``WS_CAPTION`` / 最小化 / 最大化样式加回来——拖边改大小、拖到屏幕边贴边（Aero Snap）、
  最小化 / 最大化动画、DWM 阴影和 Win11 圆角都还是系统的；
- ``WM_NCCALCSIZE`` 把非客户区收成 0（整个窗口都是我们画的），最大化时把客户区夹到显示器
  可用区域里（不能固定收一圈窗框厚度，见 handle 里的说明）；任务栏自动隐藏时底边留 2px；
- ``WM_NCHITTEST`` 自己判：四边四角 → 改大小；顶部 56px 里不是按钮 / 输入框 / 标签页的空白处
  → ``HTCAPTION``（拖动、双击最大化、右键系统菜单都是系统的）；我们的最大化按钮 →
  ``HTMAXBUTTON``，这样 Win11 鼠标停在上面会弹分屏布局；它的点击和悬停在
  ``WM_NCLBUTTONDOWN / UP`` 和命中测试里自己处理（这时 Qt 收不到鼠标事件）；
- 最大化 / 还原用 ``ShowWindow``、状态问 ``IsZoomed``，不用 Qt 的 showMaximized / showNormal
  （见 ``is_maximized``）。

窗口按钮（最小化 / 最大化或还原 / 关闭）是顶栏这一行的最后一项（header._build_header 放进去）。
顶栏一整行横跨对话区和右侧面板（参照 Claude 桌面版），右侧面板自己的标签行和关闭按钮在顶栏下面，
不和窗口按钮挤在一条线上。

只在 Windows 平台插件下启用；config.json 的 ``native_title_bar: true``（设置 → 高级）退回系统标题栏。
测试跑 offscreen 不走这里。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QComboBox, QHBoxLayout, QLineEdit, QTabBar, QWidget,
)

from .theme import color as _c

CAPTION_HEIGHT = 56          # 顶部这么高的一条里，空白处都能拖动窗口
BUTTON_W, BUTTON_H = 46, 56  # 宽是 Windows 惯用的 46；高和顶栏一样，图标和顶栏按钮在一条线上
HOVER_H, HOVER_R = 36, 8      # 悬停底色：居中的圆角块，和顶栏图标按钮的悬停一样大小
RESIZE_BORDER = 6            # 四边这么宽可以拖着改大小（逻辑像素）

# Win32 常量
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCMOUSELEAVE = 0x02A2
HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
GWL_STYLE = -16
WS_THICKFRAME, WS_CAPTION, WS_SYSMENU = 0x00040000, 0x00C00000, 0x00080000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
ABM_GETSTATE, ABS_AUTOHIDE = 0x00000004, 0x0000001
WM_SIZE = 0x0005
SW_MAXIMIZE, SW_RESTORE = 3, 9


def _native_hwnd(window: QWidget) -> int:
    """Windows 平台插件下窗口已有的原生句柄；没有（offscreen 测试、还没建出来）返回 0。"""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return 0
    try:
        return int(window.internalWinId() or 0)
    except Exception:
        return 0


def is_maximized(window: QWidget) -> bool:
    """窗口是不是最大化了：以系统为准（IsZoomed），没有原生句柄才问 Qt。
    Qt 对无边框窗口的最大化状态不可靠：从系统那边最大化（双击标题栏、拖到屏幕顶、Win+↑）后
    Qt 的 showNormal() 什么都不做；Qt 自己的 showMaximized() 又只是把窗口摆成屏幕大小、系统
    并不认为它最大化了（拖标题栏不会缩小）。"""
    hwnd = _native_hwnd(window)
    if hwnd:
        return bool(ctypes.windll.user32.IsZoomed(hwnd))
    return window.isMaximized()


def enabled() -> bool:
    """这台机器 / 这次启动要不要用自绘标题栏。"""
    if (sys.platform != "win32" or QGuiApplication.instance() is None
            or QGuiApplication.platformName() != "windows"):
        return False
    try:
        from .. import config
        return not bool(getattr(config, "NATIVE_TITLE_BAR", False))
    except Exception:
        return True


# ── 窗口按钮 ───────────────────────────────────────────────────────

class _CaptionButton(QAbstractButton):
    """自绘的窗口按钮：线条图标；关闭键悬停变红底白字（Windows 惯例）。"""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind                 # "min" / "max" / "close"
        self.maximized = False
        self.nc_hover = False            # 最大化键：命中测试报 HTMAXBUTTON 时 Qt 收不到悬停，自己记
        self.nc_pressed = False
        self.setFixedSize(BUTTON_W, BUTTON_H)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setAttribute(Qt.WA_Hover, True)
        tips = {"min": "最小化", "max": "最大化", "close": "关闭"}
        self.setToolTip(tips[kind])
        self.setAccessibleName(tips[kind])

    def sizeHint(self):
        return QSize(BUTTON_W, BUTTON_H)

    def paintEvent(self, event):
        hover = self.underMouse() or self.nc_hover
        down = self.isDown() or self.nc_pressed
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = QColor(_c("dlg_title"))     # 用正文色：次要色在浅色主题下太淡（实机看过）
        # 悬停底色只画居中的圆角块，不铺满 56px 高的整列（用户反馈太高）。按钮本身（可点区域）
        # 仍是整列：最大化时鼠标甩到屏幕右上角照样点得中关闭
        bg_rect = QRectF(3, (self.height() - HOVER_H) / 2, self.width() - 6, HOVER_H)
        if self.kind == "close" and (hover or down):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#c42b1c" if not down else "#b22a1b"))
            p.drawRoundedRect(bg_rect, HOVER_R, HOVER_R)
            fg = QColor("#ffffff")
        elif hover or down:
            bg = QColor(_c("dlg_hover_bg"))
            if down:
                bg = bg.darker(110)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(bg_rect, HOVER_R, HOVER_R)
        pen = QPen(fg, 1.0)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)           # 底色的画刷别带进图形（最大化的方框会被填上）
        cx, cy = self.width() / 2, self.height() / 2
        s = 5.0                          # 图形半宽（10px 的图形，Windows 惯例）
        if self.kind == "min":
            p.drawLine(int(cx - s), int(cy), int(cx + s), int(cy))
        elif self.kind == "close":
            p.drawLine(int(cx - s), int(cy - s), int(cx + s), int(cy + s))
            p.drawLine(int(cx + s), int(cy - s), int(cx - s), int(cy + s))
        elif self.maximized:             # 还原：两个错开的方框
            p.drawRect(QRectF(cx - s, cy - s + 2, 2 * s - 2, 2 * s - 2))
            p.drawLine(int(cx - s + 2), int(cy - s), int(cx + s), int(cy - s))
            p.drawLine(int(cx + s), int(cy - s), int(cx + s), int(cy + s - 2))
        else:
            p.drawRect(QRectF(cx - s, cy - s, 2 * s, 2 * s))
        p.end()


class WindowButtons(QWidget):
    """右上角那三个按钮，放在顶栏布局的最后。"""

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName("windowButtons")
        self._window = window
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.min_btn = _CaptionButton("min", self)
        self.max_btn = _CaptionButton("max", self)
        self.close_btn = _CaptionButton("close", self)
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)
        self.min_btn.clicked.connect(window.showMinimized)
        self.max_btn.clicked.connect(self.toggle_maximized)
        self.close_btn.clicked.connect(window.close)
        self.setFixedSize(3 * BUTTON_W, BUTTON_H)

    def toggle_maximized(self):
        """最大化 / 还原交给系统（ShowWindow），Qt 的窗口状态会跟着 WM_SIZE 更新。
        不能用 Qt 的 showMaximized / showNormal，见 is_maximized 的说明。"""
        w = self._window
        hwnd = _native_hwnd(w)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE if is_maximized(w) else SW_MAXIMIZE)
        else:
            w.showNormal() if w.isMaximized() else w.showMaximized()

    def sync_state(self):
        self.max_btn.maximized = is_maximized(self._window)
        self.max_btn.setToolTip("还原" if self.max_btn.maximized else "最大化")
        self.max_btn.setAccessibleName(self.max_btn.toolTip())
        self.max_btn.update()

    def refresh_theme(self):
        for b in (self.min_btn, self.max_btn, self.close_btn):
            b.update()


# ── 命中测试（纯逻辑，可在 offscreen 下测）──────────────────────────

_INTERACTIVE = (QAbstractButton, QLineEdit, QComboBox, QAbstractScrollArea)


def hit_test(window: QWidget, local: QPoint, *, maximized: bool, buttons: WindowButtons | None) -> int:
    """窗口坐标 local 上该报什么：改大小的边 / HTCAPTION / HTMAXBUTTON / HTCLIENT。"""
    w, h = window.width(), window.height()
    x, y = local.x(), local.y()
    if not maximized:
        b = RESIZE_BORDER
        left, right, top, bottom = x < b, x >= w - b, y < b, y >= h - b
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
    if buttons is not None and buttons.isVisible():
        mb = buttons.max_btn
        if mb.rect().contains(mb.mapFrom(window, local)):
            return HTMAXBUTTON
        if buttons.rect().contains(buttons.mapFrom(window, local)):
            return HTCLIENT
    if y >= CAPTION_HEIGHT:
        return HTCLIENT
    child = window.childAt(local)
    node = child
    while node is not None and node is not window:
        if isinstance(node, QTabBar):
            if node.tabAt(node.mapFrom(window, local)) >= 0:
                return HTCLIENT
        elif isinstance(node, _INTERACTIVE) or node.focusPolicy() & Qt.ClickFocus:
            if not isinstance(node, QTabBar):
                return HTCLIENT
        node = node.parentWidget()
    return HTCAPTION


# ── Win32 接管 ─────────────────────────────────────────────────────

class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


class _WinIdWatcher(QObject):
    """原生句柄换了（Qt 为嵌入 GPU 控件重建窗口时会这样）就把窗框样式重新加回去。
    main.py 已经让主窗口一开始就走 GPU 合成、正常不会重建；这里是兜底。"""

    def __init__(self, controller, parent):
        super().__init__(parent)
        self._c = controller

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent, QTimer
        if event.type() == QEvent.WinIdChange and self._c._applied:
            QTimer.singleShot(0, self, self._c.apply_native_frame)
        return False


class FramelessController:
    """挂在主窗口上：构造时设 FramelessWindowHint，第一次显示后把原生窗框样式加回来，
    nativeEvent 里处理 NCCALCSIZE / NCHITTEST / 最大化键。"""

    def __init__(self, window: QWidget):
        self.window = window
        self.buttons = WindowButtons(window)
        self._applied = False
        self._hwnd = 0
        window.setWindowFlag(Qt.FramelessWindowHint, True)
        window.installEventFilter(_WinIdWatcher(self, window))

    def apply_native_frame(self):
        """窗口句柄有了之后调一次（showEvent）：样式加回窗框位，DWM 留 1px 让阴影出来。"""
        if QGuiApplication.platformName() != "windows":
            return
        hwnd = int(self.window.winId())
        if self._applied and hwnd == self._hwnd:
            return
        self._applied = True
        self._hwnd = hwnd
        user32 = ctypes.windll.user32
        get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_ = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get.restype = ctypes.c_ssize_t
        style = get(hwnd, GWL_STYLE)
        set_(hwnd, GWL_STYLE, style | WS_THICKFRAME | WS_CAPTION | WS_SYSMENU
             | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
        try:
            margins = _MARGINS(0, 0, 0, 1)
            ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        except Exception:
            pass
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)

    @staticmethod
    def _work_area(hwnd):
        """窗口所在显示器的可用区域（去掉任务栏），物理像素。"""
        try:
            user32 = ctypes.windll.user32
            user32.MonitorFromWindow.restype = wintypes.HMONITOR
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            hmon = user32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return None
            return info.rcWork
        except Exception:
            return None

    @staticmethod
    def _taskbar_autohide() -> bool:
        try:
            data = _APPBARDATA()
            data.cbSize = ctypes.sizeof(_APPBARDATA)
            return bool(ctypes.windll.shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data))
                        & ABS_AUTOHIDE)
        except Exception:
            return False

    def handle(self, event_type, message):
        """返回 (handled, result)；没处理返回 None，交回 Qt。"""
        if bytes(event_type) != b"windows_generic_MSG":
            return None
        msg = wintypes.MSG.from_address(int(message))
        m = msg.message
        win = self.window
        if m == WM_NCCALCSIZE and msg.wParam:
            # 问系统而不是问 Qt：拖到屏幕顶 / 系统菜单最大化时，这条消息来的时候 Qt 的状态还没更新
            # （实测：窗口已经是 -11,-11 起的最大化矩形，win.isMaximized() 还是 False）
            if ctypes.windll.user32.IsZoomed(msg.hWnd) and not win.isFullScreen():
                # 最大化：客户区夹到显示器的可用区域里。不能固定往里收一圈窗框厚度——Qt 给无边框
                # 窗口最大化时已经把窗口摆成正好等于可用区域，再收一圈，Qt 按整块画、系统只显示
                # 中间那块，四周露出一圈窗框、底边和右上角被裁掉（用户实测「伪全屏」）。
                # 窗口真的超出屏幕时（系统按带窗框的样式最大化），夹一下也正好把多出的那圈去掉。
                params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
                rect = params.rgrc[0]
                work = self._work_area(int(msg.hWnd))
                if work is not None:
                    rect.left = max(rect.left, work.left)
                    rect.top = max(rect.top, work.top)
                    rect.right = min(rect.right, work.right)
                    rect.bottom = min(rect.bottom, work.bottom)
                    if self._taskbar_autohide():
                        rect.bottom = min(rect.bottom, work.bottom - 2)
                    params.rgrc[0] = rect
            return True, 0
        if m == WM_NCHITTEST:
            local = win.mapFromGlobal(QCursor.pos())
            ht = hit_test(win, local, maximized=is_maximized(win), buttons=self.buttons)
            mb = self.buttons.max_btn
            hover = ht == HTMAXBUTTON
            if mb.nc_hover != hover:
                mb.nc_hover = hover
                if not hover:
                    mb.nc_pressed = False
                mb.update()
            if ht == HTCLIENT:
                return None
            return True, ht
        if m == WM_NCLBUTTONDOWN and msg.wParam == HTMAXBUTTON:
            self.buttons.max_btn.nc_pressed = True
            self.buttons.max_btn.update()
            return True, 0
        if m == WM_NCLBUTTONUP and msg.wParam == HTMAXBUTTON:
            pressed = self.buttons.max_btn.nc_pressed
            self.buttons.max_btn.nc_pressed = False
            self.buttons.max_btn.update()
            if pressed:
                self.buttons.toggle_maximized()
            return True, 0
        if m == WM_SIZE:
            # 最大化 / 还原（含拖标题栏还原、贴边）都会来这条；按钮图标以系统状态为准跟上，
            # 不只靠 Qt 的 WindowStateChange（Qt 状态可能没跟上）
            self.buttons.sync_state()
            return None
        if m == WM_NCMOUSELEAVE:
            mb = self.buttons.max_btn
            if mb.nc_hover or mb.nc_pressed:
                mb.nc_hover = mb.nc_pressed = False
                mb.update()
        return None
