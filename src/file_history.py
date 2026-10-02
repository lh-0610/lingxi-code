"""文件改动的安全撤销底层能力（B11a）：写前逐文件备份 + 归属记录 + 预检/执行。

替代旧 checkpoint.py 的 git stash 方案。stash 会改写用户的 stash 列表与暂存区，
记录里没有会话/run 归属与写后指纹，撤销时无法核对现场——本模块改为**只备份目标
文件**：原始字节存进数据目录，Git 的 index、分支、stash 一概不碰（非 Git 项目
因此同样可用，备份本身就是等价的文件备份，恢复后按指纹校验）。

## 记录必须属于真实操作

每条记录在「用户允许写入之后、实际写盘之前」由写文件工具（edit/write/append）
经 `prepare_write` 建立，身份直接取自 B03 的执行前记录（`run_records.current_operation`，
由 `_execute_tool` 写 sidecar 时放进线程局部）：session_id / run_id / task_id（可为空）/
operation_id / 工具名，**不从模型文本或"当前前台会话"猜归属**。拿不到运行归属
（子 Agent、会话尚未落盘、直接调用）→ 不建记录，工具结果明确"本次改动不可撤销"。

## 记录阶段（phase）与可撤销的关系

只有 `undoable`（真实写入完成**且**写后版本指纹可靠记录）才可撤销：
- `prepared`       写前材料已备份，写入尚未确认完成（崩溃窗口）→ 不可撤销
- `undoable`       写入完成、写后指纹已定格 → **可撤销**
- `post_failed`    写入完成但写后版本读取失败 → 不可撤销（写前材料保留）
- `backup_failed`  目标存在但读不了（权限/占用）→ 不可撤销；记录里保留 read_error，
                   **不存在和读不了是两种状态**，绝不把读失败当成空文件
- `undo_failed`    撤销尝试失败（写入/删除被占用等）→ 材料保留，可重试
- `undone`         已恢复写前内容

拒绝、取消（确认卡点拒绝）发生在 prepare 之前，根本不建记录；备份或写后记录失败
只生成"不可撤销"的诚实记录或没有记录，绝不伪装成可恢复。

## 预检与执行（撤销按钮消费的接口）

`precheck` 输出三类结构化状态：`restorable`（可安全恢复）/ `conflict`（当前内容
与记录的写后版本不一致——用户或其它进程又改过）/ `unsupported`（记录缺失、损坏、
归属或工作区不符、路径结构改变、材料缺失）。执行 `execute_undo` 前在锁内**重新**
做全套核对，预检之后文件又被改过 → 转 conflict 拒绝，绝不覆盖。

## 存储与锁

- `chat_memory/file_history/<checkpoint_id>.json` 记录；`<checkpoint_id>.bin` 写前
  原始字节。两者都原子写（临时文件 + os.replace）；跨进程恢复以实际保存的材料为准。
- `_LOCK` 串行化本模块全部记录读写与恢复写盘。锁序：`memory._LOCK → file_history._LOCK`
  （delete_session 持前者再拿后者）；本模块**从不**反向获取 memory._LOCK，也绝不持锁
  等待确认卡——确认卡在 prepare_write 之前就结束了。
- 容量上限 MAX_RECORDS，超出淘汰最旧的；`prepared`（在途）与各 (session, workspace)
  最新的可撤销记录受保护，绝不淘汰。
"""
import hashlib
import os
import re
import threading
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from .paths import logger, memory_dir
from . import run_records

RECORD_VERSION = 1

# 64 位十六进制 sha256 指纹的形状校验：损坏记录里的半个指纹、占位字符串
# 一律按"记录损坏"处理，绝不把缺失字段解释成安全值。
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 记录条数上限。撤销只针对 (session, workspace) 的最近一次可撤销操作；更旧的记录
# 只占空间（含整份写前字节），必须有界。淘汰时保护在途记录与最新可撤销记录。
MAX_RECORDS = 100

# 写前核对用的存在性哨兵：expected_raw=NO_FILE 表示"确认时目标不存在"。
# None 表示"确认时存在但读不了"（核对跳过，备份环节再读并如实记录读失败）。
NO_FILE = object()

# 记录阶段。见模块 docstring 的阶段表。
PHASE_PREPARED = "prepared"
PHASE_UNDOABLE = "undoable"
PHASE_POST_FAILED = "post_failed"
PHASE_BACKUP_FAILED = "backup_failed"
PHASE_UNDO_FAILED = "undo_failed"
PHASE_UNDONE = "undone"

# 预检/执行状态。
ST_RESTORABLE = "restorable"
ST_CONFLICT = "conflict"
ST_UNSUPPORTED = "unsupported"
ST_NOT_FOUND = "not_found"

_LOCK = threading.RLock()

_REPLACE_RETRIES = 5          # 同 memory.py：目标被别的进程占用时的有界重试
_REPLACE_RETRY_DELAY = 0.02


