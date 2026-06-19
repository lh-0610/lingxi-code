"""代码地图 / 测试发现 / 关联文件：code_map（符号地图）、find_tests（找相关测试）、
related_files（导入正反向 + 相关测试）。从 tools.py 拆出的兄弟模块。

含一组导入图分析 helper（_module_to_path / _extract_imports_py / _find_test_files /
_score_test_candidate 等），被 test_related_files 直接 import → 由 tools.py re-export。
"""
import os
import re
import ast as _ast

from langchain_core.tools import tool

from .tools_common import (
    _project_cwd, _resolve_path, _SEARCH_IGNORE_DIRS, _SEARCH_MAX_FILE_SIZE,
)


@tool
def code_map(path: str = "", max_chars: int = 8000) -> str:
    """列出项目（或指定子目录）每个源码文件的类/函数清单（带行号），用于快速定位
    "某功能/类在哪个文件"，省去逐个 read_file 摸索。
    path: 相对项目根的子目录，空 = 整个项目。只读、安全。"""
    import re as _re

    # ── 路径起点 ──
    base = _resolve_path(path) if path else _project_cwd()
    # 安全：不允许 .. 逃出项目根（防扫到项目外 / 敏感目录）
    root = _project_cwd()
    try:
        if os.path.commonpath([os.path.realpath(base), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:  # 不同盘符（Windows）→ 必然越界
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.isdir(base):
        return f"失败：目录不存在 {base}"

    # ── 优先用 tree-sitter（多语言、更准确）；不可用/失败时静默回退 regex ──
    try:
        from .codeintel import code_map_ts
        ts_result = code_map_ts(base, max_chars)
        if ts_result is not None:
            return ts_result
    except Exception:
        pass  # codeintel 模块不可用，回退 regex

    # ── 回退：按扩展名定义正则 ──
    _py_re = _re.compile(r'^(?P<indent>\s*)(?P<kw>async\s+def|def|class)\s+(?P<name>\w+)')
    _js_re = _re.compile(r'^(?P<indent>\s*)(?:export\s+)?(?:async\s+)?(?P<kw>function|class)\s+(?P<name>\w+)')
    _EXT_MAP = {
        ".py": _py_re,
        ".js": _js_re, ".ts": _js_re, ".jsx": _js_re, ".tsx": _js_re,
    }
    _exts = set(_EXT_MAP.keys())

    # ── os.walk：复用 search_files 的噪声目录忽略集合 ──
    files_to_scan = []
    for root, dirs, filenames in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SEARCH_IGNORE_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _exts:
                continue
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > _SEARCH_MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files_to_scan.append((fpath, ext))

    files_to_scan.sort()

    # ── 逐文件正则提取符号 ──
    output_lines = []
    for fpath, ext in files_to_scan:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        pat = _EXT_MAP[ext]
        symbols = []
        for i, line in enumerate(lines, 1):
            m = pat.match(line)
            if m:
                indent = m.group("indent")
                keyword = m.group("kw").strip()
                name = m.group("name")
                symbols.append((indent, i, keyword, name))
        if not symbols:
            continue

        rel = os.path.relpath(fpath, base).replace(os.sep, "/")
        output_lines.append(rel)
        for indent, lineno, keyword, name in symbols:
            level = len(indent) // 2 if indent else 0
            prefix = "  " * level
            output_lines.append(f"  L{lineno:<5d} {prefix}{keyword} {name}")

    if not output_lines:
        return f"在 {base} 下未找到可扫描的源文件（.py/.js/.ts/.jsx/.tsx）"

    result = "\n".join(output_lines)
    if len(result) > max_chars:
        result = (
            result[:max_chars]
            + f"\n\n... [输出已截断（{len(output_lines)} 行中的 {max_chars} 字符）；"
            f"用 path 参数缩到子目录重新查看]"
        )
    return result


# ══════════════════════════════════════
# find_tests / related_files 辅助函数
# ══════════════════════════════════════


def _is_noise_dir(name: str) -> bool:
    """判断目录名是否为应跳过的噪声目录（.git/venv/node_modules 等）。"""
    return name in _SEARCH_IGNORE_DIRS or name.startswith(".")


def _iter_project_files(root: str, extensions: tuple = (".py",), max_file_size: int = 0):
    """遍历项目根下的文件，跳过噪声目录，返回 (absolute_path, relative_path) 元组。
    max_file_size <= 0 时用默认 _SEARCH_MAX_FILE_SIZE。"""
    _max = max_file_size if max_file_size > 0 else _SEARCH_MAX_FILE_SIZE
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_noise_dir(d)]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(abs_path) > _max:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            yield abs_path, rel


def _relpath(path: str, root: str) -> str:
    """将绝对路径转为相对项目根的正斜杠路径。"""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _module_name_for_py(abs_path: str, root: str) -> str:
    """将 Python 文件绝对路径转为模块名（src/tools.py → src.tools）。
    __init__.py 返回包名（src/__init__.py → src）。"""
    rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    rel = rel.replace("/", ".")
    if rel.endswith(".__init__"):
        rel = rel[:-9]
    return rel


def _module_to_path(module: str, root: str) -> str | None:
    """将模块名（如 src.tools）映射回项目内的文件路径。返回绝对路径或 None。
    支持 src.tools → src/tools.py 和 src.ui.header → src/ui/header.py。
    也尝试包目录 src/__init__.py。"""
    parts = module.split(".")
    # 先试文件
    fpath = os.path.join(root, *parts) + ".py"
    if os.path.isfile(fpath):
        return fpath
    # 再试包目录
    pkg_init = os.path.join(root, *parts, "__init__.py")
    if os.path.isfile(pkg_init):
        return pkg_init
    return None


def _extract_imports_py(abs_path: str, file_module: str = "") -> list[str]:
    """用 ast 从 Python 文件提取 import 的模块名列表。
    file_module: 文件自身的模块名（如 "src.tools"），用于解析相对导入。
    解析失败时退回正则匹配 import/from 语句。"""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = _ast.parse(source, filename=abs_path)
    except Exception:
        # 退回到正则
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception:
            return []
        modules = []
        for m in re.finditer(r'^\s*(?:import|from)\s+([\w.]+)', source, re.MULTILINE):
            modules.append(m.group(1))
        return modules

    modules = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, _ast.ImportFrom):
            if node.level == 0 and node.module:
                # 同时保留容器模块和导入成员。后者若是项目子模块，
                # `from src import state` 才能精确识别为 src.state。
                modules.append(node.module)
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
            elif node.level > 0 and file_module:
                # 相对导入基于当前 package，而非当前文件模块。
                is_package = os.path.basename(abs_path) == "__init__.py"
                package = file_module if is_package else file_module.rpartition(".")[0]
                base_parts = package.split(".") if package else []
                up = node.level - 1
                base = ".".join(base_parts[:len(base_parts) - up]) if up <= len(base_parts) else ""
                if node.module:
                    mod = f"{base}.{node.module}" if base else node.module
                    modules.append(mod)
                    modules.extend(f"{mod}.{alias.name}" for alias in node.names)
                elif base:
                    for alias in node.names:
                        modules.append(f"{base}.{alias.name}")
            elif node.module:
                modules.append(node.module)
    return list(dict.fromkeys(modules))


