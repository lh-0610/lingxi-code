# 多智能体并行与 worktree 隔离

## 解决什么问题
一个大任务里常有几个**相互独立**的子任务(比如"分别改三个不相关的模块")。串行做慢,并行做又怕多个 Agent 同时改文件互相踩踏。灵犀的解法:`spawn_agents` 把独立子任务并行派给子 Agent,**每个子 Agent 在自己的 git worktree 隔离区改代码**,跑完再合并回主项目。

## spawn_agents(`src/subagent.py`)
- 输入:多个独立子任务。
- 每个子 Agent 是 `is_subagent=True` 的会话,`ui_ref=None`(不弹前台确认卡——它没有前台)。
- 子 Agent 的文件/命令严格限定在自己的 worktree:`tools_common._subagent_path_rejection` / `_subagent_command_rejection` 做 best-effort 沙箱,越界直接拒绝。
- 用内部的 HeadlessUI 协议接住子 Agent 的输出(它没有真实 UI)。
- 全部跑完后合并各自的改动回主项目。

## worktree 隔离区(`src/worktree.py`)
- 用 `git worktree` 给每个子 Agent 开一个隔离的工作副本(同一个仓库、不同工作目录、独立分支/HEAD)。
- 管理隔离区的完整生命周期:创建 → 子 Agent 在里面干活 → 完成合并 → 清理。
- `Session.worktree` 字段路由该会话所有文件/命令的落点:`_project_cwd()` 优先返回 worktree 路径,所以子 Agent 的 `read_file`/`run_command` 天然落在隔离区里。

## 为什么用 git worktree 而不是拷贝目录
- worktree 共享同一个 `.git`(对象库),开销比整目录拷贝小得多。
- 天然带分支/提交能力,合并回主项目走 git 的正常流程,冲突可见可控。
- 隔离是真隔离:一个子 Agent 改崩了,不影响主工作区和其他子 Agent。

## 沙箱是 best-effort,不是安全边界
路径/命令拒绝是"防手滑越界",不是对抗恶意代码的安全沙箱。子 Agent 跑的是可信模型产出的操作,目的是**防止并行任务互相污染**,而非防入侵。这个定位要讲清楚——面试时别把它说成安全隔离。

## 与主循环的关系
`spawn_agents` 是一个写工具:Plan 模式 / 遥控触发时会被拦(它能改一堆文件)。主 Agent 调它 → 派生 N 个子 Agent 会话并行跑各自的 agent_loop → 汇总结果作为一条 ToolMessage 回到主循环。
