"""web_search 工具测试：功能 / 边界 / 安全 / 成本（截断）。

urllib.request.urlopen 全程 mock，不发起真实网络请求。
web_search 的 URL 在实现内部构造（DDG API），门卫 _check_url_safety 跳过（args 无 url）。
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import patch

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.web_search import (
    ABSOLUTE_MAX,
    DDG_API_URL,
    DEFAULT_MAX_RESULTS,
)
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):  # web_search 不用 workspace，但门卫构造需要
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["web_search"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, query, max_results=None, *, tool="web_search", audit_id="a-1"):
    args = {"query": query}
    if max_results is not None:
        args["max_results"] = max_results
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


class _FakeResp:
    def __init__(self, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _ddg_response(abstract_text="", abstract_url="", heading="", related=None):
    """构造 DuckDuckGo IA API 的 JSON 响应。"""
    return json.dumps(
        {
            "AbstractText": abstract_text,
            "AbstractURL": abstract_url,
            "Heading": heading,
            "RelatedTopics": related or [],
        }
    ).encode("utf-8")


# ── 功能 ────────────────────────────────────────────────────────


class TestWebSearchFunctional:
    def test_search_with_abstract(self, gatekeeper):
        body = _ddg_response(
            abstract_text="Python is a programming language.",
            abstract_url="https://en.wikipedia.org/wiki/Python",
            heading="Python (programming language)",
        )
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "python"))
        result = call.result
        assert "Python (programming language)" in result
        assert "programming language" in result
        assert "wikipedia.org" in result
        assert call.status == "executed"

    def test_search_with_related_topics(self, gatekeeper):
        related = [
            {"Text": "Python tutorial - learn python", "FirstURL": "https://example.com/tut"},
            {"Text": "Python docs", "FirstURL": "https://example.com/docs"},
        ]
        body = _ddg_response(related=related)
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "python"))
        result = call.result
        assert "learn python" in result
        assert "example.com/tut" in result
        assert "example.com/docs" in result

    def test_search_with_grouped_topics(self, gatekeeper):
        # RelatedTopics 含分组节点 {Topics: [...]}
        related = [
            {
                "Topics": [
                    {"Text": "sub item 1", "FirstURL": "https://a.com/1"},
                    {"Text": "sub item 2", "FirstURL": "https://a.com/2"},
                ]
            },
            {"Text": "top level", "FirstURL": "https://b.com"},
        ]
        body = _ddg_response(related=related)
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "x"))
        result = call.result
        assert "sub item 1" in result
        assert "sub item 2" in result
        assert "top level" in result

    def test_no_results_returns_empty(self, gatekeeper):
        body = _ddg_response()
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(gatekeeper, _make_call("operator", "nonexistent"))
        assert call.result == ""


# ── 边界 ────────────────────────────────────────────────────────


class TestWebSearchEdge:
    def test_empty_query(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="web_search", args={"query": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_max_results(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e2", role="operator", tool="web_search",
            args={"query": "x", "max_results": 0},
        )
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(_ddg_response()),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_max_results_capped_to_absolute(self, gatekeeper):
        # max_results=1000 应被截到 ABSOLUTE_MAX
        body = _ddg_response(related=[
            {"Text": f"item {i}", "FirstURL": f"https://x.com/{i}"}
            for i in range(ABSOLUTE_MAX + 10)
        ])
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "x", max_results=1000)
            )
        lines = call.result.split("\n")
        # ABSOLUTE_MAX 条 + 1 条截断提示
        assert len(lines) == ABSOLUTE_MAX + 1
        assert "已截断" in lines[-1]


# ── 异常 ────────────────────────────────────────────────────────


class TestWebSearchErrors:
    def test_http_error(self, gatekeeper):
        with (
            patch(
                "agent_builder.tools.impl.web_search.urllib.request.urlopen",
                side_effect=urllib.error.HTTPError(
                    DDG_API_URL, 500, "Server Error", {}, None  # type: ignore[arg-type]
                ),
            ),
            pytest.raises(AgentError) as exc_info,
        ):
            registry.execute(gatekeeper, _make_call("operator", "x"))
        assert exc_info.value.error_name == "E_TOOL"
        assert "500" in exc_info.value.info.message

    def test_url_error(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            side_effect=urllib.error.URLError("network down"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "x"))
        assert exc_info.value.error_name == "E_TOOL"

    def test_invalid_json(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(b"not json at all"),
        ), pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "x"))
        assert exc_info.value.error_name == "E_TOOL"
        assert "JSON" in exc_info.value.info.message


# ── 安全 ────────────────────────────────────────────────────────


class TestWebSearchSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "x")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(_ddg_response()),
        ):
            registry.execute(
                gatekeeper, _make_call("operator", "test", audit_id="a-log")
            )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "web_search"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestWebSearchCost:
    def test_results_truncated(self, gatekeeper):
        related = [
            {"Text": f"item {i}", "FirstURL": f"https://x.com/{i}"}
            for i in range(20)
        ]
        body = _ddg_response(related=related)
        with patch(
            "agent_builder.tools.impl.web_search.urllib.request.urlopen",
            return_value=_FakeResp(body),
        ):
            call = registry.execute(
                gatekeeper, _make_call("operator", "x", max_results=5)
            )
        lines = call.result.split("\n")
        assert len(lines) == 6  # 5 条 + 1 条截断
        assert "已截断" in lines[-1]


# ── 注册 ────────────────────────────────────────────────────────


class TestWebSearchRegistration:
    def test_registered(self):
        assert "web_search" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("web_search")
        assert spec.name == "web_search"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 30.0
        assert spec.allowed_roles == ["operator"]
        assert "query" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestWebSearchConstants:
    def test_ddg_api_url(self):
        assert DDG_API_URL == "https://api.duckduckgo.com/"

    def test_default_max_results(self):
        assert DEFAULT_MAX_RESULTS == 10

    def test_absolute_max_positive(self):
        assert ABSOLUTE_MAX > 0
