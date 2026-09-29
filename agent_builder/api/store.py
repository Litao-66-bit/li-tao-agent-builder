"""内存任务存储 —— 存 Conductor + 原始需求 + 步骤 + 执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step, TaskState
from agent_builder.roles.conductor import Conductor


@dataclass(slots=True)
class TaskEntry:
    """单个任务条目：Conductor + 原始需求 + 步骤 DAG + 执行结果。

    Attributes:
        conductor: 总指挥（持有 TaskState 状态机）。
        requirement: 原始需求文本。
        steps: 分解器产出的步骤字典（step_id → Step）。
        execution_results: 执行编排器产出的步骤结果列表（每项含
            step_id / status / executor / result / error / retries）。
        gatekeeper_audit: 工具门卫审计快照（执行后追加）。
    """

    conductor: Conductor
    requirement: str
    steps: dict[str, Step] = field(default_factory=dict)
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    gatekeeper_audit: list[dict[str, Any]] = field(default_factory=list)


class InMemoryTaskStore:
    """进程内任务存储（首版不做持久化）。"""

    def __init__(self) -> None:
        self._entries: dict[str, TaskEntry] = {}

    def create(self, task_id: str, requirement: str) -> Conductor:
        """创建任务 → 接收需求 → 状态转 PLANNING。

        Args:
            task_id: 任务唯一 ID。
            requirement: 用户需求文本。

        Returns:
            创建后的 Conductor（状态为 PLANNING）。

        Raises:
            ValueError: task_id 已存在。
        """
        if task_id in self._entries:
            raise ValueError(f"task_id 已存在: {task_id}")
        state = TaskState(task_id=task_id)
        conductor = Conductor(task_state=state, correlation_id=task_id)
        conductor.receive_request(task_id, requirement)  # RECEIVED → PLANNING
        self._entries[task_id] = TaskEntry(conductor=conductor, requirement=requirement)
        return conductor

    def get(self, task_id: str) -> TaskEntry | None:
        return self._entries.get(task_id)

    def list_all(self) -> list[str]:
        return list(self._entries.keys())


__all__ = ["InMemoryTaskStore", "TaskEntry"]
