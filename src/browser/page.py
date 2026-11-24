"""浏览器页面管理：profile / page / view 的懒加载与安全默认值。

**懒加载硬约束**（§11.7）：WebEngine 的 import 都在函数/方法内部，本模块顶层不 import
QtWebEngine*；profile / page / view 都在首次 get_browser() 时才构造。bridge 在主线程
首次 dispatch 时调 get_browser()，不用浏览器就什么都不存在。

**profile 常驻**（§11.6，2026-11-16 拍板）：profile 由本模块持有、不设 parent，关闭浏览器
只销毁 view/page。offscreen 下"用过 view 后在进程退出前销毁 profile"会触发上游 Qt 退出段错误
（纯 PySide6 最小复现见 §11.6），真实应用 windows 平台不受影响，但测试/CI 跑 offscreen。
关闭时清 cookie / visited links，HTTP 缓存标脏、下一次导航前清（见「HTTP 缓存闸门」）；
**localStorage 没有清除 API，会留到进程退出**（off-the-record 只在内存，不落盘）。

安全默认值（§6.4）：下载拒、alert/confirm/prompt dismiss、摄像头/麦克风/定位/通知一律 deny；
新窗口按是否用户 / 模型发起开新标签或拦下（多标签，见 Browser）。profile 不传 storageName → 不落盘（off-the-record），cookie 不持久化、缓存只在内存。
"""
import os
import threading
import time
from collections import deque

import shiboken6
from PySide6.QtCore import QObject, QTimer, Signal

_TREE_JS_PATH = os.path.join(os.path.dirname(__file__), "tree.js")
_browser = None
_profile = None          # 常驻：模块持有、无 parent，不挂到会先销毁的控件下
_lock = threading.RLock()
_page_cls = None
_CONSOLE_CAP = 200


class _BrowserNotifier(QObject):
    """模型首次拉起浏览器时通知 UI 开面板标签（复审 7：让用户看得见、view 不 hidden）。
    多标签：每开 / 关一个标签、当前标签换了都通知面板（面板每个网页一个标签，双向同步）。"""
    browser_created = Signal()
    tab_added = Signal(int)            # 标签 id
    tab_closed = Signal(int)
    current_changed = Signal(int)
    popup_blocked = Signal(str)        # 页面自己弹的新窗口被拦下（地址），面板闪一行提示
    download_done = Signal(str, bool)  # 用户点的下载结束了（存到哪、成没成功），面板浮一条提示
    # 模型要打开网页（browser_open）：用户把面板收起了的话弹回来——收起期间浏览器还活着，
    # 不会再走 browser_created；不弹回的话模型在看不见的页面上操作（真实点击退回脚本点击、
    # 隐藏页面定时器被节流），用户也看不见它在干什么（审「收起只隐藏」时实测）
    model_opening = Signal()


_notifier = _BrowserNotifier()


# ── 网页弹框 / 下载 / 证书 / 登录（复审 #16 #17，设计稿「侧栏浏览器 · 网页弹框、下载、证书、登录」）──
#
# 谁触发的决定问不问：浏览器工具正在跑（tools_browser 的锁被占着）算模型触发——确认框按取消、
# 下载拦下、记进 model_events，由工具结果如实告诉模型；其余时候算用户在面板里点的，照常问用户。
# 以前一律当没发生：用户亲手点「删除」，确认框永远取消；下载点了没反应。
#
# 问用户靠界面层注册的同步回调（set_ui_hooks，弹主题卡片）：页面的 confirm / 下载 / 登录都必须
# 当场给出答案。没注册（测试、无界面）一律按取消。

_ui_hooks: dict = {}
model_events: deque = deque(maxlen=50)     # (种类, 网址, 页面给的文字)；工具每次动作后取走
_cert_allowed: set = set()                 # 用户点过「仍然访问」的本机 host:port，关浏览器就忘


def set_ui_hooks(**hooks):
    """界面层注册：js_dialog(kind, url, msg, default) -> (ok, text, block_further)；
    download(name, size, url) -> None | (目录, 文件名)；auth(url, realm) -> None | (用户名, 密码)。"""
    _ui_hooks.update({k: v for k, v in hooks.items() if v is not None})


