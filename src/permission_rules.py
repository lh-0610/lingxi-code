"""持久化权限规则：让"允许并记住"跨会话、跨重启生效。

会话级白名单（`Session.command_prefix_allowlist` / `edit_path_allowlist`）只活在内存里，
重启或新开会话就得把 git、pytest、常改的那几个文件重新授权一遍。这里加一层落盘的规则，
确认时先查会话白名单、再查这里；两层都查不到才弹卡片。

安全边界与会话白名单完全一致，不因为落盘就放宽：
- 危险命令、会写文件的脚本、Git 写、派子 Agent 的 remember_key 恒为空 → 永远匹配不到规则；
- apply_patch 的占位路径 "(patch)" 不可信任（信任它等于以后所有补丁都不问）；
- 读规则文件失败（被占用 / IO 错）一律当"没有规则"→ 退回问用户，绝不放行。

范围：有当前项目 → 只对该项目生效；无项目 → 全局。撤销入口在设置 → 权限。
存储：chat_memory/permission_rules.json，原子写 + RLock（同 memory_store.py）。
"""
import contextlib
import json
import os
import threading
import uuid
from datetime import datetime

from . import paths as _paths
from .paths import logger

_LOCK = threading.RLock()

KIND_COMMAND = "command"        # value = remember_key：base 命令（"git"）或 MCP 工具（"mcp:fetch"）
KIND_EDIT_PATH = "edit_path"    # value = 规范化后的绝对路径
KINDS = (KIND_COMMAND, KIND_EDIT_PATH)

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"


class _RulesLoadError(Exception):
    """读取规则文件时的瞬时错误（被占用 / IO）。

    区别于 JSON 损坏：这类错误不能当成空结构写回去，否则会把用户攒的规则清光。
    调用方遇到它一律按"没有规则"处理（退回问用户），且绝不写盘。
    """


def normalize_path(path: str) -> str:
    """路径规则的比较口径：绝对 + 规范化 + 大小写按平台。

    会话白名单存的是工具传来的原样字符串，落盘规则统一成这个口径，
    免得 "src/a.py" 和 "D:\\proj\\src\\a.py" 被当成两条规则。
    """
    p = (path or "").strip()
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def trustable_path(path: str) -> bool:
    """能不能信任"对此文件的修改"：得是真实文件路径，不是 apply_patch 的占位名。"""
    p = (path or "").strip()
    return bool(p) and not (p.startswith("(") and p.endswith(")"))


def _empty() -> dict:
    return {"rules": []}


def _load() -> dict:
    with _LOCK:
        path = _paths.permission_rules_file()
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return _empty()
        except UnicodeDecodeError as e:
            logger.warning(f"权限规则文件编码损坏，已重置: {e}")
            return _empty()
        except OSError as e:
            logger.warning(f"读取权限规则失败（瞬时，本次按无规则处理）: {e}")
            raise _RulesLoadError(str(e)) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"权限规则文件损坏，已重置: {e}")
            return _empty()
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            return _empty()
        data["rules"] = [r for r in data["rules"] if _valid(r)]
        return data


def _valid(rule) -> bool:
    return (isinstance(rule, dict)
            and rule.get("kind") in KINDS
            and isinstance(rule.get("value"), str) and bool(rule.get("value"))
            and isinstance(rule.get("id"), str) and bool(rule.get("id")))


def _save(data: dict) -> bool:
    with _LOCK:
        path = _paths.permission_rules_file()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception as e:
            logger.error(f"保存权限规则失败: {e}")
            with contextlib.suppress(Exception):
                if os.path.exists(tmp):
                    os.remove(tmp)
            return False


def _project_key(project) -> str:
    """规则里存的项目口径；None/空 = 无项目（全局规则）。"""
    return normalize_path(project) if project else ""


def _matches(rule: dict, project) -> bool:
    if rule.get("scope") == SCOPE_PROJECT:
        return bool(rule.get("project")) and rule["project"] == _project_key(project)
    return True


def add_rule(kind: str, value: str, project=None) -> dict | None:
    """记一条规则。已存在同样的 (kind, value, 范围) 就返回 None，不重复写盘。

    project 给了 → 只对该项目生效；没给 → 全局。读失败时中止，绝不写盘。
    """
    if kind not in KINDS:
        return None
    value = (value or "").strip()
    if not value:
        return None
    if kind == KIND_EDIT_PATH:
        if not trustable_path(value):
            return None
        value = normalize_path(value)
    proj = _project_key(project)
    scope = SCOPE_PROJECT if proj else SCOPE_GLOBAL
    try:
        data = _load()
    except _RulesLoadError:
        return None
    with _LOCK:
        for r in data["rules"]:
            if (r["kind"] == kind and r["value"] == value
                    and r.get("scope") == scope and r.get("project", "") == proj):
                return None
        rule = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "value": value,
            "scope": scope,
            "project": proj,
            "created": datetime.now().isoformat(),
        }
        data["rules"].append(rule)
        if not _save(data):
            return None
    logger.info(f"记住权限规则: {kind}={value}（{scope}{'：' + proj if proj else ''}）")
    return rule


def remove_rule(rule_id: str) -> dict | None:
    """撤销一条规则，返回被撤销的那条（调用方据此清掉各会话内存里的同名白名单）。"""
    try:
        data = _load()
    except _RulesLoadError:
        return None
    with _LOCK:
        keep = [r for r in data["rules"] if r.get("id") != rule_id]
        if len(keep) == len(data["rules"]):
            return None
        removed = next(r for r in data["rules"] if r.get("id") == rule_id)
        data["rules"] = keep
        if not _save(data):
            return None
    logger.info(f"撤销权限规则: {removed['kind']}={removed['value']}")
    return removed


def list_rules() -> list:
    """全部规则，新的在前（设置 → 权限 列表用）。读失败返回空列表，不写盘。"""
    try:
        data = _load()
    except _RulesLoadError:
        return []
    return sorted(data["rules"], key=lambda r: r.get("created", ""), reverse=True)


def allows_command(remember_key: str, project=None) -> bool:
    """这条命令/MCP 工具是否已被持久规则放行。

    remember_key 为空表示"这次操作不可记住"（危险命令、脚本改文件、Git 写、派子 Agent），
    直接拒绝——落盘规则不能成为绕过危险判定的后门。
    """
    if not remember_key:
        return False
    try:
        data = _load()
    except _RulesLoadError:
        return False
    return any(r["kind"] == KIND_COMMAND and r["value"] == remember_key and _matches(r, project)
               for r in data["rules"])


def allows_edit(path: str, project=None) -> bool:
    """这个文件的修改是否已被持久规则放行。占位路径 "(patch)" 一律不放行。"""
    if not trustable_path(path):
        return False
    target = normalize_path(path)
    if not target:
        return False
    try:
        data = _load()
    except _RulesLoadError:
        return False
    return any(r["kind"] == KIND_EDIT_PATH and r["value"] == target and _matches(r, project)
               for r in data["rules"])