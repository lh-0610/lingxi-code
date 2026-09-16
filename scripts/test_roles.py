"""角色提示词与项目规则注入测试。"""
import src.roles as roles
from src import state


class TestLingxiRules:
    def test_missing_file_returns_empty(self, tmp_path):
        assert roles._load_lingxirules(str(tmp_path)) == ""

    def test_truncates_oversized_file(self, tmp_path):
        """兼容包装仍按单来源展示预算截断（B01 后该上限从 20000 提到 40000）。"""
        (tmp_path / ".lingxirules").write_text("x" * (roles._SINGLE_RULE_MAX + 1), encoding="utf-8")

        result = roles._load_lingxirules(str(tmp_path))

        assert result.startswith("x" * roles._SINGLE_RULE_MAX)
        assert f"已截断至前 {roles._SINGLE_RULE_MAX} 字" in result


class TestRoleNames:
    def test_extracts_name_from_heading_suffix(self):
        assert roles._extract_character_name("# 灵犀助手 · 小夏", "fallback") == "小夏"

    def test_extracts_name_from_brackets(self):
        assert roles._extract_character_name("角色名：「小夏」", "fallback") == "小夏"

    def test_falls_back_when_name_is_missing(self):
        assert roles._extract_character_name("普通角色说明", "fallback") == "fallback"


class TestSystemPrompt:
    def test_injects_project_rules_and_plan_mode(
        self, isolated_memory, tmp_path, monkeypatch,
    ):
        (tmp_path / ".lingxirules").write_text("必须运行 pytest", encoding="utf-8")
        monkeypatch.setattr(state, "current_project", str(tmp_path))
        monkeypatch.setattr(state, "agent_mode", "plan")

        result = roles.get_system_prompt()

        assert f"`{tmp_path}`" in result
        assert "必须运行 pytest" in result
        # Plan 提示已挪出 system prompt（它每次切模式都变，会打掉 prompt cache），
        # 现在由 get_volatile_context() 追加到发送历史尾部。
        # 详见 test_prompt_cache_stability.py。
        assert "当前是 Plan" not in result
        assert "当前是 Plan" in roles.get_volatile_context()


class TestRoleSnapshot:
    """角色卡会话快照：一轮生成途中前台换卡，不改变本会话本轮的人格。"""

    def test_capture_active_role_reads_globals(self, monkeypatch):
        monkeypatch.setattr(roles, "_role_card_content", "c")
        monkeypatch.setattr(roles, "_role_card_name", "n")
        monkeypatch.setattr(roles, "_role_card_path", "p")
        assert roles.capture_active_role() == {"content": "c", "name": "n", "path": "p"}

    def test_session_snapshot_overrides_global(self, isolated_memory, monkeypatch):
        """会话有快照时，get_system_prompt 用快照角色，无视全局当前角色。"""
        from src import session as _session
        monkeypatch.setattr(roles, "_role_card_content", "我是全局新角色")
        sess = _session.get_active()
        sess.role_snapshot = {"content": "我是快照旧角色", "name": "旧", "path": None}
        try:
            result = roles.get_system_prompt()
            assert "我是快照旧角色" in result
            assert "我是全局新角色" not in result
        finally:
            sess.role_snapshot = None

    def test_falls_back_to_global_without_snapshot(self, isolated_memory, monkeypatch):
        """会话无快照（空闲）时回退读全局——前台换卡下一轮即生效。"""
        from src import session as _session
        monkeypatch.setattr(roles, "_role_card_content", "我是全局角色")
        _session.get_active().role_snapshot = None
        assert "我是全局角色" in roles.get_system_prompt()

    def test_snapshot_with_none_content_yields_default(self, isolated_memory, monkeypatch):
        """拍快照时本无角色卡（content=None）→ 用默认 prompt，不串全局后设的卡。"""
        from src import session as _session
        monkeypatch.setattr(roles, "_role_card_content", "全局后来才设的卡")
        sess = _session.get_active()
        sess.role_snapshot = {"content": None, "name": None, "path": None}
        try:
            result = roles.get_system_prompt()
            assert "全局后来才设的卡" not in result
            assert "角色设定（必须严格遵守）" not in result
        finally:
            sess.role_snapshot = None


