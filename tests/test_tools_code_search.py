"""code_search 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess 调用全程 mock，不依赖系统是否安装 ripgrep。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.code_search import MAX_RESULTS, RG_BIN, RG_TIMEOUT
from agent_builder.tools.registry import registry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def gatekeeper(workspace):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["code_search"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-test")


def _make_call(role, path, pattern="hello", max_results=None, *, tool="code_search", audit_id="a-1"):
    args = {"path": str(path), "pattern": pattern}
    if max_results is not None:
        args["max_results"] = max_results
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _CompletedProc:
    """subprocess.CompletedProcess 的简易替身，避免构造真实对象。"""

    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _rg_match_json(path: str, line_no: int, content: str) -> str:
    """构造一行 rg --json 的 match 记录。"""
    return json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": path},
                "line_number": line_no,
                "lines": {"text": content},
            },
        }
    )


# ── 功能 ────────────────────────────────────────────────────────


class TestCodeSearchFunctional:
    def test_search_success(self, gatekeeper, workspace):
        target = workspace / "demo.py"
        target.write_text("hello world\nfoo bar\nhello again\n", encoding="utf-8")
        out = "\n".join(
            [_rg_match_json(str(target), 1, "hello world\n"),
             _rg_match_json(str(target), 3, "hello again\n")]
        )
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(out, "", 0),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "hello"))
        result = call.result
        assert str(target) + ":1:hello world" in result
        assert str(target) + ":3:hello again" in result
        assert call.status == "executed"

    def test_no_match_returns_empty(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "", 1),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "nope"))
        assert call.result == ""

    def test_unicode_content(self, gatekeeper, workspace):
        target = workspace / "中文.txt"
        target.write_text("你好世界\n", encoding="utf-8")
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(
                _rg_match_json(str(target), 1, "你好世界\n"), "", 0
            ),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", workspace, "你好"))
        assert "你好世界" in call.result


# ── 边界 ────────────────────────────────────────────────────────


class TestCodeSearchEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="code_search",
            args={"path": "", "pattern": "x"},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_empty_pattern(self, gatekeeper, workspace):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="code_search",
            args={"path": str(workspace), "pattern": ""},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_max_results(self, gatekeeper, workspace):
        call = ToolCall(
            audit_id="a-e3", role="operator", tool="code_search",
            args={"path": str(workspace), "pattern": "x", "max_results": 0},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 异常 ────────────────────────────────────────────────────────


class TestCodeSearchErrors:
    def test_rg_not_installed(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            side_effect=FileNotFoundError("rg not found"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "x"))
        assert exc_info.value.error_name == "E_TOOL"
        assert "ripgrep" in exc_info.value.info.message

    def test_rg_timeout(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="rg", timeout=RG_TIMEOUT),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "x"))
        assert exc_info.value.error_name == "E_TOOL"
        assert "超时" in exc_info.value.info.message

    def test_rg_error_code(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "bad regex", 2),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", workspace, "("))
        assert exc_info.value.error_name == "E_TOOL"
        assert "bad regex" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestCodeSearchSecurity:
    def test_unregistered_role_denied(self, gatekeeper, workspace):
        call = _make_call("intruder", workspace)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_path_outside_workspace_denied(self, gatekeeper, workspace):
        call = _make_call("operator", workspace.parent / "outside")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, workspace):
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc("", "", 1),
        ):
            registry.execute(gatekeeper, _make_call("operator", workspace, "x", audit_id="a-log"))
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "code_search"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestCodeSearchCost:
    def test_results_truncated(self, gatekeeper, workspace):
        target = workspace / "big.py"
        target.write_text("x\n", encoding="utf-8")
        # 构造 50 条匹配，max_results=10 → 截断
        matches = [_rg_match_json(str(target), i, "x\n") for i in range(50)]
        out = "\n".join(matches)
        with patch(
            "agent_builder.tools.impl.code_search.subprocess.run",
            return_value=_CompletedProc(out, "", 0),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", workspace, "x", max_results=10)
            )
        lines = call.result.split("\n")
        # 应只有 10 条 + 1 条截断提示
        assert len(lines) == 11
        assert "已截断" in lines[-1]


# ── 注册 ────────────────────────────────────────────────────────


class TestCodeSearchRegistration:
    def test_registered(self):
        assert "code_search" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("code_search")
        assert spec.name == "code_search"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["operator"]
        assert "pattern" in spec.parameters["required"]
        assert "path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestCodeSearchConstants:
    def test_max_results_default(self):
        assert MAX_RESULTS == 200

    def test_rg_bin(self):
        assert RG_BIN == "rg"

    def test_rg_timeout_positive(self):
        assert RG_TIMEOUT > 0
