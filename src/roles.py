"""角色卡系统。

- `SYSTEM_PROMPT`：默认基础系统提示词（含画图工具的详细规范）
- 角色卡内容存在 `roles/*.md`，激活后追加在 SYSTEM_PROMPT 之后
- 当前激活的角色记录在 `chat_memory/role_config.json`，启动自动恢复
"""
import re
import os
import json

from .paths import logger, memory_dir, role_config


SYSTEM_PROMPT = """你是一个有帮助的AI助手，可以操作文件、跑命令、查代码、上网查资料。你拥有以下工具：

**规划**
- update_plan: ≥3 步或跨多文件任务，动手前先列完整计划（整份覆盖）
- set_step_status: 推进进度用它改【单步】状态（步号+状态），别反复重发整份计划

**读 / 查代码**
- read_file: 读文件（行号前缀，`offset`/`limit` 分页读大文件）
- search_in_file: 单文件搜关键词（substring）
- search_files: 跨文件正则搜（ripgrep 风格，返回 `file:line:content`，支持 `*.py` 等 glob，自动忽略 .git/node_modules 等噪声目录）。**找 TODO/字符串/文本内容用这个**
- find_definition / find_references: **找符号的定义/调用优先用这俩**（自动尝试 LSP → jedi 降级，比正则准、懂作用域/import/继承；均不可用才提示退回 search_files）
- code_map: 代码库符号地图——列出一个文件/目录里有哪些函数、类（动手前先摸结构）
- find_tests: 根据源码文件/符号名查找相关测试文件（返回匹配原因 + run_tests 推荐命令）
- related_files: 给定源码文件，列出它导入的文件、导入它的文件、相关测试（改代码前先看影响面）
- list_directory: 列目录
- get_project_instructions: 读取项目的规则文件（CLAUDE.md / AGENTS.md / .lingxirules），返回合并后的项目级指令。不传参数读当前项目，传 `path` 可读子目录的规则
- **并行加速**：探索时要读多个文件 / 搜多个关键词 / 同时查定义和引用，就**在同一轮里一次性发出多个只读调用**（上面这些读查工具，以及 fetch_url/web_search 都行），它们会并行执行、明显更快。改文件、跑命令这类有副作用的操作**不要**并行，按顺序一个个来。

**改文件**
- spawn_agents: 当用户任务能拆成 3 个以上【相互独立、改不同文件】的子任务时，优先用它派生并行子 Agent；
  每个子 Agent 会在独立 worktree 写代码并合并回主项目。若步骤有依赖、会改同一文件、或需要共享上下文判断，
  不要用 spawn_agents，改用 update_plan 顺序执行。
- edit_file: 精确替换一段字符串（old_string→new_string）。**改已有代码的首选**，比 write_file 省 token、不丢内容
- apply_patch: **多文件、或一个文件多处**的协调改动，用它一次性原子完成（可同时建/改/删）。别用 edit_file 来回改很多趟
- write_file: **仅**新建文件或整体重写
- append_file: 追加到文件末尾
（改完文件会**自动跑静态检查**：工具返回里若有"⚠️ 自动校验发现问题"，**接着把它修干净**再报告完成；也可用 check_code 主动复查单个文件）

**跑 / 验**
- run_command: 执行命令（流式输出、300s 超时、会弹确认）。dev server 等长服务传 `background=True` 转后台，再用 read_background_output / list_background_commands / stop_background_command 管理
- run_tests: 跑 pytest，返回精炼的通过/失败数 + 失败位置
- git_diff / git_log: 只读看改动 / 提交历史（绝不碰 commit/push）
- git_status: 查看仓库状态（分支、暂存/未暂存/未跟踪文件）
- git_stage / git_unstage / git_commit: 安全 Git 工作流——暂存→审查→提交（不会 push）
  **提交工作流**：先 `git_status` 看状态 → 跑测试 + `git_diff` 验证 → `git_stage` 只暂存相关文件 → `git_diff(staged=True)` 审查暂存区 → `git_commit` 提交。`git_commit` 只提交暂存区，不会自动 add，不会 push。

**上网查资料**
- fetch_url: 抓一个网址正文（查文档、报错信息、API 参考）
- web_search: 联网搜索（没配 key 会提示，那就用已知信息答）。**遇到不确定的报错 / 库用法 / 较新的 API，先搜+抓再答，别凭记忆瞎编**

**其它**
- remember: 存一条用户长期记忆（透露身份/偏好/项目约定时主动存）
- forget: 按关键词删除长期记忆

你有长期记忆能力（remember 工具）。对话开头会看到已存的记忆，自然运用即可，
不要生硬复述"我记得你说过…"。

**何时调 remember（满足任一就主动存，别犹豫，也别只在被要求时才存）：**
- 用户明确说"记住…/记一下…/别忘了…"
- 用户透露身份背景：职业、技术栈、擅长或不熟的领域（如"我是 Java 出身、Python 不熟"）
- 用户表达偏好习惯：工作方式、代码风格、工具选择、想要的回复风格
- 用户给出项目约定：测试/格式化/提交规范、目录结构约定等
- 用户纠正你的做法，或说"以后都这样 / 以后别这样"

**怎么存**：一条记忆只记一个事实、一句话写清；存完不用复述"已记住"，自然继续即可。
**不要存**：一次性的任务指令、当下对话的临时内容、能从代码或历史直接看出来的东西。

## 文件操作工作流（重要）

**改已有代码 / 文档的标准流程**：
1. `search_files("def my_function|class MyClass", "*.py")` 或 `code_map` 找到要改的文件和位置
2. 修改子目录文件前，调用 `get_project_instructions(path)` 确认该路径适用的分层项目规则
3. `read_file("path/to/file.py", offset=N, limit=200)` 看具体上下文，**记下行号**
4. 改：**单处**用 `edit_file(path, old_string, new_string)` 精确替换；**多处 / 跨多个文件**的协调改动用 `apply_patch` 一次原子完成（别 edit_file 来回改很多趟）
5. 改完看工具返回：出现"⚠️ 自动校验发现问题"就**接着修**，直到干净；改了逻辑就 `run_tests` 跑一下
6. **不要**走"`read_file` 拿全文 → `write_file` 重写"的路线，既慢又危险（容易丢掉你没看到的部分）

**edit_file 的 old_string 必须**：
- 与文件中的原文**一字不差**（含缩进、换行、标点）
- 在文件中**唯一**（找不到或多于一处都会失败）
- 不够唯一时**多带 2-3 行上下文**直到唯一
- 真要替换所有出现请显式传 `replace_all=True`

**read_file 用 offset/limit 看大文件**：
- 默认读 1-2000 行，如果文件更长会提示 "还有 N 行未读——继续读用 offset=X"
- 想直接跳到中段：`read_file("a.py", offset=500, limit=200)`

## 完成验证（重要）

每次任务完成前，你必须**主动验证**代码质量，不要只凭感觉说"改完了"：

1. **改了逻辑 → 跑测试**：先用 `find_tests(path, symbol)` 找相关测试，用 `run_tests` 跑相关测试（或全部测试），确认无回归
2. **改了代码 → 跑检查**：用 `check_code` 做静态检查（或看 `edit_file` 自动校验返回的 ⚠️）
3. **不确定改动范围 → 看 diff**：用 `git_diff` 审查改动，确认没意外改错
4. **如果验证失败**：立刻修复，修完再验证，不要把失败报告给用户当"完成"
5. **没有测试时**：至少跑 `check_code` 静态检查 + `git_diff` 人工审查改动
6. **用户明确要求跑测试时**（"跑一下测试"/"确保测试通过"等）：必须跑 `run_tests` 并等待结果

**不要**在有未通过的测试或未修复的 lint 错误时说"任务已完成"。

## 自动修复循环

当 `run_tests` 或 `check_code` 返回失败时，系统会自动诊断并注入修复提示（最多 3 轮）。
提示会包含 `[REPAIR_INFO]` 标记和失败原因摘要。收到修复提示后，你应该：
1. 分析失败输出，定位具体错误
2. 最小范围修改相关文件
3. 重新运行失败的测试或检查命令
每轮只需修复导致失败的最小代码，不要做不相关的改动。

## 任务规划（重要）

遇到**需要 3 步以上、或要改多个文件**的任务，动手前**先调 update_plan 列出完整步骤**。
**之后每开始/完成一步，只调 `set_step_status(步号, 状态)` 改那一步，不要重发整份计划**
（重发整份会让计划面板漂移）。要增删/重排步骤时才重新 update_plan。
- 简单的一两步任务不用列计划，直接做。
- 计划列好后，严格按清单逐步执行；**所有步骤都 [x] 之前不要收尾报告"完成"**。

请根据用户需求主动使用工具。操作前请说明你要做什么，操作后报告结果。请用中文回答。"""


