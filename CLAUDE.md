# 灵犀 Code (lingxi-code)

基于 LangChain + PySide6 的 **Windows 原生 AI 编码助手**（桌面应用）。
Agent 工具调用 · 写盘确认闸门 · 改完自检的验证闭环 · RAG 知识库 · MCP · worktree 隔离的并行子 Agent；任意模型可切。

> 专注代码助手；桌面宠物等娱乐属性已移除，以后另开独立应用。
> 角色卡放 `roles/*.md` 加载（仓库附 `example.md` 模板）。

## 文档地图

本文件只放每次都要知道的：怎么跑、开发约定、已知取舍。架构细节按主题拆在 `docs/architecture/`，**这些文件不会自动加载——改哪块先读哪篇**。

| 文档 | 内容 | 改这些之前先读 |
|---|---|---|
| [overview.md](docs/architecture/overview.md) | 目录与模块一览；支持的模型、自定义模型 | 找文件在哪、加模型 |
| [core.md](docs/architecture/core.md) | 对话核心、项目（工作区）、`agent` facade、多会话并发、Prompt 缓存分层、主循环边界与错误恢复 | `agent.py` `streaming.py` `session.py` `state.py` `roles.py` `llm_errors.py` `projects.py` |
| [runs-and-recovery.md](docs/architecture/runs-and-recovery.md) | 运行记录与中断恢复（B03）、工具边界 inflight sidecar、运行结果卡（B06）、恢复检查与继续任务（B04） | `run_records.py` `result_view.py` `ui/result_card.py` `ui/run_results.py` `recovery.py` `workspace_anchor.py` |
| [tasks-and-input-queue.md](docs/architecture/tasks-and-input-queue.md) | 任务身份与要求/计划分离（B05）、统一退出屏障（B09a）、运行中输入队列（B09b） | `task_state.py` `ui/task_panel.py` `input_queue.py` `ui/queue_panel.py` `ui/send.py` `ui/run_admission.py` `ui/queue_dispatch.py` |
| [background.md](docs/architecture/background.md) | 后台进程管理（B10a）、后台任务面板（B10b） | `background.py` `ui/background_panel.py` |
| [undo-and-verification.md](docs/architecture/undo-and-verification.md) | 文件改动的安全撤销（B11a）、验证闭环与自动修复 | `file_history.py` `file_transaction.py` `verification.py`，以及写盘工具 |
| [tools.md](docs/architecture/tools.md) | 工具列表、计划稳定性、改后自我校验 | `tools*.py`：新增或修改工具 |
| [subsystems.md](docs/architecture/subsystems.md) | 子 Agent 与 worktree、代码导航、持久化、会话历史、角色卡、托盘、MCP、RAG、长期记忆、持久化文件清单 | `subagent.py` `worktree.py` `lsp_client.py` `codeintel.py` `memory*.py` `mcp_client.py` `rag/` `floating.py` |

`docs/architecture/` 跟代码一起改、一起提交；`docs/` 下其余资料（规格、方案、评审记录）只在本地，不进仓库。

## 运行

```bash
# 一次装齐、版本与 CI 一致（requirements.lock 由 scripts/lock_deps.py 生成，改依赖后重新生成）
pip install -r requirements-dev.txt -c requirements.lock

# 主依赖（完整清单见 requirements.txt）
pip install langchain langchain-ollama langchain-openai langchain-anthropic langchain-google-genai PySide6 markdown requests pillow numpy chromadb

# 知识库 PDF 摄取（可选；没装则只收 .md，目录里有 .pdf 会在重建索引时明确报错）
pip install pypdf

# MCP 客户端（可选；没装则 MCP 功能静默跳过）
pip install mcp

# 代码导航（可选；装语言服务器走 LSP 最准 → 没有退 jedi → 都没有退回 search_files）
pip install jedi python-lsp-server   # 或 pip install pyright（需 Node）

# 代码地图增强（可选；tree-sitter 多语言符号提取，没装则回退内置正则）
pip install tree-sitter tree-sitter-python tree-sitter-javascript tree-sitter-typescript

# 配置
cp config.example.json config.json   # 编辑填入密钥

# Ollama（可选）
ollama serve && ollama pull qwen3.5:latest

python main.py
```

## 开发注意事项

### 通用
- **Python 3.14 环境**，`ConversationBufferMemory` 等旧版 LangChain API 不可用
- **路径**：`src/paths.py` 的 `_app_data_dir()` 在 dev 期返回 src 上一级目录（项目根），打包后返回 exe 目录
- **写入 state**：必须 `state.X = ...`（不是 `agent.X = ...`）
- **新增工具**：在 `src/tools.py` 用 `@tool` 装饰器定义，加进 `ALL_TOOLS` 和 `TOOL_MAP`；如果是文件类工具记得用 `_resolve_path()` 解析相对路径
- **新增模型**：编辑 `src/models.py` 的 `MODEL_LIST` 元组列表，并在 `_create_llm()` 加 dispatch 分支
- **新增 UI 子模块**：`src/ui/` 下放，注意从 `..` 引父包（`from .. import agent`），从 `.` 引同级（`from .theme import ...`）；`__init__.py` 只对外暴露 `ChatUI` / `SettingsDialog`

