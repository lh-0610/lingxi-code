"""LSP 客户端 —— 常驻语言服务器子进程，JSON-RPC over stdio，**多语言**。

按文件扩展名选语言组（python / typescript / go / rust / cpp / java），每组一个常驻服务器
单例（默认 pyright-langserver→pylsp、typescript-language-server、gopls、rust-analyzer、
clangd、jdtls），可用 config 的 lsp_servers（python）/ lsp_languages（其它）覆盖。提供：
  - initialize / initialized 握手
  - textDocument/didOpen（查询前必须先 open，否则服务器不解析 → 这是旧版的致命缺陷；
    languageId 按**文件扩展名**给，不是写死 python）
  - textDocument/definition
  - textDocument/references

实现要点：
  - **reader 线程**：守护线程阻塞读 stdout，按 id 分发到等待的调用方（Event + slot）。
    这样跨平台都不卡（不依赖 select，Windows 上 select 不能用于管道）。
  - **server→client 请求要回应**：pyright/pylsp 会发 workspace/configuration 等请求，
    不回应会卡住握手/查询，这里统一回最小默认值。
  - 没装对应语言服务器时 get_server(group) 返回 None，所有公开函数优雅返回 None，绝不抛异常崩溃。
  - Java（jdtls）启动参数环境相关，默认 best-effort；起不来就降级，可用 lsp_languages.java 自定义。
"""
from __future__ import annotations

import itertools
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from .paths import logger

# 配置项从 config.py 读，无则用默认
try:
    from .config import LSP_SERVERS
except Exception:
    LSP_SERVERS = ["pyright-langserver", "pylsp"]
try:
    from .config import LSP_LANGUAGES
except Exception:
    LSP_LANGUAGES = {}

# ── 语言注册表 ────────────────────────────────────────────────────────────────
# 语言组 → 候选服务器命令（取 PATH 上第一个可用的）+ 裸命令的默认 stdio 参数。
# 一个组可能服务多种扩展名（typescript 组同时管 js/ts），didOpen 的 languageId 另按扩展名给。
_LANG_SPECS: dict[str, dict] = {
    "python": {"commands": ["pyright-langserver", "pylsp"]},
    "typescript": {"commands": ["typescript-language-server"], "args": ["--stdio"]},
    "go": {"commands": ["gopls"], "args": []},
    "rust": {"commands": ["rust-analyzer"], "args": []},
    "cpp": {"commands": ["clangd"], "args": []},
    "java": {"commands": ["jdtls"], "args": []},   # -data 工作区在启动时按项目根补
}

# 扩展名 → 语言组
_EXT_TO_GROUP: dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript", ".mjs": "typescript", ".cjs": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".java": "java",
}

# 扩展名 → didOpen 的 languageId（LSP 规范值；tsx/jsx 用 react 变体，C 头文件按 cpp 交给 clangd）
_EXT_TO_LANGUAGE_ID: dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescriptreact",
    ".js": "javascript", ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".java": "java",
}


def language_group_for_path(path: str) -> str | None:
    """文件扩展名 → 语言组；认不出返回 None（导航降级到 jedi / search）。"""
    return _EXT_TO_GROUP.get(os.path.splitext(path)[1].lower())


def _language_id_for_path(path: str) -> str:
    return _EXT_TO_LANGUAGE_ID.get(os.path.splitext(path)[1].lower(), "plaintext")


# ── 模块级单例（每个语言组一个）────────────────────────────────────────────────
_servers: dict[str, "_LspServer"] = {}
_server_lock = threading.Lock()


def _get_project_root() -> Path:
    """当前项目根目录（兼容打包/开发）。"""
    try:
        from . import state
        proj = getattr(state, "current_project", None)
        if proj:
            return Path(proj)
    except Exception:
        pass
    return Path.cwd()


def _find_server() -> str | None:
    """Python 语言服务器：从 LSP_SERVERS 列表找第一个 PATH 上可用的命令。找不到返回 None。

    保留无参签名（历史调用方 / 测试按 python 后端打桩）。其它语言走 _find_server_for。
    """
    servers = LSP_SERVERS if isinstance(LSP_SERVERS, list) else ["pyright-langserver", "pylsp"]
    for cmd in servers:
        exe = cmd.split()[0] if cmd else ""
        if exe and shutil.which(exe):
            return cmd
    return None


