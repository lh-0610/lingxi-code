"""浏览器安全判定纯逻辑单测（不依赖 Qt / WebEngine）。

穷举 docs/browser-design.md §6.1 的动作分级 × 权限模式表、§6.2 网址规则、
有后果结构判定、来源授权、输入方向密钥防外泄。
"""
import pytest

from src.browser import security as sec


# ── 网址规则（§6.2）─────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://localhost:3000", "https://example.com", "http://127.0.0.1:8080/x",
    "https://[::1]:443/", "http://foo.localhost:5173",
])
def test_validate_url_allows_http_https(url):
    assert sec.validate_url(url) == ""


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini", "chrome://settings", "data:text/html,<script>",
    "javascript:alert(1)", "blob:http://x/y", "about:blank", "ftp://x/y",
    "", "http://", "not a url",
])
def test_validate_url_rejects_rest(url):
    assert sec.validate_url(url) != ""


def test_validate_url_no_ssrf_block_on_localhost():
    # 与 fetch_url 有意不同：localhost / 内网不被拦（localhost 是主用途）
    assert sec.validate_url("http://127.0.0.1:3000") == ""
    assert sec.validate_url("http://192.168.1.10:8080") == ""


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]", "foo.localhost", "LOCALHOST"])
def test_is_localhost_host_true(host):
    assert sec.is_localhost_host(host) is True


@pytest.mark.parametrize("host", ["example.com", "192.168.1.1", "10.0.0.1", "localhost.evil.com", ""])
def test_is_localhost_host_false(host):
    assert sec.is_localhost_host(host) is False


def test_is_localhost_url():
    assert sec.is_localhost("http://localhost:3000/x") is True
    assert sec.is_localhost("https://example.com") is False


@pytest.mark.parametrize("url,origin", [
    ("http://localhost:3000", "http://localhost:3000"),
    ("http://localhost:3000/a/b", "http://localhost:3000"),
    ("http://localhost", "http://localhost"),                 # 默认端口省略
    ("https://example.com", "https://example.com"),
    ("https://example.com:443", "https://example.com"),
    ("http://example.com:80", "http://example.com"),
    ("http://example.com:8080", "http://example.com:8080"),
    ("HTTP://Example.COM:8080", "http://example.com:8080"),   # 大小写归一
    ("http://[::1]:3000", "http://[::1]:3000"),
    ("https://[::1]", "https://[::1]"),
])
def test_parse_origin(url, origin):
    assert sec.parse_origin(url) == origin


def test_parse_origin_different_ports_are_different():
    assert sec.parse_origin("http://localhost:3000") != sec.parse_origin("http://localhost:3001")


def test_parse_origin_invalid_returns_empty():
    assert sec.parse_origin("file:///x") == ""
    assert sec.parse_origin("garbage") == ""


# ── 有后果判定（§6.1 拍板 6）─────────────────────────────────────
@pytest.mark.parametrize("info,submit", [
    ({"tag": "button", "type": "submit", "name": "保存", "inForm": True, "formMethod": "post"}, False),
    ({"tag": "input", "type": "submit", "name": "提交", "inForm": True, "formMethod": "post"}, False),
    ({"tag": "input", "type": "image", "name": "提交", "inForm": True, "formMethod": "post"}, False),   # image 输入也是提交
    ({"tag": "button", "type": "submit", "inForm": True, "formMethod": "post"}, False),
    ({"tag": "input", "type": "text", "inForm": True, "formMethod": "post"}, True),  # POST 表单内回车
    ({"tag": "input", "type": "text", "formMethod": "post"}, True),
    ({"tag": "button", "type": "button", "name": "删除账户"}, False),  # 关键词为辅
    ({"tag": "button", "type": "button", "name": "立即购买"}, False),
    ({"tag": "a", "name": "Checkout"}, False),
    # 复审 #20：GET 表单里的按钮自带 formmethod=post
    ({"tag": "button", "type": "submit", "inForm": True, "formMethod": "get",
      "btnFormMethod": "post", "name": "关闭账户"}, False),
    ({"tag": "input", "type": "submit", "inForm": True, "formMethod": "get",
      "btnFormMethod": "POST", "name": "go"}, False),
    # 复审 #20：onSubmit + fetch 的表单看着像 GET，只能靠按钮名
    ({"tag": "button", "type": "submit", "inForm": True, "formMethod": "get", "name": "确定"}, False),
    ({"tag": "button", "type": "button", "name": "确认"}, False),
    ({"tag": "button", "type": "button", "name": "保存设置"}, False),
    ({"tag": "button", "type": "button", "name": "Save"}, False),
    ({"tag": "button", "type": "button", "name": "OK"}, False),
])
def test_is_consequential_true(info, submit):
    assert sec.is_consequential(info, submit=submit) is True


