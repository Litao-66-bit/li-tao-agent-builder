"""审计员（Auditor）—— 副架构・观测层。

职责：采集指标 + 对照基线标注异常 + 输出指标报告（客观描述，不下结论）。

边界声明：
- 指标异常但单次 → 记录不动作
- 连续 N 次异常 → 才触发优化流程
- 数据缺失 → 标注"采样不全"
- 客观描述，不下结论

执行协议：
1. 采集指标：成功率、错误类型、耗时、token、边界违规事件
2. 对照基线标注异常（超过阈值标红）
3. 输出指标报告
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 指标名集合。
METRIC_NAMES: frozenset[str] = frozenset({
    "success_rate",
    "error_types",
    "duration",
    "token_count",
    "boundary_violations",
})

# 连续异常触发优化的阈值。
ANOMALY_THRESHOLD = 3

# 指标状态。
METRIC_STATUSES = frozenset({"normal", "abnormal", "missing"})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class MetricItem:
    """单条指标项。"""

    name: str  # 指标名
    value: float  # 当前值
    baseline: float  # 基线值
    threshold: float  # 阈值
    status: str  # normal | abnormal | missing  正常/异常/采样不全
    consecutive: int = 0  # 连续异常次数


@dataclass(slots=True)
class MetricReport:
    """指标报告。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    metrics: list[MetricItem] = field(default_factory=list)
    abnormal_count: int = 0  # 异常数
    missing_count: int = 0  # 采样不全数
    trigger_optimization: bool = False  # 是否触发优化流程
    error: str | None = None


@dataclass(slots=True)
class Auditor:
    """审计员角色：采集指标 + 对照基线 + 输出报告。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        anomaly_threshold: 连续异常触发优化的阈值。
    """

    correlation_id: str = "c-unknown"
    anomaly_threshold: int = ANOMALY_THRESHOLD

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> MetricReport:
        """执行指标采集步骤。

        Args:
            step: 采集类步骤。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            MetricReport：指标报告（客观描述，不下结论）。
        """
        # 1. 校验是否采集类。
        action = step.action
        if action not in ("metric_collect", "audit"):
            return MetricReport(
                step_id=step.id,
                status="rejected",
                error=f"任务超出观测范围: {action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return MetricReport(step_id=step.id, status="pending")

        # 3. 执行采集。
        try:
            executor_fn(step)
            # 从 inputs 提取指标数据。
            raw_metrics = step.inputs.get("metrics", [])
            metrics = self._parse_metrics(raw_metrics)

            # 4. 统计异常/缺失。
            abnormal_count = sum(1 for m in metrics if m.status == "abnormal")
            missing_count = sum(1 for m in metrics if m.status == "missing")

            # 5. 连续 N 次异常 → 触发优化。
            trigger = any(
                m.consecutive >= self.anomaly_threshold
                for m in metrics
                if m.status == "abnormal"
            )

            return MetricReport(
                step_id=step.id,
                status="done",
                metrics=metrics,
                abnormal_count=abnormal_count,
                missing_count=missing_count,
                trigger_optimization=trigger,
            )
        except PermissionError as exc:
            return MetricReport(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  审计员需捕获所有执行异常
            return MetricReport(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _parse_metrics(self, raw_metrics: list[dict[str, Any]]) -> list[MetricItem]:
        """解析指标清单。"""
        metrics: list[MetricItem] = []
        for raw in raw_metrics:
            if not isinstance(raw, dict) or "name" not in raw:
                continue
            name = str(raw["name"])
            if name not in METRIC_NAMES:
                continue  # 跳过未知指标
            status = raw.get("status", "normal")
            if status not in METRIC_STATUSES:
                status = "normal"
            metrics.append(
                MetricItem(
                    name=name,
                    value=float(raw.get("value", 0.0)),
                    baseline=float(raw.get("baseline", 0.0)),
                    threshold=float(raw.get("threshold", 0.0)),
                    status=status,
                    consecutive=int(raw.get("consecutive", 0)),
                )
            )
        return metrics


__all__ = [
    "ANOMALY_THRESHOLD",
    "METRIC_NAMES",
    "METRIC_STATUSES",
    "Auditor",
    "ExecutorFn",
    "MetricItem",
    "MetricReport",
]
