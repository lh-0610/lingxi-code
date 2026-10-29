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
def _search_defaults(monkeypatch):
    """搜索相关配置固定成默认值：用户真实 config 里填了博查 / DeepSeek key、开了深度搜索时，
    测试不能因此去真实联网或改变行为（DeepSeek 的 key 是模型那边的，几乎总有）。"""
    monkeypatch.setattr(config, "BOCHA_API_KEY", "")
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(config, "WEB_SEARCH_DEEP", False)


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
        """Tavily 的 content 是用 [...] 隔开的几段相关原文：按段收，每段有上限，段间用 … 隔开。
        整条硬截会把后面几段相关内容一起砍掉。"""
        noisy = ("### 瑞士轮第四轮抽签结果[编辑] [...] BLG 对 TES[12]，^ 12.0 G2 对 HLE。" + "赛况" * 600 +
                 " [...] 10 月 29 日 MKOI 对 T1")
        out, _ = self._search(monkeypatch, {
            "answer": "瑞士轮第四轮 10 月 29 日开赛。",
            "results": [{"title": "S16 瑞士轮抽签", "url": "http://news/1",
                         "published_date": "Wed, 28 Oct 2026 10:00:00 GMT", "content": noisy}],
        })
        assert out.startswith("直接答案") and "10 月 29 日" in out.split("\n")[0]
        assert "发布：Wed, 28 Oct 2026" in out
        snippet = out.split("\n")[-1].strip()
        chunks = snippet.split(" … ")
        assert chunks[0] == "瑞士轮第四轮抽签结果"
        assert chunks[1].startswith("BLG 对 TES，") and "G2 对 HLE" in chunks[1]
        assert len(chunks[1]) <= 351 and chunks[1].endswith("…"), "长的那段有上限"
        assert chunks[2] == "10 月 29 日 MKOI 对 T1", "最后一段相关内容没被砍掉"
        for junk in ("[编辑]", "[...]", "[12]", "^ 12.0", "###"):
            assert junk not in snippet, junk

    def test_depth(self, monkeypatch):
        _, sent = self._search(monkeypatch, {"results": []}, depth="advanced")
        assert sent["search_depth"] == "advanced" and sent["chunks_per_source"] == 3
        _, sent = self._search(monkeypatch, {"results": []}, depth="deepest")
        assert "search_depth" not in sent, "不认识的值按默认 basic，不传"

    def test_answer_alone_is_still_returned(self, monkeypatch):
        out, _ = self._search(monkeypatch, {"answer": "在洛杉矶。", "results": []})
        assert "在洛杉矶" in out and "没搜到" not in out


# ── 搜索服务：Tavily / 博查 / 自动兜底 ──

def _bocha_page(name, url, summary, site="新浪体育", date="2026-10-28T10:00:00+08:00"):
    return {"name": name, "url": url, "siteName": site, "snippet": summary[:20],
            "summary": summary, "datePublished": date}


class _Router:
    """按网址分发假响应，记下每个服务收到的请求。"""

    def __init__(self, tavily=None, bocha=None, deepseek=None, deepseek_fetch=None):
        self.responses = {"tavily": tavily, "bocha": bocha, "deepseek": deepseek,
                          "deepseek_fetch": deepseek_fetch}
        self.sent = {}
        self.urls = []

    def post(self, url, json=None, headers=None, **k):
        if "bochaai" in url:
            name = "bocha"
        elif "/v1/tools/fetch" in url:
            name = "deepseek_fetch"
        elif "/v1/tools/search" in url:
            name = "deepseek"
        else:
            name = "tavily"
        self.urls.append(url)
        self.sent[name] = {"json": json or {}, "headers": headers or {}}
        resp = self.responses[name]
        return resp() if callable(resp) else resp


