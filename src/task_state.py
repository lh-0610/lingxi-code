"""任务状态、来源追溯与固定限制管理（B05）。

数据模型与设计原则：
1. 任务身份（Task）：
   - 首次有效创建计划时建立 task_id；失败或被拒绝的调用不留半个任务。
   - 同一任务改步骤、换做法时保持 task_id；结构调整必须有 explanation 与变化摘要。
   - 明确开始新任务（update_plan 传 new_task=True）时，归档旧任务，建立新任务。
   - 还没有任务时 new_task=True 按首次建立处理，不留撤销记录；空计划不能开始新任务。
   - 模型发起的新任务切换可撤销；撤销恢复原任务，被撤销的新任务进归档（不消失），
     不回滚文件或未验证义务。
   - 任务、计划、归档与撤销记录在同一个 snapshot_lock 临界区里一起改，存盘拍不到半截状态。

2. 要求与来源：
   - 真实用户消息分配稳定消息 ID（msg-uuid），存盘、重载后保持一致。
   - 程序在输入入口填写来源身份，区分 user_input, resume, repair, gate, vision_bridge。
   - 仅持久化允许的元数据白名单（lingxi_internal, lingxi_kind, lingxi_recovery, lingxi_message_id）。
   - 旧消息无来源元数据时如实标为 legacy_unknown，任何路径都不把它改标成 user_input。
   - 要求归属按消息 ID "认领"：已归入某个任务（含已归档）的消息不会再被别的任务收走；
     不依赖历史下标（压缩会移动、删除消息）。来源可信度在归入时写进 request_sources。
   - 原始要求与补充要求保留原文和来源关联，改计划/清空计划/回退步骤/历史压缩都不能删除引用；
     原文不在历史里时如实说"已不在"，不声称它被摘要覆盖。
   - 提供固定限制（pinned_constraints），仅用户可修改或删除，计划工具不得改写。
   - 固定限制按输入原文保存（复验最终裁定）：不解析、不剥除任何行首字符——
     字面量 *、命令行旗标、编号、"-" 开头统统是内容；项目符号只用于显示层。
     打开/保存/重开/修改/再存，用户写下的字符不得增删。
     预算 PINNED_CONSTRAINTS_MAX_CHARS=1000，保存前校验超限拒绝，防止静默截断。
   - 任务上下文有独立预算（TASK_CONTEXT_MAX_CHARS=3000），对最终实际输出计数、是
     硬上限：固定限制完整保留，目标及其来源有保底展示空间，装不下的段折叠成摘要
     并注明省略了什么（补读入口只指向真实存在的面板与工具）。
   - 验证状态只有一套结论（verification_status，基于 verification.gaps_from_state）：
     dirty_files / 盲区记录只是"涉及过"的记录，义务是否仍未解除以缺口判定为准，
     面板与模型上下文共用，不再各说各话。

3. 运行态与持久化：
   - 任务状态作为快照随 progress.task 落盘；旧任务快照进 progress.archived_tasks。
   - 坏任务数据整块隔离到 quarantined_progress，不拖垮聊天历史，不抹掉唯一副本。
   - 尾部运行态注入 roles.get_volatile_context()，不进稳定 system prompt，保护 prompt caching。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .paths import logger

TASK_VERSION = 1

# 固定限制（用户强制设定）的字符数上限：保存前严格校验，超限直接阻止保存，避免静默截断
PINNED_CONSTRAINTS_MAX_CHARS = 1000

# 尾部运行态中任务上下文（要求+限制+计划变化）的独立字符预算
TASK_CONTEXT_MAX_CHARS = 3000

# 消息允许的元数据键名白名单：防止任意 additional_kwargs 原样保存或冒充授权
ALLOWED_MESSAGE_METADATA_KEYS = frozenset({
    "lingxi_message_id",
    "lingxi_internal",
    "lingxi_kind",
    "lingxi_recovery",
})

# 程序注入的内部消息种类。tag_internal_message 只认这几种：拼错的种类不能被悄悄
# 归成某一类（旧实现一律当 repair），那样来源记录就是编的。
INTERNAL_MESSAGE_KINDS = frozenset({
    "resume",          # 继续任务的恢复提示
    "recovery",        # 异常中断后的恢复提示
    "repair",          # 验证失败后的自动修复提示
    "gate",            # 试图完成任务时的验证闸门提示
    "vision_bridge",   # 视觉模型识别转述说明
})

# 来源种类枚举
VALID_MESSAGE_KINDS = frozenset({
    "user_input",      # 真实用户输入（发送入口打标）
    "legacy_unknown",  # 旧版历史中无法确定来源的消息
}) | INTERNAL_MESSAGE_KINDS

# 任务要求来源的可信度：只有这两种 HumanMessage 能成为"要求"。
# legacy_unknown 可以被收进首次建立的任务（旧会话的原始要求不能丢），但来源如实标注，
# 展示与注入时都写明"来源未确认"，不追认为用户原话。
REQUEST_SOURCE_KINDS = frozenset({"user_input", "legacy_unknown"})

_VALID_STEP_STATUS = ("pending", "in_progress", "done")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    """生成唯一任务 ID。"""
    return f"task-{uuid.uuid4().hex}"


def new_message_id() -> str:
    """生成唯一消息 ID。"""
    return f"msg-{uuid.uuid4().hex}"


def tag_user_message(msg: Any, msg_id: str | None = None) -> Any:
    """在真实用户输入入口给 HumanMessage 填入身份与稳定 ID。"""
    final_id = str(msg_id or getattr(msg, "id", None) or new_message_id())
    msg.id = final_id
    ak = getattr(msg, "additional_kwargs", None)
    if not isinstance(ak, dict):
        ak = {}
        msg.additional_kwargs = ak
    ak["lingxi_kind"] = "user_input"
    ak["lingxi_message_id"] = final_id
    ak.pop("lingxi_internal", None)
    return msg


def tag_internal_message(msg: Any, kind: str, extra: dict | None = None, msg_id: str | None = None) -> Any:
    """程序注入的内部消息（恢复/修复/闸门/视觉桥接）填入身份。

    kind 必须是 INTERNAL_MESSAGE_KINDS 之一，否则抛 ValueError——调用方拼错种类是程序
    错误，静默归类只会让来源记录失真。
    """
    if kind not in INTERNAL_MESSAGE_KINDS:
        raise ValueError(f"未知的内部消息种类：{kind!r}")
    final_id = str(msg_id or getattr(msg, "id", None) or new_message_id())
    msg.id = final_id
    ak = getattr(msg, "additional_kwargs", None)
    if not isinstance(ak, dict):
        ak = {}
        msg.additional_kwargs = ak
    ak["lingxi_internal"] = True
    ak["lingxi_kind"] = kind
    ak["lingxi_message_id"] = final_id
    if extra and isinstance(extra, dict):
        for k, v in extra.items():
            if k in ALLOWED_MESSAGE_METADATA_KEYS:
                ak[k] = v
    return msg


def get_message_id(msg: Any) -> str:
    """获取消息的稳定 ID。"""
    mid = getattr(msg, "id", None)
    if isinstance(mid, str) and mid:
        return mid
    ak = getattr(msg, "additional_kwargs", None) or {}
    mid = ak.get("lingxi_message_id")
    if isinstance(mid, str) and mid:
        return mid
    return ""


def get_message_kind(msg: Any) -> str:
    """获取消息的来源身份分类。"""
    t = msg.__class__.__name__
    if t != "HumanMessage":
        return t.lower().replace("message", "")
    ak = getattr(msg, "additional_kwargs", None) or {}
    if ak.get("lingxi_internal") is True:
        k = ak.get("lingxi_kind")
        return k if isinstance(k, str) and k else "internal"
    k = ak.get("lingxi_kind")
    if isinstance(k, str) and k in VALID_MESSAGE_KINDS:
        return k
    return "legacy_unknown"


def is_verified_user_input(msg: Any) -> bool:
    """判断是否为程序确认过的真实用户输入。"""
    if msg.__class__.__name__ != "HumanMessage":
        return False
    ak = getattr(msg, "additional_kwargs", None) or {}
    if ak.get("lingxi_internal") is True:
        return False
    return ak.get("lingxi_kind") == "user_input"


def is_any_user_input(msg: Any) -> bool:
    """判断是否为用户输入（包含旧会话中未标记的 HumanMessage 兼容降级）。"""
    if msg.__class__.__name__ != "HumanMessage":
        return False
    ak = getattr(msg, "additional_kwargs", None) or {}
    if ak.get("lingxi_internal") is True:
        return False
    return True


def extract_plain_text(content: Any) -> str:
    """提取消息纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p.get("text", ""))
        return "\n".join(texts) if texts else "[图片]"
    return str(content or "")


