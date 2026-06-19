"""灵犀 Code - 核心模块包（专注代码助手；语音/图片生成/视频生成已移除）

包结构：
- paths      路径常量 + 日志初始化
- config     config.json 加载
- state      运行期可变全局状态（当前模型、对话历史、停止标志等）
- models     MODEL_LIST + _create_llm + 视觉能力判断
- roles      角色卡加载与系统提示词合并
- memory     对话历史 JSON 序列化与会话管理
- projects   项目（工作区）注册 + 当前项目持久化
- images     图片 content block 跨协议格式归一化（图片输入/视觉）
- claude_code Claude Code CLI 子进程模式
- streaming  全流式调用（_stream_with_tools）+ 工具执行
- agent      主循环 agent_loop + switch_model + 启动初始化
- tools      LangChain 工具集合（read_file / edit_file / run_command 等）
- floating   系统托盘（关窗后维持后台 + 双击唤起）
- ui         PySide6 桌面界面（包：chat_window / theme / widgets / settings_dialog / helpers / prefs）
"""

__version__ = "1.0.0"

