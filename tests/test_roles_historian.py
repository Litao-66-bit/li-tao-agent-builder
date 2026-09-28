"""Historian 记录员角色测试：功能 / 边界 / 授权 / 常量。

覆盖：
- 功能：记录/版本历史/审批被拒也记录/冲突标注/变更说明/拒绝/权限不足
- 边界：空步骤
- 授权：historian 角色权限矩阵（无高风险）
"""

from __future__ import annotations

from agent_builder.contracts.schemas import Step
from agent_builder.roles.historian import (
    PIPELINE_STAGES,
    RECORD_ACTIONS,
    Historian,
)
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_historian() -> Historian:
    return Historian(correlation_id="c-test")


def _make_step(
    action: str = "record",
    inputs: dict | None = None,
    sid: str = "s1",
) -> Step:
    return Step(id=sid, action=action, inputs=inputs or {})


# ── 功能 ────────────────────────────────────────────────────────


class TestHistorianFunctional:
    def test_record_success(self):
        """记录成功 → done + records。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [
                {"version": "v1.0", "stage": "propose", "approved": True, "approver": "user-1", "timestamp": "2026-01-01T00:00:00Z"},
            ],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert len(result.records) == 1
        assert result.records[0].version == "v1.0"

    def test_rejected_approval_also_recorded(self):
        """审批被拒也记录（approved=False）。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [
                {"version": "v1.0", "stage": "approve", "approved": False, "approver": "user-1"},
            ],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.records[0].approved is False

    def test_conflict_marked(self):
        """记录冲突 → conflict_marked=True。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [{"version": "v1.0"}],
            "conflict_marked": True,
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.conflict_marked is True

    def test_change_notice_generated(self):
        """生成变更说明（含 diff 摘要）。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [
                {"version": "v1.0", "change_desc": "修复 bug", "diff_summary": "+1 -1", "approver": "user-1", "approved": True},
            ],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.change_notice != ""
        assert "v1.0" in result.change_notice
        assert "修复 bug" in result.change_notice

    def test_log_change_action_accepted(self):
        """log_change 是记录类 action。"""
        h = _make_historian()
        step = _make_step("log_change", {
            "records": [{"version": "v1.0"}],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"

    def test_no_fn_returns_pending(self):
        """executor_fn=None → pending。"""
        h = _make_historian()
        step = _make_step("record")
        result = h.execute(step, executor_fn=None)
        assert result.status == "pending"

    def test_reject_non_record_action(self):
        """非记录类 → rejected。"""
        h = _make_historian()
        step = _make_step("file_write")
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "rejected"
        assert "超出记录范围" in result.error

    def test_permission_denied(self):
        """权限不足 → failed。"""
        h = _make_historian()
        step = _make_step("record")

        def fn(s: Step) -> str:
            raise PermissionError("需要审批")

        result = h.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "权限不足" in result.error

    def test_execution_failure(self):
        """执行失败 → failed。"""
        h = _make_historian()
        step = _make_step("record")

        def fn(s: Step) -> str:
            raise RuntimeError("记录错误")

        result = h.execute(step, executor_fn=fn)
        assert result.status == "failed"
        assert "RuntimeError" in result.error

    def test_records_sorted_by_timestamp(self):
        """记录冲突 → 以时间戳为准排序。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [
                {"version": "v2.0", "timestamp": "2026-01-02T00:00:00Z"},
                {"version": "v1.0", "timestamp": "2026-01-01T00:00:00Z"},
            ],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.records[0].version == "v1.0"  # 时间戳早的排前面
        assert result.records[1].version == "v2.0"

    def test_invalid_stage_defaults_propose(self):
        """非法阶段 → 默认 propose。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [{"version": "v1.0", "stage": "invalid"}],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.records[0].stage == "propose"

    def test_pipeline_stages_recorded(self):
        """记录 4 阶段全流程。"""
        h = _make_historian()
        step = _make_step("record", {
            "records": [
                {"version": "v1.0", "stage": "propose", "timestamp": "2026-01-01T01:00:00Z"},
                {"version": "v1.0", "stage": "approve", "timestamp": "2026-01-01T02:00:00Z"},
                {"version": "v1.0", "stage": "apply", "timestamp": "2026-01-01T03:00:00Z"},
                {"version": "v1.0", "stage": "verify", "timestamp": "2026-01-01T04:00:00Z"},
            ],
        })
        result = h.execute(step, executor_fn=lambda s: "ok")
        stages = [r.stage for r in result.records]
        assert stages == ["propose", "approve", "apply", "verify"]


# ── 边界 ────────────────────────────────────────────────────────


class TestHistorianEdge:
    def test_empty_records(self):
        """空记录 → 空清单 + 空变更说明。"""
        h = _make_historian()
        step = _make_step("record", {})
        result = h.execute(step, executor_fn=lambda s: "ok")
        assert result.status == "done"
        assert result.records == []
        assert result.change_notice == ""


# ── 授权 ────────────────────────────────────────────────────────


class TestHistorianPermissions:
    def test_role_registered(self):
        assert "historian" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        perm = DEFAULT_ROLE_PERMS["historian"]
        assert set(perm.allowed_tools) == {
            "audit_log",
            "git_log",
            "memory_read",
            "memory_write",
            "change_notify",
            "file_read",
        }

    def test_no_high_risk(self):
        perm = DEFAULT_ROLE_PERMS["historian"]
        assert perm.high_risk_tools == []

    def test_no_write_tools(self):
        """historian 无写文件/提交/回滚/网络工具（只记录不改）。"""
        perm = DEFAULT_ROLE_PERMS["historian"]
        forbidden = {"file_write", "git_commit", "rollback", "web_fetch", "web_search", "sandbox_run"}
        assert not (forbidden & set(perm.allowed_tools))


# ── 常量 ────────────────────────────────────────────────────────


class TestHistorianConstants:
    def test_record_actions_nonempty(self):
        assert len(RECORD_ACTIONS) > 0
        assert "record" in RECORD_ACTIONS
        assert "log_change" in RECORD_ACTIONS

    def test_record_actions_excludes_non_record(self):
        assert "file_write" not in RECORD_ACTIONS
        assert "web_search" not in RECORD_ACTIONS

    def test_pipeline_stages_complete(self):
        assert "propose" in PIPELINE_STAGES
        assert "approve" in PIPELINE_STAGES
        assert "apply" in PIPELINE_STAGES
        assert "verify" in PIPELINE_STAGES

    def test_pipeline_stages_count(self):
        assert len(PIPELINE_STAGES) == 4
