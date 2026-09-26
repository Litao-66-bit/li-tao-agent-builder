"""LangGraph 状态定义（最小闭环的图内数据流）。"""

from __future__ import annotations

from typing import TypedDict

from agent_builder.contracts.schemas import Plan, Step


class AgentState(TypedDict, total=False):
    """图内状态：decompose → schedule → confirm → execute → verify → summarize。

    - steps: 分解器产出的步骤列表（Pydantic Step）
    - plan: 调度器产出的执行计划
    - results: step_id → 执行结果文本
    - report: 最终报告
    - error: 失败信息（用于 CLI 展示）
    """

    task_id: str
    requirement: str
    correlation_id: str
    steps: list[Step]
    plan: Plan
    results: dict[str, str]
    report: str | None
    error: str | None


__all__ = ["AgentState"]