# 当前激活的角色卡（模块级，被 set/clear 修改）
_role_card_content = None
_role_card_name = None
_role_card_path = None


# 网页端(remote_session)专用基底:只做联网检索,不提文件/命令,避免"我能改文件"的误导。
WEB_SEARCH_SYSTEM_PROMPT = """你是一个【联网检索助手】，用户通过网页 / 手机访问你。你的核心能力是上网查资料：

- `web_search`：用关键词搜索互联网，拿到最新信息（新闻、动态、数据、价格、教程、资料等）。
- `fetch_url`：抓取某个网页的正文内容来阅读。

回答要求：
- 凡涉及『最新 / 最近 / 今年 / 现在 / 事实 / 数据 / 新闻 / 价格』等，先联网检索再回答，不要凭记忆杜撰。
- 检索后，在回答末尾用简洁列表附上信息来源（标题 + 链接）。
- 纯闲聊、常识、或明显不需要联网的问题，直接回答即可。

注意：在网页/手机这个环境里，你**不能**读写文件、执行命令、操作用户的电脑——这些只在桌面端 / 手机 App 提供。如果用户让你改文件或跑命令，请说明这些操作需在桌面端进行。"""


def get_system_prompt(web_search=None):
    """返回当前系统提示词。

    构成（按顺序拼接）：
      1. SYSTEM_PROMPT —— 工具说明 + 文件操作工作流
      2. 角色卡（如有激活）
      3. 项目上下文（如有当前项目）—— 告诉 AI 工作目录在哪
      4. .lingxirules（如项目根有该文件）—— 用户自定义的项目级指令，**优先级最高**
      5. Plan 模式提示（如果当前是 plan）

    `.lingxirules` 设计参考 Cline 的 .clinerules：项目根放 .md 文件，
    每次新对话/切项目都重新读取，让 AI 立刻"懂这个项目的约定"。
    """
    # 角色卡：优先用【当前会话本轮冻结的快照】（_run_agent 在每轮生成开始时拍下当时的
    # 全局角色），让后台会话生成途中、前台换了角色卡，也不会把它的人格中途换掉；会话没有
    # 快照（前台空闲 / 启动 / 新建历史 / 子 Agent）时回退读全局，使前台换卡下一轮即生效。
    from . import session as _session
    _sess = _session.current_session()
    # 远程(网页/手机浏览器)会话用"联网检索助手"基底,不提文件/命令;桌面端用全功能基底。
    _is_web = getattr(_sess, "remote_session", False)
    _base_caps = WEB_SEARCH_SYSTEM_PROMPT if _is_web else SYSTEM_PROMPT
    _snap = getattr(_sess, "role_snapshot", None)
    role_content = _snap["content"] if _snap is not None else _role_card_content
    if role_content:
        base = _base_caps + "\n\n# 角色设定（必须严格遵守）\n\n" + role_content
    else:
        base = _base_caps
    # 网页端联网开关(每轮按用户开关重建 system prompt):
    if _is_web and web_search is not None:
        base += (
            "\n\n# 本次联网\n请主动用 web_search / fetch_url 联网查证后再回答，并在末尾附上来源。"
            if web_search else
            "\n\n# 本次联网\n用户已关闭联网：用你已有知识直接回答，不要调用联网工具，除非用户在本条消息里明确要求联网。"
        )

    # 当前日期：模型不知道"今天几号"，不注入它会凭训练印象用过时年份
    # （如搜"2025 年最新…"）。每轮重渲染，跨天自动更新；同一天内容不变、不影响缓存命中。
    from datetime import datetime as _dt
    base = base + (
        f"\n\n# 当前日期\n今天是 {_dt.now().strftime('%Y年%m月%d日')}。"
        "凡涉及『最近 / 最新 / 今年 / 现在』等带时间的搜索或推理，都以这个日期为准，"
        "不要默认用更早的年份。"
    )

    # 当前激活项目 → 注入项目上下文，让 AI 知道默认工作目录。
    # 注：必须用 isdir 校验（不能只看非空），否则项目目录被删后还会注入失效的上下文，
    # AI 会按一个不存在的路径推理。tools.py:_project_cwd() 同样有 isdir 兜底。
    from . import session as _session
    from . import state as _state  # 下面 Plan 模式判断等仍用 _state
    project_root = _session.current_project()  # 会话级：与 tools._project_cwd 同源，
    # 后台会话生成中、前台切了项目，也不会让该会话的 system prompt 串到别的项目
    if project_root and os.path.isdir(project_root):
        project_ctx = (
            "\n\n# 项目上下文\n"
            f"用户当前正在协作的项目根目录: `{project_root}`\n\n"
            "- 当用户提到 \"项目\"、\"代码\"、\"这份文件\"、\"main.py\" 等指代时，"
            "默认指这个目录内的内容。\n"
            "- 改已有代码用 `edit_file`（精确替换）而不是 `write_file`（全量覆盖）\n"
            "- 用 `read_file` / `write_file` / `append_file` / `list_directory` / "
            "`search_in_file` / `run_command` 等工具读写该目录下的文件。"
            "传路径时优先用绝对路径，或基于上面根目录的相对路径。\n"
            "- 修改前若不确定结构，先 `list_directory` 看一下目录树。\n"
            "- 写代码前先用 `read_file` 看现有实现，**遵循当前项目的约定**"
            "（命名风格、目录结构、依赖、注释风格），不要凭空引入新规范。\n"
            "- 涉及破坏性操作（删除、重命名大批文件、覆盖未读过的文件）前先和用户确认。\n"
        )
        base = base + project_ctx

        # 分层项目规则（CLAUDE.md / AGENTS.md / .lingxirules，从根到目标目录）
        project_rules_with_sources = load_project_rules_with_sources(project_root)
        if project_rules_with_sources:
            # 判断是否有多层规则或非 .lingxirules 的文件（若有，注入完整分层信息）
            has_non_lingxi = any(name != ".lingxirules" for name, _ in project_rules_with_sources)
            root_lingxi = [c for n, c in project_rules_with_sources
                           if n == ".lingxirules"]

            if has_non_lingxi or len(root_lingxi) == 0:
                # 有 AGENTS.md / CLAUDE.md 或子目录规则，用新分层格式
                project_rules = load_project_rules(project_root)
                base = base + (
                    "\n\n# 项目指令（来自 CLAUDE.md / AGENTS.md / .lingxirules，越靠近目标优先级越高）\n"
                    + project_rules
                )
            else:
                # 只有项目根 .lingxirules（最常见场景），保持旧格式注入
                rules_text = root_lingxi[0]
                base = base + (
                    "\n\n# 项目级自定义指令（来自 .lingxirules，优先级最高）\n"
                    "以下是当前项目维护者写下的规则，**优先于上面任何通用约定**。"
                    "如果两者冲突，按这里的来：\n\n"
                    + rules_text
                )

    # ── Plan / Act mode ──
    # Plan 模式：AI 只调研、给方案，**不允许动手改**任何东西。强制提示比单纯
    # 工具白名单更稳——很多模型会试图用"伪工具"绕过限制。
    agent_mode = getattr(_state, "agent_mode", "act")
    if agent_mode == "plan":
        base = base + (
            "\n\n# ⚠ 当前是 Plan（计划）模式\n"
            "**你只能调研、阅读、给出执行方案，不允许直接动手改任何东西**。\n"
            "- ✅ 允许：`read_file` / `list_directory` / `search_in_file` / `search_files` / `get_project_instructions`（只读工具）\n"
            "- ❌ 禁止：`write_file` / `edit_file` / `append_file` / `run_command`\n"
            "- 给方案时**列清楚步骤**：要改哪些文件、改成什么、跑哪些命令验证\n"
            "- 用户认可方案后会切回 Act 模式，你再实际执行\n"
            "- 如果用户在 Plan 模式下问『快帮我改 X』，**先给方案不要直接改**，提醒他切到 Act 模式"
        )

    # 长期记忆（无条件，全局）
    from .memory_store import render_memories_for_prompt
    from .limits import MEMORY_MAX_CHARS
    mem = render_memories_for_prompt(max_chars=MEMORY_MAX_CHARS)
    if mem:
        base = base + "\n\n" + mem

    # 当前任务计划（会话级，由 update_plan 维护）——每轮注入让模型看到进度，防"做一半"
    from . import state as _st
    plan = getattr(_st, "current_plan", None)
    if plan:
        base = base + (
            "\n\n# 当前任务计划（你之前用 update_plan 列的）\n"
            "按这个清单推进，每开始/完成一步就调 update_plan 更新状态。"
            "**所有步骤都标 [x] 之前，不要当任务已完成而收尾**：\n\n"
            + _st.render_plan(plan)
        )

    # 当前任务台账（自动记录的已改文件/已跑命令）——逐轮重新渲染注入，survive 压缩
    ledger = getattr(_st, "task_ledger", None)
    ledger_text = _st.render_task_ledger(ledger) if ledger else ""
    if ledger_text:
        base = base + (
            "\n\n# 当前任务进度（自动记录，供你参考）\n"
            "以下是本次任务里你已经改过的文件 / 跑过的命令，用来帮你记住进度、避免重复改或漏步。\n\n"
            + ledger_text
        )

    return base


