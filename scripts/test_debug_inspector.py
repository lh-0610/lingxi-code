"""F12 调试窗口和模态对话框：调试窗口始终置顶，模态框（设置、确认框……）打开时它被锁住不能点，
如果还压在对话框上面，两边都点不了，只能按 Esc。被锁住期间要让出置顶，关掉对话框后恢复。

离屏平台测不了真实的窗口叠放顺序，这里测的是置顶标志的变化；叠放顺序在真 Windows 窗口上核对过。
"""
import pytest
from PySide6.QtCore import Qt, QTimer


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _on_top(widget):
    return bool(widget.windowHandle().flags() & Qt.WindowStaysOnTopHint)


def test_inspector_gives_up_staying_on_top_while_a_modal_dialog_is_open(qapp):
    from PySide6.QtWidgets import QDialog, QMainWindow
    from src.ui.debug_inspector import DebugInspector

    main = QMainWindow()
    main.show()
    inspector = DebugInspector(main)
    inspector.show()
    qapp.processEvents()
    assert _on_top(inspector), "平时浮在主窗口上方"

    dialog = QDialog(main)
    seen = {}

    def while_open():
        qapp.processEvents()
        seen["on_top"] = _on_top(inspector)
        seen["visible"] = inspector.isVisible()
        dialog.reject()

    QTimer.singleShot(50, while_open)
    dialog.exec()
    qapp.processEvents()

    assert seen == {"on_top": False, "visible": True}, "被模态框锁住时不再压在它上面，但窗口还在"
    assert _on_top(inspector), "对话框关掉后恢复置顶"
    inspector.close()
    main.close()
    inspector.deleteLater()
    main.deleteLater()
    qapp.processEvents()
