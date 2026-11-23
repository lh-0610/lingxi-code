"""设置热更新的界面这一半：监视 config.json、把改动交给 config_apply、刷新界面并告诉用户。

两条路进来：
- 设置对话框保存：对话框自己调 config_apply.reload_and_apply，结果交给 _on_config_applied
- 手改 config.json：QFileSystemWatcher 看到文件变了，等 400ms（编辑器保存常常连写几次）再重读

两条路会撞在一起（对话框保存也会触发文件监视），重读时比较的是**值有没有变**，所以第二次是空操作。
"""
import os

from PySide6.QtCore import QFileSystemWatcher, QTimer

from .. import config_apply
from ..paths import CONFIG_PATH, logger

_DEBOUNCE_MS = 400


class ConfigWatchMixin:

    def _install_config_watcher(self):
        self._config_watcher = QFileSystemWatcher(self)
        # 文件和所在目录都看：「写临时文件再替换」的保存方式会让原文件从监视里掉出去，
        # 目录变化那一下再把它加回来
        paths = [p for p in (CONFIG_PATH, os.path.dirname(CONFIG_PATH)) if os.path.exists(p)]
        if paths:
            self._config_watcher.addPaths(paths)
        self._config_watcher.fileChanged.connect(self._on_config_file_event)
        self._config_watcher.directoryChanged.connect(self._on_config_file_event)
        self._config_debounce = QTimer(self)
        self._config_debounce.setSingleShot(True)
        self._config_debounce.setInterval(_DEBOUNCE_MS)
        self._config_debounce.timeout.connect(self._reload_config_from_disk)
        self._config_mtime = self._config_file_mtime()

    @staticmethod
    def _config_file_mtime():
        try:
            return os.stat(CONFIG_PATH).st_mtime_ns
        except OSError:
            return None

    def _on_config_file_event(self, _path=""):
        if CONFIG_PATH not in self._config_watcher.files() and os.path.exists(CONFIG_PATH):
            self._config_watcher.addPath(CONFIG_PATH)
        # 目录里别的文件（会话、日志……）变了也会来这里：只有 config.json 本身变了才重读
        mtime = self._config_file_mtime()
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        self._config_debounce.start()

    def _reload_config_from_disk(self):
        if not os.path.exists(CONFIG_PATH):
            return   # 删掉 / 改名的中间态：保持现在的配置，等文件回来
        try:
            res = config_apply.reload_and_apply()
        except Exception:
            logger.warning("config.json 热更新失败", exc_info=True)
            return
        if res is None:
            self._show_toast("config.json 格式不对，这次改动没有生效", 5000)
            return
        if res.changed:
            self._on_config_applied(res, from_settings=False)

    def _on_config_applied(self, res, from_settings: bool):
        """改动应用以后刷新界面：模型按钮、知识库状态，再说一声。"""
        if res is None:
            if from_settings:
                self._show_toast("设置已保存，但没能当场生效，重启应用后生效", 5000)
            return
        if res.models_changed:
            self._refresh_model_btn()
        if any(k.startswith("RAG_") for k in res.changed) and hasattr(self, "_refresh_kb_status"):
            from .. import config
            if config.RAG_KB_DIR and hasattr(self, "kb_status_label"):
                self._refresh_kb_status()
        from .. import session as _session
        active = _session.get_active()
        lost = [(old, new) for sess, old, new in res.model_fallbacks if sess is active]
        if lost:
            old, new = lost[0]
            self._show_toast(f"「{old}」已从模型列表里去掉，当前会话换成了「{new}」", 6000)
            return
        if from_settings:
            msg = "设置已保存，已生效" if res.changed else "设置已保存"
        elif res.restart_needed:
            msg = "config.json 的改动已生效；标题栏的改动要重启才生效"
        else:
            msg = "config.json 的改动已生效"
        if res.mcp_restarting:
            msg += "，MCP 正在重连"
        self._show_toast(msg, 3000)