@contextmanager
def write_lock():
    """写入路径与恢复共用的同一把锁（RLock，可嵌套）。

    **实际写文件必须纳入本临界区**：重新核对、备份、写盘、写后定格四步都在
    `prepare_write` / 调用方写盘 / `complete_write` 里完成，两会话写同一文件时
    后到者的写前核对会看到先到者的写入并拒绝，不会出现"A 备份旧内容 → B 写入
    → A 继续写 → 撤销 A 丢掉 B 改动"的交错。确认卡等待发生在锁之外。
    """
    with _LOCK:
        yield


def encode_text_bytes(text: str) -> bytes:
    """把工具的文本内容编码成**本次实际写出的字节**。

    与原文本模式写盘逐字节一致（`\\n` → os.linesep，已用样例矩阵核对）：
    写后指纹取自这份字节——读回磁盘只用于核对，不能用来认领外部修改。
    """
    return text.replace("\n", os.linesep).encode("utf-8")


class PrepareResult:
    """`prepare_write` 的结果。

    record  建立的记录（可能 phase=backup_failed）；None = 没有可靠记录。
    abort   True = 写入前核对失败，**调用方必须中止本次写入**（目标被外部改动、
            路径越界等）；False = 可以照常写入，reason 说明为何不可撤销（若有）。
    reason  给人看的原因说明。
    """

    __slots__ = ("record", "abort", "reason")

    def __init__(self, record, abort, reason):
        self.record = record
        self.abort = abort
        self.reason = reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _norm(path) -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path or "")))
    except (OSError, ValueError):
        return ""


def _sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


def records_dir() -> str:
    return os.path.join(memory_dir(), "file_history")


def _ensure_records_dir() -> bool:
    try:
        os.makedirs(records_dir(), exist_ok=True)
        return True
    except OSError as error:
        logger.warning(f"创建恢复记录目录失败: {error}")
        return False


def _record_path(checkpoint_id: str) -> str:
    return os.path.join(records_dir(), f"{checkpoint_id}.json")


def _blob_name(checkpoint_id: str) -> str:
    return f"{checkpoint_id}.bin"


def _blob_path(checkpoint_id: str) -> str:
    return os.path.join(records_dir(), _blob_name(checkpoint_id))


def session_workspace() -> str:
    """当前线程所属会话的实际工作区根（realpath）。撤销按钮在主线程调用时即
    前台会话的根；写工具在 worker 里调用时即该会话的根——与工具解析路径用的是
    同一套规则（tools_common._project_cwd），不另抄一份。"""
    from .tools_common import _project_cwd
    root = _project_cwd()
    try:
        return os.path.realpath(root) if root else ""
    except OSError:
        return root or ""


def _inside(path: str, root: str) -> bool:
    from .tools_common import _path_inside
    return _path_inside(path, root)


# ══════════════════════════════════════════════════════════════
# 记录读写（原子）
# ══════════════════════════════════════════════════════════════

def _persist(record) -> bool:
    """记录 JSON 原子落盘。失败只返回 False（调用方决定降级），不抛。"""
    if not _ensure_records_dir():
        return False
    try:
        from . import memory
        memory._atomic_write_json(_record_path(record["checkpoint_id"]), record)
        return True
    except Exception as error:
        logger.warning(f"文件撤销记录写盘失败 {record.get('checkpoint_id')}: {error}")
        return False


def _valid_record(raw) -> bool:
    """对任意 JSON 输入都不抛异常的字段校验：**先查类型、再跑正则**——
    损坏记录（pre.sha256 是数字/列表等）只得到"记录无效"，绝不让 TypeError
    逃出去把整个记录扫描打断、连累同会话其它完好记录的撤销。"""
    if not isinstance(raw, dict):
        return False
    if raw.get("version") != RECORD_VERSION:
        return False
    for key in ("checkpoint_id", "session_id", "run_id", "operation_id",
                "tool", "workspace", "path", "phase", "task_id", "reason",
                "created_at", "completed_at"):
        if not isinstance(raw.get(key), str):
            return False
    for key in ("checkpoint_id", "session_id", "run_id", "operation_id",
                "tool", "workspace", "path", "phase"):
        if not raw.get(key):
            return False
    if raw.get("phase") not in (PHASE_PREPARED, PHASE_UNDOABLE, PHASE_POST_FAILED,
                                PHASE_BACKUP_FAILED, PHASE_UNDO_FAILED, PHASE_UNDONE):
        return False
    undo = raw.get("undo")
    if not isinstance(undo, dict) or not all(
            isinstance(undo.get(k), str) for k in ("at", "result", "detail", "restored_sha256")):
        return False
    # pre 必须逐字段严格校验：existed 缺失绝不能被解释成"原本不存在"——
    # 那会让撤销把原有文件直接删掉。损坏的记录宁可整体作废。
    pre = raw.get("pre")
    if not isinstance(pre, dict):
        return False
    existed = pre.get("existed")
    if not isinstance(existed, bool):
        return False
    sha = pre.get("sha256")
    size = pre.get("size")
    blob = pre.get("blob")
    read_error = pre.get("read_error")
    if not isinstance(read_error, str):
        return False
    if existed:
        if blob:
            if not isinstance(blob, str) or not isinstance(sha, str) \
                    or not _HEX64.match(sha):
                return False
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                return False
        else:
            # 存在但没有备份材料：只可能是"读不了"，必须带读失败原因，
            # 且不得声称任何指纹或大小
            if not read_error or sha or size:
                return False
    else:
        # 原本不存在：不得携带任何备份、指纹或读失败说明
        if sha or size or blob or read_error:
            return False
    post = raw.get("post")
    if not isinstance(post, dict):
        return False
    post_sha = post.get("sha256")
    if post_sha is not None and (
            not isinstance(post_sha, str) or (post_sha and not _HEX64.match(post_sha))):
        return False
    post_size = post.get("size")
    if post_size is not None and (
            not isinstance(post_size, int) or isinstance(post_size, bool) or post_size < 0):
        return False
    return True


