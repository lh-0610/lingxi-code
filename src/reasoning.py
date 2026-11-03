"""思考强度（reasoning effort）：档位取值、模型类型支持能力、请求参数映射。

会话字段 `reasoning_effort` 存 `off / low / high / max`（`session.Session`，随会话落盘）。
`state.reasoning_enabled` 是它的派生视图（"不是 off 就是开"），其它模块继续读布尔值即可。

模型能力由 `supported_efforts()` 决定，界面菜单按它显示：

    档位集合                    适合谁
    off / low / high / max     Responses 协议（内置 responses、自定义 protocol=responses）
    off / on                   只能开关、不分档的（ollama）
    ()                         不支持思考的（其余类型；含声明了 supports_thinking 但协议里
                               没有通用思考参数可发的自定义模型——不猜字段，宁可当作无档位）

`effective()` 把"会话档位 + 该模型能力"折算成实际要发给模型的动作：
"off" 关、"on" 布尔开、low/high/max 分档开、None 表示忽略这个字段。
"""
EFFORTS = ("off", "low", "high", "max")
BOOLEAN_EFFORTS = ("off", "on")
DEFAULT = "high"

# 界面上的档位名（输入框底栏的模型按钮和它的"思考强度"子菜单）。只能开关的模型用 关 / 开。
LABELS = {"off": "关", "on": "开", "low": "低", "high": "高", "max": "最高"}

# 手改坏的值 / 别家叫法 → 归一化。medium 归到 high：分档只有四档，且 high 是默认。
_ALIASES = {
    "none": "off",
    "disabled": "off",
    "false": "off",
    "true": "high",
    "medium": "high",
    "mid": "high",
    "minimum": "low",
    "maximum": "max",
}


def normalize(value, default=DEFAULT) -> str:
    """把手里的值（旧布尔、手改坏的字符串）收成一个合法档位；认不出就取 default。"""
    if isinstance(value, bool):
        return "high" if value else "off"
    v = str(value or "").strip().lower()
    v = _ALIASES.get(v, v)
    return v if v in EFFORTS else default


def enabled(value) -> bool:
    """这个档位算不算"开着思考"（派生 state.reasoning_enabled 用）。"""
    return normalize(value) != "off"


def legacy_to_effort(value) -> str:
    """旧会话只有 reasoning_enabled 布尔时，按它推断档位：True → high，False → off。"""
    return "high" if bool(value) else "off"


def supported_efforts(mtype, *, protocol=None, supports_thinking=True) -> tuple:
    """某类型（+ 自定义模型协议）能表达哪些档位。空元组 = 不支持思考。"""
    if not supports_thinking:
        return ()
    if mtype == "responses":
        return EFFORTS
    if mtype == "custom":
        # 只有 responses 协议有通用分档字段（reasoning.effort）。其它协议各家字段不一，
        # 声明了 supports_thinking 也不乱发——宁可菜单里不显示档位。
        return EFFORTS if (protocol or "openai").lower() == "responses" else ()
    if mtype == "ollama":
        return BOOLEAN_EFFORTS
    return ()


def effective(value, supported) -> str | None:
    """会话档位 × 模型能力 → 实际动作："off" / "on" / low/high/max / None（忽略）。"""
    supported = tuple(supported or ())
    if not supported:
        return None
    effort = normalize(value)
    if effort == "off":
        return "off" if "off" in supported else None
    if effort in supported:
        return effort
    # 只能开关的模型：off 以外一律按"开"处理
    return "on" if "on" in supported else None
