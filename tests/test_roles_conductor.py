"""Conductor 总指挥角色测试：五阶段闭环 / 返工 / 中断 / 安全 / 注册。

覆盖：
- 功能：需求→计划→确认→执行→验证→交付 全流程
- 边界：空参数 → E_VALIDATION
- 状态机：非法转换 → E_INTERNAL
- 返工循环：2 轮上限
- 中断/恢复：保存 resume_point
- 查询：can_handle / is_terminal / retry_remaining
- 授权：conductor 角色权限矩阵正确
"""

from __future__ import annotations

import pytest

from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import TaskState
from agent_builder.contracts.state_machine import MAX_RETRY, TaskEvent, TaskStatus
from agent_builder.roles.conductor import Conductor
from agent_builder.tools.permissions import DEFAULT_ROLE_PERMS


def _make_conductor(task_id: str = "t-001") -> Conductor:
    """创建初始状态（RECEIVED）的 Conductor。"""
    return Conductor(
        task_state=TaskState(task_id=task_id),
        correlation_id="c-test",
    )


# ── 功能：五阶段闭环 ────────────────────────────────────────────────


class TestConductorFullCycle:
    def test_full_happy_path(self):
        """需求→计划→确认→执行→验证→交付 全流程。"""
        c = _make_conductor()
        assert c.task_state.status == TaskStatus.RECEIVED

        # 阶段 1：接收需求 → 规划
        c.receive_request("t-001", "帮我写个函数")
        assert c.task_state.status == TaskStatus.PLANNING

        # 阶段 2：规划完成 → 等待确认
        c.handle_plan_ready()
        assert c.task_state.status == TaskStatus.AWAITING_CONFIRM

        # 阶段 3a：用户确认 → 执行
        c.handle_plan_accepted()
        assert c.task_state.status == TaskStatus.EXECUTING

        # 阶段 4：执行完成 → 验证
        c.handle_all_steps_done()
        assert c.task_state.status == TaskStatus.VERIFYING

        # 阶段 5a：验证通过 → 交付
        c.handle_verify_passed()
        assert c.task_state.status == TaskStatus.DELIVERING

        # 阶段 6：交付
        c.deliver()
        assert c.task_state.status == TaskStatus.DELIVERED
        assert c.is_terminal is True

    def test_plan_rejected_back_to_planning(self):
        """用户拒绝计划 → 回到规划。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        assert c.task_state.status == TaskStatus.AWAITING_CONFIRM

        c.handle_plan_rejected()
        assert c.task_state.status == TaskStatus.PLANNING

        # 可以重新规划
        c.handle_plan_ready()
        assert c.task_state.status == TaskStatus.AWAITING_CONFIRM


# ── 返工循环 ────────────────────────────────────────────────────


class TestConductorRework:
    def test_rework_then_pass(self):
        """验证失败 → 返工 → 重新执行 → 验证通过。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_all_steps_done()

        # 验证失败（第 1 轮）
        c.handle_verify_failed()
        assert c.task_state.status == TaskStatus.REWORKING
        assert c.task_state.retry_count == 1

        # 返工完成 → 重新执行
        c.handle_rework_done()
        assert c.task_state.status == TaskStatus.EXECUTING

        # 重新执行 → 验证 → 通过
        c.handle_all_steps_done()
        c.handle_verify_passed()
        assert c.task_state.status == TaskStatus.DELIVERING

    def test_rework_max_retry_then_fail(self):
        """返工 2 轮仍失败 → 交付部分结果 + 失败。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_all_steps_done()

        # 第 1 轮失败 → 返工
        c.handle_verify_failed()
        assert c.task_state.status == TaskStatus.REWORKING
        c.handle_rework_done()
        c.handle_all_steps_done()

        # 第 2 轮失败 → 返工
        c.handle_verify_failed()
        assert c.task_state.status == TaskStatus.REWORKING
        assert c.task_state.retry_count == MAX_RETRY
        c.handle_rework_done()
        c.handle_all_steps_done()

        # 第 3 轮失败 → 超限 → FAILED
        status = c.handle_verify_failed()
        assert status == TaskStatus.FAILED
        assert c.is_terminal is True

    def test_retry_remaining(self):
        """剩余重试次数递减。"""
        c = _make_conductor()
        assert c.retry_remaining == MAX_RETRY

        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_all_steps_done()

        c.handle_verify_failed()
        assert c.retry_remaining == MAX_RETRY - 1


# ── 中断 / 恢复 ────────────────────────────────────────────────────


class TestConductorInterrupt:
    def test_interrupt_saves_resume_point(self):
        """中断保存 resume_point。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()

        # 用户中断
        c.handle_interrupt("user_stop")
        assert c.task_state.status == TaskStatus.INTERRUPTED
        assert c.is_interrupted is True
        assert c.task_state.interrupted is not None
        assert c.task_state.interrupted.resume_point == "executing"
        assert c.task_state.interrupted.reason == "user_stop"

    def test_resume_continues(self):
        """恢复后从 resume_point 续跑。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_interrupt()

        # 恢复 → 执行
        c.handle_resume()
        assert c.task_state.status == TaskStatus.EXECUTING
        assert c.task_state.interrupted is None

    def test_resume_without_interrupt(self):
        """无中断点恢复 → E_VALIDATION。"""
        c = _make_conductor()
        with pytest.raises(AgentError) as exc_info:
            c.handle_resume()
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_abort_from_interrupted(self):
        """中断后放弃 → FAILED。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_interrupt()

        c.handle_abort()
        assert c.task_state.status == TaskStatus.FAILED
        assert c.is_terminal is True


