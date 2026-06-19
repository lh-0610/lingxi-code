"""Telegram Bot API 推送通知。

用 httpx（非 httpagent）POST sendMessage，发送带 level emoji 的消息。
"""
import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .paths import logger


# level → emoji 映射
_LEVEL_EMOJI = {
    "error":         "🔴",
    "action_needed": "🟡",
    "done":          "🟢",
    "info":          "🔵",
}


def push(level: str, title: str, message: str) -> bool:
    """向 Telegram 推一条消息。返回是否成功。

    参数:
        level: error / action_needed / done / info
        title: 标题（一行粗体）
        message: 正文（可多行）
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram 未配置 token/chat_id，跳过推送")
        return False

    emoji = _LEVEL_EMOJI.get(level, "⚪")
    # 纯文本发送，不用 parse_mode：回传内容（目录列表/代码/动作描写）常含未配对的
    # * _ [ ` 等 Markdown 特殊字符，用 parse_mode=Markdown 会 400（can't parse
    # entities）。纯文本最稳，标题靠 emoji + 换行区分。
    text = f"{emoji} {title}\n{message}"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },
            timeout=10,
        )
        if r.status_code == 200:
            logger.info(f"Telegram 推送成功: {title}")
            return True
        else:
            logger.warning(f"Telegram 推送失败 [{r.status_code}]: {r.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"Telegram 推送异常: {e}")
        return False


def push_long(level: str, title: str, message: str, chunk_size: int = 3500) -> bool:
    """长消息分段发送（Telegram 单条上限 4096，按 chunk_size 留余量切）。

    用于遥控回复回传——AI 回复可能很长，截断到 200 字会丢内容。
    """
    message = message or "(无内容)"
    chunks = [message[i:i + chunk_size] for i in range(0, len(message), chunk_size)]
    ok = True
    for idx, chunk in enumerate(chunks):
        t = title if idx == 0 else f"{title}（续 {idx + 1}/{len(chunks)}）"
        ok = push(level, t, chunk) and ok
    return ok



# ---------------------------------------------------------------------------
# 操作确认：inline 按钮
# ---------------------------------------------------------------------------

def push_confirm(text: str, confirm_id: str, is_destructive: bool = False) -> int | None:
    """发一条带 ✅允许 / ❌拒绝 / 📝记住同类 inline 按钮的确认消息。

    callback_data 格式: ``c:{id}:a`` / ``c:{id}:d`` / ``c:{id}:r``
    is_destructive=True 时隐藏"记住同类"按钮（危险命令不可永久授权）。
    返回 message_id（后续 edit_message_text 用），失败返回 None。

    注意：不用 parse_mode——命令/diff 常含未配对的 *_[`` ` `` 等字符，Markdown
    解析会 400（can't parse entities）让按钮发不出去。纯文本最稳（同 push()）。
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None

    row = [
        {"text": "✅ 允许", "callback_data": f"c:{confirm_id}:a"},
        {"text": "❌ 拒绝", "callback_data": f"c:{confirm_id}:d"},
    ]
    if not is_destructive:
        row.append({"text": "📝 记住同类", "callback_data": f"c:{confirm_id}:r"})

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "reply_markup": {
            "inline_keyboard": [row],
        },
    }
    try:
        r = httpx.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        logger.warning(f"Telegram push_confirm 失败 [{r.status_code}]: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram push_confirm 异常: {e}")
    return None


def answer_callback(callback_query_id: str, text: str = "") -> bool:
    """回答 callback_query，解除按钮 loading 状态。"""
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        r = httpx.post(url, json={
            "callback_query_id": callback_query_id,
            "text": text,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram answer_callback 异常: {e}")
        return False


def edit_message_text(message_id: int, text: str) -> bool:
    """编辑已有消息的文本（用于把确认按钮替换为结果文案）。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    try:
        r = httpx.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": text,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram edit_message_text 异常: {e}")
        return False