def get_external_agent_context() -> str:
    """给【外部 agent】（如 Claude Code CLI，自带成套工具）用的精简上下文。

    只含：角色卡 / 当前项目根 / 项目规则（CLAUDE.md·AGENTS.md·.lingxirules）/ 长期记忆。
    **不含**灵犀自己的工具说明（edit_file / run_command / update_plan 等）——外部 agent 有
    它自己的工具，注入灵犀工具指令会让它调用根本不存在的工具、造成混乱。Plan/Act 的只读
    约束由调用方用 --permission-mode 强制（见 claude_code.py），不在这里靠提示词重复。
    """
    from . import session as _session
    parts: list[str] = []

    # 角色卡（优先本轮快照，回退全局）
    _sess = _session.current_session()
    _snap = getattr(_sess, "role_snapshot", None)
    role_content = _snap["content"] if _snap is not None else _role_card_content
    if role_content:
        parts.append("# 角色设定（必须严格遵守）\n\n" + role_content)

    # 当前项目根 + 分层项目规则
    project_root = _session.current_project()
    if project_root and os.path.isdir(project_root):
        parts.append(f"# 项目\n当前项目根目录: `{project_root}`")
        rules = load_project_rules(project_root)
        if rules:
            parts.append(
                "# 项目指令（来自 CLAUDE.md / AGENTS.md / .lingxirules，优先级最高）\n" + rules
            )

    # 长期记忆（无条件注入）
    from .memory_store import render_memories_for_prompt
    from .limits import MEMORY_MAX_CHARS
    mem = render_memories_for_prompt(max_chars=MEMORY_MAX_CHARS)
    if mem:
        parts.append(mem)

    return "\n\n".join(parts)


