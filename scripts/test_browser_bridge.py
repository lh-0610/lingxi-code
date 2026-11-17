"""浏览器 page/bridge/tree.js 的 offscreen 集成实测（§11.3）。

本机双端口 HTTP 服务器供 fixture（不同端口=跨源，不依赖外网），worker 线程调 bridge.call、
主线程泵事件循环驱动 WebEngine。覆盖：快照/ info 结构判定 / 点击 / 输入 / console /
跨源 iframe / 页脚折叠 / 收起 select / diff / 超时 / 停止按钮响应 / 关闭重开。

只有装不上 WebEngine 的环境才 skip（CI 已装齐 PySide6_Addons，不允许在 CI skip）。
"""
import http.server
import importlib.util
import os
import threading
import time

import pytest

if importlib.util.find_spec("PySide6.QtWebEngineWidgets") is None:
    pytest.skip("PySide6_Addons 未安装（CI 应装齐，不 skip）", allow_module_level=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

from src import state  # noqa: E402
from src.browser import bridge as bridge_mod  # noqa: E402
from src.browser import page as page_mod  # noqa: E402
from src.browser import security as sec  # noqa: E402
from src.browser import tree  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "browser_fixtures")


@pytest.fixture(scope="module")
def qapp():
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    yield app


class _Handler(http.server.SimpleHTTPRequestHandler):
    cross_base = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=FIXTURES, **kw)

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(8)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>slow</body></html>")
            return
        if self.path.startswith("/setcookie"):
            body = b"<html><body>cookie set</body></html>"
            self.send_response(200)
            self.send_header("Set-Cookie", "lx_probe=1; Path=/; Max-Age=3600")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/frames.html"):
            with open(os.path.join(FIXTURES, "frames.html"), encoding="utf-8") as f:
                html = f.read().replace("__CROSS_ORIGIN__", self.cross_base)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def servers(qapp):
    srv_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    base_a = f"http://127.0.0.1:{srv_a.server_address[1]}"
    base_b = f"http://127.0.0.1:{srv_b.server_address[1]}"
    _Handler.cross_base = base_b
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    threading.Thread(target=srv_b.serve_forever, daemon=True).start()
    yield base_a, base_b
    srv_a.shutdown()
    srv_b.shutdown()


def _call(app, op, timeout=30, **args):
    """worker 调 bridge.call，主线程泵事件直到返回（call 不能在主线程直接调，会死锁）。"""
    b = bridge_mod.get_bridge()
    box = {}

    def worker():
        try:
            box["r"] = b.call(op, timeout=timeout, **args)
        except Exception as e:
            box["r"] = {"ok": False, "error": f"worker 异常: {e}"}

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.time() + timeout + 15
    while t.is_alive() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    t.join(timeout=2)
    return box.get("r", {"ok": False, "error": "测试等待超时"})


