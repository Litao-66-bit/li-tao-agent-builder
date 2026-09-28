"""diff_preview 工具测试：功能 / 边界 / 安全 / 成本。

纯计算工具（difflib），无文件/网络访问，无需 mock。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.diff_preview import DEFAULT_CONTEXT, MAX_CONTENT_CHARS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper():
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["diff_preview"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, correlation_id="c-test")


def _make_call(role, old, new, *, context=None, label=None, audit_id="a-1"):
    args: dict = {"old_content": old, "new_content": new}
    if context is not None:
        args["context"] = context
    if label is not None:
        args["label"] = label
    return ToolCall(audit_id=audit_id, role=role, tool="diff_preview", args=args)


# ── 功能 ────────────────────────────────────────────────────────


class TestDiffPreviewFunctional:
    def test_diff_with_changes(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "line1\nline2\n", "line1\nlineX\n")
        )
        assert "---" in call.result
        assert "+++" in call.result
        assert "-line2" in call.result
        assert "+lineX" in call.result
        assert call.status == "executed"

    def test_no_differences(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "same\n", "same\n")
        )
        assert call.result == "(no differences)"

    def test_label_in_header(self, gatekeeper):
        call = registry.execute(
            gatekeeper, _make_call("operator", "a\n", "b\n", label="config.py")
        )
        assert "config.py (old)" in call.result
        assert "config.py (new)" in call.result

    def test_context_lines(self, gatekeeper):
        """context 参数控制上下文行数。"""
        old = "l1\nl2\nl3\nl4\nl5\n"
        new = "l1\nl2\nCHANGED\nl4\nl5\n"
        # context=0：只显示变更行，不显示上下文
        call = registry.execute(
            gatekeeper, _make_call("operator", old, new, context=0)
        )
        assert "-l3" in call.result
        assert "+CHANGED" in call.result
        # context=0 时 l1/l2/l4/l5 不出现在 diff 行中
        diff_lines = call.result.split("\n")
        assert not any(ln.startswith("-l1") for ln in diff_lines)


# ── 边界 ────────────────────────────────────────────────────────


class TestDiffPreviewEdge:
    def test_both_empty(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", "", ""))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_old_empty_ok(self, gatekeeper):
        """一侧为空是合法的（纯新增/纯删除）。"""
        call = registry.execute(gatekeeper, _make_call("operator", "", "new\n"))
        assert "+++" in call.result

    def test_content_too_long(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "x" * (MAX_CONTENT_CHARS + 1), "y")
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_negative_context(self, gatekeeper):
        with pytest.raises(AgentError) as exc_info:
            registry.execute(
                gatekeeper, _make_call("operator", "a\n", "b\n", context=-1)
            )
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_default_context(self, gatekeeper):
        """不传 context 时默认 DEFAULT_CONTEXT。"""
        old = "l1\nl2\nl3\nl4\nl5\nl6\nl7\n"
        new = "l1\nl2\nl3\nCHANGED\nl5\nl6\nl7\n"
        call = registry.execute(gatekeeper, _make_call("operator", old, new))
        # 默认 context=3：l1/l2/l3 和 l5/l6/l7 作为上下文出现
        assert "l1" in call.result  # 上下文行


# ── 安全 ────────────────────────────────────────────────────────


class TestDiffPreviewSecurity:
    def test_unregistered_role_denied(self, gatekeeper):
        call = _make_call("intruder", "a\n", "b\n")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper):
        registry.execute(
            gatekeeper, _make_call("operator", "a\n", "b\n", audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "diff_preview"
        assert audit.audit_id == "a-log"


# ── 注册 ────────────────────────────────────────────────────────


class TestDiffPreviewRegistration:
    def test_registered(self):
        assert "diff_preview" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("diff_preview")
        assert spec.name == "diff_preview"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 10.0
        assert spec.allowed_roles == ["operator"]
        assert "old_content" in spec.parameters["required"]
        assert "new_content" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestDiffPreviewConstants:
    def test_max_content_chars_positive(self):
        assert MAX_CONTENT_CHARS > 0

    def test_default_context_positive(self):
        assert DEFAULT_CONTEXT >= 0
