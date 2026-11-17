"""浏览器面板视图（§7.1 / §7.2）：地址栏 + "模型正在操作"徽标 + 嵌入的页面 + F12。

**嵌入而非另建**：view 直接用 page.get_browser().view——模型和人操作同一个 page，
桥的导航/点击在面板里实时可见。关标签调 page.close_browser() 释放 view/page；
模型自己调 browser_close 时 view 被销毁，destroyed 信号通知面板收标签。

**懒加载硬约束**（§11.7）：本模块顶层不 import QtWebEngine*；view 随第一次
open_browser（或第一次 browser_* 工具调用）由 page.get_browser() 创建。
**profile 归属照 §11.6**：模块持有、无 parent，不挂到会先销毁的控件下。

定时回调一律带 receiver 上下文（CLAUDE.md 的 QTimer.singleShot 实测崩溃那条）。
"""
import shiboken6
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..browser import page, security
from ..browser.bridge import get_bridge
from .fonts import CAPTION, mono_font


class BrowserView(QWidget):
    """文件面板"浏览器"标签的内容。构造即创建页面（第一次点开标签时）。"""

    closed = Signal()        # 嵌入的 view 被外部销毁（模型 browser_close）→ 面板收标签

    def __init__(self, parent=None, theme_lookup=None):
        super().__init__(parent)
        self.setObjectName("browserView")
        self._theme = theme_lookup or (lambda key: "#64748b")
        self._embedded = None
        self._wired_page = None
        self._releasing = False
        self._activity_text = ""
        self._user_typing = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.address = QLineEdit(self)
        self.address.setObjectName("browserAddress")
        self.address.setPlaceholderText("输入网址（http/https），回车打开")
        self.address.setFont(mono_font(CAPTION))
        self.address.returnPressed.connect(self._navigate)
        self.address.textEdited.connect(self._on_text_edited)
        self.go_btn = QPushButton("打开", self)
        self.go_btn.setObjectName("browserGo")
        self.go_btn.setCursor(Qt.PointingHandCursor)
        self.go_btn.clicked.connect(self._navigate)
        row.addWidget(self.address, 1)
        row.addWidget(self.go_btn, 0)
        lay.addLayout(row)

        self.badge = QLabel(self)
        self.badge.setObjectName("browserBusyBadge")
        self.badge.setVisible(False)
        lay.addWidget(self.badge)

        self._embed(page.get_browser().view)
        get_bridge().activity.connect(self._on_activity)
        # F12 只在本面板（含子控件）有焦点时生效，不抢整窗口的 F12
        QShortcut(QKeySequence("F12"), self,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut).activated.connect(
                      self._open_devtools)
        self.apply_theme()

    # ── 嵌入 / 释放 ──────────────────────────────────────────────
    def _embed(self, view):
        self._embedded = view
        self.layout().addWidget(view, 1)
        view.destroyed.connect(self._on_view_destroyed)
        b = page.get_browser()
        if self._wired_page is not None and shiboken6.isValid(self._wired_page):
            try:
                self._wired_page.urlChanged.disconnect(self._on_url_changed)
            except (RuntimeError, TypeError):
                pass
        b.page.urlChanged.connect(self._on_url_changed)
        self._wired_page = b.page
        self.address.setText(b.page.url().toString())

    def _ensure_embedded(self):
        if self._embedded is None or not shiboken6.isValid(self._embedded):
            self._embed(page.get_browser().view)

    def _on_view_destroyed(self):
        self._embedded = None
        if not self._releasing:
            self.closed.emit()

    def release(self):
        """关标签时调：释放 view/page（profile 常驻不销毁，§11.6）。"""
        self._releasing = True
        if self._embedded is not None and shiboken6.isValid(self._embedded):
            try:
                self._embedded.destroyed.disconnect(self._on_view_destroyed)
            except (RuntimeError, TypeError):
                pass
            self.layout().removeWidget(self._embedded)
        self._embedded = None
        page.close_browser()

    # ── 导航 / 徽标 ──────────────────────────────────────────────
    def navigate(self, url: str):
        """面板入口（background_panel 按钮 / 回复链接）调：校验后导航。"""
        url = (url or "").strip()
        err = security.validate_url(url)
        if err:
            self._flash(err)
            return
        self._ensure_embedded()
        self._user_typing = False
        self.address.setText(url)
        self._embedded.load(QUrl(url))

    def _navigate(self):
        self._user_typing = False
        self.navigate(self.address.text())

    def _on_text_edited(self, _text):
        self._user_typing = True

    def _on_url_changed(self, qurl):
        # 用户正在输入时不回写地址栏（焦点丢失/异步 urlChanged 都不该打断输入）
        if not self._user_typing and not self.address.hasFocus():
            self.address.setText(qurl.toString())

    def _on_activity(self, text):
        self._activity_text = text
        if text:
            self.badge.setText(f"模型正在操作浏览器 · {text}")
            self.badge.setVisible(True)
        else:
            self.badge.setVisible(False)

    def _flash(self, text):
        self.badge.setText(text)
        self.badge.setVisible(True)
        # receiver=self：窗口销毁后回调落在对象上下文里，不会访问已删除控件
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, self, self._clear_flash)

    def _clear_flash(self):
        if not self._activity_text:
            self.badge.setVisible(False)

    def _open_devtools(self):
        page.get_browser().open_devtools()

    # ── 主题 ─────────────────────────────────────────────────────
    def apply_theme(self):
        t = self._theme
        self.setStyleSheet(
            f"QWidget#browserView {{ background: {t('sidebar_bg')}; }}"
            f"QLineEdit#browserAddress {{ background: {t('input_bg')}; color: {t('text')};"
            f" border: 1px solid {t('input_border')}; border-radius: 6px; padding: 5px 8px; }}"
            f"QPushButton#browserGo {{ background: {t('send_active_bg')}; color: {t('send_text')};"
            f" border: none; border-radius: 6px; padding: 5px 14px; font-weight: 600; }}"
            f"QLabel#browserBusyBadge {{ background: {t('badge_warn_bg')};"
            f" color: {t('badge_warn_text')}; border: 1px solid {t('badge_warn_border')};"
            f" border-radius: 6px; padding: 3px 8px; font-size: 12px; }}"
        )