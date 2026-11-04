"""只开一个灵犀：再点桌面图标 / 开始菜单时，把已经开着的窗口拿出来，不再开第二个。

两个进程同时开着同一套数据，会话和配置会互相覆盖。做法分两件：
- 谁是那一个：临时目录里的锁文件（QLockFile）。两次点击几乎同时也只有一个拿得到；进程崩溃后
  残留的锁按持有进程已不在认作失效。只靠本地服务不行——Windows 上同一个名字能挂两次。
- 叫它出来：拿到锁的进程挂一个本地服务（Windows 上是命名管道），后来的进程连上去发一句 show。

名字按数据目录区分：源码跑的（数据在项目里）和安装版（数据在 %APPDATA%\\灵犀）各管各的，
可以同时开。设置里的「立即重启」要先 release() 让出来，不然新进程会以为已经开着一个，自己退掉。
"""
import getpass
import hashlib
import os
import sys
import time

from PySide6.QtCore import QDir, QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_MESSAGE = b"show"
_TIMEOUT_MS = 500

_lock = None            # 当前进程拿着的锁和挂着的服务（没拿到就是 None）
_server = None
_name = ""
_on_show = None


def server_name(data_dir: str) -> str:
    """同一用户、同一数据目录得到同一个名字。"""
    key = f"{getpass.getuser()}|{os.path.normcase(os.path.abspath(data_dir))}"
    return "lingxi-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def acquire(name: str, on_show) -> bool:
    """成为这个名字的那一个：拿锁、挂服务；别的进程发来 show 时调 on_show（在界面线程）。

    已经有别的进程拿着返回 False；挂不上服务也返回 False（锁随即放掉，后来的照常启动）。"""
    global _lock, _server, _name, _on_show
    lock = QLockFile(os.path.join(QDir.tempPath(), name + ".lock"))
    lock.setStaleLockTime(0)        # 不按时间判过期：持有的进程真不在了才算
    if not lock.tryLock(0):
        return False
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)   # 只有本用户连得上
    QLocalServer.removeServer(name)  # 锁在手里，残留的 socket 文件（非 Windows）只能是死进程留下的
    if not server.listen(name):
        lock.unlock()
        return False
    server.newConnection.connect(lambda: _accept(server))
    _lock, _server, _name, _on_show = lock, server, name, on_show
    return True


def notify_running(name: str, wait_s: float = 0.0) -> bool:
    """叫开着的那个把窗口拿出来，返回 True（调用方该退出了）；连不上返回 False。

    wait_s：那个进程可能刚拿到锁、服务还没挂上，在这段时间里反复连。"""
    deadline = time.monotonic() + wait_s
    while True:
        sock = QLocalSocket()
        sock.connectToServer(name)
        if sock.waitForConnected(_TIMEOUT_MS):
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    if sys.platform == "win32":
        # 用户刚点了图标，前台权在这个进程手里；不交出去的话，那边 activateWindow 只会让
        # 任务栏按钮闪一下，窗口不到最前面
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY
        except Exception:
            pass
    sock.write(_MESSAGE)
    sock.waitForBytesWritten(_TIMEOUT_MS)
    sock.disconnectFromServer()
    return True


def _accept(server):
    while server.hasPendingConnections():
        conn = server.nextPendingConnection()

        def _read(c=conn):
            if bytes(c.readAll()).startswith(_MESSAGE) and _on_show is not None:
                _on_show()
            c.disconnectFromServer()

        conn.readyRead.connect(_read)
        conn.disconnected.connect(conn.deleteLater)
        if conn.bytesAvailable():
            _read()


def release():
    """让出名字（重启前、退出时）。之后再启动的进程会成为新的那一个。"""
    global _lock, _server
    if _server is not None:
        _server.close()
        _server = None
    if _lock is not None:
        _lock.unlock()
        _lock = None


def reacquire() -> bool:
    """release 之后又不退出了（重启没拉起新进程）：把名字拿回来。"""
    return bool(_name) and _lock is None and acquire(_name, _on_show)
