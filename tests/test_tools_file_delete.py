"""file_delete 工具测试：功能 / 边界 / 安全（high_risk 审批 + 沙箱）/ 注册。

file_delete 列入 operator.high_risk_tools，所有删除需审批（approval.granted_by 非空）；
仅删除常规文件，目录一律拒绝（不提供递归删除）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["file_delete"],
            high_risk_tools=["file_delete"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, *, approved=False, audit_id="a-1"):
    approval = Approval(required=True, granted_by="tester" if approved else None)
    return ToolCall(
        audit_id=audit_id,
        role=role,
        tool="file_delete",
        args={"path": str(path)},
        approval=approval,
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestFileDeleteFunctional:
    def test_delete_existing_file(self, gatekeeper, workspace):
        target = workspace / "old.txt"
        target.write_text("bye", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", target, approved=True))
        assert not target.exists()
        assert "deleted" in call.result
        assert call.status == "executed"

    def test_delete_nested_file_keeps_parent_dir(self, gatekeeper, workspace):
        target = workspace / "sub" / "nested.txt"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        registry.execute(gatekeeper, _make_call("operator", target, approved=True))
        assert not target.exists()
        assert target.parent.exists()  # 只删文件，保留目录

    def test_delete_unicode_name(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        target.write_text("内容", encoding="utf-8")
        registry.execute(gatekeeper, _make_call("operator", target, approved=True))
        assert not target.exists()


# ── 边界 ────────────────────────────────────────────────────────


class TestFileDeleteEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1",
            role="operator",
            tool="file_delete",
            args={"path": ""},
            approval=Approval(required=True, granted_by="u"),
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION）。
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_nonexistent_path(self, gatekeeper, workspace):
        call = _make_call("operator", workspace / "nope.txt", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "不存在" in exc_info.value.info.message

    def test_directory_rejected(self, gatekeeper, workspace):
        d = workspace / "adir"
        d.mkdir()
        (d / "keep.txt").write_text("keep", encoding="utf-8")
        call = _make_call("operator", d, approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "目录" in exc_info.value.info.message
        # 目录与其内容毫发无损
        assert d.exists() and (d / "keep.txt").exists()


# ── 安全（审批门 + 沙箱）───────────────────────────────────────────


class TestFileDeleteSecurity:
    def test_unapproved_rejected(self, gatekeeper, workspace):
        target = workspace / "secret.txt"
        target.write_text("data", encoding="utf-8")
        call = _make_call("operator", target, approved=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "高风险" in exc_info.value.info.message
        assert target.exists()

    def test_unregistered_role_denied(self, gatekeeper, workspace):
        target = workspace / "x.txt"
        target.write_text("x", encoding="utf-8")
        call = _make_call("intruder", target, approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert target.exists()

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        outside = workspace.parent / "outside_delete_target.txt"
        call = _make_call("operator", outside, approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        target = workspace / "audit.txt"
        target.write_text("d", encoding="utf-8")
        registry.execute(
            gatekeeper,
            _make_call("operator", target, approved=True, audit_id="a-log"),
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "file_delete"
        assert audit.audit_id == "a-log"

    def test_rejected_audit_logged(self, gatekeeper, workspace):
        target = workspace / "deny.txt"
        target.write_text("d", encoding="utf-8")
        call = _make_call("operator", target, approved=False)
        with pytest.raises(AgentError):
            registry.execute(gatekeeper, call)
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is False
        assert "高风险" in audit.reason


# ── 注册 ────────────────────────────────────────────────────────


class TestFileDeleteRegistration:
    def test_registered(self):
        assert "file_delete" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("file_delete")
        assert spec.name == "file_delete"
        assert spec.risk_level == "high"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "path" in spec.parameters["required"]
