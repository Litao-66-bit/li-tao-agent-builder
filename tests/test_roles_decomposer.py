"""Decomposer 分解器角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：拆分 / 去重 / 依赖分组 / 歧义检查
- 边界：空参数 / 格式错误 / id 重复
- 授权：decomposer 角色权限矩阵
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.roles.decomposer import (
    MAX_STEPS_BEFORE_GROUP,
    Decomposer,
    DecomposeResult,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_decomposer() -> Decomposer:
    return Decomposer(correlation_id="c-test")


# ── 功能 ────────────────────────────────────────────────────────


class TestDecomposerFunctional:
    def test_no_raw_steps_returns_pending(self):
        """无预拆分 → 返回空 DAG + 待确认。"""
        d = _make_decomposer()
        result = d.decompose("t-001", "帮我写个函数")
        assert isinstance(result, DecomposeResult)
        assert result.steps == {}
        assert result.order == []
        assert len(result.pending_questions) > 0
        assert "LLM" in result.pending_questions[0]

    def test_decompose_basic(self):
        """有预拆分 → 返回步骤 DAG。"""
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "web_search", "inputs": {"query": "python"}},
            {"id": "s2", "action": "file_write", "inputs": {"path": "/x.py"}, "depends_on": ["s1"]},
        ]
        result = d.decompose("t-001", "写函数", raw_steps=raw)
        assert len(result.steps) == 2
        assert result.order == ["s1", "s2"]
        assert result.steps["s1"].action == "web_search"
        assert result.steps["s2"].depends_on == ["s1"]
        assert result.pending_questions == []

    def test_deduplicate(self):
        """相同 action + 相同 inputs 的步骤合并。"""
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "web_search", "inputs": {"query": "python"}},
            {"id": "s2", "action": "web_search", "inputs": {"query": "python"}},  # 重复
            {"id": "s3", "action": "file_write", "inputs": {"path": "/x.py"}},
        ]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        assert len(result.steps) == 2  # s2 被合并
        assert "s1" in result.steps
        assert "s3" in result.steps

    def test_dependency_grouping(self):
        """拓扑排序分组：无依赖→第一组，依赖第一组→第二组。"""
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "a", "depends_on": []},
            {"id": "s2", "action": "b", "depends_on": []},
            {"id": "s3", "action": "c", "depends_on": ["s1"]},
            {"id": "s4", "action": "d", "depends_on": ["s2", "s3"]},
        ]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        assert result.parallel_groups[0] == ["s1", "s2"]  # 第一层
        assert result.parallel_groups[1] == ["s3"]  # 第二层
        assert result.parallel_groups[2] == ["s4"]  # 第三层

    def test_circular_dependency_grouped_together(self):
        """环依赖 → 环中步骤归入同一组（不死锁）。"""
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "a", "depends_on": ["s2"]},
            {"id": "s2", "action": "b", "depends_on": ["s1"]},
        ]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        # 环中步骤归入同一组
        assert len(result.parallel_groups) >= 1
        group = result.parallel_groups[-1]
        assert "s1" in group and "s2" in group

    def test_ambiguity_detected(self):
        """依赖引用了未定义的步骤 → pending_questions。"""
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "a", "depends_on": ["s_ghost"]},  # s_ghost 不存在
        ]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        assert len(result.pending_questions) == 1
        assert "s_ghost" in result.pending_questions[0]

    def test_optional_fields_auto_filled(self):
        """inputs / depends_on 未提供时自动补全。"""
        d = _make_decomposer()
        raw = [{"id": "s1", "action": "a"}]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        assert result.steps["s1"].inputs == {}
        assert result.steps["s1"].depends_on == []

    def test_to_dict(self):
        """to_dict 输出正确。"""
        d = _make_decomposer()
        raw = [{"id": "s1", "action": "a"}]
        result = d.decompose("t-001", "需求", raw_steps=raw)
        d2 = result.to_dict()
        assert d2["task_id"] == "t-001"
        assert "s1" in d2["steps"]
        assert d2["steps"]["s1"]["action"] == "a"
        assert d2["order"] == ["s1"]


# ── 边界 ────────────────────────────────────────────────────────


class TestDecomposerEdge:
    def test_empty_task_id(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("", "需求")
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_requirement(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "")
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_step_missing_id(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "需求", raw_steps=[{"action": "a"}])
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_step_missing_action(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "需求", raw_steps=[{"id": "s1"}])
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_duplicate_id(self):
        d = _make_decomposer()
        raw = [
            {"id": "s1", "action": "a"},
            {"id": "s1", "action": "b"},
        ]
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "需求", raw_steps=raw)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_raw_steps_not_list(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "需求", raw_steps="not a list")  # type: ignore[arg-type]
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_step_not_dict(self):
        d = _make_decomposer()
        with pytest.raises(AgentError) as exc_info:
            d.decompose("t-001", "需求", raw_steps=["not a dict"])  # type: ignore[list-item]
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 授权 ────────────────────────────────────────────────────────


class TestDecomposerPermissions:
    def test_role_registered(self):
        assert "decomposer" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["decomposer"]
        assert set(perm.allowed_tools) == {
            "config_read",
            "memory_read",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["decomposer"]
        assert perm.high_risk_tools == []

    def test_no_execution_tools(self):
        """decomposer 不可直接执行。"""
        perm = DEFAULT_ROLE_PERMS["decomposer"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestDecomposerConstants:
    def test_max_steps_before_group_positive(self):
        assert MAX_STEPS_BEFORE_GROUP > 0
