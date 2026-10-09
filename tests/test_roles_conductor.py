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

    def test_每次转换刷新updated_at(self):
        """``updated_at`` 必须随状态转换推进（任务耗时与任务列表排序都依赖它）。

        回归：此前它只在创建时由 ``default_factory`` 赋值、**再无写入点** →
        ① 前端「已深度思考（用时 N 秒）」恒为 0 秒；
        ② ``list_summaries`` 按 ``updated_at`` 倒序 → 排序恒等于创建顺序，刚跑完的任务不置顶。
        """
        c = _make_conductor()
        created_at = c.task_state.created_at
        created_updated = c.task_state.updated_at

        c.receive_request("t-001", "需求")
        first = c.task_state.updated_at
        assert first != created_updated  # 转换真的写了新时间戳
        assert c.task_state.created_at == created_at  # 创建时间不被改写

        c.handle_plan_ready()
        second = c.task_state.updated_at
        assert second >= first  # 单调不减
        c.handle_plan_accepted()
        assert c.task_state.updated_at >= second

    def test_touch只刷新时间戳不改状态(self):
        """状态**没有转换**时（agentic 非收敛停下）也要能推进 updated_at。

        否则前端「已深度思考（用时 N 秒）」里的 N 恒为 0 —— 实测踩到过。
        """
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        before = c.task_state.updated_at
        status = c.task_state.status
        stage = c.task_state.current_stage

        c.touch()

        assert c.task_state.updated_at != before
        assert c.task_state.status == status  # 状态不变
        assert c.task_state.current_stage == stage

    def test_失败转换不污染updated_at(self):
        """非法转换（抛 E_INTERNAL）不应改动时间戳 —— 状态都没变。"""
        c = _make_conductor()
        before = c.task_state.updated_at
        with pytest.raises(AgentError):
            c.handle_plan_ready()  # RECEIVED + PLAN_READY 未定义
        assert c.task_state.updated_at == before

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

    def test_reject_from_interrupted_back_to_planning(self):
        """暂停（到点暂停 / 手动中断）后「改计划」→ 回 PLANNING，并清掉中断快照。"""
        c = _make_conductor()
        c.receive_request("t-001", "需求")
        c.handle_plan_ready()
        c.handle_plan_accepted()
        c.handle_interrupt("high_risk_pending")
        assert c.task_state.status == TaskStatus.INTERRUPTED

        c.handle_plan_rejected()
        assert c.task_state.status == TaskStatus.PLANNING
        assert c.task_state.current_stage == "planning"
        # 不再残留过期的 resume_point（与 handle_resume 同口径）。
        assert c.task_state.interrupted is None

        # 退回规划后可正常重新规划。
        c.handle_plan_ready()
        assert c.task_state.status == TaskStatus.AWAITING_CONFIRM


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
        """conductor 只有 8 个低风险工具（含评审会收敛两个）。"""
        perm = DEFAULT_ROLE_PERMS["conductor"]
        assert set(perm.allowed_tools) == {
            "plan_validate",
            "approval_request",
            "change_notify",
            "audit_log",
            "memory_read",
            "config_read",
            "council_check_opinion",
            "council_build_minutes",
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
