"""项目规则的预算渲染与原文分页（B01 / S0）。

盯住的核心性质：**信息完整可获取、遗漏可见**。
改之前是读的时候就按字符数一刀切，切点之后的章节既不在 prompt 里、也不在任何清单里——
模型不会去补读自己不知道存在的内容。本仓库的 CLAUDE.md 就这样丢了末尾整整 5 节。

注意本文件不断言"模型一定会去补读"。目录里写一句"请补读"不构成程序强制，
这里验收的是：缺什么看得见、缺的部分拿得到。
"""
import re

from src import roles, tools
from src.limits import TOOL_RESULT_HARD_CAP_CHARS


def _doc(title, sections, filler=200):
    """造一份带标题层级的 Markdown。"""
    out = [f"# {title}\n"]
    for name in sections:
        out.append(f"\n## {name}\n\n" + ("正文内容。" * filler) + "\n")
    return "".join(out)


# ══════════════════════════════════════════════════════════════
# 预算内：逐字不变
# ══════════════════════════════════════════════════════════════

def test_within_budget_returns_verbatim(tmp_path):
    """装得下就原样给全文，格式与重构前一致——绝大多数项目走这条路，不该有任何变化。"""
    (tmp_path / "CLAUDE.md").write_text("# 规则\n\n必须跑测试。\n", encoding="utf-8")
    out = roles.load_project_rules(str(tmp_path))
    assert out == "## 来源：CLAUDE.md\n# 规则\n\n必须跑测试。"
    assert "尚未完整读取" not in out


def test_real_claude_md_tail_sections_are_loaded(tmp_path):
    """回归本项目那个具体故障：末尾章节必须出现在加载结果里。

    27k 字符的 CLAUDE.md 曾被 20000 的上限砍掉尾部 5 节，其中包括
    "已知限制与有意取舍（避免 code review 反复重提）"——写它正是为了让模型读到。
    """
    body = "\n".join(f"## 第{i}节\n\n" + "内容。" * 300 for i in range(1, 9))
    tail = "\n## 已知限制与有意取舍\n\nFETCH_URL 的 DNS 重绑定残留已评估，勿重复提出。\n"
    (tmp_path / "CLAUDE.md").write_text("# 项目\n\n" + body + tail, encoding="utf-8")

    out = roles.load_project_rules(str(tmp_path))

    assert "已知限制与有意取舍" in out
    assert "勿重复提出" in out, "末尾正文必须真的在输出里，不能只剩标题"


# ══════════════════════════════════════════════════════════════
# 超预算：遗漏可见
# ══════════════════════════════════════════════════════════════

def test_over_budget_marks_itself_incomplete_and_keeps_every_source(tmp_path):
    """三个大文件合计超预算：每个来源都留下记录，总长守预算，省略范围可见。

    "后续来源因前一个文件太长而消失"是旧实现最隐蔽的失败——输出看起来完整，
    只是某个来源从来没出现过。
    """
    for name in ("CLAUDE.md", "AGENTS.md", ".lingxirules"):
        # 每份约 18k 字符，三份合计远超 _COMBINED_RULES_MAX
        (tmp_path / name).write_text(
            _doc(name, [f"{name}-节{i}" for i in range(6)], filler=600),
            encoding="utf-8")

    out = roles.load_project_rules(str(tmp_path))

    assert "尚未完整读取" in out
    assert len(out) <= roles._COMBINED_RULES_MAX
    for name in ("CLAUDE.md", "AGENTS.md", ".lingxirules"):
        assert f"## 来源：{name}" in out
        assert f"{name}-节0" in out, "每个来源的目录都要在，不能被前一个大文件挤掉"
    assert "省略第" in out and "补读" in out


def test_over_budget_keeps_full_heading_structure(tmp_path):
    """超预算时标题结构完整保留，省略区间内的标题标注 [未展示]。

    目录是"缺了什么"的唯一线索。只给正文节选而不给目录，等于换一种方式让人看不见遗漏。
    """
    sections = [f"章节{i}" for i in range(12)]
    (tmp_path / "CLAUDE.md").write_text(_doc("大文档", sections, filler=400), encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 6000)

    for name in sections:
        assert name in out, f"目录里必须能看到 {name}"
    assert "未展示" in out


