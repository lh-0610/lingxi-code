"""fetch_url / web_search 测试。

不真联网——monkeypatch 掉 requests.get/post，喂假 response。
fetch_url 内部 `import requests as _requests`，_requests 即 requests 模块，
所以 patch requests.get 对它生效。web_search 的 key 用 monkeypatch config 控制。
"""
import socket

import pytest
import requests

from src import config
from src.tools import fetch_url, web_search


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """fetch_url 的 SSRF 检查会 getaddrinfo(host)；测试用假 host，统一解析成公网 IP 放行。"""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))],
    )


class FakeResp:
    def __init__(self, status=200, headers=None, text="", json_data=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.encoding = "utf-8"
        self._json = json_data

    def iter_content(self, chunk_size=8192):
        data = self.text.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def close(self):
        pass

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class TestFetchUrl:
    def test_rejects_non_http(self):
        # 非 http(s) 直接拒绝，且不发请求
        assert "不支持的协议" in fetch_url.func("file:///etc/passwd")
        assert "不支持的协议" in fetch_url.func("ftp://example.com/x")

    def test_rejects_private_host(self, monkeypatch):
        # SSRF：解析到本机/内网地址 → 拒绝，且不发请求
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, *a, **k: [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))])
        called = []
        monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1))
        out = fetch_url.func("http://169.254.169.254/latest/meta-data/")
        assert "拒绝" in out and not called

    def test_html_stripped(self, monkeypatch):
        html = ("<html><head><title>T</title><style>.x{color:red}</style></head>"
                "<body><script>alert(1)</script><p>Hello &amp; world</p></body></html>")
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, html))
        out = fetch_url.func("http://x")
        assert "Hello & world" in out          # 标签剥掉 + 实体解码
        assert "alert(1)" not in out           # <script> 内容被去掉
        assert "color:red" not in out          # <style> 内容被去掉

    def test_json_passthrough(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: FakeResp(200, {"Content-Type": "application/json"}, '{"a": 1}'))
        assert '{"a": 1}' in fetch_url.func("http://x")

    def test_binary_rejected(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: FakeResp(200, {"Content-Type": "image/png"}, ""))
        assert "不支持的内容类型" in fetch_url.func("http://x")

    def test_non_2xx(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(404, {}, ""))
        assert "HTTP 404" in fetch_url.func("http://x")

    def test_truncation(self, monkeypatch):
        html = "<p>" + "a" * 200 + "</p>"
        monkeypatch.setattr(
            requests, "get",
            lambda *a, **k: FakeResp(200, {"Content-Type": "text/html"}, html))
        out = fetch_url.func("http://x", max_chars=20)
        assert "已截断" in out

    def test_timeout_graceful(self, monkeypatch):
        def _raise(*a, **k):
            raise requests.Timeout()
        monkeypatch.setattr(requests, "get", _raise)
        assert "超时" in fetch_url.func("http://x")

    def test_redirect_to_internal_blocked(self, monkeypatch):
        """重定向 SSRF：初始 host 公网放行，但 302 跳到内网地址必须在第二跳被拦。"""
        import ipaddress

        def _dns(host, *a, **k):
            try:
                ipaddress.ip_address(host)   # 数字 IP 原样返回
                ip = host
            except ValueError:
                ip = "93.184.216.34"          # 域名 → 公网
            return [(socket.AF_INET, None, None, "", (ip, 0))]
        monkeypatch.setattr(socket, "getaddrinfo", _dns)
        # 只会被请求一次（第一跳）；内网目标在发请求前就被 SSRF 检查拦下
        routes = {"http://evil.com/": FakeResp(
            302, {"Location": "http://169.254.169.254/latest/meta-data/"})}
        monkeypatch.setattr(requests, "get", lambda url, *a, **k: routes[url])
        out = fetch_url.func("http://evil.com/")
        assert "拒绝" in out and "169.254.169.254" in out

    def test_redirect_to_public_followed(self, monkeypatch):
        """正常重定向（公网 → 公网）应被手动跟随，最终返回目标页内容。"""
        routes = {
            "http://a.com/": FakeResp(301, {"Location": "http://b.com/page"}),
            "http://b.com/page": FakeResp(200, {"Content-Type": "text/html"}, "<p>final</p>"),
        }
        monkeypatch.setattr(requests, "get", lambda url, *a, **k: routes[url])
        out = fetch_url.func("http://a.com/")
        assert "final" in out

    def test_redirect_loop_aborted(self, monkeypatch):
        """无限重定向（始终 302）超过最大跳数后中止，不挂死。"""
        monkeypatch.setattr(
            requests, "get",
            lambda url, *a, **k: FakeResp(302, {"Location": "/next"}))
        out = fetch_url.func("http://loop.com/")
        assert "重定向次数过多" in out

    def test_ssrf_check_failclosed(self, monkeypatch):
        """SSRF 校验本身抛异常（非 gaierror）时 fail-closed 拒绝，不放行、不发请求。"""
        def _boom(*a, **k):
            raise RuntimeError("resolver exploded")
        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        called = []
        monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1))
        out = fetch_url.func("http://x.com/")
        assert "已拒绝" in out and not called

    def test_dns_rebinding_peer_blocked(self, monkeypatch):
        """DNS 重绑定：DNS 校验放行（公网），但实际连到的 peer IP 是内网 → 第二道防线拦下。"""
        # 固定为"无代理"，确保 peer 检查真的运行（不被测试机的代理环境变量跳过）
        monkeypatch.setattr("src.tools_web._proxy_applies", lambda url: False)

        class _Sock:
            def getpeername(self):
                return ("10.0.0.5", 443)

        class _Conn:
            sock = _Sock()

        class _Raw:
            connection = _Conn()

        class _Resp(FakeResp):
            def __init__(self):
                super().__init__(200, {"Content-Type": "text/html"}, "<p>secret</p>")
                self.raw = _Raw()

            def close(self):
                pass

        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        out = fetch_url.func("http://looks-public.com/")
        assert "拒绝" in out and "10.0.0.5" in out


