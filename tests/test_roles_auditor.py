"""Auditor 审计员角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：采集/异常标注/连续触发/缺失标注/拒绝/权限不足
- 边界：空步骤
- 授权：auditor 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.auditor import (
    ANOMALY_THRESHOLD,
    METRIC_NAMES,
    METRIC_STATUSES,
    Auditor,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_auditor() -> Auditor:
    return Auditor(correlation_id="c-test")


def _make_step(
    action: str = "metric_collect",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestAuditorFunctional:
    def test_collect_success(self):
        """采集成功 → done + metrics。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.95, "baseline": 0.9, "threshold": 0.85, "status": "normal"},
                {"name": "duration", "value": 2.5, "baseline": 3.0, "threshold": 5.0, "status": "normal"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.metrics) == 2
        assert result.abnormal_count == 0
        assert result.trigger_optimization is False

    def test_abnormal_metric(self):
        """异常指标 → abnormal_count=1。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.7, "baseline": 0.9, "threshold": 0.85, "status": "abnormal", "consecutive": 1},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.abnormal_count == 1
        # 单次异常 → 不触发优化
        assert result.trigger_optimization is False

    def test_consecutive_triggers_optimization(self):
        """连续 N 次异常 → 触发优化。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.7, "baseline": 0.9, "threshold": 0.85, "status": "abnormal", "consecutive": ANOMALY_THRESHOLD},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.trigger_optimization is True

    def test_below_threshold_no_trigger(self):
        """连续次数 < 阈值 → 不触发。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.7, "baseline": 0.9, "threshold": 0.85, "status": "abnormal", "consecutive": ANOMALY_THRESHOLD - 1},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.trigger_optimization is False

    def test_missing_data_marked(self):
        """数据缺失 → 标"采样不全"。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.0, "baseline": 0.9, "threshold": 0.85, "status": "missing"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.missing_count == 1
        assert result.metrics[0].status == "missing"

    def test_audit_action_accepted(self):
        """audit 是采集类 action。"""
        a = _make_auditor()
        step = _make_step("audit", {
            "metrics": [{"name": "token_count", "value": 100, "baseline": 200, "threshold": 500, "status": "normal"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        a = _make_auditor()
        step = _make_step("metric_collect")
        result = a.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_metric_action(self):
        """非采集类 → rejected。"""
        a = _make_auditor()
        step = _make_step("file_write")
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出观测范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        a = _make_auditor()
        step = _make_step("metric_collect")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        a = _make_auditor()
        step = _make_step("metric_collect")

        def fn(s: Step) -> str:
            raise RuntimeError("采集错误")

        result = a.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_unknown_metric_skipped(self):
        """未知指标名 → 跳过。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "unknown_metric", "value": 1, "status": "normal"},
                {"name": "success_rate", "value": 0.9, "status": "normal"},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert len(result.metrics) == 1  # 只保留已知指标

    def test_invalid_status_defaults_normal(self):
        """非法状态 → 默认 normal。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {
            "metrics": [{"name": "success_rate", "value": 0.9, "status": "invalid"}],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.metrics[0].status == "normal"


# ── 边界 ────────────────────────────────────────────────────────


class TestAuditorEdge:
    def test_empty_metrics(self):
        """无指标 → 空清单。"""
        a = _make_auditor()
        step = _make_step("metric_collect", {})
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.metrics == []

    def test_custom_threshold(self):
        """自定义连续异常阈值。"""
        a = Auditor(correlation_id="c-test", anomaly_threshold=5)
        step = _make_step("metric_collect", {
            "metrics": [
                {"name": "success_rate", "value": 0.7, "status": "abnormal", "consecutive": 4},
            ],
        })
        result = a.execute(step, executor_fn=lambda s: "ok")
        assert result.trigger_optimization is False  # 4 < 5


# ── 授权 ────────────────────────────────────────────────────────


class TestAuditorPermissions:
    def test_role_registered(self):
        assert "auditor" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["auditor"]
        assert set(perm.allowed_tools) == {
            "metric_collect",
            "audit_log",
            "memory_read",
            "config_read",
            "file_read",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["auditor"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """auditor 无写文件/提交/回滚/网络工具（只观测）。"""
        perm = DEFAULT_ROLE_PERMS["auditor"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestAuditorConstants:
    def test_metric_names_nonempty(self):
        assert len(METRIC_NAMES) > 0
        assert "success_rate" in METRIC_NAMES
        assert "error_types" in METRIC_NAMES
        assert "duration" in METRIC_NAMES
        assert "token_count" in METRIC_NAMES
        assert "boundary_violations" in METRIC_NAMES

    def test_metric_statuses(self):
        assert "normal" in METRIC_STATUSES
        assert "abnormal" in METRIC_STATUSES
        assert "missing" in METRIC_STATUSES

    def test_anomaly_threshold_positive(self):
        assert ANOMALY_THRESHOLD > 0
