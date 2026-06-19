"""模型注册表 + LLM 工厂 + 视觉能力判断。

- MODEL_LIST：可选模型清单（显示名 / 类型 / 模型ID / 是否支持思考）
- get_vision_model_index()：返回"用户在设置里选的图片识别模型"的 index（没选返回 -1）
- _create_llm()：按 model_index 创建对应 LangChain ChatXxx 实例
- describe_images_with_vision()：用视觉模型把图片转文本，给非视觉模型使用
- check_ollama()：检测 Ollama 本机服务是否在线
"""
import os
import urllib.request

from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from . import state
from .config import (
    OLLAMA_BASE_URL,
    CLOUD_API_KEY,
    CLOUD_BASE_URL,
    ANTHROPIC_API_KEY,
    GOOGLE_API_KEY,
    MIMO_API_KEY,
    MIMO_BASE_URL,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    CUSTOM_MODELS,
    MODEL_CONTEXT_WINDOWS,
    MIMO_MODELS,
    QWEN_CLOUD_MODELS,
    OLLAMA_MODELS,
    ANTHROPIC_MODELS,
    GEMINI_MODELS,
    DEEPSEEK_MODELS,
)


# 可选模型列表: (显示名, 类型, 模型ID, 支持思考)
# 用户自定义模型（来自 config.json: custom_models）会在加载时追加进来，类型 = "custom"
# 显示名直接用 model_id 本身（Claude Code 例外，固定 "Claude Code"）


def _build_builtin_model_list():
    """从 config 的各 provider model 数组生成 (显示名, type, model_id, supports_thinking)。
    显示名直接用 model_id 本身（Claude Code 例外）。"""
    out = []
    # Claude Code（CLI 模式）——固定一条，底层模型走 claude_code_model（空=CLI 默认）
    out.append(("Claude Code", "claude-code", "claude", False))
    for mid in MIMO_MODELS:
        out.append((mid, "mimo", mid, False))
    for mid in OLLAMA_MODELS:
        out.append((mid, "ollama", mid, True))   # 本地模型默认允许 thinking
    for mid in QWEN_CLOUD_MODELS:
        out.append((mid, "cloud", mid, False))
    for mid in ANTHROPIC_MODELS:
        out.append((mid, "anthropic", mid, False))
    for mid in GEMINI_MODELS:
        out.append((mid, "gemini", mid, False))
    for mid in DEEPSEEK_MODELS:
        out.append((mid, "deepseek", mid, False))
    return out


BUILTIN_MODEL_LIST = _build_builtin_model_list()


def _build_model_list():
    """合成最终的 MODEL_LIST：内置 + 用户自定义。

    自定义条目 4-tuple 跟内置一致 (name, type, model_id, supports_thinking)，
    type 固定 = "custom"。真正的 protocol / base_url / api_key 走 CUSTOM_MODELS
    那个 dict（_create_llm 时按 model_id 反查）。
    """
    base = list(BUILTIN_MODEL_LIST)
    for cm in CUSTOM_MODELS or []:
        try:
            base.append((
                f"⚙ {cm.get('name', cm.get('model_id', '?'))}",
                "custom",
                cm.get("model_id", ""),
                bool(cm.get("supports_thinking", False)),
            ))
        except Exception:
            continue
    return base


MODEL_LIST = _build_model_list()


def _lookup_custom_model(model_id: str):
    """按 model_id 在 CUSTOM_MODELS 里找回完整配置。找不到返回 None。"""
    for cm in CUSTOM_MODELS or []:
        if cm.get("model_id") == model_id:
            return cm
    return None


def _looks_like_placeholder(value: str) -> bool:
    v = str(value or "").strip().lower()
    if not v:
        return True
    return "xxxx" in v or v in {"your-api-key", "your_api_key", "api-key", "sk-"}


