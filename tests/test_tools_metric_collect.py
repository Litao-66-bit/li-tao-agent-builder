"""metric_collect 工具测试：功能 / 边界 / 安全 / 成本。

monkeypatch audit_store._get_audit_path 指向 tmp_path。
测试数据通过 audit_store.append_audit 直接写入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl import audit_store
from agent_builder.tools.registry import registry


@pytest.fixture(autouse=True)
def _audit_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        audit_store, "_get_audit_path", lambda: tmp_path / "audit.json"
    )


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["metric_collect"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, *, scope=None, window=None, audit_id="a-1"):
    args: dict = {}
    if scope is not None:
        args["scope"] = scope
    if window is not None:
        args["window"] = window
    return ToolCall(audit_id=audit_id, role=role, tool="metric_collect", args=args)


def _seed(entries):
    """直接写入审计日志测试数据。"""
    for e in entries:
        audit_store.append_audit(
            e.get("role", "op"), e.get("action", "act"), e.get("detail", ""), e.get("correlation_id", "c")
        )


# ── 功能 ────────────────────────────────────────────────────────


class TestMetricCollectFunctional:
    def test_empty_audit(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert "total: 0" in call.result
        assert call.status == "executed"

    def test_summary(self, gatekeeper):
        _seed([{"role": "operator", "action": "file_read"},
               {"role": "operator", "action": "file_read"},
               {"role": "memory_manager", "action": "memory_write"}])
        call = registry.execute(gatekeeper, _make_call("operator", scope="summary"))
        assert "total: 3" in call.result
        assert "file_read" in call.result  # top action
        assert "operator" in call.result  # top role

    def test_by_action(self, gatekeeper):
        _seed([{"action": "a1"}, {"action": "a1"}, {"action": "a2"}])
        call = registry.execute(gatekeeper, _make_call("operator", scope="by_action"))
        assert "a1: 2" in call.result
        assert "a2: 1" in call.result

    def test_by_role(self, gatekeeper):
        _seed([{"role": "op1"}, {"role": "op1"}, {"role": "op2"}])
        call = registry.execute(gatekeeper, _make_call("operator", scope="by_role"))
        assert "op1: 2" in call.result
        assert "op2: 1" in call.result

    def test_by_status(self, gatekeeper):
        """审计条目无 status 字段时归入 unknown。"""
        _seed([{"action": "a1"}, {"action": "a2"}])
        call = registry.execute(gatekeeper, _make_call("operator", scope="by_status"))
        assert "unknown: 2" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestMetricCollectEdge:
    def test_invalid_scope(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", scope="invalid"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_negative_window(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", window=-1))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_window_filters_old(self, gatekeeper):
        """window 只统计最近 N 秒的条目。"""
        # 写入一条老记录（时间戳在 100 秒前）
        path = audit_store._get_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        entries = [{"id": "a-000001", "ts": old_ts, "role": "op", "action": "old", "detail": "", "correlation_id": "c"}]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        # 再写入一条新记录
        audit_store.append_audit("op", "new", "", "c")
        call = registry.execute(gatekeeper, _make_call("operator", window=50))
        assert "total: 1" in call.result  # 只统计 50 秒内的

    def test_window_zero_all(self, gatekeeper):
        _seed([{"action": "a1"}, {"action": "a2"}])
        call = registry.execute(gatekeeper, _make_call("operator", window=0))
        assert "total: 2" in call.result


# ── 安全 ────────────────────────────────────────────────────────


class TestMetricCollectSecurity:
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
        assert audit.tool == "metric_collect"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestMetricCollectRegistration:
    def test_registered(self):
        assert "metric_collect" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("metric_collect")
        assert spec.name == "metric_collect"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]


# ── 常量 ────────────────────────────────────────────────────────


class TestMetricCollectConstants:
    def test_scopes_valid(self):
        from agent_builder.tools.impl.metric_collect import VALID_SCOPES
        assert "summary" in VALID_SCOPES
        assert "by_action" in VALID_SCOPES
        assert "by_role" in VALID_SCOPES
        assert "by_status" in VALID_SCOPES
