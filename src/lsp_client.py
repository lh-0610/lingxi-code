"""LSP M1 客户端 —— 常驻语言服务器子进程，JSON-RPC over stdio。

功能：维护一个全局语言服务器单例（默认 pyright-langserver，降级 pylsp），提供
  - initialize / initialized 握手
  - textDocument/didOpen（查询前必须先 open，否则 pyright 不解析 → 这是旧版的致命缺陷）
  - textDocument/definition
  - textDocument/references

实现要点：
  - **reader 线程**：守护线程阻塞读 stdout，按 id 分发到等待的调用方（Event + slot）。
    这样跨平台都不卡（不依赖 select，Windows 上 select 不能用于管道）。
  - **server→client 请求要回应**：pyright/pylsp 会发 workspace/configuration 等请求，
    不回应会卡住握手/查询，这里统一回最小默认值。
  - 没装语言服务器时 get_server() 返回 None，所有公开函数优雅返回 None，绝不抛异常崩溃。
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from .paths import logger

# 配置项从 config.py 读，无则用默认
try:
    from .config import LSP_SERVERS
except Exception:
    LSP_SERVERS = ["pyright-langserver", "pylsp"]

# ── 模块级单例 ────────────────────────────────────────────────────────────────
_server: _LspServer | None = None
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
    """从 LSP_SERVERS 列表找到第一个 PATH 上可用的语言服务器命令。找不到返回 None。"""
    servers = LSP_SERVERS if isinstance(LSP_SERVERS, list) else ["pyright-langserver", "pylsp"]
    for cmd in servers:
        exe = cmd.split()[0] if cmd else ""
        if exe and shutil.which(exe):
            return cmd
    return None


def _make_args(cmd: str) -> list[str]:
    """命令字符串 → argv。已带参数的原样切；裸命令补默认 stdio 参数。"""
    parts = cmd.split()
    if len(parts) > 1:
        return parts
    exe = parts[0]
    if exe == "pylsp":
        return [exe]
    return [exe, "--stdio"]


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

    def __init__(self, cmd: str):
        self.cmd = cmd
        self.root = str(_get_project_root())
        self.running = False
        self._initialized = False
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple] = {}   # id -> (Event, slot[list])
        self._opened: set[str] = set()
        self._lock = threading.Lock()          # 保护 _pending 读写 + stdin 写

        try:
            self._proc = subprocess.Popen(
                _make_args(cmd),
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
            "languageId": "python",
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

def get_server() -> _LspServer | None:
    """获取全局 LSP 服务器单例。首次懒启动 + 握手。无可用服务器返回 None。

    项目根变了（用户切项目）时重启服务器以对准新 rootUri。
    """
    global _server
    with _server_lock:
        cur_root = str(_get_project_root())
        if _server is not None and _server.running and _server.root == cur_root:
            return _server
        # 旧服务器死了 / 根变了 → 清掉
        if _server is not None:
            try:
                _server.shutdown()
            except Exception:
                pass
            _server = None
        cmd = _find_server()
        if cmd is None:
            logger.info("[LSP] 未安装 pyright/pylsp，代码导航降级到 jedi")
            return None
        srv = _LspServer(cmd)
        if srv.running and srv._initialized:
            logger.info("[LSP] 服务器就绪: %s (PID=%s)", cmd, srv._proc.pid)
            _server = srv
            return _server
        try:
            srv.shutdown()
        except Exception:
            pass
        return None


def shutdown() -> None:
    """关闭 LSP 服务器，释放子进程。窗口退出时调（幂等）。"""
    global _server
    with _server_lock:
        if _server is not None:
            try:
                _server.shutdown()
            except Exception:
                pass
            _server = None


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
    srv = get_server()
    if srv is None:
        return None
    full = path if os.path.isabs(path) else str(_get_project_root() / path)
    try:
        res = srv.definition(full, line - 1, max(col, 0), timeout=timeout)
    except Exception as e:
        logger.debug("[LSP] definition 调用失败: %s", e)
        return None
    locs = _locations_from_result(res)
    return locs or None


def references(name: str, path: str, line: int, col: int = 0, timeout: float = 10.0):
    """查符号引用。参数同 definition。返回 [{file, line, column}, ...] 或 None。"""
    srv = get_server()
    if srv is None:
        return None
    full = path if os.path.isabs(path) else str(_get_project_root() / path)
    try:
        res = srv.references(full, line - 1, max(col, 0), timeout=timeout)
    except Exception as e:
        logger.debug("[LSP] references 调用失败: %s", e)
        return None
    locs = _locations_from_result(res)
    return locs or None