def test_tail_content_is_not_assumed_unimportant(tmp_path):
    """节选取头也取尾：重要性不能靠位置判断。

    只取头部会把"结尾的内容一律次要"变成事实上的假设，而约定、已知取舍这类最该被遵守的
    条目恰恰常写在文末。
    """
    text = ("# 文档\n\n" + "开头段落。\n\n" * 400
            + "\n## 结尾约定\n\n提交前必须跑全量测试。\n")
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 4000)

    assert "提交前必须跑全量测试" in out, "尾部正文必须被选中，不能只给头部"


def test_read_failure_is_visible_not_silently_dropped(tmp_path, monkeypatch):
    """读取失败要显式报出来——"读不出来"和"没有规则"是两件事。"""
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    (tmp_path / ".lingxirules").write_text("y", encoding="utf-8")
    real_open = open

    def boom(path, *a, **kw):
        if str(path).endswith("CLAUDE.md"):
            raise PermissionError("mocked denial")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", boom)
    sources = roles.read_rule_sources(str(tmp_path))
    failed = [s for s in sources if s["error"]]
    assert len(failed) == 1 and failed[0]["rel"] == "CLAUDE.md"
    out = roles.render_rule_sources(sources, 500)
    assert "读取失败" in out


# ══════════════════════════════════════════════════════════════
# 代码围栏
# ══════════════════════════════════════════════════════════════

def test_headings_inside_code_fences_are_not_treated_as_sections(tmp_path):
    """围栏里的 `# 注释` 不是标题。

    当成标题会凭空造出目录项；更糟的是按它去切正文，会把代码块劈成两半。
    """
    text = ("# 真标题\n\n```bash\n# 这是注释不是标题\nls -la\n```\n\n## 另一个真标题\n\n正文\n")
    headings, fences = roles._scan_markdown_structure(text)
    titles = [h["title"] for h in headings]
    assert titles == ["真标题", "另一个真标题"]
    assert len(fences) == 1


def test_body_selection_never_cuts_inside_a_code_fence(tmp_path):
    """切点不许落在代码围栏内部——半截代码看起来像完整的，比少给一段更糟。"""
    fence = "```python\n" + "print('x')\n" * 200 + "```\n"
    text = "# 文档\n\n" + "前言。\n\n" * 50 + fence + "\n后记。\n"
    _, fences = roles._scan_markdown_structure(text)
    head, tail, omit_s, omit_e = roles._select_body(text, fences, 1200)
    for start, end in fences:
        assert not (start < omit_s < end), "省略起点切进了代码围栏"
        assert not (start < omit_e < end), "省略终点切进了代码围栏"


# ══════════════════════════════════════════════════════════════
# 目录本身超预算 → 退化为目录 + 分页入口
# ══════════════════════════════════════════════════════════════

def test_toc_over_budget_degrades_to_paged_toc(tmp_path):
    """极端多标题文件：仍守预算，并提示总数与分页入口。"""
    text = "".join(f"## 小节{i}\n\n内容\n\n" for i in range(800))
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 3000)

    assert len(out) <= 3000
    assert "目录仅展示部分" in out and "共 800 项" in out
    assert "分页" in out


# ══════════════════════════════════════════════════════════════
# 原文分页
# ══════════════════════════════════════════════════════════════

def test_pages_reassemble_verbatim_including_cjk_and_long_lines(tmp_path):
    """逐页拼接必须与原文逐字一致：中文与超长单行都成立。

    offset/limit 按 Unicode 字符计数而非字节——按字节切会把一个汉字劈成两半。
    超长单行则确保分页不依赖换行符存在。
    """
    text = "# 标题\n\n" + "中文内容测试。" * 900 + "\n" + ("x" * 9000) + "\n收尾。\n"
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")
    original = roles.read_rule_sources(str(tmp_path))[0]["text"]

    buf, offset, pages = "", 0, 0
    while True:
        page = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md",
                                           offset=offset, limit=4000)
        buf += page["text"]
        pages += 1
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
        assert pages < 100, "分页没有收敛"

    assert buf == original
    assert pages > 1


def test_page_limit_is_capped(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("z" * 50000, encoding="utf-8")
    page = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md",
                                       offset=0, limit=99999)
    assert len(page["text"]) == roles._RULE_PAGE_MAX


