"""012：在浏览器里打开 index.html，控制台不能有报错，列表要真的渲染出三件商品。

独立于灵犀自己的浏览器代码（不经过 tree.js / bridge / tools_browser）：直接用
QWebEnginePage 加载、自己收控制台消息。判定的是"页面在真实浏览器内核里到底对不对"，
不是被测代码说它对不对。

只看"没报错"不够：把 render(...) 那行删掉同样没报错，但列表是空的。所以还要读 DOM，
三件商品都在、计数写着 3 才算修好。

在评测结果目录里跑（cwd = 工作区）：python "{checks}/browser_page_ok.py"
"""
import functools
import http.server
import json
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

EXPECTED = ["苹果", "香蕉", "橙子"]
LOAD_TIMEOUT_MS = 15000
SETTLE_MS = 1500


class Page(QWebEnginePage):
    def __init__(self, profile):
        super().__init__(profile)
        self.errors = []

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append(f"{message}（{os.path.basename(source)}:{line}）")


def wait(ms, until=None):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    if until is not None:
        until.connect(loop.quit)
    loop.exec()


def finish(code, lines):
    for line in lines:
        print(line)
    sys.stdout.flush()
    # 不走 Qt / WebEngine 的析构：offscreen 下退出阶段销毁 profile 偶发段错误（退出码 139），
    # 会把一次通过的检查判成失败。结论已经打印完，直接结束进程。
    os._exit(code)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    handler = functools.partial(_QuietHandler, directory=os.getcwd())
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"

    profile = QWebEngineProfile(app)            # 不落盘的独立配置，不碰用户数据
    page = Page(profile)
    loaded = {}
    page.loadFinished.connect(lambda ok: loaded.setdefault("ok", ok))
    page.load(QUrl(url))
    wait(LOAD_TIMEOUT_MS, page.loadFinished)
    if not loaded.get("ok"):
        finish(1, [f"页面没加载成功：{url}"])
    wait(SETTLE_MS)                             # 给脚本跑完、异步报错冒出来留时间

    result = {}
    script = ("JSON.stringify({items: [...document.querySelectorAll('#list li')]"
              ".map(li => li.textContent.trim()),"
              " count: (document.getElementById('count') || {}).textContent || ''})")
    page.runJavaScript(script, 0, lambda v: result.setdefault("v", v))
    for _ in range(50):
        if "v" in result:
            break
        wait(100)
    state = json.loads(result.get("v") or "{}")
    items = state.get("items") or []
    count = state.get("count") or ""

    problems = []
    if page.errors:
        problems.append("控制台有报错：" + "；".join(page.errors[:5]))
    missing = [name for name in EXPECTED if not any(name in item for item in items)]
    if missing or len(items) != len(EXPECTED):
        problems.append(f"列表不对：期望 {EXPECTED}，实际 {items}")
    if "3" not in count:
        problems.append(f"计数不对：{count!r}")
    if problems:
        finish(1, problems)
    finish(0, [f"ok：{items}，{count}"])


main()
