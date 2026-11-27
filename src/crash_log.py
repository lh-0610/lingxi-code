"""进程崩溃（访问违例这类 Python 接不住的错）时把所有线程的 Python 堆栈写进 logs/crash_*.log。

起因：一次在 PySide6 里的访问违例（锁屏几分钟后），Windows 的转储只看得到 C++ 栈、看不到
是哪行 Python 代码。faulthandler 在崩溃那一刻把每个线程的 Python 调用栈写出来。

每次启动开一个 crash_<时间>_<pid>.log 交给 faulthandler（它要求事先打开好的文件）；正常退出时
文件是空的就删掉，所以 logs 里只留有内容的。被强杀（任务管理器结束）留下的空文件，下次启动时清掉。
Windows 上被程序自己接住的访问违例也会记下（faulthandler 先于异常处理触发），这种正常退出时在
文件末尾注明「不是崩溃」。
30 天前的 .log 由 paths._cleanup_old_logs 统一清理。
"""
from __future__ import annotations

import atexit
import faulthandler
import glob
import os
from datetime import datetime

_file = None


def _remove_if_empty(path: str) -> None:
    try:
        if os.path.getsize(path) == 0:
            os.remove(path)
    except OSError:
        pass


def enable(log_dir: str) -> str | None:
    """打开崩溃堆栈记录，返回这次的文件路径；打不开就返回 None（不影响启动）。"""
    global _file
    if _file is not None:
        return _file.name
    try:
        os.makedirs(log_dir, exist_ok=True)
        for old in glob.glob(os.path.join(log_dir, "crash_*.log")):
            _remove_if_empty(old)
        path = os.path.join(log_dir, f"crash_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log")
        _file = open(path, "w", encoding="utf-8")
        faulthandler.enable(file=_file, all_threads=True)
    except Exception:
        _file = None
        return None

    def _close():
        global _file
        f, _file = _file, None
        if f is None:
            return
        try:
            faulthandler.disable()
            if f.tell() > 0:
                # Windows 上 faulthandler 在异常被接住之前就写了（比如 ctypes 读坏地址抛 OSError），
                # 进程其实没崩：注明，免得以后翻日志当成崩溃
                f.write(f"\n（{datetime.now():%H:%M:%S} 进程正常退出：上面记的是被程序接住的异常，不是崩溃）\n")
            f.close()
        except Exception:
            pass
        _remove_if_empty(path)

    atexit.register(_close)
    return path
