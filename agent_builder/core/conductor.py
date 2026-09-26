"""Conductor（总指挥）——主架构执行层的编排核心。

职责（第一版，纯逻辑层，不接 LLM/工具）：
1. 持有 TaskState，作为任务状态的唯一权威来源。
2. 按契约 02 状态机驱动全部状态转换，非法转换抛 E_INTERNAL。
3. 管理 retry_count（验证失败返工 ≤2 轮，超限转 failed）。
4. 中断必须记录 resume_point；恢复时校验 resume_point 存在。
5. 每次转换写审计日志（audit_log，只追加）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent_builder.contracts.errors import AgentError, internal_error
from agent_builder.contracts.schemas import Interruption, Plan, TaskState
from agent_builder.contracts.state_machine import (
    MAX_RETRY,
    TaskEvent,
    TaskStatus,
    TransitionError,
    next_status,
)


@dataclass(slots=True)
class AuditEntry:
    """一条状态转换审计记录。"""

    ts: str
    event: str
    from_status: str
    to_status: str
    note: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "ts": self.ts,
            "event": self.event,
            "from": self.from_status,
            "to": self.to_status,
            "note": self.note,
        }


class Conductor:
    """总指挥：状态机驱动的任务编排器。"""

    def __init__(self, task_id: str, *, correlation_id: str = "c-unknown") -> None:
        self.task_id = task_id
        self.correlation_id = correlation_id
        self.state: TaskState = TaskState(task_id=task_id, status=TaskStatus.RECEIVED)
        self.audit_log: list[AuditEntry] = []

    # ── 核心转换入口 ────────────────────────────────────────────

    def apply(self, event: TaskEvent, *, note: str | None = None) -> TaskState:
        """驱动一次状态转换；未定义转换抛 AgentError(E_INTERNAL)。"""
        current = self.state.status
        try:
            target = next_status(current, event)
        except TransitionError as exc:
            raise internal_error(
                str(exc),
                source=f"conductor[{self.task_id}]",
                correlation_id=self.correlation_id,
                cause=exc,
            ) from exc

        # 特殊逻辑：verify_failed 时 retry_count 已达上限 → failed（契约规则 1）。
        if (
            event is TaskEvent.VERIFY_FAILED
            and target is TaskStatus.REWORKING
            and self.state.retry_count >= MAX_RETRY
        ):
            target = TaskStatus.FAILED

        old = self.state.status
        self.state.status = target
        self.state.current_stage = target.value
        self.state.updated_at = datetime.now(timezone.utc).isoformat()

        # 副效应：进入 reworking 时 retry_count +1。
        if target is TaskStatus.REWORKING:
            self.state.retry_count += 1

        self.audit_log.append(
            AuditEntry(
                ts=self.state.updated_at,
                event=event.value,
                from_status=old.value,
                to_status=target.value,
                note=note,
            )
        )
        return self.state

    # ── 便捷事件方法（对应契约 02 转换表）────────────────────────

    def confirm_request(self, note: str = "需求已确认") -> TaskState:
        """received → planning：需求确认、分解器产出 DAG。"""
        return self.apply(TaskEvent.REQ_CONFIRMED, note=note)

    def plan_ready(self, plan: Plan, note: str = "执行计划已产出") -> TaskState:
        """planning → awaiting_confirm：调度器产出计划。"""
        self.state.plan = plan
        return self.apply(TaskEvent.PLAN_READY, note=note)

    def accept_plan(self, note: str = "用户确认计划") -> TaskState:
        """awaiting_confirm → executing。"""
        if self.state.plan is None:
            raise internal_error(
                "计划尚未产出，无法确认",
                source=f"conductor[{self.task_id}]",
                correlation_id=self.correlation_id,
            )
        return self.apply(TaskEvent.PLAN_ACCEPTED, note=note)

    def reject_plan(self, note: str = "用户要求修改计划") -> TaskState:
        """awaiting_confirm → planning。"""
        return self.apply(TaskEvent.PLAN_REJECTED, note=note)

    def steps_done(self, note: str = "全部步骤完成") -> TaskState:
        """executing → verifying。"""
        return self.apply(TaskEvent.ALL_STEPS_DONE, note=note)

    def verify_passed(self, note: str = "验证通过") -> TaskState:
        """verifying → delivering。"""
        return self.apply(TaskEvent.VERIFY_PASSED, note=note)

    def verify_failed(self, reason: str, test_log: str | None = None) -> TaskState:
        """verifying → reworking（retry<2）/ failed（retry≥2）。"""
        detail = f"{reason}{' | ' + test_log if test_log else ''}"
        return self.apply(TaskEvent.VERIFY_FAILED, note=detail)

    def rework_done(self, note: str = "返工完成重新提交") -> TaskState:
        """reworking → executing。"""
        return self.apply(TaskEvent.REWORK_DONE, note=note)

    def interrupt(self, resume_point: str, reason: str = "user_stop") -> TaskState:
        """executing → interrupted：必须记录 resume_point（契约规则 2）。"""
        if not resume_point:
            raise internal_error(
                "中断必须记录 resume_point",
                source=f"conductor[{self.task_id}]",
                correlation_id=self.correlation_id,
            )
        self.state.interrupted = Interruption(resume_point=resume_point, reason=reason)
        return self.apply(TaskEvent.INTERRUPT, note=f"reason={reason}, resume_point={resume_point}")

    def resume(self, note: str = "用户恢复任务") -> TaskState:
        """interrupted → executing：要求存在 resume_point（契约规则 2）。"""
        if self.state.interrupted is None:
            raise internal_error(
                "任务未被中断，无法恢复",
                source=f"conductor[{self.task_id}]",
                correlation_id=self.correlation_id,
            )
        state = self.apply(TaskEvent.RESUME, note=note)
        self.state.interrupted = None
        return state

    def abort(self, note: str = "用户放弃任务") -> TaskState:
        """interrupted → failed（用户放弃）。"""
        return self.apply(TaskEvent.ABORT, note=note)

    def deliver(self, note: str = "汇报员交付最终报告") -> TaskState:
        """delivering → delivered。"""
        return self.apply(TaskEvent.DELIVERED, note=note)

    def fail(self, note: str, error: AgentError | None = None) -> TaskState:
        """任意状态 → failed（E_INTERNAL 兜底）。"""
        state = self.apply(TaskEvent.INTERNAL_ERROR, note=note)
        self.last_error = error
        return state

    # ── 查询 ────────────────────────────────────────────────────

    @property
    def status(self) -> TaskStatus:
        return self.state.status

    def snapshot(self) -> dict[str, Any]:
        """输出 TaskState 快照（含审计日志概要）。"""
        return {
            **self.state.model_dump(),
            "correlation_id": self.correlation_id,
            "audit_log": [entry.to_dict() for entry in self.audit_log],
        }

    @property
    def retry_count(self) -> int:
        return self.state.retry_count

    def can_retry(self) -> bool:
        """当前是否仍允许进入返工（retry_count < MAX_RETRY）。"""
        return self.state.retry_count < MAX_RETRY


__all__ = ["AuditEntry", "Conductor"]
