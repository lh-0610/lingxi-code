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
        self._json = json_data

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
