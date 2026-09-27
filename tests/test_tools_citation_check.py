"""citation_check 工具测试：功能 / 边界 / 安全 / 成本。

urllib.request.urlopen 全程 mock，不发起真实网络请求。
URL 安全校验（私有 IP、元数据端点）通过真实 url_guard 跑。
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.citation_check import MAX_SOURCES, USER_AGENT
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["citation_check"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, sources, *, timeout=None, tool="citation_check", audit_id="a-1"):
    args = {"sources": sources}
    if timeout is not None:
        args["timeout"] = timeout
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b""


# ── 功能 ────────────────────────────────────────────────────────


class TestCitationCheckFunctional:
    def test_url_ok(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            return_value=_FakeResp(200),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", ["https://example.com"]))
        result = call.result
        assert "[OK]" in result
        assert "example.com" in result
        assert call.status == "executed"

    def test_doi_ok(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            return_value=_FakeResp(200),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", ["10.1234/test"]))
        result = call.result
        assert "[OK]" in result
        assert "10.1234/test" in result

    def test_mixed_sources(self, gatekeeper):
        sources = ["https://example.com", "10.1234/test", "not-a-citation"]
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            return_value=_FakeResp(200),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", sources))
        result = call.result
        assert "[OK]" in result
        assert "[SKIP]" in result
        assert "not-a-citation" in result


# ── 边界 ────────────────────────────────────────────────────────


class TestCitationCheckEdge:
    def test_empty_sources(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="citation_check", args={"sources": []}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_too_many_sources(self, gatekeeper):
        sources = [f"https://example.com/{i}" for i in range(MAX_SOURCES + 1)]
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", sources))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_timeout(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", ["https://example.com"], timeout=0)
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_source_skipped(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            return_value=_FakeResp(200),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", ["", "https://example.com"])
            )
        result = call.result
        assert "[SKIP]" in result
        assert "[OK]" in result


# ── 异常 ────────────────────────────────────────────────────────


class TestCitationCheckErrors:
    def test_url_http_error(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None),  # type: ignore[arg-type]
        ):
            call = registry.execute(gatekeeper, _make_call("operator", ["https://example.com"]))
        result = call.result
        assert "[FAIL]" in result
        assert "404" in result

    def test_url_url_error(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network down"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", ["https://example.com"]))
        result = call.result
        assert "[FAIL]" in result


# ── 安全 ────────────────────────────────────────────────────────


class TestCitationCheckSecurity:
    def test_private_ip_fail(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", ["http://127.0.0.1/admin"]))
        result = call.result
        assert "[FAIL]" in result
        assert "安全校验失败" in result

    def test_metadata_endpoint_fail(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", ["http://169.254.169.254/latest/"]))
        result = call.result
        assert "[FAIL]" in result

    def test_file_scheme_skipped(self, gatekeeper):
        call = registry.execute(gatekeeper, _make_call("operator", ["file:///etc/passwd"]))
        result = call.result
        assert "[SKIP]" in result

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", ["https://example.com"])
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.citation_check.urllib.request.urlopen",
            return_value=_FakeResp(200),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", ["https://example.com"], audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "citation_check"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestCitationCheckRegistration:
    def test_registered(self):
        assert "citation_check" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("citation_check")
        assert spec.name == "citation_check"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 120.0
        assert spec.allowed_roles == ["operator"]
        assert "sources" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestCitationCheckConstants:
    def test_max_sources_positive(self):
        assert MAX_SOURCES > 0

    def test_user_agent(self):
        assert "Mozilla" in USER_AGENT
