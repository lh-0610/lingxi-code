"""UI 偏好持久化（如关闭按钮的"记住选择"）。

存到 chat_memory/ui_prefs.json，与会话历史同目录。目录按当前数据根取（paths.memory_dir）：
桌面端就是程序目录下的 chat_memory；测试用 isolated_memory 时跟着进临时目录，不会改到真实偏好。
"""
import json
import os

from ..paths import memory_dir


def _ui_prefs_path():
    return os.path.join(memory_dir(), "ui_prefs.json")


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
        os.makedirs(memory_dir(), exist_ok=True)
        with open(_ui_prefs_path(), "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def inherited_permission_mode(prev_sess):
    """新会话的放行规则：沿用上一个会话的；"跳过权限确认"不往下传，退回上次记住的模式。"""
    from ..permission_modes import default_for_new_session
    return default_for_new_session(getattr(prev_sess, "permission_mode", "manual"),
                                   _load_ui_prefs().get("permission_mode"))