class TestProviders:
    def _setup(self, monkeypatch, router, tavily_key="tvly-x", bocha_key="sk-b", provider="auto"):
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", tavily_key)
        monkeypatch.setattr(config, "BOCHA_API_KEY", bocha_key)
        monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", provider)
        monkeypatch.setattr(requests, "post", router.post)

    def _ok_both(self):
        return _Router(
            tavily=FakeResp(200, {}, "", {"results": [{"title": "T", "url": "http://t/1", "content": "tavily 内容"}]}),
            bocha=FakeResp(200, {}, "", {"code": 200, "data": {"webPages": {"value": [
                _bocha_page("S16 瑞士轮抽签", "http://b/1", "BLG 对 TES，IG 对 LYON")]}}}),
        )

    def test_auto_sends_chinese_to_bocha_and_english_to_tavily(self, monkeypatch):
        router = self._ok_both()
        self._setup(monkeypatch, router)
        out = web_search.func("S16 瑞士轮 抽签")
        assert "BLG 对 TES" in out and "来源：新浪体育" in out and set(router.sent) == {"bocha"}
        router.sent.clear()
        out = web_search.func("Worlds 2026 swiss draw")
        assert "tavily 内容" in out and set(router.sent) == {"tavily"}

    def test_bocha_request_shape(self, monkeypatch):
        router = self._ok_both()
        self._setup(monkeypatch, router)
        web_search.func("S16 赛程", max_results=8, time_range="day")
        sent = router.sent["bocha"]
        assert sent["headers"]["Authorization"] == "Bearer sk-b"
        assert sent["json"] == {"query": "S16 赛程", "count": 8, "summary": True, "freshness": "oneDay"}
        web_search.func("S16 赛程", topic="news")
        assert router.sent["bocha"]["json"]["freshness"] == "oneWeek", "新闻没给时间范围：按最近一周"
        web_search.func("S16 赛程")
        assert router.sent["bocha"]["json"]["freshness"] == "noLimit"

    def test_falls_back_when_one_fails(self, monkeypatch):
        router = self._ok_both()
        router.responses["bocha"] = FakeResp(402, {}, "")
        self._setup(monkeypatch, router)
        out = web_search.func("S16 瑞士轮 抽签")
        assert "博查额度用完" in out and "改用 Tavily" in out and "tavily 内容" in out

    def test_bocha_business_error_counts_as_failure(self, monkeypatch):
        router = self._ok_both()
        router.responses["bocha"] = FakeResp(200, {}, "", {"code": 403, "msg": "余额不足", "data": None})
        self._setup(monkeypatch, router)
        assert "余额不足" in web_search.func("S16 赛程") and "tavily 内容" in web_search.func("S16 赛程")

    def test_fixed_provider_never_switches(self, monkeypatch):
        router = self._ok_both()
        router.responses["bocha"] = FakeResp(401, {}, "")
        self._setup(monkeypatch, router, provider="bocha")
        out = web_search.func("Worlds 2026 swiss draw")
        assert "博查 key 无效" in out and "tavily" not in router.sent, "选了只用博查，就不偷偷换成别家"

    def test_only_configured_services_are_tried(self, monkeypatch):
        router = self._ok_both()
        self._setup(monkeypatch, router, bocha_key="")
        web_search.func("S16 瑞士轮")
        assert set(router.sent) == {"tavily"}
        self._setup(monkeypatch, router, tavily_key="", bocha_key="")
        assert "未配置搜索服务" in web_search.func("S16")

    def test_bocha_long_summary_is_kept_longer(self, monkeypatch):
        router = self._ok_both()
        router.responses["bocha"] = FakeResp(200, {}, "", {"webPages": {"value": [
            _bocha_page("长文", "http://b/2", "赛况" * 400)]}})       # 没包 data 的老格式也认
        self._setup(monkeypatch, router)
        snippet = web_search.func("S16 赛况").split("\n")[-1].strip()
        assert 500 < len(snippet) <= 601 and snippet.endswith("…")


# ── 深度搜索：读前几个结果的原文，按问题挑段落 ──

class TestDeepSearch:
    PAGE = ("英雄联盟2026全球总决赛\n\n网站导航 首页 新闻 视频 下载\n\n"
            "瑞士轮第四轮 10月29日：T1 对 MKOI，BLG 对 TES，比分 2:1。\n\n"
            "| 场次 | 对阵 | 比分 |\n| 1 | T1 vs MKOI | 2:1 |\n\n"
            "版权所有 © 2026 联系我们 隐私政策")

    def test_picks_relevant_passages_in_original_order(self):
        from src.tools_web import _top_passages
        got = _top_passages("S16 瑞士轮第四轮 对阵 比分", self.PAGE)
        assert got and "瑞士轮第四轮" in got[0]
        assert not any("隐私政策" in p and "比分" not in p for p in got), "毫不相关的段落不要"
        assert _top_passages("完全无关的词语", "abc def") == []

    def test_deep_reads_top_results_and_keeps_snippets_when_a_page_fails(self, monkeypatch):
        from src import tools_web
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "tvly-x")
        monkeypatch.setattr(config, "WEB_SEARCH_DEEP", True)
        results = [{"title": f"R{i}", "url": f"http://r/{i}", "content": f"摘要{i}"} for i in range(5)]
        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {}, "", {"results": results}))
        fetched = []

        def fake_fetch(url, max_chars):
            fetched.append(url)
            return (False, "HTTP 403") if url.endswith("/1") else (True, self.PAGE)
        monkeypatch.setattr(tools_web, "_fetch_text", fake_fetch)
        out = web_search.func("瑞士轮第四轮 比分")
        assert sorted(fetched) == ["http://r/0", "http://r/1", "http://r/2"], "只读前 3 个"
        assert "深度搜索：前 2 个结果读了原文" in out
        assert out.count("原文：") >= 2 and "摘要1" in out, "403 的那篇保留原摘要"
        assert "摘要0" not in out, "读到原文的用原文段落替换摘要"

    def test_off_by_default(self, monkeypatch):
        from src import tools_web
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "tvly-x")
        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {}, "", {"results": [
            {"title": "R", "url": "http://r/0", "content": "摘要"}]}))
        monkeypatch.setattr(tools_web, "_fetch_text", lambda *a: (_ for _ in ()).throw(AssertionError("不该抓")))
        assert "原文：" not in web_search.func("瑞士轮")


