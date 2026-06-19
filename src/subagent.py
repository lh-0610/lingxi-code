"""Parallel writable sub-agents backed by per-agent git worktrees."""
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from . import session as _session, worktree
from .paths import logger
from .roles import get_system_prompt

_MAX_CONCURRENT = 4
_TIMEOUT_SECONDS = 300


def _run_agent_loop(ui):
    from . import agent
    return agent.agent_loop(ui)


class HeadlessUI:
    """Minimal UI surface used by agent_loop/streaming/tools in worker threads."""

    def __init__(self, *, parent_ui=None, label: str = ""):
        self.parent_ui = parent_ui
        self.label = label
        self._lock = threading.Lock()
        self.buffer: list[tuple[str, str]] = []
        self.bridge = None

    def _append(self, text, tag: str) -> None:
        text = "" if text is None else str(text)
        with self._lock:
            self.buffer.append((tag, text))

    def text(self) -> str:
        with self._lock:
            return "".join(text for _tag, text in self.buffer)

    def show_message(self, text, tag: str = "ai_msg"):
        self._append(text, tag)
        if self.parent_ui is not None and tag in {"tool_tag", "tool_result"}:
            try:
                prefix = f"[{self.label}] " if self.label else ""
                self.parent_ui.show_message(prefix + str(text), tag)
            except Exception:
                pass

    def render_final_markdown(self, markdown_text, speak: bool = True):
        self._append(markdown_text, "markdown")

    def show_retry(self, error_text):
        self._append(error_text, "retry")

    def show_token_usage(self, total_usage, round_usage=None):
        self._append(str(round_usage or total_usage or ""), "token_usage")

    def remove_thinking_indicator(self):
        self._append("", "remove_thinking_indicator")

    def update_thinking_indicator(self, text):
        self._append(text, "thinking_indicator")

    def show_plan(self, items):
        self._append(str(items), "plan")

    def confirm_command(self, command: str) -> tuple[bool, str]:
        self._append(command, "confirm_command")
        return True, ""

    def confirm_edit(self, path: str, diff_text: str) -> tuple[bool, str]:
        self._append(f"{path}\n{diff_text}", "confirm_edit")
        return True, ""


@dataclass
class _ChildRun:
    task: str
    index: int
    session: _session.Session
    child_id: str
    ui: HeadlessUI
    worktree_path: str | None = None
    thread: threading.Thread | None = None
    started: bool = False
    finished: bool = False
    timed_out: bool = False
    error: str = ""
    files_changed: list[str] = field(default_factory=list)


