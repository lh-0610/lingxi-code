"""Parallel writable sub-agents backed by per-agent git worktrees."""
import threading
import time
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from . import session as _session, worktree
from .paths import logger
from .roles import get_system_prompt
from .agent_result import AgentResult
from .verification import mark_dirty

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
    result: AgentResult | None = None
    files_changed: list[str] = field(default_factory=list)


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
                    if parent_sess.stop_flag or r.session.stop_flag:
                        r.result = AgentResult("cancelled", "父任务或子任务已停止。")
                    else:
                        r.result = _run_agent_loop(r.ui)
                except Exception as e:
                    r.result = AgentResult("failed", str(e))
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

    results = []
    for run in runs:
        summary = _last_ai_text(run.session, run.ui.text())
        outcome = run.result
        if not isinstance(outcome, AgentResult):
            outcome = AgentResult("failed", f"子 Agent 未返回有效运行结果: {outcome!r}")
        if run.error:
            outcome = AgentResult("failed", run.error)
        if run.timed_out:
            outcome = AgentResult("limit_reached", f"超过 {_TIMEOUT_SECONDS}s，已请求停止。")
        elif parent_sess.stop_flag or run.session.stop_flag:
            outcome = AgentResult("cancelled", "父任务或子任务已停止。")

        merge = "timeout" if run.timed_out else "skipped"
        detail = f"任务状态 {outcome.status}: {outcome.reason}"
        if run.worktree_path and run.finished:
            try:
                run.files_changed = worktree.changed_files(run.worktree_path)
            except Exception as e:
                run.error = f"读取 worktree 改动失败: {e}"
                detail += f"\n{run.error}"
        if not run.timed_out and (parent_sess.stop_flag or run.session.stop_flag):
            outcome = AgentResult("cancelled", "父任务或子任务已停止。")
            detail = f"任务状态 {outcome.status}: {outcome.reason}" + (f"\n{run.error}" if run.error else "")
        if (outcome.status == "completed" and not run.error
                and run.finished and run.worktree_path
                and not parent_sess.stop_flag and not run.session.stop_flag):
            try:
                ok, merge_detail = worktree.finish(run.session, apply_changes=True)
            except Exception as e:
                ok, merge_detail = False, f"git 合并失败: {e}"
            merge = "ok" if ok else "conflict"
            detail = merge_detail
            # finish 可能已应用补丁，却在清理隔离区时失败；失败也不能沿用父任务的旧验证。
            for path in run.files_changed:
                mark_dirty(parent_sess.verification, path)
            if run.files_changed:
                parent_sess.verification["tests_run"] = False
                parent_sess.verification["tests_passed"] = None
                parent_sess.verification["tests_reason"] = ""
        if merge != "ok" and run.worktree_path:
            detail += f"\n保留 worktree: {run.worktree_path}"
        results.append({
            "task": run.task,
            "summary": summary,
            "status": outcome.status,
            "reason": outcome.reason,
            "files_changed": run.files_changed,
            "merge": merge,
            "detail": detail,
        })
    return results
