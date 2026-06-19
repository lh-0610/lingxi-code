"""Markdown 渲染 + 思考块管理（mixin for ChatUI）。

从 chat_window.py 抽出来的 AI 输出渲染相关方法：

- `render_final_markdown`：agent 线程入口（通过 Signal 走主线程）
- `_render_markdown`：UI 线程槽，把流式纯文本替换成 Markdown HTML
- `_md_to_html`：核心转换，主题色全部 inline 进 HTML（QTextBrowser 不吃 <style>）
- `_remove_thinking` / `_update_thinking`：思考指示器原地替换

依赖宿主提供：self.bridge / self.chat_area / self._t /
self._inline_svg_img / self._svg_icon /
self._msg_buffers / self._code_blocks
"""


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

        # 保留 *xxx* 字面量（动作描写不渲染成斜体），但保留 **xxx** 加粗：
        # 1) 先用占位符暂存 **...** 加粗（用控制字符避免与正常文本冲突）
        _BOLD_OPEN = '\x01B\x02'
        _BOLD_CLOSE = '\x01E\x02'
        md_text = _re.sub(r'\*\*([^*]+)\*\*', lambda m: f'{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}', md_text)
        # 2) 转义剩余单 * 为字面量
        md_text = md_text.replace('*', r'\*')
        # 3) 恢复加粗占位符
        md_text = md_text.replace(_BOLD_OPEN, '**').replace(_BOLD_CLOSE, '**')

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
        html = html.replace(
            '<code>',
            f'<code style="background:{self._t("md_code_bg")};color:{self._t("md_code_text")};'
            f'padding:1px 6px;font-family:Consolas,\'Cascadia Code\';font-size:14px;border-radius:3px;">'
        )
        pre_border = self._t("md_pre_border_left")
        pre_border_css = (
            f'border-left:2px solid {pre_border};' if pre_border != "transparent" else ''
        )
        html = html.replace(
            '<pre>',
            f'<pre style="background:{self._t("md_pre_bg")};color:{self._t("md_pre_text")};'
            f'padding:14px 16px;font-family:Consolas,\'Cascadia Code\';font-size:14px;'
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
            f'<div style="color:{self._t("md_text")};font-size:15px;'
            f'font-family:\'Microsoft YaHei\',\'Microsoft YaHei UI\',\'Segoe UI\';line-height:1.7;">'
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
