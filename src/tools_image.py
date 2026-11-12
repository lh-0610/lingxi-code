"""读图工具：把图片文件交给视觉模型识别，返回文本描述。

为什么返回文本而不是图片块：本项目的工具结果一律是字符串——streaming 把 result
str() 之后打码、展示、装进 ToolMessage。让 ToolMessage 携带多模态图片块要改打码 /
展示 / 历史归一化多处，还要绕开 Anthropic 在 tool_result + 图片 下的 "Multimodal data
corrupted"。这里复用用户上传图片时**非视觉模型**走的同一条 OCR 桥接
（models.describe_images_with_vision）：读图 → 视觉模型转文本 → 当普通字符串结果交回。
对任何当前模型都可用、零 streaming 改动；代价是拿到的是识别文本而非原图。

闸门与 fetch_url / web_search 同类（只读 + 网络外发）：进 Plan 只读白名单、可并行、
不建运行记录；**不进遥控只读白名单**（网络外发默认不给远程），也不进 explore 子代理
工具集（子代理不给网络工具）。
"""
import base64
import os

from langchain_core.tools import tool

from .tools_common import _resolve_path, _subagent_path_rejection

# 只认视觉模型能直接吃的栅格图。SVG 是文本（XML）→ 交给 read_file 当文本读；
# ico 各家支持不一，不收。mime 由 models._image_content_block_for_model 按扩展名推。
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
})
_MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 防把超大文件整份塞进 base64 请求


@tool
def read_image(path: str, question: str = "") -> str:
    """读取一张图片，用视觉模型识别成文字交回（截图里的报错、UI 设计稿、图表、照片中的文字等）。

    参数：
      path: 图片路径（绝对或相对项目根），支持 png/jpg/jpeg/gif/bmp/webp/tiff。
      question: 可选，想让识别侧重什么（如"读出完整的报错信息和行号"）。

    返回的是**视觉模型的识别 / OCR 文本，不是原图**。需要先在「设置 → 图片识别模型」
    选一个能看图的模型；没配会明确提示。读普通文本 / PDF / notebook 用 read_file。
    """
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        if not os.path.isfile(full):
            return f"读图失败: 不是文件或不存在 {full}"
        ext = os.path.splitext(full)[1].lower()
        if ext not in _IMAGE_EXTS:
            return (f"读图失败: 扩展名 {ext or '(无)'} 不是支持的图片格式"
                    f"（{', '.join(sorted(_IMAGE_EXTS))}）。"
                    "SVG 是文本、PDF / notebook 会自动提取文字，都用 read_file。")
        size = os.path.getsize(full)
        if size > _MAX_IMAGE_BYTES:
            return (f"读图失败: 文件太大（{size} 字节，上限 {_MAX_IMAGE_BYTES}），"
                    "不整份塞进识别请求；先裁剪或压缩再读")
        with open(full, "rb") as f:
            data = f.read()
        if not data:
            return f"读图失败: 空文件 {full}"
        b64 = base64.b64encode(data).decode("ascii")

        from .models import describe_images_with_vision, get_vision_model_index
        if get_vision_model_index() < 0:
            return ("读图失败: 没有可用的图片识别模型。请在「设置 → 图片识别模型」里选一个"
                    "能看图的模型（如 GPT-4o / Claude / Gemini / Qwen-VL），再调 read_image。")
        try:
            vision_name, desc = describe_images_with_vision(question, [(full, b64)])
        except Exception as e:
            return f"读图失败: 视觉模型识别出错（{e}）"
        if not desc:
            return f"读图完成: 视觉模型（{vision_name}）没返回可识别的内容 {full}"
        return f"[图片识别结果 · {vision_name} · {os.path.basename(full)}]\n{desc}"
    except Exception as e:
        return f"读图失败: {e}"