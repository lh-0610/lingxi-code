"""安装程序脚本和打包配置对得上：exe 名、产物目录、语言文件、版本号。

installer/lingxi.iss 按名字去 dist 里拿 PyInstaller 的产物，lingxi.spec 改了名字而这边没跟上的话，
编译安装程序时才报「找不到文件」，或者更糟：快捷方式指向一个不存在的 exe。
"""
import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8-sig") as f:
        return f.read()


def _iss_define(text, name):
    return re.search(rf'^#define {name} "([^"]+)"', text, re.M).group(1)


def test_installer_takes_the_exe_and_folder_the_spec_produces():
    spec = _read("lingxi.spec")
    iss = _read("installer", "lingxi.iss")
    names = re.findall(r"^\s*name='([^']+)'", spec, re.M)
    assert len(names) == 2, "EXE 和 COLLECT 各一个 name"
    exe_name, folder = names
    assert _iss_define(iss, "AppExe") == exe_name + ".exe"
    assert _iss_define(iss, "AppName") == folder
    assert r'Source: "..\dist\{#AppName}\*"' in iss


def test_language_file_is_in_the_repo():
    iss = _read("installer", "lingxi.iss")
    for name in re.findall(r'MessagesFile: "([^"]+)"', iss):
        assert os.path.isfile(os.path.join(ROOT, "installer", name)), name


def test_build_script_reads_the_app_version():
    spec = importlib.util.spec_from_file_location("installer_build", os.path.join(ROOT, "installer", "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    from src import __version__
    assert build.app_version() == __version__
