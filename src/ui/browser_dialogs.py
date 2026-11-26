"""侧栏浏览器里网页弹的框、下载、登录：问用户的主题卡片（复审 #16 #17）。

设计稿「侧栏浏览器 · 网页弹框、下载、证书、登录」：
- 网页的 alert / confirm / prompt：标题写「网页提示 / 网页要你确认 / 网页要你输入」，网页的话放在
  灰底框里、上面标明是哪个网站说的——网页写的字不冒充灵犀；可勾「这个网页别再弹框了」
- 下载：文件名、大小、来自哪个网站；取消 / 另存为… / 保存到「下载」
- 登录（HTTP Basic）：用户名、密码只交给这个网站，不会发给模型

这些都是 browser/page.py 在网页信号里**同步**调的（confirm 要当场给答案，下载要当场 accept），
所以用 exec() 弹模态卡片。模型操作时触发的根本不会到这里（page.py 按取消处理并告诉模型）。
"""
import os

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout,
)

from ..browser import security
from . import dialogs
from .fonts import MONO_CSS, UI_CSS

_TITLES = {"alert": "网页提示", "confirm": "网页要你确认", "prompt": "网页要你输入"}


def _host(url: str) -> str:
    from PySide6.QtCore import QUrl
    u = QUrl(url)
    host = u.host() or url
    port = u.port(-1)
    return f"{host}:{port}" if port > 0 else host


def _qss() -> str:
    t = dialogs.tok
    return (
        # 全局样式会给标签铺底色：这里一律透明，要底色的（网页的话、图标格、本机标签）下面单独给
        "QLabel, QCheckBox { background: transparent; }"
        f"QLabel#bdChip {{ font-family: {UI_CSS}; font-size: 11px; color: {t('dlg_body')};"
        f" background: {t('dlg_bg')}; border: 1px solid {t('dlg_secondary_border')};"
        f" border-radius: 6px; padding: 1px 6px; }}"
        f"QLabel#bdWho {{ font-family: {UI_CSS}; font-size: 12px; color: {t('dlg_body')}; }}"
        f"QLabel#bdHost {{ font-family: {MONO_CSS}; font-size: 12px; color: {t('dlg_title')}; }}"
        f"QLabel#bdMsg {{ font-family: {UI_CSS}; font-size: 14px; color: {t('dlg_title')};"
        f" background: {t('dlg_hover_bg')}; border-radius: 10px; padding: 10px 12px; }}"
        f"QLabel#bdNote {{ font-family: {UI_CSS}; font-size: 13px; color: {t('dlg_body')}; }}"
        f"QLabel#bdField {{ font-family: {UI_CSS}; font-size: 12px; color: {t('dlg_body')}; }}"
        f"QLabel#bdFile {{ font-family: {MONO_CSS}; font-size: 13px; color: {t('dlg_title')}; }}"
        f"QLabel#bdMeta {{ font-family: {UI_CSS}; font-size: 12px; color: {t('dlg_body')}; }}"
        f"QLabel#bdTile {{ background: {t('dlg_info_bg')}; border-radius: 9px; }}"
        f"QCheckBox#bdBlock {{ font-family: {UI_CSS}; font-size: 12px; color: {t('dlg_body')}; }}"
        f"QLineEdit#lxDlgInput {{ background: {t('dlg_input_bg')}; border: 1px solid {t('dlg_input_border')};"
        f" border-radius: 10px; padding: 0 12px; color: {t('dlg_title')}; font-family: {UI_CSS};"
        f" font-size: 14px; min-height: 34px; }}"
        f"QLineEdit#lxDlgInput:focus {{ border: 1px solid {t('dlg_input_focus')}; }}"
    )


