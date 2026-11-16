"""离线回放：把已保存的会话过一遍**真实**的发送准备流程，看前缀稳不稳、折算成本多少。

不联网、不写盘、不改 chat_memory：会话只读，日志/配置/数据根全部指到临时目录，MCP 打桩，
压缩要调的 LLM 换成占位实例。走的是 `streaming._prepare_stream_history` 整条流程（system
重渲染、尾部易变消息、剥 reasoning、工具结果回收、硬截断、压缩判定、修配对），前缀比对直接
复用 `src/cache_fingerprints.py`，成本折算直接复用 `src/run_ledger.py`——都不另写一份。

用法：
    python scripts/replay_sessions.py                            # 本地 chat_memory/ 全部会话
    python scripts/replay_sessions.py 20261103_121425_632232 ...  # 只回放指定会话 ID
    python scripts/replay_sessions.py --dir D:/别处的/chat_memory --model deepseek-chat

**局限（成本是近似，用来看改动前后的相对退化，不是账单）**：
- 历史上每轮的 system prompt 和易变运行态无法复原，用的是**当前值**；项目上下文/项目规则/
  长期记忆一律不注入（数据根是空的临时目录、当前项目为 None），所以 system 比真实运行时短。
- token 是 `_estimate_tokens` 的粗估，不含工具 schema（真实输入约多 1.5 万）。
- "命中"按"与上一轮逐字节相同的前缀"估算，不是服务商真实上报的缓存命中；服务端淘汰、
  路由换实例这些看不出来。输出按会话里记下的那条 AI 消息估算，不含思考 token。
- 回收线/历史预算取当前配置，不是当时那次的配置。
- 所有会话统一按**同一个模型**回放和计价（默认第一个 API 模型，`--model` 可换，表头会打印
  用的是哪个），不按各会话当时实际用的模型——跨会话比成本时这点要记住。
"""
import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
import unicodedata
from contextlib import contextmanager

# ── 隔离：照 scripts/conftest.py，必须在 import src 之前 ──
# 已经设过就不动（pytest 里 conftest 先设好了，两边共用同一套临时目录）。
if not os.environ.get("LINGXI_LOG_DIR"):
    os.environ["LINGXI_LOG_DIR"] = tempfile.mkdtemp(prefix="lingxi-replay-logs-")