class TestWebSearch:
    def test_no_key_graceful(self, monkeypatch):
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "")
        assert "未配置" in web_search.func("python asyncio")

    def test_parses_results(self, monkeypatch):
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "fake-key")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeResp(200, {}, "", {"results": [
                {"title": "Asyncio Docs", "url": "http://docs/asyncio", "content": "事件循环"},
            ]}))
        out = web_search.func("python asyncio")
        assert "Asyncio Docs" in out and "http://docs/asyncio" in out and "事件循环" in out

    def test_no_results(self, monkeypatch):
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "fake-key")
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: FakeResp(200, {}, "", {"results": []}))
        assert "没搜到" in web_search.func("zzz")

    def test_bad_status(self, monkeypatch):
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "fake-key")
        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, {}, ""))
        assert "HTTP 401" in web_search.func("python")


class TestReadablePages:
    """抓网页只留正文、表格转行：原来维基页面前一千来字全是菜单，赛程表被拆成一格一行。"""

    WIKI = ("<html><head><title>英雄联盟2026赛季全球总决赛 - 维基百科</title></head><body>"
            "<a class='mw-jump-link' href='#content'>跳转到内容</a>"
            "<header><nav>主菜单 首页 分类索引 特色内容</nav></header>"
            "<div class='vector-toc'>目录 1 主办地遴选 2 比赛场馆</div>"
            "<main id='content'><h1>英雄联盟2026赛季全球总决赛</h1>"
            "<h2>主办地遴选<span class='mw-editsection'>[编辑]</span></h2>"
            "<p>本届大赛将在美国举行，比赛时间为2026年10月15日至11月14日" + "。" * 200 +
            "<sup class='reference'>[3]</sup></p>"
            "<table><tr><th>阶段</th><th>日期</th><th>城市</th></tr>"
            "<tr><td>瑞士轮</td><td>10月23日–11月1日</td><td>纽约</td></tr>"
            "<tr><td>决赛</td><td>11月14日</td><td>洛杉矶</td></tr></table>"
            "</main><footer>本页面最后修订于… 隐私政策</footer></body></html>")

    def _fetch(self, monkeypatch, html):
        seen = {}

        def fake_get(*a, **k):
            seen["headers"] = k.get("headers") or {}
            return FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, html)
        monkeypatch.setattr(requests, "get", fake_get)
        return fetch_url.func("http://x"), seen

    def test_keeps_the_article_and_drops_site_chrome(self, monkeypatch):
        out, _ = self._fetch(monkeypatch, self.WIKI)
        assert out.startswith("标题：英雄联盟2026赛季全球总决赛 - 维基百科")
        assert "本届大赛将在美国举行" in out
        for junk in ("跳转到内容", "主菜单", "目录 1", "[编辑]", "[3]", "隐私政策"):
            assert junk not in out, junk

    def test_tables_become_rows(self, monkeypatch):
        out, _ = self._fetch(monkeypatch, self.WIKI)
        assert "阶段 | 日期 | 城市" in out
        assert "瑞士轮 | 10月23日–11月1日 | 纽约" in out
        assert "决赛 | 11月14日 | 洛杉矶" in out

    def test_small_article_card_is_not_mistaken_for_the_page(self, monkeypatch):
        html = ("<body><article>推荐阅读</article><div id='content'>" + "正文内容。" * 80 +
                "</div></body>")
        out, _ = self._fetch(monkeypatch, html)
        assert "正文内容" in out

    def test_asks_for_chinese_pages_without_pretending_to_be_chrome(self, monkeypatch):
        """冒充 Chrome 实测没让任何 403 站放行，反而被核对出来拒掉原本能访问的站：标识保持如实。"""
        _, seen = self._fetch(monkeypatch, "<p>hi</p>")
        assert seen["headers"]["User-Agent"] == "Mozilla/5.0 (LingXi)"
        assert seen["headers"]["Accept-Language"].startswith("zh-CN")


