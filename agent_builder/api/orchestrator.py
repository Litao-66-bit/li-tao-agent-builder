"""执行编排器 —— 把 Conductor 状态、Plan、Step 与 Router/Gatekeeper 串起来。

职责：
1. 构造 ToolGatekeeper（权限 + 沙箱 + 审计）。
2. 定义 executor_fn：每个 Step → ToolCall → registry.execute。
3. 调 Router.route 派发 + 收集结果。
4. 序列化 ExecutionResult 为前端可用的 list[dict]，并附 gatekeeper 审计快照。

设计约束：
- executor_fn 内异常由 Router._execute_step 捕获并转成 status=failed；
  此处不包裹 try/except，避免双重兜底。
- Router.route 抛出的 AgentError 直接上抛到 routes 层转 HTTP 409。
"""

from __future__ import annotations

from typing import Any

from agent_builder.contracts.schemas import Plan, Step, ToolCall
from agent_builder.roles.router import Router
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.permissions import get_default_role_perms
from agent_builder.tools.registry import registry


def run_plan(task_id: str, plan: Plan, steps: dict[str, Step]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """执行已确认的 Plan，返回 (execution_results, gatekeeper_audit)。

    Args:
        task_id: 任务 ID（作为 correlation_id 与 audit_id 前缀）。
        plan: 已确认的执行计划（confirmed_by_user 会被强制置 True）。
        steps: 步骤字典（step_id → Step）。

    Returns:
        元组 (execution_results, gatekeeper_audit)：
        - execution_results: list[dict]，每项含 step_id / action / status /
          executor / result(str) / error / retries。
        - gatekeeper_audit: list[dict]，门卫审计快照（audit_id / role / tool /
          allowed / reason）。

    Raises:
        AgentError: 路由/校验失败，由 routes 层转 HTTP 409。
    """
    # 1. 构造门卫：默认角色权限 + 工作区沙箱 + 任务级关联 ID。
    # 函数内懒导入 _resolve_workspace_dir 避免 routes ↔ orchestrator 循环导入。
    from agent_builder.api.routes import _resolve_workspace_dir

    gatekeeper = ToolGatekeeper(
        role_perms=get_default_role_perms(),
        workspace_dir=_resolve_workspace_dir(),
        correlation_id=task_id,
    )

    # 2. 执行函数：每个 Step 走一次工具调用全链路。
    def executor_fn(step: Step) -> Any:
        # 角色未指派时默认用 operator（17 个 allowed_tools，含 file_write）。
        role = step.assignee or "operator"
        call = ToolCall(
            audit_id=f"{task_id}-{step.id}",
            role=role,
            tool=step.action,
            args=step.inputs,
        )
        # registry.execute 内部：门卫 check → 查注册表 → 调 impl → 回填 result。
        # 任何异常（E_PERMISSION/E_VALIDATION/E_TIMEOUT）由 Router._execute_step
        # 捕获转成 ExecutionResult.status=failed，此处不 try/except。
        executed = registry.execute(gatekeeper, call)
        return executed.result

    # 3. 派发执行。Router 要求 confirmed_by_user=True，否则抛 E_VALIDATION。
    plan.confirmed_by_user = True
    router = Router(correlation_id=task_id)
    route_result = router.route(plan, steps, executor_fn)

    # 4. 序列化 ExecutionResult → list[dict]（result 转 str 便于前端展示）。
    execution_results: list[dict[str, Any]] = []
    for sid, r in route_result.results.items():
        step = steps.get(sid)
        execution_results.append(
            {
                "step_id": r.step_id,
                "action": step.action if step else "",
                "status": r.status,
                "executor": r.executor,
                "result": _to_str(r.result),
                "error": r.error,
                "retries": r.retries,
            }
        )

    return execution_results, gatekeeper.snapshot()


def _to_str(value: Any) -> str | None:
    """把任意结果转成前端可展示的字符串（None 保持原样）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = ["run_plan"]
