"""网络只读工具：fetch_url（抓网页正文）/ web_search（Tavily 搜索）。

从 tools.py 拆出的自包含域：只依赖标准库 + 懒加载的 requests/bs4 + config，
无项目内交叉依赖，故独立成兄弟模块。工具对象由 tools.py re-export 并装入 ALL_TOOLS。
"""
import re
import urllib.parse

from langchain_core.tools import tool

from .paths import logger

# 手动跟随重定向的最大跳数（防重定向环 / 拖时间）。
_MAX_REDIRECTS = 5
# 单次抓取最多下载字节数，防超大响应吃内存（正文随后再按 max_chars 截断）。
_MAX_DOWNLOAD_BYTES = 5_000_000

# 请求头：中文优先（多语言站点直接给中文页）。User-Agent 保持如实标明是灵犀——试过冒充
# Chrome：原本 403 的站（百度百科、fandom、redbull…）一个没放行，反而有站核对出"自称 Chrome
# 却不像 Chrome"把原来能访问的也拒了（hk.trip.com 200 → 432）。
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (LingXi)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 网页里不是正文的部分：导航、页眉页脚、侧栏、表单，以及维基的目录 / 编辑链接 / 引用角标。
# 不去掉的话，维基页面前一千来字全是"跳转到内容 | 主菜单 | 导航…"，正文被挤到截断线后面。
_NON_CONTENT_TAGS = ("script", "style", "head", "noscript", "template", "svg", "iframe",
                     "nav", "header", "footer", "aside", "form", "button")
_NON_CONTENT_SELECTORS = (
    "[role=navigation]", "[role=banner]", "[role=contentinfo]", "[role=search]",
    "[aria-hidden=true]", "[hidden]",
    ".mw-jump-link", ".mw-editsection", ".vector-toc", "#toc", ".toc", ".navbox",
    ".catlinks", ".printfooter", "sup.reference", ".reference", ".mw-references-wrap",
)
# 正文容器，按优先级找；文字太少（只是个卡片）就不算
_MAIN_SELECTORS = ("main", "article", "[role=main]", "#mw-content-text", "#content", ".content")
_MAIN_MIN_CHARS = 200