def test_source_change_during_paging_is_detectable(tmp_path):
    """翻页途中文件被改要能发现，否则会把两个版本拼成一份规则。"""
    p = tmp_path / "CLAUDE.md"
    p.write_text("# 旧\n\n" + "甲" * 9000, encoding="utf-8")
    first = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md", offset=0, limit=4000)

    p.write_text("# 新\n\n" + "乙" * 9000, encoding="utf-8")
    second = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md",
                                         offset=first["next_offset"], limit=4000)

    assert first["sha"] != second["sha"], "内容指纹必须变化，否则改动不可见"


def test_unknown_source_and_bad_offset_report_clearly(tmp_path):
    """来源非法 / 偏移越界要明确报错，不能伪装成"没有规则"。"""
    (tmp_path / "CLAUDE.md").write_text("内容", encoding="utf-8")
    bad = roles.read_rule_source_page(str(tmp_path), None, "不存在.md")
    assert bad["error"] == "unknown_source" and "CLAUDE.md" in bad["available"]

    oob = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md", offset=99999)
    assert oob["error"] == "bad_offset"


def test_paging_respects_path_scope(tmp_path):
    """分页的来源必须限定在该 path 适用的规则链内，不能借它读到作用域外的文件。"""
    (tmp_path / "CLAUDE.md").write_text("根规则", encoding="utf-8")
    left = tmp_path / "left"
    left.mkdir()
    (left / ".lingxirules").write_text("左规则", encoding="utf-8")
    right = tmp_path / "right"
    right.mkdir()
    (right / ".lingxirules").write_text("右规则", encoding="utf-8")

    page = roles.read_rule_source_page(str(tmp_path), str(left), "right/.lingxirules")
    assert page["error"] == "unknown_source"


# ══════════════════════════════════════════════════════════════
# 工具层
# ══════════════════════════════════════════════════════════════

def _call(**kwargs):
    return tools.get_project_instructions.invoke(kwargs)


def test_tool_overview_stays_under_tool_result_hard_cap(project_dir):
    """概览预算必须明显低于工具结果硬上限。

    超了会被发送层二次截断，而那次截断不认识我们的结构，会把省略说明和补读入口本身
    切掉——于是"标明了缺什么"这个唯一的补救也没了。
    """
    big = "".join(f"## 节{i}\n\n" + "内容。" * 500 + "\n" for i in range(20))
    (project_dir / "CLAUDE.md").write_text(big, encoding="utf-8")

    out = _call(path=".")

    assert len(out) <= roles._TOOL_OVERVIEW_MAX
    assert len(out) < TOOL_RESULT_HARD_CAP_CHARS
    assert "尚未完整读取" in out


def test_tool_page_with_max_limit_fits_hard_cap(project_dir):
    """最大页（12000）加上头尾元数据仍要能完整通过发送路径。"""
    (project_dir / "CLAUDE.md").write_text("字" * 40000, encoding="utf-8")

    out = _call(path=".", source="CLAUDE.md", offset=0, limit=roles._RULE_PAGE_MAX)

    assert len(out) < TOOL_RESULT_HARD_CAP_CHARS
    assert "内容指纹" in out and "续读" in out


def test_tool_paging_without_source_is_refused(project_dir):
    """不指定来源就分页只能对被裁过的概览分页，拼出来是残缺内容——明确拒绝，不猜。"""
    (project_dir / "CLAUDE.md").write_text("内容", encoding="utf-8")
    out = _call(path=".", offset=100)
    assert "需要指定 source" in out


def test_tool_reports_no_rules_distinctly(project_dir):
    out = _call(path=".")
    assert "没有适用的项目规则" in out


def test_tool_overview_lists_sources_usable_for_paging(project_dir):
    """概览里给出的来源名必须能直接拿去分页——否则"可补读"只是一句空话。"""
    big = "".join(f"## 节{i}\n\n" + "内容。" * 400 + "\n" for i in range(15))
    (project_dir / "CLAUDE.md").write_text(big, encoding="utf-8")
    (project_dir / ".lingxirules").write_text("简短规则", encoding="utf-8")

    overview = _call(path=".")
    assert "## 来源：CLAUDE.md" in overview

    page = _call(path=".", source="CLAUDE.md", offset=0, limit=2000)
    assert "节0" in page and "原文" in page

