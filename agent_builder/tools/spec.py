"""工具规格描述（ToolSpec）—— 注册与执行的契约。

每个工具必须声明：名称/用途/参数(JSON Schema)/风险等级/超时/成本带/授权角色。
门卫按 risk_level 决定是否需要审批；执行器按 timeout_s 兜底超时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_RISK_LEVELS = frozenset({"low", "medium", "high"})
VALID_COST_BANDS = frozenset({"low", "medium", "high"})


@dataclass(slots=True)
class ToolSpec:
    """工具规格。注册时校验，运行时不允许修改。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema（draft-07 子集）
    risk_level: str  # low | medium | high
    timeout_s: float = 30.0
    cost_band: str = "low"  # low | medium | high
    allowed_roles: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.risk_level not in VALID_RISK_LEVELS:
            raise ValueError(
                f"非法 risk_level: {self.risk_level!r}，可选: {sorted(VALID_RISK_LEVELS)}"
            )
        if self.cost_band not in VALID_COST_BANDS:
            raise ValueError(
                f"非法 cost_band: {self.cost_band!r}，可选: {sorted(VALID_COST_BANDS)}"
            )
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正数: {self.timeout_s}")
        if not self.allowed_roles:
            raise ValueError(f"工具 {self.name!r} 必须至少声明一个 allowed_roles（最小权限）")


__all__ = ["VALID_COST_BANDS", "VALID_RISK_LEVELS", "ToolSpec"]
