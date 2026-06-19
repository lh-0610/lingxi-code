"""UI 偏好持久化（如关闭按钮的"记住选择"）。

存到 chat_memory/ui_prefs.json，与会话历史同目录。
"""
import json
import os

from ..paths import MEMORY_DIR


def _ui_prefs_path():
    return os.path.join(MEMORY_DIR, "ui_prefs.json")


def _load_ui_prefs():
    try:
        p = _ui_prefs_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_ui_prefs(prefs):
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        with open(_ui_prefs_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