# ══════════════════════════════════════════════════════════════
# 330e110 复审补漏（五处）
# ══════════════════════════════════════════════════════════════

def test_many_short_headings_stay_within_budget(tmp_path):
    """目录的 [未展示] 标记是渲染完才加的，必须计入预算。

    复现参数：1000 个短标题、每节 40 字符，预算 20000 —— 旧实现输出 25468 字符，
    随后被发送层二次截断，把省略说明和补读入口本身切掉。
    """
    text = "".join(f"## H{i}\n\n" + "内容" * 20 + "\n\n" for i in range(1000))
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 20000)

    assert len(out) <= 20000, f"溢出 {len(out) - 20000} 字符"
    assert len(out) < TOOL_RESULT_HARD_CAP_CHARS


def test_long_toc_never_drops_later_sources(tmp_path):
    """目录很长时后面的来源不能消失。

    旧实现最后一步 out[:budget]，等于把"拼完砍尾巴"请了回来：三份规则只剩第一份，
    AGENTS.md 和 .lingxirules 的来源记录、目录总数提示一起没了。
    """
    # 标题足够长，第一个来源的目录就能吃掉整个预算——旧实现在这里 [:budget]，
    # 后两个来源连标题行都还没轮到就被砍掉了
    long_titles = "".join(
        f"## {'很长的标题段落用来撑满目录预算' * 8}{i}\n\n正文\n\n" for i in range(150))
    for name in ("CLAUDE.md", "AGENTS.md", ".lingxirules"):
        (tmp_path / name).write_text(f"# {name}\n\n" + long_titles, encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 8000)

    assert len(out) <= 8000
    for name in ("CLAUDE.md", "AGENTS.md", ".lingxirules"):
        assert f"## 来源：{name}" in out, f"{name} 的来源记录被砍掉了"
    assert "未列出" in out or "目录仅展示部分" in out, "被省略的部分要有交代"


def test_truncated_toc_is_retrievable_by_paging(tmp_path):
    """目录被截断时必须真的能翻回来——只写一句"还有 N 项"而没有取回入口，那是空话。"""
    text = "".join(f"## 小节{i}\n\n内容\n\n" for i in range(500))
    (tmp_path / "CLAUDE.md").write_text(text, encoding="utf-8")

    out = roles.render_rule_sources(roles.read_rule_sources(str(tmp_path)), 4000)
    assert "目录仅展示部分" in out and "toc=True" in out

    seen, offset, guard = [], 0, 0
    while True:
        page = roles.read_rule_toc_page(str(tmp_path), None, "CLAUDE.md",
                                        offset=offset, limit=120)
        seen.extend(h["title"] for h in page["headings"])
        guard += 1
        if page["next_offset"] is None or guard > 20:
            break
        offset = page["next_offset"]

    assert seen == [f"小节{i}" for i in range(500)], "目录分页必须能取回全部标题"


def test_raw_paging_preserves_leading_whitespace(tmp_path):
    """分页返回的必须是**原文**，不能被 strip 掉首尾空白。

    基准取写入文件的字符串本身，不取同一个读取函数的返回值——旧测试正是拿被 strip
    过的结果当原文比对，才让 55 字符只翻出 47 字符这种错误一路通过。
    首行缩进被吃掉还会改变 Markdown 语义（缩进代码块变成普通段落）。
    """
    written = "\n\n    # heading\n\nkeep leading and trailing blanks\n\n\n"
    (tmp_path / "CLAUDE.md").write_text(written, encoding="utf-8")

    buf, offset, guard = "", 0, 0
    while True:
        page = roles.read_rule_source_page(str(tmp_path), None, "CLAUDE.md",
                                           offset=offset, limit=16)
        buf += page["text"]
        guard += 1
        if page["next_offset"] is None or guard > 50:
            break
        offset = page["next_offset"]

    assert buf == written, f"分页结果与写入内容不一致：{len(buf)} vs {len(written)}"
    assert page["total"] == len(written)


