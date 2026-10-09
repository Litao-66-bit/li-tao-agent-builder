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

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, fields, is_dataclass
from typing import Any

from agent_builder.api.artifacts import artifacts_of
from agent_builder.api.role_briefs import brief_for
from agent_builder.contracts.errors import AgentError
from agent_builder.contracts.schemas import Approval, Plan, Step, ToolCall
from agent_builder.narrate import summarize_result
from agent_builder.roles.auditor import Auditor
from agent_builder.roles.code_worker import CodeWorker
from agent_builder.roles.data_analyst import DataAnalyst
from agent_builder.roles.doc_worker import DocWorker
from agent_builder.roles.fact_checker import FactChecker
from agent_builder.roles.impact_analyzer import ImpactAnalyzer
from agent_builder.roles.memory_keeper import MemoryKeeper
from agent_builder.roles.proposer import Proposer
from agent_builder.roles.router import Router
from agent_builder.roles.searcher import Searcher
from agent_builder.roles.summarizer import Summarizer
from agent_builder.roles.test_runner import TestRunner
from agent_builder.tools.gatekeeper import ToolGatekeeper
from agent_builder.tools.permissions import get_default_role_perms
from agent_builder.tools.registry import registry

# 最近一次工具调用的**原始返回**（agentic 循环要把它回灌给模型）。
#
# 为什么需要：角色派发路径（``dispatch_to_role``）只把工具结果吃进自己的结果对象，
# 循环拿到的只有「列出文件完成」这类空摘要 —— 实测模型因此完全不知道列到了什么，
# 只能继续猜参数，两轮无进展就判空转停下（调研论文 agent 就是这么卡死的）。
# 这里在执行链路唯一的出口（``executor_fn``）留一份原始返回，由 routes 层取走
# 塞进 ``StepOutcome.observation``，只进提示词、不进前端。
_last_tool_result: ContextVar[str] = ContextVar("agent_builder_last_tool_result", default="")


def take_tool_observation() -> str:
    """取走最近一次工具调用的原始返回（**读取即清空**，避免串到下一轮）。"""
    value = _last_tool_result.get()
    _last_tool_result.set("")
    return value


# action → agent 角色（未命中的 action 仍走纯工具执行，行为与接入前一致）。
ACTION_ROLE_MAP: dict[str, str] = {
    "web_search": "searcher",
    "web_fetch": "doc_worker",
    "citation_check": "fact_checker",
    "code_search": "code_worker",
    "file_read": "code_worker",
    "file_list": "code_worker",
    "file_write": "code_worker",
    # 局部修改跟写文件同一归属：模型改代码时该由 code_worker 派发（不是 operator）。
    "file_edit": "code_worker",
    "summarize": "summarizer",
    "report": "summarizer",
    # 方案生成 / 影响分析类动作：让评审会（run_council）能调动这两个角色。
    "propose": "proposer",
    "optimize": "proposer",
    "impact_analyze": "impact_analyzer",
    "assess": "impact_analyzer",
    # 观测 / 验证 / 数据 / 记忆类动作：此前未映射（走纯工具执行），现派给对应角色。
    # 映射条件 = 该 action 既是注册工具、又在角色自身接受的 action 集合内；
    # 例：historian 只接受 `record` / `log_change`（均非注册工具）→ 无法派发；
    #     `sandbox_run` 被 test_runner / data_analyst / code_worker 共同接受（歧义）→ 不映射。
    "metric_collect": "auditor",
    "test_run": "test_runner",
    "data_query": "data_analyst",
    "memory_read": "memory_keeper",
    "memory_write": "memory_keeper",
    "memory_forget": "memory_keeper",
}

# 角色名 → 角色类。
_ROLE_CLASSES: dict[str, type] = {
    "searcher": Searcher,
    "doc_worker": DocWorker,
    "fact_checker": FactChecker,
    "code_worker": CodeWorker,
    "summarizer": Summarizer,
    "proposer": Proposer,
    "impact_analyzer": ImpactAnalyzer,
    "auditor": Auditor,
    "test_runner": TestRunner,
    "data_analyst": DataAnalyst,
    "memory_keeper": MemoryKeeper,
}