# ── 边界 ────────────────────────────────────────────────────────


class TestConductorEdge:
    def test_empty_task_id(self):
        c = _make_conductor()
        with pytest.raises(AgentError) as exc_info:
            c.receive_request("", "需求")
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_empty_requirement(self):
        c = _make_conductor()
        with pytest.raises(AgentError) as exc_info:
            c.receive_request("t-001", "")
        assert exc_info.value.error_name == "E_VALIDATION"

    def test_whitespace_task_id(self):
        c = _make_conductor()
        with pytest.raises(AgentError) as exc_info:
            c.receive_request("   ", "需求")
        assert exc_info.value.error_name == "E_VALIDATION"


# ── 状态机安全 ────────────────────────────────────────────────────


class TestConductorStateMachine:
    def test_plan_ready_from_wrong_state(self):
        """非 PLANNING 状态调 handle_plan_ready → E_INTERNAL。"""
        c = _make_conductor()
        # 在 RECEIVED 状态直接调 handle_plan_ready（非法）
        with pytest.raises(AgentError) as exc_info:
            c.handle_plan_ready()
        assert exc_info.value.error_name == "E_INTERNAL"

    def test_deliver_from_wrong_state(self):
        """非 DELIVERING 状态调 deliver → E_INTERNAL。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        with pytest.raises(AgentError) as exc_info:
            c.deliver()
        assert exc_info.value.error_name == "E_INTERNAL"

    def test_can_handle(self):
        """can_handle 不实际转换，只检查。"""
        c = _make_conductor()
        assert c.can_handle(TaskEvent.REQ_CONFIRMED) is True
        assert c.can_handle(TaskEvent.PLAN_READY) is False  # 需要先转 PLANNING

    def test_internal_error_transition(self):
        """INTERNAL_ERROR → FAILED（任意状态兜底）。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_internal_error()
        assert c.task_state.status == TaskStatus.FAILED


# ── 授权 ────────────────────────────────────────────────────────


class TestConductorPermissions:
    def test_role_registered(self):
        """conductor 角色在权限矩阵中。"""
        assert "conductor" in DEFAULT_ROLE_PERMS

    def test_allowed_tools(self):
        """conductor 只有 6 个低风险工具。"""
        perm = DEFAULT_ROLE_PERMS["conductor"]
        assert set(perm.allowed_tools) == {
            "plan_validate",
            "approval_request",
            "change_notify",
            "audit_log",
            "memory_read",
            "config_read",
        }

    def test_no_high_risk(self):
        """conductor 无高风险工具。"""
        perm = DEFAULT_ROLE_PERMS["conductor"]
        assert perm.high_risk_tools == []

    def test_no_execution_tools(self):
        """conductor 不可直接执行（无 file_write/git_commit/rollback/sandbox_run）。"""
        perm = DEFAULT_ROLE_PERMS["conductor"]
        forbidden = {"file_write", "git_commit", "rollback", "sandbox_run", "test_run"}
        assert not (forbidden & set(perm.allowed_tools))
