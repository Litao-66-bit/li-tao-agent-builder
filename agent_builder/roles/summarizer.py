"""汇报员（Summarizer）—— 主架构・总结层。

职责：汇集各模块产出 + 结构化整理 + 不新增判断。

边界声明：
- 不新增判断、不覆盖用户指定格式
- 存在未完成项 → 如实列出并给建议，不粉饰
- 素材缺失 → 标注缺失项，不补编

执行协议：
1. 汇集各模块产出，提炼关键结论
2. 结构化整理（结论/依据/来源/未完成项/下一步）
3. 不新增判断、不覆盖用户指定格式
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 汇报类 action 集合。
SUMMARIZE_ACTIONS: frozenset[str] = frozenset({
    "summarize",
    "report",
})

# 报告章节标题（固定五段式）。
REPORT_SECTIONS = ("结论", "依据", "来源", "未完成项", "下一步")

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class ReportSection:
    """报告章节。"""

    title: str  # 结论/依据/来源/未完成项/下一步
    content: str
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FinalReport:
    """最终报告。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    sections: list[ReportSection] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    pending_items: list[str] = field(default_factory=list)  # 未完成项
    next_steps: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)  # 缺失项
    error: str | None = None

    @property
    def has_pending(self) -> bool:
        """是否存在未完成项。"""
        return len(self.pending_items) > 0 or len(self.missing_items) > 0


@dataclass(slots=True)
class Summarizer:
    """汇报员角色：汇集产出 + 结构化整理 + 不新增判断。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> FinalReport:
        """执行汇报类步骤。

        Args:
            step: 汇报类步骤（action 必须在 SUMMARIZE_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            FinalReport：最终报告（结论/依据/来源/未完成项/下一步）。
        """
        # 1. 校验是否汇报类。
        if step.action not in SUMMARIZE_ACTIONS:
            return FinalReport(
                step_id=step.id,
                status="rejected",
                error=f"任务超出汇报范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return FinalReport(step_id=step.id, status="pending")

        # 3. 执行汇总。
        try:
            executor_fn(step)
            # 从 inputs 提取报告数据。
            conclusions = list(step.inputs.get("conclusions", []))
            evidence = list(step.inputs.get("evidence", []))
            sources = list(step.inputs.get("sources", []))
            pending_items = list(step.inputs.get("pending_items", []))
            next_steps = list(step.inputs.get("next_steps", []))
            missing_items = list(step.inputs.get("missing_items", []))

            # 4. 结构化整理（五段式）。
            sections = self._build_sections(
                conclusions, evidence, sources, pending_items, next_steps
            )

            return FinalReport(
                step_id=step.id,
                status="done",
                sections=sections,
                conclusions=conclusions,
                evidence=evidence,
                sources=sources,
                pending_items=pending_items,
                next_steps=next_steps,
                missing_items=missing_items,
            )
        except PermissionError as exc:
            return FinalReport(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  汇报员需捕获所有执行异常
            return FinalReport(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _build_sections(
        self,
        conclusions: list[str],
        evidence: list[str],
        sources: list[str],
        pending_items: list[str],
        next_steps: list[str],
    ) -> list[ReportSection]:
        """结构化整理为五段式报告。"""
        return [
            ReportSection(title="结论", content="\n".join(conclusions)),
            ReportSection(title="依据", content="\n".join(evidence)),
            ReportSection(title="来源", content="\n".join(sources), sources=sources),
            ReportSection(
                title="未完成项",
                content="\n".join(pending_items) if pending_items else "无",
            ),
            ReportSection(
                title="下一步",
                content="\n".join(next_steps) if next_steps else "无",
            ),
        ]


__all__ = [
    "REPORT_SECTIONS",
    "SUMMARIZE_ACTIONS",
    "ExecutorFn",
    "FinalReport",
    "ReportSection",
    "Summarizer",
]