def _changed_files(path: str | None) -> list[str]:
    if not path or not os.path.isdir(path):
        return []
    try:
        result = subprocess.run(
            # quotepath=false：中文文件名不转义；否则报给模型的改动列表是乱码
            ["git", "-c", "core.quotepath=false", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return []
    files = []
    for line in (result.stdout or "").splitlines():
        if len(line) >= 4:
            name = line[3:].strip()
            # 重命名条目 "R  old -> new"：取新名，别把 "old -> new" 当成一个文件名
            if " -> " in name:
                name = name.split(" -> ", 1)[1].strip()
            files.append(name)
    return sorted(set(files))


def _last_ai_text(sess: _session.Session, fallback: str = "") -> str:
    for msg in reversed(sess.chat_history):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif isinstance(item, str):
                        parts.append(item)
                return "\n".join(parts).strip()
    return fallback.strip()


def spawn(tasks: list[str], project_root: str, parent_ui=None) -> list[dict]:
    """Run independent tasks in parallel child sessions and merge their worktrees."""
    tasks = [str(t).strip() for t in (tasks or []) if str(t).strip()]
    if not tasks:
        return [{"task": "", "summary": "没有可派发的子任务。", "files_changed": [], "merge": "skipped", "detail": ""}]
    if not worktree.is_git_repo(project_root):
        return [{
            "task": "",
            "summary": "需 git 仓库才能并行隔离写。",
            "files_changed": [],
            "merge": "skipped",
            "detail": f"非 git 项目：{project_root}",
        }]

    parent_sess = _session.current_session()
    semaphore = threading.Semaphore(_MAX_CONCURRENT)
    runs: list[_ChildRun] = []
    stop_monitor = threading.Event()

    try:
        if parent_ui is not None:
            parent_ui.show_message(f"\n🤖 派生 {len(tasks)} 个子 Agent（并发上限 {_MAX_CONCURRENT}）...\n", "tool_result")
    except Exception:
        pass

    def _monitor_parent_stop(children: list[_ChildRun]):
        while not stop_monitor.wait(0.2):
            if getattr(parent_sess, "stop_flag", False):
                for child in children:
                    child.session.stop_flag = True
                return

    for i, task in enumerate(tasks, 1):
        child = _session.Session()
        child.is_subagent = True
        child.agent_mode = "act"
        child.current_model_index = parent_sess.current_model_index
        child.reasoning_enabled = parent_sess.reasoning_enabled
        child.project = project_root
        _session.bind_thread(child)
        try:
            system_prompt = get_system_prompt()
        finally:
            _session.bind_thread(parent_sess)
        child.chat_history = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=task),
        ]
        _session.register(child)
        child_id = f"subagent-{int(time.time() * 1000)}-{i}"
        ui = HeadlessUI(parent_ui=parent_ui, label=f"子 Agent {i}")
        run = _ChildRun(task=task, index=i, session=child, child_id=child_id, ui=ui)
        wt_path = worktree.create(child, project_root, child_id)
        if not wt_path:
            run.error = "无法创建 worktree。"
            runs.append(run)
            continue
        run.worktree_path = wt_path
        runs.append(run)

        def _runner(r=run):
            with semaphore:
                r.started = True
                _session.bind_thread(r.session)
                try:
                    r.session.is_generating = True
                    _run_agent_loop(r.ui)
                except Exception as e:
                    r.error = str(e)
                    logger.error(f"子 Agent {r.index} 失败: {e}", exc_info=True)
                finally:
                    r.session.is_generating = False
                    r.finished = True
                    _session.unbind_thread()

        t = threading.Thread(target=_runner, name=f"subagent-{i}", daemon=True)
        run.thread = t
        t.start()

    monitor = threading.Thread(target=_monitor_parent_stop, args=(runs,), daemon=True)
    monitor.start()

    deadline = time.monotonic() + _TIMEOUT_SECONDS
    for run in runs:
        if run.thread is None:
            continue
        while run.thread.is_alive():
            if getattr(parent_sess, "stop_flag", False):
                run.session.stop_flag = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                run.timed_out = True
                run.session.stop_flag = True
                break
            run.thread.join(min(0.2, remaining))
    stop_monitor.set()

    for run in runs:
        run.files_changed = _changed_files(run.worktree_path)

    results = []
    for run in runs:
        summary = _last_ai_text(run.session, run.ui.text())
        if run.timed_out:
            results.append({
                "task": run.task,
                "summary": summary or "子 Agent 超时，已请求停止。",
                "files_changed": run.files_changed,
                "merge": "timeout",
                "detail": f"超过 {_TIMEOUT_SECONDS}s，保留 worktree: {run.worktree_path}",
            })
            continue
        if run.error:
            results.append({
                "task": run.task,
                "summary": summary,
                "files_changed": run.files_changed,
                "merge": "error",
                "detail": run.error,
            })
            continue
        if not run.worktree_path:
            results.append({
                "task": run.task,
                "summary": summary,
                "files_changed": run.files_changed,
                "merge": "skipped",
                "detail": "未创建 worktree。",
            })
            continue

        ok, detail = worktree.finish(run.session, apply_changes=True)
        merge = "ok" if ok else "conflict"
        results.append({
            "task": run.task,
            "summary": summary,
            "files_changed": run.files_changed,
            "merge": merge,
            "detail": detail if ok else f"{detail}\n保留 worktree: {run.worktree_path}",
        })
    return results
