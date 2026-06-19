"""运行期可变全局状态 + 会话级状态代理。

历史上这里集中持有所有运行期可变全局，避免模块间循环 import。多会话并发重构后
分成两类：

- **全局共享**（所有会话共用）：llm / llm_with_tools / current_model_index /
  reasoning_enabled / ui_ref / current_project / agent_mode 等，仍是本模块的普通
  变量。
- **会话级**（每个会话一份）：chat_history / stop_flag / session_token_usage /
  compaction / current_plan / shell_cwd / 会话 id·title / remote_session 等，真身
  在 session.Session；本模块通过文件末尾的代理把 `state.X` 读写转发到"当前线程
  的当前会话"（session.current_session()），所以现有 state.X / agent.X 代码无需改动。

读写全局字段照旧 `state.X` / `state.X = ...`；会话级字段同样 `state.X` / `state.X = ...`
（自动落到当前线程的当前会话）。
"""
import re
import sys as _sys
import types as _types

from . import session as _session


# ══════════════════════════════════════
# 全局共享字段（所有会话共用，普通模块变量）
# ══════════════════════════════════════

# 当前 LangChain LLM 实例：全局缓存的"当前会话 model"对应实例，给主线程 / 调试用。
# 并发 worker 各用自己会话 model 的 llm（见 agent.resolve_bound_llm），不读这个全局值。
llm = None
llm_with_tools = None

# 当前激活的项目根路径；None = 无项目（全局工作区）
# 由侧边栏项目切换器修改，会话列表按这个 filter
current_project = None

# 主 ChatUI 实例引用。tools.py 在 worker 线程里执行命令时，需要通过它弹
# 确认框（必须走 UI 主线程）。None 表示当前是 CLI / 测试环境，无 UI，
# 此时 run_command 会默认放行，不阻塞。
ui_ref = None

# Telegram 遥控：回复完成后是否自动发 Telegram 通知（可由命令开关）
telegram_stop: bool = False

# 注：current_model_index / agent_mode / reasoning_enabled 已改为**会话级**
# （在 session._SESSION_FIELDS）——切会话时 模型 / Plan-Act / 思考 跟随该会话。
# 这几个 state.X 的读写经文件末尾的代理落到"当前线程的当前会话"。


# ══════════════════════════════════════
# 会话级字段（每会话一份，真身在 session.Session）
# ══════════════════════════════════════
# 下列名字**不在**本模块定义为普通变量——它们由文件末尾的代理 property 转发到
# session.current_session()。清单是 session._SESSION_FIELDS：
#   chat_history / current_session_id / current_session_title / stop_flag /
#   session_token_usage / compaction / current_plan / shell_cwd / remote_session /
#   _last_text_only_image_warning


# ══════════════════════════════════════
# 计划解析 / 渲染（无状态工具，tools.py / roles.py 共用，放这避免循环 import）
# ══════════════════════════════════════

# 计划状态标记 ↔ 显示符号。
_PLAN_STATUS_MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}

# 解析单行 checklist：可选的 markdown 列表前缀（- / * / + / "1." / "1)"）+ 一个
# checkbox（中括号内允许多/少空格）。group(1)=状态字符，group(2)=步骤文本。
_PLAN_LINE_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)?\[\s*([^\]]?)\s*\]\s*(.*)$")

# checkbox 内字符（小写比较）→ 状态。容忍模型写的各种"完成/进行中"变体。
_PLAN_CHAR_STATUS = {
    "": "pending", " ": "pending",
    "x": "done", "✓": "done", "√": "done", "v": "done",
    "~": "in_progress", "-": "in_progress", "/": "in_progress", ">": "in_progress",
}


