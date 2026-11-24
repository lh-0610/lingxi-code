"""Markdown 渲染 + 思考块管理（mixin for ChatUI）。

从 chat_window.py 抽出来的 AI 输出渲染相关方法：

- `render_final_markdown`：agent 线程入口（通过 Signal 走主线程）
- `_render_markdown`：UI 线程槽，把流式纯文本替换成 Markdown HTML
- `_md_to_html`：核心转换，主题色全部 inline 进 HTML（QTextBrowser 不吃 <style>）；
  行内代码里的项目文件路径在这一步换成可点链接
- `_remove_thinking` / `_update_thinking`：思考指示器原地替换

依赖宿主提供：self.bridge / self.chat_area / self._t /
self._inline_svg_img / self._svg_icon /
self._msg_buffers / self._code_blocks
"""
import os
import re
from contextlib import contextmanager

from .fonts import MONO_CSS, UI_CSS

# 代码块（``` / ~~~ 围栏）和行内代码。split 用捕获组，偶数下标是代码以外的文字。
_CODE_SPAN = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_OPEN = "\x01B\x02"     # 控制字符做占位，不会和正常文本冲突
_BOLD_CLOSE = "\x01E\x02"
_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_CODE_MARK = re.compile(r"\x01C(\d+)\x02")    # 行内代码的占位：\x01C<序号>\x02
# 列表项：- / + / 1. / 1)（* 开头的不算：单个 * 在正文里一律是字面量，见 _protect_literal_asterisks）
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-+]|\d{1,3}[.)])\s+\S")

# ── 行内代码里的项目文件路径 → 可点链接 ──────────────────────────
# 自定义 scheme：正文里点了它由 MessageView 转给 ChatUI，在右边文件面板里打开。
# 不用 http 之类的真协议——QLabel 打开外部链接会把浏览器拽起来。
_FILE_LINK_PREFIX = "lingxi-file:"
# `src/ui/task_panel.py` / `src/ui/task_panel.py:835` / `src/ui/task_panel.py:835-860`（只取起始行）
_FILE_LINK_TEXT = re.compile(r"^(?P<path>.+?)(?::(?P<line>\d+)(?:-\d+)?)?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
# 路径的每一段：文字（含中文）、点、横线；空格 / 引号 / 通配符之类都不当路径
_PATH_PART = re.compile(r"[\w.\-]+")
_LINK_TAIL = re.compile(r"^(?P<path>.+?):(?P<line>\d+)$")

# ── 正文里的 localhost 地址 → 可点链接（复审 12：行内代码 + 裸文本）──────
# 点了由 ChatUI._on_file_link_clicked 在浏览器面板打开（§7.4）。路径字符限 ASCII，
# 避免把紧跟的中文标点/文字吞进 URL。
_LOCALHOST_URL = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/[A-Za-z0-9_./\-?=&%#]*)?",
    re.IGNORECASE)
_MD_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")


def _linkify_localhost(md_text: str) -> str:
    """把行内代码里整段的 localhost URL、以及裸文本里的 localhost URL 换成 markdown 链接。

    代码块不动；已是 markdown 链接/autolink 的不重复包。链接文字保留原样（行内代码仍带反引号）。
    """
    if not md_text:
        return md_text
    parts = _CODE_SPAN.split(md_text)
    for i in range(len(parts)):
        seg = parts[i]
        if i % 2 == 1:
            if seg.startswith(("```", "~~~")):
                continue
            m = _LOCALHOST_URL.fullmatch(seg[1:-1].strip())
            if m:
                parts[i] = f"[{seg}]({m.group(0)})"
        else:
            stash = []

            def _st(m):
                stash.append(m.group(0))
                return f"\x01L{len(stash) - 1}\x02"

            seg = _MD_LINK.sub(_st, seg)
            seg = _LOCALHOST_URL.sub(lambda m: f"[{m.group(0)}]({m.group(0)})", seg)
            seg = re.sub(r"\x01L(\d+)\x02", lambda m: stash[int(m.group(1))], seg)
            parts[i] = seg
    return "".join(parts)


# ── 网页链接（设计稿「消息区：回复里的链接」）────────────────────
# 正文里光秃秃的网址：中文、空格、尖括号都不算网址的一部分（中文标点紧跟在后面很常见）
_BARE_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_URL_TRAIL = ".,;:!?'\"]"
_NO_LINK_TAGS = ("a", "code", "pre")
_SHORT_URL_MAX = 36


def _trim_url(url: str):
    """网址末尾粘着的标点不算网址（「见 https://x.com/a。」、括号里的网址）。返回 (网址, 剩下的)。"""
    tail = ""
    while url:
        ent = re.search(r"&(?:[a-z]+|#\d+);$", url)
        if ent:
            # 文本节点里的 & < > 是转义过的：末尾的整个实体（如 &gt;）不算网址
            if ent.group(0) in ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;", "&#39;"):
                tail = ent.group(0) + tail
                url = url[:ent.start()]
                continue
            break
        ch = url[-1]
        if ch in _URL_TRAIL or ch == ";" or (ch == ")" and url.count("(") < url.count(")")):
            tail = ch + tail
            url = url[:-1]
            continue
        break
    return url, tail


def _autolink_bare_urls(html: str) -> str:
    """把 HTML 文本节点里的 http(s) 网址包成 <a>；已经在链接、行内代码、代码块里的不动。"""
    if "http" not in html:
        return html
    depth = dict.fromkeys(_NO_LINK_TAGS, 0)
    out = []
    for part in re.split(r"(<[^>]+>)", html):
        if part.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z]+)", part)
            if m and m.group(2).lower() in depth:
                tag = m.group(2).lower()
                depth[tag] = max(0, depth[tag] + (-1 if m.group(1) else 1))
            out.append(part)
            continue
        if any(depth.values()) or "http" not in part:
            out.append(part)
            continue

        def wrap(m):
            url, tail = _trim_url(m.group(0))
            if len(url) <= len("https://"):
                return m.group(0)
            return f'<a href="{url}">{url}</a>{tail}'
        out.append(_BARE_URL.sub(wrap, part))
    return "".join(out)


