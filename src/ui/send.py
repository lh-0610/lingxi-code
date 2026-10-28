"""发送与 worker：发送入口（按钮 / 回车 / 遥控）、起 worker 线程跑 agent_loop、视觉桥接、结束回调、强制停止。

从 chat_window.py 拆出的 ChatUI mixin；方法原样搬移，依赖 ChatUI 上的其它属性。
"""
import os
import threading

from langchain_core.messages import HumanMessage

from .. import agent
from .. import state
from .helpers import (
    _build_image_content_block,
)


class SendMixin:
    """发送与 worker：发送入口（按钮 / 回车 / 遥控）、起 worker 线程跑 agent_loop、视觉桥接、结束回调、强制停止。"""

    def _force_stop_generation(self, wait: bool = False, timeout: float = 3.0):
        """强制停止当前生成，立即更新 UI 状态（默认非阻塞）。

        停的对象是这条会话的 **worker 线程本身**（thread / last_worker），不是
        is_generating 标志——非阻塞停止会立刻把标志清 False，而旧线程可能还在
        收尾写这个会话，删除会话这类操作若只看标志就会漏掉它（实测会复活已删正文）。
        wait=True 时 join 这条线程直到真正退出或超时；超时返回 False、不动任何状态，
        由调用方决定放弃。这段"已停止但旧 worker 还在收尾"的窗口里，发送只会登记
        待启动请求，旧线程真正退出后才启动下一轮。
        """
        from .. import session as _session
        sess = _session.get_active()
        if not self._worker_alive(sess):
            return True
        thread = getattr(sess, "thread", None) or getattr(sess, "last_worker", None)
        state.stop_flag = True
        self._release_pending_confirm()
        self._release_pending_edit()
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                return False
        # 立即标记生成结束——_on_finished 再被触发时会跳过重复处理
        sess.is_generating = False
        if sess.thread is thread:
            sess.thread = None
        self._ai_reply_start = None
        # 对齐 _on_finished 的按钮恢复（输入框有内容则 enabled）。
        self._update_btn_state("enabled" if self._has_input else "disabled")
        return True

    def _on_send_click(self):
        from .. import session as _session
        sess = _session.get_active()
        if sess.is_generating:
            # 非阻塞停止：立即恢复按钮、不等线程退出（不能在主线程 join 3 秒）。
            # "停止后旧 worker 还在收尾"的窗口由 B09a 退出屏障兜住——这期间点发送
            # 只会登记待启动请求，旧线程真正退出后才启动下一轮，绝不双开。
            self._force_stop_generation()
        elif self._has_input or self._pending_images:
            self._send_message()

    def _spawn_worker_thread(self, sess, target, args=(), kwargs=None) -> bool:
        """起 worker 线程的统一出口：启动失败回滚运行态并如实反馈，不让会话卡在"生成中"。"""
        # 先作废上一轮的令牌：新运行已经"接纳"，旧 worker 的 finished 无论何时到达
        # 都按迟到处理，不能抢在新线程登记自己的令牌之前把忙碌状态解掉。
        sess.worker_token = None
        # 线程身份在 start() **之前**登记：Thread.start() 返回后，线程未必马上进入
        # target（target 内部那几行登记代码更没执行）。这个窗口里点停止会清掉
        # is_generating，若屏障还看不到线程占用，下一条消息就会双开同一会话。
        # worker 继承启动时刻的数据根（paths.set_data_dir 是线程本地的：桌面端
        # 主线程=默认根，worker 延续即可；Web 多用户模式下 worker 必须带用户根，
        # 否则存盘落到别人的数据目录——视觉桥接 worker 的真实存盘实测踩过）。
        from .. import paths as _paths
        inherited_root = _paths.get_data_dir()

        def _rooted_runner(*a, **k):
            _paths.set_data_dir(inherited_root)
            try:
                return target(*a, **k)
            finally:
                _paths.set_data_dir(None)

        try:
            thread = threading.Thread(target=_rooted_runner, args=args,
                                      kwargs=kwargs or {}, daemon=True)
        except Exception as e:
            from ..paths import logger
            logger.error(f"worker 线程创建失败: {e}", exc_info=True)
            sess.is_generating = False
            self._update_btn_state("enabled" if self._has_input else "disabled")
            return False
        sess.thread = thread
        sess.last_worker = thread   # 未启动成功的线程 is_alive() 恒 False，屏障不受影响
        try:
            thread.start()
            return True
        except Exception as e:
            from ..paths import logger
            logger.error(f"worker 线程启动失败: {e}", exc_info=True)
            if sess.thread is thread:
                sess.thread = None
            sess.is_generating = False
            self._update_btn_state("enabled" if self._has_input else "disabled")
            return False

    def submit_from_remote(self, text: str):
        """遥控消息注入入口（可从任意线程调用，通过 Signal 跨线程）。"""
        self.bridge.remote_submit.emit(text)

    def _on_remote_submit(self, text: str):
        """远程消息注入槽（主线程）。归属在接收时确定：绑定此刻的前台会话，
        等待接纳期间不随前台漂移（复核不过就不发）。

        桌面与遥控共用同一入队服务：运行中遥控消息进队列（回执"已排队"），
        空闲直接发送，收尾窗口走 B09a 待启动请求——三条路径都有明确回执。
        """
        from .. import session as _session
        _active = _session.get_active()
        if not text:
            return
        if getattr(_active, "is_generating", False):
            self._enqueue_input(_active, text, None, source="remote")
            return
        if self._gate_new_run_request(_active, "remote", text=text) == "start":
            state.remote_session = True
            # 立即发送与"等待后接纳"两条路径都要给手机端回执，不能静默丢
            if not self._do_send(text):
                self._notify_remote_unaccepted("消息发送失败（无法启动新一轮）")

    def _send_message(self):
        text = self.entry.toPlainText().strip()
        images = self._pending_images[:]
        from .. import session as _session
        active = _session.get_active()
        if not text and not images:
            return
        # 知识库模式：未配置目录 / 索引不可用时拦截发送，避免无意义的模型 API 调用；
        # 索引恢复可用后自然放行（每次发送即时校验）。
        if getattr(active, "session_kind", "code") == "rag":
            from .. import config
            if not config.RAG_KB_DIR:
                self._show_toast("请先在左侧「选择目录」并「重建索引」，再开始知识库提问")
                return
            # 会话锚点：已锚定别的库 → 直接拒发（不静默检索别的库）
            cur_kb = self._normalized_kb_dir()
            anchor = getattr(active, "rag_kb_dir", "") or ""
            if anchor and anchor != cur_kb:
                self._show_toast("本对话的知识库与当前目录不同，请切回原目录或新建知识库对话再提问")
                return
            try:
                from ..rag.retriever import index_status
                st = index_status(kb_dir=config.RAG_KB_DIR, embed_model=config.RAG_EMBED_MODEL,
                                  embed_base_url=config.RAG_EMBED_BASE_URL,
                                  chunk_size=config.RAG_CHUNK_SIZE,
                                  chunk_overlap=config.RAG_CHUNK_OVERLAP)
            except Exception:
                st = {"ok": False, "reason": "索引状态读取失败"}
            if not st.get("ok"):
                self._show_toast(f"知识库{st.get('reason', '不可用')}——请先「重建索引」再提问")
                return
            # 索引确认可用后才绑定空锚点——被拦下的发送不该把空会话提前钉死在某个库
            if not anchor:
                active.rag_kb_dir = cur_kb
        # B09b：运行中允许继续输入——Enter 提交进队列（点发送按钮仍是停止，见
        # _on_send_click），当前轮结束后按顺序逐项派发。入队持久化成功才清输入，
        # 失败时文字与附件原样保留（可重试）。
        if getattr(active, "is_generating", False):
            if self._enqueue_input(active, text, images, source="send"):
                self.entry.clear()
                self._pending_images.clear()
                self._refresh_image_preview()
                self._has_input = False
            return
        # B09a 统一启动闸：旧 worker 真正退出前不开新一轮。排队 / 被拒时输入
        # 原样保留（不清空、不画成已发送、不提前进历史），接纳成功才清理。
        if self._gate_new_run_request(active, "send", text=text, images=images) != "start":
            return
        if not self._do_send(text, images):
            return
        # 只有真的启动了新一轮才清理输入；等待接纳期间修改过的草稿不受影响
        self.entry.clear()
        self._pending_images.clear()
        self._refresh_image_preview()
        self._has_input = False

    def _append_user_text(self, text):
        """显示用户消息：@文件引用渲染成靛蓝加粗，其余普通（MessageView 用户气泡）。"""
        import re as _re
        import html as _html
        parts = []
        pat = _re.compile(r'(?<!\S)@[^\s@]+')
        pos = 0
        for m in pat.finditer(text):
            if m.start() > pos:
                parts.append(_html.escape(text[pos:m.start()]))
            parts.append(f'<b style="color:{self._t("mv_indigo")};">{_html.escape(m.group(0))}</b>')
            pos = m.end()
        if pos < len(text):
            parts.append(_html.escape(text[pos:]))
        self.chat_area.append_user_html("".join(parts).replace("\n", "<br>"))

    def _do_send(self, text: str, images=None, message_id=None) -> bool:
        """核心发送逻辑，GUI 和远程共用。返回是否真的启动了新一轮 worker。

        message_id 非空 = B09b 队列条目的正式接纳：沿用入队时分配的稳定消息 ID
        进历史（B03 run_id 与 B05 要求来源照常接上），绝不另发一条新消息。
        """
        images = images or []
        from .. import session as _session
        # ── B09a 底层硬闸：旧 worker 没真正退出就不准再开一轮 ──
        # 所有入口都应先过 _gate_new_run_request；这里是最后一道，防止绕过入口
        # 直接调本方法时双开 worker 抢同一个会话。拒绝时不产生任何副作用。
        if self._worker_alive(_session.get_active()):
            self._show_toast("上一轮仍在结束，消息未发送，请稍后重试", 3000)
            return False
        self._clear_empty_state()

        # @文件引用：聊天区只显示原文 display_text，完整文件内容只注入发给 AI 的 send_text
        # （避免把整个文件内容也刷在聊天界面上）
        display_text = text
        send_text = self._expand_file_mentions(text)

        # 上传/拖拽/粘贴的图片都带本地路径——把路径注入给模型（像 @文件一样），需要把图片
        # 当文件处理时它能拿去用。视觉看图仍走 base64 block，这里只多给个"文件句柄"。
        if images:
            _paths = "、".join(p for p, _ in images)
            _hint = (f"[本次随消息上传了 {len(images)} 张图片，本地路径"
                     f"（需要把图片当文件处理时可直接把路径传给工具）：{_paths}]")
            send_text = (send_text + "\n\n" + _hint) if send_text else _hint

        # 带图片但当前模型不支持视觉时，不再把整轮任务切给弱视觉模型。
        # 改为先用视觉模型做识别/OCR，再把识别结果作为文本交回当前强模型。
        use_vision_bridge = bool(images) and not agent.current_model_supports_vision()
        original_model_name = agent.MODEL_LIST[agent.current_model_index][0]
        vision_model_name = ""
        if use_vision_bridge:
            vision_idx = agent.get_vision_model_index()
            if vision_idx < 0:
                self._append_html(
                    "\n当前模型不支持图片。请先在「设置 → 图片识别模型」里选一个能看图的模型。\n",
                    "tool_result",
                )
                return False
            vision_model_name = agent.MODEL_LIST[vision_idx][0]

        self._update_btn_state("stop")
        # 捕获当前会话——worker 线程绑到它，不受后续切换 active 影响
        from .. import session as _session
        sess = _session.get_active()
        _session.register(sess)   # 确保在注册表里（启动初始会话首次发消息时落册）
        sess.is_generating = True

        # 显示用户消息
        self._append_html("\n", "spacer")
        self._append_html("你\n", "user_label")
        if images:
            self._insert_images_in_chat(images)
        if display_text:
            self._append_user_text(display_text + "\n\n")
        # 发消息后强制滚到底：看到自己刚发的 + 贴底后 AI 回复会自动跟随
        self._scroll_to_bottom()

        if use_vision_bridge:
            state.stop_flag = False
            # 视觉桥接的用户消息在工作线程里追加，启动失败不留半截历史
            if not self._spawn_worker_thread(
                    sess, self._run_vision_bridge_agent,
                    args=(send_text, images, vision_model_name, original_model_name,
                          sess, message_id)):
                self._show_toast("发送失败：无法创建工作线程，消息未发送", 4000)
                return False
            return True

        # 构造消息
        from .. import task_state as _ts
        if images:
            # Anthropic / MiMo 官方建议：图片在前、文字在后，模型才能正确关联问题与图片
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if send_text:
                content.append({"type": "text", "text": send_text})
            user_msg = HumanMessage(content=content)
        else:
            user_msg = HumanMessage(content=send_text)
        _ts.tag_user_message(user_msg, msg_id=message_id)
        agent.chat_history.append(user_msg)
        _ts.sync_user_requests(sess)

        state.stop_flag = False
        if not self._spawn_worker_thread(sess, self._run_agent, args=(sess,)):
            # 启动失败：这一轮要像从未发生过一样撤干净——历史、任务要求引用、磁盘。
            # 存盘在启动成功之后才做（见下），所以磁盘上本来就没有这条消息；
            # 内存里撤掉消息 + 摘掉刚记的任务要求引用，重发不会重复。
            try:
                if agent.chat_history and agent.chat_history[-1] is user_msg:
                    agent.chat_history.pop()
                _mid = getattr(user_msg, "id", None)
                _task = getattr(sess, "current_task", None)
                if _mid and isinstance(_task, dict):
                    _ids = _task.get("request_message_ids")
                    if isinstance(_ids, list) and _mid in _ids:
                        _ids.remove(_mid)
                    _sources = _task.get("request_sources")
                    if isinstance(_sources, dict):
                        _sources.pop(_mid, None)
            except Exception:
                pass
            self._append_html(
                "\n消息发送失败（无法创建工作线程），没有发给模型；"
                "输入已保留，可直接重发。\n", "tool_result")
            self._show_toast("发送失败：无法创建工作线程，消息未发送", 4000)
            return False
        # 立即存盘 + 刷侧栏：让会话马上出现在侧栏，长任务时开新对话也能切回它
        # （否则要等 worker 跑完才 save 进 index → 正在跑的会话在侧栏找不到）。
        # 必须在启动成功之后：失败时磁盘上不能留下一条"从未发送"的消息。
        try:
            agent.save_session()
        except Exception:
            pass
        self._refresh_session_list()
        return True

    def _run_agent(self, sess=None, resume=None):
        # 把这个 worker 线程绑定到它要跑的会话：之后 agent_loop / tools 里所有
        # state.X（会话级）都落到这个 session，不受主线程切换 active 影响。
        # P1 单会话时绑的就是 active，行为等价；多会话时这是隔离的关键。
        # resume：结果卡「继续任务」的请求（见 _start_resume），None = 普通一轮。
        from .. import session as _session
        if sess is None:
            sess = _session.get_active()
        sess.is_generating = True
        with sess.render_lock:
            sess.render_log.clear()    # 新一轮提问开始：清掉上一轮的渲染事件缓冲
        sess.thread = threading.current_thread()
        sess.last_worker = sess.thread     # 「继续」的屏障等的就是它真正退出
        # 本 worker 的身份令牌：finished 带着它回来，主线程据此分辨自己是不是"当前这一轮"。
        token = object()
        sess.worker_token = token
        _session.bind_thread(sess)
        from .. import roles as _roles
        sess.role_snapshot = _roles.capture_active_role()  # 冻结本轮人格（防生成途中被换卡）
        try:
            agent.agent_loop(self, resume=resume)
        finally:
            # 只有仍然是当前 worker 才清运行态：用户可能在这条 finally 之前就点了发送、
            # 新 worker 已经起来了，这时把 is_generating 清掉会让两轮的按钮状态打架。
            if sess.worker_token is token:
                sess.is_generating = False
                sess.thread = None
                sess.role_snapshot = None    # 解冻：空闲会话回退读全局当前角色
            _session.unbind_thread()
            self.bridge.finished.emit(sess, token)

    def _run_vision_bridge_agent(self, text, images, vision_model_name, original_model_name,
                                 sess=None, message_id=None):
        """非视觉模型收到图片时：视觉模型只负责识别，原模型负责最终回答。"""
        from .. import session as _session
        if sess is None:
            sess = _session.get_active()
        sess.is_generating = True
        with sess.render_lock:
            sess.render_log.clear()    # 新一轮提问开始：清掉上一轮的渲染事件缓冲
        sess.thread = threading.current_thread()
        sess.last_worker = sess.thread
        token = object()
        sess.worker_token = token
        _session.bind_thread(sess)
        from .. import roles as _roles
        sess.role_snapshot = _roles.capture_active_role()  # 冻结本轮人格（防生成途中被换卡）
        try:
            self.show_message(
                f"\n使用「{vision_model_name}」识别图片，随后交给「{original_model_name}」继续处理\n",
                "tool_result",
            )
            # 用户消息**先**落历史再识别：识别失败时消息也不能丢（历史必须如实
            # 反映用户发过什么），而且运行记录的来源身份（source.message_id）
            # 要指向这条消息，队列收尾的身份核对才对得上。
            from .. import task_state as _ts
            content = []
            for path, b64 in images:
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                content.append(_build_image_content_block(ext, b64))
            if text:
                content.append({"type": "text", "text": text})
            user_msg = HumanMessage(content=content)
            _ts.tag_user_message(user_msg, msg_id=message_id)
            agent.chat_history.append(user_msg)
            _ts.sync_user_requests(sess)
            try:
                agent.save_session()
            except Exception:
                pass

            detected_name, description = agent.describe_images_with_vision(text, images)
            if agent.stop_flag:
                return
            if not description:
                description = "图片识别未返回有效内容。"

            preview = description
            if len(preview) > 1200:
                preview = preview[:1200] + "\n... [识别结果较长，已折叠显示；完整内容会交给当前模型]"
            self.show_message(f"图片识别完成（{detected_name}）\n{preview}\n", "tool_result")

            bridge_text = (
                "[[LINGXI_INTERNAL_VISION_BRIDGE]]\n"
                f"[图片识别结果，由 {detected_name} 提供，供 {original_model_name} 继续处理]\n"
                f"{description}"
            )
            # 视觉桥接说明是程序生成的转述，不是用户的新要求——打上内部标记，
            # 否则运行记录会把"图片识别结果…"当成本轮用户的最新要求。
            bridge_msg = HumanMessage(content=bridge_text)
            _ts.tag_internal_message(bridge_msg, kind="vision_bridge")
            agent.chat_history.append(bridge_msg)
            self.bridge.sessions_refresh.emit()
            agent.agent_loop(self)
        except Exception as e:
            # 识别失败也是一轮真实的失败：补 begin/finalize 留下 failed 记录——
            # 队列收尾的身份核对靠 source.message_id 认领这条消息，把失败如实
            # 记到条目上，而不是错拿上一轮的 completed（实测踩过）。
            from .. import run_records as _rr
            from ..agent_result import AgentResult as _AgentResult
            try:
                sess.last_run_save_failed = False
                run = _rr.begin_run(sess, ui=self)
                if run is not None:
                    _rr.finalize_run(sess, run,
                                     _AgentResult("failed", f"图片识别失败: {str(e)[:120]}"),
                                     ui=self)
            except Exception:
                pass
            self.show_retry(f"图片识别失败: {str(e)[:100]}")
        finally:
            if sess.worker_token is token:
                sess.is_generating = False
                sess.thread = None
                sess.role_snapshot = None    # 解冻：空闲会话回退读全局当前角色
            _session.unbind_thread()
            self.bridge.finished.emit(sess, token)

    def _on_finished_sess(self, finished_sess, token=None):
        """finished 信号槽：区分活跃会话 vs 后台会话。

        - finished_sess == active → 活跃会话结束，走完整收尾（刷 UI/按钮/撤销等）
        - finished_sess != active → 后台会话结束，只刷侧栏，不碰前台 UI
        """
        from .. import session as _session
        active_sess = _session.get_active()
        if token is not None and getattr(finished_sess, "worker_token", None) is not token:
            # **旧 worker 的迟到 finished**。新一轮已经在跑，按钮正处于"停止"状态——
            # 在这里恢复成"可发送"会让用户以为上一条已经处理完了，点下去就是第二个 worker。
            # 侧栏照刷（旧那轮的标题/状态确实变了），前台状态一概不碰。
            self._refresh_session_list()
            return
        self._settle_resume_card(finished_sess)
        if finished_sess is not active_sess:
            # 后台会话完成：只刷侧栏列表（显示新标题/状态），不碰前台 UI。
            # 它的队列条目照常记终态，但不自动推进（第一版只自动派发前台会话）。
            self._refresh_session_list()
            self._settle_finished_queue_item(finished_sess)
            return
        # 活跃会话完成：完整收尾（若中途切走过，切回时已 _redraw_chat + 重放 render_log
        # 补齐，worker 这轮的后续也是实时渲染的，这里无需再整体重绘）
        self._ai_reply_start = None
        if hasattr(self, "chat_area"):
            self.chat_area.end_run()   # 回复上方的用时定格，开着的思考 / 工具卡收尾
        self._update_btn_state("enabled" if self._has_input else "disabled")
        self._refresh_session_list()
        # AI 这一轮可能新建了 checkpoint，刷新撤销按钮状态
        if hasattr(self, "undo_btn"):
            self._style_undo_btn()
        state.remote_session = False
        # B09b：先给已接纳的队列条目记录程序终态，再放行等待中的启动请求，
        # 最后按条件自动推进队列（终态 completed + 保存成功 + 未暂停才推进）。
        settle_saved, advance_ok, pause_reason = \
            self._settle_finished_queue_item(finished_sess)
        # B09a：这一轮真正结束了。若有待启动请求（停止后排队的那条消息），
        # 立即尝试接纳——接纳前仍会完整复核会话 / 项目 / 运行身份；轮询是兜底。
        self._admit_pending_run_for(finished_sess)
        self._maybe_auto_advance_queue(finished_sess, settle_saved=settle_saved,
                                       advance_ok=advance_ok,
                                       pause_reason=pause_reason)
