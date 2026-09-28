"""plan_validate 工具测试：功能 / 边界 / 异常 / 安全 / 成本。

纯计算工具，无文件/网络访问，无需 mock。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.plan_validate import MAX_STEPS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["plan_validate"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, steps, order, *, parallel_groups=None, audit_id="a-1"):
    args: dict = {"steps": steps, "order": order}
    if parallel_groups is not None:
        args["parallel_groups"] = parallel_groups
    return ToolCall(audit_id=audit_id, role=role, tool="plan_validate", args=args)


def _step(sid, action="search", deps=None):
    return {"id": sid, "action": action, "depends_on": deps or []}


# ── 功能 ────────────────────────────────────────────────────────


class TestPlanValidateFunctional:
    def test_linear_dag_ok(self, gatekeeper):
        steps = [_step("s1"), _step("s2", deps=["s1"]), _step("s3", deps=["s2"])]
        call = registry.execute(gatekeeper, _make_call("operator", steps, ["s1", "s2", "s3"]))
        assert "通过" in call.result
        assert "3 个步骤" in call.result
        assert call.status == "executed"

    def test_parallel_dag_ok(self, gatekeeper):
        steps = [_step("s1"), _step("s2"), _step("s3", deps=["s1", "s2"])]
        groups = [["s1", "s2"], ["s3"]]
        call = registry.execute(
            gatekeeper, _make_call("operator", steps, ["s1", "s2", "s3"], parallel_groups=groups)
        )
        assert "通过" in call.result
        assert "2 个并行组" in call.result

    def test_single_step_ok(self, gatekeeper):
        steps = [_step("s1", action="file_read")]
        call = registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert "通过" in call.result
        assert "1 个步骤" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestPlanValidateEdge:
    def test_empty_steps(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="plan_validate",
            args={"steps": [], "order": ["s1"]},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_order(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="plan_validate",
            args={"steps": [_step("s1")], "order": []},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_too_many_steps(self, gatekeeper):
        steps = [_step(f"s{i}") for i in range(MAX_STEPS + 1)]
        order = [f"s{i}" for i in range(MAX_STEPS + 1)]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, order))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_missing_id(self, gatekeeper):
        steps = [{"action": "search", "depends_on": []}]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_id(self, gatekeeper):
        steps = [{"id": "", "action": "search", "depends_on": []}]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, [""]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_missing_action(self, gatekeeper):
        steps = [{"id": "s1", "depends_on": []}]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_duplicate_id(self, gatekeeper):
        steps = [_step("s1"), _step("s1")]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_depends_on_not_list(self, gatekeeper):
        steps = [{"id": "s1", "action": "search", "depends_on": "s2"}]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert exc_info.value.error_name == "E_VALIDATION"


# ── DAG 异常 ────────────────────────────────────────────────────


class TestPlanValidateDagErrors:
    def test_cycle_detected(self, gatekeeper):
        steps = [
            _step("s1", deps=["s2"]),
            _step("s2", deps=["s1"]),
        ]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1", "s2"]))
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "环" in str(exc_info.value.info.message)

    def test_dangling_dependency(self, gatekeeper):
        steps = [_step("s1", deps=["sX"])]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1"]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_order_references_missing(self, gatekeeper):
        steps = [_step("s1")]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", steps, ["s1", "sX"]))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_parallel_group_references_missing(self, gatekeeper):
        steps = [_step("s1")]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper,
                _make_call("operator", steps, ["s1"], parallel_groups=[["s1", "sX"]]),
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestPlanValidateSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", [_step("s1")], ["s1"])
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        steps = [_step("s1")]
        registry.execute(
            gatekeeper, _make_call("operator", steps, ["s1"], audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "plan_validate"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestPlanValidateRegistration:
    def test_registered(self):
        assert "plan_validate" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("plan_validate")
        assert spec.name == "plan_validate"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "steps" in spec.parameters["required"]
        assert "order" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestPlanValidateConstants:
    def test_max_steps_positive(self):
        assert MAX_STEPS > 0