class TestLoadRulesFromDir:
    """_load_rules_from_dir：单目录规则文件加载。"""

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        """目录不存在时返回空列表。"""
        result = roles._load_rules_from_dir(str(tmp_path / "nonexistent"))
        assert result == []

    def test_reads_lingxirules(self, tmp_path):
        """能读取 .lingxirules 文件。"""
        (tmp_path / ".lingxirules").write_text("项目规则", encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        assert len(result) == 1
        assert result[0] == (".lingxirules", "项目规则")

    def test_reads_claude_md_and_agents_md(self, tmp_path):
        """能读取 CLAUDE.md 和 AGENTS.md。"""
        (tmp_path / "CLAUDE.md").write_text("claude 指令", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("agents 指令", encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        names = [n for n, _ in result]
        assert "CLAUDE.md" in names
        assert "AGENTS.md" in names

    def test_respects_filename_order(self, tmp_path):
        """结果按 CLAUDE.md < AGENTS.md < .lingxirules 顺序排列。"""
        (tmp_path / ".lingxirules").write_text("a", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("b", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("c", encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        names = [n for n, _ in result]
        assert names == ["CLAUDE.md", "AGENTS.md", ".lingxirules"]

    def test_skips_empty_files_after_strip(self, tmp_path):
        """文件内容全空白时跳过。"""
        (tmp_path / ".lingxirules").write_text("   \n  ", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("有内容", encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        assert len(result) == 1
        assert result[0][0] == "CLAUDE.md"

    def test_truncates_oversized_file(self, tmp_path):
        """超长文件截断并附提示（兼容包装；新代码走 render_rule_sources）。"""
        big = "y" * (roles._SINGLE_RULE_MAX + 1)
        (tmp_path / ".lingxirules").write_text(big, encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        assert len(result) == 1
        content = result[0][1]
        assert content.startswith("y" * roles._SINGLE_RULE_MAX)
        assert f"已截断至前 {roles._SINGLE_RULE_MAX} 字符" in content

    def test_handles_read_failure_gracefully(self, tmp_path, monkeypatch):
        """读取失败时跳过该文件，不影响其它文件。"""
        (tmp_path / ".lingxirules").write_text("ok", encoding="utf-8")
        # 模拟 CLAUDE.md 读取失败
        original_open = open
        def mock_open(path, *args, **kwargs):
            if str(path).endswith("CLAUDE.md"):
                raise PermissionError("mocked")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr("builtins.open", mock_open)
        (tmp_path / "CLAUDE.md").write_text("fail", encoding="utf-8")
        result = roles._load_rules_from_dir(str(tmp_path))
        # .lingxirules 正常读取，CLAUDE.md 被跳过
        assert any(n == ".lingxirules" for n, _ in result)
        assert not any(n == "CLAUDE.md" for n, _ in result)


class TestLoadProjectRules:
    """load_project_rules：分层规则加载。"""

    def test_only_root_rules_when_no_target(self, tmp_path):
        """target_path=None 时只加载项目根目录规则。"""
        (tmp_path / ".lingxirules").write_text("根规则", encoding="utf-8")
        result = roles.load_project_rules(str(tmp_path))
        assert "根规则" in result

    def test_loads_rules_along_path(self, tmp_path):
        """从项目根到目标目录沿途的所有规则都被加载。"""
        (tmp_path / ".lingxirules").write_text("根规则", encoding="utf-8")
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / ".lingxirules").write_text("子目录规则", encoding="utf-8")
        result = roles.load_project_rules(str(tmp_path), target_path=str(sub))
        assert "根规则" in result
        assert "子目录规则" in result

    def test_outside_root_returns_empty(self, tmp_path):
        """target_path 指向项目根之外时返回空字符串（安全边界）。"""
        outside = tmp_path.parent / "outside_project"
        outside.mkdir(exist_ok=True)
        result = roles.load_project_rules(str(tmp_path), target_path=str(outside))
        assert result == ""

    def test_merges_order_root_to_deep(self, tmp_path):
        """合并顺序：根 → 子目录，越靠近目标优先级越高（出现在后面）。"""
        (tmp_path / "CLAUDE.md").write_text("根指令", encoding="utf-8")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("子指令", encoding="utf-8")
        result = roles.load_project_rules(str(tmp_path), target_path=str(sub))
        root_pos = result.index("根指令")
        sub_pos = result.index("子指令")
        assert root_pos < sub_pos

    def test_sibling_rules_do_not_leak(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()
        (left / "AGENTS.md").write_text("left-only", encoding="utf-8")
        (right / "AGENTS.md").write_text("right-only", encoding="utf-8")

        result = roles.load_project_rules(str(tmp_path), str(left))

        assert "left-only" in result
        assert "right-only" not in result

    def test_combined_rules_over_budget_stay_within_budget(self, tmp_path):
        """超预算时不再盲截尾巴，而是给出标注了省略范围的节选，且守住预算。

        旧行为是把拼接结果一刀切在 40000 字符——切点之后的来源既不在输出里，也不在
        任何清单里，没人知道少了什么。现在每个来源都必须留下痕迹。
        """
        (tmp_path / "CLAUDE.md").write_text("甲" * 30000, encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("乙" * 30000, encoding="utf-8")
        (tmp_path / ".lingxirules").write_text("丙" * 30000, encoding="utf-8")

        result = roles.load_project_rules(str(tmp_path))

        assert "尚未完整读取" in result                      # 明确标注不完整
        assert len(result) <= roles._COMBINED_RULES_MAX
        for rel in ("CLAUDE.md", "AGENTS.md", ".lingxirules"):
            assert f"## 来源：{rel}" in result               # 没有来源被整个吞掉
        assert "省略第" in result and "补读" in result        # 省略范围与补读入口都在
        # 节选必须是原文，不是占位符
        assert "甲" in result and "乙" in result and "丙" in result


class TestLoadProjectRulesWithSources:
    """load_project_rules_with_sources：带来源标签的规则加载。"""

    def test_returns_source_labels(self, tmp_path):
        """返回列表中每个元素为 (source_label, content) 元组。"""
        (tmp_path / ".lingxirules").write_text("规则内容", encoding="utf-8")
        result = roles.load_project_rules_with_sources(str(tmp_path))
        assert len(result) == 1
        label, content = result[0]
        assert label == ".lingxirules"
        assert "规则内容" in content

    def test_layered_sources(self, tmp_path):
        """分层规则带相对路径来源标签。"""
        (tmp_path / "CLAUDE.md").write_text("根指令", encoding="utf-8")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / ".lingxirules").write_text("pkg规则", encoding="utf-8")
        result = roles.load_project_rules_with_sources(str(tmp_path), target_path=str(sub))
        labels = [label for label, _ in result]
        assert "CLAUDE.md" in labels
        assert "pkg/.lingxirules" in labels

    def test_nonexistent_project_root(self, tmp_path):
        """项目根目录不存在时返回空列表。"""
        result = roles.load_project_rules_with_sources(str(tmp_path / "nope"))
        assert result == []


class TestSystemPromptLayered:
    """get_system_prompt 分层规则注入场景。"""

    def test_injects_layered_rules_format(self, isolated_memory, tmp_path, monkeypatch):
        """当有 CLAUDE.md 存在时，使用分层格式注入（非旧 .lingxirules 格式）。"""
        (tmp_path / "CLAUDE.md").write_text("claude 全局指令", encoding="utf-8")
        (tmp_path / ".lingxirules").write_text("lingxi 指令", encoding="utf-8")
        monkeypatch.setattr(state, "current_project", str(tmp_path))
        monkeypatch.setattr(state, "agent_mode", "act")

        result = roles.get_system_prompt()

        # 应使用分层格式标题（非旧格式标题）
        assert "来自 CLAUDE.md / AGENTS.md / .lingxirules" in result
        assert "claude 全局指令" in result
        assert "lingxi 指令" in result