def _candidate_commands(group: str) -> list[str]:
    """某语言组的候选命令：config.lsp_languages 覆盖优先，否则内置默认。"""
    override = LSP_LANGUAGES.get(group) if isinstance(LSP_LANGUAGES, dict) else None
    if isinstance(override, str) and override.strip():
        return [override.strip()]
    if isinstance(override, list) and override:
        return [str(c) for c in override if str(c).strip()]
    return list(_LANG_SPECS.get(group, {}).get("commands", []))


def _find_server_for(group: str) -> str | None:
    """某语言组第一个 PATH 上可用的服务器命令。python 委托 _find_server()。"""
    if group == "python":
        return _find_server()
    for cmd in _candidate_commands(group):
        exe = cmd.split()[0] if cmd else ""
        if exe and shutil.which(exe):
            return cmd
    return None


def _jdtls_workspace(root: str) -> str:
    """jdtls 的 -data 工作区目录：放在用户数据目录下、按项目根哈希区分，**绝不放进项目根**。

    放进项目根会被 git_diff 当成未跟踪文件，平白多出一块"未审阅的改动"，完成闸门永远收不了。
    按 realpath 哈希区分：不同项目各用一个工作区，同一项目稳定复用（索引缓存可跨次保留）。
    数据目录本身恰好就是项目根时（开发灵犀自身）退到系统临时目录，仍保证在项目外。
    """
    from .paths import get_data_dir
    from .tools_common import _path_inside
    real = os.path.realpath(root)
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()[:16]
    candidate = os.path.join(get_data_dir(), "lsp_workspaces", digest)
    if _path_inside(candidate, real):
        candidate = os.path.join(tempfile.gettempdir(), "lingxi-jdtls", digest)
    return candidate


def _make_args(cmd: str, group: str, root: str) -> list[str]:
    """命令字符串 → argv。已带参数的原样切；裸命令补该组的默认 stdio 参数。

    Java 的 jdtls 需要一个 -data 工作区目录，放在项目外的用户数据目录（best-effort，起不来就降级）。
    """
    parts = cmd.split()
    if len(parts) > 1:
        return parts
    exe = parts[0]
    if group == "python":
        return [exe] if exe == "pylsp" else [exe, "--stdio"]
    if group == "java" and exe == "jdtls":
        return [exe, "-data", _jdtls_workspace(root)]
    return [exe] + list(_LANG_SPECS.get(group, {}).get("args", ["--stdio"]))


def _path_to_uri(p: str) -> str:
    return Path(p).resolve().as_uri()


def _uri_to_path(uri: str) -> str:
    """file:///... → 本地路径（Windows 去掉盘符前多余的 /）。"""
    raw = unquote(urlparse(uri).path)
    if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    return os.path.normpath(raw)


def _ls_uri_to_rel(uri: str) -> str:
    """LSP location.uri（file:///...）→ 项目内相对路径。超出项目则返回绝对路径。"""
    if not uri:
        return uri
    raw = _uri_to_path(uri)
    try:
        return str(Path(raw).relative_to(_get_project_root()))
    except Exception:
        return raw


# ── _LspServer 类 ─────────────────────────────────────────────────────────────