def get_model_config_issues(model_index=None):
    """Return user-facing config problems for the selected model."""
    if model_index is None:
        model_index = state.current_model_index
    if model_index < 0 or model_index >= len(MODEL_LIST):
        return ["当前模型索引无效。"]

    name, mtype, model_id, _ = MODEL_LIST[model_index]
    issues = []

    def require_key(label, key):
        if _looks_like_placeholder(key):
            issues.append(f"{name} 需要先在 ⚙ 设置里填 {label}。")

    if mtype == "cloud":
        require_key("qwen_api_key", CLOUD_API_KEY)
    elif mtype == "anthropic":
        require_key("anthropic_api_key", ANTHROPIC_API_KEY)
    elif mtype == "mimo":
        require_key("mimo_api_key", MIMO_API_KEY)
    elif mtype == "gemini":
        require_key("google_api_key", GOOGLE_API_KEY)
    elif mtype == "deepseek":
        require_key("deepseek_api_key", DEEPSEEK_API_KEY)
    elif mtype == "custom":
        cm = _lookup_custom_model(model_id) or {}
        require_key(f"{name} 的 api_key（设置 → 自定义模型）", cm.get("api_key", ""))
        protocol = (cm.get("protocol") or "openai").lower()
        if protocol not in {"openai", "anthropic"}:
            issues.append(f"{name} 的 custom protocol 暂不支持：{protocol}")

    return issues


# 这些模型类型靠 API key 直接可用（新用户只要填 key 就能聊）。
# 排除 ollama（要本机起服务+拉模型）、claude-code（要本机装 claude CLI）——
# 新用户多半没搭这些，不该算作"已有可用模型"。
_KEYED_MODEL_TYPES = {"cloud", "anthropic", "mimo", "gemini", "deepseek", "custom"}


def has_usable_model() -> bool:
    """是否已有至少一个【填好 key 的云模型】可直接用。

    新用户首次上手引导用：全无 → 欢迎态提示去设置填 key。
    只认 key 型云模型；ollama / claude-code 需本机额外搭建，不计入。
    """
    for i, (_, mtype, _, _) in enumerate(MODEL_LIST):
        if mtype in _KEYED_MODEL_TYPES and not get_model_config_issues(i):
            return True
    return False


_LLM_CACHE = {}


def get_vision_model_index():
    """返回图片识别模型的 index。

    语义：图片识别模型 = 用户在设置里通过 vision_model_id 显式选定的那一个。
    找不到对应 model_id（用户没选 / config 里的 id 已删）时返回 -1，
    UI 据此提示用户去设置里挑一个。
    """
    from .config import VISION_MODEL_ID
    if not VISION_MODEL_ID:
        return -1
    for i, (_, mtype, model_id, _) in enumerate(MODEL_LIST):
        # claude-code 是 CLI 模式，不能当图片识别模型——即便用户手填了也忽略
        if model_id == VISION_MODEL_ID and mtype != "claude-code":
            return i
    return -1


def current_model_supports_vision():
    """当前选中的模型是否就是用户选定的图片识别模型。

    是 → 可以直接发图，不走 OCR 桥接；否 → 调 describe_images_with_vision 先识别。
    没配 vision_model_id → 返回 False（让 UI 提示用户去设置里选）。
    """
    from .config import VISION_MODEL_ID
    if not VISION_MODEL_ID:
        return False
    return MODEL_LIST[state.current_model_index][2] == VISION_MODEL_ID


def check_ollama():
    """检测 Ollama 服务是否可用"""
    try:
        urllib.request.urlopen(OLLAMA_BASE_URL, timeout=3)
        return True
    except Exception:
        return False


def _max_tokens_for(mtype, model_id):
    """按模型给安全的输出额度上限(max_tokens)。

    reasoning 模型(MiMo)思考会吃掉输出 token——8192 常常思考还没结束就到顶、根本轮不到
    吐正文(F12 实测 output 顶在 8192、stop_reason=max_tokens),于是 raw_text 为空、被误报成
    "连接被中断"。所以给 reasoning 模型更高额度。但 Haiku 3.5 等模型**输出上限本身就是 8192**,
    超了会 400,故按模型区分,不能一刀切。
    """
    mid = (model_id or "").lower()
    if mtype == "mimo":
        return 16384                      # MiMo 大型 reasoning,思考耗 token,给足余量
    if mtype == "anthropic":
        if "haiku" in mid:
            return 8192                   # Haiku 3.5 输出上限就是 8192,不能超
        return 16384                      # Sonnet 4 等支持更高
    return 8192                           # 其它 / 自定义保守(不知道对方上限)


# ── M3: 按模型上下文窗口设预算（独立查询函数，不改 MODEL_LIST 四元组结构）──

