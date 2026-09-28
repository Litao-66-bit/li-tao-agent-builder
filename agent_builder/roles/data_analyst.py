"""数据分析者（DataAnalyst）—— 主架构・执行层。

职责：读取数据 + 用工具计算（绝不心算）+ 输出结论区分"事实/推断/待验证"。

边界声明：
- 样本不足或数据缺失超阈值 → 不下结论，改出"数据质量报告"
- 口径冲突 → 以用户指定口径为准并标注差异
- 绝不心算：所有计算走工具（sandbox_run / data_query）

执行协议：
1. 读取数据文件，记录清洗规则（去重/补缺失/异常值处理）
2. 用工具计算（绝不心算），保留计算过程
3. 输出结论时区分"事实/推断/待验证"，写明口径
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 数据类 action 集合。
DATA_ACTIONS: frozenset[str] = frozenset({
    "data_query",
    "sandbox_run",
})

# 数据缺失率阈值（超过 → 不下结论，出数据质量报告）。
MAX_MISSING_RATIO = 0.3

# 结论类型。
CONCLUSION_TYPES = frozenset({"fact", "inference", "pending_verification"})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class Conclusion:
    """单条结论。"""

    content: str  # 结论内容
    type: str  # fact | inference | pending_verification
    caliber: str = ""  # 口径说明


@dataclass(slots=True)
class DataResult:
    """数据分析结果。"""

    step_id: str
    status: str  # done | failed | rejected | pending | quality_report
    conclusions: list[Conclusion] = field(default_factory=list)
    calc_process: str = ""  # 计算过程
    caliber_desc: str = ""  # 口径说明
    quality_report: str = ""  # 数据质量报告（样本不足时）
    error: str | None = None


@dataclass(slots=True)
class DataAnalyst:
    """数据分析者角色：读数据 + 工具计算 + 结论分类。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_missing_ratio: 数据缺失率阈值（超过 → 出质量报告）。
    """

    correlation_id: str = "c-unknown"
    max_missing_ratio: float = MAX_MISSING_RATIO

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> DataResult:
        """执行数据分析类步骤。

        Args:
            step: 数据类步骤（action 必须在 DATA_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）；
                None 则只校验不执行。
            context: 任务上下文（可选）。

        Returns:
            DataResult：结论 + 计算过程 + 口径说明。
        """
        # 1. 校验是否数据类。
        if step.action not in DATA_ACTIONS:
            return DataResult(
                step_id=step.id,
                status="rejected",
                error=f"任务超出数据范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return DataResult(
                step_id=step.id,
                status="pending",
            )

        # 3. 检查数据质量（缺失率超阈值 → 出质量报告，不下结论）。
        missing_ratio = step.inputs.get("missing_ratio", 0.0)
        if missing_ratio > self.max_missing_ratio:
            return DataResult(
                step_id=step.id,
                status="quality_report",
                quality_report=f"数据缺失率 {missing_ratio:.1%} 超过阈值 {self.max_missing_ratio:.1%}，不下结论",
                calc_process=step.inputs.get("calc_process", ""),
            )

        # 4. 执行计算（绝不心算）。
        try:
            executor_fn(step)
            # 从 inputs 提取结论和计算过程。
            raw_conclusions = step.inputs.get("conclusions", [])
            conclusions = self._parse_conclusions(raw_conclusions)
            calc_process = step.inputs.get("calc_process", "")
            caliber_desc = step.inputs.get("caliber_desc", "")

            return DataResult(
                step_id=step.id,
                status="done",
                conclusions=conclusions,
                calc_process=calc_process,
                caliber_desc=caliber_desc,
            )
        except PermissionError as exc:
            return DataResult(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
            return DataResult(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_conclusions(self, raw: list[dict[str, Any]]) -> list[Conclusion]:
        """解析结论清单。"""
        conclusions: list[Conclusion] = []
        for item in raw:
            if isinstance(item, dict) and "content" in item:
                ctype = item.get("type", "fact")
                if ctype not in CONCLUSION_TYPES:
                    ctype = "pending_verification"
                conclusions.append(
                    Conclusion(
                        content=str(item["content"]),
                        type=ctype,
                        caliber=str(item.get("caliber", "")),
                    )
                )
        return conclusions


__all__ = [
    "CONCLUSION_TYPES",
    "DATA_ACTIONS",
    "MAX_MISSING_RATIO",
    "Conclusion",
    "DataAnalyst",
    "DataResult",
    "ExecutorFn",
]
