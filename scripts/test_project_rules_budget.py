"""项目规则的预算渲染与原文分页（B01 / S0）。

盯住的核心性质：**信息完整可获取、遗漏可见**。
改之前是读的时候就按字符数一刀切，切点之后的章节既不在 prompt 里、也不在任何清单里——
模型不会去补读自己不知道存在的内容。本仓库的 CLAUDE.md 就这样丢了末尾整整 5 节。

注意本文件不断言"模型一定会去补读"。目录里写一句"请补读"不构成程序强制，
这里验收的是：缺什么看得见、缺的部分拿得到。
"""
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
