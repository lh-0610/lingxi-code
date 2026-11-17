"""worker 线程 ↔ 主线程 WebEngine 的非阻塞桥（§5.1）。

worker 调 call(op, ...) → request.emit（跨线程自动 QueuedConnection）→ 主线程 _handle
dispatch → 写 holder + ev.set() 回传。**open 不用嵌套 QEventLoop**：发起 page.load 后
worker 就在 ev.wait 上等，主线程在 loadFinished 回调里写结果，另挂带 receiver 上下文的
QTimer 管超时——嵌套循环期间主线程会处理关窗/切会话等事件，窗口被关掉后访问已删除控件会崩
（同 CLAUDE.md 里 QTimer.singleShot 无 receiver 那条实测崩溃）。

worker 等待**分片**（每 0.3s 查 state.stop_flag，仿 mcp_client.py:411-426）：用户点停止
立刻 emit cancel（page.triggerAction(Stop) + 作废请求）并返回"已停止"，不傻等整个加载。

懒加载：本模块顶层不 import WebEngine；BrowserBridge 构造也不创建 page，_handle 首次
dispatch 时才在主线程调 page.get_browser()。
"""
import json
import threading
import time

import shiboken6
from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from src import state

_bridge = None
_bridge_lock = threading.Lock()


class BrowserBridge(QObject):
    request = Signal(object)          # {op, args, holder, ev}
    activity = Signal(str)            # 主线程 dispatch 时发动作名（面板徽标），回包时发 ""

    def __init__(self):
        super().__init__()
        self._closed = False
        self._browser = None
        self._open_req = None
        self._open_timer = None
        self._open_started = False
        self._wired_page = None
        self._page_loading = False    # 页面有导航在途（含用户/页面自己发起的），在途时不清缓存
        self._pending = []            # 等回调的在途请求：关浏览器时立刻回"浏览器已关闭"，不等超时
        self._ref_seed = 0            # ref 编号跨文档单调递增的种子（换页不归零）
        self.request.connect(self._handle)   # 跨线程自动 QueuedConnection

    # ── worker 线程入口 ──────────────────────────────────────────
    def call(self, op, timeout=30, **args):
        if self._closed:
            return {"ok": False, "error": "browser closed"}
        holder, ev = {}, threading.Event()
        req = {"op": op, "args": args, "holder": holder, "ev": ev, "wait_timeout": timeout}
        t0 = time.perf_counter()
        self.request.emit(req)
        deadline = time.time() + timeout
        while not ev.wait(0.3):                 # 分片等待，响应停止按钮
            if state.stop_flag:
                self.request.emit({"op": "cancel", "args": {}, "holder": holder, "ev": ev, "target": req})
                return {"ok": False, "error": "已停止：用户停止了生成。",
                        "wallMs": round((time.perf_counter() - t0) * 1000)}
            if time.time() > deadline:
                self.request.emit({"op": "cancel", "args": {}, "holder": holder, "ev": ev, "target": req})
                return {"ok": False, "error": f"等待超时 {timeout}s",
                        "wallMs": round((time.perf_counter() - t0) * 1000)}
        r = holder.get("result") or {"ok": False, "error": "主线程未写结果"}
        if isinstance(r, dict):
            r["wallMs"] = round((time.perf_counter() - t0) * 1000)
        return r

    def shutdown(self):
        self._closed = True

    # ── 以下都在主线程 ───────────────────────────────────────────
    def _reply(self, req, result):
        if req.get("replied"):
            return
        req["replied"] = True
        if req in self._pending:
            self._pending.remove(req)
        self.activity.emit("")
        req["holder"]["result"] = result
        req["ev"].set()

    def _on_browser_closed(self):
        """关浏览器（面板关标签 / 模型 browser_close）后：在途请求立刻回"浏览器已关闭"。
        不然等回调的请求（open 的 loadFinished、lx 的 runJavaScript）会傻等到超时。
        之后 browser_open 照常重建页面（_handle 里 get_browser 会重新构造）。"""
        self._clear_open()
        self._browser = None
        for req in list(self._pending):
            self._reply(req, {"ok": False, "error": "浏览器已关闭"})

    _ACTIVITY_LABELS = {"open": "打开页面", "js": "执行脚本", "frames": "列框架",
                        "settle": "等待页面", "close": "关闭浏览器",
                        "qturl": "读地址", "console": "读控制台"}
    _LX_LABELS = {"snapshot": "快照", "find": "查找", "click": "点击", "type": "输入",
                  "readText": "读正文", "console": "控制台", "info": "查元素", "state": "读状态"}

    def _activity_label(self, req):
        op = req["op"]
        if op == "lx":
            return self._LX_LABELS.get(req["args"].get("method"), "操作页面")
        return self._ACTIVITY_LABELS.get(op, "操作页面")

    def _handle(self, req):
        op = req["op"]
        if op == "cancel":
            self._cancel(req.get("target") or req)
            return
        self.activity.emit(self._activity_label(req))
        if op == "close":
            from src.browser import page
            self._clear_open()
            page.close_browser()
            self._browser = None
            # 延迟回包：等事件泵把旧 view/page 的 deleteLater 处理完再放行下一次 open。
            # profile 常驻后新旧页面共享渲染进程，旧 page 析构撞上新导航会把加载卡死
            # （loadStarted 后 loadFinished 永不来，实测 §11.6）。
            QTimer.singleShot(50, self, lambda: self._reply(req, {"ok": True}))
            return
        if self._closed:
            self._reply(req, {"ok": False, "error": "browser closed"})
            return
        from src.browser import page
        try:
            self._browser = page.get_browser()
        except Exception as e:
            self._reply(req, {"ok": False, "error": f"浏览器初始化失败: {e}"})
            return
        if self._browser is None or not shiboken6.isValid(self._browser.page):
            self._reply(req, {"ok": False, "error": "browser closed"})
            return
        args = req["args"]
        self._pending.append(req)
        try:
            if op == "open":
                self._open(req, args)
            elif op == "js":
                self._run_js(req, self._browser.page, args["code"], False)
            elif op == "lx":
                self._lx(req, args["method"], args.get("args", {}), args.get("frame"))
            elif op == "frames":
                self._frames(req)
            elif op == "qturl":
                self._qturl(req, args.get("frame"))
            elif op == "console":
                self._console(req, args)
            elif op == "settle":
                QTimer.singleShot(args.get("ms", 800), self, lambda: self._reply(req, {"ok": True}))
            else:
                self._reply(req, {"ok": False, "error": "未知 op " + op})
        except Exception as e:
            self._reply(req, {"ok": False, "error": f"主线程异常: {e}"})

    def _cancel(self, target):
        target["cancelled"] = True
        if self._open_req is target:
            if self._browser is not None and shiboken6.isValid(self._browser.page):
                try:
                    self._browser.page.triggerAction(self._browser.page.WebAction.Stop)
                except Exception:
                    pass
            self._clear_open()
        self._reply(target, {"ok": False, "error": "已停止：用户停止了生成。"})

    # open：非阻塞加载。loadStarted/loadFinished 持久挂在 page 上，用"本次导航已开始"
    # 门控区分迟到信号——被停止的上一个导航会迟到一条 loadFinished(false)，若不门控会被
    # 误当成新 open 的结果（实测 loadMs=8 的假失败）。
    def _wire_page(self, page):
        if self._wired_page is page:
            return
        old = self._wired_page
        if old is not None and shiboken6.isValid(old):
            try:
                old.loadStarted.disconnect(self._on_load_started)
            except (RuntimeError, TypeError):
                pass
            try:
                old.loadFinished.disconnect(self._on_load_finished)
            except (RuntimeError, TypeError):
                pass
        page.loadStarted.connect(self._on_load_started)
        page.loadFinished.connect(self._on_load_finished)
        self._wired_page = page
        self._page_loading = False

    def _on_load_started(self):
        self._page_loading = True
        if self._open_req is not None:
            self._open_started = True

    def _on_load_finished(self, ok):
        self._page_loading = False
        req = self._open_req
        if req is None or req.get("cancelled") or req.get("replied"):
            return
        if not self._open_started:
            return                              # 上一个被停止导航的迟到 loadFinished，忽略
        self._finish_open(req, ok=ok, loaded=True,
                          error=None if ok else "loadFinished(false)")

    def _open(self, req, args):
        page = self._browser.page
        self._abort_open("被新的导航取代")
        self._wire_page(page)
        # call() 的 timeout 关键字被等待参数接走，args 里永远不会有 "timeout"——
        # 页面加载定时器跟着等待时限走，两者谁先到都返回超时错误（另一侧有 replied 守卫）
        timeout = args.get("timeout") or req.get("wait_timeout") or 30
        req["t0"] = time.perf_counter()
        self._open_req = req
        self._open_started = False
        timer = QTimer(self)                    # parent=self → receiver 上下文，落在主线程
        timer.setSingleShot(True)

        def on_timeout():
            if req.get("cancelled") or req.get("replied") or self._open_req is not req:
                return
            req["cancelled"] = True
            try:
                page.triggerAction(page.WebAction.Stop)
            except Exception:
                pass
            self._finish_open(req, ok=False, loaded=False,
                              error="加载超时（DOM 可能不完整）")

        timer.timeout.connect(on_timeout)
        timer.start(int(timeout * 1000))
        self._open_timer = timer
        url = QUrl(args["url"])
        if self._page_loading or page.url().isEmpty() or page.url().toString() == "about:blank":
            page.load(url)          # 新页面渲染进程里没有旧脚本；在途导航时清缓存会堵死导航
        else:
            self._clear_cache_then_load(req, page, url)

    def _clear_cache_then_load(self, req, page, url):
        """先清缓存再导航。profile 已是 NoCache，但渲染进程里的 Blink 内存缓存仍会按
        Last-Modified 推断"还新鲜"复用旧脚本——改完 app.js 重开同一地址、加 ?v=2、Reload
        都还跑旧代码（评测 012），只有 clearHttpCache（会一并清渲染进程内存缓存）管用。
        它是异步的：等 clearHttpCacheCompleted 再 load，2 秒兜底。"""
        profile = self._browser.profile
        fired = {"done": False, "conn": None}

        def go():
            if fired["done"]:
                return
            fired["done"] = True
            try:
                profile.clearHttpCacheCompleted.disconnect(fired["conn"])
            except (RuntimeError, TypeError):
                pass
            if req.get("cancelled") or req.get("replied") or self._open_req is not req:
                return
            if shiboken6.isValid(page):
                page.load(url)

        fired["conn"] = profile.clearHttpCacheCompleted.connect(go)
        QTimer.singleShot(2000, self, go)
        profile.clearHttpCache()

    def _finish_open(self, req, *, ok, loaded, error):
        page = self._browser.page if self._browser is not None else None
        valid = page is not None and shiboken6.isValid(page)
        t0 = req.get("t0", time.perf_counter())
        self._clear_open()
        self._reply(req, {
            "ok": ok, "loaded": loaded,
            "url": page.url().toString() if valid else "",
            "title": page.title() if valid else "",
            "loadMs": round((time.perf_counter() - t0) * 1000),
            "error": error,
            "nav": getattr(page, "nav_seq", 0) if valid else 0,
        })

    def _clear_open(self):
        if self._open_timer is not None:
            self._open_timer.stop()
            self._open_timer.deleteLater()
            self._open_timer = None
        self._open_req = None
        self._open_started = False

    def _abort_open(self, reason):
        req = self._open_req
        if req is not None:
            req["cancelled"] = True
            self._clear_open()
            self._reply(req, {"ok": False, "error": reason})

    # JS / 文字树操作
    def _run_js(self, req, target, code, is_json):
        from src.browser import page as page_mod
        world = page_mod.script_world()

        def cb(result):
            if req.get("cancelled"):
                return
            if is_json:
                try:
                    result = json.loads(result) if isinstance(result, str) else {
                        "ok": False, "error": f"JS 返回非字符串: {result!r}"}
                except Exception as e:
                    result = {"ok": False, "error": f"JSON 解析失败: {e}", "raw": str(result)[:300]}
                if isinstance(result, dict):
                    # 围栏/来源用的 URL 取 Qt 侧真值，不信页面自报
                    try:
                        result["qtUrl"] = target.url().toString()
                    except Exception:
                        pass
                    if result.get("nextSeq"):
                        self._ref_seed = max(self._ref_seed, int(result["nextSeq"]))
            self._reply(req, result if isinstance(result, dict) else {"ok": True, "value": result})

        try:
            target.runJavaScript(code, world, cb)
        except TypeError:
            target.runJavaScript(code, cb)   # 旧签名兜底

    def _lx(self, req, method, lxargs, frame):
        if method in ("snapshot", "find"):
            lxargs = dict(lxargs, __seed=self._ref_seed)
        # try/catch：页面脚本抛异常时给模型可读错误，而不是"JS 返回非字符串/JSON 解析失败"
        code = (f"JSON.stringify((()=>{{try{{return window.__lx ? window.__lx.{method}("
                f"{json.dumps(lxargs, ensure_ascii=False)}) : {{ok:false,error:'__lx 未注入'}};}}"
                f"catch(e){{return {{ok:false,error:'页面脚本异常: '+((e&&e.message)||e)}};}}}})())")
        target = self._browser.page
        if frame:
            target = self._frame_by_path(frame)
            if target is None:
                self._reply(req, {"ok": False, "error": "frame 不存在 " + frame})
                return
        self._run_js(req, target, code, True)

    def _frame_by_path(self, path):
        found = [None]

        def walk(frame, p):
            for i, child in enumerate(frame.children()):
                cp = f"{p}.{i}"
                if cp == path:
                    found[0] = child
                walk(child, cp)

        walk(self._browser.page.mainFrame(), "f")
        return found[0]

    def _frames(self, req):
        info = []

        def walk(frame, path):
            for i, child in enumerate(frame.children()):
                p = f"{path}.{i}"
                try:
                    url = child.url().toString()
                except Exception:
                    url = "?"
                info.append({"path": p, "url": url})
                walk(child, p)

        walk(self._browser.page.mainFrame(), "f")
        self._reply(req, {"ok": True, "frames": info})

    def _qturl(self, req, frame):
        """页面/frame 的真实 URL（Qt 侧）。闸门与围栏不信页面自报的 URL。"""
        target = self._browser.page
        if frame:
            target = self._frame_by_path(frame)
            if target is None:
                self._reply(req, {"ok": False, "error": "frame 不存在 " + frame})
                return
        self._reply(req, {"ok": True, "url": target.url().toString()})

    def _console(self, req, args):
        """console 通道 = Python sink（页面 console/alert/拦截通知）+ 隔离世界资源失败缓冲。"""
        b = self._browser
        entries = list(b.console_sink)
        if args.get("clear"):
            b.console_sink.clear()
        from src.browser import page as page_mod
        code = ("JSON.stringify(window.__lx ? window.__lx.console("
                + json.dumps({"clear": bool(args.get("clear"))}) + ") : {entries:[]})")

        def cb(result):
            if req.get("cancelled"):
                return
            js_entries = []
            if isinstance(result, str):
                try:
                    js_entries = (json.loads(result) or {}).get("entries") or []
                except Exception:
                    js_entries = []
            nav = getattr(b.page, "nav_seq", 0)
            seen = {(e.get("level"), e.get("text"), e.get("nav")) for e in entries}
            for e in js_entries:
                e = dict(e, nav=nav)            # 隔离世界的缓冲随文档重建，只会是当前这次加载的
                key = (e.get("level"), e.get("text"), nav)
                if key not in seen:
                    seen.add(key)
                    entries.append(e)
            entries.sort(key=lambda e: e.get("t") or 0)
            self._reply(req, {"ok": True, "entries": entries, "nav": nav,
                              "url": b.page.url().toString()})

        try:
            b.page.runJavaScript(code, page_mod.script_world(), cb)
        except TypeError:
            b.page.runJavaScript(code, cb)


def get_bridge() -> BrowserBridge:
    """模块单例。QObject 须活在主线程：worker 首次创建后 moveToThread(app.thread())，
    signal 自动排队到主线程事件循环。调用方任意线程。"""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            b = BrowserBridge()
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                b.moveToThread(app.thread())
            _bridge = b
        return _bridge


def shutdown():
    """app 退出时清理（main.py _cleanup_on_exit 调）。"""
    if _bridge is not None:
        _bridge.shutdown()


def notify_closed():
    """page.close_browser() 调：在途请求立刻回"浏览器已关闭"（主线程）。"""
    if _bridge is not None:
        _bridge._on_browser_closed()