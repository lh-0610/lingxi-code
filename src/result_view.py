"""把运行结果快照翻译成**给人看的结论**（纯函数，不依赖 Qt）。

和 Qt 控件分开是有理由的：这一层承担的是"程序到底能断言什么"，那是可以被测试逐条
钉死的判断，不该混在控件布局里。`ui/result_card.py` 只负责把这里的结果画出来。

贯穿全文的取舍：**卡片上的每一句都必须有程序证据支撑**。模型说"已经完成"不算证据；
没跑过测试就不写"测试通过"；`unverified` 不翻译成"代码写完了只是没测"——它并不证明
代码实现是完整的。
"""


# 状态 → 标题。判据只看程序记录的终态，不看模型正文说了什么。
_TITLES = {
    "completed": "本轮回复结束",
    "failed": "本轮执行失败",
    "cancelled": "已停止",
    "unverified": "本轮结束，仍有未验证事项",
    "limit_reached": "已达到运行上限",
}

# 有效检查证据下的 completed 标题：明确限定在"已执行的检查"范围内，
# 不宣称需求都做对了。
_TITLE_COMPLETED_CHECKED = "本轮执行结束，已执行检查通过"
# 跑了检查但没全过 / 结论已失效。必须和上面那条分开——只要有一条没解决，
# 就不能把整轮概括成"检查通过"。
_TITLE_COMPLETED_UNRESOLVED = "本轮执行结束，检查未全部通过"
_TITLE_INTERRUPTED = "上次运行被中断"
_TITLE_RUNNING = "本轮仍在运行"
_TITLE_UNKNOWN = "本轮结果未知"

_STATUS_LABELS = {
    "passed": "通过",
    "failed": "失败",
    "not_run": "未执行",
    "timeout": "超时",
    "cancelled": "已取消",
    "error": "执行出错",
    "unknown": "结果未知",
}

_KIND_LABELS = {"tests": "测试", "check": "静态检查"}

# 文件列表超过这个数就折叠（卡片要能读，不是要把改动清单铺满屏幕）。
FILE_FOLD_THRESHOLD = 8


def _is_active_run(snapshot, live_run_id=None):
    """这张卡描述的是不是**正在跑**的那一轮。

    重绘时必须传 `live_run_id`：磁盘上 `phase=running` 有两种可能——真的被中断了，
    或者那一轮此刻仍在跑。把正在跑的显示成"上次运行被中断"是明确的误报。
    """
    return bool(live_run_id) and snapshot.get("run_id") == live_run_id


def validation_rows(snapshot):
    """实际执行过的检查，逐条给出可读结论。"""
    rows = []
    for record in (snapshot.get("evidence") or {}).get("validation_runs") or []:
        status = record.get("status") or "unknown"
        checker = record.get("checker") or _KIND_LABELS.get(record.get("kind"), "检查")
        label = _STATUS_LABELS.get(status, "结果未知")
        detail = []
        code = record.get("exit_code")
        if code is not None:
            detail.append(f"退出码 {code}")
        else:
            # 没有退出码就照实说没有，**不写 0**——"没跑起来"和"跑了且成功"是两回事。
            detail.append("无退出码")
        duration = record.get("duration_ms")
        if isinstance(duration, int) and duration > 0:
            detail.append(f"{duration / 1000:.1f}s")
        rows.append({
            "id": record.get("id") or "",
            "kind": record.get("kind") or "check",
            "checker": checker,
            "path": record.get("path") or "",
            "status": status,
            "label": label,
            "exit_code": code,
            "duration_ms": duration if isinstance(duration, int) else 0,
            "stale": bool(record.get("stale")),
            "detail": "、".join(detail),
            "summary": record.get("summary") or "",
            "reason": record.get("reason") or "",
            "argv": record.get("argv"),
            "command": record.get("command") or "",
            "cwd": record.get("cwd") or "",
        })
    return rows


def summarize_checks(snapshot):
    """汇总本轮检查：`"none"` 没跑过 / `"clear"` 都过了 / `"unresolved"` 还有没解决的。

    **不能用 `any(passed)`**：静态检查过了、pytest 挂了，那也是"有一条通过"，
    标题就会写成"已执行检查通过"——而这一轮恰恰是没通过的。判据必须是
    「**所有**未过期的记录都通过」，任何失败 / 未执行 / 超时 / 结果未知都让它不成立。

    过期记录不参与判定：它的结论已经不代表当前代码了。但它也不能顶替一次有效通过，
    所以"只剩过期记录"算 `unresolved`，不是 `clear`。
    """
    rows = validation_rows(snapshot)
    if not rows:
        return "none"
    fresh = [r for r in rows if not r["stale"]]
    if not fresh:
        return "unresolved"          # 跑过，但结论全部失效
    if all(r["status"] == "passed" for r in fresh):
        return "clear"
    return "unresolved"


