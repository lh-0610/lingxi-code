"""高 DPI（Windows 缩放 150%）下 iframe 位置算得出、iframe 里的真实点击点得中。

缩放要在 QApplication 创建前定，测试进程里改不了：起一个 QT_SCALE_FACTOR=1.5 的子进程跑
scripts/browser_hidpi_check.py（环境里的数据根 / 日志 / 配置隔离变量随子进程继承）。
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.parametrize("scale", ["1.5"])
def test_iframe_clicks_work_on_a_scaled_display(scale):
    env = dict(os.environ, QT_SCALE_FACTOR=scale, QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "browser_hidpi_check.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=180, stdin=subprocess.DEVNULL)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines and lines[-1] == "OK", f"缩放 {scale}：\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
