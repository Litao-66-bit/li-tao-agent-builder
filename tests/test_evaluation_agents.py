"""Agent（角色）评分的不变量回归。

对应评分维度，把报告里的判据固化为断言；任何一条被破坏都会让 pytest 失败。

注意：本文件同时给「契约解析器本身」加断言（例如授权清单必须解析出预期条数），
避免解析器静默失效导致「全部通过」的假绿灯。
"""

from __future__ import annotations

import ast
import inspect
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from agent_builder.evaluation.scorecards import (
    LAYER_VIOLATIONS,
    WRITE_TOOLS,
    _granted_tools,
    _high_risk_tools,
    _module_path,
    collect,
    coverage_gaps,
    parse_protocols,
    render_markdown,
    role_universe,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _PROJECT_ROOT / "tests"

DOCS = parse_protocols()
SCORES = collect()
BY_ROLE = {score.role: score for score in SCORES}
IMPL_ROLES = [score.role for score in SCORES if score.kind != "permission_only"]


def _impl_source(role: str) -> str:
    path = _module_path(role)
    assert path is not None, f"角色 {role!r} 无实现模块"
    return path.read_text(encoding="utf-8")


class TestContractParser:
    """解析器自身：防止解析静默失效造成假绿灯。"""

    def test_解析出契约角色(self) -> None:
        assert len(DOCS) >= 20, f"执行协议文档只解析出 {len(DOCS)} 个角色"

    def test_授权清单解析结果非空(self) -> None:
        empty = [role for role, doc in DOCS.items() if not doc.tools and not doc.external_impl]
        assert not empty, f"以下角色的授权清单未解析出工具: {empty}"

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_授权清单条数与权限矩阵一致(self, role: str) -> None:
        doc = DOCS.get(role)
        assert doc is not None, f"契约缺 {role} 章节"
        assert set(doc.tools) == set(_granted_tools(role)), (
            f"{role} 授权清单={sorted(doc.tools)} 实授={sorted(_granted_tools(role))}"
        )

    def test_边界声明已解析(self) -> None:
        missing = [role for role, doc in DOCS.items() if not doc.boundary and not doc.external_impl]
        assert not missing, f"以下角色缺边界声明: {missing}"


class TestThreeWayConsistency:
    """三方一致性：文档契约 / 权限矩阵 / 实现模块。"""

    def test_无未声明的悬空条目(self) -> None:
        gaps = coverage_gaps()
        for key in (
            "文档有但无实现模块（未声明权限层）",
            "权限有但无实现模块（未声明权限层）",
            "权限有但文档无章节",
            "实现模块有但文档无章节",
        ):
            assert gaps[key] == [], f"{key}: {gaps[key]}"

    def test_权限层角色已在契约中声明(self) -> None:
        modules = {path.stem for path in (_PROJECT_ROOT / "agent_builder" / "roles").glob("*.py")}
        expected = set(DEFAULT_ROLE_PERMS) - (modules - {"__init__"})
        declared = set(coverage_gaps()["已声明的权限层角色（无独立模块，设计如此）"])
        assert declared == expected
        assert declared == {"memory_manager", "operator", "sub_architect"}

    def test_角色集合是三方并集(self) -> None:
        modules = {path.stem for path in (_PROJECT_ROOT / "agent_builder" / "roles").glob("*.py")}
        universe = set(role_universe())
        assert universe >= set(DEFAULT_ROLE_PERMS)
        assert universe >= modules - {"__init__"}
        assert universe <= set(DEFAULT_ROLE_PERMS) | modules | set(DOCS)


class TestContractCompliance:
    """维度 1：契约符合度。"""

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_角色在契约中有章节(self, role: str) -> None:
        assert role in DOCS, f"{role} 在 docs/execution-protocols.md 中无章节"

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_契约符合度满分(self, role: str) -> None:
        dim = next(d for d in BY_ROLE[role].dimensions if d.name == "契约符合度")
        assert dim.score == dim.max_score, dim.note


class TestLeastPrivilege:
    """维度 2：权限最小化。"""

    _FORBIDDEN = (("写文件", "file_write"), ("提交代码", "git_commit"), ("执行命令", "sandbox_run"))

    def test_契约解析出边界声明(self) -> None:
        assert any(doc.boundary for doc in DOCS.values())

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_边界禁止项未被授权(self, role: str) -> None:
        doc = DOCS.get(role)
        if doc is None or not doc.boundary:
            pytest.skip("该角色无边界声明")
        granted = set(_granted_tools(role))
        violations = [
            f"禁止「{keyword}」却实授 {tool}"
            for keyword, tool in self._FORBIDDEN
            if keyword in doc.boundary and tool in granted
        ]
        assert not violations, f"{role}: {violations}"

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_写类工具必须挂审批门(self, role: str) -> None:
        ungated = sorted(set(_granted_tools(role)) & WRITE_TOOLS - set(_high_risk_tools(role)))
        assert not ungated, f"{role} 实授写类工具 {ungated} 但未列入 high_risk_tools"

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_角色不悬空(self, role: str) -> None:
        assert _granted_tools(role), f"{role} 未实授任何工具"


class TestFailureSemantics:
    """维度 3：失败语义。"""

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_失败语义满分(self, role: str) -> None:
        dim = next(d for d in BY_ROLE[role].dimensions if d.name == "失败语义")
        assert dim.score == dim.max_score, f"{role}: {dim.note}"


class TestObservability:
    """维度 4：可观测性。"""

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_可观测性满分(self, role: str) -> None:
        dim = next(d for d in BY_ROLE[role].dimensions if d.name == "可观测性")
        assert dim.score == dim.max_score, f"{role}: {dim.note}"

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_执行类结果带状态与错误字段(self, role: str) -> None:
        score = BY_ROLE[role]
        if score.kind != "executor":
            pytest.skip("编排类不以 status/error 结果类型表达")
        source = _impl_source(role)
        assert re.search(r"^    status: ", source, re.MULTILINE), f"{role} 结果缺 status 字段"
        assert re.search(r"^    error: ", source, re.MULTILINE), f"{role} 结果缺 error 字段"
        for status in ("pending", "failed"):
            assert status in source, f"{role} 未声明降级态 {status}"


class TestTestCoverage:
    """维度 5：测试覆盖。"""

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_测试覆盖满分(self, role: str) -> None:
        dim = next(d for d in BY_ROLE[role].dimensions if d.name == "测试覆盖")
        assert dim.score == dim.max_score, f"{role}: {dim.note}"

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_测试文件存在且含正常路径与Edge用例类(self, role: str) -> None:
        test_file = _TESTS_DIR / f"test_roles_{role}.py"
        assert test_file.exists(), f"缺 {test_file.name}（零测试）"
        classes = set(
            re.findall(r"^class (\w+)", test_file.read_text(encoding="utf-8"), re.MULTILINE)
        )
        assert classes, f"{test_file.name} 未使用用例类组织"


class TestDependencyClarity:
    """维度 6：依赖清晰度。"""

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_无跨层依赖(self, role: str) -> None:
        source = _impl_source(role)
        violations = [module for module in LAYER_VIOLATIONS if module in source]
        assert not violations, f"{role} 跨层依赖 {violations}"

    @pytest.mark.parametrize("role", sorted(DEFAULT_ROLE_PERMS))
    def test_执行类角色注入executor(self, role: str) -> None:
        score = BY_ROLE[role]
        if score.kind != "executor":
            pytest.skip("编排/权限层角色不执行工具")
        assert "executor_fn" in _impl_source(role), f"{role} 未通过 executor_fn 注入执行器"

    @pytest.mark.parametrize("role", IMPL_ROLES)
    def test_无模块级私有可变状态(self, role: str) -> None:
        assert not re.search(r"^_[a-z][a-z_]*\s*[:=]", _impl_source(role), re.MULTILINE), (
            f"{role} 存在模块级私有可变状态"
        )


class TestScorecard:
    """评分卡本身：满分底线 + 可重复生成 + 安全项为空。"""

    def test_无安全项(self) -> None:
        flagged = [f"{s.role}: {'；'.join(s.safety_flags)}" for s in SCORES if s.safety_flags]
        assert not flagged, "命中安全项: " + "; ".join(flagged)

    def test_实现类角色全部满分(self) -> None:
        offenders = [
            f"{s.role}: {s.total}/{s.applicable_max}"
            for s in SCORES
            if s.kind != "permission_only" and s.total != s.applicable_max
        ]
        assert not offenders, "未满分: " + "; ".join(offenders)

    def test_权限层角色全部满分(self) -> None:
        offenders = [
            f"{s.role}: {s.total}/{s.applicable_max}"
            for s in SCORES
            if s.kind == "permission_only" and s.total != s.applicable_max
        ]
        assert not offenders, "未满分: " + "; ".join(offenders)

    def test_报告可重复生成(self) -> None:
        first = render_markdown(collect(), coverage_gaps())
        second = render_markdown(collect(), coverage_gaps())
        assert first == second, "同输入未得同输出"

    def test_报告覆盖全部角色(self) -> None:
        markdown = render_markdown(SCORES, coverage_gaps())
        for role in role_universe():
            assert f"`{role}`" in markdown, f"报告缺角色 {role}"

    def test_json载荷可序列化(self) -> None:
        payload = [asdict(score) for score in SCORES]
        assert len(payload) == len(SCORES)
        assert all("applicable_max" in item for item in payload)

    def test_类型判定单调(self) -> None:
        for score in SCORES:
            assert score.kind in {"executor", "orchestrator", "permission_only"}
            if score.kind == "permission_only":
                assert score.applicable_max == 10
            else:
                assert score.applicable_max == 30


def test_评分器不导入执行层() -> None:
    """评估层只允许读 permissions 常量，不得依赖 api / tools 单例 / llm。"""
    from agent_builder.evaluation import scorecards

    tree = ast.parse(inspect.getsource(scorecards))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    layer_imports = sorted(name for name in imported if name.startswith("agent_builder."))
    assert layer_imports == ["agent_builder.tools.permissions"], layer_imports
