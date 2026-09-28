"""change_notify 工具测试：功能 / 边界 / 安全 / 成本。

无外部依赖（纯记录生成），无需 mock。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.change_notify import MAX_SUMMARY_CHARS, VALID_CHANGE_TYPES
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["change_notify"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, target, change_type, summary, *, audit_id="a-1"):
    return ToolCall(
        audit_id=audit_id,
        role=role,
        tool="change_notify",
        args={"target": target, "change_type": change_type, "summary": summary},
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestChangeNotifyFunctional:
    def test_notify_basic(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "coder", "code", "修改了 utils.py")
        )
        assert "notified" in call.result
        assert "coder" in call.result
        assert "ntf-" in call.result
        assert call.status == "executed"

    def test_all_change_types(self, gatekeeper):
        for ct in VALID_CHANGE_TYPES:
            call = registry.execute(
                gatekeeper, _make_call("operator", "dev", ct, "摘要")
            )
            assert "ntf-" in call.result

    def test_id_contains_target(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "coder", "code", "摘要")
        )
        assert "coder" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestChangeNotifyEdge:
    def test_empty_target(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "", "code", "摘要"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_change_type(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "dev", "invalid", "摘要"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_summary(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "dev", "code", ""))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_summary_too_long(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "dev", "code", "x" * (MAX_SUMMARY_CHARS + 1))
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestChangeNotifySecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "dev", "code", "摘要")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", "dev", "code", "摘要", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "change_notify"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestChangeNotifyRegistration:
    def test_registered(self):
        assert "change_notify" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("change_notify")
        assert spec.name == "change_notify"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "target" in spec.parameters["required"]
        assert "change_type" in spec.parameters["required"]
        assert "summary" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestChangeNotifyConstants:
    def test_max_summary_chars_positive(self):
        assert MAX_SUMMARY_CHARS > 0

    def test_valid_change_types(self):
        assert "code" in VALID_CHANGE_TYPES
        assert "config" in VALID_CHANGE_TYPES
        assert "doc" in VALID_CHANGE_TYPES
        assert "test" in VALID_CHANGE_TYPES
        assert "other" in VALID_CHANGE_TYPES
