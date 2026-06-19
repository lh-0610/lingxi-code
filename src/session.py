"""会话级运行时状态容器 + 当前会话路由。

把原本散在 state.py 的"会话级"全局变量（chat_history / stop_flag / token 统计 /
compaction / plan / shell_cwd / 会话 id·title / remote 标记）收进 Session 对象，
为多会话并发打地基。

路由规则（current_session）：
- worker 线程跑某个会话的 agent_loop 时，会 bind_thread(session) 把自己绑到该会话；
  该线程里所有 state.X 访问都落到这个会话。
- 主线程（UI）/ 未绑定的线程：返回 active session（= 前台正在显示的会话）。

state.py 通过 property 把会话级字段代理到 current_session()，所以现有几十处
state.X / agent.X 读写代码无需改动，自动按"当前线程的当前会话"工作。这就是
"全局当前会话 → 线程当前会话"重构的核心。

注意：model 选择 / agent_mode(Plan·Act) 的会话级化在后续 Phase 接入，P1 它们仍是
state.py 的全局字段（行为与重构前完全等价）。
"""
import threading


# 会话级字段名 → 默认值工厂。state.py 的代理 property 依赖这份清单（单一事实源）。
# 用工厂（callable）而非字面量，避免可变默认值（list/dict）被多个会话共享同一对象。
_SESSION_FIELDS = {
    "chat_history": list,
    "current_session_id": lambda: None,
    "current_session_title": lambda: None,
    "stop_flag": lambda: False,
    "session_token_usage": lambda: {"input": 0, "output": 0, "total": 0},
    "compaction": lambda: {"summary": "", "covered_upto": 0},
    "current_plan": list,
    "task_ledger": lambda: {"files": {}, "commands": []},   # ← 新增，自动任务台账（M1）
    "shell_cwd": lambda: None,
    "remote_session": lambda: False,
    # streaming.py 用 state._last_text_only_image_warning 记"本会话本模型是否已就
    # 文本模型收到图片提示过一次"，也是会话级。
    "_last_text_only_image_warning": lambda: None,
    # model 选择 / Plan-Act 模式 / 思考开关 —— 会话级（用户决策）。切会话时跟随该会话；
    # 在某会话改 model 只影响它。默认值会被启动 / 新建会话的"继承当前"覆盖。
    "current_model_index": lambda: 0,
    "agent_mode": lambda: "act",
    "reasoning_enabled": lambda: True,
    # 验证状态（会话级）：编码任务完成闸门——确保 AI 在声称"已完成"前先验证。
    # 延迟 import 避免循环依赖（verification.py 不 import session）。
    "verification": lambda: __import__("src.verification", fromlist=["new_verification"]).new_verification(),
    # git worktree 隔离区路径（运行期临时状态；会话文件不持久化）
    "worktree": lambda: None,
}

# 哨兵：Session.project 的"尚未锚定"初值，区别于合法的 None（无项目/全局）。
_UNSET = object()


class Session:
    """一个会话的全部会话级运行时状态。

    会话级字段（_SESSION_FIELDS）通过 state.py 的代理被现有代码以 state.X 访问；
    is_generating / thread 是运行态，由 UI / agent 直接拿 Session 对象访问。
    """

    __slots__ = tuple(_SESSION_FIELDS) + (
        "is_generating", "thread", "key", "needs_redraw", "project",
        "command_allowlist", "command_prefix_allowlist", "edit_path_allowlist",
        "pending_confirm", "render_log", "render_lock", "is_subagent",
        "role_snapshot",
    )

    def __init__(self):
        for name, factory in _SESSION_FIELDS.items():
            setattr(self, name, factory())
        # 运行态（不经 state 代理）
        self.is_generating = False
        self.thread = None
        # 多会话生命周期（P2）
        self.key = None           # 注册表里的键（已存盘=session_id；新会话=临时键 _new_<n>）
        self.needs_redraw = False  # 后台会话跑完置 True，切回时触发重绘
        # 会话级命令/编辑白名单（用户"允许并记住"只影响本会话，不泄漏到别的会话）
        self.command_allowlist = set()         # 精确命令字符串（旧版，向后兼容）
        self.command_prefix_allowlist = set()  # base 命令前缀（"信任所有 git"）
        self.edit_path_allowlist = set()       # edit_file 路径（"信任此文件所有修改"）
        # 后台会话（非 active）发起的命令/编辑确认：暂存在这里，不打断前台；切到该会话时
        # 才弹卡。形如 ("command", command, result, done) 或 ("edit", path, diff, result, done)。
        self.pending_confirm = None
        # 本轮（一次用户提问到最终回复，含多轮工具）的渲染事件，供"切走→切回"时重放：
        #   ("msg", text, tag) —— 一次 show_message；("md", md_text) —— render_final_markdown
        # 前台也记（因为随时可能被切走）；新一轮开始（_run_agent）清空。render_lock 保护并发
        # append（worker 线程写）与切回时读快照（主线程）。
        self.render_log = []
        self.render_lock = threading.Lock()
        self.is_subagent = False
        # 本轮生成开始时冻结的角色卡快照（roles.capture_active_role() 的返回 dict）。
        # None = 用全局当前角色。worker 在 _run_agent 起手拍下、finally 清回 None：
        # 让后台会话生成途中、前台换了角色卡，也不会把这个会话的人格中途换掉；
        # 空闲（非生成）会话始终读全局，前台换卡下一轮即生效。
        self.role_snapshot = None
        # 会话所属项目：首次 save 时锚定为当时的全局 current_project，之后不被项目切换
        # 影响。修"无项目会话被切项目后误归到新项目"——worker 的 save 可能晚于主线程
        # 切项目，若取全局 current_project 就会被打上新项目 tag。
        self.project = _UNSET


