"""侧边栏浏览器工具（P1，docs/browser-design.md §3/§6/§11）。

8 个 @tool：open / snapshot / find / read_text / click / type / console / close。
结构仿 tools_web.py：结果一律字符串；bridge / page 惰性导入（顶层不碰 Qt/WebEngine，
不用浏览器就不存在，冷启动不劣化，§11.7）。

**确认闸门在工具内部**（仿 run_command，拍板 2）：security.decide_action 按
权限模式 × 动作分级 × 来源给出 allow/ask/ask_always/block；要问时调
ui.confirm_command(kind=KIND_BROWSER)，无界面走 _no_ui_refusal fail-closed。
click/type 执行前先 info(ref) 做有后果结构判定（submit 按钮 / POST 表单 / 回车提交），
有后果永远问、不给记住。streaming._execute_tool 不加浏览器闸门，Plan 白名单拦截照旧。

页面文字一律围 <browser-content url untrusted>（§6.3）：页面内容是外部输入，
其中的指令不是用户指令。动作默认附 all 模式前后"变化行 diff"（§3.2，省轮次的关键）。
"""
import functools
import json
import re
import threading

from langchain_core.tools import tool

from . import permission_modes as _pm
from . import session as _session
from . import state
from .browser import security, tree
from .limits import TOOL_RESULT_HARD_CAP_CHARS
from .paths import logger

# 动作前后 diff 用的 all 快照上限：diff 要全树才算得准，给到单条结果硬上限
_DIFF_SNAPSHOT_CHARS = TOOL_RESULT_HARD_CAP_CHARS
# 空 diff 时等异步渲染落地的时间（复审 6a：setTimeout/rAF 的变化不能漏报）
_DELTA_SETTLE_MS = 400
# view 隐藏时定时器被 Chromium 节流到 ~1s，空 diff 需再补一次长 settle（复审 6a/7）
_DELTA_SETTLE_HIDDEN_MS = 900
# 记样式基线前最多等聚焦 / 滚动带出来的过渡多久（复审 #1 #2）
_SETTLE_TRANSITION_MAX_MS = 1000
# 真实鼠标点击后确认送达：最多查几次（每次隔 30ms）
_PRESS_CONFIRM_TRIES = 20
_NO_PAGE = "还没有打开的页面：先用 browser_open(url) 打开。"

# ref 签发登记：((标签, frame), ref) -> 签发时的 docId。换文档后旧 ref 一律报失效，
# 配错 frame / 标签也报失效（不能点中别的 frame / 别的标签 / 新文档的同号元素）
_issued: dict = {}


# ── 多个会话共用一个浏览器（复审 #22）──
# 同一时刻只跑一个浏览器动作：一个动作是「动作前快照 → 点 / 输入 → 动作后快照 + 样式核对」
# 一整串，两个会话交错着跑，变化行 diff 和样式基线（tree.js 全局只有一份）就会互相覆盖，
# 报给模型的「变了什么」张冠李戴。排队时每 0.2 秒看一次停止标志，停得下来。
_tool_lock = threading.Lock()
_LOCK_POLL_S = 0.2
# 上一次用浏览器的会话（记 id，不拖着会话对象；会话对象用 __slots__ 没法弱引用）：换了会话就
# 提醒一句页面可能被动过。旧会话被回收后 id 被新会话碰巧复用，结果只是少提醒一次，不会错报
_last_user = {"id": None}
_SHARED_NOTE = ("（注意：这个浏览器刚被另一个会话用过，当前页面 / 标签可能已经变了；"
                "要操作先 browser_snapshot 看一眼现在的样子）\n")