class TestBetterSearch:
    def _search(self, monkeypatch, data, **kwargs):
        sent = {}

        def fake_post(url, json=None, **k):
            sent.update(json or {})
            return FakeResp(200, {}, "", data)
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "fake-key")
        monkeypatch.setattr(requests, "post", fake_post)
        return web_search.func("S16 赛程", **kwargs), sent

    def test_news_and_time_range_are_passed_through(self, monkeypatch):
        _, sent = self._search(monkeypatch, {"results": []}, topic="news", time_range="week")
        assert sent["topic"] == "news" and sent["time_range"] == "week"
        assert sent["include_answer"] is True

    def test_unknown_values_are_not_sent(self, monkeypatch):
        """乱填的值 Tavily 会整个请求报 400：不认识的就按默认。"""
        _, sent = self._search(monkeypatch, {"results": []}, topic="sports", time_range="fortnight",
                               max_results=99)
        assert sent["topic"] == "general" and "time_range" not in sent
        assert sent["max_results"] == 10

    def test_answer_first_and_snippets_cleaned_and_bounded(self, monkeypatch):
        noisy = ("瑞士轮第四轮抽签结果[编辑] [...] BLG 对 TES[12]，^ 12.0 G2 对 HLE。 " + "赛况" * 600)
        out, _ = self._search(monkeypatch, {
            "answer": "瑞士轮第四轮 10 月 29 日开赛。",
            "results": [{"title": "S16 瑞士轮抽签", "url": "http://news/1",
                         "published_date": "Wed, 28 Oct 2026 10:00:00 GMT", "content": noisy}],
        })
        assert out.startswith("直接答案") and "10 月 29 日" in out.split("\n")[0]
        assert "发布：Wed, 28 Oct 2026" in out
        snippet = out.split("\n")[-1].strip()
        assert "BLG 对 TES" in snippet and "G2 对 HLE" in snippet
        for junk in ("[编辑]", "[...]", "[12]", "^ 12.0"):
            assert junk not in snippet, junk
        assert len(snippet) <= 601 and snippet.endswith("…")

    def test_answer_alone_is_still_returned(self, monkeypatch):
        out, _ = self._search(monkeypatch, {"answer": "在洛杉矶。", "results": []})
        assert "在洛杉矶" in out and "没搜到" not in out
