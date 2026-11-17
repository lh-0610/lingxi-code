"""把主题弹窗渲染成图片（非 pytest）：真实 ChatUI 作底，六种弹窗 × 明暗两套主题。

    python scripts/render_dialogs.py [输出目录]      # 默认 build/dialog_shots/

offscreen 平台、数据根 / 配置都指到临时目录，不碰用户数据、不联网。每张图是主窗口截图
（弹窗是窗内覆盖层，截图里就带着；没有父窗口时才另外叠上去）。改了 dialogs.py 的样式后跑一遍，对着设计稿「灵犀弹窗设计」看。
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ["QT_QPA_PLATFORM"] == "offscreen" and sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                                         "Fonts"))
_tmp = tempfile.mkdtemp(prefix="lingxi-shots-")
os.environ.setdefault("LINGXI_DATA_DIR", os.path.join(_tmp, "data"))
os.environ.setdefault("LINGXI_LOG_DIR", os.path.join(_tmp, "logs"))
os.environ.setdefault("LINGXI_CONFIG_PATH", os.path.join(_tmp, "config.json"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PySide6.QtCore import QEvent, QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def main(out_dir):
    app = QApplication.instance() or QApplication([])
    from src import mcp_client
    mcp_client.init_mcp = lambda: []
    from src.ui import dialogs
    from src.ui.chat_window import ChatUI
    ChatUI._show_current_model_config_warning = lambda self: None
    from src import session as _session
    from src.ui.settings_dialog import SettingsDialog, _CustomModelEditor
    from src.ui.task_panel import EditConstraintsDialog
    from src.ui.widgets import QuitConfirmDialog

    ui = ChatUI()
    ui.resize(1180, 760)
    ui.show()
    app.processEvents()
    os.makedirs(out_dir, exist_ok=True)

    A = dialogs.Action
    cases = {
        "1_confirm": lambda: dialogs.question(
            ui, "回退对话？", "回退将丢弃这一轮及之后的所有对话。",
            dialogs.Ok | dialogs.Cancel, dialogs.Cancel, labels={dialogs.Ok: "回退"},
            informative="磁盘上的文件改动不会被撤销，要撤销文件请用顶栏的「撤销」。", glyph="rewind"),
        "2_success": lambda: dialogs.success(
            ui, "已保存", "配置已写入 config.json。密钥和 Base URL 的改动要重启应用才生效。",
            dialogs.Yes | dialogs.No, dialogs.Yes,
            labels={dialogs.Yes: "立即重启", dialogs.No: "稍后"}),
        "3_error": lambda: dialogs.critical(
            ui, "保存失败", "配置没能写入 config.json，请检查文件权限。",
            detail="PermissionError: [Errno 13] Permission denied: 'config.json'"),
        "4_multi": lambda: dialogs.choose(
            ui, "暂时无法继续任务", "继续之前需要先处理：",
            [A("use", "改用当前模型「deepseek-v4.1-flash」继续", dialogs.PRIMARY),
             A("relocate", "重新选择项目目录…"), A("copy", "复制原路径"),
             A("close", "关闭", dialogs.TEXT)],
            bullets=["原项目目录已不存在：D:\\old\\shop-api", "上次用的模型已不在模型列表里"]),
        "5_input": lambda: dialogs.get_text(ui, "重命名会话", "会话标题", "登录超时重试"),
        "6_danger": lambda: ui._confirm_bypass_mode(),
        "6b_delete": lambda: dialogs.confirm_danger(
            ui, "删除自定义模型？", "确认删除「deepseek-v4.1-flash」？", "删除", glyph="trash",
            informative="这一项会从设置移除，重启后顶栏下拉里也消失。"),
        # 第 2 批：原来自带窗框的对话框
        "7_settings": lambda: SettingsDialog(ui).exec(),
        "8_custom_model": lambda: _CustomModelEditor(ui, {
            "name": "deepseek-v4.1-flash", "model_id": "deepseek-v4.1-flash",
            "base_url": "https://api.deepseek.com"}).exec(),
        "9_constraints": lambda: EditConstraintsDialog(ui, _session.get_active()).exec(),
        "10_quit": lambda: QuitConfirmDialog("1 个对话、2 个后台命令还在运行", ui).exec(),
        "11_edit_message": lambda: ui._show_timeline_edit_dialog(
            "把登录超时的重试补上，连续失败三次后提示用户检查网络。",
            note="这条消息原本带有图片。重新发送只发文字，图片不会保留；需要的话请重新上传。"),
        "12_text_viewer": lambda: ui._show_text_dialog(
            "验证输出", "$ python -m pytest scripts/ -q\n" + "." * 72 + " [100%]\n"
            "3577 passed, 10 skipped in 892.89s"),
    }
    saved = []
    for theme_name in ("light", "dark"):
        if ui.theme != theme_name:
            ui.theme = theme_name
            ui._apply_theme()
            ui._redraw_chat()
        app.processEvents()
        for name, open_it in cases.items():
            path = os.path.join(out_dir, f"{name}_{theme_name}.png")

            def snap(path=path):
                dlg = dialogs.active_dialog()
                if dlg is None:
                    QTimer.singleShot(10, ui, snap)
                    return
                app.processEvents()
                if not dlg.isWindow():
                    pm = ui.grab()                   # 窗内覆盖层：主窗口截图里就带着它
                else:
                    base = ui.grab()
                    top = dlg.grab()
                    pm = QPixmap(base.size())
                    pm.setDevicePixelRatio(base.devicePixelRatio())
                    pm.fill(QColor(0, 0, 0, 0))
                    p = QPainter(pm)
                    p.drawPixmap(QPoint(0, 0), base)
                    p.drawPixmap(dlg.geometry().topLeft() - ui.geometry().topLeft(), top)
                    p.end()
                pm.save(path)
                saved.append(path)
                dlg.reject()

            QTimer.singleShot(0, ui, snap)
            open_it()
    saved += _render_dock(app, ui, out_dir)
    saved += _render_browser(app, ui, out_dir)
    saved += _render_menus(app, ui, out_dir)
    ui.close()
    app.processEvents()
    ui.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    for s in saved:
        print(s)


class _FakeProc:
    """渲染用的假进程：一直在运行，不碰真实子进程。"""
    pid = 0

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code if self.code is not None else 0

    def kill(self):
        self.code = -9


def _wait(app, cond, timeout=15.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.03)
    return False


def _render_browser(app, ui, out_dir):
    """第 4 批：浏览器标签的三种状态（空白 / 模型操作中 / 连不上）。本机起一个小页面当被测站点。"""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from src import background as bg
    from src import session as _session
    from src.browser import page
    from src.browser.bridge import get_bridge

    html = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>登录 · Shop Admin"
            "</title></head><body style='font-family:sans-serif;background:#f7f8fa'>"
            "<div style='margin:60px auto;width:260px;background:#fff;border:1px solid #e3e4ea;"
            "border-radius:12px;padding:22px'><h3>登录 Shop Admin</h3>"
            "<p>用户名</p><input style='width:100%'><p>密码</p><input type='password' "
            "style='width:100%'><p><button style='width:100%'>登录</button></p></div></body></html>")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    ident = bg.capture_session_identity(_session.get_active())
    bg_id = bg.start("npm run dev", _FakeProc(None), cwd="D:/lingxi-code/web",
                     project="D:/lingxi-code", **ident, spawn_reader=False)
    bg.append_output(bg_id, f"  Local:   {base}/login\n")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    shots = []
    ui.resize(1400, 860)
    try:
        for theme_name in ("light", "dark"):
            if ui.theme != theme_name:
                ui.theme = theme_name
                ui._apply_theme()
                ui._redraw_chat()
            ui.file_panel.open_browser("")
            _wait(app, lambda: ui.file_panel._browser_view is not None, 5)
            view = ui.file_panel._browser_view
            _wait(app, lambda: view.content.currentWidget() is view.empty, 5)
            path = os.path.join(out_dir, f"15_browser_empty_{theme_name}.png")
            ui.grab().save(path)
            shots.append(path)
            view.empty.service_buttons[0].click()
            _wait(app, lambda: view.page_url().startswith(base) and not view._loading)
            _wait(app, lambda: view.tab_title() == "登录 · Shop Admin", 5)
            get_bridge().activity.emit("输入")
            _wait(app, lambda: False, 0.3)
            path = os.path.join(out_dir, f"16_browser_active_{theme_name}.png")
            ui.grab().save(path)
            shots.append(path)
            get_bridge().activity.emit("")
            view.navigate(f"http://127.0.0.1:{dead}/")
            _wait(app, lambda: view.failed)
            _wait(app, lambda: False, 0.3)
            path = os.path.join(out_dir, f"17_browser_error_{theme_name}.png")
            ui.grab().save(path)
            shots.append(path)
            ui.file_panel.close_all()
            _wait(app, lambda: not page.is_browser_open(), 5)
    finally:
        srv.shutdown()
        with bg._LOCK:
            bg._tasks.pop(bg_id, None)
    return shots


def _render_menus(app, ui, out_dir):
    """第 5 批：右键菜单（输入框 / 聊天记录 / 网页），菜单截图叠在主窗口截图上。"""
    from PySide6.QtCore import QPoint as _P
    from PySide6.QtGui import QTextCursor

    from src.ui import context_menus as cm
    from src.ui.message_view import BodyText

    class _Req:
        def __init__(self, link="", sel=""):
            self._l, self._s = link, sel

        def linkUrl(self):
            from PySide6.QtCore import QUrl
            return QUrl(self._l)

        def selectedText(self):
            return self._s

        def isContentEditable(self):
            return False

        def editFlags(self):
            return 0

    shots = []
    ui.resize(1180, 760)
    for theme_name in ("light", "dark"):
        if ui.theme != theme_name:
            ui.theme = theme_name
            ui._apply_theme()
            ui._redraw_chat()
        ui.entry.setPlainText("把登录超时的重试补上，连续失败三次后提示用户检查网络。")
        cur = ui.entry.textCursor()
        cur.setPosition(11)
        cur.setPosition(21, QTextCursor.MoveMode.KeepAnchor)
        ui.entry.setTextCursor(cur)
        ui._render_markdown("已在后台启动 `npm run dev`，地址是 http://localhost:5173/ ，右侧「后台」里能看到输出。")
        app.processEvents()
        body = ui.chat_area.findChildren(BodyText)[-1]
        body.setProperty("lx_hover_link", "http://localhost:5173/")
        body.setSelection(10, 20)
        menus = [
            ("18_menu_input", cm.build_text_menu(ui.entry, _P(10, 10)), ui.entry),
            ("19_menu_chat", cm.build_text_menu(body, _P(10, 10)), body),
        ]
        ui.file_panel.open_browser("")
        _wait(app, lambda: ui.file_panel._browser_view is not None, 5)
        view = ui.file_panel._browser_view
        menus.append(("20_menu_page", view.build_page_menu(_Req()), view))
        menus.append(("21_menu_link", view.build_page_menu(_Req("http://localhost:5173/docs",
                                                                 "token 和过期时间")), view))
        for name, menu, anchor in menus:
            menu.popup(anchor.mapToGlobal(_P(30, 20)))
            _wait(app, lambda: False, 0.2)
            base = ui.grab()
            top = menu.grab()
            p = QPainter(base)
            at = anchor.mapTo(ui, _P(30, 20))
            p.drawPixmap(at, top)
            p.end()
            path = os.path.join(out_dir, f"{name}_{theme_name}.png")
            base.save(path)
            shots.append(path)
            menu.close()
            menu.deleteLater()
        ui.file_panel.close_all()
        ui.entry.clear()
        _wait(app, lambda: False, 0.3)
    return shots


def _render_dock(app, ui, out_dir):
    """第 3 批：右侧停靠栏的「后台」和「调试」标签（窗口放宽到停靠栏放得下）。"""
    from src import background as bg
    from src import debug_log
    from src import session as _session
    from langchain_core.messages import HumanMessage

    ident = bg.capture_session_identity(_session.get_active())
    ids = []
    for cmd, code, out in (
            ("npm run dev", None, "  VITE v6.2.0  ready in 412 ms\n\n  Local:   http://localhost:5173/\n"
                                  "12:41:07 [vite] page reload src/App.tsx\n"),
            ("python -m http.server 8765 --bind 127.0.0.1", None, "Serving HTTP on 127.0.0.1 port 8765\n"),
            ("pytest scripts/test_browser_tree.py -q", 0, "36 passed in 1.2s\n")):
        bg_id = bg.start(cmd, _FakeProc(code), cwd="D:/lingxi-code/web", project="D:/lingxi-code",
                         **ident, spawn_reader=False)
        bg.append_output(bg_id, out)
        ids.append(bg_id)
    for i, (n_in, ms) in enumerate(((18589, 1700), (19150, 1400), (19426, 2600)), start=4):
        rec = debug_log.make_record("deepseek-v4.1-flash", "deepseek", "/chat/completions",
                                    [HumanMessage(content="起一下前端 dev server")], ["run_command"])
        debug_log.finalize_record(
            rec, text="", tool_calls=[{"name": "read_background_output",
                                       "args": {"bg_id": ids[0], "tail": 20}}],
            usage={"input_tokens": n_in, "output_tokens": 105, "cache_read_tokens": int(n_in * 0.96)})
        rec["elapsed_ms"] = ms
    ui.resize(1400, 860)
    app.processEvents()
    shots = []
    for theme_name in ("light", "dark"):
        if ui.theme != theme_name:
            ui.theme = theme_name
            ui._apply_theme()
            ui._redraw_chat()
        ui._open_background_panel()
        panel = ui._background_panel
        panel.refresh()
        panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
        app.processEvents()
        path = os.path.join(out_dir, f"13_background_{theme_name}.png")
        ui.grab().save(path)
        shots.append(path)
        ui._toggle_debug_inspector()
        app.processEvents()
        path = os.path.join(out_dir, f"14_debug_{theme_name}.png")
        ui.grab().save(path)
        shots.append(path)
        ui.file_panel.close_all()
        app.processEvents()
    with bg._LOCK:
        bg._tasks.clear()
    return shots


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "build", "dialog_shots"))
