"""工作区锚点：记下「这一轮结束时现场长什么样」，供恢复时比对（B04）。

记什么：
- 项目根、是不是 git 仓库、HEAD、分支、根提交（判断"是不是还是同一个仓库"）；
- 本轮涉及文件的内容指纹（sha256；文件不存在记 missing；太大只记大小）。

**只记涉及文件，不哈希整个项目**：锚点在每次工具边界都会刷新，全量哈希大仓库会让每个
写操作都卡一下。涉及文件之外的变化靠 git HEAD / 分支变化和"无法完整核对"来如实说明，
不假装看见了。

比对时的两条底线（方案 §5.5）：
- **指纹相同不是测试通过的证明**，只说明"这个文件从上次到现在没变"；
- **HEAD 相同不代表工作区相同**，所以 HEAD 没变也不会被说成"现场没有变化"。
"""
import hashlib
import os
import subprocess

# 锚点里最多记多少个文件的指纹。超出的部分在 errors 里如实说明"没记全"。
MAX_FINGERPRINTS = 200
# 超过这个大小的文件只记大小、不哈希：锚点刷新在工具边界上，不能被一个大文件拖慢。
MAX_HASH_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT = 5


def _git(root, *args):
    """在 root 下跑一条 git 命令，返回 (输出, 错误)。任何失败都给错误原因，不抛。"""
    try:
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT)
    except FileNotFoundError:
        return None, "git 不可用"
    except subprocess.TimeoutExpired:
        return None, f"git {args[0]} 超时"
    except Exception as e:           # pragma: no cover - 权限等罕见错误
        return None, str(e)
    if done.returncode != 0:
        return None, (done.stderr or "").strip() or f"git {args[0]} 返回 {done.returncode}"
    return (done.stdout or "").strip(), ""


def _fingerprint(path):
    """单个文件的指纹。读不了就如实说读不了，不猜。"""
    try:
        if not os.path.exists(path):
            return {"missing": True}
        if os.path.isdir(path):
            return {"directory": True}
        size = os.path.getsize(path)
        if size > MAX_HASH_BYTES:
            return {"too_large": size}
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return {"sha256": digest.hexdigest()}
    except OSError as e:
        return {"unreadable": str(e)[:200]}


def _resolve(root, rel):
    return rel if os.path.isabs(rel) else os.path.join(root, rel)