def _html_to_text(html: str) -> str:
    """HTML → 读得下去的纯文本：只留正文，表格按行转成「列 | 列 | 列」。

    表格是重点：赛程、价格、参数这类信息几乎都在表格里，原来按格拆成一行一个，
    模型对不上哪个日期配哪场比赛。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(list(_NON_CONTENT_TAGS)):
        tag.decompose()
    for selector in _NON_CONTENT_SELECTORS:
        for el in soup.select(selector):
            el.decompose()
    root = soup.body or soup
    for selector in _MAIN_SELECTORS:
        el = soup.select_one(selector)
        if el is not None and len(el.get_text(strip=True)) >= _MAIN_MIN_CHARS:
            root = el
            break
    for table in root.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(" | ".join(cells))
        table.replace_with("\n" + "\n".join(rows) + "\n")
    text = root.get_text(separator="\n")
    return f"标题：{title}\n\n{text}" if title else text


def _ssrf_reject(url: str) -> str:
    """对单个 URL 做 SSRF 校验：协议白名单 + 把主机名解析成 IP 后拒绝
    本机 / 内网 / 链路本地 / 保留地址。返回错误串（应拒绝）或 ""（放行）。

    注意：必须对【每一跳】调用——只查初始 URL 会被 302 重定向到内网地址绕过
    （这是先前的真实漏洞），所以重定向要关掉自动跟随、逐跳重过这里。
    """
    import socket as _socket
    import ipaddress as _ipaddress

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不支持的协议: {parsed.scheme or '(无协议)'}。只允许 http:// 或 https://"
    host = parsed.hostname
    if not host:
        return "无效网址：缺少主机名。"
    try:
        for _info in _socket.getaddrinfo(host, None):
            ip = _ipaddress.ip_address(_info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return f"拒绝抓取：{host} 解析到非公网地址 {ip}（防 SSRF / 内网探测）。"
    except _socket.gaierror:
        return f"无法解析主机: {host}"
    except Exception as e:
        # fail-closed：安全校验本身出错时拒绝，不能因为检查异常就当成安全放行
        return f"网址安全校验失败，已拒绝请求: {type(e).__name__}"
    return ""


def _safe_close(resp) -> None:
    try:
        resp.close()
    except Exception:
        pass


def _peer_ip_reject(resp) -> str:
    """defense-in-depth 防 DNS 重绑定：检查实际连上的 socket peer IP 是否仍是公网。
    _ssrf_reject 解析时是公网、requests 连接时又解析到内网（重绑定）——这里抓后者。
    best-effort：拿不到底层 socket（代理 / mock / 已释放）时不在此拦截，主闸门仍是
    _ssrf_reject；只有【确实】拿到 peer 且是内网才拒绝。"""
    import ipaddress as _ipaddress
    try:
        peer_ip = resp.raw.connection.sock.getpeername()[0]
    except Exception as e:
        logger.debug(f"peer IP 校验跳过（拿不到底层 socket）: {type(e).__name__}")
        return ""   # 拿不到 peer，交回主闸门，不误杀正常请求
    try:
        ip = _ipaddress.ip_address(peer_ip)
    except ValueError:
        return ""
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return f"拒绝抓取：实际连接到非公网地址 {ip}（防 DNS 重绑定）。"
    return ""


def _proxy_applies(url: str) -> bool:
    """该 URL 是否会走代理。走代理时 peer IP 看到的是代理地址而非目标——本地代理
    （127.0.0.1）会把每个请求误判成内网而拒掉，公网代理则让 peer 检查失去意义，
    故走代理时跳过 peer 检查（_ssrf_reject 的主机名校验仍生效）。"""
    import requests as _requests
    try:
        proxies = _requests.utils.get_environ_proxies(url, no_proxy=None)
    except Exception:
        return False
    scheme = urllib.parse.urlparse(url).scheme
    return bool(proxies.get(scheme) or proxies.get("all"))


def _read_bounded(resp) -> str:
    """有界读取响应正文：最多 _MAX_DOWNLOAD_BYTES 字节，防超大响应 OOM。"""
    chunks, total = [], 0
    for chunk in resp.iter_content(8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_DOWNLOAD_BYTES:
            break
    raw = b"".join(chunks)[:_MAX_DOWNLOAD_BYTES]
    return _decode_body(raw, resp)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_-]+)""", re.I)