def _load_record(checkpoint_id):
    """读单条记录。返回 (record|None, 错误原因)。损坏只影响这一条撤销能力：
    校验本身对任意 JSON 输入都不抛异常（再兜一层，见 _valid_record）。"""
    path = _record_path(checkpoint_id)
    if not os.path.exists(path):
        return None, "恢复记录不存在"
    try:
        import json
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except Exception as error:
        return None, f"恢复记录损坏（{error}）"
    try:
        if not _valid_record(raw):
            return None, "恢复记录结构非法（版本或必要字段缺失）"
    except Exception as error:      # pragma: no cover - 校验自身兜底
        return None, f"恢复记录结构非法（校验异常 {error}）"
    return raw, ""


def _scan_records():
    """列出全部记录。返回 [(checkpoint_id, record|None, 错误)]；坏条目原位报告，
    不删除、不牵连其它记录，更不碰聊天历史。"""
    out = []
    try:
        names = os.listdir(records_dir())
    except OSError:
        return out
    for name in sorted(names):
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        cid = name[:-len(".json")]
        record, error = _load_record(cid)
        out.append((cid, record, error))
    return out


def _read_blob(record):
    """按记录校验并读取写前备份字节。返回 (bytes|None, 错误原因)。"""
    blob = (record.get("pre") or {}).get("blob") or ""
    if not blob:
        return None, "记录里没有写前备份"
    path = _blob_path(record["checkpoint_id"])
    if not os.path.exists(path):
        return None, "写前备份文件缺失"
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError as error:
        return None, f"写前备份读取失败（{error}）"
    expected_sha = (record.get("pre") or {}).get("sha256") or ""
    if expected_sha and _sha(data) != expected_sha:
        return None, "写前备份指纹不符（材料已损坏）"
    expected_size = (record.get("pre") or {}).get("size")
    if isinstance(expected_size, int) and expected_size != len(data):
        return None, "写前备份大小不符（材料已损坏）"
    return data, ""


def _write_blob(checkpoint_id: str, data: bytes):
    if not _ensure_records_dir():
        return False, "恢复记录目录不可用"
    try:
        _atomic_write_bytes(_blob_path(checkpoint_id), data)
        return True, ""
    except OSError as error:
        return False, str(error)


def _materials_present(record) -> bool:
    """按钮启停用的便宜检查：材料文件还在不在（不校验指纹，预检再做全量核对）。"""
    if not (record.get("pre") or {}).get("existed"):
        return True
    return bool((record.get("pre") or {}).get("blob")) and \
        os.path.exists(_blob_path(record["checkpoint_id"]))


def _atomic_write_bytes(path, data: bytes) -> None:
    """原始字节的原子写：同目录临时文件 → fsync → os.replace（有界重试）。
    与 memory._atomic_write_json 同一套纪律，用于 .bin 备份与恢复写回。"""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix=os.path.basename(path) + ".", suffix=".tmp")
    fd_open = True
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fd_open = False
        os.close(fd)
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY_DELAY)
    except BaseException:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ══════════════════════════════════════════════════════════════
# 写入路径：写前记录 → 写入 → 写后定格
# ══════════════════════════════════════════════════════════════

