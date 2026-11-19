"""浏览器面板视图（设计稿「浏览器面板 · 三种状态」+「开发者工具嵌在下方」）。

一个标签里依次是：工具栏（后退 / 前进 / 刷新、地址胶囊（带「本机 · 已信任」这类来源标签）、
F12、更多）→ 2px 加载条 → 内容区。内容区三种状态：
- **空白**：还没打开网页时，列出后台任务里正在跑的本机服务（点「打开」直接进）和最近打开的页面；
- **浏览中**：嵌入的页面；模型正在操作时页面描一圈靛蓝边，底部浮一张状态卡
  「灵犀正在点击…」+「停止」（停的是正在驱动浏览器的那个会话，见 bridge.driver_session）；
- **连不上**：我们自己的错误页（重新加载 / 去后台看看），不用 Chromium 那页英文。
开发者工具（F12）停靠在页面下方、可拖动分隔条，也能弹成单独窗口。

**嵌入而非另建**：每个网页一个面板标签，各嵌浏览器对应标签的 view——模型和人操作同一个
page，桥的导航/点击在面板里实时可见。关面板标签调 page.close_tab() 关掉那一页（最后一页
等于关浏览器）；模型关标签 / browser_close 时 page 发 tab_closed，面板拆掉对应视图。

**懒加载硬约束**（§11.7）：本模块顶层不 import QtWebEngine*；view 随第一次
open_browser（或第一次 browser_* 工具调用）由 page.get_browser() 创建。
**profile 归属照 §11.6**：模块持有、无 parent，不挂到会先销毁的控件下。

定时回调一律带 receiver 上下文（CLAUDE.md 的 QTimer.singleShot 实测崩溃那条）。
"""
import time
import urllib.parse

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSplitter,
    QStackedWidget, QVBoxLayout, QWidget,
)

from ..browser import page, security
from ..browser.bridge import get_bridge
from .dialogs import _svg_pixmap, qcolor
from .fonts import MONO_CSS, UI_CSS, mono_font

# 线性图标（16 视框），颜色在用的时候按主题填
_ICONS = {
    "back": '<path d="M10 3L5 8l5 5"/>',
    "forward": '<path d="M6 3l5 5-5 5"/>',
    "reload": '<path d="M13 8a5 5 0 1 1-1.5-3.5"/><path d="M13 3v3h-3"/>',
    "stop": '<path d="M4 4l8 8M12 4l-8 8"/>',
    "devtools": '<path d="M5.5 4.5L2 8l3.5 3.5"/><path d="M10.5 4.5L14 8l-3.5 3.5"/>',
    "more": '<circle cx="3.5" cy="8" r="0.6"/><circle cx="8" cy="8" r="0.6"/><circle cx="12.5" cy="8" r="0.6"/>',
    "popout": '<path d="M9 3h4v4"/><path d="M13 3L8 8"/><path d="M11 9.5V13H3V5h3.5"/>',
    "close": '<path d="M4 4l8 8M12 4l-8 8"/>',
    "globe": ('<circle cx="8" cy="8" r="6"/><path d="M2 8h12"/><path d="M8 2a9.3 9.3 0 0 1 0 12"/>'
              '<path d="M8 2a9.3 9.3 0 0 0 0 12"/>'),
    "error": '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.6"/><path d="M8 11h.01"/>',
    "plug": '<path d="M6 4.7L2.7 8 6 11.3"/><path d="M10 4.7L13.3 8 10 11.3"/><path d="M8.9 3.3l-1.8 9.4"/>',
}
_RECENT_MAX = 6
_recent_pages: list = []        # 本次运行里打开过的页面 [{title, url}]，面板重开也还在（不落盘）
_ACTIVE_LINGER_MS = 900         # 模型两次动作之间的空档不收状态卡，免得一闪一闪


def _icon(name, color, size=15, *, stroke=1.7, disabled_color=None) -> QIcon:
    body = _ICONS[name]
    if name == "more":
        stroke = 2.6                          # 三个点用粗描边画成实心
    dpr = 2.0
    icon = QIcon(_svg_pixmap(body, color, size, view=16, stroke=stroke, dpr=dpr))
    if disabled_color:
        icon.addPixmap(_svg_pixmap(body, disabled_color, size, view=16, stroke=stroke, dpr=dpr),
                       QIcon.Disabled)
    return icon


def remember_page(title: str, url: str):
    """加载成功的 http(s) 页面记进「最近打开」（去重，新的在前）。"""
    if not url or security.validate_url(url):
        return
    global _recent_pages
    _recent_pages = [p for p in _recent_pages if p["url"] != url]
    _recent_pages.insert(0, {"title": title or url, "url": url})
    del _recent_pages[_RECENT_MAX:]


def recent_pages() -> list:
    return list(_recent_pages)


def _host_port(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url
    return parsed.netloc or url


def _ago(started_at: float) -> str:
    sec = max(0, int(time.time() - (started_at or time.time())))
    if sec < 60:
        return f"{sec} 秒前启动"
    if sec < 3600:
        return f"{sec // 60} 分钟前启动"
    return f"{sec // 3600} 小时前启动"


def running_services() -> list:
    """后台任务里正在跑、能认出本机地址的服务 [{url, command, started_at, owner_id}]。"""
    from .. import background
    out, seen = [], set()
    try:
        snaps = background.list_snapshots()
    except Exception:
        return []
    for snap in sorted(snaps, key=lambda s: s.get("started_at") or 0, reverse=True):
        if snap.get("running") is not True:
            continue
        url = background.dev_server_url(snap)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "command": " ".join((snap.get("command") or "").split())[:80],
                    "started_at": snap.get("started_at"), "owner_id": snap.get("owner_id")})
    return out


# ── 小部件 ─────────────────────────────────────────────────────────

class _LoadBar(QWidget):
    """2px 加载条：加载时显示进度，加载完就收起（不占位置的话页面会跳，所以高度常驻、只画不画）。"""

    def __init__(self, parent=None, theme=None):
        super().__init__(parent)
        self._theme = theme
        self.value = 0
        self.loading = False
        self.setFixedHeight(2)

    def set_progress(self, loading: bool, value: int = 0):
        self.loading = loading
        self.value = max(0, min(100, int(value)))
        self.update()

    def paintEvent(self, event):
        if not self.loading:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(self._theme("dlg_info_bg")))
        w = int(self.width() * max(self.value, 8) / 100)
        p.fillRect(0, 0, w, self.height(), qcolor(self._theme("dlg_input_focus")))
        p.end()


