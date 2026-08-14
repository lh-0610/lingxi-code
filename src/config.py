"""config.json 加载与密钥导出。

启动时一次性读取，对外暴露各家上游的常量。
任何模块要拿密钥/base_url 都从这里导入，不要重复读文件。
"""
import json

from .paths import CONFIG_PATH, logger


try:
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as _f:
        _config = json.load(_f)
except FileNotFoundError:
    logger.warning("config.json 不存在，请复制 config.example.json 为 config.json 并填入密钥")
    _config = {}
except json.JSONDecodeError as e:
    logger.error(f"config.json 格式错误: {e}，使用空配置")
    _config = {}


OLLAMA_BASE_URL = _config.get("ollama_base_url", "http://127.0.0.1:11434")
CLOUD_API_KEY = _config.get("qwen_api_key", "")
CLOUD_BASE_URL = _config.get("qwen_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
ANTHROPIC_API_KEY = _config.get("anthropic_api_key", "")
GOOGLE_API_KEY = _config.get("google_api_key", "")
MIMO_API_KEY = _config.get("mimo_api_key", "")
MIMO_BASE_URL = _config.get("mimo_base_url", "https://token-plan-sgp.xiaomimimo.com/anthropic")
DEEPSEEK_API_KEY = _config.get("deepseek_api_key", "")
DEEPSEEK_BASE_URL = _config.get("deepseek_base_url", "https://api.deepseek.com")
# OpenAI Responses API 通道（DeepSeek V4-Flash 等挂在 /responses 端点；默认同 DeepSeek 官方）。
# 复用 deepseek key，base_url 指向支持 /responses 的端点
RESPONSES_API_KEY = _config.get("responses_api_key", "") or DEEPSEEK_API_KEY
RESPONSES_BASE_URL = _config.get("responses_base_url", "") or "https://api.deepseek.com"

# 各 provider 的可选 model_id 列表（用户在设置里编辑，重启后生效）
MIMO_MODELS       = _config.get("mimo_models", ["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni"])
QWEN_CLOUD_MODELS = _config.get("qwen_cloud_models", ["qwen3.5-plus", "qwen-max", "qwen-plus", "qwen-turbo"])
OLLAMA_MODELS     = _config.get("ollama_models", ["qwen3.5:latest"])
ANTHROPIC_MODELS  = _config.get("anthropic_models", ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"])
GEMINI_MODELS     = _config.get("gemini_models", [])
DEEPSEEK_MODELS   = _config.get("deepseek_models", ["deepseek-v4-flash", "deepseek-v4-pro"])
# Responses API 模型列表（走 output_version="responses/v1"；DeepSeek 目前仅 v4-flash 支持）
RESPONSES_MODELS  = _config.get("responses_models", ["deepseek-v4-flash"])
CLAUDE_CODE_MODEL = _config.get("claude_code_model", "")
# Claude Code 模式 Act 时是否给 CLI 带 --dangerously-skip-permissions（绕过 claude 全部
# 权限检查、全自动）。**默认 False，更安全**：此时 Act 走 --permission-mode acceptEdits
# （自动批准编辑+常见文件命令、不挂起，但不绕过全部检查）。需要真·全自动（任意命令免确认）
# 再在 config.json 设为 true。注：Plan 模式恒为 --permission-mode plan（只读），不受此开关影响。
CLAUDE_CODE_SKIP_PERMISSIONS: bool = bool(_config.get("claude_code_skip_permissions", False))
VISION_MODEL_ID   = _config.get("vision_model_id", "")
# 启动默认选中的模型（按 model_id 匹配；找不到退回列表第一个）
# 用 `or` 而非 .get 默认值：键存在但为空串（设置页空着保存过）时也回退，
# 否则会落到 MODEL_LIST[0] = Claude Code，表现为"默认模型莫名变成 claude"
DEFAULT_MODEL_ID  = _config.get("default_model_id") or "mimo-v2.5-pro"


# 自定义模型列表。用户在设置里加自己的 OpenAI/Anthropic 兼容模型。
# 每项格式：{
#   "name":              "GPT-4 Turbo",         # 显示名（顶栏下拉看到的）
#   "model_id":          "gpt-4-turbo",         # 发给 API 的 model 字段
#   "api_key":           "sk-...",
#   "base_url":          "https://api.openai.com/v1",
#   "protocol":          "openai" | "anthropic",  # 走哪个 SDK
#   "supports_vision":   false,                  # 是否能吃图片
#   "supports_thinking": false,                  # 是否支持 reasoning 模式
# }
CUSTOM_MODELS = _config.get("custom_models", [])

# 按 model_id 覆盖模型的上下文窗口（token）。内置窗口（models.py _DEFAULT_CONTEXT_WINDOWS）
# 估错时，在这里填 {"model_id": 窗口} 即可纠正，不用改代码。例：{"deepseek-v4-pro": 1048576}
MODEL_CONTEXT_WINDOWS = _config.get("model_context_windows", {}) or {}

# 自我校验闭环：编辑文件后自动跑静态检查（lint/语法），把错误回灌给模型自修
AUTO_CHECK_AFTER_EDIT = _config.get("auto_check_after_edit", True)
# 非 Python 项目可自定义检查命令，用 {file} 占位被检文件；
# 留空 = 只对 Python 自动用 ruff（没装则退化到 py_compile 只查语法）
CHECK_COMMAND = _config.get("check_command", "")
# 编辑 Python 后额外跑 mypy 类型检查（只取 call-arg/name-defined 等高信号错误码，
# 抓"臆造 API / 参数错"；动态属性噪声码已排除）。没装 mypy 时静默跳过。
TYPE_CHECK_AFTER_EDIT = _config.get("type_check_after_edit", True)

# LSP 代码导航（find_definition / find_references 使用的后端列表，按优先级排序）
LSP_SERVERS: list[str] = _config.get("lsp_servers", ["pyright-langserver", "pylsp"])
if not isinstance(LSP_SERVERS, list) or not all(isinstance(s, str) for s in LSP_SERVERS):
    LSP_SERVERS = ["pyright-langserver", "pylsp"]


def _cfg_dict(root: dict, key: str) -> dict:
    """安全取配置里的 dict 段：写成字符串/数组等（如 "rag": "oops"）时按未配置处理并告警，
    **不抛异常**——后续 .get() 若在非 dict 上调用会 AttributeError 直接炸启动。"""
    v = root.get(key) or {}
    if not isinstance(v, dict):
        logger.warning(f"config.json 的 {key} 段应是对象（实为 {type(v).__name__}），按未配置处理")
        return {}
    return v


# 通知（Telegram 推送）
_notify_cfg = _cfg_dict(_config, "notify")
NOTIFY_ENABLED: bool = _notify_cfg.get("enabled", False)
NOTIFY_LEVELS: list = _notify_cfg.get("levels", ["error", "action_needed", "done"])
NOTIFY_THROTTLE_SECONDS: int = _notify_cfg.get("throttle_seconds", 10)
TELEGRAM_BOT_TOKEN: str = _notify_cfg.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID: str = _notify_cfg.get("telegram_chat_id", "")

# 遥控（Telegram 远程发送消息给桌面端）
_remote_cfg = _cfg_dict(_config, "remote_control")
REMOTE_CONTROL: bool = _remote_cfg.get("enabled", False)
# 遥控安全分级（mode 三选一，默认最安全的 chat_only）：
#   chat_only     —— 禁所有工具，纯对话（默认；不懂/不配时最安全，不会意外泄露）
#   safe_readonly —— 可读代码，但敏感文件黑名单拦截；写工具/命令仍禁
#   unrestricted  —— 不设防，全部工具可用（你完全信任环境时）
_mode = (_remote_cfg.get("mode") or "chat_only").lower()
if _mode not in ("chat_only", "safe_readonly", "unrestricted"):
    _mode = "chat_only"
REMOTE_MODE: str = _mode
# 联网查询独立开关:开了则不论 mode 都放行 fetch_url / web_search(只读网络工具)。
# 默认 false(网络外发保守)。给 Web/手机版"能上网查"用,不必整体放到 unrestricted。
REMOTE_ALLOW_WEB: bool = bool(_remote_cfg.get("allow_web_search", False))
# safe_readonly 模式下，用户在内置黑名单之外【追加】的敏感文件名/后缀
REMOTE_BLOCKLIST: list = _remote_cfg.get("readonly_blocklist", []) or []
# 是否把需确认的操作（run_command / edit_file / MCP）推到手机 Telegram inline 按钮。
# 注意：不分电脑/手机发起，只要开启就都推——人在电脑前走开时也能掏手机批。
# 配了 telegram_bot_token/chat_id 才实际生效（push_confirm 内部会校验，没配则静默跳过）。
REMOTE_TELEGRAM_CONFIRM: bool = _remote_cfg.get("telegram_confirm", True)

# 网络搜索（Tavily）
WEB_SEARCH_API_KEY: str = _config.get("web_search_api_key", "")
# fetch_url 是否走系统代理。**默认 False（直连）**：直连才能校验实际 peer IP、SSRF 防线完整。
# 设 true 后按系统代理抓取（适合必须靠代理访问外网的环境），但此时无法校验目标真实 IP，
# 防 DNS 重绑定的 peer 检查会跳过——属于用代理换可达性的显式取舍。
FETCH_URL_ALLOW_PROXY: bool = bool(_config.get("fetch_url_allow_proxy", False))

# run_command 子进程是否脱敏环境变量。**默认 True**：模型自己拼命令串，一个 `env` /
# `echo $XXX_TOKEN` 就能把宿主的密钥读进工具结果 → 进上下文 → 落进会话文件。名字里含
# KEY/SECRET/TOKEN/PASSWORD/PASSWD/CREDENTIAL 的变量默认不传给子进程。
# 副作用是需要这些变量的命令会失败（`aws s3 ls`、要 NPM_TOKEN 的 npm publish 等）——
# 用 run_command_env_keep 精确放行，不要为了个别命令整个关掉。
RUN_COMMAND_SCRUB_ENV: bool = bool(_config.get("run_command_scrub_env", True))
RUN_COMMAND_ENV_KEEP: list = [
    str(x) for x in (_config.get("run_command_env_keep") or []) if str(x).strip()
]

# MCP Servers 配置（字典，key=server 名，value=启动参数）
MCP_SERVERS: dict = _cfg_dict(_config, "mcp_servers")

def set_rag_kb_dir(path: str) -> bool:
    """把知识库目录写回 config.json 的 rag.kb_dir，并同步更新本模块的 RAG_KB_DIR。
    供 UI「选择知识库目录」用。成功 True / 失败 False。"""
    global RAG_KB_DIR
    try:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}   # 没有配置文件 → 新建（无数据可丢）
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # fail-closed：config.json 损坏时绝不用 {} 重写整个文件——那会把 API key
            # 等全部配置清掉。留给用户手工修复，本次设置目录失败。
            logger.error(f"config.json 损坏，拒绝改写（防清空全部配置）: {e}")
            return False
        data.setdefault("rag", {})["kb_dir"] = path
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        import os
        os.replace(tmp, CONFIG_PATH)
        RAG_KB_DIR = path
        logger.info(f"知识库目录已设为: {path}")
        return True
    except Exception as e:
        logger.warning(f"写入 rag.kb_dir 失败: {e}")
        return False

# ── RAG 知识库检索（可选功能）──
# 对一个本地 md 资料库做语义检索。embedding 默认复用千问（DashScope 兼容端点）。
# kb_dir 为空 = 未启用；search_knowledge 工具会提示先配置。
_rag_cfg = _cfg_dict(_config, "rag")


def _cfg_num(cfg: dict, key: str, default, cast=int):
    """安全读数值配置：写错不能让程序起不来，也不能混进毒值。挡四类：
      非数字（top_k: "five"）→ ValueError；
      布尔（chunk_size: true）→ bool 是 int 子类，int(True)=1 会产出海量单字块，显式拒绝；
      Infinity → 转 int 抛 OverflowError；
      NaN/Infinity 过 float cast → isfinite 兜底（作 min_score 会产生异常过滤）。
    注意不用 `or default`：那会把合法的 0 吞掉（chunk_overlap: 0 = 关闭重叠）。"""
    import math
    v = cfg.get(key, default)
    if isinstance(v, bool):
        logger.warning(f"rag.{key}={v!r} 是布尔值不是数字，回退默认 {default}")
        return default
    try:
        r = cast(v)
    except (TypeError, ValueError, OverflowError):
        logger.warning(f"rag.{key}={v!r} 不是有效数字，回退默认 {default}")
        return default
    if not math.isfinite(r):
        logger.warning(f"rag.{key}={v!r} 非有限数值，回退默认 {default}")
        return default
    return r


def _parse_bool(v):
    """严格解析单个布尔配置值；无法识别返回 None。

    config 加载和设置页回填共用这一层，避免运行时把 ``"false"`` 解析成 False，
    设置页却因 ``bool("false")`` 显示为已开启、保存后反写成 true。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
    return None


def _cfg_bool(cfg: dict, key: str, default: bool) -> bool:
    """严格布尔：bool("false") == True 的坑——手改配置写字符串 "false" 会**静默开启付费重排**。
    只认真正的 bool，或明确识别的字符串（true/false/1/0/yes/no/on/off，大小写无关）；
    其余（含乱写）回退默认并告警，绝不把非空字符串当 True。"""
    v = cfg.get(key, default)
    parsed = _parse_bool(v)
    if parsed is not None:
        return parsed
    logger.warning(f"rag.{key}={v!r} 不是有效布尔（true/false），回退默认 {default}")
    return default


RAG_KB_DIR: str = _rag_cfg.get("kb_dir", "")                       # 知识库根目录（放 .md）
RAG_EMBED_MODEL: str = _rag_cfg.get("embed_model", "text-embedding-v3")
RAG_EMBED_BASE_URL: str = _rag_cfg.get("embed_base_url", "") or CLOUD_BASE_URL   # 复用千问兼容端点
RAG_EMBED_API_KEY: str = _rag_cfg.get("embed_api_key", "") or CLOUD_API_KEY      # 复用千问 key
RAG_TOP_K: int = _cfg_num(_rag_cfg, "top_k", 5)                   # 检索返回片段数
RAG_CHUNK_SIZE: int = _cfg_num(_rag_cfg, "chunk_size", 800)       # 每块目标字符数
RAG_CHUNK_OVERLAP: int = _cfg_num(_rag_cfg, "chunk_overlap", 120)  # 0 = 合法（关闭重叠）
RAG_MIN_SCORE: float = _cfg_num(_rag_cfg, "min_score", 0.0, cast=float)  # cosine 下限（0=不过滤）
RAG_RERANK: bool = _cfg_bool(_rag_cfg, "rerank", False)           # 是否两阶段重排（DashScope gte-rerank，付费）
RAG_RERANK_MODEL: str = _rag_cfg.get("rerank_model", "gte-rerank-v2")
RAG_RERANK_TOP_N: int = _cfg_num(_rag_cfg, "rerank_top_n", 20)    # 重排前向量粗召回的候选数
# rerank 走 DashScope 原生端点（非 compatible-mode），复用同一个 key
RAG_RERANK_URL: str = _rag_cfg.get("rerank_url", "") or \
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

# ── RAG 参数清洗：非法值回退默认并告警（防切块数量/API 请求量爆炸、切块除零等）──
if RAG_TOP_K <= 0:
    logger.warning(f"rag.top_k={RAG_TOP_K} 非法（须 >0），回退 5")
    RAG_TOP_K = 5
from .limits import RAG_MIN_CHUNK_SIZE as _RAG_MIN_CHUNK_SIZE
if RAG_CHUNK_SIZE < _RAG_MIN_CHUNK_SIZE:
    # 下限保护：chunk_size 太小（如 1）会把文档切成海量块 → 天量付费 embedding 请求
    logger.warning(f"rag.chunk_size={RAG_CHUNK_SIZE} 过小（须 >={_RAG_MIN_CHUNK_SIZE}），回退 800")
    RAG_CHUNK_SIZE = 800
if not (0 <= RAG_CHUNK_OVERLAP < RAG_CHUNK_SIZE):
    logger.warning(f"rag.chunk_overlap={RAG_CHUNK_OVERLAP} 非法（须 0<=overlap<chunk_size），回退")
    RAG_CHUNK_OVERLAP = max(0, min(120, RAG_CHUNK_SIZE - 1))
if RAG_RERANK_TOP_N <= 0:
    logger.warning(f"rag.rerank_top_n={RAG_RERANK_TOP_N} 非法（须 >0），回退 20")
    RAG_RERANK_TOP_N = 20

# ── agent 主循环轮次上限 ──
# 一轮 = 一次模型调用 + 它请求的工具执行。**0 = 不限**：一直调用到模型自己停或用户点停止。
# 关掉上限意味着模型陷入循环时没有任何自动刹车，token 会一直烧下去——只在明确需要超长
# 自主任务、且你会盯着它时才设 0。负数按 0（不限）处理。
# 放在文件末尾是因为 _cfg_num（负责非法值回退默认）定义在上面的 RAG 段里。
from .limits import AGENT_MAX_ROUNDS_DEFAULT as _AGENT_MAX_ROUNDS_DEFAULT  # noqa: E402
AGENT_MAX_ROUNDS: int = max(0, _cfg_num(_config, "agent_max_rounds", _AGENT_MAX_ROUNDS_DEFAULT))
