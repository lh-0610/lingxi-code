"""运行中输入队列（B09b）：排队、编辑、删除、暂停与逐项派发的状态与持久化形状。

设计边界：
- **纯状态与形状**：本模块不做 I/O、不碰 Qt、不做网络。持久化由 memory.py 把
  `snapshot(queue)` 深拷贝进会话 JSON 的 progress 信封（在 snapshot_lock 内完成），
  附件内容的读取与校验由调用方（ChatUI）执行——那里才知道怎么向用户报告。
- **队列属于 Session**，随会话 JSON 持久化。旧会话没有 `input_queue` 键 → 空队列，
  行为与从前完全一致。队列数据损坏或版本不认识 → 整块按"进度读不懂"处理，
  原始数据经 progress 的隔离机制留底，聊天历史照常加载。
- **入队 ≠ 入历史**：入队阶段不把消息追加到 chat_history、不调用 sync_user_requests；
  正式派发（接纳）时沿用入队时就分配好的 message_id，由调用方走 _do_send 接入
  真实用户输入标记、B03 run_id 与 B05 要求来源。模型无法触达本模块。

条目状态机（最小集，每个转换的落盘位置见 CLAUDE.md「B09b」节）：
    queued      待派发（可编辑/删除/立即处理）
    held        暂停（附件/归属问题；重试通过校验后回 queued）
    dispatching 派发准备已落盘、等待运行占用（崩溃后 → needs_check）
    admitted    该条已成为一轮运行（锁定编辑/删除）
    done        该轮已结束（终态；outcome 记录程序认定的结果）
    needs_check 崩溃恢复：派发中断窗口，结果未知；不自动重发
"""
import copy
import hashlib
import uuid
from datetime import datetime

from . import limits

QUEUE_VERSION = 1

# 状态常量
QUEUED = "queued"
HELD = "held"
DISPATCHING = "dispatching"
ADMITTED = "admitted"
DONE = "done"
NEEDS_CHECK = "needs_check"

# 可编辑的状态（仅待派发）；可删除的状态（暂停与待核对也能清掉，
# 否则"重试/丢弃"按钮点不动、旧条目赖着生成副本——实测踩过）
EDITABLE_STATES = (QUEUED,)
DELETABLE_STATES = (QUEUED, HELD, NEEDS_CHECK)
# 恢复/重试有意义的状态
RETRYABLE_STATES = (HELD, NEEDS_CHECK)


def new_queue():
    return {"version": QUEUE_VERSION, "paused": False, "pause_reason": "",
            "items": []}


def new_queue_item(text, images, *, project=None, worktree=None, task_id=None,
                   source="send"):
    """构造一个入队条目。message_id 在入队时就分配好，派发（接纳）时沿用。

    images 是 [(path, b64), ...]；这里只保存可恢复的引用与内容身份
    （路径 + 内容 sha256），不把 base64 塞进会话 JSON。
    """
    return {
        "queue_item_id": f"q-{uuid.uuid4().hex}",
        "message_id": f"msg-{uuid.uuid4().hex}",
        "text": str(text or ""),
        "images": _image_refs(images),
        "created_at": datetime.now().isoformat(),
        "state": QUEUED,
        "source": source,                       # send / remote
        # 入队时的现场快照：派发前核对，漂移就暂停并说明，不悄悄发到新现场
        "created_project": project,
        "created_worktree": worktree,
        "created_task_id": task_id,
        "hold_reason": "",
        "error": "",
        "admitted_run_id": "",
        "outcome_status": "",
        "outcome_reason": "",
        "done_at": "",
    }


def _image_refs(images):
    refs = []
    for path, b64 in (images or []):
        refs.append({"path": str(path),
                     "sha256": hashlib.sha256(str(b64).encode("utf-8")).hexdigest()})
    return refs


