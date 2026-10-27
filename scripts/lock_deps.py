"""重新生成 requirements.lock：对 requirements-dev.txt 做一次干净解析，把完整依赖（含传递依赖）钉成 name==version。

    python scripts/lock_deps.py            # 以现有锁为约束解析：已钉的不动，只给新增依赖挑版本
    python scripts/lock_deps.py --upgrade  # 不看现有锁，全部取最新兼容版本

只解析不安装（pip --dry-run --ignore-installed），本机已装了什么不影响结果。
解析结果取决于平台和 Python 版本；灵犀只支持 Windows，请在 Windows + Python 3.14 上运行，
改完跑一遍 `python -m pytest scripts/` 再提交。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"


def main() -> int:
    upgrade = "--upgrade" in sys.argv[1:]
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
               "--quiet", "--report", str(report), "-r", str(ROOT / "requirements-dev.txt")]
        if LOCK.exists() and not upgrade:
            cmd += ["-c", str(LOCK)]
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print("解析失败；如果是新依赖和现有锁冲突，用 --upgrade 重新解析。", file=sys.stderr)
            return r.returncode
        data = json.loads(report.read_text(encoding="utf-8"))

    env = data["environment"]
    pins = sorted(((i["metadata"]["name"], i["metadata"]["version"]) for i in data["install"]),
                  key=lambda p: p[0].lower())
    lines = [
        "# 依赖锁：requirements-dev.txt 的完整解析结果（含传递依赖），由 scripts/lock_deps.py 生成，勿手改。",
        f"# 解析环境：Python {env['python_full_version']} / {env['sys_platform']}",
        "# 用法（作约束文件，只钉版本、不额外安装）：pip install -r requirements-dev.txt -c requirements.lock",
        "",
        *(f"{name}=={version}" for name, version in pins),
    ]
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {LOCK.name}：{len(pins)} 个包")
    return 0


if __name__ == "__main__":
    sys.exit(main())
