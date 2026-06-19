"""src.ui package — UI 模块拆分后的入口。

`from src.ui import ChatUI` 与重构前保持一致；其余子模块（theme/widgets/
helpers/prefs/settings_dialog/chat_window）按职责拆开，单文件不再爆炸。
"""
from .chat_window import ChatUI
from .settings_dialog import SettingsDialog

__all__ = ["ChatUI", "SettingsDialog"]
