"""file_write 工具测试：功能 / 边界 / 安全 / 成本（high_risk 审批）。

file_write 列入 operator.high_risk_tools，所有写操作需审批（approval.granted_by 非空）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.file_write import MAX_CONTENT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_write"],
            high_risk_tools=["file_write"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, content, overwrite=False, *, approved=False, audit_id="a-1"):
    approval = Approval(required=True, granted_by="tester" if approved else None)
    return ToolCall(
        audit_id=audit_id, role=role, tool="file_write",
        args={"path": str(path), "content": content, "overwrite": overwrite},
        approval=approval,
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestFileWriteFunctional:
    def test_write_new_file(self, gatekeeper, workspace):
        target = workspace / "new.txt"
        call = registry.execute(
            gatekeeper, _make_call("operator", target, "hello", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "hello"
        assert "wrote" in call.result
        assert "5 chars" in call.result

    def test_write_unicode(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        call = registry.execute(
            gatekeeper, _make_call("operator", target, "你好世界", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "你好世界"
        assert "4 chars" in call.result

    def test_overwrite_existing(self, gatekeeper, workspace):
        target = workspace / "old.txt"
        target.write_text("old", encoding="utf-8")
        registry.execute(
            gatekeeper,
            _make_call("operator", target, "new content", overwrite=True, approved=True),
        )
        assert target.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_dirs(self, gatekeeper, workspace):
        target = workspace / "sub" / "dir" / "nested.txt"
        registry.execute(
            gatekeeper, _make_call("operator", target, "deep", approved=True)
        )
        assert target.read_text(encoding="utf-8") == "deep"


# ── 边界 ────────────────────────────────────────────────────────


class TestFileWriteEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="file_write",
            args={"path": "", "content": "x"},
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_empty_content_rejected(self, gatekeeper, workspace):
        # content=None 会在实现层报 E_VALIDATION，但 args 里 content 必填
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="file_write",
            args={"path": str(workspace / "x.txt"), "content": None},  # type: ignore[arg-type]
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # None 会触发 TypeError → E_VALIDATION（registry 包装）
        assert exc_info.value.error_name in ("E_VALIDATION", "E_PERMISSION")

    def test_file_exists_no_overwrite(self, gatekeeper, workspace):
        target = workspace / "exists.txt"
        target.write_text("keep", encoding="utf-8")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", target, "new", approved=True)
            )
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "已存在" in exc_info.value.info.message
        # 原文件未被覆盖
        assert target.read_text(encoding="utf-8") == "keep"

    def test_content_too_long(self, gatekeeper, workspace):
        target = workspace / "big.txt"
        call = ToolCall(
            audit_id="a-e3", role="operator", tool="file_write",
            args={"path": str(target), "content": "a" * (MAX_CONTENT_CHARS + 1)},
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "超长" in exc_info.value.info.message


# ── 安全（审批门 + 沙箱）───────────────────────────────────────────


class TestFileWriteSecurity:
    def test_unapproved_rejected(self, gatekeeper, workspace):
        target = workspace / "secret.txt"
        call = _make_call("operator", target, "data", approved=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "高风险" in exc_info.value.info.message
        assert not target.exists()

    def test_unregistered_role_denied(self, gatekeeper, workspace):
        target = workspace / "x.txt"
        call = _make_call("intruder", target, "x", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        outside = workspace.parent / "outside.txt"
        call = _make_call("operator", outside, "x", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        target = workspace / "audit.txt"
        registry.execute(
            gatekeeper, _make_call("operator", target, "data", approved=True, audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_write"
        assert audit.audit_id == "a-log"

    def test_rejected_audit_logged(self, gatekeeper, workspace):
        target = workspace / "deny.txt"
        call = _make_call("operator", target, "data", approved=False)
        with pytest.raises(AgentError):
            registry.execute(gatekeeper, call)
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is False
        assert "高风险" in audit.reason


# ── 注册 ────────────────────────────────────────────────────────


class TestFileWriteRegistration:
    def test_registered(self):
        assert "file_write" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_write")
        assert spec.name == "file_write"
        assert spec.risk_level == "medium"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "content" in spec.parameters["required"]
        assert "path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestFileWriteConstants:
    def test_max_content_chars_positive(self):
        assert MAX_CONTENT_CHARS > 0
