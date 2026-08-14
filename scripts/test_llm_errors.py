"""模型请求错误分类与重试策略的单测。

分类错了的代价是不对称的：把瞬时网络错误误判成鉴权失败，会让本来能自愈的请求直接失败；
把上下文溢出误判成瞬时错误，会退避重试三次、每次必然再溢出。所以既要测"认得出"，
也要测"认不出时安全地退化成 UNKNOWN"。
"""
import pytest

from src.llm_errors import (
    AUTH, CONTEXT_OVERFLOW, RATE_LIMIT, TRANSIENT, UNKNOWN,
    ContextOverflowError, classify, retry_after_seconds, retry_plan,
)


class _Resp:
    def __init__(self, status=None, headers=None):
        if status is not None:
            self.status_code = status
        self.headers = headers or {}


class _Err(Exception):
    """模拟各家 SDK 的异常：可带 status_code，或挂一个 response。"""

    def __init__(self, msg, status=None, response=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if response is not None:
            self.response = response


class TestClassifyByStatus:
    @pytest.mark.parametrize("status,expect", [
        (429, RATE_LIMIT), (401, AUTH), (403, AUTH),
        (500, TRANSIENT), (502, TRANSIENT), (503, TRANSIENT), (504, TRANSIENT), (408, TRANSIENT),
    ])
    def test_status_codes(self, status, expect):
        assert classify(_Err("boom", status=status)) == expect

    def test_status_on_nested_response(self):
        assert classify(_Err("boom", response=_Resp(429))) == RATE_LIMIT


class TestClassifyByMessage:
    @pytest.mark.parametrize("msg", [
        "This model's maximum context length is 8192 tokens",
        "prompt is too long: 250000 tokens",
        "context_length_exceeded",
        "Input is too long for requested model",
        "Request too large for gpt-4",
    ])
    def test_overflow(self, msg):
        assert classify(_Err(msg)) == CONTEXT_OVERFLOW

    @pytest.mark.parametrize("msg", [
        "Rate limit reached for requests",
        "Too Many Requests",
        "quota exceeded for this month",
    ])
    def test_rate_limit(self, msg):
        assert classify(_Err(msg)) == RATE_LIMIT

    @pytest.mark.parametrize("msg", [
        "Invalid API key provided",
        "authentication_error: check your credentials",
        "Unauthorized",
    ])
    def test_auth(self, msg):
        assert classify(_Err(msg)) == AUTH

    @pytest.mark.parametrize("msg", [
        "Connection reset by peer",
        "Read timed out",
        "Service Unavailable",
        "Overloaded",
    ])
    def test_transient(self, msg):
        assert classify(_Err(msg)) == TRANSIENT

    def test_unrecognized_falls_back_to_unknown(self):
        """认不出来必须退化成 UNKNOWN（= 保持原有的退避重试行为），绝不能瞎猜。"""
        assert classify(_Err("something entirely unexpected")) == UNKNOWN


class TestClassifyPriority:
    def test_overflow_beats_rate_limit_wording(self):
        """有的 provider 把超窗报成含 'limit' 字样的长文案，不能被限流规则抢走。"""
        msg = "Your input exceeds the maximum context length limit for this model"
        assert classify(_Err(msg)) == CONTEXT_OVERFLOW

    def test_explicit_exception_type_wins(self):
        assert classify(ContextOverflowError("whatever")) == CONTEXT_OVERFLOW

    def test_class_name_recognized(self):
        class RateLimitError(Exception):
            pass
        assert classify(RateLimitError("no details")) == RATE_LIMIT


class TestRetryAfter:
    def test_from_header(self):
        assert retry_after_seconds(_Err("x", response=_Resp(429, {"retry-after": "7"}))) == 7.0

    def test_from_message(self):
        assert retry_after_seconds(_Err("slow down, retry-after: 12.5")) == 12.5

    def test_absent(self):
        assert retry_after_seconds(_Err("nothing here")) is None

    def test_garbage_header_does_not_raise(self):
        assert retry_after_seconds(_Err("x", response=_Resp(429, {"retry-after": "soon"}))) is None


class TestRetryPlan:
    def test_auth_never_retries(self):
        """key 错了不会因为等 4 秒变对——立刻失败并说清楚。"""
        should, delay, reason = retry_plan(_Err("bad key", status=401), 0, 3)
        assert should is False and delay == 0.0 and "密钥" in reason

    def test_overflow_never_retries_here(self):
        """溢出不在这里退避重试：原样重发必然再溢出，交给上层压缩后再发。"""
        should, _, reason = retry_plan(_Err("maximum context length"), 0, 3)
        assert should is False and "上下文" in reason

    def test_rate_limit_honors_retry_after(self):
        should, delay, _ = retry_plan(
            _Err("rate limit", status=429, response=_Resp(429, {"retry-after": "9"})), 0, 3)
        assert should is True and delay == 9.0

    def test_rate_limit_falls_back_to_backoff(self):
        should, delay, _ = retry_plan(_Err("rate limit", status=429), 2, 5)
        assert should is True and delay == 4.0        # 2^2

    def test_absurd_retry_after_is_capped(self):
        """服务端偶尔给出几百上千秒，照单全收等于把 UI 挂死。"""
        should, delay, _ = retry_plan(
            _Err("rate limit", status=429, response=_Resp(429, {"retry-after": "3600"})), 0, 3)
        assert should is True and delay == 60.0

    def test_transient_uses_exponential_backoff(self):
        assert retry_plan(_Err("timeout", status=503), 0, 3)[1] == 1.0
        assert retry_plan(_Err("timeout", status=503), 1, 3)[1] == 2.0

    def test_last_attempt_stops(self):
        should, _, reason = retry_plan(_Err("timeout", status=503), 2, 3)
        assert should is False and "3" in reason
