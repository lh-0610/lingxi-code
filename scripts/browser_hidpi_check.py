"""在 Windows 缩放 150% 的条件下（QT_SCALE_FACTOR=1.5）开 iframe 小游戏，确认 iframe 位置算得出、
iframe 里的真实点击点得中。由 scripts/test_browser_hidpi.py 起子进程跑（同一个进程里改不了缩放）。

QWebEngineFrame.size() 给的是物理像素（乘过缩放），页面里量到的是 CSS 像素；bridge 不换算的话
高 DPI 屏上 iframe 对不上号，iframe 里的真实点击 / 拖拽 / 坐标点击 / 局部截图全部失效（用户实测）。
输出最后一行 OK 或 FAIL: 原因。
"""
import functools
import http.server
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
app = QApplication.instance() or QApplication([])

import src.mcp_client as _mcp  # noqa: E402
_mcp.init_mcp = lambda: []
from src import permission_modes as pm, session as _session, state  # noqa: E402
from src import tools_browser as tb  # noqa: E402
from src.browser import page as page_mod, security as sec  # noqa: E402

handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                            directory=os.path.join(ROOT, "scripts", "browser_fixtures"))
handler.log_message = lambda *a: None
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def run(fn, *a, **k):
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn(*a, **k)), daemon=True)
    t.start()
    end = time.time() + 60
    while t.is_alive() and time.time() < end:
        app.processEvents()
        time.sleep(0.005)
    return box.get("r")


class _UI:
    def confirm_command(self, *a, **k):
        return True, ""


sess = _session.current_session()
pm.apply(sess, "manual")
sess.browser_origin_allowlist.add(sec.parse_origin(base))
state.ui_ref = _UI()
run(tb.browser_open.func, base + "/game.html")
view = page_mod.get_browser().view
view.resize(800, 600)
view.show()
view.activateWindow()
view.setFocus()
end = time.time() + 1.5
while time.time() < end:
    app.processEvents()
    time.sleep(0.01)
print("dpr", view.devicePixelRatioF())
off = run(tb._frame_offset, "f.0")
print("frame offset", off)
if off is None:
    print("FAIL: iframe 位置算不出来")
    sys.exit(1)
snap = run(tb.browser_snapshot.func, frame="f.0", mode="all")
import re  # noqa: E402
ref = re.search(r'"关闭广告"[^\n]*\[ref=(e\d+)\]', snap or "")
out = run(tb.browser_click.func, ref.group(1), frame="f.0") if ref else "没有 ref"
text = run(tb.browser_read_text.func, frame="f.0") or ""
print("click:", (out or "")[:80].replace("\n", " / "))
print("OK" if "pointer" in text else "FAIL: iframe 里的真实点击没点中（退回了脚本点击？）")
