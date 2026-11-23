"""设置热更新：config.json 改了以后，让改动在运行中的程序里生效，不用重启。

两个入口都走这里：设置对话框点保存、用户手改 config.json（界面层的文件监视，见 ui/chat_window.py）。

  config.reload()  重读文件、就地重算 config 模块的常量，报出哪些常量变了
  apply(changed)   按变化的部分处理：重建模型列表并把各会话的模型对回去、丢掉建好的模型客户端、
                   刷新打码表、重启遥控轮询、重连 MCP、重开 LSP

大多数配置本来就是用的时候读 `config.X`，reload 完就生效，这里不用管。
要这里动手的，是启动时就**定下来**的东西：建好的客户端、后台线程、子进程。

仍然要重启的只有 NATIVE_TITLE_BAR（系统标题栏 / 自绘标题栏）：窗口建的时候定下来的。
"""
import threading
from dataclasses import dataclass, field

from . import config
from .paths import logger

# 重启才生效的常量
RESTART_KEYS = frozenset({"NATIVE_TITLE_BAR"})

_TELEGRAM_KEYS = frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "REMOTE_CONTROL"})
_LSP_KEYS = frozenset({"LSP_SERVERS", "LSP_LANGUAGES"})
_MCP_KEYS = frozenset({"MCP_SERVERS", "MCP_ENABLED"})
# 模型列表 / 模型客户端跟着变的常量：密钥、端点、各家模型清单、自定义模型
_MODEL_EXTRA_KEYS = frozenset({"CUSTOM_MODELS", "OLLAMA_BASE_URL"})


def _affects_models(name: str) -> bool:
    if name.startswith(("RAG_", "WEB_SEARCH_", "BOCHA_")):
        return False   # 知识库 embedding、网络搜索的密钥 / 端点：用的时候现读，和聊天模型无关
    return (name in _MODEL_EXTRA_KEYS or name.endswith("_API_KEY")
            or name.endswith("_BASE_URL") or name.endswith("_MODELS"))


@dataclass
class ApplyResult:
    changed: set = field(default_factory=set)
    restart_needed: bool = False
    models_changed: bool = False
    # 用的模型从列表里没了、换成了别的：[(会话, 原模型名, 换成的模型名)]
    model_fallbacks: list = field(default_factory=list)
    mcp_restarting: bool = False


def reload_and_apply(data: dict | None = None) -> ApplyResult | None:
    """重读 config.json 并应用。文件读不了 / 格式坏了返回 None，什么都不动（保持现在的配置）。"""
    changed = config.reload(data)
    if changed is None:
        return None
    return apply(changed)


def apply(changed: set) -> ApplyResult:
    res = ApplyResult(changed=set(changed))
    if not changed:
        return res
    logger.info(f"配置已热更新：{sorted(changed)}")
    _refresh_secret_mask()
    if any(_affects_models(k) for k in changed):
        res.models_changed = True
        res.model_fallbacks = _refresh_models()
    if changed & _TELEGRAM_KEYS:
        _restart_telegram()
    if changed & _LSP_KEYS:
        _restart_lsp()
    if changed & _MCP_KEYS:
        res.mcp_restarting = True
        _restart_mcp_bg()
    res.restart_needed = bool(changed & RESTART_KEYS)
    return res


def _refresh_secret_mask():
    """新填的 key 立刻进打码表，否则按新配置跑工具时它的值还能原样出现在结果里。"""
    try:
        from . import secret_mask
        secret_mask.refresh(config._config)
    except Exception as e:
        logger.warning(f"刷新打码表失败: {e}")


def _refresh_models() -> list:
    """重建模型列表，把每个会话的模型下标对到新列表上；丢掉建好的客户端，重新激活当前会话的模型。

    返回模型被删掉、退回默认模型的会话：[(会话, 原模型名, 新模型名)]。"""
    from . import agent, models, session
    old_list = list(models.MODEL_LIST)
    mapping = models.refresh_model_list()
    fallback = None
    fallbacks = []
    for sess in session.live_sessions():
        idx = getattr(sess, "current_model_index", 0) or 0
        new = mapping.get(idx, -1)
        if new < 0:
            if fallback is None:
                fallback = agent._resolve_default_model_index()
            new = fallback
            old_name = old_list[idx][0] if 0 <= idx < len(old_list) else "?"
            fallbacks.append((sess, old_name, models.MODEL_LIST[new][0]))
        sess.current_model_index = new
    agent._BOUND_LLM_CACHE.clear()
    agent._activate_llm()
    return fallbacks


def _restart_telegram():
    try:
        from . import telegram_poll
        telegram_poll.shutdown()
        telegram_poll.start()
    except Exception as e:
        logger.warning(f"重启遥控轮询失败: {e}")


def _restart_lsp():
    """关掉正在跑的语言服务器；下次查定义 / 引用时按新配置重新起。"""
    try:
        from . import lsp_client
        lsp_client.shutdown()
    except Exception as e:
        logger.warning(f"关闭 LSP 失败: {e}")


_mcp_lock = threading.Lock()


def _restart_mcp_bg():
    """MCP 连接要几秒到十几秒，放后台线程。连续改两次时第二次排在第一次后面（锁），
    最后一次重连读到的总是最新的配置文件。"""
    def run():
        with _mcp_lock:
            try:
                from . import agent, mcp_client
                tools = mcp_client.restart()
                # 绑定的工具列表变了：丢掉带旧 MCP 工具的绑定，当前模型立刻按新工具重新绑定
                agent._BOUND_LLM_CACHE.clear()
                agent._activate_llm()
                logger.info(f"MCP 已按新配置重连：可用 {len(tools)} 个工具")
            except Exception as e:
                logger.warning(f"MCP 重连失败: {e}", exc_info=True)
    threading.Thread(target=run, daemon=True, name="mcp-restart").start()
