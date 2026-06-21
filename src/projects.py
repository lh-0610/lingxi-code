"""项目（工作区）管理。

每条会话可以归属一个"项目"（用户选的本地文件夹路径）。侧边栏顶部的项目切换器
让用户在不同项目间切换，会话列表会过滤为当前项目的会话。

存储：chat_memory/projects.json
  {
    "current": "D:/myproject" | null,    # null = 无项目（全局）
    "projects": [
      {"path": "D:/myproject", "name": "myproject"},
      ...
    ]
  }
"""
import json
import os
import threading

from .paths import logger, memory_dir, projects_file

# 串行化所有 projects.json 读写。用 RLock：set_current/add_project/remove_project
# 整体持锁做"读-改-写"，内部还会再调持锁的 _load/_save——RLock 可重入不自死锁。
# 防多线程（主线程切项目 / 侧栏增删 / worker 读取）交错导致丢更新。
_LOCK = threading.RLock()


def _load(for_write: bool = False) -> dict:
    """读取 projects.json。
      文件不存在            → 返回空结构；
      JSON 损坏            → 当可重建，返回空（后续写会覆盖坏文件）；
      瞬时 IO 错（占用/权限）→ for_write=True 时【抛出】，绝不返回空让 _save 清空真实数据；
                              纯读（for_write=False）时返回空，避免读路径因瞬时错误崩 UI。
    """
    with _LOCK:
        path = projects_file()
        if not os.path.exists(path):
            return {"current": None, "projects": []}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 损坏（JSON 语法错 或 非法 UTF-8 字节）。
            if for_write:
                # 写路径：先把坏文件备份成 .corrupt 再按空重建（留一份可人工恢复）。
                # 【备份失败则中止】——绝不在没留底的情况下用空数据覆盖唯一的副本，
                # 否则 .corrupt 没写成、原文件又被 os.replace 覆盖，损坏但可能可救的数据永久丢失。
                try:
                    import shutil
                    shutil.copy2(path, path + ".corrupt")
                    logger.warning(f"projects.json 损坏，已备份到 {path}.corrupt 后重建: {e}")
                except OSError as be:
                    logger.error(f"projects.json 损坏且备份失败，中止写以防永久丢数据: {be}")
                    raise   # 抛 OSError → 写操作调用方 return False，不覆盖原文件
            else:
                logger.warning(f"projects.json 解析失败，按空处理: {e}")
            return {"current": None, "projects": []}
        except OSError as e:
            logger.warning(f"projects.json 读取失败（瞬时）: {e}")
            if for_write:
                raise
            return {"current": None, "projects": []}


def _save(data: dict) -> bool:
    """原子写 projects.json。成功 True / 失败 False——调用方据此报成败，
    不能写失败还报成功（否则用户重启后改动丢失却毫不知情）。"""
    with _LOCK:
        # 临时文件名带 PID，降低多实例并发写时互撞概率
        tmp = projects_file() + f".{os.getpid()}.tmp"
        try:
            os.makedirs(memory_dir(), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, projects_file())   # 原子替换，崩溃不留半截 JSON
            return True
        except Exception as e:
            logger.warning(f"保存 projects.json 失败: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)             # 清理残留临时文件
            except OSError:
                pass
            return False


def list_projects() -> list[dict]:
    """返回项目列表，每个 {path, name}。"""
    return _load().get("projects", [])


def get_current() -> str | None:
    """返回当前项目根路径；None 表示"无项目（全局）"。"""
    return _load().get("current")


def set_current(path: str | None) -> bool:
    """切换当前项目。None = 无项目（全局）。返回是否持久化成功。"""
    with _LOCK:
        try:
            data = _load(for_write=True)
        except OSError:
            logger.warning(f"切换项目中止：读取 projects.json 失败（瞬时），不冒清空风险: {path}")
            return False
        data["current"] = path
        ok = _save(data)
    if ok:
        logger.info(f"切换项目: {path or '无项目（全局）'}")
    else:
        logger.warning(f"切换项目未能持久化（磁盘/权限问题）: {path}")
    return ok


def add_project(path: str, name: str | None = None) -> bool:
    """添加新项目；同路径已存在则跳过。返回 True 仅当确实新增【且持久化成功】。"""
    if not path or not os.path.isdir(path):
        logger.warning(f"项目路径无效: {path}")
        return False
    path = os.path.normpath(path).replace("\\", "/")
    with _LOCK:
        try:
            data = _load(for_write=True)
        except OSError:
            logger.warning(f"添加项目中止：读取 projects.json 失败（瞬时）: {path}")
            return False
        for p in data.get("projects", []):
            if p["path"] == path:
                return False  # 已存在
        if not name:
            name = os.path.basename(path) or path
        data.setdefault("projects", []).append({"path": path, "name": name})
        if not _save(data):
            logger.warning(f"添加项目未能持久化（磁盘/权限问题）: {path}")
            return False
    logger.info(f"添加项目: {name} ({path})")
    return True


def remove_project(path: str) -> bool:
    """从列表移除项目。如果当前项目正好被移除，会回退到无项目。
    返回 True 仅当确实移除【且持久化成功】。"""
    if not path:
        return False
    with _LOCK:
        try:
            data = _load(for_write=True)
        except OSError:
            logger.warning(f"移除项目中止：读取 projects.json 失败（瞬时）: {path}")
            return False
        before = len(data.get("projects", []))
        data["projects"] = [p for p in data.get("projects", []) if p["path"] != path]
        if data.get("current") == path:
            data["current"] = None
        after = len(data["projects"])
        if before == after:
            return False
        if not _save(data):
            logger.warning(f"移除项目未能持久化（磁盘/权限问题）: {path}")
            return False
    logger.info(f"移除项目: {path}")
    return True


def get_current_name() -> str:
    """显示用：当前项目名（无项目时返回"无项目"）。"""
    path = get_current()
    if not path:
        return "无项目（全局）"
    for p in list_projects():
        if p["path"] == path:
            return p["name"]
    return os.path.basename(path) or path