def short_url(url: str, limit: int = _SHORT_URL_MAX) -> str:
    """网址的简短显示：去掉协议和 www.，太长截成「域名/路径前段…」。"""
    import html as _html
    from urllib.parse import urlsplit
    raw = _html.unescape(url)
    try:
        p = urlsplit(raw)
    except ValueError:
        return raw
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    rest = p.path.rstrip("/")
    if p.query:
        rest += "?" + p.query
    s = host + rest
    if len(s) > limit:
        s = s[:limit - 1].rstrip("/-_.?&=") + "…"
    return s


def _style_web_link(href: str, inner: str, accent: str, line: str) -> str:
    """一个网页链接的最终样子：标题（模型写的链接文字；就是网址本身时换成简短显示）+ 箭头。"""
    import html as _html
    from ..browser import security
    text = _html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
    raw = _html.unescape(href)
    is_bare = text in (raw, raw.rstrip("/"), raw.split("://", 1)[-1],
                       raw.split("://", 1)[-1].rstrip("/"))
    if is_bare and "<code" not in inner:
        label = _html.escape(short_url(href))
    else:
        # 模型写成行内代码的网址（`http://localhost:5173/`）保留代码样式（复审 12）；
        # 行内代码自带颜色，会盖过链接的强调色：一起改成强调色
        label = re.sub(r'(<code style="[^"]*?)color:[^;"]*', rf"\1color:{accent}", inner)
    arrow = "▸" if security.is_localhost(raw) else "↗"
    return (f'<a href="{href}" style="color:{accent};text-decoration:underline;'
            f'text-decoration-color:{line};">{label}'
            f'<span style="font-size:11px;text-decoration:none;">&#8201;{arrow}</span></a>')


