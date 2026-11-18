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
import json
import re

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
_NO_PAGE = "还没有打开的页面：先用 browser_open(url) 打开。"

# ref 签发登记：(frame, ref) -> 签发时的 docId。换文档后旧 ref 一律报失效，
# 配错 frame 也报失效（不能点中别的 frame / 新文档的同号元素）
_issued: dict = {}
_STALE_REF = ("ref 已失效：页面已换文档，或该 ref 不是当前快照/find 签发的。"
              "重新 browser_snapshot 拿新 ref 再操作。")


def _remember_refs(r, frame):
    doc = r.get("docId") or ""
    fk = frame or ""
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
    # 子 Agent 不逐条问（派出之前已经问过用户，同 run_command）
    if getattr(sess, "is_subagent", False):
        return ""
    ui = getattr(state, "ui_ref", None)
    if ui is None:
        from .tools import _no_ui_refusal
        return _no_ui_refusal("操作浏览器")
    try:
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


def _snapshot_text(r, cap):
    body = tree.fence(tree.truncate(r.get("tree", ""), cap),
                      r.get("qtUrl") or r.get("url", ""))
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
    _lx("watchBaseline", {"ref": ref}, frame)
    return before


def _style_text(frame):
    """文字树 diff 为空时，报动作前后操作处附近**实际计算样式**的变化。

    只报样式真变了的元素（class 换了但样式没变不报）；动作前就在自己变的元素、
    离得远没核对的属性变化只给计数，不说成"变了"。取不到基线返回 None，调用方退回原话。"""
    w = _lx("watchReport", {}, frame)
    if not w.get("ok"):
        return None
    changed = w.get("changed") or []
    if changed:
        lines = ["文字树没有变化，但操作处附近的实际样式变了（动作前后逐项比对计算样式）："]
        for c in changed:
            props = "；".join(f"{p['prop']} {p['from']} → {p['to']}" for p in c.get("props") or [])
            mark = "（就是操作的元素）" if c.get("self") else ""
            lines.append(f"- {c.get('desc')}{mark}：{props}")
        if w.get("more"):
            lines.append(f"- …另有 {w['more']} 个元素")
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
    delta = tree.compute_delta(before.get("tree", ""), after.get("tree", ""),
                               tree.DELTA_DEFAULT_CHARS)
    # 导航信号取首次 after：settle 重试可能取到重试间又变化的快照，不能抹掉"已导航"
    nav_url = after.get("qtUrl") or after.get("url")
    if delta == tree.DELTA_EMPTY:
        # 异步渲染（setTimeout/rAF）可能还没落到 DOM：等一小段再重取一次
        _bridge_call("settle", ms=_DELTA_SETTLE_MS)
        retry = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
        if retry.get("ok"):
            after = retry
            delta = tree.compute_delta(before.get("tree", ""), after.get("tree", ""),
                                       tree.DELTA_DEFAULT_CHARS)
        if delta == tree.DELTA_EMPTY and _view_hidden():
            # 隐藏 view 的定时器被节流到 ~1s，400ms 不够：再补一次长 settle
            _bridge_call("settle", ms=_DELTA_SETTLE_HIDDEN_MS)
            retry2 = _lx("snapshot", {"mode": "all", "maxChars": _DIFF_SNAPSHOT_CHARS}, frame)
            if retry2.get("ok"):
                after = retry2
                delta = tree.compute_delta(before.get("tree", ""), after.get("tree", ""),
                                           tree.DELTA_DEFAULT_CHARS)
    nav = ""
    if nav_url and nav_url != url:
        nav = f"\n页面已导航到 {nav_url}，旧 ref 全部失效，再操作先重新 browser_snapshot。"
    truncated = ((before.get("stats") or {}).get("truncated")
                 or (after.get("stats") or {}).get("truncated"))
    if delta == tree.DELTA_EMPTY:
        if truncated:
            return (f"\n页面变化: {tree.DELTA_EMPTY}（快照被截断，变化可能在截断区，"
                    f"diff 不全；用 root_ref/browser_find 核对）{nav}")
        if not nav:
            styled = _style_text(frame)
            if styled:
                return "\n页面变化: " + styled
        return f"\n页面变化: {tree.DELTA_EMPTY}{nav}"
    return "\n页面变化:\n" + tree.fence(delta, after.get("qtUrl") or after.get("url") or url) + nav


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


