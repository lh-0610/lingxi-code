"""浏览器安全判定（纯逻辑，不依赖 Qt / WebEngine，可单测）。

三块：网址规则（§6.2）、来源授权与动作分级（§6.1）、有后果动作判定（§6.1 拍板 6）。
设计取向与 fetch_url 有意不同：浏览器**不套 SSRF 内网拦截**——localhost 正是主用途，
拦内网会把 dev server 全挡掉；内网风险靠"只读导航 + 动作确认 + 有后果必问"控。

判定函数都返回明确结论（错误串 / 决策字符串），由工具层（tools_browser）据此走确认闸门，
本模块不碰 UI、不读会话状态，方便单测穷举。
"""
import html
import re
import urllib.parse

# ── 网址规则（§6.2）─────────────────────────────────────────────
_ALLOWED_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def validate_url(url: str) -> str:
    """只允许 http/https 且有主机名。返回错误串（应拒绝）或 ""（放行）。

    file:// / chrome:// / data: / javascript: / blob: 等一律拒——file:// 会让页面读到
    本机任意文件，是明确越权面。与 fetch_url 的协议白名单一致（tools_web.py:83），
    但**不做内网 IP 拦截**（localhost 是主用途，见模块 docstring）。
    """
    if not url or not isinstance(url, str):
        return "无效网址：空。"
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"不支持的协议: {parsed.scheme or '(无协议)'}。只允许 http:// 或 https://"
    if not parsed.hostname:
        return "无效网址：缺少主机名。"
    return ""


def is_localhost_host(host: str) -> bool:
    """主机名是否本机回环：localhost / 127.0.0.1 / ::1 / *.localhost。"""
    if not host:
        return False
    h = host.strip().lower().rstrip(".")
    if h in _LOOPBACK_HOSTS:
        return True
    return h.endswith(".localhost")


def is_localhost(url: str) -> bool:
    """URL 是否指向本机回环（导航一律放行，点击/输入按来源授权）。"""
    try:
        return is_localhost_host(urllib.parse.urlparse(url).hostname or "")
    except ValueError:
        return False


