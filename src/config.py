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

# 各 provider 的可选 model_id 列表（用户在设置里编辑，重启后生效）
MIMO_MODELS       = _config.get("mimo_models", ["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni"])
QWEN_CLOUD_MODELS = _config.get("qwen_cloud_models", ["qwen3.5-plus", "qwen-max", "qwen-plus", "qwen-turbo"])
OLLAMA_MODELS     = _config.get("ollama_models", ["qwen3.5:latest"])
ANTHROPIC_MODELS  = _config.get("anthropic_models", ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"])
GEMINI_MODELS     = _config.get("gemini_models", [])
DEEPSEEK_MODELS   = _config.get("deepseek_models", ["deepseek-v4-flash", "deepseek-v4-pro"])
CLAUDE_CODE_MODEL = _config.get("claude_code_model", "")
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

# 通知（Telegram 推送）
_notify_cfg = _config.get("notify", {}) or {}
NOTIFY_ENABLED: bool = _notify_cfg.get("enabled", False)
NOTIFY_LEVELS: list = _notify_cfg.get("levels", ["error", "action_needed", "done"])
NOTIFY_THROTTLE_SECONDS: int = _notify_cfg.get("throttle_seconds", 10)
TELEGRAM_BOT_TOKEN: str = _notify_cfg.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID: str = _notify_cfg.get("telegram_chat_id", "")

# 遥控（Telegram 远程发送消息给桌面端）
_remote_cfg = _config.get("remote_control", {}) or {}
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

# MCP Servers 配置（字典，key=server 名，value=启动参数）
MCP_SERVERS: dict = _config.get("mcp_servers", {}) or {}