def _decode_body(raw: bytes, resp) -> str:
    """按网页实际用的编码解码。响应头没写 charset 时，requests 按 HTTP 老规矩给 ISO-8859-1，
    中文网页（多是 UTF-8 / GBK）就成了乱码（实测直播吧的标题变成 "T1å»è´¥MKOI…"）。
    依次信：响应头写明的 → 页面里 <meta charset> 写明的 → 能完整按 UTF-8 解开就是 UTF-8 → GB18030。"""
    import codecs
    declared = ""
    if "charset=" in (resp.headers.get("Content-Type") or "").lower():
        declared = resp.encoding or ""
    if not declared:
        found = _META_CHARSET.search(raw[:4096])
        declared = found.group(1).decode("ascii", "ignore") if found else ""
    if declared:
        try:
            return raw.decode(declared, errors="replace")
        except LookupError:
            pass
    try:
        # 增量解码：被截断在半个汉字上的结尾不算"不是 UTF-8"
        return codecs.getincrementaldecoder("utf-8")().decode(raw, final=False)
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _fetch_text(url: str, max_chars: int = 8000) -> tuple[bool, str]:
    """抓一个网址的正文。返回 (是否拿到了正文, 正文或失败原因)——深度搜索要分得清
    哪个网页抓到了、哪个只拿回一句报错。"""
    import requests as _requests
    import html as _html

    # SSRF 防护 + 手动跟随重定向：关闭 requests 的自动重定向，自己逐跳重过
    # _ssrf_reject。否则 http://evil.com 返回 302 → http://169.254.169.254/
    # 就能绕过只查初始地址的校验，读到云元数据 / 本机端口。
    # 默认【不走系统代理】(proxies 置空覆盖 env)：直连才能让下面的 peer IP 校验有意义、
    # SSRF 防线完整。需靠代理访问外网时 config 设 fetch_url_allow_proxy: true。
    from .config import FETCH_URL_ALLOW_PROXY as _allow_proxy
    _req_proxies = None if _allow_proxy else {"http": "", "https": ""}

    current = url
    resp = None
    for _hop in range(_MAX_REDIRECTS + 1):
        reject = _ssrf_reject(current)
        if reject:
            return False, reject
        try:
            resp = _requests.get(
                current, timeout=15,
                headers=_BROWSER_HEADERS,
                allow_redirects=False,
                stream=True,   # 先拿到连接、校验实际 peer IP，再决定读不读 body
                proxies=_req_proxies,
            )
        except _requests.Timeout:
            return False, f"请求超时（15 秒）: {current}"
        except _requests.ConnectionError as e:
            return False, f"连接失败: {e}"
        except Exception as e:
            return False, f"请求异常: {e}"

        # 直连时校验实际 peer IP（防 DNS 重绑定）。仅当确实走了代理才跳过——那时 peer 是
        # 代理地址、校验无意义（且本地代理会把所有请求误判成内网）。
        proxied = _allow_proxy and _proxy_applies(current)
        if not proxied:
            peer_reject = _peer_ip_reject(resp)
            if peer_reject:
                _safe_close(resp)
                return False, peer_reject

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if location:
                _safe_close(resp)
                # 相对跳转按当前 URL 解析成绝对地址，再回到循环顶部重过 SSRF 检查
                current = urllib.parse.urljoin(current, location)
                continue
            # 没给 Location，当普通响应处理
        break
    else:
        return False, f"重定向次数过多（> {_MAX_REDIRECTS} 跳），已中止: {url}"

    # stream=True 拿到的连接必须确保关闭；正文有界读取，避免超大响应吃内存。
    try:
        if resp.status_code < 200 or resp.status_code >= 300:
            return False, f"HTTP {resp.status_code}: 服务器返回非 2xx 状态码"

        content_type = resp.headers.get("Content-Type", "").lower()

        # 二进制类型直接拒绝
        if any(t in content_type for t in ("image/", "application/pdf", "audio/", "video/",
                                            "application/zip", "application/octet-stream")):
            return False, f"不支持的内容类型: {content_type.split(';')[0].strip()}"

        text = _read_bounded(resp)
    finally:
        _safe_close(resp)

    # JSON / 纯文本直接返回
    if "json" in content_type or (content_type.startswith("text/") and "html" not in content_type):
        result = text[:max_chars]
        if len(text) > max_chars:
            result += "... [已截断]"
        return True, result

    # HTML → 纯文本（只留正文、表格转行）
    try:
        result = _html_to_text(text)
    except ImportError:
        # beautifulsoup4 未安装，用正则处理
        cleaned = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        result = _html.unescape(cleaned)

    # 收敛连续空白和空行
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\n\s*\n+", "\n\n", result)
    result = result.strip()

    truncated = len(result) > max_chars
    result = result[:max_chars]
    if truncated:
        result += "\n... [已截断]"
    return True, result

@tool
def fetch_url(url: str, max_chars: int = 8000) -> str:
    """抓取网页正文，用于查文档/报错信息/API 参考。

    url: 要抓取的网址（必须是 http:// 或 https://）
    max_chars: 最大返回字符数，默认 8000

    只允许 http/https 协议；按 Content-Type 处理内容类型；
    HTML 自动去标签转为可读纯文本。只读、不弹确认。"""
    return _fetch_page(url, max_chars)[1]


