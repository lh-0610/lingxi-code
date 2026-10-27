"""UI 杂项工具：图标生成、图片协议块构造、HTML 转义。

这些都是 UI 模块内部的无状态 helper，与 ChatUI 解耦，可以被任何 ui/ 子模块复用。
"""

import re

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygon,
)

from .. import agent


def _build_image_content_block(ext, b64):
    """根据当前模型类型，构造正确格式的图片内容块
    - Anthropic 协议 (mimo, anthropic): {"type": "image", "source": {...}}
    - OpenAI 协议 (cloud, ollama, etc): {"type": "image_url", "image_url": {...}}
    """
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "gif": "gif", "bmp": "bmp", "webp": "webp"}.get(ext, "png")
    mtype = agent.MODEL_LIST[agent.current_model_index][1]
    if mtype in ("anthropic", "mimo"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{mime}",
                "data": b64,
            }
        }
    else:
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{mime};base64,{b64}"}
        }


def _make_button_icon(arrow=True):
    """程序化绘制发送上箭头/停止(暂停)图标，白色，透明背景"""
    size = 30
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ffffff"))
    if arrow:
        # 上箭头：三角 + 矩形
        tri = QPolygon([QPoint(15, 4), QPoint(6, 15), QPoint(24, 15)])
        p.drawPolygon(tri)
        p.drawRect(12, 14, 6, 12)
    else:
        # 暂停：两个竖
        p.drawRoundedRect(6, 5, 6, 20, 2, 2)
        p.drawRoundedRect(18, 5, 6, 20, 2, 2)
    p.end()
    return QIcon(px)


def _make_upload_icon(color="#888888"):
    """绘制上传文件图标（文档轮廓+上传箭头），单色，可适配主题"""
    size = 30
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)

    # 文档轮廓（带右上折角）
    p.setPen(QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    doc = QPainterPath()
    doc.moveTo(8, 4)
    doc.lineTo(18, 4)
    doc.lineTo(22, 8)
    doc.lineTo(22, 26)
    doc.lineTo(8, 26)
    doc.lineTo(8, 4)
    p.drawPath(doc)
    # 折角小三角
    fold = QPainterPath()
    fold.moveTo(18, 4)
    fold.lineTo(18, 8)
    fold.lineTo(22, 8)
    p.drawPath(fold)

    # 上传箭头（竖线 + V形）
    p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawLine(QPoint(15, 24), QPoint(15, 15))
    p.drawLine(QPoint(11, 18), QPoint(15, 14))
    p.drawLine(QPoint(19, 18), QPoint(15, 14))

    p.end()
    return QIcon(px)


def _escape(text):
    """HTML 转义"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
            .replace(" ", "&nbsp;"))


# 界面不显示 emoji（用户偏好）。✓ ✗ ✕ ☰ 这类单色文字符号不算，照常保留。
# 后端文字里的状态 emoji（⚠️ ✅ ❌ …）同时也是发给模型的内容，不在源头删，只在显示时去掉。
_EMOJI = ("(?![✓✗✕☰])[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
          "\u23E9-\u23FA\u231A\u231B\uFE0F\u200D]")
_EMOJI_RUN = re.compile(f"(?:{_EMOJI})+ ?")
_LEADING_EMOJI = re.compile(f"(?m)^([ \\t]*)(?:{_EMOJI})+ ?")


def strip_emoji(text):
    """去掉文本里所有 emoji（连同紧跟的一个空格）。用于界面自己的短提示，如 toast。"""
    return _EMOJI_RUN.sub("", text or "")


def strip_leading_emoji(text):
    """只去掉每行行首的 emoji 状态图标。用于显示后端/工具输出：
    「⚠️ 保存失败」→「保存失败」，而行中间的内容（比如文件里本来就有的 emoji）原样显示。"""
    return _LEADING_EMOJI.sub(r"\1", text or "")