def file_link_href(rel_path: str, line: int = 0) -> str:
    """正文里的文件链接 href：`lingxi-file:src/ui/task_panel.py:835`。"""
    return f"{_FILE_LINK_PREFIX}{rel_path}" + (f":{line}" if line else "")


def parse_file_link(href: str):
    """把正文里的文件链接还原成 (项目相对路径, 行号)；不是文件链接返回 None。

    行号缺省是 0（不带行号的写法）。
    """
    s = (href or "").strip()
    if not s.startswith(_FILE_LINK_PREFIX):
        return None
    rest = s[len(_FILE_LINK_PREFIX):]
    m = _LINK_TAIL.match(rest)
    if m:
        rest, line = m.group("path"), int(m.group("line"))
    else:
        line = 0
    return (rest, line) if rest else None


# 重画历史期间的路径缓存（见 cached_paths）：None = 没在重画，每次都问文件系统
_path_cache = None


@contextmanager
def cached_paths():
    """重画一整段历史时，同一个路径、同一个工作区根只问一次文件系统。

    长会话里同几个文件被反复提到（两段长会话 200 多处行内代码），每处都 realpath + isfile；
    Windows 上 realpath 每次都要打开文件问真实路径。重画是同步的一口气，期间文件不会因为
    渲染而变，缓存只活在这一次重画里；实时回复照常每次都查（刚写出来的文件要能认出来）。"""
    global _path_cache
    outer = _path_cache
    if outer is None:
        _path_cache = {}
    try:
        yield
    finally:
        if outer is None:
            _path_cache = None


def _link_root() -> str:
    """路径按哪个目录认：和文件面板取差异用的是同一个（当前会话的实际工作区，含 worktree）。"""
    cache = _path_cache
    if cache is not None and ("root",) in cache:
        return cache[("root",)]
    try:
        from ..file_history import session_workspace
        root = session_workspace()
    except Exception:
        root = ""
    if cache is not None:
        cache[("root",)] = root
    return root


def _real_root(root: str) -> str:
    """root 解析符号链接后的真实路径（重画期间缓存）；解析失败返回 ""。"""
    cache = _path_cache
    if cache is not None and ("real", root) in cache:
        return cache[("real", root)]
    try:
        real = os.path.realpath(root)
    except (OSError, ValueError):
        real = ""
    if cache is not None:
        cache[("real", root)] = real
    return real


def _file_link_target(text: str, root: str, real_root: str = ""):
    """行内代码里的这段文字指向项目里真实存在的文件时，返回 (相对路径, 行号)，否则 None。

    real_root 是 root 解析过符号链接的结果：一段回复里有几十处行内代码时由调用方算一次传进来
    （realpath 在 Windows 上每次都要问一遍文件系统）。"""
    s = (text or "").strip()
    if not s or len(s) > 260 or any(ch.isspace() for ch in s):
        return None
    m = _FILE_LINK_TEXT.match(s)
    if not m:
        return None
    rel, line = m.group("path"), int(m.group("line") or 0)
    rel = rel.replace("\\", "/")
    # 绝对路径 / 家目录 / 盘符都不是项目内的写法
    if not rel or rel.startswith(("/", "~")) or _DRIVE.match(rel):
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or not all(_PATH_PART.fullmatch(p) for p in parts):
        return None
    if not root:
        return None
    cache = _path_cache
    key = ("file", root, "/".join(parts))
    if cache is not None and key in cache:
        hit = cache[key]
        return (hit, line) if hit else None
    found = _project_file(root, parts, real_root)
    if cache is not None:
        cache[key] = found
    return (found, line) if found else None


def _project_file(root: str, parts: list, real_root: str = "") -> str:
    """parts 拼成的相对路径是 root 里真实存在的文件（解析符号链接后仍在 root 里）时返回
    「a/b.py」，否则 ""。"""
    try:
        real_root = real_root or _real_root(root)
        if not real_root:
            return ""
        full = os.path.realpath(os.path.join(real_root, *parts))
        if os.path.commonpath([full, real_root]) != real_root:
            return ""
    except (OSError, ValueError):
        return ""
    if not os.path.isfile(full):
        return ""
    return "/".join(parts)