class _LspServer:
    """管理一个 LSP 服务器子进程（stdio JSON-RPC 2.0，reader 线程异步收）。"""

    def __init__(self, cmd: str, group: str = "python"):
        self.cmd = cmd
        self.group = group
        self.root = str(_get_project_root())
        self.running = False
        self._initialized = False
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple] = {}   # id -> (Event, slot[list])
        self._opened: set[str] = set()
        self._lock = threading.Lock()          # 保护 _pending 读写 + stdin 写

        try:
            argv = _make_args(cmd, group, self.root)
            # Windows 上 npm 装的语言服务器是 .cmd 垫片（pyright-langserver.cmd、脚本式 jdtls），
            # CreateProcess 只认 .exe、不查 PATHEXT，裸名字会 WinError 2。shutil.which 尊重
            # PATHEXT，把它解析成完整路径（.cmd/.bat 给全路径就能起），再交给 Popen。
            resolved = shutil.which(argv[0]) if argv else None
            if resolved:
                argv[0] = resolved
            self._proc = subprocess.Popen(
                argv,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("[LSP] 启动失败: %s", e)
            return
        if self._proc.poll() is not None:
            logger.warning("[LSP] %s 启动后立即退出", cmd)
            return
        self.running = True
        threading.Thread(target=self._reader, daemon=True).start()

        try:
            self._initialize()
            self._initialized = True
        except Exception as e:
            logger.warning("[LSP] initialize 握手失败: %s", e)
            self.running = False
            try:
                self._proc.terminate()
            except Exception:
                pass

    # ── 帧 IO ──────────────────────────────────────────────────────────────
    def _send(self, msg: dict) -> None:
        """写一帧：Content-Length: N\\r\\n\\r\\n{json}（N=json 的 utf-8 字节数）。需在 _lock 内调。"""
        data = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + data)
        self._proc.stdin.flush()

    def _reader(self) -> None:
        """守护线程：循环读 headers→body→分发。响应按 id 唤醒等待者；server 请求即时回应。"""
        f = self._proc.stdout
        while True:
            headers = {}
            while True:
                line = f.readline()
                if not line:                    # 进程退出 / 管道关闭
                    self.running = False
                    self._fail_all_pending()
                    return
                s = line.decode("ascii", "replace").strip()
                if s == "":
                    break
                if ":" in s:
                    k, v = s.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            try:
                n = int(headers.get("content-length", 0))
            except ValueError:
                n = 0
            body = f.read(n) if n else b""
            if not body:
                continue
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception:
                continue

            mid = msg.get("id")
            method = msg.get("method")
            if method is not None and mid is not None:
                # server → client 请求：必须回应，否则部分服务器会卡住
                self._respond_server_request(mid, method, msg.get("params"))
                continue
            if method is not None:
                # 通知（diagnostics / progress 等）M1 忽略
                continue
            # 否则是对我们某个请求的响应
            if mid is not None:
                with self._lock:
                    entry = self._pending.pop(mid, None)
                if entry is not None:
                    ev, slot = entry
                    slot.append(msg)
                    ev.set()

    def _respond_server_request(self, mid, method: str, params) -> None:
        """对 server→client 请求回最小默认值（防握手/查询卡死）。"""
        result = None
        if method == "workspace/configuration":
            items = (params or {}).get("items", []) if isinstance(params, dict) else []
            result = [None for _ in items] if isinstance(items, list) else None
        try:
            with self._lock:
                self._send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception:
            self.running = False

    def _fail_all_pending(self) -> None:
        """进程死亡时唤醒所有等待者（slot 留空 → 调用方按超时/无结果处理）。"""
        with self._lock:
            pend = list(self._pending.values())
            self._pending.clear()
        for ev, _slot in pend:
            ev.set()

    def _request(self, method: str, params: dict, timeout: float = 10.0):
        """发请求并等响应；返回 result 字段（无则 None）。超时/出错返回 None。"""
        if not self.running:
            return None
        mid = next(self._ids)
        ev = threading.Event()
        slot: list = []
        with self._lock:
            self._pending[mid] = (ev, slot)
            try:
                self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            except Exception:
                self.running = False
                self._pending.pop(mid, None)
                return None
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            return None
        if not slot:
            return None
        return slot[0].get("result")

    def _notify(self, method: str, params: dict) -> None:
        if not self.running:
            return
        with self._lock:
            try:
                self._send({"jsonrpc": "2.0", "method": method, "params": params})
            except Exception:
                self.running = False

    def _initialize(self) -> None:
        root = self.root
        self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": Path(root).as_uri(),
            "rootPath": root,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                }
            },
        }, timeout=20.0)
        self._notify("initialized", {})

    def ensure_open(self, full_path: str) -> None:
        """查询前打开文档（didOpen）。pyright 不 open 不解析——旧版漏掉这步导致 LSP 永远空。"""
        if full_path in self._opened:
            return
        try:
            with open(full_path, encoding="utf-8") as fp:
                text = fp.read()
        except Exception:
            return
        self._notify("textDocument/didOpen", {"textDocument": {
            "uri": _path_to_uri(full_path),
            "languageId": _language_id_for_path(full_path),
            "version": 1,
            "text": text,
        }})
        self._opened.add(full_path)

    def definition(self, full_path: str, line0: int, char0: int, timeout: float = 10.0):
        """textDocument/definition；line0/char0 为 0 基。返回原始 LSP result（Location|Location[]|None）。"""
        if not self._initialized:
            return None
        self.ensure_open(full_path)
        return self._request("textDocument/definition", {
            "textDocument": {"uri": _path_to_uri(full_path)},
            "position": {"line": line0, "character": char0},
        }, timeout=timeout)

    def references(self, full_path: str, line0: int, char0: int, timeout: float = 10.0):
        """textDocument/references（含声明）；line0/char0 为 0 基。返回原始 LSP result。"""
        if not self._initialized:
            return None
        self.ensure_open(full_path)
        return self._request("textDocument/references", {
            "textDocument": {"uri": _path_to_uri(full_path)},
            "position": {"line": line0, "character": char0},
            "context": {"includeDeclaration": True},
        }, timeout=timeout)

    def shutdown(self) -> None:
        """shutdown 请求 + exit 通知，关闭子进程（幂等）。"""
        try:
            if self.running and self._initialized:
                self._request("shutdown", {}, timeout=5.0)
                self._notify("exit", {})
        except Exception:
            pass
        self.running = False
        self._initialized = False
        try:
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.terminate()
            except Exception:
                pass


