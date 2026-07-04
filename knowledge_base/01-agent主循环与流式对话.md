# Agent 主循环与流式对话

## Agent 主循环长什么样
核心在 `src/agent.py:agent_loop`。它是一个手写的 ReAct 式循环,不用 LangChain 的高层 AgentExecutor(可控性更强):

```
stream 一轮 → 累加 AIMessageChunk(content + tool_call_chunks)
  → 若本轮产生了 tool_calls:逐个执行工具,把结果作为 ToolMessage 追加进历史
  → 再 stream 下一轮
若本轮没有工具调用 → 结束
```

关键点:`state.llm_with_tools.stream(history)` 返回 `AIMessageChunk`,用 `+` 运算符可以自动累加 content 和 tool_call_chunks——LangChain 重载了 chunk 的加法,把分片的工具调用参数拼成完整 JSON。

## 为什么手写循环而不用 AgentExecutor
- 需要精细控制:流式渲染、工具执行前的确认卡、Plan 模式拦截写工具、修复循环注入、三级历史管理。这些在高层封装里很难插手。
- 面试可讲点:"我实现的是可中断、可确认、带完成闸门的 agent loop,而不是黑盒 executor。"

## 流式处理三件套(`src/streaming.py`)
- `_prepare_stream_history`:发送前整理历史(截断/淘汰/压缩,见上下文工程文档)。
- `_handle_stream_chunk`:处理单个 chunk,分离出正文、思考过程、工具调用分片。
- `_stream_with_tools`:串起来的主流程,带重试退避;`_execute_tool` 负责实际调用工具。

## 思考过程(reasoning)的统一处理
不同模型的"思考"格式不一样:`<think>...</think>`、`reasoning_content` 字段、Anthropic 的 `thinking` content block。灵犀把它们统一解析成可折叠的紫色思考块显示。

## Markdown 渲染的坑
- 流式过程中显示纯文本,完成后用 `markdown` 库一次性转 HTML 替换。
- **QTextBrowser 不支持 `<style>` 标签**,所有样式必须 inline。
- QTextBrowser 对 `<div margin>` / `<p padding>` 支持差,要给消息按钮留垂直空白得用**表格 spacer**(`<table><tr><td style="height:14px">`)——HTML 邮件时代的老套路最稳。

## Claude Code 模式(特殊分支)
当选中"Claude Code"模型时,不走 LangChain,而是 `subprocess.Popen` 调本地 `claude -p --output-format stream-json`,解析 `assistant` / `user` / `result` 事件。permission-mode 映射 Plan/Act;用 `--append-system-prompt-file` + stdin 传 prompt,避开 32K 命令行长度限制。已知限制:print 模式不支持图片输入(已在 UI 明确提示而非静默丢弃)。

## 命令确认的线程模型
worker 线程要弹确认卡时,通过 `confirm_request = Signal(str, object, object)` 投递到主线程,UI 显示内联确认卡,worker 线程 `event.wait(timeout=300)` 阻塞直到用户点完。会话级 allowlist 让"允许并记住"的同样命令秒过;危险命令(`rm -rf`/`format`/`sudo` 等正则匹配)不给"记住"选项。