def _linkify_file_paths(md_text: str, root: str) -> str:
    """把行内代码里的项目文件路径换成 markdown 链接；代码块里的原样不动。

    只认项目里真实存在的文件：写错的路径、目录名、命令片段都保持原样，免得把普通代码
    当成链接。链接文字仍包在反引号里，看上去还是一段代码，点了在文件面板里打开。
    """
    if not md_text or not root:
        return md_text
    parts = _CODE_SPAN.split(md_text)
    real_root = ""
    for i in range(1, len(parts), 2):
        span = parts[i]
        if span.startswith(("```", "~~~")):
            continue                        # 代码块里的路径不处理
        if not real_root:
            real_root = _real_root(root)
            if not real_root:
                return md_text
        target = _file_link_target(span[1:-1], root, real_root)
        if target is None:
            continue
        parts[i] = f"[{span}]({file_link_href(*target)})"
    return "".join(parts)


def _protect_literal_asterisks(md_text):
    """正文里的单个 * 当字面量（动作描写不渲染成斜体），**加粗** 照常生效；
    代码块和行内代码里的 * 原样不动。

    以前对整段文本做替换，代码里的 `p, *rest = xs` 会显示成 `p, \\*rest = xs`。

    行内代码先换成不含 * 的占位再找加粗：以前按代码切段后逐段找，`**` 包着行内代码时
    （**`src/x.py`**，DeepSeek 常这么写）开头和结尾的 `**` 落在两段里配不上对，
    就被当成字面星号原样露出来。代码块仍按段隔开，不让加粗跨过整块代码去配对。
    """
    chunks = _FENCE.split(md_text)
    for i in range(0, len(chunks), 2):      # 奇数位是代码块，原样不动
        codes = []

        def _stash(m):
            codes.append(m.group(0))
            return f"\x01C{len(codes) - 1}\x02"

        seg = _INLINE_CODE.sub(_stash, chunks[i])
        seg = _BOLD.sub(lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}", seg)
        seg = seg.replace("*", r"\*").replace(_BOLD_OPEN, "**").replace(_BOLD_CLOSE, "**")
        chunks[i] = _CODE_MARK.sub(lambda m: codes[int(m.group(1))], seg)
    return "".join(chunks)


_LIST_TAG = re.compile(r"<(/?)(ul|ol|li)\b[^>]*>")


def _flatten_lists(html, marker_color=""):
    """把 <ul>/<ol>/<li> 换成两列无框表格：左列是 "•" 或 "1."，右列是内容（悬挂缩进）。

    QLabel 自带的列表缩进不好看；以前摊平成「• 文字」的段落，一项折行后第二行顶到符号
    底下，和文字对不齐（设计稿「消息流整理」④）。表格的右列整列缩进，折行自然对齐。
    嵌套列表落在外层那项的右列里，再往里缩一层。更早时直接删标签，符号和序号都没了。
    """
    # 松散列表（项之间有空行）的 <li> 里还会套一层 <p>：拆掉，不然符号单独占一行
    html = re.sub(r"<li>\s*<p>", "<li>", html)
    html = re.sub(r"</p>\s*</li>", "</li>", html)
    color = f"color:{marker_color};" if marker_color else ""
    out, pos, stack = [], 0, []      # stack 里每层是 [列表类型, 已有几项]
    for m in _LIST_TAG.finditer(html):
        out.append(html[pos:m.start()])
        pos = m.end()
        closing, tag = m.group(1) == "/", m.group(2)
        if tag in ("ul", "ol"):
            if closing:
                if stack:
                    stack.pop()
                out.append("</table>")
            else:
                # 外层列表上方留 6：Qt 里列表前那段的下边距和它的上边距取大的那个，不相加
                gap = "2px 0 0 0" if stack else "6px 0 8px 0"
                out.append(f'<table cellspacing="0" cellpadding="0" style="margin:{gap};">')
                stack.append([tag, 0])
        elif closing:
            out.append("</td></tr>")
        else:
            kind, n = stack[-1] if stack else ("ul", 0)
            if stack:
                stack[-1][1] += 1
            marker, width = (f"{n + 1}.", 22) if kind == "ol" else ("•", 16)
            out.append(f'<tr><td width="{width}" valign="top" style="{color}padding:0 0 3px 0;">'
                       f'{marker}</td><td valign="top" style="padding:0 0 3px 0;">')
    out.append(html[pos:])
    return "".join(out)


