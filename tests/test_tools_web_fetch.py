"""web_fetch 工具测试：功能 / 边界 / 安全（SSRF 防护）/ 成本（截断）。

urllib.request.urlopen 全程 mock，不发起真实网络请求。
URL 安全校验（私有 IP、元数据端点、非法 scheme）通过真实 url_guard 跑。
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.web_fetch import MAX_OUTPUT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):  # web_fetch 不用 workspace，但门卫构造需要
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["web_fetch"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, url, *, timeout=None, max_chars=None, tool="web_fetch", audit_id="a-1"):
    args = {"url": url}
    if timeout is not None:
        args["timeout"] = timeout
    if max_chars is not None:
        args["max_chars"] = max_chars
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeResp:
    """urlopen 返回的上下文管理器替身。"""

    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


# ── 功能 ────────────────────────────────────────────────────────


class TestWebFetchFunctional:
    def test_fetch_success(self, gatekeeper):
        url = "https://example.com/page"
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(b"hello world"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", url))
        assert call.result == "hello world"
        assert call.status == "executed"

    def test_fetch_unicode(self, gatekeeper):
        url = "https://example.com/cn"
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp("你好世界".encode()),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", url))
        assert call.result == "你好世界"

    def test_charset_from_header(self, gatekeeper):
        url = "https://example.com/latin"
        # 用 latin-1 编码，Content-Type 声明 charset=iso-8859-1
        body = "café".encode("iso-8859-1")
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(body, "text/html; charset=iso-8859-1"),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", url))
        assert call.result == "café"


# ── 边界 ────────────────────────────────────────────────────────


class TestWebFetchEdge:
    def test_empty_url(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="web_fetch", args={"url": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_invalid_timeout(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="web_fetch",
            args={"url": "https://example.com", "timeout": 0},
        )
        # timeout=0 在实现层校验，但 url 先过门卫 → 应能到实现层
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(b"x"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_max_chars(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e3", role="operator", tool="web_fetch",
            args={"url": "https://example.com", "max_chars": 0},
        )
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(b"x"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 异常 ────────────────────────────────────────────────────────


class TestWebFetchErrors:
    def test_http_error(self, gatekeeper):
        url = "https://example.com/404"
        with (
            patch(
                "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(url, 404, "Not Found", {}, None),  # type: ignore[arg-type]
            ),
            pytest.raises(AgentError) as exc_info,
        ):
            registry.execute(gatekeeper, _make_call("operator", url))
        assert exc_info.value.error_name == "E_TOOL"
        assert "404" in exc_info.value.info.message

    def test_url_error(self, gatekeeper):
        url = "https://example.com/down"
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network unreachable"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", url))
        assert exc_info.value.error_name == "E_TOOL"


# ── 安全（SSRF 防护）───────────────────────────────────────────────


class TestWebFetchSecurity:
    def test_private_ip_denied(self, gatekeeper):
        # 127.0.0.1 是 IP 字面量，门卫直接校验
        call = _make_call("operator", "http://127.0.0.1/admin")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "私有" in exc_info.value.info.message or "private" in exc_info.value.info.message.lower()

    def test_metadata_endpoint_denied(self, gatekeeper):
        call = _make_call("operator", "http://169.254.169.254/latest/meta-data/")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "元数据" in exc_info.value.info.message or "169.254.169.254" in exc_info.value.info.message

    def test_file_scheme_denied(self, gatekeeper):
        call = _make_call("operator", "file:///etc/passwd")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"
        assert "scheme" in exc_info.value.info.message.lower()

    def test_ftp_scheme_denied(self, gatekeeper):
        call = _make_call("operator", "ftp://example.com/file")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "https://example.com")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(b"ok"),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", "https://example.com", audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "web_fetch"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestWebFetchCost:
    def test_huge_content_truncated(self, gatekeeper):
        url = "https://example.com/huge"
        big_body = b"a" * (MAX_OUTPUT_CHARS + 500)
        with patch(
            "agent_builder.tools.impl.web_fetch.urllib.request.urlopen",
            return_value=_FakeResp(big_body),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", url))
        result = call.result
        assert len(result) < MAX_OUTPUT_CHARS + 100
        assert "已截断" in result


# ── 注册 ────────────────────────────────────────────────────────


class TestWebFetchRegistration:
    def test_registered(self):
        assert "web_fetch" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("web_fetch")
        assert spec.name == "web_fetch"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["operator"]
        assert "url" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestWebFetchConstants:
    def test_max_output_chars_positive(self):
        assert MAX_OUTPUT_CHARS > 0
