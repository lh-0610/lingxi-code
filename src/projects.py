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

from .paths import logger, memory_dir, projects_file


def _load() -> dict:
    try:
        if os.path.exists(projects_file()):
            with open(projects_file(), "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"读取 projects.json 失败: {e}")
    return {"current": None, "projects": []}


def _save(data: dict) -> None:
    try:
        os.makedirs(memory_dir(), exist_ok=True)
        with open(projects_file(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 projects.json 失败: {e}")


def list_projects() -> list[dict]:
    """返回项目列表，每个 {path, name}。"""
    return _load().get("projects", [])


def get_current() -> str | None:
    """返回当前项目根路径；None 表示"无项目（全局）"。"""
    return _load().get("current")


def set_current(path: str | None) -> None:
    """切换当前项目。None = 无项目（全局）。"""
    data = _load()
    data["current"] = path
    _save(data)
    logger.info(f"切换项目: {path or '无项目（全局）'}")


def add_project(path: str, name: str | None = None) -> bool:
    """添加新项目；同路径已存在则跳过。"""
    if not path or not os.path.isdir(path):
        logger.warning(f"项目路径无效: {path}")
        return False
    path = os.path.normpath(path).replace("\\", "/")
    data = _load()
    for p in data.get("projects", []):
        if p["path"] == path:
            return False  # 已存在
    if not name:
        name = os.path.basename(path) or path
    data.setdefault("projects", []).append({"path": path, "name": name})
    _save(data)
    logger.info(f"添加项目: {name} ({path})")
    return True


def remove_project(path: str) -> bool:
    """从列表移除项目。如果当前项目正好被移除，会回退到无项目。"""
    if not path:
        return False
    data = _load()
    before = len(data.get("projects", []))
    data["projects"] = [p for p in data.get("projects", []) if p["path"] != path]
    if data.get("current") == path:
        data["current"] = None
    after = len(data["projects"])
    if before == after:
        return False
    _save(data)
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
