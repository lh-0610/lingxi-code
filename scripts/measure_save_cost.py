"""量一下会话保存的实际开销（典型会话 / 长会话 / 工具边界 / sidecar）。

这不是 pytest 用例——它没有通过标准，只是把数字打出来。B03 的取舍是"独立 sidecar 去掉
执行前的全量重写，但操作后仍有一次全量主快照"，所以**总写盘量并没有变成与历史长度无关**。
要不要上增量日志引擎，得先有这组数字，不能凭感觉。

用法（数据根放临时目录，绝不碰真实 chat_memory/）：

    .venv\\Scripts\\python.exe scripts/measure_save_cost.py
"""
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _build(turns, chars_per_turn):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from src import session

    sess = session.Session()
    history = [SystemMessage(content="sys")]
    for i in range(turns):
        history.append(HumanMessage(content=f"第 {i} 个问题"))
        history.append(AIMessage(content="回" * chars_per_turn))
    sess.chat_history = history
    session.register(sess)
    return sess


def _time(fn, rounds=7):
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples), max(samples)


def main():
    root = tempfile.mkdtemp(prefix="lingxi-savecost-")
    from src import paths
    paths.set_data_dir(root)
    from src import memory, run_records

    print(f"数据根: {root}\n")
    print(f"{'场景':<26}{'消息数':>7}{'正文字节':>12}{'中位耗时ms':>12}{'最大ms':>9}")
    print("-" * 68)

    for label, turns, chars in (("典型会话", 20, 200),
                                ("中等会话", 120, 400),
                                ("长会话", 400, 800)):
        sess = _build(turns, chars)
        memory.save_session(session=sess)
        sid = sess.current_session_id
        median, worst = _time(lambda: memory.save_session(session=sess))
        size = os.path.getsize(os.path.join(paths.memory_dir(), f"{sid}.json"))
        print(f"{label:<26}{len(sess.chat_history):>7}{size:>12,}{median:>12.2f}{worst:>9.2f}")

        # 工具边界的一整套：执行前 sidecar + 执行后主快照 + 清理
        run_records.begin_run(sess)

        def _boundary():
            op = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c1",
                                             args={"path": "src/a.py"})
            run_records.commit_operation(sess, op)

        median, worst = _time(_boundary)
        print(f"{'  └ 工具边界（写+提交+清理）':<24}{'':>7}{'':>12}{median:>12.2f}{worst:>9.2f}")

        # 只量 sidecar 那一笔，看它是否随历史增长
        op = run_records.begin_operation(sess, tool="edit_file", tool_call_id="c2",
                                         args={"path": "src/a.py"})
        side = os.path.getsize(run_records.inflight_path(sid))
        print(f"{'  └ sidecar 字节':<25}{'':>7}{side:>12,}")
        run_records.commit_operation(sess, op)
        print()

    print("读法：sidecar 不随历史增长；主快照随历史线性增长，这一项本版没有消除。")
    paths.set_data_dir(None)


if __name__ == "__main__":
    main()
