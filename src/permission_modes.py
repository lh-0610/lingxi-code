"""权限模式：哪些操作直接放行、哪些先问用户（输入框底栏那个"模式"菜单）。

五种模式。"计划"就是原来的 Plan 模式（agent_mode="plan"，只给只读工具）；其余四种是执行时的
放行规则，存在会话的 permission_mode 字段里：

    模式                    改文件   普通命令          危险命令 / 脚本改文件 / Git 写 / MCP
    manual 手动             问       问（白名单照旧）   问
    accept_edits 接受编辑   放行     问                问
    auto 自动               放行     放行              问
    bypass 跳过权限确认     放行     放行              放行

放行只在 ui/confirm_bars.py 的 confirm_command / confirm_edit 开头判一次，按**发起确认的那个
会话**的模式，不分散到各个工具里。交互式命令（BLOCKED_COMMANDS）在更早的地方就被拒了，
任何模式都不会放行；子 Agent、手机遥控的规则不经过这里。

"自动"只是按规则拦危险命令（confirm_bars._is_destructive_command）和脚本改文件
（下面的 script_writes_files），不像 Claude Code 那样有模型逐条判断风险，所以菜单里不标"推荐"。
"""
import os
import re

PLAN = "plan"
DEFAULT = "manual"

# 确认的"操作类型"：confirm_command(text, kind=...) 由调用方明说是什么操作，放行判定按类型来，
# 不再从确认文案的开头几个字去猜（文案一改，判定就会悄悄失效）。
KIND_COMMAND = "command"            # run_command 的 shell 命令：危险与否按命令内容判断
KIND_GIT_WRITE = "git_write"        # git_commit / git_push 等写工具：每次都问、不给"记住"
KIND_MCP = "mcp"                    # MCP 工具：自动模式也要问；"记住"只记这一个工具
KIND_SPAWN = "spawn_agents"         # 派子 Agent：子 Agent 改文件、跑命令都不再逐条问，
                                    # 做完直接合并回项目——每次都问、不给"记住"
ALWAYS_ASK_KINDS = (KIND_GIT_WRITE, KIND_SPAWN)

# 派子 Agent 确认卡上的标题
SPAWN_AGENTS_PREFIX = "将派出并行子 Agent"

# (取值, 名字, 一句话说明)。菜单按这个顺序列，数字键 1-5 依次对应
MODES = (
    ("auto", "自动", "由灵犀按规则判断，危险命令、脚本改文件、Git 写操作和 MCP 工具仍会询问"),
    ("manual", "手动", "改文件、跑命令前都先问你"),
    ("accept_edits", "接受编辑", "自动接受所有文件编辑，命令仍会询问"),
    (PLAN, "计划", "只调研给方案，不动手改东西"),
    ("bypass", "跳过权限确认", "所有操作都不再询问"),
)
EXEC_MODES = tuple(m for m, _name, _desc in MODES if m != PLAN)
_NAMES = {m: n for m, n, _desc in MODES}


def normalize(mode) -> str:
    """执行时的放行规则；认不出的（旧会话没有这个字段、手改坏了）按"手动"。"""
    return mode if mode in EXEC_MODES else DEFAULT


def current(sess) -> str:
    """这个会话现在的模式（五种之一）。"""
    if getattr(sess, "agent_mode", "act") == PLAN:
        return PLAN
    return normalize(getattr(sess, "permission_mode", DEFAULT))


def apply(sess, mode) -> None:
    """切换模式：计划 → agent_mode=plan（放行规则保留，切回执行时照旧）；其余 → agent_mode=act。"""
    if mode == PLAN:
        sess.agent_mode = PLAN
    else:
        sess.agent_mode = "act"
        sess.permission_mode = normalize(mode)


def name(mode) -> str:
    return _NAMES.get(mode, _NAMES[DEFAULT])


def describe(mode) -> str:
    """菜单里那句说明。"""
    return next((d for m, _n, d in MODES if m == mode), "")


def default_for_new_session(previous, remembered) -> str:
    """新会话用哪种放行规则：沿用上一个会话的，但"跳过权限确认"不往下传，退回记住的那个。"""
    previous = normalize(previous)
    if previous != "bypass":
        return previous
    remembered = normalize(remembered)
    return DEFAULT if remembered == "bypass" else remembered