def load_images(item):
    """派发前重读附件并核对内容身份。返回 (pairs, problem)。

    pairs 是 [(path, b64), ...]；problem 非 None 时描述缺失/不可读/内容变化，
    调用方必须暂停该条并如实说明——绝不静默去掉附件、也不拿新内容顶替发送。
    """
    import base64
    import os

    pairs = []
    for ref in (item.get("images") or []):
        path = ref.get("path") or ""
        if not path or not os.path.exists(path):
            return [], f"附件文件不存在：{path}"
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
        except OSError as e:
            return [], f"附件不可读：{path}（{e}）"
        digest = hashlib.sha256(b64.encode("utf-8")).hexdigest()
        if digest != ref.get("sha256"):
            return [], f"附件内容已变化：{path}"
        pairs.append((path, b64))
    return pairs, None


# ── 结构校验与持久化形状 ──

def normalize(raw):
    """把磁盘上的 input_queue 归一成可用结构，返回 (queue, error_reason)。

    任何结构性问题（不是对象 / 版本不认识 / 条目不是对象 / 必要字段缺失）都整块
    作废并说明——调用方（memory）会把原始数据隔离留底，聊天历史照常加载。
    恢复语义也在这里落地：**新进程恢复的队列默认暂停**，派发中断窗口（dispatching /
    admitted 落了盘但进程没了）标成 needs_check，绝不自动重发。
    """
    if raw is None:
        return new_queue(), ""
    if not isinstance(raw, dict):
        return new_queue(), "input_queue 不是对象"
    ver = raw.get("version")
    if ver != QUEUE_VERSION:
        return new_queue(), f"input_queue 版本 {ver!r} 无法识别"

    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        return new_queue(), "input_queue.items 不是列表"
    items = []
    for it in items_raw:
        if not isinstance(it, dict):
            return new_queue(), "队列条目不是对象"
        if not isinstance(it.get("queue_item_id"), str) or not it["queue_item_id"]:
            return new_queue(), "队列条目缺少 queue_item_id"
        if not isinstance(it.get("message_id"), str) or not it["message_id"]:
            return new_queue(), "队列条目缺少 message_id"
        if not isinstance(it.get("text"), str):
            return new_queue(), "队列条目正文不是字符串"
        state = it.get("state")
        if state not in (QUEUED, HELD, DISPATCHING, ADMITTED, DONE, NEEDS_CHECK):
            return new_queue(), f"队列条目状态 {state!r} 无法识别"
        images = it.get("images") or []
        if not isinstance(images, list) or any(
                not isinstance(r, dict) or not isinstance(r.get("path"), str)
                for r in images):
            return new_queue(), "队列条目附件引用不合法"
        items.append(copy.deepcopy(it))

    queue = {
        "version": QUEUE_VERSION,
        "paused": bool(raw.get("paused", False)),
        "pause_reason": str(raw.get("pause_reason") or ""),
        "items": items,
    }
    # 恢复语义就地落地（默认暂停、待核对）——这是正常解析的一部分，不是错误；
    # 说明写进 pause_reason，绝不从这里走错误通道（否则每次解析都被当成"损坏"）。
    _apply_recovery(queue)
    return queue, ""


def _apply_recovery(queue):
    """新进程恢复：队列默认暂停；派发中断窗口标 needs_check。返回说明（可为空）。"""
    notes = []
    for it in queue["items"]:
        if it["state"] == DISPATCHING:
            it["state"] = NEEDS_CHECK
            it["hold_reason"] = "上次退出时正在派发，结果待核对；历史里可能已有一条未回答的消息"
            notes.append("有处于派发中断窗口的条目")
        elif it["state"] == ADMITTED:
            it["state"] = NEEDS_CHECK
            it["hold_reason"] = "上次退出时该条已接纳成运行，结果未知"
            notes.append("有接纳后中断的条目")
    queue["paused"] = True
    reason = "进程重启，队列默认暂停"
    if notes:
        reason += "；" + "、".join(dict.fromkeys(notes))
    queue["pause_reason"] = reason
    return "队列已按进程重启恢复：默认暂停" + ("" if not notes else "（含待核对条目）")