# key = model_id 包含的子串（先匹配先得），value = 上下文窗口 token 数
_DEFAULT_CONTEXT_WINDOWS = {
    "deepseek": 1_048_576,          # DeepSeek-V4 Flash/Pro 默认 1M(2026-04 起官方全线 1M)
    "claude-sonnet-4": 200_000,
    "claude-sonnet-3.7": 200_000,
    "claude-sonnet-3.6": 200_000,
    "claude-sonnet-3.5": 200_000,
    "claude-haiku": 200_000,
    "gemini": 1_048_576,
    "mimo-v2-omni": 131_072,        # V2 omni 窗口存疑,保守(预算有 MAX_HISTORY_BUDGET 上限兜底)
    "mimo": 1_048_576,              # MiMo-V2.5/V2.5-Pro/V2-Pro 实测 1M(Token Plan 托管端点支持)
    "qwen": 131_072,
}
_FALLBACK_CONTEXT_WINDOW = 65_536


def context_window_for(mtype: str, model_id: str) -> int:
    """返回模型的上下文窗口大小（token 数）。

    查找顺序：config.json 的 model_context_windows 按 model_id 显式覆盖（最高优先，方便随时
    纠正内置估值）→ custom_models 里配的 context_window → 内置 _DEFAULT_CONTEXT_WINDOWS 表按
    model_id 子串匹配 → 保守默认值。绝不抛异常。
    """
    try:
        # 0) config.json model_context_windows 按 model_id 显式覆盖（最高优先）
        if model_id in MODEL_CONTEXT_WINDOWS:
            return int(MODEL_CONTEXT_WINDOWS[model_id])
        # 1) 用户在 config.json custom_models 显式覆盖
        for cm in CUSTOM_MODELS:
            if cm.get("model_id") == model_id:
                val = cm.get("context_window")
                if val is not None:
                    return int(val)
        # 2) 内置映射表（子串匹配）
        mid = (model_id or "").lower()
        for key, cwin in _DEFAULT_CONTEXT_WINDOWS.items():
            if key in mid:
                return cwin
    except Exception:
        pass
    return _FALLBACK_CONTEXT_WINDOW