def parse_plan(plan_text: str) -> list:
    """把多行 checklist 文本解析成 [{'text','status'}, ...]。行首标记决定状态。

    容错：允许行首带 markdown 列表前缀（- / * / 1.）、checkbox 内多/少空格、
    大小写（[X]）、及常见完成/进行中字符（✓ / ~ 等）。非 checklist 行忽略，
    避免历史摘要、工具 JSON 或模型分析污染计划面板。
    """
    items = []
    for raw in (plan_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _PLAN_LINE_RE.match(line)
        if not m:
            continue
        status = _PLAN_CHAR_STATUS.get(m.group(1).lower(), "pending")
        text = m.group(2).strip()
        if text:
            items.append({"text": text, "status": status})
    return items


def render_plan(plan: list) -> str:
    """把 current_plan 渲染回 Markdown checklist 文本。"""
    if not plan:
        return ""
    out = []
    for item in plan:
        mark = _PLAN_STATUS_MARK.get(item.get("status"), "[ ]")
        out.append(f"{mark} {item.get('text', '')}")
    return "\n".join(out)


# ══════════════════════════════════════
# 任务台账（自动记录"已改文件 / 已跑命令"，逐轮注入 system prompt，survive 压缩）
# ══════════════════════════════════════

_LEDGER_MAX_COMMANDS = 8  # 命令记录只留最近 N 条，防长任务无限堆积

# 写盘类工具 → 台账里显示的动作词
_LEDGER_FILE_OPS = {
    "edit_file": "已编辑",
    "write_file": "已创建/覆盖",
    "append_file": "已追加",
}


def new_task_ledger() -> dict:
    """空台账（单一事实源；session 默认值 / reset 都用它）。"""
    return {"files": {}, "commands": []}


def record_tool_in_ledger(ledger: dict, name: str, args: dict, result) -> None:
    """把一次【成功】的工具调用记进台账。纯 dict 操作，不抛异常（调用方仍兜 try）。

    - edit/write/append → **仅当结果以"成功"开头**（成功编辑/成功写入文件/成功追加到文件）才记
      files[相对路径] = 动作词。用户拒绝（"已拒绝…"）、old_string 没匹配（"失败：…"）等都不记——
      否则会把没真改的文件误记成已改、污染台账。（"工具执行失败"前缀只在 invoke 抛异常时才有，
      盖不住工具自己返回的拒绝/失败串，所以这里用"成功"白名单而非失败黑名单。）
    - run_tests / run_command → 只要真跑过就记 {cmd, brief}（哪怕测试没过，brief 里的失败信息也有用），
      仅跳过 invoke 抛异常（result 以"工具执行失败"开头）。超 N 条丢最老。
    """
    if not isinstance(ledger, dict):
        return
    res = str(result)
    if name in _LEDGER_FILE_OPS:
        if not res.startswith("成功"):       # 只记真改成功的；拒绝/失败/没匹配都不记
            return
        path = (args or {}).get("path") or ""
        if path:
            ledger.setdefault("files", {})[str(path)] = _LEDGER_FILE_OPS[name]
    elif name in ("run_tests", "run_command"):
        if res.startswith("工具执行失败"):    # 命令只跳 invoke 异常；跑了没过仍记
            return
        cmd = "run_tests" if name == "run_tests" else str((args or {}).get("command", ""))[:60]
        brief = " ".join(res.split())[:80]   # 压成单行 + 截断，台账只要个梗概
        cmds = ledger.setdefault("commands", [])
        cmds.append({"cmd": cmd, "brief": brief})
        if len(cmds) > _LEDGER_MAX_COMMANDS:
            del cmds[:-_LEDGER_MAX_COMMANDS]


def render_task_ledger(ledger: dict) -> str:
    """台账 → 注入文本；空台账返回空串（调用方据此决定要不要注入）。"""
    if not isinstance(ledger, dict):
        return ""
    files = ledger.get("files") or {}
    cmds = ledger.get("commands") or []
    if not files and not cmds:
        return ""
    lines = []
    if files:
        lines.append("已改动的文件：")
        for p, op in files.items():
            lines.append(f"- {p}（{op}）")
    if cmds:
        lines.append("最近执行的命令：")
        for c in cmds:
            lines.append(f"- {c.get('cmd', '')} → {c.get('brief', '')}")
    return "\n".join(lines)


# ══════════════════════════════════════
# 会话级字段代理：把 state.X 读写转发到"当前线程的当前会话"
# ══════════════════════════════════════
# 模块本身不支持 property，所以用 sys.modules 把本模块替换成一个带 property 的
# ModuleType 子类实例（给"模块"加 property 的标准技巧）。会话级字段（property，
# data descriptor）的读/写都落到 session.current_session() 对应字段；全局字段、
# 常量、函数原样保留在代理实例上。
class _StateModule(_types.ModuleType):
    pass


def _make_session_prop(_name):
    def _getter(self):
        return getattr(_session.current_session(), _name)

    def _setter(self, value):
        setattr(_session.current_session(), _name, value)

    return property(_getter, _setter)


for _field in _session._SESSION_FIELDS:
    setattr(_StateModule, _field, _make_session_prop(_field))

_proxy = _StateModule(__name__)
# 把本模块现有的全局变量 / 常量 / 函数 / dunder 搬到代理实例。会话级字段不在
# globals 里（本模块根本没定义它们），property 已在 class 上负责转发。
_proxy.__dict__.update(
    {k: v for k, v in globals().items() if k not in _session._SESSION_FIELDS}
)
_sys.modules[__name__] = _proxy
