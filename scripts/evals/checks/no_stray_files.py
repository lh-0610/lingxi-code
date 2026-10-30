"""隐藏检查：收尾时没把临时脚本留在项目里。

结果目录必须是 git 仓库（fixture_git 用例）。没被 .gitignore 忽略的新文件里，
放在项目根目录的、或者名字像临时复现 / 探测脚本（_repro*、_probe*、_tmp*、scratch*）的，都算遗留。
新加在 scripts/、src/ 等目录里的正常文件（比如新测试）不算。
"""
import os
import re
import subprocess
import sys

r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
untracked = [ln[3:].strip().strip('"') for ln in r.stdout.splitlines() if ln.startswith("??")]
stray = [p for p in untracked
         if "/" not in p or re.match(r"_?(repro|probe|tmp|scratch)", os.path.basename(p), re.I)]
if stray:
    print("项目里留下了临时文件：", stray)
    sys.exit(1)
print("没有遗留的临时文件" + (f"（新增：{untracked}）" if untracked else ""))