def model_acting() -> bool:
    """模型的浏览器动作正在跑：工具层对所有浏览器动作串行加锁（复审 #22），锁被占着就是它。"""
    import sys
    tb = sys.modules.get("src.tools_browser")
    lock = getattr(tb, "_tool_lock", None)
    return bool(lock is not None and lock.locked())


def drain_model_events() -> list:
    out = []
    while model_events:
        out.append(model_events.popleft())
    return out


def _host_key(qurl) -> str:
    return f"{qurl.host().lower()}:{qurl.port(443 if qurl.scheme() == 'https' else 80)}"


def allow_certificate(url: str):
    """用户在证书错误页点了「仍然访问（只这次）」：这个本机 host:port 到关浏览器前都放行。
    只给本机地址——公网网站证书不受信任可能就是有人冒充，不给这个口子。"""
    from PySide6.QtCore import QUrl
    from . import security
    if security.is_localhost(url):
        _cert_allowed.add(_host_key(QUrl(url)))


def script_world() -> int:
    """注入脚本所在隔离世界（ApplicationWorld）。页面脚本看不到也改不了 window.__lx，
    闸门输入（info/state）才可信。runJavaScript 必须用同一个 worldId。"""
    from PySide6.QtWebEngineCore import QWebEngineScript
    return int(QWebEngineScript.ScriptWorldId.ApplicationWorld)


def _norm_console_level(level) -> str:
    name = str(level)
    if "Error" in name:
        return "error"
    if "Warning" in name:
        return "warn"
    return "info"


def _read_tree_js() -> str:
    with open(_TREE_JS_PATH, encoding="utf-8") as f:
        return f.read()