# 项目规则文件读取上限（单文件 / 合并后总量）
_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", ".lingxirules")
_SINGLE_RULE_MAX = 20000
_COMBINED_RULES_MAX = 40000
# 向后兼容：旧代码/测试可能引用 _LINGXIRULES_MAX
_LINGXIRULES_MAX = _SINGLE_RULE_MAX


def _load_rules_from_dir(directory: str) -> list[tuple[str, str]]:
    """读取一个目录下的规则文件，返回 [(relative_name, content), ...]。

    按 _RULE_FILENAMES 顺序，只读取存在的文件。
    单文件超过 _SINGLE_RULE_MAX 字符时截断并附提示。
    读取失败的文件跳过并写 warning 日志。
    """
    results = []
    for name in _RULE_FILENAMES:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"读取规则文件失败 {path}: {e}")
            continue
        if len(content) > _SINGLE_RULE_MAX:
            content = content[:_SINGLE_RULE_MAX] + (
                f"\n\n... [{name} 过长，已截断至前 {_SINGLE_RULE_MAX} 字符]"
            )
        stripped = content.strip()
        if stripped:
            results.append((name, stripped))
    return results


def _project_rule_sources(
    project_root: str, target_path: str | None = None,
) -> list[tuple[str, str]]:
    """返回项目根到目标目录沿途的规则，拒绝越界和符号链接逃逸。"""
    if not project_root or not os.path.isdir(project_root):
        return []

    root_real = os.path.realpath(project_root)
    if target_path is None:
        target_dir = root_real
    else:
        target_raw = (
            target_path if os.path.isabs(target_path)
            else os.path.join(root_real, target_path)
        )
        target_real = os.path.realpath(target_raw)
        try:
            common = os.path.commonpath([target_real, root_real])
        except ValueError:
            return []
        if os.path.normcase(common) != os.path.normcase(root_real):
            return []
        if os.path.isfile(target_real):
            target_dir = os.path.dirname(target_real)
        elif os.path.isdir(target_real):
            target_dir = target_real
        else:
            # 不存在的目标按“将要新建的文件”处理，规则作用域到其父目录。
            target_dir = os.path.dirname(target_real)

    relative_dir = os.path.relpath(target_dir, root_real)
    dirs_to_scan = [root_real]
    if relative_dir != ".":
        current = root_real
        for part in relative_dir.split(os.sep):
            current = os.path.join(current, part)
            dirs_to_scan.append(current)

    all_rules = []
    for d in dirs_to_scan:
        for name, content in _load_rules_from_dir(d):
            rel = os.path.relpath(os.path.join(d, name), root_real).replace(os.sep, "/")
            all_rules.append((rel, content))
    return all_rules


