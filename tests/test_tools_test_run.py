"""test_run 工具测试：功能 / 边界 / 安全 / 成本（截断）。

subprocess.run 和 urllib.request.urlopen 全程 mock。
路径沙箱 + URL 安全校验通过真实门卫跑。
"""

from __future__ import annotations

import subprocess
import urllib.error
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.test_run import MAX_OUTPUT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["test_run"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, target, *, timeout=None, tool="test_run", audit_id="a-1"):
    args: dict = {"target": target}
    if timeout is not None:
        args["timeout"] = timeout
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeResp:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ── 功能 ────────────────────────────────────────────────────────


class TestTestRunFunctional:
    def test_pytest_mode(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("def test_ok(): pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="1 passed"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", str(test_file)))
        assert "1 passed" in call.result
        assert call.status == "executed"

    def test_doc_mode(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp(b"hello doc"),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/doc")
            )
        assert call.result == "hello doc"

    def test_doc_unicode(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp("你好文档".encode()),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/cn")
            )
        assert call.result == "你好文档"


# ── 边界 ────────────────────────────────────────────────────────


class TestTestRunEdge:
    def test_empty_target(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="test_run", args={"target": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_invalid_timeout(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", str(test_file), timeout=0)
            )
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 异常 ────────────────────────────────────────────────────────


class TestTestRunErrors:
    def test_pytest_not_found(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            side_effect=FileNotFoundError("pytest not found"),
        ):
            call = _make_call("operator", str(test_file))
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, call)
            assert exc_info.value.error_name == "E_TOOL"
            assert "pytest" in exc_info.value.info.message

    def test_pytest_timeout(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=5),
        ):
            call = _make_call("operator", str(test_file), timeout=5)
            with pytest.raises(AgentError) as exc_info:
                registry.execute(gatekeeper, call)
            assert exc_info.value.error_name == "E_TOOL"

    def test_doc_http_error(self, gatekeeper):
        url = "https://example.com/404"
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url, 404, "Not Found", {}, None),  # type: ignore[arg-type]
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", url))
        assert exc_info.value.error_name == "E_TOOL"
        assert "404" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestTestRunSecurity:
    def test_path_outside_sandbox_denied(self, gatekeeper):
        call = _make_call("operator", "/etc/passwd")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_url_ssrf_denied(self, gatekeeper):
        call = _make_call("operator", "http://127.0.0.1/admin")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_url_metadata_denied(self, gatekeeper):
        call = _make_call("operator", "http://169.254.169.254/latest/")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "https://example.com")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout="ok"),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", str(test_file), audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "test_run"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestTestRunCost:
    def test_pytest_output_truncated(self, gatekeeper, tmp_path):
        test_file = tmp_path / "test_x.py"
        test_file.write_text("pass")
        big = "x" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.test_run.subprocess.run",
            return_value=_FakeProc(stdout=big),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", str(test_file)))
        assert len(call.result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in call.result

    def test_doc_output_truncated(self, gatekeeper):
        big = b"x" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.test_run.urllib.request.urlopen",
            return_value=_FakeResp(big),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "https://example.com/huge")
            )
        assert len(call.result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in call.result


# ── 注册 ────────────────────────────────────────────────────────


class TestTestRunRegistration:
    def test_registered(self):
        assert "test_run" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("test_run")
        assert spec.name == "test_run"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 120.0
        assert spec.allowed_roles == ["operator"]
        assert "target" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestTestRunConstants:
    def test_max_output_chars_positive(self):
        assert MAX_OUTPUT_CHARS > 0
