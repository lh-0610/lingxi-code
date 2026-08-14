"""模型请求错误的分类与重试策略。

为什么要分类：原来的重试对所有异常一视同仁——指数退避重试 N 次。这对瞬时网络错是对的，
对另外三类是错的甚至有害：

  - **上下文溢出**：重试必然再次溢出。退避三次只是让用户多等三个周期拿到同一个错误。
    正确做法是把它当成"该压缩了"的信号，压完再发。
  - **鉴权失败**：key 错了不会因为等 4 秒就变对。应当立刻失败并说清楚，别浪费重试。
  - **限流**：服务端通常在 Retry-After 里给了确切的等待时间，盲目用 2^n 要么等太短
    继续撞墙、要么等太久白白拖慢。

分类不 import 任何 provider SDK（anthropic / openai / httpx 的异常类型各不相同，而且
自定义模型走什么协议是用户配的）。改用三层判据，从最可靠到最兜底：
  ① 异常对象上的 status_code / response.status_code
  ② 异常类名（字符串匹配，例如 RateLimitError / AuthenticationError）
  ③ 错误消息里的特征词

宁可漏判成 UNKNOWN（退化成原来的行为）也不要误判：把一个瞬时网络错误误判成鉴权失败，
会让本来能自愈的请求直接失败。
"""
import re

# ── 错误类别 ──
RATE_LIMIT = "rate_limit"            # 限流，按 Retry-After 或退避后重试
CONTEXT_OVERFLOW = "context_overflow"  # 上下文超窗，压缩后才有意义重试
AUTH = "auth"                        # 鉴权/权限，重试无意义
TRANSIENT = "transient"              # 瞬时网络/服务端错误，退避重试
UNKNOWN = "unknown"                  # 认不出来，按原行为退避重试


class ContextOverflowError(RuntimeError):
    """请求超出模型上下文窗口。单独一个类型，让 agent 主循环能识别并走压缩重试路径。"""


# 消息特征词。全部转小写后匹配，覆盖 Anthropic / OpenAI / DashScope / DeepSeek 的常见措辞。
_OVERFLOW_PATTERNS = (
    "context length", "context window", "maximum context",
    "too many tokens", "prompt is too long", "input is too long",
    "exceeds the maximum", "reduce the length", "context_length_exceeded",
    "request too large", "string too long",
)
_RATE_PATTERNS = (
    "rate limit", "rate_limit", "too many requests", "quota exceeded",
    "requests per minute", "throttl",
)
_AUTH_PATTERNS = (
    "invalid api key", "incorrect api key", "authentication", "unauthorized",
    "invalid_api_key", "permission denied", "forbidden", "api key not valid",
    "no api key", "invalid token",
)
_TRANSIENT_PATTERNS = (
    "timed out", "timeout", "connection reset", "connection aborted",
    "connection error", "temporarily unavailable", "service unavailable",
    "bad gateway", "internal server error", "remote end closed", "eof occurred",
    "overloaded",
)


def _status_of(exc) -> int | None:
    """尽力取出 HTTP 状态码：异常自身的 status_code，或它挂着的 response。"""
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        for attr in ("status_code", "status"):
            v = getattr(obj, attr, None)
            if isinstance(v, int):
                return v
    return None


def classify(exc) -> str:
    """把一个模型请求异常归类。返回上面五个常量之一。"""
    if isinstance(exc, ContextOverflowError):
        return CONTEXT_OVERFLOW
    name = type(exc).__name__.lower()
    text = f"{exc}".lower()
    status = _status_of(exc)

    # ① 状态码最可靠。注意 400 不能一概而论——上下文超窗在多数 provider 上就是 400，
    #    但 400 也可能是别的参数错误，所以 400 仍要落到消息特征判断。
    if status == 429:
        return RATE_LIMIT
    if status in (401, 403):
        return AUTH
    if status in (408, 500, 502, 503, 504):
        return TRANSIENT

    # ② 异常类名（各家 SDK 都爱用这几个名字）
    if "ratelimit" in name:
        return RATE_LIMIT
    if "authentication" in name or "permissiondenied" in name:
        return AUTH
    if any(k in name for k in ("timeout", "connection", "apiconnection", "internalserver")):
        return TRANSIENT

    # ③ 消息特征词。溢出放在限流之前判：有的 provider 会把超窗也报成 400 + 长文案，
    #    其中偶尔含 "limit" 字样，先判溢出可避免被 _RATE_PATTERNS 抢走。
    if any(p in text for p in _OVERFLOW_PATTERNS):
        return CONTEXT_OVERFLOW
    if any(p in text for p in _RATE_PATTERNS):
        return RATE_LIMIT
    if any(p in text for p in _AUTH_PATTERNS):
        return AUTH
    if any(p in text for p in _TRANSIENT_PATTERNS):
        return TRANSIENT
    return UNKNOWN


_RETRY_AFTER_RE = re.compile(r"retry[- _]?after[\"'\s:=]+([0-9]+(?:\.[0-9]+)?)", re.I)


def retry_after_seconds(exc) -> float | None:
    """限流时服务端给的确切等待秒数：先读 response headers，读不到再从消息里抠。
    拿不到返回 None（调用方退回指数退避）。"""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        try:
            for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
                v = headers.get(key)
                if v is not None:
                    return max(0.0, float(str(v).strip()))
        except (TypeError, ValueError, AttributeError):
            pass
    m = _RETRY_AFTER_RE.search(f"{exc}")
    if m:
        try:
            return max(0.0, float(m.group(1)))
        except ValueError:
            return None
    return None


# 单次等待上限：服务端偶尔会给出离谱的 Retry-After（几百上千秒），照单全收等于把 UI 挂死。
MAX_RETRY_DELAY_S = 60.0


def retry_plan(exc, attempt: int, max_attempts: int) -> tuple[bool, float, str]:
    """给出这次失败的处理方案。

    attempt 从 0 开始计。返回 (是否重试, 等待秒数, 给用户看的原因)。
    """
    kind = classify(exc)
    last = attempt >= max_attempts - 1

    if kind == AUTH:
        return False, 0.0, "API 密钥无效或无权限，重试不会有帮助——请检查配置里的 key。"
    if kind == CONTEXT_OVERFLOW:
        # 不在这里退避重试：交给上层压缩后再发（原样重试必然再溢出）。
        return False, 0.0, "对话超出模型上下文窗口。"
    if last:
        return False, 0.0, f"重试 {max_attempts} 次仍失败。"

    if kind == RATE_LIMIT:
        delay = retry_after_seconds(exc)
        if delay is None:
            delay = float(2 ** attempt)
        return True, min(delay, MAX_RETRY_DELAY_S), "触发服务端限流"
    return True, float(2 ** attempt), "模型请求失败"
