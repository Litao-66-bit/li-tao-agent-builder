"""方案生成者（Proposer）—— 副架构・优化层。

职责：定位根因 + 给最小改动方案 + 理由 + 预期收益 + 失效标准。

边界声明：
- 无根因 → 不下方案
- 根因在数据 → 建议补充知识库而非改代码
- 多根因并存 → 拆成多个最小提案分批走审批

执行协议：
1. 定位根因（区分提示词/工具/流程/上下文/数据问题）
2. 给出最小改动方案 + 理由 + 预期收益 + 失效标准
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 根因类型。
ROOT_CAUSE_TYPES: frozenset[str] = frozenset({
    "prompt",    # 提示词问题
    "tool",      # 工具问题
    "process",   # 流程问题
    "context",   # 上下文问题
    "data",      # 数据问题（建议补充知识库而非改代码）
})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class ChangeProposalItem:
    """变更提案项。"""

    proposal_id: str
    target_module: str  # 目标模块
    root_cause: str  # 根因类型（prompt/tool/process/context/data）
    change_desc: str  # 变更描述
    expected_benefit: str  # 预期收益
    failure_criteria: str  # 失效标准
    diff_preview: str = ""  # diff 预览
    is_data_suggestion: bool = False  # 根因在数据 → 建议补充知识库


@dataclass(slots=True)
class ProposerResult:
    """方案生成者结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    proposals: list[ChangeProposalItem] = field(default_factory=list)
    no_root_cause: bool = False  # 无根因
    error: str | None = None


@dataclass(slots=True)
class Proposer:
    """方案生成者角色：定位根因 + 最小改动方案。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> ProposerResult:
        """执行方案生成步骤。

        Args:
            step: 方案生成类步骤。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            ProposerResult：变更提案列表。
        """
        # 1. 校验是否方案生成类。
        if step.action not in ("propose", "optimize"):
            return ProposerResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出优化范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return ProposerResult(step_id=step.id, status="pending")

        # 3. 执行方案生成。
        try:
            executor_fn(step)
            # 从 inputs 提取根因和提案数据。
            root_causes = step.inputs.get("root_causes", [])
            raw_proposals = step.inputs.get("proposals", [])

            # 4. 无根因 → 不下方案。
            if not root_causes:
                return ProposerResult(
                    step_id=step.id,
                    status="done",
                    no_root_cause=True,
                )

            # 5. 多根因 → 拆成多个最小提案。
            proposals = self._parse_proposals(raw_proposals, root_causes)

            return ProposerResult(
                step_id=step.id,
                status="done",
                proposals=proposals,
            )
        except PermissionError as exc:
            return ProposerResult(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  方案生成者需捕获所有执行异常
            return ProposerResult(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_proposals(
        self,
        raw_proposals: list[dict[str, Any]],
        root_causes: list[dict[str, Any]],
    ) -> list[ChangeProposalItem]:
        """解析提案清单（多根因 → 多提案）。"""
        proposals: list[ChangeProposalItem] = []
        for i, raw in enumerate(raw_proposals):
            if not isinstance(raw, dict):
                continue
            root_cause = str(raw.get("root_cause", ""))
            if root_cause not in ROOT_CAUSE_TYPES:
                root_cause = "process"  # 默认流程问题

            proposals.append(
                ChangeProposalItem(
                    proposal_id=str(raw.get("proposal_id", f"p-{i+1}")),
                    target_module=str(raw.get("target_module", "")),
                    root_cause=root_cause,
                    change_desc=str(raw.get("change_desc", "")),
                    expected_benefit=str(raw.get("expected_benefit", "")),
                    failure_criteria=str(raw.get("failure_criteria", "")),
                    diff_preview=str(raw.get("diff_preview", "")),
                    is_data_suggestion=root_cause == "data",
                )
            )

        # 无提案但有根因 → 为每个根因生成空提案骨架。
        if not proposals and root_causes:
            for i, rc in enumerate(root_causes):
                root_cause = str(rc.get("type", "process")) if isinstance(rc, dict) else "process"
                if root_cause not in ROOT_CAUSE_TYPES:
                    root_cause = "process"
                proposals.append(
                    ChangeProposalItem(
                        proposal_id=f"p-{i+1}",
                        target_module="",
                        root_cause=root_cause,
                        change_desc="",
                        expected_benefit="",
                        failure_criteria="",
                        is_data_suggestion=root_cause == "data",
                    )
                )

        return proposals


__all__ = [
    "ROOT_CAUSE_TYPES",
    "ChangeProposalItem",
    "ExecutorFn",
    "Proposer",
    "ProposerResult",
]
