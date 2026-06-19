# 灵犀 Code

> **多模型 AI 编码助手** —— 模型对话 / 工具调用 / 代码导航 / 改完自检的验证闭环，一个 Windows 原生 PySide6 应用里完成。"Codex 体验、模型无关"。

<!-- 演示 GIF / 截图位 —— 用 ScreenToGif 录一段 20s 的"发消息 → AI 规划 → 工具调用确认卡 → 改代码 → 自动校验" 放这里，效果比文字强 10 倍 -->

## 为什么不用现有工具？

只想聊天 → [chatbox](https://github.com/Bin-Huang/chatbox) / [NextChat](https://github.com/ChatGPTNextWeb/NextChat) / [lobe-chat](https://github.com/lobehub/lobe-chat) 更成熟。

灵犀 Code 做的是**别的项目通常只覆盖其中一两块**的组合：

| 同类项目 | 灵犀 Code 的不同 |
|---|---|
| 多模型 chat（Electron / Web） | **Windows 原生 PySide6**，启动 1 秒，不带 Chromium 内核 |
| 纯聊天 App | **完整 Agent 工具调用**（文件 / 命令 / 代码导航 / 跑测试）；写盘/命令执行前**输入框上方弹内联确认卡** + 危险命令检测 |
| Cursor / Cline（绑定某家模型） | **模型无关**：MiMo / Qwen / DeepSeek-V4 / Claude / 自定义 OpenAI·Anthropic 协议任意切；含改完自检的**验证闭环** + LSP 代码导航 |
| OpenAI / Claude 海外模型 | MiMo / Qwen / DeepSeek-V4 思考块解析 / 本地 **Claude Code CLI** 子进程模式 |

如果你**只**想要其中一项，用更专业的工具更好；如果想要**所有这些**装在一起、Windows 原生、能本地跑，灵犀 Code 是个还行的选择。

> 仓库附一份角色卡模板（`roles/example.md`），照着填就能用，也可换成任意 SillyTavern 风格的 .md。

## ✨ 功能一览

### 对话核心
- 🤖 **多模型切换**：MiMo / Qwen / Claude / DeepSeek / 本地 Ollama / 本地 Claude Code CLI，**还可在设置里自填任意 OpenAI / Anthropic 兼容的自定义模型**
- 🖼️ **多模态**：支持图片输入（自动切到视觉模型 / 多模态模型）
- 🔧 **工具调用**：文件读写、命令执行、代码导航等。**run_command 执行前在输入框上方弹内联确认卡**（含命令预览 + 1/2/3 数字快捷键 + Esc 取消），危险命令（`rm -rf` / `format` / `sudo` / `drop table` 等）不给"记住"选项。**确认卡无超时**（睡一觉回来还在等你）；**拒绝时可附一句文字反馈**（如"换 async 写法"），AI 据此调整重做，留空则直接停止后续重试
- 💬 **会话历史**：自动保存、侧边栏切换、智能生成标题
- 🗂️ **多会话并发**：多个对话能**同时在后台跑**，切走不中断、切回自动重放本轮输出；每个会话的模型 / Plan-Act 模式 / 命令白名单各自独立互不干扰
- 🧠 **思考过程显示**：折叠/展开模型的 reasoning 内容
- 📊 **Token 用量统计**：实时显示每轮和会话累计用量
- ⚡ **prompt caching + system prompt 拆分**：长指令按需注入，Anthropic/MiMo 走 `cache_control` 省 token

### 编码能力
- 🪄 **`edit_file` 智能容错替换**：改大文件局部，比全量覆盖安全省 token；**写盘前弹蓝色 diff 预览卡**让你审改动。**分层匹配 L1-L4**(精确 → 行尾空白 → 缩进重对齐 → difflib 模糊),模型缩进风格不一致、tab/空格混用都能自动修正;实在匹配不上时**返回最接近片段+行号让模型自纠重试**——弱模型也能稳定改文件
- 🌐 **`search_files` 跨文件正则搜索**（ripgrep 风格，忽略噪声目录）+ `read_file` 行号分页
- 📋 **任务计划面板**：≥3 步的任务，AI 先用 `update_plan` 列出完整步骤、之后用 `set_step_status` **按序号增量推进状态**（不重发整份、计划面板不漂移），**聊天区右上角浮层实时显示进度**（待办空心圆 / 进行中 loading / 完成勾选），长任务不漏步（对标 Codex / Claude Code）
- ⚡ **并行工具调用**：同一轮里多个只读工具（读文件 / 搜索 / 导航）并行执行，多文件场景明显提速
- 🧭 **LSP 代码导航**：`find_definition` 跳转定义、`find_references` 找全部引用，优先用语言服务器（pyright/pylsp，懂作用域/import/继承）→ 自动降级 jedi → 退回正则搜，比纯文本搜索准
- ✅ **自我校验闭环**：`edit_file` / `write_file` 成功后自动跑 ruff（正确性）/ mypy（类型，抓臆造 API/参数错）/ 语法检查，把发现的问题**追加进同一条工具返回**，模型当轮就去修（不用你提醒）；长任务还有**上下文管理**（任务台账 + 按模型预算 + 大工具结果回收，长对话不丢"改过哪些文件/跑过什么测试"）
- 🧪 **更多编码工具**：`run_tests`（pytest）/ `check_code`（静态检查）/ `apply_patch`（多文件原子补丁）/ `git_diff`·`git_log`·`git_status`（只读）+ `git_stage`·`git_commit`（git 写，**执行前强制弹确认卡、无 push**）/ `fetch_url`·`web_search`（联网查资料）
- 🧭 **Plan / Act 双模式**：Plan 模式 AI 只调研给方案、不动手（只读工具白名单 + 强制提示双保护）
- ↶ **Checkpoint / 撤销**：edit/write/append 写盘前自动 git stash 快照，顶栏一键撤销 AI 上一轮改动（路径级恢复）
- 🔒 **隔离模式（Git worktree）**：顶栏一键把 AI 的改动关进独立 worktree，主项目零影响；满意点「恢复」把改动合并回主项目（AI 在隔离区里 commit 过的也算），冲突时保留 worktree 不污染主项目。需 git 项目
- 🤖 **并行子 Agent**：任务能拆成 3 个以上【相互独立、改不同文件】的子任务时，`spawn_agents` 派生多个子 Agent，各自在独立 worktree 并行写代码、自动合并回主项目（有依赖 / 改同一文件的不并行，退回顺序执行）
- 📄 **`.lingxirules` 项目级指令**：项目根放一个文件写项目约定，自动注入、优先级最高

### MCP 客户端（可选）
- 🔌 **连外部 MCP server**（filesystem / fetch / context7 文档 / memory 等），远程工具自动注入、跟内置工具一样被 AI 调用，**不改一行代码就能扩展能力**
- config.json 配 `mcp_servers`，支持 stdio / SSE / streamable_http；没装 `mcp` 包则静默跳过

### 长期记忆（跨会话）
- 🧠 **角色"天生记得"你**：AI 用 `remember` 存下你的个人信息/偏好/项目约定，新对话自动注入 system prompt，开口就记得（`forget` 删除）
- 原子写 + 损坏/瞬时错误区分，珍贵记忆抗崩溃

### 项目（工作区）
- 📁 **多项目管理**：把不同的工作目录加为项目，会话按项目分组显示
- 🔄 **启动自动恢复**：上次激活的项目下次打开继续在那
- 📍 **输入框下方实时显示当前项目路径**，点击弹切换菜单
- 🧭 **新对话沿用当前项目**：在 A 项目点+新对话 → 仍在 A；切到 B → 新对话归 B
- 🛠️ **所有文件工具自动用项目根作为相对路径基准**（`read_file`、`run_command` 等）
- 📄 **`.lingxirules` 项目级指令**：项目根放一个 `.lingxirules` 文件（纯文本 / md），里面写项目约定（"测试用 pytest"、"格式化用 black"、"提交信息用 conventional commits"），新对话时自动注入 system prompt 末尾，优先级**高于**默认指令
- 🗑️ **移除项目时把它的会话批量改为"无项目"**，不会残留游离记录

### 角色卡
- 📜 **Markdown 系统提示词**：放进 `roles/` 即可加载
- 🎭 内置一份角色卡模板（`roles/example.md`），照着结构填写即可
- 🔁 启动时自动恢复上次激活的角色

### 系统托盘
- 🪟 关闭主窗口只**隐藏到系统托盘**、后台常驻；托盘**双击**唤起对话，**右键菜单**（打开对话 / 退出）
- 🗔 唤起时**保留最大化/全屏状态**（不会缩回默认尺寸）

> 桌面宠物（GIF 立绘 / 动画）已移除——本应用专注代码助手，娱乐属性以后另开独立应用。

### 远程通知 / 手机遥控（Telegram，可选）
- 📲 **PC → 手机推送**：任务完成 / 报错 / 等待确认时推到你的 Telegram（分级 + 节流去重），完整回复分段发回不截断
- 🎮 **手机 → PC 遥控**：手机发消息让灵犀干活，三档安全分级（`chat_only` 纯对话 / `safe_readonly` 只读代码且敏感文件黑名单 / `unrestricted` 不设防），白名单 chat_id 锁死、回调按 `from.id` 校验
- ✅ **手机审批操作**：run_command / edit_file / MCP 的确认同步推手机 inline 按钮（✅允许 / ❌拒绝 / 📝记住同类），人离开电脑也能远程批；PC 卡与手机按钮**先点先到**，杜绝双重执行
- 🔒 配 `notify` / `remote_control`（含 `telegram_confirm`）开启；不配则整段静默跳过

### 系统级
- ⚙️ **设置弹窗**：所有 API 密钥、模型、自定义模型等可视化编辑
- ❌ **关闭确认**：X 按钮时弹"最小化到托盘 / 退出软件 / 取消"，可记住选择
- 🎯 **高 DPI 锐利渲染**

## 🎨 截图

![灵犀桌面助手任务计划面板](assets/screenshots/plan-panel.png)

## 📋 支持的对话模型

| 名称 | 类型 | 模型 ID | 支持图片 |
|------|------|---------|---------|
| MiMo V2.5 Pro | mimo | mimo-v2.5-pro | ❌ |
| MiMo V2.5 | mimo | mimo-v2.5 | ✅ |
| MiMo V2 Pro | mimo | mimo-v2-pro | ❌ |
| MiMo V2 Omni | mimo | mimo-v2-omni | ✅ |
| Claude Code | claude-code | claude (本地 CLI) | ❌ |
| Qwen3.5 本地 | ollama | qwen3.5:latest | ❌ |
| Qwen3.5-Plus 云端 | cloud | qwen3.5-plus | ❌ |
| Qwen-Max / Plus / Turbo | cloud | qwen-* | ❌ |
| Claude Sonnet 4 API | anthropic | claude-sonnet-4-20250514 | ✅ |
| Claude Haiku 3.5 API | anthropic | claude-3-5-haiku-20241022 | ✅ |
| DeepSeek V4 Flash / Pro | deepseek | deepseek-v4-* | ❌ |
| ⚙ 自定义模型 | custom | config.json `custom_models` 自填（OpenAI/Anthropic 协议） | 看配置 |

## 🚀 快速开始

### 1. 安装 Python 依赖

```bash
# 主程序依赖
pip install langchain langchain-ollama langchain-openai langchain-anthropic langchain-google-genai PySide6 markdown requests pillow

# MCP 客户端依赖（可选，没装则 MCP 功能静默跳过）
pip install mcp

# 代码导航依赖（可选，装语言服务器走 LSP 最准 → 没有退 jedi → 都没有退回 search_files）
pip install jedi python-lsp-server   # 或 pip install pyright（需 Node）
```

### 2. 填一个模型的 API Key

**最省事的方式**：先直接启动（见第 5 步）。没填 key 时主界面会提示「👋 还没配置模型」并给一个
**「⚙ 打开设置」按钮** → 在设置面板里填你手头某个模型的 key → 保存 → 点「立即重启」即可开始。
**不必填全**，只填你有的那一个就够用（没填的模型切过去才会提示，不影响别的）。

> 嫌点界面麻烦也可以手动：`cp config.example.json config.json`，在 `config.json` 里填 key（密钥改动重启生效）。

### 3. （可选）启动 Ollama

```bash
ollama serve
ollama pull qwen3.5:latest
```

### 4. 运行

```bash
python main.py
```

预期：弹出主聊天窗口。**首次没填 key 时**会显示「填个 API key 就能开始」引导 + 「打开设置」按钮，照着填完重启即可。关闭窗口会隐藏到系统托盘（双击托盘图标可再唤起）。

### 5.（可选）配置 MCP 工具扩展

MCP（Model Context Protocol）让灵犀连接**外部工具服务器**，把它们的工具（读写文件 / 抓网页 / 查文档 / GitHub 等）动态加进 AI 的工具箱，**不改一行代码就能扩展能力**。

> **MCP 是高级可选功能，默认关闭。** 不配它灵犀照常用（内置工具已覆盖日常）。MCP 跟 Claude Desktop / Cursor / Cline 同款协议，门槛也一样：**stdio 类型的 server 需要你的机器装 [Node.js](https://nodejs.org/)**。

**配置步骤：**

1. **装 Node.js**（用 stdio 类型 server 才需要；SSE 类型不用）：[nodejs.org](https://nodejs.org/) 下载装好，确认 `npx -v` 能用

2. **在设置里开启 MCP**：主界面 → 设置（齿轮）→ MCP 区域 → 勾选「启用 MCP」

3. **编辑 `config.json` 的 `mcp_servers`**，加你要的 server。两种类型：

   > **config.json 在哪？**
   > - 源码运行：项目根目录 `config.json`
   > - 打包 exe 版：`%APPDATA%\灵犀\config.json`（即 `C:\Users\你\AppData\Roaming\灵犀\`）
   > - **最快**：设置弹窗左下角点「config」按钮，直接打开它所在目录


   ```jsonc
   "mcp_enabled": true,
   "mcp_servers": {
     // stdio 类型：本地拉起 server 子进程，需要 Node.js
     "filesystem": {
       "transport": "stdio",
       "command": "npx",
       "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/你的目录"],
       "env": {}
     },
     // sse 类型：连一个已经在运行的 HTTP 服务，不需要 Node.js
     "context7": {
       "transport": "sse",
       "url": "http://localhost:8010/sse"
     }
   }
   ```

4. **重启灵犀**。日志（`%APPDATA%\灵犀\logs\` 或项目 `logs/`）里搜 `[MCP]`，看到这个就是连上了：
   ```
   [MCP] ✅ filesystem: 已连接，N 个工具: [...]
   [MCP] 共注册 N 个远程工具: [mcp_filesystem_xxx, ...]
   ```

5. **用**：跟 AI 说人话触发，比如「列一下 D:/你的目录 下的文件」，AI 会调 `mcp_filesystem_*` 工具（调用前弹确认卡）。

**常见问题：**

| 现象 | 原因 / 解决 |
|---|---|
| `[MCP] ❌ xxx: Connection closed` + 手动跑报 `Cannot find module 'ajv'` | npx 缓存损坏（npm 通病）。删 `%LOCALAPPDATA%\npm-cache\_npx` 后重试 |
| `[MCP] ❌ xxx: 启动失败` | 先**手动**在终端跑那条 `npx ...` 命令看真实报错（路径不存在 / 缺 token / 没装 Node） |
| stdio server 路径无效 | filesystem 的目录参数必须是**真实存在**的路径 |
| 想临时关掉 MCP | 设置里取消勾选「启用 MCP」（或 config.json 设 `"mcp_enabled": false`），重启 |

> stdio server 第一次 `npx -y` 会联网下载包，慢一点正常；之后走缓存就快了。

## 📁 文件结构

```
.
├── main.py                 # 主入口（高 DPI 配置 + 启动 Qt App + 系统托盘）
├── icon.ico                # 应用图标
├── config.json             # 配置（已 .gitignore）
├── config.example.json     # 配置模板
├── lingxi.spec             # PyInstaller 打包配置（产物 exe 名：灵犀Code）
│
├── src/                    # 主代码
│   ├── agent.py            # Agent 主循环 + 模块 facade + 启动拉起 MCP
│   ├── streaming.py        # 全流式调用（拆成 prepare/handle_chunk/stream）+ 重试退避 + 工具执行
│   ├── tools.py            # @tool 工具函数（项目根路径解析 + 写盘 diff 确认 + build_all_tools/get_tool_map）
│   ├── models.py           # 内置 + 自定义模型合并 → MODEL_LIST；LLM 工厂（带缓存）
│   ├── limits.py           # 集中的魔法数字常量
│   ├── state.py            # 全局可变状态（含 ui_ref / agent_mode）
│   ├── mcp_client.py       # MCP 客户端（常驻 asyncio loop 连外部 server）
│   ├── memory_store.py     # 长期记忆持久化（原子写 + RLock）
│   ├── memory.py           # 对话历史持久化（RLock 串行化所有读写）
│   ├── checkpoint.py       # git stash 快照 + 撤销
│   ├── projects.py         # 项目（工作区）管理
│   ├── roles.py            # 角色卡加载 + get_system_prompt（拼角色/项目/记忆）
│   ├── images.py           # 图片输入格式归一化（视觉/多模态）
│   ├── debug_log.py        # F12 调试 record 缓冲
│   ├── claude_code.py      # Claude Code CLI 调用
│   ├── config.py           # config.json 解析
│   ├── paths.py            # 路径常量 + logger
│   ├── floating.py         # 系统托盘（关窗维持后台 + 双击唤起 + 保留最大化）
│   │
│   └── ui/                 # UI 包（chat_window 用 mixin 拆分）
│       ├── __init__.py     # 导出 ChatUI / SettingsDialog
│       ├── chat_window.py  # ChatUI 主窗口（生命周期/事件/agent 集成/渲染原语）
│       ├── confirm_bars.py # 命令确认卡 + edit diff 预览卡 mixin
│       ├── markdown_render.py # Markdown 渲染 + 思考块 mixin
│       ├── search_overlay.py  # Ctrl+F 搜索浮窗 mixin
│       ├── sidebar.py      # 侧栏 + 会话列表 + 项目管理 mixin
│       ├── header.py       # 顶栏 + 按钮样式 + 角色卡 mixin
│       ├── debug_inspector.py # F12 调试弹窗
│       ├── theme.py        # THEMES 字典 + build_stylesheet + 主题持久化
│       ├── widgets.py      # SignalBridge / DragDrop / HistoryRow / CloseConfirmDialog
│       ├── settings_dialog.py  # 设置弹窗（provider 卡片 API key + 自定义模型增删改）
│       ├── helpers.py      # 图标生成 / 图片协议块 / Markdown 处理
│       ├── prefs.py        # UI 偏好持久化（关闭按钮选择等）
│       └── _base.py        # 共享 BASE_DIR / CONFIG_PATH 常量
│
├── scripts/                # pytest 测试套件 + conftest fixtures
│
├── roles/                  # 角色卡 .md
│   └── example.md          # 角色卡模板（照着填）
│
├── icons/                  # SVG 图标（Lucide 风格）
│   ├── upload_lucide.svg
│   ├── settings_lucide.svg
│   └── ...
│
├── chat_memory/            # 会话 JSON + long_term_memory.json + projects.json + role_config.json + ui_prefs.json + theme_config.json
├── logs/                   # 按日期分文件的日志
├── docs/                   # 项目文档（含 TODO.md）
└── README.md               # 本文件
```

## 🎮 使用说明

### 文本聊天

1. 输入框输入文字 → Enter 发送（Shift+Enter 换行）
2. 顶栏下拉切换模型
3. 部分模型支持"思考模式"开关

### 图片输入

- 拖拽图片到聊天窗口 / 点击输入框左下角 📎 / Ctrl+V 粘贴截图
- 应用会自动切到支持视觉的模型（如 MiMo V2 Omni / Claude）

### 系统托盘

| 操作 | 反应 |
|------|------|
| 关闭主窗口 | 隐藏到系统托盘、后台常驻（不退出） |
| 托盘双击 | 唤起主对话窗口（保留最大化/全屏状态） |
| 托盘右键 | 菜单（打开对话 / 退出） |

### 角色卡

- 切换角色：主界面 → 角色按钮 → 选择 `roles/` 下的 .md
- 自定义：把 SillyTavern 的 Character Card V2 json 的 `description / personality / mes_example` 整理成 .md 放进 `roles/`

## 🛠️ 配置项详解（`config.json`）

```json
{
  "ollama_base_url":          "http://127.0.0.1:11434",
  "qwen_api_key":             "sk-...",
  "anthropic_api_key":        "sk-ant-...",
  "mimo_api_key":             "tp-...",
  "deepseek_api_key":         "sk-...",
  "google_api_key":           "AIza...",

  "custom_models": [
    {
      "name": "我的私有模型", "model_id": "xxx",
      "api_key": "sk-...", "base_url": "https://.../v1",
      "protocol": "openai", "supports_vision": false, "supports_thinking": false
    }
  ],
  "mcp_enabled":              true,
  "mcp_servers": {
    "filesystem": { "transport": "stdio", "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/你的真实目录"] },
    "context7":   { "transport": "sse", "url": "http://localhost:8010/sse" }
  },

  "web_search_api_key":       "tvly-...",
  "auto_check_after_edit":    true
}
```

## 🧰 内置工具

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件内容（offset/limit 行号分页） |
| `write_file` | 创建/覆盖文件（**写盘前弹 diff 确认卡**） |
| `append_file` | 追加内容到文件末尾（**弹 diff 确认卡**） |
| `edit_file` | **智能容错替换**：分层匹配 L1-L4(精确→行尾空白→缩进重对齐→模糊)，配 47 项回归测试；失败返回最接近片段让模型自纠（**弹 diff 预览卡** + 路径白名单，比 write_file 安全） |
| `list_directory` | 列出目录内容 |
| `run_command` | 执行系统命令（默认 **5 分钟**超时、可传 `timeout` 覆盖，**执行前弹确认卡**，流式输出；**`background=True` 转后台**跑 dev server / watch / 长服务，立即返回 bg_id） |
| `read_background_output` / `list_background_commands` / `stop_background_command` | 管理后台命令：读输出 / 列出 / 停止（read·list 只读，Plan 模式放行；退出时自动清理防端口残留） |
| `search_in_file` | 单文件关键词搜索（offset/limit 分页） |
| `search_files` | 跨文件正则搜索（ripgrep 风格） |
| `find_definition` / `find_references` | 代码导航：跳转符号定义 / 找全部引用（LSP 优先 → jedi 降级 → 退回 search_files） |
| `code_map` | 代码库符号地图（提取函数 / 类） |
| `apply_patch` | 多文件原子补丁（Codex 风格 `*** Begin Patch`）：一次建/改/删多文件，全校验通过才落盘 |
| `run_tests` | 跑 pytest（精炼失败定位 + 总耗时） |
| `check_code` | 静态检查单文件（Python 用 ruff，其它走 config 的 `check_command`） |
| `git_diff` / `git_log` / `git_status` | 只读看 git 改动 / 历史 / 状态 |
| `git_stage` / `git_unstage` / `git_commit` | git 写操作（暂存 / 取消暂存 / 本地提交，**无 push**）；**执行前强制弹确认卡**，路径白名单防注入 |
| `update_plan` | 维护任务计划清单（右上角浮层实时显示进度） |
| `fetch_url` / `web_search` | 抓网页正文 / Tavily 联网搜索 |
| `remember` / `forget` | 长期记忆存取（本地安全操作，不弹确认） |
| `notify_user` | 推送通知到 Telegram（分级 / 节流，可选） |

> 另外接外部 MCP server 后，远程工具以 `mcp_{server}_{tool}` 形式自动加入（调用前弹确认卡）。

## 📦 打包

```bash
pyinstaller lingxi.spec
```

产物在 `dist/灵犀Code/` 目录。

## ⚠️ 已知约束

- **Python 3.14** 环境：旧版 LangChain API（如 `ConversationBufferMemory`）不可用，主程序已绕开
- `config.json` 含 API 密钥，**已加入 `.gitignore`**，不要提交
- Windows 高 DPI（125%/150%）下文字渲染由 `QT_ENABLE_HIGHDPI_SCALING` + `PassThrough` 策略处理
- QTextBrowser 不支持 `<style>` 标签，Markdown HTML 必须使用内联样式
- MiMo 模型通过 Anthropic 兼容接口调用

## 📝 License

仅供学习和个人使用。

## 🙏 致谢

- [LangChain](https://github.com/langchain-ai/langchain)
- [PySide6 / Qt for Python](https://wiki.qt.io/Qt_for_Python)

## ✍️ 作者寄语

我本身是 Java 开发，对 Python 其实并不算熟。这个项目 99% 的代码，都是在 Claude Code 和 GPT 的帮助下写出来的。

这个项目最初是根据我个人的开发习惯和使用需求做出来的。它不一定适合所有人，但对我来说，它是一次把想法变成真实工具的尝试。

很庆幸自己生活在这个时代，可以借助 AI 把脑子里的想法一点点做成真正能用的作品。

接下来，我会继续完善这个项目，让它在一次次迭代中变得更稳定、更好用，也更接近我心中理想的 AI 助手。