class _AgentOverlay(QWidget):
    """「灵犀的鼠标」+ 操作中的柔光边框（设计稿「浏览器：灵犀的鼠标」），浮在网页上面一层。

    - 柔光：3px 实边 + 往里晕开两层光（约 20px、60px），每 2 秒缓慢明暗呼吸。早先是给页面
      框留 2px 边距露出底色的细线，不显眼，而且每次开始 / 停下操作网页都要重新排版一次
    - 指针：靛蓝箭头 + 小标签（「灵犀」/「输入中」/「被挡住了」），从上一个位置滑到这次的点
      （0.25 秒），点击处一圈涟漪。只是给人看：真正的事件由 bridge 直接发给 view，
      **不动系统鼠标**，用户自己的光标不受影响
    - 不接收鼠标（用户照常点网页），不改网页内容；截图工具截的是 view 本身，不含这一层
    """
    MOVE_MS, RIPPLE_MS, BREATH_MS, IDLE_HIDE_MS = 250, 420, 2000, 1500

    def __init__(self, parent, theme):
        super().__init__(parent)
        from PySide6.QtCore import QElapsedTimer, QPointF
        self._theme = theme
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.active = False
        self.cursor_on = False
        self.kind = ""                  # click / type / blocked
        self.label = "灵犀"
        self.pos_from = self.pos_to = QPointF(-100, -100)
        self.ripple_at = None
        self._clock = QElapsedTimer()
        self._clock.start()
        self._moved_at = self._ripple_at = -10_000
        self._fade = 1.0                # 停手后淡出
        self._fading_since = None
        self._tick = QTimer(self)
        self._tick.setInterval(33)      # 动画进行时 ~30 帧；只有柔光呼吸时降到 10 帧
        self._tick.timeout.connect(self._on_tick)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(self.IDLE_HIDE_MS)
        self._idle.timeout.connect(self._start_fade)
        self.hide()

    # ── 对外 ──
    def set_active(self, on):
        if on:
            self.active = True
            self._fading_since = None
            self._fade = 1.0
            self._idle.stop()
            self._run()
        elif self.active:
            self._idle.start()          # 停手 1.5 秒后柔光和指针一起淡出

    def show_cursor(self, x, y, kind, label):
        from PySide6.QtCore import QPointF
        now = self._clock.elapsed()
        if not self.cursor_on:
            # 第一次出现从页面中央滑过去，让人看得见它从哪来
            self.pos_from = QPointF(self.width() / 2, self.height() / 2)
        else:
            self.pos_from = self._cursor_pos(now)
        self.pos_to = QPointF(x, y)
        self._moved_at = now
        self.cursor_on = True
        self.kind = kind
        self.label = {"type": "灵犀 · 输入中", "submit": "灵犀 · 回车",
                      "blocked": "被挡住了"}.get(kind, "灵犀")
        if kind in ("click", "submit"):
            self.ripple_at = QPointF(x, y)
            self._ripple_at = now + self.MOVE_MS     # 滑到了再散开
        self.set_active(True)

    def reset(self):
        self.active = self.cursor_on = False
        self._tick.stop()
        self._idle.stop()
        self.hide()

    # ── 动画 ──
    def _run(self):
        self.show()
        self.raise_()
        if not self._tick.isActive():
            self._tick.start()
        self.update()

    def _start_fade(self):
        self._fading_since = self._clock.elapsed()

    def _on_tick(self):
        now = self._clock.elapsed()
        if self._fading_since is not None:
            self._fade = max(0.0, 1.0 - (now - self._fading_since) / 300)
            if self._fade <= 0:
                self.reset()
                return
        busy = now - self._moved_at < self.MOVE_MS or now - self._ripple_at < self.RIPPLE_MS + self.MOVE_MS
        self._tick.setInterval(33 if busy or self._fading_since is not None else 100)
        self.update()

    BORDER, RADIUS = 3.0, 10.0          # 实边宽度、圆角半径（逻辑像素）

    @staticmethod
    def _falloff(n, scale, border):
        """到边的距离 → 光的亮度（一维），实边以内开始算：两段指数叠加，近处 ~35px 比较
        浓，远处 ~110px 很淡（用户：颜色深一点、往里一点）。scale 是设备像素比。"""
        import numpy as np
        d = np.minimum(np.arange(n), np.arange(n)[::-1]) / scale - border
        glow = 0.42 * np.exp(-d / 13) + 0.12 * np.exp(-d / 42)
        return np.clip(glow, 0, 1).astype(np.float32)

    def _glow_pixmap(self, w, h, accent):
        """网页四周的柔光，按尺寸算一次缓存。
        - 光：横向、纵向各一条衰减曲线，像两盏灯那样叠加（1 - (1-横)(1-竖)），处处平滑、
          角上自然浓一点。早先是一圈圈圆角矩形叠出来的，角上叠出斜向亮缝（用户：「四个角
          漏光」），再早分两组画，接缝处有一道台阶
        - 实边和外轮廓：圆角矩形（按到圆角矩形边的距离算，边缘抗锯齿），圆角外面透明"""
        import numpy as np
        from PySide6.QtGui import QImage, QPixmap
        key = (w, h, accent.name())
        if getattr(self, "_glow_key", None) == key:
            return self._glow
        dpr = max(1.0, self.devicePixelRatioF())
        W, H = max(1, int(w * dpr)), max(1, int(h * dpr))
        fx = self._falloff(W, dpr, self.BORDER)
        fy = self._falloff(H, dpr, self.BORDER)
        glow = 1.0 - (1.0 - fy[:, None]) * (1.0 - fx[None, :])
        # 到圆角矩形边的距离（里面为正）：圆角处是到圆心的距离，其余是到最近一条边的距离
        r = self.RADIUS * dpr
        ys = np.arange(H, dtype=np.float32)[:, None] + 0.5
        xs = np.arange(W, dtype=np.float32)[None, :] + 0.5
        qx = np.maximum(r - xs, xs - (W - r))          # 落在左右圆角带里有多深（>0 才算）
        qy = np.maximum(r - ys, ys - (H - r))
        corner = (qx > 0) & (qy > 0)
        edge = np.minimum(np.minimum(xs, W - xs), np.minimum(ys, H - ys))
        inside = np.where(corner, r - np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2), edge)
        inside = inside / dpr                           # 换回逻辑像素
        border = np.clip(self.BORDER + 0.5 - inside, 0, 1)     # 实边（带半像素抗锯齿）
        mask = np.clip(inside + 0.5, 0, 1)              # 圆角外面透明
        a = np.maximum(border * 0.95, glow) * mask
        img = np.empty((H, W, 4), dtype=np.uint8)
        # ARGB32_Premultiplied 在小端机器上按 B G R A 排；颜色要乘上透明度
        # 很淡的地方只有 8 位精度，会量化出一圈圈色带：加一点抖动把色阶打散
        noise = np.random.default_rng(7).random((H, W), dtype=np.float32)
        alpha = np.floor(255 * a + noise)
        img[..., 3] = alpha.astype(np.uint8)
        k = alpha / 255.0
        img[..., 0] = np.floor(accent.blue() * k + noise).clip(0, 255).astype(np.uint8)
        img[..., 1] = np.floor(accent.green() * k + noise).clip(0, 255).astype(np.uint8)
        img[..., 2] = np.floor(accent.red() * k + noise).clip(0, 255).astype(np.uint8)
        np.minimum(img[..., :3], img[..., 3:4], out=img[..., :3])   # 预乘：颜色不能超过透明度
        qimg = QImage(img.data, W, H, W * 4, QImage.Format_ARGB32_Premultiplied).copy()
        pm = QPixmap.fromImage(qimg)
        pm.setDevicePixelRatio(dpr)
        self._glow_key, self._glow = key, pm
        return pm

    def _cursor_pos(self, now):
        from PySide6.QtCore import QPointF
        t = min(1.0, max(0.0, (now - self._moved_at) / self.MOVE_MS))
        t = 1 - (1 - t) ** 3                    # 先快后慢
        return QPointF(self.pos_from.x() + (self.pos_to.x() - self.pos_from.x()) * t,
                       self.pos_from.y() + (self.pos_to.y() - self.pos_from.y()) * t)

    def paintEvent(self, _e):
        import math
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QColor, QFont, QPainterPath, QPen, QPolygonF
        if not (self.active or self.cursor_on):
            return
        now = self._clock.elapsed()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = qcolor(self._theme("mv_indigo"))
        breath = 0.72 + 0.28 * (0.5 + 0.5 * math.sin(now / self.BREATH_MS * 2 * math.pi))
        w, h = self.width(), self.height()
        # 柔光按尺寸画一次缓存，呼吸只调透明度（每帧重画 60 圈不划算）
        p.setOpacity(breath * self._fade)
        p.drawPixmap(0, 0, self._glow_pixmap(w, h, accent))
        p.setOpacity(1.0)
        if not self.cursor_on:
            p.end()
            return
        pos = self._cursor_pos(now)
        color = qcolor(self._theme("mv_bad")) if self.kind == "blocked" else accent
        # 涟漪：滑到之后从落点散开
        if self.ripple_at is not None and 0 <= now - self._ripple_at < self.RIPPLE_MS:
            t = (now - self._ripple_at) / self.RIPPLE_MS
            c = QColor(accent)
            c.setAlphaF(0.55 * (1 - t) * self._fade)
            p.setPen(QPen(c, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self.ripple_at, 6 + 16 * t, 6 + 16 * t)
            c.setAlphaF(0.25 * (1 - t) * self._fade)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(self.ripple_at, 6, 6)
        p.setOpacity(self._fade)
        # 箭头（设计稿那个形状，尖端对准落点）
        pts = [(0, 0), (0, 19), (5, 14.5), (8.5, 22), (12, 20.5), (8.5, 13), (15.5, 13)]
        poly = QPolygonF([QPointF(pos.x() + x, pos.y() + y) for x, y in pts])
        p.setPen(QPen(qcolor(self._theme("mv_view_bg")), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(color)
        p.drawPolygon(poly)
        # 小标签
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(self.label)
        pill = QRectF(pos.x() + 16, pos.y() + 16, tw + 16, 20)
        if pill.right() > w - 4:                    # 贴右边时标签放到左边
            pill.moveRight(pos.x() - 4)
        path = QPainterPath()
        path.addRoundedRect(pill, 10, 10)
        p.setPen(Qt.NoPen)
        p.fillPath(path, color)
        p.setPen(qcolor(self._theme("mv_view_bg")))
        p.drawText(pill, Qt.AlignCenter, self.label)
        p.end()


class _StatusCard(QFrame):
    """模型正在操作页面时浮在页面底部的卡片：「灵犀正在点击」+ 第几步 + 停止。"""

    stop_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("browserStatusCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        self.dot = QLabel(self)
        self.dot.setObjectName("browserStatusDot")
        self.dot.setFixedSize(16, 16)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(self)
        self.title.setObjectName("browserStatusTitle")
        self.sub = QLabel(self)
        self.sub.setObjectName("browserStatusSub")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.stop_btn = QPushButton("停止", self)
        self.stop_btn.setObjectName("browserStatusStop")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setFixedHeight(30)
        self.stop_btn.setToolTip("停下模型这一轮（页面留着，你可以接手）")
        self.stop_btn.clicked.connect(self.stop_clicked)
        lay.addWidget(self.stop_btn, 0, Qt.AlignVCenter)


class _EmptyState(QWidget):
    """还没打开网页：本机正在运行的服务 + 最近打开。"""

    open_url = Signal(str, object)        # (url, 服务所属的 owner_id 或 None)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("browserEmpty")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(18, 18, 18, 14)
        self._lay.setSpacing(10)
        self.service_buttons: list = []
        self.recent_buttons: list = []

    def refresh(self, services, recents):
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()              # deleteLater 要等下一轮事件循环：先藏起来，免得新旧两份叠在一起
                w.deleteLater()
        self.service_buttons, self.recent_buttons = [], []
        self._lay.addWidget(self._heading("本机正在运行的服务"))
        if services:
            for i, svc in enumerate(services):
                self._lay.addWidget(self._service_row(svc, primary=(i == 0)))
        else:
            note = QLabel("还没有。让灵犀在后台起一个 dev server（比如 npm run dev），会出现在这里。", self)
            note.setObjectName("browserEmptyNote")
            note.setWordWrap(True)
            self._lay.addWidget(note)
        if recents:
            self._lay.addSpacing(8)
            self._lay.addWidget(self._heading("最近打开"))
            for rec in recents:
                self._lay.addWidget(self._recent_row(rec))
        self._lay.addStretch(1)
        foot = QLabel("模型打开网页时，这里会自动切过来。", self)
        foot.setObjectName("browserEmptyNote")
        self._lay.addWidget(foot)

    def _heading(self, text):
        lb = QLabel(text, self)
        lb.setObjectName("browserEmptyHeading")
        return lb

    def _service_row(self, svc, *, primary):
        row = QFrame(self)
        row.setObjectName("browserServiceRow")
        row.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(row)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(12)
        dot = QLabel(row)
        dot.setObjectName("browserServiceDot")
        dot.setFixedSize(8, 8)
        h.addWidget(dot, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(2)
        addr = QLabel(_host_port(svc["url"]), row)
        addr.setObjectName("browserServiceAddr")
        meta = QLabel(f"{svc['command']} · {_ago(svc.get('started_at'))}", row)
        meta.setObjectName("browserServiceMeta")
        col.addWidget(addr)
        col.addWidget(meta)
        h.addLayout(col, 1)
        btn = QPushButton("打开", row)
        btn.setObjectName("browserOpenPrimary" if primary else "browserOpenSecondary")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(30)
        btn.clicked.connect(lambda _=False, u=svc["url"], o=svc.get("owner_id"): self.open_url.emit(u, o))
        h.addWidget(btn, 0, Qt.AlignVCenter)
        self.service_buttons.append(btn)
        return row

    def _recent_row(self, rec):
        btn = QPushButton(self)
        btn.setObjectName("browserRecentRow")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(rec["url"])
        lay = QVBoxLayout(btn)
        lay.setContentsMargins(4, 8, 4, 8)
        lay.setSpacing(2)
        t = QLabel(rec["title"], btn)
        t.setObjectName("browserRecentTitle")
        u = QLabel(_host_port(rec["url"]) + urllib.parse.urlparse(rec["url"]).path, btn)
        u.setObjectName("browserRecentUrl")
        for lb in (t, u):
            lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(lb)
        btn.setMinimumHeight(52)
        btn.clicked.connect(lambda _=False, url=rec["url"]: self.open_url.emit(url, None))
        self.recent_buttons.append(btn)
        return btn


class _ErrorPage(QWidget):
    """连不上：我们自己的错误页（替代 Chromium 的英文页）。"""

    reload_clicked = Signal()
    background_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("browserError")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(2)
        box = QWidget(self)
        box.setMinimumWidth(260)
        box.setMaximumWidth(340)
        from PySide6.QtWidgets import QSizePolicy
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        self.tile = QLabel(box)
        self.tile.setObjectName("browserErrorTile")
        self.tile.setFixedSize(44, 44)
        self.tile.setAlignment(Qt.AlignCenter)
        col.addWidget(self.tile)
        self.title = QLabel(box)
        self.title.setObjectName("browserErrorTitle")
        self.title.setWordWrap(True)
        col.addWidget(self.title)
        self.body = QLabel(box)
        self.body.setObjectName("browserErrorBody")
        self.body.setWordWrap(True)
        col.addWidget(self.body)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.reload_btn = QPushButton("重新加载", box)
        self.reload_btn.setObjectName("browserOpenPrimary")
        self.reload_btn.setCursor(Qt.PointingHandCursor)
        self.reload_btn.setFixedHeight(34)
        self.reload_btn.clicked.connect(self.reload_clicked)
        self.background_btn = QPushButton("去后台看看", box)
        self.background_btn.setObjectName("browserOpenSecondary")
        self.background_btn.setCursor(Qt.PointingHandCursor)
        self.background_btn.setFixedHeight(34)
        self.background_btn.clicked.connect(self.background_clicked)
        row.addWidget(self.reload_btn)
        row.addWidget(self.background_btn)
        row.addStretch(1)
        col.addLayout(row)
        self.code = QLabel(box)
        self.code.setObjectName("browserErrorCode")
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.code)
        hl = QHBoxLayout()
        hl.addStretch(1)
        hl.addWidget(box, 10)
        hl.addStretch(1)
        outer.addLayout(hl)
        outer.addStretch(3)

    def set_error(self, url: str, error: str, code: int):
        host = _host_port(url)
        self.title.setText(f"连不上 {host}" if host else "页面没能打开")
        local = security.is_localhost(url) if url else False
        text = (error or "").upper()
        if "REFUSED" in text:
            body = "这个地址上没有服务在运行。" + ("如果它是灵犀在后台启动的，可能已经停了。" if local else "")
        elif "NAME_NOT_RESOLVED" in text or "ADDRESS_UNREACHABLE" in text:
            body = "找不到这个网址的服务器，检查一下拼写或网络。"
        elif "TIMED_OUT" in text:
            body = "等了太久没有响应。服务可能卡住了，或者网络不通。"
        elif "CERT" in text or "SSL" in text:
            body = "这个网站的安全证书有问题，没有继续打开。"
        else:
            body = "页面没能打开。"
        self.body.setText(body)
        self.background_btn.setVisible(local)
        self.code.setText(error or (f"错误 {code}" if code else ""))


# ── 面板 ───────────────────────────────────────────────────────────

class BrowserView(QWidget):
    """文件面板浏览器标签的内容。构造即创建页面（第一次点开标签时）。"""

    closed = Signal()                       # 嵌入的 view 被外部销毁（模型 browser_close）→ 面板收标签
    tab_state_changed = Signal()            # 标题 / 图标 / 操作中 / 出错 变了 → 标签刷新
    stop_requested = Signal()               # 状态卡「停止」
    background_requested = Signal()         # 错误页「去后台看看」
    close_requested = Signal()              # 「更多」里的关闭浏览器

    def __init__(self, parent=None, theme_lookup=None, tab_id=None):
        super().__init__(parent)
        self.setObjectName("browserView")
        # 这个视图对应浏览器的哪个标签（每个网页一个面板标签）；不给就是当前标签
        if tab_id is None:
            tab_id = page.get_browser().current_id
        self.tab_id = tab_id
        self._theme = theme_lookup or (lambda key: "#64748b")
        self._embedded = None
        self._wired_page = None
        self._releasing = False
        self._activity_text = ""
        self._user_typing = False
        self.model_active = False
        self._steps = 0
        self.failed = False
        self._error = ("", "", 0)              # (url, errorString, code)
        self._loading = False
        self._devtools_view = None             # 停靠在页面下方的开发者工具（懒建）
        self._devtools_popped = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 工具栏：后退 / 前进 / 刷新 · 地址胶囊 · F12 · 更多
        bar = QWidget(self)
        bar.setObjectName("browserBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(4)
        self.back_btn = self._tool_btn("后退", lambda: self._page_action("back"))
        self.forward_btn = self._tool_btn("前进", lambda: self._page_action("forward"))
        self.reload_btn = self._tool_btn("刷新", self._reload_or_stop)
        for b in (self.back_btn, self.forward_btn, self.reload_btn):
            row.addWidget(b)
        self.pill = QFrame(bar)
        self.pill.setObjectName("browserPill")
        self.pill.setAttribute(Qt.WA_StyledBackground, True)
        pl = QHBoxLayout(self.pill)
        pl.setContentsMargins(8, 0, 6, 0)
        pl.setSpacing(8)
        self.chip = QLabel(self.pill)
        self.chip.setObjectName("browserChip")
        self.chip.setVisible(False)
        pl.addWidget(self.chip, 0, Qt.AlignVCenter)
        self.address = QLineEdit(self.pill)
        self.address.setObjectName("browserAddress")
        self.address.setPlaceholderText("输入网址，或从下面选一个本机服务")
        self.address.setFont(mono_font(12))
        self.address.setFrame(False)
        self.address.returnPressed.connect(self._navigate)
        self.address.textEdited.connect(self._on_text_edited)
        self.address.installEventFilter(self)
        pl.addWidget(self.address, 1)
        self.pill.setFixedHeight(34)
        row.addWidget(self.pill, 1)
        self.devtools_btn = self._tool_btn("开发者工具（F12）", self._toggle_devtools)
        self.devtools_btn.setCheckable(True)
        self.more_btn = self._tool_btn("更多", self._show_more_menu)
        row.addWidget(self.devtools_btn)
        row.addWidget(self.more_btn)
        lay.addWidget(bar)

        self.load_bar = _LoadBar(self, self._theme)
        lay.addWidget(self.load_bar)

        # 一行临时提示（网址不合法之类），平时收起
        self.badge = QLabel(self)
        self.badge.setObjectName("browserFlash")
        self.badge.setWordWrap(True)
        self.badge.setVisible(False)
        lay.addWidget(self.badge)

        # 内容区：空白 / 页面 / 连不上
        self.content = QStackedWidget(self)
        self.content.setObjectName("browserContent")
        self.empty = _EmptyState(self.content)
        self.empty.open_url.connect(self._open_from_empty)
        self.page_frame = QFrame(self.content)
        self.page_frame.setObjectName("browserPageFrame")
        self.page_frame.setAttribute(Qt.WA_StyledBackground, True)
        pf = QVBoxLayout(self.page_frame)
        pf.setContentsMargins(0, 0, 0, 0)
        self.page_split = QSplitter(Qt.Vertical, self.page_frame)
        self.page_split.setObjectName("browserSplit")
        self.page_split.setChildrenCollapsible(False)
        self.page_split.setHandleWidth(8)
        pf.addWidget(self.page_split)
        self.devtools_panel = self._build_devtools_panel()
        self.devtools_panel.setVisible(False)
        self.error_page = _ErrorPage(self.content)
        self.error_page.reload_clicked.connect(lambda: self._page_action("reload"))
        self.error_page.background_clicked.connect(self.background_requested)
        self.content.addWidget(self.empty)
        self.content.addWidget(self.page_frame)
        self.content.addWidget(self.error_page)
        lay.addWidget(self.content, 1)

        # 「灵犀的鼠标」+ 操作中的柔光：浮在网页上面一层（状态卡再在它上面）
        self.overlay = _AgentOverlay(self.page_frame, self._theme)
        self.page_split.splitterMoved.connect(lambda *_: self._place_overlay())
        self.status_card = _StatusCard(self.page_frame)
        self.status_card.setVisible(False)
        self.status_card.stop_clicked.connect(self._on_stop_clicked)
        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.setInterval(_ACTIVE_LINGER_MS)
        self._linger.timeout.connect(self._end_active)
        self._service_timer = QTimer(self)
        self._service_timer.setInterval(2000)
        self._service_timer.timeout.connect(self._refresh_empty)

        self._embed(self._tab().view)
        get_bridge().activity.connect(self._on_activity)
        get_bridge().cursor_event.connect(self._on_cursor_event)
        # F12 只在本面板（含子控件）有焦点时生效，不抢整窗口的 F12（那是「调试」标签）
        QShortcut(QKeySequence("F12"), self,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut).activated.connect(
                      self._toggle_devtools)
        self.apply_theme()
        self._update_mode()

    # ── 构造小件 ──
    def _tool_btn(self, tip, slot):
        b = QPushButton(self)
        b.setObjectName("browserTool")
        b.setFixedSize(32, 32)
        b.setIconSize(QSize(15, 15))
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.clicked.connect(lambda _=False: slot())
        return b

    def _build_devtools_panel(self):
        panel = QWidget(self.page_split)
        panel.setObjectName("browserDevtools")
        col = QVBoxLayout(panel)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        head = QWidget(panel)
        head.setObjectName("browserDevtoolsHead")
        head.setAttribute(Qt.WA_StyledBackground, True)
        head.setFixedHeight(34)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 0, 4, 0)
        hl.setSpacing(4)
        title = QLabel("开发者工具", head)
        title.setObjectName("browserDevtoolsTitle")
        hl.addWidget(title)
        hl.addStretch(1)
        self.devtools_popout_btn = self._tool_btn("弹出为单独窗口", self._pop_out_devtools)
        self.devtools_close_btn = self._tool_btn("关闭开发者工具", self._hide_devtools)
        for b in (self.devtools_popout_btn, self.devtools_close_btn):
            b.setFixedSize(28, 28)
            b.setIconSize(QSize(13, 13))
            b.setParent(head)
            hl.addWidget(b)
        col.addWidget(head)
        self._devtools_box = QVBoxLayout()
        self._devtools_box.setContentsMargins(0, 0, 0, 0)
        col.addLayout(self._devtools_box, 1)
        return panel

    # ── 嵌入 / 释放 ──────────────────────────────────────────────
    def _embed(self, view):
        self._embedded = view
        # 网页右键菜单换成中文、跟随主题的（设计稿「右键菜单 · 浏览器网页」），不用 Chromium 的英文菜单
        view.setContextMenuPolicy(Qt.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_page_menu)
        self.page_split.insertWidget(0, view)
        self.page_split.setStretchFactor(0, 3)
        self.page_split.addWidget(self.devtools_panel)
        self.page_split.setStretchFactor(1, 2)
        view.destroyed.connect(self._on_view_destroyed)
        self._unwire_page()
        p = self._tab().page
        p.urlChanged.connect(self._on_url_changed)
        p.titleChanged.connect(self._on_title_changed)
        p.iconChanged.connect(self._on_icon_changed)
        p.loadStarted.connect(self._on_load_started)
        p.loadProgress.connect(self._on_load_progress)
        p.loadFinished.connect(self._on_load_finished)
        p.loadingChanged.connect(self._on_loading_changed)
        self._wired_page = p
        self._set_address(p.url().toString())
        self.status_card.raise_()

    def _unwire_page(self):
        p = self._wired_page
        if p is None or not shiboken6.isValid(p):
            return
        for sig, slot in ((p.urlChanged, self._on_url_changed),
                          (p.titleChanged, self._on_title_changed),
                          (p.iconChanged, self._on_icon_changed),
                          (p.loadStarted, self._on_load_started),
                          (p.loadProgress, self._on_load_progress),
                          (p.loadFinished, self._on_load_finished),
                          (p.loadingChanged, self._on_loading_changed)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _tab(self):
        b = page.get_browser()
        return b.tab(self.tab_id) or b.current

    def _ensure_embedded(self):
        if self._embedded is None or not shiboken6.isValid(self._embedded):
            b = page._browser
            if b is None or b.tab(self.tab_id) is None:
                return            # 这个标签已经关了：面板会随 tab_closed 拆掉本视图
            self._embed(b.tab(self.tab_id).view)

    def _on_view_destroyed(self):
        self._embedded = None
        if not self._releasing:
            self.closed.emit()

    def release(self):
        """关这个网页标签：拆本视图，再让浏览器关掉对应的标签（最后一个等于关浏览器，
        profile 常驻不销毁，§11.6）。"""
        self.detach()
        page.close_tab(self.tab_id)

    def detach(self):
        """只拆本视图（停计时器、摘开发者工具、断页面信号），不动浏览器那边的标签。"""
        if self._releasing:
            return
        self._releasing = True
        self._service_timer.stop()
        self._linger.stop()
        self.overlay.reset()
        self._drop_devtools()
        if self._embedded is not None and shiboken6.isValid(self._embedded):
            try:
                self._embedded.destroyed.disconnect(self._on_view_destroyed)
            except (RuntimeError, TypeError):
                pass
        self._unwire_page()
        self._embedded = None

    # ── 状态 ────────────────────────────────────────────────────
    def page_url(self) -> str:
        p = self._wired_page
        if p is None or not shiboken6.isValid(p):
            return ""
        return p.url().toString()

    def tab_title(self) -> str:
        """标签上显示的文字：出错「无法访问」，空白「新标签页」，否则页面标题（没有就用主机名）。"""
        if self.failed:
            return "无法访问"
        url = self.page_url()
        if not url or url == "about:blank":
            return "新标签页"
        p = self._wired_page
        title = p.title() if p is not None and shiboken6.isValid(p) else ""
        if title and title != url and not title.startswith(("http://", "https://")):
            return title
        return _host_port(url)

    def tab_icon(self) -> QIcon:
        if self.failed:
            return _icon("error", self._theme("dlg_err_fg"), 14)
        p = self._wired_page
        if p is not None and shiboken6.isValid(p) and not p.icon().isNull():
            return p.icon()
        return _icon("globe", self._theme("dlg_nav_active_text"), 14)

    def _is_blank(self) -> bool:
        url = self.page_url()
        return not url or url == "about:blank"

    def _update_mode(self):
        if self.failed:
            self.content.setCurrentWidget(self.error_page)
        elif self._is_blank() and not self._loading:
            self.content.setCurrentWidget(self.empty)
            self._refresh_empty()
        else:
            self.content.setCurrentWidget(self.page_frame)
        if self.content.currentWidget() is self.empty and self.isVisible():
            self._service_timer.start()
        else:
            self._service_timer.stop()
        self._update_nav_buttons()
        self._update_chip()
        self.tab_state_changed.emit()

    def _refresh_empty(self):
        self.empty.refresh(running_services(), recent_pages())

    def _update_nav_buttons(self):
        p = self._wired_page
        ok = p is not None and shiboken6.isValid(p)
        hist = p.history() if ok else None
        self.back_btn.setEnabled(bool(hist and hist.canGoBack()))
        self.forward_btn.setEnabled(bool(hist and hist.canGoForward()))
        self.reload_btn.setEnabled(ok and not self._is_blank())
        self._paint_reload_icon()

    def _update_chip(self):
        url = self.page_url()
        if self._is_blank() or security.validate_url(url):
            self.chip.setVisible(False)
            return
        from .. import session as _session
        # 正在驱动浏览器的会话优先（它的白名单决定模型这一步问不问）；没在跑就看前台会话
        driver = get_bridge().driver_session()
        if driver is not None and getattr(driver, "is_generating", False):
            sess = driver
        else:
            sess = _session.get_active()
        allow = getattr(sess, "browser_origin_allowlist", set()) if sess is not None else set()
        kind = security.origin_kind(url, allow)
        local = security.is_localhost(url)
        if kind == security.ORIGIN_TRUSTED:
            text, style = ("本机 · 已信任" if local else "已信任"), "ok"
        elif kind == security.ORIGIN_LOCALHOST:
            text, style = "本机", "plain"
        else:
            text, style = "外部网站", "warn"
        self.chip.setText(text)
        self.chip.setProperty("tone", style)
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)
        self.chip.setToolTip({"ok": "来源已被这个会话信任：模型在这里的普通操作不再逐个问你",
                              "plain": "本机地址：模型在这里输入、点击仍按权限模式询问",
                              "warn": "外部网站：模型的操作都会先问你"}[style])
        self.chip.setVisible(True)

    # ── 页面信号 ──
    def _on_url_changed(self, qurl):
        # 用户正在输入时不回写地址栏（焦点丢失/异步 urlChanged 都不该打断输入）
        if not self._user_typing and not self.address.hasFocus():
            self._set_address(qurl.toString())
        self._update_mode()

    def _on_title_changed(self, _title):
        self.tab_state_changed.emit()

    def _on_icon_changed(self, _icon):
        self.tab_state_changed.emit()

    def _on_load_started(self):
        self._loading = True
        self.failed = False
        self.load_bar.set_progress(True, 0)
        self._update_mode()

    def _on_load_progress(self, value):
        self.load_bar.set_progress(self._loading, value)

    def _on_load_finished(self, ok):
        self._loading = False
        self.load_bar.set_progress(False)
        if ok and not self.failed:
            p = self._wired_page
            if p is not None and shiboken6.isValid(p):
                remember_page(p.title(), p.url().toString())
        self._update_mode()

    def _on_loading_changed(self, info):
        from PySide6.QtWebEngineCore import QWebEngineLoadingInfo
        status = info.status()
        if status == QWebEngineLoadingInfo.LoadStatus.LoadStartedStatus:
            self.failed = False
        elif status == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
            err = info.errorString() or ""
            if "ERR_ABORTED" in err.upper():     # 被新导航取代 / 用户停止：不算连不上
                return
            self.failed = True
            self._error = (info.url().toString(), err, int(info.errorCode()))
            self.error_page.set_error(*self._error)
            self._update_mode()

    # ── 导航 ────────────────────────────────────────────────────
    def navigate(self, url: str):
        """面板入口（background_panel 按钮 / 回复链接 / 空白页）调：校验后导航。"""
        url = (url or "").strip()
        err = security.validate_url(url)
        if err:
            self._flash(err)
            return
        self._ensure_embedded()
        self._user_typing = False
        self._set_address(url)
        self.failed = False
        was_loading = self._loading
        self._loading = True
        self._update_mode()
        # 走缓存闸门：清缓存在途时排队等清完（和清理重叠的导航会卡死）；关过浏览器先清一次
        from ..browser import page as page_mod
        page_mod.load_url(self._embedded.page(), QUrl(url), page_loading=was_loading)

    def _navigate(self):
        self._user_typing = False
        text = self.address.text().strip()
        if text and "://" not in text:
            text = "http://" + text                   # 「localhost:5173」这种直接补上协议
        self.navigate(text)

    def _on_text_edited(self, _text):
        self._user_typing = True

    def _display_url(self, url):
        return "" if url == "about:blank" else url

    def _set_address(self, url):
        self.address.setText(self._display_url(url))
        if not self.address.hasFocus():
            self.address.setCursorPosition(0)

    def _page_action(self, which):
        p = self._wired_page
        if p is None or not shiboken6.isValid(p):
            return
        if which == "back":
            p.triggerAction(p.WebAction.Back)
        elif which == "forward":
            p.triggerAction(p.WebAction.Forward)
        elif which == "reload":
            self.failed = False
            from ..browser import page as page_mod      # 清缓存在途时等清完再刷新（同 navigate）
            page_mod.when_cache_idle(
                lambda: shiboken6.isValid(p) and p.triggerAction(p.WebAction.Reload))

    def _reload_or_stop(self):
        p = self._wired_page
        if p is None or not shiboken6.isValid(p):
            return
        if self._loading:
            p.triggerAction(p.WebAction.Stop)
        else:
            self._page_action("reload")

    def _open_from_empty(self, url, owner_id):
        """空白页点「打开」：人主动打开灵犀自己起的服务，来源加进那个会话的白名单（同后台面板）。"""
        from .. import session as _session
        if owner_id is not None:
            sess = next((s for s in _session.live_sessions()
                         if getattr(s, "background_owner_id", None) == owner_id), None)
            sess = sess or _session.get_active()
            origin = security.parse_origin(url)
            if origin and sess is not None:
                sess.browser_origin_allowlist.add(origin)
        self.navigate(url)

    def flash(self, text):
        self._flash(text)

    def _flash(self, text):
        self.badge.setText(text)
        self.badge.setVisible(True)
        # receiver=self：窗口销毁后回调落在对象上下文里，不会访问已删除控件
        QTimer.singleShot(3000, self, self._clear_flash)

    def _clear_flash(self):
        self.badge.setVisible(False)

    def _show_more_menu(self):
        menu = QMenu(self)
        url = self.page_url()
        has_page = bool(url) and not security.validate_url(url)
        a_sys = menu.addAction("在系统浏览器中打开")
        a_sys.setEnabled(has_page)
        a_copy = menu.addAction("复制网址")
        a_copy.setEnabled(has_page)
        menu.addSeparator()
        a_close = menu.addAction("关闭浏览器")
        chosen = menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))
        if chosen is a_sys:
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(url))
        elif chosen is a_copy:
            QApplication.clipboard().setText(url)
        elif chosen is a_close:
            self.close_requested.emit()

    # ── 模型正在操作 ────────────────────────────────────────────
    def _on_activity(self, text):
        # 桥的动作作用于当前标签：别的网页标签不亮「操作中」
        if text and page.current_tab_id() != self.tab_id:
            return
        self._activity_text = text
        if text:
            self._linger.stop()
            if not self.model_active:
                self.model_active = True
                self._steps = 0
                self._set_outline(True)
                self.tab_state_changed.emit()
            self._steps += 1
            self.status_card.title.setText(f"灵犀正在{text}")
            self.status_card.sub.setText(f"第 {self._steps} 步 · 点「停止」可以接手")
            self.status_card.setVisible(True)
            self._place_status_card()
        else:
            # 两次动作之间的空档不收：一个工具调用里会连着发好几次桥请求
            self._linger.start()

    def _end_active(self):
        if self._activity_text:
            return
        self.model_active = False
        self.status_card.setVisible(False)
        self._set_outline(False)
        self.tab_state_changed.emit()

    def _set_outline(self, on):
        """模型操作中：网页四周的柔光（浮层画，不再给页面框留边距——那样每次开始 / 停下
        网页都要重新排版一次）。"""
        self._place_overlay()
        self.overlay.set_active(on)
        self.status_card.raise_()

    def _place_overlay(self):
        """浮层盖住网页那一块（不盖开发者工具）。"""
        v = self._embedded
        if v is None or not shiboken6.isValid(v) or not v.isVisible():
            return
        from PySide6.QtCore import QPoint, QRect
        self.overlay.setGeometry(QRect(v.mapTo(self.page_frame, QPoint(0, 0)), v.size()))

    def _on_cursor_event(self, tab_id, x, y, kind, label):
        """bridge 报的「灵犀的鼠标」动作（页面 CSS 坐标）：只认自己这个标签。"""
        if tab_id != self.tab_id or self._releasing:
            return
        if label and kind != "blocked":
            self.status_card.title.setText(f"灵犀正在{label}")
        from .. import config as _cfg
        if not _cfg.AGENT_CURSOR:
            return
        tab = page._browser.tab(self.tab_id) if page._browser is not None else None
        zoom = (tab.page.zoomFactor() if tab is not None and shiboken6.isValid(tab.page) else 1.0) or 1.0
        self._place_overlay()
        self.overlay.show_cursor(x * zoom, y * zoom, kind, label)
        self.status_card.raise_()

    def _place_status_card(self):
        f = self.page_frame
        w = max(200, f.width() - 32)
        h = self.status_card.sizeHint().height()
        self.status_card.setGeometry(16, max(0, f.height() - h - 16), w, h)
        self.status_card.raise_()

    def _on_stop_clicked(self):
        self.status_card.title.setText("正在停止…")
        self.stop_requested.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self, self._place_overlay)
        if self.status_card.isVisible():
            QTimer.singleShot(0, self, self._place_status_card)

    def showEvent(self, event):
        super().showEvent(event)
        self._update_mode()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._service_timer.stop()

    def eventFilter(self, obj, event):
        if obj is self.address and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            focus = event.type() == QEvent.FocusIn
            self.pill.setProperty("focus", "true" if focus else "false")
            self.pill.style().unpolish(self.pill)
            self.pill.style().polish(self.pill)
            if not focus and not self._user_typing:
                self._set_address(self.page_url())
        return super().eventFilter(obj, event)

    # ── 网页右键菜单 ────────────────────────────────────────────
    def build_page_menu(self, req):
        """按右键落点拼菜单：可编辑区域给编辑项，链接给链接项，选中文字给复制 / 发到输入框，
        空白处给后退 / 前进 / 重新加载 / 复制地址 / 系统浏览器；最后都有「检查元素」。"""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWebEngineCore import QWebEngineContextMenuRequest
        from . import context_menus as cm
        p = self._wired_page
        if p is None or not shiboken6.isValid(p):
            return None
        wa = p.WebAction
        menu = cm.make_menu(self)
        link = req.linkUrl().toString() if req is not None and req.linkUrl().isValid() else ""
        sel = (req.selectedText() if req is not None else "") or ""
        host = cm._host()

        def act(a):
            return lambda: p.triggerAction(a)

        if req is not None and req.isContentEditable():
            flags = req.editFlags()
            ef = QWebEngineContextMenuRequest.EditFlag
            cm.add_item(menu, "撤销", act(wa.Undo), shortcut="Ctrl+Z", enabled=bool(flags & ef.CanUndo))
            cm.add_item(menu, "重做", act(wa.Redo), shortcut="Ctrl+Y", enabled=bool(flags & ef.CanRedo))
            menu.addSeparator()
            cm.add_item(menu, "剪切", act(wa.Cut), shortcut="Ctrl+X", enabled=bool(flags & ef.CanCut))
            cm.add_item(menu, "复制", act(wa.Copy), shortcut="Ctrl+C", enabled=bool(flags & ef.CanCopy))
            cm.add_item(menu, "粘贴", act(wa.Paste), shortcut="Ctrl+V", enabled=bool(flags & ef.CanPaste))
            menu.addSeparator()
            cm.add_item(menu, "全选", act(wa.SelectAll), shortcut="Ctrl+A",
                        enabled=bool(flags & ef.CanSelectAll))
        elif link:
            web = not security.validate_url(link)
            cm.add_item(menu, "打开链接", lambda: self.navigate(link), enabled=web)
            cm.add_item(menu, "复制链接地址", lambda: QApplication.clipboard().setText(link))
            cm.add_item(menu, "在系统浏览器中打开链接", lambda: QDesktopServices.openUrl(QUrl(link)),
                        enabled=web)
            if sel:
                menu.addSeparator()
                cm.add_item(menu, "复制", act(wa.Copy), shortcut="Ctrl+C")
                cm.add_item(menu, "把选中文字发到输入框",
                            lambda: host.context_menu_quote(sel), enabled=host is not None)
        elif sel:
            cm.add_item(menu, "复制", act(wa.Copy), shortcut="Ctrl+C")
            cm.add_item(menu, "把选中文字发到输入框",
                        lambda: host.context_menu_quote(sel), enabled=host is not None)
        else:
            hist = p.history()
            url = self.page_url()
            has_page = bool(url) and not security.validate_url(url)
            cm.add_item(menu, "后退", act(wa.Back), shortcut="Alt+←", enabled=hist.canGoBack())
            cm.add_item(menu, "前进", act(wa.Forward), shortcut="Alt+→", enabled=hist.canGoForward())
            cm.add_item(menu, "重新加载", lambda: self._page_action("reload"), shortcut="F5",
                        enabled=has_page)
            menu.addSeparator()
            cm.add_item(menu, "复制页面地址", lambda: QApplication.clipboard().setText(url),
                        enabled=has_page)
            cm.add_item(menu, "在系统浏览器中打开", lambda: QDesktopServices.openUrl(QUrl(url)),
                        enabled=has_page)
        menu.addSeparator()
        cm.add_item(menu, "检查元素", self._inspect_element, shortcut="F12")
        return menu

    def _on_page_menu(self, pos):
        view = self._embedded
        if view is None or not shiboken6.isValid(view):
            return
        menu = self.build_page_menu(view.lastContextMenuRequest())
        if menu is None:
            return
        menu.exec(view.mapToGlobal(pos))
        menu.deleteLater()

    def _inspect_element(self):
        """「检查元素」：开停靠的开发者工具并定位到右键点的那个元素。"""
        self._open_devtools()
        p = self._wired_page
        if p is not None and shiboken6.isValid(p):
            p.triggerAction(p.WebAction.InspectElement)

    # ── 开发者工具（停靠在页面下方）──────────────────────────────
    def _toggle_devtools(self):
        if self.devtools_panel.isVisible():
            self._hide_devtools()
        else:
            self._open_devtools()

    def _open_devtools(self):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        if self._devtools_view is None or not shiboken6.isValid(self._devtools_view):
            self._devtools_view = QWebEngineView(self.devtools_panel)
            self._devtools_box.addWidget(self._devtools_view)
        self._tab().page.setDevToolsPage(self._devtools_view.page())
        self._devtools_popped = False
        self.devtools_panel.setVisible(True)
        total = max(300, self.page_split.height())
        self.page_split.setSizes([int(total * 0.58), int(total * 0.42)])
        self.devtools_btn.setChecked(True)

    def _hide_devtools(self):
        self.devtools_panel.setVisible(False)
        self.devtools_btn.setChecked(False)

    def _pop_out_devtools(self):
        """弹成单独窗口：停靠的那份收起，用 Browser.open_devtools 开独立窗口（同一个页面只能挂一份）。"""
        self._hide_devtools()
        self._devtools_popped = True
        page.set_current_tab(self.tab_id)        # 独立窗口挂在当前标签上
        page.get_browser().open_devtools()

    def _drop_devtools(self):
        dt = self._devtools_view
        self._devtools_view = None
        if dt is not None and shiboken6.isValid(dt):
            try:
                dt.page().setInspectedPage(None)
            except (RuntimeError, TypeError):
                pass
            dt.setParent(None)
            dt.deleteLater()

    # ── 主题 ─────────────────────────────────────────────────────
    def apply_theme(self):
        t = self._theme
        sec, text = t("mv_text_sec"), t("dlg_title")
        self.setStyleSheet(
            f"QWidget#browserView {{ background: transparent; }}"
            f"QWidget#browserBar {{ background: transparent; }}"
            f"QPushButton#browserTool {{ background: transparent; border: none; border-radius: 8px; }}"
            f"QPushButton#browserTool:hover {{ background: {t('dlg_hover_bg')}; }}"
            f"QPushButton#browserTool:checked {{ background: {t('dlg_nav_active_bg')}; }}"
            f"QFrame#browserPill {{ background: {t('dlg_hover_bg')}; border: 1px solid transparent;"
            f" border-radius: 10px; }}"
            f"QFrame#browserPill[focus=\"true\"] {{ background: {t('dlg_input_bg')};"
            f" border: 1px solid {t('dlg_input_focus')}; }}"
            f"QLineEdit#browserAddress {{ background: transparent; border: none; color: {text};"
            f" font-family: {MONO_CSS}; font-size: 12px; padding: 0; }}"
            f"QLabel#browserChip {{ font-family: {UI_CSS}; font-size: 11px; border-radius: 6px;"
            f" padding: 1px 6px; color: {sec}; background: {t('dlg_bg')};"
            f" border: 1px solid {t('dlg_secondary_border')}; }}"
            f"QLabel#browserChip[tone=\"ok\"] {{ color: {t('dlg_ok_fg')}; background: {t('dlg_ok_bg')};"
            f" border: 1px solid {t('dlg_ok_border')}; }}"
            f"QLabel#browserChip[tone=\"warn\"] {{ color: {t('dlg_warn_fg')}; background: {t('dlg_warn_bg')};"
            f" border: 1px solid {t('dlg_warn_border')}; }}"
            f"QLabel#browserFlash {{ background: {t('dlg_warn_bg')}; color: {t('dlg_warn_text')};"
            f" border: 1px solid {t('dlg_warn_border')}; border-radius: 8px; padding: 6px 10px;"
            f" font-size: 12px; margin: 0 0 6px 0; }}"
            f"QStackedWidget#browserContent {{ background: transparent; }}"
            f"QFrame#browserPageFrame {{ background: {t('mv_view_bg')}; border: none;"
            f" border-top: 1px solid {t('dlg_border')}; }}"
            f"QSplitter#browserSplit::handle {{ background: {t('dlg_border')}; }}"
            f"QWidget#browserDevtoolsHead {{ background: {t('dlg_nav_bg')};"
            f" border-bottom: 1px solid {t('dlg_border')}; }}"
            f"QLabel#browserDevtoolsTitle {{ color: {sec}; font-size: 12px; font-weight: 600; }}"
            # 状态卡
            f"QFrame#browserStatusCard {{ background: {t('dlg_bg')}; border: 1px solid {t('dlg_info_border')};"
            f" border-radius: 12px; }}"
            f"QLabel#browserStatusTitle {{ color: {text}; font-size: 13px; font-weight: 600;"
            f" background: transparent; }}"
            f"QLabel#browserStatusSub {{ color: {sec}; font-size: 12px; background: transparent; }}"
            f"QLabel#browserStatusDot {{ background: transparent; }}"
            f"QPushButton#browserStatusStop {{ background: {t('dlg_bg')}; color: {t('dlg_warn_fg')};"
            f" border: 1px solid {t('dlg_warn_border')}; border-radius: 8px; padding: 0 12px;"
            f" font-size: 12px; }}"
            f"QPushButton#browserStatusStop:hover {{ background: {t('dlg_warn_bg')}; }}"
            # 空白页 / 错误页
            f"QWidget#browserEmpty, QWidget#browserError {{ background: transparent;"
            f" border-top: 1px solid {t('dlg_border')}; }}"
            f"QLabel#browserEmptyHeading {{ color: {text}; font-size: 13px; font-weight: 600; }}"
            f"QLabel#browserEmptyNote {{ color: {sec}; font-size: 12px; }}"
            f"QFrame#browserServiceRow {{ background: {t('dlg_bg')}; border: 1px solid {t('dlg_border')};"
            f" border-radius: 12px; }}"
            f"QLabel#browserServiceDot {{ background: {t('dlg_ok_fg')}; border-radius: 4px; }}"
            f"QLabel#browserServiceAddr {{ color: {text}; font-family: {MONO_CSS}; font-size: 13px;"
            f" background: transparent; }}"
            f"QLabel#browserServiceMeta {{ color: {sec}; font-size: 12px; background: transparent; }}"
            f"QPushButton#browserOpenPrimary {{ background: {t('dlg_primary_bg')}; color: {t('dlg_primary_text')};"
            f" border: 1px solid {t('dlg_primary_bg')}; border-radius: 8px; padding: 0 12px;"
            f" font-size: 12px; font-weight: 600; }}"
            f"QPushButton#browserOpenPrimary:hover {{ background: {t('dlg_primary_hover')}; }}"
            f"QPushButton#browserOpenSecondary {{ background: {t('dlg_secondary_bg')};"
            f" color: {t('dlg_secondary_text')}; border: 1px solid {t('dlg_secondary_border')};"
            f" border-radius: 8px; padding: 0 12px; font-size: 12px; }}"
            f"QPushButton#browserOpenSecondary:hover {{ background: {t('dlg_secondary_hover')}; }}"
            f"QPushButton#browserRecentRow {{ background: transparent; border: none;"
            f" border-bottom: 1px solid {t('dlg_border')}; border-radius: 0; text-align: left; }}"
            f"QPushButton#browserRecentRow:hover {{ background: {t('dlg_hover_bg')}; }}"
            f"QLabel#browserRecentTitle {{ color: {text}; font-size: 13px; background: transparent; }}"
            f"QLabel#browserRecentUrl {{ color: {sec}; font-family: {MONO_CSS}; font-size: 12px;"
            f" background: transparent; }}"
            f"QLabel#browserErrorTile {{ background: {t('dlg_err_bg')}; border: 1px solid {t('dlg_err_border')};"
            f" border-radius: 12px; }}"
            f"QLabel#browserErrorTitle {{ color: {text}; font-size: 17px; font-weight: 600; }}"
            f"QLabel#browserErrorBody {{ color: {sec}; font-size: 14px; }}"
            f"QLabel#browserErrorCode {{ color: {sec}; font-family: {MONO_CSS}; font-size: 11px; }}"
        )
        icon_c, dis_c = t("mv_text_sec"), t("mv_muted3")
        self.back_btn.setIcon(_icon("back", icon_c, disabled_color=dis_c))
        self.forward_btn.setIcon(_icon("forward", icon_c, disabled_color=dis_c))
        self.devtools_btn.setIcon(_icon("devtools", icon_c))
        self.more_btn.setIcon(_icon("more", icon_c))
        self.devtools_popout_btn.setIcon(_icon("popout", icon_c, 13))
        self.devtools_close_btn.setIcon(_icon("close", icon_c, 12))
        self._paint_reload_icon()
        self.error_page.tile.setPixmap(_svg_pixmap(_ICONS["plug"], t("dlg_err_fg"), 20, view=16,
                                                   stroke=1.6, dpr=2.0))
        dot = _svg_pixmap('<circle cx="8" cy="8" r="2.4"/>', t("dlg_input_focus"), 16, view=16,
                          stroke=4.0, dpr=2.0)
        self.status_card.dot.setPixmap(dot)
        self.load_bar.update()
        if self.content.currentWidget() is self.empty:
            self._refresh_empty()

    def _paint_reload_icon(self):
        c = self._theme("mv_text_sec")
        if self._loading:
            self.reload_btn.setIcon(_icon("stop", c, 13))
            self.reload_btn.setToolTip("停止加载")
        else:
            self.reload_btn.setIcon(_icon("reload", c, disabled_color=self._theme("mv_muted3")))
            self.reload_btn.setToolTip("刷新")
