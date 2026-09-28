"""Gatekeeper 看门人角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：应用成功/回归失败回滚/回滚失败冻结/无批准拒绝/批准过期/拒绝/权限不足
- 边界：空步骤
- 授权：gatekeeper 角色权限矩阵（git_commit + rollback 高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.gatekeeper import (
    APPLY_ACTIONS,
    APPROVAL_TTL_HOURS,
    Gatekeeper,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_gatekeeper() -> Gatekeeper:
    return Gatekeeper(correlation_id="c-test")


def _make_step(
    action: str = "apply",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestGatekeeperFunctional:
    def test_apply_success(self):
        """应用成功 → done + tests_passed。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "commit_sha": "abc123",
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.commit_sha == "abc123"
        assert result.tests_passed is True

    def test_tests_failed_rollback(self):
        """回归失败 → rolled_back。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "commit_sha": "abc123",
            "tests_passed": False,
            "rollback_sha": "def456",
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rolled_back"
        assert result.rolled_back is True
        assert result.rollback_sha == "def456"

    def test_rollback_failed_freeze(self):
        """回滚也失败 → frozen。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "commit_sha": "abc123",
            "tests_passed": False,
            "rollback_failed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "frozen"
        assert result.module_frozen is True
        assert result.rolled_back is True
        assert "冻结" in result.error

    def test_no_approval_rejected(self):
        """无批准 → rejected。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {
            "commit_sha": "abc123",
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "无批准" in result.error

    def test_approval_expired(self):
        """批准过期 → rejected + approval_expired。"""
        g = _make_gatekeeper()
        # 构造 25 小时前的时间戳
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "approval_ts": old_ts,
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert result.approval_expired is True

    def test_approval_not_expired(self):
        """批准未过期 → 正常执行。"""
        g = _make_gatekeeper()
        from datetime import datetime, timedelta, timezone
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "approval_ts": recent_ts,
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_gatekeep_action_accepted(self):
        """gatekeep 是应用类 action。"""
        g = _make_gatekeeper()
        step = _make_step("gatekeep", {
            "approval_granted_by": "user-1",
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {"approval_granted_by": "user-1"})
        result = g.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_apply_action(self):
        """非应用类 → rejected。"""
        g = _make_gatekeeper()
        step = _make_step("file_write", {"approval_granted_by": "user-1"})
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出看门范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {"approval_granted_by": "user-1"})

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = g.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {"approval_granted_by": "user-1"})

        def fn(s: Step) -> str:
            raise RuntimeError("应用错误")

        result = g.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error


# ── 边界 ────────────────────────────────────────────────────────


class TestGatekeeperEdge:
    def test_empty_inputs_no_approval(self):
        """空输入 → 无批准 → rejected。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {})
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "无批准" in result.error

    def test_invalid_approval_ts_not_expired(self):
        """非法批准时间戳 → 不过期。"""
        g = _make_gatekeeper()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "approval_ts": "invalid-ts",
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_custom_ttl(self):
        """自定义批准有效期。"""
        from datetime import datetime, timedelta, timezone
        g = Gatekeeper(correlation_id="c-test", approval_ttl_hours=1.0)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        step = _make_step("apply", {
            "approval_granted_by": "user-1",
            "approval_ts": old_ts,
            "tests_passed": True,
        })
        result = g.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert result.approval_expired is True


# ── 授权 ────────────────────────────────────────────────────────


class TestGatekeeperPermissions:
    def test_role_registered(self):
        assert "gatekeeper" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["gatekeeper"]
        assert set(perm.allowed_tools) == {
            "git_commit",
            "rollback",
            "test_run",
            "sandbox_run",
            "audit_log",
            "memory_read",
        }

    def test_high_risk_tools(self):
        """git_commit + rollback 是高风险。"""
        perm = DEFAULT_ROLE_PERMS["gatekeeper"]
        assert "git_commit" in perm.high_risk_tools
        assert "rollback" in perm.high_risk_tools

    def test_no_unauthorized_tools(self):
        """gatekeeper 无网络/写文件/数据工具。"""
        perm = DEFAULT_ROLE_PERMS["gatekeeper"]
        forbidden = {"file_write", "web_fetch", "web_search", "data_query", "code_search"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestGatekeeperConstants:
    def test_apply_actions_nonempty(self):
        assert len(APPLY_ACTIONS) > 0
        assert "apply" in APPLY_ACTIONS
        assert "gatekeep" in APPLY_ACTIONS

    def test_apply_actions_excludes_non_apply(self):
        assert "file_write" not in APPLY_ACTIONS
        assert "web_search" not in APPLY_ACTIONS

    def test_approval_ttl_positive(self):
        assert APPROVAL_TTL_HOURS > 0
