"""file_read 工具测试：功能 / 边界 / 安全 / 成本（截断）。"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_read import MAX_OUTPUT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    """临时白名单目录。"""
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    """operator 角色允许 file_read 的门卫。"""
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_read"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, *, tool="file_read", audit_id="a-1"):
    """构造 ToolCall（不执行），供成功/异常测试统一使用。"""
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args={"path": str(path)})


# ── 功能 ────────────────────────────────────────────────────────


class TestFileReadFunctional:
    def test_read_success(self, gatekeeper, workspace):
        target = workspace / "demo.txt"
        target.write_text("hello world", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == "hello world"
        assert call.status == "executed"

    def test_read_unicode(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        target.write_text("你好，世界\n第二行", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == "你好，世界\n第二行"

    def test_empty_file(self, gatekeeper, workspace):
        target = workspace / "empty.txt"
        target.write_text("", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        assert call.result == ""


# ── 边界 ────────────────────────────────────────────────────────


class TestFileReadEdge:
    def test_missing_path_arg(self, gatekeeper):
        # args 不含 path → 门卫 _check_sandbox_path 取到空串 → E_PERMISSION
        call = ToolCall(audit_id="a-e1", role="operator", tool="file_read", args={})
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_empty_path_string(self, gatekeeper):
        call = ToolCall(audit_id="a-e2", role="operator", tool="file_read", args={"path": ""})
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_nonexistent_file(self, gatekeeper, workspace):
        call = _make_call("operator", workspace / "nope.txt")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不存在" in exc_info.value.info.message

    def test_path_is_directory(self, gatekeeper, workspace):
        sub = workspace / "subdir"
        sub.mkdir()
        call = _make_call("operator", sub)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不是文件" in exc_info.value.info.message

    def test_binary_file_rejected(self, gatekeeper, workspace):
        target = workspace / "binary.bin"
        target.write_bytes(bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01, 0x02, 0xFF]))
        call = _make_call("operator", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_TOOL"
        assert "UTF-8" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestFileReadSecurity:
    def test_unregistered_role_denied(self, gatekeeper, workspace):
        target = workspace / "secret.txt"
        target.write_text("top secret", encoding="utf-8")
        call = _make_call("intruder", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_role_without_tool_denied(self, workspace):
        perms = {
            "reader": RolePerm(role="reader", allowed_tools=["web_search"], high_risk_tools=[]),
        }
        gk = ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-s2")
        target = workspace / "x.txt"
        target.write_text("x", encoding="utf-8")
        call = _make_call("reader", target)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        # workspace.parent 一定在白名单之外
        outside = workspace.parent / "outside_tool_test.txt"
        call = _make_call("operator", outside)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        target = workspace / "audit.txt"
        target.write_text("data", encoding="utf-8")
        registry.execute(gatekeeper, _make_call("operator", target, audit_id="a-log"))
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_read"
        assert audit.audit_id == "a-log"


# ── 成本（大输出截断）───────────────────────────────────────────


class TestFileReadCost:
    def test_huge_file_truncated(self, gatekeeper, workspace):
        target = workspace / "huge.txt"
        target.write_text("a" * (MAX_OUTPUT_CHARS + 500), encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target))
        result = call.result
        assert len(result) < MAX_OUTPUT_CHARS + 100
        assert result.startswith("a" * MAX_OUTPUT_CHARS)
        assert "已截断" in result


# ── 注册 ────────────────────────────────────────────────────────


class TestFileReadRegistration:
    def test_registered_in_registry(self):
        assert "file_read" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_read")
        assert spec.name == "file_read"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "path" in spec.parameters["required"]