def test_deep_mode_keeps_the_other_results_to_one_line(monkeypatch):
    """前几条已有原文段落，其余只留一句话当线索——照常给完整摘要，开了深度反而更费 token。"""
    from src import tools_web
    monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "tvly-x")
    monkeypatch.setattr(config, "WEB_SEARCH_DEEP", True)
    long = "第一段赛况" * 60 + " [...] " + "第二段赛况" * 60
    results = [{"title": f"R{i}", "url": f"http://r/{i}", "content": long} for i in range(6)]
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {}, "", {"results": results}))
    monkeypatch.setattr(tools_web, "_fetch_text", lambda url, n: (True, TestDeepSearch.PAGE))
    out = web_search.func("瑞士轮第四轮 比分")
    others = out.split("R5\n")[1].splitlines()
    snippet = others[-1].strip()
    assert len(snippet) <= 121 and "第二段" not in snippet


# ── DeepSeek 自家的搜索 / 抓取接口（和模型共用 key）──

def _ds_item(title, url, summary, content=""):
    return {"title": title, "url": url, "site_name": "新浪电竞", "date": "2026-10-28T16:00:00+00:00",
            "snippet": summary[:30], "summary": summary, "content": content,
            "rerank_score": 2.3, "authority": 3}


class TestDeepSeekSearch:
    def _setup(self, monkeypatch, router, ds_key="sk-real-ds", base="https://api.deepseek.com",
               tavily_key="tvly-x", provider="auto", deep=False):
        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", ds_key)
        monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", base)
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", tavily_key)
        monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", provider)
        monkeypatch.setattr(config, "WEB_SEARCH_DEEP", deep)
        monkeypatch.setattr(requests, "post", router.post)

    def _router(self, items=None):
        items = items if items is not None else [_ds_item("IG 2:1 LYON", "http://ds/1", "IG 2:1 战胜 LYON 晋级八强")]
        return _Router(tavily=FakeResp(200, {}, "", {"results": [{"title": "T", "url": "http://t/1", "content": "tavily 内容"}]}),
                       deepseek=FakeResp(200, {}, "", {"results": items}))

    def test_chinese_goes_to_deepseek_first(self, monkeypatch):
        router = self._router()
        self._setup(monkeypatch, router)
        out = web_search.func("S16 瑞士轮 第四轮 比分")
        assert "IG 2:1 战胜 LYON" in out and "来源：新浪电竞" in out
        assert router.urls == ["https://api.deepseek.com/v1/tools/search"]
        assert router.sent["deepseek"]["headers"]["Authorization"] == "Bearer sk-real-ds"
        router.sent.clear()
        web_search.func("Worlds 2026 swiss round 4")
        assert "tavily" in router.sent and "deepseek" not in router.sent

    def test_time_range_becomes_an_after_operator(self, monkeypatch):
        from datetime import date, timedelta
        router = self._router()
        self._setup(monkeypatch, router)
        web_search.func("S16 赛程", time_range="week", max_results=8)
        sent = router.sent["deepseek"]["json"]
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        assert sent == {"query": f"S16 赛程 after:{week_ago}", "count": 8, "need_content": False}
        web_search.func("S16 赛程", topic="news")
        assert router.sent["deepseek"]["json"]["query"].endswith(f"after:{week_ago}")
        web_search.func("赛程" * 100, time_range="day")
        q = router.sent["deepseek"]["json"]["query"]
        assert len(q) <= 128 and " after:" in q, "超长查询截断时保住时间条件"

    def test_base_url_with_v1_and_placeholder_key(self, monkeypatch):
        router = self._router()
        self._setup(monkeypatch, router, base="https://api.deepseek.com/v1/")
        web_search.func("S16 赛程")
        assert router.urls[-1] == "https://api.deepseek.com/v1/tools/search"
        router.sent.clear()
        self._setup(monkeypatch, router, ds_key="sk-xxxxxxxxxxxxxxxx")
        web_search.func("S16 赛程")
        assert "deepseek" not in router.sent, "示例配置里的占位 key 不能拿去请求"

    def test_insufficient_balance_falls_back(self, monkeypatch):
        router = self._router()
        router.responses["deepseek"] = FakeResp(402, {}, "", {"error": {"message": "Insufficient Balance"}})
        self._setup(monkeypatch, router)
        out = web_search.func("S16 赛程")
        assert "账户余额不足" in out and "改用 Tavily" in out and "tavily 内容" in out

    def test_deep_search_uses_the_returned_page_text(self, monkeypatch):
        from src import tools_web
        page = TestDeepSearch.PAGE
        router = self._router([_ds_item("S16 战报", "http://ds/1", "摘要", content=page)])
        self._setup(monkeypatch, router, deep=True)
        monkeypatch.setattr(tools_web, "_fetch_text", lambda *a: (_ for _ in ()).throw(AssertionError("不该自己抓")))
        out = web_search.func("瑞士轮第四轮 比分")
        assert router.sent["deepseek"]["json"]["need_content"] is True
        assert "原文：" in out and "T1 对 MKOI" in out