def snapshot(queue):
    """快照锁内取走的深拷贝（memory._snapshot_progress 调用）。"""
    return copy.deepcopy(queue)


# ── 查询 ──

def items(queue):
    return queue["items"]


def find(queue, queue_item_id):
    for it in queue["items"]:
        if it.get("queue_item_id") == queue_item_id:
            return it
    return None


def next_queued(queue):
    """按入队顺序第一条待派发条目；被 hold 的头部会挡住后面的（不跳号）。"""
    for it in queue["items"]:
        if it["state"] == HELD:
            return None, it
        if it["state"] == QUEUED:
            return it, None
    return None, None


def message_ids(queue):
    return {it.get("message_id") for it in queue["items"]}


def excerpt(text, limit=None):
    limit = limit or limits.QUEUE_EXCERPT_CHARS
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


# ── 变更（调用方负责持久化并在正文失败时回滚） ──

def pending_count(queue):
    """待处理条数：done 只留作结果记录，不占排队容量（否则处理满 20 条后
    队列永久满员——实测踩过）。"""
    return sum(1 for it in queue["items"] if it["state"] != DONE)


def pending_chars(queue):
    return sum(len(it.get("text") or "") for it in queue["items"]
               if it["state"] != DONE)


def enqueue(queue, item):
    """入队。返回 (ok, reason)。超限拒绝并保留输入，绝不静默截断。"""
    if pending_count(queue) >= limits.QUEUE_MAX_ITEMS:
        return False, f"队列已满（待处理上限 {limits.QUEUE_MAX_ITEMS} 条）"
    text = item.get("text") or ""
    if len(text) > limits.QUEUE_MAX_TEXT_CHARS:
        return False, f"单条内容超长（上限 {limits.QUEUE_MAX_TEXT_CHARS} 字符）"
    if pending_chars(queue) + len(text) > limits.QUEUE_MAX_TOTAL_CHARS:
        return False, f"队列内容总量超限（上限 {limits.QUEUE_MAX_TOTAL_CHARS} 字符）"
    queue["items"].append(item)
    return True, ""


def delete(queue, queue_item_id):
    """删除一条队列条目（queued/held/needs_check 可删；派发与运行中锁定）。
    返回 (ok, reason, index)。"""
    for i, it in enumerate(queue["items"]):
        if it.get("queue_item_id") == queue_item_id:
            if it["state"] not in DELETABLE_STATES:
                return False, "该条已进入派发或运行，不能再删除", i
            queue["items"].pop(i)
            return True, "", i
    return False, "队列里没有这条消息", -1


def restore_at(queue, item, index):
    """正文保存失败时把条目放回原位置（内存回滚）。"""
    index = max(0, min(index, len(queue["items"])))
    queue["items"].insert(index, item)


def set_text(queue, queue_item_id, text):
    """编辑正文：保持原身份与位置。返回 (ok, reason, backup)。

    单条与总量都按【替换后】校验——只查单条的话，编辑能把总量顶破
    （实测 100,000 被编辑撑到 119,500）。
    """
    it = find(queue, queue_item_id)
    if it is None:
        return False, "队列里没有这条消息", None
    if it["state"] not in EDITABLE_STATES:
        return False, "该条已进入派发或运行，不能再编辑", None
    if len(text) > limits.QUEUE_MAX_TEXT_CHARS:
        return False, f"单条内容超长（上限 {limits.QUEUE_MAX_TEXT_CHARS} 字符）", None
    others = sum(len(x.get("text") or "") for x in queue["items"]
                 if x is not it and x["state"] != DONE)
    if others + len(text) > limits.QUEUE_MAX_TOTAL_CHARS:
        return False, f"队列内容总量超限（上限 {limits.QUEUE_MAX_TOTAL_CHARS} 字符）", None
    backup = it["text"]
    it["text"] = text
    return True, "", backup


