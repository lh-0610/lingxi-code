"""edit_file 健壮化验收测试（第二轮 11 个用例）

直接测 _locate_edit 核心逻辑，不走 edit_file 的确认卡/checkpoint。
"""
import sys
import os
import time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.tools import _locate_edit, _realign_indent

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ═══════════════════════════════════════════
# 用例 1：精确命中 -> L1 成功（回归不破）
# ═══════════════════════════════════════════
print("\n用例 1：精确命中 -> L1 成功")
content1 = "line1\nline2\nline3\nline4\n"
old1 = "line2\nline3"
new1 = "line2_edited\nline3_edited"
status, spans, new_texts, info = _locate_edit(content1, old1, new1, False)
check("status == 'exact'", status == "exact", f"got {status}")
check("spans 非空且唯一", len(spans) == 1, f"got {len(spans)}")
if spans:
    s, e = spans[0]
    check("span 内容 == old", content1[s:e] == old1, f"got {content1[s:e]!r}")
check("line_no == 2", info[1] == [2], f"got {info[1]}")
check("new_texts[0] == new1", new_texts[0] == new1, f"got {new_texts[0]!r}")


# ═══════════════════════════════════════════
# 用例 2：old_string 每行带/少尾随空格 -> L2 成功
# ═══════════════════════════════════════════
print("\n用例 2：尾随空格差异 -> L2 成功")
content2 = "line1\nline2   \nline3   \nline4\n"
old2 = "line2\nline3"
new2 = "line2_new\nline3_new"
status2, spans2, new_texts2, info2 = _locate_edit(content2, old2, new2, False)
check("status == 'normalized'", status2 == "normalized", f"got {status2}")
check("spans 唯一", len(spans2) == 1, f"got {len(spans2)}")
check("描述含 L2", "L2" in info2[0], f"got {info2[0]}")
check("line_no == 2", info2[1] == [2], f"got {info2[1]}")
check("new_texts[0] == new2", new_texts2[0] == new2, f"got {new_texts2[0]!r}")


# ═══════════════════════════════════════════
# 用例 3：整段缩进错 -> L3 成功且落盘缩进正确
# ═══════════════════════════════════════════
print("\n用例 3：缩进错误 -> L3 成功且缩进重对齐")
# 文件用 tab 缩进（\t\t 代表二层嵌套）
content3 = "line1\n\tdef foo():\n\t\tpass\nline4\n"
# 模型用 4 空格（与文件不同，L1 不命中；strip 后匹配 L3）
old3 = "    def foo():\n        pass"
new3 = "    def foo():\n        return 42"
status3, spans3, new_texts3, info3 = _locate_edit(content3, old3, new3, False)
check("status == 'normalized'", status3 == "normalized", f"got {status3}")
check("描述含 L3", "L3" in info3[0], f"got {info3[0]}")
if new_texts3:
    realigned = new_texts3[0]
    check("重对齐后首行以 tab 开头", realigned.startswith("\t"), f"got {realigned!r}")
    # level 乘法：model_unit=4空格, file_unit=tab, 8空格→level 2→2 tab
    check("重对齐后第二行为 \\t\\treturn 42", "\t\treturn 42" in realigned, f"got {realigned!r}")
    check("重对齐后含 return 42", "return 42" in realigned, f"got {realigned!r}")


# ═══════════════════════════════════════════
# 用例 4：old_string 漏一个空行 -> L4 成功
# ═══════════════════════════════════════════
print("\n用例 4：漏空行 -> L4 模糊匹配成功")
content4 = "line1\nline2\n\nline3\nline4\n"
old4 = "line1\nline2\nline3\nline4"
new4 = "line1\nline2\nline3\nline4_REPLACED"
status4, spans4, new_texts4, info4 = _locate_edit(content4, old4, new4, False)
check("status ∈ {'fuzzy', 'normalized'}", status4 in ("fuzzy", "normalized"), f"got {status4}")
check("spans 唯一", len(spans4) == 1, f"got {len(spans4)}")
check("命中行号合理", info4[1] == [1], f"got {info4[1]}")