def prepare_write(tool: str, target: str, *, expected_raw=None) -> PrepareResult:
    """用户允许写入之后、实际写盘**之前**调用：核对目标 + 建立可靠写前记录。

    - 重新核对：目标 realpath 后必须仍在当前工作区内；与确认时看到的原始字节
      （expected_raw）逐一比对，确认期间被外部改动 → abort=True，调用方必须
      停止本次写入。
    - 备份：目标当前原始字节（含 BOM/CRLF）存进数据目录并记指纹；读不了就记录
      read_error（不伪装成空文件），该改动明确不可撤销。
    - 备份失败（磁盘/权限）→ 返回 record=None + 原因；写入是否继续由调用方决定
      （工具会继续写，但在结果里如实说明不可撤销）。
    """
    operation = run_records.current_operation()
    if operation is None:
        return PrepareResult(None, False,
                             "没有进行中的运行归属（operation_id 缺失），无法建立可撤销记录")
    if not getattr(operation, "run_id", ""):
        # 运行内必有 run_id（begin_run 设置）；缺失说明这不是一轮真实运行里的工具调用，
        # 按"无法归属"处理——宁可不可撤销，也不建一条没有运行身份的记录。
        return PrepareResult(None, False,
                             "本轮没有运行归属（run_id 缺失），无法建立可撤销记录")
    workspace = session_workspace()
    if not workspace:
        return PrepareResult(None, False, "无法确定当前工作区，无法建立可撤销记录")
    try:
        resolved = os.path.realpath(target)
    except OSError as error:
        return PrepareResult(None, True, f"未写入：目标路径无法解析（{error}）")
    if not _inside(resolved, workspace):
        return PrepareResult(None, True,
                             "未写入：目标路径经符号链接/junction 解析后超出当前工作区，拒绝写入")

    existed_now = os.path.lexists(resolved)
    fresh = None
    read_error = ""
    if existed_now:
        try:
            with open(resolved, "rb") as stream:
                fresh = stream.read()
        except OSError as error:
            read_error = str(error)

    # 写入前核对：与确认（diff 卡）时看到的原始字节比对。
    if expected_raw is not None:
        if expected_raw is NO_FILE:
            if existed_now:
                return PrepareResult(None, True,
                                     "未写入：写入前核对失败——确认时目标不存在，现在却出现了"
                                     "（可能被外部创建）。请重新确认后再试。")
        elif fresh is None:
            return PrepareResult(None, True,
                                 f"未写入：写入前核对失败——目标当前无法读取（{read_error}），"
                                 "无法确认未被外部修改。")
        elif fresh != expected_raw:
            return PrepareResult(None, True,
                                 "未写入：写入前核对失败——目标文件在确认期间被外部修改"
                                 "（可能是您或其它程序改动了它）。请重新读取文件后重试。")

    record = {
        "version": RECORD_VERSION,
        "checkpoint_id": run_records.new_id("fh"),
        "session_id": operation.session_id,
        "run_id": operation.run_id,
        "task_id": getattr(operation, "task_id", "") or "",
        "operation_id": operation.operation_id,
        "tool": tool,
        "workspace": workspace,
        "path": resolved,
        "pre": {"existed": False, "sha256": "", "size": 0, "blob": "", "read_error": ""},
        "post": {"sha256": "", "size": None},
        "phase": PHASE_PREPARED,
        "reason": "",
        "created_at": _now(),
        "completed_at": "",
        "undo": {"at": "", "result": "", "detail": "", "restored_sha256": ""},
    }

    with _LOCK:
        if existed_now and fresh is None:
            # 存在但读不了：与"不存在"严格区分，记录读失败原因，明确不可撤销。
            record["pre"] = {"existed": True, "sha256": "", "size": None,
                             "blob": "", "read_error": read_error}
            record["phase"] = PHASE_BACKUP_FAILED
            record["reason"] = f"写前内容读取失败：{read_error}"
            saved = _persist(record)
            if not saved:
                return PrepareResult(None, False, "恢复记录写盘失败，本次改动不可撤销")
            return PrepareResult(record, False, record["reason"])

        if existed_now:
            ok, error = _write_blob(record["checkpoint_id"], fresh)
            if not ok:
                return PrepareResult(None, False, f"写前备份保存失败（{error}），本次改动不可撤销")
            record["pre"] = {"existed": True, "sha256": _sha(fresh), "size": len(fresh),
                             "blob": _blob_name(record["checkpoint_id"]), "read_error": ""}
        if not _persist(record):
            if existed_now:
                try:
                    os.unlink(_blob_path(record["checkpoint_id"]))
                except OSError:
                    pass
            return PrepareResult(None, False, "恢复记录写盘失败，本次改动不可撤销")
        _prune_locked()
        return PrepareResult(record, False, "")


def _read_bytes_now(path):
    """读当前原始字节。返回 (bytes|None, 错误)；读失败不当成空或不存在。"""
    try:
        with open(path, "rb") as stream:
            return stream.read(), ""
    except OSError as error:
        return None, str(error)


