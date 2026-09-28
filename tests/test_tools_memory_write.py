"""memory_write 工具测试：功能 / 边界 / 安全 / 成本。

monkeypatch memory_store._get_memory_path 指向 tmp_path。
通过 memory_store.load_memory 验证存储层（含加密标记）。
"""

from __future__ import annotations

import base64

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl import memory_store
from agent_builder.tools.impl.memory_write import MAX_CONTENT_CHARS
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
            allowed_tools=["memory_read", "memory_write"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, key, content, *, scope=None, sensitive=None, audit_id="a-1"):
    args: dict = {"key": key, "content": content}
    if scope is not None:
        args["scope"] = scope
    if sensitive is not None:
        args["sensitive"] = sensitive
    return ToolCall(audit_id=audit_id, role=role, tool="memory_write", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestMemoryWriteFunctional:
    def test_write_long(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("memory_manager", "k1", "hello"))
        assert "stored" in call.result
        assert "k1" in call.result
        data = memory_store.load_memory()
        assert len(data["long"]) == 1
        assert data["long"][0]["key"] == "k1"
        assert data["long"][0]["content"] == "hello"
        assert data["long"][0]["encrypted"] is False

    def test_write_short(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", "k1", "hi", scope="short")
        )
        assert "short" in call.result
        data = memory_store.load_memory()
        assert len(data["short"]) == 1

    def test_sensitive_encrypted(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("memory_manager", "k1", "secret", sensitive=True)
        )
        assert "sensitive=True" in call.result
        data = memory_store.load_memory()
        entry = data["long"][0]
        assert entry["encrypted"] is True
        assert entry["sensitive"] is True
        # 存储层是 base64 编码，不是明文
        assert entry["content"] != "secret"
        assert base64.b64decode(entry["content"]) == b"secret"

    def test_overwrite_same_key(self, gatekeeper):
        registry.execute(gatekeeper, _make_call("memory_manager", "k1", "old"))
        registry.execute(gatekeeper, _make_call("memory_manager", "k1", "new"))
        data = memory_store.load_memory()
        assert len(data["long"]) == 1
        assert data["long"][0]["content"] == "new"

    def test_ts_recorded(self, gatekeeper):
        registry.execute(gatekeeper, _make_call("memory_manager", "k1", "c1"))
        data = memory_store.load_memory()
        assert data["long"][0]["ts"]  # 非空时间戳


# ── 边界 ────────────────────────────────────────────────────────


class TestMemoryWriteEdge:
    def test_empty_key(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("memory_manager", "", "c"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_whitespace_key(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("memory_manager", "   ", "c"))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_content(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("memory_manager", "k1", ""))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_scope(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("memory_manager", "k1", "c", scope="invalid")
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_content_too_long(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("memory_manager", "k1", "x" * (MAX_CONTENT_CHARS + 1))
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestMemoryWriteSecurity:
    def test_operator_denied(self, gatekeeper):
        call = _make_call("operator", "k1", "c")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "k1", "c")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_sensitive_not_stored_plaintext(self, gatekeeper):
        """敏感信息不能在存储文件里以明文出现。"""
        registry.execute(
            gatekeeper, _make_call("memory_manager", "k1", "my-secret", sensitive=True)
        )
        path = memory_store._get_memory_path()
        raw = path.read_text(encoding="utf-8")
        assert "my-secret" not in raw  # 明文不在文件里
        assert "bXktc2VjcmV0" in raw  # base64 编码后的内容在

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("memory_manager", "k1", "c", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "memory_write"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestMemoryWriteRegistration:
    def test_registered(self):
        assert "memory_write" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("memory_write")
        assert spec.name == "memory_write"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["memory_manager"]
        assert "key" in spec.parameters["required"]
        assert "content" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestMemoryWriteConstants:
    def test_max_content_chars_positive(self):
        assert MAX_CONTENT_CHARS > 0
