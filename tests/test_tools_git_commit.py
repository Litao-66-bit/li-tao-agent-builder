"""git_commit 工具测试：功能 / 边界 / 安全 / 成本。

git_ops.run_git 全程 mock（避免真实 git 操作）。
路径沙箱校验通过真实门卫跑（workspace_dir=tmp_path）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.git_commit import MAX_MESSAGE_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "sub_architect": RolePerm(
            role="sub_architect",
            allowed_tools=["git_commit"],
            high_risk_tools=["git_commit"],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, repo_path, message, *, files=None, approved=False, audit_id="a-1"):
    args: dict = {"repo_path": repo_path, "message": message}
    if files is not None:
        args["files"] = files
    approval = Approval(required=True, granted_by="user" if approved else None)
    return ToolCall(audit_id=audit_id, role=role, tool="git_commit", args=args, approval=approval)


# ── 功能 ────────────────────────────────────────────────────────


class TestGitCommitFunctional:
    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_commit_basic(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["", "", "abc123def456789"]
        call = registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "test commit", approved=True)
        )
        assert "committed" in call.result
        assert "abc123" in call.result
        assert call.status == "executed"

    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_add_all_when_no_files(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["", "", "deadbeef"]
        registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "msg", approved=True)
        )
        add_call = mock_run.call_args_list[0]
        assert add_call[0][1] == ["add", "-A"]

    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_add_specific_files(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["", "", "deadbeef"]
        registry.execute(
            gatekeeper,
            _make_call("sub_architect", str(tmp_path), "msg", files=["a.py", "b.py"], approved=True),
        )
        add_call = mock_run.call_args_list[0]
        assert add_call[0][1] == ["add", "a.py", "b.py"]


# ── 边界 ────────────────────────────────────────────────────────


class TestGitCommitEdge:
    def test_empty_message(self, gatekeeper, tmp_path):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("sub_architect", str(tmp_path), "", approved=True)
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_message_too_long(self, gatekeeper, tmp_path):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper,
                _make_call("sub_architect", str(tmp_path), "x" * (MAX_MESSAGE_CHARS + 1), approved=True),
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 安全 ────────────────────────────────────────────────────────


class TestGitCommitSecurity:
    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_operator_denied(self, mock_run, gatekeeper, tmp_path):
        """operator 角色无 git_commit 权限。"""
        call = _make_call("operator", str(tmp_path), "msg", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_no_approval_denied(self, mock_run, gatekeeper, tmp_path):
        """高风险工具无审批拒绝。"""
        call = _make_call("sub_architect", str(tmp_path), "msg", approved=False)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_repo_path_outside_sandbox(self, mock_run, gatekeeper):
        """repo_path 超出沙箱拒绝。"""
        call = _make_call("sub_architect", "/etc/passwd", "msg", approved=True)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.git_commit.run_git")
    def test_audit_logged(self, mock_run, gatekeeper, tmp_path):
        mock_run.side_effect = ["", "", "abc123"]
        registry.execute(
            gatekeeper, _make_call("sub_architect", str(tmp_path), "msg", approved=True, audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "git_commit"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestGitCommitRegistration:
    def test_registered(self):
        assert "git_commit" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("git_commit")
        assert spec.name == "git_commit"
        assert spec.risk_level == "high"
        assert spec.cost_band == "medium"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["sub_architect"]
        assert "repo_path" in spec.parameters["required"]
        assert "message" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestGitCommitConstants:
    def test_max_message_chars_positive(self):
        assert MAX_MESSAGE_CHARS > 0
