"""工具结果里的密钥打码：把用户自己配置的密钥值替换成「‹已隐藏：字段名›」。

为什么要做：`run_command` 的子进程环境已经剔掉了名字像密钥的变量
（`tools._scrubbed_env`），但工具**结果**本身没有任何处理。`read_file` 读 config.json、
`run_command` 跑一段读配置的脚本、命令输出里 echo 出 key——原文都会进 `chat_history`
（落进 chat_memory 的会话 JSON）、日志、界面，并随下一轮请求发给模型服务商。
其中"只露出开头 10 个字符"（排查问题时常见的 `key[:10]`）同样是泄露。

收集范围：
  - `config.json` 里字段名含 key / token / secret / password 的**非空字符串**，
    递归进嵌套对象与数组（`custom_models`、`mcp_servers` 里的 `env` 都覆盖）；
  - 环境变量里名字像密钥的值（判定沿用 `tools._SECRET_ENV_PARTS`）；
  - 只收长度 >= `MIN_SECRET_LEN` 的值，避免把 `"true"`、`"abc"` 这类短值当密钥；
  - 按空白切开取片段（"Bearer xxx" 只收 xxx），路径、URL 不收（装路径的 *_CREDENTIALS 变量）。

匹配规则：完整值打码；字母、数字都有的随机样式值，只出现开头一段（>= `MIN_PARTIAL_LEN` 个
字符，如 `key[:10]`）也打码——足够长的前缀已经能定位到具体 key。纯字母的值只认完整出现。

刷新时机：进程内首次用到时构建一次；设置页保存配置后调 `refresh()` 重建
（`src/ui/settings_dialog.py` 已接）。**不改变 config.json 的存储方式，也不禁止读它**——
打码只作用于工具结果，配置文件照旧可读，只是读出来的密钥值会被替换。

`verification.redact` 是另一套东西（证据摘要用的正则打码），保持不动。
"""
import os
import re
import threading

from . import paths

# 只收这么长的值：短值是开关、枚举、路径片段，误伤代价远大于收益
MIN_SECRET_LEN = 12
# 只露出开头一段时，这一段至少这么长才打码
MIN_PARTIAL_LEN = 8

# config.json / MCP 配置里"字段名像密钥"的模式（小写子串匹配）。authorization：MCP / 自定义
# 接口的请求头里常是 "Bearer xxx"
_SECRET_NAME_PARTS = ("key", "token", "secret", "password", "passwd", "credential", "authorization")

# 环境变量名判定复用 tools 的常量，避免两份清单漂移。tools 在导入期就会 import 本模块，
# 所以只能延迟导入（放在函数里），不能写到模块顶层。
_FALLBACK_ENV_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")

_lock = threading.RLock()
_secrets = {}            # 值 -> (字段名, 允许按前缀打码)
_loaded = False


def _env_parts():
    try:
        from .tools import _SECRET_ENV_PARTS
        return _SECRET_ENV_PARTS
    except Exception:
        return _FALLBACK_ENV_PARTS


def _looks_secret_name(name) -> bool:
    if not isinstance(name, str) or not name:
        return False
    low = name.lower()
    return any(part in low for part in _SECRET_NAME_PARTS)


def _looks_like_path(piece: str) -> bool:
    return bool(re.match(r"[A-Za-z]:[\\/]", piece)) or piece.startswith(("/", "\\", "~", ".")) \
        or "\\" in piece


def _add_secret(out, value: str, name: str) -> None:
    """把一个配置值里真正像凭据的部分记进 out：{片段: (字段名, 允许按前缀打码)}。

    按空白切开（"Bearer xxx" 只要后半截）；路径、URL 不算——GOOGLE_APPLICATION_CREDENTIALS 这类
    变量装的是路径，当成密钥的话，前缀打码会把输出里所有 C:\\Users… 都换掉。
    前缀打码只给字母、数字都有的随机样式值：纯字母的口令（correcthorsebattery）开头 8 个字符
    就是个普通词，按前缀打码会误伤正文，这类只在完整出现时打码。"""
    for piece in value.split():
        if len(piece) < MIN_SECRET_LEN or "://" in piece or _looks_like_path(piece):
            continue
        prefix_ok = bool(re.search(r"[A-Za-z]", piece) and re.search(r"\d", piece))
        # 同一个值出现在多个字段里时保留先见到的（外层字段名更可读）
        out.setdefault(piece, (name, prefix_ok))