def complete_write(record, *, expected_bytes=None) -> tuple:
    """写入完成后调用：把**本次操作预期的完整写后内容**定格为写后版本，读回磁盘仅用于核对。

    - `expected_bytes` 是本次操作的预期完整文件内容：覆盖/编辑 = 新文本编码；
      追加 = 写前备份原文 + 追加字节（新建文件 = 追加字节本身）。写后指纹取自它，
      而不是读回磁盘的现状——AI 写完之后、定格之前用户/外部程序又写了内容，
      那是外部修改，绝不认领成"本次写入版本"（否则撤销会覆盖外部内容）。
    - 读回核对：磁盘现状与预期内容一致 → `undoable`；不一致（并发写入 / 部分写入）
      或读不回 → `post_failed`，明确不可撤销，写前材料保留。
    - `expected_bytes=None`（预期写后内容未知，如无备份的追加）→ 直接 `post_failed`。

    返回 (是否可撤销, 原因)。
    """
    if not record:
        return False, "没有恢复记录"
    checkpoint_id = record.get("checkpoint_id") or ""
    with _LOCK:
        current, error = _load_record(checkpoint_id)
        if current is None:
            record["phase"] = PHASE_POST_FAILED
            record["reason"] = error
            return False, f"恢复记录已无法读取（{error}），本次改动不可撤销"
        if current.get("phase") != PHASE_PREPARED:
            return False, (f"恢复记录状态为 {current.get('phase')}（写前材料不完整），"
                           "本次改动不可撤销")
        current["completed_at"] = _now()
        if expected_bytes is None:
            current["phase"] = PHASE_POST_FAILED
            current["reason"] = "写后版本未知（未取得本次操作的预期完整内容），本次改动不可撤销"
        else:
            expected = _sha(expected_bytes)
            current["post"] = {"sha256": expected, "size": len(expected_bytes)}
            readback, read_error = _read_bytes_now(current["path"])
            if readback is None:
                current["phase"] = PHASE_POST_FAILED
                current["reason"] = (f"写后核对失败：磁盘现状无法读取（{read_error}），"
                                     "本次改动不可撤销")
            elif _sha(readback) != expected:
                current["phase"] = PHASE_POST_FAILED
                current["reason"] = ("写后核对不一致：磁盘现状与本次写入的预期内容不同"
                                     "（可能有并发写入），不认领外部修改，本次改动不可撤销")
            else:
                current["phase"] = PHASE_UNDOABLE
                current["reason"] = ""
        if not _persist(current):
            record.update(current)
            return False, "写后记录保存失败，本次改动不可撤销"
        record.update(current)
        _prune_locked()
        return current["phase"] == PHASE_UNDOABLE, current["reason"]


# ══════════════════════════════════════════════════════════════
# 查询 / 预检 / 执行
# ══════════════════════════════════════════════════════════════

def _summary(record) -> dict:
    return {
        "checkpoint_id": record["checkpoint_id"],
        "session_id": record["session_id"],
        "run_id": record["run_id"],
        "task_id": record.get("task_id") or "",
        "operation_id": record["operation_id"],
        "tool": record["tool"],
        "workspace": record["workspace"],
        "path": record["path"],
        "phase": record["phase"],
        "reason": record.get("reason") or "",
        "created_at": record.get("created_at") or "",
        "completed_at": record.get("completed_at") or "",
        "pre": dict(record.get("pre") or {}),
        "post": dict(record.get("post") or {}),
    }


def latest_undoable(session_id: str, workspace: str):
    """(session, workspace) 下最近一条材料齐全的可撤销记录摘要；没有则 None。

    只查询指定会话与指定实际工作区——不取全局栈顶，避免误撤其它会话的改动。
    """
    if not session_id:
        return None
    want = _norm(workspace)
    best = None
    best_key = ""
    with _LOCK:
        for _cid, record, _error in _scan_records():
            if record is None or record["phase"] not in (PHASE_UNDOABLE, PHASE_UNDO_FAILED):
                continue
            if record["session_id"] != session_id:
                continue
            if _norm(record.get("workspace")) != want:
                continue
            if not _materials_present(record):
                continue
            key = record.get("completed_at") or record.get("created_at") or ""
            if key >= best_key:
                best, best_key = record, key
    return _summary(best) if best is not None else None


def _read_current(record):
    """读目标当前原始字节。返回 (bytes|None, existed, 错误)。读失败不当成不存在。"""
    path = record["path"]
    if not os.path.lexists(path):
        return None, False, ""
    try:
        with open(path, "rb") as stream:
            return stream.read(), True, ""
    except OSError as error:
        return None, True, str(error)


def _later_unresolved_records(record):
    """同工作区同一路径上、晚于该记录且**尚未撤销**的操作（任何非 undone 阶段）。

    当前内容与写后指纹一致不能证明版本链完整：v0→v1→v2→v1 之后请求撤销第一次
    操作，指纹恰好吻合但中间还有两次未撤销的修改——必须核对后续记录，不完整就拒绝。
    """
    want_path = _norm(record.get("path"))
    want_workspace = _norm(record.get("workspace"))
    key = record.get("created_at") or ""
    out = []
    for _cid, other, _error in _scan_records():
        if other is None or other.get("checkpoint_id") == record.get("checkpoint_id"):
            continue
        if other.get("phase") == PHASE_UNDONE:
            continue
        if _norm(other.get("path")) != want_path or _norm(other.get("workspace")) != want_workspace:
            continue
        if (other.get("created_at") or "") > key:
            out.append(other)
    return out