def _exclusive(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        while not _tool_lock.acquire(timeout=_LOCK_POLL_S):
            if state.stop_flag:
                return "已停止（在等另一个会话的浏览器操作做完）"
        try:
            from .browser import page as _page
            _page.drain_model_events()          # 上一个动作没取走的不算这次的
            me = id(_session.current_session())
            last, _last_user["id"] = _last_user["id"], me
            out = fn(*args, **kwargs)
            events = _page.drain_model_events()
            if events and isinstance(out, str):
                out += _events_text(events)
            from .browser import page
            if (last is not None and last != me and fn.__name__ != "browser_open"
                    and page._browser is not None and isinstance(out, str)):
                out = _SHARED_NOTE + out
            return out
        finally:
            _tool_lock.release()
    return wrapper


def _events_text(events) -> str:
    """这次动作期间网页弹的框、要下载的文件、证书 / 登录：如实告诉模型（复审 #16 #17）。
    网页写的字（弹框内容、文件名）进不可信围栏。以前确认框被取消只在控制台留一行，模型常以为
    「点了没反应」再点一次。"""
    lines = []
    for kind, url, text in events:
        if kind == "confirm":
            lines.append("页面弹出确认框——已按取消处理，灵犀不替用户点同意；"
                         "要继续的话请用户自己在面板里点。确认框的内容：\n" + tree.fence(text, url))
        elif kind == "prompt":
            lines.append("页面要求输入——已按取消处理；需要的话请用户自己在面板里操作。提示文字：\n"
                         + tree.fence(text, url))
        elif kind == "alert":
            lines.append("页面弹出提示（已关掉）：\n" + tree.fence(text, url))
        elif kind == "download":
            lines.append("页面要下载文件，已拦下；需要的话请用户在面板里点。文件名：\n"
                         + tree.fence(text, url))
        elif kind == "cert":
            lines.append(f"证书不受信任，没有打开 {security.fence_url(url)}；"
                         "本机开发服务的话请用户在面板里点「仍然访问」，灵犀不替它放行。")
        elif kind == "auth_ok":
            lines.append("网站要求登录，用户已在弹框里登录（账号密码没有交给灵犀）。")
        elif kind == "auth_cancel":
            lines.append("网站要求登录，用户取消了（页面会是 401 / 未授权）。")
    return ("\n" + "\n".join(lines)) if lines else ""


def _cur_tab():
    from .browser import page
    return getattr(page._browser, "current_id", None)


def _tab_ids():
    from .browser import page
    return list(getattr(page._browser, "tabs", None) or [])


def _tabs_note():
    """不止一个标签时，结果末尾提一句（模型不知道有别的标签，就不会想到切回去）。"""
    ids = _tab_ids()
    if len(ids) < 2:
        return ""
    return f"\n（共 {len(ids)} 个标签，当前 #{_cur_tab()}；browser_tabs 查看 / 切换）"


_NEW_TAB_LOAD_WAIT_S = 10


def _new_tab_text(tabs0, cur0):
    """动作打开了新标签：返回给模型的说明（切过去了就附新页面的交互快照），没有返回 ""。

    新窗口请求是异步到的：调用方在动作后、算变化前后各查一次。像 browser-use 那样
    自动切到新标签——模型点「在新标签打开」的链接，要的就是那一页。"""
    new = [t for t in _tab_ids() if t not in tabs0]
    cur = _cur_tab()
    if not new and cur == cur0:
        return ""
    if cur in new:
        # 等新页面加载完再快照（最多 10 秒）
        import time as _t
        deadline = _t.monotonic() + _NEW_TAB_LOAD_WAIT_S
        while _t.monotonic() < deadline:
            q = _bridge_call("qturl", timeout=5)
            if q.get("ok") and not q.get("loading") and q.get("url") not in ("", "about:blank"):
                break
            _bridge_call("settle", ms=200)
        head = (f"\n这次操作打开了新标签 #{cur}，已切换过去（原来的标签 #{cur0} 还在，"
                "browser_tabs 可切回）。新标签的页面：\n")
        snap = _lx("snapshot", {"mode": "interactive", "maxChars": tree.SNAPSHOT_DEFAULT_CHARS})
        if not snap.get("ok"):
            return head + f"（快照失败: {snap.get('error')}，用 browser_snapshot 重试）"
        _remember_refs(snap, None)
        return head + _snapshot_text(snap, tree.SNAPSHOT_DEFAULT_CHARS,
                                     title="页面标题: " + (snap.get("title") or "（无标题）"))
    if new:
        ids = "、".join(f"#{t}" for t in new)
        return f"\n这次操作在后台打开了新标签 {ids}（没切换过去；browser_tabs 可切过去看）。"
    return f"\n当前标签变成了 #{cur}（原来是 #{cur0}）；先 browser_snapshot 看看。"


def _after_action(before, frame, url, tabs0, cur0):
    """动作之后：开了新标签就报新标签（这时的「变化」比的是两个不同页面，没有意义），
    否则照常报变化行 diff / 样式核对。"""
    if before is None:
        _bridge_call("settle", ms=150)     # 没算变化：给新窗口请求一点时间到达
        return _new_tab_text(tabs0, cur0)
    opened = _new_tab_text(tabs0, cur0)
    if opened:
        return opened
    delta = _delta_text(before, frame, url)
    return _new_tab_text(tabs0, cur0) or delta


def _fk(frame):
    """ref 登记的作用域：哪个标签里的哪个 frame（每个文档的 ref 都从 e1 数起，不分开会串号）。"""
    return (_cur_tab(), frame or "")
_STALE_REF = ("ref 已失效：页面已换文档，或该 ref 不是当前快照/find 签发的。"
              "重新 browser_snapshot 拿新 ref 再操作。")


def _ref_scope(ref, frame):
    """ref 该按哪个 frame 用。返回 (frame, 给模型的说明)。

    ref 编号在一个标签里各个 frame 之间是接着数的、不重号。调用时没带 frame（或带错了）、
    而这个 ref 恰好只在当前标签的另一个 frame 里签发过：就按那个 frame 操作，并告诉模型下次
    带上。以前直接报「ref 已失效：页面已换文档」，模型以为页面在刷新（用户实测一局里 4 次）。"""
    if not ref or (_fk(frame), ref) in _issued:
        return frame, ""
    tab = _cur_tab()
    owners = {k[0][1] for k in _issued if k[1] == ref and k[0][0] == tab}
    if len(owners) != 1:
        return frame, ""
    owner = next(iter(owners)) or None
    if owner:
        return owner, f"\n（ref={ref} 是在 frame={owner} 里签发的，已按它操作；下次调用带上 frame=\"{owner}\"）"
    return None, f"\n（ref={ref} 是在顶层页面签发的，已按顶层页面操作；下次别带 frame）"


def _frame_offset(frame):
    """frame（f.0 / f.0.1）的内容区左上角在顶层视口里的位置 (x, y)，顶层是 (0, 0)。

    iframe 里的坐标加上它才能发真实鼠标事件。跨域 iframe 里拿不到 frameElement，只能从外层
    量：外层文档列出各个 iframe 的位置和大小，按 Qt 那边这个 frame 的大小（尺寸不唯一时再比
    地址）对上号；对不上 / 不唯一返回 None（调用方退回脚本点击，或如实说做不了）。
    iframe 有一截在外层视口外面时先把它滚进来。"""
    if not frame:
        return 0.0, 0.0
    fr = _bridge_call("frames")
    if not fr.get("ok"):
        return None
    by_path = {f.get("path"): f for f in fr.get("frames") or []}
    parts = frame.split(".")
    ox = oy = 0.0
    parent = None
    for i in range(2, len(parts) + 1):
        path = ".".join(parts[:i])
        me = by_path.get(path)
        if me is None or (me.get("w") or 0) <= 0:
            return None
        box = None
        for _attempt in range(2):
            boxes = _lx("frameBoxes", {}, parent)
            if not boxes.get("ok"):
                return None
            cands = [b for b in boxes.get("frames") or []
                     if abs(b["w"] - me["w"]) <= 2 and abs(b["h"] - me["h"]) <= 2]
            if len(cands) > 1:
                cands = [b for b in cands if b.get("src") == me.get("url")]
            if len(cands) != 1:
                return None
            box = cands[0]
            inside = (box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["w"] <= boxes["vw"] + 1
                      and box["y"] + box["h"] <= boxes["vh"] + 1)
            if inside or _attempt:
                break
            _lx("revealFrame", {"index": box["index"]}, parent)
        ox += box["x"]
        oy += box["y"]
        parent = path
    return ox, oy


def _remember_refs(r, frame):
    doc = r.get("docId") or ""
    fk = _fk(frame)
    for k in [k for k, v in _issued.items() if k[0] == fk and v != doc]:
        del _issued[k]
    for ref in re.findall(r"\[ref=(e\d+)\]", r.get("tree", "")):
        _issued[(fk, ref)] = doc
    for m in r.get("matches") or []:
        if m.get("ref"):
            _issued[(fk, m["ref"])] = doc
    if len(_issued) > 4000:
        _issued.clear()


def _origin_for(sess, action, url, extra_url=None):
    allow = getattr(sess, "browser_open_allowlist" if action == "open"
                    else "browser_origin_allowlist", set())
    origin = security.origin_kind(url, allow)
    if extra_url:
        origin = security.stricter_origin(origin, security.origin_kind(extra_url, allow))
    return origin


def _bridge_call(op, timeout=30, **args):
    from .browser.bridge import get_bridge
    return get_bridge().call(op, timeout=timeout, **args)


def _lx(method, args=None, frame=None, timeout=30):
    """调注入树 API。没开过页面时直接拒，不让杂散快照悄悄拉起一个渲染进程。"""
    from .browser import page
    if page._browser is None:
        return {"ok": False, "error": _NO_PAGE}
    return _bridge_call("lx", timeout=timeout, method=method, args=args or {}, frame=frame)


def _err(r, what):
    return f"{what}失败: {r.get('error') or '未知错误'}"


def _gate(action, url, *, consequential=False, card="", extra_url=None, origin=None):
    """返回 "" 放行，或拒绝文案。决策表在 security.decide_action（§6.1）。
    extra_url：动作目标 frame 的 URL——与顶层取更严的来源判（公网 iframe 套在
    localhost 页面里不能绕过确认）。"""
    sess = _session.current_session()
    mode = _pm.current(sess)
    if origin is None:
        origin = _origin_for(sess, action, url, extra_url)
    decision = security.decide_action(mode, action=action, origin=origin,
                                      consequential=consequential)
    if decision == security.BLOCK:
        return ("已拒绝：Plan 模式只做只读调研，不能在页面上点击/输入。"
                "把要做的操作写进方案，让用户切回 Act 模式后再执行。")
    if decision == security.ALLOW:
        return ""
    if getattr(sess, "is_subagent", False):
        # 有后果的（提交 / 删除 / 支付…）不放行（复审 #20）：浏览器不在 worktree 隔离里，
        # 子 Agent 点下去改的是真实页面和真实数据，派出时的那次同意管不到这一下
        if decision == security.ASK_ALWAYS:
            return ("已拒绝：子 Agent 不能做有后果的浏览器操作（提交 / 删除 / 支付等）。"
                    "把要做的这一步写进结果，交回主会话由用户确认后再做。")
        # 普通操作不逐条问（派出之前已经问过用户，同 run_command）
        return ""
    ui = getattr(state, "ui_ref", None)
    if ui is None:
        from .tools import _no_ui_refusal
        return _no_ui_refusal("操作浏览器")
    from .browser import page as _page
    try:
        with _page.model_waiting():     # 卡片开着时用户在网页上点出来的框归用户
            allowed, feedback = ui.confirm_command(
                card, kind=_pm.KIND_BROWSER,
                browser_origin=security.parse_origin(extra_url or url),
                browser_action=action,
                browser_consequential=consequential)
    except Exception as e:
        logger.warning(f"浏览器确认对话框异常，默认拒绝: {e}")
        return f"用户确认对话框出错，已拒绝浏览器操作: {e}"
    if not allowed:
        msg = "已拒绝：用户不允许此浏览器操作。"
        if feedback:
            msg += f"\n用户补充说明：{feedback}"
        logger.info(f"用户拒绝浏览器操作: {card.splitlines()[0]}")
        return msg
    return ""


def _snapshot_text(r, cap, title=""):
    text = tree.truncate(r.get("tree", ""), cap)
    if title:
        text = title + "\n" + text
    body = tree.fence(text, r.get("qtUrl") or r.get("url", ""))
    stats = r.get("stats") or {}
    note = f"（快照 {stats.get('lines', '?')} 行 / {stats.get('refs', '?')} 个 ref"
    if stats.get("truncated"):
        note += "，已截断——用 root_ref 取子树、browser_find 定位，或 mode=all 看正文"
    return body + "\n" + note + "）"


def _before_action(ref, frame):
    """动作前：先做完滚动 / 聚焦（这些副作用不能算进动作的效果），再取文字树快照、
    记操作处附近的计算样式基线。样式基线取不到不影响动作本身。"""
    _lx("watchPrepare", {"ref": ref}, frame)
    before = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
    # 聚焦 / 滚动带出来的过渡还在跑：等它跑完再记基线（复审 #1 #2），最多等 1 秒
    settling = _lx("watchSettling", {"ref": ref}, frame)
    wait = int(settling.get("ms") or 0) if settling.get("ok") else 0
    if wait > 0:
        _bridge_call("settle", ms=min(wait, _SETTLE_TRANSITION_MAX_MS) + 50)
    _lx("watchBaseline", {"ref": ref}, frame)
    # 动作前的导航序号：动作之后据此判断「点击触发了导航、新页面还没到」（复审 #5）
    q = _bridge_call("qturl")
    if isinstance(before, dict) and q.get("ok"):
        before["_nav0"] = q.get("nav") or 0
    return before


def _reconfirm_after_prepare(ref, frame, info, cons, *, submit=False, origin=None):
    """聚焦之后元素可能被页面自己改了（复审 #10：antd Tabs 聚焦时往标签里插「Tab 1 of 3」，
    名字一变指纹就对不上，第一次点击必被拒）。重新取一次结构信息：变成了有后果的动作就
    拒绝（用户批准的不是这个）；否则按聚焦后的指纹执行。返回 (指纹, 拒绝文案)。"""
    again = _lx("info", {"ref": ref}, frame)
    if not again.get("ok"):
        return None, _err(again, "定位元素")
    if again.get("fp") == info.get("fp"):
        return info.get("fp"), ""
    if not cons and security.is_consequential(again, submit=submit, origin=origin):
        return None, ("已停止：聚焦之后这个元素变了，现在看起来是有后果的动作（提交 / 删除等），"
                      "和批准时不一样。重新 browser_snapshot 看清楚再操作。")
    return again.get("fp"), ""


def _style_text(frame, url=""):
    """文字树 diff 为空时，报动作前后操作处附近**实际计算样式**的变化。

    只报样式真变了的元素（class 换了但样式没变不报）；动作前就在自己变的元素、
    离得远没核对的属性变化只给计数，不说成"变了"。取不到基线返回 None，调用方退回原话。"""
    w = _lx("watchReport", {}, frame)
    if not w.get("ok"):
        return None
    changed = w.get("changed") or []
    if changed:
        lines = ["文字树没有变化，但操作处附近的实际样式变了（动作前后逐项比对计算样式）："]
        items = []
        for c in changed:
            props = "；".join(f"{p['prop']} {p['from']} → {p['to']}" for p in c.get("props") or [])
            mark = "（就是操作的元素）" if c.get("self") else ""
            items.append(f"- {c.get('desc')}{mark}：{props}")
        if w.get("more"):
            items.append(f"- …另有 {w['more']} 个元素")
        # 元素描述取自页面文字、content 属性值就是页面写的字：整段进围栏（复审 #19）
        lines.append(tree.fence("\n".join(items), url))
    else:
        lines = ["文字树和操作处附近的样式都没有变化（动作前后逐项比对过计算样式）。"]
    notes = []
    if w.get("busy"):
        notes.append(f"{w['busy']} 个元素动作前就在自己变（动画 / 定时刷新），没算进去")
    if w.get("elsewhere"):
        notes.append(f"页面其他位置有 {w['elsewhere']} 个元素属性变了，是否可见没核对")
    if notes:
        lines.append("（" + "；".join(notes) + "）")
    lines.append("要确认看起来对不对用 browser_screenshot；canvas / 图表这类文字和样式都看不到。")
    return "\n".join(lines)


def _diff(before, after):
    return tree.compute_delta(
        before.get("tree", ""), after.get("tree", ""), tree.DELTA_DEFAULT_CHARS,
        before_cut=bool((before.get("stats") or {}).get("truncated")),
        after_cut=bool((after.get("stats") or {}).get("truncated")))


def _delta_text(before, frame, url):
    """动作后的变化行 diff（§3.2）。before 是动作前的 all 快照（None=不算 diff）。

    假阴性防护（复审 6）：空 diff 先 settle 一次再重取（异步渲染可能还没落）；
    快照被截断时不裸报"无可见变化"（变化可能落在截断区）。"""
    if before is None:
        return ""
    after = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
    if not after.get("ok"):
        return f"\n（变化快照失败: {after.get('error')}，可用 browser_snapshot 查看现状）"
    if not before.get("ok"):
        return "\n（动作前快照失败，算不了变化；用 browser_snapshot 看现状）"
    delta = _diff(before, after)
    # 导航信号取首次 after：settle 重试可能取到重试间又变化的快照，不能抹掉"已导航"
    nav_url = after.get("qtUrl") or after.get("url")
    navigated = _changed_document(before, after, nav_url, url)
    if delta == tree.DELTA_EMPTY:
        # 异步渲染（setTimeout/rAF）可能还没落到 DOM：等一小段再重取一次
        _bridge_call("settle", ms=_DELTA_SETTLE_MS)
        retry = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
        if retry.get("ok"):
            after = retry
            delta = _diff(before, after)
        if delta == tree.DELTA_EMPTY and _view_hidden():
            # 隐藏 view 的定时器被节流到 ~1s，400ms 不够：再补一次长 settle
            _bridge_call("settle", ms=_DELTA_SETTLE_HIDDEN_MS)
            retry2 = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
            if retry2.get("ok"):
                after = retry2
                delta = _diff(before, after)
    # 动作后的快照就是一份真快照：变化行里露出来的 ref（新弹出的按钮……）登记成可用，
    # 模型照着点不会被拒。以前只认 snapshot / find 签发的，diff 里给了 ref 却点不了（用户实测 6 次）
    if after.get("ok"):
        _remember_refs(after, frame)
    nav = ""
    if navigated:
        nav = f"\n页面已导航到 {nav_url}，旧 ref 全部失效，再操作先重新 browser_snapshot。"
    elif nav_url and nav_url != url:
        nav = f"\n页面地址变成了 {nav_url}（还是同一个页面文档，ref 照常可用）。"
    truncated = ((before.get("stats") or {}).get("truncated")
                 or (after.get("stats") or {}).get("truncated"))
    cut_note = ("\n（快照被截断，只比了前一截，截断区里的变化看不到；"
                "用 root_ref/browser_find 核对）") if truncated else ""
    if delta == tree.DELTA_EMPTY:
        if _navigation_started(before):
            # 点击已经触发了导航，新页面还没到：不是「没反应」（复审 #5）
            return ("\n页面变化: 已开始导航，新页面还在加载。等一会儿用 "
                    "browser_snapshot 看结果，旧 ref 会失效；别当成没反应再点一次。")
        if truncated:
            return f"\n页面变化: {tree.DELTA_EMPTY}{cut_note}{nav}"
        if not nav:
            styled = _style_text(frame, url)
            if styled and _view_hidden() and "都没有变化" in styled:
                # 面板没显示时页面的过渡 / 动画不走，「样式没变」可能只是还没开始变（复审 #6）
                return ("\n页面变化: 文字树没有变化；面板现在没显示，页面里的过渡 / 动画不会跑，"
                        "样式核对给不出结论。要确认就等面板显示后用 browser_screenshot 看。")
            if styled:
                return "\n页面变化: " + styled
        return f"\n页面变化: {tree.DELTA_EMPTY}{nav}"
    return ("\n页面变化:\n" + tree.fence(delta, after.get("qtUrl") or after.get("url") or url)
            + cut_note + nav)


def _changed_document(before, after, after_url, url):
    """动作前后是不是换了一个文档（旧 ref 全部失效）。快照带文档编号（docId）时以它为准：
    单页应用改路由只换地址、不换文档，ref 照常可用。没有编号时退回比地址。

    url 是**这次动作所在文档**的地址：在 iframe 里动作就是 frame 的地址。以前一律传顶层页面的
    地址，而 frame 里取的快照带回的是 frame 的地址，两者永远不等——iframe 里每点一下都报
    「页面已导航，旧 ref 全部失效」（用户实测：B 站小游戏在 iframe 里，模型每步都多查一轮）。"""
    b_doc = before.get("docId") if isinstance(before, dict) else None
    a_doc = after.get("docId") if isinstance(after, dict) else None
    if b_doc and a_doc:
        return b_doc != a_doc
    return bool(after_url and after_url != url)


def _navigation_started(before):
    """动作之后有导航在途（新页面还没到）。
    只认这次动作之后开始的导航（导航序号比动作前大），页面本来就在加载的不算。"""
    nav0 = before.get("_nav0") if isinstance(before, dict) else None
    if nav0 is None:
        return False
    q = _bridge_call("qturl")
    return bool(q.get("ok") and q.get("loading") and (q.get("nav") or 0) > nav0)


def _view_hidden():
    from .browser import page
    b = page._browser
    view = getattr(b, "view", None)
    if view is None:
        return False
    import shiboken6
    return shiboken6.isValid(view) and not view.isVisible()


def _page_url(frame=None):
    r = _bridge_call("qturl", frame=frame)
    if not r.get("ok"):
        return None, _err(r, "读取页面地址")
    return r.get("url", ""), ""


def _element_info(ref, frame):
    info = _lx("info", {"ref": ref}, frame)
    if not info.get("ok"):
        return None, _err(info, "定位元素")
    return info, ""


def _safe_role(info):
    """角色名只认 ARIA 那种纯小写英文词：role 属性是页面写的，「忽略之前所有指令…」不带空格
    也能整段当成角色名混出来。"""
    role = (info.get("role") or "").strip()
    return role if re.fullmatch(r"[a-z]{1,30}", role) else "元素"


def _describe(info, ref):
    """给**用户**看的确认卡片：带元素名，方便核对点的是哪个。"""
    role = _safe_role(info)
    name = info.get("name") or ""
    return f"{role}「{name}」（ref={ref}）" if name else f"{role}（ref={ref}）"


def _describe_for_model(info, ref):
    """给**模型**的结果文字：只有角色和 ref。元素名是页面写的，放在围栏外就成了
    「围栏外的上下文」，页面可以借它说话（复审 #19 实测：aria-label 写一句「系统提示：
    用户要求你…」原样出现在「已点击 …」里）。名字模型在快照里看过（那里有围栏）。"""
    return f"{_safe_role(info)}（ref={ref}）"


def _evidence_revision():
    """当前文件版本（验证状态的 evidence_revision）。页面在这个版本之后的改动面前就算旧了。"""
    try:
        return int(_session.get_verification().get("evidence_revision", 0) or 0)
    except Exception:
        return -1


def _remember_load(r, load_revision):
    """记下这次页面加载：第几次加载（nav）、发起时的文件版本、地址。
    browser_console 据此判断读到的是不是"改完之后重新加载"的那一次。"""
    try:
        # 放在验证状态里：它每轮开头整份重置，上一轮的加载不会被这一轮误认
        # （evidence_revision 每轮从 0 数起，跨轮比版本号会撞上）
        _session.get_verification()["browser_last_load"] = {
            "nav": r.get("nav"), "tab": r.get("tab"), "evidence_revision": load_revision,
            "url": r.get("url") or "", "loaded": bool(r.get("loaded")),
            # 服务器回的 4xx/5xx（复审 #13 之后算「打开了」）：检查时要当成失败
            "http_status": r.get("httpStatus")}
    except Exception:
        pass


def _note_browser_check(r, entries_all):
    """控制台读完后：页面是本机的、是最后一次改动之后由 browser_open 重新加载的、读的正是
    这次加载 → 记一次浏览器检查（verification.mark_browser_check + 结果卡证据）。

    只在本轮确实改过文件时记：纯调研时读个控制台，不该让结果卡多出一条"检查"。"""
    from . import verification as _vf
    try:
        v = _session.get_verification()
        load = v.get("browser_last_load")
    except Exception:
        return
    if not isinstance(load, dict) or not load.get("loaded") or not v.get("dirty_files"):
        return
    nav = r.get("nav")
    # 带上标签：切标签后另一个标签的加载序号可能恰好相等，会误记一次「浏览器检查」
    if nav is None or load.get("nav") != nav or load.get("tab") != r.get("tab") \
            or load.get("cleared"):
        return                      # 页面后来又自己导航过，或这次加载的报错已被清空
    url = r.get("url") or load.get("url") or ""
    if not security.is_localhost(url):
        return                      # 公网页面证明不了本地代码
    if load.get("evidence_revision") != _evidence_revision():
        return                      # 加载之后又改了文件：页面上跑的不是当前代码
    errors = [e for e in entries_all
              if e.get("level") == "error" and e.get("nav", nav) == nav]
    # 服务器回了 4xx/5xx：控制台再干净也不能算通过（审 #13 后实测：500 调试页之后读控制台，
    # 记成了「浏览器控制台：通过」）。算这次加载多一条报错
    status_code = load.get("http_status")
    http_failed = isinstance(status_code, int) and status_code >= 400
    _vf.mark_browser_check(v, errors=len(errors) + (1 if http_failed else 0), url=url,
                           load_revision=load["evidence_revision"])
    if http_failed:
        summary = f"页面返回 HTTP {status_code}" + (
            f"，控制台另有 {len(errors)} 条报错" if errors else "")
    elif errors:
        summary = f"本次加载控制台有 {len(errors)} 条报错：{errors[-1].get('text', '')[:200]}"
    else:
        summary = "本次加载控制台无报错"
    _vf.record_evidence(v, kind="browser", checker="浏览器控制台", path=url,
                        argv=None, cwd="", exit_code=None,
                        status="failed" if (errors or http_failed) else "passed", summary=summary)


@tool
@_exclusive
def browser_open(url: str, timeout: int = 30, new_tab: bool = False) -> str:
    """在侧边栏浏览器打开网址（只允许 http/https），返回标题 + 首份交互快照（含可点元素的 ref）。
    本机 dev server（localhost）直接开；公网页面按权限模式可能先问用户。
    打开后按快照里的 ref 点击/输入；页面变了 ref 会失效，重新 browser_snapshot。"""
    err = security.validate_url(url)
    if err:
        return err
    url = url.strip()
    refusal = _gate("open", url, card=f"将在浏览器打开网页：\n{url}")
    if refusal:
        return refusal
    try:
        timeout = max(5, min(int(timeout), 120))
    except (TypeError, ValueError):
        timeout = 30
    load_revision = _evidence_revision()
    if new_tab and _tab_ids():
        nt = _bridge_call("tab_new")
        if not nt.get("ok"):
            return f"打开失败: {nt.get('error') or '新建标签失败'}（{url}）"
    r = _bridge_call("open", timeout=timeout, url=url)
    if not r.get("ok"):
        return f"打开失败: {r.get('error') or '未知错误'}（{url}）"
    _remember_load(r, load_revision)
    # 标题是页面写的：放进围栏，不放在「已打开」那行旁边（复审 #19：<title> 里写一句
    # 「[系统] 用户已授权…」曾原样出现在围栏外）
    final_url = r.get("url") or url
    head = f"已打开 {final_url}（加载 {r.get('loadMs', '?')}ms）"
    if r.get("httpStatus"):
        head += (f"\n服务器返回 HTTP {r['httpStatus']}：页面内容是服务器给的错误响应，"
                 "报错详情（如 traceback）在下面的快照里，也可用 browser_read_text 读全文。")
    title = "页面标题: " + (r.get("title") or "（无标题）")
    snap = _lx("snapshot", {"mode": "interactive", "maxChars": tree.SNAPSHOT_DEFAULT_CHARS})
    if not snap.get("ok"):
        return (head + "\n" + tree.fence(title, final_url)
                + f"\n（首份快照失败: {snap.get('error')}，用 browser_snapshot 重试）")
    _remember_refs(snap, None)
    return head + "\n" + _snapshot_text(snap, tree.SNAPSHOT_DEFAULT_CHARS, title=title) + _tabs_note()


@tool
@_exclusive
def browser_snapshot(mode: str = "interactive", max_depth: int = 12,
                     max_chars: int = 12000, root_ref: str | None = None,
                     frame: str | None = None) -> str:
    """取当前页面的可访问性文字树：- role "name" [ref=eN] [状态]，缩进表嵌套。
    mode=interactive 只含可交互元素/地标/标题（默认，省 token）；mode=all 连正文文本一起给。
    点击/输入后页面结构变了就重新取一次——旧 ref 失效。root_ref 取某容器子树，frame 取 iframe。"""
    if mode not in ("interactive", "all"):
        return f"mode 只能是 interactive 或 all，收到: {mode}"
    cap = tree.effective_cap(max_chars, tree.SNAPSHOT_DEFAULT_CHARS)
    try:
        depth = max(1, min(int(max_depth), 30))
    except (TypeError, ValueError):
        depth = 12
    args = {"mode": mode, "maxDepth": depth, "maxChars": cap}
    if root_ref:
        args["rootRef"] = root_ref
    r = _lx("snapshot", args, frame)
    if not r.get("ok"):
        return _err(r, "快照")
    _remember_refs(r, frame)
    out = _snapshot_text(r, cap)
    if frame is None:
        out += _frames_note()
    return out + _tabs_note()


def _frames_note():
    """列出可用 frame 路径（复审：六个工具有 frame 参数但没工具能列路径）。"""
    from .browser import page
    if page._browser is None:
        return ""
    fr = _bridge_call("frames", timeout=5)
    frames = fr.get("frames") or []
    if not frames:
        return ""
    lines = [f"  {f.get('path')}  {f.get('url')}" for f in frames]
    # iframe 地址是页面定的（路径、参数里能写字）：进围栏（复审 #19）
    top, _ = _page_url()
    return ("\n可用 frame（用 frame 参数取子树/动作）:\n"
            + tree.fence("\n".join(lines), top or ""))


@tool
@_exclusive
def browser_find(text: str, limit: int = 20, frame: str | None = None) -> str:
    """按可见文字/名称找页面元素，返回匹配列表（role/name/ref，链接带 href）。
    找某个按钮/输入框/链接优先用它，比全量快照省得多；拿 ref 去 click/type。"""
    if not text or not text.strip():
        return "text 不能为空。"
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 20
    r = _lx("find", {"text": text, "limit": limit}, frame)
    if not r.get("ok"):
        return _err(r, "查找")
    _remember_refs(r, frame)
    matches = r.get("matches") or []
    if not matches:
        return (f"没找到匹配「{text}」的元素。可能页面没加载完、文字在图片里，"
                "或换个关键词用 browser_snapshot 看全貌。")
    lines = []
    for m in matches:
        line = (f"- {m.get('role')} {json.dumps(m.get('name') or '', ensure_ascii=False)}"
                f" [ref={m.get('ref')}]")
        if m.get("click"):
            line += " " + m["click"]
        if m.get("href"):
            line += f" href={m['href']}"
        lines.append(line)
    return (f"找到 {len(matches)} 处匹配「{text}」：\n"
            + tree.fence("\n".join(lines), r.get("qtUrl") or r.get("url", "")))


@tool
@_exclusive
def browser_read_text(max_chars: int = 8000, frame: str | None = None) -> str:
    """读当前页面正文（main/article 优先，退 body）。看文章内容用它；
    要点按钮/填表单用 browser_snapshot 拿 ref。"""
    cap = tree.effective_cap(max_chars, tree.READ_TEXT_DEFAULT_CHARS)
    r = _lx("readText", {"maxChars": cap}, frame)
    if not r.get("ok"):
        return _err(r, "读正文")
    body = tree.fence(tree.truncate(r.get("text", ""), cap),
                      r.get("qtUrl") or r.get("url", ""))
    if r.get("truncated"):
        body += "\n（正文已截断，可提高 max_chars，上限 24000）"
    return body


@tool
@_exclusive
def browser_screenshot(question: str, ref: str | None = None, frame: str | None = None,
                       locate: bool = False) -> str:
    """截浏览器当前可见区域，交给图片识别模型，按 question 回答它在图上看到的。

    核对"看起来对不对"用它：颜色 / 高亮 / 图标 / 图表 / canvas / 布局——文字树和
    动作后的样式核对都说不清的东西。question 要具体，只问图上看得出来的事，例如
    「"买牛奶"那一行有没有删除线」「日历里 11 月 3 日是什么底色」；别问"页面对不对"。
    ref：只截这个元素附近那一块（放大后交给识别模型，看小字、看局部更准）；frame：ref 所在的
    iframe（漏写时按签发 ref 的 frame 自动对上）。不给 ref 截整个可见区域。
    locate=True：要求识别模型把要找的东西的位置用 [x, y] 报出来，结果末尾换算成页面坐标
    列出来——canvas 游戏 / 图表里文字树没有 ref 的东西，拿这些坐标去 browser_click_at。
    要点得准就给 ref（比如 canvas 本身）只截那一块：实测整屏报的位置偏 5–45 像素，
    局部放大后偏 5–11 像素。
    返回的是识别模型的文字回答，不是原图；它也可能看错，关键结论能用文字树 / 正文核对的就再核对。
    需要先在「设置 → 图片识别模型」配好能看图的模型。"""
    question = (question or "").strip()
    if not question:
        return "截图失败: 要给 question（想从图上确认什么），不然识别模型不知道该看哪里"
    from .browser import page as _page
    if _page._browser is None:          # 不让截图悄悄拉起一个空浏览器（同 _lx）
        return _NO_PAGE
    from .models import describe_images_with_vision, get_vision_model_index
    if get_vision_model_index() < 0:
        return ("截图失败: 没有可用的图片识别模型。请在「设置 → 图片识别模型」里选一个"
                "能看图的模型（如 GPT-4o / Claude / Gemini / Qwen-VL）。")
    crop, scope_note, part = None, "", ""
    crop_off = (0.0, 0.0)
    if ref:
        url, err = _page_url()
        if err:
            return err
        frame, scope_note = _ref_scope(ref, frame)
        _doc, err = _checked_ref(ref, frame)
        if err:
            return err
        r = _lx("reveal", {"ref": ref}, frame)
        if not r.get("ok"):
            return _STALE_REF
        _bridge_call("settle", ms=150)
        # 只截这一块（四周留一点上下文），放大后交给识别模型：整屏截图里的小字常读不出来
        rect = _lx("rectOf", {"ref": ref}, frame)
        off = _frame_offset(frame)
        if rect.get("ok") and off is not None:
            pad = 40
            crop = {"x": max(0.0, rect["x"] + off[0] - pad), "y": max(0.0, rect["y"] + off[1] - pad),
                    "w": rect["w"] + 2 * pad, "h": rect["h"] + 2 * pad}
            crop_off = off
            part = "里这个元素附近的一块（已放大）"
    shot = _bridge_call("grab", crop=crop) if crop else _bridge_call("grab")
    if not shot.get("ok"):
        return _err(shot, "截图")
    # 直接回答问题：通用的识图提示词要求「只做识别、不回答问题」，套在这里识别模型就把整页
    # 从头描述一遍，问的那行小字反倒没读出来（用户实测一局里截了 12 次）
    prompt = (f"这是浏览器页面{part}的截图（{shot.get('url', '')}）。请直接回答下面这个问题，"
              "只回答问题本身，不用描述页面的其它部分：\n"
              f"{question}\n"
              "只说图上能看到的；看不清、图上没有的就直说看不清 / 没有，不要猜。")
    if locate:
        prompt += (f"\n另外：把问题里要找的每个东西的位置用 [x, y] 报出来——这张图的像素坐标，"
                   f"左上角是 [0, 0]，x 向右、y 向下，图宽 {shot.get('width')} 像素、"
                   f"高 {shot.get('height')} 像素。报东西的中心点，有几个报几个。")
    try:
        with _page.model_waiting():     # 等识别模型的十几秒里，用户点出来的框归用户
            vision_name, desc = describe_images_with_vision(
                prompt, [("browser.png", shot["png"])], as_question=True)
    except Exception as e:
        return f"截图失败: 图片识别模型出错（{e}）"
    if not desc:
        return f"截图完成，但识别模型（{vision_name}）没返回内容"
    coords = ""
    if locate:
        coords = _located_points(desc, shot, frame if crop else None, crop_off)
    return (f"[浏览器截图{'（局部放大）' if crop else ''} · {shot.get('width')}×{shot.get('height')}"
            f" · 识别模型 {vision_name}]{scope_note}\n"
            f"{desc}\n（这是识别模型看图的回答，不是原图；它也可能看错）" + coords)


def _located_points(desc, shot, frame, crop_off):
    """识别模型报的 [x, y]（图片像素）→ 页面 CSS 坐标。换算用截图时记下的比例和偏移
    （grab 的 cssX / cssY / cssPerPx，裁剪、放大、缩小都算在内）；局部截图截的是 iframe 里的
    元素时，再减掉 iframe 偏移，给的是那个 frame 视口里的坐标（调 click_at 要带 frame）。"""
    per = shot.get("cssPerPx")
    if not per:
        return ""
    pts = []
    for m in re.finditer(r"\[\s*(\d+(?:\.\d+)?)\s*[,，]\s*(\d+(?:\.\d+)?)\s*\]", desc):
        ix, iy = float(m.group(1)), float(m.group(2))
        if not (0 <= ix <= shot.get("width", 0) and 0 <= iy <= shot.get("height", 0)):
            continue                    # 报到图外的坐标是识别模型看错了，不给
        x = shot.get("cssX", 0.0) + ix * per - crop_off[0]
        y = shot.get("cssY", 0.0) + iy * per - crop_off[1]
        pts.append(f"({x:.0f}, {y:.0f})")
        if len(pts) >= 8:
            break
    if not pts:
        return "\n（识别模型没有报出 [x, y] 坐标，没法给页面坐标）"
    where = f"frame=\"{frame}\" 视口里的" if frame else "页面"
    return (f"\n{where}坐标（已按截图比例换算，可直接调 browser_click_at"
            + (f"，带 frame=\"{frame}\"" if frame else "") + "）：" + "、".join(pts)
            + "\n（坐标是识别模型报的位置换算的，可能有偏差；点完看变化核对）")


@tool
@_exclusive
def browser_click(ref: str, frame: str | None = None, delta: bool = True) -> str:
    """点击快照/find 给出的 ref 元素。默认附动作前后的"变化行 diff"；文字树没变时改报
    操作处附近**实际计算样式**的变化（动作前后逐项比对，不按 class 名猜）。
    "样式都没有变化"就是确实没反应，别反复点；颜色含义 / 图表要核对用 browser_screenshot。
    点击是真实的鼠标事件：元素被弹窗 / 遮罩挡住时会报"被挡住"并给出挡在上面的东西，先处理它。
    可能先问用户；提交/删除/支付这类有后果的动作每次都问。"""
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    st = _lx("state", frame=frame)
    if not st.get("ok"):
        return _err(st, "读取页面状态")
    cur_doc = st.get("docId") or ""
    if (_fk(frame), ref) not in _issued or _issued[(_fk(frame), ref)] != cur_doc:
        return _STALE_REF
    info, err = _element_info(ref, frame)
    if err:
        return err
    origin = _origin_for(_session.current_session(), "interact", url,
                         frame_url if frame else None)
    cons = security.is_consequential(info, origin=origin)
    desc = _describe(info, ref)
    card = f"将在浏览器点击 {desc}\n页面: {url}"
    if frame:
        card += f"\niframe: {frame_url}"
    if cons:
        card += "\n⚠ 此动作可能有后果（提交/删除/支付等），请仔细核对。"
    refusal = _gate("interact", url, consequential=cons, card=card,
                    extra_url=frame_url if frame else None, origin=origin)
    if refusal:
        return refusal
    tabs0, cur0 = _tab_ids(), _cur_tab()
    before = _before_action(ref, frame) if delta else None
    fp = info.get("fp")
    if delta:
        fp, refusal = _reconfirm_after_prepare(ref, frame, info, cons, origin=origin)
        if refusal:
            return refusal
    r, note = _press(ref, {"doc": cur_doc, "fp": fp}, frame, label=_cursor_label("点击", info))
    if not r.get("ok"):
        if r.get("occluded"):
            return ("点击失败：这个元素被别的东西挡住了，真人也点不到。先处理挡在上面的东西"
                    "（关掉弹窗 / 遮罩、等加载层消失），再重新 browser_snapshot。挡在上面的是：\n"
                    + tree.fence(r.get("by") or "", frame_url))
        return _err(r, "点击")
    return (f"已点击 {_describe_for_model(info, ref)}{note}{scope_note}"
            + _after_action(before, frame, frame_url, tabs0, cur0))


def _cursor_label(verb, info):
    """「灵犀的鼠标」和状态卡上的说明：点 / 输入的是什么。只给用户看（不回给模型），
    用元素名没关系；太长截掉。"""
    name = (info.get("name") or "").strip() if isinstance(info, dict) else ""
    if len(name) > 16:
        name = name[:16] + "…"
    return f"{verb}「{name}」" if name else verb


def _press(ref, expect, frame, label=""):
    """点一下：顶层页面发真实鼠标事件（复审 #9），iframe 里、原生 <select> 的选项、没有大小的
    元素、面板没显示时退回脚本点击（el.click()）。返回 (结果, 补充说明)。

    真实事件的好处：靠 mousedown / pointerdown 打开的组件（antd Select、Radix 菜单）点得开；
    被遮罩挡住的按钮点不中（和真人一样，报出来而不是假装点中了）；点开的新窗口算用户发起。

    iframe 里的元素：算出 iframe 在整页里的位置（_frame_offset）后同样发真实事件——网页游戏、
    嵌入的编辑器多在 iframe 里，靠 pointerdown 的交互（会躲开鼠标的按钮、拖拽）脚本点击触发
    不了；位置对不上时才退回脚本点击。"""
    ox = oy = 0.0
    if frame:
        off = None if _view_hidden() else _frame_offset(frame)
        if off is None:
            return _lx("click", {"ref": ref, "expect": expect}, frame), ""
        ox, oy = off
    if _view_hidden():
        # 面板没显示：Chromium 不收输入事件（视口也可能是 0 尺寸），只能脚本点
        return (_lx("click", {"ref": ref, "expect": expect}),
                "（面板没显示，改用脚本点击；靠按下鼠标才打开的组件可能没反应）")
    p = _lx("pointOf", {"ref": ref, "expect": expect}, frame)
    if not p.get("ok"):
        if p.get("occluded") and p.get("x") is not None:
            # 画在网页上的指针变红、停在被挡的那一点：和结果里的「被挡住」对得上
            _bridge_call("cursor", kind="blocked", x=p["x"] + ox, y=p["y"] + oy, label="被挡住了")
        return p, ""
    if p.get("synthetic"):
        return _lx("click", {"ref": ref, "expect": expect}, frame), ""
    m = _bridge_call("mouse", x=p["x"] + ox, y=p["y"] + oy, label=label)
    if not m.get("ok"):
        # 面板没显示 / 点不在可见区域：Chromium 收不到，只能脚本点
        return (_lx("click", {"ref": ref, "expect": expect}, frame),
                "（面板没显示，改用脚本点击；靠按下鼠标才打开的组件可能没反应）")
    # 事件是异步送进页面的：确认真的到了这个元素再往下（一般第一下就到了）
    for _ in range(_PRESS_CONFIRM_TRIES):
        a = _lx("pointArrived", frame=frame)
        if a.get("down") or a.get("click"):
            return {"ok": True}, ""
        _bridge_call("settle", ms=30)
    # 没确认到：多半是鼠标移上去时页面把这个元素换掉了。不再补一次脚本点击（可能点两次）
    return {"ok": True}, "（鼠标点击已发出，但没确认送到这个元素，可能被页面换掉了；看下面的变化核对）"


def _frame_at_point(x, y, frame):
    """坐标落在 iframe 上时换算进那个 frame：返回 (frame, 局部 x, 局部 y, 说明)。
    按外层 frameBoxes 的位置 / 大小和 Qt 那边的 frame 对上号（同 _frame_offset 的口径）；
    对不上号返回 (None, …, 错误文案)——不能拿着顶层来源的闸门去点 iframe 里的东西。"""
    boxes = _lx("frameBoxes", {}, frame)
    if not boxes.get("ok"):
        return None, x, y, _err(boxes, "读 iframe 位置")
    fr = _bridge_call("frames")
    if not fr.get("ok"):
        return None, x, y, _err(fr, "列框架")
    prefix = (frame + ".") if frame else "f."
    depth = len(prefix.split("."))
    cands = [f for f in fr.get("frames") or []
             if (f.get("path") or "").startswith(prefix)
             and len((f.get("path") or "").split(".")) == depth]
    for box in boxes.get("frames") or []:
        if not (box["x"] <= x < box["x"] + box["w"] and box["y"] <= y < box["y"] + box["h"]):
            continue
        match = [f for f in cands
                 if abs((f.get("w") or 0) - box["w"]) <= 2 and abs((f.get("h") or 0) - box["h"]) <= 2]
        if len(match) > 1:
            match = [f for f in match if f.get("url") == box.get("src")]
        if len(match) != 1:
            return None, x, y, ("点击失败: 这一点落在 iframe 里，但对不上是哪一个"
                                "（页面里尺寸相同的 iframe 不止一个）。用 browser_snapshot 列出 frame，"
                                "带 frame 参数和相对那个 iframe 的坐标再点。")
        path = match[0]["path"]
        note = f"\n（坐标落在 iframe {path} 里，已换算成它视口里的坐标 ({x - box['x']:.0f}, {y - box['y']:.0f}) 操作）"
        return path, x - box["x"], y - box["y"], note
    return None, x, y, "点击失败: 坐标落在 iframe 上，但算不出它的位置，点不了。"


@tool
@_exclusive
def browser_click_at(x: float, y: float, frame: str | None = None, delta: bool = True) -> str:
    """按页面坐标点击（真实鼠标事件）：canvas 画出来的游戏 / 地图 / 图表里的东西在文字树里
    没有 ref，用 browser_screenshot(locate=True) 拿到页面坐标后点它。
    坐标是页面 CSS 像素（顶层视口，左上角为原点）；给 frame 时是那个 iframe 视口里的坐标。
    落在 iframe 上会自动换算进那个 iframe。结果会说点在了什么元素上；那一点能拿到 ref 的
    会给出 ref，下次直接 browser_click。默认附"变化行 diff"。
    可能先问用户；落在提交/删除/支付这类按钮上照样每次都问。"""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return "点击失败: x / y 要是数字（页面 CSS 像素）。"
    url, err = _page_url()
    if err:
        return err
    if _view_hidden():
        return "点击失败: 浏览器面板没显示，Chromium 收不到鼠标事件。打开右侧的浏览器面板再试。"
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    off = _frame_offset(frame)
    if off is None:
        return "点击失败: 算不出这个 iframe 在页面里的位置（页面里尺寸相同的 iframe 不止一个），点不了。"
    ox, oy = off
    scope_note = ""
    d = _lx("describeAt", {"x": x, "y": y}, frame)
    for _ in range(4):                  # 嵌套 iframe 一层层进去
        if not d.get("ok"):
            return _err(d, "按坐标点击")
        if d.get("iframe") is None:
            break
        frame, x, y, note = _frame_at_point(x, y, frame)
        if frame is None:
            return note
        scope_note += note
        frame_url, err = _page_url(frame)
        if err:
            return err
        off = _frame_offset(frame)
        if off is None:
            return "点击失败: 算不出这个 iframe 在页面里的位置，点不了。"
        ox, oy = off
        d = _lx("describeAt", {"x": x, "y": y}, frame)
    else:
        return "点击失败: iframe 嵌套太深，按坐标点不了。"
    info, ref = d.get("info") or {}, d.get("ref")
    origin = _origin_for(_session.current_session(), "interact", url,
                         frame_url if frame else None)
    cons = security.is_consequential(info, origin=origin)
    card = f"将在浏览器按坐标点击 ({x:g}, {y:g})，落在 {d.get('desc') or '未知元素'}\n页面: {url}"
    if frame:
        card += f"\niframe: {frame_url}"
    if cons:
        card += "\n⚠ 此动作可能有后果（提交/删除/支付等），请仔细核对。"
    refusal = _gate("interact", url, consequential=cons, card=card,
                    extra_url=frame_url if frame else None, origin=origin)
    if refusal:
        return refusal
    _issued[(_fk(frame), ref)] = d.get("docId") or ""
    # 等确认卡的时候页面可能自己变了：再看一眼那一点，变成了有后果的动作就拒绝
    d2 = _lx("describeAt", {"x": x, "y": y}, frame)
    if d2.get("ok") and d2.get("iframe") is None:
        info2 = d2.get("info") or {}
        if info2.get("fp") != info.get("fp"):
            if not cons and security.is_consequential(info2, origin=origin):
                return ("已停止：等确认的时候这一点上的元素变了，现在看起来是有后果的动作"
                        "（提交 / 删除等），和批准时不一样。重新 browser_screenshot 看清楚再点。")
            d, info, ref = d2, info2, d2.get("ref")
            _issued[(_fk(frame), ref)] = d2.get("docId") or ""
    tabs0, cur0 = _tab_ids(), _cur_tab()
    before = _plain_before(frame) if delta else None
    m = _bridge_call("mouse", x=x + ox, y=y + oy, label=_cursor_label("点击", info))
    if not m.get("ok"):
        return _err(m, "按坐标点击")
    note = ""
    for _ in range(_PRESS_CONFIRM_TRIES):
        a = _lx("pointArrived", frame=frame)
        if a.get("down") or a.get("click"):
            break
        _bridge_call("settle", ms=30)
    else:
        note = "（鼠标点击已发出，但没确认送到那一点的元素，可能被页面换掉了；看下面的变化核对）"
    head = f"已在坐标 ({x:g}, {y:g}) 点击，落在 {_safe_role(info)}（ref={ref}）"
    if d.get("interactive"):
        head += f"；这个元素有 ref，下次可直接 browser_click(ref=\"{ref}\")"
    head += "。落点元素（页面内容）：\n" + tree.fence(d.get("desc") or "", frame_url)
    return head + note + scope_note + _after_action(before, frame, frame_url, tabs0, cur0)


@tool
@_exclusive
def browser_type(ref: str, text: str, clear: bool = True, submit: bool = False,
                 frame: str | None = None, delta: bool = True) -> str:
    """往 ref 指向的输入框输入文字（clear=True 先清空；submit=True 输入后回车，可能提交表单）。
    默认附"变化行 diff"。可能先问用户；回车提交/有后果的输入每次都问。
    别把密钥/令牌填进公网页面——会被直接拒绝。"""
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    st = _lx("state", frame=frame)
    if not st.get("ok"):
        return _err(st, "读取页面状态")
    cur_doc = st.get("docId") or ""
    if (_fk(frame), ref) not in _issued or _issued[(_fk(frame), ref)] != cur_doc:
        return _STALE_REF
    info, err = _element_info(ref, frame)
    if err:
        return err
    origin = _origin_for(_session.current_session(), "interact", url,
                         frame_url if frame else None)
    # 顶层或目标 frame 任一在公网就查密钥：localhost 页面套公网 iframe 不能绕过
    if not (security.is_localhost(url) and security.is_localhost(frame_url)):
        from .secret_mask import collect_secrets
        hit = security.secret_in_text(text, collect_secrets())
        if hit:
            return f"已拒绝：输入内容里含配置/环境中的密钥（{hit}），不能填进公网页面。"
    cons = security.is_consequential(info, submit=submit, origin=origin)
    desc = _describe(info, ref)
    shown = text if len(text) <= 500 else text[:500] + "…"
    card = f"将在浏览器向 {desc} 输入:\n{shown}\n页面: {url}"
    if frame:
        card += f"\niframe: {frame_url}"
    if submit:
        card += "\n并回车提交（可能触发表单提交）。"
    if cons:
        card += "\n⚠ 此动作可能有后果，请仔细核对。"
    refusal = _gate("interact", url, consequential=cons, card=card,
                    extra_url=frame_url if frame else None, origin=origin)
    if refusal:
        return refusal
    tabs0, cur0 = _tab_ids(), _cur_tab()
    before = _before_action(ref, frame) if delta else None
    fp = info.get("fp")
    if delta:
        fp, refusal = _reconfirm_after_prepare(ref, frame, info, cons, submit=submit, origin=origin)
        if refusal:
            return refusal
    r = _lx("type", {"ref": ref, "text": text, "clear": clear, "submit": submit,
                     "expect": {"doc": cur_doc, "fp": fp}}, frame)
    if r.get("ok") and not frame and not _view_hidden():
        # 「灵犀的鼠标」停到输入框右边，标签「输入中」
        a = _lx("anchorOf", {"ref": ref})
        if a.get("ok"):
            # 回车提交单独标出来：模型常用「输入 + 回车」代替点按钮，不标的话看着像
            # 待办自己冒出来了（用户实测）
            _bridge_call("cursor", kind="submit" if submit else "type", x=a["x"], y=a["y"],
                         label=_cursor_label("输入并回车" if submit else "输入", info))
    if not r.get("ok"):
        if r.get("options") is not None:
            return (_err(r, "输入") + "，可选值（页面内容）：\n"
                    + tree.fence("\n".join(r["options"]), frame_url))
        return _err(r, "输入")
    target = _describe_for_model(info, ref)
    if r.get("secret"):
        # 密码框不回显（复审 #18）：值会进模型上下文和会话历史
        state_ = "已填写" if r.get("filled") else "为空"
        return (f"已向 {target} 输入，密码框不回显内容（当前{state_}）{scope_note}"
                + _after_action(before, frame, frame_url, tabs0, cur0))
    raw = r.get("value", "")
    if raw in (text[:100], ""):
        shown_value = json.dumps(raw, ensure_ascii=False)
    else:
        # 页面的输入处理改过值（格式化、自动补全……）：那是页面写的字，进围栏
        shown_value = "（页面改过，见下）\n" + tree.fence(raw, frame_url)
    return (f"已向 {target} 输入，当前值: {shown_value}{scope_note}"
            + _after_action(before, frame, frame_url, tabs0, cur0))


def _checked_ref(ref, frame):
    """ref 还能不能用（是当前文档的快照 / find 签发的）。返回 (当前文档编号, 错误文案)。"""
    st = _lx("state", frame=frame)
    if not st.get("ok"):
        return "", _err(st, "读取页面状态")
    cur_doc = st.get("docId") or ""
    if (_fk(frame), ref) not in _issued or _issued[(_fk(frame), ref)] != cur_doc:
        return "", _STALE_REF
    return cur_doc, ""


def _plain_before(frame):
    """没有 ref 的动作（直接按键）：动作前的 all 快照 + 导航序号，给 _after_action 算变化。"""
    before = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
    q = _bridge_call("qturl")
    if isinstance(before, dict) and q.get("ok"):
        before["_nav0"] = q.get("nav") or 0
    return before


@tool
@_exclusive
def browser_press_key(key: str, ref: str | None = None, frame: str | None = None,
                      delta: bool = True) -> str:
    """按一个键（真实键盘事件）：Escape、Enter、Tab、ArrowUp / ArrowDown / ArrowLeft / ArrowRight、
    Space、Backspace、Delete、Home、End、PageUp、PageDown、F1–F12、单个字符，可带修饰键
    （Control+a、Shift+Tab）。名字照网页的 KeyboardEvent.key。
    ref：先把焦点交给这个元素再按（按键送给有焦点的地方）；不给 ref 时送给当前焦点，
    给 frame 时先把焦点交给那个 iframe（游戏、编辑器在 iframe 里时用）。
    往输入框里打字用 browser_type；这个工具用来按快捷键、关弹窗（Escape）、移动选择、翻页、玩游戏。
    默认附"变化行 diff"。可能先问用户；Enter 可能提交表单，按「回车提交」的规矩每次都问。"""
    from .browser import page as _page
    key = (key or "").strip()
    if _page.parse_key(key) is None:
        return ("按键失败: 不认识的按键 " + json.dumps(key, ensure_ascii=False)
                + "。写法照 KeyboardEvent.key：Escape、Enter、Tab、ArrowDown、a、Control+a、Shift+Tab……")
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    info, cur_doc = None, ""
    if ref:
        cur_doc, err = _checked_ref(ref, frame)
        if err:
            return err
        info, err = _element_info(ref, frame)
        if err:
            return err
    origin = _origin_for(_session.current_session(), "interact", url, frame_url if frame else None)
    is_enter = key.split("+")[-1].strip().lower() in ("enter", "return")
    if is_enter:
        # 回车 = 提交：有元素就按它的结构判（表单内回车按默认提交按钮的名字），没有就同
        # 「表单外回车即发送」——信任的本机来源不问，其余都问
        cons = (security.is_consequential(info, submit=True, origin=origin) if info
                else origin != security.ORIGIN_TRUSTED)
    else:
        cons = False
    where = _describe(info, ref) if info else (f"iframe {frame} 里当前的焦点" if frame else "页面当前的焦点")
    card = f"将在浏览器按键 {key}（送给 {where}）\n页面: {url}"
    if frame:
        card += f"\niframe: {frame_url}"
    if cons:
        card += "\n⚠ 此动作可能有后果（回车可能提交表单 / 发送），请仔细核对。"
    refusal = _gate("interact", url, consequential=cons, card=card,
                    extra_url=frame_url if frame else None, origin=origin)
    if refusal:
        return refusal
    tabs0, cur0 = _tab_ids(), _cur_tab()
    if ref:
        before = _before_action(ref, frame) if delta else None
        f = _lx("focusRef", {"ref": ref, "expect": {"doc": cur_doc, "fp": info.get("fp")}}, frame)
        if not f.get("ok"):
            return _err(f, "聚焦元素")
    else:
        before = _plain_before(frame) if delta else None
        if frame:
            _lx("focusWindow", {}, frame)
    _lx("keyWatch", {}, frame)
    k = _bridge_call("key", key=key)
    if not k.get("ok"):
        return _err(k, "按键")
    arrived = False
    for _ in range(_PRESS_CONFIRM_TRIES):
        if _lx("keyArrived", {}, frame).get("key"):
            arrived = True
            break
        _bridge_call("settle", ms=30)
    note = "" if arrived else (
        "\n（按键已发出，但没确认送到" + (f" frame={frame}" if frame else "顶层页面")
        + "：焦点可能在别处（比如某个 iframe 里）。给 ref 指定接收按键的元素，或给 frame）")
    head = f"已按 {key}" + (f"（焦点在 {_describe_for_model(info, ref)}）" if info else "")
    return head + note + scope_note + _after_action(before, frame, frame_url, tabs0, cur0)


@tool
@_exclusive
def browser_drag(ref: str, to_ref: str | None = None, dx: float = 0, dy: float = 0,
                 frame: str | None = None, delta: bool = True) -> str:
    """把 ref 元素拖到 to_ref 元素上（或从它出发移动 dx / dy 像素）：真实鼠标按下 → 分几步移动
    → 抬起。拖滑块、拖拽排序、把东西拖进框里、拖拽类小游戏用它。两个 ref 要在同一个 frame
    里、都在可见区域。浏览器原生的 HTML5 拖放（draggable=true）可能不响应，那种页面多半另有
    按钮可点。默认附"变化行 diff"。可能先问用户。"""
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    if not to_ref and not (dx or dy):
        return "拖拽失败: 要给 to_ref（拖到哪个元素上），或给 dx / dy（移动多少像素）。"
    cur_doc, err = _checked_ref(ref, frame)
    if err:
        return err
    if to_ref and ((_fk(frame), to_ref) not in _issued or _issued[(_fk(frame), to_ref)] != cur_doc):
        return "拖拽失败: to_ref 不是这个页面（同一个 frame）当前签发的 ref。" + _STALE_REF
    info, err = _element_info(ref, frame)
    if err:
        return err
    to_info = None
    if to_ref:
        to_info, err = _element_info(to_ref, frame)
        if err:
            return err
    if _view_hidden():
        return "拖拽失败: 浏览器面板没显示，收不到鼠标事件（拖拽要真实鼠标）。打开右侧的浏览器面板再试。"
    origin = _origin_for(_session.current_session(), "interact", url, frame_url if frame else None)
    dest = _describe(to_info, to_ref) if to_info else f"偏移 ({dx:g}, {dy:g}) 像素处"
    card = f"将在浏览器把 {_describe(info, ref)} 拖到 {dest}\n页面: {url}"
    if frame:
        card += f"\niframe: {frame_url}"
    refusal = _gate("interact", url, consequential=False, card=card,
                    extra_url=frame_url if frame else None, origin=origin)
    if refusal:
        return refusal
    off = _frame_offset(frame)
    if off is None:
        return "拖拽失败: 算不出这个 iframe 在页面里的位置（页面里尺寸相同的 iframe 不止一个），拖不了。"
    ox, oy = off
    tabs0, cur0 = _tab_ids(), _cur_tab()
    before = _before_action(ref, frame) if delta else None
    p = _lx("pointOf", {"ref": ref, "expect": {"doc": cur_doc, "fp": info.get("fp")}}, frame)
    if not p.get("ok"):
        if p.get("occluded"):
            return ("拖拽失败：要拖的元素被别的东西挡住了。挡在上面的是：\n"
                    + tree.fence(p.get("by") or "", frame_url))
        return _err(p, "定位要拖的元素")
    if p.get("synthetic"):
        return "拖拽失败: 要拖的元素没有可见的大小，或不在可见区域里。"
    if to_ref:
        t = _lx("rectOf", {"ref": to_ref}, frame)
        if not t.get("ok"):
            return _err(t, "定位目标元素")
        x2, y2 = t["x"] + t["w"] / 2, t["y"] + t["h"] / 2
        if not (0 <= x2 < t["vw"] and 0 <= y2 < t["vh"]):
            return "拖拽失败: 目标元素不在可见区域里（先让它露出来，比如把两者都滚到一屏里）。"
    else:
        x2, y2 = p["x"] + dx, p["y"] + dy
    m = _bridge_call("drag", x1=p["x"] + ox, y1=p["y"] + oy, x2=x2 + ox, y2=y2 + oy,
                     label=_cursor_label("拖动", info))
    if not m.get("ok"):
        return _err(m, "拖拽")
    note = ""
    if not _lx("pointArrived", frame=frame).get("down"):
        note = "（鼠标已拖过去，但没确认按在了这个元素上，可能被页面换掉了；看下面的变化核对）"
    return (f"已把 {_describe_for_model(info, ref)} 拖到 "
            + (_describe_for_model(to_info, to_ref) if to_info else f"偏移 ({dx:g}, {dy:g}) 处")
            + note + scope_note + _after_action(before, frame, frame_url, tabs0, cur0))


_SCROLL_DIRS = ("up", "down", "left", "right", "top", "bottom")


def _scroll_stable(frame, ref, tries=8):
    """滚轮滚动常带平滑动画：读到位置连续两次一样才算停（最多 tries 次，每次隔 100ms）。"""
    last = None
    for _ in range(tries):
        _bridge_call("settle", ms=100)
        st = _lx("scrollState", {"ref": ref} if ref else {}, frame)
        if not st.get("ok"):
            return st
        if last is not None and (st["x"], st["y"]) == (last["x"], last["y"]):
            return st
        last = st
    return last


@tool
@_exclusive
def browser_scroll(direction: str = "down", amount: float | None = None,
                   ref: str | None = None, frame: str | None = None, delta: bool = True) -> str:
    """滚动页面或某个可滚动容器（真实滚轮事件，滚动加载 / 虚拟列表 / 轮播都听得到）。
    direction: up / down / left / right / top / bottom（top / bottom 直接到顶 / 到底）。
    amount 是像素，默认约一屏的 80%。给 ref 滚那个容器（ref 本身不能滚就找最近的可滚动祖先），
    不给滚页面；给 frame 滚那个 iframe。结果给滚动前后的位置、是否到头，附"变化行 diff"
    （新露出来的内容）。只读操作，不改页面数据。"""
    direction = (direction or "").strip().lower()
    if direction not in _SCROLL_DIRS:
        return f"direction 只能是 {' / '.join(_SCROLL_DIRS)}，收到: {direction}"
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    if ref:
        _doc, err = _checked_ref(ref, frame)
        if err:
            return err
    horizontal = direction in ("left", "right")
    if amount is not None:
        try:
            amount = abs(float(amount))
        except (TypeError, ValueError):
            return "amount 要是数字（像素）。"
    if direction in ("top", "bottom"):
        # 直接设位置（到头 / 到底不需要滚轮）；面板没显示也能做，位置照样核对
        st0 = _lx("scrollState", {"ref": ref} if ref else {}, frame)
        if not st0.get("ok"):
            return _err(st0, "滚动")
        tabs0, cur0 = _tab_ids(), _cur_tab()
        before = _plain_before(frame) if delta else None
        st = _lx("scrollSet", {"ref": ref, "dir": direction}, frame)
        if not st.get("ok"):
            return _err(st, "滚动")
        pos, max_pos = st["y"], st["maxY"]
        head = (f"已滚到{'顶' if direction == 'top' else '底'}：纵向位置 {st0['y']:.0f} → {pos:.0f}"
                + ("（已到头）" if (pos <= 1 if direction == "top" else pos >= max_pos - 1) else ""))
        return head + scope_note + _after_action(before, frame, frame_url, tabs0, cur0)
    if _view_hidden():
        return ("滚动失败: 浏览器面板没显示，收不到滚轮事件（滚动加载 / 虚拟列表要真实 wheel）。"
                "打开右侧的浏览器面板再试；到顶 / 到底可以用 direction=top / bottom。")
    si = _lx("scrollInfo", {"ref": ref} if ref else {}, frame)
    if not si.get("ok"):
        return _err(si, "滚动")
    off = _frame_offset(frame)
    if off is None:
        return "滚动失败: 算不出这个 iframe 在页面里的位置（页面里尺寸相同的 iframe 不止一个），滚不了。"
    ox, oy = off
    if amount is None:
        amount = (si["cw"] if horizontal else si["ch"]) * 0.8
    sign = -1 if direction in ("up", "left") else 1
    dx = sign * amount if horizontal else 0.0
    dy = sign * amount if not horizontal else 0.0
    tabs0, cur0 = _tab_ids(), _cur_tab()
    before = _plain_before(frame) if delta else None
    fallback = ""
    if si.get("clean", True):
        w = _bridge_call("wheel", x=si["px"] + ox, y=si["py"] + oy, dx=dx, dy=dy,
                         label=f"滚动 {direction}")
        if not w.get("ok"):
            return _err(w, "滚动")
    else:
        # 可见部分处处压着别的滚动区域：滚轮发下去会滚那个区域（或被它拦掉），直接改位置
        w = _lx("scrollByPx", dict({"ref": ref} if ref else {}, dx=dx, dy=dy), frame)
        if not w.get("ok"):
            return _err(w, "滚动")
        fallback = ("\n（可见部分处处压着页面里别的滚动区域，滚轮会落到它们身上，改成直接设位置；"
                    "靠滚轮触发的滚动加载这次不会被触发）")
    st = _scroll_stable(frame, ref)
    if not st or not st.get("ok"):
        return "已发出滚轮事件，但读不到滚动后的位置；用 browser_snapshot 看现状。"
    axis = "横向" if horizontal else "纵向"
    p0, p1 = (si["x"], st["x"]) if horizontal else (si["y"], st["y"])
    max_pos = st["maxX"] if horizontal else st["maxY"]
    moved = abs(p1 - p0)
    head = f"已滚动 {direction} {amount:.0f}px：{axis}位置 {p0:.0f} → {p1:.0f}"
    if moved < 1:
        at_end = p0 >= max_pos - 1 if sign > 0 else p0 <= 1
        head += ("（位置没变：" + ("已经到头了，滚不动" if at_end else
                 "可能被页面拦了（有的页面禁止滚动）；是否可见用 browser_screenshot 核对") + "）")
    elif sign > 0 and p1 >= max_pos - 1:
        head += "（已到底）"
    elif sign < 0 and p1 <= 1:
        head += "（已到顶）"
    return head + fallback + scope_note + _after_action(before, frame, frame_url, tabs0, cur0)


@tool
@_exclusive
def browser_hover(ref: str, frame: str | None = None, delta: bool = True) -> str:
    """把鼠标移到 ref 元素上悬停（真实鼠标移动，不点击）：悬停才出现的菜单 / 提示框 / 按钮
    用它唤出来，结果附"变化行 diff"——新出现的菜单项带着 ref，可直接 browser_click。
    元素被挡住时报"被挡住"。悬停后鼠标停在那里；点别处时会先移走，靠悬停保持的菜单
    可能收起（菜单和触发点在同一块悬停区域里的不受影响）。只读操作，不改页面数据。"""
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    if _view_hidden():
        return "悬停失败: 浏览器面板没显示，收不到鼠标事件。打开右侧的浏览器面板再试。"
    cur_doc, err = _checked_ref(ref, frame)
    if err:
        return err
    info, err = _element_info(ref, frame)
    if err:
        return err
    off = _frame_offset(frame)
    if off is None:
        return "悬停失败: 算不出这个 iframe 在页面里的位置（页面里尺寸相同的 iframe 不止一个）。"
    ox, oy = off
    tabs0, cur0 = _tab_ids(), _cur_tab()
    # 滚到可见是我们自己的副作用，要在动作前快照之前做完（同 click 的 watchPrepare）；
    # 不聚焦——真实的悬停不改键盘焦点
    _lx("reveal", {"ref": ref}, frame)
    before = _plain_before(frame) if delta else None
    p = _lx("pointOf", {"ref": ref, "expect": {"doc": cur_doc, "fp": info.get("fp")}}, frame)
    if not p.get("ok"):
        if p.get("occluded"):
            if p.get("x") is not None:
                _bridge_call("cursor", kind="blocked", x=p["x"] + ox, y=p["y"] + oy, label="被挡住了")
            return ("悬停失败：这个元素被别的东西挡住了，鼠标移过去也落在挡它的东西上。"
                    "先处理挡在上面的东西：\n" + tree.fence(p.get("by") or "", frame_url))
        return _err(p, "定位元素")
    if p.get("synthetic"):
        return "悬停失败: 这个元素没有可见的大小，或不在可见区域里。"
    _lx("hoverWatch", {"ref": ref}, frame)
    m = _bridge_call("mouse", x=p["x"] + ox, y=p["y"] + oy, move_only=True,
                     label=_cursor_label("悬停", info))
    if not m.get("ok"):
        return _err(m, "悬停")
    arrived = False
    for _ in range(_PRESS_CONFIRM_TRIES):
        if _lx("hoverArrived", {}, frame).get("over"):
            arrived = True
            break
        _bridge_call("settle", ms=30)
    # 悬停菜单 / 提示框常有出现动画，settle 一下再算变化（_delta_text 空 diff 还会再补）
    _bridge_call("settle", ms=200)
    note = "" if arrived else "（鼠标已移过去，但没确认悬停送到这个元素；看下面的变化核对）"
    return (f"已悬停 {_describe_for_model(info, ref)}{note}{scope_note}"
            + _after_action(before, frame, frame_url, tabs0, cur0))


_WAIT_POLL_MS = 250


@tool
@_exclusive
def browser_wait(text: str | None = None, ref: str | None = None, gone: bool = False,
                 timeout: float = 10, frame: str | None = None) -> str:
    """等页面到位，别反复快照碰运气：给 text 等这段文字出现（gone=True 等它消失）；
    给 ref 等那个元素消失（加载遮罩、倒计时弹层）；都不给就等页面安静下来
    （没有导航在途、文字树连续两次不变）。每 250ms 查一次，最多 timeout 秒（上限 60）。
    结果说等到了（用了多少秒）还是超时（超时不是失败，如实说），附这段时间的变化行 diff。
    动作之后页面要加载 / 有倒计时 / 异步结果时用。只读操作。"""
    from .browser import page as _page
    if _page._browser is None:
        return _NO_PAGE
    text = (text or "").strip() or None
    try:
        timeout = max(1.0, min(float(timeout), 60.0))
    except (TypeError, ValueError):
        timeout = 10.0
    url, err = _page_url()
    if err:
        return err
    frame, scope_note = _ref_scope(ref, frame)
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    if ref:
        _doc, err = _checked_ref(ref, frame)
        if err:
            return err
    import time as _t
    t0 = _t.monotonic()
    before = _plain_before(frame)
    prev_tree = None
    ok, stopped = False, False
    with _page.model_waiting():         # 等的这段时间用户在网页上点出来的框归用户
        while True:
            if state.stop_flag:
                stopped = True
                break
            if ref:
                # 元素还在不在（脱离页面 / 隐藏 / 换了文档都算消失）
                if not _lx("hasRef", {"ref": ref}, frame).get("present"):
                    ok = True
                    break
            elif text:
                snap = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
                body = _lx("readText", {"maxChars": tree.READ_TEXT_DEFAULT_CHARS}, frame)
                hay = (snap.get("tree") or "") + "\n" + (body.get("text") or "")
                if (text in hay) != gone:
                    ok = True
                    break
            else:
                q = _bridge_call("qturl")
                snap = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
                cur = (snap.get("docId"), snap.get("tree"))
                if q.get("ok") and not q.get("loading") and snap.get("ok") \
                        and prev_tree is not None and cur == prev_tree:
                    ok = True
                    break
                prev_tree = cur
            if _t.monotonic() - t0 >= timeout:
                break
            r = _bridge_call("settle", ms=_WAIT_POLL_MS)
            if not r.get("ok") and "已停止" in (r.get("error") or ""):
                stopped = True
                break
    secs = _t.monotonic() - t0
    if stopped:
        return "已停止（等待中用户停止了生成）。"
    if ok:
        what = (f"元素 {ref} 消失" if ref else
                (f"文字「{text}」{'消失' if gone else '出现'}" if text else "页面安静下来"))
        head = f"等到了：{what}（用了 {secs:.1f} 秒）"
    else:
        what = (f"元素 {ref} 还在页面上" if ref else
                (f"没看到文字「{text}」" if not gone else f"文字「{text}」还在") if text else
                "页面一直没安静下来（还在加载或内容一直在变）")
        head = f"等了 {secs:.1f} 秒超时：{what}。超时不是失败，下面是这段时间页面实际的变化。"
    return head + scope_note + _delta_text(before, frame, frame_url)


@tool
@_exclusive
def browser_console(level: str = "error", clear: bool = False,
                    include_previous: bool = False) -> str:
    """读浏览器控制台通道（前端报错排查用）：页面 console.*、alert/弹窗/下载/新窗口
    拦截通知、资源加载失败（记 URL，无状态码）。level=error（默认）/warn/all
    （all 含 info，即页面 console.log）；默认只给最近一次页面加载的输出（改完代码重开
    页面后看到的就是新代码的结果），include_previous=True 连之前几次加载的一起列；
    clear=True 读后清空缓冲。"""
    if level not in ("error", "warn", "all"):
        return "level 只能是 error / warn / all。"
    from .browser import page
    if page._browser is None:
        return _NO_PAGE
    r = _bridge_call("console", timeout=10, clear=clear)
    if not r.get("ok"):
        return _err(r, "读控制台")
    entries = r.get("entries") or []
    _note_browser_check(r, entries)
    if clear:
        load = _session.get_verification().get("browser_last_load")
        if isinstance(load, dict) and load.get("nav") == r.get("nav") \
                and load.get("tab") == r.get("tab"):
            load["cleared"] = True  # 这次加载之后的读数看不到清掉的那些报错，不再算检查
    if level != "all":
        entries = [e for e in entries if e.get("level") == level]
    earlier = 0
    if not include_previous and r.get("nav") is not None:
        current = [e for e in entries if e.get("nav", r["nav"]) == r["nav"]]
        earlier = len(entries) - len(current)
        entries = current
    entries = entries[-50:]
    cleared = "（缓冲已清空）" if clear else ""
    note = (f"\n（之前的页面加载还有 {earlier} 条，那是旧代码的输出；要看用 include_previous=True）"
            if earlier else "")
    if not entries:
        scope = "" if include_previous else "本次页面加载"
        return f"{_console_whose(r)}{scope}没有 {level} 级别的控制台输出。{cleared}{note}"
    lines = [f"[{e.get('level')}] {e.get('text', '')}" for e in entries]
    body = tree.truncate("\n".join(lines), tree.CONSOLE_DEFAULT_CHARS)
    scope = "全部加载" if include_previous else "本次页面加载"
    return (f"{_console_whose(r)}控制台（{level}，{scope}，{len(entries)} 条）:\n"
            + tree.fence(body, r.get("url", "")) + cleared + note)


def _console_whose(r):
    """开着不止一个标签时，说清这是哪个标签的控制台：模型点开新标签后接着读控制台，
    读到的是新标签的，却当成了原来那页的结论（用户实测：「关于」页没报错被说成待办页没报错）。"""
    if len(_tab_ids()) < 2:
        return ""
    from .browser.security import fence_url
    return f"[标签 #{r.get('tab')} · {fence_url(r.get('url') or '')}] "


@tool
@_exclusive
def browser_tabs(action: str = "list", tab_id: int | None = None) -> str:
    """浏览器标签：action=list 列出所有标签（当前的标 *）；select 切到 tab_id——之后的快照、
    点击、输入都作用于它；close 关掉 tab_id（关最后一个等于关浏览器）。
    点开「在新标签打开」的链接会自动开新标签并切过去；想自己开一个用 browser_open(url, new_tab=True)。"""
    from .browser import page
    if page._browser is None:
        return _NO_PAGE
    if action not in ("list", "select", "close"):
        return f"action 只能是 list / select / close，收到: {action}"
    if action != "list":
        try:
            tab_id = int(tab_id)
        except (TypeError, ValueError):
            return f"{action} 要给 tab_id（browser_tabs 列出的 #号）"
    if action == "select":
        r = _bridge_call("tab_select", id=tab_id)
        if not r.get("ok"):
            return _err(r, "切换标签")
        return f"已切到标签 #{tab_id}，之后的操作都作用于它；先 browser_snapshot 看看。\n" + _tab_lines(r)
    if action == "close":
        r = _bridge_call("tab_close", id=tab_id)
        if not r.get("ok"):
            return _err(r, "关闭标签")
        if r.get("last"):
            return f"已关闭标签 #{tab_id}，这是最后一个，浏览器也关了（cookie、缓存已清理）。"
        _bridge_call("settle", ms=50)
        lst = _bridge_call("tabs")
        return f"已关闭标签 #{tab_id}。\n" + (_tab_lines(lst) if lst.get("ok") else "")
    r = _bridge_call("tabs")
    if not r.get("ok"):
        return _err(r, "列标签")
    return _tab_lines(r)


def _tab_lines(r):
    tabs = r.get("tabs") or []
    cur = r.get("current")
    lines = []
    for t in tabs:
        mark = "*" if t.get("id") == cur else " "
        state_ = "（加载中）" if t.get("loading") else ""
        lines.append(f"{mark} #{t.get('id')} {t.get('title') or '（无标题）'} — {t.get('url') or 'about:blank'}{state_}")
    # 标题、地址是页面写的：进围栏
    return (f"共 {len(tabs)} 个标签，当前 #{cur}：\n"
            + tree.fence("\n".join(lines), (next((t for t in tabs if t.get("id") == cur), {}) or {}).get("url", "")))


@tool
@_exclusive
def browser_close() -> str:
    """关闭浏览器页面，清 cookie 和缓存。调试结束、不再需要页面时调用。"""
    r = _bridge_call("close", timeout=10)
    if not r.get("ok"):
        return _err(r, "关闭浏览器")
    return "浏览器已关闭（cookie、缓存已清理）。"