def _build_role(role_name: str, *, correlation_id: str, llm_client: Any) -> Any:
    """构造角色实例。

    只有部分角色具备 LLM 能力（dataclass 字段 ``llm_client``）；无该字段的角色
    （如 proposer / impact_analyzer）不传该参数，避免为「统一构造」去改已有角色定义。
    """
    role_cls = _ROLE_CLASSES[role_name]
    has_llm_field = is_dataclass(role_cls) and "llm_client" in {
        field.name for field in fields(role_cls)
    }
    if has_llm_field:
        return role_cls(correlation_id=correlation_id, llm_client=llm_client)
    return role_cls(correlation_id=correlation_id)


# 角色结果里「失败原因」的字段名并不统一（历史命名），按序兜底提取。
_DETAIL_FIELDS: tuple[str, ...] = ("error", "error_msg", "reason")


def _outcome_detail(outcome: Any) -> str:
    """提取角色结果的失败原因（字段名不统一，逐个兜底）。

    只认**非空字符串**：同名字段在不同角色里语义不同 —— ``TestReport.error``
    是「错误用例数（int）」，若被当成人话返回，失败信息会退化成 ``"1"``。
    取不到字符串时返回通用串（调用方据此判定失败）。
    """
    for name in _DETAIL_FIELDS:
        value = getattr(outcome, name, None)
        if isinstance(value, str) and value.strip():
            return value
    return "角色执行未通过"


def dispatch_to_role(
    step: Step,
    executor_fn: Callable[[Step], Any],
    *,
    correlation_id: str,
    llm_client: Any,
    role_brief_mode: str | None = None,
) -> tuple[bool, Any]:
    """按 action 把步骤派给对应 agent 角色（供执行阶段使用）。

    角色内部经 ``executor_fn`` 调真实工具（权限/沙箱/审计不变），并用注入的
    LLM 客户端完成生成/推理部分。角色返回的 status 映射回 Router 语义：
    ``pending_approval`` → PermissionError（转发审批门）；``env_failure`` →
    RuntimeError 且 ``retryable=False``（角色内已重试过，不再重派）；
    ``failed`` → RuntimeError（标记失败，透传角色自身的 ``retryable``）；
    ``rejected`` → ValueError；其余（``done`` / ``quality_report`` 等）视为成功。

    「串味」防护：``run_plan`` 里同一个 llm_client 会被传给所有角色，因此角色
    简报启用时在此**按角色派生实例**（各自带自己的简报），而不是共用一个身份；
    简报关闭时行为与接入前完全一致。

    Args:
        step: 待执行的步骤。
        executor_fn: 交给角色的执行函数（角色内部用它调真实工具）。
        correlation_id: 关联 ID（贯穿审计日志）。
        llm_client: 已注入运行时密钥的 LLM 客户端（必须可用）。
        role_brief_mode: 角色简报档位覆盖（``off`` / ``core`` / ``full``）；
            None 用环境变量。（A/B 对照按请求切档。）

    Returns:
        ``(是否已派发, 结果)``：action 未命中角色时返回 ``(False, None)``，
        由调用方回退纯工具执行。
    """
    role_name = ACTION_ROLE_MAP.get(step.action)
    if role_name is None:
        return False, None

    role_llm = llm_client
    brief = brief_for(role_name, role_brief_mode)
    if brief and hasattr(llm_client, "with_role_brief"):
        role_llm = llm_client.with_role_brief(brief, role=role_name)
    role = _build_role(role_name, correlation_id=correlation_id, llm_client=role_llm)
    outcome = role.execute(step, executor_fn=executor_fn, context=None)
    status = str(getattr(outcome, "status", "done"))
    detail = _outcome_detail(outcome)

    if status == "pending_approval":
        raise PermissionError(detail)
    if status == "env_failure":
        # 环境/权限类失败（test_runner）：角色内部已自行重试过，重派同一 inputs 无意义。
        error = RuntimeError(detail)
        error.retryable = False  # type: ignore[attr-defined]
        raise error
    if status == "failed":
        error = RuntimeError(detail)
        # 角色已判定「确定性失败」时（如 file_write 未授权覆盖）透传不可重试标记，
        # 由 Router 依据该属性跳过重派（重派同一 inputs 必然再失败）。
        error.retryable = bool(getattr(outcome, "retryable", True))  # type: ignore[attr-defined]
        raise error
    if status == "rejected":
        raise ValueError(detail)
    # 其余状态（done / quality_report 等）视为成功：quality_report 是 data_analyst
    # 「样本不足 → 只出质量报告、不下结论」的**合法产出**，不是失败。
    return True, _role_result_to_payload(outcome)


def _role_result_to_payload(outcome: Any) -> Any:
    """把角色结果转成可被前端序列化的结构（dataclass → dict）。"""
    if is_dataclass(outcome) and not isinstance(outcome, type):
        return asdict(outcome)
    return outcome


