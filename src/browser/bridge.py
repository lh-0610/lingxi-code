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
    # 「灵犀的鼠标」（设计稿「浏览器：灵犀的鼠标」）：标签 id、页面 CSS 坐标、动作
    # （click / type / blocked）、说明（点的是什么，状态卡用）
    cursor_event = Signal(int, float, float, str, str)

    def __init__(self):
        super().__init__()
        self._closed = False
        self._browser = None
        self._open_req = None
        self._open_timer = None
        self._open_started = False
        self._wired_page = None
        self._page_loading = False    # 页面有导航在途（含用户/页面自己发起的），在途时不清缓存
        self._load_fail = None        # 本次导航 loadingChanged 报的失败信息（区分 HTTP 状态码域）
        self._pending = []            # 等回调的在途请求：关浏览器时立刻回"浏览器已关闭"，不等超时
        self._ref_seed = 0            # ref 编号跨文档单调递增的种子（换页不归零）
        self._driver = None           # 最近一次发请求的会话（worker 线程里的 current_session）
        self.request.connect(self._handle)   # 跨线程自动 QueuedConnection

    # ── worker 线程入口 ──────────────────────────────────────────
    def call(self, op, timeout=30, **args):
        if self._closed:
            return {"ok": False, "error": "browser closed"}
        # 记下是哪个会话在驱动浏览器：面板状态卡的「停止」停的是它，不是碰巧在前台的那个
        from src import session as _session_mod
        self._driver = _session_mod.current_session()
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

    def driver_session(self):
        """最近一次让浏览器干活的会话（没有就 None）。面板状态卡「停止」和来源标签用。"""
        return self._driver

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

    def _tab_list(self):
        out = []
        for t in self._browser.tabs.values():
            pg = t.page
            valid = pg is not None and shiboken6.isValid(pg)
            out.append({"id": t.id, "url": pg.url().toString() if valid else "",
                        "title": pg.title() if valid else "",
                        "loading": bool(getattr(pg, "lx_loading", False)) if valid else False,
                        "opener": t.opener})
        return out

    def _close_tab_now(self, tab_id):
        from src.browser import page as page_mod
        last = self._browser is not None and len(self._browser.tabs) <= 1
        page_mod.close_tab(tab_id)
        if last:
            self._browser = None

    def _tab_of(self, page):
        b = self._browser
        t = b.tab_of_page(page) if b is not None and page is not None else None
        return t.id if t is not None else None

    def _on_tab_closed(self):
        """关掉的是当前标签：在途请求作用的就是那一页，立刻回，不等超时。"""
        self._clear_open()
        for req in list(self._pending):
            self._reply(req, {"ok": False, "error": "这个标签已关闭"})

    _ACTIVITY_LABELS = {"open": "打开页面", "js": "执行脚本", "frames": "列框架",
                        "settle": "等待页面", "close": "关闭浏览器",
                        "qturl": "读地址", "console": "读控制台", "grab": "截图",
                        "mouse": "点击", "wheel": "滚动", "tabs": "列标签", "tab_select": "切换标签",
                        "tab_close": "关闭标签", "tab_new": "新建标签"}
    _LX_LABELS = {"snapshot": "快照", "find": "查找", "click": "点击", "type": "输入",
                  "readText": "读正文", "console": "控制台", "info": "查元素", "state": "读状态",
                  "watchPrepare": "准备操作", "watchBaseline": "记样式", "watchReport": "核对样式",
                  "reveal": "滚到元素", "watchSettling": "等过渡结束",
                  "pointOf": "定位点击点", "pointArrived": "确认点击送达", "anchorOf": "定位输入框",
                  "describeAt": "看坐标落点", "scrollInfo": "定位滚动容器", "scrollState": "核对滚动位置",
                  "scrollSet": "滚到头", "hoverWatch": "准备悬停", "hoverArrived": "确认悬停送达",
                  "hasRef": "确认元素还在"}

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
        if op in ("mouse", "key", "drag") or (op == "lx" and args.get("method") in ("click", "type", "act")):
            # 模型的点击 / 输入：之后不久到达的新窗口请求算它发起的（脚本点击时
            # isUserInitiated 是 False，见 page._on_new_window）
            self._browser.note_model_action()
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
            elif op == "grab":
                self._grab(req, args)
            elif op == "mouse":
                self._mouse(req, args)
            elif op == "wheel":
                self._wheel(req, args)
            elif op == "key":
                self._key(req, args)
            elif op == "drag":
                self._drag(req, args)
            elif op == "cursor":
                # 输入中 / 被挡住：只挪画在网页上的指针，不发任何事件
                self.cursor_event.emit(self._browser.current_id, float(args.get("x", 0)),
                                       float(args.get("y", 0)), str(args.get("kind") or ""),
                                       str(args.get("label") or ""))
                self._reply(req, {"ok": True})
            elif op == "tabs":
                self._reply(req, {"ok": True, "tabs": self._tab_list(),
                                  "current": self._browser.current_id})
            elif op == "tab_select":
                if args.get("id") not in self._browser.tabs:
                    self._reply(req, {"ok": False, "error": f"没有标签 #{args.get('id')}"})
                else:
                    self._browser.set_current(args["id"])
                    self._reply(req, {"ok": True, "tabs": self._tab_list(),
                                      "current": self._browser.current_id})
            elif op == "tab_close":
                tid = args.get("id")
                if tid not in self._browser.tabs:
                    self._reply(req, {"ok": False, "error": f"没有标签 #{tid}"})
                else:
                    last = len(self._browser.tabs) <= 1
                    # 先回包再关：关当前标签会把在途请求（包括这一个）都回成「已关闭」
                    self._reply(req, {"ok": True, "last": last})
                    QTimer.singleShot(0, self, lambda: self._close_tab_now(tid))
            elif op == "tab_new":
                if len(self._browser.tabs) >= page.MAX_TABS:
                    self._reply(req, {"ok": False, "error": f"标签已开满 {page.MAX_TABS} 个"})
                else:
                    tab = self._browser.new_tab(make_current=True)
                    self._reply(req, {"ok": True, "id": tab.id})
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
            try:
                old.loadingChanged.disconnect(self._on_loading_changed)
            except (RuntimeError, TypeError):
                pass
        page.loadStarted.connect(self._on_load_started)
        page.loadFinished.connect(self._on_load_finished)
        page.loadingChanged.connect(self._on_loading_changed)
        self._wired_page = page
        self._page_loading = False

    def _on_load_started(self):
        self._page_loading = True
        self._load_fail = None
        if self._open_req is not None:
            self._open_started = True

    def _on_loading_changed(self, info):
        from PySide6.QtWebEngineCore import QWebEngineLoadingInfo
        if info.status() == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
            self._load_fail = {
                "http": info.errorDomain() == QWebEngineLoadingInfo.ErrorDomain.HttpStatusCodeDomain,
                "code": int(info.errorCode()),
            }
            req = self._open_req
            if req is not None and req.get("await_fail_info"):
                self._resolve_failed_open(req)

    def _on_load_finished(self, ok):
        self._page_loading = False
        req = self._open_req
        if req is None or req.get("cancelled") or req.get("replied"):
            return
        if not self._open_started:
            return                              # 上一个被停止导航的迟到 loadFinished，忽略
        if not ok:
            # loadFinished(false) 比 loadingChanged(LoadFailedStatus) 先到（实测），
            # 失败域还没记下来：等一下再判。HTTP 4xx/5xx 带内容的响应（Flask 调试页 /
            # Express Cannot GET）页面已经渲染出来了，不算"连不上"——模型要读得到
            # traceback（复审 #13）。域信息到了立刻判，150ms 还没到就按真失败报
            req["await_fail_info"] = True
            QTimer.singleShot(150, self, lambda: self._resolve_failed_open(req))
            return
        self._finish_open(req, ok=True, loaded=True, error=None)

    def _resolve_failed_open(self, req):
        if req is not self._open_req or req.get("cancelled") or req.get("replied"):
            return
        if self._load_fail and self._load_fail["http"]:
            self._finish_open(req, ok=True, loaded=True, error=None,
                              http_status=self._load_fail["code"])
        else:
            self._finish_open(req, ok=False, loaded=True, error="loadFinished(false)")

    def _open(self, req, args):
        from src.browser import page as page_mod
        # 下一轮事件循环再通知界面：别在 dispatch 中途重入面板控件
        QTimer.singleShot(0, page_mod._notifier, page_mod._notifier.model_opening.emit)
        page = self._browser.page
        self._abort_open("被新的导航取代")
        req["page"] = page
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
        # 先清缓存再导航：profile 已是 NoCache，但渲染进程里的 Blink 内存缓存仍会按
        # Last-Modified 推断"还新鲜"复用旧脚本——改完 app.js 重开同一地址、加 ?v=2、Reload
        # 都还跑旧代码（评测 012），只有 clearHttpCache（会一并清渲染进程内存缓存）管用。
        # 空白新页面里没有旧脚本，不用清（关过浏览器则由闸门的"标脏"兜住）；在途导航时不清。
        blank = page.url().isEmpty() or page.url().toString() == "about:blank"
        page_mod.load_url(page, QUrl(args["url"]),
                          clear_first=not blank, page_loading=getattr(page, "lx_loading", False),
                          still_wanted=lambda: not (req.get("cancelled") or req.get("replied")
                                                    or self._open_req is not req),
                          on_go=lambda was_loading: self._on_open_sent(req, was_loading))

    def _on_open_sent(self, req, was_loading):
        """导航发出去了。那一刻页面正在加载（用户在面板里点的链接、热更新整页刷新……）：
        新导航顶替在途的那个，Qt 不再发 loadStarted，接下来那个 loadFinished 就是这次的——
        以前它被当成「上一个导航的迟到信号」丢掉，open 卡满超时报失败，页面其实已经打开了。"""
        if was_loading and req is self._open_req and not req.get("replied"):
            self._open_started = True

    def _finish_open(self, req, *, ok, loaded, error, http_status=None):
        # 发起时的那一页：加载期间当前标签可能换了（页面自己开了新标签）
        page = req.get("page") or (self._browser.page if self._browser is not None else None)
        valid = page is not None and shiboken6.isValid(page)
        t0 = req.get("t0", time.perf_counter())
        self._clear_open()
        result = {
            "ok": ok, "loaded": loaded,
            "url": page.url().toString() if valid else "",
            "title": page.title() if valid else "",
            "loadMs": round((time.perf_counter() - t0) * 1000),
            "error": error,
            "nav": getattr(page, "nav_seq", 0) if valid else 0,
            "tab": self._tab_of(page),
        }
        if http_status is not None:
            result["httpStatus"] = http_status
        self._reply(req, result)

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
        if method in ("snapshot", "find", "describeAt"):
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
        # QWebEngineFrame.size() 是物理像素（乘过屏幕缩放），页面里量到的是 CSS 像素：
        # 除掉屏幕缩放和页面缩放再交出去。不除的话高 DPI 屏上（Windows 缩放 150%）
        # 尺寸永远对不上，iframe 位置算不出来，iframe 里的真实点击 / 拖拽 / 坐标点击 / 局部截图
        # 全部失效（用户实测；测试环境缩放是 1 倍，看不出来）
        try:
            dpr = float(self._browser.view.devicePixelRatioF()) or 1.0
        except Exception:
            dpr = 1.0
        scale = dpr * (self._browser.page.zoomFactor() or 1.0)

        def walk(frame, path):
            for i, child in enumerate(frame.children()):
                p = f"{path}.{i}"
                try:
                    url = child.url().toString()
                except Exception:
                    url = "?"
                try:
                    size = child.size()           # 视口里的大小：工具层按它把 iframe 对上号
                    w, h = float(size.width()) / scale, float(size.height()) / scale
                except Exception:
                    w = h = -1.0
                info.append({"path": p, "url": url, "w": w, "h": h})
                walk(child, p)

        walk(self._browser.page.mainFrame(), "f")
        self._reply(req, {"ok": True, "frames": info})

    def _mouse(self, req, args):
        """往页面发一次真实的鼠标点击（复审 #9）：先移过去（hover 菜单、pointerenter），
        再按下、抬起。坐标是页面的 CSS 像素，按缩放换成 view 的坐标。

        面板没显示 / 太小时 Chromium 不收输入事件，回 ok=False，工具层改走脚本点击。

        事件直接发给 view 的 focusProxy，**不动系统鼠标**：早先用 QTest.mouseMove 移过去，
        它会顺带 QCursor.setPos，把用户正在用的鼠标拽到页面上（用户实测）。

        键盘焦点：用户正在灵犀输入框里打字（见 page.note_user_typing）就不碰，点完万一被
        网页拿走也还回去；没在打字就像真点击那样交给网页。直接发的事件 Qt 不会自己挪焦点。"""
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        from src.browser import page as page_mod
        view = self._browser.view
        if not shiboken6.isValid(view) or not view.isVisible() or view.width() < 50:
            self._reply(req, {"ok": False, "error": "面板没显示，收不到鼠标事件"})
            return
        target = view.focusProxy() or view
        zoom = self._browser.page.zoomFactor() or 1.0
        pt = QPoint(int(args["x"] * zoom), int(args["y"] * zoom))
        if not target.rect().contains(pt):
            self._reply(req, {"ok": False, "error": "点击点不在可见区域里"})
            return
        if args.get("move_only"):
            # 悬停：只移动，不按下。真实的鼠标移动不改键盘焦点，这里也不碰
            QApplication.sendEvent(target, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(pt), QPointF(target.mapToGlobal(pt)),
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
            self.cursor_event.emit(self._browser.current_id, pt.x() / zoom, pt.y() / zoom,
                                   "move", str(args.get("label") or ""))
            self._reply(req, {"ok": True})
            return
        typing_in = QApplication.focusWidget() if page_mod.user_typing_recently() else None
        if typing_in is None:
            # 直接发的事件 Qt 不算真点击，不会「点哪焦点给哪」：没在打字就照真点击那样把
            # 焦点交给网页（有的页面靠 document 有焦点才处理键盘 / focus 事件）
            target.setFocus(Qt.FocusReason.MouseFocusReason)
        local, glob = QPointF(pt), QPointF(target.mapToGlobal(pt))
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        for etype, button, buttons in ((QEvent.Type.MouseMove, none, none),
                                       (QEvent.Type.MouseButtonPress, left, left),
                                       (QEvent.Type.MouseButtonRelease, left, none)):
            QApplication.sendEvent(target, QMouseEvent(etype, local, glob, button, buttons,
                                                       Qt.KeyboardModifier.NoModifier))
        # 画在网页上的「灵犀的鼠标」：滑过去 + 点一下的涟漪（只是给人看，不碍事件）
        self.cursor_event.emit(self._browser.current_id, pt.x() / zoom, pt.y() / zoom,
                               "click", str(args.get("label") or ""))
        if typing_in is not None and shiboken6.isValid(typing_in) and typing_in is not target \
                and not view.isAncestorOf(typing_in):
            typing_in.setFocus(Qt.FocusReason.OtherFocusReason)
        self._reply(req, {"ok": True})

    def _input_target(self, req):
        """真实输入事件发给谁：view 的 focusProxy。面板没显示 / 太小时 Chromium 不收，回 None。"""
        view = self._browser.view
        if not shiboken6.isValid(view) or not view.isVisible() or view.width() < 50:
            self._reply(req, {"ok": False, "error": "面板没显示，收不到键盘 / 鼠标事件"})
            return None
        return view.focusProxy() or view

    def _take_focus(self, target):
        """同 _mouse：用户没在灵犀输入框里打字才把焦点交给网页；返回要还回去的控件。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        from src.browser import page as page_mod
        typing_in = QApplication.focusWidget() if page_mod.user_typing_recently() else None
        if typing_in is None:
            target.setFocus(Qt.FocusReason.MouseFocusReason)
        return typing_in

    def _give_back_focus(self, typing_in, target):
        from PySide6.QtCore import Qt
        view = self._browser.view
        if typing_in is not None and shiboken6.isValid(typing_in) and typing_in is not target \
                and not view.isAncestorOf(typing_in):
            typing_in.setFocus(Qt.FocusReason.OtherFocusReason)

    def _key(self, req, args):
        """往页面发一次真实按键（按下 + 抬起），送给页面里有焦点的地方。key 写法见
        page.parse_key（Escape / Enter / ArrowDown / Tab / a / Control+a …）。"""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QApplication
        from src.browser import page as page_mod
        parsed = page_mod.parse_key(str(args.get("key") or ""))
        if parsed is None:
            self._reply(req, {"ok": False, "error": f"不认识的按键: {args.get('key')!r}"})
            return
        target = self._input_target(req)
        if target is None:
            return
        qt_key, mods, text = parsed
        typing_in = self._take_focus(target)
        for etype in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(target, QKeyEvent(etype, qt_key, mods, text))
        self._give_back_focus(typing_in, target)
        self._reply(req, {"ok": True})

    def _drag(self, req, args):
        """真实鼠标拖拽：移到起点、按下、分几步移到终点（每步之间让页面处理一下 pointermove）、
        抬起。坐标是页面 CSS 像素（顶层视口），同 _mouse 按缩放换算、不动系统鼠标。"""
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QApplication
        target = self._input_target(req)
        if target is None:
            return
        zoom = self._browser.page.zoomFactor() or 1.0
        p1 = QPoint(int(args["x1"] * zoom), int(args["y1"] * zoom))
        p2 = QPoint(int(args["x2"] * zoom), int(args["y2"] * zoom))
        if not (target.rect().contains(p1) and target.rect().contains(p2)):
            self._reply(req, {"ok": False, "error": "起点或终点不在可见区域里"})
            return
        steps = max(2, min(int(args.get("steps") or 12), 60))
        typing_in = self._take_focus(target)
        left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
        tab = self._browser.current_id

        def send(etype, pt, button, buttons):
            QApplication.sendEvent(target, QMouseEvent(
                etype, QPointF(pt), QPointF(target.mapToGlobal(pt)), button, buttons,
                Qt.KeyboardModifier.NoModifier))

        send(QEvent.Type.MouseMove, p1, none, none)
        send(QEvent.Type.MouseButtonPress, p1, left, left)
        done = [0]

        def step():
            if req.get("cancelled") or not shiboken6.isValid(target):
                self._reply(req, {"ok": False, "error": "拖到一半页面没了"})
                return
            done[0] += 1
            t = done[0] / steps
            pt = QPoint(round(p1.x() + (p2.x() - p1.x()) * t), round(p1.y() + (p2.y() - p1.y()) * t))
            send(QEvent.Type.MouseMove, pt, none, left)
            self.cursor_event.emit(tab, pt.x() / zoom, pt.y() / zoom, "move",
                                   str(args.get("label") or ""))
            if done[0] < steps:
                QTimer.singleShot(16, self, step)
                return
            send(QEvent.Type.MouseButtonRelease, p2, left, none)
            self._give_back_focus(typing_in, target)
            self._reply(req, {"ok": True})

        QTimer.singleShot(16, self, step)

    def _wheel(self, req, args):
        """往页面发一次真实滚轮事件（滚动加载 / 虚拟列表 / 轮播常听 wheel，scrollBy 触发不了）。
        dx / dy 是想让内容滚动的像素（正 = 向右 / 向下），落点是页面 CSS 坐标（顶层视口），
        同 _mouse 按缩放换算、不动系统鼠标、不碰键盘焦点。"""
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QApplication
        target = self._input_target(req)
        if target is None:
            return
        zoom = self._browser.page.zoomFactor() or 1.0
        pt = QPoint(int(args["x"] * zoom), int(args["y"] * zoom))
        if not target.rect().contains(pt):
            self._reply(req, {"ok": False, "error": "滚动落点不在可见区域里"})
            return
        dx, dy = float(args.get("dx") or 0), float(args.get("dy") or 0)
        # 实测（offscreen QtWebEngine）：ScrollUpdate 相位的事件根本到不了页面，要用
        # NoScrollPhase；页面收到的 deltaY = -angleDelta.y / 2（pixelDelta 被忽略），
        # 所以想让内容滚 N 像素就发 angleDelta = -2N。Qt 的正方向是「内容向上 / 向左滚」
        angle = QPoint(round(-2 * dx), round(-2 * dy))
        QApplication.sendEvent(target, QWheelEvent(
            QPointF(pt), QPointF(target.mapToGlobal(pt)), QPoint(0, 0), angle,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))
        self.cursor_event.emit(self._browser.current_id, pt.x() / zoom, pt.y() / zoom,
                               "move", str(args.get("label") or ""))
        self._reply(req, {"ok": True})

    def _grab(self, req, args):
        """截页面当前可见区域（view.grab），PNG base64。宽度超过 max_width 等比缩小。
        view 不显示（面板收起 / 尺寸为零）时截出来是空白，直接说清楚，不交一张白图去识别。"""
        import base64

        from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
        view = self._browser.view
        if view is None or not shiboken6.isValid(view) or not view.isVisible() \
                or view.width() < 50 or view.height() < 50:
            self._reply(req, {"ok": False, "error": "浏览器面板没显示，截不到页面（打开右侧的浏览器面板再截）"})
            return
        pix = view.grab()
        if pix.isNull():
            self._reply(req, {"ok": False, "error": "截图失败（拿到空图）"})
            return
        max_w = int(args.get("max_width") or 1280)
        img = pix.toImage()
        crop = args.get("crop")
        # 图上像素 → 页面 CSS 坐标的换算（locate 用）：记下这张图左上角对应的页面坐标
        # 和它覆盖的 CSS 宽度，缩放 / 裁剪都算进去
        zoom = self._browser.page.zoomFactor() or 1.0
        css_x = css_y = 0.0
        css_w = pix.deviceIndependentSize().width() / zoom
        if crop:
            # 只截一块（局部截图）：crop 是页面 CSS 像素，换成图上的物理像素；小块放大一些，
            # 识别模型看小字更清楚
            from PySide6.QtCore import QRect
            scale = zoom * (pix.devicePixelRatio() or 1.0)
            rect = QRect(int(crop["x"] * scale), int(crop["y"] * scale),
                         int(crop["w"] * scale), int(crop["h"] * scale)).intersected(img.rect())
            if rect.width() < 8 or rect.height() < 8:
                self._reply(req, {"ok": False, "error": "要截的那块不在可见区域里"})
                return
            css_x, css_y, css_w = rect.left() / scale, rect.top() / scale, rect.width() / scale
            img = img.copy(rect)
            if img.width() < 640:
                img = img.scaledToWidth(min(img.width() * 3, 960),
                                        Qt.TransformationMode.SmoothTransformation)
        if img.width() > max_w:
            img = img.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        self._reply(req, {"ok": True, "png": base64.b64encode(bytes(data)).decode("ascii"),
                          "width": img.width(), "height": img.height(),
                          "cssX": css_x, "cssY": css_y, "cssPerPx": css_w / img.width(),
                          "url": self._browser.page.url().toString()})

    def _qturl(self, req, frame):
        """页面/frame 的真实 URL（Qt 侧）。闸门与围栏不信页面自报的 URL。"""
        target = self._browser.page
        if frame:
            target = self._frame_by_path(frame)
            if target is None:
                self._reply(req, {"ok": False, "error": "frame 不存在 " + frame})
                return
        # loading / nav：动作之后判断「点击已经触发了导航、新页面还没到」（复审 #5）
        page = self._browser.page
        self._reply(req, {"ok": True, "url": target.url().toString(),
                          "loading": bool(getattr(page, "lx_loading", False)),
                          "tab": self._browser.current_id,
                          "nav": getattr(page, "nav_seq", 0)})

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
                              "url": b.page.url().toString(), "tab": b.current_id})

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


def notify_tab_closed():
    """page.close_tab() 关掉当前标签时调（主线程）。"""
    if _bridge is not None:
        _bridge._on_tab_closed()


def notify_closed():
    """page.close_browser() 调：在途请求立刻回"浏览器已关闭"（主线程）。"""
    if _bridge is not None:
        _bridge._on_browser_closed()