def _imports_target(imports: list[str], target_module: str) -> bool:
    """是否确实导入目标模块；父包 import 不等于导入其任意子模块。"""
    if not target_module:
        return False
    return any(
        imp == target_module or imp.startswith(target_module + ".")
        for imp in imports
    )


def _extract_imports_generic(abs_path: str) -> list[str]:
    """用 codeintel 从非 Python 文件提取导入路径/模块名。
    返回的字符串是原始 import specifier（如 './utils'、'lodash'、'./api'）。
    codeintel 不可用或解析失败时返回空列表。"""
    try:
        from .codeintel import extract_imports_from_content
    except Exception:
        return []
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []
    return extract_imports_from_content(content, path=abs_path)


def _resolve_import_to_file(specifier: str, source_dir: str, project_root: str) -> str | None:
    """把 import specifier（如 './utils'、'../services/api'、'./helpers.ts'）解析为
    项目内的绝对文件路径。返回 None 表示解析不到。
    只匹配项目根范围内的文件，第三方库跳过。"""
    # 只处理相对路径（以 ./ 或 ../ 开头）；绝对路径和裸模块名（如 'react'）跳过
    if not specifier.startswith("."):
        return None

    base_dir = os.path.normpath(os.path.join(source_dir, specifier))
    # 尝试后缀列表（按常见度排序）
    extensions = [".ts", ".tsx", ".js", ".jsx", ".py", ""]
    # 先试自身带扩展名的情况
    candidates = [base_dir] + [base_dir + ext for ext in extensions]
    # 也试 index 文件
    candidates += [os.path.join(base_dir, "index" + ext) for ext in extensions]

    real_root = os.path.realpath(project_root)
    for cand in candidates:
        if os.path.isfile(cand):
            real_cand = os.path.realpath(cand)
            if os.path.commonpath([real_cand, real_root]) == real_root:
                return real_cand
    return None