def _create_llm(model_index=None, reasoning=None):
    """根据选择创建 LLM 实例"""
    if model_index is None:
        model_index = state.current_model_index
    if reasoning is None:
        reasoning = state.reasoning_enabled

    name, mtype, model_id, supports_think = MODEL_LIST[model_index]

    # 长超时：深度思考阶段服务端可能数分钟不发 SSE，默认超时容易被中间代理切断
    LONG_TIMEOUT = 1800  # 30 分钟

    if mtype == "ollama":
        kwargs = {"model": model_id, "base_url": OLLAMA_BASE_URL}
        if supports_think and reasoning:
            kwargs["reasoning"] = True
        return ChatOllama(**kwargs)
    elif mtype == "anthropic":
        return ChatAnthropic(  # type: ignore[call-arg]  # langchain_anthropic pydantic 别名,mypy 桩误报 model/max_tokens
            model=model_id,
            api_key=ANTHROPIC_API_KEY,
            max_tokens=_max_tokens_for(mtype, model_id),
            default_request_timeout=LONG_TIMEOUT,
        )
    elif mtype == "mimo":
        return ChatAnthropic(  # type: ignore[call-arg]  # langchain_anthropic pydantic 别名,mypy 桩误报 model/max_tokens
            model=model_id,
            api_key=MIMO_API_KEY,
            base_url=MIMO_BASE_URL,
            max_tokens=_max_tokens_for(mtype, model_id),
            default_request_timeout=LONG_TIMEOUT,
        )
    elif mtype == "gemini":
        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=GOOGLE_API_KEY,
            timeout=LONG_TIMEOUT,
        )
    elif mtype == "deepseek":
        kwargs = {
            "model": model_id,
            "api_key": DEEPSEEK_API_KEY,
            "base_url": DEEPSEEK_BASE_URL,
            "timeout": LONG_TIMEOUT,
        }
        # DeepSeek V4 服务端默认开启思考模式，但 langchain 不能把 reasoning_content
        # 回传到下一轮，会触发 "reasoning_content must be passed back" 400 错。
        # 必须显式禁用思考模式。未来如果灵犀能正确保留 reasoning_content，再支持开启。
        if "v4" in model_id.lower():
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return ChatOpenAI(**kwargs)
    elif mtype == "custom":
        # 用户自定义模型：从 CUSTOM_MODELS 反查完整配置，按 protocol 选 SDK
        cm = _lookup_custom_model(model_id) or {}
        protocol = (cm.get("protocol") or "openai").lower()
        api_key = cm.get("api_key", "")
        base_url = cm.get("base_url", "")
        if protocol == "anthropic":
            return ChatAnthropic(  # type: ignore[call-arg]  # langchain_anthropic pydantic 别名,mypy 桩误报 model/max_tokens
                model=model_id,
                api_key=api_key,
                base_url=base_url or None,
                max_tokens=_max_tokens_for(mtype, model_id),
                default_request_timeout=LONG_TIMEOUT,
            )
        # 默认 OpenAI 兼容协议（适配大多数第三方 API：OpenAI / 月之暗面 /
        # 火山引擎 / 智谱 / 硅基流动 / 自部署 vLLM 等都走这个）
        kwargs = {
            "model": model_id,
            "api_key": api_key,
            "timeout": LONG_TIMEOUT,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    else:
        kwargs = {
            "model": model_id,
            "api_key": CLOUD_API_KEY,
            "base_url": CLOUD_BASE_URL,
            "timeout": LONG_TIMEOUT,
        }
        return ChatOpenAI(**kwargs)


_create_llm_uncached = _create_llm


def _create_llm(model_index=None, reasoning=None):
    """Create or reuse a LangChain LLM instance for the selected model."""
    if model_index is None:
        model_index = state.current_model_index
    if reasoning is None:
        reasoning = state.reasoning_enabled
    _, mtype, model_id, supports_think = MODEL_LIST[model_index]
    effective_reasoning = bool(reasoning and supports_think)
    custom = _lookup_custom_model(model_id) if mtype == "custom" else None
    custom_key = None
    if custom:
        custom_key = (
            custom.get("protocol", ""),
            custom.get("api_key", ""),
            custom.get("base_url", ""),
        )
    key = (model_index, mtype, model_id, effective_reasoning, custom_key)
    if key not in _LLM_CACHE:
        _LLM_CACHE[key] = _create_llm_uncached(model_index=model_index, reasoning=reasoning)
    return _LLM_CACHE[key]


def _image_content_block_for_model(model_index, path, b64):
    """按指定模型协议构造图片 content block。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = "jpeg" if ext in ("jpg", "jpeg") else (ext or "png")
    name, mtype, model_id, _ = MODEL_LIST[model_index]
    # 判断协议：内置 anthropic/mimo 走 anthropic block；custom 看 protocol 字段
    use_anthropic = mtype in ("anthropic", "mimo")
    if mtype == "custom":
        cm = _lookup_custom_model(model_id) or {}
        use_anthropic = (cm.get("protocol") or "openai").lower() == "anthropic"
    if use_anthropic:
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{mime}",
                "data": b64,
            },
        }
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/{mime};base64,{b64}"},
    }


def describe_images_with_vision(user_text, images):
    """用视觉模型把图片转成文本描述，供非视觉强模型继续处理。

    images: [(path, base64), ...]
    返回: (vision_model_name, description)
    """
    vision_idx = get_vision_model_index()
    if vision_idx < 0:
        raise RuntimeError("没有可用的视觉模型（请在设置里选一个图片识别模型）")

    vision_name = MODEL_LIST[vision_idx][0]
    vision_llm = _create_llm(model_index=vision_idx, reasoning=False)

    content = []
    for path, b64 in images:
        content.append(_image_content_block_for_model(vision_idx, path, b64))

    original_question = (user_text or "").strip() or "用户只上传了图片，没有附加文字。"
    content.append({
        "type": "text",
        "text": (
            "你是图片识别/OCR 助手。请把图片内容转换成给另一个更强文本/代码模型使用的中文上下文。\n"
            "要求：\n"
            "1. 客观描述图片里可见的信息，不要脑补。\n"
            "2. 如果是报错、代码、终端、网页、软件界面或设计稿，优先提取所有关键文字、错误信息、路径、行号、按钮、布局和状态。\n"
            "3. 如果图片里有代码，请尽量按原样抄录关键片段。\n"
            "4. 不要直接解决用户问题，不要写最终答案，只输出识别结果。\n\n"
            f"用户原始问题：{original_question}"
        ),
    })

    resp = vision_llm.invoke([HumanMessage(content=content)])
    desc = getattr(resp, "content", str(resp))
    if isinstance(desc, list):
        parts = []
        for part in desc:
            if isinstance(part, dict):
                parts.append(part.get("text", ""))
            else:
                parts.append(str(part))
        desc = "\n".join(p for p in parts if p)
    return vision_name, str(desc).strip()