# ═══════════════════════════════════════════
# 用例 5：old_string 完全对不上 -> 失败信息含文件真实最近片段 + 行号
# ═══════════════════════════════════════════
print("\n用例 5：完全不匹配 -> 自纠反馈")
content5 = "aaa\nbbb\nccc\nddd\neee\n"
old5 = "XYZ DOES NOT EXIST AT ALL ZYX"
new5 = "irrelevant"
status5, spans5, new_texts5, info5 = _locate_edit(content5, old5, new5, False)
check("status == 'none'", status5 == "none", f"got {status5}")
desc5 = info5[0]
check("反馈含 '失败'", "失败" in desc5, f"got {desc5[:80]}")
check("反馈含行号 '第'", "第" in desc5, f"got {desc5[:80]}")
check("反馈含 '请直接复制'", "请直接复制" in desc5, f"got {desc5[:80]}")


# ═══════════════════════════════════════════
# 用例 6：两处等价命中且 replace_all=False -> 返回候选行号
# ═══════════════════════════════════════════
print("\n用例 6：两处精确命中 + replace_all=False -> multi")
content6 = "aaa\ntarget\nbbb\ntarget\nccc\n"
old6 = "target"
new6 = "REPLACED"
status6, spans6, new_texts6, info6 = _locate_edit(content6, old6, new6, False)
check("status == 'multi'", status6 == "multi", f"got {status6}")
desc6 = info6[0]
check("描述含 '2 处' 或 '2'", "2" in desc6, f"got {desc6}")
line_nos6 = info6[1]
check("返回行号 2 和 4", sorted(line_nos6) == [2, 4], f"got {line_nos6}")


# ═══════════════════════════════════════════
# 用例 7：replace_all=True 精确多处 -> 全替换（回归不破）
# ═══════════════════════════════════════════
print("\n用例 7：replace_all=True 精确多处 -> 全替换")
content7 = "aaa\ntarget\nbbb\ntarget\nccc\n"
old7 = "target"
new7 = "REPLACED"
status7, spans7, new_texts7, info7 = _locate_edit(content7, old7, new7, replace_all=True)
check("status == 'exact'", status7 == "exact", f"got {status7}")
check("spans 有 2 处", len(spans7) == 2, f"got {len(spans7)}")
if len(spans7) == 2:
    check("span1 内容 == target", content7[spans7[0][0]:spans7[0][1]] == "target")
    check("span2 内容 == target", content7[spans7[1][0]:spans7[1][1]] == "target")
check("new_texts 全是 new7", all(t == new7 for t in new_texts7), f"got {new_texts7}")


# ═══════════════════════════════════════════
# 用例 8：L3 深嵌套 + tab/空格混用（Fix 1 暴露）
# ═══════════════════════════════════════════
print("\n用例 8：L3 深嵌套+tab/空格混用 -> 前缀替换")
# 原文件：tab 缩进，3 层嵌套
content8 = "line1\n\tdef foo():\n\t\tif x:\n\t\t\treturn 42\nline5\n"
# old_string：4 空格（与文件不同，L1 不命中；strip 后匹配 L3）
old8 = "    def foo():\n        if x:\n            return 42"
# new_string：4 空格，加一层嵌套（共 3 层）
new8 = "    def bar():\n        if x:\n            if y:\n                return 99"
status8, spans8, new_texts8, info8 = _locate_edit(content8, old8, new8, False)
check("用例8 status == 'normalized'", status8 == "normalized", f"got {status8}")
if new_texts8:
    realigned8 = new_texts8[0]
    lines8 = realigned8.splitlines()
    # level 乘法：model_unit=4空格, file_unit=tab
    check("用例8 首行 '\\tdef bar():'", lines8[0] == "\tdef bar():", f"got {lines8[0]!r}")
    check("用例8 第二行 '\\t\\tif x:'", lines8[1] == "\t\tif x:", f"got {lines8[1]!r}")
    check("用例8 第三行 '\\t\\t\\tif y:'", lines8[2] == "\t\t\tif y:", f"got {lines8[2]!r}")
    check("用例8 第四行 '\\t\\t\\t\\treturn 99'", lines8[3] == "\t\t\t\treturn 99", f"got {lines8[3]!r}")
else:
    check("用例8 new_texts 非空", False, "new_texts 为空")