if not os.environ.get("LINGXI_CONFIG_PATH"):
    os.environ["LINGXI_CONFIG_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="lingxi-replay-cfg-"), "config.json")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import src.mcp_client as _mcp_client  # noqa: E402

_mcp_client.init_mcp = lambda: []

from langchain_core.messages import AIMessage  # noqa: E402

from src import config, memory, paths, session, state, streaming  # noqa: E402
from src.run_ledger import RunLedger  # noqa: E402
from src.subagent import HeadlessUI  # noqa: E402

# 这些文件不是会话（索引/角色卡/项目列表/长期记忆/权限规则/工具边界 sidecar）
_NON_SESSION_FILES = frozenset((
    "index.json", "role_config.json", "projects.json", "long_term_memory.json",
    "permission_rules.json", "last_model.json", "theme_config.json", "ui_prefs.json",
))


class OfflineLLM:
    """回放不联网：`_compact_history` 会调 `state.llm.invoke`，这里给一个占位实例。"""

    max_tokens = None

    def invoke(self, messages):
        return AIMessage(content="[离线回放占位摘要]")


def default_model_index() -> int:
    """回放用的模型：跳过本地 ollama 和 Claude Code CLI（后者根本不走这条发送流程）。"""
    for index, model in enumerate(streaming.MODEL_LIST):
        if model[1] not in ("ollama", "claude-code"):
            return index
    return 0


def model_index_for(model_id: str) -> int:
    for index, model in enumerate(streaming.MODEL_LIST):
        if model[2] == model_id:
            return index
    raise SystemExit(f"配置里没有 model_id={model_id!r} 的模型；可用：\n  "
                     + "\n  ".join(m[2] for m in streaming.MODEL_LIST))


@contextmanager
def _offline_runtime(sess, compaction_flag):
    """回放期间把全局运行时换成离线的，退出时逐项还原（pytest 里共用同一份 state）。"""
    saved_llm, saved_project, saved_ui = state.llm, state.current_project, state.ui_ref
    real_compact = streaming._compact_history
    saved_data_dir = paths.get_data_dir()
    data_root = tempfile.mkdtemp(prefix="lingxi-replay-data-")

    def probe(messages, budget=streaming.HISTORY_TOKEN_BUDGET,
              keep_recent=streaming.HISTORY_KEEP_RECENT):
        # 触发条件不另写一份：真跑一遍压缩判定，但结果丢掉——回放只报告"这一轮会触发压缩"。
        # 压缩缓存也要还原，否则下一轮会命中上一轮的假摘要、不再报告。
        saved_compaction = dict(state.compaction)
        try:
            _out, dropped = real_compact(messages, budget=budget, keep_recent=keep_recent)
        finally:
            state.compaction = saved_compaction
        if dropped > 0:
            compaction_flag.append(True)
        return messages, 0

    paths.set_data_dir(data_root)
    state.llm, state.current_project, state.ui_ref = OfflineLLM(), None, None
    streaming._compact_history = probe
    session.bind_thread(sess)
    try:
        yield
    finally:
        session.unbind_thread()
        streaming._compact_history = real_compact
        state.llm, state.current_project, state.ui_ref = saved_llm, saved_project, saved_ui
        paths.set_data_dir(None if saved_data_dir == paths.APP_DIR else saved_data_dir)
        shutil.rmtree(data_root, ignore_errors=True)


def _common_tokens(previous, sent, event) -> int:
    """本轮估算里有多少落在"与上一轮逐字节相同的前缀"上（= 近似缓存命中）。"""
    if previous is None:
        return 0
    if event is not None:
        # message_index 是 1 起的消息序号；0 表示工具列表变了，整段前缀都作废
        index = event["message_index"]
        return 0 if index == 0 else streaming._estimate_tokens(sent[:index - 1])
    stable = len(previous["messages"]) - (1 if previous["volatile_tail"] else 0)
    return streaming._estimate_tokens(sent[:stable])


def replay_session(messages, *, model_index=None, pricing=None) -> dict:
    """按轮重放一个会话的消息列表，返回 {"rounds": 每轮一行, "summary": 汇总}。

    每轮把历史追加到"模型即将发出第 N 条 AI 消息"的那一刻，跑真实的发送准备流程。
    回收状态从空开始（和真实运行一样），由流程自己累积。
    """
    index = default_model_index() if model_index is None else model_index
    name, mtype, model_id, _think = streaming.MODEL_LIST[index]
    sess = session.Session()
    sess.current_model_index = index
    sess.run_ledger = RunLedger({"id": model_id, "name": name, "type": mtype})
    ui = HeadlessUI()
    positions = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    rows, compaction_flag = [], []
    cursor = 0

    with _offline_runtime(sess, compaction_flag):
        for round_no, pos in enumerate(positions, start=1):
            sess.chat_history.extend(messages[cursor:pos])
            cursor = pos
            # 流程内部按 run_round_index + 1 给断裂事件编号，这里对齐成 1 起的轮次
            sess.run_round_index = round_no - 1
            previous = sess.cache_prefix
            already_evicted = len(sess.evicted_tool_results)
            compaction_flag.clear()
            sent, _record = streaming._prepare_stream_history(ui)
            evicted = len(sess.evicted_tool_results) - already_evicted

            tokens = streaming._estimate_tokens(sent)
            output = streaming._estimate_tokens([messages[pos]])
            events = sess.run_ledger.snapshot()["cache_breaks"]
            event = events[-1] if events and events[-1]["round"] == round_no else None
            hit = _common_tokens(previous, sent, event)
            sess.run_ledger.record_usage({"input": tokens, "output": output, "cache_read": hit})
            rows.append({
                "round": round_no, "tokens": tokens, "output": output,
                "hit": hit, "miss": tokens - hit,
                "break_index": None if event is None else event["message_index"],
                "break_reason": None if event is None else event["reason"],
                "evicted": evicted, "compaction": bool(compaction_flag),
            })

    snapshot = sess.run_ledger.snapshot(config.MODEL_PRICING if pricing is None else pricing)
    return {"rounds": rows, "summary": {
        "model": model_id,
        "rounds": len(rows),
        "breaks": sum(1 for row in rows if row["break_index"] is not None),
        "trips": sum(1 for row in rows if row["evicted"]),
        "compactions": sum(1 for row in rows if row["compaction"]),
        "evicted_total": sum(row["evicted"] for row in rows),
        "input_tokens": sum(row["tokens"] for row in rows),
        "output_tokens": sum(row["output"] for row in rows),
        "hit_tokens": sum(row["hit"] for row in rows),
        "miss_tokens": sum(row["miss"] for row in rows),
        "peak_tokens": max((row["tokens"] for row in rows), default=0),
        "cost": snapshot["cost"],
        "equivalent_tokens": snapshot["equivalent_tokens"],
    }}


def load_sessions(directory, ids=None) -> list:
    """只读地取出会话：给了 ID 就按 ID 取，没给就取目录下全部会话文件（按文件名=时间排序）。"""
    if ids:
        candidates = [os.path.join(directory, sid if sid.endswith(".json") else sid + ".json")
                      for sid in ids]
    else:
        candidates = sorted(glob.glob(os.path.join(directory, "*.json")))
    items = []
    for path in candidates:
        base = os.path.basename(path)
        if base in _NON_SESSION_FILES or base.endswith(".inflight.json"):
            continue
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
                raise ValueError("不是会话文件（缺 messages 列表）")
            messages = [memory._dict_to_msg(d) for d in data["messages"]]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"跳过 {base}：{error}")
            continue
        items.append({"id": data.get("id") or base[:-5], "title": data.get("title") or "",
                      "messages": messages})
    return items