# ══════════════════════════════════════════════════════════════
# Task 数据结构校验与归一化
# ══════════════════════════════════════════════════════════════

def create_task(
    task_id: str | None = None,
    request_message_ids: list[str] | None = None,
    pinned_constraints: list[str] | None = None,
    last_plan_change_reason: str = "",
    last_plan_change_summary: str = "",
    created_at: str | None = None,
    request_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """创建新的 Task 字典。"""
    return {
        "version": TASK_VERSION,
        "id": task_id or new_task_id(),
        "created_at": created_at or _now(),
        "request_message_ids": list(request_message_ids or []),
        # 每条要求在**归入任务的那一刻**记下的来源可信度（id → user_input / legacy_unknown）。
        # 存下来而不是每次从剩余历史重新判断：历史会被压缩，消息会消失，但"这条要求当初
        # 是不是程序确认过的用户原话"这件事不能跟着消失或改变。
        "request_sources": dict(request_sources or {}),
        "pinned_constraints": list(pinned_constraints or []),
        "last_plan_change_reason": str(last_plan_change_reason or ""),
        "last_plan_change_summary": str(last_plan_change_summary or ""),
        "plan_version": 1,
        "model_summary": "",
    }


def _normalize_request_fields(raw: dict, where: str) -> tuple[list[str], dict[str, str], str]:
    """校验 request_message_ids / request_sources，返回 (ids, sources, 错误原因)。"""
    req_ids = raw.get("request_message_ids", [])
    if not isinstance(req_ids, list) or not all(isinstance(x, str) for x in req_ids):
        return [], {}, f"{where}.request_message_ids 必须是字符串列表"
    sources = raw.get("request_sources", {})
    if sources is None:
        sources = {}
    if not isinstance(sources, dict):
        return [], {}, f"{where}.request_sources 不是对象"
    for k, v in sources.items():
        if not isinstance(k, str) or v not in REQUEST_SOURCE_KINDS:
            return [], {}, f"{where}.request_sources 含非法项"
    return list(req_ids), dict(sources), ""


def normalize_task(raw: Any, session_id: str = "") -> tuple[dict[str, Any] | None, str]:
    """校验磁盘上的 task 快照，返回 (task_dict 或 None, 错误原因)。

    若为 None 表示会话尚未建立任务（普通问答）。
    若结构损坏，返回 (None, 原因)，由上层整块作废隔离。
    """
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "task 不是对象"

    ver = raw.get("version")
    if ver is not None and (not isinstance(ver, int) or ver > TASK_VERSION):
        return None, f"task 版本 {ver!r} 无法识别"

    tid = raw.get("id")
    if not isinstance(tid, str) or not tid:
        return None, "task.id 缺失或非法"

    req_ids, sources, why = _normalize_request_fields(raw, "task")
    if why:
        return None, why

    constraints = raw.get("pinned_constraints", [])
    if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
        return None, "task.pinned_constraints 必须是字符串列表"

    plan_ver = raw.get("plan_version", 1)
    if not isinstance(plan_ver, int) or plan_ver < 1:
        plan_ver = 1

    return {
        "version": TASK_VERSION,
        "id": tid,
        # 缺失就如实留空：补成"现在"会把一个不知道何时建立的任务说成刚建立的。
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else "",
        "request_message_ids": req_ids,
        "request_sources": sources,
        "pinned_constraints": list(constraints),
        "last_plan_change_reason": raw.get("last_plan_change_reason") if isinstance(raw.get("last_plan_change_reason"), str) else "",
        "last_plan_change_summary": raw.get("last_plan_change_summary") if isinstance(raw.get("last_plan_change_summary"), str) else "",
        "plan_version": plan_ver,
        "model_summary": raw.get("model_summary") if isinstance(raw.get("model_summary"), str) else "",
    }, ""


# 归档原因的种类：switched = 被新任务替换；undone_switch = 撤销了一次切换，被撤销的新任务
_ARCHIVE_KINDS = ("switched", "undone_switch")


def normalize_archived_tasks(raw: Any, session_id: str = "") -> tuple[list[dict[str, Any]], str]:
    """校验已归档任务历史。"""
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return [], "archived_tasks 不是列表"

    archived = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"archived_tasks[{idx}] 不是对象"
        tid = item.get("id")
        if not isinstance(tid, str) or not tid:
            return [], f"archived_tasks[{idx}] 缺少 id"
        req_ids, sources, why = _normalize_request_fields(item, f"archived_tasks[{idx}]")
        if why:
            return [], why
        constraints = item.get("pinned_constraints", [])
        if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
            return [], f"archived_tasks[{idx}].pinned_constraints 非法"
        final_plan = item.get("final_plan", [])
        if not isinstance(final_plan, list):
            return [], f"archived_tasks[{idx}].final_plan 非法"
        for p in final_plan:
            if not isinstance(p, dict) or not isinstance(p.get("text"), str) or p.get("status") not in _VALID_STEP_STATUS:
                return [], f"archived_tasks[{idx}].final_plan 包含非法步骤"
        kind = item.get("archive_kind", "switched")
        archived.append({
            "id": tid,
            "created_at": item.get("created_at") if isinstance(item.get("created_at"), str) else "",
            "archived_at": item.get("archived_at") if isinstance(item.get("archived_at"), str) else "",
            "request_message_ids": req_ids,
            "request_sources": sources,
            "pinned_constraints": list(constraints),
            "final_plan": [dict(p) for p in final_plan],
            "final_run_id": item.get("final_run_id") if isinstance(item.get("final_run_id"), str) else "",
            "archive_kind": kind if kind in _ARCHIVE_KINDS else "switched",
            "archive_reason": item.get("archive_reason") if isinstance(item.get("archive_reason"), str) else "",
            "model_summary": item.get("model_summary") if isinstance(item.get("model_summary"), str) else "",
        })
    return archived, ""


def normalize_task_switch(raw: Any, session_id: str = "") -> tuple[dict[str, Any] | None, str]:
    """校验新任务切换的可撤销记录。"""
    if raw is None:
        return None, ""
    if not isinstance(raw, dict):
        return None, "last_task_switch 不是对象"
    new_tid = raw.get("new_task_id")
    if not isinstance(new_tid, str) or not new_tid:
        return None, "last_task_switch 缺少 new_task_id"
    version = raw.get("switch_version")
    if not isinstance(version, int) or version < 0:
        return None, "last_task_switch.switch_version 非法"
    prev_raw = raw.get("previous_task")
    prev_task = None
    if prev_raw is not None:
        prev_task, why = normalize_task(prev_raw, session_id)
        if why:
            return None, f"last_task_switch.previous_task 损坏：{why}"
    return {
        "session_id": raw.get("session_id") if isinstance(raw.get("session_id"), str) else "",
        "new_task_id": new_tid,
        "previous_task": prev_task,
        "previous_plan": [dict(p) for p in raw.get("previous_plan", [])] if isinstance(raw.get("previous_plan"), list) else [],
        "switch_version": version,
        "reason": raw.get("reason") if isinstance(raw.get("reason"), str) else "",
        "switched_at": raw.get("switched_at") if isinstance(raw.get("switched_at"), str) else "",
    }, ""


# ══════════════════════════════════════════════════════════════
# 固定限制（Pinned Constraints）校验
# ══════════════════════════════════════════════════════════════

# 固定限制按**输入原文**保存（复验最终裁定）：不解析、不剥除任何行首字符——
# "- * 参数"、"- - 参数"、"--force"、"- 列表"、编号、圆点统统是用户内容；
# 展示所需的项目符号只存在于显示层（面板画 •，模型上下文每条一行）。
# 逐行拆分只为承载"每行一条"的输入形态与字符计数：空白行丢弃、行首尾空白规整，
# 其余字符一个不动。由此"存入、还原、再次解析"天然可逆——没有任何一次保存
# 会改动用户写下的字符，也就不存在"根据内容猜测哪些字符属于用户"的问题。


def validate_pinned_constraints(constraints: list[str] | str) -> tuple[bool, str, list[str]]:
    """校验用户设定的固定限制，逐条按原文返回。

    返回 (是否合法, 错误说明, 逐条原文列表)：
    - 字符串输入（编辑对话框）：按行拆分（每行一条），丢弃空白行、规整行首尾
      空白，其余字符（包括行首的 -、*、编号）一律按原文保留；
    - 列表输入（已是逐条原文）：原样保留。
    打开 → 保存 → 重新打开 → 修改 → 再保存，用户写下的字符不得增删。
    严格受限在 PINNED_CONSTRAINTS_MAX_CHARS 预算内，超限直接拒绝，绝不静默截断。
    """
    if isinstance(constraints, str):
        raw_lines = constraints.splitlines()
    elif isinstance(constraints, (list, tuple)):
        raw_lines = [str(x) for x in constraints]
    else:
        return False, "固定限制必须是字符串或列表", []

    cleaned: list[str] = []
    for line in raw_lines:
        s = line.strip()
        if s:
            cleaned.append(s)

    total_chars = sum(len(c) for c in cleaned)
    if total_chars > PINNED_CONSTRAINTS_MAX_CHARS:
        return (
            False,
            f"固定限制总长度超出预算（当前 {total_chars} 字符，上限 {PINNED_CONSTRAINTS_MAX_CHARS} 字符），请精简后再保存。",
            cleaned,
        )
    return True, "", cleaned


# ══════════════════════════════════════════════════════════════
# 计划变动对比与摘要
# ══════════════════════════════════════════════════════════════

def summarize_plan_change(old_plan: list[dict], new_plan: list[dict]) -> str:
    """比对新旧两版计划，生成可读的修改摘要（展示在任务面板与模型上下文）。"""
    if not old_plan and not new_plan:
        return ""
    if not old_plan:
        return f"创建初始计划，共 {len(new_plan)} 步"
    if not new_plan:
        return f"清空原计划（原包含 {len(old_plan)} 步）"

    old_texts = [p.get("text", "") for p in old_plan]
    new_texts = [p.get("text", "") for p in new_plan]

    added = [t for t in new_texts if t not in old_texts]
    removed = [t for t in old_texts if t not in new_texts]

    changes: list[str] = []
    if added:
        changes.append(f"新增 {len(added)} 步")
    if removed:
        changes.append(f"移除 {len(removed)} 步")

    if old_texts == new_texts:
        # 仅状态变化
        new_done = sum(1 for p in new_plan if p.get("status") == "done")
        old_done = sum(1 for p in old_plan if p.get("status") == "done")
        if new_done > old_done:
            changes.append(f"推进 {new_done - old_done} 步")
        elif new_done < old_done:
            changes.append(f"回退 {old_done - new_done} 步")
        else:
            changes.append("步骤进度微调")
    elif not added and not removed:
        changes.append("重排步骤顺序")

    return "，".join(changes) if changes else "调整计划步骤"


# ══════════════════════════════════════════════════════════════
# 任务来源关联与提取
# ══════════════════════════════════════════════════════════════
#
# 归属规则（不依赖历史下标——压缩会移动、删除消息）：
#   - 一条要求消息一旦归入某个任务（当前或已归档），它的 ID 就"被认领"了；
#     之后的同步只收**尚未被任何任务认领**的消息。于是 A 的要求不会在 B 的下一次
#     补充时被重新带进 B，已归档任务里的引用也不会因为原消息被压缩掉而消失。
#   - 来源可信度在归入的那一刻写进 request_sources，之后不再从剩余历史重新猜。
#   - 只有首次建立任务时收 legacy_unknown（旧会话的原始要求不能丢），并如实标注；
#     之后的补充只收发送入口确认过的 user_input。
#   - 任何路径都**不改写**消息的来源种类。缺 ID 的旧消息只补一个 ID，种类保持原样。


def _ensure_message_id(msg: Any) -> str:
    """给消息补一个稳定 ID（已有就沿用），**不触碰它的来源种类**。"""
    mid = get_message_id(msg)
    if not mid:
        mid = new_message_id()
        msg.id = mid
    ak = getattr(msg, "additional_kwargs", None)
    if not isinstance(ak, dict):
        ak = {}
        msg.additional_kwargs = ak
    ak["lingxi_message_id"] = mid
    return mid


def _request_kind(msg: Any) -> str | None:
    """这条消息能否作为任务要求，能的话返回它的来源种类。"""
    if msg.__class__.__name__ != "HumanMessage":
        return None
    kind = get_message_kind(msg)
    return kind if kind in REQUEST_SOURCE_KINDS else None


def _claimed_request_ids(sess: Any) -> set[str]:
    claimed: set[str] = set()
    task = getattr(sess, "current_task", None)
    if isinstance(task, dict):
        claimed.update(task.get("request_message_ids") or [])
    for t in getattr(sess, "archived_tasks", None) or []:
        if isinstance(t, dict):
            claimed.update(t.get("request_message_ids") or [])
    return claimed


def _unclaimed_requests(sess: Any, *, include_legacy: bool) -> list[tuple[str, str]]:
    """按历史顺序列出尚未归入任何任务的要求消息 (id, 来源种类)。"""
    claimed = _claimed_request_ids(sess)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for msg in getattr(sess, "chat_history", None) or []:
        kind = _request_kind(msg)
        if kind is None or (kind == "legacy_unknown" and not include_legacy):
            continue
        mid = _ensure_message_id(msg)
        if mid in claimed or mid in seen:
            continue
        seen.add(mid)
        found.append((mid, kind))
    return found


def sync_user_requests(sess: Any) -> list[str]:
    """把尚未归入任何任务的真实用户输入追加为当前任务的补充要求。

    只追加，从不删除：原消息被历史压缩移除后，引用照样保留（展示时如实说明原文已不在）。
    返回更新后的 ID 列表。
    """
    with sess.snapshot_lock:
        task = getattr(sess, "current_task", None)
        if not isinstance(task, dict):
            return []
        ids = task.setdefault("request_message_ids", [])
        sources = task.setdefault("request_sources", {})
        for mid, kind in _unclaimed_requests(sess, include_legacy=False):
            ids.append(mid)
            sources[mid] = kind
        return list(ids)


def extract_task_requests(
    chat_history: list[Any],
    request_message_ids: list[str],
    request_sources: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """从会话历史中提取对应 request_message_ids 的消息原文与来源信息。

    第一条为原始要求，后续为补充要求。返回字典列表：
    {
        "id": "msg-uuid",
        "is_initial": bool,
        "kind": "user_input" | "legacy_unknown" | "missing",
        "text": str,
        "index": int,   # 仅供展示排序，不作为身份
    }
    kind 优先取归入任务时记下的来源（request_sources），不按现在的消息重新判断。
    """
    if not request_message_ids:
        return []
    sources = request_sources or {}

    target_ids = set(request_message_ids)
    found_by_id: dict[str, dict[str, Any]] = {}

    for idx, msg in enumerate(chat_history or []):
        mid = get_message_id(msg)
        if mid and mid in target_ids and mid not in found_by_id:
            found_by_id[mid] = {
                "id": mid,
                "kind": sources.get(mid) or get_message_kind(msg),
                "text": extract_plain_text(msg.content),
                "index": idx,
            }

    results = []
    for idx, mid in enumerate(request_message_ids):
        if mid in found_by_id:
            info = dict(found_by_id[mid])
            info["is_initial"] = (idx == 0)
            results.append(info)
        else:
            # 原消息不在当前历史里。不知道它是否进了压缩摘要，就不说它"已被覆盖"。
            results.append({
                "id": mid,
                "is_initial": (idx == 0),
                "kind": "missing",
                "text": f"[原始消息已不在当前会话历史中（可能已被上下文压缩移除），无法展示原文；消息 ID: {mid}]",
                "index": -1,
            })
    return results


def task_requests(sess: Any, task: dict | None = None) -> list[dict[str, Any]]:
    """当前任务（或给定任务）的要求原文与来源。"""
    task = task if task is not None else getattr(sess, "current_task", None)
    if not isinstance(task, dict):
        return []
    return extract_task_requests(
        getattr(sess, "chat_history", None) or [],
        task.get("request_message_ids") or [],
        task.get("request_sources") or {},
    )


def create_or_attach_task(sess: Any, plan: list[dict] | None = None, explanation: str = "") -> dict[str, Any]:
    """首次有效创建计划时建立任务身份。

    已有任务：同步补充要求后返回它。
    没有任务：以尚未被认领的要求消息建立新任务；给了 plan 就在**同一个快照锁临界区**
    里一起写入计划——存盘不能拍到"任务已建立、计划仍为空"的半截状态。
    plan=None 表示调用方不改计划（例如编辑固定限制时补建任务）。
    """
    with sess.snapshot_lock:
        task = getattr(sess, "current_task", None)
        if isinstance(task, dict) and task.get("id"):
            sync_user_requests(sess)
            return task

        requests = _unclaimed_requests(sess, include_legacy=True)
        new_task = create_task(
            request_message_ids=[mid for mid, _ in requests],
            request_sources=dict(requests),
            last_plan_change_reason=explanation,
            last_plan_change_summary=summarize_plan_change([], plan or []),
        )
        sess.current_task = new_task
        if plan is not None:
            sess.current_plan = [dict(p) for p in plan]
    logger.info(f"建立新任务身份: {new_task['id']}，关联 {len(requests)} 条要求来源")
    return new_task


# ══════════════════════════════════════════════════════════════
# 任务切换与撤销（Switch & Undo）
# ══════════════════════════════════════════════════════════════

def _latest_user_input_id(sess: Any) -> str:
    """触发切换的那条要求：历史里最后一条程序确认过的用户输入。"""
    for msg in reversed(getattr(sess, "chat_history", None) or []):
        if _request_kind(msg) == "user_input":
            return _ensure_message_id(msg)
    return ""


def _without(ids: list[str], sources: dict[str, str], drop: set[str]) -> tuple[list[str], dict[str, str]]:
    return ([i for i in ids if i not in drop],
            {k: v for k, v in sources.items() if k not in drop})


def switch_to_new_task(
    sess: Any,
    new_plan_items: list[dict],
    explanation: str,
    final_run_id: str = "",
) -> tuple[dict[str, Any], str]:
    """明确开始新任务：归档旧任务，建立新任务，并留出可撤销记录。

    返回 (新任务 dict, 操作说明)。
    必须有非空 explanation 和至少一个计划步骤。不回滚任何文件改动或未验证义务。

    还没有任务时这不是"切换"：按首次建立处理，不留撤销记录——撤销到"没有任务"
    既无意义，还会让撤销逻辑去碰与之无关的归档。
    """
    explanation = (explanation or "").strip()
    if not explanation:
        raise ValueError("开始新任务必须填写 explanation 说明切换原因。")
    if not new_plan_items:
        raise ValueError("开始新任务至少需要一个计划步骤。")

    with sess.snapshot_lock:
        old_task = getattr(sess, "current_task", None)
        if not isinstance(old_task, dict) or not old_task.get("id"):
            task = create_or_attach_task(sess, plan=new_plan_items, explanation=explanation)
            return task, f"已建立任务 {task['id']}"

        old_plan = [dict(p) for p in (getattr(sess, "current_plan", None) or [])]

        # 触发切换的那条消息在发送时已作为补充要求归进了旧任务，这里把它移给新任务：
        # 它是新任务的原始要求，不是旧任务的补充。
        trigger = _latest_user_input_id(sess)
        archived = getattr(sess, "archived_tasks", None)
        if not isinstance(archived, list):
            archived = []
            sess.archived_tasks = archived
        # 被撤销的切换（undone_switch）不算认领：撤销时它的要求已并回原任务，那份归档只是
        # 留底。算进去的话，撤销后没等用户再开口就重做切换，触发消息拿不回来，新任务一条来源都没有。
        archived_claims = {i for t in archived
                           if isinstance(t, dict) and t.get("archive_kind") != "undone_switch"
                           for i in (t.get("request_message_ids") or [])}
        moved = {trigger} if trigger and trigger not in archived_claims else set()

        old_ids, old_sources = _without(list(old_task.get("request_message_ids") or []),
                                        dict(old_task.get("request_sources") or {}), moved)
        previous_task = json.loads(json.dumps(old_task))
        previous_task["request_message_ids"] = old_ids
        previous_task["request_sources"] = old_sources

        archive_entry = {
            "id": old_task["id"],
            "created_at": old_task.get("created_at") or "",
            "archived_at": _now(),
            "request_message_ids": list(old_ids),
            "request_sources": dict(old_sources),
            "pinned_constraints": list(old_task.get("pinned_constraints") or []),
            "final_plan": old_plan,
            "final_run_id": final_run_id,
            "archive_kind": "switched",
            "archive_reason": explanation,
            "model_summary": old_task.get("model_summary") or "",
        }

        new_ids = [trigger] if moved else []
        new_task = create_task(
            request_message_ids=new_ids,
            request_sources={trigger: "user_input"} if moved else {},
            last_plan_change_reason=explanation,
            last_plan_change_summary=summarize_plan_change([], new_plan_items),
        )

        version = int(getattr(sess, "task_switch_version", 0) or 0) + 1
        # 任务、计划、归档、撤销记录在同一临界区里一起换：存盘看到的要么全是旧的，要么全是新的。
        archived.append(archive_entry)
        sess.task_switch_version = version
        sess.last_task_switch = {
            "session_id": getattr(sess, "current_session_id", "") or "",
            "new_task_id": new_task["id"],
            "previous_task": previous_task,
            "previous_plan": old_plan,
            "switch_version": version,
            "reason": explanation,
            "switched_at": _now(),
        }
        sess.current_task = new_task
        sess.current_plan = [dict(p) for p in new_plan_items]
    logger.info(f"已归档旧任务: {old_task['id']}，切换原因: {explanation}")
    return new_task, f"已切换至新任务 {new_task['id']}"


def undo_task_switch(sess: Any, expected_new_task_id: str, expected_version: int) -> tuple[bool, str]:
    """撤销模型发起的新任务切换。

    安全核对：切换版本、新任务 ID 与当前任务都对得上才允许撤销。
    - 原任务恢复为当前任务；新任务期间用户说的话并入原任务，作为补充要求。
    - 被撤销的新任务不消失：以 archive_kind="undone_switch" 进入归档，它的要求、计划仍可查。
    - 只移除**这次切换**归档的那一项，不碰其它归档。
    **绝不回滚文件改动，绝不删除新任务已执行的工具记录或待验证义务。**
    """
    with sess.snapshot_lock:
        last_switch = getattr(sess, "last_task_switch", None)
        if not isinstance(last_switch, dict):
            return False, "没有可撤销的任务切换记录。"

        if last_switch.get("switch_version") != expected_version:
            return False, f"任务切换版本不匹配（当前版本 {last_switch.get('switch_version')} != 请求版本 {expected_version}），已忽略迟到操作。"

        if last_switch.get("new_task_id") != expected_new_task_id:
            return False, f"当前任务 ID ({last_switch.get('new_task_id')}) 与待撤销任务 ({expected_new_task_id}) 不符。"

        current = getattr(sess, "current_task", None)
        if not isinstance(current, dict) or current.get("id") != expected_new_task_id:
            return False, "当前任务已不是这次切换建立的任务，撤销已取消。"

        previous_task = last_switch.get("previous_task")
        if not isinstance(previous_task, dict) or not previous_task.get("id"):
            # 旧格式记录：切换前根本没有任务。没有可恢复的对象，收起撤销入口即可。
            sess.last_task_switch = None
            return False, "这次切换之前没有任务，无需撤销；已收起撤销入口。"

        restored = json.loads(json.dumps(previous_task))
        r_ids = list(restored.get("request_message_ids") or [])
        r_sources = dict(restored.get("request_sources") or {})
        for mid in current.get("request_message_ids") or []:
            if mid not in r_ids:
                r_ids.append(mid)
                kind = (current.get("request_sources") or {}).get(mid)
                if kind:
                    r_sources[mid] = kind
        restored["request_message_ids"] = r_ids
        restored["request_sources"] = r_sources

        archived = getattr(sess, "archived_tasks", None)
        if not isinstance(archived, list):
            archived = []
            sess.archived_tasks = archived
        for i in range(len(archived) - 1, -1, -1):
            item = archived[i]
            if (isinstance(item, dict) and item.get("id") == previous_task["id"]
                    and item.get("archive_kind", "switched") == "switched"):
                del archived[i]
                break
        archived.append({
            "id": current["id"],
            "created_at": current.get("created_at") or "",
            "archived_at": _now(),
            "request_message_ids": list(current.get("request_message_ids") or []),
            "request_sources": dict(current.get("request_sources") or {}),
            "pinned_constraints": list(current.get("pinned_constraints") or []),
            "final_plan": [dict(p) for p in (getattr(sess, "current_plan", None) or [])],
            "final_run_id": "",
            "archive_kind": "undone_switch",
            "archive_reason": f"用户撤销了这次任务切换（原切换原因：{last_switch.get('reason') or '未说明'}）",
            "model_summary": current.get("model_summary") or "",
        })

        sess.current_task = restored
        sess.current_plan = [dict(p) for p in (last_switch.get("previous_plan") or [])]
        sess.last_task_switch = None

    logger.info(f"已撤销任务切换，恢复任务: {restored['id']}，被撤销的任务 {current['id']} 已归档")
    return True, ("已撤销任务切换：原任务的要求、固定限制与计划已恢复，新任务期间的要求并入原任务作为补充；"
                  "被撤销的任务记录已归档（文件修改与待验证事项保持不变）。")


# ══════════════════════════════════════════════════════════════
# 验证状态（面板与运行态共用，只陈述有证据的事实）
# ══════════════════════════════════════════════════════════════

_FAILED_EV = ("failed", "error", "timeout")


def _run_state_has_content(v: dict) -> bool:
    """运行态里是否已经装进东西（begin_run 之后：上一轮义务并入了、或本轮已产生记录）。

    用于区分两个时态：运行态还是**全新**的（刚加载会话、恢复并入尚未发生）时，
    pending_verification 快照是义务的唯一线索；运行态一旦有内容就以它为准——
    快照在运行中可能还没刷新，只信它会把上一轮的旧账当成这一轮的待办。
    """
    return bool(
        v.get("dirty_files") or v.get("code_dirty_files") or v.get("checks")
        or v.get("unknown_changes") or v.get("tracking_errors") or v.get("evidence")
        or v.get("tests_run") or v.get("diff_reviewed")
        or int(v.get("change_revision", 0) or 0)
    )


def _live_gaps(v: dict) -> list[str]:
    """当前仍未解除的验证义务：复用现有验证体系的判定，不另养一套放行规则。

    判定不了就是不知道：内部出错时如实报"无法核对"，绝不能因为异常就显示成
    没有缺口——那等于拿故障当通行凭证。
    """
    try:
        from .verification import gaps_from_state
        return list(gaps_from_state(v))
    except Exception:
        from .paths import logger
        logger.exception("验证缺口判定失败")
        return ["验证状态暂时无法核对（内部错误），请重新运行测试并查看 diff 确认。"]


def _snapshot_gaps(pending: dict) -> list[str]:
    """运行态尚未建立时，把持久化的义务快照转述成待办说明。

    快照由 summarize_obligations 在上一轮收尾时写就，begin_run 会把它并回运行态、
    之后以实时判定为准。这里只做原样转述，不做任何"是否已解除"的判断——此时也
    没有任何本轮检查证据，不可能据此显示"已验证"。
    """
    gaps: list[str] = []
    files = [str(f) for f in (pending.get("files") or []) if f] \
        or [str(f) for f in (pending.get("code_files") or []) if f]
    if files:
        shown = "、".join(files[:8]) + ("等" if len(files) > 8 else "")
        gaps.append(f"以下文件的改动尚未完成针对性验证（上一轮遗留）：{shown}。")
    for root, reason in (pending.get("tracking_incomplete") or {}).items():
        gaps.append(f"验证盲区 {root}：{reason}。期间的写入没有被观察到，"
                    "需要显式运行测试并查看 diff 后才能解除。")
    if not gaps and isinstance(pending.get("reason"), str) and pending["reason"]:
        gaps.append(pending["reason"])
    return gaps


def verification_status(sess: Any) -> dict[str, Any]:
    """汇总当前会话的验证状态，给任务面板和运行态上下文共用。

    「还有哪些义务未解决」复用现有验证体系的判定（verification.gaps_from_state，
    含检查范围、Git 工作区边界与证据过期规则）。dirty_files / unknown_changes 只是
    **记录**——本轮涉及过哪些改动、发生过哪些盲区；记录还在不等于义务没解除，
    解除与否以 gaps 为准。"checked" 只认当前运行态里未被改动作废的检查证据，
    历史成功不是当前运行的通行凭证。

    返回 {"state", "gaps", "passed", "failed", "inconclusive"}，state 取值：
      - pending：存在尚未解除的验证缺口
      - blind：缺口涉及未被完整观察的盲区
      - failed：没有未解除的缺口，但最近的检查证据有未通过的
      - checked：没有缺口，且有未过期的检查证据且都通过（只说明"已执行的检查通过"，
        不等于需求已满足）
      - none：没有任何检查证据——**不能**说成已验证
    """
    pending = getattr(sess, "pending_verification", None)
    pending = pending if isinstance(pending, dict) else {}
    v = getattr(sess, "verification", None)
    v = v if isinstance(v, dict) else {}

    revision = int(v.get("change_revision", 0) or 0)
    latest: dict[str, dict] = {}
    for rec in v.get("evidence") or []:
        if not isinstance(rec, dict):
            continue
        if int(rec.get("change_revision", 0) or 0) < revision:
            continue        # 之后又有改动，这条证据已过期
        latest[rec.get("identity") or rec.get("id") or str(len(latest))] = rec
    passed = sum(1 for r in latest.values() if r.get("status") == "passed")
    failed = sum(1 for r in latest.values() if r.get("status") in _FAILED_EV)
    inconclusive = len(latest) - passed - failed

    if _run_state_has_content(v):
        gaps = _live_gaps(v)
        blind_owed = bool(v.get("unknown_changes") or v.get("tracking_errors"))
    else:
        gaps = _snapshot_gaps(pending)
        blind_owed = bool(pending.get("tracking_incomplete"))

    if gaps:
        state = "blind" if blind_owed else "pending"
    elif failed:
        state = "failed"
    elif passed and not inconclusive:
        state = "checked"
    else:
        state = "none"
    return {"state": state, "gaps": gaps, "passed": passed,
            "failed": failed, "inconclusive": inconclusive}


# ══════════════════════════════════════════════════════════════
# 尾部运行态上下文组装（Volatile Context）
# ══════════════════════════════════════════════════════════════

_SOURCE_NOTES = {
    "legacy_unknown": "；旧会话消息，来源未确认，可能不是用户原话",
    "missing": "；原文已不在当前历史中",
}
_SECTION_SEP = "\n\n"
_REQ_HEADER = "# 当前任务需求来源\n"
_CONS_HEADER = "# 用户固定限制（必须严格遵守，任何工具调用或代码改动不得违反；每行一条）\n"

# 任务目标及其来源的保底展示空间：标题 + 一条要求的出处（消息 ID）与开头原文。
# 固定限制、验证义务和计划说明再长，也不能把这块挤没。
_GOAL_MIN_RESERVE = 240


def _shorten(text: str, limit: int) -> str:
    """压成单行并截断（用于把可能多行的义务说明并进列表项）。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _request_label(idx: int, total: int) -> str:
    if idx == 0:
        return "用户原始要求"
    if idx == total - 1:
        return "最新补充要求"
    return f"补充要求 {idx}"


def _request_head(item: dict, label: str, folded: bool) -> str:
    note = _SOURCE_NOTES.get(item.get("kind"), "")
    fold = "；内容过长已折叠，完整原文见该消息" if folded else ""
    return f"**{label}**（消息 ID: `{item['id']}`{note}{fold}）："


_REQ_MIN_TEXT = 80      # 选入的每条要求至少展示这么多原文
_OMIT_RESERVE = 48      # 给"另有 N 条未展示"留的位置


def _render_requests(items: list[dict], budget: int) -> list[str]:
    """在 budget 字符内渲染要求块（块之间按 _SECTION_SEP 计长）。

    两步分配，免得排在前面的一条长要求把后面的整条挤掉：
      1. 选入：**原始目标保底**（它是任务的目标本身，先于一切补充被选入），
         然后最新补充，其余按新旧，每条先只占"标题 + 前 _REQ_MIN_TEXT 字"，
         放不下的（优先级最低的那些）只计数；
      2. 扩展：剩余预算按 最新补充 → 原始要求 → 其余补充 的顺序扩成全文，
         扩不满的保持折叠并注明消息 ID。
    不会有哪一条因为"优先"而越过预算；原始目标的最小块在 goal 保底预算内必然装得下。
    """
    n = len(items)
    if n == 0 or budget <= 0:
        return []
    sep = len(_SECTION_SEP)
    select_order = [0, n - 1] + list(range(1, n - 1)) if n > 1 else [0]
    extend_order = [n - 1, 0] + list(range(n - 2, 0, -1)) if n > 1 else [0]

    def block(idx: int, text_len: int | None) -> str:
        item = items[idx]
        label = _request_label(idx, n)
        text = item.get("text") or ""
        if text_len is None or text_len >= len(text):
            return _request_head(item, label, folded=False) + "\n" + text
        return _request_head(item, label, folded=True) + "\n" + text[:text_len].rstrip() + "…"

    chosen: list[int] = []
    rendered: dict[int, str] = {}
    used = 0
    for idx in select_order:
        b = block(idx, _REQ_MIN_TEXT)
        if used + len(b) + sep + _OMIT_RESERVE > budget:
            break
        chosen.append(idx)
        rendered[idx] = b
        used += len(b) + sep
    omitted = n - len(chosen)
    chosen_set = set(chosen)

    avail = budget - used - (_OMIT_RESERVE if omitted else 0)
    for idx in extend_order:
        if idx not in chosen_set or avail <= 0:
            continue
        cur = rendered[idx]
        full = block(idx, None)
        if len(full) - len(cur) <= avail:
            rendered[idx] = full
            avail -= len(full) - len(cur)
        else:
            longer = block(idx, _REQ_MIN_TEXT + avail)
            if len(longer) <= len(cur) + avail:
                rendered[idx] = longer
                avail -= len(longer) - len(cur)

    blocks = [rendered[i] for i in sorted(chosen)]
    if omitted:
        blocks.append(f"（另有 {omitted} 条要求因长度预算未展示，见历史消息）")
    return blocks


def _constraints_section(task: dict, budget: int) -> str:
    """固定限制段：逐条原文、每行一条，不加项目符号（项目符号是显示层的事）。

    保存时按内容字符数（≤ PINNED_CONSTRAINTS_MAX_CHARS）限制了输入：最坏情形
    （1000 条单字限制）渲染成本约 2×1000 + 标题，budget 按 3000 − 目标保底计必然
    容纳。这里的逐条裁剪只兜底旧版本或外部写入的超长数据——裁剪必须显式注明
    省略了什么，绝不无声截断，也绝不假装展示完整。
    """
    constraints = [str(c) for c in (task.get("pinned_constraints") or []) if str(c)]
    if not constraints:
        return ""
    full = _CONS_HEADER + "\n".join(constraints)
    if len(full) <= budget:
        return full
    note = ("（固定限制共 {n} 条，因上下文长度预算只完整展示部分条目，"
            "其余见任务面板，可在「编辑固定限制」里重新整理）").format(n=len(constraints))
    kept: list[str] = []
    used = len(_CONS_HEADER) + len(note) + 1
    for c in constraints:
        cost = len(c) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(c)
        used += cost
    return _CONS_HEADER + "\n".join(kept + [note])


def _verification_section(gaps: list[str]) -> str:
    """尚未解除的验证义务。结论与任务面板同源（verification_status），不另行判断。"""
    if not gaps:
        return ""
    lines = ["# 尚未解决的验证义务（声称完成任务前必须逐项解除）"]
    for g in gaps[:6]:
        lines.append("- " + _shorten(g, 300))
    if len(gaps) > 6:
        lines.append(f"- （另有 {len(gaps) - 6} 项未逐条列出，见任务面板的「执行与验证状态」）")
    return "\n".join(lines)


def _verification_note(gaps: list[str]) -> str:
    if not gaps:
        return ""
    return ("# 尚未解决的验证义务\n"
            f"（共 {len(gaps)} 项因上下文长度预算未逐条展开，见任务面板的「执行与验证状态」；"
            "解除义务仍需显式运行 run_tests 并用 git_diff 查看改动）")


def _plan_change_section(task: dict) -> str:
    reason = task.get("last_plan_change_reason")
    summary = task.get("last_plan_change_summary")
    if not reason and not summary:
        return ""
    parts = []
    if reason:
        parts.append(f"- 调整原因：{str(reason)[:300]}")
    if summary:
        parts.append(f"- 变化摘要：{str(summary)[:200]}")
    return "# 最近计划调整说明\n" + "\n".join(parts)


def _plan_change_note(task: dict) -> str:
    reason = task.get("last_plan_change_reason")
    summary = task.get("last_plan_change_summary")
    if not reason and not summary:
        return ""
    return "# 最近计划调整说明\n（调整说明因上下文长度预算未展开，见任务面板的「执行计划步骤」）"


def _requests_section(items: list[dict], model_summary: Any, budget: int) -> str:
    """任务目标及其来源段：标题、各块与摘要在 budget 内计长。

    原始目标的最小块（标题 + 消息 ID + 最小摘录）是本段的保底；模型摘要这类
    锦上添花的内容装不下时先让位，不挤占目标。
    """
    if not items or budget <= 0:
        return ""
    extra: list[str] = []
    if model_summary and isinstance(model_summary, str):
        extra.append(f"【模型整理摘要（仅供参考，不替代用户原话）】：{model_summary[:300]}")
    n = len(items)
    floor = len(_request_head(items[0], _request_label(0, n), folded=True)) + 1 + _REQ_MIN_TEXT + 1
    while extra and budget - len(_REQ_HEADER) - sum(len(e) + len(_SECTION_SEP) for e in extra) < floor:
        extra.pop()
    inner = budget - len(_REQ_HEADER) - sum(len(e) + len(_SECTION_SEP) for e in extra)
    blocks = _render_requests(items, inner)
    if not blocks:
        return ""
    return _REQ_HEADER + _SECTION_SEP.join(blocks + extra)


def format_task_volatile_context(sess: Any, max_chars: int = TASK_CONTEXT_MAX_CHARS) -> list[str]:
    """组装当前任务的要求、固定限制、计划调整与未解决事项。

    预算约定：返回各段按 "\\n\\n" 连接后**总长不超过 max_chars**——对最终实际输出
    计数（含标题、换行、列表符号与各段分隔），默认 3000 是硬上限，不做整段切片：
    - 固定限制完整保留（保存时的内容预算保证最坏渲染成本也在预算内）；
    - 任务目标及其来源保底 _GOAL_MIN_RESERVE 的展示空间，剩余预算尽量放开原文；
    - 验证义务与计划说明装不下时折叠成一行摘要并注明省略了什么；补读入口只指向
      真实存在的任务面板与 run_tests / git_diff，不虚构工具。
    """
    task = getattr(sess, "current_task", None)
    if not isinstance(task, dict) or not task.get("id"):
        return []

    sep = len(_SECTION_SEP)
    parts: list[str] = []
    used = 0

    def add(s: str) -> None:
        nonlocal used
        if not s:
            return
        used += len(s) + (sep if parts else 0)
        parts.append(s)

    def replace_last(s: str) -> None:
        nonlocal used
        used -= len(parts[-1])
        parts[-1] = s
        used += len(s)

    # 还能给"后续段 + 目标保底空间"的余量；任何段要挤进来都得先留出目标的保底。
    def room() -> int:
        return max_chars - used - _GOAL_MIN_RESERVE - (sep if parts else 0)

    # 1. 固定限制：完整保留；只在旧数据超长、连目标保底都保不住时才显式裁剪。
    add(_constraints_section(task, max_chars - _GOAL_MIN_RESERVE - sep))

    # 2. 尚未解除的验证义务（与面板同一结论）：完整装不下就折叠成摘要。
    gaps = verification_status(sess).get("gaps") or []
    v_full = _verification_section(gaps)
    v_note = _verification_note(gaps)
    v_full_shown = False
    if v_full:
        if len(v_full) + sep <= room():
            add(v_full)
            v_full_shown = True
        else:
            add(v_note)

    # 3. 最近计划调整说明：完整装不下就折叠成摘要。若连摘要都放不下（义务全文
    #    刚好把余量吃光），把义务也降为摘要、换计划说明入场——每一段被省略的
    #    内容都要有一行交代，不能悄悄消失。
    p_full = _plan_change_section(task)
    if p_full:
        p_note = _plan_change_note(task)
        if len(p_full) + sep <= room():
            add(p_full)
        elif len(p_note) + sep <= room():
            add(p_note)
        elif v_full_shown and len(v_note) + len(p_note) + 2 * sep <= room() + len(v_full):
            replace_last(v_note)
            if len(p_full) + sep <= room():
                add(p_full)
            else:
                add(p_note)

    # 4. 任务目标及其来源：剩余预算全部给它（至少保底，由 room() 的预留保证）。
    goal_budget = max_chars - used - (sep if parts else 0)
    add(_requests_section(task_requests(sess, task), task.get("model_summary"), goal_budget))
    return parts