# ── 模块级 API ────────────────────────────────────────────────────────────────

def get_server(group: str = "python") -> _LspServer | None:
    """获取某语言组的 LSP 服务器单例。首次懒启动 + 握手。无可用服务器返回 None。

    默认 python（历史调用方 / 测试按此打桩）。项目根变了（用户切项目）时重启该组服务器以对准新 rootUri。
    """
    with _server_lock:
        cur_root = str(_get_project_root())
        srv = _servers.get(group)
        if srv is not None and srv.running and srv.root == cur_root:
            return srv
        # 旧服务器死了 / 根变了 → 清掉
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            _servers.pop(group, None)
        cmd = _find_server() if group == "python" else _find_server_for(group)
        if cmd is None:
            logger.info("[LSP] 未安装 %s 语言服务器，代码导航降级到 jedi / search", group)
            return None
        srv = _LspServer(cmd, group)
        if srv.running and srv._initialized:
            logger.info("[LSP] 服务器就绪: %s (group=%s, PID=%s)", cmd, group, srv._proc.pid)
            _servers[group] = srv
            return srv
        try:
            srv.shutdown()
        except Exception:
            pass
        return None


def shutdown() -> None:
    """关闭所有语言组的 LSP 服务器，释放子进程。窗口退出时调（幂等）。"""
    with _server_lock:
        for srv in list(_servers.values()):
            try:
                srv.shutdown()
            except Exception:
                pass
        _servers.clear()


def _locations_from_result(result) -> list:
    """LSP definition/references result（Location | Location[] | LocationLink[]）→ [{file,line,column}]。"""
    if not result:
        return []
    if isinstance(result, dict):
        result = [result]
    out = []
    for loc in result:
        if not isinstance(loc, dict):
            continue
        uri = loc.get("uri") or loc.get("targetUri") or ""
        rng = loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}
        start = rng.get("start", {}) if isinstance(rng, dict) else {}
        out.append({
            "file": _ls_uri_to_rel(uri),
            "line": int(start.get("line", 0)) + 1,      # 转回 1 基
            "column": int(start.get("character", 0)) + 1,
        })
    return out


def definition(name: str, path: str, line: int, col: int = 0, timeout: float = 10.0):
    """查符号定义。path：绝对或相对项目根；line：1 基；col：0 基。

    返回 [{file, line, column}, ...]；无服务器 / 无结果 / 出错均返回 None。
    （name 仅用于统一调用风格，定位靠 line+col。）
    """
    full = path if os.path.isabs(path) else str(_get_project_root() / path)
    group = language_group_for_path(full)
    if group is None:
        return None
    srv = get_server(group)
    if srv is None:
        return None
    try:
        res = srv.definition(full, line - 1, max(col, 0), timeout=timeout)
    except Exception as e:
        logger.debug("[LSP] definition 调用失败: %s", e)
        return None
    locs = _locations_from_result(res)
    return locs or None


def references(name: str, path: str, line: int, col: int = 0, timeout: float = 10.0):
    """查符号引用。参数同 definition。返回 [{file, line, column}, ...] 或 None。"""
    full = path if os.path.isabs(path) else str(_get_project_root() / path)
    group = language_group_for_path(full)
    if group is None:
        return None
    srv = get_server(group)
    if srv is None:
        return None
    try:
        res = srv.references(full, line - 1, max(col, 0), timeout=timeout)
    except Exception as e:
        logger.debug("[LSP] references 调用失败: %s", e)
        return None
    locs = _locations_from_result(res)
    return locs or None