@pytest.mark.parametrize("info,submit", [
    ({"tag": "button", "type": "button", "name": "添加一行"}, False),
    ({"tag": "a", "name": "其它页面", "href": "/x"}, False),
    ({"tag": "input", "type": "text", "name": "用户名"}, False),
    ({"tag": "input", "type": "text", "inForm": True}, False),      # GET 表单输入不提交
    ({"tag": "input", "type": "text", "inForm": True, "formMethod": "get"}, True),  # GET 回车按普通点击
    ({"tag": "button", "type": "submit", "inForm": True, "formMethod": "get", "name": "搜索"}, False),
    ({"tag": "button", "type": "submit", "name": "下一步"}, False),   # 表单外 submit 按钮惰性
    ({"tag": "button", "type": "button", "name": "Facebook"}, False),  # ok 按整词，不误中 book
    ({"tag": "button", "type": "button", "name": "token 设置"}, False),
    # GET 表单里按钮写 formmethod=get：照普通点击
    ({"tag": "button", "type": "submit", "inForm": True, "formMethod": "post",
      "btnFormMethod": "get", "name": "搜索"}, False),
    ({"tag": "input", "type": "image", "inForm": False}, False),
    ({}, False),
    (None, False),
])
def test_is_consequential_false(info, submit):
    assert sec.is_consequential(info, submit=submit) is False


@pytest.mark.parametrize("name", ["发送", "发布", "send message", "publish now"])
def test_consequence_keywords_send_publish(name):
    assert sec.is_consequential({"tag": "button", "type": "button", "name": name}) is True


def test_enter_submit_outside_form_depends_on_origin():
    """表单外回车（回车即发送输入框）：公网算有后果，信任本机不算，其它本机宁可多问。"""
    info = {"tag": "input", "type": "text", "name": "消息"}
    assert sec.is_consequential(info, submit=True, origin=sec.ORIGIN_PUBLIC) is True
    assert sec.is_consequential(info, submit=True, origin=sec.ORIGIN_TRUSTED) is False
    assert sec.is_consequential(info, submit=True, origin=sec.ORIGIN_LOCALHOST) is True
    assert sec.is_consequential(info, submit=False, origin=sec.ORIGIN_PUBLIC) is False


def test_get_form_submit_is_rememberable_trusted():
    """GET 搜索按钮：信任来源 manual 下放行（可记住），不再 ask_always。"""
    info = {"tag": "button", "type": "submit", "inForm": True,
            "formMethod": "get", "name": "搜索"}
    c = sec.is_consequential(info, origin=sec.ORIGIN_TRUSTED)
    assert c is False
    assert sec.decide_action("manual", action="interact",
                             origin=sec.ORIGIN_TRUSTED, consequential=c) == sec.ALLOW


# ── 来源类别 ─────────────────────────────────────────────────────
def test_origin_kind():
    allow = {"http://localhost:3000"}
    assert sec.origin_kind("http://localhost:3000/x", allow) == sec.ORIGIN_TRUSTED
    assert sec.origin_kind("http://localhost:9999", allow) == sec.ORIGIN_LOCALHOST
    assert sec.origin_kind("http://127.0.0.1:5173", None) == sec.ORIGIN_LOCALHOST
    assert sec.origin_kind("https://example.com", allow) == sec.ORIGIN_PUBLIC


def test_stricter_origin():
    """iframe 门控：顶层与 frame 取更严的来源。"""
    t, lo, p = sec.ORIGIN_TRUSTED, sec.ORIGIN_LOCALHOST, sec.ORIGIN_PUBLIC
    assert sec.stricter_origin(t, p) == p
    assert sec.stricter_origin(p, t) == p
    assert sec.stricter_origin(t, lo) == lo
    assert sec.stricter_origin(lo, t) == lo
    assert sec.stricter_origin(t, t) == t
    assert sec.stricter_origin(lo, p) == p


