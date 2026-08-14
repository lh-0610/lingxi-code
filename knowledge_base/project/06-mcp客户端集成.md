# MCP 客户端集成

## 是什么
MCP(Model Context Protocol)让灵犀连接**外部工具 server**(filesystem / fetch / context7 / memory 等),把远程工具动态注入到工具集,跟内置工具一样被 AI 调用。代码在 `src/mcp_client.py`。**没装 `mcp` 包 / 没配 `mcp_servers` 时整段静默跳过**(零回归)。

## 配置
`config.json` 的 `mcp_servers`(dict,key=server 名),transport 支持:
- `stdio`(command + args,如 `npx -y @modelcontextprotocol/server-memory`)
- `sse`(url)
- `streamable_http`

## 核心难点:同步 agent × 异步 SDK
mcp SDK 是 asyncio 异步的,而灵犀的 agent 主循环是同步的。桥接方案:
1. 起**一个常驻后台线程跑 asyncio event loop**。
2. 每个 server 一个常驻协程:`async with stdio_client/sse_client(...) as session: ... await _shutdown_event.wait()` ——用一个永不结束的等待把连接挂住保活。
3. **绝不能把 session `return` 出去**:一旦退出 `async with` 上下文,连接就断了。所以协程必须在上下文内挂住。
4. 工具调用从 agent 线程走 `run_coroutine_threadsafe(session.call_tool(...), loop).result()` 投进 loop 执行、同步拿结果。

## 致命坑(已避开)
**不要在 loop 自己的线程上,对同一个 loop 用 `run_coroutine_threadsafe().result()`——会自死锁**(在 loop 线程里同步等 loop 里的任务完成,而任务永远等不到这个线程让出)。所以 `_build_mcp_tools` 是纯同步的:它读取 `_server_loop` 提前缓存好的 `_server_tools`,不在 loop 线程里反向阻塞。

## 工具注入与命名
- 远程工具名加 `mcp_{server}_{tool}` 前缀,防撞内置工具。
- `_execute_tool` 里 `name.startswith("mcp_")` 的工具走**执行前确认**(MCP 工具能干任意事);Plan 模式当写工具拦。
- 启动时 `agent.py` 后台线程调 `init_mcp()`,工具就绪后清 `_BOUND_LLM_CACHE`,让下次 stream 重新 `bind_tools` 把新工具带上。关窗时 `main.py` 调 `shutdown()` 清理。

## 打包注意
`lingxi.spec` 用 `collect_submodules('mcp')` + `collect_data_files('jsonschema_specifications')`——因为 mcp 是懒导入 + 带数据文件,PyInstaller 的静态分析抓不到,不显式收集会打包后缺文件。

## 可讲的设计价值
这是一个典型的"**同步世界接入异步生态**"的工程题:用常驻 loop 线程 + 保活协程 + 线程安全投递,把异步 SDK 封装成同步可用的能力,并且优雅降级(没装依赖就跳过)。面试聊"如何集成第三方异步 SDK / 避免事件循环死锁"时是好素材。
