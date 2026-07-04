"""Markdown 切块：按标题分层 → 再按大小切（带 overlap），保留标题路径。

- 标题路径（"文档标题 › 二级 › 三级"）随每块一起存，检索时能显示出处、也能拼进
  embedding 文本提升召回。
- 代码围栏 ``` 内的 `#` 不当作标题，避免把代码注释误切。
"""
import re

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def chunk_markdown(text: str, source: str, chunk_size: int = 800, overlap: int = 120) -> list[dict]:
    """把一篇 markdown 切成若干块。返回 [{text, heading, source, chunk_id}, ...]。

    text: 文件内容；source: 用于引用的相对路径/名字。
    """
    return list(iter_markdown_chunks(text, source, chunk_size, overlap))


def iter_markdown_chunks(text: str, source: str, chunk_size: int = 800, overlap: int = 120):
    """惰性产出 Markdown 切片。

    对外 ``chunk_markdown`` 仍返回 list 保持兼容；索引器使用本迭代器，可在达到总切片
    上限时立即中止，不必先为单个超长文档构造完整的百万级切片列表。
    """
    lines = text.splitlines()
    heading_stack: list[tuple[int, str]] = []   # [(level, title), ...]
    sections: list[tuple[str, str]] = []         # [(heading_path, body), ...]
    buf: list[str] = []
    in_fence = False

    def _flush():
        body = "\n".join(buf).strip()
        if body:
            hp = " › ".join(t for _, t in heading_stack)
            sections.append((hp, body))

    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            buf.append(ln)
            continue
        m = None if in_fence else _HEADER_RE.match(ln)
        if m:
            _flush()
            buf = []
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        else:
            buf.append(ln)
    _flush()

    cid = 0
    for hp, body in sections:
        for piece in _iter_split_by_size(body, chunk_size, overlap):
            yield {"text": piece, "heading": hp, "source": source, "chunk_id": cid}
            cid += 1


def _split_by_size(body: str, size: int, overlap: int) -> list[str]:
    """把一段正文切成 <=size 的块，且**任意相邻块都保留 overlap 字符的上下文**。

    先按空行分段：短段打包、超长段滑窗硬切。关键点——每关闭一个块，就用它尾部
    overlap 个字符作为下一个块的开头（不只在硬切超长段时才有重叠），这样召回时
    命中块边界的信息不会被截断。overlap<=0 时退化为无重叠切分。
    """
    return list(_iter_split_by_size(body, size, overlap))


def _iter_split_by_size(body: str, size: int, overlap: int):
    """``_split_by_size`` 的惰性实现；语义保持完全一致。"""
    if len(body) <= size:
        yield body
        return
    overlap = max(0, min(overlap, size - 1))   # 兜底：0 <= overlap < size
    step = max(1, size - overlap)

    def _tail(s: str) -> str:
        return s[-overlap:] if (overlap and s) else ""

    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > size:
            # cur 满了：冲出去，新块以上一块的尾巴续写 → 相邻块重叠
            yield cur
            seed = _tail(cur)
            cur = (seed + "\n\n" + p) if seed else p
        else:
            cur = (cur + "\n\n" + p) if cur else p
        # cur 若超长（单段很长，或接续后超长）→ 滑窗硬切，步进 step 保留 overlap 尾巴
        while len(cur) > size:
            yield cur[:size]
            cur = cur[step:]
    if cur:
        yield cur
