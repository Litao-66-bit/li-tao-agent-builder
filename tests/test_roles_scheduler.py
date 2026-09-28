"""Scheduler 调度器角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：调度/环依赖/资源上限/并行收益/失败预案
- 边界：空参数
- 授权：scheduler 角色权限矩阵
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.roles.decomposer import Decomposer, DecomposeResult
from agent_builder.roles.scheduler import (
    DEFAULT_FALLBACK,
    FALLBACK_POLICY,
    MAX_PARALLEL,
    QUICK_ACTIONS,
    Scheduler,
    ScheduleResult,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_scheduler() -> Scheduler:
    return Scheduler(correlation_id="c-test")


def _make_decompose_result(raw_steps: list[dict]) -> DecomposeResult:
    """用 Decomposer 生成 DecomposeResult。"""
    d = Decomposer(correlation_id="c-test")
    return d.decompose("t-001", "需求", raw_steps=raw_steps)


# ── 功能 ────────────────────────────────────────────────────────


class TestSchedulerFunctional:
    def test_schedule_basic(self):
        """步骤 DAG → 执行计划。"""
        s = _make_scheduler()
        dr = _make_decompose_result([
            {"id": "s1", "action": "web_search", "inputs": {"query": "x"}},
            {"id": "s2", "action": "file_write", "inputs": {"path": "/x"}, "depends_on": ["s1"]},
        ])
        result = s.schedule("t-001", dr)
        assert isinstance(result, ScheduleResult)
        assert isinstance(result.plan, Plan)
        assert result.pending_questions == []
        assert "s1" in result.plan.order
        assert "s2" in result.plan.order
        assert result.plan.confirmed_by_user is False

    def test_parallel_groups(self):
        """无依赖步骤分组为并行批次。"""
        s = _make_scheduler()
        dr = _make_decompose_result([
            {"id": "s1", "action": "web_search", "depends_on": []},
            {"id": "s2", "action": "sandbox_run", "depends_on": []},
            {"id": "s3", "action": "file_write", "depends_on": ["s1", "s2"]},
        ])
        result = s.schedule("t-001", dr)
        # s1, s2 无依赖 → 第一组（并行）
        assert "s1" in result.plan.parallel_groups[0]
        assert "s2" in result.plan.parallel_groups[0]
        # s3 依赖 s1+s2 → 第二组
        assert result.plan.parallel_groups[1] == ["s3"]

    def test_cycle_detection(self):
        """环依赖 → 返回修正请求。"""
        s = _make_scheduler()
        # 手动构造环依赖（Decomposer 的去重/分组不处理环）
        steps = {
            "s1": Step(id="s1", action="a", depends_on=["s2"]),
            "s2": Step(id="s2", action="b", depends_on=["s1"]),
        }
        dr = DecomposeResult(
            task_id="t-001",
            steps=steps,
            order=["s1", "s2"],
            parallel_groups=[["s1", "s2"]],
            pending_questions=[],
        )
        result = s.schedule("t-001", dr)
        assert len(result.pending_questions) > 0
        assert "环依赖" in result.pending_questions[0]
        assert result.plan.order == []  # 空计划

    def test_resource_limit_clip(self):
        """资源上限裁剪：超过 MAX_PARALLEL 的步骤裁剪到 MAX_PARALLEL。"""
        s = _make_scheduler()
        # 构造 6 个无依赖步骤（超过 MAX_PARALLEL=4）
        raw = [{"id": f"s{i}", "action": "web_search", "depends_on": []} for i in range(6)]
        dr = _make_decompose_result(raw)
        result = s.schedule("t-001", dr)
        # 第一组不超过 MAX_PARALLEL
        assert len(result.plan.parallel_groups[0]) <= MAX_PARALLEL

    def test_parallel_to_sequential_for_quick_actions(self):
        """2 个快速操作 → 改顺序（并行收益 < 协调成本）。"""
        s = _make_scheduler()
        # file_read + code_search 都是 QUICK_ACTIONS
        dr = _make_decompose_result([
            {"id": "s1", "action": "file_read", "depends_on": []},
            {"id": "s2", "action": "code_search", "depends_on": []},
        ])
        result = s.schedule("t-001", dr)
        # 改为顺序：2 个单步组
        assert len(result.plan.parallel_groups) == 2
        assert result.plan.parallel_groups[0] == ["s1"]
        assert result.plan.parallel_groups[1] == ["s2"]

    def test_keep_parallel_for_non_quick(self):
        """非快速操作保持并行。"""
        s = _make_scheduler()
        # web_search + sandbox_run 不是快速操作
        dr = _make_decompose_result([
            {"id": "s1", "action": "web_search", "depends_on": []},
            {"id": "s2", "action": "sandbox_run", "depends_on": []},
        ])
        result = s.schedule("t-001", dr)
        # 保持并行：1 个 2 步组
        assert len(result.plan.parallel_groups) == 1
        assert len(result.plan.parallel_groups[0]) == 2

    def test_fallback_policy(self):
        """失败预案：不同 action 映射到 skip/retry。"""
        s = _make_scheduler()
        dr = _make_decompose_result([
            {"id": "s1", "action": "web_search"},
            {"id": "s2", "action": "file_write"},
            {"id": "s3", "action": "test_run"},
            {"id": "s4", "action": "sandbox_run"},
        ])
        result = s.schedule("t-001", dr)
        assert result.plan.fallback["s1"]["action"] == "retry"
        assert result.plan.fallback["s2"]["action"] == "skip"
        assert result.plan.fallback["s3"]["action"] == "skip"
        assert result.plan.fallback["s4"]["action"] == "retry"

    def test_unknown_action_default_fallback(self):
        """未知 action → 默认 fallback。"""
        s = _make_scheduler()
        dr = _make_decompose_result([
            {"id": "s1", "action": "unknown_action"},
        ])
        result = s.schedule("t-001", dr)
        assert result.plan.fallback["s1"]["action"] == DEFAULT_FALLBACK["action"]

    def test_no_steps_returns_pending(self):
        """无步骤 → 返回待确认。"""
        s = _make_scheduler()
        dr = DecomposeResult(
            task_id="t-001",
            steps={},
            order=[],
            parallel_groups=[],
            pending_questions=["需要 LLM 拆分"],
        )
        result = s.schedule("t-001", dr)
        assert len(result.pending_questions) > 0
        assert result.plan.order == []

    def test_order_is_topological(self):
        """order 是拓扑排序的扁平化。"""
        s = _make_scheduler()
        dr = _make_decompose_result([
            {"id": "s1", "action": "a", "depends_on": []},
            {"id": "s2", "action": "b", "depends_on": ["s1"]},
            {"id": "s3", "action": "c", "depends_on": ["s2"]},
        ])
        result = s.schedule("t-001", dr)
        # s1 在 s2 前，s2 在 s3 前
        assert result.plan.order.index("s1") < result.plan.order.index("s2")
        assert result.plan.order.index("s2") < result.plan.order.index("s3")


# ── 边界 ────────────────────────────────────────────────────────


class TestSchedulerEdge:
    def test_empty_task_id(self):
        s = _make_scheduler()
        dr = _make_decompose_result([{"id": "s1", "action": "a"}])
        with pytest.raises(AgentError) as exc_info:
            s.schedule("", dr)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_custom_max_parallel(self):
        """自定义资源上限。"""
        s = Scheduler(correlation_id="c-test", max_parallel=2)
        raw = [{"id": f"s{i}", "action": "web_search", "depends_on": []} for i in range(4)]
        dr = _make_decompose_result(raw)
        result = s.schedule("t-001", dr)
        assert len(result.plan.parallel_groups[0]) <= 2


# ── 授权 ────────────────────────────────────────────────────────


class TestSchedulerPermissions:
    def test_role_registered(self):
        assert "scheduler" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["scheduler"]
        assert set(perm.allowed_tools) == {
            "plan_validate",
            "config_read",
            "memory_read",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["scheduler"]
        assert perm.high_risk_tools == []

    def test_no_execution_tools(self):
        """scheduler 不可直接执行。"""
        perm = DEFAULT_ROLE_PERMS["scheduler"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestSchedulerConstants:
    def test_max_parallel_positive(self):
        assert MAX_PARALLEL > 0

    def test_quick_actions_nonempty(self):
        assert len(QUICK_ACTIONS) > 0
        assert "file_read" in QUICK_ACTIONS

    def test_fallback_policy_covers_write_ops(self):
        assert "file_write" in FALLBACK_POLICY
        assert FALLBACK_POLICY["file_write"]["action"] == "skip"

    def test_fallback_policy_covers_search(self):
        assert "web_search" in FALLBACK_POLICY
        assert FALLBACK_POLICY["web_search"]["action"] == "retry"

    def test_default_fallback_is_skip(self):
        assert DEFAULT_FALLBACK["action"] == "skip"