def _assess(record, load_error, session_id, workspace) -> dict:
    """预检核心：核对归属、现场、材料，输出结构化状态与差异信息。"""
    base = {
        "checkpoint_id": record["checkpoint_id"] if record else "",
        "status": ST_UNSUPPORTED,
        "reason": "",
        "record": _summary(record) if record else None,
        "diff": {},
        "path": record["path"] if record else "",
        "tool": record["tool"] if record else "",
    }

    def _fail(status, reason):
        base["status"] = status
        base["reason"] = reason
        return base

    if record is None:
        status = ST_NOT_FOUND if load_error == "恢复记录不存在" else ST_UNSUPPORTED
        return _fail(status, load_error or "恢复记录不可用")
    if session_id and record["session_id"] != session_id:
        return _fail(ST_UNSUPPORTED, "该恢复记录属于其它会话，不能在本会话撤销")
    if workspace and _norm(record.get("workspace")) != _norm(workspace):
        return _fail(ST_UNSUPPORTED, "该恢复记录属于其它工作区，不能在当前工作区撤销")
    if record["phase"] == PHASE_PREPARED:
        return _fail(ST_UNSUPPORTED, "写入未确认完成（写后版本未记录），没有可安全恢复的完整证据")
    if record["phase"] == PHASE_POST_FAILED:
        return _fail(ST_UNSUPPORTED, "写后版本未能可靠记录，无法核对现场：" + (record.get("reason") or ""))
    if record["phase"] == PHASE_BACKUP_FAILED:
        return _fail(ST_UNSUPPORTED, "写前材料不完整：" + (record.get("reason") or ""))
    if record["phase"] == PHASE_UNDONE:
        return _fail(ST_UNSUPPORTED, "该次改动已经撤销过")

    path = record["path"]
    try:
        if _norm(os.path.realpath(path)) != _norm(path):
            return _fail(ST_UNSUPPORTED,
                         "目标路径的符号链接/junction 结构已改变，恢复目标不可靠")
    except OSError as error:
        return _fail(ST_UNSUPPORTED, f"目标路径无法解析（{error}）")
    if os.path.islink(path):
        return _fail(ST_UNSUPPORTED, "目标已变成符号链接，拒绝按旧记录覆盖")
    if not _inside(path, record.get("workspace") or ""):
        return _fail(ST_UNSUPPORTED, "目标路径已不在记录的工作区内")

    current, current_existed, read_error = _read_current(record)
    post_sha = (record.get("post") or {}).get("sha256") or ""
    pre = record.get("pre") or {}
    diff = {
        "target": path,
        "pre_exists": bool(pre.get("existed")),
        "current_exists": current_existed,
        "pre_sha256": pre.get("sha256") or "",
        "expected_post_sha256": post_sha,
        "current_sha256": _sha(current) if current is not None else "",
        "pre_size": pre.get("size"),
        "current_size": len(current) if current is not None else None,
        "blob_path": _blob_path(record["checkpoint_id"]) if pre.get("blob") else "",
    }
    base["diff"] = diff

    if current_existed and current is None:
        return _fail(ST_UNSUPPORTED,
                     f"无法读取目标当前内容进行核对（{read_error}）；恢复材料已保留")
    if pre.get("existed"):
        blob, blob_error = _read_blob(record)
        if blob is None:
            return _fail(ST_UNSUPPORTED, f"恢复材料不可用：{blob_error}")
        if not current_existed:
            return _fail(ST_CONFLICT,
                         "目标文件已被外部删除；恢复会重新创建它，已拒绝以保留您的删除决定")
        if diff["current_sha256"] != post_sha:
            return _fail(ST_CONFLICT,
                         "文件在本次写入之后又被修改（当前内容与写后版本不一致）；"
                         "已拒绝恢复以保留后来的修改")
    else:
        if not current_existed:
            base["reason"] = "文件已不存在，无需恢复（执行时按已完成撤销处理）"
        elif diff["current_sha256"] != post_sha:
            return _fail(ST_CONFLICT,
                         "文件在本次写入之后又被修改（当前内容与写后版本不一致）；"
                         "已拒绝删除以保留后来的修改")
    # 版本链核对：后续还有未撤销的操作时，即使当前内容与写后指纹一致也拒绝——
    # 撤销必须按可靠的版本关系逐次进行，不能跳过中间修改。
    later = _later_unresolved_records(record)
    if later:
        return _fail(ST_UNSUPPORTED,
                     f"版本链不完整：此操作之后同一路径还有 {len(later)} 次未撤销的记录"
                     "操作，必须先撤销最近的一次")
    if record["phase"] == PHASE_UNDO_FAILED:
        base["reason"] = "上次撤销尝试失败，材料完整可重试：" + (record.get("reason") or "")
    else:
        base["reason"] = "现场核对通过：当前内容与本次写入版本一致"
    base["status"] = ST_RESTORABLE
    return base


