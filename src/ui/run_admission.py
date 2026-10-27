"""待启动请求的准入（B09a 统一退出屏障）：旧 worker 没退干净时登记请求、非阻塞等待、接纳前逐项复核。

从 chat_window.py 拆出的 ChatUI mixin；方法原样搬移，依赖 ChatUI 上的其它属性。
"""

from PySide6.QtCore import QTimer

from .. import state


class _PendingRunRequest:
    """一次待启动的运行请求（B09a 退出屏障）。

    上一轮 worker 还没真正退出时，发送 / 遥控入口不丢请求也不立刻启动，而是把它
    登记成这样一个对象，由非阻塞轮询等旧线程退出后再重新核对、接纳。这只是
    「一次待启动请求」的内存状态，不是多条输入队列。

    请求在登记时定格内容与上下文（文本 / 附件快照、旧 worker 线程、会话与项目归属），
    接纳时逐项复核——任何一项漂移都拒绝启动，绝不把输入发到别处。
    """

    __slots__ = ("session", "source", "text", "images", "old_worker",
                 "session_id", "session_project", "global_project", "worktree",
                 "queue_item_id", "attempts", "cancelled")

    def __init__(self, session, source, text="", images=None,
                 old_worker=None, session_id=None,
                 session_project=None, global_project=None, worktree=None,
                 queue_item_id=None):
        self.session = session
        self.source = source          # "send"（普通发送） / "remote"（遥控注入）
        self.text = text
        self.images = list(images or [])
        self.old_worker = old_worker  # 登记时还活着的那条旧 worker 线程
        self.session_id = session_id
        self.session_project = session_project
        self.global_project = global_project
        self.worktree = worktree      # 登记时的隔离区（实际工作目录）身份
        self.queue_item_id = queue_item_id  # 非空 = 这次启动是队列条目的派发
        self.attempts = 0
        self.cancelled = False