def label(mode, *, long: bool = False) -> str:
    """回复上方那行小字、结果卡"上次使用"里的写法："手动模式""计划模式"……"""
    if mode == PLAN and long:
        return "计划模式（只调研，不改动）"
    return f"{name(mode)}模式"


def snapshot_mode(snapshot) -> str:
    """结果快照里记的模式。旧快照只有 agent_mode：那时执行一律每次都问，就是"手动"。"""
    if (snapshot or {}).get("agent_mode") == PLAN:
        return PLAN
    return normalize((snapshot or {}).get("permission_mode"))


def allows_edit(mode) -> bool:
    """这个模式下写文件（edit_file / write_file / append_file / apply_patch）不用问。"""
    return mode in ("accept_edits", "auto", "bypass")


def allows_command(mode, *, risky: bool) -> bool:
    """这个模式下这条命令不用问。risky：危险命令、脚本改文件、Git 写操作或 MCP 工具。"""
    if mode == "bypass":
        return True
    if mode == "auto":
        return not risky
    return False


# ── 脚本改文件识别 ─────────────────────────────────────────────
# run_command 跑脚本（python fix.py / python - + stdin）批量改文件时，edit_file 的逐处确认
# 不会触发——真实任务里实测过"8 个脚本改文件只确认一条命令"。这里把"会写文件的脚本执行"
# 认出来，confirm_command 把它和危险命令一样对待：每次必问、不给"记住"。
# 是启发式，宁可多问：防的是模型图省事绕确认，不是对抗蓄意混淆。扫不到的形态：
# python -m 模块（如 -m black）、脚本里再调 subprocess、start "标题" 后面的命令
# （引号切词后分不出标题和命令，只认空标题 start ""）。

_INTERP_LANG = {
    "python": "python", "python3": "python", "py": "python",
    "node": "js", "bash": "sh", "sh": "sh",
    "powershell": "ps1", "pwsh": "ps1",
}
_EXT_LANG = {
    ".py": "python", ".js": "js", ".mjs": "js", ".cjs": "js",
    ".sh": "sh", ".ps1": "ps1", ".bat": "cmd", ".cmd": "cmd",
}
_SCRIPT_SCAN_MAX = 200_000     # 脚本文件最多读这么多字符来判定
_CD_COMMANDS = {"cd", "chdir", "pushd"}
_SHELL_LANGS = {"sh", "ps1"}   # 内联代码本身又是一条命令行（bash -c "python fix.py"），要再判一层
_MAX_NESTING = 3               # cmd /c "bash -c '...'" 这种套娃最多跟几层