def _blank_line_before_lists(md_text):
    """列表紧跟在一行文字下面（中间没空行）时补一个空行，否则 markdown 不认它是列表。

    DeepSeek 常写"……如下：\\n- 第一项\\n- 第二项"，原样渲染出来每行开头都是一个 "- "。
    前一行本身是列表项、或是缩进的续行时不补（那是同一个列表）；代码块里不动。
    """
    chunks = _FENCE.split(md_text)
    for i in range(0, len(chunks), 2):
        out, prev = [], ""
        for line in chunks[i].split("\n"):
            if (_LIST_ITEM.match(line) and prev.strip() and not _LIST_ITEM.match(prev)
                    and not prev.startswith((" ", "\t"))):
                out.append("")
            out.append(line)
            prev = line
        chunks[i] = "\n".join(out)
    return "".join(chunks)


class MarkdownRenderMixin:
    """AI 回复 Markdown 渲染 + 思考块原地替换的全部逻辑。"""

    def render_final_markdown(self, md_text, speak=True):
        """通知 UI 渲染最终 Markdown（从 agent 线程调用）。

        speak 参数已废弃（语音模块移除），保留签名以兼容调用方。
        """
        # 记本轮渲染事件供"切走→切回"重放；后台会话不实时渲染（切回时统一重放）
        from .. import session as _session
        _sess = _session.current_session()
        with _sess.render_lock:
            _sess.render_log.append(("md", md_text))
        if _sess is not _session.get_active():
            _sess.needs_redraw = True
            return
        self.bridge.render_md.emit(md_text)

    def _md_to_html(self, md_text):
        """Markdown 转带内联样式的 HTML（QTextBrowser 不支持 <style> 标签）"""
        import markdown
        import re as _re

        # 注意：不要做 `md_text.replace('\n\n', '\n&nbsp;\n')` 这种"保留空行"的 hack——
        # 它会删掉所有空行，而 markdown 靠空行分隔块（表格靠空行结束、标题/列表靠空行分隔）。
        # 空行没了，整篇会被当成一个块：表格后面的标题 / 列表会被表格扩展整行吞成单元格。
        # 段落间距交给下面的 <p style="margin:6px 0"> 处理即可。

        md_text = _blank_line_before_lists(md_text)
        md_text = _protect_literal_asterisks(md_text)
        # 行内代码里的项目文件路径 → 可点链接（代码块里的不处理，只认真实存在的文件）
        md_text = _linkify_file_paths(md_text, _link_root())
        # 行内代码/裸文本里的 localhost 地址 → 可点链接（点了在浏览器面板打开）
        md_text = _linkify_localhost(md_text)

        html = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'nl2br'])

        # 正文里光秃秃的网址变成链接（以前只认 localhost，公网地址点不了）；代码里的不动
        html = _autolink_bare_urls(html)
        # 列表摊平成段落（QLabel 自带的列表缩进不好看），但要留下项目符号 / 序号
        html = _flatten_lists(html, self._t("mv_muted"))
        # 去掉 <em>/<i> 斜体标签，避免动作描述*...* 与正常对话字体不一样
        html = _re.sub(r'</?em[^>]*>', '', html)
        html = _re.sub(r'</?i[^>]*>', '', html)
        # 段落只留下边距：上下各 6 时，段落、标题、列表挨在一起的空隙忽大忽小
        html = html.replace('<p>', '<p style="margin:0 0 8px 0;">')
        # 代码块：markdown 输出的是 <pre><code class="language-python">，带 class 的标签下面按 '<code>'
        # 精确替换匹配不到，Qt 就给它用默认的 Courier New（又细又宽，括号前后像多了空格）。
        # 先把块里的 code 换成只带字体的样式；剩下不带属性的 <code> 才是行内代码。
        html = _re.sub(r'<pre><code[^>]*>',
                       f'<pre><code style="font-family:{MONO_CSS};font-size:13px;">', html)
        # 行内代码：正文色 + 很浅的底（路径、编号多的回答不再像一排按钮）
        html = html.replace(
            '<code>',
            f'<code style="background:{self._t("md_code_bg")};color:{self._t("md_code_text")};'
            f'padding:0 4px;font-family:{MONO_CSS};font-size:13px;border-radius:3px;">'
        )
        pre_border = self._t("md_pre_border_left")
        pre_border_css = (
            f'border-left:2px solid {pre_border};' if pre_border != "transparent" else ''
        )
        html = html.replace(
            '<pre>',
            f'<pre style="background:{self._t("md_pre_bg")};color:{self._t("md_pre_text")};'
            f'padding:14px 16px;font-family:{MONO_CSS};font-size:13px;'
            f'white-space:pre-wrap;{pre_border_css}border-radius:6px;">'
        )

        # ---- #5 Code block copy buttons ----
        copy_bg = self._t("md_copy_btn_bg")
        copy_text = self._t("md_copy_btn_text")
        copy_border = self._t("md_copy_btn_border")
        copy_border_css = (
            f'border:1px solid {copy_border};' if copy_border != "transparent" else 'border:none;'
        )
        def _add_copy_btn(match):
            block = match.group(0)
            code_match = _re.search(r'<code[^>]*>(.*?)</code>', block, _re.DOTALL)
            raw_code = code_match.group(1) if code_match else block
            raw_code = (raw_code.replace('&amp;', '&').replace('&lt;', '<')
                        .replace('&gt;', '>').replace('&nbsp;', ' ').replace('&quot;', '"'))
            idx = len(self._code_blocks)
            self._code_blocks[str(idx)] = raw_code
            copy_icon = self._inline_svg_img("copy_lucide.svg", copy_text, 14, "Copy")
            return (
                f'<div style="position:relative;">'
                f'<a href="action:copy_code:{idx}" '
                f'style="position:absolute;top:4px;right:4px;z-index:1;'
                f'background:{copy_bg};color:{copy_text};font-size:13px;padding:3px 8px;'
                f'{copy_border_css}border-radius:6px;text-decoration:none;" title="复制代码">{copy_icon}</a>'
                f'{block}</div>'
            )
        html = _re.sub(r'<pre[^>]*>.*?</pre>', _add_copy_btn, html, flags=_re.DOTALL)

        # 正文里的文件链接（markdown 渲染时由 _linkify_file_paths 生成）：去掉默认下划线，
        # 用强调色——和普通行内代码一个颜色的话，看不出哪些能点。里面那层 <code> 自带
        # color，会盖过 <a> 的颜色，所以连它一起改
        accent = self._t("mv_indigo")
        html = _re.sub(
            r'<a href="(lingxi-file:[^"]*)">(<code style="[^"]*?)color:[^;"]*',
            lambda m: (
                f'<a href="{m.group(1)}" title="在右侧文件面板里打开" '
                f'style="color:{accent};text-decoration:none;">{m.group(2)}color:{accent}'
            ),
            html,
        )
        # 网页链接（设计稿「消息区：回复里的链接」）：强调色 + 浅一档的下划线 + 箭头。
        # 公网 ↗（点了按设置用系统浏览器 / 侧栏浏览器开），本机 ▸（总在侧栏浏览器开）。
        # 光秃秃的网址显示成「域名/路径前段…」，不再是一整串。Qt 富文本画不了虚线下划线
        # （text-underline-style 不认，实测），用 text-decoration-color 把下划线调浅
        line = self._t("mv_panel_accent")
        html = _re.sub(
            r'<a href="(https?://[^"]*)"[^>]*>(.*?)</a>',
            lambda m: _style_web_link(m.group(1), m.group(2), accent, line),
            html, flags=_re.DOTALL,
        )
        # 其余链接（mailto 之类）：强调色、不加下划线——Qt 默认的纯蓝下划线在深色底上几乎看不清
        html = _re.sub(
            r'<a href="((?!lingxi-file:|action:|https?://)[^"]*)">',
            lambda m: f'<a href="{m.group(1)}" style="color:{accent};text-decoration:none;">',
            html,
        )

        html = html.replace(
            '<table>',
            f'<table style="border-collapse:collapse;margin:8px 0;border:1px solid {self._t("md_table_border")};"'
            f' cellpadding="6" cellspacing="0">'
        )
        html = html.replace(
            '<th>',
            f'<th style="background:{self._t("md_th_bg")};color:{self._t("md_th_text")};'
            f'padding:6px 12px;border:1px solid {self._t("md_table_border")};font-weight:600;">'
        )
        html = html.replace(
            '<td>',
            f'<td style="padding:6px 12px;border:1px solid {self._t("md_table_border")};'
            f'color:{self._t("md_td_text")};">'
        )
        bq_bg = self._t("md_blockquote_bg")
        bq_bg_css = f'background:{bq_bg};' if bq_bg != "transparent" else ''
        html = html.replace(
            '<blockquote>',
            f'<blockquote style="border-left:3px solid {self._t("md_blockquote_border")};margin:6px 0;'
            f'padding:4px 14px;color:{self._t("md_blockquote_text")};{bq_bg_css}">'
        )
        # 标题：和上一段拉开、和下面的内容贴近；字号压到正文附近。换成带样式的段落——
        # Qt 给 <hN> 套自己的字号级别（h1 是 xx-large），style 里写的 font-size 盖不过它
        for i, size in enumerate((18, 16, 15, 14, 14, 14), start=1):
            html = _re.sub(
                rf'<h{i}(?:\s[^>]*)?>(.*?)</h{i}>',
                lambda m, s=size: (
                    f'<p style="margin:18px 0 6px 0;font-size:{s}px;font-weight:600;'
                    f'color:{self._t("md_h_color")};">{m.group(1)}</p>'),
                html, flags=_re.DOTALL,
            )
        # 回复以标题开头：上方已经有模型那行的间距，不再多空 18
        html = _re.sub(r'^\s*<p style="margin:18px 0 6px 0;', '<p style="margin:0 0 6px 0;', html)

        return (
            f'<div style="color:{self._t("md_text")};font-size:14px;'
            f'font-family:{UI_CSS};line-height:1.7;">'
            f'{html}</div>'
        )

    def _render_markdown(self, md_text):
        """用 Markdown 渲染结果替换纯文本 AI 回复（MessageView：定格当前正文块为富文本）"""
        from PySide6.QtWidgets import QApplication
        styled_html = self._md_to_html(md_text)
        self._msg_buffers[str(len(self._msg_buffers))] = md_text
        self.chat_area.finalize_markdown(styled_html, md_text)
        self.chat_area.add_message_actions(
            on_copy=lambda t=md_text: (QApplication.clipboard().setText(t), self._show_toast("已复制")),
            on_regen=self._on_retry,
        )

    def _remove_thinking(self):
        """移除等待指示器（MessageView 的 WaitingIndicator）。"""
        self.chat_area.remove_waiting()

    def _update_thinking(self, text):
        """更新等待指示器的阶段——秒数 WaitingIndicator 自己走，这里只换标签、收放排队提示。"""
        self.chat_area.update_waiting(text)