# ── 当前会话路由 ──
_thread_local = threading.local()
_active = None
_lock = threading.RLock()
# 运行期打开的会话注册表（id → Session）。P1 先建好，多会话注册在后续 Phase 用。
sessions = {}


def get_active() -> Session:
    """主线程 / 未绑定线程看到的会话（前台显示的那个）。首次访问惰性创建。"""
    global _active
    with _lock:
        if _active is None:
            _active = Session()
        return _active


def set_active(session: "Session") -> None:
    """切换前台显示的会话。"""
    global _active
    with _lock:
        _active = session


def current_session() -> "Session":
    """当前线程的当前会话：worker 线程 → 它 bind 的会话；否则 → active。"""
    s = getattr(_thread_local, "session", None)
    return s if s is not None else get_active()


def get_verification() -> dict:
    """当前会话的验证状态。给 tools.py 记录写入/测试/diff 使用。"""
    return current_session().verification


def current_project():
    """当前会话锚定的项目根（_UNSET 回退全局 state.current_project；None = 无项目）。

    tools（cwd / 文件读写落点）和 roles（system prompt 的项目上下文 / .lingxirules）
    统一用这个，保证后台会话的工具落点 + 模型"以为自己在哪个项目"一致——不会因为前台
    切了项目，让正在后台跑的会话串到别的项目。
    """
    p = current_session().project
    if p is _UNSET:
        from . import state  # 延迟 import 避免循环（state 顶层 import session）
        p = state.current_project
    return p


def seal_render_log() -> None:
    """清空当前会话本轮的 render_log——在每次往 chat_history append 消息（AIMessage 中间轮 /
    ToolMessage）之后调。保证 render_log 只剩"还没固化到 chat_history 的当前流式部分"，
    切回时 _redraw_chat（已固化）+ 重放 render_log（未固化）不会重复渲染同一段。"""
    s = current_session()
    with s.render_lock:
        s.render_log.clear()


def bind_thread(session: "Session") -> None:
    """把当前线程绑定到某会话（worker 线程进 agent_loop 时调）。"""
    _thread_local.session = session


def unbind_thread() -> None:
    """解除当前线程的会话绑定（worker 退出时调）。"""
    _thread_local.session = None


# ── 多会话注册表 API（P2）──

def new_session_key() -> str:
    """生成一个临时 key（_new_0, _new_1, ...），用于未存盘的新会话。"""
    with _lock:
        i = 0
        while f"_new_{i}" in sessions:
            i += 1
        return f"_new_{i}"


def register(sess: Session) -> str:
    """将 Session 注册到 sessions 注册表；返回 key。

    - 若 sess 已有 key（已注册），直接返回。
    - 若 sess 有 current_session_id（已存盘），用 id 作 key。
    - 否则分配临时 key。
    """
    with _lock:
        if sess.key is not None:
            return sess.key
        key = sess.current_session_id or new_session_key()
        sess.key = key
        sessions[key] = sess
        return key


def get(key: str):
    """按 key 查注册表；找不到返回 None。"""
    with _lock:
        return sessions.get(key)


def drop(key: str) -> None:
    """从注册表移除。"""
    with _lock:
        sessions.pop(key, None)


def rekey(sess: Session, new_key: str) -> None:
    """会话存盘拿到正式 id 后，把注册表里的临时 key（_new_N）换成 id。

    不迁移的话：新会话存盘前 key 是 _new_0，存盘后 current_session_id 有了但注册表
    仍以 _new_0 为键 → load_session(id) 用 id 查注册表查不到，会重复建一个 Session，
    和内存里正在跑的那个脱节。
    """
    if not new_key:
        return
    with _lock:
        if sess.key is not None and sess.key != new_key:
            sessions.pop(sess.key, None)
        sess.key = new_key
        sessions[new_key] = sess