def _width(text) -> int:
    """终端显示宽度：中日韩全角字符算 2 列，否则表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in str(text))


def _pad(text, width) -> str:
    return str(text) + " " * max(0, width - _width(text))


def _table(rows) -> str:
    head = ("轮次", "估算token", "命中", "未命中", "前缀首个不同", "回收", "会压缩")
    body = []
    for row in rows:
        where = "—" if row["break_index"] is None else (
            "工具列表" if row["break_index"] == 0
            else f"第{row['break_index']}条·{row['break_reason']}")
        body.append((str(row["round"]), f"{row['tokens']:,}", f"{row['hit']:,}", f"{row['miss']:,}",
                     where, str(row["evicted"]) if row["evicted"] else "—",
                     "是" if row["compaction"] else "—"))
    widths = [max(_width(head[i]), max((_width(row[i]) for row in body), default=0))
              for i in range(len(head))]
    lines = ["  ".join(_pad(cell, widths[i]) for i, cell in enumerate(head)).rstrip()]
    lines += ["  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip() for row in body]
    return "\n".join(lines)


def _cost_text(summary) -> str:
    if summary["cost"] is not None:
        return f"费用 {summary['cost']['currency']} {summary['cost']['amount']:.6f}"
    if summary["equivalent_tokens"] is not None:
        return f"未命中等价 token {summary['equivalent_tokens']:,.1f}"
    return "未配单价/折算比，只列 token"


def _summary_text(summary) -> str:
    return (
        f"小计：{summary['rounds']} 轮 · 断裂 {summary['breaks']} 次"
        f"（回收 {summary['trips']} 次 / 会压缩 {summary['compactions']} 轮）"
        f"· 累计回收 {summary['evicted_total']} 条 · 峰值 {summary['peak_tokens']:,} token\n"
        f"      输入合计 {summary['input_tokens']:,}"
        f"（命中 {summary['hit_tokens']:,} / 未命中 {summary['miss_tokens']:,}）"
        f"· 输出 {summary['output_tokens']:,}\n"
        f"      折算成本：{_cost_text(summary)}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="离线回放已保存的会话：每轮前缀是否稳定、折算成本多少（不联网、不改 chat_memory）",
        epilog="成本是近似：system 与易变内容用的是当前值，token 是粗估，命中按相同前缀估算。详见模块 docstring。")
    parser.add_argument("sessions", nargs="*", help="会话 ID（不给则回放目录下全部会话）")
    parser.add_argument("--dir", default=paths.MEMORY_DIR, help="会话目录（默认本地 chat_memory/）")
    parser.add_argument("--model", default=None, help="按 model_id 指定回放用的模型（默认第一个 API 模型）")
    args = parser.parse_args(argv)

    index = default_model_index() if args.model is None else model_index_for(args.model)
    items = load_sessions(args.dir, args.sessions)
    if not items:
        print(f"{args.dir} 里没有可回放的会话")
        return 0
    name, mtype, model_id, _think = streaming.MODEL_LIST[index]
    print(f"回放 {len(items)} 个会话 · 模型 {name}（{model_id} / {mtype}）· 来源 {args.dir}")
    print("局限：system 与易变运行态用当前值、不注入项目上下文/规则/长期记忆；token 为粗估、"
          "命中按相同前缀估算；成本是近似，用于比较改动前后的相对退化。")

    totals = []
    for item in items:
        report = replay_session(item["messages"], model_index=index)
        summary = report["summary"]
        totals.append(summary)
        print(f"\n《{item['title'] or item['id']}》 {item['id']}")
        if not report["rounds"]:
            print("  没有 AI 消息，跳过")
            continue
        print(_table(report["rounds"]))
        print(_summary_text(summary))

    print("\n=== 汇总 ===")
    grand = {key: sum(s[key] for s in totals) for key in
             ("rounds", "breaks", "trips", "compactions", "evicted_total",
              "input_tokens", "output_tokens", "hit_tokens", "miss_tokens")}
    grand["peak_tokens"] = max(s["peak_tokens"] for s in totals)
    print(f"{len(totals)} 个会话 · {grand['rounds']} 轮 · 断裂 {grand['breaks']} 次"
          f"（回收 {grand['trips']} 次 / 会压缩 {grand['compactions']} 轮）")
    print(f"输入合计 {grand['input_tokens']:,}（命中 {grand['hit_tokens']:,} / "
          f"未命中 {grand['miss_tokens']:,}）· 输出 {grand['output_tokens']:,} · "
          f"单会话峰值最大 {grand['peak_tokens']:,}")
    costs = [s["cost"] for s in totals if s["cost"] is not None]
    equivalents = [s["equivalent_tokens"] for s in totals if s["equivalent_tokens"] is not None]
    if costs and len(costs) == len(totals) and len({c["currency"] for c in costs}) == 1:
        print(f"折算成本：费用 {costs[0]['currency']} {sum(c['amount'] for c in costs):.6f}")
    elif equivalents and len(equivalents) == len(totals):
        print(f"折算成本：未命中等价 token {sum(equivalents):,.1f}")
    elif not costs and not equivalents:
        print("折算成本：未配单价/折算比，只列 token")
    else:
        print("折算成本：各会话单价/折算比配置不齐，只列 token（逐个会话见上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())