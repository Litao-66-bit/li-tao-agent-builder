"""Router 路由者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：匹配执行者/执行/重派/超时/权限不足/类型不明
- 边界：未确认计划/空步骤
- 授权：router 角色权限矩阵
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError, permission_error, timeout_error
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.roles.router import (
    DEFAULT_EXECUTOR,
    EXECUTOR_MAP,
    MAX_RETRIES,
    Router,
    RouteResult,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_router() -> Router:
    return Router(correlation_id="c-test")


def _make_plan(steps: dict[str, Step], confirmed: bool = True) -> Plan:
    """构造已确认的 Plan。"""
    order = list(steps.keys())
    groups = [[sid] for sid in order]
    return Plan(
        task_id="t-001",
        order=order,
        parallel_groups=groups,
        fallback={},
        confirmed_by_user=confirmed,
    )


def _make_steps(n: int = 2) -> dict[str, Step]:
    """构造 n 个步骤。"""
    return {f"s{i+1}": Step(id=f"s{i+1}", action="web_search") for i in range(n)}


# ── 功能 ────────────────────────────────────────────────────────


class TestRouterFunctional:
    def test_route_match_only(self):
        """executor_fn=None → 只匹配执行者，不执行。"""
        r = _make_router()
        plan = _make_plan(_make_steps(2))
        steps = _make_steps(2)
        result = r.route(plan, steps, executor_fn=None)
        assert isinstance(result, RouteResult)
        assert len(result.results) == 2
        assert result.results["s1"].status == "pending"
        assert result.results["s1"].executor == "search_executor"
        assert result.pending_escalation == []

    def test_route_execute_success(self):
        """executor_fn 执行成功 → done。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)

        def fn(step: Step) -> str:
            return f"result_{step.id}"

        result = r.route(plan, steps, executor_fn=fn)
        assert result.results["s1"].status == "done"
        assert result.results["s1"].result == "result_s1"
        assert result.results["s1"].retries == 0
        assert result.all_done is True

    def test_route_retry_on_failure(self):
        """步骤失败 → 重派 1 次 → 仍失败 → 上报。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)
        call_count = 0

        def fn(step: Step) -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fails")

        result = r.route(plan, steps, executor_fn=fn)
        assert call_count == MAX_RETRIES + 1  # 初始 + 重派
        assert result.results["s1"].status == "failed"
        assert result.results["s1"].retries == MAX_RETRIES
        assert "s1" in result.pending_escalation

    def test_route_retry_then_success(self):
        """步骤失败 → 重派 → 成功。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)
        call_count = 0

        def fn(step: Step) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")
            return "ok"

        result = r.route(plan, steps, executor_fn=fn)
        assert call_count == 2
        assert result.results["s1"].status == "done"
        assert result.results["s1"].retries == 1
        assert result.all_done is True

    def test_route_timeout(self):
        """超时 → 标记失败重派 → 仍超时 → 上报。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)

        def fn(step: Step) -> str:
            raise TimeoutError("step timed out")

        result = r.route(plan, steps, executor_fn=fn)
        assert result.results["s1"].status == "failed"
        assert "超时" in result.results["s1"].error
        assert "s1" in result.pending_escalation

    def test_route_permission_denied(self):
        """权限不足 → 转发审批门（不重派）。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)

        def fn(step: Step) -> str:
            raise PermissionError("需要审批")

        result = r.route(plan, steps, executor_fn=fn)
        assert result.results["s1"].status == "pending_approval"
        assert "权限不足" in result.results["s1"].error
        assert "s1" in result.pending_approval
        assert result.all_done is False

    def test_executor_mapping(self):
        """action → 执行者类型映射。"""
        r = _make_router()
        test_cases = [
            ("web_search", "search_executor"),
            ("file_write", "code_executor"),
            ("code_search", "code_executor"),
            ("web_fetch", "doc_executor"),
            ("citation_check", "doc_executor"),
            ("data_query", "data_executor"),
            ("sandbox_run", "data_executor"),
            ("memory_write", "memory_executor"),
            ("git_commit", "sub_arch_executor"),
            ("unknown_action", DEFAULT_EXECUTOR),
        ]
        for action, expected_executor in test_cases:
            assert r._match_executor(action) == expected_executor

    def test_multiple_steps_mixed(self):
        """多步骤混合结果。"""
        r = _make_router()
        steps = {
            "s1": Step(id="s1", action="web_search"),
            "s2": Step(id="s2", action="file_write"),
            "s3": Step(id="s3", action="code_search"),
        }
        plan = _make_plan(steps)

        def fn(step: Step) -> str:
            if step.id == "s2":
                raise PermissionError("需要审批")
            return f"ok_{step.id}"

        result = r.route(plan, steps, executor_fn=fn)
        assert result.results["s1"].status == "done"
        assert result.results["s2"].status == "pending_approval"
        assert result.results["s3"].status == "done"
        assert "s2" in result.pending_approval
        assert result.all_done is False

    def test_missing_step_in_plan(self):
        """plan 引用了不存在的步骤 → 上报。"""
        r = _make_router()
        steps = {"s1": Step(id="s1", action="web_search")}
        plan = Plan(
            task_id="t-001",
            order=["s1", "s_ghost"],
            parallel_groups=[["s1"], ["s_ghost"]],
            fallback={},
            confirmed_by_user=True,
        )
        result = r.route(plan, steps, executor_fn=lambda s: "ok")
        assert "s_ghost" in result.pending_escalation