def precheck(record_id: str, *, session_id=None, workspace=None) -> dict:
    """结构化撤销预检：restorable / conflict / unsupported / not_found + 原因与差异信息。"""
    with _LOCK:
        record, load_error = _load_record(record_id)
        return _assess(record, load_error, session_id, workspace)


def execute_undo(record_id: str, *, session_id: str, workspace: str) -> dict:
    """执行撤销：锁内**重新**做全套核对（预检之后文件可能又被改过），然后恢复。

    - 原本存在的文件 → 原始字节原子写回，恢复后按指纹校验；
    - 原本不存在的文件 → 仅当当前内容仍是本次操作的产物（指纹一致）才删除；
    - 任何失败都保留恢复材料并把记录标为 undo_failed，可重试；
    - 只动目标文件本身：不碰 Git index / 分支 / stash，不做整项目 reset。

    结果字段把「文件已经改变」与「恢复校验成功」**分开**：`changed=True` 表示
    目标文件内容已因本次恢复发生改变（os.replace 原子完成即成立，即使随后的
    读回校验失败）——调用方必须据此作废旧验证结论；`verified` 只说明读回核对
    是否完成并一致，绝不反过来否定 `changed`。

    返回 dict：status / changed / verified / restored / record_saved / reason 分开
    ——文件恢复成功而记录保存失败时，两者分别报告，不用一句"撤销成功"掩盖。
    """
    result = {
        "checkpoint_id": record_id, "path": "", "tool": "", "workspace": "",
        "status": ST_UNSUPPORTED, "changed": False, "verified": None,
        "restored": False, "record_saved": False,
        "reason": "",
    }
    with _LOCK:
        record, load_error = _load_record(record_id)
        assessment = _assess(record, load_error, session_id, workspace)
        result["status"] = assessment["status"]
        result["path"] = assessment["path"]
        result["tool"] = assessment["tool"]
        result["reason"] = assessment["reason"]
        if record is not None:
            result["workspace"] = record.get("workspace") or ""
        if assessment["status"] != ST_RESTORABLE or record is None:
            result["record_saved"] = True      # 无需写记录：材料原样保留
            return result

        pre = record.get("pre") or {}
        detail = ""
        restored_sha = ""
        if pre.get("existed"):
            data, blob_error = _read_blob(record)
            if data is None:
                return _mark_undo_failed(record, result, f"恢复材料不可用：{blob_error}")
            try:
                _atomic_write_bytes(record["path"], data)
            except OSError as error:
                return _mark_undo_failed(record, result, f"恢复写入失败：{error}")
            # 原子替换已完成：文件已经是写前内容，`changed` 在此成立且不因
            # 读回校验的成败而改变（校验只影响 verified）。
            result["changed"] = True
            readback, read_error = _read_bytes_now(record["path"])
            if readback is not None and _sha(readback) == (pre.get("sha256") or ""):
                result["verified"] = True
                detail = f"已恢复写前内容（{len(data)} 字节）"
            else:
                result["verified"] = False
                detail = (f"已恢复写前内容（{len(data)} 字节），"
                          f"但恢复后读回校验未完成（{read_error or '内容不一致'}）")
            restored_sha = pre.get("sha256") or ""
        else:
            current, existed, read_error = _read_current(record)
            if existed and current is None:
                return _mark_undo_failed(record, result, f"无法读取目标（{read_error}），已保留现场")
            if existed:
                try:
                    os.remove(record["path"])
                except OSError as error:
                    return _mark_undo_failed(record, result, f"删除本次新建的文件失败：{error}")
                result["changed"] = True
                result["verified"] = True
                detail = "已删除本次新建的文件"
            else:
                detail = "文件已不存在，无需恢复"
            restored_sha = (record.get("post") or {}).get("sha256") or ""

        record["phase"] = PHASE_UNDONE
        record["undo"] = {"at": _now(),
                          "result": "ok" if result["verified"] else "unverified",
                          "detail": detail, "restored_sha256": restored_sha}
        saved = _persist(record)
        result["status"] = "ok"
        result["restored"] = result["changed"]
        result["record_saved"] = saved
        if not saved:
            result["reason"] = detail + "；但撤销记录保存失败（下次启动将无法确认它已撤销）"
        else:
            result["reason"] = detail
        return result


