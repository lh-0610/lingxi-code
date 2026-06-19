import os
import sys
import time
import re
import contextlib
import difflib
import shutil
import subprocess
import threading
from collections import deque
from langchain_core.tools import tool

from . import state
from . import session as _session
from . import checkpoint as _checkpoint
from .verification import mark_tests as _v_mark_tests
from .paths import logger
from .tools_common import (  # 共享底座（拆包：路径/项目根/子 Agent 沙箱/验证标记/shell cwd/搜索常量）
    _project_cwd, _resolve_path, _subagent_path_rejection, _subagent_command_rejection,
    _mark_current_dirty, _mark_current_check, _shell_cwd, _parse_cd,
    _SEARCH_IGNORE_DIRS, _SEARCH_MAX_FILE_SIZE,
)
from .limits import (
    BG_MAX_RETAINED_EXITED,
    READ_FILE_DEFAULT_LIMIT,
    RUN_COMMAND_MAX_OUTPUT_CHARS,
    RUN_COMMAND_TIMEOUT_S,
    SEARCH_FILES_MAX_RESULTS,
    SEARCH_IN_FILE_DEFAULT_LIMIT,
    SEARCH_IN_FILE_MAX_LIMIT,
)


@tool
def read_file(path: str, offset: int = 1, limit: int = READ_FILE_DEFAULT_LIMIT) -> str:
    """读取文件内容，按行返回（带行号前缀，方便后续 edit_file 定位）。

    参数：
      path: 文件路径（绝对或相对项目根）
      offset: 起始行号（**从 1 开始**），默认 1
      limit: 最多读取的行数，默认 2000（大文件请分批）

    返回格式（类 `cat -n`）：
        1: import os
        2: import sys
        ...
        [显示第 1-50 行 / 共 200 行]

    用法：
      - 想看大文件中段：`read_file("a.py", offset=500, limit=200)`
      - 默认 2000 行通常已经够；如果文件 > 2000 行，会自动截断并提示总行数
    """
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        with open(full, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except Exception as e:
        return f"读取失败: {e}"

    total = len(all_lines)
    if total == 0:
        return "（空文件）"

    # 1-indexed offset，做边界保护
    if offset < 1:
        offset = 1
    if offset > total:
        return f"[文件共 {total} 行，offset={offset} 超出范围]"
    if limit < 1:
        limit = 1

    start = offset - 1
    end = min(start + limit, total)
    lines = all_lines[start:end]

    # 行号宽度按总行数算（5 位足够 99999 行）
    width = max(4, len(str(end)))
    rendered = "\n".join(
        f"{(start + i + 1):>{width}}: {ln.rstrip()}" for i, ln in enumerate(lines)
    )

    if end >= total and offset == 1:
        footer = f"\n[完整文件，共 {total} 行]"
    elif end >= total:
        footer = f"\n[显示第 {offset}-{end} 行 / 共 {total} 行（已读到末尾）]"
    else:
        remaining = total - end
        footer = (
            f"\n[显示第 {offset}-{end} 行 / 共 {total} 行，"
            f"还有 {remaining} 行未读——继续读用 offset={end + 1}]"
        )
    return rendered + footer


def _confirm_file_write(full: str, old_content: str, new_content: str):
    """写盘前的 diff 确认（写盘类工具共用）。

    worker 线程算 unified diff → 通过 SignalBridge 投到 UI 主线程弹 diff 卡 →
    阻塞等用户点完。无 UI（CLI / 测试）时返回 (True, None) 直接放行。

    返回 (allowed, reject_message)：allowed=True 时 reject_message 为 None；
    allowed=False 时 reject_message 是给 AI 的拒绝文案。
    """
    if full != "(patch)":
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return False, reject
    try:
        from . import session as _session
        if getattr(_session.current_session(), "is_subagent", False):
            return True, None
    except Exception:
        pass
    ui = getattr(state, "ui_ref", None)
    if ui is None:
        return True, None
    import difflib as _difflib
    base = os.path.basename(full)
    diff_text = "".join(_difflib.unified_diff(
        (old_content or "").splitlines(keepends=True),
        (new_content or "").splitlines(keepends=True),
        fromfile=f"a/{base}",
        tofile=f"b/{base}",
        n=3,
    ))
    if not diff_text:
        diff_text = f"--- a/{base}\n+++ b/{base}\n(无 diff，可能是看不见的空白差异)\n"
    try:
        allowed, user_feedback = ui.confirm_edit(full, diff_text)
    except Exception as e:
        logger.warning(f"文件写入确认对话框异常，默认拒绝: {e}")
        return False, f"用户确认对话框出错，已拒绝写入: {e}"
    if not allowed:
        _msg = "已拒绝：用户不允许此次写入。"
        if user_feedback:
            _msg += f"\n用户补充说明：{user_feedback}"
        logger.info(f"用户拒绝写入 {full}")
        return False, _msg
    return True, None


@tool
def write_file(path: str, content: str) -> str:
    """写入内容到文件（覆盖）。path: 文件路径, content: 要写入的内容"""
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        # 读旧内容算 diff（文件不存在视为空）
        old_content = ""
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""
        # 写盘前确认（全量覆盖比 edit_file 更危险，必须让用户审 diff）
        allowed, reject = _confirm_file_write(full, old_content, content)
        if not allowed:
            return reject
        os.makedirs(os.path.dirname(os.path.abspath(full)), exist_ok=True)
        # 改动前打 checkpoint（git 项目自动 stash 一份，方便用户撤销）
        proj = _project_cwd()
        try:
            _checkpoint.make_checkpoint(proj, "write_file", full)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响写入）: {e}")
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        _mark_current_dirty(full)
        return f"成功写入文件: {full}" + _auto_check_suffix(full)
    except Exception as e:
        return f"写入失败: {e}"


@tool
def append_file(path: str, content: str) -> str:
    """追加内容到文件末尾。path: 文件路径, content: 要追加的内容"""
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        # 读旧内容算 diff（追加 = 旧内容 + 新内容）
        old_content = ""
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as f:
                    old_content = f.read()
            except Exception:
                old_content = ""
        allowed, reject = _confirm_file_write(full, old_content, old_content + content)
        if not allowed:
            return reject
        proj = _project_cwd()
        try:
            _checkpoint.make_checkpoint(proj, "append_file", full)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响追加）: {e}")
        with open(full, "a", encoding="utf-8") as f:
            f.write(content)
        _mark_current_dirty(full)
        return f"成功追加到文件: {full}" + _auto_check_suffix(full)
    except Exception as e:
        return f"追加失败: {e}"


def _get_indent(line):
    return line[:len(line) - len(line.lstrip())]


def _detect_indent_unit(lines):
    """从一组行里推断"一级缩进"。

    取最短的非空前导空白当一级单元。整段顶格则返回 ""（无依据）。
    例:
      ['class Foo:', '    def bar():', '        return 1']  → '    '
      ['class Foo:', '\tdef bar():',   '\t\treturn 1']      → '\t'
      ['x = 1', 'y = 2']                                     → ''  （整段顶格）
    """
    units = []
    for ln in lines:
        if not ln.strip():
            continue  # 跳过空行
        leading = ln[:len(ln) - len(ln.lstrip())]
        if leading:
            units.append(leading)
    if not units:
        return ""
    return min(units, key=len)


def _realign_indent(new_string, file_indent_unit, model_indent_unit):
    """按缩进单元换算：模型 N 级 model_unit → file 的 N 级 file_unit。

    比"首行 prefix 替换"更鲁棒:
    - 文件 / 模型首行顶格时仍能从子行推断 unit
    - tab ↔ 空格混用时按层级正确换算
    """
    if not model_indent_unit or not file_indent_unit:
        return new_string  # 任一侧整段顶格，无依据重算

    mu_len = len(model_indent_unit)
    result = []
    for line in new_string.splitlines(keepends=True):
        if not line.strip():
            result.append(line)  # 空行原样
            continue
        leading = line[:len(line) - len(line.lstrip())]
        level = len(leading) // mu_len
        result.append(file_indent_unit * level + line.lstrip())
    return "".join(result)


def _matched_span_end(file_lines, start_line, line_count, old):
    """匹配区间的结束字符位置。L2/L3/L4 按行匹配后算区间——若 old 末行不带换行
    （read_file 用 rstrip 展示，model 给的 old/new 通常都没有尾换行），就不要把文件
    末匹配行的换行算进区间，否则替换成无尾换行的 new_string 后会把下一行黏上来。"""
    end = sum(len(file_lines[j]) for j in range(start_line + line_count))
    if not old.endswith("\n"):
        last = file_lines[start_line + line_count - 1]
        end -= len(last) - len(last.rstrip("\r\n"))   # 减掉文件末匹配行的换行（\n 或 \r\n）
    return end


def _locate_edit(content: str, old: str, new_string: str, replace_all: bool):
    """分层匹配级联：L1 精确 → L2 去行尾空白 → L3 去全部首尾空白+缩进重对齐 → L4 模糊。

    返回 (status, spans, new_texts, info)：
      status: "exact" | "normalized" | "fuzzy" | "multi" | "none"
      spans: [(start_char, end_char)] 要替换的【文件真实】字符区间
      new_texts: 与 spans 对应的替换文本（L3/L4 已做缩进重对齐；L1/L2 直接用 new_string）
      info: 成功时为 (match_level_desc, line_numbers)；失败时为 (closest_snippet_desc, None)
    """
    old_lines = old.splitlines(keepends=True)
    file_lines = content.splitlines(keepends=True)
    old_line_count = len(old_lines)
    file_line_count = len(file_lines)

    if old_line_count == 0:
        return "none", [], [], ("old_string 为空行", None)

    # ── L1 精确匹配 ──
    count = content.count(old)
    if count > 0:
        if count > 1 and not replace_all:
            # 多处命中，收集行号
            line_nos = []
            idx = 0
            while True:
                idx = content.find(old, idx)
                if idx == -1:
                    break
                line_nos.append(content[:idx].count("\n") + 1)
                idx += 1
            return "multi", [], [], (f"L1 精确匹配到 {count} 处", line_nos)
        # replace_all 或唯一命中
        spans = []
        idx = 0
        while True:
            idx = content.find(old, idx)
            if idx == -1:
                break
            spans.append((idx, idx + len(old)))
            idx += len(old)
        line_no = content[:spans[0][0]].count("\n") + 1
        return "exact", spans, [new_string] * len(spans), ("L1 精确匹配", [line_no])

    # ── L2 逐行 rstrip 比对（按行滑窗）──
    def _rstrip_lines(lines):
        return [ln.rstrip() for ln in lines]

    old_rstripped = _rstrip_lines(old_lines)
    file_rstripped = _rstrip_lines(file_lines)
    l2_hits = []
    for i in range(file_line_count - old_line_count + 1):
        if file_rstripped[i:i + old_line_count] == old_rstripped:
            l2_hits.append(i)
    if l2_hits:
        if len(l2_hits) > 1 and not replace_all:
            line_nos = [i + 1 for i in l2_hits]
            return "multi", [], [], (f"L2 去行尾空白匹配到 {len(l2_hits)} 处", line_nos)
        # 唯一或 replace_all
        spans = []
        for start_line in l2_hits:
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = _matched_span_end(file_lines, start_line, old_line_count, old)
            spans.append((char_start, char_end))
        line_no = l2_hits[0] + 1
        return "normalized", spans, [new_string] * len(spans), ("L2 去行尾空白匹配", [line_no])

    # ── L3 逐行 strip 比对 + 缩进重对齐 ──
    old_stripped = [ln.strip() for ln in old_lines]
    file_stripped = [ln.strip() for ln in file_lines]
    l3_hits = []
    for i in range(file_line_count - old_line_count + 1):
        if file_stripped[i:i + old_line_count] == old_stripped:
            l3_hits.append(i)
    if l3_hits:
        if len(l3_hits) > 1 and not replace_all:
            line_nos = [i + 1 for i in l3_hits]
            return "multi", [], [], (f"L3 strip 匹配到 {len(l3_hits)} 处", line_nos)
        # 唯一或 replace_all → 做缩进重对齐
        spans = []
        new_texts = []
        for start_line in l3_hits:
            file_indent_unit = _detect_indent_unit(file_lines[start_line:start_line + old_line_count])
            model_indent_unit = _detect_indent_unit(old_lines)
            realigned = _realign_indent(new_string, file_indent_unit, model_indent_unit)
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = _matched_span_end(file_lines, start_line, old_line_count, old)
            spans.append((char_start, char_end))
            new_texts.append(realigned)
        line_no = l3_hits[0] + 1
        return "normalized", spans, new_texts, ("L3 strip+缩进重对齐匹配", [line_no])

    # ── L4 difflib 模糊滑窗（多档容差）──
    # 尝试 [len-2, len], [len-1, len+1], [len, len+2] 窗口大小
    best_hits = []  # (start_line, ratio, window_size)
    for delta in range(-2, 3):
        ws = old_line_count + delta
        if ws < 1 or ws > file_line_count:
            continue
        sm = difflib.SequenceMatcher()
        sm.set_seq2(old_stripped)
        for i in range(file_line_count - ws + 1):
            sm.set_seq1(file_stripped[i:i + ws])
            ratio = sm.ratio()
            if ratio >= 0.85:
                best_hits.append((i, ratio, ws))

    if best_hits:
        # 找最优
        max_ratio = max(r for _, r, _ in best_hits)
        # 次优低于最优 0.1 以上才算唯一
        sorted_ratios = sorted(set(r for _, r, _ in best_hits), reverse=True)
        second_best = sorted_ratios[1] if len(sorted_ratios) > 1 else 0
        unique = (max_ratio - second_best) >= 0.1

        if not unique and not replace_all:
            # 多个等价候选
            candidates = [(s, r, w) for s, r, w in best_hits if r >= max_ratio - 0.05]
            line_nos = [s + 1 for s, _, _ in candidates]
            return "multi", [], [], (f"L4 模糊匹配到 {len(candidates)} 个相似位置", line_nos)

        # 取最优的那些（ratio 最高的）
        top_hits = [(s, r, w) for s, r, w in best_hits if abs(r - max_ratio) < 0.001]
        if not replace_all and len(top_hits) > 1:
            line_nos = [s + 1 for s, _, _ in top_hits]
            return "multi", [], [], (f"L4 模糊匹配到 {len(top_hits)} 个相似位置", line_nos)

        # 缩进重对齐（使用模块级 _realign_indent）
        spans = []
        new_texts = []
        for start_line, ratio, window_size in top_hits:
            file_indent_unit = _detect_indent_unit(file_lines[start_line:start_line + window_size])
            model_indent_unit = _detect_indent_unit(old_lines)
            realigned = _realign_indent(new_string, file_indent_unit, model_indent_unit)
            char_start = sum(len(file_lines[j]) for j in range(start_line))
            char_end = _matched_span_end(file_lines, start_line, window_size, old)
            spans.append((char_start, char_end))
            new_texts.append(realigned)
        line_no = top_hits[0][0] + 1
        return "fuzzy", spans, new_texts, (f"L4 模糊匹配 (ratio={max_ratio:.2f})", [line_no])

    # ── 全部失败 → 自纠反馈 ──
    # 用 difflib 找文件里与 old 最相似的片段
    best_i = 0
    best_ratio = 0.0
    if file_line_count >= old_line_count:
        sm = difflib.SequenceMatcher()
        sm.set_seq2(old_stripped)
        for i in range(file_line_count - old_line_count + 1):
            sm.set_seq1(file_stripped[i:i + old_line_count])
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_i = i
    else:
        # 文件比 old 还短，整体比较
        sm = difflib.SequenceMatcher(None, old_stripped, file_stripped)
        best_ratio = sm.ratio()

    # 取最接近片段上下文 ±2 行
    snippet_start = max(0, best_i - 2)
    snippet_end = min(file_line_count, best_i + old_line_count + 2)
    snippet_lines = []
    for idx in range(snippet_start, snippet_end):
        snippet_lines.append(f"  第 {idx + 1} 行: {file_lines[idx].rstrip()}")
    snippet_text = "\n".join(snippet_lines)
    desc = (
        f"失败：没找到匹配的 old_string。文件里最接近的是第 {best_i + 1}–{best_i + old_line_count} 行"
        f"（相似度 {best_ratio:.0%}）：\n{snippet_text}\n"
        "请直接复制上面的真实内容作为 old_string 重试（注意缩进与空行）。"
    )
    return "none", [], [], (desc, None)


@tool
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """在文件中精确替换字符串（适合改大文件的局部，比 write_file 全量重写更安全更省 token）。

    - `old_string` 必须**完整**包含要被替换的那段（保留缩进、换行、标点）；
    - 默认要求 `old_string` 在文件中**只出现一次**——出现多次或没找到都会报错；
    - 想替换所有出现请显式传 `replace_all=True`；
    - 用这个工具比 write_file 安全：write_file 是全文覆盖容易丢内容，edit_file 只动指定那段。

    参数：
      path: 文件路径（绝对或相对项目根）
      old_string: 要被替换的旧文本（必须与文件中的原文一字不差，含空白）
      new_string: 替换成的新文本
      replace_all: True 时替换全部出现；False（默认）时要求只出现一次
    """
    if not old_string:
        return "失败：old_string 不能为空"
    if old_string == new_string:
        return "失败：old_string 和 new_string 相同，不需要替换"

    full = _resolve_path(path)
    reject = _subagent_path_rejection(full, "文件")
    if reject:
        return reject
    if not os.path.exists(full):
        return f"失败：文件不存在 {full}"

    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"读取失败: {e}"

    # ── 分层匹配级联 ──
    status, spans, new_texts, info = _locate_edit(content, old_string, new_string, replace_all)
    match_desc, line_nos = info

    # multi：多处候选，返回行号提示
    if status == "multi":
        lines_str = ", ".join(str(n) for n in line_nos)
        return (
            f"失败：{match_desc}（行 {lines_str}）。"
            "请提供更多上下文让它唯一，或显式传 replace_all=True 替换全部。"
        )

    # none：全部失败，返回自纠反馈
    if status == "none":
        return match_desc  # 已包含完整自纠文案

    # ── 成功命中：构建新内容 ──
    # spans 按位置排序（从后往前替换避免偏移）
    if spans:
        sorted_pairs = sorted(zip(spans, new_texts), key=lambda x: x[0][0], reverse=True)
        new_content = content
        for (start, end), replacement in sorted_pairs:
            new_content = new_content[:start] + replacement + new_content[end:]
    else:
        # fallback（不应发生）
        new_content = content.replace(old_string, new_string)

    # ── Diff 预览 + 用户确认（写盘前的最后一道关）──
    allowed, reject = _confirm_file_write(full, content, new_content)
    if not allowed:
        return reject

    # 写盘前打 checkpoint
    try:
        _checkpoint.make_checkpoint(_project_cwd(), "edit_file", full)
    except Exception as e:
        logger.warning(f"checkpoint 失败（不影响编辑）: {e}")

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"写入失败: {e}"

    _mark_current_dirty(full)

    # 成功信息
    count = len(spans)
    primary_line = line_nos[0] if line_nos else "?"
    level_hint = f"（{match_desc}）" if "L1" not in match_desc else ""
    suffix = _auto_check_suffix(full)
    if count == 1:
        return f"成功编辑 {full}（第 {primary_line} 行附近替换 1 处）{level_hint}" + suffix
    else:
        return f"成功编辑 {full}（替换全部 {count} 处出现，第一处在第 {primary_line} 行）{level_hint}" + suffix


@tool
def list_directory(path: str = ".") -> str:
    """列出目录下的文件和文件夹。path: 目录路径，默认当前项目根（无项目时为进程目录）"""
    try:
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "目录")
        if reject:
            return reject
        items = os.listdir(full)
        dirs, files = [], []
        for item in sorted(items):
            full_item = os.path.join(full, item)
            if os.path.isdir(full_item):
                dirs.append(f"📁 {item}/")
            else:
                size = os.path.getsize(full_item)
                if size < 1024:
                    s = f"{size}B"
                elif size < 1024 * 1024:
                    s = f"{size/1024:.1f}KB"
                else:
                    s = f"{size/1024/1024:.1f}MB"
                files.append(f"📄 {item}  ({s})")
        result = dirs + files
        header = f"[目录: {full}]\n"
        return header + ("\n".join(result) if result else "空目录")
    except Exception as e:
        return f"列目录失败: {e}"