@pytest.fixture()
def browser(qapp, servers, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    state.stop_flag = False
    yield qapp
    state.stop_flag = False
    _call(qapp, "close", timeout=10)


def _ref_of(tree_text, name):
    import re
    for line in tree_text.splitlines():
        if f'"{name}"' in line:
            m = re.search(r"\[ref=(e\d+)\]", line)
            if m:
                return m.group(1)
    return None


def test_open_and_snapshot(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r["ok"] is True, r
    assert r["loaded"] is True
    assert "表单页" in r["title"]
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    assert s["ok"] is True
    assert "保存" in s["tree"]
    assert "隐藏按钮" not in s["tree"]          # display:none 剔除
    assert s["stats"]["refs"] > 0


def test_info_structural_consequence(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r["ok"] is True, r
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    assert s.get("ok") is True, s
    submit_ref = _ref_of(s["tree"], "保存")
    info = _call(browser, "lx", method="info", args={"ref": submit_ref})
    assert info["ok"] is True
    assert info["tag"] == "button" and info["type"] == "submit"
    assert info["inForm"] is True
    # fixture 表单是 GET：新口径下 GET 提交按普通点击；POST 才算有后果
    assert sec.is_consequential(info) is False
    assert sec.is_consequential(dict(info, formMethod="post")) is True

    add_ref = _ref_of(s["tree"], "添加一行")
    info2 = _call(browser, "lx", method="info", args={"ref": add_ref})
    assert info2["type"] == "button"
    assert sec.is_consequential(info2) is False


def test_click_and_type(browser, servers):
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/form.html", timeout=20)
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    add_ref = _ref_of(s["tree"], "添加一行")
    r = _call(browser, "lx", method="click", args={"ref": add_ref})
    assert r["ok"] is True
    n = _call(browser, "js", code="document.querySelectorAll('#list li').length")
    assert n["value"] == 2

    name_ref = _ref_of(s["tree"], "用户名")
    r = _call(browser, "lx", method="type", args={"ref": name_ref, "text": "灵犀"})
    assert r["ok"] is True and r["value"] == "灵犀"


def test_delta_after_click(browser, servers):
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/form.html", timeout=20)
    before = _call(browser, "lx", method="snapshot", args={"mode": "all"})["tree"]
    add_ref = _ref_of(before, "添加一行")
    _call(browser, "lx", method="click", args={"ref": add_ref})
    after = _call(browser, "lx", method="snapshot", args={"mode": "all"})["tree"]
    delta = tree.compute_delta(before, after)
    assert "新行" in delta
    assert len(delta) < len(after)              # diff 远小于全量


def test_collapsed_select(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r["ok"] is True, r
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    assert s.get("ok") is True, s
    assert "上海" in s["tree"]                   # 选中项保留
    assert "北京" not in s["tree"]               # 未选中项收起
    assert "还有 2 个选项" in s["tree"]


def test_footer_link_folding(browser, servers):
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/footer.html", timeout=20)
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    assert "正文按钮" in s["tree"]
    assert "还有 4 个链接" in s["tree"]          # 12 链接留 8 折 4
    assert s["tree"].count("- link") == 8


def test_shadow_dom(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/shadow.html", timeout=20)
    assert r["ok"] is True, r
    s = _call(browser, "lx", method="snapshot", args={"mode": "all"})
    assert s.get("ok") is True, s
    assert "影子里的按钮" in s["tree"]           # open shadow 穿得进
    assert "封闭影子里的按钮" not in s["tree"]    # closed shadow 拿不到
    ref = _ref_of(s["tree"], "影子里的按钮")
    r = _call(browser, "lx", method="click", args={"ref": ref})
    assert r["ok"] is True
    light = _call(browser, "js", code="document.getElementById('light').textContent")
    assert light["value"] == "影子按钮被点了"


def test_cross_origin_iframe(browser, servers):
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/frames.html", timeout=20)
    _call(browser, "settle", ms=1500)
    fr = _call(browser, "frames")
    assert fr["ok"] is True
    paths = [f["path"] for f in fr["frames"]]
    assert "f.0" in paths and "f.1" in paths
    # 跨源 iframe（第二个端口）也能取快照
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"}, frame="f.1")
    assert s["ok"] is True, s
    assert "保存" in s["tree"]


def test_console_capture(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/errors.html", timeout=20)
    assert r["ok"] is True, r
    _call(browser, "settle", ms=800)
    r = _call(browser, "console")
    assert r["ok"] is True, r
    texts = " ".join(e["text"] for e in r["entries"])
    assert "TypeError" in texts or "boom" in texts
    assert len(r["entries"]) >= 2


def test_isolated_world_page_cannot_see_lx(browser, servers):
    """注入在隔离世界：桥的 js/lx 走隔离世界（页面 MainWorld 看不到 __lx，
    MainWorld 不可见性由 test_browser_gate_real 的 DOM 记录页断言）。"""
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/form.html", timeout=20)
    main = _call(browser, "js", code="typeof window.__lx")
    assert main["value"] == "object", main
    iso = _call(browser, "lx", method="state")
    assert iso["ok"] is True and iso.get("docId")


def test_load_timeout_then_recover(browser, servers):
    base_a, _ = servers
    r = _call(browser, "open", url=base_a + "/slow", timeout=2)
    assert r["ok"] is False
    assert "超时" in r.get("error", "")
    assert r["wallMs"] < 5000
    # 超时后桥必须还能用
    r2 = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r2["ok"] is True, r2
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    assert s["ok"] is True


def test_stop_flag_responsive(browser, servers):
    base_a, _ = servers
    state.stop_flag = True
    t0 = time.perf_counter()
    r = _call(browser, "open", url=base_a + "/slow", timeout=20)
    elapsed = (time.perf_counter() - t0) * 1000
    assert r["ok"] is False
    assert "已停止" in r.get("error", "")
    assert elapsed < 3000                        # 立刻返回，不傻等 8s
    state.stop_flag = False
    r2 = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r2["ok"] is True, r2


def test_close_and_reopen(browser, servers):
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert page_mod.is_browser_open() is True
    r = _call(browser, "close", timeout=10)
    assert r["ok"] is True
    assert page_mod.is_browser_open() is False
    r2 = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r2["ok"] is True
    assert page_mod.is_browser_open() is True


def test_close_clears_cookies(browser, servers):
    base_a, _ = servers
    # 先泵掉上一用例 close 遗留的异步 deleteAllCookies，避免它落在本用例设 cookie 之后
    deadline = time.time() + 0.3
    while time.time() < deadline:
        browser.processEvents()
        time.sleep(0.01)
    r = _call(browser, "open", url=base_a + "/setcookie", timeout=20)
    assert r["ok"] is True, r
    # Set-Cookie 落库是异步的：等 cookie 出现再断言（前置条件），不裸读
    deadline = time.time() + 2.0
    c = {}
    while time.time() < deadline:
        c = _call(browser, "js", code="document.cookie")
        if "lx_probe=1" in (c.get("value") or ""):
            break
        browser.processEvents()
        time.sleep(0.01)
    assert "lx_probe=1" in c["value"], c
    _call(browser, "close", timeout=10)
    # deleteAllCookies 是异步的，泵事件等它完成（不用 settle——那会提前重建浏览器）
    deadline = time.time() + 1.0
    while time.time() < deadline:
        browser.processEvents()
        time.sleep(0.01)
    r = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r["ok"] is True, r
    c2 = _call(browser, "js", code="document.cookie")
    assert "lx_probe" not in c2["value"]


def test_close_while_open_pending_returns_closed_and_recovers(browser, servers):
    """面板关标签时模型正在加载：在途请求立刻回"浏览器已关闭"，不傻等超时；之后 open 能重建。"""
    base_a, _ = servers
    b = bridge_mod.get_bridge()
    box = {}

    def worker():
        box["r"] = b.call("open", timeout=20, url=base_a + "/slow")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and not b._open_started:
        browser.processEvents()
        time.sleep(0.005)
    assert b._open_started, "导航应已开始"
    t0 = time.perf_counter()
    page_mod.close_browser()          # 模拟面板关标签释放页面
    browser.processEvents()
    t.join(timeout=5)
    elapsed = (time.perf_counter() - t0) * 1000
    r = box.get("r")
    assert r and r["ok"] is False and "浏览器已关闭" in r.get("error", ""), r
    assert elapsed < 3000, f"应立刻返回，实际 {elapsed:.0f}ms"
    r2 = _call(browser, "open", url=base_a + "/form.html", timeout=20)
    assert r2["ok"] is True, r2


def test_close_while_settle_pending_returns_closed(browser, servers):
    """等回调的请求（settle）同样立刻回"浏览器已关闭"。"""
    b = bridge_mod.get_bridge()
    box = {}

    def worker():
        box["r"] = b.call("settle", timeout=20, ms=8000)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and not b._pending:
        browser.processEvents()
        time.sleep(0.005)
    assert b._pending, "settle 应已登记在途"
    t0 = time.perf_counter()
    page_mod.close_browser()
    browser.processEvents()
    t.join(timeout=5)
    elapsed = (time.perf_counter() - t0) * 1000
    r = box.get("r")
    assert r and r["ok"] is False and "浏览器已关闭" in r.get("error", ""), r
    assert elapsed < 3000, f"应立刻返回，实际 {elapsed:.0f}ms"


def test_activity_signal_tracks_dispatch(browser, servers):
    """面板徽标靠 activity 信号：dispatch 发动作名，回包发空串。"""
    base_a, _ = servers
    b = bridge_mod.get_bridge()
    seen = []
    b.activity.connect(lambda t: seen.append(t))
    try:
        _call(browser, "open", url=base_a + "/form.html", timeout=20)
        _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
        assert "打开页面" in seen and "快照" in seen
        assert seen[-1] == ""
    finally:
        b.activity.disconnect()


# ── 审后修复回归（ref 稳定 / 折叠 find / 默认 submit / frame 门控）：断言页面实际效果 ──

def _out(browser):
    r = _call(browser, "js", code="document.getElementById('out').textContent")
    return r.get("value")


def _tool(app, fn, *a, **kw):
    """工具函数须在 worker 线程调（bridge.call 主线程直调会死锁），主线程泵事件。"""
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("r", fn(*a, **kw)), daemon=True)
    t.start()
    deadline = time.time() + 40
    while t.is_alive() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.005)
    t.join(timeout=2)
    return box.get("r")


def test_root_ref_refs_stable_and_click_hits_right_element(browser, servers):
    """root_ref 子树快照的 ref，经 all 快照（diff 用）后仍指向原元素；页面实际点中 Y。"""
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/ref.html", timeout=20)
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    region = _ref_of(s["tree"], "二区")
    assert region
    sub = _call(browser, "lx", method="snapshot",
                args={"mode": "interactive", "rootRef": region})
    ref_y = _ref_of(sub["tree"], "Y")
    assert ref_y
    _call(browser, "lx", method="snapshot", args={"mode": "all"})   # diff 前置快照
    r = _call(browser, "lx", method="click", args={"ref": ref_y})
    assert r["ok"] is True and r["name"] == "Y", r
    assert _out(browser) == "点了:Y", "页面实际点中的必须是 Y，不是甲"
    _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    r2 = _call(browser, "lx", method="click", args={"ref": ref_y})
    assert r2["ok"] is True, r2
    assert _out(browser) == "点了:Y", "ref 跨多次快照稳定"


def test_find_folded_link_then_click_hits(browser, servers):
    """被折叠的页脚链接：find 拿 ref → all 快照 → 点击，页面实际点中链接11。"""
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/ref.html", timeout=20)
    f = _call(browser, "lx", method="find", args={"text": "链接11"})
    assert f["ok"] is True and f["matches"], f
    ref_l = f["matches"][0]["ref"]
    _call(browser, "lx", method="snapshot", args={"mode": "all"})
    r = _call(browser, "lx", method="click", args={"ref": ref_l})
    assert r["ok"] is True, r
    assert _out(browser) == "点了:链接11"


def test_submit_button_without_type_consequential_and_real_submit(browser, servers):
    """没写 type 的 <button> 在 POST 表单里：info 读 submit、判有后果、点击真触发提交。"""
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/submit.html", timeout=20)
    f = _call(browser, "lx", method="find", args={"text": "保存"})
    ref_s = f["matches"][0]["ref"]
    info = _call(browser, "lx", method="info", args={"ref": ref_s})
    assert info["ok"] is True
    assert info["tag"] == "button" and info["type"] == "submit", info
    assert info["inForm"] is True and info["formMethod"] == "post"
    assert sec.is_consequential(info) is True
    r = _call(browser, "lx", method="click", args={"ref": ref_s})
    assert r["ok"] is True, r
    assert _out(browser) == "提交了", "页面实际发生了表单提交"


def test_detached_element_ref_pruned(browser, servers):
    """脱离页面的元素 ref 被剔除：点击报"已脱离页面"，不复用指向别的元素。"""
    base_a, _ = servers
    _call(browser, "open", url=base_a + "/ref.html", timeout=20)
    s = _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    ref_jia = _ref_of(s["tree"], "甲")
    _call(browser, "js", code="document.querySelector('header button').remove()")
    _call(browser, "lx", method="snapshot", args={"mode": "interactive"})
    r = _call(browser, "lx", method="click", args={"ref": ref_jia})
    assert r["ok"] is False, r
    assert "脱离" in r.get("error", "") or "不存在" in r.get("error", ""), r
    assert _out(browser) == "未点击", "失效 ref 不该点中别的元素"


def test_click_in_cross_origin_iframe_asks_even_when_top_trusted(browser, servers, monkeypatch):
    """顶层 localhost 已信任、跨源 iframe 来源不在白名单：manual 下点击 iframe 内元素仍要问，
    确认卡带 iframe URL；批准后页面实际在 iframe 里加行。"""
    from src import permission_modes as pm
    from src import session as _session
    from src import state as _state
    from src import tools_browser as tb
    base_a, base_b = servers
    sess = _session.current_session()
    old_mode = pm.current(sess)
    old_allow = set(sess.browser_origin_allowlist)
    pm.apply(sess, "manual")
    sess.browser_origin_allowlist.add(sec.parse_origin(base_a))
    asked = []

    class _UI:
        def confirm_command(self, text, **kw):
            asked.append((text, kw))
            return True, ""

    monkeypatch.setattr(_state, "ui_ref", _UI())
    try:
        r = _tool(browser, tb.browser_open.func, base_a + "/frames.html")
        assert "已打开" in r and not asked
        _call(browser, "settle", ms=1500)
        fr = _call(browser, "frames")
        cross = [f["path"] for f in fr["frames"] if f["url"].startswith(base_b)]
        assert cross, fr
        fp = cross[0]
        snap = _tool(browser, tb.browser_snapshot.func, frame=fp)
        ref = _ref_of(snap, "添加一行")
        assert ref
        out = _tool(browser, tb.browser_click.func, ref, frame=fp)
        assert len(asked) == 1, "顶层 trusted 不能替 iframe 来源免确认"
        assert "iframe:" in asked[0][0] and base_b in asked[0][0]
        assert asked[0][1]["browser_origin"] == sec.parse_origin(base_b)
        assert "已点击" in out, out
        after = _call(browser, "lx", method="snapshot", args={"mode": "all"}, frame=fp)
        assert "新行" in after["tree"], "页面实际效果：iframe 内加了一行"
    finally:
        pm.apply(sess, old_mode)
        sess.browser_origin_allowlist.clear()
        sess.browser_origin_allowlist.update(old_allow)


def test_offscreen_close_exit_code_zero():
    """offscreen 子进程：建浏览器→关闭→自然退出，退出码必须为 0（§11.6 profile 常驻回归）。"""
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from src.browser import page as page_mod\n"
        "b = page_mod.get_browser()\n"
        "b.view.load('about:blank')\n"
        "for _ in range(200):\n"
        "    app.processEvents()\n"
        "page_mod.close_browser()\n"
        "for _ in range(200):\n"
        "    app.processEvents()\n"
        "print('closed', flush=True)\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu",
               NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    assert r.returncode == 0, (
        f"exit={r.returncode}\nstdout={r.stdout[-500:]!r}\nstderr={r.stderr[-1500:]!r}")

# ── 评测 012 暴露的两个调试闭环问题：改完文件重开要用新代码；控制台只看这次加载 ──────────

@pytest.fixture()
def editable_site(tmp_path):
    """可改写的本机站点。app.js 的修改时间设成一年前——真实项目里的文件大多是这样，
    Chromium 按 Last-Modified 推断"还新鲜"，正是这种情况下才会复用缓存里的旧文件。"""
    import functools

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    (tmp_path / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>t</title><p id="out">-</p>'
        '<script src="app.js"></script>', encoding="utf-8")
    app_js = tmp_path / "app.js"
    app_js.write_text("document.getElementById('out').textContent = 'v1';\n"
                      "throw new Error('旧版本的报错');\n", encoding="utf-8")
    old = time.time() - 365 * 24 * 3600
    os.utime(app_js, (old, old))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app_js
    srv.shutdown()
    srv.server_close()


def test_edited_script_is_used_when_the_page_is_opened_again(browser, editable_site):
    """改完 app.js 再打开同一个地址，执行的必须是新代码（评测 012：模型改对了却一直看到旧报错，
    关掉浏览器清了缓存才绕过去）。"""
    base, app_js = editable_site
    assert _call(browser, "open", url=base + "/index.html", timeout=20)["ok"]
    assert _out(browser) == "v1"
    app_js.write_text("document.getElementById('out').textContent = 'v2';\n", encoding="utf-8")
    r = _call(browser, "open", url=base + "/index.html", timeout=20)
    assert r["ok"]
    assert r["wallMs"] < 1500, f"清缓存应等到完成信号就导航，不是吃满 2 秒兜底：{r['wallMs']}ms"
    assert _out(browser) == "v2", "重新打开后执行的还是缓存里的旧 app.js"
    app_js.write_text("document.getElementById('out').textContent = 'v3';\n", encoding="utf-8")
    old = time.time() - 365 * 24 * 3600
    os.utime(app_js, (old, old))
    assert _call(browser, "open", url=base + "/index.html?v=3", timeout=20)["ok"]
    assert _out(browser) == "v3", "换个查询参数打开，脚本还是旧的"


def test_console_shows_only_the_latest_page_load(browser, editable_site):
    """同一个页面打开两次，控制台默认只给这次加载的报错，并说明之前还有几条；
    include_previous=True 才把之前的也列出来。"""
    from src import tools_browser as tb
    base, _ = editable_site
    for _ in range(2):
        assert _call(browser, "open", url=base + "/index.html", timeout=20)["ok"]
    latest = _tool(browser, tb.browser_console.invoke, {"level": "error"})
    assert latest.count("旧版本的报错") == 1, latest
    assert "之前" in latest and "include_previous" in latest, "要告诉模型之前的加载还有报错"
    both = _tool(browser, tb.browser_console.invoke, {"level": "error", "include_previous": True})
    assert both.count("旧版本的报错") == 2, both


def test_console_is_empty_after_the_fix_is_reloaded(browser, editable_site):
    """修好之后重开：默认视图里不能再出现旧报错——这正是模型据以判断"修好了"的那一眼。"""
    from src import tools_browser as tb
    base, app_js = editable_site
    assert _call(browser, "open", url=base + "/index.html", timeout=20)["ok"]
    app_js.write_text("document.getElementById('out').textContent = 'v2';\n", encoding="utf-8")
    assert _call(browser, "open", url=base + "/index.html", timeout=20)["ok"]
    latest = _tool(browser, tb.browser_console.invoke, {"level": "error"})
    assert "旧版本的报错" not in latest.split("之前")[0], latest


def test_browser_check_counts_only_after_a_clean_reload_of_the_edited_page(browser, editable_site):
    """端到端走一遍评测 012 的路径：报错 → 改了没重开（不算）→ 重开干净（算）。"""
    from src import session as _s, tools_browser as tb, verification as vf
    from src.browser import security
    base, app_js = editable_site
    sess = _s.Session()
    _s.register(sess)
    for allow in (sess.browser_origin_allowlist, sess.browser_open_allowlist):
        allow.add(security.parse_origin(base))
    v = sess.verification

    def bound(fn):
        def run(*a, **kw):
            _s.bind_thread(sess)
            try:
                return fn(*a, **kw)
            finally:
                _s.unbind_thread()
        return run

    open_ = bound(tb.browser_open.invoke)
    console = bound(tb.browser_console.invoke)

    vf.mark_dirty(v, "app.js")
    _tool(browser, open_, {"url": base + "/index.html"})
    _tool(browser, console, {"level": "error"})
    assert vf.browser_check_state(v) == "errors"

    app_js.write_text("document.getElementById('out').textContent = 'v2';\n", encoding="utf-8")
    vf.mark_dirty(v, "app.js")
    _tool(browser, console, {"level": "all"})      # 改完没重开：读到的还是旧加载
    assert vf.browser_check_state(v) == "stale"

    _tool(browser, open_, {"url": base + "/index.html"})
    _tool(browser, console, {"level": "warn", "clear": True})   # 只看 warn 也照样按 error 判
    assert vf.browser_check_state(v) == "ok"
    vf.mark_tests(v, None, "没有收集到任何测试用例", no_tests=True)
    assert [g for g in vf.gaps_from_state(v) if "git_diff" not in g] == []
    last = [e for e in v["evidence"] if e["kind"] == "browser"][-1]
    assert last["status"] == "passed" and last["path"].startswith(base)