def parse_origin(url: str) -> str:
    """来源 = scheme://host:port（localhost 按端口区分，不同端口是不同服务）。

    host 小写、IPv6 带方括号、默认端口（http 80 / https 443）省略，保证同一服务
    的不同写法归一到同一个来源串。解析失败返回 ""。
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return ""
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:                       # IPv6
        host = f"[{host}]"
    port = parsed.port
    if port is None or port == _DEFAULT_PORTS.get(parsed.scheme):
        return f"{parsed.scheme}://{host}"
    return f"{parsed.scheme}://{host}:{port}"


# ── 有后果动作判定（§6.1 拍板 6：结构为主、关键词为辅）──────────────
# 关键词清单写死在代码里（仿 permission_modes 的正则清单），不做设置项。宁可多问。
CONSEQUENCE_KEYWORDS = (
    "提交", "购买", "支付", "付款", "下单", "确认订单", "转账", "删除", "移除",
    "注册", "注销", "登录", "发送", "发布",
    # 复审 #20：React / Vue 的表单常是「onSubmit + fetch POST」，不写 method，结构上看着
    # 像 GET 表单；按钮多叫「确定 / 确认 / 保存」——只能靠名字兜底
    "确定", "确认", "保存",
    "signin", "signup", "submit", "pay", "purchase",
    "delete", "remove", "checkout", "confirm", "transfer", "buy", "send", "publish", "save",
)
# 太短、子串匹配会误中（book / token / look）的词，按整词匹配
CONSEQUENCE_WORDS = re.compile(r"\b(ok)\b")


def is_consequential(info: dict, *, submit: bool = False, origin: str | None = None) -> bool:
    """这个 click/type 动作有没有后果（提交表单 / 触发写操作）。

    info 来自 tree.js 的 info({ref})：{tag,type,name,formMethod,formAction,inForm}，
    按**可激活祖先**的结构判（点提交按钮里的 img 也算点提交）。
    口径（2026-11-17 拍板）：
    - POST 表单的提交（submit/image 按钮、表单内回车）→ 有后果；
    - GET 表单的提交按普通点击处理（可记住、信任来源放行），关键词命中除外；
    - 表单外的 submit 按钮惰性、不算；表单外回车（submit=True，"回车即发送"输入框）
      公网算有后果，信任的本机来源不算（其它本机来源宁可多问，算）；
    - 按钮名关键词为辅（提交/删除/支付/发送/发布…），命中即有后果。
    命中即"永远问、不给记住"（ask_always），来源白名单绕不过。
    """
    info = info or {}
    tag = (info.get("tag") or "").lower()
    typ = (info.get("type") or "").lower()
    name = (info.get("name") or "").lower()
    method = (info.get("formMethod") or "").lower()
    # 按钮自己的 formmethod 盖过表单的 method（复审 #20：GET 表单里一个
    # <button formmethod="post" formaction="/api/account/close"> 以前被判成无后果）
    if typ in ("submit", "image") and info.get("btnFormMethod"):
        method = info["btnFormMethod"].lower()
    in_form = bool(info.get("inForm"))

    if in_form and method == "post":
        if tag == "button" and typ == "submit":
            return True
        if tag == "input" and typ in ("submit", "image"):
            return True
        if submit:
            return True
    if submit and not in_form:
        return origin != ORIGIN_TRUSTED
    if submit and in_form:
        # 表单内回车 = 浏览器替用户点默认提交按钮：按那个按钮的名字判（「发送」「发布」……）。
        # 以前只看输入框自己的名字，同一个表单点「发送」每次都问、回车就直接发出去了
        if _has_keyword((info.get("submitName") or "").lower()):
            return True
        # 没写 method 的表单（onSubmit + fetch POST 多是这样）：同「表单外回车即发送」——
        # 信任的本机来源不问，其余宁可多问。明写 method="get" 的仍按普通 GET 表单
        if not info.get("formMethodSet", True):
            return origin != ORIGIN_TRUSTED
    return _has_keyword(name)


def _has_keyword(name: str) -> bool:
    return any(k in name for k in CONSEQUENCE_KEYWORDS) or bool(CONSEQUENCE_WORDS.search(name))


# ── 动作分级 × 权限模式（§6.1 表）─────────────────────────────────
# 决策：allow 放行 / ask 问（可记住到会话白名单）/ ask_always 永远问（不给记住）/ block 拦（Plan）。
ALLOW = "allow"
ASK = "ask"
ASK_ALWAYS = "ask_always"
BLOCK = "block"

# 来源类别：trusted=信任的 dev server 或已在会话白名单；localhost=其它本机来源；public=公网。
ORIGIN_TRUSTED = "trusted"
ORIGIN_LOCALHOST = "localhost"
ORIGIN_PUBLIC = "public"


def origin_kind(url: str, allowlist) -> str:
    """把 URL 归到来源类别。allowlist 是当前会话的 browser_origin_allowlist（可空）。"""
    origin = parse_origin(url)
    if allowlist and origin in allowlist:
        return ORIGIN_TRUSTED
    if is_localhost(url):
        return ORIGIN_LOCALHOST
    return ORIGIN_PUBLIC


_ORIGIN_RANK = {ORIGIN_TRUSTED: 0, ORIGIN_LOCALHOST: 1, ORIGIN_PUBLIC: 2}


def stricter_origin(a: str, b: str) -> str:
    """两个来源取更严的那个（trusted < localhost < public）。
    iframe 动作门控用：顶层 localhost 套公网 iframe 时按公网判。"""
    return a if _ORIGIN_RANK.get(a, 2) >= _ORIGIN_RANK.get(b, 2) else b


def decide_action(mode: str, *, action: str, origin: str, consequential: bool = False) -> str:
    """按 §6.1 表给出决策。

    action: readonly（snapshot/find/read_text/console）/ open（导航）/ interact（click/type）。
    origin: origin_kind 的返回值。mode: permission_modes.current(sess)（含 plan）。
    """
    if action == "readonly":
        return ALLOW
    if action == "open":
        if origin in (ORIGIN_TRUSTED, ORIGIN_LOCALHOST):
            return ALLOW
        if mode == "plan":                # 公网导航在 Plan 当只读调研放行（同 fetch_url）
            return ALLOW
        if mode in ("manual", "accept_edits"):
            return ASK
        return ALLOW                      # auto / bypass
    # interact（click / type）
    if mode == "plan":
        return BLOCK
    if consequential:
        return ALLOW if mode == "bypass" else ASK_ALWAYS
    if mode == "bypass":
        return ALLOW
    if origin == ORIGIN_TRUSTED:
        return ALLOW
    if origin == ORIGIN_PUBLIC:
        return ALLOW if mode == "auto" else ASK
    return ASK                            # 其它 localhost：首次问，同意后进会话白名单


def is_rememberable(decision: str) -> bool:
    """只有普通 ask 能给"允许并记住"（记到会话白名单、不落盘）；ask_always 永远不给。"""
    return decision == ASK


# ── 输入方向密钥防外泄（§6.5）─────────────────────────────────────
def secret_in_text(text: str, secrets: dict) -> str:
    """text 里是否含配置/环境里的密钥值（>=8 字符）。命中返回字段名，否则 ""。

    secrets 形如 secret_mask.collect_secrets() 的 {值: (字段名, 允许前缀打码)}。
    打码只管"结果进上下文"，这里管"模型别把密钥填进公网表单"——localhost 不拦（调试常填测试值），
    公网页面执行前由工具层调用。纯函数，secrets 由调用方传入。
    """
    if not text or not secrets:
        return ""
    for value, meta in secrets.items():
        if isinstance(value, str) and len(value) >= 8 and value in text:
            if isinstance(meta, (tuple, list)) and meta:
                return str(meta[0])
            return str(meta)
    return ""


def fence_url(url: str) -> str:
    """围栏属性里的 URL 转义（防引号/尖括号破坏围栏结构）。"""
    return html.escape(url or "", quote=True)