"""git_log 工具测试：功能 / 边界 / 安全 / 成本。

git_ops.run_git 全程 mock（避免真实 git 操作）。
路径沙箱校验通过真实门卫跑（workspace_dir=tmp_path）。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.git_log import DEFAULT_LIMIT, MAX_LIMIT
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["git_log"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, repo_path, *, limit=None, oneline=None, audit_id="a-1"):
    args: dict = {"repo_path": repo_path}
    if limit is not None:
        args["limit"] = limit
    if oneline is not None:
        args["oneline"] = oneline
    return ToolCall(audit_id=audit_id, role=role, tool="git_log", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestGitLogFunctional:
    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_query_basic(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = "abc1234 msg1\ndef5678 msg2\n"
        call = registry.execute(gatekeeper, _make_call("operator", str(tmp_path)))
        assert "abc1234" in call.result
        assert call.status == "executed"

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_oneline_adds_flag(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = "abc1234 msg\n"
        registry.execute(gatekeeper, _make_call("operator", str(tmp_path), oneline=True))
        args = mock_run.call_args[0][1]
        assert "--oneline" in args

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_no_oneline(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = "commit abc1234\nAuthor: ...\n"
        registry.execute(gatekeeper, _make_call("operator", str(tmp_path), oneline=False))
        args = mock_run.call_args[0][1]
        assert "--oneline" not in args

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_empty_log(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = ""
        call = registry.execute(gatekeeper, _make_call("operator", str(tmp_path)))
        assert call.result == "(no commits)"


# ── 边界 ────────────────────────────────────────────────────────


class TestGitLogEdge:
    def test_zero_limit(self, gatekeeper, tmp_path):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", str(tmp_path), limit=0))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_negative_limit(self, gatekeeper, tmp_path):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", str(tmp_path), limit=-1))
        assert exc_info.value.error_name == "E_VALIDATION"

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_limit_capped(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = ""
        registry.execute(gatekeeper, _make_call("operator", str(tmp_path), limit=999))
        args = mock_run.call_args[0][1]
        n_arg = next(a for a in args if a.startswith("-n"))
        assert n_arg == f"-n{MAX_LIMIT}"


# ── 安全 ────────────────────────────────────────────────────────


class TestGitLogSecurity:
    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_unregistered_role_denied(self, mock_run, gatekeeper, tmp_path):
        call = _make_call("intruder", str(tmp_path))
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_repo_path_outside_sandbox(self, mock_run, gatekeeper):
        call = _make_call("operator", "/etc/passwd")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        mock_run.assert_not_called()

    @patch("agent_builder.tools.impl.git_log.run_git")
    def test_audit_logged(self, mock_run, gatekeeper, tmp_path):
        mock_run.return_value = "abc1234 msg\n"
        registry.execute(
            gatekeeper, _make_call("operator", str(tmp_path), audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "git_log"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestGitLogRegistration:
    def test_registered(self):
        assert "git_log" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("git_log")
        assert spec.name == "git_log"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 15.0
        assert "operator" in spec.allowed_roles
        assert "sub_architect" in spec.allowed_roles
        assert "repo_path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestGitLogConstants:
    def test_default_limit_positive(self):
        assert DEFAULT_LIMIT > 0

    def test_max_limit_positive(self):
        assert MAX_LIMIT > 0
