"""内容块(content blocks)工具：统一识别不同协议的 block 类型，一处定义、多处复用。

不同上游把"正文"和"思考"放进不同的 block type：
  - 正文：Anthropic / OpenAI 兼容 → ``'text'``；OpenAI Responses(responses/v1) → ``'output_text'``。
  - 思考：Anthropic → ``'thinking'``(文本在 ``block['thinking']``)；
          Responses → ``'reasoning'``(思考文本在 ``summary`` 列表的 ``'summary_text'`` 项里；
          整块还带 ``id``——工具调用回合里必须把它回传给服务端，否则缺 reasoning item 会 400)。

只认 ``text``/``thinking`` 的话，Responses 的 ``output_text``/``reasoning`` 会被静默丢弃：
展示空白、Debug 无思考、历史存空、下一轮回传丢上下文。所以集中在这里判定，展示 /
重绘 / 持久化 / Debug 全部复用同一套判据，避免漏改某处又把 Responses 内容吞掉。
"""

TEXT_BLOCK_TYPES = ("text", "output_text")
THINK_BLOCK_TYPES = ("thinking", "reasoning")


def is_text_block(block) -> bool:
    """是否正文块(Anthropic ``text`` / Responses ``output_text``)。"""
    return isinstance(block, dict) and block.get("type") in TEXT_BLOCK_TYPES


def is_think_block(block) -> bool:
    """是否思考块(Anthropic ``thinking`` / Responses ``reasoning``)。"""
    return isinstance(block, dict) and block.get("type") in THINK_BLOCK_TYPES


def block_text(block) -> str:
    """正文块的文本(``text`` 与 ``output_text`` 都放在 ``'text'`` 键)；非正文块 → ``''``。"""
    return (block.get("text", "") or "") if is_text_block(block) else ""


def block_thinking(block) -> str:
    """思考块的文本：Anthropic 取 ``thinking`` 键；Responses ``reasoning`` 拼接 summary_text。"""
    if not isinstance(block, dict):
        return ""
    bt = block.get("type")
    if bt == "thinking":
        return block.get("thinking", "") or ""
    if bt == "reasoning":
        parts = [s.get("text", "") for s in (block.get("summary") or [])
                 if isinstance(s, dict) and s.get("type") == "summary_text"]
        return "".join(parts) or (block.get("reasoning") or "")
    return ""


def content_text(content) -> str:
    """从 message content(``str`` 或 block 列表)提取纯正文文本(标题 / 预览 / 重绘用)。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            else:
                t = block_text(b)
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""