def _who_row(url: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    chip = QLabel("本机" if security.is_localhost(url) else "网站")
    chip.setObjectName("bdChip")
    host = QLabel(_host(url))
    host.setObjectName("bdHost")
    who = QLabel("上的网页说：")
    who.setObjectName("bdWho")
    chip.setFixedHeight(20)
    for w in (chip, host, who):
        row.addWidget(w, 0, Qt.AlignVCenter)
    row.addStretch(1)
    return row


class PageDialog(dialogs.CardDialog):
    """网页的 alert / confirm / prompt。"""

    def __init__(self, parent, kind: str, url: str, msg: str, default: str = ""):
        super().__init__(parent, title=_TITLES.get(kind, "网页提示"), width=440)
        self.kind = kind
        self.body.setStyleSheet(_qss())
        col = QVBoxLayout(self.body)
        col.setContentsMargins(24, 18, 24, 16)
        col.setSpacing(12)
        col.addLayout(_who_row(url))
        self.msg = QLabel(msg or "（网页没写内容）")
        self.msg.setObjectName("bdMsg")
        self.msg.setWordWrap(True)
        self.msg.setTextFormat(Qt.PlainText)          # 网页写的字原样显示，不当富文本解析
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        col.addWidget(self.msg)
        self.input = None
        if kind == "prompt":
            self.input = QLineEdit(default)
            self.input.setObjectName("lxDlgInput")
            col.addWidget(self.input)
        self.block = QCheckBox("这个网页别再弹框了")
        self.block.setObjectName("bdBlock")
        col.addWidget(self.block)
        if kind == "alert":
            self.add_button("知道了", dialogs.PRIMARY, self.accept, default=True)
        else:
            self.add_button("取消", dialogs.SECONDARY, self.reject)
            self.add_button("确定", dialogs.PRIMARY, self.accept, default=True)


def _size_text(n: int) -> str:
    if n is None or n < 0:
        return "大小未知"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return ""


def downloads_dir() -> str:
    d = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
    return d or os.path.expanduser("~")


class DownloadDialog(dialogs.CardDialog):
    SAVE, SAVE_AS = 1, 2

    def __init__(self, parent, name: str, size: int, url: str):
        super().__init__(parent, title="下载文件", width=440)
        self.choice = 0
        self.body.setStyleSheet(_qss())
        row = QHBoxLayout(self.body)
        row.setContentsMargins(24, 18, 24, 16)
        row.setSpacing(12)
        tile = QLabel()
        tile.setObjectName("bdTile")
        tile.setFixedSize(36, 36)
        tile.setAlignment(Qt.AlignCenter)
        tile.setPixmap(dialogs._svg_pixmap(
            '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
            dialogs.tok("dlg_info_fg"), 18, dpr=2.0))
        row.addWidget(tile, 0, Qt.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(2)
        self.file_label = QLabel(name or "（没有文件名）")
        self.file_label.setObjectName("bdFile")
        self.file_label.setTextFormat(Qt.PlainText)
        self.file_label.setWordWrap(True)
        meta = QLabel(f"{_size_text(size)} · 来自 {_host(url)}")
        meta.setObjectName("bdMeta")
        info.addWidget(self.file_label)
        info.addWidget(meta)
        row.addLayout(info, 1)
        self.add_button("取消", dialogs.SECONDARY, self.reject)
        self.add_button("另存为…", dialogs.SECONDARY, lambda: self._pick(self.SAVE_AS))
        self.add_button("保存到「下载」", dialogs.PRIMARY, lambda: self._pick(self.SAVE), default=True)

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class AuthDialog(dialogs.CardDialog):
    def __init__(self, parent, url: str, realm: str):
        super().__init__(parent, title=f"登录 {_host(url)}", width=420)
        self.body.setStyleSheet(_qss())
        col = QVBoxLayout(self.body)
        col.setContentsMargins(24, 18, 24, 16)
        col.setSpacing(8)
        realm_part = f"（「{realm}」）" if realm else ""
        note = QLabel(f"这个网站要求登录{realm_part}。账号密码只交给这个网站，不会发给模型。")
        note.setObjectName("bdNote")
        note.setWordWrap(True)
        note.setTextFormat(Qt.PlainText)
        col.addWidget(note)
        col.addSpacing(4)
        for label, attr, echo in (("用户名", "user", QLineEdit.Normal), ("密码", "password", QLineEdit.Password)):
            lab = QLabel(label)
            lab.setObjectName("bdField")
            col.addWidget(lab)
            edit = QLineEdit()
            edit.setObjectName("lxDlgInput")
            edit.setEchoMode(echo)
            setattr(self, attr, edit)
            col.addWidget(edit)
        self.add_button("取消", dialogs.SECONDARY, self.reject)
        self.add_button("登录", dialogs.PRIMARY, self.accept, default=True)


# ── 给 browser/page.py 的同步回调 ──

def make_hooks(parent_getter):
    """parent_getter() 返回弹框挂在哪个窗口上（主窗口）。"""

    def js_dialog(kind, url, msg, default):
        dlg = PageDialog(parent_getter(), kind, url, msg, default)
        accepted = dlg.exec() == dialogs.QDialog.Accepted
        text = dlg.input.text() if dlg.input is not None else ""
        block = dlg.block.isChecked()
        dlg.deleteLater()
        return accepted, text, block

    def download(name, size, url):
        parent = parent_getter()
        dlg = DownloadDialog(parent, name, size, url)
        dlg.exec()
        choice = dlg.choice
        dlg.deleteLater()
        if choice == DownloadDialog.SAVE:
            return downloads_dir(), _unique_name(downloads_dir(), name or "download")
        if choice == DownloadDialog.SAVE_AS:
            path, _ = QFileDialog.getSaveFileName(parent, "另存为", os.path.join(downloads_dir(), name or ""))
            if path:
                return os.path.dirname(path), os.path.basename(path)
        return None

    def auth(url, realm):
        dlg = AuthDialog(parent_getter(), url, realm)
        ok = dlg.exec() == dialogs.QDialog.Accepted
        creds = (dlg.user.text(), dlg.password.text()) if ok and dlg.user.text() else None
        dlg.deleteLater()
        return creds

    return {"js_dialog": js_dialog, "download": download, "auth": auth}


def _unique_name(folder: str, name: str) -> str:
    """「下载」里已有同名文件就加 (1) (2)：不覆盖用户原来的文件。"""
    base, ext = os.path.splitext(name)
    cand, i = name, 1
    while os.path.exists(os.path.join(folder, cand)):
        cand = f"{base} ({i}){ext}"
        i += 1
    return cand