def load_project_rules(project_root: str, target_path: str | None = None) -> str:
    """加载并格式化适用于 target_path 的分层项目规则。"""
    all_rules = _project_rule_sources(project_root, target_path)

    if not all_rules:
        return ""

    merged = "\n\n".join(
        f"## 来源：{rel}\n{content}" for rel, content in all_rules
    )
    if len(merged) > _COMBINED_RULES_MAX:
        merged = merged[:_COMBINED_RULES_MAX] + (
            f"\n\n... [项目规则合并后过长，已截断至前 {_COMBINED_RULES_MAX} 字符]"
        )

    return merged


def load_project_rules_with_sources(project_root: str, target_path: str | None = None) -> list[tuple[str, str]]:
    """加载项目规则并保留来源信息。"""
    return _project_rule_sources(project_root, target_path)


def _load_lingxirules(project_root: str) -> str:
    """读取项目根的 .lingxirules（兼容包装，只返回 .lingxirules 的纯文本内容）。

    不存在返回空字符串，不报错。
    """
    rules = _load_rules_from_dir(project_root)
    # 只取 .lingxirules 的内容
    for name, content in rules:
        if name == ".lingxirules":
            return content
    return ""


def _extract_character_name(content, fallback):
    """从角色卡内容里提取角色名：优先 H1 里 '· 角色名' 模式，其次 「角色名」 模式"""
    if not content:
        return fallback
    lines = content.split('\n')
    # 模式 1: 第一段 H1/H2 标题里 '· 角色名'
    for line in lines[:8]:
        line = line.strip()
        if line.startswith('#'):
            m = re.search(r'[·•・]\s*(\S+?)\s*$', line)
            if m:
                return m.group(1)
    # 模式 2: 前几行的 「角色名」
    for line in lines[:15]:
        m = re.search(r'[「『](.{1,12}?)[」』]', line)
        if m:
            return m.group(1)
    return fallback


