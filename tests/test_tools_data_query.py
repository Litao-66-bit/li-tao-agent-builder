"""data_query 工具测试：功能 / 边界 / 安全 / 成本（截断）。

用真实 CSV/JSON 文件测试，不 mock 文件系统。
路径沙箱校验通过真实门卫跑。
"""

from __future__ import annotations

import json

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import RolePerm, ToolCall
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.impl.data_query import DEFAULT_LIMIT, MAX_ROWS
from agent_builder.tools.registry import registry


@pytest.fixture
def gatekeeper(tmp_path):
    perms = {
        "operator": RolePerm(
            role="operator",
            allowed_tools=["data_query"],
            high_risk_tools=[],
        ),
    }
    return ToolGatekeeper(perms, workspace_dir=tmp_path, correlation_id="c-test")


def _make_call(role, path, *, limit=None, tool="data_query", audit_id="a-1"):
    args: dict = {"path": str(path)}
    if limit is not None:
        args["limit"] = limit
    return ToolCall(audit_id=audit_id, role=role, tool=tool, args=args)


def _write_csv(path, rows):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


# ── 功能 ────────────────────────────────────────────────────────


class TestDataQueryFunctional:
    def test_read_csv(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "data.csv"
        _write_csv(csv_file, [["name", "age"], ["Alice", "30"], ["Bob", "25"]])
        call = registry.execute(gatekeeper, _make_call("operator", csv_file))
        result = call.result
        assert "name" in result
        assert "Alice" in result
        assert "Bob" in result
        assert call.status == "executed"

    def test_read_json(self, gatekeeper, tmp_path):
        json_file = tmp_path / "data.json"
        json_file.write_text(
            json.dumps([{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]),
            encoding="utf-8",
        )
        call = registry.execute(gatekeeper, _make_call("operator", json_file))
        result = call.result
        assert "Alice" in result
        assert "Bob" in result

    def test_read_json_object(self, gatekeeper, tmp_path):
        json_file = tmp_path / "obj.json"
        json_file.write_text(
            json.dumps({"key": "value", "count": 42}),
            encoding="utf-8",
        )
        call = registry.execute(gatekeeper, _make_call("operator", json_file))
        result = call.result
        assert "key" in result
        assert "value" in result
        assert "42" in result


# ── 边界 ────────────────────────────────────────────────────────


class TestDataQueryEdge:
    def test_empty_path(self, gatekeeper):
        call = ToolCall(
            audit_id="a-e1", role="operator", tool="data_query", args={"path": ""}
        )
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        # 缺参数 → E_VALIDATION（不是 E_PERMISSION）。
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_invalid_limit(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "data.csv"
        _write_csv(csv_file, [["a"], ["b"]])
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", csv_file, limit=0))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_nonexistent_file(self, gatekeeper, tmp_path):
        call = _make_call("operator", tmp_path / "noexist.csv")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_unsupported_format(self, gatekeeper, tmp_path):
        txt_file = tmp_path / "data.txt"
        txt_file.write_text("hello", encoding="utf-8")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", txt_file))
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_csv(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "empty.csv"
        csv_file.write_text("", encoding="utf-8")
        call = registry.execute(gatekeeper, _make_call("operator", csv_file))
        assert "empty" in call.result.lower()


# ── 安全 ────────────────────────────────────────────────────────


class TestDataQuerySecurity:
    def test_path_outside_sandbox_denied(self, gatekeeper):
        call = _make_call("operator", "/etc/passwd")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_unregistered_role_denied(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "data.csv"
        _write_csv(csv_file, [["a"], ["b"]])
        call = _make_call("intruder", csv_file)
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, call)
        assert exc_info.value.error_name == "E_PERMISSION"

    def test_audit_logged(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "data.csv"
        _write_csv(csv_file, [["a"], ["b"]])
        registry.execute(
            gatekeeper, _make_call("operator", csv_file, audit_id="a-log")
        )
        audit = gatekeeper.audit_log[-1]
        assert audit.allowed is True
        assert audit.tool == "data_query"
        assert audit.audit_id == "a-log"


# ── 成本（截断）───────────────────────────────────────────────────


class TestDataQueryCost:
    def test_csv_truncated(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "big.csv"
        rows = [["val"]] + [[str(i)] for i in range(200)]
        _write_csv(csv_file, rows)
        call = registry.execute(
            gatekeeper, _make_call("operator", csv_file, limit=5)
        )
        lines = call.result.split("\n")
        # 5 行 + 1 条截断提示
        assert len(lines) == 6
        assert "已截断" in lines[-1]

    def test_json_array_truncated(self, gatekeeper, tmp_path):
        json_file = tmp_path / "big.json"
        data = [{"id": i} for i in range(200)]
        json_file.write_text(json.dumps(data), encoding="utf-8")
        call = registry.execute(
            gatekeeper, _make_call("operator", json_file, limit=5)
        )
        assert "已截断" in call.result

    def test_limit_capped_to_max(self, gatekeeper, tmp_path):
        csv_file = tmp_path / "cap.csv"
        rows = [["val"]] + [[str(i)] for i in range(MAX_ROWS + 100)]
        _write_csv(csv_file, rows)
        call = registry.execute(
            gatekeeper, _make_call("operator", csv_file, limit=99999)
        )
        # limit 被截到 MAX_ROWS
        lines = call.result.split("\n")
        assert len(lines) == MAX_ROWS + 1  # MAX_ROWS 行 + 截断提示
        assert "已截断" in lines[-1]


# ── 异常 ────────────────────────────────────────────────────────


class TestDataQueryErrors:
    def test_invalid_json(self, gatekeeper, tmp_path):
        json_file = tmp_path / "bad.json"
        json_file.write_text("not json at all", encoding="utf-8")
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", json_file))
        assert exc_info.value.error_name == "E_TOOL"
        assert "JSON" in exc_info.value.info.message

    def test_directory_not_file(self, gatekeeper, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        with pytest.raises(AgentError) as exc_info:
            registry.execute(gatekeeper, _make_call("operator", subdir))
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 注册 ────────────────────────────────────────────────────────


class TestDataQueryRegistration:
    def test_registered(self):
        assert "data_query" in registry.list_tools()

    def test_spec_fields(self):
        spec, _ = registry.get("data_query")
        assert spec.name == "data_query"
        assert spec.risk_level == "low"
        assert spec.cost_band == "low"
        assert spec.timeout_s == 15.0
        assert spec.allowed_roles == ["operator"]
        assert "path" in spec.parameters["required"]


# ── 常量 ────────────────────────────────────────────────────────


class TestDataQueryConstants:
    def test_default_limit_positive(self):
        assert DEFAULT_LIMIT > 0

    def test_max_rows_positive(self):
        assert MAX_ROWS > 0
        assert MAX_ROWS >= DEFAULT_LIMIT
