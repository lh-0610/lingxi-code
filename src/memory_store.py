"""长期记忆存储（跨会话持久化）。

- 独立于 memory.py（那个管会话历史，这个管长期记忆）
- 使用 threading.RLock 串行化所有读写（参考 memory.py 模式）
- 存储文件：chat_memory/long_term_memory.json
- 文件不存在/损坏时静默返回空，不崩主流程
"""
import os
import json
import uuid
import contextlib
import threading
from datetime import datetime

from .paths import logger, memory_dir, long_term_memory_file
from .limits import MEMORY_FACT_MAX_LENGTH


# 串行化 chat_memory/long_term_memory.json 的读-改-写。
# 用 RLock 是因为同一线程内可能嵌套调用。
_LOCK = threading.RLock()

# 存储文件路径(按当前用户上下文动态解析)→ paths.long_term_memory_file()


def _ensure_file():
    """确保存储文件存在，不存在则创建空结构。"""
    with _LOCK:
        os.makedirs(memory_dir(), exist_ok=True)
        if not os.path.exists(long_term_memory_file()):
            with open(long_term_memory_file(), "w", encoding="utf-8") as f:
                json.dump({"memories": []}, f, ensure_ascii=False, indent=2)


class _MemoryLoadError(Exception):
    """读取记忆文件时的非"损坏"错误（如文件被占用 / 临时 IO 错）。

    区别于 JSON 损坏：这类错误**不能**当成空结构返回，否则后续 _save 会把空写回去、
    把真实记忆清光。调用方（add/delete 等会写盘的）遇到它要中止本次操作，绝不 _save。
    """


def _load() -> dict:
    """加载记忆数据。

    - 文件**真损坏**（JSON 解析失败 / 结构不对）→ 返回空结构（可被覆盖重置）。
    - 文件**读取异常**（IO / 被占用等瞬时错）→ 抛 _MemoryLoadError，**不**返回空，
      避免后续 _save 把空结构写回去导致记忆全丢。
    """
    _ensure_file()
    with _LOCK:
        try:
            with open(long_term_memory_file(), "r", encoding="utf-8") as f:
                raw = f.read()
        except UnicodeDecodeError as e:
            # 字节级损坏（非法编码）= 真损坏，重置允许重建
            logger.warning(f"长期记忆文件编码损坏，已重置: {e}")
            return {"memories": []}
        except OSError as e:
            # IO / 权限 / 文件被占用 = 瞬时错误，绝不当空返回（否则后续 _save 会写空丢数据）
            logger.warning(f"读取长期记忆失败（瞬时，跳过本次操作）: {e}")
            raise _MemoryLoadError(str(e)) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # JSON 语法损坏 = 真损坏，重置
            logger.warning(f"长期记忆文件损坏，已重置: {e}")
            return {"memories": []}
        if not isinstance(data, dict) or "memories" not in data:
            return {"memories": []}
        return data


def _save(data: dict):
    """原子保存：先写临时文件再 os.replace 替换。

    崩溃 / 断电时要么是旧文件、要么是新文件，不会留下半截损坏的 JSON——
    对"珍贵且不可重建"的长期记忆很重要。
    """
    with _LOCK:
        tmp = long_term_memory_file() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, long_term_memory_file())  # 原子替换
        except Exception as e:
            logger.error(f"保存长期记忆失败: {e}")
            with contextlib.suppress(Exception):
                if os.path.exists(tmp):
                    os.remove(tmp)


def _normalize_text(text: str) -> str:
    """规范化文本用于去重比较。"""
    return text.strip().lower()


def add_memory(text: str, scope: str = "global") -> dict:
    """添加一条长期记忆（自动去重）。
    
    Args:
        text: 一句话事实（短）
        scope: 记忆范围，默认 "global"
    
    Returns:
        添加的记忆条目，如果已存在则返回 None
    """
    # 截断过长文本
    if len(text) > MEMORY_FACT_MAX_LENGTH:
        text = text[:MEMORY_FACT_MAX_LENGTH] + "..."
    
    text = text.strip()
    if not text:
        return None

    try:
        data = _load()
    except _MemoryLoadError:
        # 读失败（瞬时）：中止本次添加，绝不写盘（否则可能把空结构覆盖真实记忆）
        return None
    normalized = _normalize_text(text)

    # 去重检查
    for mem in data["memories"]:
        if _normalize_text(mem.get("text", "")) == normalized:
            logger.info(f"记忆已存在，跳过: {text}")
            return None

    # 生成新条目
    mem_id = uuid.uuid4().hex[:8]  # 随机 id，避免同毫秒撞 ID
    new_mem = {
        "id": mem_id,
        "text": text,
        "created": datetime.now().isoformat(),
        "scope": scope,
    }
    
    data["memories"].append(new_mem)
    _save(data)
    logger.info(f"添加长期记忆: {text}")
    return new_mem


def list_memories(scope: str = "global") -> list:
    """列出指定范围的所有记忆。
    
    Args:
        scope: 记忆范围，默认 "global"
    
    Returns:
        记忆条目列表
    """
    try:
        data = _load()
    except _MemoryLoadError:
        return []  # 读失败：本次当作没有记忆，不影响下次（且没写盘，无数据风险）
    return [m for m in data["memories"] if m.get("scope") == scope]


def delete_memory(mem_id: str) -> bool:
    """删除指定 ID 的记忆。
    
    Args:
        mem_id: 记忆 ID
    
    Returns:
        是否删除成功
    """
    try:
        data = _load()
    except _MemoryLoadError:
        return False  # 读失败：中止，绝不写盘
    original_len = len(data["memories"])
    data["memories"] = [m for m in data["memories"] if m.get("id") != mem_id]
    
    if len(data["memories"]) < original_len:
        _save(data)
        logger.info(f"删除长期记忆: {mem_id}")
        return True
    return False


def search_memories(query: str, scope: str = "global") -> list:
    """按关键词搜索记忆。
    
    Args:
        query: 搜索关键词
        scope: 记忆范围，默认 "global"
    
    Returns:
        匹配的记忆条目列表
    """
    query_lower = query.strip().lower()
    if not query_lower:
        return []
    try:
        data = _load()
    except _MemoryLoadError:
        return []
    
    results = []
    for mem in data["memories"]:
        if mem.get("scope") != scope:
            continue
        if query_lower in mem.get("text", "").lower():
            results.append(mem)
    return results


def render_memories_for_prompt(scope: str = "global", max_chars: int = 4000) -> str:
    """渲染记忆为 system prompt 注入文本。
    
    Args:
        scope: 记忆范围，默认 "global"
        max_chars: 最大字符数，超出时保留最近的
    
    Returns:
        渲染后的文本，无记忆时返回空字符串
    """
    memories = list_memories(scope)
    if not memories:
        return ""
    
    # 按创建时间排序（最新的在前）
    memories.sort(key=lambda m: m.get("created", ""), reverse=True)
    
    lines = []
    lines.append("【关于用户的长期记忆（你应自然地运用这些，不要生硬复述）】")
    
    total_chars = 0
    for mem in memories:
        text = mem.get("text", "").strip()
        if not text:
            continue
        line = f"- {text}"
        if total_chars + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total_chars += len(line) + 1  # +1 for newline
    
    return "\n".join(lines)