def _mark_undo_failed(record, result, reason: str) -> dict:
    record["phase"] = PHASE_UNDO_FAILED
    record["reason"] = reason
    record["undo"] = {"at": _now(), "result": "failed", "detail": reason,
                      "restored_sha256": ""}
    saved = _persist(record)
    result["status"] = "error"
    result["record_saved"] = saved
    result["reason"] = reason + ("；恢复材料已保留，可重试" if saved else
                                 "；恢复材料已保留（撤销记录保存失败）")
    return result


def apply_undo_to_session(sess, undo_result) -> dict:
    """恢复成功后的会话侧簿记（撤销按钮与本模块的测试共用同一份实现）：

    - 作废受影响的当前测试 / 检查 / diff 结论（`verification.mark_dirty`）；
    - 把这次恢复接入待验证义务并随之保留；
    - 可靠保存会话快照，**保存结果单独返回**——文件恢复成功而保存失败时，
      两个事实分别报告，不用一句"撤销成功"掩盖。

    必须在 `_LOCK` 之外调用（内部会拿 memory 的保存锁）。返回：
    `{"verification_noted", "verification_error", "saved", "save_error"}`。
    """
    out = {"verification_noted": False, "verification_error": "",
           "saved": None, "save_error": ""}
    # 只有目标文件**确实改变了**才需要作废旧验证结论；恢复校验失败不能否定
    # changed（os.replace 原子完成即已改变），同样要接入义务。
    if not undo_result.get("changed"):
        return out
    path = undo_result.get("path") or ""
    workspace = undo_result.get("workspace") or ""
    try:
        from .verification import mark_dirty
        try:
            rel = os.path.relpath(path, workspace).replace("\\", "/") if workspace else path
        except ValueError:
            rel = path
        mark_dirty(getattr(sess, "verification", None) or {}, rel, abs_path=path)
        run_records.refresh_pending_verification(sess)
        out["verification_noted"] = True
    except Exception as error:
        out["verification_error"] = str(error)[:200]
    try:
        from . import memory
        save = memory.save_session_report(session=sess)
        out["saved"] = bool(getattr(save, "fully_saved", False))
        out["save_body_written"] = bool(getattr(save, "body_written", False))
        if getattr(save, "error", None) is not None:
            out["save_error"] = str(save.error)[:200]
    except Exception as error:
        out["saved"] = False
        out["save_error"] = str(error)[:200]
    return out


# ══════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════

def discard_session(session_id: str) -> None:
    """删除某会话的全部恢复记录（delete_session 钩子）。只删我们自己的记录文件，
    不碰用户文件、不碰 Git。"""
    if not session_id:
        return
    with _LOCK:
        for cid, record, _error in _scan_records():
            if record is not None and record.get("session_id") == session_id:
                for path in (_record_path(cid), _blob_path(cid)):
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError as error:
                        logger.warning(f"删除恢复记录失败 {path}: {error}")


def _undoable_key(record):
    key = record.get("completed_at") or record.get("created_at") or ""
    return key if isinstance(key, str) else ""


def _is_latest_undoable_for_its_session(record, all_records) -> bool:
    """是不是 (session, workspace) 下最新的可撤销记录——撤销按钮只消费这一条，
    淘汰时必须保住它。更旧的可撤销记录随容量淘汰（撤销能力退化为最近约
    MAX_RECORDS 条操作，与旧 checkpoint 的栈上限同一取舍）。"""
    if record["phase"] not in (PHASE_UNDOABLE, PHASE_UNDO_FAILED):
        return False
    want_session = record.get("session_id")
    want_workspace = _norm(record.get("workspace"))
    for _cid, other, _error in all_records:
        if other is None or other is record:
            continue
        if (other.get("session_id") == want_session
                and _norm(other.get("workspace")) == want_workspace
                and other["phase"] in (PHASE_UNDOABLE, PHASE_UNDO_FAILED)
                and _undoable_key(other) > _undoable_key(record)):
            return False
    return True


def _prune_locked() -> None:
    """超出 MAX_RECORDS 时淘汰最旧的记录。保护：在途（prepared）与各
    (session, workspace, path) 最新的可撤销记录。只删自己的文件。"""
    try:
        all_records = _scan_records()
    except Exception as error:      # pragma: no cover - _scan 不抛，兜底
        logger.warning(f"扫描恢复记录失败: {error}")
        return
    excess = len(all_records) - MAX_RECORDS
    if excess <= 0:
        return
    candidates = [entry for entry in all_records if entry[1] is not None]
    candidates.sort(key=lambda entry: entry[1].get("created_at") or "")
    removed = 0
    for cid, record, _error in candidates:
        if removed >= excess:
            break
        if record["phase"] == PHASE_PREPARED:
            continue
        if _is_latest_undoable_for_its_session(record, all_records):
            continue
        for path in (_record_path(cid), _blob_path(cid)):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as error:
                logger.warning(f"淘汰恢复记录失败 {path}: {error}")
        removed += 1
