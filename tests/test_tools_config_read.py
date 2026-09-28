"""config_read 工具测试：功能 / 边界 / 安全 / 成本。

读取 permissions/registry 配置，无外部依赖，无需 mock。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["config_read"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, *, section=None, audit_id="a-1"):
    args: dict = {}
    if section is not None:
        args["section"] = section
    return ToolCall(audit_id=audit_id, role=role, tool="config_read", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestConfigReadFunctional:
    def test_read_all(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert "## Roles" in call.result
        assert "## Tools" in call.result
        assert call.status == "executed"

    def test_read_roles(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", section="roles"))
        assert "## Roles" in call.result
        assert "## Tools" not in call.result
        assert "operator" in call.result
        assert "memory_manager" in call.result

    def test_read_tools(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", section="tools"))
        assert "## Tools" in call.result
        assert "## Roles" not in call.result
        assert "file_read" in call.result

    def test_roles_show_permissions(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", section="roles"))
        assert "allowed_tools" in call.result
        assert "high_risk_tools" in call.result

    def test_tools_show_specs(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", section="tools"))
        assert "risk:" in call.result
        assert "timeout:" in call.result
        assert "roles:" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestConfigReadEdge:
    def test_invalid_section(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", section="invalid"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_default_section_all(self, gatekeeper):
        """不传 section 时默认 all。"""
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert "## Roles" in call.result
        assert "## Tools" in call.result


# ── 安全 ────────────────────────────────────────────────────────


class TestConfigReadSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "config_read"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestConfigReadRegistration:
    def test_registered(self):
        assert "config_read" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("config_read")
        assert spec.name == "config_read"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]


# ── 常量 ────────────────────────────────────────────────────────


class TestConfigReadConstants:
    def test_sections_valid(self):
        from agent_builder.tools.impl.config_read import VALID_SECTIONS
        assert "roles" in VALID_SECTIONS
        assert "tools" in VALID_SECTIONS
        assert "all" in VALID_SECTIONS
