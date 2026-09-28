"""approval_request 工具测试：功能 / 边界 / 安全 / 成本。

无外部依赖（纯记录生成），无需 mock。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.approval_request import MAX_REASON_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["approval_request"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, tool_call_id, reason, *, requested_role=None, audit_id="a-1"):
    args: dict = {"tool_call_id": tool_call_id, "reason": reason}
    if requested_role is not None:
        args["requested_role"] = requested_role
    return ToolCall(audit_id=audit_id, role=role, tool="approval_request", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestApprovalRequestFunctional:
    def test_request_basic(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "tc-001", "需要写文件")
        )
        assert "approval requested" in call.result
        assert "apr-" in call.result
        assert call.status == "executed"

    def test_with_requested_role(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "tc-001", "原因", requested_role="sub_architect")
        )
        assert "apr-" in call.result

    def test_id_contains_tool_call_id(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "tc-abc123", "原因")
        )
        assert "tc-abc12" in call.result  # 前 8 字符


# ── 边界 ────────────────────────────────────────────────────────


class TestApprovalRequestEdge:
    def test_empty_tool_call_id(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "", "原因"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_reason(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "tc-001", ""))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_reason_too_long(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "tc-001", "x" * (MAX_REASON_CHARS + 1))
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestApprovalRequestSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "tc-001", "原因")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", "tc-001", "原因", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "approval_request"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestApprovalRequestRegistration:
    def test_registered(self):
        assert "approval_request" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("approval_request")
        assert spec.name == "approval_request"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "tool_call_id" in spec.parameters["required"]
        assert "reason" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestApprovalRequestConstants:
    def test_max_reason_chars_positive(self):
        assert MAX_REASON_CHARS > 0
