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

from .fonts import MONO_CSS, UI_CSS

# 代码块（``` / ~~~ 围栏）和行内代码。split 用捕获组，偶数下标是代码以外的文字。
_CODE_SPAN = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_OPEN = "\x01B\x02"     # 控制字符做占位，不会和正常文本冲突
_BOLD_CLOSE = "\x01E\x02"

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


def _link_root() -> str:
    """路径按哪个目录认：和文件面板取差异用的是同一个（当前会话的实际工作区，含 worktree）。"""
    try:
        from ..file_history import session_workspace
        return session_workspace()
    except Exception:
        return ""


def _file_link_target(text: str, root: str):
    """行内代码里的这段文字指向项目里真实存在的文件时，返回 (相对路径, 行号)，否则 None。"""
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
    try:
        real_root = os.path.realpath(root)
        full = os.path.realpath(os.path.join(real_root, *parts))
        if os.path.commonpath([full, real_root]) != real_root:
            return None
    except (OSError, ValueError):
        return None
    if not os.path.isfile(full):
        return None
    return "/".join(parts), line


def _linkify_file_paths(md_text: str, root: str) -> str:
    """把行内代码里的项目文件路径换成 markdown 链接；代码块里的原样不动。

    只认项目里真实存在的文件：写错的路径、目录名、命令片段都保持原样，免得把普通代码
    当成链接。链接文字仍包在反引号里，看上去还是一段代码，点了在文件面板里打开。
    """
    if not md_text or not root:
        return md_text
    parts = _CODE_SPAN.split(md_text)
    for i in range(1, len(parts), 2):
        span = parts[i]
        if span.startswith(("```", "~~~")):
            continue                        # 代码块里的路径不处理
        target = _file_link_target(span[1:-1], root)
        if target is None:
            continue
        parts[i] = f"[{span}]({file_link_href(*target)})"
    return "".join(parts)


def _protect_literal_asterisks(md_text):
    """正文里的单个 * 当字面量（动作描写不渲染成斜体），**加粗** 照常生效；
    代码块和行内代码里的 * 原样不动。

    以前对整段文本做替换，代码里的 `p, *rest = xs` 会显示成 `p, \\*rest = xs`。
    """
    parts = _CODE_SPAN.split(md_text)
    for i in range(0, len(parts), 2):
        seg = _BOLD.sub(lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}", parts[i])
        seg = seg.replace("*", r"\*")
        parts[i] = seg.replace(_BOLD_OPEN, "**").replace(_BOLD_CLOSE, "**")
    return "".join(parts)


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

        md_text = _protect_literal_asterisks(md_text)
        # 行内代码里的项目文件路径 → 可点链接（代码块里的不处理，只认真实存在的文件）
        md_text = _linkify_file_paths(md_text, _link_root())

        html = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'nl2br'])

        # 去掉 <ul>/<ol>/<li> 列表标签，保留纯文本换行
        html = _re.sub(r'</?ul[^>]*>', '', html)
        html = _re.sub(r'</?ol[^>]*>', '', html)
        html = _re.sub(r'<li[^>]*>', '<p style="margin:2px 0;">', html)
        html = html.replace('</li>', '</p>')
        # 去掉 <em>/<i> 斜体标签，避免动作描述*...* 与正常对话字体不一样
        html = _re.sub(r'</?em[^>]*>', '', html)
        html = _re.sub(r'</?i[^>]*>', '', html)
        html = html.replace('<p>', '<p style="margin:6px 0;">')
        # 代码块：markdown 输出的是 <pre><code class="language-python">，带 class 的标签下面按 '<code>'
        # 精确替换匹配不到，Qt 就给它用默认的 Courier New（又细又宽，括号前后像多了空格）。
        # 先把块里的 code 换成只带字体的样式；剩下不带属性的 <code> 才是行内代码。
        html = _re.sub(r'<pre><code[^>]*>',
                       f'<pre><code style="font-family:{MONO_CSS};font-size:13px;">', html)
        html = html.replace(
            '<code>',
            f'<code style="background:{self._t("md_code_bg")};color:{self._t("md_code_text")};'
            f'padding:1px 6px;font-family:{MONO_CSS};font-size:13px;border-radius:3px;">'
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
        # 标题
        for i in range(1, 4):
            html = html.replace(
                f'<h{i}>', f'<h{i} style="margin:14px 0 6px 0;color:{self._t("md_h_color")};">'
            )

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
        self.chat_area.finalize_markdown(styled_html)
        self.chat_area.add_message_actions(
            on_copy=lambda t=md_text: (QApplication.clipboard().setText(t), self._show_toast("已复制")),
            on_regen=self._on_retry,
        )

    def _remove_thinking(self):
        """移除等待指示器（MessageView 的 WaitingIndicator）。"""
        self.chat_area.remove_waiting()

    def _update_thinking(self, text):
        """更新等待指示器——WaitingIndicator 自带秒表自走,这里无需原地刷,no-op。"""
        return
