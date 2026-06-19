"""F12 Debug Inspector 对话框。

像浏览器 F12 网络面板那样：左侧列出每次 LLM 调用，右侧看请求 / 响应详情。
- 数据从 src.debug_log 全局 recorder 拿
- 非模态：开着 inspector 还能继续聊
- F12 切换显示，再按一次关闭
"""
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QApplication,
)

from .. import debug_log
from ..limits import (
    DEBUG_MESSAGE_PREVIEW_CHARS,
    DEBUG_RESPONSE_PREVIEW_CHARS,
    DEBUG_SYSTEM_PROMPT_PREVIEW_CHARS,
)


class DebugInspector(QDialog):
    """F12 触发的非模态调试窗口。"""

    def __init__(self, parent=None):
        # 不传 parent 否则会跟着主窗口的 stacking/最小化走，达不到"并列两个窗口"
        super().__init__(None)
        self.setWindowTitle("灵犀 Debug Inspector")
        # 默认尺寸做小，主窗口边上能并排站住
        self.resize(720, 620)
        self._parent_window = parent  # 自己持引用做定位用
        # 非模态：主窗口能正常聊
        self.setModal(False)
        # 让它"浮在主窗口上方"但不抢全局焦点——切到主窗口聊天时 Inspector 还能看见
        flags = self.windowFlags()
        flags |= Qt.WindowStaysOnTopHint
        # 关掉 dialog 的"问号"按钮
        flags &= ~Qt.WindowContextHelpButtonHint
        self.setWindowFlags(flags)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(6)

        # ── 顶部工具栏 ──
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        tip = QLabel("内存中最近 50 次 LLM 调用 · 关 app 清空")
        tip.setStyleSheet("color: #888; font-size: 11px;")
        top.addWidget(tip)
        top.addStretch()
        self.follow_check = QCheckBox("跟随最新")
        self.follow_check.setChecked(True)
        top.addWidget(self.follow_check)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._on_clear)
        top.addWidget(clear_btn)
        copy_btn = QPushButton("复制选中 JSON")
        copy_btn.clicked.connect(self._on_copy)
        top.addWidget(copy_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.hide)
        top.addWidget(close_btn)
        root.addLayout(top)

        # ── 主体：左列表 / 右详情 ──
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(6)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumWidth(220)
        self.list_widget.currentItemChanged.connect(self._on_select)
        split.addWidget(self.list_widget)

        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        # 强制显示竖向滚动条，不然 JSON 长了不知道能滚
        self.detail.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.detail.setLineWrapMode(QTextBrowser.WidgetWidth)
        font = QFont("Consolas")
        font.setPixelSize(12)
        self.detail.setFont(font)
        split.addWidget(self.detail)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 840])
        root.addWidget(split, 1)

        self._apply_style()

        # 初始填充已有数据
        for rec in debug_log.recorder.all():
            self._append_to_list(rec)

        # 连接新增信号（默认 AutoConnection：worker 线程 emit 时会 queue 到主线程）
        debug_log.recorder.record_added.connect(self._on_record_added)

        # 默认选中最新一条
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    # 标志：是不是第一次 show（用于自动定位到主窗口边上）
    _has_positioned = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._has_positioned:
            self._dock_beside_parent()
            self._has_positioned = True

    def _dock_beside_parent(self):
        """第一次 show 时，自动把 Inspector 贴到主窗口右侧（屏幕装不下则贴左边）。"""
        pw = self._parent_window
        if pw is None or not pw.isVisible():
            return
        try:
            screen = pw.screen().availableGeometry()
        except Exception:
            return
        main_geo = pw.frameGeometry()
        my_w, my_h = self.width(), self.height()
        gap = 8
        # 优先贴右侧
        right_x = main_geo.right() + gap
        if right_x + my_w <= screen.right():
            x = right_x
        else:
            # 右侧装不下，试试左侧
            left_x = main_geo.left() - my_w - gap
            x = left_x if left_x >= screen.left() else screen.right() - my_w - gap
        y = max(screen.top(), main_geo.top())
        # 别让高度超出屏幕
        if y + my_h > screen.bottom():
            y = max(screen.top(), screen.bottom() - my_h)
        self.move(x, y)

    # ── 槽 ──

    def _on_record_added(self, record: dict):
        self._append_to_list(record)
        if self.follow_check.isChecked():
            self.list_widget.setCurrentRow(0)

    def _on_select(self, current, previous):
        if current is None:
            self.detail.clear()
            return
        rec = current.data(Qt.UserRole)
        if rec is None:
            self.detail.clear()
            return
        self.detail.setHtml(self._render_detail(rec))

    def _on_clear(self):
        debug_log.recorder.clear()
        self.list_widget.clear()
        self.detail.clear()

    def _on_copy(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        rec = item.data(Qt.UserRole)
        if rec is None:
            return
        QApplication.clipboard().setText(debug_log.record_to_json(rec))

    # ── 工具方法 ──

    def _append_to_list(self, record: dict):
        item = QListWidgetItem(debug_log.record_summary(record))
        item.setData(Qt.UserRole, record)
        if record.get("error"):
            item.setForeground(Qt.red)
        # 新的插到最前面
        self.list_widget.insertItem(0, item)
        # 截到 MAX_RECORDS 之内
        while self.list_widget.count() > debug_log.MAX_RECORDS:
            self.list_widget.takeItem(self.list_widget.count() - 1)

    def _render_detail(self, rec: dict) -> str:
        """把一条 record 渲染成 HTML。

        注意：QTextBrowser 用 QTextDocument，**不支持** HTML5 `<details>` / `<summary>`，
        所以全部内容平铺展开，靠 styled `<div>` 块分区。如果某一块太长就接受滚动。
        """
        def esc(s):
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace("\n", "<br>"))

        def section_title(title):
            return (f'<p style="background:#eef1ff;color:#3842b8;padding:6px 12px;'
                    f'margin:14px 0 4px 0;border-radius:5px;font-weight:600;">{title}</p>')

        def code_block(text, bg="#fafafa", fg="#444"):
            return (f'<pre style="background:{bg};color:{fg};padding:10px 12px;'
                    f'margin:4px 0;font-family:Consolas;font-size:11px;'
                    f'white-space:pre-wrap;word-wrap:break-word;">{text}</pre>')

        req = rec.get("request") or {}
        resp = rec.get("response") or {}
        usage = resp.get("usage") or {}
        err = rec.get("error")

        # ── 概要 ──
        status = ('<span style="color:#c0392b;">✗ 失败</span>' if err
                  else '<span style="color:#27ae60;">✓ 成功</span>')
        summary = (
            f'<div style="background:#f3f4f6;padding:12px 16px;border-radius:8px;">'
            f'<b>状态</b>：{status}<br>'
            f'<b>模型</b>：{esc(rec.get("model",""))} '
            f'<span style="color:#888;">({esc(rec.get("provider",""))})</span><br>'
            f'<b>端点</b>：<code style="color:#444;">{esc(rec.get("endpoint","") or "（未识别）")}</code><br>'
            f'<b>耗时</b>：{rec.get("elapsed_ms",0)} ms<br>'
            f'<b>Usage</b>：input {usage.get("input",0)} · output {usage.get("output",0)} · '
            f'总 {usage.get("total",0)}'
            f'</div>'
        )

        # ── 错误（如有，紧跟概要后）──
        err_block = ""
        if err:
            err_block = (
                f'<div style="background:#fff0ed;padding:10px 14px;border-radius:6px;'
                f'margin:8px 0;color:#c0392b;font-family:Consolas;font-size:11px;'
                f'white-space:pre-wrap;word-wrap:break-word;">'
                f'<b>❌ 错误</b><br>{esc(err)}</div>'
            )

        # ── 请求：system prompt ──
        sys_prompt = req.get("system_prompt", "") or ""
        sys_view = esc(
            sys_prompt[:DEBUG_SYSTEM_PROMPT_PREVIEW_CHARS]
            + ("..." if len(sys_prompt) > DEBUG_SYSTEM_PROMPT_PREVIEW_CHARS else "")
        )
        request_block = section_title(
            f'📥 请求（{len(req.get("messages") or [])} 条 messages · '
            f'{len(req.get("tools") or [])} 个工具）'
        )
        request_block += (
            f'<p style="color:#666;margin:4px 0;">'
            f'system prompt（前 {DEBUG_SYSTEM_PROMPT_PREVIEW_CHARS} 字）：</p>'
        )
        request_block += code_block(sys_view or "<em>（空）</em>")

        # ── 请求：messages ──
        request_block += '<p style="color:#666;margin:8px 0 4px 0;">messages：</p>'
        for i, m in enumerate(req.get("messages") or []):
            role = m.get("role", "?")
            color = {"user": "#1e6fff", "assistant": "#d87755",
                     "system": "#7f8c8d", "tool": "#16a085"}.get(role, "#333")
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for blk in content:
                    if isinstance(blk, dict):
                        bt = blk.get("type", "?")
                        if bt == "text":
                            parts.append(f"text: {esc(blk.get('text','')[:300])}")
                        elif bt in ("image", "image_url"):
                            parts.append(f"[{bt}]")
                        elif bt == "thinking":
                            parts.append(f"thinking: {esc((blk.get('thinking') or '')[:200])}")
                        else:
                            parts.append(f"[{bt}]")
                content_view = "<br>".join(parts)
            else:
                content_str = str(content)
                content_view = esc(
                    content_str[:DEBUG_MESSAGE_PREVIEW_CHARS]
                    + ("..." if len(content_str) > DEBUG_MESSAGE_PREVIEW_CHARS else "")
                )
            tcs = m.get("tool_calls") or []
            tcs_view = ""
            if tcs:
                tcs_view = "<br>🔧 " + ", ".join(
                    f"{esc(tc.get('name',''))}({esc(json.dumps(tc.get('args',{}), ensure_ascii=False))[:120]})"
                    for tc in tcs
                )
            request_block += (
                f'<div style="margin:4px 0;padding:6px 10px;border-left:3px solid {color};'
                f'background:#fafbfc;">'
                f'<b style="color:{color};">[{i}] {role}</b><br>{content_view}{tcs_view}'
                f'</div>'
            )

        # ── 请求：tools ──
        tools_list = req.get("tools") or []
        request_block += (
            f'<p style="color:#666;margin:8px 0 4px 0;">'
            f'tools enabled ({len(tools_list)})：'
            f'<span style="color:#888;">{esc(", ".join(tools_list))}</span></p>'
        )

        # ── 响应：text ──
        resp_text = resp.get("text") or ""
        resp_view = esc(
            resp_text[:DEBUG_RESPONSE_PREVIEW_CHARS]
            + ("..." if len(resp_text) > DEBUG_RESPONSE_PREVIEW_CHARS else "")
        )
        response_block = section_title('📤 响应')
        response_block += f'<p style="color:#666;margin:4px 0;">text（{len(resp_text)} 字）：</p>'
        response_block += code_block(resp_view or "<em>（空）</em>")

        # ── 响应：thinking ──
        thinking = resp.get("thinking") or ""
        if thinking:
            response_block += (
                f'<p style="color:#666;margin:8px 0 4px 0;">'
                f'thinking / reasoning（{len(thinking)} 字）：</p>'
            )
            response_block += code_block(
                esc(
                    thinking[:DEBUG_RESPONSE_PREVIEW_CHARS]
                    + ("..." if len(thinking) > DEBUG_RESPONSE_PREVIEW_CHARS else "")
                ),
                bg="#f5efff", fg="#5b66d6",
            )

        # ── 响应：tool calls ──
        tc_list = resp.get("tool_calls") or []
        response_block += (
            f'<p style="color:#666;margin:8px 0 4px 0;">tool calls ({len(tc_list)})：</p>'
        )
        if tc_list:
            for tc in tc_list:
                response_block += (
                    f'<div style="margin:3px 0;padding:5px 10px;background:#eef9f3;'
                    f'border-left:3px solid #16a085;">'
                    f'🔧 <b>{esc(tc.get("name",""))}</b>'
                    f'({esc(json.dumps(tc.get("args",{}), ensure_ascii=False))[:300]})'
                    f'</div>'
                )
        else:
            response_block += '<p style="color:#aaa;margin:4px 0;">（无）</p>'

        # ── Raw JSON 平铺展开（不折叠）──
        raw_block = section_title('▼ Raw JSON（完整 record）')
        raw_block += code_block(
            esc(debug_log.record_to_json(rec)), bg="#1e1e1e", fg="#e8e2d4",
        )

        return summary + err_block + request_block + response_block + raw_block

    def _apply_style(self):
        self.setStyleSheet(
            "QListWidget { background:#fafbfc; border:1px solid #e2e6ee; border-radius:6px;"
            "  font-family:'Microsoft YaHei UI'; font-size:12px; }"
            "QListWidget::item { padding:8px 10px; border-bottom:1px solid #f0f0f0; }"
            "QListWidget::item:selected { background:#e8ecff; color:#232b7a; }"
            "QTextBrowser { background:#ffffff; border:1px solid #e2e6ee; border-radius:6px;"
            "  padding:10px; }"
            "QPushButton { padding:5px 12px; border:1px solid #dfe4ee; border-radius:5px;"
            "  background:#ffffff; font-size:12px; }"
            "QPushButton:hover { background:#f4f6fb; border-color:#5b66d6; color:#3842b8; }"
            "QCheckBox { font-size:12px; }"
        )