def build_step_executor(
    task_id: str,
    *,
    llm_client: Any = None,
    role_brief_mode: str | None = None,
    approved_tools: set[str] | None = None,
) -> tuple[Callable[[Step], Any], ToolGatekeeper]:
    """构造「单步执行器 + 工具门卫」——固定工作流与 agentic 循环**共用**这条执行链路。

    抽出来的目的：agentic 循环需要**逐轮**执行单步，但权限 / 沙箱 / 审计语义必须与
    ``run_plan`` 完全一致，所以两边共用本函数，而不是各写一份。

    Args:
        task_id: 任务 ID（作为 correlation_id 与 audit_id 前缀）。
        llm_client: 可选 LLM 客户端；提供时命中的 action 派给对应角色执行，
            未命中的 action 仍走纯工具执行。
        role_brief_mode: 角色简报档位覆盖（``off`` / ``core`` / ``full``）；None 用环境变量。
        approved_tools: 本次显式授权的高风险工具名集合；None / 空集 = 不授权任何高风险工具。

    Returns:
        ``(agent_executor, gatekeeper)``：前者接收 ``Step``，返回结果或抛异常；
        后者用于执行结束后取审计快照。
    """
    # 函数内懒导入 _resolve_workspace_dir 避免 routes ↔ orchestrator 循环导入。
    from agent_builder.api.routes import _resolve_workspace_dir

    role_perms = get_default_role_perms()
    gatekeeper = ToolGatekeeper(
        role_perms=role_perms,
        workspace_dir=_resolve_workspace_dir(),
        correlation_id=task_id,
    )

    def _approval_for(role: str, tool: str) -> Approval:
        """按角色与显式授权构造审批标记（高风险工具需审批且需显式授权）。"""
        perm = role_perms.get(role)
        if perm is None or not perm.is_high_risk(tool):
            return Approval()  # 非高风险：无需审批
        if approved_tools and tool in approved_tools:
            return Approval(required=True, granted_by="user")
        return Approval(required=True)  # 未授权 → 门卫拒绝

    def executor_fn(step: Step) -> Any:
        # 「臆造工具」防护：action 必须是已注册工具，否则不进入工具调用链
        # （否则会被门卫以「未列入角色白名单」拒绝——语义误导，且该失败是
        # 确定性的，重派无意义）。用不可重试错误直接上报，并列出可用工具。
        if registry.get(step.action) is None:
            raise AgentError(
                "E_VALIDATION",
                f"动作 {step.action!r} 无对应工具，无法执行"
                f"（可用工具: {', '.join(registry.list_tools())}）",
                source="orchestrator.executor_fn",
                correlation_id=task_id,
                retryable=False,
            )
        # 角色未指派时默认用 operator（17 个 allowed_tools，含 file_write）。
        role = step.assignee or "operator"
        call = ToolCall(
            audit_id=f"{task_id}-{step.id}",
            role=role,
            tool=step.action,
            args=step.inputs,
            approval=_approval_for(role, step.action),
        )
        # registry.execute 内部：门卫 check → 查注册表 → 调 impl → 回填 result。
        # 任何异常（E_PERMISSION/E_VALIDATION/E_TIMEOUT）由 Router._execute_step
        # 捕获转成 ExecutionResult.status=failed，此处不 try/except。
        executed = registry.execute(gatekeeper, call)
        # 留一份原始返回给 agentic 循环（见 take_tool_observation）。
        _last_tool_result.set("" if executed.result is None else str(executed.result))
        return executed.result

    def agent_executor(step: Step) -> Any:
        # LLM 可用时：命中的 action 交给对应 agent 角色执行（角色内部仍经
        # executor_fn 调工具，并用 LLM 完成生成部分）；未命中则回退纯工具执行。
        if llm_client is None:
            return executor_fn(step)
        dispatched, result = dispatch_to_role(
            step,
            executor_fn,
            correlation_id=task_id,
            llm_client=llm_client,
            role_brief_mode=role_brief_mode,
        )
        return result if dispatched else executor_fn(step)

    return agent_executor, gatekeeper


