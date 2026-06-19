"""网络只读工具：fetch_url（抓网页正文）/ web_search（Tavily 搜索）。

从 tools.py 拆出的自包含域：只依赖标准库 + 懒加载的 requests/bs4 + config，
无项目内交叉依赖，故独立成兄弟模块。工具对象由 tools.py re-export 并装入 ALL_TOOLS。
"""
import re
import urllib.parse

from langchain_core.tools import tool


@tool
def fetch_url(url: str, max_chars: int = 8000) -> str:
    """抓取网页正文，用于查文档/报错信息/API 参考。

    url: 要抓取的网址（必须是 http:// 或 https://）
    max_chars: 最大返回字符数，默认 8000

    只允许 http/https 协议；按 Content-Type 处理内容类型；
    HTML 自动去标签转为可读纯文本。只读、不弹确认。"""
    import requests as _requests
    import html as _html

    # 协议白名单
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"不支持的协议: {parsed.scheme or '(无协议)'}。只允许 http:// 或 https://"

    # SSRF 防护：拒绝指向本机 / 内网 / 链路本地 / 保留地址的主机，防被诱导读取
    # 云元数据（169.254.169.254）或本机管理端口。
    import socket as _socket
    import ipaddress as _ipaddress
    _host = parsed.hostname
    if not _host:
        return "无效网址：缺少主机名。"
    try:
        for _info in _socket.getaddrinfo(_host, None):
            _ip = _ipaddress.ip_address(_info[4][0])
            if (_ip.is_private or _ip.is_loopback or _ip.is_link_local
                    or _ip.is_reserved or _ip.is_multicast or _ip.is_unspecified):
                return f"拒绝抓取：{_host} 解析到非公网地址 {_ip}（防 SSRF / 内网探测）。"
    except _socket.gaierror:
        return f"无法解析主机: {_host}"
    except Exception:
        pass

    try:
        resp = _requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (LingXi)"})
    except _requests.Timeout:
        return f"请求超时（15 秒）: {url}"
    except _requests.ConnectionError as e:
        return f"连接失败: {e}"
    except Exception as e:
        return f"请求异常: {e}"

    if resp.status_code < 200 or resp.status_code >= 300:
        return f"HTTP {resp.status_code}: 服务器返回非 2xx 状态码"

    content_type = resp.headers.get("Content-Type", "").lower()

    # 二进制类型直接拒绝
    if any(t in content_type for t in ("image/", "application/pdf", "audio/", "video/",
                                        "application/zip", "application/octet-stream")):
        return f"不支持的内容类型: {content_type.split(';')[0].strip()}"

    text = resp.text

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