def test_system_prompt_uses_the_budget_renderer_for_lone_lingxirules(
        isolated_memory, tmp_path, monkeypatch):
    """只有 .lingxirules 时，system prompt 也必须走预算渲染，而不是旧的截断拼接。

    这是最常见的项目布局。渲染器修好了而注入口没跟上，等于没修：尾部约定照样丢，
    而且连"哪里被省略、怎么补读"都没有。
    """
    from src import state
    body = "\n".join(f"## 第{i}节\n\n" + "内容。" * 400 for i in range(40))
    tail = "\n## 尾部约定\n\n提交前必须跑全量测试。\n"
    (tmp_path / ".lingxirules").write_text("# 规则\n\n" + body + tail, encoding="utf-8")
    monkeypatch.setattr(state, "current_project", str(tmp_path))

    prompt = roles.get_system_prompt()

    assert "尚未完整读取" in prompt, "超预算时 system prompt 必须自报不完整"
    assert "尾部约定" in prompt, "尾部章节至少要在目录里可见"
    assert "get_project_instructions" in prompt and "offset=" in prompt, "必须给出具体补读入口"


def test_system_prompt_surfaces_rule_read_errors(isolated_memory, tmp_path, monkeypatch):
    """全部规则读取失败时，system prompt 要报错，不能说成"这个项目没有规则"。"""
    from src import state
    (tmp_path / "CLAUDE.md").write_text("内容", encoding="utf-8")
    monkeypatch.setattr(state, "current_project", str(tmp_path))
    real_open = open

    def boom(path, *a, **kw):
        if str(path).endswith("CLAUDE.md"):
            raise PermissionError("mocked denial")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", boom)
    prompt = roles.get_system_prompt()

    assert "读取失败" in prompt and "mocked denial" in prompt


def test_four_backtick_fence_is_not_closed_by_inner_triple(tmp_path):
    """四反引号围栏不能被内部的三反引号提前关闭（CommonMark：闭栏不得短于开栏）。

    提前关闭会让示例里的 `# xxx` 被当成真标题，围栏区间也随之错位。
    """
    text = ("# REAL\n\n````markdown\n```\n# EXAMPLE_NOT_HEADING\n```\n````\n\n"
            "## REAL_TAIL\n\n正文\n")
    headings, fences = roles._scan_markdown_structure(text)

    assert [h["title"] for h in headings] == ["REAL", "REAL_TAIL"]
    assert len(fences) == 1, "四反引号块应当是一个整体"


def test_closing_fence_must_have_nothing_after_it(tmp_path):
    """行尾还有内容的就不是闭栏（```python 不关闭前一个围栏）。"""
    text = "# T\n\n```\n# inside\n```python\n# still inside\n```\n\n## AFTER\n"
    headings, fences = roles._scan_markdown_structure(text)

    assert [h["title"] for h in headings] == ["T", "AFTER"]
    assert len(fences) == 1


def test_tilde_fence_not_closed_by_backticks(tmp_path):
    """波浪号围栏只能被波浪号关闭。"""
    text = "# T\n\n~~~\n# inside\n```\n# also inside\n~~~\n\n## AFTER\n"
    headings, _ = roles._scan_markdown_structure(text)
    assert [h["title"] for h in headings] == ["T", "AFTER"]


def test_tool_toc_paging_round_trip(project_dir):
    """工具层目录分页：能翻、能续、越界报错。"""
    text = "".join(f"## 节{i}\n\n内容\n\n" for i in range(300))
    (project_dir / "CLAUDE.md").write_text(text, encoding="utf-8")

    first = _call(path=".", source="CLAUDE.md", toc=True, limit=100)
    assert "共 300 项" in first and "续读" in first and "节0" in first

    bad = _call(path=".", source="CLAUDE.md", toc=True, offset=9999)
    assert "越界" in bad and "个标题" in bad

    no_src = _call(path=".", toc=True)
    assert "需要指定 source" in no_src

# ══════════════════════════════════════════════════════════════
# bd3b1c0 复审：目录分页的字符预算
# ══════════════════════════════════════════════════════════════

def _through_send_layer(text):
    """把工具结果真的过一遍发送层的硬上限截断，返回模型实际收到的内容。

    不自己按 TOOL_RESULT_HARD_CAP_CHARS 复刻一份截断逻辑——复刻件和真货迟早会分叉，
    而分叉的那一刻正好就是"测试说没问题、线上却丢内容"的时刻。
    """
    from langchain_core.messages import ToolMessage
    from src import streaming

    msgs, _ = streaming._cap_oversized_tool_results(
        [ToolMessage(content=text, tool_call_id="t1")], budget=0)
    return msgs[0].content


