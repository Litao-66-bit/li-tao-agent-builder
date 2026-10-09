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

import json
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
        llm_client: LLM 客户端（可选；由运行时密钥工厂注入）。
            可用且步骤未给出结论时，由 LLM 汇总产出。
    """

    correlation_id: str = "c-unknown"
    llm_client: Any = None

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
            if not self._llm_ready():
                # 无 LLM：沿用原行为，由执行函数产出，结果从 inputs 提取。
                executor_fn(step)
            # 优先取步骤显式声明的结构化字段。
            conclusions = list(step.inputs.get("conclusions", []))
            evidence = list(step.inputs.get("evidence", []))
            sources = list(step.inputs.get("sources", []))
            pending_items = list(step.inputs.get("pending_items", []))
            next_steps = list(step.inputs.get("next_steps", []))
            missing_items = list(step.inputs.get("missing_items", []))

            # 缺结论且 LLM 可用 → 由 LLM 汇总（不新增判断、不编造）。
            if self._llm_ready() and not conclusions:
                generated = self._llm_report(step)
                conclusions = generated.get("conclusions", [])
                evidence = evidence or generated.get("evidence", [])
                sources = sources or generated.get("sources", [])
                next_steps = next_steps or generated.get("next_steps", [])

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

    def _llm_ready(self) -> bool:
        """LLM 客户端是否可用（鸭子类型判定，不依赖具体类型）。"""
        return self.llm_client is not None and bool(getattr(self.llm_client, "is_available", False))

    def _llm_report(self, step: Step) -> dict[str, list[str]]:
        """由 LLM 汇总执行上下文为四类要素；不可用/失败返回空字典。"""
        schema = '{"conclusions": [], "evidence": [], "sources": [], "next_steps": []}'
        prompt = (
            "把下面的执行上下文汇总为「结论/依据/来源/下一步」四类要素。\n"
            "要求：只做归纳，不新增判断、不编造；没有对应内容的类别返回空列表。\n"
            f"上下文：{json.dumps(step.inputs, ensure_ascii=False)}"
        )
        try:
            result = self.llm_client.complete_json(prompt, schema_hint=schema)
        except Exception:  # noqa: BLE001  LLM 调用失败降级为空报告
            return {}
        if not isinstance(result, dict):
            return {}
        generated: dict[str, list[str]] = {}
        for key in ("conclusions", "evidence", "sources", "next_steps"):
            raw = result.get(key, [])
            if isinstance(raw, list):
                generated[key] = [str(item) for item in raw]
        return generated

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