def _describe(info, ref):
    role = info.get("role") or "元素"
    name = info.get("name") or ""
    return f"{role}「{name}」（ref={ref}）" if name else f"{role}（ref={ref}）"


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
            "nav": r.get("nav"), "evidence_revision": load_revision,
            "url": r.get("url") or "", "loaded": bool(r.get("loaded"))}
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
    if nav is None or load.get("nav") != nav or load.get("cleared"):
        return                      # 页面后来又自己导航过，或这次加载的报错已被清空
    url = r.get("url") or load.get("url") or ""
    if not security.is_localhost(url):
        return                      # 公网页面证明不了本地代码
    if load.get("evidence_revision") != _evidence_revision():
        return                      # 加载之后又改了文件：页面上跑的不是当前代码
    errors = [e for e in entries_all
              if e.get("level") == "error" and e.get("nav", nav) == nav]
    _vf.mark_browser_check(v, errors=len(errors), url=url,
                           load_revision=load["evidence_revision"])
    summary = ("本次加载控制台无报错" if not errors
               else f"本次加载控制台有 {len(errors)} 条报错：{errors[-1].get('text', '')[:200]}")
    _vf.record_evidence(v, kind="browser", checker="浏览器控制台", path=url,
                        argv=None, cwd="", exit_code=None,
                        status="failed" if errors else "passed", summary=summary)


@tool
def browser_open(url: str, timeout: int = 30) -> str:
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
    r = _bridge_call("open", timeout=timeout, url=url)
    if not r.get("ok"):
        return f"打开失败: {r.get('error') or '未知错误'}（{url}）"
    _remember_load(r, load_revision)
    head = (f"已打开 {r.get('url') or url}\n"
            f"标题: {r.get('title') or '（无标题）'}（加载 {r.get('loadMs', '?')}ms）")
    snap = _lx("snapshot", {"mode": "interactive", "maxChars": tree.SNAPSHOT_DEFAULT_CHARS})
    if not snap.get("ok"):
        return head + f"\n（首份快照失败: {snap.get('error')}，用 browser_snapshot 重试）"
    _remember_refs(snap, None)
    return head + "\n" + _snapshot_text(snap, tree.SNAPSHOT_DEFAULT_CHARS)


@tool
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
    return out


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
    return "\n可用 frame（用 frame 参数取子树/动作）:\n" + "\n".join(lines)


@tool
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
        if m.get("href"):
            line += f" href={m['href']}"
        lines.append(line)
    return (f"找到 {len(matches)} 处匹配「{text}」：\n"
            + tree.fence("\n".join(lines), r.get("qtUrl") or r.get("url", "")))


