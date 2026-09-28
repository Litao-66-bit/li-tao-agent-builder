"""总指挥（Conductor）—— 任务编排的唯一权威。

职责：接收用户需求，维护任务状态机（10 状态 13 事件），
管理返工循环（最多 2 轮）、中断与审批门、收尾归档。

边界声明：
- 只编排不执行：不可直接写文件/提交代码/执行命令
- 高风险动作（删除/覆盖/外发）暂停等待用户确认
- 副架构角色改动主架构 → 必须经编程者同意（变更通知不可省略）

执行协议（五阶段闭环）：
1. 接收需求 → 状态转 PLANNING → 调用分解器
2. 规划完成 → 状态转 AWAITING_CONFIRM → 等待用户确认
   - 用户确认 → EXECUTING
   - 用户拒绝 → 回 PLANNING
3. 执行 → 分派任务给执行层 → 全部完成转 VERIFYING
   - 用户中断 → INTERRUPTED（保存 resume_point）
4. 验证 → 通过转 DELIVERING；失败转 REWORKING（retry < 2）或 ABORT（超限）
5. 交付 → 最终答复给用户 → DELIVERED → 收尾归档

异常处理：
- 任一步骤抛错 → 记录错误、重试 1 次，仍失败则降级
- 验证 2 轮失败 → 交付部分结果 + 失败原因（不硬扛）
- 用户中断 → 保存当前状态可续跑
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_builder.contracts.errors import internal_error, validation_error
from agent_builder.contracts.schemas import Interruption, TaskState
from agent_builder.contracts.state_machine import (
    MAX_RETRY,
    TaskEvent,
    TaskStatus,
    TransitionError,
    can_transition,
    next_status,
)


@dataclass(slots=True)
class Conductor:
    """总指挥角色：任务编排的唯一权威。

    维护 TaskState 状态机，管理五阶段闭环：
    需求→计划→确认→执行→验证→汇报。

    Attributes:
        task_state: 唯一状态权威来源（Conductor 维护）。
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    task_state: TaskState
    correlation_id: str = "c-unknown"

    # ── 状态转换（内部）──────────────────────────────────────────

    def _transition(self, event: TaskEvent) -> TaskStatus:
        """执行状态转换；非法转换抛 E_INTERNAL（永不重试）。"""
        try:
            new_status = next_status(self.task_state.status, event)
        except TransitionError as exc:
            raise internal_error(
                f"非法状态转换: {self.task_state.status.value} + {event.value}",
                source="conductor",
                correlation_id=self.correlation_id,
                cause=exc,
            ) from exc
        self.task_state.status = new_status
        self.task_state.current_stage = new_status.value
        return new_status

    # ── 五阶段流程 ──────────────────────────────────────────────

    def receive_request(self, task_id: str, requirement: str) -> TaskStatus:
        """阶段 1：接收需求 → 进入规划（REQ_CONFIRMED → PLANNING）。

        Args:
            task_id: 任务唯一 ID。
            requirement: 用户需求文本。

        Returns:
            转换后的状态（PLANNING）。

        Raises:
            AgentError(E_VALIDATION): task_id / requirement 为空。
            AgentError(E_INTERNAL): 非法状态转换（非 RECEIVED 状态调用）。
        """
        if not task_id or not task_id.strip():
            raise validation_error(
                "conductor: task_id 不能为空",
                source="conductor",
                correlation_id=self.correlation_id,
            )
        if not requirement or not requirement.strip():
            raise validation_error(
                "conductor: requirement 不能为空",
                source="conductor",
                correlation_id=self.correlation_id,
            )
        return self._transition(TaskEvent.REQ_CONFIRMED)

    def handle_plan_ready(self) -> TaskStatus:
        """阶段 2：规划完成 → 等待用户确认（PLAN_READY → AWAITING_CONFIRM）。"""
        return self._transition(TaskEvent.PLAN_READY)

    def handle_plan_accepted(self) -> TaskStatus:
        """阶段 3a：用户确认计划 → 执行（PLAN_ACCEPTED → EXECUTING）。"""
        return self._transition(TaskEvent.PLAN_ACCEPTED)

    def handle_plan_rejected(self) -> TaskStatus:
        """阶段 3b：用户拒绝计划 → 重新规划（PLAN_REJECTED → PLANNING）。"""
        return self._transition(TaskEvent.PLAN_REJECTED)

    def handle_all_steps_done(self) -> TaskStatus:
        """阶段 4：全部步骤完成 → 验证（ALL_STEPS_DONE → VERIFYING）。"""
        return self._transition(TaskEvent.ALL_STEPS_DONE)

    def handle_interrupt(self, reason: str = "user_stop") -> TaskStatus:
        """中断：用户点击停止 → 挂起（INTERRUPT → INTERRUPTED）。

        保存 resume_point 以便后续续跑。

        Args:
            reason: 中断原因（默认 user_stop）。

        Returns:
            转换后的状态（INTERRUPTED）。
        """
        self.task_state.interrupted = Interruption(
            resume_point=self.task_state.current_stage,
            reason=reason,
        )
        return self._transition(TaskEvent.INTERRUPT)

    def handle_resume(self) -> TaskStatus:
        """恢复：用户恢复 → 从 resume_point 续跑（RESUME → EXECUTING）。

        Returns:
            转换后的状态（EXECUTING）。

        Raises:
            AgentError(E_VALIDATION): 无中断点可恢复。
        """
        if self.task_state.interrupted is None:
            raise validation_error(
                "conductor: 无中断点可恢复（当前未处于中断态）",
                source="conductor",
                correlation_id=self.correlation_id,
            )
        self.task_state.interrupted = None
        return self._transition(TaskEvent.RESUME)

    def handle_verify_passed(self) -> TaskStatus:
        """阶段 5a：验证通过 → 交付（VERIFY_PASSED → DELIVERING）。"""
        return self._transition(TaskEvent.VERIFY_PASSED)

    def handle_verify_failed(self) -> TaskStatus:
        """阶段 5b：验证失败 → 返工 or 放弃。

        retry_count < MAX_RETRY → REWORKING（返工）；
        retry_count >= MAX_RETRY → ABORT（交付部分结果 + 失败原因）。

        Returns:
            转换后的状态（REWORKING 或 FAILED）。
        """
        if self.task_state.retry_count < MAX_RETRY:
            self.task_state.retry_count += 1
            return self._transition(TaskEvent.VERIFY_FAILED)
        return self._transition(TaskEvent.ABORT)

    def handle_rework_done(self) -> TaskStatus:
        """返工完成 → 重新执行（REWORK_DONE → EXECUTING）。"""
        return self._transition(TaskEvent.REWORK_DONE)

    def deliver(self) -> TaskStatus:
        """阶段 6：交付最终报告（DELIVERED → DELIVERED）。

        Returns:
            转换后的状态（DELIVERED）。
        """
        return self._transition(TaskEvent.DELIVERED)

    def handle_abort(self) -> TaskStatus:
        """放弃 → 失败（ABORT → FAILED）。"""
        return self._transition(TaskEvent.ABORT)

    def handle_internal_error(self) -> TaskStatus:
        """不可恢复内部错误 → 失败（INTERNAL_ERROR → FAILED）。"""
        return self._transition(TaskEvent.INTERNAL_ERROR)

    # ── 查询 ─────────────────────────────────────────────────────

    def can_handle(self, event: TaskEvent) -> bool:
        """检查当前状态是否能处理该事件（不实际转换）。"""
        return can_transition(self.task_state.status, event)

    @property
    def is_terminal(self) -> bool:
        """是否处于终态（DELIVERED / FAILED）。"""
        return self.task_state.status in (TaskStatus.DELIVERED, TaskStatus.FAILED)

    @property
    def is_interrupted(self) -> bool:
        """是否处于中断态。"""
        return self.task_state.status == TaskStatus.INTERRUPTED

    @property
    def retry_remaining(self) -> int:
        """剩余重试次数。"""
        return max(0, MAX_RETRY - self.task_state.retry_count)


__all__ = ["Conductor"]
