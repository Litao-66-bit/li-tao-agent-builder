"""plan_validate：校验步骤 DAG（格式 + 引用完整性 + 环依赖）。

安全边界：纯计算，无文件/网络访问，无审批。门卫只做角色权限校验。
本模块复用 contracts/schemas.Plan.validate_steps 的 DAG 校验逻辑。
"""

from __future__ import annotations

from typing import Any

from agent_builder.contracts.errors import validation_error
from agent_builder.contracts.schemas import Plan, Step
from agent_builder.tools.registry import current_correlation_id, registry
from agent_builder.tools.spec import ToolSpec

# 单次校验最大步骤数。
MAX_STEPS = 200


def validate_plan(
    steps: list[dict[str, Any]],
    order: list[str],
    parallel_groups: list[list[str]] | None = None,
) -> str:
    """校验执行计划 DAG。

    Args:
        steps: 步骤列表，每项 {"id", "action", "depends_on": [...]}。
        order: 执行顺序（步骤 id 列表）。
        parallel_groups: 可选，并行组列表（每组为同时执行的步骤 id）。

    Returns:
        成功消息：``DAG 校验通过：N 个步骤，M 个并行组，无环依赖``。

    Raises:
        AgentError(E_VALIDATION): steps/order 为空 / 步骤数超限 / 格式非法 /
            引用不存在的步骤 / DAG 存在环依赖。
    """
    cid = current_correlation_id.get()
    if not steps:
        raise validation_error(
            "plan_validate: steps 不能为空",
            source="tool.plan_validate",
            correlation_id=cid,
        )
    if not order:
        raise validation_error(
            "plan_validate: order 不能为空",
            source="tool.plan_validate",
            correlation_id=cid,
        )
    if len(steps) > MAX_STEPS:
        raise validation_error(
            f"plan_validate: steps 数量 {len(steps)} 超过上限 {MAX_STEPS}",
            source="tool.plan_validate",
            correlation_id=cid,
        )

    # 构造 Step 实例（触发字段校验）。
    steps_dict: dict[str, Step] = {}
    for raw in steps:
        sid = raw.get("id")
        if not sid or not str(sid).strip():
            raise validation_error(
                "plan_validate: 步骤缺少 id 或 id 为空",
                source="tool.plan_validate",
                correlation_id=cid,
            )
        sid = str(sid)
        if sid in steps_dict:
            raise validation_error(
                f"plan_validate: 步骤 id 重复: {sid!r}",
                source="tool.plan_validate",
                correlation_id=cid,
            )
        action = raw.get("action")
        if not action or not str(action).strip():
            raise validation_error(
                f"plan_validate: 步骤 {sid!r} 缺少 action 或 action 为空",
                source="tool.plan_validate",
                correlation_id=cid,
            )
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise validation_error(
                f"plan_validate: 步骤 {sid!r} 的 depends_on 必须是列表",
                source="tool.plan_validate",
                correlation_id=cid,
            )
        try:
            steps_dict[sid] = Step(
                id=sid, action=str(action), depends_on=[str(d) for d in depends_on]
            )
        except ValueError as exc:
            raise validation_error(
                f"plan_validate: 步骤 {sid!r} 格式非法: {exc}",
                source="tool.plan_validate",
                correlation_id=cid,
            ) from exc

    # 构造 Plan 并校验 DAG（引用完整性 + 环检测）。
    groups = parallel_groups if parallel_groups is not None else []
    try:
        plan = Plan(
            task_id="validation",
            order=list(order),
            parallel_groups=[list(g) for g in groups],
        )
        plan.validate_steps(steps_dict)
    except ValueError as exc:
        raise validation_error(
            f"plan_validate: DAG 校验失败: {exc}",
            source="tool.plan_validate",
            correlation_id=cid,
        ) from exc

    n_groups = len(groups)
    return f"DAG 校验通过：{len(steps_dict)} 个步骤，{n_groups} 个并行组，无环依赖"


spec = ToolSpec(
    name="plan_validate",
    description="校验步骤 DAG（格式 + 引用完整性 + 环依赖检测）",
    parameters={
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "action": {"type": "string"},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["id", "action"],
                },
                "description": "步骤列表",
            },
            "order": {
                "type": "array",
                "items": {"type": "string"},
                "description": "执行顺序（步骤 id 列表）",
            },
            "parallel_groups": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
                "description": "并行组列表（可选）",
            },
        },
        "required": ["steps", "order"],
        "additionalProperties": False,
    },
    risk_level="low",
    timeout_s=10.0,
    cost_band="low",
    allowed_roles=["operator"],
)

registry.register(spec, validate_plan)


__all__ = ["MAX_STEPS", "spec", "validate_plan"]