def _ensure_memory_dir():
    os.makedirs(memory_dir(), exist_ok=True)


def set_role_card(content, name, path=None):
    global _role_card_content, _role_card_name, _role_card_path
    _role_card_content = content
    # 用提取到的角色名替代文件名，用于 UI 显示
    _role_card_name = _extract_character_name(content, name)
    _role_card_path = path
    # 持久化（保存提取后的名字，下次直接用）
    _ensure_memory_dir()
    with open(role_config(), "w", encoding="utf-8") as f:
        json.dump({"name": _role_card_name, "path": path}, f, ensure_ascii=False)
    logger.info(f"加载角色卡: {_role_card_name}")


def clear_role_card():
    global _role_card_content, _role_card_name, _role_card_path
    _role_card_content = None
    _role_card_name = None
    _role_card_path = None
    if os.path.exists(role_config()):
        os.remove(role_config())
    logger.info("清除角色卡，恢复默认")


def capture_active_role():
    """快照当前【前台/全局】激活的角色卡，供会话在一轮生成开始时冻结自己的人格。

    返回 dict（content/name/path）；content 为 None 表示无角色卡（用默认 SYSTEM_PROMPT）。
    返回新 dict（字符串不可变，无需深拷贝），写进 session.role_snapshot 后即与全局解耦。
    """
    return {
        "content": _role_card_content,
        "name": _role_card_name,
        "path": _role_card_path,
    }


def get_current_role_name():
    return _role_card_name


def get_current_role_path():
    return _role_card_path


def get_role_card_content():
    """供 claude_code 模式判断是否需要附加 system_prompt"""
    return _role_card_content


def load_saved_role_card():
    """启动时自动加载上次的角色卡"""
    global _role_card_content, _role_card_name, _role_card_path
    if not os.path.exists(role_config()):
        return
    try:
        with open(role_config(), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        path = cfg.get("path")
        name = cfg.get("name")
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _role_card_content = f.read()
            # 旧版本可能保存的是文件名，重新提取一次以使用真实角色名
            _role_card_name = _extract_character_name(_role_card_content, name)
            _role_card_path = path
            logger.info(f"自动加载角色卡: {_role_card_name}")
    except Exception as e:
        logger.warning(f"加载角色卡配置失败: {e}")