def test_toc_page_survives_send_layer_with_long_headings(project_dir):
    """长标题目录逐页读取不能有遗漏。

    复现参数：150 个长标题、默认调用。旧实现只限条数不限字符，一页 39,713 字符，
    发送层砍掉中段后只剩 54 条可见，而 next_offset 仍报 120——模型照提示续读，
    中间 66 条静默消失。补读通道自己漏内容，比一开始就说"目录太长"更糟。
    """
    titles = [f"第{i:03d}节_" + "很长的标题用来撑爆单页字符预算" * 12 for i in range(150)]
    (project_dir / "CLAUDE.md").write_text(
        "".join(f"## {t}\n\n正文\n\n" for t in titles), encoding="utf-8")

    seen, offset, guard = [], 0, 0
    while True:
        raw = _call(path=".", source="CLAUDE.md", toc=True, offset=offset)
        assert len(raw) < TOOL_RESULT_HARD_CAP_CHARS, f"单页 {len(raw)} 字符，会被发送层截断"
        delivered = _through_send_layer(raw)
        assert delivered == raw, "整页必须原样送达，不能被发送层砍中段"

        for t in titles:
            if f"- {t}  (offset=" in delivered:
                seen.append(t)

        guard += 1
        assert guard < 60, "分页没有收敛"
        m = re.search(r"offset=(\d+)\)\]", delivered)
        if "续读" not in delivered:
            break
        assert m, "续读提示里必须给出下一页偏移"
        nxt = int(m.group(1))
        assert nxt > offset, "next_offset 必须前进，否则分页卡死"
        offset = nxt

    assert seen == titles, f"逐页读取有遗漏：拿到 {len(seen)} / {len(titles)} 条"


def test_toc_next_offset_tracks_actually_returned_count(tmp_path):
    """next_offset 按**实际返回条数**推进，不按请求的 limit。"""
    titles = ["很长的标题" * 60 + str(i) for i in range(80)]
    (tmp_path / "CLAUDE.md").write_text(
        "".join(f"## {t}\n\n正文\n\n" for t in titles), encoding="utf-8")

    page = roles.read_rule_toc_page(str(tmp_path), None, "CLAUDE.md", offset=0, limit=80)

    assert len(page["headings"]) < 80, "字符预算应当先于条数上限生效"
    assert page["next_offset"] == len(page["headings"])
    assert page["total"] == 80


def test_toc_page_respects_char_budget(tmp_path):
    titles = ["标题" * 80 + str(i) for i in range(200)]
    (tmp_path / "CLAUDE.md").write_text(
        "".join(f"## {t}\n\n正文\n\n" for t in titles), encoding="utf-8")

    page = roles.read_rule_toc_page(str(tmp_path), None, "CLAUDE.md",
                                    offset=0, limit=200, char_budget=2000)

    rendered = "\n".join(roles._toc_page_line(h) for h in page["headings"])
    assert len(rendered) <= 2000
    assert page["headings"], "预算再小也要至少返回一条，否则分页无法推进"


def test_single_overlong_heading_is_excerpted_and_still_advances(tmp_path):
    """单条标题就超预算时：节选 + 标注 + 保留原文偏移，分页仍要能往前走。

    不处理的话这一页永远放不下任何东西，next_offset 不前进，补读通道彻底卡死。
    """
    monster = "巨" * 5000
    (tmp_path / "CLAUDE.md").write_text(
        f"## {monster}\n\n正文\n\n## 后面一节\n\n正文\n", encoding="utf-8")

    page = roles.read_rule_toc_page(str(tmp_path), None, "CLAUDE.md",
                                    offset=0, limit=10, char_budget=300)

    assert len(page["headings"]) == 1
    h = page["headings"][0]
    assert h["truncated"] is True
    assert len(h["title"]) < len(monster)
    assert "标题已节选" in roles._toc_page_line(h)
    assert "offset=" in roles._toc_page_line(h), "要给出原文偏移，便于按 offset 读全称"
    assert page["next_offset"] == 1, "必须前进，否则分页卡死"
