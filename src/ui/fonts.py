"""界面字体与字号的唯一来源。

界面文字用 Segoe UI Variable（Windows 11 自带，Windows 10 退到 Segoe UI）配微软雅黑 UI 显示中文；
代码、命令、路径用 Cascadia Mono，没有时退到 Consolas。等宽字体后面必须显式跟上微软雅黑 UI——
不写的话 Qt 给中文找的回退字体是宋体，工具输出和提示框里的中文就全成了宋体。

字号只有五档，新代码不要再写别的数：
    CAPTION 12  元信息、标签、提示
    UI      13  按钮、侧栏、工具卡等界面默认
    BODY    14  聊天正文、输入框
    TITLE   16  小标题、对话框标题
    DISPLAY 20  空状态等大标题
字重只用常规（400）和中粗（600）。
"""
from PySide6.QtGui import QFont

UI_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Microsoft YaHei UI"]

# QSS 和内联 HTML 里的 font-family 值。用单引号，嵌进 style="…" 属性也不会提前截断。
UI_CSS = ", ".join(f"'{f}'" for f in UI_FAMILIES) + ", sans-serif"
MONO_CSS = ", ".join(f"'{f}'" for f in MONO_FAMILIES) + ", monospace"

CAPTION = 12
UI = 13
BODY = 14
TITLE = 16
DISPLAY = 20

SEMIBOLD = 600


def ui_font(px: int = UI, weight: int | None = None) -> QFont:
    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPixelSize(px)
    if weight is not None:
        font.setWeight(QFont.Weight(weight))
    return font


def mono_font(px: int = UI) -> QFont:
    font = QFont()
    font.setFamilies(MONO_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(px)
    return font
