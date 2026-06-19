"""统一通知入口：分级 + 节流去重 + 环形历史。

外部模块调用 notify(level, title, message, event_type) 即可，
内部分流到 telegram_push 等渠道。
"""
import time
from collections import deque

from .config import (
    NOTIFY_ENABLED,
    NOTIFY_LEVELS,
    NOTIFY_THROTTLE_SECONDS,
)
from . import telegram_push
from .paths import logger

# 节流记录：event_type → 上次发送时间戳
_last_sent: dict[str, float] = {}

# 环形历史（最多保留 100 条）
_HISTORY_MAXLEN = 100
_history: deque = deque(maxlen=_HISTORY_MAXLEN)


def notify(level: str, title: str, message: str, event_type: str) -> bool:
    """发送一条通知。

    参数:
        level: error / action_needed / done / info
        title: 短标题
        message: 详细内容
        event_type: 事件类型标识（用于节流去重，如 "agent_error"）
    返回: 是否实际发送
    """
    now = time.time()

    # 1. 总开关
    if not NOTIFY_ENABLED:
        return False

    # 2. 分级过滤
    if level not in NOTIFY_LEVELS:
        return False

    # 3. 节流去重
    last = _last_sent.get(event_type, 0)
    if now - last < NOTIFY_THROTTLE_SECONDS:
        logger.debug(f"通知被节流: {event_type}（{NOTIFY_THROTTLE_SECONDS}s 内不重复）")
        return False

    # 4. 存历史（不管成败都记一次尝试）
    _history.append({
        "time": now,
        "level": level,
        "title": title,
        "message": message,
        "event_type": event_type,
    })

    # 5. 推送；只有成功才记节流时间——失败不该把重试窗口占掉（否则网络恢复后真通知被压掉）
    ok = telegram_push.push(level, title, message)
    if ok:
        _last_sent[event_type] = now
    return ok


def notify_long(level: str, title: str, message: str, event_type: str) -> bool:
    """同 notify 的开关 / 分级 / 节流判断，但用 push_long 发【完整】内容（长则分段不截断）。

    用于"任务完成"这类需要把完整回复发回手机的通知——不分电脑/手机发起都完整推。
    """
    now = time.time()
    if not NOTIFY_ENABLED:
        return False
    if level not in NOTIFY_LEVELS:
        return False
    last = _last_sent.get(event_type, 0)
    if now - last < NOTIFY_THROTTLE_SECONDS:
        logger.debug(f"通知被节流: {event_type}（{NOTIFY_THROTTLE_SECONDS}s 内不重复）")
        return False
    _history.append({
        "time": now, "level": level, "title": title,
        "message": message, "event_type": event_type,
    })
    ok = telegram_push.push_long(level, title, message)
    if ok:
        _last_sent[event_type] = now   # 成功才占节流窗口，失败留给重试
    return ok


def get_history() -> list[dict]:
    """返回最近的通知历史（副本）。"""
    return list(_history)
