"""DataAnalyst 数据分析者角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：执行/结论分类/缺失率超阈值/拒绝/权限不足
- 边界：空步骤
- 授权：data_analyst 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.data_analyst import (
    CONCLUSION_TYPES,
    DATA_ACTIONS,
    MAX_MISSING_RATIO,
    DataAnalyst,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_analyst() -> DataAnalyst:
    return DataAnalyst(correlation_id="c-test")


def _make_step(
    action: str = "data_query",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestDataAnalystFunctional:
    def test_execute_success(self):
        """执行成功 → done + conclusions + calc_process。"""
        a = _make_analyst()
        step = _make_step("data_query", {
            "conclusions": [
                {"content": "均值 42", "type": "fact", "caliber": "全量"},
                {"content": "趋势上升", "type": "inference", "caliber": "近 7 天"},
                {"content": "原因待查", "type": "pending_verification"},
            ],
            "calc_process": "sum / count = 42",
            "caliber_desc": "全量统计",
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.conclusions) == 3
        assert result.conclusions[0].content == "均值 42"
        assert result.conclusions[0].type == "fact"
        assert result.conclusions[1].type == "inference"
        assert result.conclusions[2].type == "pending_verification"
        assert result.calc_process == "sum / count = 42"
        assert result.caliber_desc == "全量统计"

    def test_execute_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        a = _make_analyst()
        step = _make_step("data_query")
        result = a.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_data_action(self):
        """非数据类 → rejected。"""
        a = _make_analyst()
        step = _make_step("web_search")
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出数据范围" in result.error

    def test_quality_report_high_missing(self):
        """缺失率超阈值 → quality_report（不下结论）。"""
        a = _make_analyst()
        step = _make_step("data_query", {"missing_ratio": 0.5})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "quality_report"
        assert "缺失率" in result.quality_report
        assert result.conclusions == []

    def test_quality_report_at_threshold(self):
        """缺失率等于阈值 → 不触发质量报告（正常执行）。"""
        a = _make_analyst()
        step = _make_step("data_query", {"missing_ratio": MAX_MISSING_RATIO})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_permission_denied(self):
        """权限不足 → failed。"""
        a = _make_analyst()
        step = _make_step("data_query")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        a = _make_analyst()
        step = _make_step("data_query")

        def fn(s: Step) -> str:
            raise RuntimeError("计算失败")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_conclusion_type_validation(self):
        """非法结论类型 → 归入 pending_verification。"""
        a = _make_analyst()
        step = _make_step("data_query", {
            "conclusions": [
                {"content": "未知类型", "type": "invalid_type"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.conclusions[0].type == "pending_verification"

    def test_sandbox_run_accepted(self):
        """sandbox_run 是数据类 action。"""
        a = _make_analyst()
        step = _make_step("sandbox_run", {"command": "python calc.py"})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_custom_threshold(self):
        """自定义缺失率阈值。"""
        a = DataAnalyst(correlation_id="c-test", max_missing_ratio=0.1)
        step = _make_step("data_query", {"missing_ratio": 0.2})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "quality_report"


# ── 边界 ────────────────────────────────────────────────────────


class TestDataAnalystEdge:
    def test_empty_conclusions(self):
        """无结论 → 空清单。"""
        a = _make_analyst()
        step = _make_step("data_query")
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.conclusions == []

    def test_zero_missing_ratio(self):
        """缺失率 0 → 正常执行。"""
        a = _make_analyst()
        step = _make_step("data_query", {"missing_ratio": 0.0})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"


# ── 授权 ────────────────────────────────────────────────────────


class TestDataAnalystPermissions:
    def test_role_registered(self):
        assert "data_analyst" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["data_analyst"]
        assert set(perm.allowed_tools) == {
            "data_query",
            "sandbox_run",
            "file_read",
            "memory_read",
            "audit_log",
            "citation_check",
        }

    def test_no_high_risk(self):
        """data_analyst 无高风险工具（只读+计算）。"""
        perm = DEFAULT_ROLE_PERMS["data_analyst"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """data_analyst 无写文件/提交/回滚工具。"""
        perm = DEFAULT_ROLE_PERMS["data_analyst"]
        forbidden = {"file_write", "git_commit", "rollback", "test_run", "web_fetch"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestDataAnalystConstants:
    def test_data_actions_nonempty(self):
        assert len(DATA_ACTIONS) > 0
        assert "data_query" in DATA_ACTIONS
        assert "sandbox_run" in DATA_ACTIONS

    def test_data_actions_excludes_non_data(self):
        assert "web_search" not in DATA_ACTIONS
        assert "file_write" not in DATA_ACTIONS
        assert "code_search" not in DATA_ACTIONS

    def test_conclusion_types(self):
        assert "fact" in CONCLUSION_TYPES
        assert "inference" in CONCLUSION_TYPES
        assert "pending_verification" in CONCLUSION_TYPES

    def test_max_missing_ratio_valid(self):
        assert 0 < MAX_MISSING_RATIO < 1
