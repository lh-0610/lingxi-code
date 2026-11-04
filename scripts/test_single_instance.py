"""只开一个灵犀：再点图标时叫出已经开着的窗口，不开第二个；重启时先让出名字。

真的锁文件 + 本地服务（Windows 上是命名管道）；「第二次启动」用真的子进程，和实际一样不在
同一个线程里。名字带随机后缀，不和本机正开着的灵犀撞。
"""
import os
import subprocess
import sys
import time
import uuid

import pytest

from src import single_instance

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程里模拟「又点了一次图标」：抢不到就去叫开着的那个；打印 抢到没有 / 叫到没有
_SECOND_LAUNCH = """
import sys
sys.path.insert(0, {root!r})
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from src import single_instance as si
got = si.acquire({name!r}, lambda: None)
print(got, (not got) and si.notify_running({name!r}, wait_s=1))
"""


@pytest.fixture()
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def name():
    yield f"lingxi-test-{uuid.uuid4().hex[:12]}"
    single_instance.release()
    single_instance._name, single_instance._on_show = "", None


def _second_launch(app, name, until=lambda: True, seconds=20.0):
    """起一个子进程当第二次启动，期间本进程照常跑事件循环（服务端要靠它收消息）；
    子进程退出后再等 until 成立（最多 2 秒：消息可能比退出晚一点处理到）。"""
    proc = subprocess.Popen([sys.executable, "-c", _SECOND_LAUNCH.format(root=ROOT, name=name)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        app.processEvents()
        time.sleep(0.01)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not until():
        app.processEvents()
        time.sleep(0.01)
    out = proc.communicate(timeout=10)[0]
    return out.strip().splitlines()[-1] if out.strip() else out


def test_name_depends_on_the_data_folder(tmp_path):
    """源码跑的和安装版数据不在一处，可以同时开；同一套数据只能开一个。"""
    a, b = tmp_path / "a", tmp_path / "b"
    assert single_instance.server_name(str(a)) == single_instance.server_name(str(a))
    assert single_instance.server_name(str(a)) != single_instance.server_name(str(b))


def test_nobody_running_means_start_normally(qapp, name):
    t0 = time.monotonic()
    assert single_instance.notify_running(name) is False
    assert time.monotonic() - t0 < 3, "没有在跑的就别让启动多等"


def test_second_launch_brings_up_the_first_window_and_quits(qapp, name):
    shown = []
    assert single_instance.acquire(name, lambda: shown.append(1))
    result = _second_launch(qapp, name, until=lambda: shown)
    assert result == "False True", f"第二次启动拿不到、叫到了开着的那个：{result}"
    assert shown, "开着的那个要把窗口拿出来"


def test_restart_releases_the_name_and_can_take_it_back(qapp, name):
    """设置里「立即重启」：先让出名字，新进程才能成为那一个；没重启成就拿回来。"""
    assert single_instance.acquire(name, lambda: None)
    single_instance.release()
    assert _second_launch(qapp, name) == "True False", "让出来之后新进程能拿到"
    assert single_instance.reacquire()
    assert single_instance.notify_running(name) is True