# 网站自己拒绝我们（403 / 429 / 5xx / 超时……）时，改由 DeepSeek 的抓取接口去读。只在请求已经发出、
# 是网站那边不给的情况下才转——被我们自己的 SSRF 检查拦下的（内网 / 本机地址）、不支持的类型，
# 绝不把网址转给第三方。
_FALLBACK_FAILURES = re.compile(r"^(?:HTTP (?:401|403|429|451|5\d\d)\b|请求超时|连接失败)")


def _deepseek_fetch(url, max_chars):
    import requests as _requests
    key, base = _deepseek_key_and_base()
    if not key:
        return False, ""
    try:
        resp = _requests.post(f"{base}/v1/tools/fetch", json={"url": url}, timeout=30,
                              headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        return False, f"DeepSeek 抓取失败: {e}"
    if resp.status_code != 200:
        return False, _deepseek_error(resp, "抓取")
    try:
        data = resp.json()
    except Exception:
        return False, "DeepSeek 抓取返回了无法解析的响应"
    content = (data.get("content") or "").strip()
    if not content:
        return False, "DeepSeek 抓取没有拿到正文"
    title = (data.get("title") or "").strip()
    text = (f"标题：{title}\n\n" if title else "") + content
    return True, text if len(text) <= max_chars else text[:max_chars] + "\n... [已截断]"


def _fetch_page(url, max_chars):
    """先自己抓；网站拒绝时（见 _FALLBACK_FAILURES）有 DeepSeek key 就改用它的抓取接口再试一次。"""
    ok, text = _fetch_text(url, max_chars)
    if ok or not _FALLBACK_FAILURES.match(text):
        return ok, text
    ok2, text2 = _deepseek_fetch(url, max_chars)
    if ok2:
        return True, f"（网站拒绝了直接访问：{text}。以下内容经 DeepSeek 抓取接口获取）\n{text2}"
    return False, text + (f"；{text2}" if text2 else "")


# Tavily 的可选参数，值不在这里面就不传（传错值 Tavily 会整个请求报 400）
_SEARCH_TOPICS = ("general", "news")
_SEARCH_TIME_RANGES = ("day", "week", "month", "year")
_SEARCH_DEPTHS = ("basic", "advanced")
_SEARCH_MAX_RESULTS = 10
# Tavily 每条结果的 content 是它按问题挑出的 2~3 段原文，用 "[...]" 隔开（basic、advanced 都是）。
# 按段收：每段留开头一截，段间用 … 隔开——整条硬截会把后面几段相关内容一起砍掉。
_CHUNK_SEPARATOR = re.compile(r"\s*\[(?:\.\.\.|…)\]\s*")
_CHUNK_MAX = 350
_SNIPPET_NOISE = (
    re.compile(r"\[(?:编辑|編輯|edit)\]", re.I),             # [编辑]
    re.compile(r"\[\d+\]"),                                 # [12] 引用角标
    re.compile(r"\^\s*\d+(?:\.\d+)*"),                      # ^ 12.0 引用回链
    re.compile(r"(?m)^#{1,6}\s*"),                          # Markdown 标题的 ###
)


def _clean_snippet(text: str, chunk_max: int = _CHUNK_MAX) -> str:
    chunks = []
    for chunk in _CHUNK_SEPARATOR.split(text or ""):
        for pattern in _SNIPPET_NOISE:
            chunk = pattern.sub("", chunk)
        chunk = " ".join(chunk.split())
        if chunk:
            chunks.append(chunk if len(chunk) <= chunk_max else chunk[:chunk_max].rstrip() + "…")
    return " … ".join(chunks)


class _SearchFailed(Exception):
    """这个搜索服务这次没给出结果（HTTP 出错、额度用完、响应解析不了……）；消息给模型看。"""


def _tavily_search(query, count, topic, time_range, depth, deep=False):
    """Tavily：返回 (直接答案, [结果])。每条结果的 content 是按问题挑出的几段原文。"""
    import requests as _requests
    from .config import WEB_SEARCH_API_KEY

    payload = {"api_key": WEB_SEARCH_API_KEY, "query": query, "max_results": count,
               "topic": topic if topic in _SEARCH_TOPICS else "general",
               "include_answer": True}
    if time_range in _SEARCH_TIME_RANGES:
        payload["time_range"] = time_range
    if depth in _SEARCH_DEPTHS and depth != "basic":
        payload["search_depth"] = depth
        payload["chunks_per_source"] = 3
    try:
        resp = _requests.post("https://api.tavily.com/search", json=payload, timeout=15)
    except _requests.Timeout:
        raise _SearchFailed("搜索请求超时（15 秒），请稍后重试")
    except _requests.ConnectionError as e:
        raise _SearchFailed(f"搜索连接失败: {e}")
    except Exception as e:
        raise _SearchFailed(f"搜索请求异常: {e}")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise _SearchFailed(f"搜索服务返回 HTTP {resp.status_code}，请检查 API key 是否正确")
    try:
        data = resp.json()
    except Exception:
        raise _SearchFailed("搜索服务返回了无法解析的响应")
    results = [{"title": item.get("title"), "url": item.get("url", ""),
                "date": item.get("published_date"), "content": item.get("content", "")}
               for item in (data.get("results") or [])]
    return (data.get("answer") or "").strip(), results


# 博查只接普通的 Web Search（不接收费更高、带大模型总结的 AI Search：总结由灵犀自己的模型做）。
# 时间范围对应 freshness；"新闻"博查没有单独的类型，没给时间范围时按最近一周找。
_BOCHA_FRESHNESS = {"day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear"}
# 博查的 summary 是一整段长摘要（不像 Tavily 用 [...] 分段），上限放宽一些
_BOCHA_CHUNK_MAX = 600


def _bocha_search(query, count, topic, time_range, depth, deep=False):
    """博查 Web Search：返回 ("", [结果])。打开 summary，每条带网页长摘要。"""
    import requests as _requests
    from .config import BOCHA_API_KEY

    freshness = _BOCHA_FRESHNESS.get(time_range) or ("oneWeek" if topic == "news" else "noLimit")
    payload = {"query": query, "count": count, "summary": True, "freshness": freshness}
    try:
        resp = _requests.post("https://api.bochaai.com/v1/web-search", json=payload, timeout=15,
                              headers={"Authorization": f"Bearer {BOCHA_API_KEY}"})
    except _requests.Timeout:
        raise _SearchFailed("博查搜索超时（15 秒）")
    except Exception as e:
        raise _SearchFailed(f"博查搜索连接失败: {e}")
    if resp.status_code in (401, 403):
        raise _SearchFailed(f"博查 key 无效或没有权限（HTTP {resp.status_code}）")
    if resp.status_code in (402, 429):
        raise _SearchFailed(f"博查额度用完或请求太频繁（HTTP {resp.status_code}）")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise _SearchFailed(f"博查搜索返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        raise _SearchFailed("博查返回了无法解析的响应")
    # 正式接口外面包一层 {"code", "msg", "data": SearchResponse}；HTTP 200 也可能是业务错误
    code = data.get("code")
    if isinstance(code, int) and code != 200:
        raise _SearchFailed(f"博查返回错误（{code}）：{data.get('msg') or '未说明原因'}")
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    pages = ((body or {}).get("webPages") or {}).get("value") or []
    results = [{"title": p.get("name"), "url": p.get("url", ""), "site": p.get("siteName"),
                "date": p.get("datePublished"), "content": p.get("summary") or p.get("snippet") or "",
                "chunk_max": _BOCHA_CHUNK_MAX}
               for p in pages]
    return "", results


# DeepSeek 自家的搜索 / 抓取接口（2026-10 上线，和模型共用一个 key，按成功请求计费、失败和空结果
# 不收）。没有时间范围参数，用查询语法 after:日期 限定；查询最长 128 字。
# 文档：https://api-docs.deepseek.com/guides/search
_DEEPSEEK_SINCE_DAYS = {"day": 1, "week": 7, "month": 31, "year": 366}
_DEEPSEEK_QUERY_MAX = 128
_DEEPSEEK_CHUNK_MAX = 800    # summary 是给模型读的长摘要（官方说 600~800 字）


def _deepseek_key_and_base():
    """模型那边配的 DeepSeek key 和地址（示例里的占位 key 不算）。地址带了 /v1 的去掉，接口路径自己带。"""
    from . import config
    key = str(config.DEEPSEEK_API_KEY or "").strip()
    v = key.lower()
    if not key or "xxxx" in v or v in {"sk-", "your-api-key", "your_api_key"}:
        return "", ""
    base = str(config.DEEPSEEK_BASE_URL or "https://api.deepseek.com").rstrip("/")
    return key, base[:-3] if base.endswith("/v1") else base


def _deepseek_error(resp, what):
    try:
        message = (resp.json().get("error") or {}).get("message") or ""
    except Exception:
        message = ""
    reason = {401: "key 无效", 402: "账户余额不足", 429: "请求太频繁"}.get(resp.status_code, "")
    return f"DeepSeek {what}返回 HTTP {resp.status_code}" + (f"（{reason or message}）" if reason or message else "")


def _deepseek_search(query, count, topic, time_range, depth, deep=False):
    """DeepSeek 搜索：返回 ("", [结果])。summary 是给模型读的长摘要；深度搜索时要回正文（need_content），
    深读就不用再自己去抓网页、也就不会被网站 403。"""
    import requests as _requests
    from datetime import date, timedelta
    key, base = _deepseek_key_and_base()
    days = _DEEPSEEK_SINCE_DAYS.get(time_range) or (7 if topic == "news" else None)
    operator = f" after:{(date.today() - timedelta(days=days)).isoformat()}" if days else ""
    text = " ".join((query or "").split())[:_DEEPSEEK_QUERY_MAX - len(operator)]
    payload = {"query": text + operator, "count": count, "need_content": bool(deep)}
    try:
        resp = _requests.post(f"{base}/v1/tools/search", json=payload, timeout=20,
                              headers={"Authorization": f"Bearer {key}"})
    except _requests.Timeout:
        raise _SearchFailed("DeepSeek 搜索超时（20 秒）")
    except Exception as e:
        raise _SearchFailed(f"DeepSeek 搜索连接失败: {e}")
    if resp.status_code != 200:
        raise _SearchFailed(_deepseek_error(resp, "搜索"))
    try:
        items = resp.json().get("results") or []
    except Exception:
        raise _SearchFailed("DeepSeek 搜索返回了无法解析的响应")
    results = [{"title": it.get("title"), "url": it.get("url", ""), "site": it.get("site_name"),
                "date": it.get("date"), "content": it.get("summary") or it.get("snippet") or "",
                "chunk_max": _DEEPSEEK_CHUNK_MAX, "full_text": it.get("content") or ""}
               for it in items]
    return "", results


_SEARCHERS = {"tavily": _tavily_search, "bocha": _bocha_search, "deepseek": _deepseek_search}
_SEARCHER_NAMES = {"tavily": "Tavily", "bocha": "博查", "deepseek": "DeepSeek"}
_CJK = re.compile(r"[一-鿿]")


def _search_order(query):
    """这次按什么顺序试搜索服务（只列配了 key 的）。

    auto：中文问题先 DeepSeek（中文最好、最便宜，用模型那边的 key）、再博查、再 Tavily；
    其余先 Tavily、再 DeepSeek、再博查；一个出错就换下一个。tavily / bocha / deepseek：只用它——
    用户选了"只用"，就不偷偷换成另一家。"""
    from . import config
    keys = {"tavily": config.WEB_SEARCH_API_KEY, "bocha": config.BOCHA_API_KEY,
            "deepseek": _deepseek_key_and_base()[0]}
    pref = config.WEB_SEARCH_PROVIDER
    if pref in _SEARCHERS:
        order = [pref]
    elif _CJK.search(query or ""):
        order = ["deepseek", "bocha", "tavily"]
    else:
        order = ["tavily", "deepseek", "bocha"]
    return [name for name in order if keys[name]]


# ── 深度搜索：抓前几个结果的原文，按问题挑出最相关的段落（设置里的开关）──
_DEEP_PAGES = 3          # 读前几个结果的原文
_DEEP_PASSAGES = 3       # 每篇留几段
_DEEP_PASSAGE_MAX = 500  # 一段最多多少字
_DEEP_FETCH_CHARS = 60000
_DEEP_OTHERS_MAX = 120   # 深度模式下没读原文的结果只留一句话


def _terms(text):
    """打分用的词：英文 / 数字按词切，中文按相邻两字切（不需要分词库）。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", text)
    grams = []
    for run in re.findall(r"[一-鿿]+", text):
        if len(run) == 1:
            grams.append(run)
        else:
            grams.extend(run[i:i + 2] for i in range(len(run) - 1))
    return words + grams


def _split_passages(text):
    """把网页正文切成几百字一段：按行攒，遇到空行或攒够就断。表格行（列 | 列）和上下文留在一起。"""
    passages, buf = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            if sum(len(x) for x in buf) >= 80:
                passages.append(" ".join(buf))
                buf = []
            continue
        buf.append(line)
        if sum(len(x) for x in buf) >= 300:
            passages.append(" ".join(buf))
            buf = []
    if buf:
        passages.append(" ".join(buf))
    return [p if len(p) <= _DEEP_PASSAGE_MAX else p[:_DEEP_PASSAGE_MAX].rstrip() + "…" for p in passages]


def _top_passages(query, text, k=_DEEP_PASSAGES):
    """按和问题的相关度挑 k 段：主要看覆盖了问题里多少个不同的词，其次看命中次数。
    按原文顺序返回（读起来连贯）。一个词都没命中的段不要。"""
    wanted = set(_terms(query))
    if not wanted:
        return []
    scored = []
    for i, passage in enumerate(_split_passages(text)):
        terms = _terms(passage)
        covered = wanted & set(terms)
        if not covered:
            continue
        hits = sum(1 for t in terms if t in wanted)
        scored.append((len(covered) / len(wanted) + 0.1 * min(hits, 20) / 20, i, passage))
    best = sorted(scored, key=lambda x: (-x[0], x[1]))[:k]
    return [p for _, _, p in sorted(best, key=lambda x: x[1])]


def _deep_read(query, results):
    """读前几个结果的原文，挑出最相关的段落放进 result["deep"]。搜索服务已经带回正文的（DeepSeek）
    直接用，其余并发去抓；抓不到（403、超时）的保留原摘要。"""
    import concurrent.futures as _cf
    targets = [r for r in results if r.get("url")][:_DEEP_PAGES]
    if not targets:
        return 0

    def _text_of(result):
        if result.get("full_text"):
            return True, result["full_text"]
        return _fetch_page(result["url"], _DEEP_FETCH_CHARS)

    with _cf.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        fetched = list(pool.map(_text_of, targets))
    read = 0
    for result, (ok, text) in zip(targets, fetched):
        passages = _top_passages(query, text) if ok else []
        if passages:
            result["deep"] = passages
            read += 1
    return read


@tool
def web_search(query: str, max_results: int = 5, topic: str = "general", time_range: str = "",
               depth: str = "basic") -> str:
    """搜索网络，返回一句直接答案（有的话）和每条结果的标题、链接、发布时间，
    以及每个网页里和问题最相关的几段原文。

    一次把问题问全（比如"S16 瑞士轮第四轮 对阵 比分"），先看直接答案和这次的结果够不够，
    够了就答；通常 1~3 次就够，不要一场比赛、一个名字各搜一次。

    query: 搜索关键词
    max_results: 最大返回结果数，默认 5，最多 10
    topic: "general" 普通搜索（默认）；"news" 新闻——查赛事、时事、最新动态时用
    time_range: 只要最近一段时间的结果："day" / "week" / "month" / "year"；留空不限
    depth: "basic"（默认，先用它）；"advanced" 段落挑得更准，但每次多花一份额度——
           只在 basic 的结果里确实缺关键细节时再用，或改用 fetch_url 抓那篇原文

    用 DeepSeek（模型的 key）/ Tavily / 博查，见设置 → 高级 → 网络搜索。只读、不弹确认。"""
    from . import config

    order = _search_order(query)
    if not order:
        return ("未配置搜索服务：在设置里填 DeepSeek 模型的 key（它自带搜索），或在设置 → 高级 → 网络搜索 "
                "里填 Tavily / 博查的 key（tavily.com 有免费额度）")
    try:
        count = max(1, min(int(max_results), _SEARCH_MAX_RESULTS))
    except (TypeError, ValueError):
        count = 5

    import time as _time
    failures = []
    started = _time.monotonic()
    for name in order:
        try:
            answer, results = _SEARCHERS[name](query, count, topic, time_range, depth,
                                               deep=config.WEB_SEARCH_DEEP)
            break
        except _SearchFailed as e:
            failures.append(f"{_SEARCHER_NAMES[name]}：{e}" if len(order) > 1 else str(e))
            logger.warning(f"web_search：{_SEARCHER_NAMES[name]} 失败（{e}），查询 {query[:60]!r}")
    else:
        return "；".join(failures)

    used = _SEARCHER_NAMES[name]
    if not results and not answer:
        logger.info(f"web_search：{used} 没搜到，查询 {query[:60]!r}")
        return f"（用 {used} 搜索）没搜到"
    deep_read = _deep_read(query, results) if config.WEB_SEARCH_DEEP else 0
    logger.info(f"web_search：用 {used}，{len(results)} 条，{_time.monotonic() - started:.1f}s"
                + (f"，深读 {deep_read} 篇" if deep_read else "") + f"，查询 {query[:60]!r}")

    # 第一行写明用的是哪家：AI 只管调"网络搜索"，走哪家由设置决定，不写出来用户看不出来
    if failures:
        lines = [f"（{'；'.join(failures)}；这次改用 {used} 搜索）"]
    else:
        lines = [f"（用 {used} 搜索）"]
    if deep_read:
        lines.append(f"（深度搜索：前 {deep_read} 个结果读了原文，下面标「原文」的是按问题挑出的段落）")
    if answer:
        lines.append(f"直接答案（搜索服务根据下面的结果归纳，引用前对照来源核实）：{answer}")
    for item in results:
        title = item.get("title") or "(无标题)"
        head = f"{title}\n  {item.get('url', '')}"
        if item.get("site"):
            head += f"\n  来源：{item['site']}"
        if item.get("date"):
            head += f"\n  发布：{item['date']}"
        if item.get("deep"):
            body = "\n".join(f"  原文：{p}" for p in item["deep"])
        elif deep_read:
            # 深度模式下前几条已有原文段落，其余只留一句话当线索：照常给完整摘要的话，
            # 一次搜索仍有七八千字，评测里开了深度反而更费 token
            body = "  " + _clean_snippet(item.get("content", ""), _DEEP_OTHERS_MAX).split(" … ")[0]
        else:
            body = "  " + _clean_snippet(item.get("content", ""), item.get("chunk_max", _CHUNK_MAX))
        lines.append(f"{head}\n{body}")
    return "\n\n".join(lines)
