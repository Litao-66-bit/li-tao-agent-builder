"""工具规格描述（ToolSpec）—— 注册与执行的契约。

每个工具必须声明：名称/用途/参数(JSON Schema)/风险等级/超时/成本带/授权角色。

真源与边界（避免误读）：
- **执行真源**是 ``permissions.py`` 的 ``allowed_tools`` / ``high_risk_tools``：
  门卫 ``ToolGatekeeper.check`` 据此放行，并要求高风险工具提供审批标记。
- ``ToolSpec.risk_level`` 与 ``ToolSpec.allowed_roles`` 是**声明性元数据**，
  不参与运行时判定；它们必须与执行真源保持一致，由
  ``agent_builder.evaluation.tool_report`` 与 ``tests/test_evaluation_tools.py`` 守住。
- ``allowed_roles`` 为该工具的名义授权角色（运行时以 permissions.py 为准）。
- ``timeout_s`` 由 registry 在调用实现时兜底生效。
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
    risk_level: str  # low | medium | high（声明性元数据，不参与门卫判定）
    timeout_s: float = 30.0
    cost_band: str = "low"  # low | medium | high
    allowed_roles: list[str] = field(default_factory=list)  # 名义授权角色（执行真源见 permissions.py）

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


# 常见同义参数名 → 规范参数名。
# 仅在「目标参数已在 spec 中声明、且源参数名未声明」时生效，因此不会覆盖真源参数名
# （例如 web_search 本身就用 query，不会被映射成 pattern）。
ARG_ALIASES: dict[str, str] = {
    # 检索式
    "query": "pattern",
    "keyword": "pattern",
    "keywords": "pattern",
    "regex": "pattern",
    # 路径
    "file": "path",
    "filename": "path",
    "filepath": "path",
    "file_path": "path",
    "dir": "path",
    "directory": "path",
    "folder": "path",
    # 内容
    "text": "content",
    "data": "content",
}


def normalize_args(spec: ToolSpec, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """按 spec 归一化工具参数（宽松）：别名映射 + 丢弃未声明的键。

    背景：编排层传入的是**分解器产出的自由 ``inputs``**，其键名未必等于工具
    签名；历史实现直接把 args ``impl(**args)`` 展开，多一个键即抛 Python
    ``TypeError``（报错信息对模型/用户均无意义）。此处按 spec 声明的
    ``properties`` 归一化，使「参数名不匹配」不再导致调用失败：

    1. 已声明的键 → 原样保留；
    2. 未声明但命中 :data:`ARG_ALIASES`、且目标已声明且未被占用 → 重命名为目标；
    3. 其余未声明的键 → 丢弃（记入返回的 ``dropped``）。

    本函数**不做安全校验**（路径沙箱 / URL 校验仍由门卫负责），也**不校验必填项**
    （由调用方在归一化后检查）。

    Args:
        spec: 工具规格（参数契约真源）。
        args: 原始参数字典。

    Returns:
        ``(归一化后的参数, 被丢弃的键名列表)``。
    """
    properties = spec.parameters.get("properties") or {}
    if not isinstance(args, dict):
        return {}, []
    normalized: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in args.items():
        if key in properties:
            normalized[key] = value
            continue
        alias = ARG_ALIASES.get(key)
        if alias and alias in properties and alias not in normalized:
            normalized[alias] = value
            continue
        dropped.append(key)
    return normalized, dropped


__all__ = ["ARG_ALIASES", "VALID_COST_BANDS", "VALID_RISK_LEVELS", "ToolSpec", "normalize_args"]