def _find_test_files(root: str) -> list[tuple[str, str]]:
    """在项目根下查找所有测试文件，返回 [(abs_path, rel_path), ...]。
    rel_path 始终相对于项目根。搜索范围：tests/, test/, scripts/ 目录 + 根下所有 test_*.py / *_test.py。"""
    test_files = []
    seen = set()

    # 已知测试目录。即便在 scripts/ 下，也只把 pytest 命名文件当测试，
    # 避免 conftest.py、构建脚本和一次性工具被推荐给 run_tests。
    for d in ("tests", "test", "scripts"):
        dpath = os.path.join(root, d)
        if os.path.isdir(dpath):
            for abs_path, _ in _iter_project_files(dpath, (".py",)):
                basename = os.path.basename(abs_path)
                if not (basename.startswith("test_") or basename.endswith("_test.py")):
                    continue
                if abs_path not in seen:
                    seen.add(abs_path)
                    rel = _relpath(abs_path, root)
                    test_files.append((abs_path, rel))

    # 项目根下直接匹配 test_*.py / *_test.py
    for abs_path, rel in _iter_project_files(root, (".py",)):
        basename = os.path.basename(abs_path)
        if (basename.startswith("test_") or basename.endswith("_test.py")) and abs_path not in seen:
            seen.add(abs_path)
            test_files.append((abs_path, rel))

    return test_files