def has_fresh_pass(snapshot):
    """本轮已执行的检查是不是**全部**通过（结果卡据此决定能不能说"检查通过"）。"""
    return summarize_checks(snapshot) == "clear"


def describe(snapshot, *, live_run_id=None):
    """把快照翻译成结果卡的全部内容。返回纯数据 dict，便于逐条断言。"""
    if not isinstance(snapshot, dict):
        return None

    phase = snapshot.get("phase") or ""
    outcome = snapshot.get("outcome")
    rows = validation_rows(snapshot)
    files = list((snapshot.get("evidence") or {}).get("changed_files") or [])
    pending = snapshot.get("pending_verification") or {}

    # ── 标题 ──
    if phase == "running":
        if _is_active_run(snapshot, live_run_id):
            title, tone = _TITLE_RUNNING, "running"
        else:
            # 没收到终态。展示中断**不改写磁盘上的 phase/outcome**，只是这样呈现。
            title, tone = _TITLE_INTERRUPTED, "interrupted"
    elif outcome == "completed":
        checks = summarize_checks(snapshot)
        if checks == "clear":
            title, tone = _TITLE_COMPLETED_CHECKED, "ok"
        elif checks == "unresolved":
            # 跑过检查但没全过。既不能说"检查通过"，也不能只说"回复结束"
            # （那会把一次有失败项的运行说得像纯问答）。
            title, tone = _TITLE_COMPLETED_UNRESOLVED, "warn"
        else:
            # 纯问答 / 调研：没有检查证据就只说回复结束，绝不暗示"测过了"。
            title, tone = _TITLES["completed"], "ok"
    elif outcome in _TITLES:
        title = _TITLES[outcome]
        tone = {"failed": "bad", "cancelled": "muted",
                "unverified": "warn", "limit_reached": "warn"}[outcome]
    else:
        title, tone = _TITLE_UNKNOWN, "muted"

    # ── 保存状态：与运行结果完全分开的一行 ──
    save_note = ""
    saved = snapshot.get("saved")
    if saved is False:
        if snapshot.get("save_body_written") and not snapshot.get("save_index_written"):
            save_note = "会话正文已保存，但侧栏索引更新失败（下次打开会自动补回）。"
        else:
            detail = snapshot.get("save_error") or snapshot.get("save_skipped") or "原因未知"
            save_note = f"最新进度未保存：{detail}"

    # ── 未验证事项 ──
    pending_files = list(pending.get("files") or [])
    pending_reason = pending.get("reason") or ""

    return {
        "title": title,
        "tone": tone,
        "reason": snapshot.get("reason") or "",
        "run_id": snapshot.get("run_id") or "",
        "session_id": snapshot.get("session_id") or "",
        "project": snapshot.get("project"),
        # 本轮实际的落点（worktree 优先）。"查看改动"必须用它：拿项目根去 diff 一个
        # 在隔离区里跑的轮次，只会回一句"工作区干净"。
        "work_dir": snapshot.get("work_dir") or snapshot.get("project"),
        "task_id": snapshot.get("task_id"),
        "files": files,
        # 没有净差异证据，所以只说"本轮涉及"——写"净修改 N 个文件"是它给不出的结论。
        "files_caption": f"本轮涉及 {len(files)} 个文件" if files else "",
        "files_folded": len(files) > FILE_FOLD_THRESHOLD,
        "validations": rows,
        "has_validations": bool(rows),
        "has_stale": any(r["stale"] for r in rows),
        "pending_files": pending_files,
        "pending_reason": pending_reason,
        "has_pending": bool(pending_files or pending_reason
                            or pending.get("tracking_incomplete")),
        "save_note": save_note,
        "diff_reviewed": bool((snapshot.get("evidence") or {}).get("diff_reviewed")),
        "display_source": snapshot.get("display_source") or "live",
        # 有真实资料才给入口：没有目录就点不出差异，没有证据就没有输出可看。
        "can_view_diff": bool(snapshot.get("work_dir") or snapshot.get("project")),
        "can_view_output": any(r["summary"] or r["reason"] or r["argv"] for r in rows),
    }


def plain_text(view) -> str:
    """结果卡的纯文本形态（日志 / 无 GUI 环境 / 测试断言用）。"""
    if not view:
        return ""
    lines = [view["title"]]
    if view["reason"]:
        lines.append(f"原因：{view['reason']}")
    if view["files_caption"]:
        lines.append(f"{view['files_caption']}：{'、'.join(view['files'])}")
    for row in view["validations"]:
        target = f" {row['path']}" if row["path"] else ""
        stale = "（已过期）" if row["stale"] else ""
        lines.append(f"{_KIND_LABELS.get(row['kind'], '检查')}{target} · "
                     f"{row['checker']}：{row['label']}{stale}（{row['detail']}）")
    if view["has_pending"]:
        lines.append(f"未验证：{view['pending_reason'] or '存在未验证改动'}")
    if view["save_note"]:
        lines.append(f"保存：{view['save_note']}")
    return "\n".join(lines)
