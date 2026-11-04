"""打包并做安装程序：PyInstaller 打出 dist/灵犀Code/，Inno Setup 再包成 dist/LingxiCode-Setup-<版本>.exe。

在项目根目录运行：python installer/build.py
要先装好：python -m pip install pyinstaller；Inno Setup 6（winget install JRSoftware.InnoSetup）。
版本号取 src/__init__.py 的 __version__，发新版改那一处就行。
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_version() -> str:
    with open(os.path.join(ROOT, "src", "__init__.py"), encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not match:
        sys.exit("src/__init__.py 里找不到 __version__")
    return match.group(1)


def find_iscc() -> str:
    """ISCC.exe：PATH 里有就用；winget 按用户装在 LOCALAPPDATA 下的 Programs，按机器装在 Program Files (x86)。"""
    found = shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", "") + r"\Programs",
                 os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        path = os.path.join(base, "Inno Setup 6", "ISCC.exe")
        if base and os.path.isfile(path):
            return path
    sys.exit("找不到 Inno Setup 6 的 ISCC.exe：winget install JRSoftware.InnoSetup")


def main():
    version = app_version()
    iscc = find_iscc()           # 先找齐工具再开始打包：PyInstaller 一跑好几分钟，最后才发现缺东西白等
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "lingxi.spec"], cwd=ROOT, check=True)
    subprocess.run([iscc, f"/DAppVersion={version}", os.path.join("installer", "lingxi.iss")],
                   cwd=ROOT, check=True)
    print(f"安装程序：{os.path.join(ROOT, 'dist', f'LingxiCode-Setup-{version}.exe')}")


if __name__ == "__main__":
    main()
