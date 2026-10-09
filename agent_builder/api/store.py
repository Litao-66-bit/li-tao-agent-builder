"""内存任务存储 —— 存 Conductor + 原始需求 + 步骤 + 执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_builder.api.schemas import LoopState, ProposalView
from agent_builder.contracts.schemas import ChangeProposal, Step, TaskState
from agent_builder.roles.conductor import Conductor

_TITLE_MAX = 60


def _task_title(requirement: str) -> str:
    """从需求文本派生任务标题：取首个非空行并截断（供任务列表展示与回看）。"""
    for line in (requirement or "").splitlines():
        text = line.strip()
        if text:
            # 超长加省略号：直接硬截会在词中间断掉（实测列表里显示成「…请把实现代码写进工」）。
            return text if len(text) <= _TITLE_MAX else text[:_TITLE_MAX] + "…"
    return "(未命名任务)"


@dataclass(slots=True)
class TaskEntry:
    """单个任务条目：Conductor + 原始需求 + 步骤 DAG + 执行结果。

    Attributes:
        conductor: 总指挥（持有 TaskState 状态机）。
        requirement: 原始需求文本。
        steps: 分解器产出的步骤字典（step_id → Step）。
        execution_results: 执行编排器产出的步骤结果列表（每项含
            step_id / action / status / executor / summary(人话摘要) /
            result(str 原始数据) / error / retries）。
        gatekeeper_audit: 工具门卫审计快照（执行后追加）。
        options: 规划阶段的执行选项（模型/温度/详细度/副结构自检/LLM 开关），
            由 /plan 写入、/approve 读取，用于在执行阶段生效。
        self_check: 副结构自检报告（{passed, checked, issues}）；未开启为 None。
        council: 评审会纪要（方案/分歧/未决/建议决议）；未召开为 None。
        usage: 任务级 token 计量（total + by_role，cached/uncached 分开）+
            记录时的角色简报档位 mode；供 ``metric_collect`` / ``auditor`` 消费。
        rework_count: 任务级返工计量（Router 层重试次数累计）。
        pending_questions: 规划阶段产出的待确认问题（人的未决来源之一）。
        handover_ack: 交接提示的 ack 状态 ``{status, at}``（none/seen/ignored），
            用于限次与去重。
        handover_prompts_shown: 本会话已提示次数（限次计数器）。
        title: 任务标题（取自需求首行，供任务列表展示与回看）。
        approved_tools: 已被用户放行的高风险工具集合（「到点暂停」后按次累积）。
        pending_approval: 待用户放行的高风险步骤 ``{tools, steps}``；非 None
            表示任务因高风险动作挂起、等待用户决定是否继续。
        loop_state: agentic 循环上下文（轮次 / 预算 / 待放行决策 / 终止原因 / 归因）；
            ``mode=workflow`` 时为 None。``/resume`` 靠它从挂起的那一步继续。
        pending_proposal: 待用户批准的副结构提议（``kind=propose`` 浮出后挂在这里）；
            非 None 表示任务因"等扩编决定"挂起。
        proposals: 提议历史（含已批准 / 已拒绝），供回看与审计。
        pending_scaffold: 与 ``pending_proposal`` 配套的**待落盘内容**（相对路径 → 内容），
            在提议浮出时就算好 —— 批准时写入的就是用户在卡片上预览过的那份。
    """

    conductor: Conductor
    requirement: str
    steps: dict[str, Step] = field(default_factory=dict)
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    gatekeeper_audit: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    self_check: dict[str, Any] | None = None
    council: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    rework_count: int = 0
    pending_questions: list[str] = field(default_factory=list)
    handover_ack: dict[str, Any] = field(default_factory=dict)
    handover_prompts_shown: int = 0
    title: str = ""
    approved_tools: set[str] = field(default_factory=set)
    pending_approval: dict[str, Any] | None = None
    loop_state: LoopState | None = None
    pending_proposal: ProposalView | None = None
    proposals: list[ChangeProposal] = field(default_factory=list)
    pending_scaffold: dict[str, str] | None = None


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
        self._entries[task_id] = TaskEntry(
            conductor=conductor,
            requirement=requirement,
            title=_task_title(requirement),
        )
        return conductor

    def get(self, task_id: str) -> TaskEntry | None:
        return self._entries.get(task_id)

    def list_all(self) -> list[str]:
        return list(self._entries.keys())

    def list_summaries(self) -> list[TaskEntry]:
        """按更新时间倒序列出任务条目（供前端「多任务并存、可回看」列表）。"""
        return sorted(
            self._entries.values(),
            key=lambda e: e.conductor.task_state.updated_at,
            reverse=True,
        )

    def remove(self, task_id: str) -> bool:
        """删除任务条目；返回是否确实删除了（供 DELETE /tasks/{id} 用）。

        删除**不触发任何执行副作用**：后端执行是请求内同步完成的，不存在后台在跑
        的任务，因此移除条目只是让该任务从列表与详情中消失，不会中断任何运行中的工作。
        """
        return self._entries.pop(task_id, None) is not None


__all__ = ["InMemoryTaskStore", "TaskEntry"]
