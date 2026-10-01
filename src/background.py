"""B10a 后台进程的唯一注册表：冻结归属、真实状态、停止与有界输出。

工具和后续面板共用结构化快照。注册表只活在本进程中，绝不从历史 PID
恢复控制权。锁内只做非阻塞 poll 和短小状态操作，不等进程、不读管道。
"""
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime

from . import limits
from .paths import logger

END_NATURAL = "natural"
# 表示终止请求已被接受且主进程已退出；不证明退出由该请求导致或整棵树已退出。
END_STOPPED = "stopped"
_LOCK = threading.Lock()
_tasks: dict = {}
_counter = [0]
_BOOT_ID = uuid.uuid4().hex
_closing = False


class BackgroundStartError(RuntimeError):
    """进程已创建，但管理线程未能启动；记录保留，已尝试有界停止。"""

    def __init__(self, bg_id, error):
        super().__init__(str(error))
        self.bg_id = bg_id


def ensure_accepting():
    """后台启动入口在 Popen 前检查；应用退出后不再接收新进程。"""
    with _LOCK:
        if _closing:
            raise RuntimeError("应用正在退出，不再启动后台命令")


def capture_session_identity(sess):
    """只在调用线程拍归属；reader/monitor 不再查询 active。

    owner_id 是逻辑会话在本进程的令牌，保存/rekey 不改变它；重置或装载另一个
    会话会换令牌。展示用 session_id 则保留创建时的持久化/运行期标识。
    """
    with sess.snapshot_lock:
        task = sess.current_task or {}
        return {
            "owner_id": sess.background_owner_id,
            "session_id": str(sess.current_session_id or
                              f"unsaved:{sess.background_owner_id}"),
            "session_key": str(sess.key or ""),
            "is_subagent": bool(sess.is_subagent),
            "run_id": str(sess.active_run_id or ""),
            "task_id": task.get("id") or None,
        }


class _Task:
    def __init__(self, bg_id, command, proc, *, cwd, project, session_id,
                 session_key, is_subagent, run_id, task_id, owner_id=None):
        self.bg_id, self.command, self.proc = bg_id, command, proc
        self.cwd, self.project = cwd, project
        self.session_id, self.session_key = session_id, session_key
        self.owner_id = owner_id
        self.is_subagent, self.run_id, self.task_id = is_subagent, run_id, task_id
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self.ended_at = self.elapsed_final = self.exit_code = self.end_kind = None
        self.process_error = ""
        self.output = deque()
        self.max_lines = max(1, limits.BG_MAX_OUTPUT_LINES)
        self.max_chars = max(1, limits.BG_MAX_OUTPUT_CHARS)
        self.max_pending_bytes = max(4, limits.BG_MAX_NO_NEWLINE_BYTES)
        self.output_total_lines = self.output_chars = self.output_total_chars = 0
        self.output_dropped_lines = self.output_dropped_chars = 0
        self.output_head_dropped = self.output_truncated = False
        self.read_error = ""
        self.reader_done = threading.Event()
        self.stop_lock = threading.Lock()  # 单任务控制互斥，不阻塞其他任务的快照/停止
        self.stop_requested = self.stop_dispatched = self.stop_in_progress = False
        self.stop_requested_at = None
        self.stop_attempts = 0
        self.stop_error = self.tree_error = ""


def _confirm_exit_locked(task):
    if task.ended_at is not None:
        return task.exit_code
    try:
        code = task.proc.poll()
        task.process_error = ""
    except Exception as exc:
        task.process_error = f"{type(exc).__name__}: {exc}"[:1000]
        return None
    if code is not None:
        task.ended_at = time.time()
        task.elapsed_final = max(0.0, time.monotonic() - task.started_mono)
        task.exit_code = code
        task.end_kind = END_STOPPED if task.stop_dispatched else END_NATURAL
    return code


def _append_output_locked(task, text):
    """保留最新输出；行/段与字符双限。总已读 = 保留 + 丢弃，标记不混入原文。"""
    if not text:
        return
    task.output_total_lines += 1
    task.output_total_chars += len(text)
    if len(text) > task.max_chars:
        task.output_dropped_chars += len(text) - task.max_chars
        text = text[-task.max_chars:]
    while task.output and (len(task.output) >= task.max_lines or
                           task.output_chars + len(text) > task.max_chars):
        oldest = task.output.popleft()
        task.output_chars -= len(oldest)
        task.output_dropped_chars += len(oldest)
        task.output_dropped_lines += 1
    task.output.append(text)
    task.output_chars += len(text)
    task.output_head_dropped = task.output_dropped_chars > 0
    task.output_truncated = task.output_head_dropped