def capture(root, files, *, previous=None, with_roots=True, refresh_git=True):
    """在 root 下给 files 拍一张锚点。

    `previous` 是同一轮之前拍的锚点：根提交不会变，同一个根目录就直接沿用，
    省掉一次可能较慢的 `git rev-list`。

    `refresh_git=False`：沿用 previous 的 HEAD / 分支，只重算文件指纹。给"只会写文件"的
    工具边界用——它们改不了 HEAD，而一次 git 子进程在 Windows 上就要几十毫秒，
    是整套工具边界提交（B03 实测 7–15 ms）的好几倍。沿用的信息只会更旧不会更新，
    恢复时比对的结果因此只会更保守（多报一次变化），不会漏报。
    """
    anchor = {"root": root or "", "is_git": False, "git_head": None, "git_branch": None,
              "git_roots": None, "fingerprints": {}, "errors": []}
    if not root or not os.path.isdir(root):
        anchor["errors"].append("项目目录不存在")
        return anchor

    reuse = (not refresh_git and isinstance(previous, dict) and previous.get("root") == root)
    if reuse:
        for key in ("is_git", "git_head", "git_branch", "git_roots"):
            value = previous.get(key)
            anchor[key] = list(value) if isinstance(value, list) else value
        anchor["errors"] = [e for e in (previous.get("errors") or [])
                            if not e.startswith("涉及文件 ")]
        head, err = None, ""
    else:
        head, err = _git(root, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")
    if head is not None:
        lines = head.splitlines()
        anchor["is_git"] = True
        anchor["git_head"] = lines[0] if lines else None
        anchor["git_branch"] = lines[1] if len(lines) > 1 else None
        if (isinstance(previous, dict) and previous.get("root") == root
                and previous.get("git_roots")):
            anchor["git_roots"] = list(previous["git_roots"])
        elif with_roots:
            roots, rerr = _git(root, "rev-list", "--max-parents=0", "HEAD")
            if roots is not None:
                anchor["git_roots"] = sorted(r for r in roots.split() if r)
            else:
                anchor["errors"].append(f"读取根提交失败：{rerr}")
    elif err and "not a git repository" not in err.lower():
        # "不是 git 仓库" 是正常情况；其它失败（git 缺失、超时）要如实记下。
        anchor["errors"].append(f"读取 git 状态失败：{err}")

    unique = []
    for rel in files or []:
        if isinstance(rel, str) and rel and rel not in unique:
            unique.append(rel)
    if len(unique) > MAX_FINGERPRINTS:
        anchor["errors"].append(
            f"涉及文件 {len(unique)} 个，只记录了前 {MAX_FINGERPRINTS} 个的指纹")
        unique = unique[:MAX_FINGERPRINTS]
    for rel in unique:
        anchor["fingerprints"][rel] = _fingerprint(_resolve(root, rel))
    return anchor


class _Invalid(ValueError):
    pass


# 指纹字段 → 合法取值的判定。多一个认不出的键无妨（向前兼容），已知键类型不对就整张作废。
_FP_CHECKS = {
    "sha256": lambda v: isinstance(v, str),
    "missing": lambda v: v is True,
    "directory": lambda v: v is True,
    "too_large": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "unreadable": lambda v: isinstance(v, str),
}


def invalid(reason):
    """"锚点坏了、无法核对"的标记。比对时如实报告，而不是当成没有锚点、更不是当成现场没变。"""
    return {"invalid": str(reason)[:200]}


def normalize(raw):
    """校验磁盘上的锚点。**永不抛异常**。

    坏掉就降级成带原因的"无法核对"标记，不牵连别的——进度坏掉不能拖垮聊天历史，
    锚点只是进度里的一小块，更不能拖垮整份进度。早先 `errors` 被改成数字时
    `load_session` 直接抛 TypeError，整段聊天都打不开；保存路径也会对旧文件做同一次
    归一化，于是这个会话之后每次保存都会失败。

    **逐字段严格校验，不"尽量抢救"**：只丢掉坏字段、留下好字段的话，比如坏的是
    `errors`，"只记录了前 200 个文件"这类说明就悄悄没了，比对结论反而更乐观。
    """
    if raw is None:
        return None
    try:
        return _normalize_strict(raw)
    except _Invalid as e:
        return invalid(e)
    except Exception as e:           # 兜底：任何意外都降级，不往上抛
        return invalid(f"锚点无法解析：{e}")


def _normalize_strict(raw):
    if not isinstance(raw, dict):
        raise _Invalid("锚点不是对象")
    if "invalid" in raw:             # 之前就降级过的标记，原样往返
        return invalid(raw.get("invalid") if isinstance(raw.get("invalid"), str) else "锚点已损坏")
    root = raw.get("root")
    if not isinstance(root, str):
        raise _Invalid("root 不是字符串")
    is_git = raw.get("is_git", False)
    if not isinstance(is_git, bool):
        raise _Invalid("is_git 不是布尔值")
    for key in ("git_head", "git_branch"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise _Invalid(f"{key} 类型非法")
    roots = raw.get("git_roots")
    if roots is not None:
        if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
            raise _Invalid("git_roots 不是字符串列表")
    prints = raw.get("fingerprints", {})
    if not isinstance(prints, dict):
        raise _Invalid("fingerprints 不是对象")
    clean = {}
    for rel, fp in prints.items():
        if not isinstance(rel, str) or not isinstance(fp, dict):
            raise _Invalid("fingerprints 含非法条目")
        entry = {}
        for key, check in _FP_CHECKS.items():
            if key in fp:
                if not check(fp[key]):
                    raise _Invalid(f"指纹 {rel} 的 {key} 类型非法")
                entry[key] = fp[key]
        if not entry:
            raise _Invalid(f"指纹 {rel} 没有可识别的内容")
        clean[rel] = entry
    errors = raw.get("errors", [])
    if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
        raise _Invalid("errors 不是字符串列表")
    return {
        "root": root,
        "is_git": is_git,
        "git_head": raw.get("git_head"),
        "git_branch": raw.get("git_branch"),
        "git_roots": list(roots) if roots is not None else None,
        "fingerprints": clean,
        "errors": list(errors),
    }


def same_repository(anchor, path):
    """新目录是不是锚点记录的那个仓库。返回 (结论, 说明)。

    True = 根提交有交集；False = 确定不是（不是 git 仓库了 / 根提交不相交）；
    None = 判断不了（上次没记仓库身份、或读不出来）——交给用户确认，不替他猜。
    """
    if not os.path.isdir(path or ""):
        return False, "目录不存在"
    if (not isinstance(anchor, dict) or "invalid" in anchor or not anchor.get("is_git")
            or not anchor.get("git_roots")):
        return None, "上次没有记录（或记录已损坏）仓库身份，无法确认新目录就是原来的项目"
    out, err = _git(path, "rev-list", "--max-parents=0", "HEAD")
    if out is None:
        if "not a git repository" in (err or "").lower():
            return False, "新目录不是 git 仓库，而原项目是"
        return None, f"读取新目录的仓库身份失败：{err}"
    roots = {r for r in out.split() if r}
    if roots & set(anchor["git_roots"]):
        return True, ""
    return False, "新目录的根提交与上次记录的不一致，是另一个仓库"


def compare(anchor, root=None):
    """把现场和锚点比一遍，返回**只读**报告。

    返回 dict：
      blocking      —— 必须解决才能继续（目录没了 / 指向了另一个仓库）
      changed       —— 内容与记录不同的文件
      deleted       —— 记录时存在、现在不存在的文件
      appeared      —— 记录时不存在、现在出现了的文件
      head_change   —— (旧, 新) 或 None
      branch_change —— (旧, 新) 或 None
      incomplete    —— 无法完整核对的原因（有则说明现场结论不完整）
      checked       —— 实际比对过指纹的文件数（0 = 上次没记录任何文件，"都没变"无从谈起）
    """
    report = {"blocking": [], "changed": [], "deleted": [], "appeared": [],
              "head_change": None, "branch_change": None, "incomplete": [], "checked": 0,
              "root": root or (anchor or {}).get("root") or ""}
    if not isinstance(anchor, dict):
        report["incomplete"].append("上次运行没有记录现场锚点（可能是本功能之前的会话）")
        return report
    if "invalid" in anchor:
        report["incomplete"].append(f"上次的现场记录已损坏（{anchor.get('invalid')}），无法核对")
        return report
    root = root or anchor.get("root") or ""
    report["root"] = root
    if not root or not os.path.isdir(root):
        report["blocking"].append(f"项目目录已不存在：{root or '（未记录）'}")
        return report

    report["incomplete"].extend(anchor.get("errors") or [])
    current = capture(root, list((anchor.get("fingerprints") or {}).keys()),
                      previous=None, with_roots=bool(anchor.get("git_roots")))

    if anchor.get("is_git"):
        if not current["is_git"]:
            report["blocking"].append(
                f"{root} 上次是 git 仓库，现在不是——目录可能已被替换成另一个项目")
        else:
            old_roots = set(anchor.get("git_roots") or [])
            new_roots = set(current.get("git_roots") or [])
            if old_roots and new_roots and not (old_roots & new_roots):
                report["blocking"].append(
                    f"{root} 现在指向另一个仓库（根提交与上次记录的不一致）")
            elif old_roots and not new_roots:
                report["incomplete"].append("无法读取现在的根提交，不能确认仍是同一个仓库")
            if anchor.get("git_head") != current.get("git_head"):
                report["head_change"] = (anchor.get("git_head"), current.get("git_head"))
            if anchor.get("git_branch") != current.get("git_branch"):
                report["branch_change"] = (anchor.get("git_branch"), current.get("git_branch"))
    report["incomplete"].extend(e for e in current["errors"] if e not in report["incomplete"])

    for rel, old in (anchor.get("fingerprints") or {}).items():
        report["checked"] += 1
        new = current["fingerprints"].get(rel) or {}
        if "unreadable" in new or "unreadable" in old:
            report["incomplete"].append(f"{rel} 无法读取，不能核对")
            continue
        if old.get("missing"):
            if not new.get("missing"):
                report["appeared"].append(rel)
            continue
        if new.get("missing"):
            report["deleted"].append(rel)
            continue
        if "too_large" in old or "too_large" in new:
            if old.get("too_large") != new.get("too_large"):
                report["changed"].append(rel)
            else:
                report["incomplete"].append(f"{rel} 过大未哈希，只比对了大小")
            continue
        if old.get("sha256") != new.get("sha256"):
            report["changed"].append(rel)
    return report