class RunAdmissionMixin:
    """待启动请求的准入（B09a 统一退出屏障）：旧 worker 没退干净时登记请求、非阻塞等待、接纳前逐项复核。"""

    # ── 发送消息 ──

    # ── B09a 统一退出屏障：上一轮真正退出前，同一会话不启动下一轮 ──
    # 判据只有一条：sess.last_worker 这条线程还活着就不算结束。is_generating 会被
    # 强制停止立刻置 False（旧线程还在收尾写这个会话），finished 信号也可能先于
    # 线程退出到达——都不能作为"可以开新一轮"的依据。

    def _worker_alive(self, sess) -> bool:
        """这个会话是否还有一条没退出的 worker 线程（含正在收尾的）。"""
        worker = getattr(sess, "last_worker", None)
        return bool(getattr(sess, "is_generating", False)) or (
            worker is not None and worker.is_alive())

    def _pending_runs_by_session(self) -> dict:
        """待启动请求表（Session → _PendingRunRequest）。每会话最多一条。"""
        runs = getattr(self, "_pending_runs", None)
        if runs is None:
            runs = self._pending_runs = {}
        return runs

    def _pending_run_for(self, sess):
        return self._pending_runs_by_session().get(sess)

    def _gate_new_run_request(self, sess, source, text="", images=None,
                              queue_item_id=None) -> str:
        """所有"开新一轮"入口的统一启动闸。返回：
        - "start"  —— 会话空闲，调用方可以立刻启动（_do_send / 重试线程）
        - "queued" —— 已登记为待启动请求，旧 worker 退出后由屏障接纳
        - "busy"   —— 拒绝（正常生成中 / 已有等待请求 / 继续或撤销正持有屏障）

        正常生成中不代停、不排队（点发送按钮 = 停止的语义留在 _on_send_click）；
        只有"已停止但旧 worker 还在收尾"的窗口才排队等待。
        queue_item_id 非空表示这次启动是 B09b 队列条目的派发，接纳时走
        _admit_queue_dispatch（沿用入队时的 message_id 进历史）。
        """
        if self._pending_run_for(sess) is not None:
            self._show_toast("已有一条消息在等待上一轮结束，未重复登记", 2500)
            if source == "remote":
                self._notify_remote_unaccepted("已有一条消息在等待发送，本条未接收")
            return "busy"
        if getattr(sess, "resume_pending", False):
            self._show_toast("「继续/撤销」正在等待当前运行结束，请稍后再试", 2500)
            if source == "remote":
                self._notify_remote_unaccepted("正在等待当前运行结束，消息未接收")
            return "busy"
        if getattr(sess, "is_generating", False):
            if source == "queue":
                # 队列派发只应发生在上一轮结束之后（_maybe_auto_advance_queue）；
                # 运行中走到这里说明时序异常，条目原地保留，等下一次结束信号再试。
                return "busy"
            self._show_toast("正在生成中，请先停止再发送", 2000)
            if source == "remote":
                self._notify_remote_unaccepted("当前正在生成中，消息未接收")
            return "busy"
        if self._worker_alive(sess):
            if source == "retry":
                # 重试要动 chat_history（弹掉空消息），等旧 worker 退出前不能碰
                self._show_toast("上一轮仍在结束，暂时无法重试，请稍后再试", 2500)
                return "busy"
            req = _PendingRunRequest(
                sess, source, text=text, images=images,
                old_worker=getattr(sess, "last_worker", None),
                session_id=getattr(sess, "current_session_id", None),
                session_project=getattr(sess, "project", None),
                global_project=state.current_project,
                worktree=getattr(sess, "worktree", None),
                queue_item_id=queue_item_id,
            )
            self._pending_runs_by_session()[sess] = req
            if source != "queue":
                self._show_toast("上一轮仍在结束，将在其退出后自动发送…", 2500)
            if source == "remote":
                self._notify_remote_unaccepted("上一轮正在结束，消息将在其退出后发送")
            QTimer.singleShot(self._RESUME_WAIT_MS, self,
                              lambda req=req: self._pending_wait_tick(req))
            return "queued"
        return "start"

    def _pending_wait_tick(self, req):
        """非阻塞轮询：旧 worker 退出 → 尝试接纳；超时 → 放弃（不强行放行）。"""
        sess = req.session
        if req.cancelled or self._pending_runs_by_session().get(sess) is not req:
            return          # 已被取消或被取代：一次性请求，绝不二次启动
        if self._worker_alive(sess):
            req.attempts += 1
            if req.attempts >= self._RESUME_WAIT_LIMIT:
                self._pending_runs_by_session().pop(sess, None)
                req.cancelled = True
                if req.queue_item_id:
                    # 队列条目：回到 queued，条目本身就是"输入保留"，可重试
                    self._queue_dispatch_aborted(sess, req.queue_item_id,
                                                 "等待上一轮退出超时，已停止尝试；可在面板重试")
                    return
                self._show_toast("上一轮仍在结束，消息未自动发送；输入已保留，可稍后重试", 4000)
                if req.source == "remote":
                    self._notify_remote_unaccepted("等待上一轮结束超时，消息未发送")
                # 旧线程仍在运行这一事实不动：不假装它结束了，也不清任何运行标记
                return
            QTimer.singleShot(self._RESUME_WAIT_MS, self,
                              lambda req=req: self._pending_wait_tick(req))
            return
        self._admit_pending_run(req)

    def _admit_pending_run(self, req):
        """接纳一次待启动请求：一次性弹出 → 完整复核 → 按来源启动。"""
        sess = req.session
        if self._pending_runs_by_session().get(sess) is not req:
            return
        if self._worker_alive(sess):
            # finished 信号可能先于线程退出到达：回到轮询，绝不带病启动
            self._pending_wait_tick(req)
            return
        del self._pending_runs_by_session()[sess]
        reason = self._pending_invalid_reason(req)
        if reason:
            if req.queue_item_id:
                self._queue_dispatch_aborted(sess, req.queue_item_id, reason)
                return
            if req.source == "remote":
                self._notify_remote_unaccepted(reason + "，消息未发送")
            self._show_toast(f"消息未发送：{reason}" +
                             ("（输入已保留）" if req.source == "send" else ""), 4000)
            return
        if req.queue_item_id:
            self._admit_queue_dispatch(sess, req.queue_item_id)
            return
        if req.source == "remote":
            state.remote_session = True
            if not self._do_send(req.text):
                self._notify_remote_unaccepted("消息发送失败（无法启动新一轮）")
            return
        if self._do_send(req.text, req.images):
            self._clear_sent_input(req)     # 只清理实际发送的那份
        else:
            self._show_toast("消息发送失败：无法创建工作线程，输入已保留，可重试", 4000)

    def _admit_pending_run_for(self, sess):
        """worker 结束信号到达时主动尝试接纳一次（轮询是兜底，不必等下一跳）。"""
        req = self._pending_run_for(sess)
        if req is not None and not req.cancelled:
            self._admit_pending_run(req)

    def _pending_invalid_reason(self, req) -> str:
        """接纳前的复核：会话、请求、项目归属、工作目录、运行身份任何一项漂移都拒绝。"""
        from .. import session as _session
        sess = req.session
        if req.cancelled:
            return "请求已取消"
        if sess is not _session.get_active():
            return "已切换到其它会话"
        if getattr(sess, "current_session_id", None) != req.session_id:
            return "会话已被重置或重新载入"
        if getattr(sess, "project", None) != req.session_project \
                or state.current_project != req.global_project:
            return "项目归属已改变"
        # 实际工作目录也要复核：项目根没动、隔离区从 A 换到 B，照样不算原来的现场
        if getattr(sess, "worktree", None) != req.worktree:
            return "工作目录（隔离区）已改变"
        if getattr(sess, "last_worker", None) is not req.old_worker:
            return "已有新一轮运行"
        return ""

    def _cancel_pending_run(self, sess, message):
        """明确取消某会话的待启动请求（切会话 / 重置 / 删除时调用）。"""
        req = self._pending_runs_by_session().pop(sess, None)
        if req is None:
            return
        req.cancelled = True
        if req.queue_item_id:
            # 队列条目的派发等待被取消：条目回到 queued（切走的会话队列随后会被暂停）
            self._queue_dispatch_aborted(sess, req.queue_item_id,
                                         "已切换会话，本次派发已取消")
        if message:
            self._show_toast(message, 3000)
            if req.source == "remote":
                self._notify_remote_unaccepted(message)

    def _clear_sent_input(self, req):
        """接纳成功后清理输入：只清实际发送的那份，等待期间的新编辑 / 新附件一律保留。"""
        if self.entry.toPlainText().strip() == req.text:
            self.entry.clear()
        for _img in req.images:
            try:
                self._pending_images.remove(_img)
            except ValueError:
                pass
        self._refresh_image_preview()
        self._check_input_state()

    def _notify_remote_unaccepted(self, message):
        """遥控注入无法接纳时给手机端一个明确回执（不能静默丢弃请求）。"""
        try:
            from .. import telegram_push
            telegram_push.push("info", "灵犀消息未发送", str(message)[:300])
        except Exception:
            pass