def _score_test_candidate(
    test_abs: str, test_rel: str,
    target_stem: str, target_module: str,
    symbol: str, root: str,
) -> tuple[int, list[str]]:
    """为一个测试文件打分，返回 (score, reasons)。
    target_stem: 目标文件的无后缀名（如 tools）
    target_module: 目标文件的模块名（如 src.tools）
    symbol: 可选符号名。"""
    score = 0
    reasons = []
    test_stem = os.path.splitext(os.path.basename(test_abs))[0]

    # 1. 文件名强匹配 +50
    if test_stem in (f"test_{target_stem}", f"{target_stem}_test"):
        score += 50
        reasons.append("文件名匹配")
    elif target_stem in test_stem and test_stem.startswith("test_"):
        score += 30
        reasons.append("文件名部分匹配")

    # 2. import 目标模块 +40
    try:
        test_module = _module_name_for_py(test_abs, root)
        imports = _extract_imports_py(test_abs, test_module)
        if _imports_target(imports, target_module):
            score += 40
            reasons.append("import 匹配")
    except Exception:
        pass

    # 3. 符号相关
    if symbol:
        try:
            with open(test_abs, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # 测试函数名包含 symbol +35
            for m in re.finditer(r'def\s+(test_\w*' + re.escape(symbol) + r'\w*)\s*\(', content):
                score += 35
                reasons.append(f"测试函数 {m.group(1)}")
                break
            else:
                # 文件内容包含 symbol +25
                if symbol in content:
                    score += 25
                    reasons.append(f"内容包含 {symbol}")
        except Exception:
            pass

    # 4. 同目录 / 常见测试目录弱匹配 +10
    top_dir = test_rel.replace("\\", "/").split("/", 1)[0]
    if top_dir in ("tests", "test", "scripts"):
        score += 10
        reasons.append("测试目录")

    return score, reasons


# ══════════════════════════════════════
# find_tests / related_files 工具
# ══════════════════════════════════════


@tool
def find_tests(path: str = "", symbol: str = "", max_results: int = 20) -> str:
    """根据源码文件路径和可选符号名，返回最可能相关的测试文件 / 测试用例候选。
    path: 源码文件或目录，相对项目根或绝对路径。留空 = 项目根。
    symbol: 可选函数 / 类 / 方法名。
    max_results: 最多返回条数，范围 1-50。
    返回匹配的测试文件、命中原因和推荐 run_tests 命令。"""
    max_results = max(1, min(50, max_results))
    root = _project_cwd()

    # 路径解析和安全校验
    if path:
        full = _resolve_path(path)
        try:
            if os.path.commonpath([os.path.realpath(full), os.path.realpath(root)]) != os.path.realpath(root):
                return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        except ValueError:
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
        if not os.path.exists(full):
            return f"失败：路径不存在 {path}"
    else:
        full = root

    # 确定目标文件名和模块名
    if os.path.isfile(full):
        target_stem = os.path.splitext(os.path.basename(full))[0]
        target_module = _module_name_for_py(full, root) if full.endswith(".py") else ""
    elif os.path.isdir(full):
        target_stem = os.path.basename(full.rstrip("/\\")) or ""
        target_module = ""
    else:
        return f"失败：路径不存在 {path}"

    # 搜索测试文件
    test_files = _find_test_files(root)
    if not test_files:
        return "未找到任何测试文件（搜索了 tests/ test/ scripts/ 和 test_*.py / *_test.py 模式）。"

    # 评分
    scored = []
    for test_abs, test_rel in test_files:
        sc, reasons = _score_test_candidate(test_abs, test_rel, target_stem, target_module, symbol, root)
        # 指定目标时，单凭“位于测试目录”的 10 分不算相关；否则所有测试都会入榜。
        # 无 path 时视为“列出项目测试”，允许测试目录弱候选。
        if sc > (0 if not path else 10):
            scored.append((sc, test_rel, reasons))
    scored.sort(key=lambda x: (-x[0], x[1].lower()))

    if not scored:
        return (f"未找到与 `{path or '项目根'}` 相关的测试文件。\n"
                "建议：检查项目是否有 tests/ 或 scripts/ 目录，或手动运行 run_tests() 跑全量测试。")

    # 输出
    lines = [f"为 `{path or '项目根'}` 找到的测试候选（共 {len(scored)} 个，显示前 {max_results}）："]
    if symbol:
        lines.append(f"符号过滤: `{symbol}`")
    lines.append("")

    for sc, rel, reasons in scored[:max_results]:
        reason_str = ", ".join(reasons)
        lines.append(f"- {rel}  (分数 {sc}: {reason_str})")

    # 推荐运行命令
    lines.append("")
    lines.append("建议运行：")
    top_score = scored[0][0]
    top_files = [rel for score, rel, _ in scored if score == top_score][:3]
    if symbol:
        for top_file in top_files:
            try:
                with open(os.path.join(root, top_file), "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                has_test_fn = bool(re.search(r'def\s+test_\w*' + re.escape(symbol), content))
            except Exception:
                has_test_fn = False
            if has_test_fn:
                lines.append(f'run_tests("{top_file}", k="{symbol}")')
            else:
                lines.append(f'run_tests("{top_file}")')
    else:
        for top_file in top_files:
            lines.append(f'run_tests("{top_file}")')
    if len(top_files) > 1:
        lines.append("（以上候选同分；可提供 symbol 进一步缩小范围。）")

    return "\n".join(lines)


@tool
def related_files(path: str, max_results: int = 30) -> str:
    """给定一个源码文件，返回修改它前应关注的相关文件。
    path: 源码文件路径（相对项目根或绝对路径）。
    max_results: 最多返回条数，范围 1-100。
    输出分组：目标文件、它导入的项目内文件、导入它的项目内文件、相关测试候选、建议下一步。"""
    max_results = max(1, min(100, max_results))
    root = _project_cwd()

    # 路径解析和安全校验
    full = _resolve_path(path)
    try:
        if os.path.commonpath([os.path.realpath(full), os.path.realpath(root)]) != os.path.realpath(root):
            return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    except ValueError:
        return "失败：路径超出项目范围，不允许（不能用 .. 逃出项目根）"
    if not os.path.isfile(full):
        return f"失败：文件不存在: {path}"

    rel_target = _relpath(full, root)
    is_py = full.endswith(".py")
    source_dir = os.path.dirname(full)
    target_stem = os.path.splitext(os.path.basename(full))[0]

    # 1. 解析目标文件的 imports
    if is_py:
        target_module = _module_name_for_py(full, root)
        imports = _extract_imports_py(full, target_module)
        imported_files = []
        for mod in imports:
            p = _module_to_path(mod, root)
            if p and os.path.isfile(p):
                imported_files.append(_relpath(p, root))
    else:
        target_module = ""
        imports = _extract_imports_generic(full)
        imported_files = []
        for spec in imports:
            p = _resolve_import_to_file(spec, source_dir, root)
            if p:
                imported_files.append(_relpath(p, root))

    # 去重保持顺序
    seen = set()
    imported_files = [f for f in imported_files if f not in seen and not seen.add(f)]

    # 2. 反向导入：扫描项目源文件，找谁 import 了 target
    reverse_importers = []
    if is_py:
        all_files = list(_iter_project_files(root, (".py",)))
    else:
        exts = (".ts", ".tsx", ".js", ".jsx", ".py")
        all_files = list(_iter_project_files(root, exts))

    for abs_path, rel in all_files:
        if abs_path == full:
            continue
        try:
            if abs_path.endswith(".py") and is_py:
                file_module = _module_name_for_py(abs_path, root)
                file_imports = _extract_imports_py(abs_path, file_module)
                if _imports_target(file_imports, target_module):
                    reverse_importers.append(rel)
            else:
                # 非 Python（或目标非 Python）：用 codeintel 匹配导入路径
                file_imports = _extract_imports_generic(abs_path)
                file_dir = os.path.dirname(abs_path)
                for spec in file_imports:
                    resolved = _resolve_import_to_file(spec, file_dir, root)
                    if resolved and os.path.normcase(resolved) == os.path.normcase(full):
                        reverse_importers.append(rel)
                        break
        except Exception:
            pass

    # 3. 测试候选（复用 find_tests 逻辑）
    test_files = _find_test_files(root)
    test_candidates = []
    for test_abs, test_rel in test_files:
        sc, reasons = _score_test_candidate(test_abs, test_rel, target_stem, target_module, "", root)
        if sc > 10:
            test_candidates.append((sc, test_rel, reasons))
    test_candidates.sort(key=lambda x: (-x[0], x[1].lower()))

    # 构建输出
    lines = [f"目标文件: {rel_target}", ""]

    # 它导入的项目内文件
    if imported_files:
        lines.append("它导入的项目内文件:")
        for f in imported_files[:max_results]:
            lines.append(f"- {f}")
        lines.append("")

    # 导入它的项目内文件
    if reverse_importers:
        lines.append("导入它的项目内文件:")
        for f in reverse_importers[:max_results]:
            lines.append(f"- {f}")
        lines.append("")

    # 相关测试候选
    if test_candidates:
        lines.append("相关测试候选:")
        for sc, rel, reasons in test_candidates[:10]:
            reason_str = ", ".join(reasons)
            lines.append(f"- {rel}  ({reason_str})")
        lines.append("")

    # 建议下一步
    lines.append("建议下一步:")
    lines.append(f'- read_file("{rel_target}", ...)')
    if imported_files:
        lines.append(f'- read_file("{imported_files[0]}", ...)')
    if reverse_importers:
        lines.append(f'- read_file("{reverse_importers[0]}", ...)')
    if test_candidates:
        top_test = test_candidates[0][1]
        lines.append(f'- run_tests("{top_test}")')
    else:
        lines.append("- run_tests()  # 全量测试")

    return "\n".join(lines)
