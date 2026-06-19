"""验证状态管理模块（纯函数 + 会话级状态操作）。

目标：在编码任务中，确保 AI 在声称"已完成"前必须先验证（跑测试 / 静态检查通过），
防止无验证的草率收尾。

验证状态存储在 `Session.verification`（会话级，多会话隔离）。
本模块提供纯函数操作这些状态 + 间隙检测，不引入循环依赖。
"""
import os


# 代码文件扩展名——写入这些文件需要代码验证（测试 / 静态检查）
_CODE_EXTENSIONS = frozenset({
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".vue", ".svelte",
})


def new_verification() -> dict:
    """创建一个全新的验证状态字典（Session.__init__ 调用）。"""
    return {
        "dirty_files": [],        # 相对路径列表（去重后）of 成功写入的文件
        "code_dirty_files": [],   # 需要代码验证的子集（代码文件）
        "checks": {},             # path -> {"passed": bool|None, "checker": str}
        "tests_run": False,       # 是否调过 run_tests
        "tests_passed": None,     # True/False/None（未跑 / 无法确定）
        "tests_reason": "",       # tests_passed=None 时的简短原因
        "diff_reviewed": False,   # 是否调过 git_diff（写入后）
        "gate_prompted": False,   # 完成闸门是否已提示过一次（两次尝试机制）
        "failure_diagnosis": {    # 自动修复循环状态（独立于上面的验证闸门）
            "tool": "",           # 触发失败的工具名（"run_tests" / "check_code"）
            "attempt": 0,         # 已注入修复提示的次数
            "max_attempts": 3,    # 最大修复尝试次数
            "reason": "",         # 失败原因简述
        },
    }


def reset_verification(v: dict) -> None:
    """新用户消息开始时重置验证状态（agent_loop 开头调用）。"""
    v["dirty_files"] = []
    v["code_dirty_files"] = []
    v["checks"] = {}
    v["tests_run"] = False
    v["tests_passed"] = None
    v["tests_reason"] = ""
    v["diff_reviewed"] = False
    v["gate_prompted"] = False
    # failure_diagnosis 在 check_repair_allowed() 成功通过时自动归零；
    # 新用户消息开始时也重置，防止上一轮残留状态。
    v["failure_diagnosis"] = {
        "tool": "", "attempt": 0, "max_attempts": 3, "reason": "",
    }