# ── 边界 ────────────────────────────────────────────────────────


class TestRouterEdge:
    def test_unconfirmed_plan(self):
        """plan 未确认 → E_VALIDATION。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps, confirmed=False)
        with pytest.raises(AgentError) as exc_info:
            r.route(plan, steps, executor_fn=None)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_steps(self):
        """steps 为空 → 空结果。"""
        r = _make_router()
        plan = Plan(task_id="t-001", order=[], parallel_groups=[], confirmed_by_user=True)
        result = r.route(plan, {}, executor_fn=None)
        assert result.results == {}
        assert result.all_done is True


# ── 授权 ────────────────────────────────────────────────────────


class TestRouterPermissions:
    def test_role_registered(self):
        assert "router" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["router"]
        assert set(perm.allowed_tools) == {
            "plan_validate",
            "config_read",
            "audit_log",
            "metric_collect",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["router"]
        assert perm.high_risk_tools == []

    def test_no_execution_tools(self):
        """router 不可直接执行。"""
        perm = DEFAULT_ROLE_PERMS["router"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestRouterConstants:
    def test_max_retries_positive(self):
        assert MAX_RETRIES >= 0

    def test_executor_map_covers_core_actions(self):
        for action in ["web_search", "file_write", "code_search", "data_query"]:
            assert action in EXECUTOR_MAP

    def test_default_executor(self):
        assert DEFAULT_EXECUTOR == "general_executor"

    def test_executor_types_diverse(self):
        """执行者类型有多样性（不全是同一个）。"""
        types = set(EXECUTOR_MAP.values())
        assert len(types) >= 4


# ── 可重试性契约（contracts/errors）─────────────────────────────


class TestRouterRetryableContract:
    """Router 必须尊重 ``AgentError.retryable``：不可重试错误不得重派。"""

    def test_不可重试AgentError不重派(self):
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)
        calls = 0

        def fn(step: Step) -> str:
            nonlocal calls
            calls += 1
            raise permission_error("动作无对应工具", source="s", correlation_id="c")

        result = r.route(plan, steps, executor_fn=fn)
        assert calls == 1  # 只调用一次（未重派）
        assert result.results["s1"].status == "failed"
        assert result.results["s1"].retries == 0
        assert "E_PERMISSION" in result.results["s1"].error
        assert "s1" in result.pending_escalation

    def test_可重试AgentError仍重派(self):
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)
        calls = 0

        def fn(step: Step) -> str:
            nonlocal calls
            calls += 1
            raise timeout_error("超时", source="s", correlation_id="c")

        result = r.route(plan, steps, executor_fn=fn)
        assert calls == MAX_RETRIES + 1
        assert result.results["s1"].status == "failed"
        assert result.results["s1"].retries == MAX_RETRIES
        assert "s1" in result.pending_escalation

    def test_异常标注retryable为假时不重派(self):
        """编排层在异常上标注 retryable=False（角色已判定确定性失败）→ 不重派。"""
        r = _make_router()
        steps = _make_steps(1)
        plan = _make_plan(steps)
        calls = 0

        def fn(step: Step) -> str:
            nonlocal calls
            calls += 1
            err = RuntimeError("角色已判定确定性失败")
            err.retryable = False  # type: ignore[attr-defined]
            raise err

        result = r.route(plan, steps, executor_fn=fn)
        assert calls == 1
        assert result.results["s1"].status == "failed"
        assert result.results["s1"].retries == 0
        assert "s1" in result.pending_escalation
