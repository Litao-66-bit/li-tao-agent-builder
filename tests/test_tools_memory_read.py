"""memory_read 工具测试：功能 / 边界 / 安全 / 成本。

monkeypatch memory_store._get_memory_path 指向 tmp_path，无真实文件 IO。
测试数据通过 memory_store.save_memory 直接写入（独立于 memory_write）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl import memory_store
from agent_builder.tools.impl.memory_read import MAX_ENTRIES
from agent_builder.tools.impl.memory_store import encode_sensitive
from agent_builder.tools.registry import registry


@pytest.fixture(autouse=True)
def _memory_path(monkeypatch, tmp_path):
    """每个测试自动重定向记忆文件到 tmp_path。"""
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
            allowed_tools=["memory_read"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, *, query=None, scope=None, limit=None, audit_id="a-1"):
    args: dict = {}
    if query is not None:
        args["query"] = query
    if scope is not None:
        args["scope"] = scope
    if limit is not None:
        args["limit"] = limit
    return ToolCall(audit_id=audit_id, role=role, tool="memory_read", args=args)


def _seed(data):
    memory_store.save_memory(data)


# ── 功能 ────────────────────────────────────────────────────────


class TestMemoryReadFunctional:
    def test_empty_store(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert call.result == "(no memory found)"

    def test_read_all(self, gatekeeper):
        _seed({"short": [{"key": "k1", "content": "c1", "ts": "t1"}],
               "long": [{"key": "k2", "content": "c2", "ts": "t2"}]})
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert "[short]" in call.result
        assert "k1" in call.result
        assert "[long]" in call.result
        assert "k2" in call.result

    def test_filter_by_scope(self, gatekeeper):
        _seed({"short": [{"key": "k1", "content": "c1", "ts": "t1"}],
               "long": [{"key": "k2", "content": "c2", "ts": "t2"}]})
        call = registry.execute(gatekeeper, _make_call("operator", scope="short"))
        assert "k1" in call.result
        assert "k2" not in call.result

    def test_filter_by_query(self, gatekeeper):
        _seed({"short": [{"key": "alpha", "content": "first", "ts": "t1"},
                          {"key": "beta", "content": "second", "ts": "t2"}],
               "long": []})
        call = registry.execute(gatekeeper, _make_call("operator", query="first"))
        assert "alpha" in call.result
        assert "beta" not in call.result

    def test_sensitive_decoded_with_tag(self, gatekeeper):
        encoded = encode_sensitive("secret-value")
        _seed({"short": [],
               "long": [{"key": "k1", "content": encoded, "encrypted": True,
                         "sensitive": True, "ts": "t1"}]})
        call = registry.execute(gatekeeper, _make_call("operator"))
        assert "[SENSITIVE]" in call.result
        assert "secret-value" in call.result
        assert encoded not in call.result  # 原始编码不显示


# ── 边界 ────────────────────────────────────────────────────────


class TestMemoryReadEdge:
    def test_invalid_scope(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", scope="invalid"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_limit(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", limit=0))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_limit_truncates(self, gatekeeper):
        entries = [{"key": f"k{i}", "content": f"c{i}", "ts": "t"} for i in range(5)]
        _seed({"short": entries, "long": []})
        call = registry.execute(gatekeeper, _make_call("operator", scope="short", limit=2))
        # 每行一条，结果应有 2 条记忆
        lines = [ln for ln in call.result.split("\n") if ln.startswith("[")]
        assert len(lines) == 2

    def test_limit_capped_to_max(self, gatekeeper):
        # limit 超过 MAX_ENTRIES 应自动截断
        entries = [{"key": f"k{i}", "content": "c", "ts": "t"} for i in range(MAX_ENTRIES + 5)]
        _seed({"short": entries, "long": []})
        call = registry.execute(
            gatekeeper, _make_call("operator", scope="short", limit=MAX_ENTRIES + 100)
        )
        lines = [ln for ln in call.result.split("\n") if ln.startswith("[")]
        assert len(lines) == MAX_ENTRIES

    def test_query_no_match(self, gatekeeper):
        _seed({"short": [{"key": "k1", "content": "c1", "ts": "t1"}], "long": []})
        call = registry.execute(gatekeeper, _make_call("operator", query="nonexistent"))
        assert call.result == "(no memory found)"


# ── 安全 ────────────────────────────────────────────────────────


class TestMemoryReadSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        _seed({"short": [{"key": "k1", "content": "c1", "ts": "t1"}], "long": []})
        registry.execute(
            gatekeeper, _make_call("operator", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "memory_read"
        assert audit.audit_id == "a-log"

    def test_memory_manager_can_read(self, gatekeeper):
        _seed({"short": [{"key": "k1", "content": "c1", "ts": "t1"}], "long": []})
        call = registry.execute(gatekeeper, _make_call("memory_manager"))
        assert "k1" in call.result


# ── 注册 ────────────────────────────────────────────────────────


class TestMemoryReadRegistration:
    def test_registered(self):
        assert "memory_read" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("memory_read")
        assert spec.name == "memory_read"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert "operator" in spec.allowed_roles
        assert "memory_manager" in spec.allowed_roles


# ── 常量 ────────────────────────────────────────────────────────


class TestMemoryReadConstants:
    def test_max_entries_positive(self):
        assert MAX_ENTRIES > 0