def _iter_config_secrets(node, out) -> None:
    """递归收集 dict / list 里字段名像密钥的字符串（也收 "api_keys": [...] 这种列表里的）。"""
    if isinstance(node, dict):
        for name, value in node.items():
            if isinstance(value, str):
                if _looks_secret_name(name):
                    _add_secret(out, value, name)
            elif isinstance(value, (dict, list)):
                if isinstance(value, list) and _looks_secret_name(name):
                    for item in value:
                        if isinstance(item, str):
                            _add_secret(out, item, name)
                _iter_config_secrets(value, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _iter_config_secrets(item, out)


def _read_config() -> dict:
    """读当前 config.json。读不到/损坏时返回空字典——打码是安全兜底，不能因此让工具失败。"""
    path = getattr(paths, "CONFIG_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        import json
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def collect_secrets(config=None) -> dict:
    """收集配置 + 环境变量里的密钥值，返回 {值: (字段名, 允许按前缀打码)}。
    config 为空则读磁盘上的 config.json。"""
    out: dict = {}
    _iter_config_secrets(_read_config() if config is None else config, out)

    parts = _env_parts()
    for name, value in os.environ.items():
        if isinstance(value, str) and any(p in name.upper() for p in parts):
            _add_secret(out, value, name)
    return out


def _rebuild(config=None) -> None:
    global _secrets, _loaded
    try:
        secrets = collect_secrets(config)
    except Exception:
        secrets = {}
    with _lock:
        _secrets = secrets
        _loaded = True


def _get() -> dict:
    with _lock:
        if _loaded:
            return _secrets
    _rebuild()
    with _lock:
        return _secrets


def refresh(config=None) -> None:
    """重建打码表。设置里保存配置后调用，让新填的 key 立刻生效。

    config 传入刚保存的配置字典时直接用它（不必再读盘）；省略则重新读 config.json。
    """
    _rebuild(config)


def _common_prefix_len(value: str, text: str, pos: int, limit: int) -> int:
    """text[pos:] 与 value 的最长公共前缀长度（只比 limit 个字符，不复制字符串）。"""
    n = min(len(value), limit, len(text) - pos)
    i = 0
    while i < n and value[i] == text[pos + i]:
        i += 1
    return i


def mask(text):
    """把文本里出现的密钥值（含 >= 8 字符的开头一段）换成「‹已隐藏：字段名›」。

    非字符串原样返回；任何内部异常都不让工具结果丢失（宁可漏打码也不抛）。
    """
    if not isinstance(text, str) or len(text) < MIN_PARTIAL_LEN:
        return text
    secrets = _get()
    if not secrets:
        return text
    try:
        spans = []      # (起始位置, 结束位置, 字段名)
        for value, (name, prefix_ok) in secrets.items():
            need = MIN_PARTIAL_LEN if prefix_ok else len(value)
            head = value[:need]
            pos = text.find(head)
            while pos != -1:
                # 这里可能只是"开头相同"，也可能就是完整 key：按实际重合长度打码
                k = _common_prefix_len(value, text, pos, len(value))
                if k >= need:
                    spans.append((pos, pos + k, name))
                pos = text.find(head, pos + 1)
        if not spans:
            return text
        # 重叠时优先保留更长的命中（如两个 key 共享 8 字符前缀）
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        merged = []
        for start, end, name in spans:
            if merged and start < merged[-1][1]:
                last_start, last_end, last_name = merged[-1]
                if end - start > last_end - last_start:
                    merged[-1] = (last_start, end, name)
                continue
            merged.append((start, end, name))

        out = []
        cursor = 0
        for start, end, name in merged:
            out.append(text[cursor:start])
            out.append(f"‹已隐藏：{name}›")
            cursor = end
        out.append(text[cursor:])
        return "".join(out)
    except Exception:
        return text
