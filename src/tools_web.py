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
    enc = resp.encoding or "utf-8"
    try:
        return raw.decode(enc, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


@tool
def fetch_url(url: str, max_chars: int = 8000) -> str:
    """抓取网页正文，用于查文档/报错信息/API 参考。

    url: 要抓取的网址（必须是 http:// 或 https://）
    max_chars: 最大返回字符数，默认 8000

    只允许 http/https 协议；按 Content-Type 处理内容类型；
    HTML 自动去标签转为可读纯文本。只读、不弹确认。"""
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
            return reject
        try:
            resp = _requests.get(
                current, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (LingXi)"},
                allow_redirects=False,
                stream=True,   # 先拿到连接、校验实际 peer IP，再决定读不读 body
                proxies=_req_proxies,
            )
        except _requests.Timeout:
            return f"请求超时（15 秒）: {current}"
        except _requests.ConnectionError as e:
            return f"连接失败: {e}"
        except Exception as e:
            return f"请求异常: {e}"

        # 直连时校验实际 peer IP（防 DNS 重绑定）。仅当确实走了代理才跳过——那时 peer 是
        # 代理地址、校验无意义（且本地代理会把所有请求误判成内网）。
        proxied = _allow_proxy and _proxy_applies(current)
        if not proxied:
            peer_reject = _peer_ip_reject(resp)
            if peer_reject:
                _safe_close(resp)
                return peer_reject

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
        return f"重定向次数过多（> {_MAX_REDIRECTS} 跳），已中止: {url}"

    # stream=True 拿到的连接必须确保关闭；正文有界读取，避免超大响应吃内存。
    try:
        if resp.status_code < 200 or resp.status_code >= 300:
            return f"HTTP {resp.status_code}: 服务器返回非 2xx 状态码"

        content_type = resp.headers.get("Content-Type", "").lower()

        # 二进制类型直接拒绝
        if any(t in content_type for t in ("image/", "application/pdf", "audio/", "video/",
                                            "application/zip", "application/octet-stream")):
            return f"不支持的内容类型: {content_type.split(';')[0].strip()}"

        text = _read_bounded(resp)
    finally:
        _safe_close(resp)

    # JSON / 纯文本直接返回
    if "json" in content_type or (content_type.startswith("text/") and "html" not in content_type):
        result = text[:max_chars]
        if len(text) > max_chars:
            result += "... [已截断]"
        return result

    # HTML → 纯文本
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        result = soup.get_text(separator="\n")
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
    return result


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """用 Tavily 搜索引擎搜索网络内容，返回标题、链接和摘要。

    query: 搜索关键词
    max_results: 最大返回结果数，默认 5

    需要在 config.json 配置 web_search_api_key（tavily.com 免费申请）。
    只读、不弹确认。"""
    import requests as _requests

    from .config import WEB_SEARCH_API_KEY
    if not WEB_SEARCH_API_KEY:
        return "未配置搜索服务，请在 config.json 填 web_search_api_key（tavily.com 免费申请）"

    try:
        resp = _requests.post(
            "https://api.tavily.com/search",
            json={"api_key": WEB_SEARCH_API_KEY, "query": query, "max_results": max_results},
            timeout=15,
        )
    except _requests.Timeout:
        return "搜索请求超时（15 秒），请稍后重试"
    except _requests.ConnectionError as e:
        return f"搜索连接失败: {e}"
    except Exception as e:
        return f"搜索请求异常: {e}"

    if resp.status_code < 200 or resp.status_code >= 300:
        return f"搜索服务返回 HTTP {resp.status_code}，请检查 API key 是否正确"

    try:
        data = resp.json()
    except Exception:
        return "搜索服务返回了无法解析的响应"

    results = data.get("results", [])
    if not results:
        return "没搜到"

    lines = []
    for item in results:
        title = item.get("title", "(无标题)")
        url = item.get("url", "")
        content = item.get("content", "")
        lines.append(f"{title}\n  {url}\n  {content}")
    return "\n\n".join(lines)