def _console_page_class():
    """惰性定义 QWebEnginePage 子类（避免模块顶层 import WebEngine）。"""
    global _page_cls
    if _page_cls is not None:
        return _page_cls
    from PySide6.QtWebEngineCore import QWebEnginePage

    class ConsolePage(QWebEnginePage):
        def __init__(self, profile, parent, sink):
            super().__init__(profile, parent)
            self._sink = sink
            # 第几次页面加载：控制台条目带上它，browser_console 默认只给最近一次加载的，
            # 免得改好代码重开后还看到上一次的旧报错（评测 012）
            self.nav_seq = 0
            # 这个页面有没有导航在途：按页记（多标签后不能只有一个全局标志）
            self.lx_loading = False
            # 灵犀 / 用户在面板里发起的跳转刚开始：这段时间里弹出的「确定离开吗」放行一次（复审 #15）
            self._leave_ok_until = 0.0
            self._dialog_blocked = set()   # 用户勾了「这个网页别再弹框了」的来源（这个标签里）
            self.lx_cert_error = None      # 最近一次证书错误（面板据此画证书页）
            self.loadStarted.connect(self._on_load_started)
            self.certificateError.connect(self._on_certificate_error)
            self.authenticationRequired.connect(self._on_authentication)
            self.loadFinished.connect(self._on_load_finished)

        def _on_load_started(self):
            self.nav_seq += 1
            self.lx_loading = True
            # 离开确认在导航真正开始之前（被拒的话导航根本不开始）：开始加载就收回放行，
            # 不然旧页面没挂 beforeunload 时，这次放行会留给新页面自己的 confirm()（实测：
            # 刚打开的页面上点「删除」被当成同意）
            self._leave_ok_until = 0.0

        def _on_load_finished(self, _ok):
            self.lx_loading = False

        def _note(self, level, text):
            self._sink.append({"level": level, "text": str(text)[:2000], "t": time.time(),
                               "nav": self.nav_seq})

        def javaScriptConsoleMessage(self, level, message, line, source):
            # 隔离世界拦不到页面的 console.*，页面 console 全由这条 Python 通道承担
            self._note(_norm_console_level(level), f"{message} @ {source}:{line}")

        def _page_dialog(self, kind, url, msg, default=""):
            """网页的 alert / confirm / prompt：模型在操作时按取消并记给模型；用户操作时问用户。
            返回 (ok, 输入的文字)。"""
            from . import security
            u = url.toString()
            origin = security.parse_origin(u) or u
            if model_acting():
                model_events.append((kind, u, str(msg)))
                return False, ""
            if origin in self._dialog_blocked:
                self._note("info", f"{kind}（这个网页已设成别再弹框，按取消）: {msg}")
                return False, ""
            hook = _ui_hooks.get("js_dialog")
            if hook is None:
                return False, ""
            try:
                ok, text, block = hook(kind, u, str(msg), str(default or ""))
            except Exception:
                return False, ""
            if block:
                self._dialog_blocked.add(origin)
            return bool(ok), str(text or "")

        def javaScriptAlert(self, url, msg):
            self._note("warn", f"alert: {msg}")
            self._page_dialog("alert", url, msg)

        def allow_leave(self):
            """我们自己要离开这个页面了（打开新地址 / 刷新 / 前进后退）：接下来弹出的离开确认
            （页面的 beforeunload）放行一次。离开确认走的是 confirm 这条通道，以前一律拒绝：
            用户在页面上点过、页面挂了「有未保存的修改」，模型再 browser_open 就卡满超时，
            面板里后退 / 跳转没反应、刷新键停在「停止」。"""
            self._leave_ok_until = time.monotonic() + LEAVE_CONFIRM_WINDOW_S

        def javaScriptConfirm(self, url, msg):
            if time.monotonic() < self._leave_ok_until:
                # 只放这一次：时间窗里页面自己再弹的 confirm 照旧拒绝
                self._leave_ok_until = 0.0
                self._note("info", f"离开确认（我们发起的跳转，已放行）: {msg}")
                return True
            ok, _ = self._page_dialog("confirm", url, msg)
            self._note("info" if ok else "warn", f"confirm（{'用户同意了' if ok else '已取消'}）: {msg}")
            return ok

        def javaScriptPrompt(self, url, msg, default):
            ok, text = self._page_dialog("prompt", url, msg, default)
            self._note("info" if ok else "warn", f"prompt（{'用户填了' if ok else '已取消'}）: {msg}")
            return (ok, text if ok else "")

        def _on_certificate_error(self, err):
            """证书不受信任：用户点过「仍然访问」的本机地址放行，其余一律拒绝（面板画证书页，
            模型那边如实告诉它，不替它放行）。"""
            from . import security
            u = err.url()
            url = u.toString()
            overridable = bool(err.isOverridable())
            if overridable and _host_key(u) in _cert_allowed:
                err.acceptCertificate()
                return
            self.lx_cert_error = {"url": url, "desc": err.description(),
                                  "local": security.is_localhost(url), "overridable": overridable}
            if model_acting():
                model_events.append(("cert", url, err.description()))
            self._note("warn", f"证书不受信任，没有打开: {url}（{err.description()}）")
            err.rejectCertificate()

        def _on_authentication(self, url, auth):
            """网站要登录（HTTP Basic 等）：弹框请用户填（模型触发的也是——设计稿⑤，用户拍板）。
            账号密码只交给网站，不进模型上下文：给模型的只有「登录了 / 取消了」。
            不填就是取消，网站回 401。"""
            u = url.toString()
            hook = _ui_hooks.get("auth")
            creds = None
            if hook is not None:
                try:
                    creds = hook(u, auth.realm())
                except Exception:
                    creds = None
            acting = model_acting()
            if creds:
                auth.setUser(creds[0])
                auth.setPassword(creds[1])
                if acting:
                    model_events.append(("auth_ok", u, ""))
            elif acting:
                model_events.append(("auth_cancel", u, ""))

        def acceptNavigationRequest(self, url, navtype, isMainFrame):
            # 协议白名单兜底（工具层 open 已校验，这里防页面跳转/重定向到 file:// 等）
            scheme = url.scheme()
            if scheme in ("http", "https"):
                return True
            if url.toString() == "about:blank":
                return True
            if not isMainFrame and scheme in ("blob", "data"):
                # 页面自己在 iframe 里放的 blob: / data: 内容（预览、编辑器沙箱）：子框架照常显示，
                # 以前也一律拦了（复审 #12）。顶层页面仍然只放 http(s)
                return True
            self._note("warn", f"已拦截非 http(s) 导航: {url.toString()}")
            return False

        # 不覆写 createWindow：新窗口 / target=_blank 走 newWindowRequested（Browser 里接），
        # 按「是不是用户发起的」决定开新标签还是拦下（以前一律拦，B 站点视频没反应）

    _page_cls = ConsolePage
    return _page_cls