def _is_code_file(path: str) -> bool:
    """判断路径是否为代码文件（需要代码验证）。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in _CODE_EXTENSIONS


def mark_dirty(v: dict, rel_path: str) -> None:
    """写文件工具成功后调用，标记文件为脏。

    rel_path: 相对于项目根的路径（已规范化的）。
    """
    if not rel_path:
        return
    # 去重
    if rel_path not in v["dirty_files"]:
        v["dirty_files"].append(rel_path)
    if _is_code_file(rel_path) and rel_path not in v["code_dirty_files"]:
        v["code_dirty_files"].append(rel_path)
    # 写入即失效：该文件的静态检查结果作废
    v["checks"].pop(rel_path, None)
    # 写入即失效：diff 需要重新审查
    v["diff_reviewed"] = False
    # 写入即失效：测试结果作废（如果有代码文件被改）
    if _is_code_file(rel_path):
        v["tests_run"] = False
        v["tests_passed"] = None
        v["tests_reason"] = ""


def mark_check(v: dict, rel_path: str, passed: bool | None, checker: str = "") -> None:
    """静态检查工具成功后调用，记录检查结果。

    passed: True=通过, False=有问题, None=无法确定（不支持的语言 / 无检查器）
    checker: 检查器名称（如 "ruff", "py_compile", "check_command"）
    """
    if not rel_path:
        return
    v["checks"][rel_path] = {"passed": passed, "checker": checker}


def mark_tests(v: dict, passed: bool | None, reason: str = "") -> None:
    """run_tests 工具成功后调用，记录测试结果。

    passed: True=全部通过, False=有失败, None=无法确定
    reason: 简短原因（如 "0 failed", "解析未命中" 等）
    """
    v["tests_run"] = True
    v["tests_passed"] = passed
    v["tests_reason"] = reason


def mark_diff_reviewed(v: dict) -> None:
    """git_diff 工具成功后调用。"""
    v["diff_reviewed"] = True


def get_verification_gaps(v: dict) -> list[str]:
    """纯函数：检测验证间隙，返回需要补充验证的提示列表。

    空列表 = 无间隙，可以放心完成任务。
    非空列表 = 有未验证的改动，AI 应在回复完成前补充验证。
    """
    gaps = []
    has_code_changes = bool(v["code_dirty_files"])
    has_any_changes = bool(v["dirty_files"])

    if not has_any_changes:
        return []

    # 1. 测试未通过或未运行（仅有代码改动时检查）
    if has_code_changes:
        if not v["tests_run"]:
            gaps.append(
                f"代码文件被修改（{', '.join(v['code_dirty_files'])}）但尚未运行测试。"
                "请先调用 run_tests 验证改动不会引入回归。"
            )
        elif v["tests_passed"] is False:
            gaps.append("测试未通过（有失败用例），请先修复测试再声称任务完成。")

    # 2. 静态检查未通过
    failed_checks = [
        path for path, result in v["checks"].items()
        if result.get("passed") is False
    ]
    if failed_checks:
        gaps.append(
            f"以下文件的静态检查未通过（{', '.join(failed_checks)}），"
            "请先修复检查问题再声称任务完成。"
        )

    # 3. diff 未审查（有改动时检查）
    if has_any_changes and not v["diff_reviewed"]:
        gaps.append("本轮已有文件修改，但尚未调用 git_diff 查看最终改动。")

    return gaps


def needs_verification(v: dict) -> bool:
    """快速判断是否有任何需要验证的内容。"""
    return bool(v["dirty_files"])


# ── 自动修复循环 ──

# 识别可触发修复循环的工具（与 _REPAIR_TOOLS 对应）
_REPAIR_TOOLS = {"run_tests", "check_code"}


def _is_failure_result(content: str, tool_name: str) -> bool:
    """根据工具返回内容判断是否为失败结果（纯函数，不读全局状态）。"""
    if tool_name == "run_tests":
        # 可靠失败标记：pytest 的 FAILED/ERROR(段)、解析器的 ❌/失败。
        # 不用裸 "error"/"Error" 子串——通过的运行里 warning 文本或名字含 error 的用例
        # 会被误判成失败，白触发一轮修复。
        return any(marker in content for marker in (
            "❌", "FAILED", "ERROR", "失败",
        ))
    if tool_name == "check_code":
        # 有检查问题（不是"✅"开头，也不是"未知"降级）
        return "⚠️" in content or "❌" in content
    return False


def check_repair_allowed(
    v: dict,
    tool_name: str,
    result_content: str,
) -> tuple[bool, str]:
    """检测 run_tests/check_code 失败结果，返回 (是否应注入修复提示, 诊断原因)。

    同时原子性更新 v["failure_diagnosis"] 的 attempt 计数。
    由 agent_loop 在工具执行后、往 chat_history 插入 ToolMessage 前调用。

    返回值：
        (True, reason)  —— 应注入修复提示，agent 继续自动修复
        (False, reason) —— 不应注入。reason 为：
            - None: 该工具不触发修复（非 run_tests/check_code）或结果为成功
            - str: 已达最大重试次数（可提示给用户的自然语言）
    """
    if tool_name not in _REPAIR_TOOLS:
        return False, None
    if not v.get("code_dirty_files"):
        return False, None

    if not _is_failure_result(result_content, tool_name):
        # 成功：归零诊断状态（修复后测试通过）
        v["failure_diagnosis"]["tool"] = ""
        v["failure_diagnosis"]["attempt"] = 0
        v["failure_diagnosis"]["reason"] = ""
        return False, None

    diag = v["failure_diagnosis"]
    max_att = diag["max_attempts"]

    # 达到最大重试次数 → 不再注入，交由完成闸门或模型自行收尾
    if diag["attempt"] >= max_att:
        last_reason = diag.get("reason", "")
        limit_reason = f"自动修复已尝试 {max_att} 次仍未通过"
        if last_reason and limit_reason not in last_reason:
            limit_reason += f"：{last_reason}"
        diag["reason"] = limit_reason
        return False, limit_reason

    # 更新诊断状态：记录失败工具、原因、递增尝试次数
    diag["tool"] = tool_name
    diag["attempt"] += 1
    # 从内容提取简短原因（取第一个失败行作为摘要）
    diag["reason"] = _extract_failure_summary(result_content)

    return True, diag["reason"]


def _extract_failure_summary(content: str) -> str:
    """从工具返回内容中提取失败摘要（纯函数）。"""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        # 优先找 pytest 失败摘要行（FAILED / Error / 失败数）
        if "FAILED" in stripped or "失败" in stripped:
            return stripped[:200]
        if stripped.startswith("❌"):
            return stripped[:200]
    return content[:200] if content else "未知失败"


def get_failure_diagnosis(v: dict) -> dict:
    """获取当前会话的失败诊断状态（只读）。"""
    return v.get("failure_diagnosis", {})


def inject_repair_prompt(v: dict) -> str:
    """生成自动修复提示文本，由 agent_loop 注入到 chat_history。

    包含：已尝试次数、失败摘要、明确修复指令。
    """
    diag = v["failure_diagnosis"]
    attempt = diag["attempt"]
    max_att = diag["max_attempts"]
    tool = diag["tool"]
    reason = diag["reason"]

    remaining = max_att - attempt

    prompt = (
        f"⚠️ 自动诊断：刚才的 {tool} 失败了（{reason}）。\n"
        f"这是第 {attempt}/{max_att} 次自动修复尝试，还能重试 {remaining} 次。\n\n"
        "请立即：\n"
        "1. 分析上面的失败输出，定位失败的具体代码位置\n"
        "2. 找到相关文件，只修改导致失败的最小范围\n"
        "3. 重新运行对应的测试或检查命令验证修复\n\n"
        "注意：修复应尽可能小范围，不要改动不相关的代码。"
    )
    return prompt