# ═══════════════════════════════════════════
# 用例 9：model_indent 为空 -> 内部缩进保留（Fix 2 暴露）
# ═══════════════════════════════════════════
print("\n用例 9：_realign_indent model_indent='' -> 内部缩进保留")
# 直接测 _realign_indent：model_indent='' 时不应剥掉任何缩进
new9_input = "def foo():\n    return 2"
result9 = _realign_indent(new9_input, "    ", "")
check("用例9 model_indent='' 原样返回", result9 == new9_input, f"got {result9!r}")
# 另一个：file_indent 任意，model_indent='' 都不该改
result9b = _realign_indent("    if x:\n        return 1", "\t", "")
check("用例9 model_indent='' 不改缩进", result9b == "    if x:\n        return 1", f"got {result9b!r}")


# ═══════════════════════════════════════════
# 用例 10：深层嵌套保留（Fix 1 副产品）
# ═══════════════════════════════════════════
print("\n用例 10：深层嵌套缩进保留 -> _realign_indent 单元测试")
# model_indent=4空格, file_indent=tab
# new_string: 第一行4空格, 第二行4+4=8空格, 第三行4+4+4=12空格
new10 = "    if x:\n        if y:\n            return 2"
result10 = _realign_indent(new10, "\t", "    ")
lines10 = result10.splitlines()
check("用例10 第一行 '\\tif x:'", lines10[0] == "\tif x:", f"got {lines10[0]!r}")
check("用例10 第二行 '\\t\\tif y:'", lines10[1] == "\t\tif y:", f"got {lines10[1]!r}")
check("用例10 第三行 '\\t\\t\\treturn 2'", lines10[2] == "\t\t\treturn 2", f"got {lines10[2]!r}")


# ═══════════════════════════════════════════
# 用例 11：大文件性能 sanity check（Fix 3）
# ═══════════════════════════════════════════
print("\n用例 11：大文件 5000 行性能 < 200ms")
# 构造 5000 行文件
big_lines = [f"line_{i:04d} = {i * 37 % 999}\n" for i in range(5000)]
content11 = "".join(big_lines)
old11 = "line_2500 = 592"
new11 = "line_2500 = 999"
t0 = time.perf_counter()
status11, spans11, new_texts11, info11 = _locate_edit(content11, old11, new11, False)
elapsed = (time.perf_counter() - t0) * 1000
check("用例11 status == 'exact'", status11 == "exact", f"got {status11}")
check(f"用例11 耗时 {elapsed:.0f}ms < 200ms", elapsed < 200, f"took {elapsed:.0f}ms")


# ═══════════════════════════════════════════
# 用例 12：顶级 class + tab/空格混用（冒烟测试 A4 暴露的 bug）
# ═══════════════════════════════════════════
print("\n用例 12：顶级 class + tab/空格混用 -> L3 + indent unit 推断")
file_content_12 = "class Foo:\n    def bar(self):\n        return 1\n"  # 文件 4 空格
old12 = "class Foo:\n\tdef bar(self):\n\t\treturn 1\n"                   # 模型 tab
new12 = "class Foo:\n\tdef bar(self):\n\t\treturn 2\n"                   # 模型 tab

status12, spans12, new_texts12, info12 = _locate_edit(file_content_12, old12, new12, False)
match_desc12, line_nos12 = info12

check("用例12 status == 'normalized'(L3)", status12 == "normalized", f"got {status12}")
check("用例12 描述含 L3", "L3" in match_desc12, f"got {match_desc12}")
# 关键：重对齐后的新内容应该是 file 风格（4 空格），不是 model 风格（tab）
assert spans12, "应有 spans"
realigned12 = new_texts12[0]
check("用例12 重对齐后第二行是 '    def bar(self):'",
       "    def bar(self):" in realigned12, f"got {realigned12!r}")
check("用例12 重对齐后第三行是 '        return 2'（8 空格）",
       "        return 2" in realigned12, f"got {realigned12!r}")
check("用例12 重对齐后不再含 tab", "\t" not in realigned12, f"got {realigned12!r}")


# ═══════════════════════════════════════════
# 汇总
# ═══════════════════════════════════════════
print(f"\n{'='*50}")
total = PASS + FAIL
print(f"Total {total} checks: PASS={PASS} FAIL={FAIL}")
if FAIL > 0:
    sys.exit(1)
else:
    print("All passed!")
