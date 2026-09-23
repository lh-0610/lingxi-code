"""验证状态管理（会话状态 + 完成前的项目文件复查）。

目标：在编码任务中，确保 AI 在声称"已完成"前必须先验证（跑测试 / 静态检查通过），
防止无验证的草率收尾。

验证状态存储在 `Session.verification`（会话级，多会话隔离）。
本模块提供纯函数操作这些状态 + 间隙检测，不引入循环依赖。
"""
import hashlib
import json
import os
import re
import uuid


# 代码文件扩展名——写入这些文件需要代码验证（测试 / 静态检查）
_CODE_EXTENSIONS = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".vue", ".svelte",
})


def new_verification() -> dict:
    """创建一个全新的验证状态字典（Session.__init__ 调用）。"""
    return {
        "dirty_files": [],        # 相对路径列表（去重后）of 成功写入的文件
        "code_dirty_files": [],   # 需要代码验证的子集（代码文件）
        "checks": {},             # path -> {"passed": bool|None, "checker": str}
        "tests_run": False,       # 是否调过 run_tests
        "tests_passed": None,     # True/False/None（未跑 / 无法确定）
        "tests_reason": "",       # tests_passed=None 时的简短原因
        "diff_reviewed": False,   # 是否调过 git_diff（写入后）
        "gate_prompted": False,   # 完成闸门是否已提示过一次（两次尝试机制）
        # ── 检查落在哪儿（B04 复核）──
        # 义务是按目录记的（盲区根目录、改动文件的实际位置），放行却只看上面两个全局标志的话，
        # A 目录里的改动能被 B 目录的一次检查清掉。所以还要记：本轮测试通过时实际在哪个目录跑、
        # diff 实际查的是哪个目录；每个改动文件实际在哪儿。失效规则与两个全局标志相同。
        "tests_roots": [],        # 通过的测试实际运行的目录（realpath）
        "diff_roots": [],         # 算作审阅过的 diff 实际覆盖的目录 / 路径（realpath）
        "dirty_abs": {},          # dirty 路径 → 实际绝对位置（位置未知的不在里面）
        # ── B06 结构化证据 ──
        # 每条记录来自**实际执行位置**（run_tests / 各 checker），带 argv / cwd / 退出码 /
        # 耗时。布尔值 + 原因不足以支撑结果卡：那样只能说"通过了"，说不出跑的是什么命令、
        # 退了什么码，也就无从分辨"真跑过并通过"和"根本没跑起来"。
        "evidence": [],
        # 文件变化版本。**只随实际变更 / 追踪不确定性递增，不随保存递增**——
        # 用 progress_revision 当版本的话，一次纯保存就会把刚做完的检查标成过期。
        "change_revision": 0,
        "failure_diagnosis": {    # 自动修复循环状态（独立于上面的验证闸门）
            "tool": "",           # 触发失败的工具名（"run_tests" / "check_code"）
            "attempt": 0,         # 已注入修复提示的次数
            "max_attempts": 3,    # 最大修复尝试次数
            "reason": "",         # 失败原因简述
        },
    }


def reset_verification(v: dict) -> None:
    """新用户消息开始时重置验证状态（agent_loop 开头调用）。"""
    v["dirty_files"] = []
    v["code_dirty_files"] = []
    v["checks"] = {}
    v["tests_run"] = False
    v["tests_passed"] = None
    v["tests_reason"] = ""
    v["diff_reviewed"] = False
    v["gate_prompted"] = False
    v["tests_roots"] = []
    v["diff_roots"] = []
    v["dirty_abs"] = {}
    # 证据是**本轮**的执行记录：上一轮的已经随 last_run 落盘，留在内存里只会让结果卡
    # 把上轮跑过的命令算进这一轮。change_revision 同归零，它只在一轮之内用于比新旧。
    v["evidence"] = []
    v["change_revision"] = 0
    v.pop("workspace_snapshots", None)
    v.pop("tracking_errors", None)
    # 盲区同样在这里清、随后由 restore_obligations 按已保存的义务填回——
    # 与 dirty_files 完全同一套路径，否则"哪些义务能跨轮"会有两套互相不知道的规则。
    v.pop("unknown_changes", None)
    # failure_diagnosis 在 check_repair_allowed() 成功通过时自动归零；
    # 新用户消息开始时也重置，防止上一轮残留状态。
    v["failure_diagnosis"] = {
        "tool": "", "attempt": 0, "max_attempts": 3, "reason": "",
    }