# 直接执行后面那段代码的选项（区分大小写）
_INLINE_FLAGS = {
    "python": {"-c"},
    "js": {"-e", "--eval", "-p", "--print", "-pe"},
    "sh": {"-c"},
}
# 后面单独跟一个值的选项：那个值不是脚本，要跳过（python -X utf8 fix.py）。
# 区分大小写——python 的 -X 带值，-x 不带
_VALUE_FLAGS = {
    "python": {"-W", "-X", "--check-hash-based-pycs"},
    "js": {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"},
    "sh": {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"},
}
# PowerShell 的参数不分大小写、可以只写前缀（-ex = -ExecutionPolicy，-enc = -EncodedCommand）
_PS_VALUE_PARAMS = (
    "executionpolicy", "windowstyle", "outputformat", "inputformat", "version",
    "configurationname", "psconsolefile", "workingdirectory", "settingsfile", "custompipename",
)
_PS_ALIASES = {"ep": "executionpolicy", "wd": "workingdirectory", "ec": "encodedcommand"}

# open(...) 的写模式：纯 mode 字符串里含 w/a/x/+（"r"/"rb" 不算，errors="replace" 不算）
_PY_OPEN_MODE = r"""['"](?=[rwaxb+]*[wax+])[rwaxb+]{1,4}['"]"""
_WRITE_PATTERNS = {
    "python": (
        r"\bopen\s*\([^)\n]*,\s*" + _PY_OPEN_MODE,
        r"\bopen\s*\([^)\n]*\bmode\s*=\s*" + _PY_OPEN_MODE,
        # 不含 .replace/.rename：str.replace、DataFrame.rename 都是只读操作，误伤面太大
        r"\.(?:write_text|write_bytes|unlink|touch)\s*\(",
        r"\bos\.(?:remove|unlink|rename|renames|replace|rmdir|removedirs)\s*\(",
        r"\bshutil\.(?:copy|copy2|copyfile|copyfileobj|copytree|move|rmtree)\s*\(",
        r"\binplace\s*=\s*True",          # fileinput.input(..., inplace=True) 原地改文件
    ),
    "js": (
        r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream|"
        r"unlink|unlinkSync|rm|rmSync|rmdir|rmdirSync|rename|renameSync|"
        r"copyFile|copyFileSync|mkdir|mkdirSync|truncate|truncateSync)\s*\(",
    ),
    "sh": (
        r">(?!\s*&)(?!\s*/dev/null)",     # 重定向写文件（2>&1、>/dev/null 不算）
        r"\b(?:tee|cp|mv|rm|mkdir|touch|ln|truncate|dd)\b",
        r"\bsed\b[^|\n]*\s-i\b",
    ),
    "ps1": (
        r"\b(?:Set|Add|Clear)-Content\b|\bOut-File\b|"
        r"\b(?:New|Remove|Copy|Move|Rename)-Item\b",
        r">(?!\s*&)",
    ),
    "cmd": (
        r"\b(?:del|erase|copy|xcopy|robocopy|move|ren|rename|md|mkdir|rd|rmdir)\b",
        r">(?!\s*&)(?!\s*nul)",
    ),
}


def script_writes_files(command: str, stdin: str = "", cwd: str = "", _depth: int = 0) -> bool:
    """这条命令会不会"跑一段会写文件的脚本"。

    脚本来源都查：stdin（python - ）、-c/-e/-Command 内联代码、脚本文件（按 cwd 解析，
    链式命令里的 cd 会跟进）、从 < 文件或管道喂给解释器的脚本（type fix.py | python）。
    外面包的一层启动命令会剥掉再看（call / start / cmd /c），shell 的内联代码当命令行再判一层
    （bash -c "python fix.py"）。
    脚本存在但读不了、管道上游产出什么看不到、内容是编码过的 → 按会写处理（宁可多问）。
    """
    if not command or _depth > _MAX_NESTING:
        return False
    segments = _split_segments(command)
    if segments is None:                             # 引号不配对：不认引号再切一遍，不崩
        segments = [([w.strip("\"'") for w in words], infile.strip("\"'"), piped)
                    for words, infile, piped in _split_segments(command, quotes="")]
    prev = []
    for seg, stdin_file, piped in segments:
        upstream, prev = (prev if piped else None), seg
        inner, seg_cwd, nested = _unwrap(seg, cwd)
        if nested:                                   # cmd /c "一整条命令行"：整条重新判
            if script_writes_files(nested, stdin, seg_cwd, _depth + 1):
                return True
            continue
        if not inner:
            continue
        if inner[0].lower() in _CD_COMMANDS and len(inner) > 1:   # cd sub && python fix.py：跟进目录
            dirs = [a for a in inner[1:] if a.lower() != "/d"]     # cmd 的 cd /d sub
            # 包在 cmd /c 里的 cd 只在那层 shell 里生效，不带到后面的段
            if dirs and inner is seg:
                cwd = _follow_dir(cwd, dirs[0])
            continue
        source, lang, inline = _script_source(inner, stdin, seg_cwd, stdin_file, upstream)
        if source is None:                           # 脚本存在但读不了 / 管道上游看不到
            return True
        if source and any(re.search(p, source, re.I) for p in _WRITE_PATTERNS[lang]):
            return True
        if (inline and source and lang in _SHELL_LANGS
                and script_writes_files(source, stdin, seg_cwd, _depth + 1)):
            return True
    return False


def _full_path(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)


def _follow_dir(cwd: str, target: str) -> str:
    full = _full_path(target, cwd)
    return full if os.path.isdir(full) else cwd


def _unwrap(seg: list, cwd: str):
    """剥掉包在外面的启动命令，返回 (里面那段的词, 那段的 cwd, 嵌套的整条命令行)。

    call fix.bat / start "" /b python fix.py / cmd /c python fix.py：真正跑的是后面那段。
    cmd /c "cd sub && python fix.py"：引号里是一整条命令行，从第三项带出来给调用方整条重判。
    没包东西 → 原样返回同一个 seg 对象（调用方靠这个区分"cd 是不是包在里面的"）。
    """
    for _ in range(_MAX_NESTING):
        if not seg:
            break
        head = os.path.basename(seg[0]).lower()
        if head.endswith(".exe"):
            head = head[:-4]
        if head == "call":
            seg = seg[1:]
        elif head == "start":
            rest = seg[1:]
            while rest and (not rest[0] or rest[0].startswith("/")):   # 空标题和 /b /wait 等开关
                if rest[0].lower() == "/d" and len(rest) > 1:           # start /d 目录
                    cwd = _follow_dir(cwd, rest[1])
                    rest = rest[2:]
                else:
                    rest = rest[1:]
            seg = rest
        elif head == "cmd":
            at = next((k for k, a in enumerate(seg) if a.lower() in ("/c", "/k")), -1)
            if at < 0:
                break
            rest = seg[at + 1:]
            if len(rest) == 1:
                return [], cwd, rest[0]
            seg = rest
        else:
            break
    return seg, cwd, ""


def _split_segments(command: str, quotes: str = "\"'"):
    """按引号外的 && || | & ; 把命令切成段，返回 [(词列表, 输入重定向的文件, 是否接在管道后)]。

    逐字符扫而不是按空白切词：模型常写 `a&&b`、`a;b`、`fix.py>out.log`，分隔符和重定向
    两边不带空格，按空白切会把它们和旁边的词粘成一个，后面那段脚本就认不出来。
    重定向（> >> 2> 2>&1 <）从词里摘掉，其中 < 的文件单独带出来（python < fix.py）。
    引号没配对 → None，调用方换 quotes="" 再切。
    """
    segments = []
    words, word, in_word = [], [], False
    stdin_file, piped = "", False
    redirect = ""                                    # 下一个词是重定向的目标："<" 或 ">"
    quote = ""

    def end_word():
        nonlocal in_word, redirect, stdin_file
        if in_word:
            text = "".join(word)
            if redirect == "<":
                stdin_file = text
            elif not redirect:
                words.append(text)
            redirect = ""
        word.clear()
        in_word = False

    def end_segment(next_piped: bool):
        nonlocal words, stdin_file, piped, redirect
        end_word()
        segments.append((words, stdin_file, piped))
        words, stdin_file, piped, redirect = [], "", next_piped, ""

    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                quote = ""
            else:
                word.append(c)
        elif c in quotes:
            quote, in_word = c, True
        elif c.isspace():
            end_word()
        elif c in "<>":
            if in_word and all(ch.isdigit() for ch in word):   # 2> 前面粘着的 fd 号
                word.clear()
                in_word = False
            end_word()
            if command[i:i + 2] == ">>":
                i += 1
            if command[i + 1:i + 2] == "&":          # 2>&1：fd 复制，没有目标文件，& 也不是分隔符
                i += 1
                while i + 1 < n and command[i + 1].isdigit():
                    i += 1
            else:
                redirect = c
        elif c in "&|;":
            two = command[i:i + 2]
            if two in ("&&", "||"):
                i += 1
            end_segment(next_piped=(c == "|" and two != "||"))
        else:
            word.append(c)
            in_word = True
        i += 1
    if quote:
        return None
    end_segment(False)
    return segments


def _stdin_script(lang: str, stdin: str, cwd: str, stdin_file: str, upstream):
    """解释器从标准输入读脚本时，读到的到底是什么。upstream：管道上一段的词，没接管道是 None。"""
    if stdin_file:                                   # python < fix.py
        return _read_script(stdin_file, cwd, lang)
    if upstream is None:
        return stdin, lang                           # 没接管道：就是 run_command 带的 stdin
    head = os.path.basename(upstream[0]).lower() if upstream else ""
    if head in ("type", "cat"):                      # type fix.py | python
        parts = []
        for path in upstream[1:]:
            if path.startswith("-"):
                continue
            text, _ = _read_script(path, cwd, lang)
            if text is None:
                return None, lang
            parts.append(text)
        return "\n".join(parts), lang
    if head == "echo":                               # echo 代码 | python
        return " ".join(upstream[1:]), lang
    return None, lang                                # 上游产出什么看不到：按会写处理


def _script_source(seg: list, stdin: str, cwd: str, stdin_file: str = "", upstream=None):
    """一段命令的 (脚本内容, 语言, 是不是命令行里直接写的内联代码)。

    不是脚本执行 → ("", "", False)；脚本读不了 / 内容看不到 → (None, 语言, False)。
    """
    base = os.path.basename(seg[0]).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    lang = _INTERP_LANG.get(base)
    if not lang:
        if os.path.splitext(base)[1] in _EXT_LANG:
            return (*_read_script(seg[0], cwd), False)   # 直接跑脚本（Windows 文件关联）
        return "", "", False
    args = seg[1:]
    i = 0
    while i < len(args):                             # 先过选项，停在第一个位置参数上
        arg = args[i]
        if arg == "-":
            return (*_stdin_script(lang, stdin, cwd, stdin_file, upstream), False)
        if not arg.startswith("-") and arg not in _VALUE_FLAGS.get(lang, ()):
            break
        i += 1
        if lang == "ps1":
            param = _ps_param(arg)
            if param == "command":
                return " ".join(args[i:]), lang, True
            if param == "file":
                return (*(_read_script(args[i], cwd, lang) if i < len(args) else ("", lang)), False)
            if param == "encodedcommand":            # base64，看不到内容：按会写处理
                return None, lang, False
            if param == "value":
                i += 1
            continue
        if lang == "python" and arg in ("-m", "--module"):   # 模块代码扫不了，跳过
            return "", lang, False
        # -lc / -Ic 这种粘在一起的短选项，以 c 结尾的也是"后面跟代码"
        if arg in _INLINE_FLAGS[lang] or (lang != "js" and re.fullmatch(r"-[A-Za-z]+c", arg)):
            return " ".join(args[i:]), lang, True
        if arg in _VALUE_FLAGS[lang]:
            i += 1
    rest = args[i:]
    if not rest:                                     # 裸解释器：从 stdin 读脚本
        return (*_stdin_script(lang, stdin, cwd, stdin_file, upstream), False)
    if lang == "ps1":
        # 不写 -Command 也一样：位置参数是脚本文件就跑文件，否则整段当命令执行
        if os.path.exists(_full_path(rest[0], cwd)):
            return (*_read_script(rest[0], cwd, lang), False)
        return " ".join(rest), lang, True
    # 脚本 = 第一个真实存在的位置参数。不认识的带值选项（node --某选项 值 fix.js）
    # 那个值通常不是文件，会被跳过；语言跟解释器走，不看扩展名（python fix.txt 也是脚本）
    for arg in rest:
        if not arg.startswith("-") and os.path.exists(_full_path(arg, cwd)):
            return (*_read_script(arg, cwd, lang), False)
    return "", lang, False                           # 脚本不存在：命令本来就跑不起来


def _ps_param(flag: str) -> str:
    """PowerShell 的 -xxx 是哪一类：command / file / encodedcommand / value（后面跟一个值）/ ""（开关）。"""
    key = flag.lstrip("-").lower()
    key = _PS_ALIASES.get(key, key)
    if not key:
        return ""
    for name in ("command", "file", "encodedcommand"):
        if name.startswith(key):
            return name
    return "value" if any(name.startswith(key) for name in _PS_VALUE_PARAMS) else ""


def _read_script(path: str, cwd: str, lang: str = ""):
    """lang 给了就按它算（python < fix.txt：文件名说明不了语言）；没给按扩展名认。"""
    lang = lang or _EXT_LANG.get(os.path.splitext(path)[1].lower())
    if not lang:
        return "", ""
    try:
        with open(_full_path(path, cwd), "r", encoding="utf-8", errors="replace") as f:
            return f.read(_SCRIPT_SCAN_MAX), lang
    except FileNotFoundError:
        return "", ""                                # 不存在：命令本来就跑不起来
    except OSError:
        return None, lang
