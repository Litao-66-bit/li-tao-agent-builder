"""memory_forget 工具测试：功能 / 边界 / 安全 / 成本。

monkeypatch memory_store._get_memory_path 指向 tmp_path。
测试数据通过 memory_store.save_memory 直接写入。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl import memory_store
from agent_builder.tools.impl.memory_forget import DEFAULT_SHORT_TTL
from agent_builder.tools.registry import registry


@pytest.fixture(autouse=True)
def _memory_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        memory_store, "_get_memory_path", lambda: tmp_path / "memory.json"
    )


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["memory_read"],
            high_risk_tools=[],
        ),
        "memory_manager": RolePerm(
            role="memory_manager",
            allowed_tools=["memory_read", "memory_write", "memory_forget"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, *, key=None, scope=None, expired_only=None, audit_id="a-1"):
    args: dict = {}
    if key is not None:
        args["key"] = key
    if scope is not None:
        args["scope"] = scope
    if expired_only is not None:
        args["expired_only"] = expired_only
    return ToolCall(audit_id=audit_id, role=role, tool="memory_forget", args=args)


def _ts(delta_s: float = 0) -> str:
    """返回相对当前时间偏移的 ISO 时间戳。"""
    t = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return t.isoformat()


# ── 功能 ────────────────────────────────────────────────────────


class TestMemoryForgetFunctional:
    def test_forget_by_key(self, gatekeeper):
        memory_store.save_memory(
            {"short": [{"key": "k1", "ts": _ts()}, {"key": "k2", "ts": _ts()}], "long": []}
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", key="k1", expired_only=False)
        )
        assert "forgot 1 entries" in call.result
        data = memory_store.load_memory()
        assert len(data["short"]) == 1
        assert data["short"][0]["key"] == "k2"

    def test_forget_all_scope(self, gatekeeper):
        memory_store.save_memory(
            {"short": [{"key": "k1", "ts": _ts()}, {"key": "k2", "ts": _ts()}],
             "long": [{"key": "k3", "ts": _ts()}]}
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", scope="short", expired_only=False)
        )
        assert "forgot 2 entries" in call.result
        data = memory_store.load_memory()
        assert len(data["short"]) == 0
        assert len(data["long"]) == 1  # long 未清理

    def test_forget_all_scopes(self, gatekeeper):
        memory_store.save_memory(
            {"short": [{"key": "k1", "ts": _ts()}],
             "long": [{"key": "k2", "ts": _ts()}]}
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", scope="all", expired_only=False)
        )
        assert "forgot 2 entries" in call.result
        data = memory_store.load_memory()
        assert len(data["short"]) == 0
        assert len(data["long"]) == 0

    def test_expired_only_clears_short(self, gatekeeper):
        """expired_only=true 清理过期的 short 记忆（超过 TTL）。"""
        memory_store.save_memory(
            {"short": [
                {"key": "old", "ts": _ts(-DEFAULT_SHORT_TTL - 100)},  # 过期
                {"key": "new", "ts": _ts()},  # 未过期
            ], "long": []}
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", scope="short", expired_only=True)
        )
        assert "forgot 1 entries" in call.result
        data = memory_store.load_memory()
        assert len(data["short"]) == 1
        assert data["short"][0]["key"] == "new"

    def test_expired_only_keeps_long(self, gatekeeper):
        """expired_only=true 不清理 long 记忆（long 无 TTL）。"""
        memory_store.save_memory(
            {"short": [],
             "long": [{"key": "k1", "ts": _ts(-99999)}]}  # 很老但 long 不过期
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", scope="long", expired_only=True)
        )
        assert "forgot 0 entries" in call.result
        data = memory_store.load_memory()
        assert len(data["long"]) == 1

    def test_empty_store(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", expired_only=False)
        )
        assert "forgot 0 entries" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestMemoryForgetEdge:
    def test_invalid_scope(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("memory_manager", scope="invalid")
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_nonexistent_key(self, gatekeeper):
        memory_store.save_memory(
            {"short": [{"key": "k1", "ts": _ts()}], "long": []}
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", key="nonexistent", expired_only=False)
        )
        assert "forgot 0 entries" in call.result

    def test_expired_no_timestamp_skipped(self, gatekeeper):
        """无时间戳的 short 条目不清理（损坏数据保护）。"""
        memory_store.save_memory(
            {"short": [{"key": "k1"}], "long": []}  # 无 ts 字段
        )
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", scope="short", expired_only=True)
        )
        assert "forgot 0 entries" in call.result


# ── 安全 ────────────────────────────────────────────────────────


class TestMemoryForgetSecurity:
    def test_operator_denied(self, gatekeeper):
        call = _make_call("operator", expired_only=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", expired_only=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        memory_store.save_memory(
            {"short": [{"key": "k1", "ts": _ts()}], "long": []}
        )
        registry.execute(
            gatekeeper,
            _make_call("memory_manager", key="k1", expired_only=False, audit_id="a-log"),
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "memory_forget"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestMemoryForgetRegistration:
    def test_registered(self):
        assert "memory_forget" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("memory_forget")
        assert spec.name == "memory_forget"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["memory_manager"]


# ── 常量 ────────────────────────────────────────────────────────


class TestMemoryForgetConstants:
    def test_default_short_ttl_positive(self):
        assert DEFAULT_SHORT_TTL > 0