def _is_code_file(path: str) -> bool:
    """判断路径是否为代码文件（需要代码验证）。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in _CODE_EXTENSIONS


def bump_change_revision(v: dict) -> int:
    """文件确实变了（或追踪出现不确定）→ 推进文件变化版本。

    结果卡据此判断"这条检查证据是不是在改动之前拿到的"。**绝不能**拿
    `progress_revision` 代替：那个每保存一次就 +1，于是一次纯保存就把刚跑完的
    绿灯测试标成过期，用户看到的是"刚测完就失效了"。
    """
    if not isinstance(v, dict):
        return 0
    v["change_revision"] = int(v.get("change_revision", 0) or 0) + 1
    return v["change_revision"]


def mark_dirty(v: dict, rel_path: str, *, abs_path: str | None = None) -> None:
    """写文件工具成功后调用，标记文件为脏。

    rel_path: 相对于项目根的路径（已规范化的）。
    abs_path: 这个文件**实际**在哪儿（知道的话）。放行时要求检查覆盖到这个位置——
    相对路径本身说不出它相对的是哪个目录，跨目录的检查就能把它蒙混过去。
    """
    if not rel_path:
        return
    bump_change_revision(v)
    # 去重
    if rel_path not in v["dirty_files"]:
        v["dirty_files"].append(rel_path)
    if _is_code_file(rel_path) and rel_path not in v["code_dirty_files"]:
        v["code_dirty_files"].append(rel_path)
    if abs_path:
        v.setdefault("dirty_abs", {})[rel_path] = _real(abs_path)
    # 写入即失效：该文件的静态检查结果作废
    v["checks"].pop(rel_path, None)
    # 写入即失效：diff 需要重新审查
    v["diff_reviewed"] = False
    v["diff_roots"] = []
    # 写入即失效：测试结果作废（如果有代码文件被改）
    if _is_code_file(rel_path):
        v["tests_run"] = False
        v["tests_passed"] = None
        v["tests_reason"] = ""
        v["tests_roots"] = []


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return os.path.normpath(path)


def _add_root(v, key, root):
    real = _real(root)
    roots = v.setdefault(key, [])
    if not any(_same(real, r) for r in roots):
        roots.append(real)


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def covers(check_root, target, *, cache=None):
    """在 check_root 做的检查算不算覆盖了 target。

    `cache`：同一次核对里复用"某路径属于哪个工作区"的结论（每个路径要逐级查目录）。
    只在一次计算内有效，不跨调用保留——之后新建的 worktree 不能被旧结论漏掉。

    两个条件都要满足：
    1. target 就是它本身或在它下面（路径包含）；
    2. 两者在**同一个 Git 工作区**里——不能跨过 worktree / 子模块 / 嵌套仓库的边界。

    只看路径包含是不够的：灵犀的隔离区就放在主项目的 `.lingxi-worktrees/` 下面，
    路径上"被包含"，却是另一份文件现场——主项目的测试跑不到它、`git diff` 也看不见它的改动
    （复核实测：隔离区里改了 app.py 后中断，重开回到主项目、只查主项目，返回 completed）。
    共享 Git 历史也不算同一个现场。两边都不在 Git 工作区里时，只按路径包含判断。
    """
    try:
        c = os.path.normcase(os.path.normpath(check_root))
        t = os.path.normcase(os.path.normpath(target))
        if os.path.commonpath([c, t]) != c:
            return False
    except ValueError:              # 不同盘符
        return False
    return _work_tree_root(target, cache) == _work_tree_root(check_root, cache)


def _work_tree_root(path, cache=None):
    if cache is not None:
        key = os.path.normcase(os.path.normpath(path))
        if key not in cache:
            cache[key] = _find_work_tree_root(path)
        return cache[key]
    return _find_work_tree_root(path)


def _find_work_tree_root(path):
    """path 所在 Git 工作区的根（规范化后），不在任何工作区里返回 None。

    从 path 本身（是目录的话）往上找第一个带 `.git` 的目录。`.git` 是目录（普通仓库、
    独立的嵌套仓库）或文件（linked worktree、子模块）都算边界。path 不存在（比如被删掉的
    文件）时从仍然存在的上级目录找起，照样能认出它原来属于哪个工作区。
    """
    current = os.path.normpath(path)
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return os.path.normcase(current)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def mark_blind_period(v: dict, root: str, reason: str) -> None:
    """记一段"看不见写入"的窗口：枚举失败期间发生的改动无人观察。

    与 `tracking_errors` 的区别是**时态**，这一点是本函数存在的全部理由：
    `tracking_errors` 说的是"**此刻**读不了这个目录"，枚举一旦成功它就该消失；
    盲区说的是"**曾经**有一段时间没人看着"，它不因为"现在能读了"而消失。

    把两者混成一个字段的后果实测过：枚举失败期间真改了 app.py，下一轮枚举成功、
    没跑任何测试，闸门却放行并返回 completed——"能读取目录"被当成了"改动已验证"。

    像 `mark_dirty` 一样作废旧的测试 / diff 结论：盲区里可能改了代码，
    之前那次绿灯覆盖不到它。每次枚举失败都作废（而不只是第一次）——
    目录恢复后又坏掉是新的一段盲区，上一次的记录不能替它背书。
    """
    if not root:
        return
    bump_change_revision(v)     # 盲区里可能改了东西 → 之前的检查证据同样该标过期
    v.setdefault("unknown_changes", {})[root] = reason or "无法完整枚举项目文件"
    v["tests_run"] = False
    v["tests_passed"] = None
    v["tests_reason"] = ""
    v["diff_reviewed"] = False
    v["tests_roots"] = []
    v["diff_roots"] = []


# 证据的状态枚举。"没跑起来"(not_run) / "超时" / "取消" 必须和 "跑了且失败"(failed) 分开：
# 合成一个 False 的话，结果卡就说不出"检查根本没执行"这件事。
_EVIDENCE_STATUS = ("passed", "failed", "not_run", "timeout", "cancelled", "error", "unknown")


# 证据条数上限：结果卡要能读，不是要把整份工具日志再存一遍（完整输出仍在 chat_history）。
_MAX_EVIDENCE = 40
_SUMMARY_MAX = 400
_ARGV_ITEM_MAX = 300

# 命令里像密钥的片段：形如 --token=xxx / api_key=xxx / sk-xxxxxxxx。
# 这是**尽力而为**的脱敏，不是保证——所以除此之外一条更硬的规则是：
# 只记 argv 和 cwd，**绝不记录环境变量**（密钥绝大多数从那里来）。
_SECRET_RE = re.compile(
    r"(?i)((?:api[-_]?key|token|secret|password|passwd|pwd|authorization)\s*[=:]\s*)(\S+)")
_SK_RE = re.compile(r"(?i)\b((?:sk|ghp|gho|xox[abp])-)[A-Za-z0-9_\-]{8,}")


def redact(text: str) -> str:
    """把命令/摘要里像密钥的部分打码。短字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return ""
    out = _SECRET_RE.sub(lambda m: m.group(1) + "***", text)
    return _SK_RE.sub(lambda m: m.group(1) + "***", out)