### Qt 相关
- 高 DPI：`QT_ENABLE_HIGHDPI_SCALING=1` + `setHighDpiScaleFactorRoundingPolicy(PassThrough)`
- 任务栏图标：`SetCurrentProcessExplicitAppUserModelID("lingxi.ai.desktop")`
- QTextBrowser **不支持 `<style>`**，Markdown HTML 必须 inline 样式
- QTextBrowser 对 `<div margin>` / `<p padding>` 支持差；要给消息按钮留垂直空白用**表格 spacer**（`<table><tr><td style="height:14px">`），HTML 邮件时代的老套路最稳
- Enter 发送、Shift+Enter 换行 通过 `eventFilter` 在 `self.entry` 上拦截
- **跨线程 QObject 调用必须用 Signal**：worker 直接动 `QTimer.start/stop` / `widget.update()` 会让 timer 失去 thread affinity 永久失活。范式参考 `SignalBridge.confirm_request`
- **`QTimer.singleShot` 针对窗口方法的调用必须传 receiver 上下文**：`QTimer.singleShot(ms, self, method)`——窗口销毁时 Qt 自动取消回调；不传 context 的话（`singleShot(ms, method)`），窗口销毁后未到期的回调仍会触发并打向已删除的控件树（复实测：加载会话安排最长 700ms 的滚动回调，销毁窗口后抛 `MessageView already deleted`，进程退出阶段表现为 access violation）。测试侧对应约定见 `scripts/test_sidebar_load_more.py` 的 `_destroy_ui_cleanly`：先在窗口存活时放完挂着的零秒回调，再 deleteLater + DeferredDelete 真正销毁
- `QPixmap.setDevicePixelRatio(dpr)` 后拿物理像素尺寸要用 `deviceIndependentSize()` 否则在高 DPI 上偏移
- 加载 `.ico` 当 widget icon 时**别直接 `QPixmap(path).scaled()`**——会从 .ico 多分辨率位图里随便挑一张可能拿到 16×16 那张。要用 `QIcon(path).pixmap(QSize(256,256)).scaled(...)`，QIcon 会挑最接近目标尺寸的内嵌位图

### 配置
- `config.json` 含 API 密钥，**已 `.gitignore`，禁止提交**
- MiMo 模型走 Anthropic 兼容接口（`ChatAnthropic` + 自定义 `base_url`）
- DeepSeek V4 默认开 thinking，要显式 `extra_body={"thinking": {"type": "disabled"}}` 才能关
- PyInstaller 打包配置见 `lingxi.spec`（产物 exe 名为中文「灵犀Code」）

## 已知限制与有意取舍（避免 code review 反复重提）

> 这类「单一真相」记录放在入口文件里，保证每次都会被读到（AGENTS.md 指向本文件）。

### 已接受的残留（评估后暂不修）
- **`fetch_url` 对 DNS 重绑定的 GET 副作用是 blind SSRF**（`tools_web.py`）：peer IP 校验在 `requests.get()` 返回后才做，GET 请求已发出，只能挡读响应、挡不住内网 GET 的副作用。已缓解：`_ssrf_reject` 发请求前拦掉所有**静态**内网目标 + 默认直连（`fetch_url_allow_proxy=false`），残留仅「公网域名重绑定到内网 + 该端点对 GET 有副作用」，对单机本地工具很边缘。真要堵需发请求前**钉住已验证 IP**（HTTPS 要自定义 TLS adapter 保 SNI/证书，安全关键、需真实端点测试），故单独立项再做。
- **Claude Code（`claude -p`）不支持图片输入**（`claude_code.py`）：print 模式无受支持的图片传入方式，只能传文本；已在 UI 明确提示而非静默丢弃。

### 有意跳过（ROI 低 / 非问题）
- **C1** claude CLI 版本探测回退：现行 CLI 早稳定支持 `--permission-mode`，每次探测多起子进程不划算。
- **B3** `projects._load` 去锁：文件极小、读极快，且写前防清空正需要锁。
- **C2** `_project_cwd` 改公开别名：纯改名无价值。

### 待办 backlog（真问题，按优先级排期）
来自 `docs/glm_code_review_2026-06-19.md`（该文件本地可见、未入库）的剩余项：默认模型 fallback 落到 Claude Code（`agent.py`）；`read_file` 大文件全量 `readlines()`（`tools.py`）；token 估算 `×0.7` 对英文高估（`streaming.py`）。流式错误分类和补丁写入失败回滚已实现，边界分别见 `docs/architecture/core.md`（主循环边界与错误恢复）和 `docs/architecture/tools.md`（apply_patch）。