BLOCKED_COMMANDS = [
    "more", "pause", "edit", "choice", "set /p",
    "cmd /k", "powershell -noexit", "python -i",
    "nslookup", "ftp", "telnet", "ssh", "diskpart",
]


def _kill_proc_tree(proc):
    """跨平台杀整个进程树。

    Windows: `shell=True` 的 Popen 启动的是 cmd.exe，cmd 又 spawn 真正的命令进程。
    `proc.kill()` 只杀 cmd，子进程会继续跑（"中断不掉"的根因）。这里用
    `taskkill /F /T /PID` 把进程树整个连根拔。
    Unix: 进程组可以一起杀，但 shell=True 也有类似问题，这里 fallback 到 proc.kill()。
    """
    if proc is None or proc.poll() is not None:
        return
    import sys as _sys
    if _sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _decode_chunk(b: bytes) -> str:
    """命令输出按 utf-8 → gbk 顺序兜底解码。Windows 中文环境下 npm/pip/git 走 UTF-8、
    cmd 内置走 GBK，混着来很常见。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


# ══════════════════════════════════════
# 后台进程注册表
# ══════════════════════════════════════
# bg_id → {proc, command, output: deque(maxlen=2000), start_ts}
_bg_procs: dict[str, dict] = {}
_bg_lock = threading.Lock()
_bg_counter = [0]


def _new_bg_id() -> str:
    with _bg_lock:
        _bg_counter[0] += 1
        return f"bg{_bg_counter[0]}"


def _evict_old_exited_bg() -> None:
    """淘汰积累的【已退出】后台进程，保留最近 BG_MAX_RETAINED_EXITED 个仍可读最终输出。

    必须在持有 _bg_lock 时调用。只淘汰已退出项（proc.poll() 非 None），运行中的从不动；
    按 start_ts 淘汰最老的。防长会话里崩溃/跑完的后台任务连同 2000 行输出 deque 无限驻留。
    """
    exited = [(sid, info) for sid, info in _bg_procs.items()
              if info["proc"].poll() is not None]
    if len(exited) <= BG_MAX_RETAINED_EXITED:
        return
    exited.sort(key=lambda kv: kv[1]["start_ts"])  # 最老的在前
    for sid, _info in exited[:len(exited) - BG_MAX_RETAINED_EXITED]:
        _bg_procs.pop(sid, None)


@tool
def run_command(command: str, timeout: int | None = None, background: bool = False) -> str:
    """执行系统命令并**流式**返回输出（边跑边显示，不必等命令结束）。

    命令耗时 > 几秒时（pytest / npm test / build / 长 curl 等），UI 上能实时
    看到 stdout/stderr 进度；AI 拿到的工具结果仍是完整输出（超过 5000 字会截断）。
    默认 5 分钟超时；传 timeout 参数（秒）可覆盖（如跑大测试套件传 600）。
    随时可点停止按钮中断；执行前会弹用户确认卡片；危险命令需用户允许。

    background=True 时命令在后台运行（适用于 dev server / watch / 长服务），
    立即返回 bg_id；用 read_background_output 看输出，stop_background_command 停止。
    """
    import threading as _thr_local

    effective_timeout = timeout if timeout is not None else RUN_COMMAND_TIMEOUT_S

    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        parts = [p.strip() for p in cmd_lower.replace("&&", "|").split("|")]
        for part in parts:
            if part == blocked or part.startswith(blocked + " "):
                    return f"拒绝执行: '{blocked}' 是交互式命令，会导致程序挂起"

    # ── 纯 cd 拦截：只切目录、不弹确认、不起进程 ──
    cd_target = _parse_cd(command)
    if cd_target is not None:
        if os.path.isdir(cd_target):
            reject = _subagent_path_rejection(cd_target, "目录")
            if reject:
                return reject
            state.shell_cwd = cd_target
            return f"已切换工作目录到: {cd_target}"
        return f"目录不存在: {cd_target}"

    # ── 用户确认（同原逻辑）──
    try:
        from . import session as _session
        if getattr(_session.current_session(), "is_subagent", False):
            ui = None
        else:
            ui = getattr(state, "ui_ref", None)
    except Exception:
        ui = getattr(state, "ui_ref", None)
    if ui is not None:
        try:
            allowed, user_feedback = ui.confirm_command(command)
        except Exception as e:
            logger.warning(f"确认对话框异常，默认拒绝执行: {e}")
            return f"用户确认对话框出错，已拒绝执行: {e}"
        if not allowed:
            _msg = "已拒绝：用户不允许执行此命令。"
            if user_feedback:
                _msg += f"\n用户补充说明：{user_feedback}"
            logger.info(f"用户拒绝执行命令: {command}")
            return _msg

    run_cwd = _shell_cwd()
    reject = _subagent_command_rejection(command, run_cwd)
    if reject:
        return reject

    # stderr 合并进 stdout 走同一管道，按时间顺序输出（不再分开拼接）
    try:
        proc = subprocess.Popen(
            command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=run_cwd,
            bufsize=0,
        )
    except Exception as e:
        return f"启动失败: {e}"

    # ── 后台模式：起 reader 线程写 deque，立即返回 ──
    if background:
        bg_id = _new_bg_id()
        out_deque: deque[str] = deque(maxlen=2000)
        start_ts = time.time()

        def _bg_reader():
            """后台 reader：把输出 append 进 deque，不刷 UI。"""
            try:
                buf = b""
                while True:
                    raw = proc.stdout.read(4096)
                    if not raw:
                        if buf:
                            text = _decode_chunk(buf)
                            with _bg_lock:
                                out_deque.append(text)
                        break
                    buf += raw
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = _decode_chunk(line + b"\n")
                        with _bg_lock:
                            out_deque.append(text)
            except Exception:
                pass  # 进程被杀时 stdout 关闭会抛异常，忽略

        with _bg_lock:
            _evict_old_exited_bg()   # 注册新进程前先淘汰积累的已退出项，防无限驻留
            _bg_procs[bg_id] = {
                "proc": proc,
                "command": command,
                "output": out_deque,
                "start_ts": start_ts,
            }
        bg_thread = threading.Thread(target=_bg_reader, daemon=True)
        bg_thread.start()
        logger.info(f"后台命令已启动 [{bg_id}]: {command}")
        return (
            f"已后台启动 [{bg_id}]: {command}\n"
            f"用 read_background_output('{bg_id}') 看输出，"
            f"stop_background_command('{bg_id}') 停止。"
        )

    output_chunks: list[str] = []
    chunks_lock = _thr_local.Lock()
    reader_done = _thr_local.Event()

    def _reader():
        """子线程：从 proc.stdout 读字节，按 utf-8/gbk 解码，行边界 push 到 UI。"""
        try:
            buf = b""
            while True:
                raw = proc.stdout.read(4096)
                if not raw:
                    if buf:
                        text = _decode_chunk(buf)
                        with chunks_lock:
                            output_chunks.append(text)
                        if ui is not None:
                            try:
                                ui.show_message(text, "tool_result")
                            except Exception:
                                pass
                    break
                buf += raw
                # 按 \n 切分，把已经完整的行 flush 出去，剩余半行留在 buf 等下次
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = _decode_chunk(line + b"\n")
                    with chunks_lock:
                        output_chunks.append(text)
                    if ui is not None:
                        try:
                            ui.show_message(text, "tool_result")
                        except Exception:
                            pass
        finally:
            reader_done.set()

    rt = _thr_local.Thread(target=_reader, daemon=True)
    rt.start()

    # 主循环：等进程结束 / 监控 stop_flag / 监控超时
    start = time.time()
    timed_out = False
    interrupted = False
    while True:
        if proc.poll() is not None:
            break
        elapsed = time.time() - start
        if elapsed > effective_timeout:
            timed_out = True
            _kill_proc_tree(proc)
            break
        if getattr(state, "stop_flag", False):
            interrupted = True
            _kill_proc_tree(proc)
            break
        time.sleep(0.05)

    # 让 reader 把剩余 buf flush 完
    reader_done.wait(timeout=2)
    try:
        proc.stdout.close()
    except Exception:
        pass

    if timed_out:
        if ui is not None:
            try:
                ui.show_message(f"\n⏱️ 超时强杀（{effective_timeout}s）\n", "tool_result")
            except Exception:
                pass
        return f"命令执行超时（{effective_timeout} 秒），已强杀进程"
    if interrupted:
        if ui is not None:
            try:
                ui.show_message("\n⏹ 用户中断\n", "tool_result")
            except Exception:
                pass
        return "用户中断执行"

    with chunks_lock:
        output = "".join(output_chunks)

    if not output:
        output = "(无输出)"
    if len(output) > RUN_COMMAND_MAX_OUTPUT_CHARS:
        output = (
            output[:RUN_COMMAND_MAX_OUTPUT_CHARS]
            + f"\n... [输出过长，已截断；UI 上能看到全量约 {len(output)} 字符]"
        )

    # 完成标记一行（让 UI 上能看到"结束了"，不会和上一段输出粘在一起）
    if ui is not None:
        try:
            ui.show_message(f"\n✓ 退出码 {proc.returncode}\n", "tool_result")
        except Exception:
            pass

    return f"退出码: {proc.returncode}\n{output}"


@tool
def search_in_file(path: str, keyword: str, offset: int = 0, limit: int = SEARCH_IN_FILE_DEFAULT_LIMIT) -> str:
    """在单个文件中搜索关键词，返回匹配的行。
    path: 文件路径, keyword: 搜索关键词, offset: 从第几处匹配开始显示（0-based）, limit: 本次最多显示多少处。
    跨文件 / 跨目录搜索请用 `search_files`。"""
    try:
        offset = max(0, int(offset or 0))
        limit = max(1, min(SEARCH_IN_FILE_MAX_LIMIT, int(limit or SEARCH_IN_FILE_DEFAULT_LIMIT)))
        full = _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        # search_in_file 是单文件搜索；传目录会让 open() 在 Windows 上报 Errno 13
        # Permission denied，给个清晰提示引导用 search_files，而不是抛系统错。
        if os.path.isdir(full):
            return f"`{path}` 是目录、不是单个文件。跨目录搜索请用 search_files（正则、自动遍历目录、忽略噪声目录）。"
        if not os.path.isfile(full):
            return f"文件不存在: {path}"
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()
        matches = []
        for i, line in enumerate(lines, 1):
            if keyword.lower() in line.lower():
                matches.append(f"  L{i}: {line.rstrip()}")
        if matches:
            shown = matches[offset:offset + limit]
            if not shown:
                return (
                    f"找到 {len(matches)} 处匹配，但 offset={offset} 已超出范围。"
                    f" 请使用 0 到 {max(0, len(matches) - 1)} 之间的 offset。"
                )
            next_offset = offset + len(shown)
            remaining = max(0, len(matches) - next_offset)
            tail = (
                f"\n... [本次显示 {offset + 1}-{next_offset} / {len(matches)} 处，"
                f"还有 {remaining} 处未列出；继续查看用 offset={next_offset}]"
                if remaining > 0 else
                f"\n[已显示 {offset + 1}-{next_offset} / {len(matches)} 处匹配]"
            )
            return f"找到 {len(matches)} 处匹配:\n" + "\n".join(shown) + tail
        return f"未找到 '{keyword}'"
    except Exception as e:
        return f"搜索失败: {e}"


# search_files 默认跳过的目录（编译产物、依赖、版本控制等噪声）


@tool
def search_files(regex: str, path: str = ".", file_pattern: str = "*", max_results: int = SEARCH_FILES_MAX_RESULTS) -> str:
    """跨文件 / 跨目录用**正则**搜索（ripgrep 风格），返回 `相对路径:行号:内容`。

    用法：
      - `regex`：Python 正则，例如 `def\\s+\\w+\\(` / `TODO|FIXME` / `class \\w+\\(BaseModel\\)`
      - `path`：搜索目录的相对/绝对路径（默认 `.` 即项目根）
      - `file_pattern`：glob 过滤文件名，例如 `*.py` / `*.{ts,tsx}` / `test_*.py`
      - `max_results`：截断阈值（默认 50，超过会提示 "还有 N 处未列出"）

    默认忽略噪声目录：.git / node_modules / __pycache__ / .venv / venv / build / dist 等；
    单文件 > 1MB 自动跳过（避免读到二进制大文件）。
    """
    import re as _re
    import fnmatch

    if not regex:
        return "失败：regex 不能为空"
    try:
        pat = _re.compile(regex)
    except _re.error as e:
        return f"失败：正则不合法 — {e}"

    full = _resolve_path(path) if path else _project_cwd()
    reject = _subagent_path_rejection(full, "目录")
    if reject:
        return reject
    if not os.path.isdir(full):
        return f"失败：目录不存在 {full}"

    # 支持 `*.{ts,tsx}` 这种 brace expansion
    def _expand_braces(pattern):
        m = _re.match(r"^(.*)\{([^{}]+)\}(.*)$", pattern)
        if not m:
            return [pattern]
        prefix, choices, suffix = m.group(1), m.group(2).split(","), m.group(3)
        return [f"{prefix}{c.strip()}{suffix}" for c in choices]

    patterns = _expand_braces(file_pattern)

    matches = []
    total = 0
    truncated = False

    for root, dirs, files in os.walk(full):
        # 原地修改 dirs 跳过忽略目录
        dirs[:] = [d for d in dirs if d not in _SEARCH_IGNORE_DIRS and not d.startswith(".")]
        for fname in files:
            if not any(fnmatch.fnmatch(fname, p) for p in patterns):
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > _SEARCH_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, 1):
                        if pat.search(line):
                            total += 1
                            if len(matches) < max_results:
                                rel = os.path.relpath(fpath, full).replace(os.sep, "/")
                                matches.append(f"{rel}:{ln}:{line.rstrip()}")
                            else:
                                truncated = True
            except (OSError, UnicodeDecodeError):
                continue

    if not matches:
        return f"未在 {full} 找到匹配 /{regex}/ 的内容（file_pattern={file_pattern}）"

    header = f"在 {full} 下找到 {total} 处匹配 /{regex}/（file_pattern={file_pattern}）:\n"
    body = "\n".join(matches)
    if truncated:
        body += f"\n... [仅显示前 {max_results} 处，还有 {total - max_results} 处未列出，请缩小 path 或 file_pattern]"
    return header + body


# ══════════════════════════════════════
# 长期记忆工具
# ══════════════════════════════════════


@tool
def remember(fact: str) -> str:
    """存一条关于用户的长期记忆。

    当用户透露了值得长期记住的个人信息、偏好、项目约定时调用。
    fact 应该是简洁的一句话陈述，不要太长。
    示例：用户说"我习惯用 pytest 测试"，存为 "用户用 pytest 做测试"
    """
    from .memory_store import add_memory
    result = add_memory(fact)
    if result:
        return f"已记住: {result['text']}"
    return "该记忆已存在，无需重复保存"


@tool
def forget(query: str) -> str:
    """按关键词删除长期记忆。

    列出匹配项并删除，返回删除了哪些记忆。
    示例：forget("pytest") 会删除包含 "pytest" 的记忆
    """
    from .memory_store import search_memories, delete_memory
    matches = search_memories(query)
    if not matches:
        return f"未找到包含 '{query}' 的记忆"
    
    deleted = []
    for mem in matches:
        if delete_memory(mem["id"]):
            deleted.append(mem["text"])
    
    if deleted:
        return f"已删除 {len(deleted)} 条记忆:\n" + "\n".join(f"- {t}" for t in deleted)
    return "删除失败，请稍后重试"


@tool
def get_project_instructions(path: str = ".") -> str:
    """读取目标路径适用的 AI 编码规则（CLAUDE.md / AGENTS.md / .lingxirules）。

    path 可指向当前项目内的文件或目录；返回项目根到目标目录的完整规则链。
    不传参数时只读取当前项目根规则。
    """
    from .roles import load_project_rules
    root = _project_cwd()
    target = _resolve_path(path or ".")
    reject = _subagent_path_rejection(target, "目录")
    if reject:
        return reject
    result = load_project_rules(root, target)
    if not result:
        return (
            f"路径 `{path or '.'}` 没有适用的项目规则，"
            "或该路径不在当前项目根目录内。"
        )
    return result


@tool
def notify_user(title: str, message: str, level: str = "info") -> str:
    """主动向用户手机推送 Telegram 通知。

    用于 AI 判断需要提醒用户的场景（如长时间任务的关键节点、需要用户注意的事项）。
    level: info / done / action_needed / error（默认 info）
    """
    from .notify import notify
    ok = notify(level, title, message, f"ai_notify_{level}")
    if ok:
        return f"已推送 Telegram 通知: {title}"
    return "通知未发送（Telegram 未配置或被节流）"


@tool
def spawn_agents(tasks: list[str]) -> str:
    """把多个【相互独立】的子任务派给并行子 Agent，各自在隔离 worktree 改代码，跑完自动
    合并回主项目并汇总。仅用于真正独立、可并行的多任务（分别改不相干模块）；有依赖/会改
    同一文件的别用（那是 update_plan 顺序做的事）。"""
    from . import session as _session
    cur = _session.current_session()
    if getattr(cur, "is_subagent", False):
        return "子 Agent 不能再派生子 Agent。"
    if getattr(cur, "worktree", None):
        return ("隔离模式下暂不支持并行子 Agent：子 Agent 基于主项目 HEAD 改动并合并回主项目，"
                "会绕过当前隔离。请先退出隔离模式，或顺序执行这些子任务。")
    if not isinstance(tasks, list) or not [t for t in tasks if str(t).strip()]:
        return "请提供非空 tasks 列表，每项是一个独立子任务。"

    project_root = _session.current_project() or getattr(state, "current_project", None)
    if not project_root or not os.path.isdir(project_root):
        return "无法确定主项目根目录，不能派生并行子 Agent。"

    from . import subagent
    results = subagent.spawn(tasks, project_root, getattr(state, "ui_ref", None))
    lines = ["并行子 Agent 汇总："]
    for idx, r in enumerate(results, 1):
        files = ", ".join(r.get("files_changed") or []) or "无"
        lines.append(
            f"\n[{idx}] merge={r.get('merge', '?')}\n"
            f"任务：{r.get('task', '')}\n"
            f"文件：{files}\n"
            f"摘要：{r.get('summary', '') or '（无）'}\n"
            f"详情：{r.get('detail', '') or '（无）'}"
        )
    return "\n".join(lines)


def _auto_advance_plan(plan: list) -> None:
    """兜底高亮"当前这一步"：若没有任何步骤在 in_progress 且还有待办，把第一个待办自动
    提升为 in_progress（原地改）。模型常只标 done、跳过 in_progress，导致计划面板永远看不到
    "进行中"高亮、看着像静态清单。这里在单步更新后兜底，保证执行期间总有一步高亮，对齐设计图。
    全部完成（无待办）时不提升——面板如实显示 N/N 完成。模型显式设了某步 in_progress 时也不动。"""
    if any(it.get("status") == "in_progress" for it in plan):
        return
    for it in plan:
        if it.get("status") == "pending":
            it["status"] = "in_progress"
            break


@tool
def update_plan(plan: str) -> str:
    """创建 / 重列当前任务的执行计划（待办清单，**整份覆盖**）。

    任务需要 3 步以上、或要改多个文件时，动手前先调一次列出全部步骤。
    **之后推进进度用 `set_step_status(步号, 状态)` 改单步，不要反复重发整份计划。**
    仅当要大改结构（增删步骤、重排）时才再调本工具重列。

    plan: 多行文本，每行一个步骤，行首标记：[ ] 未开始  [~] 进行中  [x] 已完成
    """
    from . import state
    # 历史压缩摘要曾被模型续接进 plan 参数；摘要不是计划的一部分，硬截断防污染。
    clean_plan = re.split(
        r"\s*\[(?:历史摘要|之前已有摘要|新增对话)\]\s*:?",
        plan or "",
        maxsplit=1,
    )[0]
    items = state.parse_plan(clean_plan)
    if plan and not items:
        return "计划未更新：没有检测到合法 checklist 行，请用 [ ] / [~] / [x] 标记。"
    state.current_plan = items          # 整份替换——不再模糊合并(churn 根源)、不再拒绝
    _ui = getattr(state, "ui_ref", None)
    if _ui is not None and hasattr(_ui, "show_plan"):
        try:
            _ui.show_plan(list(items))
        except Exception:
            pass
    if not items:
        return "计划已清空。"
    done = sum(1 for it in items if it["status"] == "done")
    return f"计划已更新（{done}/{len(items)} 完成）：\n" + state.render_plan(items)


# 模型传的状态词 → 内部状态。容忍中英 / checkbox 字符各种写法。
_STEP_STATUS_ALIASES = {
    "done": "done", "完成": "done", "已完成": "done", "完": "done", "x": "done", "✓": "done", "√": "done",
    "in_progress": "in_progress", "进行中": "in_progress", "正在做": "in_progress", "doing": "in_progress",
    "~": "in_progress", "-": "in_progress",
    "pending": "pending", "待办": "pending", "未开始": "pending", "todo": "pending", "": "pending", " ": "pending",
}


def _normalize_step_status(status: str):
    """状态词 → 'done'/'in_progress'/'pending'；不认识返回 None。"""
    return _STEP_STATUS_ALIASES.get(str(status).strip().lower())


@tool
def set_step_status(step: int, status: str) -> str:
    """更新计划中【某一步】的状态（增量更新，**不用重发整份计划**）。

    step: 步号（1 基，就是计划面板上看到的第几行）。
    status: 完成 / 进行中 / 待办（也接受 done / in_progress / pending / x / ~）。
    用法：开头用 update_plan 列全计划，之后每开始或完成一步就调本工具改那一步。
    """
    from . import state
    plan = list(getattr(state, "current_plan", None) or [])
    if not plan:
        return "还没有计划。请先用 update_plan 列出完整步骤。"
    try:
        idx = int(step)
    except (TypeError, ValueError):
        return f"步号无效：{step}。请传 1 到 {len(plan)} 之间的整数。"
    if idx < 1 or idx > len(plan):
        return f"步号 {idx} 超出范围（当前计划共 {len(plan)} 步）。"
    s = _normalize_step_status(status)
    if s is None:
        return f"状态无效：{status}。请用 完成 / 进行中 / 待办（或 done / in_progress / pending）。"
    plan[idx - 1] = {"text": plan[idx - 1].get("text", ""), "status": s}
    _auto_advance_plan(plan)   # 标完 done 后，自动把下一个待办提为进行中（保证面板始终高亮当前步）
    state.current_plan = plan
    _ui = getattr(state, "ui_ref", None)
    if _ui is not None and hasattr(_ui, "show_plan"):
        try:
            _ui.show_plan(list(plan))
        except Exception:
            pass
    done = sum(1 for it in plan if it["status"] == "done")
    return f"已把第 {idx} 步标为「{s}」（{done}/{len(plan)} 完成）：\n" + state.render_plan(plan)


# ══════════════════════════════════════
# 后台命令管理工具
# ══════════════════════════════════════


@tool
def read_background_output(bg_id: str, tail: int = 50) -> str:
    """读后台命令的累积输出（最后 tail 行）。tail<=0 看全部缓冲。"""
    with _bg_lock:
        info = _bg_procs.get(bg_id)
    if info is None:
        return f"未找到后台命令 '{bg_id}'。可用 list_background_commands() 查看所有。"

    proc = info["proc"]
    with _bg_lock:
        lines = list(info["output"])
    total = len(lines)  # 锁内快照长度；下面不再锁外迭代 deque（reader 并发 append 会 RuntimeError）

    status = "运行中" if proc.poll() is None else f"已退出(码 {proc.returncode})"
    elapsed = int(time.time() - info["start_ts"])

    if tail > 0 and total > tail:
        lines = lines[-tail:]
        truncated_hint = f"\n... (仅显示最后 {tail} 行，共缓冲 {total} 段)"
    else:
        truncated_hint = ""

    return (
        f"[{bg_id}] {status} | {elapsed}s | {info['command']}\n"
        + "".join(lines) + truncated_hint
    )


@tool
def list_background_commands() -> str:
    """列出所有后台命令：bg_id / 命令 / 运行中或已退出 / 启动多久。"""
    with _bg_lock:
        if not _bg_procs:
            return "没有后台命令在运行。"
        rows = []
        for bg_id, info in _bg_procs.items():
            proc = info["proc"]
            status = "运行中" if proc.poll() is None else f"已退出(码 {proc.returncode})"
            elapsed = int(time.time() - info["start_ts"])
            rows.append(f"  [{bg_id}] {status} | {elapsed}s | {info['command']}")
    return "后台命令列表:\n" + "\n".join(rows)


@tool
def stop_background_command(bg_id: str) -> str:
    """停止一个后台命令（taskkill 杀进程树），并从注册表移除。"""
    with _bg_lock:
        info = _bg_procs.pop(bg_id, None)
    if info is None:
        return f"未找到后台命令 '{bg_id}'。"

    proc = info["proc"]
    _kill_proc_tree(proc)
    elapsed = int(time.time() - info["start_ts"])
    logger.info(f"已停止后台命令 [{bg_id}]: {info['command']}（运行 {elapsed}s）")
    return f"已停止 [{bg_id}]: {info['command']}（运行 {elapsed}s）"


def stop_all_background():
    """停止所有后台命令（应用退出时调用）。"""
    with _bg_lock:
        procs = list(_bg_procs.items())
        _bg_procs.clear()
    for bg_id, info in procs:
        try:
            _kill_proc_tree(info["proc"])
            logger.info(f"退出清理：停止 [{bg_id}] {info['command']}")
        except Exception:
            pass


def _locate_symbol(src: str, name: str):
    """在源码里找符号 name 第一次以独立标识符出现的 (line 1-based, col 0-based)；没有→(None, None)。"""
    import re
    m = re.search(rf"\b{re.escape(name)}\b", src)
    if not m:
        return None, None
    line = src.count("\n", 0, m.start()) + 1
    col = m.start() - (src.rfind("\n", 0, m.start()) + 1)
    return line, col


def _locate_all_symbols(src: str, name: str):
    """源码里 name 作为独立标识符出现的所有 (line 1-based, col 0-based)，按出现顺序。
    用于在指定文件内逐个位置尝试解析——首处可能落在注释/字符串里 jedi 解析不到。"""
    import re
    out = []
    for m in re.finditer(rf"\b{re.escape(name)}\b", src):
        line = src.count("\n", 0, m.start()) + 1
        col = m.start() - (src.rfind("\n", 0, m.start()) + 1)
        out.append((line, col))
    return out


def _fmt_jedi_name(n, root):
    """把 jedi Name 格式化成一行：相对路径:行  [类型]  描述。"""
    mp = str(n.module_path) if n.module_path else "?"
    try:
        mp = os.path.relpath(mp, root)
    except Exception:
        pass
    return f"  {mp}:{n.line}  [{n.type}]  {(n.description or '').strip()[:80]}"


def _pick_symbol_pos(src: str, name: str, line: int):
    """在源码里定位符号 name 作为独立标识符的 (行 1-based, 列 0-based)。

    line>0 时优先取该行上的出现；该行没有则退用文件里第一处出现。都没有 → (0, 0)。
    给 LSP 传准确的列，避免旧版 col=0 去查错位置（如行首是别的符号）返回误命中。
    """
    positions = _locate_all_symbols(src, name) if src else []
    if line:
        for ln, lc in positions:
            if ln == line:
                return ln, lc
    if positions:
        return positions[0]
    return 0, 0


@tool
def find_definition(name: str, path: str = "", line: int = 0) -> str:
    """跳到符号（函数/类/变量）的定义位置。比 search_files 正则准——LSP / jedi 懂作用域/import/继承。
    name: 符号名，如 "agent_loop" / "Session"。
    path: 指定某文件（相对项目根）里该符号的引用点去跟踪定义；留空则在整个项目搜该符号的定义。
    line: 符号所在行号（1-based，配合 path 用，提高 LSP 精度；留空则自动扫描文件找 name）。
    LSP 服务器未安装时静默降级到 jedi；jedi 也未装则给出安装提示。"""
    proj_dir = _project_cwd()

    # ── 优先尝试 LSP ──
    _resolved = None
    if path:
        _resolved = _resolve_path(path)
        reject = _subagent_path_rejection(_resolved, "文件")
        if reject:
            return reject
        try:
            with open(_resolved, encoding="utf-8") as _f:
                src = _f.read()
            file_ok = True
        except Exception:
            src = ""
            file_ok = False
        target_line, col = _pick_symbol_pos(src, name, line)
        # 文件能读但符号根本不在里面 → 明确提示（别拿 col=0 去 LSP 瞎查）；
        # 文件读不到（不存在）则不在此拦，落到 jedi 给"读取失败"/降级提示。
        if file_ok and not target_line:
            return (f"在 {path} 里未找到符号 `{name}`；"
                    f"留空 path 可在整个项目搜该符号的定义。")
        if target_line:
            try:
                from . import lsp_client
                resp = lsp_client.definition(name, _resolved, target_line, col)
                if resp:
                    return f"🔎 {name} 的定义（LSP）：\n" + "\n".join(
                        f"  • {loc['file']}:{loc['line']}" for loc in resp)
            except Exception:
                pass  # LSP 不可用，继续降级

    # ── 降级到 jedi ──
    try:
        import jedi
    except ImportError:
        return ("未安装 jedi（pip install jedi，或装 pyright/pylsp 启用精准代码导航）；"
                "可改用 search_files 正则搜定义。")
    root = proj_dir
    proj = jedi.Project(root)
    names = []
    if path:
        full = _resolved if _resolved else _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        try:
            with open(full, encoding="utf-8") as f:
                src = f.read()
        except Exception as e:
            return f"读取 {path} 失败: {e}"
        script = jedi.Script(code=src, path=full, project=proj)
        for ln, col in _locate_all_symbols(src, name):
            names = script.goto(ln, col, follow_imports=True)
            if names:
                break
        if not names:
            return (f"在 {path} 里没找到 `{name}` 的可解析定义"
                    f"（可能只是注释/字符串里提到它，没有真实绑定）；"
                    f"留空 path 可在整个项目搜该符号定义。")
    else:
        names = list(proj.search(name))
        if not names:
            return f"没找到 `{name}` 的定义。"
    return f"`{name}` 的定义（jedi）：\n" + "\n".join(_fmt_jedi_name(n, root) for n in names[:10])


@tool
def find_references(name: str, path: str = "", line: int = 0) -> str:
    """找符号的所有引用/调用处（谁用了它）。比 search_files 准——按真实绑定找、不误匹配同名。
    name: 符号名。path: 该符号出现的文件（相对项目根）；留空则先在项目里定位其定义再找引用。
    line: 符号所在行号（1-based，配合 path 用，提高 LSP 精度；留空则自动扫描文件找 name）。
    LSP 服务器未安装时静默降级到 jedi；jedi 也未装则给出安装提示。"""
    proj_dir = _project_cwd()

    # ── 优先尝试 LSP ──
    _resolved = None
    if path:
        _resolved = _resolve_path(path)
        reject = _subagent_path_rejection(_resolved, "文件")
        if reject:
            return reject
        try:
            with open(_resolved, encoding="utf-8") as _f:
                src = _f.read()
            file_ok = True
        except Exception:
            src = ""
            file_ok = False
        target_line, col = _pick_symbol_pos(src, name, line)
        if file_ok and not target_line:
            return (f"在 {path} 里未找到符号 `{name}`；"
                    f"留空 path 可先在项目里定位定义再找引用。")
        if target_line:
            try:
                from . import lsp_client
                resp = lsp_client.references(name, _resolved, target_line, col)
                if resp:
                    return f"🔗 {name} 的引用（LSP，{len(resp)} 处）：\n" + "\n".join(
                        f"  • {loc['file']}:{loc['line']}" for loc in resp)
            except Exception:
                pass  # LSP 不可用，继续降级

    # ── 降级到 jedi ──
    try:
        import jedi
    except ImportError:
        return ("未安装 jedi（pip install jedi，或装 pyright/pylsp 启用精准代码导航）；"
                "可改用 search_files 正则搜引用。")
    root = proj_dir
    proj = jedi.Project(root)
    refs = []
    if path:
        full = _resolved if _resolved else _resolve_path(path)
        reject = _subagent_path_rejection(full, "文件")
        if reject:
            return reject
        try:
            with open(full, encoding="utf-8") as f:
                src = f.read()
        except Exception as e:
            return f"读取 {path} 失败: {e}"
        script = jedi.Script(code=src, path=full, project=proj)
        for ln, col in _locate_all_symbols(src, name):
            refs = script.get_references(ln, col, include_builtins=False)
            if refs:
                break
        if not refs:
            return (f"在 {path} 里没找到 `{name}` 的可解析引用"
                    f"（可能只是注释/字符串里提到它）；"
                    f"留空 path 可先在全项目定位定义再找引用。")
    else:
        defs = list(proj.search(name))
        if not defs:
            return f"项目里没找到符号 `{name}`，无引用可查。"
        d = defs[0]
        refs = jedi.Script(path=str(d.module_path), project=proj).get_references(
            d.line, d.column, include_builtins=False)
    if not refs:
        return f"没找到 `{name}` 的引用。"
    return f"`{name}` 的引用（jedi，{len(refs)} 处）：\n" + "\n".join(_fmt_jedi_name(n, root) for n in refs[:50])


# ══════════════════════════════════════
# 测试运行工具
# ══════════════════════════════════════


def _parse_pytest_output(stdout: str, elapsed: float = 0.0) -> str:
    """解析 pytest stdout，提取计数 + 失败用例摘要 + 错误详情；
    解析不到则退回末尾 ~2000 字。
    失败时在末尾追加 [REPAIR_INFO] 结构化块，供自动修复循环使用。"""
    lines = stdout.strip().splitlines()

    # ── 计数：从末尾 summary 行抓 passed / failed / error ──
    passed = failed = errors = 0
    m_passed = re.search(r'(\d+)\s+passed', stdout)
    m_failed = re.search(r'(\d+)\s+failed', stdout)
    m_error  = re.search(r'(\d+)\s+error', stdout)
    if m_passed:
        passed = int(m_passed.group(1))
    if m_failed:
        failed = int(m_failed.group(1))
    if m_error:
        errors = int(m_error.group(1))

    has_counts = bool(m_passed or m_failed or m_error)

    # ── 失败用例行：pytest -q 末尾 "FAILED path::test — ErrorType: msg" ──
    failed_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failed_lines.insert(0, stripped)
        elif failed_lines:
            # 遇到非 FAILED 行就停（FAILED 块通常是连续的）
            break

    # ── 提取每个失败用例的短 traceback（--tb=short 输出段） ──
    # pytest --tb=short 格式：
    #   FAILED tests/test_foo.py::test_bar - AssertionError: ...
    #   ====== short test summary =======
    #   或在 stdout 中间有 "____ test_bar ____" 分隔段 + traceback
    error_details: dict[str, str] = {}  # test_id -> 最后几行错误信息
    _tb_section_re = re.compile(r'^_{2,}\s+(.+?)\s+_{2,}$')  # ______ test_bar ______
    _file_line_re = re.compile(r'^\s+(.+\.py):(\d+):\s+in\s+')  # 文件:行号: in func
    _assertion_re = re.compile(r'^[Ee]\s+(.+)$')  # E   AssertionError: ...

    i = 0
    while i < len(lines):
        # 匹配 traceback 段的标题行 "____ test_name ____"
        m = _tb_section_re.match(lines[i])
        if m:
            test_name = m.group(1)
            # 往后扫描几行，找文件:行号 和 E 行
            tb_lines: list[str] = []
            j = i + 1
            while j < len(lines) and j < i + 30:  # 限制扫描范围
                line = lines[j]
                # 遇到下一个段标题或短 summary 行停止
                if _tb_section_re.match(line) and j > i + 1:
                    break
                if line.strip().startswith("FAILED ") or line.strip().startswith("===="):
                    break
                fm = _file_line_re.match(line)
                if fm:
                    tb_lines.append(f"  File {fm.group(1)}:{fm.group(2)}")
                am = _assertion_re.match(line)
                if am:
                    tb_lines.append(f"  {am.group(1)}")
                j += 1
            if tb_lines:
                error_details[test_name] = "\n".join(tb_lines[-4:])  # 最多 4 行
            i = j
            continue
        i += 1

    # ── 无法解析：退回 stdout 末尾 ~2000 字 ──
    if not has_counts and not failed_lines:
        tail = stdout[-2000:] if len(stdout) > 2000 else stdout
        return f"（pytest 输出解析未命中计数/失败行，以下为原始输出尾部）\n{tail}"

    # ── 拼精炼摘要 ──
    time_str = f"（{elapsed:.2f}s）" if elapsed > 0 else ""
    parts = []
    if failed or errors:
        parts.append(f"❌ {failed + errors} failed / {passed} passed{time_str}")
    else:
        parts.append(f"✅ {passed} passed{time_str}，全部通过")

    if failed_lines:
        parts.append("失败用例：")
        for fl in failed_lines[:20]:  # 最多列 20 条
            # "FAILED path::test — ErrorType: msg" 原样展示
            test_info = fl[len("FAILED "):]
            parts.append(f"  - {test_info}")
            # 尝试匹配 traceback 详情
            # test_info 格式："path::test_name — ErrorType: msg"
            test_id = test_info.split(" — ")[0].strip() if " — " in test_info else test_info.strip()
            # 按 test_id 或 test_id 的最后部分（::test_name）匹配
            for key, detail in error_details.items():
                if key == test_id or test_id.endswith(f"::{key}") or key in test_id:
                    parts.append(detail)
                    break
        if len(failed_lines) > 20:
            parts.append(f"  ...（共 {len(failed_lines)} 个失败用例，仅列前 20）")

    # ── [REPAIR_INFO] 结构化块（供自动修复循环解析） ──
    if failed or errors:
        repair_info: list[str] = []
        repair_info.append(f"status=failed|tests={failed + errors}|passed={passed}|errors={errors}")
        for fl in failed_lines[:5]:  # 最多报 5 个
            test_info = fl[len("FAILED "):]
            repair_info.append(f"test: {test_info}")
        parts.append("\n[REPAIR_INFO]")
        parts.append("\n".join(repair_info))
        parts.append("[/REPAIR_INFO]")
        parts.append("\n（用 read_file 打开对应文件定位修复）")

    return "\n".join(parts)


def _resolve_python():
    """挑一个真 Python 解释器跑 pytest（不是裸 sys.executable）：
    ① 项目内 venv（.venv/venv/env）——最贴合被测项目的依赖
    ② 开发期（非 frozen）用 sys.executable（应用自己的 Python，跟项目同环境时正好）
    ③ 系统 PATH 上的 python/python3
    打包(frozen)后 sys.executable=灵犀.exe、`-m pytest` 跑不了，故 frozen 下跳过 ②。"""
    root = _project_cwd()
    bindir = "Scripts" if os.name == "nt" else "bin"
    pyname = "python.exe" if os.name == "nt" else "python"
    for venv in (".venv", "venv", "env"):
        cand = os.path.join(root, venv, bindir, pyname)
        if os.path.isfile(cand):
            return cand
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python") or shutil.which("python3") or sys.executable


@tool
def run_tests(path: str = "", k: str = "", timeout: int = 300) -> str:
    """跑 pytest 测试，返回精炼结果：通过/失败数 + 每个失败用例的位置和错误摘要。
    path: 测试路径/文件（相对项目根，空 = pytest 自动发现）。k: pytest -k 过滤表达式。
    比 run_command 跑 pytest 省事——直接给你哪些挂了、错在哪，便于定位修复。"""
    # ── 构建命令：<解释器> -m pytest（frozen 下 sys.executable=exe 不能用，见 _resolve_python） ──
    cmd = [_resolve_python(), "-m", "pytest", "--tb=short", "-q"]
    run_cwd = _shell_cwd()
    reject = _subagent_command_rejection("pytest", run_cwd)
    if reject:
        return reject

    # ── path 安全校验：_resolve_path + commonpath 防逃逸（同 code_map） ──
    if path:
        resolved = _resolve_path(path)
        root = _project_cwd()
        try:
            if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        except ValueError:
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        cmd.append(resolved)

    if k:
        cmd.extend(["-k", k])

    # ── 执行 ──
    try:
        t0 = time.time()
        result = subprocess.run(
            cmd, cwd=run_cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        elapsed = time.time() - t0
    except FileNotFoundError:
        with contextlib.suppress(Exception):
            _v_mark_tests(_session.get_verification(), None, "pytest 未安装或找不到")
        return "pytest 未安装或找不到，请先运行 `pip install pytest` 安装。"
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            _v_mark_tests(_session.get_verification(), None, f"测试超时（>{timeout}s）")
        return f"测试超时（>{timeout}s），可能有用例卡住，请检查或增大 timeout。"
    except Exception as e:
        with contextlib.suppress(Exception):
            _v_mark_tests(_session.get_verification(), None, f"运行 pytest 失败: {e}")
        return f"运行 pytest 失败: {e}"

    # ── 解析输出 ──
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # pytest 没装时 stderr 里会有 "No module named pytest"
    if stderr and "No module named pytest" in stderr:
        with contextlib.suppress(Exception):
            _v_mark_tests(_session.get_verification(), None, "pytest 未安装")
        return "pytest 未安装，请先运行 `pip install pytest` 安装。"

    summary = _parse_pytest_output(stdout, elapsed)

    # pytest 的 warning/提示信息单独附加（有的话挺有用）
    warning_lines = []
    for line in (stderr or "").strip().splitlines():
        if "warning" in line.lower() or "Warning" in line:
            warning_lines.append(line)
    if warning_lines and len("\n".join(warning_lines)) < 500:
        summary += "\n\n⚠️ pytest warnings:\n" + "\n".join(warning_lines[:10])

    # ── 输出截断防爆 ──
    if len(summary) > 6000:
        summary = summary[:6000] + "\n... [输出已截断，超过 6000 字]"

    # ── 标记验证状态：测试已执行 ──
    with contextlib.suppress(Exception):
        _v_mark_tests(_session.get_verification(), result.returncode == 0)

    return summary


# ══════════════════════════════════════
# 自我校验闭环：静态检查（lint/语法），编辑后自动回灌错误给模型自修
# ══════════════════════════════════════


def _bundled_ruff():
    """打包随 app 发的 ruff 可执行文件（onefile 在 _MEIPASS、onedir 在 exe 旁）。
    见 lingxi.spec 的 _ruff_datas。没有返回 None。"""
    name = "ruff.exe" if os.name == "nt" else "ruff"
    bases = [getattr(sys, "_MEIPASS", None)]
    if getattr(sys, "frozen", False):
        bases.append(os.path.dirname(sys.executable))
    for base in bases:
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return None


# 类型检查只保留这些高信号错误码：都是"用错了 API / 参数 / 名字"，几乎不误报。
# 刻意排除 attr-defined / union-attr / var-annotated / index 等——它们在动态代码
# （state 代理、langchain 动态属性等）上会狂误报，否则模型会去追一堆不存在的问题。
_TYPE_ERROR_CODES = ("call-arg", "call-overload", "name-defined",
                     "arg-type", "return-value", "valid-type")


def _run_code_check(full_path: str):
    """对单个文件跑静态检查。返回 (issues, checker)：
    - checker=None → 没有可用检查器（不支持的语言且没配 check_command）
    - issues=""    → 检查通过、无问题
    - issues=非空  → 问题文本（file:line: 说明）
    Python：ruff（F/E9 正确性+语法）+ mypy 类型检查（高信号错误码，抓臆造 API/参数错），
    两者结果合并。没装 ruff 退化到 py_compile；没装 mypy 则跳过类型检查。
    其它语言走 config 的 check_command。"""
    from .config import CHECK_COMMAND
    ext = os.path.splitext(full_path)[1].lower()
    cwd = _shell_cwd()

    # 其它语言：用户自定义命令（shell 执行，{file} 占位）
    if CHECK_COMMAND:
        return _run_check_subprocess(CHECK_COMMAND.replace("{file}", full_path), cwd, True, "check_command")

    if ext not in (".py", ".pyi"):
        return None, None

    ruff_issues, ruff_checker = _run_ruff_check(full_path, cwd)
    type_issues, type_checker = _run_type_check(full_path, cwd)

    parts = [p for p in (ruff_issues, type_issues) if p]
    checkers = [c for c in (ruff_checker, type_checker) if c]
    return "\n".join(parts), ("+".join(checkers) if checkers else None)


def _run_ruff_check(full_path: str, cwd):
    """ruff（F/E9）单文件检查，返回 (issues, checker)。
    打包后 sys.executable=exe，-m ruff 跑不了，所以 frozen 下只认独立二进制（ruff 自包含 exe）；
    开发期才用 sys.executable -m ruff（不看 PATH 最稳）。都没有 → 内置 compile() 查语法。"""
    import importlib.util
    frozen = getattr(sys, "frozen", False)
    bundled = _bundled_ruff()
    ruff_cmd = None
    if bundled:
        ruff_cmd = [bundled, "check", "--select", "F,E9", full_path]
    elif not frozen and importlib.util.find_spec("ruff") is not None:
        ruff_cmd = [sys.executable, "-m", "ruff", "check", "--select", "F,E9", full_path]
    elif shutil.which("ruff"):
        ruff_cmd = [shutil.which("ruff"), "check", "--select", "F,E9", full_path]
    if ruff_cmd:
        return _run_check_subprocess(ruff_cmd, cwd, False, "ruff")
    return _py_syntax_check(full_path), "py_compile"


def _run_type_check(full_path: str, cwd):
    """mypy 单文件类型检查，只保留 _TYPE_ERROR_CODES 里的高信号错误码。
    返回 (issues, "mypy")；无命中 → ("", "mypy")；没装 mypy / 开关关 → (None, None) 静默跳过。"""
    from .config import TYPE_CHECK_AFTER_EDIT
    if not TYPE_CHECK_AFTER_EDIT:
        return None, None
    import importlib.util
    frozen = getattr(sys, "frozen", False)
    base = None
    if not frozen and importlib.util.find_spec("mypy") is not None:
        base = [sys.executable, "-m", "mypy"]
    elif shutil.which("mypy"):
        base = [shutil.which("mypy")]
    if not base:
        return None, None     # 没装 mypy：跳过类型检查，不影响 ruff 结果
    cmd = base + [
        "--check-untyped-defs", "--ignore-missing-imports", "--follow-imports=silent",
        "--no-error-summary", "--no-color-output", "--show-error-codes",
        "--hide-error-context", "--no-pretty", full_path,
    ]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
    except Exception:
        return None, None
    out = (r.stdout or "") + (r.stderr or "")
    base_name = os.path.basename(full_path)
    kept = []
    for line in out.splitlines():
        if " error:" not in line:
            continue
        if not any(f"[{c}]" in line for c in _TYPE_ERROR_CODES):
            continue
        kept.append(line.replace(full_path, base_name).strip())
    if not kept:
        return "", "mypy"
    return "\n".join(kept[:15]), "mypy"


def _run_check_subprocess(cmd, cwd, use_shell, checker):
    """跑一个检查命令，返回 (issues, checker)。退出码 0 = 通过("")；非 0 = 问题文本。"""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, shell=use_shell, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except Exception:
        return None, None
    if r.returncode == 0:
        return "", checker
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
    if len(out) > 2000:
        out = out[:2000] + "\n... [检查输出已截断]"
    return out or f"{checker} 返回非零退出码（无输出）", checker


def _py_syntax_check(full_path):
    """用内置 compile() 在进程内查 Python 语法错（不起子进程，打包后也能用）。
    通过返回 ""；语法错返回 "文件:行: SyntaxError: ..."；读不了文件返回 ""（不打扰）。"""
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception:
        return ""
    try:
        compile(src, full_path, "exec")
        return ""
    except SyntaxError as e:
        return f"{os.path.basename(full_path)}:{e.lineno or '?'}: SyntaxError: {e.msg}"
    except Exception as e:
        return f"{os.path.basename(full_path)}: 语法检查失败: {e}"


def _auto_check_suffix(full_path: str) -> str:
    """编辑/写入成功后自动校验，返回追加到工具结果的提示串。
    无问题 / 不支持的语言 / 开关关闭 → 返回 ""（不打扰）。"""
    from .config import AUTO_CHECK_AFTER_EDIT
    if not AUTO_CHECK_AFTER_EDIT:
        return ""
    try:
        issues, checker = _run_code_check(full_path)
    except Exception:
        return ""
    if checker:
        _mark_current_check(full_path, not bool(issues), checker)
    elif os.path.splitext(full_path)[1].lower() in {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".kts", ".vue", ".svelte",
    }:
        _mark_current_check(full_path, None, "")
    if not checker or not issues:
        return ""
    return f"\n\n⚠️ 自动校验（{checker}）发现问题，请修复后再继续：\n{issues}"


def _parse_patch(content: str):
    """解析 patch 字符串，返回 (operations, errors)。

    每个 operation 是 dict:
      {"action": "add"|"update"|"delete", "path": str, "content": str,
       "hunks": [{"hint": str, "lines": [str]}] (update only),
       "new_lines": [str] (add only)}
    errors 是 list[str]。
    """
    raw_lines = content.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines = raw_lines[:-1]

    if not any(ln.startswith("*** Begin Patch") for ln in raw_lines):
        return [], ["缺少 *** Begin Patch 标记"]

    operations = []
    errors = []
    current_op = None

    for line in raw_lines:
        sline = line.strip()

        # 整体开始/结束标记（忽略）
        if sline == "*** Begin Patch":
            continue
        if sline == "*** End Patch":
            break

        if sline.startswith("*** Update File:"):
            path = sline[len("*** Update File:"):].strip()
            current_op = {"action": "update", "path": path, "hunks": []}
            operations.append(current_op)
        elif sline.startswith("*** Add File:"):
            path = sline[len("*** Add File:"):].strip()
            current_op = {"action": "add", "path": path, "new_lines": []}
            operations.append(current_op)
        elif sline.startswith("*** Delete File:"):
            path = sline[len("*** Delete File:"):].strip()
            current_op = {"action": "delete", "path": path}
            operations.append(current_op)
        elif sline.startswith("***"):
            errors.append(f"无法识别的文件操作: {sline}")
        elif current_op is not None and current_op["action"] == "update":
            if line.startswith("@@"):
                hint = line[2:].strip()
                current_op["hunks"].append({"hint": hint, "lines": []})
            elif current_op["hunks"]:
                current_op["hunks"][-1]["lines"].append(line)
            else:
                if line and not line.startswith(" "):
                    errors.append(f"在 hunk 头 (@@) 之前遇到非上下文行: {line}")
                current_op.setdefault("_preamble", []).append(line)
        elif current_op is not None and current_op["action"] == "add":
            if line.startswith("+"):
                current_op["new_lines"].append(line[1:])
            elif not line.strip():
                pass
            else:
                current_op["new_lines"].append(line)

    return operations, errors


@tool
def apply_patch(patch: str) -> str:
    """批量文件补丁工具：在一个原子操作中创建、修改、删除多个文件。

    Patch 格式（类似 git diff，但靠上下文定位、不用行号）：

    *** Begin Patch
    *** Update File: src/utils.py
    @@
     def greet(name):
    -    print("hi")
    +    print(f"hi {name}")
    *** Add File: src/bar.py
    +def baz():
    +    return 1
    *** Delete File: src/old.py
    *** End Patch

    规则（务必照做，否则文件内容会错）：
    - 每个文件块以 *** Update File / Add File / Delete File: <相对路径> 开头
    - Update 用 @@ 起一个 hunk；行首第一个字符是标记：空格=上下文、- =删除、+ =新增
    - **标记后【紧跟】内容，标记和内容之间不要再加空格**：写 `+def x():` / `+    return 1`，
      别写 `+ def x():`——那个空格会变成文件内容，导致缩进 / 语法错。缩进是内容自身的缩进。
    - 上下文行写文件里【真实存在且连续】的行，**不能跳过中间的空行或其它行**
      （定位靠精确匹配，不够精确会判失败、让你补全上下文重试，绝不模糊猜测）
    - Add File：每行都是 +<内容>；目标已存在 → 失败
    - Delete File：无 hunk；目标不存在 → 失败
    - 路径不能用 ../ 逃出项目；任何 hunk 定位失败或路径非法 → 整个 patch 中止、不改任何文件
    - 改完自动跑代码检查（lint/语法），有问题会一并提示
    """
    # ── Phase 1: 解析 ──
    operations, parse_errors = _parse_patch(patch)
    if parse_errors:
        return "Patch 格式错误:\n" + "\n".join(f"  - {e}" for e in parse_errors)

    if not operations:
        return "Patch 为空（没有文件操作）。"

    # ── Phase 2: 校验 + 内存计算（不写盘）──
    root = _project_cwd()
    resolved_ops: list = []  # (action, path, resolved, old/None, new/None) 异质 tuple,给 list 注解防 mypy 窄推断
    errors = []

    for op in operations:
        action = op["action"]
        path = op["path"]
        if not path:
            errors.append(f"路径为空（{action} 操作）")
            continue

        resolved = _resolve_path(path)
        try:
            if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
                errors.append(f"路径超出项目范围，不允许: {path}")
                continue
        except ValueError:
            errors.append(f"路径超出项目范围，不允许: {path}")
            continue

        if action == "update":
            if not os.path.isfile(resolved):
                errors.append(f"文件不存在，无法更新: {path}")
                continue
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()

            hunk_failures = []
            for i, hunk in enumerate(op["hunks"]):
                # hunk → old_block(上下文+删除行) / new_block(上下文+新增行)，复用 edit_file 的
                # _locate_edit 做【连续块】匹配 + 缩进对齐（不自己造"允许间隙"的匹配器——那会把
                # 上下文行锚到文件里不相干的散落位置、产出垃圾编辑）。
                old_lines, new_lines = [], []
                for line in hunk["lines"]:
                    if line.startswith("-"):
                        old_lines.append(line[1:])
                    elif line.startswith("+"):
                        new_lines.append(line[1:])
                    else:
                        c = line[1:] if line.startswith(" ") else line
                        old_lines.append(c)
                        new_lines.append(c)

                if not old_lines:
                    hunk_failures.append(f"Hunk {i+1}: 无上下文/删除行，无法定位（纯新增请带上下文）")
                    continue

                old_block = "\n".join(old_lines)
                new_block = "\n".join(new_lines)
                status, spans, new_texts, info = _locate_edit(content, old_block, new_block, False)
                # 多文件原子补丁不做模糊猜测：只接受精确 / 规范化(去行尾空白 + 缩进重对齐)匹配，
                # 且必须唯一命中。none/fuzzy/multi 一律判失败——让模型补全连续上下文重试，
                # 绝不在原子补丁里靠相似度猜位置（会 silent 改错地方）。
                if status not in ("exact", "normalized") or len(spans) != 1:
                    reason = {
                        "none": "未找到对应连续块",
                        "fuzzy": "只能模糊匹配，上下文不够精确",
                        "multi": "匹配到多处无法确定",
                    }.get(status, status)
                    hunk_failures.append(f"Hunk {i+1} 定位失败（{reason}）——请给更精确的连续上下文")
                    continue
                start, end = spans[0]
                content = content[:start] + new_texts[0] + content[end:]

            if hunk_failures:
                errors.append(f"文件 {path}:\n" + "\n".join(f"  - {e}" for e in hunk_failures))
                continue

            resolved_ops.append((action, path, resolved, None, content))

        elif action == "add":
            if os.path.exists(resolved):
                errors.append(f"文件已存在，无法新增: {path}")
                continue
            new_content = "\n".join(op.get("new_lines", []))
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            resolved_ops.append((action, path, resolved, None, new_content))

        elif action == "delete":
            if not os.path.exists(resolved):
                errors.append(f"文件不存在，无法删除: {path}")
                continue
            resolved_ops.append((action, path, resolved, None, None))

    if errors:
        return "Patch 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)

    # ── Phase 3: 汇总 diff ──
    all_diffs = []
    for action, path, resolved, _, new_content in resolved_ops:
        if action == "update":
            with open(resolved, "r", encoding="utf-8") as f:
                old_content = f.read()
            diff_text = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
            ))
            if diff_text:
                all_diffs.append(diff_text)
        elif action == "add":
            diff_text = "".join(difflib.unified_diff(
                [],
                new_content.splitlines(keepends=True),
                fromfile="/dev/null", tofile=f"b/{path}", n=3,
            ))
            all_diffs.append(diff_text)
        elif action == "delete":
            with open(resolved, "r", encoding="utf-8") as f:
                old_content = f.read()
            diff_text = "".join(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                [],
                fromfile=f"a/{path}", tofile="/dev/null", n=3,
            ))
            all_diffs.append(diff_text)

    if not all_diffs:
        return "Patch 为空（没有实际变化）。"

    combined_diff = "\n".join(all_diffs)

    # ── Phase 4: 用户确认 ──
    allowed, reject = _confirm_file_write("(patch)", "", combined_diff)
    if not allowed:
        return reject

    # ── Phase 5: 写盘 ──
    added_files = 0
    modified_files = 0
    deleted_files = 0
    check_warnings = []

    for action, path, resolved, _, new_content in resolved_ops:
        try:
            _checkpoint.make_checkpoint(root, f"apply_patch_{action}", resolved)
        except Exception as e:
            logger.warning(f"checkpoint 失败（不影响 patch 应用）: {e}")

        if action == "add":
            os.makedirs(os.path.dirname(os.path.abspath(resolved)), exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)
            added_files += 1
        elif action == "update":
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(new_content)
            modified_files += 1
        elif action == "delete":
            os.remove(resolved)
            deleted_files += 1

        if action in ("add", "update"):
            _mark_current_dirty(resolved)
            issues, checker = _run_code_check(resolved)
            if checker:
                _mark_current_check(resolved, not bool(issues), checker)
            else:
                _mark_current_check(resolved, None, "")
            if checker and issues:
                check_warnings.append(f"{path}:\n{issues}")
        elif action == "delete":
            _mark_current_dirty(resolved)

    # ── 组装结果 ──
    parts = [f"Patch 已应用: {added_files} 个新增, {modified_files} 个修改, {deleted_files} 个删除"]
    if check_warnings:
        parts.append("\n⚠️ 自动校验发现问题:\n" + "\n".join(check_warnings))
    return "\n".join(parts)


@tool
def check_code(path: str) -> str:
    """静态检查单个代码文件（lint/语法/类型），返回问题列表。Python 用 ruff（F/E9 正确性）
    + mypy 类型检查（抓参数数量/签名/未定义名等"用错 API"的错;没装则相应跳过）；
    其它语言用 config 的 check_command（{file} 占位）。
    path: 要检查的文件（相对项目根）。注：编辑文件后已会自动校验，这个用于手动复查。"""
    if not path:
        return "请指定要检查的文件 path。"
    resolved = _resolve_path(path)
    root = _project_cwd()
    try:
        if os.path.commonpath([os.path.realpath(resolved), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.exists(resolved):
        return f"文件不存在: {resolved}"
    issues, checker = _run_code_check(resolved)
    if checker is None:
        ext = os.path.splitext(resolved)[1] or "（无扩展名）"
        _mark_current_check(resolved, None, "")
        return f"没有可用的检查器处理 {ext} 文件。可在 config.json 配 check_command（用 {{file}} 占位）。"
    passed = not issues
    _mark_current_check(resolved, passed, checker)
    if passed:
        return f"✓ {checker} 检查通过，无问题。"
    # 提取问题的文件:行号供自动修复循环使用
    issue_lines = issues.strip().splitlines()
    repair_entries: list[str] = [f"status=failed|checker={checker}"]
    for il in issue_lines[:10]:
        repair_entries.append(f"issue: {il.strip()}")
    parts = [f"{checker} 检查发现问题：\n{issues}"]
    parts.append("\n[REPAIR_INFO]")
    parts.append("\n".join(repair_entries))
    parts.append("[/REPAIR_INFO]")
    return "\n".join(parts)


# ══════════════════════════════════════
# 网络工具（只读调研用）
# ══════════════════════════════════════


# 从兄弟模块导入的工具（拆包：自包含域已外移，这里 import 回来装入 ALL_TOOLS + 供外部 re-export）。
# 刻意放在工具定义之后、ALL_TOOLS 之前（逻辑分组），故 E402 在这几行用 noqa 注明是有意为之。
from .tools_web import fetch_url, web_search  # noqa: E402
from .tools_git import (  # noqa: E402
    git_diff, git_log, git_status, git_stage, git_unstage, git_commit,
    build_git_write_confirmation,  # noqa: F401  re-export 给 streaming（tools.py 不直接用）
)
from .tools_codemap import code_map, find_tests, related_files  # noqa: E402
# codemap 私有 helper：test_related_files 直接 import，tools.py 不直接用 → noqa 防 ruff 删
from .tools_codemap import _extract_imports_py, _find_test_files, _module_name_for_py, _module_to_path, _score_test_candidate  # noqa: F401,E402

# 导出
ALL_TOOLS = [
    read_file, write_file, append_file, edit_file,
    list_directory, run_command,
    search_in_file, search_files,
    find_definition, find_references,
    find_tests, related_files,
    remember, forget,
    spawn_agents,
    update_plan,
    set_step_status,
    get_project_instructions,
    notify_user,
    read_background_output, list_background_commands, stop_background_command,
    code_map,
    git_diff, git_log,
    git_status, git_stage, git_unstage, git_commit,
    run_tests, check_code,
    apply_patch,
    fetch_url, web_search,
]


def get_mcp_tools() -> list:
    """延迟导入 MCP 工具列表（mcp_client.init_mcp 后才填充）。"""
    try:
        from .mcp_client import MCP_TOOLS
        return list(MCP_TOOLS)
    except Exception:
        return []


def build_all_tools() -> list:
    """返回内置工具 + 远程 MCP 工具的完整列表。"""
    return ALL_TOOLS + get_mcp_tools()


def get_tool_map() -> dict:
    """返回内置工具 + MCP 工具的 name→tool 映射（动态，每次调用重新计算）。"""
    tool_map = {t.name: t for t in ALL_TOOLS}
    for t in get_mcp_tools():
        tool_map[t.name] = t
    # 合并 MCP display names 到 TOOL_DISPLAY_NAMES（运行时注入）
    try:
        from .mcp_client import MCP_DISPLAY_NAMES
        TOOL_DISPLAY_NAMES.update(MCP_DISPLAY_NAMES)
    except Exception:
        pass
    return tool_map


TOOL_DISPLAY_NAMES = {
    "read_file": "📖 读取文件",
    "write_file": "✏️ 写入文件",
    "append_file": "📝 追加文件",
    "edit_file": "🪄 精确编辑",
    "list_directory": "📂 列出目录",
    "run_command": "⚡ 执行命令",
    "search_in_file": "🔍 单文件搜索",
    "search_files": "🌐 跨文件搜索",
    "find_definition": "🔎 跳转定义",
    "find_references": "🔗 查找引用",
    "find_tests": "🧪 查找测试",
    "related_files": "📁 关联文件",
    "remember": "🧠 记住事实",
    "forget": "🗑️ 遗忘记忆",
    "spawn_agents": "🤖 并行子 Agent",
    "update_plan": "📋 更新计划",
    "set_step_status": "📍 更新步骤",
    "read_background_output": "📋 读取后台输出",
    "list_background_commands": "📋 列出后台命令",
    "stop_background_command": "⏹ 停止后台命令",
    "code_map": "🗺 代码地图",
    "git_diff": "🔀 查看改动",
    "git_log": "📜 提交历史",
    "git_status": "📌 Git 状态",
    "git_stage": "➕ 暂存文件",
    "git_unstage": "➖ 取消暂存",
    "git_commit": "✅ 创建提交",
    "run_tests": "🧪 跑测试",
    "check_code": "🔎 代码检查",
    "apply_patch": "📦 批量补丁",
    "fetch_url": "🌐 抓取网页",
    "web_search": "🔍 网络搜索",
    "get_project_instructions": "📜 项目规则",
}


TOOL_MAP = get_tool_map()
