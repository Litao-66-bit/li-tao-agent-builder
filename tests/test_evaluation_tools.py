"""工具层评估的不变量回归。

报告（docs/reports/tools-scorecard.md）只呈现结论；本文件把同样的判据固化成
断言，任何一条被破坏都会让 pytest 失败，而不是悄悄留在报告里。

对应评分维度：元数据完整性 / Schema 质量 / 最小权限 / 风险分级 / 错误语义 / 测试覆盖。
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_builder.evaluation.tool_report import (
    EXEC_TOOLS,
    MUTATING_TOOLS,
    _approval_gated_tools,
    _grants_by_tool,
    collect,
    doc_drift,
    render_markdown,
)
from agent_builder.tools import registry
from agent_builder.tools.spec import VALID_COST_BANDS, VALID_RISK_LEVELS, ToolSpec

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _PROJECT_ROOT / "tests"

TOOL_NAMES = registry.list_tools()


def _spec(tool: str) -> ToolSpec:
    entry = registry.get(tool)
    assert entry is not None, f"工具 {tool!r} 未注册"
    return entry[0]


def _impl_source(tool: str) -> str:
    entry = registry.get(tool)
    assert entry is not None
    path = inspect.getsourcefile(entry[1])
    assert path is not None, f"工具 {tool!r} 的实现无法定位源码"
    return Path(path).read_text(encoding="utf-8")


class TestInventory:
    """清单真源：registry.list_tools()。"""

    def test_清单非空(self) -> None:
        assert TOOL_NAMES, "注册表为空，评估无从谈起"
        assert TOOL_NAMES == sorted(TOOL_NAMES), "list_tools() 应返回有序清单"

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_每个工具都有可调用实现(self, tool: str) -> None:
        spec, impl = registry.get(tool)
        assert spec.name == tool
        assert callable(impl)

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_每个工具都有测试文件(self, tool: str) -> None:
        test_file = _TESTS_DIR / f"test_tools_{tool}.py"
        assert test_file.exists(), f"缺 {test_file.name}（零测试）"


class TestMetadataContract:
    """维度 1：元数据完整性。"""

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_七要素齐全且合法(self, tool: str) -> None:
        spec = _spec(tool)
        assert spec.name == tool
        assert len(spec.description.strip()) >= 10, "description 过短"
        assert isinstance(spec.parameters, dict) and spec.parameters, "parameters 缺失"
        assert spec.risk_level in VALID_RISK_LEVELS
        assert spec.cost_band in VALID_COST_BANDS
        assert 0 < spec.timeout_s <= 300.0, f"timeout_s 不合理: {spec.timeout_s}"
        assert spec.allowed_roles, "allowed_roles 不能为空"


class TestSchemaContract:
    """维度 2：Schema 质量（file_read 是良好范例）。"""

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_schema_约束完备(self, tool: str) -> None:
        params = _spec(tool).parameters
        assert params.get("type") == "object", "parameters.type 必须是 object"
        props = params.get("properties")
        assert isinstance(props, dict) and props, "properties 必须是非空对象"
        assert params.get("additionalProperties") is False, "必须显式禁止额外字段"
        required = params.get("required")
        assert isinstance(required, list), "required 必须是数组（无必填项时给空数组）"
        missing_from_props = [name for name in required if name not in props]
        assert not missing_from_props, f"required 含未声明字段: {missing_from_props}"
        for name, schema in props.items():
            assert isinstance(schema, dict), f"属性 {name} 的 schema 必须是对象"
            assert schema.get("type"), f"属性 {name} 缺 type"
            assert str(schema.get("description", "")).strip(), f"属性 {name} 缺 description"


class TestPermissionParity:
    """维度 3：最小权限 —— 执行真源是 permissions.py。"""

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_无越权声明(self, tool: str) -> None:
        declared = set(_spec(tool).allowed_roles)
        granted = set(_grants_by_tool().get(tool, ()))
        over = sorted(declared - granted)
        assert not over, f"{tool} 声明了未实授的角色（越权声明）: {over}"

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_无悬空工具(self, tool: str) -> None:
        granted = _grants_by_tool().get(tool, ())
        assert granted, f"{tool} 未被任何角色实授（悬空工具，在 permissions.py 中不可达）"

    def test_权限矩阵引用的工具都已注册(self) -> None:
        registered = set(TOOL_NAMES)
        referenced = set(_grants_by_tool()) | _approval_gated_tools()
        assert referenced <= registered, f"权限矩阵引用了未注册工具: {sorted(referenced - registered)}"


class TestRiskGrading:
    """维度 4：风险分级正确。"""

    @pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
    def test_写类工具非low且进审批门(self, tool: str) -> None:
        spec = _spec(tool)
        assert spec.risk_level != "low", f"{tool} 是写/提交/回滚类，不能标为 low"
        assert tool in _approval_gated_tools(), f"{tool} 必须列入某角色的 high_risk_tools"

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_高风险工具必须进审批门(self, tool: str) -> None:
        spec = _spec(tool)
        if spec.risk_level == "high":
            assert tool in _approval_gated_tools(), (
                f"{tool} risk_level=high 但不在任何 high_risk_tools，审批门失效"
            )

    @pytest.mark.parametrize("tool", sorted(EXEC_TOOLS))
    def test_执行类工具不得为low(self, tool: str) -> None:
        assert _spec(tool).risk_level != "low", f"{tool} 会执行外部命令/代码，不能标为 low"


class TestErrorSemantics:
    """维度 5：错误语义。"""

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_走契约异常而非内置异常(self, tool: str) -> None:
        source = _impl_source(tool)
        assert any(
            token in source
            for token in ("validation_error", "tool_error", "permission_error")
        ), f"{tool} 未见契约异常构造"

    @pytest.mark.parametrize("tool", TOOL_NAMES)
    def test_不吞异常(self, tool: str) -> None:
        source = _impl_source(tool)
        assert not re.search(r"except(\s+Exception)?\s*:\s*\n\s*pass\b", source), (
            f"{tool} 存在兜底吞异常（except: pass / except Exception: pass）"
        )


class TestDocParity:
    """文档漂移：docs/tools.md ↔ registry。"""

    def test_文档与注册表一一对应(self) -> None:
        drift = doc_drift()
        assert drift["未收录进文档"] == [], f"未收录进文档: {drift['未收录进文档']}"
        assert drift["角色声明漂移"] == [], "文档角色声明与 permissions.py 不一致: " + "; ".join(
            drift["角色声明漂移"]
        )


class TestScorecard:
    """评分卡本身：满分底线 + 可重复生成。"""

    def test_全部工具满分(self) -> None:
        offenders = []
        for score in collect():
            if score.total == 30:
                continue
            deficits = [f"{d.name}={d.score}/{d.max_score}" for d in score.dimensions if d.score < d.max_score]
            offenders.append(f"{score.tool}: {', '.join(deficits)}")
        assert not offenders, "以下工具未满分（安全优先 → 错误语义 → 测试）: " + "; ".join(offenders)

    def test_无安全项(self) -> None:
        flagged = [f"{s.tool}: {'；'.join(s.safety_flags)}" for s in collect() if s.safety_flags]
        assert not flagged, "命中安全项: " + "; ".join(flagged)

    def test_报告可重复生成(self) -> None:
        first = render_markdown(collect(), doc_drift())
        second = render_markdown(collect(), doc_drift())
        assert first == second, "同输入未得同输出"

    def test_报告覆盖全部工具(self) -> None:
        markdown = render_markdown(collect(), doc_drift())
        for tool in TOOL_NAMES:
            assert f"`{tool}`" in markdown, f"报告缺工具 {tool}"

    def test_json_载荷可序列化且含全部工具(self) -> None:
        payload = [asdict(score) for score in collect()]
        dumped = json.dumps(payload, ensure_ascii=False)
        assert len(payload) == len(TOOL_NAMES)
        assert "safety_flags" in dumped