def set_paused(queue, paused, reason=""):
    queue["paused"] = bool(paused)
    queue["pause_reason"] = str(reason or "") if paused else ""


def hold(queue, queue_item_id, reason):
    """派发前校验失败：该条暂停并说明，重试通过校验后回 queued。"""
    it = find(queue, queue_item_id)
    if it is None or it["state"] not in (QUEUED, DISPATCHING):
        return False
    it["state"] = HELD
    it["hold_reason"] = str(reason)
    return True


def retry(queue, queue_item_id):
    """用户点重试：重试通过校验后的入口。返回 (ok, reason)。"""
    it = find(queue, queue_item_id)
    if it is None:
        return False, "队列里没有这条消息"
    if it["state"] not in RETRYABLE_STATES:
        return False, "该条当前状态不能重试"
    it["state"] = QUEUED
    it["hold_reason"] = ""
    it["error"] = ""
    return True, ""


def mark_dispatching(queue, queue_item_id, prepared_at=None):
    """派发准备：接纳记录落盘后才会真正起线程（绝不先删条目再试）。"""
    it = find(queue, queue_item_id)
    if it is None or it["state"] != QUEUED:
        return False
    it["state"] = DISPATCHING
    it["prepared_at"] = (prepared_at or datetime.now().isoformat())
    return True


def unmark_dispatching(queue, queue_item_id, error=""):
    """等待接纳阶段失败（闸超时 / 启动失败 / 保存失败）：条目回到 queued 并说明。"""
    it = find(queue, queue_item_id)
    if it is None or it["state"] != DISPATCHING:
        return False
    it["state"] = QUEUED
    it["error"] = str(error)
    it.pop("prepared_at", None)
    return True


def mark_admitted(queue, queue_item_id):
    """begin_run 已接纳（消息进历史、新一轮已启动）：锁定编辑/删除。"""
    it = find(queue, queue_item_id)
    if it is None or it["state"] != DISPATCHING:
        return False
    it["state"] = ADMITTED
    it["error"] = ""
    return True


def mark_done(queue, queue_item_id, run_id, status, reason=""):
    """该轮结束：记录程序认定的终态（不是模型的“完成了”）。"""
    it = find(queue, queue_item_id)
    if it is None or it["state"] != ADMITTED:
        return False
    it["state"] = DONE
    it["admitted_run_id"] = str(run_id or "")
    it["outcome_status"] = str(status or "")
    it["outcome_reason"] = str(reason or "")[:300]
    it["done_at"] = datetime.now().isoformat()
    return True


def admitted_item(queue):
    for it in queue["items"]:
        if it["state"] == ADMITTED:
            return it
    return None


def requeue_as_new(queue, text, images, *, image_refs=None, project=None,
                   worktree=None, task_id=None, source="send"):
    """needs_check 条目的“重新入队”：作为一条新输入（新身份）追加，不自动重发旧身份。

    image_refs 直接给附件引用（重试场景内容身份已经核过），不重复读文件。
    """
    item = new_queue_item(text, images, project=project, worktree=worktree,
                          task_id=task_id, source=source)
    if image_refs is not None:
        item["images"] = copy.deepcopy(image_refs)
    ok, reason = enqueue(queue, item)
    return item if ok else None, reason


def ownership_change(item, *, project=None, worktree=None, task_id=None):
    """派发前核对入队时的现场快照。返回漂移说明（"" = 没变）。

    task_id 为 None 的条目正常工作：入队时没有任务、派发时有了任务，那是本轮
    建立任务身份的正常路径，不算漂移；入队时已有任务、派发时换成了别的任务才算。
    """
    if (item.get("created_project") or None) != (project or None):
        return "项目归属已改变"
    if (item.get("created_worktree") or None) != (worktree or None):
        return "工作目录（隔离区）已改变"
    old_task = item.get("created_task_id") or None
    if old_task is not None and old_task != (task_id or None):
        return "任务归属已改变"
    return ""