def _get_profile():
    """首次使用时构造常驻 profile（主线程）。注入脚本只插一次，重开浏览器复用。"""
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import (QWebEngineProfile, QWebEngineScript)
        # 不传 storageName → off-the-record，不落盘；不设 parent → 模块持有、常驻
        profile = QWebEngineProfile()
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        # 不缓存：这是调试本地页面的浏览器，改完 app.js 重开就得跑新代码。内存缓存会按
        # Last-Modified 推断"还新鲜"直接复用旧文件，连 ?v=2 都绕不过去（评测 012）
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        script = QWebEngineScript()
        script.setName("lx-tree")
        script.setSourceCode(_read_tree_js())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setRunsOnSubFrames(True)
        profile.scripts().insert(script)
        profile.clearHttpCacheCompleted.connect(_cache_cleared)
        _profile = profile
    return _profile


MAX_TABS = 10
# 模型点击 / 输入之后这么久之内到达的新窗口请求，算模型发起的（脚本点击退路下
# isUserInitiated 是 False，和广告弹窗分不开，只能按时间归因）
_MODEL_ACTION_GRACE_S = 1.5


class Tab:
    """一个标签：自己的 view / page / 控制台缓冲。"""

    def __init__(self, tab_id, view, page, console_sink, opener=None):
        self.id = tab_id
        self.view = view
        self.page = page
        self.console_sink = console_sink
        self.opener = opener          # 从哪个标签点开的（None = 自己开的）


