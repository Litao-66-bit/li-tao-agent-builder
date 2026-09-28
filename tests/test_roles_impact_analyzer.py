"""ImpactAnalyzer 影响分析者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：波及范围/风险评估/高风险人工审/波及面不明确/拒绝/权限不足
- 边界：空步骤
- 授权：impact_analyzer 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.impact_analyzer import (
    CHANGE_TYPES,
    RISK_LEVELS,
    ImpactAnalyzer,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_analyzer() -> ImpactAnalyzer:
    return ImpactAnalyzer(correlation_id="c-test")


def _make_step(
    action: str = "impact_analyze",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestImpactAnalyzerFunctional:
    def test_analyze_success(self):
        """分析成功 → done + areas。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [
                {"file_path": "roles/conductor.py", "module": "conductor", "change_type": "modify", "risk_level": "low"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.areas) == 1
        assert result.risk_level == "low"
        assert result.needs_manual_review is False

    def test_high_risk_needs_manual_review(self):
        """风险高 → needs_manual_review=True + mitigation。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [
                {"file_path": "tools/gatekeeper.py", "module": "gatekeeper", "change_type": "modify", "risk_level": "high"},
            ],
            "mitigation": "先备份再改，改完跑全量测试",
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.risk_level == "high"
        assert result.needs_manual_review is True
        assert result.mitigation == "先备份再改，改完跑全量测试"

    def test_medium_risk_no_manual_review(self):
        """中风险 → 不需人工重点审。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [{"file_path": "f.py", "change_type": "modify", "risk_level": "medium"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.risk_level == "medium"
        assert result.needs_manual_review is False

    def test_mixed_risk_takes_highest(self):
        """混合风险 → 取最高。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [
                {"file_path": "f1.py", "risk_level": "low"},
                {"file_path": "f2.py", "risk_level": "high"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.risk_level == "high"

    def test_incomplete_when_no_areas(self):
        """波及面不明确 → incomplete=True。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {"areas": []})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.incomplete is True
        assert result.areas == []

    def test_assess_action_accepted(self):
        """assess 是影响分析类 action。"""
        a = _make_analyzer()
        step = _make_step("assess", {
            "areas": [{"file_path": "f.py", "risk_level": "low"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze")
        result = a.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_analyze_action(self):
        """非影响分析类 → rejected。"""
        a = _make_analyzer()
        step = _make_step("file_write")
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出影响分析范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {"areas": [{"file_path": "f.py"}]})

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze")

        def fn(s: Step) -> str:
            raise RuntimeError("分析错误")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_token_and_time_cost(self):
        """估算 token/时间成本。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [{"file_path": "f.py", "risk_level": "low"}],
            "token_cost": 500,
            "time_cost_s": 30.0,
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.token_cost == 500
        assert result.time_cost_s == 30.0

    def test_rollback_difficulty(self):
        """回滚难度。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [{"file_path": "f.py", "risk_level": "low"}],
            "rollback_difficulty": "medium",
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.rollback_difficulty == "medium"

    def test_invalid_risk_defaults_low(self):
        """非法风险等级 → 默认 low。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [{"file_path": "f.py", "risk_level": "invalid"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.areas[0].risk_level == "low"

    def test_invalid_change_type_defaults_modify(self):
        """非法变更类型 → 默认 modify。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {
            "areas": [{"file_path": "f.py", "change_type": "invalid"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.areas[0].change_type == "modify"


# ── 边界 ────────────────────────────────────────────────────────


class TestImpactAnalyzerEdge:
    def test_empty_inputs(self):
        """空输入 → incomplete。"""
        a = _make_analyzer()
        step = _make_step("impact_analyze", {})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.incomplete is True


# ── 授权 ────────────────────────────────────────────────────────


class TestImpactAnalyzerPermissions:
    def test_role_registered(self):
        assert "impact_analyzer" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["impact_analyzer"]
        assert set(perm.allowed_tools) == {
            "file_read",
            "code_search",
            "diff_preview",
            "memory_read",
            "config_read",
            "audit_log",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["impact_analyzer"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """impact_analyzer 无写文件/提交/回滚/网络工具（只分析不改）。"""
        perm = DEFAULT_ROLE_PERMS["impact_analyzer"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestImpactAnalyzerConstants:
    def test_risk_levels(self):
        assert "low" in RISK_LEVELS
        assert "medium" in RISK_LEVELS
        assert "high" in RISK_LEVELS

    def test_change_types(self):
        assert "add" in CHANGE_TYPES
        assert "modify" in CHANGE_TYPES
        assert "delete" in CHANGE_TYPES
