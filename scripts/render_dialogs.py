"""把主题弹窗渲染成图片（非 pytest）：真实 ChatUI 作底，六种弹窗 × 明暗两套主题。

    python scripts/render_dialogs.py [输出目录]      # 默认 build/dialog_shots/

offscreen 平台、数据根 / 配置都指到临时目录，不碰用户数据、不联网。每张图 = 主窗口截图 +
弹窗截图（遮罩半透明，叠在窗口上面）。改了 dialogs.py 的样式后跑一遍，对着设计稿「灵犀弹窗设计」看。
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
    ui.close()
    app.processEvents()
    ui.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    for s in saved:
        print(s)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "build", "dialog_shots"))