def run_plan(
    task_id: str,
    plan: Plan,
    steps: dict[str, Step],
    *,
    llm_client: Any = None,
    role_brief_mode: str | None = None,
    approved_tools: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """执行已确认的 Plan，返回 (execution_results, gatekeeper_audit)。

    Args:
        task_id: 任务 ID（作为 correlation_id 与 audit_id 前缀）。
        plan: 已确认的执行计划（confirmed_by_user 会被强制置 True）。
        steps: 步骤字典（step_id → Step）。
        llm_client: 可选的 LLM 客户端（由运行时密钥工厂注入）。
            提供时按 action 派发给对应 agent 角色；未命中角色的 action
            仍走纯工具执行，行为与接入前一致。None 时全部走纯工具执行。
        role_brief_mode: 角色简报档位覆盖（``off`` / ``core`` / ``full``）；
            None 用环境变量 ``AGENT_BUILDER_ROLE_BRIEF``。（A/B 对照按请求切档。）
        approved_tools: 本次执行**显式授权**的高风险工具名集合。对属所属角色
            ``high_risk_tools`` 的步骤：一律置 ``Approval(required=True)``，仅当工具
            在该集合中时才附 ``granted_by`` → 否则被门卫拒绝（E_PERMISSION）。
            None / 空集 = 不授权任何高风险工具（安全默认）。

    Returns:
        元组 (execution_results, gatekeeper_audit)：
        - execution_results: list[dict]，每项含 step_id / action / status /
          executor / summary(人话摘要) / result(str) / error / retries。
        - gatekeeper_audit: list[dict]，门卫审计快照（audit_id / role / tool /
          allowed / reason）。

    Raises:
        AgentError: 路由/校验失败，由 routes 层转 HTTP 409。
    """
    # 1~2. 门卫 + 单步执行器（与 agentic 循环共用同一条执行链路）。
    agent_executor, gatekeeper = build_step_executor(
        task_id,
        llm_client=llm_client,
        role_brief_mode=role_brief_mode,
        approved_tools=approved_tools,
    )

    # 3. 派发执行。Router 要求 confirmed_by_user=True，否则抛 E_VALIDATION。
    plan.confirmed_by_user = True
    router = Router(correlation_id=task_id)
    route_result = router.route(plan, steps, agent_executor)

    # 4. 序列化 ExecutionResult → list[dict]。
    #    summary = 人话摘要（前端默认展示）；result 转 str 保留原始机器数据（「查看原始数据」/审计）。
    execution_results: list[dict[str, Any]] = []
    for sid, r in route_result.results.items():
        step = steps.get(sid)
        action = step.action if step else ""
        execution_results.append(
            {
                "step_id": r.step_id,
                "action": action,
                "status": r.status,
                "executor": r.executor,
                "summary": summarize_result(
                    action=action,
                    status=r.status,
                    result=r.result,
                    error=r.error,
                    inputs=step.inputs if step else None,
                    retries=r.retries,
                ),
                "artifacts": artifacts_of(step, r.result) if step else [],
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


def check_execution_consistency(
    plan: Plan,
    steps: dict[str, Step],
    execution_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """「计划 ↔ 执行结果」一致性自检 —— 前端「副结构自检」开关的落地。

    纯计算，不改动执行链路，只做客观核对：
    1. 计划步骤是否都被执行（漏执行）；
    2. 是否存在未列入计划的执行结果（越界执行）；
    3. 每条结果是否都带 status；
    4. status=failed 的条目是否都带 error（不能只报失败不给原因）。

    Args:
        plan: 已确认的计划（用其 order 作为计划步骤来源之一）。
        steps: 步骤字典（step_id → Step）。
        execution_results: 执行编排器产出的结果列表。

    Returns:
        ``{"passed": bool, "checked": int, "issues": [{"kind", "detail"}]}``。
    """
    planned = set(steps) | set(plan.order)
    executed = {str(item.get("step_id")) for item in execution_results}
    issues: list[dict[str, Any]] = []

    missing = sorted(planned - executed)
    if missing:
        issues.append({"kind": "missing_execution", "detail": missing})

    extra = sorted(executed - set(steps))
    if extra:
        issues.append({"kind": "unplanned_execution", "detail": extra})

    without_status = sorted(
        str(item.get("step_id")) for item in execution_results if not item.get("status")
    )
    if without_status:
        issues.append({"kind": "missing_status", "detail": without_status})

    failed_without_error = sorted(
        str(item.get("step_id"))
        for item in execution_results
        if item.get("status") == "failed" and not item.get("error")
    )
    if failed_without_error:
        issues.append({"kind": "failed_without_error", "detail": failed_without_error})

    return {"passed": not issues, "checked": len(execution_results), "issues": issues}


__all__ = ["build_step_executor", "check_execution_consistency", "run_plan"]
