"""Telegram 遥控：后台长轮询收消息 → 白名单 chat_id → 注入 ChatUI 主线程。

httpx getUpdates long-polling（timeout=30），只认自己的 chat_id。
同一时刻只允许一个远程任务——正在生成时回"忙"。
"""
import threading
import time

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, REMOTE_CONTROL
from . import state
from .paths import logger

_BASE_URL: str = ""
_thread: threading.Thread | None = None
_running = False          # shutdown 门控
_offset = 0               # getUpdates offset


# ─── 内部分发 ────────────────────────────────────────────────────────────────────

def _handle_callback_query(cb: dict):
    """处理 inline 按钮回调（允许/拒绝/记住同类）。

    callback_data 格式: ``c:{confirm_id}:a|d|r``
    - a = allow (允许本次)
    - d = deny   (拒绝)
    - r = allow + remember (允许并记住同类命令/文件)
    """
    from . import telegram_push
    from .ui.confirm_bars import _resolve_remote_confirm

    # 白名单校验【点按钮的人】 from.id，而非消息所在 chat.id——后者在群组场景下
    # 任何成员都能过，前者保证只有你本人能批准（私聊里两者等价）
    from_id = str(cb.get("from", {}).get("id", ""))
    if from_id != str(TELEGRAM_CHAT_ID):
        telegram_push.answer_callback(cb["id"], "无权限操作")
        return

    data = cb.get("data", "")
    # 格式: "c:{confirm_id}:{action}"
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "c":
        cid = parts[1]
        act = parts[2]
    elif ":" in data:
        # 兼容旧格式 "allow:CID" / "deny:CID"
        act_legacy, cid = data.split(":", 1)
        act = {"allow": "a", "deny": "d"}.get(act_legacy, "")
    else:
        telegram_push.answer_callback(cb["id"], "未知操作")
        return

    if act == "a":
        ok = _resolve_remote_confirm(cid, allow=True, remember=False)
        telegram_push.answer_callback(cb["id"], "✅ 已允许" if ok else "已过期")
    elif act == "d":
        ok = _resolve_remote_confirm(cid, allow=False, remember=False)
        telegram_push.answer_callback(cb["id"], "❌ 已拒绝" if ok else "已过期")
    elif act == "r":
        ok = _resolve_remote_confirm(cid, allow=True, remember=True)
        telegram_push.answer_callback(cb["id"], "✅ 已记住同类并允许" if ok else "已过期")
    else:
        telegram_push.answer_callback(cb["id"], "未知操作")


def _dispatch(text: str):
    """收到白名单消息后的分发逻辑（在轮询线程内调用）。"""
    ui = getattr(state, "ui_ref", None)
    if ui is None:
        return

    from . import session as _session
    if _session.get_active().is_generating:  # is_generating 已迁到 Session（ui.is_generating 是 fallback）
        from . import telegram_push
        telegram_push.push("info", "灵犀正忙", "当前正在生成中，请稍后再试～")
        logger.info(f"遥控消息被拒（忙）: {text[:50]}")
        return

    # 通过 Signal 跨线程注入 ChatUI 主线程
    ui.submit_from_remote(text)


# ─── 长轮询主循环 ──────────────────────────────────────────────────────────────

def _poll_loop():
    global _offset, _running
    logger.info("Telegram 遥控轮询线程已启动")
    # 启动先跳过积压：离线期间堆积的旧消息/旧回调不该一上线就执行（offset=-1 只取最新一条，
    # 把 _offset 推到它之后即丢弃全部 backlog；也避免旧 callback 撞上重置归 1 的 confirm 计数器）。
    try:
        _r0 = httpx.get(f"{_BASE_URL}/getUpdates", params={"offset": -1, "timeout": 0}, timeout=10)
        if _r0.status_code == 200:
            _d0 = _r0.json()
            if _d0.get("ok") and _d0.get("result"):
                _offset = _d0["result"][-1]["update_id"] + 1
                logger.info(f"Telegram 跳过启动前积压，offset 从 {_offset} 开始")
    except Exception:
        pass
    while _running:
        try:
            r = httpx.get(
                f"{_BASE_URL}/getUpdates",
                params={"offset": _offset, "timeout": 30},
                timeout=35,
            )
            if r.status_code != 200:
                logger.warning(f"getUpdates [{r.status_code}]: {r.text[:200]}")
                time.sleep(5)
                continue

            data = r.json()
            if not data.get("ok"):
                logger.warning(f"getUpdates 非 ok: {data}")
                continue

            for upd in data.get("result", []):
                _offset = upd["update_id"] + 1

                # ── inline 按钮回调 ──
                cb = upd.get("callback_query")
                if cb:
                    _handle_callback_query(cb)
                    continue

                msg = upd.get("message")
                if not msg:
                    continue
                # 白名单校验【发消息的人】 from.id（群组场景下比 chat.id 更严，私聊等价）
                sender_id = str(msg.get("from", {}).get("id", ""))
                if sender_id != str(TELEGRAM_CHAT_ID):
                    logger.debug(f"忽略非白名单消息: from_id={sender_id}")
                    continue
                text = msg.get("text", "")
                if text:
                    logger.info(f"遥控收到: {text[:80]}")
                    _dispatch(text)

        except httpx.TimeoutException:
            pass  # 长轮询超时是正常的
        except Exception as e:
            if _running:
                logger.warning(f"Telegram 轮询异常: {e}")
                time.sleep(5)


# ─── 公开 API ──────────────────────────────────────────────────────────────────

def start():
    """启动轮询守护线程。可安全重复调用。"""
    global _thread, _running, _BASE_URL

    if not TELEGRAM_BOT_TOKEN:
        logger.info("未配置 telegram_bot_token，遥控轮询不启动")
        return
    if not REMOTE_CONTROL:
        logger.info("remote_control=false，遥控轮询不启动")
        return
    if _thread is not None and _thread.is_alive():
        return  # 已在运行

    _BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    _running = True
    _thread = threading.Thread(target=_poll_loop, daemon=True, name="telegram-poll")
    _thread.start()


def shutdown():
    """停止轮询线程。"""
    global _running, _thread
    _running = False
    if _thread is not None:
        # 轮询线程多半正卡在 30s 长轮询（httpx 同步请求没法中断），久等无益；
        # 给 0.5s 优雅退出，没退就交给 daemon 线程随主进程回收。原 join(3) 白等 3s。
        _thread.join(timeout=0.5)
        _thread = None
    logger.info("Telegram 遥控轮询已停止")
