"""任务状态机（docs/contracts/02-state-machine.md 的实现）。

Conductor 维护的唯一状态权威来源。任何状态转换必须符合本表。
"""

from __future__ import annotations

from enum import Enum

from agent_builder.contracts.errors import internal_error

# ── 状态一览（10 状态）──────────────────────────────────────────


class TaskStatus(str, Enum):
    RECEIVED = "received"
    PLANNING = "planning"
    AWAITING_CONFIRM = "awaiting_confirm"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REWORKING = "reworking"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    INTERRUPTED = "interrupted"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


# ── 事件（触发转换的唯一入口）───────────────────────────────────


class TaskEvent(str, Enum):
    REQ_CONFIRMED = "req_confirmed"    # 需求已确认、分解器产出 DAG → planning
    PLAN_READY = "plan_ready"          # 调度器产出执行计划 → awaiting_confirm
    PLAN_ACCEPTED = "plan_accepted"    # 用户确认计划 → executing
    PLAN_REJECTED = "plan_rejected"    # 用户要求修改 → planning
    ALL_STEPS_DONE = "all_steps_done"  # 全部步骤完成 → verifying
    INTERRUPT = "interrupt"            # 用户停止/高风险动作挂起 → interrupted
    VERIFY_PASSED = "verify_passed"    # 验证通过 → delivering
    VERIFY_FAILED = "verify_failed"    # 验证失败 → reworking / failed（看 retry_count）
    REWORK_DONE = "rework_done"        # 返工完成重新提交 → executing
    RESUME = "resume"                  # 用户恢复（带 resume_point）→ executing
    ABORT = "abort"                    # 用户放弃 / 不可恢复错误 → failed
    DELIVERED = "delivered"            # 汇报员交付最终报告 → delivered
    INTERNAL_ERROR = "internal_error"  # 不可恢复内部错误 → failed


# ── 转换表（契约 02 · 13 行转换 + 任意→failed 兜底）──────────────

_TRANSITIONS: dict[TaskStatus, dict[TaskEvent, TaskStatus]] = {
    TaskStatus.RECEIVED: {TaskEvent.REQ_CONFIRMED: TaskStatus.PLANNING},
    TaskStatus.PLANNING: {TaskEvent.PLAN_READY: TaskStatus.AWAITING_CONFIRM},
    TaskStatus.AWAITING_CONFIRM: {
        TaskEvent.PLAN_ACCEPTED: TaskStatus.EXECUTING,
        TaskEvent.PLAN_REJECTED: TaskStatus.PLANNING,
    },
    TaskStatus.EXECUTING: {
        TaskEvent.ALL_STEPS_DONE: TaskStatus.VERIFYING,
        TaskEvent.INTERRUPT: TaskStatus.INTERRUPTED,
    },
    TaskStatus.VERIFYING: {
        TaskEvent.VERIFY_PASSED: TaskStatus.DELIVERING,
        TaskEvent.VERIFY_FAILED: TaskStatus.REWORKING,  # retry_count < 2 时
    },
    TaskStatus.REWORKING: {TaskEvent.REWORK_DONE: TaskStatus.EXECUTING},
    TaskStatus.INTERRUPTED: {
        TaskEvent.RESUME: TaskStatus.EXECUTING,
        TaskEvent.ABORT: TaskStatus.FAILED,
    },
    TaskStatus.DELIVERING: {TaskEvent.DELIVERED: TaskStatus.DELIVERED},
}

# 任意状态 → failed 的兜底事件。
_ANY_TO_FAILED: frozenset[TaskEvent] = frozenset({TaskEvent.ABORT, TaskEvent.INTERNAL_ERROR})

MAX_RETRY = 2  # 契约：retry_count 上限 2，超限转 failed。


class TransitionError(Exception):
    """非法状态转换（对应 E_INTERNAL：状态机非法转换）。"""

    def __init__(self, current: TaskStatus, event: TaskEvent, correlation_id: str = "c-unknown") -> None:
        self.current = current
        self.event = event
        super().__init__(
            f"非法状态转换: {current.value} + {event.value}（契约 02 未定义该转换）"
        )

    def to_agent_error(self):
        return internal_error(
            f"非法状态转换: {self.current.value} + {self.event.value}",
            source="state_machine",
            correlation_id=self.correlation_id if hasattr(self, "correlation_id") else "c-unknown",
        )


def next_status(current: TaskStatus, event: TaskEvent) -> TaskStatus:
    """按契约转换表求目标状态；未定义转换抛 TransitionError。"""
    if event in _ANY_TO_FAILED:
        return TaskStatus.FAILED
    mapping = _TRANSITIONS.get(current)
    if mapping is None or event not in mapping:
        raise TransitionError(current, event)
    return mapping[event]


def can_transition(current: TaskStatus, event: TaskEvent) -> bool:
    try:
        next_status(current, event)
        return True
    except TransitionError:
        return False


__all__ = [
    "MAX_RETRY",
    "TaskEvent",
    "TaskStatus",
    "TransitionError",
    "can_transition",
    "next_status",
]