def _decode_prefix(data):
    """分段时留下未完整的 UTF-8/GBK 字符，避免在字节上限处造乱码。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc), b""
        except UnicodeDecodeError as exc:
            if exc.end == len(data) and exc.reason in (
                    "unexpected end of data", "incomplete multibyte sequence"):
                try:
                    return data[:exc.start].decode(enc), data[exc.start:]
                except UnicodeDecodeError:
                    pass
    return _decode_chunk(data), b""


def _reader(task):
    pending = b""
    try:
        while True:
            raw = task.proc.stdout.read(min(4096, task.max_pending_bytes))
            if not raw:
                break
            # 即使测试流无视 read(n)，临时半行也不超过字节上限。
            offset = 0
            while offset < len(raw):
                take = min(len(raw) - offset, task.max_pending_bytes - len(pending))
                pending += raw[offset:offset + take]
                offset += take
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    with _LOCK:
                        _append_output_locked(task, _decode_chunk(line + b"\n"))
                if len(pending) == task.max_pending_bytes:
                    text, pending = _decode_prefix(pending)
                    with _LOCK:
                        _append_output_locked(task, text)
    except Exception as exc:
        with _LOCK:
            task.read_error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.warning(f"后台任务 [{task.bg_id}] 输出读取失败: {exc}")
    finally:
        # 异常前已读到的半行同样有诊断价值，不能随异常丢掉。
        if pending:
            with _LOCK:
                _append_output_locked(task, _decode_chunk(pending))
        try:
            task.proc.stdout.close()
        except Exception:
            pass
        task.reader_done.set()


def _monitor(task):
    """持续观察实际退出，耗时不延长到用户下次刷新；不持注册表锁等待。"""
    try:
        task.proc.wait()
    except Exception as exc:
        with _LOCK:
            task.process_error = f"监测退出失败: {type(exc).__name__}: {exc}"[:1000]
        logger.warning(f"后台任务 [{task.bg_id}] 监测退出失败: {exc}")
        return
    with _LOCK:
        _confirm_exit_locked(task)
        _evict_ended_locked()


def start(command, proc, *, cwd, project, session_id, session_key,
          is_subagent, run_id, task_id, owner_id=None, spawn_reader=True):
    """接管刚创建的 Popen；调用方必须在 Popen 前冻结归属。"""
    with _LOCK:
        _counter[0] += 1
        # 历史工具消息仍可能提到旧 bg_id；重启后不能让 bg1 指向另一个进程。
        bg_id = f"bg{_counter[0]}-{_BOOT_ID}"
        task = _Task(bg_id, command, proc, cwd=cwd, project=project,
                     session_id=session_id, session_key=session_key,
                     is_subagent=is_subagent, run_id=run_id, task_id=task_id,
                     owner_id=owner_id)
        _tasks[bg_id] = task
        _refresh_locked()
        closing = _closing
    if closing:
        # Popen 与退出闸之间仍可能交错；接管已创建的进程并停止，不能直接抛掉它。
        task.reader_done.set()
        stop(bg_id, wait_timeout=2.0)
        try:
            proc.stdout.close()
        except Exception:
            pass
        raise BackgroundStartError(bg_id, "后台命令创建期间应用开始退出")
    if spawn_reader:
        reader_started = False
        try:
            threading.Thread(target=_monitor, args=(task,), daemon=True,
                             name=f"{bg_id}-exit").start()
            threading.Thread(target=_reader, args=(task,), daemon=True,
                             name=f"{bg_id}-output").start()
            reader_started = True
        except Exception as exc:
            with _LOCK:
                task.read_error = f"后台管理线程启动失败: {exc}"[:1000]
            stop(bg_id, wait_timeout=2.0)
            if not reader_started:
                task.reader_done.set()
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            raise BackgroundStartError(bg_id, exc) from exc
    else:
        task.reader_done.set()
    logger.info(f"后台命令已启动 [{bg_id}]: {command}")
    return bg_id


def _evict_ended_locked():
    ended = sorted((t for t in _tasks.values() if t.ended_at is not None),
                   key=lambda t: (t.ended_at, t.started_mono))
    excess = len(ended) - max(0, limits.BG_MAX_RETAINED_EXITED)
    for task in ended[:max(0, excess)]:
        # 控制调用正在用的记录稍后淘汰，不能让失败重试变成找不到。
        if not task.stop_in_progress:
            _tasks.pop(task.bg_id, None)


def _refresh_locked():
    for task in _tasks.values():
        _confirm_exit_locked(task)
    _evict_ended_locked()


def _snapshot_locked(task):
    ended = task.ended_at
    return {
        "bg_id": task.bg_id, "command": task.command,
        "owner_id": task.owner_id, "session_id": task.session_id,
        "session_key": task.session_key, "is_subagent": task.is_subagent,
        "project": task.project, "cwd": task.cwd,
        "run_id": task.run_id, "task_id": task.task_id,
        "started_at": task.started_at,
        "started_iso": datetime.fromtimestamp(task.started_at).isoformat(timespec="seconds"),
        "ended_at": ended, "exit_code": task.exit_code,
        "running": False if ended is not None else (None if task.process_error else True),
        "end_kind": task.end_kind,
        "elapsed_s": int(task.elapsed_final if ended is not None else
                         max(0.0, time.monotonic() - task.started_mono)),
        "process_error": task.process_error,
        "stop_requested": task.stop_requested,
        "stop_requested_at": task.stop_requested_at,
        "stop_attempts": task.stop_attempts, "stop_in_progress": task.stop_in_progress,
        "stop_dispatched": task.stop_dispatched,
        "stop_error": task.stop_error, "tree_error": task.tree_error,
        "read_error": task.read_error, "output_complete": task.reader_done.is_set(),
        "output_tail": list(task.output), "output_chars": task.output_chars,
        "output_total_lines": task.output_total_lines,
        "output_total_chars": task.output_total_chars,
        "output_head_dropped": task.output_head_dropped,
        "output_truncated": task.output_truncated,
        "output_dropped_lines": task.output_dropped_lines,
        "output_dropped_chars": task.output_dropped_chars,
    }


def get_snapshot(bg_id, *, owner_id=None):
    with _LOCK:
        _refresh_locked()
        task = _tasks.get(bg_id)
        if task is None or (owner_id is not None and task.owner_id != owner_id):
            return None
        return _snapshot_locked(task)


def list_snapshots(*, owner_id=None):
    with _LOCK:
        _refresh_locked()
        return [_snapshot_locked(t) for t in _tasks.values()
                if owner_id is None or t.owner_id == owner_id]


def _kill_proc_tree(proc, timeout=3.0):
    """终止请求的回执，退出仍需另行核对。前台命令也共用此助手。

    Windows 先 taskkill /T，失败时只尝试 kill 主进程并保留树错误。
    Unix 只 kill 主进程；这里不承诺进程组/树的完整终止。
    """
    result = {"request_sent": False, "tree_error": "", "error": ""}
    if proc is None:
        return result
    try:
        if proc.poll() is not None:
            return result
        if timeout <= 0:
            result["error"] = "停止预算已用尽，未发送终止请求"
            return result
        if sys.platform == "win32":
            try:
                killed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=min(3.0, timeout), check=False,
                )
                if killed.returncode == 0:
                    result["request_sent"] = True
                    return result
                result["tree_error"] = (f"taskkill 退出码 {killed.returncode}: " +
                                        _decode_chunk(killed.stdout or b""))[:1000]
            except Exception as exc:
                result["tree_error"] = f"taskkill 请求失败: {type(exc).__name__}: {exc}"[:1000]
        else:
            result["tree_error"] = "此平台只请求终止主进程，未终止完整进程树"
        # 再核对一次，避免对已经退出的 Popen 继续按 PID 发请求。
        if proc.poll() is None:
            proc.kill()
            result["request_sent"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
    return result


def _request_stop(task, timeout):
    """调用者持 task.stop_lock；与注册表锁不嵌套等候进程。"""
    with _LOCK:
        if _confirm_exit_locked(task) is not None:
            return False
        if task.process_error:
            task.stop_error = "无法核对主进程状态，未发送终止请求: " + task.process_error
            return False
        task.stop_requested = task.stop_in_progress = True
        if task.stop_requested_at is None:
            task.stop_requested_at = time.time()
        task.stop_attempts += 1
        task.stop_error = task.tree_error = ""
    try:
        receipt = _kill_proc_tree(task.proc, timeout=timeout)
    except Exception as exc:
        receipt = {"request_sent": False, "error": f"{type(exc).__name__}: {exc}"}
    with _LOCK:
        task.stop_dispatched |= bool(receipt.get("request_sent"))
        task.stop_error = receipt.get("error", "")
        task.tree_error = receipt.get("tree_error", "")
        _confirm_exit_locked(task)
        if task.ended_at is not None and task.stop_dispatched:
            task.end_kind = END_STOPPED
    return True


def _wait_stop(task, deadline):
    with _LOCK:
        if _confirm_exit_locked(task) is not None:
            return
    try:
        task.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        with _LOCK:
            if not task.stop_error:
                task.stop_error = "停止等待超时，尚未确认主进程退出"
    except Exception as exc:
        with _LOCK:
            task.stop_error = f"等待退出失败: {type(exc).__name__}: {exc}"[:1000]
    with _LOCK:
        _confirm_exit_locked(task)


def _stop_result(task, wait_timeout, *, already_exited=False, in_progress=False):
    with _LOCK:
        _confirm_exit_locked(task)
        snap = _snapshot_locked(task)
    return {**snap, "found": True, "already_exited": already_exited,
            "confirmed": snap["running"] is False, "alive": snap["running"],
            "in_progress": in_progress, "stop_in_progress": in_progress,
            "wait_timeout": wait_timeout}


def stop(bg_id, wait_timeout=5.0, *, owner_id=None):
    """单任务请求→等待→核对，共用一个截止时间；失败保留、可重试。

    owner_id=None 供明确的应用级管理；模型工具必须传当前逻辑会话令牌。
    """
    deadline = time.monotonic() + max(0.0, wait_timeout)
    with _LOCK:
        task = _tasks.get(bg_id)
        if task is None:
            return {"bg_id": bg_id, "found": False}
        if owner_id is not None and task.owner_id != owner_id:
            return {"bg_id": bg_id, "found": True, "authorized": False}
    if not task.stop_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        return _stop_result(task, wait_timeout, in_progress=True)
    try:
        with _LOCK:
            already_exited = _confirm_exit_locked(task) is not None
        if not already_exited:
            _request_stop(task, max(0.0, deadline - time.monotonic()))
            _wait_stop(task, deadline)
        return _stop_result(task, wait_timeout, already_exited=already_exited)
    finally:
        with _LOCK:
            task.stop_in_progress = False
        task.stop_lock.release()


def stop_all(wait_timeout=5.0):
    """应用级退出：先请求所有任务，再等退出；整批共用截止时间。

    已在停止中的任务不重复发请求。预算用尽/失败/未知都保留记录并如实返回。
    """
    deadline = time.monotonic() + max(0.0, wait_timeout)
    with _LOCK:
        tasks = list(_tasks.values())
    held = []
    try:
        for index, task in enumerate(tasks):
            if not task.stop_lock.acquire(blocking=False):
                continue
            held.append(task)
            # 给剩余请求及等待留时间，taskkill 的超时也算在整批预算内。
            budget = max(0.0, deadline - time.monotonic()) / (len(tasks) - index + 1)
            _request_stop(task, budget)
        for task in tasks:
            _wait_stop(task, deadline)
    finally:
        for task in held:
            with _LOCK:
                task.stop_in_progress = False
            task.stop_lock.release()
    with _LOCK:
        for task in tasks:
            _confirm_exit_locked(task)
        snapshots = [_snapshot_locked(task) for task in tasks]
        _evict_ended_locked()
    for snap in snapshots:
        if snap["running"] is not False:
            logger.warning(f"退出清理：[{snap['bg_id']}] 未确认退出: "
                           f"{snap['stop_error'] or snap['process_error']}")
        elif snap["tree_error"]:
            logger.warning(f"退出清理：[{snap['bg_id']}] 主进程已退出，进程树请求有问题: "
                           f"{snap['tree_error']}")
    return snapshots


def shutdown(wait_timeout=5.0):
    """最终退出入口，永久关闭启动闸；stop_all 本身可用于普通批量停止。"""
    global _closing
    with _LOCK:
        _closing = True
    return stop_all(wait_timeout)


def _decode_chunk(data):
    """保持前台命令既有 UTF-8 → GBK → replacement 的解码约定。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
