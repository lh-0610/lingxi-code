"""只读探索子代理：explore 工具。

主 Agent 把"X 是怎么工作的 / Y 在哪里处理"这类要翻好几个文件才弄得清的问题派出去，子代理在
**自己的上下文**里读代码，只交回一份带 `路径:行号` 的报告。大仓库里主循环原本要花几十轮挨个读文件
（评测 011：灵犀修自己的启动崩溃要 60 轮），读过的内容还一直占着主上下文。七家主流 coding agent
（DeepSeek Harness / ZCode / Kimi Code / Qwen Code / Codex / OpenCode / Claude Code）都是这个做法。

和 spawn_agents（写代码的子 Agent）的区别：不建 worktree，直接在项目里读；只绑定读/搜工具
（session.allowed_tools，streaming._execute_tool 再硬拦一道）；按 quick / medium / thorough 设轮次上限；
跑满上限也要交回已查到的内容，而不是一句"已达上限"。
"""
import threading
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

from . import session as _session
from . import state
from .agent_result import AgentResult
from .paths import logger
from .tools_common import _project_cwd

# 子代理能用的工具：只读、不跑命令（run_command / run_tests 会执行项目代码，不算只读）
EXPLORE_TOOL_NAMES = frozenset({
    "read_file", "read_files", "search_files", "search_in_file", "list_directory",
    "find_definition", "find_references", "code_map", "related_files", "find_tests",
    "git_log", "git_diff", "get_project_instructions", "run_code",
})

# 彻底程度 → (轮次上限, 超时秒数)
_DEPTHS = {"quick": (8, 180), "medium": (20, 420), "thorough": (40, 900)}
_REPORT_MAX_CHARS = 6000
_LIMIT_NOTE = "（探索达到轮次上限，下面是到目前为止查到的内容）"


def _ai_text(msg) -> str:
    content = msg.content
    if isinstance(content, str):
        return content.strip()
    parts = [str(p.get("text", "")) for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]
    return "\n".join(parts).strip()


def _final_report(child) -> str:
    """跑满上限时让子代理不调工具、直接把已查到的写成报告（多一次模型调用，换回前面几十轮的结果）。"""
    from .agent import resolve_bound_llm
    history = list(child.chat_history)
    if history and isinstance(history[-1], AIMessage) and "轮次上限" in _ai_text(history[-1]):
        history.pop()       # 那条"已达上限"是写给用户看的，别让模型当成自己的结论
    history.append(HumanMessage(content="轮次用完了。不要再调用工具，根据你到目前为止查到的内容，按报告格式写最终报告。"))
    try:
        llm = resolve_bound_llm(child)[0]
        return _ai_text(llm.invoke(history))
    except Exception as e:
        logger.warning(f"explore 收尾报告失败: {e}")
        return ""


def _last_report(child) -> str:
    for msg in reversed(child.chat_history):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            text = _ai_text(msg)
            if text and "轮次上限" not in text:
                return text
    return ""


@tool
def explore(question: str, thoroughness: str = "medium") -> str:
    """派一个只读的探索子代理去弄清代码库里的一个问题（"X 是怎么工作的""Y 在哪里处理""改 Z 会影响哪些地方"），
    它在自己的上下文里搜、读代码，只交回一份带 `路径:行号` 的报告，不占你的上下文。

    要读 3 个以上文件才能弄清的问题，先派它，别自己一个个读；几个互不相关的问题在同一轮里各派一个，会并行跑。
    子代理看不到你的对话：question 里写清要查什么、已经知道什么、最后要拿来干嘛。
    只读：不改文件、不跑命令。

    question: 要查清的问题，写完整
    thoroughness: "quick"（几轮内给出最可能的位置）/ "medium"（默认，把主要调用链走通）/
                  "thorough"（相关入口、分支、测试都找全，慢）"""
    from .agent import agent_loop
    from .roles import get_explore_system_prompt
    from .subagent import HeadlessUI

    question = (question or "").strip()
    if not question:
        return "没给问题：question 里写清要查什么"
    depth = thoroughness if thoroughness in _DEPTHS else "medium"
    max_rounds, timeout_s = _DEPTHS[depth]
    parent = _session.current_session()
    project_root = _project_cwd()

    child = _session.Session()
    child.is_subagent = True
    child.agent_mode = "act"
    child.current_model_index = parent.current_model_index
    # quick 只是定位，不值得开思考
    child.reasoning_enabled = parent.reasoning_enabled and depth != "quick"
    child.project = project_root
    child.worktree = project_root       # 子代理沙箱以它为界：只能读项目里的文件
    child.allowed_tools = set(EXPLORE_TOOL_NAMES)
    child.max_rounds = max_rounds
    child.system_prompt_override = get_explore_system_prompt(project_root)
    child.chat_history = [
        SystemMessage(content=child.system_prompt_override),
        HumanMessage(content=f"{question}\n\n（彻底程度：{depth}）"),
    ]
    _session.register(child)

    ui = HeadlessUI(parent_ui=getattr(state, "ui_ref", None), label="探索")
    outcome = {}

    def _run():
        _session.bind_thread(child)
        try:
            child.is_generating = True
            outcome["result"] = agent_loop(ui)
        except Exception as e:
            logger.error(f"explore 子代理失败: {e}", exc_info=True)
            outcome["result"] = AgentResult("failed", str(e))
        finally:
            child.is_generating = False
            _session.unbind_thread()

    started = time.monotonic()
    worker = threading.Thread(target=_run, name="explore", daemon=True)
    worker.start()
    timed_out = False
    while worker.is_alive():
        if getattr(parent, "stop_flag", False):
            child.stop_flag = True
        if time.monotonic() - started > timeout_s:
            timed_out = True
            child.stop_flag = True
            break
        worker.join(0.2)
    if timed_out:
        worker.join(10)

    result = outcome.get("result")
    status = getattr(result, "status", "failed")
    try:
        if getattr(parent, "stop_flag", False):
            return "探索已停止（主任务被停止）。"
        report = _last_report(child)
        if status == "limit_reached" or timed_out:
            report = _final_report(child) or report
            report = f"{_LIMIT_NOTE}\n{report}" if report else ""
        if not report:
            reason = getattr(result, "reason", "") or ("超时" if timed_out else "没有给出报告")
            return f"探索没有得到结果（{status}：{str(reason)[:200]}）。可以换个问法再派，或自己查。"
        if len(report) > _REPORT_MAX_CHARS:
            report = report[:_REPORT_MAX_CHARS] + "\n…（报告过长已截断）"
        rounds = sum(1 for m in child.chat_history if isinstance(m, AIMessage))
        usage = getattr(child, "session_token_usage", {}) or {}
        logger.info(f"explore（{depth}）：{rounds} 轮，输入 {usage.get('input', 0)} token，"
                    f"{time.monotonic() - started:.0f}s，问题 {question[:60]!r}")
        return f"{report}\n\n（探索子代理：{depth}，{rounds} 轮）"
    finally:
        if child.key:
            _session.drop(child.key)