class TestFetchFallback:
    def _setup(self, monkeypatch, router, ds_key="sk-real-ds"):
        monkeypatch.setattr(config, "DEEPSEEK_API_KEY", ds_key)
        monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setattr(requests, "post", router.post)

    def test_site_refusal_is_retried_through_deepseek(self, monkeypatch):
        router = _Router(deepseek_fetch=FakeResp(200, {}, "", {"title": "Worlds 2026", "content": "# Swiss stage\nT1 2:1 MKOI"}))
        self._setup(monkeypatch, router)
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(403, {}, ""))
        out = fetch_url.func("http://blocked.example/a")
        assert "网站拒绝了直接访问：HTTP 403" in out and "T1 2:1 MKOI" in out and "标题：Worlds 2026" in out
        assert router.sent["deepseek_fetch"]["json"] == {"url": "http://blocked.example/a"}

    def test_not_forwarded_when_we_blocked_it_or_the_page_is_gone(self, monkeypatch):
        router = _Router(deepseek_fetch=FakeResp(200, {}, "", {"title": "x", "content": "y"}))
        self._setup(monkeypatch, router)
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda host, *a, **k: [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))])
        assert "拒绝" in fetch_url.func("http://169.254.169.254/latest/meta-data/")
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda host, *a, **k: [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))])
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(404, {}, ""))
        assert "HTTP 404" in fetch_url.func("http://x/gone")
        assert "deepseek_fetch" not in router.sent, "内网地址、404 都不转给第三方"

    def test_no_key_means_no_fallback(self, monkeypatch):
        router = _Router()
        self._setup(monkeypatch, router, ds_key="")
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(403, {}, ""))
        assert fetch_url.func("http://blocked.example/a").startswith("HTTP 403") and not router.sent


class TestPageEncoding:
    """响应头没写 charset 时 requests 按 ISO-8859-1 解，中文网页成了乱码。"""

    class _Raw:
        def __init__(self, raw, content_type):
            self.headers = {"Content-Type": content_type}
            self.encoding = requests.utils.get_encoding_from_headers(self.headers)
            self._raw = raw

    def _decode(self, raw, content_type="text/html"):
        from src.tools_web import _decode_body
        return _decode_body(raw, self._Raw(raw, content_type))

    def test_utf8_without_any_declaration(self):
        assert self._decode("T1 击败 MKOI 晋级八强".encode("utf-8")) == "T1 击败 MKOI 晋级八强"

    def test_meta_charset_is_honoured(self):
        page = '<html><head><meta charset="gbk"></head><body>晋级八强</body></html>'
        assert "晋级八强" in self._decode(page.encode("gbk"))

    def test_gbk_without_declaration_still_reads(self):
        assert self._decode("瑞士轮第四轮".encode("gbk")) == "瑞士轮第四轮"

    def test_header_charset_wins(self):
        assert self._decode("晋级".encode("gbk"), "text/html; charset=gbk") == "晋级"

    def test_cut_in_the_middle_of_a_character_is_still_utf8(self):
        raw = "晋级八强".encode("utf-8")[:-1]
        assert self._decode(raw).startswith("晋级八")