@tool
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
def browser_screenshot(question: str, ref: str | None = None) -> str:
    """截浏览器当前可见区域，交给图片识别模型，按 question 回答它在图上看到的。

    核对"看起来对不对"用它：颜色 / 高亮 / 图标 / 图表 / canvas / 布局——文字树和
    动作后的样式核对都说不清的东西。question 要具体，只问图上看得出来的事，例如
    「"买牛奶"那一行有没有删除线」「日历里 11 月 3 日是什么底色」；别问"页面对不对"。
    ref：先把这个元素滚到可见区域再截。
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
    if ref:
        url, err = _page_url()
        if err:
            return err
        st = _lx("state")
        if not st.get("ok"):
            return _err(st, "读取页面状态")
        if ("", ref) not in _issued or _issued[("", ref)] != (st.get("docId") or ""):
            return _STALE_REF
        r = _bridge_call("js", code=(
            "(() => { const el = document.querySelector('[data-lxref=\"' + "
            + json.dumps(ref) + " + '\"]'); if (!el) return false;"
            " el.scrollIntoView({block: 'center', inline: 'center'}); return true; })()"))
        if not r.get("ok") or not r.get("value"):
            return _STALE_REF
        _bridge_call("settle", ms=150)
    shot = _bridge_call("grab")
    if not shot.get("ok"):
        return _err(shot, "截图")
    prompt = (f"这是浏览器页面的截图（{shot.get('url', '')}）。请回答：{question}\n"
              "只说图上能看到的；看不清、图上没有的就直说看不清 / 没有，不要猜。")
    try:
        vision_name, desc = describe_images_with_vision(prompt, [("browser.png", shot["png"])])
    except Exception as e:
        return f"截图失败: 图片识别模型出错（{e}）"
    if not desc:
        return f"截图完成，但识别模型（{vision_name}）没返回内容"
    return (f"[浏览器截图 · {shot.get('width')}×{shot.get('height')} · 识别模型 {vision_name}]\n"
            f"{desc}\n（这是识别模型看图的回答，不是原图；它也可能看错）")


@tool
def browser_click(ref: str, frame: str | None = None, delta: bool = True) -> str:
    """点击快照/find 给出的 ref 元素。默认附动作前后的"变化行 diff"；文字树没变时改报
    操作处附近**实际计算样式**的变化（动作前后逐项比对，不按 class 名猜）。
    "样式都没有变化"就是确实没反应，别反复点；颜色含义 / 图表要核对用 browser_screenshot。
    可能先问用户；提交/删除/支付这类有后果的动作每次都问。"""
    url, err = _page_url()
    if err:
        return err
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    st = _lx("state", frame=frame)
    if not st.get("ok"):
        return _err(st, "读取页面状态")
    cur_doc = st.get("docId") or ""
    if (frame or "", ref) not in _issued or _issued[(frame or "", ref)] != cur_doc:
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
    before = _before_action(ref, frame) if delta else None
    r = _lx("click", {"ref": ref,
                      "expect": {"doc": cur_doc, "fp": info.get("fp")}}, frame)
    if not r.get("ok"):
        return _err(r, "点击")
    return f"已点击 {desc}" + _delta_text(before, frame, url)


@tool
def browser_type(ref: str, text: str, clear: bool = True, submit: bool = False,
                 frame: str | None = None, delta: bool = True) -> str:
    """往 ref 指向的输入框输入文字（clear=True 先清空；submit=True 输入后回车，可能提交表单）。
    默认附"变化行 diff"。可能先问用户；回车提交/有后果的输入每次都问。
    别把密钥/令牌填进公网页面——会被直接拒绝。"""
    url, err = _page_url()
    if err:
        return err
    frame_url = url
    if frame:
        frame_url, err = _page_url(frame)
        if err:
            return err
    st = _lx("state", frame=frame)
    if not st.get("ok"):
        return _err(st, "读取页面状态")
    cur_doc = st.get("docId") or ""
    if (frame or "", ref) not in _issued or _issued[(frame or "", ref)] != cur_doc:
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
    before = _before_action(ref, frame) if delta else None
    r = _lx("type", {"ref": ref, "text": text, "clear": clear, "submit": submit,
                     "expect": {"doc": cur_doc, "fp": info.get("fp")}}, frame)
    if not r.get("ok"):
        return _err(r, "输入")
    value = json.dumps(r.get("value", ""), ensure_ascii=False)
    return f"已向 {desc} 输入，当前值: {value}" + _delta_text(before, frame, url)


@tool
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
        if isinstance(load, dict) and load.get("nav") == r.get("nav"):
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
        return f"{scope}没有 {level} 级别的控制台输出。{cleared}{note}"
    lines = [f"[{e.get('level')}] {e.get('text', '')}" for e in entries]
    body = tree.truncate("\n".join(lines), tree.CONSOLE_DEFAULT_CHARS)
    scope = "全部加载" if include_previous else "本次页面加载"
    return (f"控制台（{level}，{scope}，{len(entries)} 条）:\n"
            + tree.fence(body, r.get("url", "")) + cleared + note)


@tool
def browser_close() -> str:
    """关闭浏览器页面，清 cookie 和缓存。调试结束、不再需要页面时调用。"""
    r = _bridge_call("close", timeout=10)
    if not r.get("ok"):
        return _err(r, "关闭浏览器")
    return "浏览器已关闭（cookie、缓存已清理）。"