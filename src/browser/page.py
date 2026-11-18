"""浏览器页面管理：profile / page / view 的懒加载与安全默认值。

**懒加载硬约束**（§11.7）：WebEngine 的 import 都在函数/方法内部，本模块顶层不 import
QtWebEngine*；profile / page / view 都在首次 get_browser() 时才构造。bridge 在主线程
首次 dispatch 时调 get_browser()，不用浏览器就什么都不存在。

**profile 常驻**（§11.6，2026-11-16 拍板）：profile 由本模块持有、不设 parent，关闭浏览器
只销毁 view/page。offscreen 下"用过 view 后在进程退出前销毁 profile"会触发上游 Qt 退出段错误
（纯 PySide6 最小复现见 §11.6），真实应用 windows 平台不受影响，但测试/CI 跑 offscreen。
关闭时清 cookie / visited links，HTTP 缓存标脏、下一次导航前清（见「HTTP 缓存闸门」）；
**localStorage 没有清除 API，会留到进程退出**（off-the-record 只在内存，不落盘）。

安全默认值（§6.4）：下载拒、新窗口拒、alert/confirm/prompt dismiss、摄像头/麦克风/定位/通知
一律 deny。profile 不传 storageName → 不落盘（off-the-record），cookie 不持久化、缓存只在内存。
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
    """模型首次拉起浏览器时通知 UI 开面板标签（复审 7：让用户看得见、view 不 hidden）。"""
    browser_created = Signal()


_notifier = _BrowserNotifier()


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
            self.loadStarted.connect(self._on_load_started)

        def _on_load_started(self):
            self.nav_seq += 1

        def _note(self, level, text):
            self._sink.append({"level": level, "text": str(text)[:2000], "t": time.time(),
                               "nav": self.nav_seq})

        def javaScriptConsoleMessage(self, level, message, line, source):
            # 隔离世界拦不到页面的 console.*，页面 console 全由这条 Python 通道承担
            self._note(_norm_console_level(level), f"{message} @ {source}:{line}")

        def javaScriptAlert(self, url, msg):
            self._note("warn", f"alert（已忽略）: {msg}")

        def javaScriptConfirm(self, url, msg):
            self._note("warn", f"confirm（已拒绝）: {msg}")
            return False

        def javaScriptPrompt(self, url, msg, default):
            self._note("warn", f"prompt（已拒绝）: {msg}")
            return (False, "")

        def acceptNavigationRequest(self, url, navtype, isMainFrame):
            # 协议白名单兜底（工具层 open 已校验，这里防页面跳转/重定向到 file:// 等）
            scheme = url.scheme()
            if scheme in ("http", "https"):
                return True
            if url.toString() == "about:blank":
                return True
            self._note("warn", f"已拦截非 http(s) 导航: {url.toString()}")
            return False

        def createWindow(self, windowtype):
            # P1 拒绝新窗口 / target=_blank（§6.4）；返回 None 即拦截
            self._note("warn", "链接要求新窗口，已拦截")
            return None

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


class Browser(QObject):
    """一个浏览器实例：常驻 off-the-record profile + 注入文字树的 page + view。主线程构造。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.console_sink = deque(maxlen=_CONSOLE_CAP)
        self.profile = _get_profile()
        self.profile.downloadRequested.connect(self._on_download)

        self.view = QWebEngineView()
        self.page = _console_page_class()(self.profile, self.view, self.console_sink)
        self.view.setPage(self.page)
        self.page.featurePermissionRequested.connect(self._deny_permission)

    def _on_download(self, item):
        try:
            item.cancel()
        except Exception:
            pass
        nav = getattr(self.page, "nav_seq", 0) if shiboken6.isValid(self.page) else 0
        self.console_sink.append({"level": "warn", "text": "下载已拦截（浏览器不下载文件）",
                                  "t": time.time(), "nav": nav})

    def _deny_permission(self, frame, feature):
        from PySide6.QtWebEngineCore import QWebEnginePage
        self.page.setFeaturePermission(
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
        """释放 page / view（WebEngine 进程内存不小，关浏览器时调用）。主线程。
        profile 常驻不销毁（§11.6），只丢引用。"""
        # 先停掉在途加载：冷启动时旧 page 带着未完成的导航被析构，会在共享渲染进程下
        # 撞死下一次导航（loadStarted 后 loadFinished 永不来，复审 8）
        if self.page is not None and shiboken6.isValid(self.page):
            try:
                self.page.triggerAction(self.page.WebAction.Stop)
            except Exception:
                pass
        for attr in ("_devtools_view", "view"):
            w = getattr(self, attr, None)
            if w is not None and shiboken6.isValid(w):
                w.deleteLater()
            setattr(self, attr, None)
        self.page = None
        self.profile = None


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
            page.load(qurl)

    if ((clear_first or _cache_state["dirty"]) and not page_loading
            and not _cache_state["clearing"]):
        clear_cache_then(go)
    else:
        when_cache_idle(go)
