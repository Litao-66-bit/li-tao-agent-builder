"""registry 参数归一化（宽松）测试：别名映射 + 丢弃未声明键。

背景：编排层把分解器产出的自由 ``inputs`` 直接 ``impl(**args)`` 展开，多一个键
即抛 Python ``TypeError``（对模型/用户无意义的报错）。本测试覆盖：

- 单元：``normalize_args`` 的别名/丢弃/占用/非 dict 行为；
- 端到端：经 ``registry.execute`` 时，多余键被丢弃而非崩溃、别名键被正确映射、
  别名后的 ``path`` 仍能被门卫沙箱校验、缺必填给出可读 E_VALIDATION；
- 边界：未注册工具仍由门卫拒绝（本次不改动「臆造工具」行为）。
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.code_search import spec as code_search_spec
from agent_builder.tools.impl.file_list import spec as file_list_spec
from agent_builder.tools.impl.file_read import spec as file_read_spec
from agent_builder.tools.impl.web_search import spec as web_search_spec
from agent_builder.tools.registry import registry
from agent_builder.tools.spec import normalize_args

# ── 单元：normalize_args ────────────────────────────────────────


class TestNormalizeArgsUnit:
    def test_keeps_declared_keys(self):
        normalized, dropped = normalize_args(file_list_spec, {"path": "notes"})
        assert normalized == {"path": "notes"}
        assert dropped == []

    def test_alias_query_to_pattern_for_code_search(self):
        # code_search 声明 pattern、未声明 query → query 应映射为 pattern
        normalized, dropped = normalize_args(
            code_search_spec, {"path": "notes", "query": "TODO"}
        )
        assert normalized == {"path": "notes", "pattern": "TODO"}
        assert dropped == []

    def test_declared_query_is_not_aliased(self):
        # web_search 本身就用 query → 必须保持 query，不得映射成 pattern
        normalized, dropped = normalize_args(web_search_spec, {"query": "abc"})
        assert normalized == {"query": "abc"}
        assert dropped == []

    def test_unknown_key_is_dropped(self):
        # file_list 只有 path；pattern 无等价参数 → 丢弃（而非报错）
        normalized, dropped = normalize_args(
            file_list_spec, {"path": "notes", "pattern": "*.md", "recursive": True}
        )
        assert normalized == {"path": "notes"}
        assert sorted(dropped) == ["pattern", "recursive"]

    def test_alias_not_applied_when_target_undeclared(self):
        # pattern 是别名目标，但 file_list 未声明 pattern → 不得凭空造出该键
        normalized, dropped = normalize_args(file_list_spec, {"pattern": "*.md"})
        assert normalized == {}
        assert dropped == ["pattern"]

    def test_alias_source_dropped_when_target_occupied(self):
        normalized, dropped = normalize_args(
            code_search_spec, {"pattern": "a", "query": "b"}
        )
        assert normalized == {"pattern": "a"}
        assert dropped == ["query"]

    def test_alias_file_to_path_for_file_tools(self):
        normalized, dropped = normalize_args(file_read_spec, {"file": "notes/a.md"})
        assert normalized == {"path": "notes/a.md"}
        assert dropped == []

    def test_non_dict_args_returns_empty(self):
        normalized, dropped = normalize_args(file_list_spec, None)  # type: ignore[arg-type]
        assert normalized == {}
        assert dropped == []


# ── 端到端：registry.execute ────────────────────────────────────


@pytest.fixture
def workspace(tmp_path):
    """临时白名单目录（含一个 notes 子目录）。"""
    (tmp_path / "notes").mkdir()
    return tmp_path


def _gatekeeper(workspace, tools):
    """构造允许给定工具、且不设审批门的门卫（专注参数契约，不掺审批逻辑）。"""
    perms = {
        "operator": RolePerm(role="operator", allowed_tools=list(tools), high_risk_tools=[]),
    }
    return ToolGatekeeper(perms, workspace_dir=workspace, correlation_id="c-args")


class TestRegistryNormalizationEndToEnd:
    def test_file_list_extra_key_no_longer_fails(self, workspace):
        # 历史行为：list_dir() got an unexpected keyword argument 'pattern' → 失败
        (workspace / "notes" / "a.md").write_text("x", encoding="utf-8")
        gk = _gatekeeper(workspace, ["file_list"])
        call = ToolCall(
            audit_id="a-1",
            role="operator",
            tool="file_list",
            args={"path": str(workspace / "notes"), "pattern": "*.md"},
        )
        result = registry.execute(gk, call)
        assert result.status == "executed"
        assert "a.md" in result.result

    def test_alias_reaches_impl_and_passes_sandbox(self, workspace):
        # 别名 file → path：须先于门卫生效，否则沙箱会因缺 path 直接拒绝
        (workspace / "notes" / "b.md").write_text("hello", encoding="utf-8")
        gk = _gatekeeper(workspace, ["file_read"])
        call = ToolCall(
            audit_id="a-2",
            role="operator",
            tool="file_read",
            args={"file": str(workspace / "notes" / "b.md")},
        )
        result = registry.execute(gk, call)
        assert result.status == "executed"
        assert result.result == "hello"

    def test_file_write_extra_key_dropped(self, workspace):
        gk = _gatekeeper(workspace, ["file_write"])
        call = ToolCall(
            audit_id="a-3",
            role="operator",
            tool="file_write",
            args={
                "path": str(workspace / "notes" / "out.md"),
                "content": "body",
                "mode": "w",
            },
        )
        result = registry.execute(gk, call)
        assert result.status == "executed"
        assert (workspace / "notes" / "out.md").read_text(encoding="utf-8") == "body"

    def test_missing_required_raises_readable_validation(self, workspace):
        # code_search 必填 path+pattern；只给 path → 可读 E_VALIDATION（不调 rg）
        gk = _gatekeeper(workspace, ["code_search"])
        call = ToolCall(
            audit_id="a-4",
            role="operator",
            tool="code_search",
            args={"path": str(workspace / "notes")},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)
        assert exc_info.value.error_name == "E_VALIDATION"
        assert "缺少必填参数" in exc_info.value.info.message
        assert "pattern" in exc_info.value.info.message

    def test_audit_records_normalized_args(self, workspace):
        gk = _gatekeeper(workspace, ["file_list"])
        call = ToolCall(
            audit_id="a-5",
            role="operator",
            tool="file_list",
            args={"path": str(workspace), "pattern": "*.md"},
        )
        registry.execute(gk, call)
        assert "pattern" not in gk.audit_log[-1].args

    def test_dropped_keys_logged(self, workspace, caplog):
        gk = _gatekeeper(workspace, ["file_list"])
        call = ToolCall(
            audit_id="a-6",
            role="operator",
            tool="file_list",
            args={"path": str(workspace), "recursive": True},
        )
        with caplog.at_level("WARNING", logger="agent_builder.tools.registry"):
            registry.execute(gk, call)
        assert any("忽略未声明参数" in rec.message for rec in caplog.records)


# ── 边界：未注册工具不动（本次范围外）─────────────────────────────


class TestUnknownToolUnchanged:
    def test_unregistered_tool_still_denied_by_gatekeeper(self, workspace):
        # file_delete 不在角色白名单 → 仍由门卫拒绝（臆造工具问题另行处理）
        gk = _gatekeeper(workspace, ["file_list"])
        call = ToolCall(
            audit_id="a-7",
            role="operator",
            tool="file_delete",
            args={"path": str(workspace / "notes")},
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gk, call)
        assert exc_info.value.error_name == "E_PERMISSION"


# ── 契约一致性：别名表不得覆盖任何工具的声明参数 ────────────────────


class TestAliasTableConsistency:
    def test_alias_targets_are_never_declared_alongside_source(self):
        """别名只在「源未声明」时生效；本测试确保别名不会静默改写真源参数名。"""
        from agent_builder.tools.spec import ARG_ALIASES

        for tool in registry.list_tools():
            spec, _ = registry.get(tool)
            props = set((spec.parameters.get("properties") or {}).keys())
            for source in ARG_ALIASES:
                if source in props:
                    # 工具自己声明了该名 → 归一化时必须保留原名（不映射）
                    normalized, _ = normalize_args(spec, {source: "v"})
                    assert normalized == {source: "v"}