class Browser(QObject):
    """浏览器：常驻 off-the-record profile + 若干标签（各有注入文字树的 page + view）。主线程构造。

    `view` / `page` / `console_sink` 指**当前标签**——模型的动作、桥的请求都作用于当前标签，
    面板里切标签就是切当前标签（双向同步）。页面要求开新窗口（target=_blank、window.open）时：
    用户发起的（真实点击，模型的点击现在也是真实鼠标事件）、或模型动作后不久到达的，开新标签；
    页面自己弹的（广告、定时弹窗）拦下并提示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.profile = _get_profile()
        self.profile.downloadRequested.connect(self._on_download)
        self.tabs = {}                 # id -> Tab，按打开顺序
        self.current_id = None
        self._seq = 0
        self.model_action_at = 0.0     # 最近一次模型点击 / 输入（monotonic），见 note_model_action
        self.new_tab()

    # ── 当前标签 ──
    @property
    def current(self):
        return self.tabs.get(self.current_id)

    @property
    def view(self):
        t = self.current
        return t.view if t else None

    @property
    def page(self):
        t = self.current
        return t.page if t else None

    @property
    def console_sink(self):
        t = self.current
        return t.console_sink if t else deque(maxlen=_CONSOLE_CAP)

    def tab(self, tab_id):
        return self.tabs.get(tab_id)

    def tab_of_page(self, pg):
        return next((t for t in self.tabs.values() if t.page is pg), None)

    # ── 开 / 关 / 切 ──
    def new_tab(self, *, make_current=True, opener=None):
        from PySide6.QtWebEngineWidgets import QWebEngineView
        self._seq += 1
        sink = deque(maxlen=_CONSOLE_CAP)
        view = QWebEngineView()
        pg = _console_page_class()(self.profile, view, sink)
        view.setPage(pg)
        pg.featurePermissionRequested.connect(
            lambda frame, feature, p=pg: self._deny_permission(p, frame, feature))
        pg.newWindowRequested.connect(lambda req, p=pg: self._on_new_window(p, req))
        tab = Tab(self._seq, view, pg, sink, opener=opener)
        self.tabs[tab.id] = tab
        first = self.current_id is None
        if make_current or first:
            self.current_id = tab.id
        tid = tab.id
        # 下一轮事件循环再通知：别在 bridge dispatch / 页面信号中途重入面板控件
        QTimer.singleShot(0, _notifier, lambda: _notifier.tab_added.emit(tid))
        if (make_current or first) and not first:
            QTimer.singleShot(0, _notifier, lambda: _notifier.current_changed.emit(tid))
        return tab

    def set_current(self, tab_id):
        if tab_id not in self.tabs or tab_id == self.current_id:
            return
        self.current_id = tab_id
        _notifier.current_changed.emit(tab_id)

    def close_tab(self, tab_id):
        """关一个标签。最后一个由调用方走 close_browser（清 cookie 那一套）。"""
        tab = self.tabs.pop(tab_id, None)
        if tab is None:
            return
        if self.current_id == tab_id:
            # 回到打开它的那个标签（还在的话），否则相邻的最后一个
            back = tab.opener if tab.opener in self.tabs else None
            self.current_id = back if back is not None else (list(self.tabs)[-1] if self.tabs else None)
            if self.current_id is not None:
                _notifier.current_changed.emit(self.current_id)
        _release_tab(tab)
        _notifier.tab_closed.emit(tab_id)

    def note_model_action(self):
        self.model_action_at = time.monotonic()

    def _on_new_window(self, opener_page, req):
        from PySide6.QtWebEngineCore import QWebEngineNewWindowRequest as R
        url = req.requestedUrl().toString()
        opener = self.tab_of_page(opener_page)
        by_model = time.monotonic() - self.model_action_at < _MODEL_ACTION_GRACE_S
        sink = opener.console_sink if opener else self.console_sink
        nav = getattr(opener_page, "nav_seq", 0)
        if not (req.isUserInitiated() or by_model):
            sink.append({"level": "warn", "text": f"已拦截页面自己弹出的新窗口: {url}",
                         "t": time.time(), "nav": nav})
            _notifier.popup_blocked.emit(url)
            return
        if len(self.tabs) >= MAX_TABS:
            sink.append({"level": "warn", "t": time.time(), "nav": nav,
                         "text": f"标签已开满 {MAX_TABS} 个，新窗口没打开: {url}"})
            _notifier.popup_blocked.emit(url)
            return
        background = req.destination() == R.DestinationType.InNewBackgroundTab
        tab = self.new_tab(make_current=not background,
                           opener=opener.id if opener else None)
        # openIn 保留 window.opener：第三方登录弹窗要靠它把结果传回原页
        req.openIn(tab.page)

    def _on_download(self, item):
        """模型操作时触发的下载拦下、告诉模型；用户点的问存哪，存好了面板浮一条提示。
        Qt 要求在这个信号里当场 accept / cancel，所以问用户是同步弹框。"""
        hook = _ui_hooks.get("download")
        name = item.downloadFileName()
        url = item.url().toString()
        if not model_acting() and hook is not None:
            try:
                target = hook(name, int(item.totalBytes()), url)
            except Exception:
                target = None
            if target:
                folder, fname = target
                item.setDownloadDirectory(folder)
                item.setDownloadFileName(fname)
                path = os.path.join(folder, fname)

                def _state(_st=None, it=item, p=path):
                    from PySide6.QtWebEngineCore import QWebEngineDownloadRequest as D
                    st = it.state()
                    if st == D.DownloadState.DownloadCompleted:
                        _notifier.download_done.emit(p, True)
                    elif st in (D.DownloadState.DownloadCancelled, D.DownloadState.DownloadInterrupted):
                        _notifier.download_done.emit(p, False)
                item.stateChanged.connect(_state)
                item.accept()
                return
        elif model_acting():
            model_events.append(("download", url, name))
        try:
            item.cancel()
        except Exception:
            pass
        tab = None
        try:
            tab = self.tab_of_page(item.page())
        except Exception:
            pass
        tab = tab or self.current
        if tab is None:
            return
        nav = getattr(tab.page, "nav_seq", 0) if shiboken6.isValid(tab.page) else 0
        tab.console_sink.append({"level": "warn", "text": f"下载已拦下: {name}",
                                 "t": time.time(), "nav": nav})

    def _deny_permission(self, pg, frame, feature):
        from PySide6.QtWebEngineCore import QWebEnginePage
        pg.setFeaturePermission(
            frame, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)

    def open_devtools(self):
        """F12：给当前 page 挂一个 devtools page（保留 devtools_resources.pak 才可用，§8）。"""
        from PySide6.QtWebEngineWidgets import QWebEngineView
        if getattr(self, "_devtools_view", None) is not None:
            self._devtools_view.show()
            return
        self._devtools_view = QWebEngineView()
        self._devtools_view.setWindowTitle("DevTools")
        self.page.setDevToolsPage(self._devtools_view.page())
        self._devtools_view.resize(900, 700)
        self._devtools_view.show()

    def release(self):
        """释放所有标签的 page / view（WebEngine 进程内存不小，关浏览器时调用）。主线程。
        profile 常驻不销毁（§11.6），只丢引用。"""
        w = getattr(self, "_devtools_view", None)
        if w is not None and shiboken6.isValid(w):
            w.deleteLater()
        self._devtools_view = None
        closed = list(self.tabs)
        for tab in list(self.tabs.values()):
            _release_tab(tab)
        self.tabs.clear()
        self.current_id = None
        self.profile = None
        for tid in closed:
            _notifier.tab_closed.emit(tid)


def _release_tab(tab):
    # 先停掉在途加载：冷启动时旧 page 带着未完成的导航被析构，会在共享渲染进程下
    # 撞死下一次导航（loadStarted 后 loadFinished 永不来，复审 8）
    if tab.page is not None and shiboken6.isValid(tab.page):
        try:
            tab.page.triggerAction(tab.page.WebAction.Stop)
        except Exception:
            pass
    if tab.view is not None and shiboken6.isValid(tab.view):
        tab.view.deleteLater()        # page 是 view 的子对象，一起走
    tab.view = None
    tab.page = None


def get_browser() -> Browser:
    """首次使用时在主线程构造浏览器；之后复用。调用方须在主线程（bridge dispatch）。"""
    global _browser
    with _lock:
        if _browser is None:
            _browser = Browser()
            # 延迟到下一轮事件循环再通知 UI 开标签：避免在 bridge dispatch 中途重入控件
            QTimer.singleShot(0, _notifier, lambda: _notifier.browser_created.emit())
        return _browser


def is_browser_open() -> bool:
    return _browser is not None


# 用户最近一次在灵犀的输入框里打字（monotonic）：模型真实点击网页会让网页拿走键盘焦点，
# 用户正打着字时点完要还回去（bridge._mouse）。由界面在输入框的按键里记
_USER_TYPING_GRACE_S = 3.0
_user_typed_at = 0.0


def note_user_typing():
    global _user_typed_at
    _user_typed_at = time.monotonic()


def user_typing_recently() -> bool:
    return time.monotonic() - _user_typed_at < _USER_TYPING_GRACE_S


def current_tab_id():
    b = _browser
    return b.current_id if b is not None else None


def set_current_tab(tab_id):
    if _browser is not None:
        _browser.set_current(tab_id)


def close_tab(tab_id):
    """关一个标签（主线程）。最后一个标签走 close_browser：清 cookie、在途请求回「已关闭」。"""
    b = _browser
    if b is None or tab_id not in b.tabs:
        return
    if len(b.tabs) <= 1:
        close_browser()
        return
    was_current = b.current_id == tab_id
    b.close_tab(tab_id)
    if was_current:
        # 在途请求作用的是刚关掉的那页：立刻回，不等超时
        from src.browser import bridge
        bridge.notify_tab_closed()


def close_browser():
    """关闭并释放 view/page（主线程），profile 常驻复用。下次 get_browser() 重新构造页面。
    关闭时清 cookie / visited links，HTTP 缓存标脏留给下一次导航先清（和导航重叠会卡死它）；
    localStorage 无清除 API，留到进程退出（off-the-record 只在内存、不落盘，§11.6）。"""
    global _browser
    with _lock:
        if _browser is not None:
            _browser.release()
            _browser.deleteLater()
            _browser = None
            # 同步把旧 view/page 的 DeferredDelete 处理掉再返回：否则下一次 open 的新导航
            # 会和旧 page 析构在共享渲染进程里相撞（冷启动首载途中关闭→下次卡满超时，复审 8）
            from PySide6.QtCore import QEvent
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.sendPostedEvents(None, QEvent.DeferredDelete)
        if _profile is not None:
            _profile.cookieStore().deleteAllCookies()   # 异步，安全；调用方需泵事件等它完成
            _profile.clearAllVisitedLinks()             # 实测不碍导航，直接清
            # HTTP 缓存不在这里清，只记一笔、由下一次导航先清再走（见 load_url）。早先在这里
            # 延后清（轮询等桥空闲），但面板自己的导航、以及"刚判定空闲、模型的 open 紧跟着到"
            # 都会撞上——clearHttpCache 和导航重叠，那次导航就再也等不到 loadFinished
            mark_cache_dirty()
            _cert_allowed.clear()           # 「仍然访问（只这次）」= 关掉浏览器就忘
        from src.browser import bridge
        bridge.notify_closed()   # 在途请求立刻回"浏览器已关闭"，不等超时


# ── HTTP 缓存闸门 ──────────────────────────────────────────────────
#
# clearHttpCache 和导航**不能重叠**：清的过程中（发出前后都算）开始的导航，loadStarted 之后
# 再也等不到 loadFinished，卡满加载超时（实测：load 前后紧挨着清，8/8 卡死；clearAllVisitedLinks
# 不受影响）。所以所有导航都走 load_url：有清理在途就排队等清完，清理一次只跑一个。
# 曾经的三个口子：关闭后延时清撞上紧接着的重开；两次清叠在一起，前一次的"清完了"信号把后一次
# 等着的导航提前放行；2 秒兜底到点就 load、清理其实还没完。

_CACHE_CLEAR_FALLBACK_MS = 5000
_cache_state = {"clearing": False, "dirty": False, "waiters": [], "timer": None}


def mark_cache_dirty():
    """下一次导航前要先清 HTTP 缓存（关浏览器后：新页面可能复用旧渲染进程的内存缓存）。"""
    _cache_state["dirty"] = True


def cache_dirty() -> bool:
    return _cache_state["dirty"]


def cache_clearing() -> bool:
    return _cache_state["clearing"]


def _cache_cleared():
    st = _cache_state
    if not st["clearing"]:
        return
    st["clearing"] = False
    timer, st["timer"] = st["timer"], None
    if timer is not None:
        timer.stop()
        timer.deleteLater()
    waiters, st["waiters"] = st["waiters"], []
    for fn in waiters:
        try:
            fn()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("清缓存后的导航回调出错")


def _cache_clear_timed_out():
    import logging
    logging.getLogger(__name__).warning(
        "clearHttpCache %d ms 没回完成信号，按已完成继续导航", _CACHE_CLEAR_FALLBACK_MS)
    _cache_cleared()


def clear_cache_then(callback):
    """清 HTTP 缓存，清完（clearHttpCacheCompleted）再调 callback。主线程。
    已经有一次在清就排在它后面，不再叠一次（叠起来时前一次的完成信号会提前放行）。"""
    st = _cache_state
    st["waiters"].append(callback)
    if st["clearing"]:
        return
    profile = _get_profile()
    st["clearing"] = True
    st["dirty"] = False
    timer = QTimer(profile)                  # receiver 上下文：落在主线程
    timer.setSingleShot(True)
    timer.timeout.connect(_cache_clear_timed_out)
    timer.start(_CACHE_CLEAR_FALLBACK_MS)
    st["timer"] = timer
    profile.clearHttpCache()


def when_cache_idle(callback):
    """没有清理在途就立刻调 callback，否则等清完。"""
    if _cache_state["clearing"]:
        _cache_state["waiters"].append(callback)
    else:
        callback()


# 发起跳转后多久内弹出的离开确认算「我们这次跳转引起的」。beforeunload 在导航一开始就同步触发，
# 留几秒给慢页面；只放一次（见 ConsolePage.javaScriptConfirm）
LEAVE_CONFIRM_WINDOW_S = 5.0


def allow_leave(page):
    """要离开 page 了：放行它接下来的那一次离开确认（页面类型不是我们的就什么都不做）。"""
    fn = getattr(page, "allow_leave", None)
    if fn is not None and shiboken6.isValid(page):
        fn()


def load_url(page, url, *, clear_first=False, page_loading=False, still_wanted=None):
    """所有导航的唯一入口（模型的 open 和面板的地址栏都走这里）。

    clear_first：先清 HTTP 缓存再导航（模型重开改过的页面要跑新代码，评测 012）。
    缓存被标脏（关过浏览器）时也先清。page_loading（页面正在加载）时不清、脏标记留着——
    在途导航会被清理卡死，新导航直接接替它。still_wanted：排队期间请求可能被取代 / 取消，
    回调前再问一次。
    """
    from PySide6.QtCore import QUrl
    qurl = url if isinstance(url, QUrl) else QUrl(url)

    def go():
        if still_wanted is not None and not still_wanted():
            return
        if shiboken6.isValid(page):
            allow_leave(page)
            page.load(qurl)

    if ((clear_first or _cache_state["dirty"]) and not page_loading
            and not _cache_state["clearing"]):
        clear_cache_then(go)
    else:
        when_cache_idle(go)
