"""影响分析者（ImpactAnalyzer）—— 副架构・优化层。

职责：圈定波及文件/模块 + 评估回归风险 + 估算成本。

边界声明：
- 风险高 → 标"需人工重点审"，附缓解措施
- 波及面不明确 → 返回方案生成者补全
- 随提案送审（不独立决策）

执行协议：
1. 圈定波及文件/模块
2. 评估回归风险等级（低/中/高）+ 回滚难度
3. 估算 token/时间成本
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 风险等级。
RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})

# 变更类型。
CHANGE_TYPES: frozenset[str] = frozenset({"add", "modify", "delete"})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class ImpactArea:
    """波及范围项。"""

    file_path: str  # 波及文件
    module: str  # 波及模块
    change_type: str  # add | modify | delete
    risk_level: str  # low | medium | high


@dataclass(slots=True)
class ImpactReport:
    """影响分析报告。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    areas: list[ImpactArea] = field(default_factory=list)  # 波及范围
    risk_level: str = "low"  # 综合回归风险等级
    rollback_difficulty: str = "low"  # 回滚难度 low/medium/high
    token_cost: int = 0  # token 成本
    time_cost_s: float = 0.0  # 时间成本（秒）
    needs_manual_review: bool = False  # 是否需人工重点审
    mitigation: str = ""  # 缓解措施
    incomplete: bool = False  # 波及面不明确 → 返回方案生成者补全
    error: str | None = None


@dataclass(slots=True)
class ImpactAnalyzer:
    """影响分析者角色：圈定波及范围 + 评估风险 + 估算成本。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
    """

    correlation_id: str = "c-unknown"

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> ImpactReport:
        """执行影响分析步骤。

        Args:
            step: 影响分析类步骤。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            ImpactReport：影响分析报告。
        """
        # 1. 校验是否影响分析类。
        if step.action not in ("impact_analyze", "assess"):
            return ImpactReport(
                step_id=step.id,
                status="rejected",
                error=f"任务超出影响分析范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return ImpactReport(step_id=step.id, status="pending")

        # 3. 执行影响分析。
        try:
            executor_fn(step)
            # 从 inputs 提取波及范围。
            raw_areas = step.inputs.get("areas", [])
            areas = self._parse_areas(raw_areas)

            # 4. 波及面不明确 → 返回方案生成者补全。
            if not areas:
                return ImpactReport(
                    step_id=step.id,
                    status="done",
                    incomplete=True,
                )

            # 5. 评估综合回归风险。
            risk_level = self._assess_risk(areas)
            rollback_difficulty = str(step.inputs.get("rollback_difficulty", "low"))
            if rollback_difficulty not in RISK_LEVELS:
                rollback_difficulty = "low"

            token_cost = int(step.inputs.get("token_cost", 0))
            time_cost_s = float(step.inputs.get("time_cost_s", 0.0))

            # 6. 风险高 → 标"需人工重点审" + 缓解措施。
            needs_manual = risk_level == "high"
            mitigation = str(step.inputs.get("mitigation", ""))

            return ImpactReport(
                step_id=step.id,
                status="done",
                areas=areas,
                risk_level=risk_level,
                rollback_difficulty=rollback_difficulty,
                token_cost=token_cost,
                time_cost_s=time_cost_s,
                needs_manual_review=needs_manual,
                mitigation=mitigation,
            )
        except PermissionError as exc:
            return ImpactReport(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  影响分析者需捕获所有执行异常
            return ImpactReport(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_areas(self, raw_areas: list[dict[str, Any]]) -> list[ImpactArea]:
        """解析波及范围清单。"""
        areas: list[ImpactArea] = []
        for raw in raw_areas:
            if not isinstance(raw, dict) or "file_path" not in raw:
                continue
            change_type = str(raw.get("change_type", "modify"))
            if change_type not in CHANGE_TYPES:
                change_type = "modify"
            risk_level = str(raw.get("risk_level", "low"))
            if risk_level not in RISK_LEVELS:
                risk_level = "low"
            areas.append(
                ImpactArea(
                    file_path=str(raw["file_path"]),
                    module=str(raw.get("module", "")),
                    change_type=change_type,
                    risk_level=risk_level,
                )
            )
        return areas

    def _assess_risk(self, areas: list[ImpactArea]) -> str:
        """评估综合回归风险等级（取最高）。"""
        if any(a.risk_level == "high" for a in areas):
            return "high"
        if any(a.risk_level == "medium" for a in areas):
            return "medium"
        return "low"


__all__ = [
    "CHANGE_TYPES",
    "RISK_LEVELS",
    "ExecutorFn",
    "ImpactAnalyzer",
    "ImpactArea",
    "ImpactReport",
]