def identity_fingerprint(*, kind, checker, cwd, path, argv, command) -> str:
    """「这是哪一项检查」的指纹，从**完整未截断**的值算。

    展示字段是要脱敏和截断的（argv 每项 300 字、命令 400 字），拿它们去比对身份
    会把长参数的差异整个抹掉：实测两组 `pytest -k <702 字符>` 只有末尾不同，
    截断后 identity 完全一样，于是第一组的失败被第二组"取代"，卡片写成
    "已执行检查通过"——而第二组根本没跑那个失败用例。

    所以身份用指纹、展示用截断值，两者分开。哈希是单向的，即便完整命令里带了密钥
    也不会因为存了指纹而泄露。
    """
    payload = json.dumps(
        {"kind": kind or "", "checker": checker or "", "cwd": cwd or "",
         "path": path or "", "argv": list(argv) if argv else None,
         "command": command or ""},
        sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def record_evidence(v: dict, **fields) -> dict | None:
    """在**实际执行位置**记一条检查证据，返回该记录（无状态可记时返回 None）。

    调用方必须给出真实发生的事实：`argv` / `cwd` / `exit_code` / `duration_ms` / `status`。
    拿不到退出码就传 None，**不要填 0**——"没跑起来"和"跑了且成功"在结果卡上是完全
    不同的两件事，用 0 冒充会让用户以为检查通过了。
    """
    if not isinstance(v, dict):
        return None
    # **先算指纹，再截断**：顺序反了就等于拿展示数据当身份用。
    identity = identity_fingerprint(
        kind=fields.get("kind"), checker=fields.get("checker"),
        cwd=fields.get("cwd"), path=fields.get("path"),
        argv=fields.get("argv"), command=fields.get("command"))
    argv = fields.get("argv")
    if isinstance(argv, (list, tuple)):
        argv = [redact(str(a))[:_ARGV_ITEM_MAX] for a in argv]
    else:
        argv = None
    status = fields.get("status")
    if status not in _EVIDENCE_STATUS:
        status = "unknown"
    exit_code = fields.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        exit_code = None
    record = {
        "id": f"ev-{uuid.uuid4().hex[:12]}",
        # 归并用这个，不用下面那些被截断过的展示字段
        "identity": identity,
        "kind": fields.get("kind") if fields.get("kind") in ("tests", "check") else "check",
        "run_id": str(fields.get("run_id") or ""),
        "tool_call_id": str(fields.get("tool_call_id") or ""),
        "checker": str(fields.get("checker") or ""),
        "path": str(fields.get("path") or ""),
        "argv": argv,
        "command": redact(str(fields.get("command") or ""))[:_SUMMARY_MAX],
        "cwd": str(fields.get("cwd") or ""),
        "started_at": str(fields.get("started_at") or ""),
        "duration_ms": int(fields.get("duration_ms") or 0),
        "exit_code": exit_code,
        "status": status,
        "summary": redact(str(fields.get("summary") or ""))[:_SUMMARY_MAX],
        "reason": redact(str(fields.get("reason") or ""))[:_SUMMARY_MAX],
        # 采集这一刻的文件变化版本。之后再有改动，版本一涨这条就算过期。
        "change_revision": int(v.get("change_revision", 0) or 0),
    }
    records = v.setdefault("evidence", [])
    records.append(record)
    del records[:-_MAX_EVIDENCE]
    return record


def evidence_is_stale(record, current_revision) -> bool:
    """这条证据是不是在之后的改动里失效了。"""
    if not isinstance(record, dict):
        return False
    return int(record.get("change_revision", 0) or 0) < int(current_revision or 0)


def mark_check(v: dict, rel_path: str, passed: bool | None, checker: str = "") -> None:
    """静态检查工具成功后调用，记录检查结果。

    passed: True=通过, False=有问题, None=无法确定（不支持的语言 / 无检查器）
    checker: 检查器名称（如 "ruff", "py_compile", "check_command"）
    """
    if not rel_path:
        return
    v["checks"][rel_path] = {"passed": passed, "checker": checker}


def mark_tests(v: dict, passed: bool | None, reason: str = "", *, root: str | None = None) -> None:
    """run_tests 工具成功后调用，记录测试结果。

    passed: True=全部通过, False=有失败, None=无法确定
    reason: 简短原因（如 "0 failed", "解析未命中" 等）
    root: 这次测试**实际运行的目录**。只有通过时才记；失败 / 结论不明就清空——
    之后得在对应目录重新跑通，不能靠别处更早的一次绿灯顶替。没有 root 的调用
    不覆盖任何按目录记着的义务。
    """
    v["tests_run"] = True
    v["tests_passed"] = passed
    v["tests_reason"] = reason
    if passed is True:
        if root:
            _add_root(v, "tests_roots", root)
    else:
        v["tests_roots"] = []


def mark_diff_reviewed(v: dict, *, root: str | None = None) -> None:
    """git_diff 工具成功后调用。root：这次 diff 实际覆盖的目录（或限定的路径）。"""
    v["diff_reviewed"] = True
    if root:
        _add_root(v, "diff_roots", root)


def _located_obligations(v):
    """按位置记着的义务：(需要测试覆盖的位置, 需要 diff 覆盖的位置)。

    盲区根目录；以及位置已知的改动文件（绝对路径的 dirty 本身就是位置）。

    两类盲区不参与按位置核对，仍由全局的测试 + diff 了结——否则就是永远过不去的死结：
    占位键 `（工作目录无法确定）` 不是路径，无从谈"覆盖"；**已不存在的目录**没法再在那里
    跑任何检查（里面的内容也已经不在了）。搬家的情形不走这里：确认是同一个项目后，
    义务已经迁到新位置、照常按新目录核对。
    """
    blind = [r for r in (v.get("unknown_changes") or {})
             if os.path.isabs(r) and os.path.isdir(r)]
    located = dict(v.get("dirty_abs") or {})
    for path in v.get("dirty_files") or []:
        if path not in located and os.path.isabs(path):
            located[path] = path
    code = set(v.get("code_dirty_files") or [])
    need_tests = blind + [p for f, p in located.items() if f in code]
    need_diff = blind + [p for f, p in located.items() if f in (v.get("dirty_files") or [])]
    return need_tests, need_diff


def _describe_places(paths, limit=3):
    shown = "、".join(paths[:limit])
    return shown + (f" 等 {len(paths)} 处" if len(paths) > limit else "")


def get_verification_gaps(v: dict) -> list[str]:
    """复查命令追踪的工作区并返回待补充的验证要求。

    空列表表示没有已知验证缺口，不是任务语义正确性的证明。
    非空列表 = 有未验证的改动，AI 应在回复完成前补充验证。
    """
    from .workspace_changes import refresh_workspace_tracking
    refresh_workspace_tracking(v)
    return gaps_from_state(v)


def gaps_from_state(v: dict) -> list[str]:
    """只看验证状态算缺口，**不重扫磁盘**。

    与 `get_verification_gaps` 分开是因为两类调用方需求不同：完成闸门必须先复查工作区
    （命令可能在背后改了文件）；而工具边界保存每次调用都要算一次义务摘要，
    在那里重扫整棵项目树会让每个写操作都付一次全量哈希的代价。
    """
    gaps = [f"工作区当前无法完整枚举：{reason}"
            for reason in v.get("tracking_errors", {}).values()]

    # 盲区：曾经有一段时间看不见写入。它**不随"现在能读了"消失**，只能靠显式的测试 + diff
    # 了结——这既是它与 tracking_errors 的分工，也是它的出口。
    #
    # 注意它**没有**自己独立的一条 gap：那样写就成了无解的死结（测试跑通、diff 看过，
    # 那条声明仍然挂着，闸门永远过不去）。盲区的作用是把下面的测试 / diff 要求**打开**，
    # 满足了就一起消失。
    blind = v.get("unknown_changes") or {}
    _blind_note = ""
    if blind:
        _blind_note = (
            "（曾有一段时间无法完整枚举项目文件："
            + "；".join(f"{root} → {reason}" for root, reason in blind.items())
            + "，期间的写入没有被观察到；目录现在能读取**不等于**那些改动已验证）"
        )

    has_code_changes = bool(v["code_dirty_files"])
    has_any_changes = bool(v["dirty_files"])

    if not has_any_changes and not blind:
        return gaps

    # 1. 测试未通过或未运行（有代码改动、或存在盲区时检查）
    if has_code_changes or blind:
        if not v["tests_run"]:
            _what = (f"代码文件被修改（{', '.join(v['code_dirty_files'])}）"
                     if has_code_changes else "存在未被观察到的工作区写入")
            gaps.append(
                f"{_what}{_blind_note}但尚未运行测试。"
                "请先调用 run_tests 验证改动不会引入回归。"
            )
        elif v["tests_passed"] is False:
            gaps.append("测试未通过（有失败用例），请先修复测试再声称任务完成。")
        elif v["tests_passed"] is not True:
            reason = v.get("tests_reason") or "未取得明确结果"
            gaps.append(f"测试尚未验证：{reason}。不能将结果未知视为通过。")

    # 2. 静态检查未通过
    failed_checks = [
        path for path, result in v["checks"].items()
        if result.get("passed") is False
    ]
    if failed_checks:
        gaps.append(
            f"以下文件的静态检查未通过（{', '.join(failed_checks)}），"
            "请先修复检查问题再声称任务完成。"
        )

    # 3. diff 未审查（有改动、或存在盲区时检查）
    if (has_any_changes or blind) and not v["diff_reviewed"]:
        _what = "本轮已有文件修改" if has_any_changes else "本轮可能有未被观察到的文件修改"
        gaps.append(f"{_what}{_blind_note}，但尚未调用 git_diff 查看最终改动。")

    # 4. 检查必须落在义务所在的目录上（B04 复核）。义务按目录记着、放行却只看上面的全局标志的话，
    #    A 目录里的未知写入能被 B 目录的一次检查清掉——实测过：命令在 A 改了文件后中断，
    #    在 B 重开、只在 B 跑测试看 diff，agent_loop 返回 completed，A 的改动从头到尾没人看过。
    #    只在全局标志已满足时才核对位置：没跑 / 没看的情形上面已经各有一条了。
    need_tests, need_diff = _located_obligations(v)
    tree_cache = {}                 # 本次核对内复用"路径属于哪个 Git 工作区"
    if (has_code_changes or blind) and v["tests_run"] and v["tests_passed"] is True:
        missing = [p for p in need_tests
                   if not any(covers(r, p, cache=tree_cache) for r in v.get("tests_roots") or [])]
        if missing:
            where = _describe_places(v.get("tests_roots") or []) or "（没有记录运行目录）"
            gaps.append(f"测试是在 {where} 跑的，没有覆盖 {_describe_places(missing)}——"
                        "那里的改动还没测过。请先用 run_command 执行 cd 切到对应目录，"
                        "再调用 run_tests 补跑。")
    if (has_any_changes or blind) and v["diff_reviewed"]:
        missing = [p for p in need_diff
                   if not any(covers(r, p, cache=tree_cache) for r in v.get("diff_roots") or [])]
        if missing:
            where = _describe_places(v.get("diff_roots") or []) or "（没有记录查看范围）"
            gaps.append(f"git_diff 查看的是 {where}，没有覆盖 {_describe_places(missing)}——"
                        "那里的改动还没看过。git_diff 在项目目录里执行：目录不在当前项目里、"
                        "或属于另一个 Git 工作区（隔离区 / 子模块 / 嵌套仓库）时，需要把它作为项目"
                        "打开后再查看；在那之前这一项保持未验证。")

    return gaps


def needs_verification(v: dict) -> bool:
    """快速判断是否有任何需要验证的内容。"""
    return bool(v["dirty_files"])


# ── 跨轮次的待验证义务（B03）──

def summarize_obligations(v) -> dict:
    """把当前验证状态里**尚未解决**的部分整理成可持久化的义务摘要。

    只读内存状态，不扫磁盘。没有缺口时返回空义务——这时"已跑的检查都过了"，
    但它仍然只是"已执行的检查通过"，不等于任务做对了（见 CLAUDE.md 的两条边界）。
    """
    from .run_records import empty_pending_verification

    pending = empty_pending_verification()
    if not isinstance(v, dict):
        return pending
    gaps = gaps_from_state(v)
    if not gaps:
        return pending
    pending["files"] = list(v.get("dirty_files") or [])
    pending["code_files"] = list(v.get("code_dirty_files") or [])
    # 位置跟着义务一起跨轮：只存相对路径的话，下一轮换了目录跑检查就又能蒙混过去。
    located = v.get("dirty_abs") or {}
    pending["file_paths"] = {f: located[f] for f in pending["files"] if f in located}
    # 两个来源都要存：`unknown_changes` 是已经确认过的盲区，`tracking_errors` 是此刻还
    # 读不了（它也意味着刚刚那段时间没人看着）。只存后者的话，"目录已经恢复、但盲区未了结"
    # 这个最关键的状态在重启后就没了。
    incomplete = {}
    for source in (v.get("unknown_changes"), v.get("tracking_errors")):
        if isinstance(source, dict):
            incomplete.update({str(k): str(val) for k, val in source.items()})
    pending["tracking_incomplete"] = incomplete
    pending["reason"] = "；".join(gaps)[:2000]
    return pending


def restore_obligations(v: dict, pending) -> None:
    """把上一轮没解决的验证义务填回**刚重置过**的验证状态。

    必须排在 `reset_verification` 之后：先填充再被清掉，等于每轮开头静默清零。

    走 `mark_dirty` 而不是直接塞字段，是为了让它顺带把 `tests_run` / `tests_passed`
    打回未验证——**历史测试成功只是历史证据，不能恢复成当前任务的通行状态**。
    """
    if not isinstance(v, dict) or not isinstance(pending, dict):
        return
    located = pending.get("file_paths") if isinstance(pending.get("file_paths"), dict) else {}
    for path in pending.get("files") or []:
        if isinstance(path, str) and path:
            where = located.get(path)
            mark_dirty(v, path, abs_path=where if isinstance(where, str) and where else None)
    # 扩展名判断不出是代码的（比如无后缀的脚本），按上次的判断补回，不靠这次重算。
    for path in pending.get("code_files") or []:
        if isinstance(path, str) and path and path not in v["code_dirty_files"]:
            v["code_dirty_files"].append(path)
            if path not in v["dirty_files"]:
                v["dirty_files"].append(path)
    tracking = pending.get("tracking_incomplete") or {}
    if isinstance(tracking, dict) and tracking:
        snapshots = v.setdefault("workspace_snapshots", {})
        for root, reason in tracking.items():
            if not isinstance(root, str) or not isinstance(reason, str):
                continue
            # 填回**盲区**，不是 tracking_errors：恢复的这一刻并不知道目录现在读不读得了，
            # 而且要保留的本来就是"曾经有一段没人看着"这件事。若它现在仍然读不了，
            # 本轮第一次复查会把 tracking_errors 重新记上。
            mark_blind_period(v, root, reason)
            # 种一个空基线：让本轮的复查去重新枚举这个根目录，建立一个新的比较起点。
            # previous=None 不会凭空造出 dirty 文件，也不会追认盲区里发生过什么。
            #
            # **只给现在确实存在的目录种**。目录没了（项目搬走、被删）或者根本不是路径
            # （"工作目录无法确定"的占位键）时种下去，每次复查都会枚举失败、重新记一段盲区，
            # 把刚跑完的测试和 diff 结论又作废掉——用户按提示补查也永远收不了尾。
            # 盲区本身照样保留：出口仍是显式的 run_tests + git_diff，只是不再追着一个
            # 不存在的目录反复失败。
            if os.path.isabs(root) and os.path.isdir(root):
                snapshots.setdefault(root, None)


# ── 自动修复循环 ──

# 识别可触发修复循环的工具（与 _REPAIR_TOOLS 对应）
_REPAIR_TOOLS = {"run_tests", "check_code"}


def _is_failure_result(content: str, tool_name: str) -> bool:
    """根据工具返回内容判断是否为失败结果（纯函数，不读全局状态）。"""
    if tool_name == "run_tests":
        # 可靠失败标记：pytest 的 FAILED/ERROR(段)、解析器的 ❌/失败。
        # 不用裸 "error"/"Error" 子串——通过的运行里 warning 文本或名字含 error 的用例
        # 会被误判成失败，白触发一轮修复。
        return any(marker in content for marker in (
            "❌", "FAILED", "ERROR", "失败",
        ))
    if tool_name == "check_code":
        # 有检查问题（不是"✅"开头，也不是"未知"降级）
        return (any(line.startswith("status=failed|checker=") for line in content.splitlines())
                or "⚠️" in content or "❌" in content)
    return False


def check_repair_allowed(
    v: dict,
    tool_name: str,
    result_content: str,
) -> tuple[bool, str]:
    """检测 run_tests/check_code 失败结果，返回 (是否应注入修复提示, 诊断原因)。

    同时原子性更新 v["failure_diagnosis"] 的 attempt 计数。
    由 agent_loop 在工具执行后、往 chat_history 插入 ToolMessage 前调用。

    返回值：
        (True, reason)  —— 应注入修复提示，agent 继续自动修复
        (False, reason) —— 不应注入。reason 为：
            - None: 该工具不触发修复（非 run_tests/check_code）或结果为成功
            - str: 已达最大重试次数（可提示给用户的自然语言）
    """
    if tool_name not in _REPAIR_TOOLS:
        return False, None
    if not v.get("code_dirty_files"):
        return False, None

    if not _is_failure_result(result_content, tool_name):
        # 成功：归零诊断状态（修复后测试通过）
        v["failure_diagnosis"]["tool"] = ""
        v["failure_diagnosis"]["attempt"] = 0
        v["failure_diagnosis"]["reason"] = ""
        return False, None

    diag = v["failure_diagnosis"]
    max_att = diag["max_attempts"]

    # 达到最大重试次数 → 不再注入，交由完成闸门或模型自行收尾
    if diag["attempt"] >= max_att:
        last_reason = diag.get("reason", "")
        limit_reason = f"自动修复已尝试 {max_att} 次仍未通过"
        if last_reason and limit_reason not in last_reason:
            limit_reason += f"：{last_reason}"
        diag["reason"] = limit_reason
        return False, limit_reason

    # 更新诊断状态：记录失败工具、原因、递增尝试次数
    diag["tool"] = tool_name
    diag["attempt"] += 1
    # 从内容提取简短原因（取第一个失败行作为摘要）
    diag["reason"] = _extract_failure_summary(result_content)

    return True, diag["reason"]


def _extract_failure_summary(content: str) -> str:
    """从工具返回内容中提取失败摘要（纯函数）。"""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        # 优先找 pytest 失败摘要行（FAILED / Error / 失败数）
        if "FAILED" in stripped or "失败" in stripped:
            return stripped[:200]
        if stripped.startswith("❌"):
            return stripped[:200]
    return content[:200] if content else "未知失败"


def get_failure_diagnosis(v: dict) -> dict:
    """获取当前会话的失败诊断状态（只读）。"""
    return v.get("failure_diagnosis", {})


def inject_repair_prompt(v: dict) -> str:
    """生成自动修复提示文本，由 agent_loop 注入到 chat_history。

    包含：已尝试次数、失败摘要、明确修复指令。
    """
    diag = v["failure_diagnosis"]
    attempt = diag["attempt"]
    max_att = diag["max_attempts"]
    tool = diag["tool"]
    reason = diag["reason"]

    remaining = max_att - attempt

    prompt = (
        f"⚠️ 自动诊断：刚才的 {tool} 失败了（{reason}）。\n"
        f"这是第 {attempt}/{max_att} 次自动修复尝试，还能重试 {remaining} 次。\n\n"
        "请立即：\n"
        "1. 分析上面的失败输出，定位失败的具体代码位置\n"
        "2. 找到相关文件，只修改导致失败的最小范围\n"
        "3. 重新运行对应的测试或检查命令验证修复\n\n"
        "注意：修复应尽可能小范围，不要改动不相关的代码。"
    )
    return prompt
