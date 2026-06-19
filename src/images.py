"""图片 content block 在不同协议间的格式归一化。

chat_history 里的图片块按"写入时的模型类型"固化（OpenAI 风格 image_url 或
Anthropic 风格 image），跨模型/跨会话再发送会被服务端拒绝。这些函数把
history 副本里的图片块转换/剥离成当前模型期望的形态，原 chat_history 不变。

三个层次：
1. `_normalize_image_blocks_for_current_model`：在两种 image 协议间互转
2. `_strip_images_in_followup_rounds`：Anthropic 协议在 user → tool_use →
   tool_result 多轮里图片会触发 "Multimodal data corrupted"，先剥掉
3. `_strip_images_for_text_only_model`：当前模型不支持视觉时整体剥成文本占位
"""
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage

from . import state
from .models import MODEL_LIST, current_model_supports_vision


def _normalize_image_blocks_for_current_model(history):
    """把 history 中的图片 content block 转成当前模型期望的格式。
    chat_history 里图片块格式按"写入时的模型类型"固化（OpenAI 风格 image_url
    或 Anthropic 风格 image），跨模型 / 跨会话再发送会被服务端拒绝
    （mimo / anthropic 收到 image_url 会报 "Multimodal data corrupted"）。
    返回新 list，原 chat_history 不变。
    """
    if not history:
        return history
    mtype = MODEL_LIST[state.current_model_index][1]
    is_anthropic = mtype in ("anthropic", "mimo")

    def _convert(part):
        if not isinstance(part, dict):
            return part
        ptype = part.get("type")
        if ptype == "image_url" and is_anthropic:
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:") and "," in url:
                head, b64 = url.split(",", 1)
                media_type = head[5:].split(";")[0]  # "image/png"
                return {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                }
            return part
        if ptype == "image" and not is_anthropic:
            src = part.get("source", {})
            if src.get("type") == "base64":
                media_type = src.get("media_type", "image/png")
                b64 = src.get("data", "")
                return {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{b64}"},
                }
            return part
        return part

    new_history = []
    for msg in history:
        c = msg.content
        if isinstance(msg, HumanMessage) and isinstance(c, list):
            new_parts = [_convert(p) for p in c]
            if new_parts != c:
                new_history.append(HumanMessage(content=new_parts))
                continue
        new_history.append(msg)
    return new_history


def _strip_images_in_followup_rounds(history):
    """
    Anthropic 协议（mimo / anthropic）在 "user(图片) → assistant(tool_use) → user(tool_result)"
    的对话组合下会 400 'Multimodal data is corrupted'。

    后续轮次模型只是收尾回复，不需要再处理图片，所以在历史中已经存在 ToolMessage 时
    把图片块替换成文本占位符，避免再次发送。
    """
    if not history:
        return history
    mtype = MODEL_LIST[state.current_model_index][1]
    if mtype not in ("anthropic", "mimo"):
        return history
    if not any(isinstance(m, ToolMessage) for m in history):
        return history

    new_history = []
    for msg in history:
        if msg is None:
            continue
        if isinstance(msg, HumanMessage) and isinstance(msg.content, list):
            stripped = False
            new_parts = []
            for p in msg.content:
                if isinstance(p, dict) and p.get("type") in ("image_url", "image"):
                    stripped = True
                    continue
                new_parts.append(p)
            if stripped:
                if not any(isinstance(p, dict) and p.get("type") == "text" for p in new_parts):
                    new_parts.insert(0, {"type": "text", "text": "[此处之前有图片，已识别]"})
                new_history.append(HumanMessage(content=new_parts))
                continue
        new_history.append(msg)
    return new_history


def _strip_images_for_text_only_model(history):
    """当前模型不支持视觉时，发送前把历史里的图片块替换成文本占位。

    UI/会话文件仍保留原始图片块用于历史重绘；这里只处理发给模型的副本。
    """
    if current_model_supports_vision():
        return history
    if not history:
        return history

    new_history = []
    for msg in history:
        if isinstance(msg, HumanMessage) and isinstance(msg.content, list):
            new_parts = []
            stripped = False
            for part in msg.content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    stripped = True
                    continue
                new_parts.append(part)
            if stripped:
                new_parts.insert(0, {
                    "type": "text",
                    "text": "[用户上传了一张图片；图片内容已在后续识别结果中转写为文本。]",
                })
                text = "\n".join(
                    p.get("text", "")
                    for p in new_parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
                new_history.append(HumanMessage(content=text or "[用户上传了一张图片。]"))
                continue
        new_history.append(msg)
    return new_history


def _strip_reasoning_for_deepseek(history):
    """DeepSeek 严格校验：history 里只要有 reasoning_content 就强制按思考模式检查。
    当前模型若是 DeepSeek，把所有 AIMessage 的 reasoning_content 字段清掉。
    """
    if MODEL_LIST[state.current_model_index][1] != "deepseek":
        return history
    cleaned = []
    for msg in history:
        if isinstance(msg, AIMessage):
            ak = dict(getattr(msg, 'additional_kwargs', {}) or {})
            if 'reasoning_content' in ak:
                ak.pop('reasoning_content', None)
                msg = AIMessage(
                    content=msg.content,
                    tool_calls=msg.tool_calls,
                    additional_kwargs=ak,
                )
        cleaned.append(msg)
    return cleaned
