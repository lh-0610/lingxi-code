"""权限模式：哪些操作直接放行、哪些先问用户（输入框底栏那个"模式"菜单）。

五种模式。"计划"就是原来的 Plan 模式（agent_mode="plan"，只给只读工具）；其余四种是执行时的
放行规则，存在会话的 permission_mode 字段里：

    模式                    改文件   普通命令          危险命令 / Git 写操作 / MCP 工具
    manual 手动             问       问（白名单照旧）   问
    accept_edits 接受编辑   放行     问                问
    auto 自动               放行     放行              问
    bypass 跳过权限确认     放行     放行              放行

放行只在 ui/confirm_bars.py 的 confirm_command / confirm_edit 开头判一次，按**发起确认的那个
会话**的模式，不分散到各个工具里。交互式命令（BLOCKED_COMMANDS）在更早的地方就被拒了，
任何模式都不会放行；子 Agent、手机遥控的规则不经过这里。

"自动"只是按规则拦危险命令（confirm_bars._is_destructive_command），不像 Claude Code 那样
有模型逐条判断风险，所以菜单里不标"推荐"。
"""

PLAN = "plan"
DEFAULT = "manual"

# 确认的"操作类型"：confirm_command(text, kind=...) 由调用方明说是什么操作，放行判定按类型来，
# 不再从确认文案的开头几个字去猜（文案一改，判定就会悄悄失效）。
KIND_COMMAND = "command"            # run_command 的 shell 命令：危险与否按命令内容判断
KIND_GIT_WRITE = "git_write"        # git_commit / git_push 等写工具：每次都问、不给"记住"
KIND_MCP = "mcp"                    # MCP 工具：自动模式也要问；"记住"只记这一个工具
KIND_SPAWN = "spawn_agents"         # 派子 Agent：子 Agent 改文件、跑命令都不再逐条问，
                                    # 做完直接合并回项目——每次都问、不给"记住"
ALWAYS_ASK_KINDS = (KIND_GIT_WRITE, KIND_SPAWN)

# 派子 Agent 确认卡上的标题
SPAWN_AGENTS_PREFIX = "将派出并行子 Agent"

# (取值, 名字, 一句话说明)。菜单按这个顺序列，数字键 1-5 依次对应
MODES = (
    ("auto", "自动", "由灵犀按规则判断，危险命令、Git 写操作和 MCP 工具仍会询问"),
    ("manual", "手动", "改文件、跑命令前都先问你"),
    ("accept_edits", "接受编辑", "自动接受所有文件编辑，命令仍会询问"),
    (PLAN, "计划", "只调研给方案，不动手改东西"),
    ("bypass", "跳过权限确认", "所有操作都不再询问"),
)
EXEC_MODES = tuple(m for m, _name, _desc in MODES if m != PLAN)
_NAMES = {m: n for m, n, _desc in MODES}


def normalize(mode) -> str:
    """执行时的放行规则；认不出的（旧会话没有这个字段、手改坏了）按"手动"。"""
    return mode if mode in EXEC_MODES else DEFAULT


def current(sess) -> str:
    """这个会话现在的模式（五种之一）。"""
    if getattr(sess, "agent_mode", "act") == PLAN:
        return PLAN
    return normalize(getattr(sess, "permission_mode", DEFAULT))


def apply(sess, mode) -> None:
    """切换模式：计划 → agent_mode=plan（放行规则保留，切回执行时照旧）；其余 → agent_mode=act。"""
    if mode == PLAN:
        sess.agent_mode = PLAN
    else:
        sess.agent_mode = "act"
        sess.permission_mode = normalize(mode)


def name(mode) -> str:
    return _NAMES.get(mode, _NAMES[DEFAULT])


def describe(mode) -> str:
    """菜单里那句说明。"""
    return next((d for m, _n, d in MODES if m == mode), "")


def default_for_new_session(previous, remembered) -> str:
    """新会话用哪种放行规则：沿用上一个会话的，但"跳过权限确认"不往下传，退回记住的那个。"""
    previous = normalize(previous)
    if previous != "bypass":
        return previous
    remembered = normalize(remembered)
    return DEFAULT if remembered == "bypass" else remembered


def label(mode, *, long: bool = False) -> str:
    """回复上方那行小字、结果卡"上次使用"里的写法："手动模式""计划模式"……"""
    if mode == PLAN and long:
        return "计划模式（只调研，不改动）"
    return f"{name(mode)}模式"


def snapshot_mode(snapshot) -> str:
    """结果快照里记的模式。旧快照只有 agent_mode：那时执行一律每次都问，就是"手动"。"""
    if (snapshot or {}).get("agent_mode") == PLAN:
        return PLAN
    return normalize((snapshot or {}).get("permission_mode"))


def allows_edit(mode) -> bool:
    """这个模式下写文件（edit_file / write_file / append_file / apply_patch）不用问。"""
    return mode in ("accept_edits", "auto", "bypass")


def allows_command(mode, *, risky: bool) -> bool:
    """这个模式下这条命令不用问。risky：危险命令、Git 写操作或 MCP 工具。"""
    if mode == "bypass":
        return True
    if mode == "auto":
        return not risky
    return False