# ── 动作分级 × 权限模式（§6.1 表，穷举）──────────────────────────
_MODES = ["manual", "accept_edits", "auto", "bypass", "plan"]


@pytest.mark.parametrize("mode", _MODES)
def test_readonly_always_allow(mode):
    assert sec.decide_action(mode, action="readonly", origin=sec.ORIGIN_PUBLIC) == sec.ALLOW


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("origin", [sec.ORIGIN_TRUSTED, sec.ORIGIN_LOCALHOST])
def test_open_localhost_always_allow(mode, origin):
    assert sec.decide_action(mode, action="open", origin=origin) == sec.ALLOW


@pytest.mark.parametrize("mode,expected", [
    ("manual", sec.ASK), ("accept_edits", sec.ASK),
    ("auto", sec.ALLOW), ("bypass", sec.ALLOW), ("plan", sec.ALLOW),
])
def test_open_public(mode, expected):
    assert sec.decide_action(mode, action="open", origin=sec.ORIGIN_PUBLIC) == expected


@pytest.mark.parametrize("mode,expected", [
    ("manual", sec.ALLOW), ("accept_edits", sec.ALLOW),
    ("auto", sec.ALLOW), ("bypass", sec.ALLOW), ("plan", sec.BLOCK),
])
def test_interact_trusted(mode, expected):
    assert sec.decide_action(mode, action="interact", origin=sec.ORIGIN_TRUSTED) == expected


@pytest.mark.parametrize("mode,expected", [
    ("manual", sec.ASK), ("accept_edits", sec.ASK),
    ("auto", sec.ASK), ("bypass", sec.ALLOW), ("plan", sec.BLOCK),
])
def test_interact_other_localhost(mode, expected):
    assert sec.decide_action(mode, action="interact", origin=sec.ORIGIN_LOCALHOST) == expected


@pytest.mark.parametrize("mode,expected", [
    ("manual", sec.ASK), ("accept_edits", sec.ASK),
    ("auto", sec.ALLOW), ("bypass", sec.ALLOW), ("plan", sec.BLOCK),
])
def test_interact_public(mode, expected):
    assert sec.decide_action(mode, action="interact", origin=sec.ORIGIN_PUBLIC) == expected


@pytest.mark.parametrize("mode", ["manual", "accept_edits", "auto"])
@pytest.mark.parametrize("origin", [sec.ORIGIN_TRUSTED, sec.ORIGIN_LOCALHOST, sec.ORIGIN_PUBLIC])
def test_consequential_always_ask(mode, origin):
    # 有后果：来源白名单绕不过，永远问、不给记住（bypass 除外）
    assert sec.decide_action(mode, action="interact", origin=origin, consequential=True) == sec.ASK_ALWAYS


def test_consequential_bypass_allow_plan_block():
    assert sec.decide_action("bypass", action="interact", origin=sec.ORIGIN_PUBLIC, consequential=True) == sec.ALLOW
    assert sec.decide_action("plan", action="interact", origin=sec.ORIGIN_TRUSTED, consequential=True) == sec.BLOCK


def test_is_rememberable():
    assert sec.is_rememberable(sec.ASK) is True
    for d in (sec.ASK_ALWAYS, sec.ALLOW, sec.BLOCK):
        assert sec.is_rememberable(d) is False


# ── 输入方向密钥防外泄（§6.5）─────────────────────────────────────
def test_secret_in_text_hit():
    secrets = {"sk-abcdef1234567890": ("openai_api_key", True)}
    assert sec.secret_in_text("填 sk-abcdef1234567890 进去", secrets) == "openai_api_key"


def test_secret_in_text_short_value_ignored():
    secrets = {"short": ("k", True)}
    assert sec.secret_in_text("short", secrets) == ""


def test_secret_in_text_no_match():
    secrets = {"sk-abcdef1234567890": ("k", True)}
    assert sec.secret_in_text("普通测试值", secrets) == ""
    assert sec.secret_in_text("", secrets) == ""
    assert sec.secret_in_text("anything", {}) == ""


def test_fence_url_escapes():
    assert sec.fence_url('http://x/"<y>') == "http://x/&quot;&lt;y&gt;"