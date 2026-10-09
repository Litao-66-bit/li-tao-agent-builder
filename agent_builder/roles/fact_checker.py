"""事实核验者（FactChecker）—— 主架构・验证层。

职责：逐一核对引用来源 + 复核关键数字/版本号/人名 + 输出核验报告。

边界声明：
- 无法核实 → 标"存疑"不放行
- 证伪 → 打回修改
- 来源链接失效 → 用缓存/原站再查 1 次，仍失败标存疑

执行协议：
1. 逐一核对引用来源是否真实存在
2. 复核关键数字、版本号、人名
3. 输出核验报告（通过/存疑/证伪 + 依据）
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_builder.contracts.schemas import Step

# 核验类 action 集合。
VERIFY_ACTIONS: frozenset[str] = frozenset({
    "citation_check",
    "web_fetch",
})

# 来源失效最大重试次数。
MAX_RETRIES = 1

# 核验状态。
VERIFY_STATUSES = frozenset({"passed", "suspicious", "falsified"})

# 执行函数类型。
ExecutorFn = Callable[[Step], Any]


@dataclass(slots=True)
class VerificationItem:
    """单条核验结果。"""

    claim: str  # 事实声明
    source: str  # 引用来源
    status: str  # passed | suspicious | falsified  通过/存疑/证伪
    evidence: str = ""  # 依据
    retries: int = 0  # 来源失效重试次数


@dataclass(slots=True)
class FactReport:
    """核验报告。"""

    step_id: str
    status: str  # done | failed | rejected | pending
    items: list[VerificationItem] = field(default_factory=list)
    passed: int = 0
    suspicious: int = 0  # 存疑
    falsified: int = 0  # 证伪
    error: str | None = None

    @property
    def all_passed(self) -> bool:
        """是否全部通过（无存疑无证伪）。"""
        return self.suspicious == 0 and self.falsified == 0


@dataclass(slots=True)
class FactChecker:
    """事实核验者角色：核对来源 + 复核关键数据 + 核验报告。

    Attributes:
        correlation_id: 关联 ID（贯穿审计日志）。
        max_retries: 来源失效重试次数。
        llm_client: LLM 客户端（可选；由运行时密钥工厂注入）。
            可用且步骤未给出核验结果时，由 LLM 复核产出。
    """

    correlation_id: str = "c-unknown"
    max_retries: int = MAX_RETRIES
    llm_client: Any = None

    def execute(
        self,
        step: Step,
        executor_fn: ExecutorFn | None = None,
        context: dict[str, Any] | None = None,
    ) -> FactReport:
        """执行核验类步骤。

        Args:
            step: 核验类步骤（action 必须在 VERIFY_ACTIONS 中）。
            executor_fn: 执行函数（接收 Step，返回结果）。
            context: 任务上下文（可选）。

        Returns:
            FactReport：核验报告（通过/存疑/证伪 + 依据）。
        """
        # 1. 校验是否核验类。
        if step.action not in VERIFY_ACTIONS:
            return FactReport(
                step_id=step.id,
                status="rejected",
                error=f"任务超出核验范围: {step.action}",
            )

        # 2. 无执行函数 → 只校验不执行。
        if executor_fn is None:
            return FactReport(
                step_id=step.id,
                status="pending",
            )

        # 3. 执行核验（来源失效 → 重试 1 次）。
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    executor_fn(step)
                    break
                except OSError:
                    if attempt < self.max_retries:
                        continue
                    raise

            # 从 inputs 提取核验结果；缺失且 LLM 可用 → 由 LLM 复核产出。
            raw_items = step.inputs.get("verifications", [])
            if not raw_items and self._llm_ready():
                raw_items = self._llm_verifications(step)
            items = self._parse_items(raw_items, step.inputs.get("retries", 0))
            passed = sum(1 for i in items if i.status == "passed")
            suspicious = sum(1 for i in items if i.status == "suspicious")
            falsified = sum(1 for i in items if i.status == "falsified")

            return FactReport(
                step_id=step.id,
                status="done",
                items=items,
                passed=passed,
                suspicious=suspicious,
                falsified=falsified,
            )
        except PermissionError as exc:
            return FactReport(
                step_id=step.id,
                status="failed",
                error=f"权限不足: {exc}",
            )
        except OSError as exc:
            # 来源失效重试仍失败 → 标存疑。
            return FactReport(
                step_id=step.id,
                status="done",
                suspicious=1,
                error=f"来源链接失效: {exc}",
            )
        except Exception as exc:  # noqa: BLE001  执行者需捕获所有执行异常
            return FactReport(
                step_id=step.id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _llm_ready(self) -> bool:
        """LLM 客户端是否可用（鸭子类型判定，不依赖具体类型）。"""
        return self.llm_client is not None and bool(getattr(self.llm_client, "is_available", False))

    def _llm_verifications(self, step: Step) -> list[dict[str, Any]]:
        """由 LLM 复核步骤上下文中的声明；不可用/失败返回空列表。

        只允许三种结论：passed（通过）/ suspicious（存疑）/ falsified（证伪）；
        无法核实一律标 suspicious，不放行。
        """
        schema = (
            '{"verifications": [{"claim": "", "source": "", '
            '"status": "passed|suspicious|falsified", "evidence": ""}]}'
        )
        prompt = (
            "核对下面上下文中的关键声明（数字/版本号/人名/引用来源）。\n"
            "无法核实的标 suspicious，不要臆断；只返回结构化结果。\n"
            f"上下文：{json.dumps(step.inputs, ensure_ascii=False)}"
        )
        try:
            result = self.llm_client.complete_json(prompt, schema_hint=schema)
        except Exception:  # noqa: BLE001  LLM 调用失败降级为无核验项
            return []
        if not isinstance(result, dict):
            return []
        raw = result.get("verifications", [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict) and "claim" in item]

    def _parse_items(self, raw_items: list[dict[str, Any]], retries: int = 0) -> list[VerificationItem]:
        """解析核验结果清单。"""
        items: list[VerificationItem] = []
        for raw in raw_items:
            if isinstance(raw, dict) and "claim" in raw:
                status = raw.get("status", "suspicious")
                if status not in VERIFY_STATUSES:
                    status = "suspicious"
                items.append(
                    VerificationItem(
                        claim=str(raw["claim"]),
                        source=str(raw.get("source", "")),
                        status=status,
                        evidence=str(raw.get("evidence", "")),
                        retries=retries,
                    )
                )
        return items


__all__ = [
    "MAX_RETRIES",
    "VERIFY_ACTIONS",
    "VERIFY_STATUSES",
    "ExecutorFn",
    "FactChecker",
    "FactReport",
    "VerificationItem",
]
