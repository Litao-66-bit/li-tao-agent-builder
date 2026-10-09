"""audit_log 工具测试：功能 / 边界 / 安全 / 成本。

monkeypatch audit_store._get_audit_path 指向 tmp_path。
通过 audit_store.load_audit 验证存储层（只追加）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper, current_workspace_dir
from agent_builder.tools.impl import audit_store
from agent_builder.tools.impl.audit_log import MAX_DETAIL_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture(autouse=True)
def _audit_path(monkeypatch, tmp_path):
    """每个测试自动重定向审计日志到 tmp_path。"""
    monkeypatch.setattr(
        audit_store, "_get_audit_path", lambda: tmp_path / "audit.json"
    )


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["audit_log"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, role_arg, action, *, detail=None, correlation_id=None, audit_id="a-1"):
    args: dict = {"role": role_arg, "action": action}
    if detail is not None:
        args["detail"] = detail
    if correlation_id is not None:
        args["correlation_id"] = correlation_id
    return ToolCall(audit_id=audit_id, role=role, tool="audit_log", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestAuditLogFunctional:
    def test_write_basic(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "operator", "file_write:/x.py")
        )
        assert "logged" in call.result
        assert call.status == "executed"
        entries = audit_store.load_audit()
        assert len(entries) == 1
        assert entries[0]["role"] == "operator"
        assert entries[0]["action"] == "file_write:/x.py"

    def test_id_increments(self, gatekeeper):
        c1 = registry.execute(gatekeeper, _make_call("operator", "op", "act1"))
        c2 = registry.execute(gatekeeper, _make_call("operator", "op", "act2"))
        assert "a-000001" in c1.result
        assert "a-000002" in c2.result

    def test_with_detail(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", "op", "act", detail="extra info")
        )
        entries = audit_store.load_audit()
        assert entries[0]["detail"] == "extra info"

    def test_correlation_id_fallback(self, gatekeeper):
        """correlation_id 为空时用门卫的 correlation_id。"""
        registry.execute(
            gatekeeper, _make_call("operator", "op", "act", correlation_id="")
        )
        entries = audit_store.load_audit()
        assert entries[0]["correlation_id"] == "c-test"

    def test_ts_recorded(self, gatekeeper):
        registry.execute(gatekeeper, _make_call("operator", "op", "act"))
        entries = audit_store.load_audit()
        assert entries[0]["ts"]  # 非空时间戳


# ── 边界 ────────────────────────────────────────────────────────


class TestDefaultStoreLocation:
    """回归：默认审计位置必须在**当前工作区内**（同 memory_store，见其同名测试）。"""

    def test_默认审计目录落在工作区内(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("AGENT_AUDIT_FILE", raising=False)
        token = current_workspace_dir.set(tmp_path)
        try:
            assert audit_store.default_audit_dir() == tmp_path / ".agent-audit"
        finally:
            current_workspace_dir.reset(token)


class TestAuditLogEdge:
    def test_empty_role(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "", "act"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_whitespace_role(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "   ", "act"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_action(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "op", ""))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_detail_too_long(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "op", "act", detail="x" * (MAX_DETAIL_CHARS + 1))
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestAuditLogSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "op", "act")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_append_only(self, gatekeeper):
        """只追加：写入后记录存在，再次写入只增不改。"""
        registry.execute(gatekeeper, _make_call("operator", "op", "act1", audit_id="a-1"))
        first_entries = audit_store.load_audit()
        registry.execute(gatekeeper, _make_call("operator", "op", "act2", audit_id="a-2"))
        second_entries = audit_store.load_audit()
        assert len(second_entries) == len(first_entries) + 1
        assert second_entries[0] == first_entries[0]  # 第一条不变

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", "op", "act", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "audit_log"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestAuditLogRegistration:
    def test_registered(self):
        assert "audit_log" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("audit_log")
        assert spec.name == "audit_log"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "role" in spec.parameters["required"]
        assert "action" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestAuditLogConstants:
    def test_max_detail_chars_positive(self):
        assert MAX_DETAIL_CHARS > 0
