"""rollback 工具测试：功能 / 边界 / 安全 / 成本。

git_ops.run_git 全程 mock（避免真实 git 操作）。
路径沙箱校验通过真实门卫跑（workspace_dir=tmp_path）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "sub_architect": RolePerm(
            role="sub_architect",
            allowed_tools=["rollback"],
            high_risk_tools=["rollback"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, repo_path, target, *, approved=False, audit_id="a-1"):
    approval = Approval(required=True, granted_by="user" if approved else None)
    return ToolCall(
        audit_id=audit_id,
        role=role,
        tool="rollback",
        args={"repo_path": repo_path, "target": target},
        approval=approval,
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestRollbackFunctional:
    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_rollback_basic(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["oldhead123", "", "newhead456"]
        call = registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "HEAD~1", approved=True)
        )
        assert "rolled back" in call.result
        assert "oldhead1" in call.result
        assert "newhead4" in call.result
        assert call.status == "executed"

    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_reset_hard_called(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["old", "", "new"]
        registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "abc123", approved=True)
        )
        reset_call = mock_run.call_args_list[1]
        assert reset_call[0][1] == ["reset", "--hard", "abc123"]


# ── 边界 ────────────────────────────────────────────────────────


class TestRollbackEdge:
    def test_empty_target(self, gatekeeper, tmp_path):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("sub_architect", str(tmp_path), "", approved=True)
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestRollbackSecurity:
    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_operator_denied(self, mock_run, gatekeeper, tmp_path):
        call = _make_call("operator", str(tmp_path), "HEAD~1", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_no_approval_denied(self, mock_run, gatekeeper, tmp_path):
        call = _make_call("sub_architect", str(tmp_path), "HEAD~1", approved=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_repo_path_outside_sandbox(self, mock_run, gatekeeper):
        call = _make_call("sub_architect", "/etc/passwd", "HEAD~1", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.rollback.run_git")
    def test_audit_logged(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["old", "", "new"]
        registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "HEAD~1", approved=True, audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "rollback"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestRollbackRegistration:
    def test_registered(self):
        assert "rollback" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("rollback")
        assert spec.name == "rollback"
        assert spec.risk_level == "high"
        assert spec.cost_band == "medium"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["sub_architect"]
        assert "repo_path" in spec.parameters["required"]
        assert "target" in spec.parameters["required"]
